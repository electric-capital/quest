"""Tests for the unread state of conversations.

A conversation is unread while ``unread_seq > read_seq``:

1. Store: ``mark_conversation_unread`` advances ``unread_seq`` to the row's
   high-water message seq (web-origin conversations only; not archived
   ones, and a routine's conversation only once the user has sent a message
   of their own), ``mark_conversation_read`` advances ``read_seq``
   monotonically, clamped to that high-water seq and owner-checked, and
   archiving marks the conversation read. The unread listings behind the
   project dot and the Slack reminder.
2. ``chat.unread.mark_unread_after_run`` publishes
   ``conversation_unread_changed`` to the owner and never raises.
3. ``POST /conversations/{id}/read`` publishes only when ``read_seq`` moved
   and changes nothing under admin impersonation.
4. Run paths: the WS send task and the headless resume mark the
   conversation unread before their ``send_message_finished`` publish, and
   not at all when the run was stopped; each says whether the user started
   the run.
5. The ``subscribed`` ack carries both seqs.

Store tests run against the shared ``isolated_db`` fixture with asyncio.run.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from chat import unread as unread_mod


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(isolated_db):
    """The conversation store on a fresh database holding users 1, 7, 8."""
    import db.conversation_store as store_mod

    async def _add_users():
        async with store_mod.AsyncSessionLocal() as db:
            for user_id in (1, 7, 8):
                db.add(isolated_db.models.User(
                    id=user_id, email=f"u{user_id}@example.com",
                    api_key=f"key-{user_id}",
                ))
            await db.commit()

    _run(_add_users())
    return store_mod


TS = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_conversation(store, user_id=1, *, origin=None, last_seq=0,
                       project_id=None, routine_id=None, custom_name=None,
                       ts=TS):
    conv_id = str(uuid.uuid4())
    _run(store.create_conversation(
        user_id, conv_id, ts, origin=origin, project_id=project_id,
        routine_id=routine_id, custom_name=custom_name,
    ))
    if last_seq:
        _run(store.record_messages_appended(conv_id, last_message_seq=last_seq))
    return conv_id


def _add(store, row):
    async def _do():
        async with store.AsyncSessionLocal() as db:
            db.add(row)
            await db.commit()

    _run(_do())


def _make_project(store, models, user_id=1, *, name=None, archived=False):
    project_id = str(uuid.uuid4())
    _add(store, models.Project(
        id=project_id, user_id=user_id, name=name or f"Project {project_id[:8]}",
        archived=archived,
        created_at=TS, updated_at=TS,
    ))
    return project_id


def _make_routine(store, models, project_id, user_id=1):
    routine_id = str(uuid.uuid4())
    _add(store, models.Routine(
        id=routine_id, project_id=project_id, user_id=user_id,
        name="Daily digest", prompt="Summarize.",
    ))
    return routine_id


def _mark_unread(store, conv_id, *, user_started=True):
    return _run(store.mark_conversation_unread(conv_id, user_started=user_started))


class _RecordingBus:
    def __init__(self):
        self.user_events: list[tuple[int, dict]] = []

    def publish_to_user(self, user_id, event):
        self.user_events.append((user_id, event))


@pytest.fixture()
def bus(monkeypatch):
    recording = _RecordingBus()
    monkeypatch.setattr(unread_mod, "bus", recording)
    return recording


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_new_conversation_is_read(store):
    conv_id = _make_conversation(store)
    meta = _run(store.get_conversation_meta(1, conv_id))
    assert (meta["unread_seq"], meta["read_seq"]) == (0, 0)


def test_mark_unread_advances_to_high_water_seq(store):
    conv_id = _make_conversation(store, last_seq=4)

    meta = _mark_unread(store, conv_id)

    assert (meta["unread_seq"], meta["read_seq"]) == (4, 0)
    stored = _run(store.get_conversation_meta(1, conv_id))
    assert stored["unread_seq"] == 4


def test_mark_unread_is_a_noop_without_new_messages(store):
    conv_id = _make_conversation(store, last_seq=4)
    _mark_unread(store, conv_id)

    assert _mark_unread(store, conv_id) is None
    assert _mark_unread(store, "missing") is None


@pytest.mark.parametrize("origin", ["slack", "inference_api", "user_subagent"])
def test_mark_unread_skips_conversations_read_elsewhere(store, origin):
    conv_id = _make_conversation(store, origin=origin, last_seq=4)

    assert _mark_unread(store, conv_id) is None
    assert _run(store.get_conversation_meta(1, conv_id))["unread_seq"] == 0


def test_mark_unread_applies_to_explicit_web_origin(store):
    conv_id = _make_conversation(store, origin="web", last_seq=2)
    assert _mark_unread(store, conv_id)["unread_seq"] == 2


@pytest.fixture()
def routine_conversation(store, isolated_db):
    """A routine's conversation holding the routine run's messages."""
    project_id = _make_project(store, isolated_db.models)
    routine_id = _make_routine(store, isolated_db.models, project_id)
    return _make_conversation(
        store, project_id=project_id, routine_id=routine_id, last_seq=4,
    )


def test_routine_run_leaves_its_conversation_read(store, routine_conversation):
    assert _mark_unread(store, routine_conversation, user_started=False) is None
    assert _run(store.get_conversation_meta(1, routine_conversation))["unread_seq"] == 0


def test_resume_of_a_routine_run_leaves_it_read(store, routine_conversation):
    """The routine run suspended on an approval card and resumed twice."""
    for seq in (6, 9):
        _run(store.record_messages_appended(routine_conversation, last_message_seq=seq))
        assert _mark_unread(store, routine_conversation, user_started=False) is None


def test_user_message_makes_a_routine_conversation_unread(store, routine_conversation):
    _run(store.record_messages_appended(routine_conversation, last_message_seq=7))

    meta = _mark_unread(store, routine_conversation, user_started=True)

    assert (meta["unread_seq"], meta["read_seq"]) == (7, 0)


def test_resume_after_a_user_message_marks_a_routine_conversation_unread(
    store, routine_conversation,
):
    """Once the user has taken the conversation over, a run that suspended
    and resumed is theirs too."""
    _run(store.record_messages_appended(routine_conversation, last_message_seq=7))
    _mark_unread(store, routine_conversation, user_started=True)
    _run(store.mark_conversation_read(1, routine_conversation, 7))
    _run(store.record_messages_appended(routine_conversation, last_message_seq=10))

    meta = _mark_unread(store, routine_conversation, user_started=False)

    assert (meta["unread_seq"], meta["read_seq"]) == (10, 7)


def test_resume_marks_an_ordinary_conversation_unread(store):
    conv_id = _make_conversation(store, last_seq=4)
    assert _mark_unread(store, conv_id, user_started=False)["unread_seq"] == 4


def test_archiving_marks_the_conversation_read(store):
    conv_id = _make_conversation(store, last_seq=4)
    _mark_unread(store, conv_id)

    meta = _run(store.archive_conversation(1, conv_id))

    assert (meta["unread_seq"], meta["read_seq"]) == (4, 4)
    # Unarchiving does not bring the dot back.
    meta = _run(store.unarchive_conversation(1, conv_id))
    assert (meta["unread_seq"], meta["read_seq"]) == (4, 4)


def test_archived_conversation_does_not_become_unread(store):
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.archive_conversation(1, conv_id))

    assert _mark_unread(store, conv_id) is None


def test_mark_read_clears_unread(store):
    conv_id = _make_conversation(store, last_seq=4)
    _mark_unread(store, conv_id)

    meta, changed = _run(store.mark_conversation_read(1, conv_id, 4))

    assert changed is True
    assert (meta["unread_seq"], meta["read_seq"]) == (4, 4)


def test_mark_read_never_moves_backwards(store):
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.mark_conversation_read(1, conv_id, 4))

    meta, changed = _run(store.mark_conversation_read(1, conv_id, 2))

    assert changed is False
    assert meta["read_seq"] == 4


def test_mark_read_is_clamped_to_the_high_water_seq(store):
    conv_id = _make_conversation(store, last_seq=4)

    meta, changed = _run(store.mark_conversation_read(1, conv_id, 999))

    assert changed is True
    assert meta["read_seq"] == 4


def test_stale_read_ack_leaves_newer_output_unread(store):
    """An acknowledgement that predates the latest run must not hide it."""
    conv_id = _make_conversation(store, last_seq=4)
    _run(store.record_messages_appended(conv_id, last_message_seq=9))
    _mark_unread(store, conv_id)

    meta, _ = _run(store.mark_conversation_read(1, conv_id, 4))

    assert meta["unread_seq"] > meta["read_seq"]


def test_mark_read_checks_ownership(store):
    conv_id = _make_conversation(store, user_id=1, last_seq=4)

    assert _run(store.mark_conversation_read(2, conv_id, 4)) is None
    assert _run(store.mark_conversation_read(1, "missing", 4)) is None
    assert _run(store.get_conversation_meta(1, conv_id))["read_seq"] == 0


def test_list_rows_carry_the_unread_seqs(store):
    from chat.storage import ChatStorage

    conv_id = _make_conversation(store, last_seq=4)
    _mark_unread(store, conv_id)

    rows = _run(ChatStorage.list_conversations(1))

    row = next(r for r in rows if r["id"] == conv_id)
    assert (row["unread_seq"], row["read_seq"]) == (4, 0)


# ---------------------------------------------------------------------------
# Unread listings (project dot, Slack reminder)
# ---------------------------------------------------------------------------


def test_project_unread_conversations_are_grouped_by_project(store, isolated_db):
    project_a = _make_project(store, isolated_db.models)
    project_b = _make_project(store, isolated_db.models)
    other_users = _make_project(store, isolated_db.models, user_id=7)
    unread_a = _make_conversation(store, project_id=project_a, last_seq=4)
    read_a = _make_conversation(store, project_id=project_a, last_seq=4)
    standalone = _make_conversation(store, last_seq=4)
    theirs = _make_conversation(store, user_id=7, project_id=other_users, last_seq=4)
    for conv_id in (unread_a, read_a, standalone, theirs):
        _mark_unread(store, conv_id)
    _run(store.mark_conversation_read(1, read_a, 4))
    _make_conversation(store, project_id=project_b, last_seq=4)  # never unread

    assert _run(store.list_project_unread_conversations(1)) == {
        project_a: [{"id": unread_a, "unread_seq": 4, "read_seq": 0}],
    }


def test_project_list_rows_carry_their_unread_conversations(store, isolated_db):
    from chat.project_routes import list_user_projects

    with_unread = _make_project(store, isolated_db.models)
    without = _make_project(store, isolated_db.models)
    conv_id = _make_conversation(store, project_id=with_unread, last_seq=4)
    _mark_unread(store, conv_id)

    body = _run(list_user_projects(include_archived=False, user={"id": 1}))

    rows = {p["id"]: p["unread_conversations"] for p in body["projects"]}
    assert rows == {
        with_unread: [{"id": conv_id, "unread_seq": 4, "read_seq": 0}],
        without: [],
    }


def test_unread_summaries_count_per_user_with_the_oldest_time(store, isolated_db):
    from datetime import timedelta

    hidden = _make_project(store, isolated_db.models, archived=True)
    older = _make_conversation(store, last_seq=2, ts=TS - timedelta(hours=3))
    newer = _make_conversation(store, last_seq=2, ts=TS)
    read = _make_conversation(store, last_seq=2, ts=TS - timedelta(hours=9))
    in_hidden = _make_conversation(
        store, project_id=hidden, last_seq=2, ts=TS - timedelta(hours=9),
    )
    theirs = _make_conversation(store, user_id=7, last_seq=2)
    for conv_id in (older, newer, read, in_hidden, theirs):
        _mark_unread(store, conv_id)
    _run(store.mark_conversation_read(1, read, 2))

    summaries = {s["user_id"]: s for s in _run(store.list_unread_summaries())}

    assert set(summaries) == {1, 7}
    assert summaries[1]["unread_count"] == 2
    assert summaries[1]["oldest_unread_at"] == (TS - timedelta(hours=3)).replace(tzinfo=None)
    assert summaries[7]["unread_count"] == 1


def test_unread_conversations_list_newest_first_with_titles(store, isolated_db):
    from datetime import timedelta

    project_id = _make_project(store, isolated_db.models, name="Ops")
    hidden = _make_project(store, isolated_db.models, archived=True)
    oldest = _make_conversation(
        store, last_seq=2, ts=TS - timedelta(hours=2), custom_name="Budget",
    )
    middle = _make_conversation(
        store, project_id=project_id, last_seq=2, ts=TS - timedelta(hours=1),
    )
    newest = _make_conversation(store, last_seq=2, ts=TS, custom_name="Hiring")
    in_hidden = _make_conversation(store, project_id=hidden, last_seq=2)
    for conv_id in (oldest, middle, newest, in_hidden):
        _mark_unread(store, conv_id)

    rows = _run(store.list_unread_conversations(1, limit=2))

    assert rows == [
        {"id": newest, "project_id": None, "project_name": None, "title": "Hiring"},
        {"id": middle, "project_id": project_id, "project_name": "Ops", "title": None},
    ]


# ---------------------------------------------------------------------------
# Run-end hook
# ---------------------------------------------------------------------------


def test_mark_unread_after_run_publishes_to_the_owner(store, bus):
    conv_id = _make_conversation(store, user_id=7, last_seq=3)

    _run(unread_mod.mark_unread_after_run(conv_id, user_started=True))

    assert bus.user_events == [(7, {
        "type": "conversation_unread_changed",
        "conversation_id": conv_id,
        "project_id": None,
        "unread_seq": 3,
        "read_seq": 0,
    })]


def test_unread_event_names_the_conversations_project(store, bus, isolated_db):
    project_id = _make_project(store, isolated_db.models)
    conv_id = _make_conversation(store, project_id=project_id, last_seq=3)

    _run(unread_mod.mark_unread_after_run(conv_id, user_started=True))

    assert bus.user_events[0][1]["project_id"] == project_id


def test_mark_unread_after_run_is_silent_when_nothing_changed(store, bus):
    conv_id = _make_conversation(store, origin="slack", last_seq=3)

    _run(unread_mod.mark_unread_after_run(conv_id, user_started=True))

    assert bus.user_events == []


def test_mark_unread_after_run_never_raises(monkeypatch, bus):
    async def boom(_conversation_id, *, user_started):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        unread_mod.conversation_store, "mark_conversation_unread", boom,
    )

    _run(unread_mod.mark_unread_after_run("conv", user_started=True))

    assert bus.user_events == []


# ---------------------------------------------------------------------------
# POST /conversations/{id}/read
# ---------------------------------------------------------------------------


def test_read_endpoint_marks_read_and_publishes(store, bus):
    from chat.routes.conversations import (
        MarkReadRequest, mark_conversation_read_endpoint,
    )

    conv_id = _make_conversation(store, user_id=7, last_seq=5)
    _mark_unread(store, conv_id)

    body = _run(mark_conversation_read_endpoint(
        conv_id, MarkReadRequest(seq=5), user={"id": 7},
    ))

    assert body == {"unread_seq": 5, "read_seq": 5}
    assert bus.user_events == [(7, {
        "type": "conversation_unread_changed",
        "conversation_id": conv_id,
        "project_id": None,
        "unread_seq": 5,
        "read_seq": 5,
    })]

    # A repeat changes nothing and publishes nothing.
    bus.user_events.clear()
    _run(mark_conversation_read_endpoint(
        conv_id, MarkReadRequest(seq=5), user={"id": 7},
    ))
    assert bus.user_events == []


def test_read_endpoint_is_a_noop_under_impersonation(store, bus):
    """An admin viewing as the owner must not clear the owner's unread dot."""
    from chat.routes.conversations import (
        MarkReadRequest, mark_conversation_read_endpoint,
    )

    conv_id = _make_conversation(store, user_id=7, last_seq=5)
    _mark_unread(store, conv_id)

    body = _run(mark_conversation_read_endpoint(
        conv_id, MarkReadRequest(seq=5),
        user={"id": 7, "_impersonator_uid": 1},
    ))

    assert body == {"unread_seq": 5, "read_seq": 0}
    assert bus.user_events == []
    assert _run(store.get_conversation_meta(7, conv_id))["read_seq"] == 0


def test_read_endpoint_404s_for_another_users_conversation(store, bus):
    from chat.routes.conversations import (
        MarkReadRequest, mark_conversation_read_endpoint,
    )

    conv_id = _make_conversation(store, user_id=7, last_seq=5)

    with pytest.raises(HTTPException) as excinfo:
        _run(mark_conversation_read_endpoint(
            conv_id, MarkReadRequest(seq=5), user={"id": 8},
        ))

    assert excinfo.value.status_code == 404
    assert bus.user_events == []


# ---------------------------------------------------------------------------
# Run paths
# ---------------------------------------------------------------------------


def _started(user_started):
    return "user" if user_started else "continued"


class _ConversationBus:
    def __init__(self, log):
        self._log = log

    def publish_to_conversation(self, _conversation_id, event):
        self._log.append(event["type"])


def _patch_send_run(monkeypatch, run_impl):
    """Stub everything ``_run_send_message`` touches; returns the ordered
    log of unread marks and conversation-channel publishes."""
    from chat.realtime import socket as socket_mod

    log: list[str] = []

    async def fake_mark(conversation_id, *, user_started):
        log.append(f"unread:{conversation_id}:{_started(user_started)}")

    def fake_make_flush(_conversation_id, _messages_out, *, log_prefix):
        async def flush():
            return None

        return flush, asyncio.Lock()

    async def fake_cancel_handles(_user_id, _conversation_id):
        return None

    monkeypatch.setattr(unread_mod, "mark_unread_after_run", fake_mark)
    monkeypatch.setattr(socket_mod, "make_flush_callback", fake_make_flush)
    monkeypatch.setattr(socket_mod, "run_conversation_turn", run_impl)
    monkeypatch.setattr(socket_mod, "bus", _ConversationBus(log))
    monkeypatch.setattr(
        socket_mod, "_save_interrupted_sdk_history", lambda *a, **k: None,
    )
    monkeypatch.setattr(socket_mod, "remove_chat_session", lambda *a, **k: None)
    monkeypatch.setattr(
        socket_mod, "cancel_pending_wait_handles_for_conversation",
        fake_cancel_handles,
    )
    return socket_mod, log


def _send_kwargs(conversation_id):
    return dict(
        app=None, user={"id": 1, "email": "u@example.com"},
        conversation_id=conversation_id, user_message="hi",
        timezone_str="UTC", selected_model=None, project_id=None,
        routine_id=None, skill_ids=None,
    )


def test_send_run_marks_unread_before_finishing(monkeypatch):
    async def fake_run(**_kwargs):
        return None

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)

    _run(socket_mod._run_send_message(**_send_kwargs("c1")))

    assert log == ["unread:c1:user", "send_message_finished"]


def test_failed_send_run_still_marks_unread(monkeypatch):
    async def fake_run(**_kwargs):
        raise RuntimeError("boom")

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)

    _run(socket_mod._run_send_message(**_send_kwargs("c1")))

    assert log == ["unread:c1:user", "send_message_finished"]


def test_routine_send_run_is_not_user_started(monkeypatch):
    """The one-click routine run (the routine prompt as first message) is
    the routine's own run, not a message the user wrote."""
    async def fake_run(**_kwargs):
        return None

    async def fake_drive(run_turn, *, prompt, **_kwargs):
        await run_turn(prompt)
        return True

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)
    monkeypatch.setattr("chat.routine_runs.drive_routine_run", fake_drive)

    _run(socket_mod._run_send_message(
        **_send_kwargs("c1"), routine_run=True,
    ))

    assert log == ["unread:c1:continued", "send_message_finished"]


def test_stopped_send_run_does_not_mark_unread(monkeypatch):
    async def fake_run(**_kwargs):
        raise asyncio.CancelledError()

    socket_mod, log = _patch_send_run(monkeypatch, fake_run)

    _run(socket_mod._run_send_message(**_send_kwargs("c1")))

    assert log == ["send_message_finished"]


def _patch_resume(monkeypatch, run_impl):
    import chat.gemini_api as gemini_mod
    import db.conversation_store as conv_store_mod
    from chat.realtime.bus import bus as live_bus
    from chat.wait_handles import resume as resume_mod

    log: list[str] = []

    async def no_handles(_user_id, _conversation_id):
        return []

    async def fake_meta(_user_id, _conversation_id):
        return {"origin": "web"}

    async def fake_flags(_conversation_id):
        return []

    async def fake_mark(conversation_id, *, user_started):
        log.append(f"unread:{conversation_id}:{_started(user_started)}")

    def fake_make_flush(_conversation_id, _messages_out, *, log_prefix):
        async def flush():
            return None

        return flush, asyncio.Lock()

    monkeypatch.setattr(resume_mod, "_unresolved_blocking_handle_ids", no_handles)
    monkeypatch.setattr(resume_mod, "_stopped_handle_ids", no_handles)
    monkeypatch.setattr(resume_mod, "_streaming_resumes", set())
    monkeypatch.setattr(resume_mod, "make_flush_callback", fake_make_flush)
    monkeypatch.setattr(conv_store_mod, "get_conversation_meta", fake_meta)
    monkeypatch.setattr(conv_store_mod, "get_conversation_flags", fake_flags)
    monkeypatch.setattr(gemini_mod, "run_conversation_turn", run_impl)
    monkeypatch.setattr(unread_mod, "mark_unread_after_run", fake_mark)
    monkeypatch.setattr(
        live_bus, "publish_to_conversation",
        lambda _cid, event: log.append(event["type"]),
    )
    return resume_mod, log


def test_resume_marks_unread_before_finishing(monkeypatch):
    async def fake_run(**_kwargs):
        return None

    resume_mod, log = _patch_resume(monkeypatch, fake_run)

    _run(resume_mod._run_resume(None, {"id": 1}, "c2"))

    assert log == ["resume_started", "unread:c2:continued", "send_message_finished"]


def test_stopped_resume_does_not_mark_unread(monkeypatch):
    async def fake_run(**_kwargs):
        raise asyncio.CancelledError()

    resume_mod, log = _patch_resume(monkeypatch, fake_run)

    with pytest.raises(asyncio.CancelledError):
        _run(resume_mod._run_resume(None, {"id": 1}, "c2"))

    assert log == ["resume_started", "send_message_finished"]


# ---------------------------------------------------------------------------
# Subscribe ack
# ---------------------------------------------------------------------------


def test_subscribed_ack_carries_the_unread_seqs(monkeypatch, store):
    from chat.realtime import socket as socket_mod
    from chat.realtime.bus import Bus
    from chat.realtime.replay_buffer import ReplayBuffer

    class _FakeWebSocket:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

    conv_id = _make_conversation(store, user_id=7, last_seq=6)
    _mark_unread(store, conv_id)
    _run(store.mark_conversation_read(7, conv_id, 2))

    ws = _FakeWebSocket()
    conn = socket_mod._Connection(ws, {"id": 7, "email": "u@example.com"})
    monkeypatch.setattr(socket_mod, "bus", Bus())
    monkeypatch.setattr(socket_mod, "replay_buffer", ReplayBuffer())

    _run(socket_mod._handle_subscribe(
        conn, {"op": "subscribe", "conversation_id": conv_id, "last_seq": 6},
    ))

    ack = ws.sent[-1]
    assert ack["type"] == "subscribed"
    assert ack["mode"] == "up_to_date"
    assert (ack["unread_seq"], ack["read_seq"]) == (6, 2)
