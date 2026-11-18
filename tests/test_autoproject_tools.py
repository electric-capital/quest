"""AutoProject run tools: rosters, the run-scoped loop tools and the log reads.

- Rosters: an AutoProject run's session gets ``AUTOPROJECT_RUN_TOOLS`` (the
  top-level tier plus ``complete_autoproject_run`` and
  ``ask_autoproject_question``); no other tier has either. The log reads
  (``list_autoproject_runs``, ``read_autoproject_run``) are core
  ``tool_call`` tools gated on the ``autoprojects`` service, read-only,
  reachable from scripts and never from public projects.
- The loop tools act only on the calling run and only for its own
  top-level model: refused outside a run, for sub-agents and for scripts.
- The log reads are scoped to the calling conversation's AutoProject (never
  an argument): a foreign AutoProject reads exactly like a missing one, a
  deleted run number exactly like one never allocated.
- File spaces: ``autoproject://`` exists only in a run, ``proj://`` never
  in one; the sandbox mounts the AutoProject workspace at ``/autoproject``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chat.autoprojects import service
from chat.gemini_api.tool_dispatch import ToolContext, _dispatch_tool_call
from chat.gemini_api.tool_handlers import autoproject_runs as handlers
from chat.llm import tool_schemas
from db import autoproject_store, conversation_store, tool_wait_handle_store, user_store

OWNER_EMAIL = "owner@example.com"
STRANGER_EMAIL = "stranger@example.com"
LOOP_TOOLS = ["complete_autoproject_run", "ask_autoproject_question"]
READ_TOOLS = {"list_autoproject_runs", "read_autoproject_run"}


def _run(coro):
    return asyncio.run(coro)


def _names(tier) -> list[str]:
    return [t["name"] for t in tier]


# ---------------------------------------------------------------------------
# Rosters and tiers
# ---------------------------------------------------------------------------


def test_run_tier_is_the_top_level_tier_plus_the_two_loop_tools():
    top_level = _names(tool_schemas.TOP_LEVEL_TOOLS)
    assert _names(tool_schemas.AUTOPROJECT_RUN_TOOLS) == top_level + LOOP_TOOLS
    specs = {t["name"]: t for t in tool_schemas.AUTOPROJECT_RUN_TOOLS}
    complete = specs["complete_autoproject_run"]["parameters"]
    assert complete["required"] == ["summary", "result", "notes"]
    assert set(complete["properties"]) == {
        "summary", "result", "notes", "brief", "expected_brief_version", "autoproject_title",
    }
    ask = specs["ask_autoproject_question"]["parameters"]
    assert ask["required"] == ["question"]
    assert set(ask["properties"]) == {"question", "options"}
    # Neither tool takes an AutoProject or run id: the run is the caller's own.
    for spec in (complete, ask):
        assert not any("id" in name.split("_") for name in spec["properties"])


@pytest.mark.parametrize("tier", [
    "TOP_LEVEL_TOOLS", "ROUTINE_TOP_LEVEL_TOOLS", "SLACK_TOP_LEVEL_TOOLS", "SUB_AGENT_TOOLS",
    "USER_SUBAGENT_TOOLS", "INFERENCE_API_TOOLS", "PUBLIC_TOOLS", "PUBLIC_ROUTINE_TOOLS",
])
def test_no_other_tier_has_the_loop_tools(tier):
    names = _names(getattr(tool_schemas, tier))
    assert not set(LOOP_TOOLS) & set(names)


def test_read_tools_are_core_gated_read_only_and_script_reachable():
    from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST

    assert tool_schemas.AUTOPROJECT_ONLY_TOOL_CALL_TOOLS == READ_TOOLS
    for name in READ_TOOLS:
        spec = tool_schemas.TOOL_CALL_REGISTRY[name]
        assert spec["requires_service"] == "autoprojects"
        assert spec["core"] is True
        assert name in tool_schemas.CORE_TOOL_CALL_TOOLS
        assert name in SCRIPT_TOOL_CALL_ALLOWLIST
        assert name not in tool_schemas.PUBLIC_TOOL_CALL_ALLOWLIST
    assert not READ_TOOLS & tool_schemas.mutating_tool_call_tools()
    assert not set(LOOP_TOOLS) & SCRIPT_TOOL_CALL_ALLOWLIST
    assert not set(LOOP_TOOLS) & set(tool_schemas.TOOL_CALL_REGISTRY)


def test_loop_arms_registered_in_both_spellings_and_blocked_in_public():
    from chat.gemini_api import turn_tools

    for name in LOOP_TOOLS:
        key = turn_tools.registry_key_for("tool_call", {"tool_name": name})
        assert key == f"tool_call:{name}"
        assert turn_tools.registry_key_for(name, {}) == name
        for spelling in (name, key):
            assert spelling in turn_tools.TURN_TOOL_HANDLERS
            assert spelling in turn_tools._PUBLIC_BLOCKED_LOOP_TOOLS


# ---------------------------------------------------------------------------
# Fixture and helpers (isolated database)
# ---------------------------------------------------------------------------


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
    return SimpleNamespace(
        owner=owner, stranger=stranger, gate=gate, tmp=tmp_path,
        autoprojects_dir=tmp_path / "autoprojects",
    )


async def _start(user, autoproject, prompt="Do it") -> dict:
    cid = await service.create_run_conversation(user, autoproject["id"])
    meta = await conversation_store.get_conversation_meta(user["id"], cid)
    return (await service.start_run(user, meta, prompt=prompt, model="fake-model")).run


async def _finished_runs(user, autoproject, count: int) -> list[dict]:
    runs = []
    for n in range(1, count + 1):
        run = await _start(user, autoproject, prompt=f"Prompt {n}")
        await autoproject_store.complete_run(
            run["id"], summary=f"Summary {n}", result=f"Result {n}", notes=f"Notes {n}",
            handoff_missing=(n == 2),
        )
        runs.append(run)
    return runs


def _ctx(user, run=None, **overrides) -> ToolContext:
    fields = dict(
        app=None, provider=None, user=dict(user),
        conversation_id=run["conversation_id"] if run else "c-plain", timezone="UTC",
        autoproject_id=run["autoproject_id"] if run else None,
        autoproject_run_id=run["id"] if run else None,
    )
    fields.update(overrides)
    return ToolContext(**fields)


async def _list(ctx, **args) -> dict:
    return json.loads(await handlers.handle_list_autoproject_runs(ctx, args))


async def _read(ctx, **args) -> dict:
    return json.loads(await handlers.handle_read_autoproject_run(ctx, args))


# ---------------------------------------------------------------------------
# list_autoproject_runs / read_autoproject_run
# ---------------------------------------------------------------------------


def test_list_pages_the_log_newest_first(env, monkeypatch):
    monkeypatch.setattr(handlers, "AUTOPROJECT_RUN_LIST_PAGE_SIZE", 3)

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        await _finished_runs(env.owner, autoproject, 5)
        current = await _start(env.owner, autoproject, prompt="The current prompt")
        ctx = _ctx(env.owner, current)

        first = await _list(ctx)
        assert (first["offset"], first["next_offset"], first["total"]) == (0, 3, 6)
        assert [e["run"] for e in first["runs"]] == [6, 5, 4]
        newest = first["runs"][0]
        assert newest["status"] == "running" and newest["summary"] is None
        assert newest["in_progress"] is True and newest["current"] is True
        assert newest["prompt_excerpt"] == "The current prompt"
        assert first["runs"][1] == {
            "run": 5, "date": first["runs"][1]["date"], "status": "completed",
            "summary": "Summary 5",
        }
        assert len(first["runs"][1]["date"]) == 10

        last = await _list(ctx, offset=3)
        assert [e["run"] for e in last["runs"]] == [3, 2, 1]
        assert last["next_offset"] is None
        assert last["runs"][1]["handoff_missing"] is True
        assert (await _list(ctx, offset=99))["runs"] == []
        assert (await _list(ctx, offset=-1))["code"] == "invalid_argument"

    _run(body())


def test_read_returns_one_run_with_its_questions(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        first = await _start(env.owner, autoproject)
        handle = await tool_wait_handle_store.create_handle(
            user_id=env.owner["id"], conversation_id=first["conversation_id"],
            kind="autoproject_question", tool_id="t", payload={"question": "Which?", "options": []},
        )
        await tool_wait_handle_store.resolve_handle(
            handle["id"], new_status="accepted", response={"answer": "EU"},
            correlation_kind="autoproject_run", correlation_id=first["id"],
            user_id=env.owner["id"],
        )
        await autoproject_store.complete_run(first["id"], summary="S", result="R", notes="N")
        current = await _start(env.owner, autoproject, prompt="Second")
        ctx = _ctx(env.owner, current)

        for spelling in (1, "1", "#1", 1.0):
            got = await _read(ctx, run=spelling)
            assert got["run"] == 1
        assert (got["prompt"], got["status"], got["summary"], got["result"], got["notes"]) == (
            "Do it", "completed", "S", "R", "N",
        )
        assert got["handoff_missing"] is False and got["error"] is None
        assert [(q["question"], q["answer"], q["status"]) for q in got["questions"]] == [
            ("Which?", "EU", "answered"),
        ]
        assert "in_progress" not in got

        own = await _read(ctx, run=2)
        assert own["status"] == "running"
        assert (own["summary"], own["result"], own["notes"]) == (None, None, None)
        assert own["in_progress"] is True and own["current"] is True

    _run(body())


def test_an_active_runs_pending_entry_is_not_shown(env):
    """A brief_conflict saves an entry on a run that is still open; the read
    tools show that run as in progress, not its pending text."""
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        await autoproject_store.save_run_entry(run["id"], summary="S", result="R", notes="N")
        got = await _read(_ctx(env.owner, run), run=1)
        assert (got["summary"], got["result"], got["notes"]) == (None, None, None)
        listed = (await _list(_ctx(env.owner, run)))["runs"][0]
        assert listed["summary"] is None and listed["in_progress"] is True

    _run(body())


def test_deleted_and_never_allocated_runs_read_the_same(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        runs = await _finished_runs(env.owner, autoproject, 2)
        await service.delete_run(env.owner, autoproject["id"], runs[0]["id"])
        ctx = _ctx(env.owner, runs[1])
        deleted = await _read(ctx, run=1)
        never = await _read(ctx, run=9)
        assert deleted["code"] == never["code"] == "run_not_found"
        assert deleted["error"].replace("#1", "#N") == never["error"].replace("#9", "#N")
        # Run numbers are per AutoProject: another AutoProject's run 2 is not here.
        other = await service.create_autoproject(env.owner, title="Other")
        other_run = await _start(env.owner, other)
        assert (await _read(_ctx(env.owner, other_run), run=2))["code"] == "run_not_found"

    _run(body())


@pytest.mark.parametrize("args", [{}, {"run": None}, {"run": "abc"}, {"run": 0}, {"run": True}])
def test_read_argument_refusals(env, args):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        assert (await _read(_ctx(env.owner, run), **args))["code"] == "invalid_argument"

    _run(body())


@pytest.mark.parametrize("tool", ["list", "read"])
def test_reads_refuse_outside_a_run_with_the_gate_closed_and_for_a_foreign_autoproject(env, tool):
    call = _list if tool == "list" else _read
    args = {} if tool == "list" else {"run": 1}

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)

        outside = await call(_ctx(env.owner), **args)
        assert outside["code"] == "not_autoproject_run"

        # A foreign AutoProject id in the context reads exactly like a missing one.
        foreign = await call(_ctx(env.stranger, run), **args)
        missing_id = "00000000-0000-4000-8000-000000000000"
        missing = await call(_ctx(env.owner, run, autoproject_id=missing_id), **args)
        assert foreign == {
            "error": f"AutoProject not found: {autoproject['id']}",
            "code": "autoproject_not_found",
        }
        assert missing == {
            "error": f"AutoProject not found: {missing_id}", "code": "autoproject_not_found",
        }

        env.gate.discard(OWNER_EMAIL)
        assert (await call(_ctx(env.owner, run), **args))["code"] == "autoprojects_disabled"

    _run(body())


# ---------------------------------------------------------------------------
# Shared dispatch: callers other than the run's own model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller", [{"is_sub_agent": True, "agent_name": "A"}, {"is_script": True}])
@pytest.mark.parametrize("tool", LOOP_TOOLS)
@pytest.mark.parametrize("via_tool_call", [False, True])
def test_loop_tools_are_refused_for_sub_agents_and_scripts(env, caller, tool, via_tool_call):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        args = {"summary": "s", "result": "r", "notes": "n", "question": "q"}
        name, call_args = (
            ("tool_call", {"tool_name": tool, "arguments": args}) if via_tool_call else (tool, args)
        )
        result, _parts = await _dispatch_tool_call(
            None, None, dict(env.owner), run["conversation_id"], "UTC", name, call_args,
            autoproject_id=run["autoproject_id"], autoproject_run_id=run["id"], **caller,
        )
        error = json.loads(result)["error"]
        assert "can only be called by the AutoProject run's own model" in error
        assert (await autoproject_store.get_run(run["id"]))["status"] == "running"
        assert await tool_wait_handle_store.list_pending_for_conversation(
            env.owner["id"], run["conversation_id"],
        ) == []

    _run(body())


@pytest.mark.parametrize("caller", [
    {}, {"is_sub_agent": True, "agent_name": "A"}, {"is_script": True},
])
def test_read_tools_reach_the_runs_autoproject_through_dispatch(env, caller):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        await _finished_runs(env.owner, autoproject, 1)
        run = await _start(env.owner, autoproject)
        result, _parts = await _dispatch_tool_call(
            None, None, dict(env.owner), run["conversation_id"], "UTC", "tool_call",
            {"tool_name": "read_autoproject_run", "arguments": {"run": 1}},
            autoproject_id=run["autoproject_id"], autoproject_run_id=run["id"], **caller,
        )
        assert json.loads(result)["summary"] == "Summary 1"

    _run(body())


def test_unknown_tool_error_lists_the_read_tools_only_in_a_run(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        inside, _ = await _dispatch_tool_call(
            None, None, dict(env.owner), run["conversation_id"], "UTC", "tool_call",
            {"tool_name": "nope"}, autoproject_id=run["autoproject_id"],
            autoproject_run_id=run["id"],
        )
        outside, _ = await _dispatch_tool_call(
            None, None, dict(env.owner), "c-plain", "UTC", "tool_call", {"tool_name": "nope"},
        )
        project, _ = await _dispatch_tool_call(
            None, None, dict(env.owner), "c-proj", "UTC", "tool_call", {"tool_name": "nope"},
            project_id="p1",
        )
        assert "list_autoproject_runs" in inside and "read_autoproject_run" in inside
        assert "autoproject" not in outside and "autoproject" not in project
        # A run has the scheme file tools but not the four *_workspace_file tools.
        assert "read_file" in inside and "get_workspace_file" not in inside

    _run(body())


def test_script_bridge_hands_the_lease_autoproject_to_dispatch(env):
    from chat.gemini_api.script_tool_call import ScriptToolCallRequest, script_tool_call_endpoint
    from chat.sandbox_tokens import SandboxLease

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        await _finished_runs(env.owner, autoproject, 1)
        run = await _start(env.owner, autoproject)
        lease = SandboxLease(
            user_id=env.owner["id"], conversation_id=run["conversation_id"], expires_at=1e12,
            autoproject_id=run["autoproject_id"], autoproject_run_id=run["id"],
        )
        ok = await script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="list_autoproject_runs", arguments={}),
            user=dict(env.owner), lease=lease,
        )
        assert ok.status_code == 200
        assert [e["run"] for e in json.loads(ok.body)["runs"]] == [2, 1]

        # Without an AutoProject run behind the container the read refuses.
        plain = await script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="list_autoproject_runs", arguments={}),
            user=dict(env.owner), lease=None,
        )
        assert json.loads(plain.body)["code"] == "not_autoproject_run"

        for tool in LOOP_TOOLS:
            refused = await script_tool_call_endpoint(
                ScriptToolCallRequest(tool_name=tool, arguments={}),
                user=dict(env.owner), lease=lease,
            )
            assert refused.status_code == 400
        assert (await autoproject_store.get_run(run["id"]))["status"] == "running"

    _run(body())


def test_set_conversation_name_is_refused_in_a_run(env):
    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        result, _ = await _dispatch_tool_call(
            None, None, dict(env.owner), run["conversation_id"], "UTC", "tool_call",
            {"tool_name": "set_conversation_name", "arguments": {"name": "Renamed"}},
            autoproject_id=run["autoproject_id"], autoproject_run_id=run["id"],
        )
        assert "no conversation name" in json.loads(result)["error"]
        meta = await conversation_store.get_conversation_meta(
            env.owner["id"], run["conversation_id"],
        )
        assert meta.get("custom_name") in (None, "")

    _run(body())


# ---------------------------------------------------------------------------
# The complete_autoproject_run arm
# ---------------------------------------------------------------------------


def _run_ctx(env, run, **overrides):
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
    return RunContext(**fields), emitted


def _complete_call(args, *, via_tool_call=False):
    from chat.gemini_api.turn_tools import COMPLETE_AUTOPROJECT_RUN_VIA_TOOL_CALL_KEY, ToolCall

    if via_tool_call:
        return ToolCall(
            name="tool_call", key=COMPLETE_AUTOPROJECT_RUN_VIA_TOOL_CALL_KEY,
            tool_id="tid", raw_tool_id="raw",
            args={"tool_name": "complete_autoproject_run", "arguments": args},
        )
    return ToolCall(
        name="complete_autoproject_run", key="complete_autoproject_run",
        tool_id="tid", raw_tool_id="raw", args=args,
    )


@pytest.mark.parametrize("via_tool_call", [False, True])
def test_accepted_completion_closes_the_tool_use_and_ends_the_loop(env, via_tool_call):
    from chat.gemini_api.turn_tools import (
        FinishAutoProjectRun,
        TurnState,
        _handle_complete_autoproject_run,
    )

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        ctx, emitted = _run_ctx(env, run)
        with pytest.raises(FinishAutoProjectRun) as exc:
            await _handle_complete_autoproject_run(
                ctx, _complete_call({"summary": "s", "result": "r", "notes": "n"},
                                    via_tool_call=via_tool_call),
                TurnState(),
            )
        assert exc.value.tool_id == "tid"
        assert [e["type"] for e in emitted] == ["tool_result"]
        assert json.loads(emitted[0]["tool_output"])["status"] == "completed"
        assert ctx.structured_messages[0]["role"] == "assistant"
        assert (await autoproject_store.get_run(run["id"]))["status"] == "completed"

    _run(body())


def test_completion_refusals_are_tool_results_and_keep_the_loop_going(env):
    from chat.gemini_api.turn_tools import TurnState, _handle_complete_autoproject_run

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Log")
        run = await _start(env.owner, autoproject)
        outside, _ = _run_ctx(env, run, autoproject_id=None, autoproject_run_id=None)
        out = json.loads(await _handle_complete_autoproject_run(
            outside, _complete_call({"summary": "s"}), TurnState(),
        ))
        assert "only available inside AutoProject runs" in out["error"]

        ctx, emitted = _run_ctx(env, run)
        out = json.loads(await _handle_complete_autoproject_run(
            ctx, _complete_call({"summary": "s" * 500, "result": "r", "notes": "n"}), TurnState(),
        ))
        assert out["code"] == "summary_too_long"
        assert emitted == []
        assert (await autoproject_store.get_run(run["id"]))["status"] == "running"

    _run(body())


# ---------------------------------------------------------------------------
# File spaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,in_run,expected", [
    ("chat://notes.md", True, ("chat", "notes.md")),
    ("autoproject://reports/q3.md", True, ("autoproject", "reports/q3.md")),
    ("proj://notes.md", True, "no_project"),
    ("autoproject://notes.md", False, "no_autoproject"),
    ("proj://notes.md", False, ("proj", "notes.md")),
    ("autoproject://../escape", True, "invalid_path"),
    ("autoproject:///abs", True, "invalid_path"),
    ("notes.md", True, "invalid_path"),
])
def test_parse_space_path_by_conversation_kind(raw, in_run, expected):
    from chat.gemini_api.tool_handlers.file_paths import (
        SpacePathError,
        conversation_spaces,
        grammar_hint,
        parse_space_path,
    )

    spaces = conversation_spaces("ap-1" if in_run else None)
    if isinstance(expected, tuple):
        assert parse_space_path(raw, allow_root=False, spaces=spaces) == expected
        return
    with pytest.raises(SpacePathError) as exc:
        parse_space_path(raw, allow_root=False, spaces=spaces)
    assert exc.value.code == expected
    # The hint names the conversation's own two spaces.
    assert exc.value.message.endswith(grammar_hint(spaces))
    assert ("autoproject://<path>" in grammar_hint(spaces)) is in_run


def test_file_tools_read_and_write_the_autoproject_workspace_in_a_run(env):
    from chat.gemini_api import tool_dispatch

    async def body():
        autoproject = await service.create_autoproject(env.owner, title="Files")
        run = await _start(env.owner, autoproject)
        ctx = _ctx(env.owner, run)
        written = json.loads(await tool_dispatch._tool_write_file(
            ctx, {"path": "chat://draft.md", "content": "draft"},
        ))
        assert "error" not in written, written
        copied = json.loads(await tool_dispatch._tool_copy_file(
            ctx, {"src": "chat://draft.md", "dest": "autoproject://kept/draft.md"},
        ))
        assert "error" not in copied, copied
        on_disk = env.autoprojects_dir / autoproject["id"] / "workspace" / "kept" / "draft.md"
        assert on_disk.read_text() == "draft"
        listed = await tool_dispatch._tool_list_files(ctx, {"path": "autoproject://kept"})
        assert "draft.md" in listed

        # Another run of the same AutoProject sees the file.
        await autoproject_store.complete_run(run["id"], summary="s", result="r", notes="")
        later = await _start(env.owner, autoproject)
        read, _parts = await tool_dispatch._tool_read_file(
            _ctx(env.owner, later), {"path": "autoproject://kept/draft.md"},
        )
        assert "draft" in read

        refused = json.loads(await tool_dispatch._tool_write_file(
            ctx, {"path": "proj://x.md", "content": "x"},
        ))
        assert refused["code"] == "no_project"
        escape = json.loads(await tool_dispatch._tool_write_file(
            ctx, {"path": "autoproject://../x.md", "content": "x"},
        ))
        assert escape["code"] == "invalid_path"

        # Outside a run the scheme does not exist.
        outside = json.loads(await tool_dispatch._tool_write_file(
            _ctx(env.owner, conversation_id=run["conversation_id"], project_id="p1"),
            {"path": "autoproject://x.md", "content": "x"},
        ))
        assert outside["code"] == "no_autoproject"
        assert not (env.autoprojects_dir / autoproject["id"] / "workspace" / "x.md").exists()

    _run(body())


def test_sandbox_mounts_the_autoproject_workspace_only_for_a_run():
    from chat.gemini_api.tool_handlers import _build_script_podman_cmd

    plain = _build_script_podman_cmd("/ws", ["python3", "x.py"], "k")
    run = _build_script_podman_cmd("/ws", ["python3", "x.py"], "k", autoproject_dir="/ap/ws")
    assert not any("/autoproject" in part for part in plain)
    mount = run.index("/ap/ws:/autoproject:z")
    assert run[mount - 1] == "-v"
    # Everything else is the plain argv.
    assert run[:mount - 1] + run[mount + 1:] == plain
