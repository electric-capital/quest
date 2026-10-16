"""The ``create_table`` action request: handler, pre-card arm and wiring.

Runs against the isolated ``env`` fixture of tests/test_table_service.py
(tmp SQLite file, TABLES_DIR, ``tables`` gate open, alice owning a private
and a public project, realtime events captured). Covers:

1. ``CreateTableHandler.validate_params``: defaults, shape errors, unknown
   keys, server-injected preview keys dropped.
2. Pre-card refusals: gate closed (before any DB work), public
   conversation, Slack run, target project without a project, bad column
   specs (code + column named), duplicate title (case-insensitive, same
   scope only), table cap; success injection (no column key anywhere).
3. ``execute``: user and project tables (owner = proposer, private, change
   #1 "Created table" with ``action_request:<id>`` source and the approver
   as actor, ``last_write_source``), approve-time collision TOCTOU (no
   second table, no orphan directory), owner attribution through
   ``get_action_request_owners``.
4. Preview fields, summary snippet, labels, registration, the request-id
   route frozenset, ``_PUBLIC_BLOCKED_LOOP_TOOLS`` unchanged.
5. End to end through the ``create_action_request`` dispatch arm and the
   resolve route.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

from chat.tables import service, storage
from chat.tables.access import DENY_SCHEMA_PUBLIC, DENY_SCHEMA_SLACK
from chat.tables.constants import tables_disabled_message
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    convo,
    env,
    table_dir,
    ui,
    uid,
)

COLUMNS = [
    {"name": "Name", "type": "text", "required": True},
    {"name": "Price", "type": "number", "precision": 2},
    {"name": "Status", "type": "enum", "options": ["open", "done"], "default": "open"},
]
INJECTED = ("table_mode", "project_title", "column_specs")


def _run(coro):
    return asyncio.run(coro)


def handler():
    from chat.action_request_types.create_table import CreateTableHandler
    return CreateTableHandler()


def params(title="Inventory", **extra):
    return {"title": title, "columns": [dict(c) for c in COLUMNS], **extra}


def precard(validated, caller, *, is_slack_origin=False):
    from chat.action_request_types.table_precard import table_precard_check

    _run(table_precard_check(
        "create_table", validated, caller.user, caller.project_id,
        caller.conversation_id, is_slack_origin=is_slack_origin,
    ))
    return validated


def propose(caller, raw, **kwargs):
    return precard(handler().validate_params(raw), caller, **kwargs)


def execute(validated, caller, request_id=7):
    return _run(handler().execute(
        validated, caller.user,
        conversation_id=caller.conversation_id,
        project_id=caller.project_id,
        request_id=request_id,
    ))


def refusal(caller, raw, **kwargs) -> str:
    with pytest.raises(ValueError) as exc:
        propose(caller, raw, **kwargs)
    return str(exc.value)


def all_tables(env):
    return _run(env.store.list_table_ids_for_user(uid(env, "alice")))


def has_key_anywhere(value) -> bool:
    if isinstance(value, dict):
        return "key" in value or any(has_key_anywhere(v) for v in value.values())
    if isinstance(value, list):
        return any(has_key_anywhere(v) for v in value)
    return False


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


class TestValidateParams:
    def test_defaults(self):
        assert handler().validate_params(params()) == {
            "title": "Inventory", "description": "", "target": "user",
            "columns": COLUMNS,
        }

    def test_explicit_values_and_nulls(self):
        out = handler().validate_params(params(description="d", target="project"))
        assert (out["description"], out["target"]) == ("d", "project")
        out = handler().validate_params(params(description=None, target=None))
        assert (out["description"], out["target"]) == ("", "user")

    @pytest.mark.parametrize("raw, match", [
        ({"columns": COLUMNS}, "Missing required parameter: title"),
        (params(title=5), "title must be a string"),
        (params(title="   "), "title must be a non-empty string"),
        (params(description=3), "description must be a string"),
        (params(target="team"), "Invalid target 'team'"),
        (params(target=["user"]), "Invalid target"),
        ({"title": "T"}, "Missing required parameter: columns"),
        ({"title": "T", "columns": {"Name": "text"}}, "columns must be a list"),
        ({"title": "T", "columns": []}, "at least one column"),
        ({"title": "T", "columns": [{"name": "a", "type": "text"}, "b"]},
         r"columns\[1\] must be an object"),
        ({"title": "T", "columns": [{"name": f"c{i}", "type": "text"} for i in range(101)]},
         "at most 100 columns"),
    ])
    def test_shape_errors(self, raw, match):
        with pytest.raises(ValueError, match=match):
            handler().validate_params(raw)

    def test_non_dict(self):
        with pytest.raises(ValueError, match="params must be an object"):
            handler().validate_params(["x"])

    @pytest.mark.parametrize("extra", [{"table_id": "t"}, {"owner": 1}, {"mode": "public"}])
    def test_unknown_keys(self, extra):
        with pytest.raises(ValueError, match="Unknown parameter for create_table"):
            handler().validate_params(params(**extra))

    def test_injected_keys_dropped(self):
        out = handler().validate_params(params(
            table_mode="public", project_title="X", column_specs=[{"key": "c_1"}],
        ))
        assert not set(INJECTED) & set(out)


# ---------------------------------------------------------------------------
# Pre-card
# ---------------------------------------------------------------------------


class TestPrecard:
    def test_gate_closed_before_any_db_work(self, env, monkeypatch):
        import chat.action_request_types.table_precard as table_precard
        import db.project_store as project_store

        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        for name in ("get_project",):
            monkeypatch.setattr(project_store, name, boom)
        for name in ("title_taken", "list_table_ids_for_user", "create_table"):
            monkeypatch.setattr(env.store, name, boom)
        monkeypatch.setattr(table_precard, "project_is_public", boom)
        for caller in (convo(env), convo(env, project=env.private_project)):
            assert refusal(caller, params(target="project")) == (
                f"tables_disabled: {tables_disabled_message()}"
            )

    def test_public_conversation_denied(self, env):
        caller = convo(env, project=env.public_project, public=True)
        assert refusal(caller, params(target="project")) == f"denied: {DENY_SCHEMA_PUBLIC}"
        assert refusal(caller, params()) == f"denied: {DENY_SCHEMA_PUBLIC}"

    def test_slack_denied(self, env):
        assert refusal(convo(env), params(), is_slack_origin=True) == (
            f"denied: {DENY_SCHEMA_SLACK}"
        )

    def test_target_project_without_project(self, env):
        msg = refusal(convo(env), params(target="project"))
        assert msg.startswith("invalid_args: target 'project' needs a project conversation")

    def test_foreign_project(self, env):
        caller = convo(env, "bob", project=env.private_project)
        assert refusal(caller, params(target="project")) == (
            "project_not_found: The conversation's project was not found."
        )

    def test_title_and_description_checks(self, env):
        assert refusal(convo(env), params(title="x" * 201)).startswith(
            "invalid_request: Table title exceeds maximum length of 200",
        )
        assert refusal(convo(env), params(description="d" * 501)).startswith(
            "invalid_request: Table description exceeds",
        )

    @pytest.mark.parametrize("columns, code, named", [
        ([{"name": "A", "type": "text"}, {"name": "a", "type": "number"}],
         "duplicate_name", "'a'"),
        ([{"name": "A", "type": "time"}], "invalid_type", "'A'"),
        ([{"name": "A", "type": "text", "key": "c_12345678"}], "unknown_field", "'A'"),
        ([{"name": "Flag", "type": "boolean", "unique": True}], None, "'Flag'"),
        ([{"name": "E", "type": "enum"}], None, "'E'"),
        ([{"name": "A", "type": "text", "colour": "red"}], "unknown_field", "'A'"),
        ([{"type": "text"}], "missing_field", None),
    ])
    def test_bad_columns(self, env, columns, code, named):
        msg = refusal(convo(env), {"title": "T", "columns": columns})
        if code is not None:
            assert msg.startswith(f"{code}: ")
        else:
            assert msg.split(": ", 1)[0].isidentifier()
        if named is not None:
            assert named in msg.split(": ", 1)[1]
        assert all_tables(env) == []

    def test_duplicate_title_same_scope_only(self, env):
        _run(service.create_table(ui(env), title="Inventory", columns=COLUMNS))
        _run(service.create_table(
            ui(env), title="Projected", columns=COLUMNS, project_id=env.private_project,
        ))
        _run(service.create_table(
            ui(env), title="Opened", columns=COLUMNS, project_id=env.public_project,
        ))
        assert refusal(convo(env), params(title=" inVENtory ")) == (
            "duplicate_title: A table titled 'inVENtory' already exists."
        )
        in_project = convo(env, project=env.private_project)
        assert refusal(in_project, params(title="PROJECTED", target="project")) == (
            "duplicate_title: A table titled 'PROJECTED' already exists in this project."
        )
        # Another scope never collides: a user table vs a project table, and
        # a private project table vs the public project's table.
        propose(convo(env), params(title="Projected"))
        propose(in_project, params(title="Inventory", target="project"))
        propose(in_project, params(title="Opened", target="project"))
        # Another owner never collides.
        propose(convo(env, "bob"), params(title="Inventory"))

    def test_collision_checked_before_cap(self, env, monkeypatch):
        import chat.action_request_types.table_precard as table_precard

        monkeypatch.setattr(table_precard, "TABLE_MAX_PER_OWNER", 1)
        _run(service.create_table(ui(env), title="Inventory", columns=COLUMNS))
        assert refusal(convo(env), params()).startswith("duplicate_title: ")

    def test_table_limit(self, env, monkeypatch):
        import chat.action_request_types.table_precard as table_precard

        monkeypatch.setattr(table_precard, "TABLE_MAX_PER_OWNER", 1)
        _run(service.create_table(ui(env), title="One", columns=COLUMNS))
        assert refusal(convo(env), params()).startswith(
            "table_limit: Table limit reached: an owner can have at most 1 tables",
        )

    def test_user_table_injection(self, env):
        out = propose(convo(env), params(title="  Inventory ", description=" d "))
        assert (out["title"], out["description"]) == ("Inventory", "d")
        assert out["table_mode"] == "private"
        assert out["project_title"] is None
        assert out["column_specs"] == [
            {"name": "Name", "type": "text", "required": True, "unique": False,
             "default": None, "options": None, "multiple": False, "precision": None,
             "description": ""},
            {"name": "Price", "type": "number", "required": False, "unique": False,
             "default": None, "options": None, "multiple": False, "precision": 2,
             "description": ""},
            {"name": "Status", "type": "enum", "required": False, "unique": False,
             "default": "open", "options": ["open", "done"], "multiple": False,
             "precision": None, "description": ""},
        ]
        assert not has_key_anywhere(out)
        json.dumps(out)

    def test_project_table_injection(self, env):
        out = propose(convo(env, project=env.private_project), params(target="project"))
        assert (out["table_mode"], out["project_title"]) == ("private", "Research")

    def test_user_table_from_project_conversation(self, env):
        out = propose(convo(env, project=env.private_project), params())
        assert (out["target"], out["project_title"]) == ("user", None)

    def test_mode_mismatch_is_fail_closed(self, env, monkeypatch):
        # Unreachable through the deny check (public conversations are
        # refused first); force it by making the deny check pass.
        import chat.action_request_types.table_precard as table_precard

        monkeypatch.setattr(table_precard, "create_table_deny_reason", lambda **_k: None)
        caller = convo(env, project=env.public_project, public=True)
        assert refusal(caller, params(target="project")).startswith("mode_mismatch: ")

    def test_unknown_type_rejected(self, env):
        from chat.action_request_types.table_precard import table_precard_check

        with pytest.raises(ValueError, match="Not a table request type"):
            _run(table_precard_check("write_doc", {}, env.users["alice"], None, None))

    def test_lazy_arms_imported_only_when_requested(self, env, monkeypatch):
        import importlib

        import chat.action_request_types.table_precard as table_precard

        seen = []
        real = importlib.import_module

        def spy(name, *a, **k):
            seen.append(name)
            return real(name, *a, **k)

        monkeypatch.setattr(table_precard.importlib, "import_module", spy)
        propose(convo(env), params())
        assert seen == []


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


class TestExecute:
    def _assert_created(self, env, result, *, request_id, project_id=None):
        table = _run(env.store.get_table(result["table_id"]))
        assert table["owner_id"] == uid(env, "alice")
        assert table["mode"] == "private"
        assert table["project_id"] == project_id
        assert table["shares"] == []
        assert table["last_write_source"] == f"action_request:{request_id}"
        changes = storage.list_changes(table_dir(env, table))
        assert len(changes) == 1
        change = storage.get_change(table_dir(env, table), changes[0]["id"])
        assert change["id"] == 1
        assert change["summary"] == "Created table"
        assert change["source"] == f"action_request:{request_id}"
        assert change["actor_user_id"] == uid(env, "alice")
        return table

    def test_user_table(self, env):
        caller = convo(env)
        result = execute(propose(caller, params(description="Stock")), caller, request_id=41)
        assert result == {
            "table_id": result["table_id"], "title": "Inventory", "mode": "private",
            "scope": "user", "project_id": None,
            "columns": [{"name": "Name", "type": "text"},
                        {"name": "Price", "type": "number"},
                        {"name": "Status", "type": "enum"}],
            "schema_version": 1,
        }
        table = self._assert_created(env, result, request_id=41)
        assert table["description"] == "Stock"

    def test_project_table(self, env):
        caller = convo(env, project=env.private_project)
        result = execute(propose(caller, params(target="project")), caller, request_id=42)
        assert (result["scope"], result["project_id"]) == ("project", env.private_project)
        self._assert_created(env, result, request_id=42, project_id=env.private_project)

    def test_user_target_from_project_conversation(self, env):
        caller = convo(env, project=env.private_project)
        result = execute(propose(caller, params()), caller)
        assert result["project_id"] is None and result["scope"] == "user"

    def test_toctou_duplicate_title(self, env):
        caller = convo(env)
        validated = propose(caller, params())
        _run(service.create_table(ui(env), title="INVENTORY", columns=COLUMNS))
        before = sorted(p.name for p in env.dirs["tables"].iterdir())
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value) == (
            "duplicate_title: A table titled 'Inventory' already exists."
        )
        assert len(all_tables(env)) == 1
        assert sorted(p.name for p in env.dirs["tables"].iterdir()) == before

    def test_mode_mismatch_at_approve(self, env, monkeypatch):
        # The service's own taint check: a context claiming to be private
        # can never create a table in a public project.
        import chat.action_request_types.table_precard as table_precard

        async def private(*_a, **_k):
            return False

        monkeypatch.setattr(table_precard, "project_is_public", private)
        caller = convo(env, project=env.public_project)
        with pytest.raises(RuntimeError) as exc:
            execute(handler().validate_params(params(target="project")), caller)
        assert str(exc.value).startswith("mode_mismatch: ")
        assert all_tables(env) == []
        assert list(env.dirs["tables"].iterdir()) == []

    @pytest.mark.parametrize("tamper", [
        lambda p: p["columns"].append({"name": "Extra", "type": "text"}),
        lambda p: p["columns"][1].update(precision=4),
        lambda p: p["column_specs"][0].update(required=False),
    ])
    def test_columns_differ_from_card(self, env, tamper):
        caller = convo(env)
        validated = propose(caller, params())
        tamper(validated)
        with pytest.raises(RuntimeError, match="^stale_request: "):
            execute(validated, caller)
        assert all_tables(env) == []

    def test_gate_closed_at_approve(self, env):
        caller = convo(env)
        validated = propose(caller, params())
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)
        with pytest.raises(RuntimeError) as exc:
            execute(validated, caller)
        assert str(exc.value) == f"tables_disabled: {tables_disabled_message()}"
        assert all_tables(env) == []

    def test_public_context_refused_at_approve(self, env):
        caller = convo(env, project=env.public_project, public=True)
        with pytest.raises(RuntimeError) as exc:
            execute(handler().validate_params(params(target="project")), caller)
        assert str(exc.value) == f"denied: {DENY_SCHEMA_PUBLIC}"
        assert all_tables(env) == []

    def test_project_gone_at_approve(self, env):
        import db.project_store as project_store

        caller = convo(env, project=env.private_project)
        validated = propose(caller, params(target="project"))
        _run(project_store.delete_project(uid(env, "alice"), env.private_project))
        with pytest.raises(RuntimeError, match="^project_not_found: "):
            execute(validated, caller)

    def test_bad_columns_at_approve(self, env):
        caller = convo(env)
        validated = handler().validate_params(
            {"title": "T", "columns": [{"name": "A", "type": "time"}]},
        )
        with pytest.raises(RuntimeError, match="^invalid_type: "):
            execute(validated, caller)

    def test_request_id_required(self, env):
        caller = convo(env)
        with pytest.raises(RuntimeError, match="^invalid_args: "):
            execute(propose(caller, params()), caller, request_id=None)
        assert all_tables(env) == []

    @pytest.mark.parametrize("bad", [
        {}, {"title": 5, "columns": []}, {"title": "T", "columns": "x"},
        {"title": "T", "columns": [], "target": "team"},
    ])
    def test_malformed_params(self, env, bad):
        with pytest.raises(RuntimeError, match="^invalid_request: Malformed"):
            execute(bad, convo(env))

    def test_owner_attribution(self, env):
        from db import action_request_store

        from db import conversation_store

        caller = convo(env)
        _run(conversation_store.create_conversation(
            user_id=uid(env, "alice"), conversation_id=caller.conversation_id,
            created_at=datetime.now(timezone.utc),
        ))
        request_id = _run(action_request_store.create_action_request(
            uid(env, "alice"), caller.conversation_id, "create_table", params(), "r",
        ))["id"]
        result = execute(propose(caller, params()), caller, request_id=request_id)
        table = _run(env.store.get_table(result["table_id"]))
        source = table["last_write_source"]
        rid = int(source.split(":", 1)[1])
        owners = _run(action_request_store.get_action_request_owners([rid]))
        assert owners == {request_id: uid(env, "alice")}


# ---------------------------------------------------------------------------
# Preview, labels, registration
# ---------------------------------------------------------------------------


def _preview(p):
    return _run(handler().render_preview(p))


class TestPreviewAndLabels:
    def test_user_table_fields(self, env):
        fields = _preview(propose(convo(env), params(description="Stock")))
        assert [f["key"] for f in fields] == ["Title", "Description", "Mode", "Scope", "Columns"]
        assert fields[0] == {"key": "Title", "value": "Inventory"}
        assert fields[1] == {"key": "Description", "value": "Stock"}
        assert fields[2] == {"key": "Mode", "value": "private"}
        assert fields[3] == {"key": "Scope", "value": "user"}
        columns = fields[4]
        assert columns["type"] == "table_columns"
        assert columns["value"] == "3 columns: Name (text), Price (number), Status (enum)"
        assert [c["name"] for c in columns["columns"]] == ["Name", "Price", "Status"]
        assert not has_key_anywhere(columns["columns"])
        assert all(isinstance(f["value"], str) for f in fields)

    def test_project_scope_and_no_description(self, env):
        fields = _preview(propose(convo(env, project=env.private_project), params(target="project")))
        assert [f["key"] for f in fields] == ["Title", "Mode", "Scope", "Project", "Columns"]
        assert fields[2] == {"key": "Scope", "value": "project"}
        assert fields[3] == {"key": "Project", "value": "Research"}

    def test_fallback_without_injected_keys(self):
        fields = _preview({
            "title": "T", "target": "project",
            "columns": [{"name": "A", "type": "text", "key": "c_12345678"}],
        })
        assert fields[1] == {"key": "Scope", "value": "project"}
        assert fields[-1]["value"] == "1 column: A (text)"
        assert fields[-1]["columns"] == [{"name": "A", "type": "text"}]

    @pytest.mark.parametrize("p", [
        {}, {"title": 5, "columns": "x"}, {"column_specs": ["junk", 3]},
        {"columns": [None, {"name": "A"}]}, {"target": ["x"], "table_mode": 7},
    ])
    def test_odd_params_never_raise(self, p):
        fields = _preview(p)
        assert fields and all(isinstance(f["value"], str) for f in fields)
        json.dumps(fields)

    @pytest.mark.parametrize("p, expected", [
        (params(), "Inventory (3 columns)"),
        ({"title": "T", "columns": [{}]}, "T (1 column)"),
        ({"title": "T", "column_specs": [{}, {}]}, "T (2 columns)"),
        ({"title": "T"}, "T"),
        ({"columns": [{}]}, "(1 column)"),
        ({"title": 5, "columns": 3}, ""),
        ({}, ""),
    ])
    def test_summary_snippet(self, p, expected):
        assert handler().summary_snippet(p) == expected

    def test_labels_and_registration(self):
        from chat.action_request_types import get_handler, get_summary_snippet
        from chat.action_request_types.create_table import CreateTableHandler
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM
        from db.models import ActionRequestType

        h = handler()
        assert (h.display_name, h.approve_label, h.resolved_label) == (
            "Create Table", "Create", "Created",
        )
        assert isinstance(get_handler("create_table"), CreateTableHandler)
        assert get_handler("create_table").type_name == ActionRequestType.CREATE_TABLE
        assert "create_table" in ACTION_REQUEST_TYPE_ENUM
        assert get_summary_snippet("create_table", params()) == "Inventory (3 columns)"

    def test_request_id_types(self):
        from chat.action_request_routes import REQUEST_ID_TYPES

        assert REQUEST_ID_TYPES == frozenset({
            "write_doc", "create_table", "change_table_schema", "write_table_rows",
        })

    def test_public_blocked_loop_tools_unchanged(self):
        from chat.gemini_api.turn_tools import (
            _PUBLIC_BLOCKED_LOOP_TOOLS,
            WAIT_FOR_HANDLES_KEY,
        )

        assert _PUBLIC_BLOCKED_LOOP_TOOLS == frozenset({
            "agent_task",
            "agent_task_parallel",
            "agent_task_parallel_template",
            "create_action_request",
            "send_slack_reply_and_get_response",
            "return_to_caller",
            "return_final_response",
            WAIT_FOR_HANDLES_KEY,
        })


# ---------------------------------------------------------------------------
# End to end: create_action_request arm -> card -> resolve route
# ---------------------------------------------------------------------------


@pytest.fixture()
def ar_env(env, monkeypatch):
    """env + the wait-handle store on the same DB."""
    import db.action_request_store as action_request_store
    import db.tool_wait_handle_store as tool_wait_handle_store
    from chat.storage import ChatStorage
    from chat.wait_handles import resume as wait_resume

    monkeypatch.setattr(
        tool_wait_handle_store, "AsyncSessionLocal", env.store.AsyncSessionLocal,
    )
    monkeypatch.setattr(
        ChatStorage, "update_action_request_message",
        staticmethod(lambda **kwargs: None),
    )
    monkeypatch.setattr(wait_resume, "maybe_kick_resume", lambda *a, **k: None)
    env.action_request_store = action_request_store
    return env


def _arm(caller, p):
    from chat.gemini_api.turn_tools import TurnState, _handle_create_action_request
    from tests.test_turn_tools_registry import _call, _make_ctx

    ctx, _events = _make_ctx(
        user=caller.user, conversation_id=caller.conversation_id,
        project_id=caller.project_id,
    )
    call = _call("create_action_request", {
        "request_type": "create_table", "params": p, "reasoning": "a table",
    })
    return _run(_handle_create_action_request(ctx, call, TurnState()))


class _Req:
    app = None


class TestEndToEnd:
    def test_arm_refusals(self, env):
        assert json.loads(_arm(convo(env), params(target="project"))) == {"error": (
            "Invalid parameters: invalid_args: target 'project' needs a project "
            "conversation; use target 'user' for a table of your own."
        )}
        _run(service.create_table(ui(env), title="Inventory", columns=COLUMNS))
        assert json.loads(_arm(convo(env), params())) == {"error": (
            "Invalid parameters: duplicate_title: A table titled 'Inventory' already exists."
        )}

    def test_card_then_approve(self, ar_env):
        from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request
        from chat.gemini_api.turn_tools import SuspendForActionRequest
        from db import conversation_store

        caller = convo(ar_env, project=ar_env.private_project)
        _run(conversation_store.create_conversation(
            user_id=caller.user["id"], conversation_id=caller.conversation_id,
            created_at=datetime.now(timezone.utc), project_id=caller.project_id,
        ))
        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, params(target="project", table_mode="public"))
        request_id = suspended.value.request_id

        row = _run(ar_env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["request_type"] == "create_table" and row["status"] == "open"
        assert row["params"]["table_mode"] == "private"
        assert row["params"]["project_title"] == "Research"
        assert not has_key_anywhere(row["params"])
        assert all_tables(ar_env) == []

        resolved = _run(resolve_user_action_request(
            request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
        ))
        assert resolved["status"] == "executed"
        assert resolved["resolved_label"] == "Created"
        assert resolved["summary_snippet"] == "Inventory (3 columns)"
        table = _run(ar_env.store.get_table(resolved["result"]["table_id"]))
        assert table["project_id"] == ar_env.private_project
        assert table["last_write_source"] == f"action_request:{request_id}"

    def test_approve_collision_keeps_request_open(self, ar_env):
        from fastapi import HTTPException

        from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request
        from chat.gemini_api.turn_tools import SuspendForActionRequest
        from db import conversation_store

        caller = convo(ar_env)
        _run(conversation_store.create_conversation(
            user_id=caller.user["id"], conversation_id=caller.conversation_id,
            created_at=datetime.now(timezone.utc),
        ))
        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, params())
        request_id = suspended.value.request_id
        _run(service.create_table(ui(ar_env), title="inventory", columns=COLUMNS))
        with pytest.raises(HTTPException) as exc:
            _run(resolve_user_action_request(
                request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
            ))
        assert exc.value.status_code == 500
        assert exc.value.detail["message"] == (
            "Failed to execute request: duplicate_title: A table titled "
            "'Inventory' already exists."
        )
        row = _run(ar_env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["status"] == "open"
        assert len(all_tables(ar_env)) == 1
