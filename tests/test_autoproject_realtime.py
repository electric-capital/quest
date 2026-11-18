"""AutoProject realtime events, the WebSocket Stop, and where run conversations
do not appear.

- ``autoproject_list_changed`` and ``autoproject_changed {autoproject_id,
  event_seq, brief_version, active_run}`` reach the AutoProject's owner only,
  on the transitions that change what the list or the AutoProject page shows.
- The WS ``stop`` op acts only on the caller's own conversation: another
  user's Stop neither cancels the run's turn nor stops the run.
- Run conversations stay out of the Conversations list, search and the
  unread queries, and cannot be converted to a project or compacted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from chat.autoprojects import runs as runs_mod
from chat.autoprojects import service
from db import autoproject_store, conversation_store, tool_wait_handle_store, user_store

OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"
CHANGED_KEYS = {"type", "autoproject_id", "event_seq", "brief_version", "active_run"}


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
    events: list[tuple[int, dict]] = []
    publish = {"fail": False}

    def _publish(uid, event):
        if publish["fail"]:
            raise RuntimeError("bus down")
        events.append((uid, event))

    monkeypatch.setattr(bus, "publish_to_user", _publish)
    monkeypatch.setattr(bus, "publish_to_conversation", lambda cid, ev: None)

    async def _users():
        owner = await user_store.create_user(OWNER_EMAIL, "Owner", "key-owner")
        stranger = await user_store.create_user(STRANGER_EMAIL, "Stranger", "key-stranger")
        return owner, stranger

    owner, stranger = _run(_users())
    return SimpleNamespace(
        owner=owner, stranger=stranger, events=events, publish=publish, db=isolated_db,
    )


async def _start(user, autoproject, prompt="Go") -> dict:
    cid = await service.create_run_conversation(user, autoproject["id"])
    meta = await conversation_store.get_conversation_meta(user["id"], cid)
    return (await service.start_run(user, meta, prompt=prompt, model="fake-model")).run


def _drain(env) -> list[dict]:
    """The events published since the last drain (owner only, checked)."""
    got = list(env.events)
    env.events.clear()
    assert all(uid == env.owner["id"] for uid, _ev in got), got
    return [ev for _uid, ev in got]


def _types(events) -> list[str]:
    return [ev["type"] for ev in events if ev["type"].startswith("autoproject")]


async def _suspend(run) -> None:
    async def turn(_message):
        messages.append({"type": "tool_use", "tool_name": "ask_autoproject_question",
                         "tool_input": {}, "tool_id": "q"})

    async def on_event(_event):
        return None

    messages: list[dict] = []
    await runs_mod.drive_autoproject_run(
        turn, run=run, prompt="Go", messages_out=messages, on_event=on_event,
        log_prefix="[test]",
    )


# ---------------------------------------------------------------------------
# Events on each transition
# ---------------------------------------------------------------------------


def test_events_follow_the_lifecycle_and_reach_only_the_owner(env):
    async def body():
        # Another user's AutoProject activity never reaches the owner, and
        # the owner's never reaches them (checked by _drain).
        await service.create_autoproject(env.stranger, title="Theirs")
        env.events.clear()

        autoproject = await service.create_autoproject(env.owner, title="Mine")
        assert _types(_drain(env)) == ["autoproject_list_changed"]

        await service.update_autoproject(env.owner, autoproject["id"], title="Renamed")
        assert _types(_drain(env)) == ["autoproject_changed", "autoproject_list_changed"]

        run = await _start(env.owner, autoproject)
        started = _drain(env)
        assert _types(started) == ["autoproject_changed", "autoproject_list_changed"]
        changed = started[0]
        assert set(changed) == CHANGED_KEYS
        assert changed["autoproject_id"] == autoproject["id"]
        assert changed["active_run"] == {"id": run["id"], "seq": 1, "status": "running"}

        await _suspend(run)
        waiting = _drain(env)
        assert _types(waiting) == ["autoproject_changed", "autoproject_list_changed"]
        assert waiting[0]["active_run"]["status"] == "waiting"

        # The user's brief edit changes the page, not the list row.
        await service.set_brief(env.owner, autoproject["id"], "Brief", expected_brief_version=0)
        edited = _drain(env)
        assert _types(edited) == ["autoproject_changed"]
        assert edited[0]["brief_version"] == 1

        # Completing with a brief: the brief write, then the status change.
        await autoproject_store.freeze_run_context(run["id"], context="c", brief_version=1)
        outcome = await runs_mod.complete_autoproject_run(
            autoproject_id=autoproject["id"], run_id=run["id"],
            args={"summary": "s", "result": "r", "notes": "n", "brief": "New brief"},
        )
        assert outcome.ended is True
        completed = _drain(env)
        assert _types(completed) == [
            "autoproject_changed", "autoproject_changed", "autoproject_list_changed",
        ]
        assert completed[0]["brief_version"] == 2
        assert completed[-2]["active_run"] is None

        seqs = [ev["event_seq"] for ev in started + waiting + edited + completed
                if ev["type"] == "autoproject_changed"]
        assert seqs == sorted(seqs) and len(set(seqs)) >= 3

        # Reading it changes the list row's unread mark only.
        current = await autoproject_store.get_autoproject_by_id(autoproject["id"])
        await service.mark_autoproject_read(env.owner, autoproject["id"], seq=current["event_seq"])
        assert _types(_drain(env)) == ["autoproject_list_changed"]
        # A repeated acknowledgement changes nothing and publishes nothing.
        await service.mark_autoproject_read(env.owner, autoproject["id"], seq=current["event_seq"])
        assert _drain(env) == []

        await service.set_autoproject_archived(env.owner, autoproject["id"], True)
        assert _types(_drain(env)) == ["autoproject_changed", "autoproject_list_changed"]
        await service.set_autoproject_archived(env.owner, autoproject["id"], False)
        assert _types(_drain(env)) == ["autoproject_changed", "autoproject_list_changed"]

        await service.delete_autoproject(env.owner, autoproject["id"])
        deleted = _drain(env)
        assert _types(deleted) == ["autoproject_changed", "autoproject_list_changed"]
        assert deleted[0]["active_run"] is None

    _run(body())


def test_stop_and_failure_publish_both_events(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Mine")
        run = await _start(env.owner, autoproject)
        _drain(env)
        await runs_mod.stop_autoproject_run(env.owner["id"], run["conversation_id"])
        stopped = _drain(env)
        assert _types(stopped) == ["autoproject_changed", "autoproject_list_changed"]
        assert stopped[0]["active_run"] is None

        run2 = await _start(env.owner, autoproject)
        _drain(env)
        await service.fail_unstarted_run(run2["id"], "the message could not be saved")
        assert _types(_drain(env)) == ["autoproject_changed", "autoproject_list_changed"]
        assert (await autoproject_store.get_run(run2["id"]))["status"] == "failed"

    _run(body())


def test_a_failing_bus_never_undoes_the_write(env):
    async def body():
        env.publish["fail"] = True
        autoproject = await service.create_autoproject(env.owner, title="Mine")
        run = await _start(env.owner, autoproject)
        await runs_mod.stop_autoproject_run(env.owner["id"], run["conversation_id"])
        assert (await autoproject_store.get_run(run["id"]))["status"] == "stopped"

    _run(body())


# ---------------------------------------------------------------------------
# The WebSocket stop op
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.app = object()
        self.cookies: dict = {}

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        pass


@pytest.mark.parametrize("caller", ["stranger", "owner"])
def test_ws_stop_acts_only_on_the_callers_own_conversation(env, monkeypatch, caller):
    from chat.realtime import socket as socket_mod
    from chat.wait_handles import resume as wait_resume

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Mine")
        run = await _start(env.owner, autoproject)
        await _suspend(run)
        question = await tool_wait_handle_store.create_handle(
            user_id=env.owner["id"], conversation_id=run["conversation_id"],
            kind="autoproject_question", tool_id="q", payload={"question": "?"},
        )
        live = asyncio.create_task(asyncio.sleep(30))
        resume = asyncio.create_task(asyncio.sleep(30))
        monkeypatch.setitem(socket_mod._active_send_runs, run["conversation_id"], live)
        monkeypatch.setattr(
            wait_resume, "get_active_resume",
            lambda cid: resume if cid == run["conversation_id"] else None,
        )

        user = env.stranger if caller == "stranger" else env.owner
        ws = _FakeWebSocket()
        await socket_mod._handle_stop(
            socket_mod._Connection(ws, dict(user)), {"conversation_id": run["conversation_id"]},
        )
        await asyncio.sleep(0)
        status = (await autoproject_store.get_run(run["id"]))["status"]
        handle_status = (await tool_wait_handle_store.get_handle(question["id"]))["status"]
        if caller == "stranger":
            assert not live.cancelled() and not resume.cancelled()
            assert status == "waiting" and handle_status == "pending"
            assert ws.sent == []
            live.cancel()
            resume.cancel()
        else:
            assert live.cancelled() and resume.cancelled()
            assert status == "stopped" and handle_status == "cancelled"
            assert ws.sent == [
                {"type": "stop_acknowledged", "conversation_id": run["conversation_id"]},
            ]

    _run(body())


# ---------------------------------------------------------------------------
# Run conversations stay out of the conversation surfaces
# ---------------------------------------------------------------------------


def test_run_conversations_are_not_listed_searched_or_unread(env):
    from chat.storage import ChatStorage

    async def body():
        plain, _ = await ChatStorage.create_conversation(env.owner["id"])
        autoproject = await service.create_autoproject(env.owner, title="Mine")
        run = await _start(env.owner, autoproject)
        for cid in (plain, run["conversation_id"]):
            await ChatStorage.append_structured_messages(cid, [{
                "type": "text", "role": "assistant", "content": "the zebra report",
                "timestamp": "2026-11-18T00:00:00Z",
            }])
        return plain, run

    plain, run = _run(body())
    # Mark both unread directly, as the unread machinery would a web chat.
    with env.db.engine.begin() as connection:
        connection.execute(
            text("UPDATE conversations SET unread_seq = 5, read_seq = 0 WHERE id IN (:a, :b)"),
            {"a": plain, "b": run["conversation_id"]},
        )

    async def after():
        listed = await conversation_store.list_conversations_meta(env.owner["id"])
        assert [c["id"] for c in listed] == [plain]
        with_runs = await conversation_store.list_conversations_meta(
            env.owner["id"], include_autoproject_runs=True,
        )
        assert {c["id"] for c in with_runs} == {plain, run["conversation_id"]}

        found = await ChatStorage.search_conversations(env.owner["id"], "zebra")
        found_ids = {r.get("conversation_id") for r in found.get("results", [])}
        assert plain in found_ids and run["conversation_id"] not in found_ids

        unread = await conversation_store.list_unread_conversations(env.owner["id"], 50)
        assert [c["id"] for c in unread] == [plain]
        summaries = await conversation_store.list_unread_summaries()
        assert [(s["user_id"], s["unread_count"]) for s in summaries] == [(env.owner["id"], 1)]

    _run(after())


def test_run_conversations_cannot_be_converted_or_compacted(env):
    from chat.project_routes import (
        CreateProjectFromConversationRequest,
        create_project_from_conversation,
    )
    from chat.routes.conversations import compact_conversation_endpoint

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Mine")
        run = await _start(env.owner, autoproject)
        cid = run["conversation_id"]

        with pytest.raises(HTTPException) as convert:
            await create_project_from_conversation(
                CreateProjectFromConversationRequest(name="P", conversation_id=cid),
                user=dict(env.owner),
            )
        assert convert.value.status_code == 400
        assert convert.value.detail["error"] == "autoproject_run_conversation"

        with pytest.raises(HTTPException) as compact:
            await compact_conversation_endpoint(cid, user=dict(env.owner))
        assert compact.value.status_code == 409
        assert compact.value.detail["error"] == "autoproject_run_conversation"

        # The store-level move refuses it too.
        assert await conversation_store.set_conversation_project(
            env.owner["id"], cid, "some-project",
        ) is None
        meta = await conversation_store.get_conversation_meta(env.owner["id"], cid)
        assert meta["project_id"] is None

    _run(body())
