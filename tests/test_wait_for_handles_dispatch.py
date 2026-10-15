"""Tests for the wait_for_handles dispatch helpers in turn_tools.py.

The wait arm (``_await_wait_for_handles`` + the suspend sentinels) lives
in chat/gemini_api/turn_tools.py; conversation.py re-imports the
sentinels for its except clauses, so both import surfaces are covered
here.
"""

import asyncio
import json
import uuid
from unittest.mock import MagicMock, patch

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
                email=f"wait-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Wait Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


_DEFAULT_REASON = "Waiting for you to review the proposed memory"


def _make_ctx(user_id, conversation_id="c1"):
    """Build a minimal RunContext for driving _await_wait_for_handles.

    Only the fields the wait arm touches matter (user, conversation_id,
    app); everything else is inert filler.
    """
    from chat.gemini_api.run_context import RunContext
    from chat.gemini_api.usage import UsageAccumulator

    async def _noop_event(_event):
        return None

    return RunContext(
        app=None,
        user={"id": user_id},
        conversation_id=conversation_id,
        timezone="UTC",
        model="test-model",
        origin="web",
        project_id=None,
        routine_id=None,
        slack_context=None,
        provider=MagicMock(),
        provider_name="gemini",
        chat=MagicMock(),
        is_slack_origin=False,
        is_user_subagent=False,
        is_inference_api=False,
        is_public=False,
        nested_subagents=False,
        user_subagents_enabled=False,
        user_subagents_gate_open=True,
        subagent_run=None,
        subagent_caller=None,
        custom_prompt="",
        resolved_project_guide="",
        resolved_skills_content="",
        on_event=_noop_event,
        structured_messages=[],
        usage_acc=UsageAccumulator(),
    )


def test_schema_marks_reason_required():
    """The wait_for_handles tool schema lists `reason` as required.

    Without this, the model can omit the user-facing label and the chat UI
    leaks the raw tool name.
    """
    from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

    spec = TOOL_CALL_REGISTRY["wait_for_handles"]
    params = spec["parameters"]
    assert "reason" in params["properties"]
    assert "reason" in params["required"]
    assert "handle_ids" in params["required"]


def test_already_resolved_returns_immediately(_isolated_db, _user_id):
    store, _ = _isolated_db
    # Force the turn_tools module to bind to the freshly reloaded store.
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import _await_wait_for_handles

        h = _run(store.create_handle(
            user_id=_user_id, conversation_id="c1",
            kind="action_request", tool_id="t1",
            payload={"request_id": 1},
        ))
        _run(store.resolve_handle(
            h["id"], new_status="accepted",
            response={"memory_id": "m-1"},
        ))

        with patch(
            "chat.gemini_api.conversation._save_sdk_history",
        ) as save_mock:
            payload_str = _run(_await_wait_for_handles(
                _make_ctx(_user_id),
                {
                    "handle_ids": [h["id"]],
                    "reason": _DEFAULT_REASON,
                },
            ))

        # Should have short-circuited without a save (nothing was pending).
        save_mock.assert_not_called()
        payload = json.loads(payload_str)
        assert payload["resolved"][0]["id"] == h["id"]
        assert payload["resolved"][0]["status"] == "accepted"
        assert payload["still_pending"] == []


def test_unknown_handle_id_is_an_error(_isolated_db, _user_id):
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import _await_wait_for_handles

        result = _run(_await_wait_for_handles(
            _make_ctx(_user_id),
            {
                "handle_ids": ["not-a-real-handle"],
                "reason": _DEFAULT_REASON,
            },
        ))
        payload = json.loads(result)
        assert "error" in payload
        assert "not-a-real-handle" in payload["error"]


def test_validation_rejects_empty_list():
    from chat.gemini_api.turn_tools import _await_wait_for_handles

    result = _run(_await_wait_for_handles(
        _make_ctx(1),
        {"handle_ids": [], "reason": _DEFAULT_REASON},
    ))
    payload = json.loads(result)
    assert "error" in payload


def test_validation_rejects_missing_reason():
    from chat.gemini_api.turn_tools import _await_wait_for_handles

    result = _run(_await_wait_for_handles(
        _make_ctx(1),
        {"handle_ids": ["h-1"]},
    ))
    payload = json.loads(result)
    assert "error" in payload
    assert "reason" in payload["error"]


def test_validation_rejects_empty_reason():
    from chat.gemini_api.turn_tools import _await_wait_for_handles

    result = _run(_await_wait_for_handles(
        _make_ctx(1),
        {"handle_ids": ["h-1"], "reason": "   "},
    ))
    payload = json.loads(result)
    assert "error" in payload
    assert "reason" in payload["error"]


def test_validation_rejects_non_string_reason():
    from chat.gemini_api.turn_tools import _await_wait_for_handles

    result = _run(_await_wait_for_handles(
        _make_ctx(1),
        {"handle_ids": ["h-1"], "reason": 42},
    ))
    payload = json.loads(result)
    assert "error" in payload
    assert "reason" in payload["error"]


def test_long_reason_is_truncated_not_rejected(_isolated_db, _user_id):
    """A reason longer than the cap is silently truncated, not rejected."""
    store, _ = _isolated_db
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import (
            _await_wait_for_handles,
            _WAIT_FOR_HANDLES_MAX_REASON_LEN,
            _normalize_wait_reason,
        )

        # Pure helper assertion: no error, just a clipped string.
        big = "x" * (_WAIT_FOR_HANDLES_MAX_REASON_LEN + 50)
        normalized = _normalize_wait_reason(big)
        assert len(normalized) == _WAIT_FOR_HANDLES_MAX_REASON_LEN

        # End-to-end: dispatch arm accepts a long reason and proceeds. Use
        # an already-resolved handle so we don't have to wait for a real
        # async future to fire.
        h = _run(store.create_handle(
            user_id=_user_id, conversation_id="c1",
            kind="action_request", tool_id="t1",
            payload={"request_id": 1},
        ))
        _run(store.resolve_handle(
            h["id"], new_status="accepted", response={"memory_id": "m-1"},
        ))
        with patch("chat.gemini_api.conversation._save_sdk_history"):
            result = _run(_await_wait_for_handles(
                _make_ctx(_user_id),
                {"handle_ids": [h["id"]], "reason": big},
            ))
        payload = json.loads(result)
        assert "error" not in payload
        assert payload["resolved"][0]["status"] == "accepted"


def test_pending_handle_raises_suspend_sentinel(_isolated_db, _user_id):
    """A pending handle causes _await_wait_for_handles to raise the
    SuspendForWaitHandles sentinel, leaving the row in the DB unchanged.

    There is no in-process timer arm any more: the timeout becomes a
    background asyncio.Task that flips the row to ``timed_out`` on
    expiry. We still verify that the call does not return synchronously
    and does not mutate the DB state for a pending row.
    """
    store, _ = _isolated_db
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import (
            _await_wait_for_handles,
            SuspendForWaitHandles,
        )

        h = _run(store.create_handle(
            user_id=_user_id, conversation_id="c1",
            kind="action_request", tool_id="t1",
            payload={"request_id": 1},
        ))
        with patch("chat.gemini_api.conversation._save_sdk_history"):
            with pytest.raises(SuspendForWaitHandles) as excinfo:
                _run(_await_wait_for_handles(
                    _make_ctx(_user_id),
                    {
                        "handle_ids": [h["id"]],
                        "reason": _DEFAULT_REASON,
                    },
                    tool_id="tu-1",
                ))
        # Sentinel carries the dangling tool_id and the pending handle ids.
        assert excinfo.value.tool_id == "tu-1"
        assert excinfo.value.handle_ids == [h["id"]]
        assert excinfo.value.reason == _DEFAULT_REASON
        # Row remains pending in the DB; the resume path / timer flips it.
        fresh = _run(store.get_handle(h["id"]))
        assert fresh["status"] == "pending"


def test_pending_wait_persists_its_deadline(_isolated_db, _user_id):
    """The wait's timeout is written to expires_at so the expiry sweep
    enforces it if the process restarts and the in-memory timer is lost."""
    from datetime import datetime, timedelta, timezone

    store, _ = _isolated_db
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import (
            _await_wait_for_handles,
            SuspendForWaitHandles,
        )

        h = _run(store.create_handle(
            user_id=_user_id, conversation_id="c1",
            kind="user_subagent", tool_id="",
            payload={},
            expires_at=datetime.now(timezone.utc) + timedelta(days=14),
        ))
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        with patch("chat.wait_handles.wait_timer.schedule_timeout_for_handles") as sched:
            with pytest.raises(SuspendForWaitHandles):
                _run(_await_wait_for_handles(
                    _make_ctx(_user_id),
                    {
                        "handle_ids": [h["id"]],
                        "reason": _DEFAULT_REASON,
                        "timeout_seconds": 300,
                    },
                    tool_id="tu-1",
                ))
        after = datetime.now(timezone.utc).replace(tzinfo=None)

        sched.assert_called_once()
        assert sched.call_args.kwargs["timeout_seconds"] == 300
        row = _run(store.get_handle(h["id"]))
        assert row["status"] == "pending"
        expires_at = datetime.fromisoformat(row["expires_at"])
        assert before + timedelta(seconds=300) <= expires_at
        assert expires_at <= after + timedelta(seconds=300)


def test_overdue_handle_times_out_at_the_wait_site(_isolated_db, _user_id, monkeypatch):
    """A pending handle whose expires_at already passed is timed out by the
    wait arm and returned immediately instead of suspending on it."""
    from datetime import datetime, timedelta, timezone

    from chat.realtime import bus as rt_bus

    events: list = []
    monkeypatch.setattr(
        rt_bus, "publish_to_user",
        lambda user_id, event: events.append((user_id, event)),
    )
    store, _ = _isolated_db
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import _await_wait_for_handles

        h = _run(store.create_handle(
            user_id=_user_id, conversation_id="c1",
            kind="user_subagent", tool_id="",
            payload={},
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
        with patch("chat.wait_handles.wait_timer.schedule_timeout_for_handles") as sched:
            payload_str = _run(_await_wait_for_handles(
                _make_ctx(_user_id),
                {"handle_ids": [h["id"]], "reason": _DEFAULT_REASON},
                tool_id="tu-1",
            ))

        sched.assert_not_called()
        payload = json.loads(payload_str)
        assert payload["still_pending"] == []
        assert payload["resolved"][0]["id"] == h["id"]
        assert payload["resolved"][0]["status"] == "timed_out"
        assert _run(store.get_handle(h["id"]))["status"] == "timed_out"
        [(event_user, event)] = events
        assert event_user == _user_id
        assert event["status"] == "timed_out"


def test_suspend_sentinel_inherits_from_exception():
    """Sentinel must derive from Exception, not BaseException, so it
    cannot mask asyncio.CancelledError on the unwind path."""
    from chat.gemini_api.conversation import (
        SuspendForWaitHandles,
        SuspendForSlackReply,
    )
    assert issubclass(SuspendForWaitHandles, Exception)
    assert not issubclass(SuspendForWaitHandles, BaseException) or \
        issubclass(SuspendForWaitHandles, Exception)
    assert issubclass(SuspendForSlackReply, Exception)


def test_dangling_wait_detection_recognises_slack_reply(tmp_path, monkeypatch):
    """``_conversation_has_dangling_wait`` must detect both suspend kinds.

    The bug: after the live-future refactor the helper only matched
    ``wait_for_handles``, so a Slack-driven resume kicked from
    ``slack_driven_runtime._flush_after`` would silently no-op when the
    dangling tool_use was actually ``send_slack_reply_and_get_response``.
    The user's reply was lost between "handle resolved" and "model woken".
    """
    from chat.wait_handles import resume as wait_resume

    # Anthropic-shaped sdk_history with a dangling
    # send_slack_reply_and_get_response tool_use on the last turn.
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Hey!"},
                {
                    "type": "tool_use",
                    "id": "toolu_slackreply_1",
                    "name": "send_slack_reply_and_get_response",
                    "input": {"text": "Hey Alice!"},
                },
            ],
        },
    ]

    def _fake_load(_cid):
        return (history, "anthropic")

    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history", _fake_load,
    )
    assert wait_resume._conversation_has_dangling_wait("c-slack") is True

    # And the existing wait_for_handles path still works.
    history_wfh = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_wfh_1",
                    "name": "wait_for_handles",
                    "input": {"handle_ids": ["h1"]},
                },
            ],
        },
    ]

    def _fake_load_wfh(_cid):
        return (history_wfh, "anthropic")

    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history", _fake_load_wfh,
    )
    assert wait_resume._conversation_has_dangling_wait("c-wfh") is True

    # No dangling tool_use -> False.
    history_done = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "All done."}],
        },
    ]

    def _fake_load_done(_cid):
        return (history_done, "anthropic")

    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history", _fake_load_done,
    )
    assert wait_resume._conversation_has_dangling_wait("c-done") is False


def test_dangling_slack_reply_detection_distinguishes_suspend_kinds(monkeypatch):
    """``_conversation_has_dangling_slack_reply`` must match ONLY the
    Slack-reply suspend, not the generic wait_for_handles suspend.

    The Slack dispatch / resume tasks use this to decide whether to clear
    the typing indicator at end-of-run. Clearing while the model is still
    suspended on send_slack_reply_and_get_response would race the resume
    bucket's re-set. Conversely, NOT clearing when the run truly ended
    leaves a stale "Quest is working..." indicator in Slack until the
    next user turn -- exactly the regression this helper guards.
    """
    from chat.wait_handles import resume as wait_resume

    # Dangling slack_reply tool_use -> True.
    history_slack = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_slack_1",
                    "name": "send_slack_reply_and_get_response",
                    "input": {"text": "Hi back"},
                },
            ],
        },
    ]
    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history",
        lambda _cid: (history_slack, "anthropic"),
    )
    assert (
        wait_resume._conversation_has_dangling_slack_reply("c-slack") is True
    )

    # Dangling wait_for_handles tool_use -> False (different suspend kind).
    history_wfh = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_wfh_1",
                    "name": "wait_for_handles",
                    "input": {"handle_ids": ["h1"]},
                },
            ],
        },
    ]
    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history",
        lambda _cid: (history_wfh, "anthropic"),
    )
    assert (
        wait_resume._conversation_has_dangling_slack_reply("c-wfh") is False
    )

    # No dangling tool_use -> False.
    history_done = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "All done."}],
        },
    ]
    monkeypatch.setattr(
        "chat.gemini_api.history._load_sdk_history",
        lambda _cid: (history_done, "anthropic"),
    )
    assert (
        wait_resume._conversation_has_dangling_slack_reply("c-done") is False
    )


def test_send_slack_reply_does_not_set_typing_before_suspend(_isolated_db, _user_id):
    """The ``send_slack_reply_and_get_response`` arm must NOT set the
    typing indicator before raising :class:`SuspendForSlackReply`.

    Slack auto-clears ``assistant.threads.setStatus`` when the bot posts
    via ``chat.postMessage`` (which ``post_thread_reply`` does). Re-setting
    "Quest is working..." right before the suspend (the regression this
    test guards) leaves a stale indicator visible to the user during the
    user-wait period -- exactly when Quest is NOT working. Re-asserting
    the indicator is the resume task's responsibility, not the suspend
    arm's.
    """
    import inspect

    from chat.gemini_api import turn_tools

    src = inspect.getsource(turn_tools._handle_send_slack_reply)

    # Locate the SuspendForSlackReply branch.
    idx = src.find("raise SuspendForSlackReply(")
    assert idx > 0, (
        "SuspendForSlackReply raise not found in _handle_send_slack_reply"
    )

    # The code in the 600 chars immediately before the raise must NOT
    # call set_typing. Comment lines are ignored -- the arm deliberately
    # carries a "do NOT call set_typing here" explanation that names it.
    leading = src[max(0, idx - 600):idx]
    code_only = "\n".join(
        line for line in leading.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "set_typing" not in code_only, (
        "set_typing must not be called immediately before "
        "SuspendForSlackReply -- Slack auto-clears the indicator on "
        "chat.postMessage and re-setting it here leaves the indicator "
        "stuck during the user-wait period."
    )


def test_resume_path_tolerates_missing_reason(_isolated_db, _user_id):
    """The resume path re-builds the result from the DB.

    The reason is informational only at resume time (the wait already
    happened), so missing reason in saved sdk_history must not crash.
    """
    store, _ = _isolated_db
    import db.tool_wait_handle_store as fresh_store
    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        from chat.gemini_api.turn_tools import _build_wait_for_handles_result

        h = _run(store.create_handle(
            user_id=_user_id, conversation_id="c1",
            kind="action_request", tool_id="t1",
            payload={"request_id": 1},
        ))
        _run(store.resolve_handle(
            h["id"], new_status="accepted", response={"memory_id": "m-1"},
        ))
        # Build the result with no reason in scope -- this mirrors the
        # resume path's call when older saved sdk_history lacks a reason.
        payload = _run(_build_wait_for_handles_result(_user_id, [h["id"]]))
        assert payload["resolved"][0]["status"] == "accepted"
        assert payload["still_pending"] == []
