"""The legacy project database is retired for projects without tables in it.

``chat/project_db.py``: a project keeps ``project_db_query``, the
``system:project_db`` skill and the "Project Database" prompt paragraph only
while its ``project.db`` holds at least one user table. Every other project
(new, never used it, or all tables dropped) has none of them, and the
handler refuses, so no project can start a new database.
"""

import asyncio
import json
import sqlite3
from unittest.mock import patch

import pytest

import chat.storage as storage_mod
from chat.gemini_api.system_prompt import (
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
)
from chat.project_db import (
    PROJECT_DB_SERVICE_KEY,
    project_db_retired_message,
    project_has_legacy_db,
    with_project_db_service,
)
from tests.prompt_support import tool_offered

PARAGRAPH = "**Project Database:**"


@pytest.fixture()
def no_plugins():
    from config import plugins as plugins_mod
    with patch.object(plugins_mod, "_LOADED", []), \
            patch("config.service_credentials.read_service_credentials",
                  return_value=None):
        yield


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)
    return projects


def _make_db(projects_dir, project_id, *tables):
    db = projects_dir / project_id / "project.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    for name in tables:
        conn.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, v TEXT)')
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# project_has_legacy_db
# ---------------------------------------------------------------------------


class TestProjectHasLegacyDb:
    def test_no_project(self, projects_dir):
        assert project_has_legacy_db(None) is False
        assert project_has_legacy_db("") is False

    def test_missing_database(self, projects_dir):
        assert project_has_legacy_db("fresh") is False
        (projects_dir / "fresh").mkdir(parents=True)
        assert project_has_legacy_db("fresh") is False

    def test_empty_database_is_not_legacy(self, projects_dir):
        # A file created lazily by a query that never made a table.
        _make_db(projects_dir, "empty")
        assert project_has_legacy_db("empty") is False

    def test_database_with_a_table(self, projects_dir):
        _make_db(projects_dir, "legacy", "tasks")
        assert project_has_legacy_db("legacy") is True

    def test_dropping_the_last_table_closes_the_gate(self, projects_dir):
        db = _make_db(projects_dir, "legacy", "tasks")
        assert project_has_legacy_db("legacy") is True
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE tasks")
        conn.commit()
        conn.close()
        assert project_has_legacy_db("legacy") is False

    def test_invalid_project_id(self, projects_dir):
        assert project_has_legacy_db("../x") is False

    def test_unreadable_file_counts_as_legacy(self, projects_dir):
        # Never hide an existing project's data over a read error.
        db = projects_dir / "broken" / "project.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"not a sqlite file at all, long enough to fail the header check")
        assert project_has_legacy_db("broken") is True

    def test_check_does_not_create_the_file(self, projects_dir):
        assert project_has_legacy_db("fresh") is False
        assert not (projects_dir / "fresh" / "project.db").exists()


# ---------------------------------------------------------------------------
# Connected-services pseudo-key
# ---------------------------------------------------------------------------


class TestServiceKey:
    def test_user_level_maps_default_closed(self, no_plugins):
        from api.instructions import (
            PSEUDO_SERVICE_KEYS,
            get_public_connected_services,
            get_user_connected_services,
        )
        user = {"email": "u@example.com"}
        assert PROJECT_DB_SERVICE_KEY in PSEUDO_SERVICE_KEYS
        assert get_user_connected_services(user)[PROJECT_DB_SERVICE_KEY] is False
        assert get_public_connected_services(user)[PROJECT_DB_SERVICE_KEY] is False

    def test_with_project_db_service(self, projects_dir):
        _make_db(projects_dir, "legacy", "tasks")
        services = with_project_db_service({"slack": True}, "legacy")
        assert services == {"slack": True, PROJECT_DB_SERVICE_KEY: True}
        assert with_project_db_service({}, "fresh") == {PROJECT_DB_SERVICE_KEY: False}
        assert with_project_db_service({}, None) == {PROJECT_DB_SERVICE_KEY: False}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _query(project_id, sql):
    from chat.gemini_api.tool_handlers.project_db import _handle_project_db_query
    return json.loads(asyncio.run(_handle_project_db_query(project_id, sql)))


class TestHandler:
    def test_fresh_project_cannot_create_a_table(self, projects_dir):
        result = _query("fresh", "CREATE TABLE t (id INTEGER)")
        assert result == {"error": project_db_retired_message()}
        assert not (projects_dir / "fresh" / "project.db").exists()

    def test_fresh_project_cannot_read_either(self, projects_dir):
        result = _query("fresh", "SELECT 1")
        assert result == {"error": project_db_retired_message()}

    def test_standalone_conversation_keeps_its_own_error(self, projects_dir):
        result = _query(None, "SELECT 1")
        assert "only available in project conversations" in result["error"]

    def test_legacy_project_still_works(self, projects_dir):
        _make_db(projects_dir, "legacy", "tasks")
        assert _query("legacy", "INSERT INTO tasks (v) VALUES ('a')")["status"] == "ok"
        assert _query("legacy", "SELECT v FROM tasks")["rows"] == [["a"]]
        # Projects with tables keep the full feature, new tables included.
        assert _query("legacy", "CREATE TABLE more (id INTEGER)")["status"] == "ok"

    def test_retired_message_points_at_tables(self):
        assert "Quest Tables" in project_db_retired_message()
        assert "system:tables" in project_db_retired_message()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

LEGACY = {"tables": True, PROJECT_DB_SERVICE_KEY: True}
NO_TABLES = {"tables": True, PROJECT_DB_SERVICE_KEY: False}


def _private_prompts(services):
    return {
        "standard": get_system_prompt("k", connected_services=services, has_project=True),
        "routine": get_system_prompt(
            "k", connected_services=services, has_project=True, is_routine=True,
        ),
        "sub_agent": get_sub_agent_system_prompt(
            "helper", "k", connected_services=services, has_project=True,
        ),
    }


class TestPrompts:
    def test_legacy_project_keeps_everything(self):
        for kind, prompt in _private_prompts(LEGACY).items():
            assert PARAGRAPH in prompt, kind
            assert "already holds tables" in prompt, kind
            assert tool_offered(prompt, "project_db_query"), kind
            assert "`system:project_db`" in prompt, kind
            # The Quest Tables legacy note still follows the paragraph.
            assert prompt.index("The project database is legacy") > prompt.index(PARAGRAPH), kind

    def test_project_without_tables_has_none_of_it(self):
        for kind, prompt in _private_prompts(NO_TABLES).items():
            assert PARAGRAPH not in prompt, kind
            assert "project_db_query" not in prompt, kind
            assert "system:project_db" not in prompt, kind
            assert "The project database is legacy" not in prompt, kind
            # The rest of the project text is unchanged.
            assert "Two file spaces" in prompt or "proj://" in prompt, kind

    def test_gate_changes_only_project_db_lines(self):
        legacy = _private_prompts(LEGACY)
        for kind, prompt in _private_prompts(NO_TABLES).items():
            removed = [
                line for line in legacy[kind].splitlines()
                if line not in prompt.splitlines()
            ]
            added = [
                line for line in prompt.splitlines()
                if line not in legacy[kind].splitlines()
            ]
            assert added == [], (kind, added)
            assert removed, kind
            for line in removed:
                assert "project_db" in line or "project database" in line.lower(), (kind, line)

    def test_unknown_context_shows_everything(self):
        # connected_services=None keeps the show-everything behaviour that
        # tests and admin tooling rely on.
        prompt = get_system_prompt("k", connected_services=None, has_project=True)
        assert PARAGRAPH in prompt
        assert tool_offered(prompt, "project_db_query")

    def test_public_prompt_follows_the_flag(self):
        closed = get_public_project_system_prompt(user_email="u@example.com")
        assert PARAGRAPH not in closed
        assert "project_db_query" not in closed
        opened = get_public_project_system_prompt(
            user_email="u@example.com", project_db_enabled=True,
        )
        assert PARAGRAPH in opened
        assert tool_offered(opened, "project_db_query")


# ---------------------------------------------------------------------------
# Tool spec and skill
# ---------------------------------------------------------------------------


def test_tool_spec_and_skill_are_gated_on_the_key():
    from chat.llm.tool_schemas import TOOL_CALL_REGISTRY, tool_is_advertised
    from chat.system_skills import CATALOG

    spec = TOOL_CALL_REGISTRY["project_db_query"]
    assert spec["requires_service"] == PROJECT_DB_SERVICE_KEY
    assert tool_is_advertised(spec, NO_TABLES) is False
    assert tool_is_advertised(spec, LEGACY) is True
    assert tool_is_advertised(spec, None) is True

    skill = CATALOG["system:project_db"]
    assert skill.requires == PROJECT_DB_SERVICE_KEY
    assert skill.requires_project is True
    assert "deprecated" in skill.content_builder("", "")
