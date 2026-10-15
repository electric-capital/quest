"""Integration test for the create_action_request dispatch arm when
``validate_params`` rejects an unknown field.

Mirrors the logic at ``chat/gemini_api/conversation.py:1190-1211`` --
``handler = get_handler(req_type); try:
validated_params = handler.validate_params(req_params); except
ValueError as e: result = json.dumps({"error":
f"Invalid parameters: {e}"})``. The arm short-circuits before any DB
write, wait-handle insert, structured-message append, WS event emit,
or ``SuspendForActionRequest`` raise -- this test asserts that all of
those side effects are absent.
"""

import asyncio
import json
import uuid

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.action_request_store as ar_store_mod
    import db.tool_wait_handle_store as store_mod

    return store_mod, ar_store_mod, isolated_db.models


@pytest.fixture()
def _user_id(_isolated_db):
    _wh_store, _ar_store, models_mod = _isolated_db
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"ar-unknown-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Unknown Param Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _simulate_dispatch_arm(req_type: str, req_params: dict) -> dict:
    """Mirror the synchronous ``ValueError`` arm of the dispatch logic.

    Captures everything that the dispatch arm at conversation.py:
    1190-1211 does **before** it would insert the DB row at line 1265:
    handler resolution, validate_params, ValueError -> JSON. Returns a
    dict with ``raised_suspend`` (bool), ``result`` (the JSON tool
    result), and ``validated_params`` (None if validation failed).
    """
    from chat.action_request_types import get_handler
    from chat.gemini_api.conversation import SuspendForActionRequest

    out: dict = {
        "raised_suspend": False,
        "result": None,
        "validated_params": None,
    }

    handler = get_handler(req_type)
    if not handler:
        out["result"] = json.dumps({
            "error": f"Unknown request type: {req_type}",
        })
        return out

    try:
        validated = handler.validate_params(req_params)
    except ValueError as e:
        out["result"] = json.dumps({
            "error": f"Invalid parameters: {e}",
        })
        return out
    except SuspendForActionRequest:
        out["raised_suspend"] = True
        return out

    out["validated_params"] = validated
    return out


def test_dispatch_arm_returns_invalid_parameters_json_no_db_writes(
    _isolated_db, _user_id, example_plugin,
):
    """The motivating bug shape (driven through the _example plugin's
    ``example_echo`` handler so the arm is exercised with a
    plugin-registered type): the
    model proposes a request with an unknown key. The dispatch arm must:

    1. Catch the ValueError raised by validate_params.
    2. Return ``{"error": "Invalid parameters: ..."}`` synchronously.
    3. NOT insert a row in ``action_requests``.
    4. NOT insert a row in ``tool_wait_handles``.
    5. NOT raise ``SuspendForActionRequest``.

    The lack of WS publishing is implicit because the dispatch arm
    publishes only AFTER ``await create_action_request(...)`` succeeds.
    """
    wh_store, ar_store, _ = _isolated_db

    # Sanity: both stores start empty for this user.
    assert _run(ar_store.list_action_requests(_user_id)) == []
    # No public "list all wait handles for user" helper, so we sample
    # the row count directly via SQLAlchemy.
    from db.engine import AsyncSessionLocal
    from sqlalchemy import select, func

    async def _count_wait_handles() -> int:
        from db.models import ToolWaitHandle

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.count()).select_from(ToolWaitHandle)
                .where(ToolWaitHandle.user_id == _user_id)
            )
            return result.scalar_one()

    assert _run(_count_wait_handles()) == 0

    # Drive the dispatch arm.
    out = _simulate_dispatch_arm(
        "example_echo",
        {
            "message": "hello",
            "person_associations": [{"person_id": 4}],  # not a real field
        },
    )

    # Must NOT have suspended.
    assert out["raised_suspend"] is False
    # validate_params must have failed (no validated_params produced).
    assert out["validated_params"] is None
    # The synchronous result must carry the "Invalid parameters" prefix.
    assert out["result"] is not None
    parsed = json.loads(out["result"])
    assert "error" in parsed
    assert parsed["error"].startswith("Invalid parameters:")
    assert "person_associations" in parsed["error"]

    # And no rows in either table.
    assert _run(ar_store.list_action_requests(_user_id)) == []
    assert _run(_count_wait_handles()) == 0


def test_dispatch_arm_unknown_field_create_memory_no_db_writes(
    _isolated_db, _user_id,
):
    """Same contract for create_memory. Tags is not a valid param."""
    wh_store, ar_store, _ = _isolated_db

    out = _simulate_dispatch_arm(
        "create_memory",
        {"content": "remember this", "tags": ["x"]},
    )

    assert out["raised_suspend"] is False
    assert out["validated_params"] is None
    parsed = json.loads(out["result"])
    assert parsed["error"].startswith("Invalid parameters:")
    assert "tags" in parsed["error"]
    assert _run(ar_store.list_action_requests(_user_id)) == []


def test_dispatch_arm_valid_payload_passes_validation(
    _isolated_db, _user_id,
):
    """Symmetric counterpoint: a valid payload does NOT trip the new
    unknown-field check. validated_params is returned and contains the
    expected keys. (We deliberately stop short of inserting the
    action_request row -- that path requires the full conversation
    machinery; the validate-then-pass branch is what this fix needs to
    keep working.)"""

    out = _simulate_dispatch_arm(
        "create_memory",
        {"content": "Likes coffee"},
    )
    assert out["raised_suspend"] is False
    assert out["result"] is None  # No error emitted.
    assert out["validated_params"] == {"content": "Likes coffee"}
