"""Tests for the rename_autoproject action request.

Covers ``RenameAutoProjectHandler.validate_params``, the pre-card check
(only a run, only once the title is no longer the default one, only a real
change), ``execute`` (the rename of the proposing run's AutoProject, with
the gate and ownership re-checked at approve time) and the registration.
"""

import asyncio
from types import SimpleNamespace

import pytest

from chat.action_request_types.rename_autoproject import (
    RenameAutoProjectHandler,
    rename_autoproject_precard_check,
)
from chat.autoprojects import service
from chat.autoprojects.constants import AUTOPROJECT_TITLE_MAX_CHARS
from db import autoproject_store, conversation_store, user_store

OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(isolated_db, tmp_path, monkeypatch):
    import chat.storage as storage_mod
    import config.feature_gates as feature_gates
    import config.model_selection as model_selection
    from chat.realtime.bus import bus

    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats")
    monkeypatch.setattr(storage_mod, "AUTOPROJECTS_DIR", tmp_path / "autoprojects")
    gate = {OWNER_EMAIL, STRANGER_EMAIL}
    monkeypatch.setattr(feature_gates, "autoprojects_enabled_for", lambda email: email in gate)
    monkeypatch.setattr(model_selection, "is_model_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: None)
    monkeypatch.setattr(bus, "publish_to_conversation", lambda cid, ev: None)

    async def _users():
        owner = await user_store.create_user(OWNER_EMAIL, "Owner", "key-owner")
        stranger = await user_store.create_user(STRANGER_EMAIL, "Stranger", "key-stranger")
        return owner, stranger

    owner, stranger = _run(_users())
    return SimpleNamespace(owner=owner, stranger=stranger, gate=gate)


async def _start(user, autoproject) -> dict:
    cid = await service.create_run_conversation(user, autoproject["id"])
    meta = await conversation_store.get_conversation_meta(user["id"], cid)
    return (await service.start_run(user, meta, prompt="Do it", model="fake-model")).run


async def _precard(user, autoproject_id, title) -> dict:
    validated = RenameAutoProjectHandler().validate_params({"title": title})
    await rename_autoproject_precard_check(validated, dict(user), autoproject_id)
    return validated


# ---------------------------------------------------------------------------
# validate_params and metadata
# ---------------------------------------------------------------------------


def test_validate_params_normalizes_the_title():
    out = RenameAutoProjectHandler().validate_params({"title": "  Vendor \n review  "})
    assert out == {"title": "Vendor review"}


@pytest.mark.parametrize("params, match", [
    ({}, "Missing required parameter: title"),
    ({"title": "   "}, "cannot be empty"),
    ({"title": 7}, "must be a string"),
    ({"title": "x" * (AUTOPROJECT_TITLE_MAX_CHARS + 1)}, str(AUTOPROJECT_TITLE_MAX_CHARS)),
    ({"title": "New", "current_title": "Old"}, "Unknown parameter for rename_autoproject"),
    ({"title": "New", "autoproject_id": "a1"}, "Unknown parameter for rename_autoproject"),
])
def test_validate_params_rejects(params, match):
    with pytest.raises(ValueError, match=match):
        RenameAutoProjectHandler().validate_params(params)


def test_handler_metadata_and_preview():
    handler = RenameAutoProjectHandler()
    assert handler.type_name.value == "rename_autoproject"
    assert handler.display_name == "Rename AutoProject"
    assert handler.approve_label == "Rename"
    assert handler.resolved_label == "Renamed"
    params = {"title": "New name", "current_title": "Old name"}
    assert handler.summary_snippet(params) == "New name"
    assert _run(handler.render_preview(params)) == [
        {"key": "Current title", "value": "Old name"},
        {"key": "New title", "value": "New name"},
    ]


def test_registered_and_advertised_but_not_in_public_projects():
    from chat.action_request_types import get_handler
    from chat.llm.tool_schemas import (
        ACTION_REQUEST_TYPE_ENUM, PUBLIC_ACTION_REQUEST_TYPES,
    )
    assert get_handler("rename_autoproject") is not None
    assert "rename_autoproject" in ACTION_REQUEST_TYPE_ENUM
    assert "rename_autoproject" not in PUBLIC_ACTION_REQUEST_TYPES


def test_documented_in_the_autoprojects_skill():
    from chat.system_skills.catalog import CATALOG

    content = CATALOG["system:autoprojects"].content_builder("http://base", "key")
    assert 'request_type="rename_autoproject"' in content


# ---------------------------------------------------------------------------
# Pre-card check
# ---------------------------------------------------------------------------


def test_precard_snapshots_the_current_title(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Old name")
        validated = await _precard(env.owner, autoproject["id"], "New name")
        assert validated == {"title": "New name", "current_title": "Old name"}

    _run(body())


def test_precard_refuses_outside_a_run(env):
    async def body():
        with pytest.raises(ValueError, match="only available in an AutoProject run"):
            await _precard(env.owner, None, "New name")

    _run(body())


def test_precard_refuses_while_the_title_is_the_default_one(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, prompt="Compare vendors")
        assert autoproject["title_is_default"] is True
        with pytest.raises(ValueError, match="autoproject_title"):
            await _precard(env.owner, autoproject["id"], "Vendor comparison")
        # Once a run has named it, the card is the only way to rename it.
        assert await autoproject_store.set_title_from_run(autoproject["id"], "Vendor comparison")
        assert not await autoproject_store.set_title_from_run(autoproject["id"], "Again")
        validated = await _precard(env.owner, autoproject["id"], "Vendor shortlist")
        assert validated["current_title"] == "Vendor comparison"

    _run(body())


def test_precard_refuses_an_unchanged_title(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Old name")
        with pytest.raises(ValueError, match="already titled"):
            await _precard(env.owner, autoproject["id"], " Old  name ")

    _run(body())


def test_precard_refuses_a_foreign_autoproject_and_a_closed_gate(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Old name")
        with pytest.raises(ValueError) as foreign:
            await _precard(env.stranger, autoproject["id"], "New name")
        with pytest.raises(ValueError) as missing:
            await _precard(env.stranger, "0" * len(autoproject["id"]), "New name")
        assert str(foreign.value).replace(autoproject["id"], "X") == str(
            missing.value
        ).replace("0" * len(autoproject["id"]), "X")
        env.gate.discard(OWNER_EMAIL)
        with pytest.raises(ValueError):
            await _precard(env.owner, autoproject["id"], "New name")

    _run(body())


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_renames_the_runs_autoproject(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Old name")
        run = await _start(env.owner, autoproject)
        before = await autoproject_store.get_autoproject_by_id(autoproject["id"])
        result = await RenameAutoProjectHandler().execute(
            {"title": "New name", "current_title": "Old name"}, dict(env.owner),
            conversation_id=run["conversation_id"],
        )
        assert result == {"success": True, "title": "New name", "previous_title": "Old name"}
        after = await autoproject_store.get_autoproject_by_id(autoproject["id"])
        assert after["title"] == "New name"
        assert after["title_is_default"] is False
        assert after["event_seq"] == before["event_seq"] + 1

    _run(body())


def test_execute_rechecks_at_approve_time(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Old name")
        run = await _start(env.owner, autoproject)
        handler = RenameAutoProjectHandler()
        # Not a run conversation: nothing to rename.
        with pytest.raises(ValueError, match="only available in an AutoProject run"):
            await handler.execute({"title": "New name"}, dict(env.owner), conversation_id="c-plain")
        # Someone else's run.
        with pytest.raises(ValueError):
            await handler.execute(
                {"title": "New name"}, dict(env.stranger),
                conversation_id=run["conversation_id"],
            )
        # The gate closed while the card was open.
        env.gate.discard(OWNER_EMAIL)
        with pytest.raises(ValueError):
            await handler.execute(
                {"title": "New name"}, dict(env.owner),
                conversation_id=run["conversation_id"],
            )
        assert (await autoproject_store.get_autoproject_by_id(autoproject["id"]))["title"] == "Old name"

    _run(body())
