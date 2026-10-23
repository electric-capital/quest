"""Quest Tables prompting (issue #110 phase A14).

Covers:

1. The ``system:tables`` system skill: registration, gated on the
   ``tables`` pseudo-key in the enumeration and at load time (refused with
   the feature-gate text), and content that names the real tools, request
   types, schema operations, filter ops and column fields, with examples
   that pass the real validators.
2. Prompt gating per variant: the seven table tools, the skill line and
   the project-database note appear only while the gate is open
   (standalone, project, Slack, routine, sub-agent, cross-user subagent,
   inference); the read-only tiers never offer the row writes.
3. The public-project "Quest Tables" paragraph and tools follow
   ``tables_enabled`` only.
4. No table titles in any prompt (discovery is ``list_tables``), and a
   gate-open prompt differs from the gate-closed one in table lines only.
"""

import json
import re
from unittest.mock import patch

import pytest

import config.feature_gates as fg
from chat.project_db import PROJECT_DB_SERVICE_KEY
from chat.tables.constants import (
    TABLE_WRITE_MAX_ROWS,
    TABLES_SERVICE_KEY,
    tables_disabled_message,
)
from tests.prompt_support import core_bullet, named_under_skill, tool_offered

TABLE_TOOLS = (
    "list_tables", "get_table", "query_rows",
    "insert_rows", "update_rows", "upsert_rows", "delete_rows",
)
READ_TOOLS = ("list_tables", "get_table", "query_rows")
WRITE_TOOLS = ("insert_rows", "update_rows", "upsert_rows", "delete_rows")
REQUEST_TYPES = ("create_table", "change_table_schema", "write_table_rows")

BASE_URL = "http://localhost:8000"
API_KEY = "test-api-key"
# A project that still holds legacy project-database tables, so the
# "Project Database" paragraph (and its legacy note) is rendered.
OPEN = {TABLES_SERVICE_KEY: True, PROJECT_DB_SERVICE_KEY: True}
CLOSED = {TABLES_SERVICE_KEY: False, PROJECT_DB_SERVICE_KEY: True}
DB_NOTE = "The project database is legacy"


def _skill_content() -> str:
    from chat.system_skills import CATALOG
    return CATALOG["system:tables"].content_builder(BASE_URL, API_KEY)


def _offered(prompt: str, name: str) -> bool:
    """Table tools are skill-documented: offered by being named on the
    `system:tables` enumeration line, never as a core bullet."""
    assert core_bullet(name) not in prompt, name
    return named_under_skill(prompt, "system:tables", name)


def _prompts(services, *, has_project=False) -> dict[str, str]:
    from chat.gemini_api.system_prompt import (
        get_inference_api_system_prompt,
        get_sub_agent_system_prompt,
        get_system_prompt,
        get_user_subagent_system_prompt,
    )
    return {
        "standard": get_system_prompt(
            API_KEY, base_url=BASE_URL, connected_services=services,
            has_project=has_project,
        ),
        "slack": get_system_prompt(
            API_KEY, base_url=BASE_URL, connected_services=services,
            has_project=has_project, is_slack=True,
        ),
        "routine": get_system_prompt(
            API_KEY, base_url=BASE_URL, connected_services=services,
            has_project=has_project, is_routine=True,
        ),
        "sub_agent": get_sub_agent_system_prompt(
            "helper", API_KEY, base_url=BASE_URL, connected_services=services,
            has_project=has_project, can_nest=True,
        ),
        "user_subagent": get_user_subagent_system_prompt(
            API_KEY, base_url=BASE_URL, connected_services=services,
        ),
        "inference": get_inference_api_system_prompt(
            API_KEY, base_url=BASE_URL, connected_services=services,
        ),
    }


READ_ONLY = ("sub_agent", "user_subagent", "inference")


@pytest.fixture()
def gates_file(tmp_path, monkeypatch):
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.fixture()
def no_plugins():
    from config import plugins as plugins_mod
    with patch.object(plugins_mod, "_LOADED", []), \
            patch("config.service_credentials.read_service_credentials",
                  return_value=None):
        yield


# ---------------------------------------------------------------------------
# 1. system:tables
# ---------------------------------------------------------------------------


class TestTablesSkill:
    def test_registration(self):
        from chat.system_skills import CATALOG
        skill = CATALOG["system:tables"]
        assert skill.name == "Quest Tables"
        assert skill.requires == TABLES_SERVICE_KEY
        assert skill.requires_project is False
        assert 0 < len(skill.description) <= 120
        for name in REQUEST_TYPES:
            assert name in skill.description, name

    def test_registry_tools_documented_by_skill(self):
        from chat.gemini_api.system_prompt import _table_tool_names, _table_write_tool_names
        from chat.llm.tool_schemas import tools_for_skill
        assert _table_tool_names() == frozenset(TABLE_TOOLS)
        assert _table_write_tool_names() == frozenset(WRITE_TOOLS)
        assert set(tools_for_skill("system:tables")) == set(TABLE_TOOLS)

    def test_enumeration_follows_gate(self):
        from chat.system_skills import build_system_skills_enumeration, list_system_skills
        assert "system:tables" in {s.id for s in list_system_skills(OPEN)}
        assert "system:tables" not in {s.id for s in list_system_skills(CLOSED)}
        assert "system:tables" not in {s.id for s in list_system_skills({})}
        assert "system:tables" in build_system_skills_enumeration(OPEN)
        assert "system:tables" not in build_system_skills_enumeration(CLOSED)

    def test_load_follows_gate(self):
        from chat.system_skills import load_system_skills
        [loaded] = load_system_skills(["system:tables"], OPEN, BASE_URL, API_KEY)
        assert "error" not in loaded
        # The loader appends the registry-rendered tool reference.
        assert "Tool reference" in loaded["content"]
        for name in TABLE_TOOLS:
            assert name in loaded["content"], name
        [refused] = load_system_skills(["system:tables"], CLOSED, BASE_URL, API_KEY)
        assert "content" not in refused
        assert refused["error"] == tables_disabled_message()
        assert "Data Connections" not in refused["error"]

    def test_content_names_tools_request_types_and_handoff(self):
        content = _skill_content()
        for name in TABLE_TOOLS + REQUEST_TYPES:
            assert f"`{name}" in content or f'"{name}"' in content, name
        assert 'request_type="write_table_rows"' in content
        assert "params=<suggested_request.params>" in content
        assert "approval_required" in content and "suggested_request" in content
        assert "expected_schema_version" in content
        assert "stale_schema" in content and "rows_violate" in content
        assert "ONE card holding" in content

    def test_content_covers_rules(self):
        content = _skill_content()
        # project_db_query is legacy; tables are the default.
        assert "`project_db_query`) is\nlegacy" in content
        assert "new structured data goes into tables" in content
        # Numbers, caps, paging, ids, refresh, restore.
        assert '"1,234.5"' in content
        assert "15\n  significant digits" in content
        assert f"at most {TABLE_WRITE_MAX_ROWS} rows per call" in content
        assert "invalid_rows" in content and "problems" in content
        for word in ("offset", "total", "truncated", "hint", "count_only"):
            assert f"`{word}" in content, word
        assert "`_id`s come only from queries" in content
        assert 'key_column="SKU"' in content and "`unique`" in content
        assert "**History** panel" in content
        assert 'schema_change: "denied"' in content
        assert "Quest UI (Tables)" in content

    def test_content_names_every_schema_operation(self):
        from chat.tables.schema_ops import _OP_PARAMS
        content = _skill_content()
        for op, (required, optional) in _OP_PARAMS.items():
            assert f"`{op}`" in content, op
            for field in required + optional:
                assert f"`{field}" in content, (op, field)

    def test_content_names_every_filter_op_and_column_field(self):
        from chat.tables.constants import TABLE_COLUMN_TYPES
        from chat.tables.filters import ALL_OPS
        from chat.tables.schema import _SPEC_FIELDS
        content = _skill_content()
        for op in ALL_OPS:
            assert f"`{op}`" in content, op
        for field in _SPEC_FIELDS:
            if field != "key":  # server-assigned, never exposed to models
                assert f"`{field}`" in content, field
        for type_name in TABLE_COLUMN_TYPES:
            assert f"`{type_name}`" in content, type_name
        assert "`key`" not in content

    def test_examples_pass_the_validators(self):
        """The create_table / change_table_schema params and the filter and
        sort examples in the skill are accepted by the real validators."""
        from chat.action_request_types.change_table_schema import ChangeTableSchemaHandler
        from chat.action_request_types.create_table import CreateTableHandler
        from chat.tables.filters import parse_filter, parse_sort
        from chat.tables.schema import parse_columns
        from chat.tables.schema_ops import apply_schema_operations

        content = _skill_content()

        def params_of(request_type: str) -> dict:
            m = re.search(
                rf'request_type="{request_type}",\n    params=(\{{.*?\n    \}}),\n',
                content, re.S,
            )
            assert m, request_type
            return json.loads(m.group(1))

        create = CreateTableHandler().validate_params(params_of("create_table"))
        columns = parse_columns(create["columns"])
        change = ChangeTableSchemaHandler().validate_params(params_of("change_table_schema"))
        apply_schema_operations(columns, change["operations"], table_empty=False)

        query = content[content.index("### Querying"):content.index("### Writing")]
        block = query.split("```")[1]
        decoder = json.JSONDecoder()
        filters, pos, block = [], 0, block.strip()
        while pos < len(block):
            value, end = decoder.raw_decode(block, pos)
            filters.append(value)
            pos = end
            while pos < len(block) and block[pos].isspace():
                pos += 1
        assert len(filters) == 3
        for node in filters:
            assert parse_filter(columns, node) is not None, node
        sort = json.loads(re.search(r"`(\[\{\"column\".*?\}\])`", query).group(1))
        assert parse_sort(columns, sort)

    def test_action_request_index_does_not_leak_tables(self):
        # The ungated generic action-request skill must not point at the
        # gated system:tables skill.
        from chat.system_skills import CATALOG
        index = CATALOG["system:action_requests"].content_builder(BASE_URL, API_KEY)
        assert "system:tables" not in index


# ---------------------------------------------------------------------------
# 2. Prompt gating per variant
# ---------------------------------------------------------------------------


class TestPromptGating:
    @pytest.mark.parametrize("has_project", [False, True])
    def test_closed_gate_hides_everything(self, has_project):
        for kind, prompt in _prompts(CLOSED, has_project=has_project).items():
            for name in TABLE_TOOLS:
                assert not tool_offered(prompt, name), (kind, name)
            assert "system:tables" not in prompt, kind
            assert DB_NOTE not in prompt, kind

    @pytest.mark.parametrize("has_project", [False, True])
    def test_open_gate_offers_tools_and_skill(self, has_project):
        for kind, prompt in _prompts(OPEN, has_project=has_project).items():
            assert "system:tables" in prompt, kind
            for name in READ_TOOLS:
                assert _offered(prompt, name), (kind, name)
            for name in WRITE_TOOLS:
                assert _offered(prompt, name) is (kind not in READ_ONLY), (kind, name)

    def test_project_db_note_only_in_project_prompts_with_gate_open(self):
        project = _prompts(OPEN, has_project=True)
        standalone = _prompts(OPEN, has_project=False)
        for kind in ("standard", "slack", "routine", "sub_agent"):
            prompt = project[kind]
            db = prompt.index("**Project Database:**")
            assert prompt.index(DB_NOTE) > db, kind
            assert DB_NOTE not in standalone[kind], kind

    def test_end_to_end_with_feature_gate(self, gates_file, no_plugins):
        from api.instructions import get_user_connected_services
        from chat.gemini_api.system_prompt import get_system_prompt
        user = {"email": "u@example.com"}
        # A project holding legacy tables, so the note has a paragraph to
        # follow (the per-project key the conversation callers set).
        services = get_user_connected_services(user) | {PROJECT_DB_SERVICE_KEY: True}
        closed = get_system_prompt(
            API_KEY, connected_services=services, has_project=True,
        )
        assert "system:tables" not in closed
        assert not any(tool_offered(closed, n) for n in TABLE_TOOLS)
        fg.set_feature_enabled(fg.FEATURE_TABLES, True)
        services = get_user_connected_services(user) | {PROJECT_DB_SERVICE_KEY: True}
        opened = get_system_prompt(
            API_KEY, connected_services=services, has_project=True,
        )
        assert all(_offered(opened, n) for n in TABLE_TOOLS)
        assert DB_NOTE in opened
        # A capability, not a connector: the proxy preamble is unchanged.
        assert "No API services are currently connected" in opened
        assert "Tables APIs" not in opened

    @pytest.mark.parametrize("has_project", [False, True])
    def test_open_gate_changes_only_table_lines(self, has_project):
        """Gate-open vs gate-closed prompts differ by exactly the expected
        added lines (the golden snapshots render with the gate closed):
        the system:tables enumeration line, plus the project-database note
        in project conversations of the standard and sub-agent prompts."""
        from chat.gemini_api.system_prompt import (
            _PROJECT_DB_TABLES_NOTE,
            _SUB_AGENT_PROJECT_DB_TABLES_NOTE,
        )
        from chat.system_skills import build_system_skills_enumeration

        closed = _prompts(CLOSED, has_project=has_project)
        opened = _prompts(OPEN, has_project=has_project)
        skill_lines = [
            line for line in build_system_skills_enumeration(OPEN).splitlines()
            if line.startswith("- `system:tables`")
        ]
        assert len(skill_lines) == 1
        for kind in closed:
            before = closed[kind].splitlines()
            after = opened[kind].splitlines()
            removed = [line for line in before if line not in after]
            added = [line for line in after if line not in before]
            assert removed == [], (kind, removed)
            [skill_line] = [line for line in added if line.startswith("- `system:tables`")]
            if kind in READ_ONLY:
                # Read-only tiers drop the write tools from the Tools: tail.
                for name in WRITE_TOOLS:
                    assert f"`{name}`" not in skill_line, (kind, name)
            else:
                assert skill_line == skill_lines[0], kind
            expected_extra = []
            if has_project and kind in ("standard", "slack", "routine"):
                expected_extra = [_PROJECT_DB_TABLES_NOTE.rstrip("\n")]
            elif has_project and kind == "sub_agent":
                expected_extra = [_SUB_AGENT_PROJECT_DB_TABLES_NOTE.rstrip("\n")]
            assert [line for line in added if line != skill_line] == expected_extra, kind

    @pytest.mark.parametrize("has_project", [False, True])
    def test_public_paragraph_never_in_private_prompts(self, has_project):
        for services in (OPEN, CLOSED, None):
            for kind, prompt in _prompts(services, has_project=has_project).items():
                assert "**Quest Tables:**" not in prompt, (kind, services)

    def test_sub_agent_note_says_read_only(self):
        from chat.gemini_api.system_prompt import (
            _PROJECT_DB_TABLES_NOTE,
            _SUB_AGENT_PROJECT_DB_TABLES_NOTE,
        )
        prompt = _prompts(OPEN, has_project=True)["sub_agent"]
        assert _SUB_AGENT_PROJECT_DB_TABLES_NOTE.rstrip("\n") in prompt
        assert _PROJECT_DB_TABLES_NOTE.rstrip("\n") not in prompt
        assert "return new records to the top-level agent" in prompt


# ---------------------------------------------------------------------------
# 3. Public-project prompt
# ---------------------------------------------------------------------------


class TestPublicPrompt:
    def _prompt(self, **kw):
        from chat.gemini_api.system_prompt import get_public_project_system_prompt
        return get_public_project_system_prompt(
            user_name="Ada", user_email="ada@example.com",
            project_guide="Track BTC prices.", **kw,
        )

    @pytest.mark.parametrize("docs_enabled", [False, True])
    def test_closed_gate_has_no_tables(self, docs_enabled):
        p = self._prompt(docs_enabled=docs_enabled)
        assert "Quest Tables" not in p
        for name in TABLE_TOOLS:
            assert name not in p, name

    @pytest.mark.parametrize("docs_enabled", [False, True])
    def test_open_gate_paragraph_and_tools(self, docs_enabled):
        p = self._prompt(tables_enabled=True, docs_enabled=docs_enabled)
        for name in TABLE_TOOLS:
            assert core_bullet(name) in p, name
        start = p.index("**Quest Tables:**")
        assert start > p.index("**Boundaries (this is a public project):**")
        end = p.index("**Conversation naming")
        paragraph = p[start:end]
        assert "PUBLIC tables" in paragraph
        assert "private tables" in paragraph and "invisible" in paragraph
        assert "no approval step" in paragraph
        assert "Quest UI (Tables)" in paragraph
        for name in TABLE_TOOLS:
            assert f"`{name}`" in paragraph, name
        # The three table cards (public-safe action requests), with the
        # create / schema examples; no skills, no other gated feature's text.
        for request_type in ("create_table", "change_table_schema", "write_table_rows"):
            assert f'create_action_request(request_type="{request_type}"' in paragraph
        assert '"target": "project"' in paragraph
        assert "system:" not in p
        assert ("Quest Docs" in p) is docs_enabled

    def test_no_project_db_note(self):
        from chat.gemini_api.system_prompt import (
            _PROJECT_DB_TABLES_NOTE,
            _SUB_AGENT_PROJECT_DB_TABLES_NOTE,
        )
        for enabled in (False, True):
            p = self._prompt(tables_enabled=enabled)
            assert _PROJECT_DB_TABLES_NOTE.rstrip("\n") not in p
            assert _SUB_AGENT_PROJECT_DB_TABLES_NOTE.rstrip("\n") not in p
        p = self._prompt(tables_enabled=True)
        assert "propose one with `create_table`" in p

    def test_routine_run_keeps_paragraph(self):
        p = self._prompt(tables_enabled=True, is_routine=True)
        assert "**Quest Tables:**" in p
        assert "- **set_conversation_name**" not in p


# ---------------------------------------------------------------------------
# 4. No table titles in prompts
# ---------------------------------------------------------------------------


SEEDED_TITLE = "Zebra Inventory 7781"


def test_no_prompt_contains_a_seeded_table_title(isolated_db, gates_file, no_plugins):
    """Discovery is list_tables: with a user table and a project table
    seeded and the gate open, no rendered prompt variant names them."""
    import asyncio as _asyncio

    import db.models as models
    import db.project_store as project_store
    import db.table_store as table_store
    from api.instructions import get_user_connected_services
    from chat.gemini_api.system_prompt import get_public_project_system_prompt
    from chat.tables.schema import parse_columns

    async def seed():
        async with isolated_db.AsyncSessionLocal() as db:
            user = models.User(email="u@example.com", api_key="k-zebra")
            db.add(user)
            await db.commit()
            await db.refresh(user)
            uid = user.id
        project = await project_store.create_project(uid, "Zoo")
        schema = [c.to_dict() for c in parse_columns([{"name": "Name", "type": "text"}])]
        user_table = await table_store.create_table(
            uid, SEEDED_TITLE, mode="private", schema=schema,
        )
        await table_store.create_table(
            uid, SEEDED_TITLE + " (project)", mode="private", schema=schema,
            project_id=project["id"],
        )
        assert (await table_store.get_table(user_table["id"]))["title"] == SEEDED_TITLE
        assert len(await table_store.list_table_ids_for_project(project["id"])) == 1
        return {"id": uid, "email": "u@example.com"}

    user = _asyncio.run(seed())

    fg.set_feature_enabled(fg.FEATURE_TABLES, True)
    services = get_user_connected_services(user)
    assert services[TABLES_SERVICE_KEY] is True
    prompts = [
        *_prompts(services, has_project=True).values(),
        *_prompts(services, has_project=False).values(),
        get_public_project_system_prompt(
            user_email=user["email"], tables_enabled=True, docs_enabled=True,
        ),
    ]
    for prompt in prompts:
        assert "Zebra Inventory" not in prompt
