"""Tests for the archive_memory action request.

Covers ``ArchiveMemoryHandler.validate_params`` (required fields, the
reason cap, scope), the pre-card check (unknown / already-archived /
wrong-store ids, the ``memory_content`` snapshot), ``execute`` for both
scopes (archives, stores ``archive_reason``, re-checks at approve time),
the stores' reason handling, and the get-by-id tool view.
"""

import asyncio
import json
import uuid

import pytest

from tests.migration_support import PASSWORD, deployment  # noqa: F401  (fixture)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(isolated_db):
    import db.memory_store as memory_store
    import db.project_memory_store as project_memory_store
    import db.project_store as project_store
    from db.engine import AsyncSessionLocal

    async def _user(name):
        async with AsyncSessionLocal() as db:
            u = isolated_db.models.User(
                email=f"{name}-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name=name,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    class Env:
        pass

    e = Env()
    e.memories = memory_store
    e.project_memories = project_memory_store
    e.alice = _run(_user("alice"))
    e.bob = _run(_user("bob"))
    e.project_id = _run(project_store.create_project(e.alice, "Launch"))["id"]
    e.other_project_id = _run(project_store.create_project(e.alice, "Other"))["id"]
    return e


def _handler():
    from chat.action_request_types.archive_memory import ArchiveMemoryHandler
    return ArchiveMemoryHandler()


def _precard(params, user_id, project_id=None):
    from chat.action_request_types.archive_memory import (
        archive_memory_precard_check,
    )
    validated = _handler().validate_params(params)
    _run(archive_memory_precard_check(validated, {"id": user_id}, project_id))
    return validated


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------

def test_validate_params_normalizes_and_defaults_scope():
    out = _handler().validate_params({"memory_id": " m1 ", "reason": "  stale  "})
    assert out == {"memory_id": "m1", "reason": "stale", "scope": "personal"}


@pytest.mark.parametrize("params,match", [
    ({"reason": "stale"}, "memory_id"),
    ({"memory_id": "", "reason": "stale"}, "memory_id"),
    ({"memory_id": 7, "reason": "stale"}, "memory_id"),
    ({"memory_id": "m1"}, "reason"),
    ({"memory_id": "m1", "reason": "   "}, "reason"),
    ({"memory_id": "m1", "reason": "x" * 501}, "500"),
    ({"memory_id": "m1", "reason": "stale", "scope": "team"}, "scope"),
    ({"memory_id": "m1", "reason": "stale", "memory_content": "spoofed"},
     "Unknown parameter for archive_memory"),
])
def test_validate_params_rejects(params, match):
    with pytest.raises(ValueError, match=match):
        _handler().validate_params(params)


def test_validate_params_accepts_reason_at_cap():
    out = _handler().validate_params({"memory_id": "m1", "reason": "x" * 500})
    assert len(out["reason"]) == 500


def test_handler_metadata_and_preview():
    handler = _handler()
    assert handler.type_name.value == "archive_memory"
    assert handler.display_name == "Archive Memory"
    assert handler.approve_label == "Archive"
    assert handler.resolved_label == "Archived"
    params = {
        "memory_id": "m1", "reason": "Left the company", "scope": "personal",
        "memory_content": "Reports to Dana",
    }
    assert _run(handler.render_preview(params)) == [
        {"key": "Memory", "value": "Reports to Dana"},
        {"key": "Reason", "value": "Left the company"},
    ]
    assert _run(handler.render_preview({**params, "scope": "project"}))[0]["key"] == (
        "Project memory"
    )
    assert handler.summary_snippet(params) == "Reports to Dana"
    assert handler.summary_snippet({}) == ""


# ---------------------------------------------------------------------------
# Pre-card check
# ---------------------------------------------------------------------------

def test_precard_snapshots_personal_memory(env):
    memory = _run(env.memories.create_memory(env.alice, "Prefers tea"))
    validated = _precard({"memory_id": memory["id"], "reason": "stale"}, env.alice)
    assert validated["memory_content"] == "Prefers tea"


def test_precard_refuses_unknown_other_users_and_archived(env):
    memory = _run(env.memories.create_memory(env.alice, "Prefers tea"))
    with pytest.raises(ValueError, match="No memory"):
        _precard({"memory_id": "nope", "reason": "stale"}, env.alice)
    # Another user's memory is indistinguishable from a missing one.
    with pytest.raises(ValueError, match="No memory"):
        _precard({"memory_id": memory["id"], "reason": "stale"}, env.bob)
    _run(env.memories.archive_memory(env.alice, memory["id"]))
    with pytest.raises(ValueError, match="already archived"):
        _precard({"memory_id": memory["id"], "reason": "stale"}, env.alice)


def test_precard_project_scope(env):
    memory = _run(env.project_memories.create_project_memory(
        env.project_id, "Figures are in EUR",
    ))
    params = {"memory_id": memory["id"], "reason": "stale", "scope": "project"}
    assert _precard(params, env.alice, env.project_id)["memory_content"] == (
        "Figures are in EUR"
    )
    with pytest.raises(ValueError, match="no project"):
        _precard(params, env.alice, None)
    with pytest.raises(ValueError, match="No memory"):
        _precard(params, env.alice, env.other_project_id)
    # A project memory id is not a personal memory id, and the reverse.
    with pytest.raises(ValueError, match="No memory"):
        _precard({**params, "scope": "personal"}, env.alice, env.project_id)


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_execute_archives_personal_memory_with_reason(env):
    memory = _run(env.memories.create_memory(env.alice, "Prefers tea"))
    params = _precard(
        {"memory_id": memory["id"], "reason": "Switched to coffee"}, env.alice,
    )
    result = _run(_handler().execute(params, {"id": env.alice}))
    assert result == {
        "success": True, "memory_id": memory["id"],
        "scope": "personal", "archived": True,
    }
    stored = _run(env.memories.get_memory(env.alice, memory["id"]))
    assert stored["archived"] is True
    assert stored["archive_reason"] == "Switched to coffee"
    assert _run(env.memories.list_memories(env.alice)) == []


def test_execute_archives_project_memory_with_reason(env):
    memory = _run(env.project_memories.create_project_memory(
        env.project_id, "Figures are in EUR",
    ))
    params = _precard(
        {"memory_id": memory["id"], "reason": "Now USD", "scope": "project"},
        env.alice, env.project_id,
    )
    result = _run(_handler().execute(
        params, {"id": env.alice}, project_id=env.project_id,
    ))
    assert result["archived"] is True and result["scope"] == "project"
    stored = _run(env.project_memories.get_project_memory(env.project_id, memory["id"]))
    assert stored["archived"] is True
    assert stored["archive_reason"] == "Now USD"


def test_execute_rechecks_at_approve_time(env):
    memory = _run(env.memories.create_memory(env.alice, "Prefers tea"))
    params = _precard({"memory_id": memory["id"], "reason": "stale"}, env.alice)
    # Archived from Settings while the card was open.
    _run(env.memories.archive_memory(env.alice, memory["id"]))
    with pytest.raises(ValueError, match="already archived"):
        _run(_handler().execute(params, {"id": env.alice}))
    assert _run(env.memories.get_memory(env.alice, memory["id"]))["archive_reason"] is None

    gone = _run(env.memories.create_memory(env.alice, "Temporary"))
    params = _precard({"memory_id": gone["id"], "reason": "stale"}, env.alice)
    _run(env.memories.delete_memory(env.alice, gone["id"]))
    with pytest.raises(ValueError, match="No memory"):
        _run(_handler().execute(params, {"id": env.alice}))

    project_memory = _run(env.project_memories.create_project_memory(
        env.project_id, "Figures are in EUR",
    ))
    params = _precard(
        {"memory_id": project_memory["id"], "reason": "stale", "scope": "project"},
        env.alice, env.project_id,
    )
    with pytest.raises(ValueError, match="no project"):
        _run(_handler().execute(params, {"id": env.alice}, project_id=None))


# ---------------------------------------------------------------------------
# Stores and the get-by-id view
# ---------------------------------------------------------------------------

def test_stores_clear_the_reason_on_unarchive(env):
    memory = _run(env.memories.create_memory(env.alice, "Prefers tea"))
    assert memory["archive_reason"] is None
    _run(env.memories.archive_memory(env.alice, memory["id"], reason="stale"))
    restored = _run(env.memories.unarchive_memory(env.alice, memory["id"]))
    assert restored["archived"] is False and restored["archive_reason"] is None
    # A plain archive (the Settings button) records no reason.
    assert _run(env.memories.archive_memory(env.alice, memory["id"]))["archive_reason"] is None

    pm = _run(env.project_memories.create_project_memory(env.project_id, "EUR"))
    archived = _run(env.project_memories.set_project_memory_archived(
        env.project_id, pm["id"], True, reason="stale",
    ))
    assert archived["archive_reason"] == "stale"
    restored = _run(env.project_memories.set_project_memory_archived(
        env.project_id, pm["id"], False, reason="ignored",
    ))
    assert restored["archived"] is False and restored["archive_reason"] is None


def test_get_tools_return_the_reason_only_when_recorded(env):
    from chat.gemini_api.tool_handlers.memory import (
        _handle_memory_get, _handle_project_memory_get,
    )
    active = _run(env.memories.create_memory(env.alice, "Active"))
    retired = _run(env.memories.create_memory(env.alice, "Retired"))
    _run(env.memories.archive_memory(env.alice, retired["id"], reason="stale"))
    out = json.loads(_run(_handle_memory_get(env.alice, [active["id"], retired["id"]])))
    by_id = {m["id"]: m for m in out["memories"]}
    assert "archive_reason" not in by_id[active["id"]]
    assert by_id[retired["id"]]["archived"] is True
    assert by_id[retired["id"]]["archive_reason"] == "stale"

    pm = _run(env.project_memories.create_project_memory(env.project_id, "EUR"))
    _run(env.project_memories.set_project_memory_archived(
        env.project_id, pm["id"], True, reason="Now USD",
    ))
    out = json.loads(_run(_handle_project_memory_get(env.project_id, [pm["id"]])))
    assert out["project_memories"][0]["archive_reason"] == "Now USD"


def test_registered_and_advertised_but_not_in_public_projects():
    from chat.action_request_types import get_handler
    from chat.llm.tool_schemas import (
        ACTION_REQUEST_TYPE_ENUM, PUBLIC_ACTION_REQUEST_TYPES,
    )
    assert get_handler("archive_memory") is not None
    assert "archive_memory" in ACTION_REQUEST_TYPE_ENUM
    assert "archive_memory" not in PUBLIC_ACTION_REQUEST_TYPES


def test_migration_adds_the_column_to_both_tables_and_downgrades(deployment):
    from config import encryption
    from tests.migration_support import columns
    # An earlier migration in the chain needs the encryption-at-rest key.
    encryption.create_key_file(PASSWORD, deployment.key_file)
    db_path = deployment.db_path
    deployment.alembic("upgrade", "e7c2a9f4b1d6")
    assert "archive_reason" not in columns(db_path, "memories")
    deployment.alembic("upgrade", "a3d9e5c7f2b4")
    assert "archive_reason" in columns(db_path, "memories")
    assert "archive_reason" in columns(db_path, "project_memories")
    deployment.alembic("downgrade", "e7c2a9f4b1d6")
    assert "archive_reason" not in columns(db_path, "memories")
    assert "archive_reason" not in columns(db_path, "project_memories")
