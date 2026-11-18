"""AutoProject runs: starting, driving, completing, stopping and settling a run.

A run is one conversation (``origin="autoproject"``) with one
``autoproject_runs`` row. These tests drive the real service, store and run
driver against an isolated database; only the model turn itself is faked
(``run_turn`` callables that do what a model would: call the completion
tool, end on a dangling tool_use, or just stop talking).

Covered: the send path's refusals (gate, archive, one active run, one
message per run, model rule, prompt cap), run-conversation creation and the
abandoned-conversation sweeps, every way the driver ends a run (completed,
waiting, one nudge, handoff missing, failed, stopped, a resume after the
gate closed), ``complete_autoproject_run`` (caps, optional brief, the
default-title rename, the brief conflict and its forge-proof retry token),
the settling every finish shares (open cards closed, unread marking),
Stop, the startup sweeps and deleting an AutoProject or a run while it is
active.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from chat.autoprojects import runs as runs_mod
from chat.autoprojects import service
from chat.autoprojects.constants import (
    AUTOPROJECT_BRIEF_MAX_CHARS,
    AUTOPROJECT_PROMPT_MAX_CHARS,
    AUTOPROJECT_RUN_NOTES_MAX_CHARS,
    AUTOPROJECT_RUN_RESULT_MAX_CHARS,
    AUTOPROJECT_RUN_SUMMARY_MAX_CHARS,
    AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE,
    MAX_AUTOPROJECT_COMPLETION_NUDGES,
)
from chat.autoprojects.runs import (
    AUTOPROJECT_BRIEF_CONFLICT_NUDGE,
    AUTOPROJECT_COMPLETION_NUDGE,
    AUTOPROJECT_NUDGE_MESSAGE_TYPE,
    RUN_FINISHED_REASON,
)
from db import (
    action_request_store,
    autoproject_store,
    conversation_store,
    tool_wait_handle_store,
    user_store,
)

OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixture and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(isolated_db, tmp_path, monkeypatch):
    """Isolated database and data dirs, the gate open for both users, every
    model allowed, and the per-user bus captured."""
    import chat.storage as storage_mod
    import config.feature_gates as feature_gates
    import config.model_selection as model_selection
    from chat.realtime.bus import bus

    chats = tmp_path / "chats"
    autoprojects_dir = tmp_path / "autoprojects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats)
    monkeypatch.setattr(storage_mod, "AUTOPROJECTS_DIR", autoprojects_dir)

    gate = {OWNER_EMAIL, STRANGER_EMAIL}
    monkeypatch.setattr(
        feature_gates, "autoprojects_enabled_for", lambda email: email in gate,
    )
    allowed = {"ok": True}
    monkeypatch.setattr(
        model_selection, "is_model_allowed",
        lambda model, public=False: allowed["ok"] and model != "blocked-model",
    )

    events: list[tuple[int, dict]] = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: events.append((uid, ev)))
    monkeypatch.setattr(bus, "publish_to_conversation", lambda cid, ev: None)

    async def _users():
        owner = await user_store.create_user(OWNER_EMAIL, "Owner", "key-owner")
        stranger = await user_store.create_user(STRANGER_EMAIL, "Stranger", "key-stranger")
        return owner, stranger

    owner, stranger = _run(_users())
    return SimpleNamespace(
        owner=owner, stranger=stranger, gate=gate, events=events,
        chats=chats, autoprojects_dir=autoprojects_dir, db=isolated_db,
    )


async def _new_autoproject(env, user=None, **kwargs) -> dict:
    if "title" not in kwargs and "prompt" not in kwargs:
        kwargs["title"] = "Research"
    return await service.create_autoproject(user or env.owner, **kwargs)


async def _meta(user, conversation_id) -> dict:
    return await conversation_store.get_conversation_meta(user["id"], conversation_id)


async def _start(env, autoproject, *, user=None, prompt="Find three sources") -> dict:
    """Create a run conversation and accept its message: the run row."""
    user = user or env.owner
    conversation_id = await service.create_run_conversation(user, autoproject["id"])
    started = await service.start_run(
        user, await _meta(user, conversation_id), prompt=prompt, model="fake-model",
    )
    return started.run


async def _get_run(run) -> dict:
    return await autoproject_store.get_run(run["id"])


async def _get_autoproject(autoproject) -> dict:
    return await autoproject_store.get_autoproject_by_id(autoproject["id"])


async def _open_cards(env, run) -> tuple[dict, dict]:
    """An open approval card (action request + its wait handle) on the run's
    conversation, the way create_action_request leaves one."""
    request = await action_request_store.create_action_request(
        env.owner["id"], run["conversation_id"], "send_email", {"to": "x"}, "because",
    )
    handle = await tool_wait_handle_store.create_handle(
        user_id=env.owner["id"], conversation_id=run["conversation_id"],
        kind="action_request", tool_id="tool-card", payload={"request_id": request["id"]},
        correlation_kind="action_request", correlation_id=str(request["id"]),
    )
    return request, handle


def _complete_args(**overrides) -> dict:
    args = {
        "summary": "Found three sources",
        "result": "Here are the three sources.",
        "notes": "Source B is paywalled.",
    }
    args.update(overrides)
    return args


async def _complete(run, **overrides):
    return await runs_mod.complete_autoproject_run(
        autoproject_id=run["autoproject_id"], run_id=run["id"], args=_complete_args(**overrides),
    )


def _event_types(env) -> list[str]:
    return [event["type"] for _uid, event in env.events]


def _tool_use(name: str, tool_id: str) -> dict:
    return {"type": "tool_use", "tool_name": name, "tool_input": {}, "tool_id": tool_id}


class _Turns:
    """Scripted turns for ``drive_autoproject_run``: each ``run_turn`` call
    runs the next script, an async callable taking the shared message list."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.inputs: list[str] = []
        self.messages: list[dict] = []
        self.events: list[dict] = []

    async def run_turn(self, message: str) -> None:
        self.inputs.append(message)
        await self.scripts.pop(0)(self.messages)

    async def on_event(self, event: dict) -> None:
        self.events.append(event)

    async def drive(self, run, *, prompt="Find three sources", user=None):
        return await runs_mod.drive_autoproject_run(
            self.run_turn, run=run, prompt=prompt, messages_out=self.messages,
            on_event=self.on_event, log_prefix="[test]", user=user,
        )


def _says(text_):
    async def script(messages):
        messages.append({"type": "text", "role": "assistant", "content": text_})
    return script


def _completes(run, **overrides):
    async def script(messages):
        outcome = await _complete(run, **overrides)
        messages.append({"type": "tool_result", "tool_id": "done", "tool_output": outcome.result})
    return script


def _suspends(messages_tail=None):
    async def script(messages):
        messages.append(_tool_use("ask_autoproject_question", "q-1"))
        messages.extend(messages_tail or [])
    return script


# ---------------------------------------------------------------------------
# The send path: _handle_send_message on a run conversation
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


@pytest.fixture()
def send(env, monkeypatch):
    """Drive the WS send path on the real database. The model run itself
    (``_run_send_message``) is recorded, not run."""
    from chat.realtime import socket as socket_mod

    started: list[dict] = []

    async def _fake_run_send_message(**kwargs) -> None:
        started.append(kwargs)

    monkeypatch.setattr(socket_mod, "_run_send_message", _fake_run_send_message)
    monkeypatch.setattr(socket_mod, "check_user_allowed", lambda _email: True)

    async def _send(user, conversation_id, message, **extra):
        ws = _FakeWebSocket()
        conn = socket_mod._Connection(ws, dict(user))
        await socket_mod._handle_send_message(
            conn, {"conversation_id": conversation_id, "message": message, **extra},
        )
        task = socket_mod._active_send_runs.pop(conversation_id, None)
        if task is not None:
            await task
        return ws.sent

    return SimpleNamespace(send=_send, started=started)


def _rejection(sent: list[dict]) -> dict | None:
    return next((m for m in sent if m.get("type") == "send_message_rejected"), None)


def test_first_message_starts_the_run_with_the_prompt_verbatim(env, send):
    async def body():
        autoproject = await _new_autoproject(env)
        cid = await service.create_run_conversation(env.owner, autoproject["id"])
        # A run takes no conversation flags: the magic line stays in the prompt.
        prompt = "%%flags[nested_subagents]\nSummarize the inbox"
        sent = await send.send(env.owner, cid, prompt, model="fake-model")
        assert _rejection(sent) is None, sent
        run = await autoproject_store.get_run_by_conversation(cid)
        assert run["status"] == "running" and run["seq"] == 1
        assert run["prompt"] == prompt
        assert run["model"] == "fake-model"
        assert len(send.started) == 1
        kwargs = send.started[0]
        assert kwargs["autoproject_id"] == autoproject["id"]
        assert kwargs["autoproject_run"]["id"] == run["id"]
        assert kwargs["flags"] is None
        meta = await _meta(env.owner, cid)
        assert meta["flags"] == [] and meta["project_id"] is None

    _run(body())


@pytest.mark.parametrize("case", [
    "gate_closed", "archived", "run_active", "second_message", "model_not_allowed",
    "prompt_too_long",
])
def test_send_path_refusals_leave_no_run_and_no_message(env, send, case):
    async def body():
        autoproject = await _new_autoproject(env)
        cid = await service.create_run_conversation(env.owner, autoproject["id"])
        message, extra = "Find three sources", {"model": "fake-model"}
        expected_runs = 0
        if case == "gate_closed":
            env.gate.discard(OWNER_EMAIL)
            reason = "autoprojects_disabled"
        elif case == "archived":
            await service.set_autoproject_archived(env.owner, autoproject["id"], True)
            reason = "autoproject_archived"
        elif case == "run_active":
            # The other conversation of the AutoProject holds the active run;
            # the service would refuse to create this one, so it is created
            # straight through storage, as a race would leave it.
            await _start(env, autoproject)
            from chat.storage import ChatStorage
            cid = await ChatStorage.create_autoproject_run_conversation(
                env.owner["id"], autoproject["id"],
            )
            expected_runs = 1
            reason = "autoproject_run_active"
        elif case == "second_message":
            first = await send.send(env.owner, cid, "First prompt", model="fake-model")
            assert _rejection(first) is None
            expected_runs = 1
            reason = "autoproject_run_single_message"
        elif case == "model_not_allowed":
            extra = {"model": "blocked-model"}
            reason = "model_not_allowed"
        else:
            message = "x" * (AUTOPROJECT_PROMPT_MAX_CHARS + 1)
            reason = "invalid_prompt"

        before = await _meta(env.owner, cid)
        sent = await send.send(env.owner, cid, message, **extra)
        rejection = _rejection(sent)
        assert rejection is not None, sent
        assert rejection["reason"] == reason
        assert rejection["conversation_id"] == cid
        assert rejection["message"]
        if case == "run_active":
            assert rejection["active_run"]["seq"] == 1
        # Nothing was appended and no run row came from this send.
        after = await _meta(env.owner, cid)
        assert after["last_message_seq"] == before["last_message_seq"]
        assert await autoproject_store.count_runs(autoproject["id"]) == expected_runs
        assert len(send.started) == (1 if case == "second_message" else 0)

    _run(body())


def test_prompt_at_the_cap_is_accepted(env, send):
    async def body():
        autoproject = await _new_autoproject(env)
        cid = await service.create_run_conversation(env.owner, autoproject["id"])
        sent = await send.send(
            env.owner, cid, "x" * AUTOPROJECT_PROMPT_MAX_CHARS, model="fake-model",
        )
        assert _rejection(sent) is None
        run = await autoproject_store.get_run_by_conversation(cid)
        assert len(run["prompt"]) == AUTOPROJECT_PROMPT_MAX_CHARS

    _run(body())


def test_run_model_falls_back_to_the_autoproject_default(env):
    async def body():
        autoproject = await _new_autoproject(env, model="ap-default-model", thinking_effort="low")
        cid = await service.create_run_conversation(env.owner, autoproject["id"])
        started = await service.start_run(env.owner, await _meta(env.owner, cid), prompt="go")
        assert started.model == "ap-default-model"
        assert started.thinking_effort == "low"
        assert started.run["model"] == "ap-default-model"

    _run(body())


def test_start_run_refuses_an_ordinary_conversation(env):
    async def body():
        from chat.storage import ChatStorage
        cid, _ = await ChatStorage.create_conversation(env.owner["id"])
        with pytest.raises(ValueError):
            await service.start_run(env.owner, await _meta(env.owner, cid), prompt="go")

    _run(body())


# ---------------------------------------------------------------------------
# Creating run conversations; the abandoned-conversation sweeps
# ---------------------------------------------------------------------------


def test_run_conversation_is_private_and_outside_any_project(env):
    async def body():
        autoproject = await _new_autoproject(env)
        cid = await service.create_run_conversation(env.owner, autoproject["id"])
        meta = await _meta(env.owner, cid)
        assert meta["autoproject_id"] == autoproject["id"]
        assert meta["origin"] == "autoproject"
        assert meta["project_id"] is None
        assert (env.chats / cid).is_dir()
        # No run row until the message arrives.
        assert await autoproject_store.get_run_by_conversation(cid) is None

    _run(body())


@pytest.mark.parametrize("case,code", [
    ("gate_closed", "autoprojects_disabled"),
    ("foreign", "autoproject_not_found"),
    ("missing", "autoproject_not_found"),
    ("archived", "autoproject_archived"),
    ("run_active", "autoproject_run_active"),
])
def test_create_run_conversation_refusals(env, case, code):
    async def body():
        autoproject = await _new_autoproject(env)
        user, autoproject_id = env.owner, autoproject["id"]
        if case == "gate_closed":
            env.gate.discard(OWNER_EMAIL)
        elif case == "foreign":
            user = env.stranger
        elif case == "missing":
            autoproject_id = "00000000-0000-4000-8000-000000000000"
        elif case == "archived":
            await service.set_autoproject_archived(env.owner, autoproject_id, True)
        else:
            await _start(env, autoproject)
        with pytest.raises(autoproject_store.AutoProjectStoreError) as exc:
            await service.create_run_conversation(user, autoproject_id)
        assert exc.value.code == code
        if case in ("foreign", "missing"):
            # A foreign AutoProject reads exactly like a missing one.
            assert exc.value.message == f"AutoProject not found: {autoproject_id}"

    _run(body())


def _age_conversation(env, conversation_id: str, age: timedelta) -> None:
    created = (datetime.now(timezone.utc) - age).replace(tzinfo=None)
    with env.db.engine.begin() as connection:
        connection.execute(
            text("UPDATE conversations SET created_at = :c WHERE id = :id"),
            {"c": created, "id": conversation_id},
        )


def test_creating_a_run_conversation_drops_old_unsent_ones_of_that_autoproject(env):
    from chat.storage import ChatStorage

    async def body():
        autoproject = await _new_autoproject(env)
        other = await _new_autoproject(env, title="Other")
        old_unsent = await service.create_run_conversation(env.owner, autoproject["id"])
        young_unsent = await service.create_run_conversation(env.owner, autoproject["id"])
        other_old = await service.create_run_conversation(env.owner, other["id"])
        # An old run conversation that did start its run is kept.
        finished = await _start(env, autoproject)
        await _complete(finished)
        return autoproject, old_unsent, young_unsent, other_old, finished

    autoproject, old_unsent, young_unsent, other_old, finished = _run(body())
    grace = AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE
    for cid in (old_unsent, other_old, finished["conversation_id"]):
        _age_conversation(env, cid, grace + timedelta(minutes=1))
    _age_conversation(env, young_unsent, grace - timedelta(minutes=5))

    async def after():
        new_cid = await service.create_run_conversation(env.owner, autoproject["id"])
        assert await _meta(env.owner, old_unsent) is None
        assert not (env.chats / old_unsent).exists()
        for kept in (young_unsent, other_old, finished["conversation_id"], new_cid):
            assert await _meta(env.owner, kept) is not None, kept
        assert ChatStorage.get_conversation_dir(young_unsent).is_dir()

    _run(after())


def test_startup_sweep_deletes_day_old_conversations_without_a_run(env):
    async def body():
        autoproject = await _new_autoproject(env)
        abandoned = await service.create_run_conversation(env.owner, autoproject["id"])
        fresh = await service.create_run_conversation(env.owner, autoproject["id"])
        run = await _start(env, autoproject)
        await _complete(run)
        return abandoned, fresh, run

    abandoned, fresh, run = _run(body())
    for cid in (abandoned, run["conversation_id"]):
        _age_conversation(env, cid, timedelta(days=2))

    async def after():
        deleted = await service.sweep_abandoned_run_conversations()
        assert deleted == [abandoned]
        assert not (env.chats / abandoned).exists()
        assert await _meta(env.owner, fresh) is not None
        assert await _meta(env.owner, run["conversation_id"]) is not None

    _run(after())


# ---------------------------------------------------------------------------
# The driver: every way a turn can end
# ---------------------------------------------------------------------------


def test_driver_completion_by_the_tool_runs_one_turn(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        turns = _Turns(_completes(run))
        final = await turns.drive(run)
        assert final["status"] == "completed"
        assert final["handoff_missing"] is False
        assert turns.inputs == ["Find three sources"]
        assert turns.events == []

    _run(body())


def test_driver_suspended_turn_leaves_the_run_waiting_and_unread(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        env.events.clear()
        turns = _Turns(_suspends())
        final = await turns.drive(run)
        assert final["status"] == "waiting"
        assert turns.inputs == ["Find three sources"]  # never nudged
        ap = await _get_autoproject(autoproject)
        assert ap["unread"] is True
        changed = [ev for _u, ev in env.events if ev["type"] == "autoproject_changed"]
        assert changed[-1]["active_run"] == {"id": run["id"], "seq": 1, "status": "waiting"}
        assert "autoproject_list_changed" in _event_types(env)

    _run(body())


def test_driver_nudges_once_then_completes(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        turns = _Turns(_says("I looked around."), _completes(run))
        final = await turns.drive(run)
        assert final["status"] == "completed" and final["handoff_missing"] is False
        assert turns.inputs == ["Find three sources", AUTOPROJECT_COMPLETION_NUDGE]
        notices = [m for m in turns.messages if m.get("type") == AUTOPROJECT_NUDGE_MESSAGE_TYPE]
        assert len(notices) == 1
        assert notices[0]["role"] == "system" and notices[0]["attempt"] == 1
        assert notices[0]["content"] == AUTOPROJECT_COMPLETION_NUDGE
        assert [e["type"] for e in turns.events] == [AUTOPROJECT_NUDGE_MESSAGE_TYPE]
        assert turns.events[0]["conversation_id"] == run["conversation_id"]

    _run(body())


def test_driver_without_the_tool_after_the_nudge_saves_the_final_text(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        final_text = "## Three sources found\n\nA, B and C."
        turns = _Turns(*([_says("Working on it.")] + [_says(final_text)] * MAX_AUTOPROJECT_COMPLETION_NUDGES))
        final = await turns.drive(run)
        assert len(turns.inputs) == 1 + MAX_AUTOPROJECT_COMPLETION_NUDGES
        assert final["status"] == "completed"
        assert final["handoff_missing"] is True
        assert final["result"] == final_text
        assert final["summary"] == "Three sources found"
        assert final["notes"] == ""
        assert (await _get_autoproject(autoproject))["unread"] is True

    _run(body())


def test_driver_failure_marks_the_run_failed_and_reraises(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)

        async def boom(_messages):
            raise RuntimeError("provider down")

        with pytest.raises(RuntimeError):
            await _Turns(boom).drive(run)
        failed = await _get_run(run)
        assert failed["status"] == "failed"
        assert failed["error"] == "RuntimeError: provider down"
        assert failed["finished_at"] is not None
        assert (await _get_autoproject(autoproject))["unread"] is True

    _run(body())


def test_driver_cancellation_marks_the_run_stopped_without_unread(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)

        async def cancelled(_messages):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _Turns(cancelled).drive(run)
        assert (await _get_run(run))["status"] == "stopped"
        assert (await _get_autoproject(autoproject))["unread"] is False

    _run(body())


def test_driver_resume_moves_a_waiting_run_back_to_running(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await _Turns(_suspends()).drive(run)
        seen = {}

        async def resumed(_messages):
            seen["status"] = (await _get_run(run))["status"]
            await _completes(run)(_messages)

        turns = _Turns(resumed)
        final = await turns.drive(run, prompt=None)
        assert seen["status"] == "running"
        assert turns.inputs == [""]
        assert final["status"] == "completed"

    _run(body())


def test_driver_resume_of_a_finished_run_runs_no_turn(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await runs_mod.stop_autoproject_run(env.owner["id"], run["conversation_id"])
        turns = _Turns(_says("should not run"))
        final = await turns.drive(run, prompt=None)
        assert final["status"] == "stopped"
        assert turns.inputs == []

    _run(body())


@pytest.mark.parametrize("pass_user", [True, False])
def test_resume_after_the_gate_closed_stops_the_run_and_closes_its_cards(env, pass_user):
    """A waiting run whose owner lost the gate is stopped, not resumed (the
    owner is looked up when the resume does not pass one)."""
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await _Turns(_suspends()).drive(run)
        request, handle = await _open_cards(env, run)
        env.gate.discard(OWNER_EMAIL)
        turns = _Turns(_says("should not run"))
        final = await turns.drive(run, prompt=None, user=env.owner if pass_user else None)
        assert turns.inputs == []
        assert final["status"] == "stopped"
        assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "cancelled"
        closed = await action_request_store.get_action_request(env.owner["id"], request["id"])
        assert closed["status"] == "stopped"

    _run(body())


def test_unanswered_brief_conflict_keeps_the_saved_entry_and_the_users_brief(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await autoproject_store.freeze_run_context(run["id"], context="ctx", brief_version=0)
        await service.set_brief(env.owner, autoproject["id"], "User brief", expected_brief_version=0)
        turns = _Turns(
            _completes(run, brief="Run brief"),
            _says("I will not merge."),
        )
        final = await turns.drive(run)
        assert turns.inputs == ["Find three sources", AUTOPROJECT_BRIEF_CONFLICT_NUDGE]
        assert final["status"] == "completed"
        assert final["handoff_missing"] is False
        assert final["summary"] == "Found three sources"
        assert final["notes"] == "Source B is paywalled."
        assert (await _get_autoproject(autoproject))["brief"] == "User brief"

    _run(body())


# ---------------------------------------------------------------------------
# complete_autoproject_run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,cap,code", [
    ("summary", AUTOPROJECT_RUN_SUMMARY_MAX_CHARS, "summary_too_long"),
    ("result", AUTOPROJECT_RUN_RESULT_MAX_CHARS, "result_too_long"),
    ("notes", AUTOPROJECT_RUN_NOTES_MAX_CHARS, "notes_too_long"),
    ("brief", AUTOPROJECT_BRIEF_MAX_CHARS, "brief_too_long"),
])
def test_over_cap_fields_are_refused_and_the_run_stays_open(env, field, cap, code):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        at_cap = await _complete(run, **{field: "y" * cap}) if field != "brief" else None
        if at_cap is not None:
            # Exactly at the cap is fine: start a fresh run for the over-cap call.
            assert at_cap.ended is True
            run = await _start(env, autoproject)
        outcome = await _complete(run, **{field: "y" * (cap + 1)})
        assert outcome.ended is False
        body_ = json.loads(outcome.result)
        assert body_["code"] == code
        assert body_["cap"] == cap and body_["actual"] == cap + 1
        assert "still open" in body_["error"]
        current = await _get_run(run)
        assert current["status"] == "running"
        assert current["summary"] is None
        assert (await _get_autoproject(autoproject))["brief_version"] == 0

    _run(body())


@pytest.mark.parametrize("args,code", [
    ({"summary": 7}, "invalid_summary"),
    ({"result": 5}, "invalid_result"),
    ({"notes": ["x"]}, "invalid_notes"),
    ({"brief": 3}, "invalid_brief"),
    ({"expected_brief_version": -1}, "invalid_expected_brief_version"),
    ({"expected_brief_version": True}, "invalid_expected_brief_version"),
    ({"expected_brief_version": "1"}, "invalid_expected_brief_version"),
])
def test_malformed_arguments_are_refused(env, args, code):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        outcome = await _complete(run, **args)
        assert outcome.ended is False
        assert json.loads(outcome.result)["code"] == code
        assert (await _get_run(run))["status"] == "running"

    _run(body())


def test_completion_without_a_brief_leaves_the_brief_alone(env):
    async def body():
        autoproject = await _new_autoproject(env)
        await service.set_brief(env.owner, autoproject["id"], "Kept", expected_brief_version=0)
        run = await _start(env, autoproject)
        outcome = await _complete(
            run, summary="  One line  ", result="  Answer  ", notes="  Handoff  ", brief="  ",
        )
        assert outcome.ended is True
        assert json.loads(outcome.result) == {"status": "completed", "run": 1, "brief_version": 1}
        done = await _get_run(run)
        assert (done["status"], done["summary"], done["result"], done["notes"]) == (
            "completed", "One line", "Answer", "Handoff",
        )
        ap = await _get_autoproject(autoproject)
        assert ap["brief"] == "Kept" and ap["brief_version"] == 1
        assert ap["unread"] is True

    _run(body())


def test_completion_with_a_brief_writes_a_version_attributed_to_the_run(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        from chat.autoprojects.context import load_run_autoproject_context
        await load_run_autoproject_context(run)  # freezes context_brief_version 0
        outcome = await _complete(run, brief="## State\n\nThree sources found.")
        assert json.loads(outcome.result)["brief_version"] == 1
        ap = await _get_autoproject(autoproject)
        assert ap["brief"] == "## State\n\nThree sources found."
        versions = await autoproject_store.list_brief_versions(autoproject["id"])
        assert versions[0]["source"] == f"run:{run['id']}"

    _run(body())


def test_title_is_renamed_only_while_it_is_the_default(env):
    async def body():
        cut = await _new_autoproject(env, prompt="please look into the quarterly numbers")
        assert cut["title_is_default"] is True
        run = await _start(env, cut)
        outcome = await _complete(run, autoproject_title="Quarterly numbers")
        assert json.loads(outcome.result)["title_set"] is True
        renamed = await _get_autoproject(cut)
        assert renamed["title"] == "Quarterly numbers"
        assert renamed["title_is_default"] is False

        # Once named, a later run's title is ignored.
        run2 = await _start(env, cut)
        outcome = await _complete(run2, autoproject_title="Something else")
        assert "title_set" not in json.loads(outcome.result)
        assert (await _get_autoproject(cut))["title"] == "Quarterly numbers"

        # A title the user chose is never replaced.
        named = await _new_autoproject(env, title="My title")
        run3 = await _start(env, named)
        await _complete(run3, autoproject_title="Model title")
        assert (await _get_autoproject(named))["title"] == "My title"

    _run(body())


def test_brief_conflict_saves_the_entry_and_the_merge_retry_completes(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        from chat.autoprojects.context import load_run_autoproject_context
        await load_run_autoproject_context(run)  # the run starts from version 0
        await service.set_brief(env.owner, autoproject["id"], "User edit", expected_brief_version=0)

        outcome = await _complete(run, brief="Run brief")
        assert outcome.ended is False
        conflict = json.loads(outcome.result)
        assert conflict["status"] == "brief_conflict"
        assert conflict["entry_saved"] is True
        assert conflict["brief_conflict"] == {"brief": "User edit", "brief_version": 1}
        pending = await _get_run(run)
        assert pending["status"] == "running"
        assert pending["summary"] == "Found three sources"
        assert pending["brief_conflict_version"] == 1
        assert (await _get_autoproject(autoproject))["brief"] == "User edit"

        outcome = await _complete(run, brief="User edit + run brief", expected_brief_version=1)
        assert outcome.ended is True
        assert json.loads(outcome.result)["brief_version"] == 2
        assert (await _get_autoproject(autoproject))["brief"] == "User edit + run brief"
        assert (await _get_run(run))["status"] == "completed"

    _run(body())


def test_a_brief_version_the_run_was_never_handed_does_not_override(env):
    """The model cannot skip the conflict by passing the current version."""
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        from chat.autoprojects.context import load_run_autoproject_context
        await load_run_autoproject_context(run)
        await service.set_brief(env.owner, autoproject["id"], "User edit", expected_brief_version=0)

        forged = await _complete(run, brief="Overwrite", expected_brief_version=1)
        assert json.loads(forged.result)["status"] == "brief_conflict"
        assert (await _get_autoproject(autoproject))["brief"] == "User edit"

        # The handed version goes stale when the user edits again.
        await service.set_brief(env.owner, autoproject["id"], "Second edit", expected_brief_version=1)
        stale = await _complete(run, brief="Merged", expected_brief_version=1)
        again = json.loads(stale.result)
        assert again["status"] == "brief_conflict"
        assert again["brief_conflict"]["brief_version"] == 2
        assert (await _get_autoproject(autoproject))["brief"] == "Second edit"

    _run(body())


@pytest.mark.parametrize("run_row,expected,base", [
    ({"context_brief_version": 3, "brief_conflict_version": None}, None, 3),
    ({"context_brief_version": 3, "brief_conflict_version": None}, 7, 3),
    ({"context_brief_version": 3, "brief_conflict_version": 5}, 5, 5),
    ({"context_brief_version": 3, "brief_conflict_version": 5}, 6, 3),
    ({"context_brief_version": None, "brief_conflict_version": None}, None, None),
])
def test_brief_write_base_version(run_row, expected, base):
    assert runs_mod.brief_write_base_version(run_row, expected) == base


def test_completing_a_finished_run_is_refused(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        assert (await _complete(run)).ended is True
        again = await _complete(run)
        assert again.ended is False
        assert json.loads(again.result)["code"] == "autoproject_run_finished"
        # Another AutoProject's id does not reach this run.
        other = await _new_autoproject(env, title="Other")
        wrong = await runs_mod.complete_autoproject_run(
            autoproject_id=other["id"], run_id=run["id"], args=_complete_args(),
        )
        assert json.loads(wrong.result)["code"] == "autoproject_run_not_found"

    _run(body())


# ---------------------------------------------------------------------------
# Settling: every finish closes the run's open cards
# ---------------------------------------------------------------------------


async def _finish_by(env, path: str, run) -> None:
    if path == "completion":
        assert (await _complete(run)).ended is True
    elif path == "stop":
        assert await runs_mod.stop_autoproject_run(env.owner["id"], run["conversation_id"])
    elif path == "failure":
        async def boom(_messages):
            raise RuntimeError("provider down")
        with pytest.raises(RuntimeError):
            await _Turns(boom).drive(run)
    elif path == "startup_sweep":
        failed = await service.fail_interrupted_runs_at_startup()
        assert [r["id"] for r in failed] == [run["id"]]
    elif path == "handoff_missing":
        await _Turns(*[_says("done, I think")] * (1 + MAX_AUTOPROJECT_COMPLETION_NUDGES)).drive(run)
    else:  # pragma: no cover
        raise AssertionError(path)


@pytest.mark.parametrize("path,status", [
    ("completion", "completed"),
    ("stop", "stopped"),
    ("failure", "failed"),
    ("startup_sweep", "failed"),
    ("handoff_missing", "completed"),
])
def test_every_finish_closes_open_cards_and_questions(env, path, status):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        request, card_handle = await _open_cards(env, run)
        question = await tool_wait_handle_store.create_handle(
            user_id=env.owner["id"], conversation_id=run["conversation_id"],
            kind="autoproject_question", tool_id="tool-q",
            payload={"question": "Which?", "options": []},
            correlation_kind="autoproject_run", correlation_id=run["id"],
        )
        env.events.clear()
        await _finish_by(env, path, run)

        assert (await _get_run(run))["status"] == status
        for handle in (card_handle, question):
            assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "cancelled"
        closed = await action_request_store.get_action_request(env.owner["id"], request["id"])
        assert closed["status"] == "stopped"
        assert closed["result"] == {"stopped": True, "reason": RUN_FINISHED_REASON}
        assert "request_count_changed" in _event_types(env)
        assert {"autoproject_changed", "autoproject_list_changed"} <= set(_event_types(env))
        # No active run is left behind.
        assert await autoproject_store.get_active_run(autoproject["id"]) is None

    _run(body())


def test_settling_leaves_other_conversations_cards_alone(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        from chat.storage import ChatStorage
        other_cid, _ = await ChatStorage.create_conversation(env.owner["id"])
        other = await action_request_store.create_action_request(
            env.owner["id"], other_cid, "send_email", {}, "r",
        )
        await _complete(run)
        kept = await action_request_store.get_action_request(env.owner["id"], other["id"])
        assert kept["status"] == "open"

    _run(body())


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def test_stop_ends_a_waiting_run_for_its_owner_only(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await _Turns(_suspends()).drive(run)
        await service.mark_autoproject_read(
            env.owner, autoproject["id"], seq=(await _get_autoproject(autoproject))["event_seq"],
        )

        # Another user, and a conversation that is not a run: no-ops.
        assert await runs_mod.stop_autoproject_run(env.stranger["id"], run["conversation_id"]) is None
        from chat.storage import ChatStorage
        plain, _ = await ChatStorage.create_conversation(env.owner["id"])
        assert await runs_mod.stop_autoproject_run(env.owner["id"], plain) is None
        assert (await _get_run(run))["status"] == "waiting"

        stopped = await runs_mod.stop_autoproject_run(env.owner["id"], run["conversation_id"])
        assert stopped["status"] == "stopped" and stopped["finished_at"]
        # A Stop does not mark the AutoProject unread; a second Stop is a no-op.
        assert (await _get_autoproject(autoproject))["unread"] is False
        assert await runs_mod.stop_autoproject_run(env.owner["id"], run["conversation_id"]) is None

    _run(body())


# ---------------------------------------------------------------------------
# Startup sweeps
# ---------------------------------------------------------------------------


def test_restart_fails_running_runs_and_keeps_waiting_ones(env):
    async def body():
        running_ap = await _new_autoproject(env)
        waiting_ap = await _new_autoproject(env, title="Waiting")
        running = await _start(env, running_ap)
        waiting = await _start(env, waiting_ap)
        await _Turns(_suspends()).drive(waiting)
        await tool_wait_handle_store.create_handle(
            user_id=env.owner["id"], conversation_id=waiting["conversation_id"],
            kind="autoproject_question", tool_id="q-1", payload={"question": "?"},
        )

        failed = await service.fail_interrupted_runs_at_startup()
        assert [r["id"] for r in failed] == [running["id"]]
        after = await _get_run(running)
        assert after["status"] == "failed"
        assert after["error"] == service.INTERRUPTED_BY_RESTART_ERROR
        assert (await _get_autoproject(running_ap))["unread"] is True

        # A waiting run with a pending handle survives both sweeps.
        assert await service.resume_waiting_runs_at_startup(app=object()) == []
        assert (await _get_run(waiting))["status"] == "waiting"

    _run(body())


@pytest.mark.parametrize("app,kick_returns,status", [
    (object(), object(), "waiting"),   # resumed: left for the resume to drive
    (object(), None, "failed"),        # nothing to resume
    (None, object(), "failed"),        # no app to resume with
])
def test_waiting_run_answered_just_before_a_restart(env, monkeypatch, app, kick_returns, status):
    from chat.wait_handles import resume as wait_resume

    kicks: list[tuple] = []

    def _kick(app_, user, conversation_id):
        kicks.append((user["id"], conversation_id))
        return kick_returns

    monkeypatch.setattr(wait_resume, "maybe_kick_resume", _kick)

    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await _Turns(_suspends()).drive(run)
        # The question was answered (no pending handle) but its resume was lost.
        failed = await service.resume_waiting_runs_at_startup(app=app)
        after = await _get_run(run)
        assert after["status"] == status
        if app is not None:
            assert kicks == [(env.owner["id"], run["conversation_id"])]
        else:
            assert kicks == []
        if status == "failed":
            assert [r["id"] for r in failed] == [run["id"]]
            assert after["error"] == service.INTERRUPTED_BY_RESTART_ERROR
        else:
            assert failed == []

    _run(body())


# ---------------------------------------------------------------------------
# Deleting an AutoProject or a run while a run is active
# ---------------------------------------------------------------------------


def test_deleting_an_active_run_is_refused_and_a_finished_one_frees_nothing(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        with pytest.raises(autoproject_store.AutoProjectRunActiveError):
            await service.delete_run(env.owner, autoproject["id"], run["id"])
        await _complete(run)
        await service.delete_run(env.owner, autoproject["id"], run["id"])
        assert await _get_run(run) is None
        assert await _meta(env.owner, run["conversation_id"]) is None
        assert not (env.chats / run["conversation_id"]).exists()
        # The seq is never reused.
        assert (await _start(env, autoproject))["seq"] == 2

    _run(body())


def test_deleting_an_autoproject_with_a_waiting_run_closes_everything(env):
    async def body():
        autoproject = await _new_autoproject(env)
        run = await _start(env, autoproject)
        await _Turns(_suspends()).drive(run)
        request, handle = await _open_cards(env, run)
        workspace = env.autoprojects_dir / autoproject["id"] / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "notes.md").write_text("x")
        env.events.clear()

        await service.delete_autoproject(env.owner, autoproject["id"])

        assert await _get_autoproject(autoproject) is None
        assert await _get_run(run) is None
        assert await _meta(env.owner, run["conversation_id"]) is None
        assert not (env.chats / run["conversation_id"]).exists()
        assert not (env.autoprojects_dir / autoproject["id"]).exists()
        assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "cancelled"
        closed = await action_request_store.get_action_request(env.owner["id"], request["id"])
        assert closed["status"] == "stopped"
        assert _event_types(env)[-1] == "autoproject_list_changed"
        assert all(uid == env.owner["id"] for uid, _ev in env.events)

    _run(body())


def test_delete_of_a_foreign_autoproject_reads_as_missing(env):
    async def body():
        autoproject = await _new_autoproject(env)
        with pytest.raises(autoproject_store.AutoProjectNotFoundError) as foreign:
            await service.delete_autoproject(env.stranger, autoproject["id"])
        with pytest.raises(autoproject_store.AutoProjectNotFoundError) as missing:
            await service.delete_autoproject(env.stranger, "00000000-0000-4000-8000-000000000000")
        assert foreign.value.code == missing.value.code == "autoproject_not_found"
        assert await _get_autoproject(autoproject) is not None

    _run(body())
