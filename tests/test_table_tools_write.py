"""Quest Tables row write tools (phase A10): insert_rows, update_rows,
upsert_rows, delete_rows, and the write_table_rows params shape
(chat/tables/write_request.py).

Driven through ``_dispatch_tool_call(tool_call ...)`` (the real
TOOL_CALL_HANDLERS entries and ``tool_dispatch._table_caller``) against the
isolated ``env`` fixture of tests/test_table_service.py.

Covers:

1. Registry entries, mutating roster, allowlists, inference-API dispatch
   rejection, the script bridge and restricted sandbox leases.
2. Every cell of the spec 5.1 rows matrix through the tools, for every
   operation: free / approval_required / denied / not found, with nothing
   written on a refusal.
3. The suggested_request round trip: validate_write_request_params, then
   apply_row_write(bypass_approval=True) gives the free path's table state.
4. Attribution: change source ``conversation:<id>``, actor the user.
5. Atomic validation (invalid_rows), caps (too_many_rows), update with an
   unknown _id, upsert key errors, delete by ids and by filter, argument
   shapes, no column key in any output, gate closed.
6. write_request unit tests.
"""

import asyncio
import json
import uuid

import pytest

from chat.tables import service, storage
from chat.tables.access import (
    APPROVAL_REQUIRED_BY_OWNER_MESSAGE,
    APPROVAL_REQUIRED_MESSAGE,
    DENY_INFERENCE_API,
    DENY_PUBLIC_APPROVAL_REQUIRED,
    DENY_PUBLIC_TABLE_FROM_PRIVATE,
    DENY_READ_ONLY_SHARE,
    DENY_SCRIPT,
    DENY_SLACK_APPROVAL_REQUIRED,
    DENY_SLACK_NEEDS_APPROVAL,
    DENY_SUB_AGENT,
    DENY_USER_SUBAGENT,
)
from chat.tables.constants import (
    TABLE_WRITE_MAX_ROWS,
    TABLES_SERVICE_KEY,
    table_not_found_message,
    tables_disabled_message,
)
from chat.tables.schema import load_schema
from chat.tables.service import Caller
from chat.tables.write_request import (
    WriteRequestError,
    build_write_request_params,
    validate_write_request_params,
)
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    env,
    make_table,
    ui,
)

WRITE_TOOLS = ("insert_rows", "update_rows", "upsert_rows", "delete_rows")
TOOL_OP = {"insert_rows": "insert", "update_rows": "update",
           "upsert_rows": "upsert", "delete_rows": "delete"}
SEED = [
    {"Name": "Apple", "Price": "1", "SKU": "S1"},
    {"Name": "Pear", "Price": "2", "SKU": "S2"},
]


def _run(coro):
    return asyncio.run(coro)


def _dispatch_raw(env, tool, arguments, *, who="alice", cid="new", **kwargs) -> str:
    from chat.gemini_api.tool_dispatch import _dispatch_tool_call

    if cid == "new":
        cid = str(uuid.uuid4())
    if cid is not None:
        (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    result, extra = _run(_dispatch_tool_call(
        app=None, provider=None, user=env.users[who],
        conversation_id=cid, timezone="UTC",
        tool_name="tool_call",
        args={"tool_name": tool, "arguments": dict(arguments)},
        **kwargs,
    ))
    assert extra == []
    return result


def _dispatch(env, tool, arguments, **kwargs) -> dict:
    return json.loads(_dispatch_raw(env, tool, arguments, **kwargs))


def _seed(env, table, rows=SEED):
    owner = next(n for n, u in env.users.items() if u["id"] == table["owner_id"])
    _run(service.apply_row_write(ui(env, owner), table["id"], "insert", {"rows": list(rows)}))
    return _run(env.store.get_table(table["id"], with_shares=True))


def _args(tool, table_id) -> dict:
    """Valid arguments for each tool against a SEED-ed table."""
    if tool == "insert_rows":
        return {"table_id": table_id, "rows": [{"Name": "Plum", "SKU": "S3"}]}
    if tool == "update_rows":
        return {"table_id": table_id, "updates": [{"_id": 1, "Price": 7}]}
    if tool == "upsert_rows":
        return {"table_id": table_id, "key_column": "SKU", "rows": [
            {"SKU": "S2", "Price": 9}, {"SKU": "S4", "Name": "Fig"},
        ]}
    return {"table_id": table_id, "row_ids": [2]}


def _tdir(env, table):
    return env.dirs["tables"] / table["id"]


def _state(env, table):
    """Rows by name, without timestamps (comparable across tables)."""
    fresh = _run(env.store.get_table(table["id"]))
    names = {c.key: c.name for c in load_schema(fresh["schema"])}
    return [
        {"_id": r["_id"], **{names[k]: v for k, v in r.items() if k in names}}
        for r in storage.dump_rows(_tdir(env, fresh), load_schema(fresh["schema"]))
    ]


def _snapshot(env, table):
    """What a refused write must leave alone: table.db rows and change
    count, quest.db row_count and updated_at."""
    fresh = _run(env.store.get_table(table["id"]))
    return (
        _state(env, table),
        len(storage.list_changes(_tdir(env, table))),
        storage.row_count(_tdir(env, table)),
        fresh["row_count"],
        fresh["updated_at"],
    )


def _set_require_approval(env, table, value=True):
    _run(env.store.update_table_metadata(table["id"], require_approval=value))


@pytest.fixture()
def world(env):
    """alice owns: ``mine`` (user, unshared), ``read_shared`` / ``write_shared``
    (user, shared with bob), ``open`` (public project, unshared),
    ``open_write`` / ``open_read`` (public project, shared with bob). Each
    holds the two SEED rows."""
    from db import project_store

    t = {
        "mine": make_table(env, "Mine"),
        "read_shared": make_table(env, "Read shared", shares=[("bob", "read")]),
        "write_shared": make_table(env, "Write shared", shares=[("bob", "write")]),
        "open": make_table(env, "Open", project_id=env.public_project),
        "open_write": make_table(env, "Open write", project_id=env.public_project,
                                 shares=[("bob", "write")]),
        "open_read": make_table(env, "Open read", project_id=env.public_project,
                                shares=[("bob", "read")]),
    }
    env.t = {key: _seed(env, table) for key, table in t.items()}
    _run(project_store.set_doc_source_projects(
        env.users["alice"]["id"], env.private_project, [env.public_project],
    ))
    env.published.clear()
    return env


def _kw(world, kind):
    return {
        "private": {},
        "public": {"project_id": world.public_project, "is_public": True},
        "doc_source": {"project_id": world.private_project},
        "slack": {"is_slack": True},
    }[kind]


# ---------------------------------------------------------------------------
# 1. Registry, rosters, dispatch-level refusals
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_entries_handlers_and_flags(self):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import (
            PUBLIC_TOOL_CALL_ALLOWLIST,
            TOOL_CALL_REGISTRY,
        )

        for name in WRITE_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["name"] == name
            assert spec["mutating"] is True
            assert spec["requires_service"] == TABLES_SERVICE_KEY
            assert spec["skill"] == "system:tables"
            assert name in TOOL_CALL_HANDLERS
            assert name in PUBLIC_TOOL_CALL_ALLOWLIST
            assert name not in SCRIPT_TOOL_CALL_ALLOWLIST

    def test_parameters(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        def params(name):
            return TOOL_CALL_REGISTRY[name]["parameters"]

        assert params("insert_rows")["required"] == ["table_id", "rows"]
        assert params("update_rows")["required"] == ["table_id", "updates"]
        assert params("upsert_rows")["required"] == ["table_id", "key_column", "rows"]
        assert params("delete_rows")["required"] == ["table_id"]
        assert {"row_ids", "filter"} <= set(params("delete_rows")["properties"])

    def test_descriptions_are_public_prompt_safe(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        from tests.test_docs_tools import _PUBLIC_PROMPT_FORBIDDEN

        for name in WRITE_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            text = json.dumps([spec["description"], spec["parameters"]])
            for term in _PUBLIC_PROMPT_FORBIDDEN:
                assert term not in text, (name, term)
            assert "suggested_request" in spec["description"]

    def test_mutating_roster_has_exactly_the_row_writes(self):
        from chat.gemini_api.system_prompt import _table_tool_names
        from chat.llm.tool_schemas import mutating_tool_call_tools

        assert _table_tool_names() & mutating_tool_call_tools() == frozenset(WRITE_TOOLS)

    @pytest.mark.parametrize("tool", WRITE_TOOLS)
    def test_inference_api_dispatch_rejects(self, world, tool):
        before = _snapshot(world, world.t["mine"])
        got = _dispatch(world, tool, _args(tool, world.t["mine"]["id"]),
                        is_inference_api=True)
        assert "not available in inference API runs" in got["error"]
        assert _snapshot(world, world.t["mine"]) == before

    @pytest.mark.parametrize("tool", WRITE_TOOLS)
    def test_script_bridge_and_restricted_lease(self, world, tool, monkeypatch):
        import chat.gemini_api.script_tool_call as bridge
        from chat.sandbox_tokens import SandboxLease

        alice = world.users["alice"]
        args = _args(tool, world.t["mine"]["id"])
        lease = SandboxLease(user_id=alice["id"], conversation_id="c",
                             expires_at=1e12, block_mutating_tools=True)
        before = _snapshot(world, world.t["mine"])

        def call(lease_):
            return _run(bridge.script_tool_call_endpoint(
                bridge.ScriptToolCallRequest(tool_name=tool, arguments=args),
                user=alice, lease=lease_,
            ))

        # Not on the script allowlist at all.
        for lease_ in (None, lease):
            assert call(lease_).status_code == 400
        # Defense in depth: even if allowlisted, a restricted lease blocks it.
        monkeypatch.setattr(
            bridge, "SCRIPT_TOOL_CALL_ALLOWLIST",
            bridge.SCRIPT_TOOL_CALL_ALLOWLIST | {tool},
        )
        blocked = call(lease)
        assert blocked.status_code == 403
        assert "read-only" in json.loads(blocked.body)["error"]
        # And without the lease the access rule denies a script.
        assert json.loads(call(None).body) == {"error": "denied", "message": DENY_SCRIPT}
        assert _snapshot(world, world.t["mine"]) == before


# ---------------------------------------------------------------------------
# 2. The matrix, through the tools
# ---------------------------------------------------------------------------

# (who, conversation kind, table, require_approval, expected, message)
MATRIX = [
    ("alice", "private", "mine", False, "free", None),
    ("alice", "private", "write_shared", False, "approval", APPROVAL_REQUIRED_MESSAGE),
    ("alice", "private", "read_shared", False, "approval", APPROVAL_REQUIRED_MESSAGE),
    ("bob", "private", "read_shared", False, "denied", DENY_READ_ONLY_SHARE),
    ("bob", "private", "write_shared", False, "approval", APPROVAL_REQUIRED_MESSAGE),
    ("alice", "public", "mine", False, "not_found", None),
    ("alice", "public", "write_shared", False, "not_found", None),
    ("bob", "public", "write_shared", False, "not_found", None),
    ("alice", "doc_source", "open", False, "denied", DENY_PUBLIC_TABLE_FROM_PRIVATE),
    ("alice", "public", "open", False, "free", None),
    ("alice", "public", "open_write", False, "free", None),
    ("bob", "public", "open_write", False, "free", None),
    ("bob", "public", "open_read", False, "denied", DENY_READ_ONLY_SHARE),
    ("alice", "private", "mine", True, "approval", APPROVAL_REQUIRED_BY_OWNER_MESSAGE),
    ("alice", "public", "open", True, "denied", DENY_PUBLIC_APPROVAL_REQUIRED),
    ("alice", "slack", "mine", True, "denied", DENY_SLACK_APPROVAL_REQUIRED),
    ("alice", "slack", "write_shared", False, "denied", DENY_SLACK_NEEDS_APPROVAL),
    ("alice", "slack", "mine", False, "free", None),
]


def _cell_id(cell):
    who, kind, table, req, expected, _ = cell
    return f"{who}-{kind}-{table}{'-req' if req else ''}-{expected}"


class TestMatrix:
    @pytest.mark.parametrize("tool", WRITE_TOOLS)
    @pytest.mark.parametrize("cell", MATRIX, ids=_cell_id)
    def test_cell(self, world, cell, tool):
        who, kind, key, require_approval, expected, message = cell
        table = world.t[key]
        if require_approval:
            _set_require_approval(world, table)
        before = _snapshot(world, table)
        args = _args(tool, table["id"])
        raw = _dispatch_raw(world, tool, args, who=who, **_kw(world, kind))
        got = json.loads(raw)

        if expected == "free":
            assert "error" not in got, got
            assert got["change_id"] is not None
            assert _snapshot(world, table) != before
            return
        assert _snapshot(world, table) == before
        if expected == "not_found":
            assert raw == json.dumps({
                "error": "table_not_found",
                "message": table_not_found_message(table["id"]),
            })
            missing = str(uuid.uuid4())
            other = _dispatch_raw(world, tool, _args(tool, missing), who=who,
                                  **_kw(world, kind))
            assert other.replace(missing, table["id"]) == raw
        elif expected == "denied":
            assert got == {"error": "denied", "message": message}
        else:
            params = {**args, "operation": TOOL_OP[tool]}
            assert got == {
                "error": "approval_required",
                "message": message,
                "suggested_request": {"request_type": "write_table_rows", "params": params},
            }

    @pytest.mark.parametrize("tool", WRITE_TOOLS)
    @pytest.mark.parametrize("flags, reason", [
        ({"is_sub_agent": True}, DENY_SUB_AGENT),
        ({"is_user_subagent": True}, DENY_USER_SUBAGENT),
        ({"is_script": True, "cid": None}, DENY_SCRIPT),
    ])
    def test_read_only_run_kinds_through_dispatch(self, world, tool, flags, reason):
        table = world.t["mine"]
        before = _snapshot(world, table)
        got = _dispatch(world, tool, _args(tool, table["id"]), **flags)
        assert got == {"error": "denied", "message": reason}
        assert _snapshot(world, table) == before

    @pytest.mark.parametrize("tool", WRITE_TOOLS)
    @pytest.mark.parametrize("run_kind, reason", [
        ("sub_agent", DENY_SUB_AGENT),
        ("inference_api", DENY_INFERENCE_API),
        ("user_subagent", DENY_USER_SUBAGENT),
        ("script", DENY_SCRIPT),
    ])
    def test_read_only_run_kinds_at_the_handler(self, world, tool, run_kind, reason):
        # inference_api never reaches the handler through dispatch (rejected
        # above); the access rule denies it anyway.
        import chat.gemini_api.tool_handlers.tables as handlers

        table = world.t["mine"]
        caller = Caller(user=world.users["alice"], conversation_id="c",
                        project_id=None, is_public=False, run_kind=run_kind)
        handler = getattr(handlers, f"_handle_{tool}")
        before = _snapshot(world, table)
        got = json.loads(_run(handler(caller, _args(tool, table["id"]))))
        assert got == {"error": "denied", "message": reason}
        assert _snapshot(world, table) == before


# ---------------------------------------------------------------------------
# 3. suggested_request round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("tool", WRITE_TOOLS)
    @pytest.mark.parametrize("who", ["alice", "bob"])
    def test_card_params_apply_like_the_free_path(self, world, tool, who):
        shared = world.t["write_shared"]
        twin = _seed(world, make_table(world, f"Twin {tool} {who}"))
        assert _state(world, twin) == _state(world, shared)

        got = _dispatch(world, tool, _args(tool, shared["id"]), who=who)
        assert got["error"] == "approval_required"
        params = got["suggested_request"]["params"]
        table_id, operation, op_params = validate_write_request_params(params)
        assert (table_id, operation) == (shared["id"], TOOL_OP[tool])
        assert build_write_request_params(table_id, operation, op_params) == params

        caller = Caller(user=world.users[who], conversation_id=str(uuid.uuid4()),
                        project_id=None, is_public=False, run_kind="top_level")
        applied = _run(service.apply_row_write(
            caller, table_id, operation, op_params, bypass_approval=True,
        ))
        free = _dispatch(world, tool, _args(tool, twin["id"]))
        assert "error" not in free
        assert _state(world, shared) == _state(world, twin)
        assert applied["row_ids"] == free["row_ids"]
        assert applied["row_count"] == free["row_count"]

    def test_filter_delete_round_trip(self, world):
        shared = world.t["write_shared"]
        args = {"table_id": shared["id"],
                "filter": {"column": "Price", "op": "gt", "value": "1"}}
        got = _dispatch(world, "delete_rows", args)
        params = got["suggested_request"]["params"]
        assert params == {**args, "operation": "delete"}
        table_id, op, op_params = validate_write_request_params(params)
        assert op_params == {"filter": args["filter"]}
        out = _run(service.apply_row_write(ui(world), table_id, op, op_params,
                                           bypass_approval=True))
        assert out["row_ids"] == [2]

    def test_approval_path_validates_shape_first(self, world):
        tid = world.t["write_shared"]["id"]
        bad = _dispatch(world, "update_rows", {"table_id": tid, "updates": [{"Price": 1}]})
        assert bad["error"] == "invalid_args"
        both = _dispatch(world, "delete_rows", {
            "table_id": tid, "row_ids": [1], "filter": {"column": "SKU", "op": "eq", "value": "S1"},
        })
        assert both["error"] == "invalid_args"
        big = _dispatch(world, "insert_rows", {
            "table_id": tid, "rows": [{"Name": "x"}] * (TABLE_WRITE_MAX_ROWS + 1),
        })
        assert (big["error"], big["count"], big["cap"]) == (
            "too_many_rows", TABLE_WRITE_MAX_ROWS + 1, TABLE_WRITE_MAX_ROWS,
        )

    def test_coerced_arguments_reach_the_card(self, world):
        tid = world.t["write_shared"]["id"]
        got = _dispatch(world, "update_rows", {
            "table_id": tid, "updates": json.dumps([{"_id": "1", "Price": 3}]),
        })
        assert got["suggested_request"]["params"] == {
            "table_id": tid, "operation": "update", "updates": [{"_id": 1, "Price": 3}],
        }
        got = _dispatch(world, "delete_rows", {"table_id": tid, "row_ids": "2"})
        assert got["suggested_request"]["params"]["row_ids"] == [2]


# ---------------------------------------------------------------------------
# 4. Attribution and result shapes
# ---------------------------------------------------------------------------


class TestResults:
    def test_shapes_and_attribution(self, world):
        table = world.t["mine"]
        tid = table["id"]
        cid = str(uuid.uuid4())
        alice = world.users["alice"]["id"]

        ins = _dispatch(world, "insert_rows", {"table_id": tid, "rows": [
            {"Name": "Plum", "SKU": "S3"}, {"Name": "Kiwi", "Price": 4},
        ]}, cid=cid)
        assert list(ins) == ["inserted", "row_ids", "change_id", "row_count", "updated_at"]
        assert (ins["inserted"], ins["row_ids"], ins["row_count"]) == (2, [3, 4], 4)
        change = storage.get_change(_tdir(world, table), ins["change_id"])
        assert change["source"] == f"conversation:{cid}"
        assert change["actor_user_id"] == alice
        assert change["op"] == "insert" and change["row_ids"] == [3, 4]
        fresh = _run(world.store.get_table(tid))
        assert fresh["row_count"] == 4 and fresh["updated_at"] == ins["updated_at"]
        assert fresh["last_write_source"] == f"conversation:{cid}"

        upd = _dispatch(world, "update_rows", {"table_id": tid, "updates": [
            {"_id": 4, "Price": "5.50"}, {"_id": 1, "Name": "Green apple"},
        ]})
        assert list(upd) == ["updated", "row_ids", "change_id", "row_count", "updated_at"]
        assert (upd["updated"], upd["row_ids"]) == (2, [1, 4])

        ups = _dispatch(world, "upsert_rows", {"table_id": tid, "key_column": "SKU", "rows": [
            {"SKU": "S9", "Name": "New"}, {"SKU": "S1", "Price": 10},
        ]})
        assert list(ups) == [
            "inserted", "updated", "inserted_ids", "updated_ids", "row_ids",
            "change_id", "row_count", "updated_at",
        ]
        assert (ups["inserted"], ups["updated"]) == (1, 1)
        assert (ups["inserted_ids"], ups["updated_ids"], ups["row_ids"]) == ([5], [1], [1, 5])

        dele = _dispatch(world, "delete_rows", {"table_id": tid, "row_ids": [5, 2]})
        assert list(dele) == ["deleted", "row_ids", "change_id", "row_count", "updated_at"]
        assert (dele["deleted"], dele["row_ids"], dele["row_count"]) == (2, [2, 5], 3)
        rows = {r["_id"]: r for r in _state(world, table)}
        assert set(rows) == {1, 3, 4}
        assert rows[1]["Name"] == "Green apple" and rows[1]["Price"] == "10"
        assert rows[4]["Price"] == "5.5"

    def test_public_recipient_is_the_actor(self, world):
        table = world.t["open_write"]
        cid = str(uuid.uuid4())
        got = _dispatch(world, "insert_rows", {"table_id": table["id"], "rows": [{"Name": "B"}]},
                        who="bob", cid=cid, **_kw(world, "public"))
        change = storage.get_change(_tdir(world, table), got["change_id"])
        assert change["source"] == f"conversation:{cid}"
        assert change["actor_user_id"] == world.users["bob"]["id"]

    def test_filter_delete(self, world):
        table = world.t["mine"]
        tid = table["id"]
        got = _dispatch(world, "delete_rows", {
            "table_id": tid, "filter": json.dumps({"column": "Name", "op": "eq", "value": "Pear"}),
        })
        assert (got["deleted"], got["row_ids"], got["row_count"]) == (1, [2], 1)
        before = _snapshot(world, table)
        none = _dispatch(world, "delete_rows", {
            "table_id": tid, "filter": {"column": "Name", "op": "eq", "value": "Nothing"},
        })
        assert none["deleted"] == 0 and none["row_ids"] == [] and none["change_id"] is None
        assert _snapshot(world, table) == before


# ---------------------------------------------------------------------------
# 5. Validation, caps, errors
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_rows_is_atomic_and_lists_every_problem(self, world):
        table = world.t["mine"]
        before = _snapshot(world, table)
        got = _dispatch(world, "insert_rows", {"table_id": table["id"], "rows": [
            {"Name": "Good", "SKU": "G1"},
            {"Name": "Bad price", "Price": "abc"},
            {"Price": 3, "Colour": "red"},
            {"Name": "Dup", "SKU": "S1"},
            {"Name": "Fine"},
        ]})
        assert got["error"] == "invalid_rows"
        problems = [(p["row"], p["column"]) for p in got["problems"]]
        assert (1, "Price") in problems
        assert (2, "Name") in problems and (2, "Colour") in problems
        assert (3, "SKU") in problems
        assert not any(p["row"] in (0, 4) for p in got["problems"])
        assert all(set(p) >= {"row", "column", "reason"} for p in got["problems"])
        dup = next(p for p in got["problems"] if p["row"] == 3)
        assert "_id 1" in dup["reason"]
        assert _snapshot(world, table) == before

    def test_update_with_unknown_id_fails_the_whole_call(self, world):
        table = world.t["mine"]
        before = _snapshot(world, table)
        got = _dispatch(world, "update_rows", {"table_id": table["id"], "updates": [
            {"_id": 1, "Name": "Changed"}, {"_id": 99, "Name": "Ghost"},
        ]})
        assert got["error"] == "invalid_rows"
        assert [(p["row"], p["column"], p["code"]) for p in got["problems"]] == [
            (1, "_id", "unknown_row"),
        ]
        assert _snapshot(world, table) == before

    def test_upsert_key_errors(self, world):
        tid = world.t["mine"]["id"]
        before = _snapshot(world, world.t["mine"])
        dup = _dispatch(world, "upsert_rows", {"table_id": tid, "key_column": "SKU", "rows": [
            {"SKU": "S7", "Name": "a"}, {"SKU": "S7", "Name": "b"},
        ]})
        assert dup["error"] == "invalid_rows"
        assert [(p["row"], p["code"]) for p in dup["problems"]] == [(1, "duplicate_key")]
        not_unique = _dispatch(world, "upsert_rows", {
            "table_id": tid, "key_column": "Name", "rows": [{"Name": "a"}],
        })
        assert not_unique["error"] == "key_not_unique"
        unknown = _dispatch(world, "upsert_rows", {
            "table_id": tid, "key_column": "Nope", "rows": [{"Name": "a"}],
        })
        assert unknown["error"] == "unknown_key_column"
        missing = _dispatch(world, "upsert_rows", {"table_id": tid, "rows": [{"Name": "a"}]})
        assert missing["error"] == "invalid_args"
        assert _snapshot(world, world.t["mine"]) == before

    def test_caps(self, world, monkeypatch):
        table = world.t["mine"]
        tid = table["id"]
        before = _snapshot(world, table)
        for tool, key, entry in (
            ("insert_rows", "rows", {"Name": "x"}),
            ("update_rows", "updates", {"_id": 1, "Name": "x"}),
            ("delete_rows", "row_ids", 1),
        ):
            got = _dispatch(world, tool, {
                "table_id": tid, key: [entry] * (TABLE_WRITE_MAX_ROWS + 1),
            })
            assert got == {
                "error": "too_many_rows",
                "message": got["message"],
                "count": TABLE_WRITE_MAX_ROWS + 1,
                "cap": TABLE_WRITE_MAX_ROWS,
            }, tool
        assert _snapshot(world, table) == before

        # A filter delete over the cap names the matching count.
        monkeypatch.setattr(storage, "TABLE_WRITE_MAX_ROWS", 1)
        got = _dispatch(world, "delete_rows", {
            "table_id": tid, "filter": {"column": "Price", "op": "gte", "value": 0},
        })
        assert (got["error"], got["count"], got["cap"]) == ("too_many_rows", 2, 1)
        assert _snapshot(world, table) == before

    def test_table_full(self, world, monkeypatch):
        table = world.t["mine"]
        monkeypatch.setattr(storage, "TABLE_MAX_ROWS", 3)
        got = _dispatch(world, "insert_rows", {"table_id": table["id"], "rows": [
            {"Name": "a"}, {"Name": "b"},
        ]})
        assert (got["error"], got["row_count"], got["adding"], got["cap"]) == (
            "table_full", 2, 2, 3,
        )

    def test_argument_shapes(self, world):
        tid = world.t["mine"]["id"]
        cases = [
            ("insert_rows", {"table_id": tid}),
            ("insert_rows", {"table_id": tid, "rows": []}),
            ("insert_rows", {"table_id": tid, "rows": {"Name": "x"}}),
            ("insert_rows", {"table_id": tid, "rows": ["x"]}),
            ("insert_rows", {"table_id": tid, "rows": "not json"}),
            ("insert_rows", {"rows": [{"Name": "x"}]}),
            ("update_rows", {"table_id": tid, "updates": [{"_id": "one", "Name": "x"}]}),
            ("update_rows", {"table_id": tid, "updates": [{"_id": True, "Name": "x"}]}),
            ("delete_rows", {"table_id": tid}),
            ("delete_rows", {"table_id": tid, "row_ids": [1],
                             "filter": {"column": "Name", "op": "eq", "value": "x"}}),
            ("delete_rows", {"table_id": tid, "row_ids": ["x"]}),
            ("delete_rows", {"table_id": tid, "filter": ["Name"]}),
        ]
        before = _snapshot(world, world.t["mine"])
        for tool, args in cases:
            got = _dispatch(world, tool, args)
            assert got["error"] == "invalid_args", (tool, args, got)
            assert set(got) == {"error", "message"}
        assert _snapshot(world, world.t["mine"]) == before

    def test_bad_filter_payload(self, world):
        got = _dispatch(world, "delete_rows", {
            "table_id": world.t["mine"]["id"],
            "filter": {"and": [{"column": "Name", "op": "gt", "value": "x"}]},
        })
        assert got["error"] == "op_not_allowed" and got["path"] == "and[0].op"
        assert set(got) == {"error", "message", "path"}

    def test_json_string_rows_and_coerced_ids(self, world):
        tid = world.t["mine"]["id"]
        ins = _dispatch(world, "insert_rows", {
            "table_id": tid, "rows": json.dumps([{"Name": "Json"}]),
        })
        assert ins["row_ids"] == [3]
        upd = _dispatch(world, "update_rows", {
            "table_id": tid, "updates": [{"_id": "3", "Price": 1}],
        })
        assert upd["row_ids"] == [3]
        dele = _dispatch(world, "delete_rows", {"table_id": tid, "row_ids": "[3, 2.0]"})
        assert dele["row_ids"] == [2, 3]

    def test_column_keys_never_appear(self, world):
        table = world.t["mine"]
        tid = table["id"]
        outputs = [
            _dispatch_raw(world, "insert_rows", {"table_id": tid, "rows": [
                {"Name": None, "Price": "x", "SKU": "S1", "Other": 1, "_id": 4},
            ]}),
            _dispatch_raw(world, "update_rows", {"table_id": tid, "updates": [
                {"_id": 1, "SKU": "S2"}, {"_id": 1, "Name": "x"}, {"_id": 50},
            ]}),
            _dispatch_raw(world, "upsert_rows", {"table_id": tid, "key_column": "SKU",
                                                 "rows": [{"Name": "x"}]}),
            _dispatch_raw(world, "delete_rows", {"table_id": tid, "filter": {
                "column": "Missing", "op": "eq", "value": 1}}),
            _dispatch_raw(world, "insert_rows", {"table_id": world.t["write_shared"]["id"],
                                                 "rows": [{"Name": "x"}]}),
        ]
        keys = [c["key"] for t in world.t.values() for c in t["schema"]]
        assert keys and all(k.startswith("c_") for k in keys)
        for out in outputs:
            assert '"key"' not in out
            for key in keys:
                assert key not in out, out

    def test_gate_closed_before_any_db_work(self, world, monkeypatch):
        tid = world.t["mine"]["id"]
        world.fg.set_feature_enabled(world.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(world.store, "get_table", boom)
        expected = {"error": "tables_disabled", "message": tables_disabled_message()}
        for tool in WRITE_TOOLS:
            assert _dispatch(world, tool, _args(tool, tid)) == expected
            assert _dispatch(world, tool, {"table_id": tid, "rows": "junk"}) == expected
            assert _dispatch(world, tool, _args(tool, tid), **_kw(world, "public")) == expected


# ---------------------------------------------------------------------------
# 6. write_request
# ---------------------------------------------------------------------------


class TestWriteRequest:
    @pytest.mark.parametrize("operation, op_params", [
        ("insert", {"rows": [{"Name": "a"}]}),
        ("update", {"updates": [{"_id": 1, "Name": None}]}),
        ("upsert", {"key_column": "SKU", "rows": [{"SKU": "x"}]}),
        ("delete", {"row_ids": [1, 2]}),
        ("delete", {"filter": {"column": "Name", "op": "eq", "value": "a"}}),
    ])
    def test_round_trip(self, operation, op_params):
        params = build_write_request_params("t1", operation, op_params)
        assert params == {"table_id": "t1", "operation": operation, **op_params}
        assert validate_write_request_params(params) == ("t1", operation, op_params)

    def test_build_drops_absent_selector_and_coerces_ids(self):
        params = build_write_request_params("t1", "delete", {"row_ids": ["3", 4.0], "filter": None})
        assert params == {"table_id": "t1", "operation": "delete", "row_ids": [3, 4]}
        upd = {"updates": [{"_id": "7", "Name": "x"}]}
        assert build_write_request_params("t1", "update", upd)["updates"] == [
            {"_id": 7, "Name": "x"},
        ]
        assert upd["updates"][0]["_id"] == "7"  # the caller's dict is untouched

    @pytest.mark.parametrize("params", [
        None,
        [],
        {"operation": "insert", "rows": [{}]},
        {"table_id": " ", "operation": "insert", "rows": [{}]},
        {"table_id": "t", "operation": "replace", "rows": [{}]},
        {"table_id": "t", "operation": "insert", "rows": [{}], "updates": [{"_id": 1}]},
        {"table_id": "t", "operation": "insert", "rows": [{}], "extra": 1},
        {"table_id": "t", "operation": "insert", "rows": [{}], "key_column": "SKU"},
        {"table_id": "t", "operation": "insert"},
        {"table_id": "t", "operation": "insert", "rows": []},
        {"table_id": "t", "operation": "insert", "rows": [1]},
        {"table_id": "t", "operation": "update", "updates": [{"Name": "x"}]},
        {"table_id": "t", "operation": "upsert", "rows": [{}]},
        {"table_id": "t", "operation": "upsert", "key_column": "", "rows": [{}]},
        {"table_id": "t", "operation": "delete"},
        {"table_id": "t", "operation": "delete", "row_ids": [1], "filter": {}},
        {"table_id": "t", "operation": "delete", "row_ids": [None]},
        {"table_id": "t", "operation": "delete", "row_ids": []},
        {"table_id": "t", "operation": "delete", "filter": "Name = x"},
    ])
    def test_invalid(self, params):
        with pytest.raises(WriteRequestError) as info:
            validate_write_request_params(params)
        assert info.value.code == "invalid_args"

    def test_build_refuses_envelope_keys_in_params(self):
        with pytest.raises(WriteRequestError):
            build_write_request_params("t1", "insert", {"rows": [{}], "table_id": "t2"})

    def test_cap(self):
        with pytest.raises(storage.TooManyRowsError) as info:
            validate_write_request_params({
                "table_id": "t", "operation": "delete",
                "row_ids": list(range(1, TABLE_WRITE_MAX_ROWS + 2)),
            })
        assert (info.value.count, info.value.cap) == (TABLE_WRITE_MAX_ROWS + 1, TABLE_WRITE_MAX_ROWS)


# ---------------------------------------------------------------------------
# 7. A9 review follow-ups (read tools)
# ---------------------------------------------------------------------------


class TestReadFollowUps:
    @pytest.mark.parametrize("sort", [
        {"column": "Price", "direction": "desc"},
        json.dumps({"column": "Price", "direction": "desc"}),
    ])
    def test_query_rows_accepts_a_bare_sort_object(self, world, sort):
        got = _dispatch(world, "query_rows", {"table_id": world.t["mine"]["id"], "sort": sort})
        assert [r["_id"] for r in got["rows"]] == [2, 1]

    def test_list_tables_skips_an_unreadable_schema(self, world, monkeypatch):
        import chat.gemini_api.tool_handlers.tables as handlers
        from chat.tables.schema import ColumnSpecError

        odd = _run(service.create_table(ui(world), title="Odd", columns=[
            {"name": "Only", "type": "text"},
        ]))
        real = handlers.load_schema

        def load(data):
            if [c["name"] for c in data] == ["Only"]:
                raise ColumnSpecError("invalid_spec", "broken")
            return real(data)

        before = _ids_listed(world)
        assert odd["id"] in before
        monkeypatch.setattr(handlers, "load_schema", load)
        listed = _dispatch(world, "list_tables", {})
        assert {t["id"] for t in listed["tables"]} == before - {odd["id"]}
        assert listed["count"] == len(before) - 1

    def test_query_rows_description_names_the_in_cap(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        from chat.tables.constants import TABLE_FILTER_IN_MAX_VALUES

        desc = TOOL_CALL_REGISTRY["query_rows"]["description"]
        assert f"at most {TABLE_FILTER_IN_MAX_VALUES} values" in desc
        assert "compare the whole list" in desc


def _ids_listed(world) -> set[str]:
    return {t["id"] for t in _dispatch(world, "list_tables", {})["tables"]}


# ---------------------------------------------------------------------------
# Read-only tiers' prompts hide the row write tools
# ---------------------------------------------------------------------------

_TABLE_READ_TOOLS = ("list_tables", "get_table", "query_rows")
_TABLE_WRITE_TOOLS = ("insert_rows", "update_rows", "upsert_rows", "delete_rows")


def _offered_table_tool(prompt: str, name: str) -> bool:
    from tests.prompt_support import named_under_skill
    return named_under_skill(prompt, "system:tables", name)


def test_table_write_tool_roster_helper():
    from chat.gemini_api.system_prompt import _table_write_tool_names
    assert _table_write_tool_names() == frozenset(_TABLE_WRITE_TOOLS)


@pytest.mark.parametrize("tier", ["sub_agent", "user_subagent"])
def test_read_only_tier_prompts_offer_reads_not_writes(tier):
    from chat.gemini_api.system_prompt import (
        get_sub_agent_system_prompt,
        get_user_subagent_system_prompt,
    )
    services = {"tables": True}
    if tier == "sub_agent":
        prompt = get_sub_agent_system_prompt(
            "helper", "key", base_url="http://x", connected_services=services,
        )
    else:
        prompt = get_user_subagent_system_prompt(
            "key", base_url="http://x", connected_services=services,
            target_user_email="t@example.com", caller_email="c@example.com",
        )
    for name in _TABLE_READ_TOOLS:
        assert _offered_table_tool(prompt, name), name
    for name in _TABLE_WRITE_TOOLS:
        assert not _offered_table_tool(prompt, name), name
