"""AutoProject prompting: the ``## AutoProject`` section, the run's tools and skill.

- A run's system prompt carries the frozen ``## AutoProject`` block as its
  own section after every stable section and before the proxy preamble
  (which stays last), offers the two loop tools, the log reads and the
  ``chat://`` + ``autoproject://`` file tools, and drops
  ``set_conversation_name``. Its sub-agents get a read-only variant.
- Off means unchanged: every other prompt (standalone, project, routine,
  Slack, sub-agent, cross-user subagent, inference, public) is
  byte-identical whether the ``autoprojects`` gate is open or closed and
  mentions nothing AutoProject-related; the existing golden snapshots
  (tests/snapshots/system_prompt_*.txt) are pinned by
  tests/test_conversation_workspace_prompts.py.
- ``system:autoprojects`` is listed and loads only in a run with the gate
  open.
- ``run_conversation_turn`` wires a run conversation: the frozen context
  (identical across a resume after the brief changed), the run tier, no
  conversation flags, and an accepted ``complete_autoproject_run`` ends the
  turn with no further model call.

Golden snapshots of a run prompt and its sub-agent prompt live in
tests/snapshots/autoproject_prompt_*.txt; regenerate after an intended
change with ``QUEST_UPDATE_SNAPSHOTS=1 uv run pytest
tests/test_autoproject_prompting.py`` and review the diff.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.autoprojects.context import (
    build_autoproject_context,
    build_autoproject_subagent_context,
)
from chat.gemini_api.system_prompt import (
    get_inference_api_system_prompt,
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
    get_user_subagent_system_prompt,
)
from chat.system_skills import list_system_skills, load_system_skills
from tests.prompt_support import core_bullet, tool_offered

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
GATE_OPEN = {"autoprojects": True}
GATE_CLOSED = {"autoprojects": False}
READ_TOOLS = ("list_autoproject_runs", "read_autoproject_run")
SCHEME_FILE_TOOLS = ("list_files", "read_file", "write_file", "edit_file", "copy_file")
WORKSPACE_TOOLS = (
    "list_workspace_files", "get_workspace_file", "write_workspace_file", "edit_workspace_file",
)

_AUTOPROJECT = {
    "id": "ap-snapshot",
    "title": "Quarterly numbers",
    "brief": (
        "## State\n\nQ3 revenue table drafted in `autoproject://q3.csv`.\n\n"
        "## Preferences\n\nEuro amounts, no decimals."
    ),
    "brief_version": 3,
}
_RUNS = [
    {
        "seq": 4, "started_at": "2026-11-04T09:00:00+00:00", "status": "completed",
        "prompt": "Check the table against the ledger", "summary": None, "notes": "",
        "handoff_missing": True, "error": None,
    },
    {
        "seq": 3, "started_at": "2026-11-03T09:00:00+00:00", "status": "stopped",
        "prompt": "Add Q2 for comparison", "summary": None, "notes": None,
        "handoff_missing": False, "error": None,
    },
    {
        "seq": 2, "started_at": "2026-11-02T09:00:00+00:00", "status": "failed",
        "prompt": "Collect the Q3 numbers", "summary": None, "notes": None,
        "handoff_missing": False, "error": "RuntimeError: provider down",
    },
    {
        "seq": 1, "started_at": "2026-11-01T09:00:00+00:00", "status": "completed",
        "prompt": "Draft the Q3 revenue table", "summary": "Drafted the Q3 table",
        "notes": "Source: the finance export in autoproject://exports/q3.xlsx.",
        "handoff_missing": False, "error": None,
    },
]
RUN_CONTEXT = build_autoproject_context(_AUTOPROJECT, _RUNS, current_seq=5)
SUBAGENT_CONTEXT = build_autoproject_subagent_context(_AUTOPROJECT)
_BASE = dict(base_url="http://quest.test", user_name="Snap User", user_email="snap@example.com")


def _run(coro):
    return asyncio.run(coro)


def _core_registry():
    """Patch TOOL_CALL_REGISTRY down to the core tools, so plugin tools a
    previous test registered do not change the rendered prompts."""
    from chat.llm.tool_schemas import PLUGIN_TOOL_NAMES, TOOL_CALL_REGISTRY

    core = {n: s for n, s in TOOL_CALL_REGISTRY.items() if n not in PLUGIN_TOOL_NAMES}
    return patch.dict(TOOL_CALL_REGISTRY, core, clear=True)


def _run_prompt(**kwargs) -> str:
    kwargs.setdefault("connected_services", GATE_OPEN)
    return get_system_prompt("KEY", autoproject_context=RUN_CONTEXT, **_BASE, **kwargs)


def _sub_agent_prompt(**kwargs) -> str:
    kwargs.setdefault("connected_services", GATE_OPEN)
    return get_sub_agent_system_prompt(
        "Snap Agent", "KEY", autoproject_context=SUBAGENT_CONTEXT, **_BASE, **kwargs,
    )


# ---------------------------------------------------------------------------
# The run's prompt
# ---------------------------------------------------------------------------


def test_section_sits_after_the_stable_sections_and_before_the_proxy_preamble():
    prompt = _run_prompt()
    assert prompt.count("## AutoProject\n") == 1
    start = prompt.index("## AutoProject\n")
    assert prompt[start:].startswith(RUN_CONTEXT)
    # Last before the separator that introduces the proxy preamble.
    separator = prompt.rindex("\n---\n")
    assert prompt[start + len(RUN_CONTEXT):separator].strip() == ""
    assert start > prompt.index("load_skills(skill_ids=[\"system:gmail\"])")
    # The same prompt without the block is the stable prefix plus the tail.
    assert "This conversation is AutoProject run #5." in prompt


def test_run_prompt_offers_the_run_tools_and_hides_the_rest():
    prompt = _run_prompt()
    assert "You have fifteen tools available:" in prompt
    assert "14. **complete_autoproject_run(" in prompt
    assert "15. **ask_autoproject_question(" in prompt
    for name in READ_TOOLS:
        assert core_bullet(name) in prompt, name
    for name in SCHEME_FILE_TOOLS:
        assert tool_offered(prompt, name), name
    for name in WORKSPACE_TOOLS + ("set_conversation_name",):
        assert not tool_offered(prompt, name), name
    assert "proj://" not in prompt
    assert "/autoproject" in prompt and "autoproject://" in prompt
    assert "**AutoProject run:**" in prompt
    assert "`system:autoprojects`" in prompt


def test_run_prompt_with_the_gate_closed_offers_no_read_tools_or_skill():
    prompt = _run_prompt(connected_services=GATE_CLOSED)
    for name in READ_TOOLS:
        assert not tool_offered(prompt, name), name
    assert "- `system:autoprojects` --" not in prompt


def test_sub_agent_prompt_gets_the_read_only_block_and_the_read_tools():
    prompt = _sub_agent_prompt()
    assert prompt.count("## AutoProject\n") == 1
    start = prompt.index("## AutoProject\n")
    assert prompt[start:].startswith(SUBAGENT_CONTEXT)
    assert prompt[start + len(SUBAGENT_CONTEXT):prompt.rindex("\n---\n")].strip() == ""
    assert prompt.index("**Two file spaces:** You share the two file spaces") < start
    for name in READ_TOOLS:
        assert core_bullet(name) in prompt, name
    for name in ("complete_autoproject_run", "ask_autoproject_question"):
        assert f"**{name}(" not in prompt
    for name in WORKSPACE_TOOLS:
        assert not tool_offered(prompt, name), name
    # Read-only background: neither the log window nor the run number.
    assert "### Recent runs" not in prompt
    assert "AutoProject run #" not in prompt


@pytest.mark.parametrize("blank", ["", "   \n"])
def test_a_blank_context_is_no_context(blank):
    assert get_system_prompt("KEY", autoproject_context=blank, **_BASE) == get_system_prompt(
        "KEY", **_BASE,
    )
    assert get_sub_agent_system_prompt(
        "A", "KEY", autoproject_context=blank, **_BASE,
    ) == get_sub_agent_system_prompt("A", "KEY", **_BASE)


@pytest.mark.parametrize("name,render", [
    ("run", _run_prompt),
    ("sub_agent", _sub_agent_prompt),
])
def test_prompt_snapshot(name, render):
    with _core_registry():
        rendered = render()
    path = SNAPSHOT_DIR / f"autoproject_prompt_{name}.txt"
    if os.environ.get("QUEST_UPDATE_SNAPSHOTS") == "1":
        path.write_text(rendered)
    assert path.exists(), f"missing snapshot {path.name}; run with QUEST_UPDATE_SNAPSHOTS=1"
    assert rendered == path.read_text(), (
        f"AutoProject prompt '{name}' differs from {path.name}; if the change is "
        "intended, regenerate with QUEST_UPDATE_SNAPSHOTS=1 and review the diff"
    )


# ---------------------------------------------------------------------------
# Off means unchanged: every other prompt
# ---------------------------------------------------------------------------


def _other_prompts(connected_services) -> dict[str, str]:
    cs = dict(connected_services)
    return {
        "standalone": get_system_prompt("KEY", connected_services=cs, **_BASE),
        "project": get_system_prompt(
            "KEY", connected_services=cs, has_project=True, project_guide="Guide.", **_BASE,
        ),
        "routine": get_system_prompt(
            "KEY", connected_services=cs, has_project=True, is_routine=True, **_BASE,
        ),
        "slack": get_system_prompt("KEY", connected_services=cs, is_slack=True, **_BASE),
        "sub_agent": get_sub_agent_system_prompt("A", "KEY", connected_services=cs, **_BASE),
        "project_sub_agent": get_sub_agent_system_prompt(
            "A", "KEY", connected_services=cs, has_project=True, **_BASE,
        ),
        "user_subagent": get_user_subagent_system_prompt("KEY", connected_services=cs),
        "inference": get_inference_api_system_prompt("KEY", connected_services=cs),
        "public": get_public_project_system_prompt(user_name="Snap User"),
    }


def test_no_other_prompt_mentions_autoprojects_whatever_the_gate():
    with _core_registry():
        open_ = _other_prompts(GATE_OPEN)
        closed = _other_prompts(GATE_CLOSED)
        absent = _other_prompts({})
    for kind, prompt in open_.items():
        assert "autoproject" not in prompt.lower(), kind
        assert prompt == closed[kind] == absent[kind], kind


# ---------------------------------------------------------------------------
# system:autoprojects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("services,in_run,listed", [
    (GATE_OPEN, True, True),
    (GATE_CLOSED, True, False),
    (GATE_OPEN, False, False),
    (None, False, False),  # "every backend skill" still respects run scoping
])
def test_skill_is_listed_only_in_a_run_with_the_gate_open(services, in_run, listed):
    ids = [s.id for s in list_system_skills(services, False, is_autoproject_run=in_run)]
    assert ("system:autoprojects" in ids) is listed
    assert "system:autoprojects" not in [
        s.id for s in list_system_skills(services, True, is_autoproject_run=False)
    ]


def test_skill_loads_only_in_a_run_with_the_gate_open():
    from chat.autoprojects.constants import autoprojects_disabled_message

    def _load(services, in_run):
        return load_system_skills(
            ["system:autoprojects"], services, "http://quest.test", "KEY",
            is_autoproject_run=in_run,
        )[0]

    loaded = _load(GATE_OPEN, True)
    assert "error" not in loaded
    assert "brief" in loaded["content"] and "autoproject://" in loaded["content"]
    assert _load(GATE_CLOSED, True)["error"] == autoprojects_disabled_message()
    assert "only available in an AutoProject run" in _load(GATE_OPEN, False)["error"]


# ---------------------------------------------------------------------------
# run_conversation_turn wiring (isolated database, scripted provider)
# ---------------------------------------------------------------------------


@pytest.fixture()
def turn(isolated_db, tmp_path, monkeypatch):
    import chat.storage as storage_mod
    import config.feature_gates as feature_gates
    import config.model_selection as model_selection
    from chat.gemini_api import conversation as conv_mod
    from chat.realtime.bus import bus
    from db import conversation_store, user_store

    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats")
    monkeypatch.setattr(storage_mod, "AUTOPROJECTS_DIR", tmp_path / "autoprojects")
    monkeypatch.setattr(feature_gates, "autoprojects_enabled_for", lambda _email: True)
    monkeypatch.setattr(model_selection, "is_model_allowed", lambda *a, **kw: True)
    monkeypatch.setattr(bus, "publish_to_user", lambda uid, ev: None)
    monkeypatch.setattr(bus, "publish_to_conversation", lambda cid, ev: None)

    monkeypatch.setattr(conv_mod, "load_server_config", lambda: {"gemini": {"model": "fake-model"}})
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())
    monkeypatch.setattr(conversation_store, "update_conversation_model", AsyncMock())
    import db.skill_store as skill_store
    monkeypatch.setattr(skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]))
    import api.instructions as instructions
    monkeypatch.setattr(instructions, "get_user_connected_services", lambda _u: dict(GATE_OPEN))
    monkeypatch.setattr(
        conv_mod.ChatStorage, "set_system_prompt", staticmethod(lambda *a, **kw: None),
    )

    prompts: list[dict] = []
    sessions: list[dict] = []

    def _capture_prompt(*_a, **kw):
        prompts.append(kw)
        return "system prompt"

    def _capture_session(*_a, **kw):
        sessions.append(kw)
        return object()

    monkeypatch.setattr(conv_mod, "get_system_prompt", _capture_prompt)
    monkeypatch.setattr(conv_mod, "get_or_create_chat", _capture_session)

    owner = _run(user_store.create_user("owner@example.com", "Owner", "key-owner"))

    def _provider(turns):
        from chat.llm.base import UsageStats

        provider = MagicMock()
        provider.get_pending_tool_use_args = MagicMock(return_value=[])
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

    async def _start():
        from chat.autoprojects import service

        autoproject = await service.create_autoproject(owner, title="Wiring")
        cid = await service.create_run_conversation(owner, autoproject["id"])
        meta = await conversation_store.get_conversation_meta(owner["id"], cid)
        run = (await service.start_run(owner, meta, prompt="Go", model="fake-model")).run
        return autoproject, run

    async def _turn(run, provider, *, message="Go", flags=None):
        monkeypatch.setattr(conv_mod, "get_provider_instance", lambda _n, _i=None: provider)
        messages_out: list[dict] = []

        async def _on_event(_event):
            return None

        await conv_mod.run_conversation_turn(
            app=None, user=dict(owner, api_key="k"), message=message,
            conversation_id=run["conversation_id"], timezone="UTC", model="fake-model",
            on_event=_on_event, messages_out=messages_out, flags=flags,
            autoproject_id=run["autoproject_id"],
        )
        return messages_out

    return SimpleNamespace(
        owner=owner, prompts=prompts, sessions=sessions, provider=_provider,
        start=_start, turn=_turn,
    )


def test_run_turn_builds_the_run_prompt_and_tier(turn):
    from chat.llm.base import StreamEvent
    from chat.llm.tool_schemas import AUTOPROJECT_RUN_TOOLS

    async def body():
        _autoproject, run = await turn.start()
        reply = StreamEvent(type="text", text="Working.")
        await turn.turn(run, turn.provider([[reply]]), flags=["nested_subagents"])
        kwargs = turn.prompts[-1]
        assert kwargs["autoproject_context"].startswith("## AutoProject\n")
        assert "This conversation is AutoProject run #1." in kwargs["autoproject_context"]
        assert kwargs.get("has_project") in (False, None)
        assert kwargs["nested_subagents"] is False
        assert turn.sessions[-1]["tools"] is AUTOPROJECT_RUN_TOOLS

    _run(body())


def test_a_resume_keeps_the_context_the_run_started_with(turn):
    """The run's context is frozen on its first turn: a resume after the
    user edited the brief builds the prompt from the same block."""
    from chat.autoprojects import service
    from chat.llm.base import StreamEvent

    async def body():
        autoproject, run = await turn.start()
        reply = StreamEvent(type="text", text="ok")
        await turn.turn(run, turn.provider([[reply]]))
        await service.set_brief(
            turn.owner, autoproject["id"], "Edited mid-run", expected_brief_version=0,
        )
        await turn.turn(run, turn.provider([[reply]]), message="")
        first = turn.prompts[-2]["autoproject_context"]
        resumed = turn.prompts[-1]["autoproject_context"]
        assert first == resumed
        assert "Edited mid-run" not in resumed
        # A later run starts from the edited brief.
        from db import autoproject_store, conversation_store
        await autoproject_store.complete_run(run["id"], summary="s", result="r", notes="")
        cid = await service.create_run_conversation(turn.owner, autoproject["id"])
        meta = await conversation_store.get_conversation_meta(turn.owner["id"], cid)
        later = (await service.start_run(turn.owner, meta, prompt="Next", model="fake-model")).run
        await turn.turn(later, turn.provider([[reply]]), message="Next")
        assert "Edited mid-run" in turn.prompts[-1]["autoproject_context"]

    _run(body())


def test_accepted_completion_ends_the_turn_without_another_model_call(turn):
    from chat.llm.base import StreamEvent
    from db import autoproject_store

    async def body():
        _autoproject, run = await turn.start()
        provider = turn.provider([[
            StreamEvent(
                type="tool_call", tool_name="complete_autoproject_run", tool_id="done-1",
                tool_args={"summary": "Done", "result": "The answer.", "notes": "None."},
            ),
            # Never dispatched: the loop ends at the accepted completion.
            StreamEvent(
                type="tool_call", tool_name="ask_autoproject_question", tool_id="late-1",
                tool_args={"question": "Anything else?"},
            ),
        ], [StreamEvent(type="text", text="must not be requested")]])
        messages = await turn.turn(run, provider)
        assert provider.stream_calls["n"] == 1
        assert provider.format_tool_results.call_count == 0
        results = [m for m in messages if m.get("type") == "tool_result"]
        assert [m["tool_id"] for m in results] == ["done-1"]
        assert json.loads(results[0]["tool_output"])["status"] == "completed"
        assert any(m.get("type") == "stats" for m in messages)
        stored = await autoproject_store.get_run(run["id"])
        assert (stored["status"], stored["result"]) == ("completed", "The answer.")
        from db import tool_wait_handle_store
        assert await tool_wait_handle_store.list_pending_for_conversation(
            turn.owner["id"], run["conversation_id"],
        ) == []

    _run(body())


def test_sub_agents_of_a_run_get_the_current_brief_and_the_run_ids(turn, monkeypatch):
    """A spawn inside a run hands its sub-agents the read-only block (loaded
    fresh, so the brief as it is now) and the run's ids; outside a run
    nothing; a spawn after the AutoProject was deleted is a tool error."""
    from chat.autoprojects import service
    from chat.gemini_api import turn_tools
    from chat.gemini_api.run_context import RunContext
    from chat.gemini_api.usage import UsageAccumulator

    seen: list[dict] = []

    async def _fake_run_sub_agent(**kwargs):
        seen.append(kwargs)
        return "done"

    monkeypatch.setattr(turn_tools, "_run_sub_agent", _fake_run_sub_agent)

    def _ctx(run):
        async def _on_event(_event):
            return None

        return RunContext(
            app=None, user=dict(turn.owner, api_key="k", settings={}),
            conversation_id=run["conversation_id"] if run else "c-plain", timezone="UTC",
            model="fake-model", origin="web", project_id=None, routine_id=None,
            slack_context=None, provider=MagicMock(), provider_name="gemini", chat=object(),
            is_slack_origin=False, is_user_subagent=False, is_inference_api=False,
            is_public=False, nested_subagents=False, user_subagents_enabled=False,
            user_subagents_gate_open=False, subagent_run=None, subagent_caller=None,
            resolved_project_guide="", resolved_skills_content="", on_event=_on_event,
            structured_messages=[], usage_acc=UsageAccumulator(),
            autoproject_id=run["autoproject_id"] if run else None,
            autoproject_run_id=run["id"] if run else None,
        )

    def _spawn(ctx):
        call = turn_tools.ToolCall(
            name="agent_task", key="agent_task", tool_id="tid", raw_tool_id="tid",
            args={"name": "A", "prompt": "look", "description": "d"},
        )
        return turn_tools._handle_agent_task(ctx, call, turn_tools.TurnState())

    async def body():
        autoproject, run = await turn.start()
        await service.set_brief(
            turn.owner, autoproject["id"], "Brief now", expected_brief_version=0,
        )
        await _spawn(_ctx(run))
        kwargs = seen[-1]
        assert kwargs["autoproject_context"].startswith("## AutoProject\n")
        assert "Brief now" in kwargs["autoproject_context"]
        assert (kwargs["autoproject_id"], kwargs["autoproject_run_id"]) == (
            autoproject["id"], run["id"],
        )

        await _spawn(_ctx(None))
        assert seen[-1]["autoproject_context"] == ""
        assert seen[-1]["autoproject_id"] is None

        calls = len(seen)
        await service.delete_autoproject(turn.owner, autoproject["id"])
        result = await _spawn(_ctx(run))
        assert len(seen) == calls
        assert "no longer exists" in result

    _run(body())
