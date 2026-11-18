"""AutoProject questions: ``ask_autoproject_question`` and answering it.

A run that needs the user calls ``ask_autoproject_question``; the loop arm
opens a pending ``autoproject_question`` wait handle (the handle is the
question: no table of its own), records a durable transcript message and
suspends the run. The user answers on the AutoProject page
(``service.answer_question``), which validates the answer, resolves the
handle, rewrites the transcript message and kicks the resume; the resume
bucket closes the dangling tool_use with ``{"answer": ...}`` (or
``{"status": "cancelled"}`` after a Stop). The generic wait-handle resolve
route refuses this kind so none of that can be skipped.

Runs against an isolated database; the model is a scripted fake provider.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat.autoprojects import questions, service
from chat.autoprojects.constants import (
    AUTOPROJECT_ANSWER_MAX_CHARS,
    AUTOPROJECT_QUESTION_MAX_CHARS,
    AUTOPROJECT_QUESTION_MAX_OPTIONS,
    AUTOPROJECT_QUESTION_OPTION_MAX_CHARS,
)
from chat.autoprojects.runs import stop_autoproject_run
from chat.llm.base import StreamEvent, UsageStats
from db import autoproject_store, conversation_store, tool_wait_handle_store, user_store
from db.autoproject_store import AutoProjectStoreError, AutoProjectValidationError

OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Validation (pure)
# ---------------------------------------------------------------------------


def test_question_is_stripped_and_options_deduplicated():
    question, options = questions.validate_question(
        "  Which region?  ", [" EU ", "US", "EU"],
    )
    assert question == "Which region?"
    assert options == ["EU", "US"]
    assert questions.validate_question("Go on?", None) == ("Go on?", [])
    # Duplicates do not count against the cap.
    many = [f"o{i}" for i in range(AUTOPROJECT_QUESTION_MAX_OPTIONS)]
    assert questions.validate_question("q", many + many)[1] == many


@pytest.mark.parametrize("question,options,code", [
    (None, None, "invalid_question"),
    ("   ", None, "invalid_question"),
    (5, None, "invalid_question"),
    ("q" * (AUTOPROJECT_QUESTION_MAX_CHARS + 1), None, "question_too_long"),
    ("q", "EU", "invalid_options"),
    ("q", ["EU", ""], "invalid_options"),
    ("q", ["EU", 3], "invalid_options"),
    ("q", ["o" * (AUTOPROJECT_QUESTION_OPTION_MAX_CHARS + 1)], "option_too_long"),
    ("q", [f"o{i}" for i in range(AUTOPROJECT_QUESTION_MAX_OPTIONS + 1)], "too_many_options"),
])
def test_question_refusals(question, options, code):
    with pytest.raises(AutoProjectValidationError) as exc:
        questions.validate_question(question, options)
    assert exc.value.code == code
    assert exc.value.message


def test_question_at_its_caps_is_accepted():
    questions.validate_question(
        "q" * AUTOPROJECT_QUESTION_MAX_CHARS,
        ["o" * AUTOPROJECT_QUESTION_OPTION_MAX_CHARS],
    )


def test_answer_validation():
    assert questions.validate_answer("  EU  ") == "EU"
    at_cap = "a" * AUTOPROJECT_ANSWER_MAX_CHARS
    assert questions.validate_answer(at_cap) == at_cap
    for bad in (None, 3, "", "   "):
        with pytest.raises(AutoProjectValidationError) as exc:
            questions.validate_answer(bad)
        assert exc.value.code == "invalid_answer"
    with pytest.raises(AutoProjectValidationError) as exc:
        questions.validate_answer("a" * (AUTOPROJECT_ANSWER_MAX_CHARS + 1))
    assert exc.value.code == "invalid_answer"
    assert exc.value.details == {
        "cap": AUTOPROJECT_ANSWER_MAX_CHARS, "actual": AUTOPROJECT_ANSWER_MAX_CHARS + 1,
    }


@pytest.mark.parametrize("status,shown,answer", [
    ("pending", "pending", None),
    ("accepted", "answered", "EU"),
    ("cancelled", "cancelled", None),
    ("rejected", "cancelled", None),
    ("timed_out", "cancelled", None),
])
def test_question_view_status(status, shown, answer):
    view = questions.question_view({
        "id": "h1", "status": status, "payload": {"question": "Which?", "options": ["EU"]},
        "response": {"answer": "EU"}, "created_at": "t0", "resolved_at": "t1",
    })
    assert view["status"] == shown
    assert view["answer"] == answer
    assert view["answered_at"] == ("t1" if shown == "answered" else None)
    assert (view["question"], view["options"]) == ("Which?", ["EU"])


# ---------------------------------------------------------------------------
# Fixture and helpers (isolated database)
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(isolated_db, tmp_path, monkeypatch):
    import chat.storage as storage_mod
    import config.feature_gates as feature_gates
    import config.model_selection as model_selection
    from chat.realtime.bus import bus
    from chat.wait_handles import resume as wait_resume

    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats")
    monkeypatch.setattr(storage_mod, "AUTOPROJECTS_DIR", tmp_path / "autoprojects")
    gate = {OWNER_EMAIL, STRANGER_EMAIL}
    monkeypatch.setattr(feature_gates, "autoprojects_enabled_for", lambda email: email in gate)
    monkeypatch.setattr(model_selection, "is_model_allowed", lambda *a, **kw: True)
    events: list[tuple[int, dict]] = []
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: events.append((uid, ev)))
    monkeypatch.setattr(bus, "publish_to_conversation", lambda cid, ev: None)
    kicks: list[tuple] = []
    monkeypatch.setattr(
        wait_resume, "maybe_kick_resume",
        lambda app, user, cid: kicks.append((app, user["id"], cid)) or object(),
    )

    async def _users():
        owner = await user_store.create_user(OWNER_EMAIL, "Owner", "key-owner")
        stranger = await user_store.create_user(STRANGER_EMAIL, "Stranger", "key-stranger")
        return owner, stranger

    owner, stranger = _run(_users())
    return SimpleNamespace(
        owner=owner, stranger=stranger, gate=gate, events=events, kicks=kicks,
        app=object(),
    )


async def _waiting_run(env, user=None, *, title="Research") -> tuple[dict, dict]:
    """An AutoProject with a started run (``running``)."""
    user = user or env.owner
    autoproject = await service.create_autoproject(user, title=title)
    cid = await service.create_run_conversation(user, autoproject["id"])
    meta = await conversation_store.get_conversation_meta(user["id"], cid)
    started = await service.start_run(user, meta, prompt="Plan the trip", model="fake-model")
    return autoproject, started.run


async def _ask(env, run, *, user=None, question="Which region?", options=("EU", "US")) -> dict:
    """A posted question with its transcript message, as the arm leaves it."""
    from chat.storage import ChatStorage

    user = user or env.owner
    handle = await questions.post_question(
        user_id=user["id"], conversation_id=run["conversation_id"], run_id=run["id"],
        tool_id="tool-q", question=question, options=list(options),
    )
    await ChatStorage.append_structured_messages(
        run["conversation_id"], [{**questions.make_question_message(handle), "timestamp": "t"}],
    )
    return handle


def _transcript_question(run, handle_id) -> dict:
    from chat.storage import ChatStorage

    data = ChatStorage.get_conversation(run["conversation_id"])
    return next(
        m for m in data["messages"]
        if m.get("type") == "autoproject_question" and m.get("wait_handle_id") == handle_id
    )


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def test_answer_resolves_rewrites_publishes_and_kicks_the_resume(env):
    async def body():
        autoproject, run = await _waiting_run(env)
        handle = await _ask(env, run)
        before = await autoproject_store.get_autoproject_by_id(autoproject["id"])
        env.events.clear()

        view = await service.answer_question(
            env.owner, autoproject["id"], handle["id"], "  EU  ", app=env.app,
        )
        assert view["status"] == "answered" and view["answer"] == "EU"
        stored = await tool_wait_handle_store.get_handle(handle["id"])
        assert stored["status"] == "accepted"
        assert stored["response"] == {"answer": "EU"}
        correlation = (stored["correlation_kind"], stored["correlation_id"])
        assert correlation == ("autoproject_run", run["id"])
        message = _transcript_question(run, handle["id"])
        assert (message["status"], message["answer"]) == ("answered", "EU")
        assert env.kicks == [(env.app, env.owner["id"], run["conversation_id"])]

        types = [ev["type"] for _uid, ev in env.events]
        assert types == ["wait_handle_resolved", "autoproject_changed"]
        assert all(uid == env.owner["id"] for uid, _ev in env.events)
        changed = env.events[1][1]
        assert changed["event_seq"] > before["event_seq"]
        # The run stays waiting/running until its resume starts.
        assert changed["active_run"]["id"] == run["id"]

    _run(body())


def test_answer_at_the_cap_and_over_it(env):
    async def body():
        autoproject, run = await _waiting_run(env)
        handle = await _ask(env, run)
        with pytest.raises(AutoProjectValidationError) as exc:
            await service.answer_question(
                env.owner, autoproject["id"], handle["id"],
                "a" * (AUTOPROJECT_ANSWER_MAX_CHARS + 1), app=env.app,
            )
        assert exc.value.code == "invalid_answer"
        assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "pending"
        assert env.kicks == []
        view = await service.answer_question(
            env.owner, autoproject["id"], handle["id"],
            "a" * AUTOPROJECT_ANSWER_MAX_CHARS, app=env.app,
        )
        assert view["status"] == "answered"

    _run(body())


def test_answering_twice_is_a_conflict_with_the_first_answer(env):
    async def body():
        autoproject, run = await _waiting_run(env)
        handle = await _ask(env, run)
        await service.answer_question(env.owner, autoproject["id"], handle["id"], "EU", app=env.app)
        with pytest.raises(AutoProjectStoreError) as exc:
            await service.answer_question(
                env.owner, autoproject["id"], handle["id"], "US", app=env.app,
            )
        assert exc.value.code == "question_resolved"
        assert exc.value.details == {"status": "answered", "answer": "EU"}
        assert len(env.kicks) == 1

    _run(body())


@pytest.mark.parametrize("case,code", [
    ("missing_handle", "question_not_found"),
    ("other_kind", "question_not_found"),
    ("other_autoproject", "question_not_found"),
    ("stranger_own_autoproject", "question_not_found"),
    ("stranger_foreign_autoproject", "autoproject_not_found"),
    ("gate_closed", "autoprojects_disabled"),
    ("cancelled", "question_resolved"),
    ("run_finished", "question_resolved"),
])
def test_answer_refusals(env, case, code):
    async def body():
        autoproject, run = await _waiting_run(env)
        handle = await _ask(env, run)
        user, autoproject_id, handle_id = env.owner, autoproject["id"], handle["id"]
        if case == "missing_handle":
            handle_id = "00000000-0000-4000-8000-000000000000"
        elif case == "other_kind":
            other = await tool_wait_handle_store.create_handle(
                user_id=env.owner["id"], conversation_id=run["conversation_id"],
                kind="action_request", tool_id="t-card", payload={},
            )
            handle_id = other["id"]
        elif case == "other_autoproject":
            other_ap = await service.create_autoproject(env.owner, title="Other")
            autoproject_id = other_ap["id"]
        elif case == "stranger_own_autoproject":
            stranger_ap = await service.create_autoproject(env.stranger, title="Mine")
            user, autoproject_id = env.stranger, stranger_ap["id"]
        elif case == "stranger_foreign_autoproject":
            user = env.stranger
        elif case == "gate_closed":
            env.gate.discard(OWNER_EMAIL)
        elif case == "cancelled":
            await stop_autoproject_run(env.owner["id"], run["conversation_id"])
        else:
            # A pending handle left on a run that finished some other way.
            await autoproject_store.complete_run(run["id"], summary="s", result="r", notes="")
        with pytest.raises(AutoProjectStoreError) as exc:
            await service.answer_question(user, autoproject_id, handle_id, "EU", app=env.app)
        assert exc.value.code == code
        if case in ("cancelled", "run_finished"):
            assert exc.value.details["status"] == "cancelled"
        assert env.kicks == []
        if case not in ("cancelled",):
            assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "pending"

    _run(body())


def test_stop_cancels_the_question_everywhere(env):
    async def body():
        autoproject, run = await _waiting_run(env)
        handle = await _ask(env, run)
        await stop_autoproject_run(env.owner["id"], run["conversation_id"])
        assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "cancelled"
        assert _transcript_question(run, handle["id"])["status"] == "cancelled"
        listed = await questions.list_run_questions([await autoproject_store.get_run(run["id"])])
        assert [q["status"] for q in listed[run["id"]]] == ["cancelled"]

    _run(body())


def test_completion_cancels_an_open_question(env):
    async def body():
        from chat.autoprojects.runs import complete_autoproject_run

        autoproject, run = await _waiting_run(env)
        handle = await _ask(env, run)
        outcome = await complete_autoproject_run(
            autoproject_id=autoproject["id"], run_id=run["id"],
            args={"summary": "s", "result": "r", "notes": "n"},
        )
        assert outcome.ended is True
        assert (await tool_wait_handle_store.get_handle(handle["id"]))["status"] == "cancelled"
        assert _transcript_question(run, handle["id"])["status"] == "cancelled"

    _run(body())


def test_questions_are_grouped_under_their_own_run(env):
    async def body():
        _ap1, run1 = await _waiting_run(env)
        _ap2, run2 = await _waiting_run(env, title="Two")
        h1 = await _ask(env, run1, question="First?")
        await _ask(env, run2, question="Second?")
        # A handle correlated to another run still lands under its own
        # conversation's run.
        h3 = await tool_wait_handle_store.create_handle(
            user_id=env.owner["id"], conversation_id=run1["conversation_id"],
            kind="autoproject_question", tool_id="t3", payload={"question": "Third?"},
            correlation_kind="autoproject_run", correlation_id=run2["id"],
        )
        listed = await questions.list_run_questions([run1, run2])
        assert [q["handle_id"] for q in listed[run1["id"]]] == [h1["id"], h3["id"]]
        assert [q["question"] for q in listed[run2["id"]]] == ["Second?"]

    _run(body())


# ---------------------------------------------------------------------------
# The generic resolve route refuses questions
# ---------------------------------------------------------------------------


def test_generic_wait_handle_resolve_refuses_a_question(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import chat.wait_handle_routes as routes_mod

    autoproject, run = _run(_waiting_run(env))
    handle = _run(_ask(env, run))

    app = FastAPI()
    app.include_router(routes_mod.router)

    async def _current():
        return env.owner

    app.dependency_overrides[routes_mod.get_current_user_cookie_or_apikey_checked] = _current
    resp = TestClient(app).post(
        f"/app/api/wait-handles/{handle['id']}/resolve",
        json={"status": "accepted", "response": {"answer": "sneaky"}},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "use_autoproject_answer_route"
    stored = _run(tool_wait_handle_store.get_handle(handle["id"]))
    assert stored["status"] == "pending" and stored["response"] in (None, {})
    assert env.kicks == []


# ---------------------------------------------------------------------------
# The loop arm
# ---------------------------------------------------------------------------


def _ctx(env, run, **overrides):
    from chat.gemini_api.run_context import RunContext
    from chat.gemini_api.usage import UsageAccumulator

    emitted: list[dict] = []

    async def _on_event(event):
        emitted.append(event)

    fields = dict(
        app=None, user=dict(env.owner), conversation_id=run["conversation_id"],
        timezone="UTC", model="fake-model", origin="autoproject", project_id=None,
        routine_id=None, slack_context=None, provider=None, provider_name="gemini",
        chat=None, is_slack_origin=False, is_user_subagent=False,
        is_inference_api=False, is_public=False, nested_subagents=False,
        user_subagents_enabled=False, user_subagents_gate_open=False,
        subagent_run=None, subagent_caller=None, resolved_project_guide="",
        resolved_skills_content="", on_event=_on_event, structured_messages=[],
        usage_acc=UsageAccumulator(),
        autoproject_id=run["autoproject_id"], autoproject_run_id=run["id"],
    )
    fields.update(overrides)
    ctx = RunContext(**fields)
    return ctx, emitted


def _ask_call(args, *, via_tool_call=False):
    from chat.gemini_api.turn_tools import ASK_AUTOPROJECT_QUESTION_VIA_TOOL_CALL_KEY, ToolCall

    if via_tool_call:
        return ToolCall(
            name="tool_call", key=ASK_AUTOPROJECT_QUESTION_VIA_TOOL_CALL_KEY,
            tool_id="tid", raw_tool_id="raw-ask",
            args={"tool_name": "ask_autoproject_question", "arguments": args},
        )
    return ToolCall(
        name="ask_autoproject_question", key="ask_autoproject_question",
        tool_id="tid", raw_tool_id="raw-ask", args=args,
    )


@pytest.mark.parametrize("via_tool_call", [False, True])
def test_arm_posts_the_question_and_suspends(env, via_tool_call):
    from chat.gemini_api.turn_tools import (
        SuspendForAutoProjectQuestion,
        TurnState,
        _handle_ask_autoproject_question,
    )

    async def body():
        _autoproject, run = await _waiting_run(env)
        ctx, emitted = _ctx(env, run)
        with pytest.raises(SuspendForAutoProjectQuestion) as exc:
            await _handle_ask_autoproject_question(
                ctx, _ask_call({"question": " Which region? ", "options": ["EU", "US"]},
                               via_tool_call=via_tool_call),
                TurnState(),
            )
        assert exc.value.tool_id == "raw-ask" and exc.value.run_id == run["id"]
        handle = await tool_wait_handle_store.get_handle(exc.value.handle_id)
        assert handle["kind"] == "autoproject_question"
        assert handle["status"] == "pending"
        assert handle["tool_id"] == "raw-ask"
        assert handle["payload"] == {"question": "Which region?", "options": ["EU", "US"]}
        correlation = (handle["correlation_kind"], handle["correlation_id"])
        assert correlation == ("autoproject_run", run["id"])
        assert handle["expires_at"] is None
        assert emitted == [{
            "type": "autoproject_question", "wait_handle_id": handle["id"],
            "status": "pending", "question": "Which region?", "options": ["EU", "US"],
        }]
        assert ctx.structured_messages[0]["type"] == "autoproject_question"

    _run(body())


def test_arm_refusals_are_tool_results(env):
    from chat.gemini_api.turn_tools import TurnState, _handle_ask_autoproject_question

    async def body():
        _autoproject, run = await _waiting_run(env)
        # Outside an AutoProject run.
        ctx, _ = _ctx(env, run, autoproject_id=None, autoproject_run_id=None)
        out = json.loads(await _handle_ask_autoproject_question(
            ctx, _ask_call({"question": "q"}), TurnState(),
        ))
        assert "only available inside AutoProject runs" in out["error"]
        # A malformed call.
        ctx, emitted = _ctx(env, run)
        out = json.loads(await _handle_ask_autoproject_question(
            ctx, _ask_call({"question": "q", "options": [""]}), TurnState(),
        ))
        assert out["code"] == "invalid_options"
        # A run that already finished.
        await autoproject_store.complete_run(run["id"], summary="s", result="r", notes="")
        out = json.loads(await _handle_ask_autoproject_question(
            ctx, _ask_call({"question": "q"}), TurnState(),
        ))
        assert out["code"] == "autoproject_run_finished"
        assert emitted == []
        assert await tool_wait_handle_store.list_pending_for_conversation(
            env.owner["id"], run["conversation_id"],
        ) == []

    _run(body())


# ---------------------------------------------------------------------------
# Through the loop: suspend, then resume with the answer
# ---------------------------------------------------------------------------


@pytest.fixture()
def loop(env, monkeypatch):
    """run_conversation_turn with a scripted provider; the AutoProject run row,
    its context and the wait handles come from the isolated database."""
    from chat.gemini_api import conversation as conv_mod

    monkeypatch.setattr(conv_mod, "load_server_config", lambda: {"gemini": {"model": "fake-model"}})
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_system_prompt", lambda *a, **kw: "system prompt")
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
    monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: object())
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())
    monkeypatch.setattr(conversation_store, "update_conversation_model", AsyncMock())
    import db.skill_store as skill_store
    monkeypatch.setattr(skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]))
    import api.instructions as instructions
    monkeypatch.setattr(
        instructions, "get_user_connected_services", lambda _u: {"autoprojects": True},
    )
    monkeypatch.setattr(
        conv_mod.ChatStorage, "set_system_prompt", staticmethod(lambda *a, **kw: None),
    )

    def _provider(turns, pending=None):
        provider = MagicMock()
        provider.get_pending_tool_use_args = MagicMock(return_value=pending or [])
        provider.repair_session_history = MagicMock(return_value=0)
        provider.get_usage = MagicMock(
            return_value=UsageStats(input_tokens=1, output_tokens=1, cached_tokens=0),
        )
        provider.format_tool_results = MagicMock(
            side_effect=lambda _chat, results: {"tool_results": list(results)},
        )
        calls = {"n": 0}

        async def _stream(_chat, _message):
            i = calls["n"]
            calls["n"] += 1
            for ev in turns[i] if i < len(turns) else []:
                yield ev

        provider.send_message_stream = _stream
        provider.stream_calls = calls
        return provider

    async def _turn(run, provider, *, message):
        monkeypatch.setattr(conv_mod, "get_provider_instance", lambda _n, _i=None: provider)
        messages_out: list[dict] = []

        async def _on_event(_event):
            return None

        await conv_mod.run_conversation_turn(
            app=None, user=dict(env.owner, api_key="k"), message=message,
            conversation_id=run["conversation_id"], timezone="UTC", model="fake-model",
            on_event=_on_event, messages_out=messages_out,
            autoproject_id=run["autoproject_id"],
        )
        return messages_out

    return SimpleNamespace(provider=_provider, turn=_turn)


def test_question_suspends_the_turn_and_the_answer_resumes_it(env, loop):
    async def body():
        autoproject, run = await _waiting_run(env)
        ask = StreamEvent(
            type="tool_call", tool_name="ask_autoproject_question", tool_id="ask-1",
            tool_args={"question": "Which region?", "options": ["EU", "US"]},
        )
        first = loop.provider([[ask]])
        messages = await loop.turn(run, first, message="Plan the trip")
        assert first.stream_calls["n"] == 1
        assert first.format_tool_results.call_count == 0
        shown = ("tool_use", "tool_result", "autoproject_question")
        assert [m["type"] for m in messages if m.get("type") in shown] == [
            "tool_use", "autoproject_question",
        ]
        handle = (await tool_wait_handle_store.list_pending_for_conversation(
            env.owner["id"], run["conversation_id"],
        ))[0]
        assert handle["kind"] == "autoproject_question" and handle["tool_id"] == "ask-1"

        await service.answer_question(env.owner, autoproject["id"], handle["id"], "EU", app=None)

        resumed = loop.provider(
            [[SimpleNamespace(type="text", text="Going with EU.")]],
            pending=[("ask-1", "ask_autoproject_question", {})],
        )
        await loop.turn(run, resumed, message="")
        (_chat, results), _kw = resumed.format_tool_results.call_args
        assert [(r["tool_id"], json.loads(r["result"])) for r in results] == [
            ("ask-1", {"answer": "EU"}),
        ]

    _run(body())


@pytest.mark.parametrize("outcome,expected", [
    ("cancelled", {"status": "cancelled"}),
    ("pending", {"status": "still_waiting"}),
])
def test_resume_bucket_closes_a_cancelled_or_open_question(env, loop, outcome, expected):
    async def body():
        _autoproject, run = await _waiting_run(env)
        handle = await questions.post_question(
            user_id=env.owner["id"], conversation_id=run["conversation_id"],
            run_id=run["id"], tool_id="ask-1", question="Which?", options=[],
        )
        if outcome == "cancelled":
            await tool_wait_handle_store.cancel_pending_for_conversation(
                env.owner["id"], run["conversation_id"],
            )
        provider = loop.provider(
            [[SimpleNamespace(type="text", text="ok")]],
            pending=[("ask-1", "tool_call", {"tool_name": "ask_autoproject_question"})],
        )
        await loop.turn(run, provider, message="")
        (_chat, results), _kw = provider.format_tool_results.call_args
        got = json.loads(results[0]["result"])
        assert {k: got[k] for k in expected} == expected
        status = (await tool_wait_handle_store.get_handle(handle["id"]))["status"]
        assert status == ("cancelled" if outcome == "cancelled" else "pending")

    _run(body())


# ---------------------------------------------------------------------------
# Resume gating
# ---------------------------------------------------------------------------


def test_dangling_question_counts_as_a_wait_in_both_spellings(monkeypatch):
    from chat.wait_handles import resume as resume_mod

    for pending in (
        [("t1", "ask_autoproject_question", {})],
        [("t1", "tool_call", {"tool_name": "ask_autoproject_question"})],
    ):
        monkeypatch.setattr(resume_mod, "_pending_tool_uses", lambda _cid, p=pending: p)
        assert resume_mod._conversation_has_dangling_wait("c1") is True


def test_question_and_approval_card_both_hold_the_resume(env, monkeypatch):
    from chat.wait_handles import resume as resume_mod

    async def body():
        autoproject, run = await _waiting_run(env)
        question = await questions.post_question(
            user_id=env.owner["id"], conversation_id=run["conversation_id"],
            run_id=run["id"], tool_id="t-q", question="Which?", options=[],
        )
        card = await tool_wait_handle_store.create_handle(
            user_id=env.owner["id"], conversation_id=run["conversation_id"],
            kind="action_request", tool_id="t-card", payload={},
        )
        monkeypatch.setattr(resume_mod, "_pending_tool_uses", lambda _cid: [
            ("t-q", "ask_autoproject_question", {}),
            ("t-card", "create_action_request", {}),
        ])
        blocking = await resume_mod._unresolved_blocking_handle_ids(
            env.owner["id"], run["conversation_id"],
        )
        assert sorted(blocking) == sorted([question["id"], card["id"]])

        await service.answer_question(env.owner, autoproject["id"], question["id"], "EU", app=None)
        assert await resume_mod._unresolved_blocking_handle_ids(
            env.owner["id"], run["conversation_id"],
        ) == [card["id"]]

    _run(body())
