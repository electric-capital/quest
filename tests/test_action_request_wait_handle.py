"""Tests for the action_request <-> wait_handle linkage.

Covers the new helpers added to ``db.tool_wait_handle_store`` and the
projection that ``_build_wait_for_handles_result`` produces for
``kind=action_request`` rows.
"""

import asyncio
import uuid

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.tool_wait_handle_store as store_mod

    return store_mod, isolated_db.models


@pytest.fixture()
def _user_id(_isolated_db):
    _store, models_mod = _isolated_db
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"action-req-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Action Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def test_create_handle_persists_correlation_at_create_time(
    _isolated_db, _user_id,
):
    """For kind=action_request, correlation columns are populated up
    front (the request_id is known when the handle is created)."""
    store, _ = _isolated_db
    handle = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="create_action_request_t1",
        payload={
            "request_id": 17,
            "request_type": "send_slack_message",
            "params": {"channel_id": "C123", "message": "hi"},
        },
        correlation_kind="action_request",
        correlation_id="17",
    ))
    assert handle["correlation_kind"] == "action_request"
    assert handle["correlation_id"] == "17"

    fetched = _run(store.get_handle(handle["id"]))
    assert fetched is not None
    assert fetched["correlation_kind"] == "action_request"
    assert fetched["correlation_id"] == "17"


def test_find_pending_for_action_request_returns_pending(
    _isolated_db, _user_id,
):
    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 42},
        correlation_kind="action_request",
        correlation_id="42",
    ))
    found = _run(store.find_pending_for_action_request(_user_id, 42))
    assert found is not None
    assert found["id"] == h["id"]


def test_find_pending_for_action_request_skips_resolved(
    _isolated_db, _user_id,
):
    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 99},
        correlation_kind="action_request",
        correlation_id="99",
    ))
    _run(store.resolve_handle(
        h["id"],
        new_status="accepted",
        response={"verdict": "executed", "request_id": 99, "result": {}},
        correlation_kind="action_request",
        correlation_id="99",
    ))
    found = _run(store.find_pending_for_action_request(_user_id, 99))
    assert found is None


def test_find_pending_for_action_request_scopes_by_user(
    _isolated_db, _user_id,
):
    store, models_mod = _isolated_db
    from db.engine import AsyncSessionLocal

    async def _create_other():
        async with AsyncSessionLocal() as db:
            u2 = models_mod.User(
                email=f"other-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Other",
            )
            db.add(u2)
            await db.commit()
            await db.refresh(u2)
            return u2.id

    other = _run(_create_other())
    _run(store.create_handle(
        user_id=other,
        conversation_id="c-other",
        kind="action_request",
        tool_id="t-other",
        payload={"request_id": 7},
        correlation_kind="action_request",
        correlation_id="7",
    ))
    found = _run(store.find_pending_for_action_request(_user_id, 7))
    assert found is None


def test_find_pending_for_action_request_ignores_other_kinds(
    _isolated_db, _user_id,
):
    """A non-action_request row that happens to share an id must not be
    returned by find_pending_for_action_request."""
    store, _ = _isolated_db
    _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="slack_reply",
        tool_id="t-slack",
        payload={"channel": "C1", "thread_ts": "1.0"},
        correlation_kind="action_request",
        correlation_id="123",
    ))
    found = _run(store.find_pending_for_action_request(_user_id, 123))
    assert found is None


def test_handle_response_for_model_projection_for_action_request(
    _isolated_db, _user_id,
):
    """``_build_wait_for_handles_result`` projects an accepted
    action_request row into the shape the model expects (kind, status,
    response carrying verdict + request_id + result)."""
    store, _ = _isolated_db
    from chat.gemini_api.conversation import _build_wait_for_handles_result

    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="create_action_request_t1",
        payload={"request_id": 7, "request_type": "send_slack_message"},
        correlation_kind="action_request",
        correlation_id="7",
    ))
    _run(store.resolve_handle(
        h["id"],
        new_status="accepted",
        response={
            "verdict": "executed",
            "request_id": 7,
            "result": {"success": True, "note_id": "abc"},
        },
        correlation_kind="action_request",
        correlation_id="7",
        user_id=_user_id,
    ))

    payload = _run(_build_wait_for_handles_result(_user_id, [h["id"]]))
    assert payload["resolved"] and not payload["still_pending"]
    entry = payload["resolved"][0]
    assert entry["id"] == h["id"]
    assert entry["kind"] == "action_request"
    assert entry["status"] == "accepted"
    assert entry["tool_id"] == "create_action_request_t1"
    assert entry["response"]["verdict"] == "executed"
    assert entry["response"]["request_id"] == 7
    assert entry["response"]["result"] == {"success": True, "note_id": "abc"}


def test_pending_action_request_handle_appears_in_still_pending(
    _isolated_db, _user_id,
):
    store, _ = _isolated_db
    from chat.gemini_api.conversation import _build_wait_for_handles_result

    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 1},
        correlation_kind="action_request",
        correlation_id="1",
    ))
    payload = _run(_build_wait_for_handles_result(_user_id, [h["id"]]))
    assert payload["still_pending"] == [h["id"]]
    assert payload["resolved"] == []


def test_rejected_action_request_handle_carries_denied_verdict(
    _isolated_db, _user_id,
):
    store, _ = _isolated_db
    from chat.gemini_api.conversation import _build_wait_for_handles_result

    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 5},
        correlation_kind="action_request",
        correlation_id="5",
    ))
    _run(store.resolve_handle(
        h["id"],
        new_status="rejected",
        response={
            "verdict": "denied",
            "request_id": 5,
            "result": {"denied": True},
        },
        correlation_kind="action_request",
        correlation_id="5",
        user_id=_user_id,
    ))
    payload = _run(_build_wait_for_handles_result(_user_id, [h["id"]]))
    entry = payload["resolved"][0]
    assert entry["status"] == "rejected"
    assert entry["response"]["verdict"] == "denied"
    assert entry["response"]["result"] == {"denied": True}


def test_create_handle_default_correlation_is_none(
    _isolated_db, _user_id,
):
    """Existing call sites that don't pass correlation args still work."""
    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="slack_reply",
        tool_id="t1",
        payload={"channel": "C1", "thread_ts": "1.0"},
    ))
    assert h["correlation_kind"] is None
    assert h["correlation_id"] is None


def test_rejected_action_request_handle_carries_feedback(
    _isolated_db, _user_id,
):
    """Deny-with-feedback path: the wait-handle response carries
    feedback both at the top level (next to verdict) and inside result,
    so the model reads it from response.feedback after wait_for_handles
    wakes up."""
    store, _ = _isolated_db
    from chat.gemini_api.conversation import _build_wait_for_handles_result

    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 9},
        correlation_kind="action_request",
        correlation_id="9",
    ))
    feedback_text = "wrong channel, send to #general instead"
    _run(store.resolve_handle(
        h["id"],
        new_status="rejected",
        response={
            "verdict": "denied",
            "request_id": 9,
            "feedback": feedback_text,
            "result": {"denied": True, "feedback": feedback_text},
        },
        correlation_kind="action_request",
        correlation_id="9",
        user_id=_user_id,
    ))
    payload = _run(_build_wait_for_handles_result(_user_id, [h["id"]]))
    entry = payload["resolved"][0]
    assert entry["status"] == "rejected"
    assert entry["response"]["verdict"] == "denied"
    assert entry["response"]["feedback"] == feedback_text
    assert entry["response"]["result"]["feedback"] == feedback_text
    assert entry["response"]["result"]["denied"] is True


# ---------------------------------------------------------------------------
# Suspend / resume of blocking create_action_request
# ---------------------------------------------------------------------------


def test_suspend_for_action_request_inherits_from_exception():
    """The new sentinel must derive from Exception (not BaseException)
    so it cannot mask asyncio.CancelledError."""
    from chat.gemini_api.conversation import SuspendForActionRequest

    assert issubclass(SuspendForActionRequest, Exception)


def test_suspend_for_action_request_carries_fields():
    """The sentinel carries handle_id, tool_id, request_id so the
    top-level catch can log and the resume bucket can resolve."""
    from chat.gemini_api.conversation import SuspendForActionRequest

    exc = SuspendForActionRequest(
        handle_id="wh-abc", tool_id="toolu-1", request_id=42,
    )
    assert exc.handle_id == "wh-abc"
    assert exc.tool_id == "toolu-1"
    assert exc.request_id == 42


def test_dangling_create_action_request_recognised_by_resume_helper(
    monkeypatch,
):
    """``_conversation_has_dangling_wait`` must recognise a dangling
    ``create_action_request`` tool_use, so a Approve / Revise / Deny
    resolve kicks the headless resume task."""
    from chat.wait_handles import resume as wait_resume

    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_car_1",
                    "name": "create_action_request",
                    "input": {
                        "request_type": "send_slack_message",
                        "params": {},
                        "reasoning": "x",
                    },
                },
            ],
        },
    ]
    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history",
        lambda _cid: (history, "anthropic"),
    )
    assert wait_resume._conversation_has_dangling_wait("c-car") is True


def _build_action_request_result_str(row: dict) -> str:
    """Mimic the resume bucket's branch for a kind=action_request row.

    Mirrors the inline logic in ``run_conversation_turn`` so the per-verdict
    payload contracts can be unit-tested without standing up the full
    conversation loop.
    """
    import json as _json

    response = row.get("response") or {}
    if row.get("status") == "pending":
        return _json.dumps({
            "status": "still_waiting",
            "note": "User has not resolved the action request yet.",
        })
    return _json.dumps(response)


def test_resume_payload_for_executed_verdict(_isolated_db, _user_id):
    """Resume bucket closes a dangling create_action_request tool_use
    with the wait-handle row's response verbatim on Approve."""
    import json as _json

    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 11},
        correlation_kind="action_request",
        correlation_id="11",
    ))
    _run(store.resolve_handle(
        h["id"],
        new_status="accepted",
        response={
            "verdict": "executed",
            "request_id": 11,
            "result": {"success": True, "ts": "1700000000.000100"},
        },
        correlation_kind="action_request",
        correlation_id="11",
        user_id=_user_id,
    ))
    fresh = _run(store.get_handle(h["id"]))
    payload = _json.loads(_build_action_request_result_str(fresh))
    assert payload["verdict"] == "executed"
    assert payload["request_id"] == 11
    assert payload["result"]["success"] is True
    assert payload["result"]["ts"] == "1700000000.000100"


def test_resume_payload_for_denied_no_feedback(_isolated_db, _user_id):
    """Plain Deny without feedback resumes with verdict denied + no
    feedback field."""
    import json as _json

    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 12},
        correlation_kind="action_request",
        correlation_id="12",
    ))
    _run(store.resolve_handle(
        h["id"],
        new_status="rejected",
        response={
            "verdict": "denied",
            "request_id": 12,
            "result": {"denied": True},
        },
        correlation_kind="action_request",
        correlation_id="12",
        user_id=_user_id,
    ))
    fresh = _run(store.get_handle(h["id"]))
    payload = _json.loads(_build_action_request_result_str(fresh))
    assert payload["verdict"] == "denied"
    assert payload["request_id"] == 12
    assert payload["result"] == {"denied": True}
    assert "feedback" not in payload


def test_resume_payload_for_revise_with_feedback(_isolated_db, _user_id):
    """Revise (deny + feedback) resumes with both top-level feedback and
    the same string mirrored inside result.feedback."""
    import json as _json

    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 13},
        correlation_kind="action_request",
        correlation_id="13",
    ))
    feedback_text = "wrong channel, send to #general"
    _run(store.resolve_handle(
        h["id"],
        new_status="rejected",
        response={
            "verdict": "denied",
            "request_id": 13,
            "feedback": feedback_text,
            "result": {"denied": True, "feedback": feedback_text},
        },
        correlation_kind="action_request",
        correlation_id="13",
        user_id=_user_id,
    ))
    fresh = _run(store.get_handle(h["id"]))
    payload = _json.loads(_build_action_request_result_str(fresh))
    assert payload["verdict"] == "denied"
    assert payload["feedback"] == feedback_text
    assert payload["result"]["feedback"] == feedback_text
    assert payload["result"]["denied"] is True


def test_resume_payload_when_row_still_pending(_isolated_db, _user_id):
    """Race: resume kicked before the resolve endpoint finished. Close
    the dangling tool_use with a still-waiting placeholder so the model
    can re-issue or wait again on the next user turn."""
    import json as _json

    store, _ = _isolated_db
    h = _run(store.create_handle(
        user_id=_user_id,
        conversation_id="c1",
        kind="action_request",
        tool_id="t1",
        payload={"request_id": 14},
        correlation_kind="action_request",
        correlation_id="14",
    ))
    fresh = _run(store.get_handle(h["id"]))
    payload = _json.loads(_build_action_request_result_str(fresh))
    assert payload["status"] == "still_waiting"
    assert "not resolved" in payload["note"]
