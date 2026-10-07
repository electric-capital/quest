"""Tests for the cross-user subagent feature (run_user_subagent /
subagent_return handlers, tool tiers, and the return-file copy path).

DB-touching paths are exercised with monkeypatched store functions; the
file verification / copy paths run against real tmp-dir workspaces.
"""

import asyncio
from pathlib import Path

import pytest

from chat.action_request_types.run_user_subagent import RunUserSubagentHandler
from chat.action_request_types.subagent_return import (
    SubagentReturnHandler,
    prepare_return_params,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# run_user_subagent: validate_params
# ---------------------------------------------------------------------------


def test_run_user_subagent_rejects_unknown_field():
    handler = RunUserSubagentHandler()
    with pytest.raises(ValueError, match="Unknown parameter for run_user_subagent"):
        handler.validate_params({
            "target_user_email": "b@example.com",
            "prompt": "hi",
            "bogus": 1,
        })


def test_run_user_subagent_requires_email_and_prompt():
    handler = RunUserSubagentHandler()
    with pytest.raises(ValueError, match="target_user_email is required"):
        handler.validate_params({"prompt": "hi"})
    with pytest.raises(ValueError, match="does not look like an email"):
        handler.validate_params({"target_user_email": "nope", "prompt": "hi"})
    with pytest.raises(ValueError, match="prompt is required"):
        handler.validate_params({"target_user_email": "b@example.com"})
    with pytest.raises(ValueError, match="prompt is too long"):
        handler.validate_params({
            "target_user_email": "b@example.com",
            "prompt": "x" * 20_001,
        })


def test_run_user_subagent_normalizes_and_dedupes():
    handler = RunUserSubagentHandler()
    validated = handler.validate_params({
        "target_user_email": "  B@Example.COM ",
        "prompt": "  do the thing  ",
        "skill_ids": ["s1", "s2", "s1"],
    })
    assert validated == {
        "target_user_email": "b@example.com",
        "prompt": "do the thing",
        "skill_ids": ["s1", "s2"],
    }


def test_run_user_subagent_rejects_system_skills_and_overflow():
    handler = RunUserSubagentHandler()
    with pytest.raises(ValueError, match="system:"):
        handler.validate_params({
            "target_user_email": "b@example.com",
            "prompt": "hi",
            "skill_ids": ["system:slack"],
        })
    with pytest.raises(ValueError, match="at most 10 skills"):
        handler.validate_params({
            "target_user_email": "b@example.com",
            "prompt": "hi",
            "skill_ids": [f"s{i}" for i in range(11)],
        })


def test_run_user_subagent_validates_model():
    handler = RunUserSubagentHandler()
    with pytest.raises(ValueError, match="Unknown model"):
        handler.validate_params({
            "target_user_email": "b@example.com",
            "prompt": "hi",
            "model": "gpt-oops",
        })
    validated = handler.validate_params({
        "target_user_email": "b@example.com",
        "prompt": "hi",
        "model": "claude-sonnet-5",
    })
    assert validated["model"] == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# run_user_subagent: upstream validation (target user + skill visibility)
# ---------------------------------------------------------------------------


def _upstream_params(**overrides):
    params = {
        "target_user_email": "b@example.com",
        "prompt": "hi",
        "skill_ids": ["s1"],
    }
    params.update(overrides)
    return params


@pytest.fixture(autouse=True)
def _user_subagents_feature_enabled(tmp_path, monkeypatch):
    """Open the server-global cross-user subagent gate for these tests.

    validate_against_upstream/execute check the admin feature gate (off by
    default) before the target/skill checks; gate-closed behavior itself is
    covered in tests/test_feature_gates.py.
    """
    import config.feature_gates as fg

    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
    fg.set_feature_enabled(fg.FEATURE_USER_SUBAGENTS, True)


def test_upstream_rejects_missing_target_user(monkeypatch):
    import db.user_store as user_store

    async def _none(email):
        return None

    monkeypatch.setattr(user_store, "get_user_by_email", _none)
    handler = RunUserSubagentHandler()
    with pytest.raises(ValueError, match="No Quest user exists"):
        _run(handler.validate_against_upstream(_upstream_params(), {"id": 1}))


def test_upstream_rejects_skill_invisible_to_target(monkeypatch):
    import db.skill_store as skill_store
    import db.user_store as user_store

    async def _target(email):
        return {"id": 2, "email": email, "name": "Bee"}

    async def _cannot(user_id, skill_id):
        return False

    monkeypatch.setattr(user_store, "get_user_by_email", _target)
    monkeypatch.setattr(skill_store, "user_can_access_skill", _cannot)
    handler = RunUserSubagentHandler()
    with pytest.raises(ValueError, match="cannot access these skills"):
        _run(handler.validate_against_upstream(_upstream_params(), {"id": 1}))


def test_upstream_injects_names(monkeypatch):
    import db.skill_store as skill_store
    import db.user_store as user_store

    async def _target(email):
        return {"id": 2, "email": email, "name": "Bee"}

    async def _can(user_id, skill_id):
        return True

    async def _by_ids(user_id, skill_ids):
        return [{"id": sid, "name": f"Skill {sid}"} for sid in skill_ids]

    monkeypatch.setattr(user_store, "get_user_by_email", _target)
    monkeypatch.setattr(skill_store, "user_can_access_skill", _can)
    monkeypatch.setattr(skill_store, "get_accessible_skills_by_ids", _by_ids)
    handler = RunUserSubagentHandler()
    params = _run(
        handler.validate_against_upstream(_upstream_params(), {"id": 1})
    )
    assert params["target_user_name"] == "Bee"
    assert params["skill_names"] == ["Skill s1"]

    preview = _run(handler.render_preview(params, None))
    keys = [f["key"] for f in preview]
    assert keys == ["Target User", "Prompt", "Autoload Skills"]
    assert preview[0]["value"] == "Bee (b@example.com)"


# ---------------------------------------------------------------------------
# subagent_return: validate_params
# ---------------------------------------------------------------------------


def test_subagent_return_rejects_unknown_field():
    handler = SubagentReturnHandler()
    with pytest.raises(ValueError, match="Unknown parameter for subagent_return"):
        handler.validate_params({"response": "done", "extra": True})


def test_subagent_return_requires_response():
    handler = SubagentReturnHandler()
    with pytest.raises(ValueError, match="response is required"):
        handler.validate_params({})
    with pytest.raises(ValueError, match="response is too long"):
        handler.validate_params({"response": "x" * 50_001})


def test_subagent_return_file_list_shape():
    handler = SubagentReturnHandler()
    with pytest.raises(ValueError, match="files must be a list"):
        handler.validate_params({"response": "ok", "files": "a.txt"})
    with pytest.raises(ValueError, match="at most 10"):
        handler.validate_params({
            "response": "ok",
            "files": [f"f{i}.txt" for i in range(11)],
        })
    validated = handler.validate_params({
        "response": " ok ",
        "files": ["a.txt", "a.txt", "b.txt"],
    })
    assert validated == {"response": "ok", "files": ["a.txt", "b.txt"]}


# ---------------------------------------------------------------------------
# subagent_return: pre-card file verification + preview enrichment
# ---------------------------------------------------------------------------


@pytest.fixture()
def _workspaces(tmp_path, monkeypatch):
    """Two conversation dirs under a patched CHATS_DIR (real resolvers).

    The caller conversation is a PROJECT conversation of ``p1`` (its
    chat_history.json and conversation meta carry ``project_id``), and
    ``p1``'s project workspace exists with one file in it, to prove returned
    files land in the caller's own conversation workspace and the project
    workspace is left untouched.
    """
    import json

    import chat.storage as storage_mod

    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects)

    async def _meta(user_id, conversation_id):
        if conversation_id == "caller-conv":
            return {"id": conversation_id, "user_id": 1, "project_id": "p1"}
        if conversation_id == "sub-conv":
            return {"id": conversation_id, "user_id": 2, "project_id": None}
        return None

    monkeypatch.setattr("db.conversation_store.get_conversation_meta", _meta)

    sub_dir = chats / "sub-conv"
    caller_dir = chats / "caller-conv"
    (sub_dir / "workspace").mkdir(parents=True)
    (caller_dir / "workspace").mkdir(parents=True)
    (caller_dir / "chat_history.json").write_text(json.dumps({
        "id": "caller-conv", "user_id": 1, "project_id": "p1", "messages": [],
    }))
    project_root = storage_mod.ChatStorage.create_project_workspace("p1")
    (project_root / "shared.txt").write_text("project file")
    return sub_dir, caller_dir


_RUN = {
    "id": "run-1",
    "caller_user_id": 1,
    "target_user_id": 2,
    "caller_conversation_id": "caller-conv",
    "subagent_conversation_id": "sub-conv",
    "wait_handle_id": "wh-1",
    "status": "running",
}


def test_prepare_return_params_verifies_and_enriches(_workspaces, monkeypatch):
    sub_dir, _ = _workspaces
    (sub_dir / "workspace" / "report.md").write_text("# hi")

    import db.user_store as user_store

    async def _caller(user_id):
        return {"id": user_id, "email": "a@example.com", "name": "Aye"}

    monkeypatch.setattr(user_store, "get_user_by_id", _caller)

    params = _run(prepare_return_params(
        {"response": "done", "files": ["report.md"]}, dict(_RUN), "sub-conv",
    ))
    assert params["file_entries"] == [
        {"path": "report.md", "name": "report.md", "size_bytes": 4},
    ]
    assert params["caller_email"] == "a@example.com"
    assert params["run_id"] == "run-1"

    handler = SubagentReturnHandler()
    preview = _run(handler.render_preview(params, None))
    files_field = next(f for f in preview if f["key"] == "Files")
    assert files_field["type"] == "subagent_return_files"
    assert files_field["files"] == params["file_entries"]


def test_prepare_return_params_rejects_missing_and_escaping_files(_workspaces):
    with pytest.raises(ValueError, match="File not found"):
        _run(prepare_return_params(
            {"response": "done", "files": ["nope.md"]}, dict(_RUN), "sub-conv",
        ))
    with pytest.raises(ValueError, match="Invalid file path"):
        _run(prepare_return_params(
            {"response": "done", "files": ["../secret.txt"]},
            dict(_RUN), "sub-conv",
        ))


# ---------------------------------------------------------------------------
# subagent_return: execute copies files with no-clobber suffixing
# ---------------------------------------------------------------------------


def test_subagent_return_execute_copies_files(_workspaces, monkeypatch):
    sub_dir, caller_dir = _workspaces
    (sub_dir / "workspace" / "report.md").write_text("fresh")
    dest_dir = caller_dir / "workspace" / ".subagent_responses"
    dest_dir.mkdir(parents=True)
    (dest_dir / "report.md").write_text("old")  # forces -2 suffix

    import db.tool_wait_handle_store as wh_store
    import db.user_subagent_run_store as run_store
    from chat import user_subagent

    async def _get_run(conversation_id):
        return dict(_RUN)

    async def _get_handle(handle_id):
        return {"id": handle_id, "status": "pending"}

    updates = []

    async def _update_status(run_id, status, error=None):
        updates.append((run_id, status))
        return {**_RUN, "status": status}

    resolved = {}

    async def _resolve_caller(run, new_status, response):
        resolved["status"] = new_status
        resolved["response"] = response
        return {"id": run["wait_handle_id"], "status": new_status}

    monkeypatch.setattr(
        run_store, "get_run_by_subagent_conversation", _get_run,
    )
    monkeypatch.setattr(run_store, "update_run_status", _update_status)
    monkeypatch.setattr(wh_store, "get_handle", _get_handle)
    monkeypatch.setattr(user_subagent, "resolve_caller_handle", _resolve_caller)

    handler = SubagentReturnHandler()
    result = _run(handler.execute(
        {"response": "done", "files": ["report.md"]},
        {"id": 2, "email": "b@example.com"},
        conversation_id="sub-conv",
    ))

    assert result["success"] is True
    assert result["files_returned"] == [".subagent_responses/report-2.md"]
    assert (dest_dir / "report-2.md").read_text() == "fresh"
    # The caller is a project conversation; nothing went to the project space.
    project_root = caller_dir.parent.parent / "projects" / "p1" / "workspace" / "workspace"
    assert [p.name for p in project_root.rglob("*")] == ["shared.txt"]
    assert (project_root / "shared.txt").read_text() == "project file"
    assert (dest_dir / "report.md").read_text() == "old"
    assert updates == [("run-1", "returned")]
    assert resolved["status"] == "accepted"
    assert resolved["response"]["status"] == "returned"
    assert resolved["response"]["files"] == [".subagent_responses/report-2.md"]


def test_subagent_return_execute_refuses_when_caller_stopped_waiting(
    _workspaces, monkeypatch,
):
    sub_dir, _ = _workspaces
    (sub_dir / "workspace" / "report.md").write_text("fresh")

    import db.tool_wait_handle_store as wh_store
    import db.user_subagent_run_store as run_store

    async def _get_run(conversation_id):
        return dict(_RUN)

    async def _get_handle(handle_id):
        return {"id": handle_id, "status": "timed_out"}

    monkeypatch.setattr(
        run_store, "get_run_by_subagent_conversation", _get_run,
    )
    monkeypatch.setattr(wh_store, "get_handle", _get_handle)

    handler = SubagentReturnHandler()
    with pytest.raises(ValueError, match="no longer waiting"):
        _run(handler.execute(
            {"response": "done", "files": ["report.md"]},
            {"id": 2, "email": "b@example.com"},
            conversation_id="sub-conv",
        ))


# ---------------------------------------------------------------------------
# Tool tiers + schema surface
# ---------------------------------------------------------------------------


def test_user_subagent_tools_tier():
    from chat.llm.tool_schemas import USER_SUBAGENT_TOOLS

    names = {t["name"] for t in USER_SUBAGENT_TOOLS}
    assert "return_to_caller" in names
    assert "create_action_request" not in names
    assert "agent_task" not in names
    assert "agent_task_parallel" not in names
    assert "wait_for_handles" not in names
    # Base read/skill tools stay available.
    assert {"tool_call", "run_python", "load_skills"} <= names


def test_create_action_request_enum_gates_subagent_types():
    from chat.llm.tool_schemas import TOP_LEVEL_TOOLS

    spec = next(
        t for t in TOP_LEVEL_TOOLS if t["name"] == "create_action_request"
    )
    enum = set(spec["parameters"]["properties"]["request_type"]["enum"])
    assert "run_user_subagent" in enum
    assert "subagent_return" not in enum


def test_system_user_subagents_skill_documents_flow():
    from chat.system_skills import CATALOG

    skill = CATALOG["system:user_subagents"]
    content = skill.content_builder("http://localhost:8000", "test-key")
    assert "run_user_subagent" in content
    assert "wait_for_handles" in content
    assert ".subagent_responses" in content
    # The feature is flag-gated; the skill must say so.
    assert "user_subagents" in content
    assert "%%flags[user_subagents]" in content


def test_user_subagents_conversation_flag_registered():
    from chat.conversation_flags import (
        FLAG_LABELS,
        FLAG_USER_SUBAGENTS,
        KNOWN_FLAGS,
        is_flag_enabled,
        parse_flags_line,
    )

    assert FLAG_USER_SUBAGENTS in KNOWN_FLAGS
    assert FLAG_USER_SUBAGENTS in FLAG_LABELS
    flags, stripped = parse_flags_line("%%flags[user_subagents]\ngo")
    assert flags == [FLAG_USER_SUBAGENTS]
    assert stripped == "go"
    assert is_flag_enabled(flags, FLAG_USER_SUBAGENTS)
    assert not is_flag_enabled(None, FLAG_USER_SUBAGENTS)
