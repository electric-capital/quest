"""Quest Tables read tools (phase A9): list_tables, get_table, query_rows.

Driven through ``_dispatch_tool_call(tool_call ...)`` (the real
TOOL_CALL_HANDLERS entries and ``tool_dispatch._table_caller``) against the
isolated ``env`` fixture of tests/test_table_service.py (temp quest.db and
TABLES_DIR, ``tables`` gate open).

Covers:

1. Registry entries, requires_service prompt gating, public / script
   allowlists, prompt tiers.
2. Visibility per conversation kind (private standalone, private project,
   public project, sub-agent, inference API, user-subagent, Slack, script),
   scope filtering, doc-source public tables.
3. Hidden-vs-missing parity through the tools (byte-equal JSON).
4. Output shapes: column labels, get_table columns without ``key``,
   sample_rows, the key never appearing in any output.
5. query_rows: filter / sort / paging / total, JSON-string arguments,
   count_only, FilterError payloads, the response byte cap.
6. The sandbox script bridge: user tables only, read-only.
7. Gate closed: tables_disabled before any DB work.
"""

import asyncio
import json
import uuid

import pytest

from chat.tables import service
from chat.tables.access import (
    DENY_INFERENCE_API,
    DENY_OUTSIDE_PROJECT,
    DENY_PUBLIC_TABLE_FROM_PRIVATE,
    DENY_SCRIPT,
    DENY_SUB_AGENT,
    SCHEMA_APPROVAL_NOTE,
    DENY_USER_SUBAGENT,
)
from chat.tables.constants import (
    TABLES_SERVICE_KEY,
    table_not_found_message,
    tables_disabled_message,
)
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    env,
    make_table,
    ui,
)

READ_TOOLS = ("list_tables", "get_table", "query_rows")


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


def _insert(env, table, rows, who="alice"):
    return _run(service.apply_row_write(ui(env, who), table["id"], "insert", {"rows": rows}))


def _keys(table) -> list[str]:
    return [c["key"] for c in table["schema"]]


def _ids(listed) -> set[str]:
    return {t["id"] for t in listed["tables"]}


@pytest.fixture()
def world(env):
    """alice: a user table (7 rows), a private-project table, a
    public-project table; bob: a user table shared read with alice."""
    user = make_table(env, "Inventory")
    _insert(env, user, [
        {"Name": f"Item {i}", "Price": str(i * 5), "SKU": f"S{i}"} for i in range(1, 8)
    ])
    private_proj = make_table(env, "Research data", project_id=env.private_project)
    public_proj = make_table(env, "Open data", project_id=env.public_project)
    bobs = make_table(env, "Bob list", owner="bob", shares=[("alice", "read")])
    env.t = {"user": user, "private": private_proj, "public": public_proj, "bob": bobs}
    return env


# ---------------------------------------------------------------------------
# 1. Registry, allowlists, prompt gating
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_entries_handlers_and_service_key(self):
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY, mutating_tool_call_tools

        for name in READ_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["name"] == name
            assert spec["requires_service"] == TABLES_SERVICE_KEY == "tables"
            assert spec["skill"] == "system:tables"
            assert not spec.get("core")
            assert name in TOOL_CALL_HANDLERS
        assert not set(READ_TOOLS) & mutating_tool_call_tools()

    def test_parameters(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        def params(name):
            return TOOL_CALL_REGISTRY[name]["parameters"]

        assert params("list_tables")["properties"]["scope"]["enum"] == [
            "user", "project", "public", "all",
        ]
        limit = params("list_tables")["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 200, 50)
        assert params("get_table")["required"] == ["table_id"]
        q = params("query_rows")
        assert q["required"] == ["table_id"]
        assert (q["properties"]["limit"]["maximum"], q["properties"]["limit"]["default"]) == (1000, 100)
        assert q["properties"]["offset"]["maximum"] == 100000
        assert set(q["properties"]) >= {
            "filter", "sort", "limit", "offset", "columns", "count_only",
        }

    def test_descriptions_are_public_prompt_safe(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        from tests.test_docs_tools import _PUBLIC_PROMPT_FORBIDDEN

        for name in READ_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            text = json.dumps([spec["description"], spec["parameters"]])
            for term in _PUBLIC_PROMPT_FORBIDDEN:
                assert term not in text, (name, term)
        assert "Name:enum[]" in TOOL_CALL_REGISTRY["list_tables"]["description"]

    def test_allowlists(self):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST

        assert set(READ_TOOLS) <= PUBLIC_TOOL_CALL_ALLOWLIST
        assert set(READ_TOOLS) <= SCRIPT_TOOL_CALL_ALLOWLIST

    def test_requires_service_hides_the_tools(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY, tool_is_advertised

        for name in READ_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            assert not tool_is_advertised(spec, {})
            assert not tool_is_advertised(spec, {TABLES_SERVICE_KEY: False})
            assert tool_is_advertised(spec, {TABLES_SERVICE_KEY: True})

    def test_prompt_enumeration_follows_the_tables_key(self):
        from chat.system_skills import build_system_skills_enumeration
        from tests.prompt_support import named_under_skill

        hidden = build_system_skills_enumeration(connected_services={})
        for name in READ_TOOLS:
            assert name not in hidden
        shown = build_system_skills_enumeration(connected_services={TABLES_SERVICE_KEY: True})
        for name in READ_TOOLS:
            assert named_under_skill(shown, "system:tables", name)


# ---------------------------------------------------------------------------
# 2. Visibility per conversation kind
# ---------------------------------------------------------------------------


class TestVisibility:
    def test_private_standalone(self, world):
        """A standalone chat sees user tables plus, read-only, the tables of
        all of the user's own projects (scope ``project``)."""
        t = world.t
        listed = _dispatch(world, "list_tables", {})
        assert _ids(listed) == {t[k]["id"] for k in ("user", "bob", "private", "public")}
        assert listed["count"] == 4
        project = _dispatch(world, "list_tables", {"scope": "project"})
        assert _ids(project) == {t["private"]["id"], t["public"]["id"]}
        for row in project["tables"]:
            assert row["scope"] == "project"
            assert row["writable"] == "denied" and row["schema_change"] == "denied"
        assert _dispatch(world, "list_tables", {"scope": "user"})["count"] == 2
        assert _dispatch(world, "list_tables", {"scope": "public"}) == {"tables": [], "count": 0}
        got = _dispatch(world, "get_table", {"table_id": t["private"]["id"]})
        assert (got["writable"], got["write_note"]) == ("denied", DENY_OUTSIDE_PROJECT)
        assert (got["schema_change"], got["schema_note"]) == ("denied", DENY_OUTSIDE_PROJECT)
        got = _dispatch(world, "get_table", {"table_id": t["public"]["id"]})
        assert got["write_note"] == DENY_PUBLIC_TABLE_FROM_PRIVATE
        rows = _dispatch(world, "query_rows", {"table_id": t["private"]["id"]})
        assert rows["rows"] == [] and rows["total"] == 0
        # Bob's standalone chat never sees alice's project tables.
        got = _dispatch(world, "get_table", {"table_id": t["private"]["id"]}, who="bob")
        assert got["error"] == "table_not_found"

    def test_private_project(self, world):
        t = world.t
        kw = {"project_id": world.private_project}
        listed = _dispatch(world, "list_tables", {}, **kw)
        assert _ids(listed) == {t["user"]["id"], t["bob"]["id"], t["private"]["id"]}
        project = _dispatch(world, "list_tables", {"scope": "project"}, **kw)
        assert _ids(project) == {t["private"]["id"]}
        assert project["tables"][0]["scope"] == "project"
        assert project["tables"][0]["project_id"] == world.private_project
        user = _dispatch(world, "list_tables", {"scope": "user"}, **kw)
        assert _ids(user) == {t["user"]["id"], t["bob"]["id"]}
        # The public project is not a doc source yet: its tables are hidden.
        assert _dispatch(world, "get_table", {"table_id": t["public"]["id"]}, **kw)[
            "error"] == "table_not_found"

    def test_private_project_reads_doc_source_public_tables(self, world):
        from db import project_store

        t = world.t
        _run(project_store.set_doc_source_projects(
            world.users["alice"]["id"], world.private_project, [world.public_project],
        ))
        kw = {"project_id": world.private_project}
        public = _dispatch(world, "list_tables", {"scope": "public"}, **kw)
        assert _ids(public) == {t["public"]["id"]}
        assert public["tables"][0]["writable"] == "denied"
        assert t["public"]["id"] in _ids(_dispatch(world, "list_tables", {}, **kw))
        rows = _dispatch(world, "query_rows", {"table_id": t["public"]["id"]}, **kw)
        assert rows["rows"] == [] and rows["total"] == 0

    def test_public_project_never_sees_private_tables(self, world):
        t = world.t
        kw = {"project_id": world.public_project, "is_public": True}
        for scope in ("all", "project", "public"):
            listed = _dispatch(world, "list_tables", {"scope": scope}, **kw)
            assert _ids(listed) == {t["public"]["id"]}, scope
            assert all(row["mode"] == "public" for row in listed["tables"])
        assert _dispatch(world, "list_tables", {"scope": "user"}, **kw)["tables"] == []
        for key in ("user", "private", "bob"):
            for tool in ("get_table", "query_rows"):
                got = _dispatch(world, tool, {"table_id": t[key]["id"]}, **kw)
                assert got["error"] == "table_not_found", (key, tool)
        own = _dispatch(world, "get_table", {"table_id": t["public"]["id"]}, **kw)
        # Rows free; the schema card (a public-safe action request).
        assert own["writable"] == "free" and own["schema_change"] == "approval"
        assert own["schema_note"] == SCHEMA_APPROVAL_NOTE

    @pytest.mark.parametrize("flags, reason", [
        ({"is_sub_agent": True}, DENY_SUB_AGENT),
        ({"is_inference_api": True}, DENY_INFERENCE_API),
        ({"is_user_subagent": True}, DENY_USER_SUBAGENT),
    ])
    def test_read_only_run_kinds(self, world, flags, reason):
        t = world.t
        listed = _dispatch(world, "list_tables", {}, project_id=world.private_project, **flags)
        assert _ids(listed) == {t["user"]["id"], t["bob"]["id"], t["private"]["id"]}
        for row in listed["tables"]:
            assert row["writable"] == "denied" and row["write_note"] == reason
        got = _dispatch(world, "get_table", {"table_id": t["user"]["id"]}, **flags)
        assert got["writable"] == "denied" and got["write_note"] == reason
        assert _dispatch(world, "query_rows", {"table_id": t["user"]["id"]}, **flags)["total"] == 7

    def test_slack(self, world):
        t = world.t
        listed = _dispatch(world, "list_tables", {}, is_slack=True)
        assert _ids(listed) == {t[k]["id"] for k in ("user", "bob", "private", "public")}
        own = next(r for r in listed["tables"] if r["id"] == t["user"]["id"])
        assert own["writable"] == "free" and own["schema_change"] == "denied"
        proj = next(r for r in listed["tables"] if r["id"] == t["private"]["id"])
        assert proj["writable"] == "denied" and proj["write_note"] == DENY_OUTSIDE_PROJECT
        assert _dispatch(world, "query_rows", {"table_id": t["user"]["id"]},
                         is_slack=True)["total"] == 7

    def test_verdict_fields_for_owner_and_recipient(self, world):
        t = world.t
        listed = {r["id"]: r for r in _dispatch(world, "list_tables", {})["tables"]}
        own = listed[t["user"]["id"]]
        assert (own["writable"], own["write_note"], own["schema_change"]) == ("free", None, "approval")
        shared = listed[t["bob"]["id"]]
        assert shared["writable"] == "denied" and shared["write_note"]

    def test_limit_and_order(self, world):
        listed = _dispatch(world, "list_tables", {"limit": "1"})
        assert listed["count"] == 1
        # Newest updated first: bob's table was created last.
        assert listed["tables"][0]["id"] == world.t["bob"]["id"]
        assert _dispatch(world, "list_tables", {"limit": 5000})["count"] == 4  # clamped
        bad = _dispatch(world, "list_tables", {"scope": "everything"})
        assert bad["error"] == "invalid_args"
        assert _dispatch(world, "list_tables", {"limit": "many"})["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# 3. Hidden parity
# ---------------------------------------------------------------------------


class TestHiddenParity:
    @pytest.mark.parametrize("tool", ["get_table", "query_rows"])
    def test_private_table_from_public_conversation_equals_missing(self, world, tool):
        hidden_id = world.t["user"]["id"]
        missing_id = str(uuid.uuid4())
        kw = {"project_id": world.public_project, "is_public": True}
        args = {"limit": 5} if tool == "query_rows" else {}
        hidden = _dispatch_raw(world, tool, {"table_id": hidden_id, **args}, **kw)
        missing = _dispatch_raw(world, tool, {"table_id": missing_id, **args}, **kw)
        expected = json.dumps({
            "error": "table_not_found", "message": table_not_found_message(hidden_id),
        })
        assert hidden == expected
        assert missing.replace(missing_id, hidden_id) == hidden

    def test_hidden_from_other_user_equals_missing(self, world):
        table_id = world.t["user"]["id"]
        hidden = _dispatch_raw(world, "get_table", {"table_id": table_id}, who="carol")
        assert hidden == json.dumps({
            "error": "table_not_found", "message": table_not_found_message(table_id),
        })


# ---------------------------------------------------------------------------
# 4. Shapes, sample rows, no key leak
# ---------------------------------------------------------------------------


class TestShapes:
    def test_list_row_shape(self, world):
        t = world.t
        row = next(r for r in _dispatch(world, "list_tables", {})["tables"]
                   if r["id"] == t["user"]["id"])
        assert list(row) == [
            "id", "title", "description", "mode", "scope", "project_id", "row_count",
            "column_count", "columns", "updated_at", "writable", "write_note",
            "schema_change",
        ]
        assert row["columns"] == ["Name:text", "Price:number", "SKU:text"]
        assert (row["row_count"], row["column_count"]) == (7, 3)
        assert (row["mode"], row["scope"], row["project_id"]) == ("private", "user", None)

    def test_enum_labels(self, world):
        table = _run(service.create_table(ui(world), title="Tagged", columns=[
            {"name": "Status", "type": "enum", "options": ["Open", "Done"]},
            {"name": "Tags", "type": "enum", "options": ["a", "b"], "multiple": True},
            {"name": "Ok", "type": "boolean"},
            {"name": "Link", "type": "url"},
            {"name": "Due", "type": "date"},
        ]))
        row = next(r for r in _dispatch(world, "list_tables", {})["tables"]
                   if r["id"] == table["id"])
        assert row["columns"] == [
            "Status:enum", "Tags:enum[]", "Ok:boolean", "Link:url", "Due:date",
        ]

    def test_get_table_shape_and_sample_rows(self, world):
        table = world.t["user"]
        got = _dispatch(world, "get_table", {"table_id": table["id"]})
        assert list(got) == [
            "id", "title", "description", "mode", "scope", "project_id", "columns",
            "row_count", "schema_version", "updated_at", "writable", "write_note",
            "schema_change", "schema_note", "sample_rows",
        ]
        assert [list(c) for c in got["columns"]] == [[
            "name", "type", "required", "unique", "default", "options", "multiple",
            "precision", "description",
        ]] * 3
        assert got["columns"][0]["name"] == "Name" and got["columns"][0]["required"] is True
        assert got["columns"][2]["unique"] is True
        assert got["row_count"] == 7 and got["schema_version"] == 1
        assert [r["_id"] for r in got["sample_rows"]] == [1, 2, 3, 4, 5]
        first = got["sample_rows"][0]
        assert set(first) == {"_id", "_updated_at", "Name", "Price", "SKU"}
        assert (first["Name"], first["Price"]) == ("Item 1", "5")
        assert got["writable"] == "free" and got["schema_change"] == "approval"
        assert got["schema_note"]

    def test_column_keys_never_appear(self, world):
        t = world.t
        outputs = [
            _dispatch_raw(world, "list_tables", {}, project_id=world.private_project),
            _dispatch_raw(world, "get_table", {"table_id": t["user"]["id"]}),
            _dispatch_raw(world, "query_rows", {"table_id": t["user"]["id"]}),
            _dispatch_raw(world, "query_rows", {
                "table_id": t["user"]["id"],
                "filter": {"column": "Nope", "op": "eq", "value": 1},
            }),
            _dispatch_raw(world, "query_rows", {
                "table_id": t["user"]["id"], "columns": ["Name", "Missing"],
            }),
        ]
        keys = [k for table in t.values() for k in _keys(table)]
        assert keys and all(k.startswith("c_") for k in keys)
        for out in outputs:
            assert '"key"' not in out
            for key in keys:
                assert key not in out


# ---------------------------------------------------------------------------
# 5. query_rows
# ---------------------------------------------------------------------------


class TestQueryRows:
    def test_paging_and_total(self, world):
        tid = world.t["user"]["id"]
        page = _dispatch(world, "query_rows", {"table_id": tid, "limit": 2, "offset": 2})
        assert [r["_id"] for r in page["rows"]] == [3, 4]
        assert page["total"] == 7 and page["truncated"] is False
        assert (page["offset"], page["limit"]) == (2, 2)
        assert "hint" not in page
        last = _dispatch(world, "query_rows", {"table_id": tid, "limit": "5", "offset": "5"})
        assert [r["_id"] for r in last["rows"]] == [6, 7]
        default = _dispatch(world, "query_rows", {"table_id": tid})
        assert (default["offset"], default["limit"], len(default["rows"])) == (0, 100, 7)

    def test_filter_sort_columns(self, world):
        tid = world.t["user"]["id"]
        got = _dispatch(world, "query_rows", {
            "table_id": tid,
            "filter": {"column": "Price", "op": "gt", "value": "10"},
            "sort": [{"column": "Price", "direction": "desc"}],
            "columns": ["Name"],
        })
        assert got["total"] == 5
        assert [r["Name"] for r in got["rows"]] == [f"Item {i}" for i in (7, 6, 5, 4, 3)]
        assert set(got["rows"][0]) == {"_id", "_updated_at", "Name"}
        # Values validated like writes: 10 and "10" select the same rows.
        same = _dispatch(world, "query_rows", {
            "table_id": tid, "filter": {"column": "Price", "op": "gt", "value": 10},
        })
        assert same["total"] == 5

    def test_json_string_arguments(self, world):
        tid = world.t["user"]["id"]
        got = _dispatch(world, "query_rows", {
            "table_id": tid,
            "filter": json.dumps({"or": [
                {"column": "SKU", "op": "eq", "value": "S1"},
                {"column": "SKU", "op": "eq", "value": "S2"},
            ]}),
            "sort": json.dumps([{"column": "SKU", "direction": "desc"}]),
            "columns": json.dumps(["SKU"]),
            "count_only": "false",
        })
        assert [r["SKU"] for r in got["rows"]] == ["S2", "S1"]
        bare = _dispatch(world, "query_rows", {"table_id": tid, "columns": "SKU", "limit": 1})
        assert set(bare["rows"][0]) == {"_id", "_updated_at", "SKU"}
        bad = _dispatch(world, "query_rows", {"table_id": tid, "filter": "Price > 10"})
        assert bad["error"] == "invalid_args"
        empty = _dispatch(world, "query_rows", {"table_id": tid, "filter": "", "sort": ""})
        assert empty["total"] == 7

    def test_count_only(self, world):
        tid = world.t["user"]["id"]
        got = _dispatch(world, "query_rows", {
            "table_id": tid, "count_only": True,
            "filter": {"column": "Price", "op": "lte", "value": 10},
        })
        assert got == {"total": 2}
        assert _dispatch(world, "query_rows", {"table_id": tid, "count_only": "true"}) == {"total": 7}

    def test_filter_error_payload(self, world):
        tid = world.t["user"]["id"]
        got = _dispatch(world, "query_rows", {
            "table_id": tid,
            "filter": {"and": [
                {"column": "Name", "op": "eq", "value": "x"},
                {"column": "Name", "op": "gt", "value": "x"},
            ]},
        })
        assert got["error"] == "op_not_allowed"
        assert got["path"] == "and[1].op"
        assert got["message"]
        unknown = _dispatch(world, "query_rows", {
            "table_id": tid, "filter": {"column": "Nope", "op": "eq", "value": 1},
        })
        assert unknown["error"] == "unknown_column" and "path" in unknown
        limit = _dispatch(world, "query_rows", {"table_id": tid, "limit": 5000})
        assert limit == {
            "error": "invalid_limit",
            "message": "limit must be a whole number from 1 to 1000.",
            "path": "limit",
        }
        assert _dispatch(world, "query_rows", {"table_id": tid, "offset": -1})["error"] == \
            "invalid_offset"
        assert _dispatch(world, "query_rows", {"table_id": tid, "limit": True})["error"] == \
            "invalid_args"
        assert _dispatch(world, "query_rows", {})["error"] == "invalid_args"

    def test_truncation_halves_rows_with_a_hint(self, world, monkeypatch):
        import chat.gemini_api.tool_handlers.tables as handlers

        tid = world.t["user"]["id"]
        full = _dispatch_raw(world, "query_rows", {"table_id": tid})
        monkeypatch.setattr(handlers, "TABLE_QUERY_MAX_BYTES", 600)
        raw = _dispatch_raw(world, "query_rows", {"table_id": tid})
        assert len(full.encode()) > 600 >= len(raw.encode())
        got = json.loads(raw)
        assert got["truncated"] is True and got["total"] == 7
        n = len(got["rows"])
        assert n == 3  # 7 rows halved to 3
        assert [r["_id"] for r in got["rows"]] == list(range(1, n + 1))
        assert f"offset={n}" in got["hint"]
        # Paging from an offset names the next offset after it.
        page = _dispatch(world, "query_rows", {"table_id": tid, "offset": 2})
        assert page["truncated"] is True and len(page["rows"]) == 2  # 5 halved to 2
        assert [r["_id"] for r in page["rows"]] == [3, 4]
        assert "offset=4" in page["hint"]

    def test_single_row_over_the_cap_terminates(self, world, monkeypatch):
        import chat.gemini_api.tool_handlers.tables as handlers

        table = make_table(world, "Big")
        _insert(world, table, [{"Name": "x" * 5000}])
        monkeypatch.setattr(handlers, "TABLE_QUERY_MAX_BYTES", 1000)
        got = _dispatch(world, "query_rows", {"table_id": table["id"]})
        assert got["rows"] == [] and got["truncated"] is True and got["total"] == 1
        assert "columns" in got["hint"]


# ---------------------------------------------------------------------------
# 6. Script bridge
# ---------------------------------------------------------------------------


class TestScripts:
    def test_dispatch_drops_project_context(self, world):
        t = world.t
        # Even if a project id reached dispatch, a script sees user tables only.
        listed = _dispatch(world, "list_tables", {}, cid=None, is_script=True,
                           project_id=world.private_project)
        assert _ids(listed) == {t["user"]["id"], t["bob"]["id"]}
        for row in listed["tables"]:
            assert row["writable"] == "denied" and row["write_note"] == DENY_SCRIPT
        assert _dispatch(world, "list_tables", {"scope": "project"}, cid=None,
                         is_script=True, project_id=world.private_project)["tables"] == []
        hidden = _dispatch(world, "get_table", {"table_id": t["private"]["id"]},
                           cid=None, is_script=True, project_id=world.private_project)
        assert hidden["error"] == "table_not_found"

    def test_script_bridge(self, world):
        from chat.gemini_api.script_tool_call import (
            ScriptToolCallRequest,
            script_tool_call_endpoint,
        )

        t = world.t
        alice = world.users["alice"]

        def call(tool, arguments):
            return _run(script_tool_call_endpoint(
                ScriptToolCallRequest(tool_name=tool, arguments=arguments),
                user=alice, lease=None,
            ))

        listed = json.loads(call("list_tables", {}).body)
        assert _ids(listed) == {t["user"]["id"], t["bob"]["id"]}
        got = json.loads(call("get_table", {"table_id": t["user"]["id"]}).body)
        assert got["writable"] == "denied" and got["write_note"] == DENY_SCRIPT
        assert len(got["sample_rows"]) == 5
        rows = json.loads(call("query_rows", {"table_id": t["user"]["id"], "limit": 3}).body)
        assert rows["total"] == 7 and len(rows["rows"]) == 3
        for key in ("private", "public"):
            hidden = json.loads(call("get_table", {"table_id": t[key]["id"]}).body)
            assert hidden == {
                "error": "table_not_found",
                "message": table_not_found_message(t[key]["id"]),
            }


# ---------------------------------------------------------------------------
# 7. Gate closed
# ---------------------------------------------------------------------------


class TestGateClosed:
    def test_tables_disabled_before_any_db_work(self, world, monkeypatch):
        tid = world.t["user"]["id"]
        world.fg.set_feature_enabled(world.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(world.store, "get_table", boom)
        monkeypatch.setattr(world.store, "list_accessible_tables", boom)
        expected = {"error": "tables_disabled", "message": tables_disabled_message()}
        for tool, args in (
            ("list_tables", {"limit": "nonsense"}),
            ("get_table", {"table_id": tid}),
            ("query_rows", {"table_id": tid, "filter": "not json"}),
        ):
            assert _dispatch(world, tool, args) == expected, tool
            assert _dispatch(world, tool, args, project_id=world.public_project,
                             is_public=True) == expected, tool
            assert _dispatch(world, tool, args, cid=None, is_script=True) == expected, tool
