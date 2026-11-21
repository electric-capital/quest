"""The user dict a cross-user subagent run is driven with.

``_drive_run`` passes ``run_conversation_turn`` the target user's identity
with a blank ``api_key``, the same convention chat/inference_api.py uses for
its headless runs, and leaves the stored user dict untouched.
"""

import asyncio
import uuid

from db.models import UserSubagentRunStatus


def _run(coro):
    return asyncio.run(coro)


def test_drive_run_uses_identity_with_blank_api_key(monkeypatch):
    import chat.gemini_api as gemini_api
    import chat.user_subagent as user_subagent
    import db.conversation_store as conversation_store
    import db.user_store as user_store
    import db.user_subagent_run_store as run_store

    target = {
        "id": 7,
        "email": "target@example.com",
        "name": "Target",
        "api_key": "target-api-key-" + uuid.uuid4().hex,
        "settings": {},
    }
    run = {
        "id": "run-1",
        "target_user_id": target["id"],
        "caller_user_id": 3,
        "subagent_conversation_id": "conv-subagent-1",
    }

    async def _get_user_by_id(user_id):
        assert user_id == target["id"]
        return target

    async def _get_meta(user_id, conversation_id):
        return {"model": None}

    async def _get_run(run_id):
        # Not "running" any more: the driver returns after the first turn.
        return {**run, "status": UserSubagentRunStatus.AWAITING_RETURN}

    async def _noop_flush():
        return None

    monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)
    monkeypatch.setattr(conversation_store, "get_conversation_meta", _get_meta)
    monkeypatch.setattr(run_store, "get_run", _get_run)
    monkeypatch.setattr(
        user_subagent, "make_flush_callback",
        lambda *a, **k: (_noop_flush, None),
    )

    seen: dict = {}

    async def _fake_turn(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(gemini_api, "run_conversation_turn", _fake_turn)

    _run(user_subagent._drive_run(run, "approved prompt"))

    run_user = seen["user"]
    assert run_user["api_key"] == ""
    assert run_user["id"] == target["id"]
    assert run_user["email"] == target["email"]
    assert seen["origin"] == "user_subagent"
    # The stored user dict is not mutated.
    assert target["api_key"].startswith("target-api-key-")

    # The prompt is built from the run's user dict.
    from chat.gemini_api.system_prompt import get_user_subagent_system_prompt

    prompt = get_user_subagent_system_prompt(
        run_user["api_key"],
        base_url="http://localhost:8000",
        connected_services={},
        target_user_name=target["name"],
        target_user_email=target["email"],
        caller_name="Caller",
        caller_email="caller@example.com",
    )
    assert target["api_key"] not in prompt
