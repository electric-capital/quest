"""Saved memories injected into the top-level system prompt
(chat/memory_prompt.py, the "Saved Memories" section of get_system_prompt,
and the run_conversation_turn wiring).

Covers the rendering (newest first, ids and dates, the count and character
caps with the "N not shown" line, the first memory always included), the
failure path (a store error yields no section, never an exception), the
prompt section (present with content, absent without, the memory_search
rule pointing at it) and which runs get memories from
``run_conversation_turn`` (standard, routine and Slack; never public,
cross-user subagent or inference-API runs).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from chat import memory_prompt
from chat.gemini_api import conversation as conv_mod
from chat.gemini_api.system_prompt import get_system_prompt
from chat.memory_prompt import (
    MAX_INJECTED_MEMORIES,
    MAX_INJECTED_MEMORY_CHARS,
    build_memories_content,
    load_memories_content,
)

API_KEY = "test-api-key"
BASE_URL = "http://localhost:8000"
SECTION = "## Saved Memories"


def _memory(i: int, content: str | None = None, created: str | None = None) -> dict:
    return {
        "id": f"m{i}",
        "user_id": 1,
        "content": content if content is not None else f"fact {i}",
        "created_at": created if created is not None else f"2026-10-{i % 28 + 1:02d}T10:00:00",
        "updated_at": None,
        "archived": False,
    }


# ---------------------------------------------------------------------------
# build_memories_content
# ---------------------------------------------------------------------------

class TestBuildMemoriesContent:
    def test_empty(self):
        assert build_memories_content([]) == ""

    def test_entries_keep_order_with_id_and_date(self):
        text = build_memories_content([
            _memory(1, "Prefers metric units", "2026-10-03T08:00:00+00:00"),
            _memory(2, "Works at Puntium", "2026-09-30T08:00:00+00:00"),
        ])
        assert text == (
            '<memory id="m1" created="2026-10-03">\nPrefers metric units\n</memory>'
            '\n\n'
            '<memory id="m2" created="2026-09-30">\nWorks at Puntium\n</memory>'
        )
        assert "not shown" not in text

    def test_missing_date_and_whitespace(self):
        text = build_memories_content([_memory(1, "  padded  \n", created="")])
        assert text == '<memory id="m1">\npadded\n</memory>'

    def test_count_cap_reports_omitted(self):
        memories = [_memory(i) for i in range(MAX_INJECTED_MEMORIES + 7)]
        text = build_memories_content(memories)
        assert text.count("<memory ") == MAX_INJECTED_MEMORIES
        assert f'id="m{MAX_INJECTED_MEMORIES - 1}"' in text
        assert f'id="m{MAX_INJECTED_MEMORIES}"' not in text
        assert (
            f"7 older memories are not shown ({MAX_INJECTED_MEMORIES} of "
            f"{MAX_INJECTED_MEMORIES + 7} fit here)."
        ) in text
        assert "`memory_list`" in text and "`memory_search`" in text

    def test_char_cap_reports_omitted(self):
        big = "x" * 4000
        memories = [_memory(i, big) for i in range(10)]
        text = build_memories_content(memories)
        shown = text.count("<memory ")
        assert 0 < shown < 10
        # Everything that was included fits the budget.
        entries_len = len(text.split("\n\n" + str(10 - shown) + " older")[0])
        assert entries_len <= MAX_INJECTED_MEMORY_CHARS
        assert f"{10 - shown} older memories are not shown ({shown} of 10 fit here)." in text

    def test_single_omitted_memory_wording(self):
        memories = [_memory(i) for i in range(MAX_INJECTED_MEMORIES + 1)]
        assert "1 older memory is not shown" in build_memories_content(memories)

    def test_first_memory_always_included(self):
        huge = "y" * (MAX_INJECTED_MEMORY_CHARS + 100)
        text = build_memories_content([_memory(1, huge), _memory(2)])
        assert text.count("<memory ") == 1
        assert 'id="m1"' in text
        assert "1 older memory is not shown (1 of 2 fit here)." in text


# ---------------------------------------------------------------------------
# load_memories_content
# ---------------------------------------------------------------------------

class TestLoadMemoriesContent:
    def test_renders_active_memories(self, monkeypatch):
        import db.memory_store as store
        listed = AsyncMock(return_value=[_memory(1, "Likes tea")])
        monkeypatch.setattr(store, "list_memories", listed)
        text = asyncio.run(load_memories_content(7))
        assert "Likes tea" in text
        listed.assert_awaited_once_with(7, include_archived=False)

    def test_store_failure_yields_empty(self, monkeypatch):
        import db.memory_store as store
        monkeypatch.setattr(store, "list_memories", AsyncMock(side_effect=RuntimeError("db down")))
        assert asyncio.run(load_memories_content(7)) == ""


# ---------------------------------------------------------------------------
# The prompt section
# ---------------------------------------------------------------------------

class TestPromptSection:
    def test_section_present_with_content(self):
        content = build_memories_content([_memory(1, "Prefers metric units")])
        prompt = get_system_prompt(API_KEY, base_url=BASE_URL, memories_content=content)
        assert SECTION in prompt
        assert '<memory id="m1"' in prompt
        assert "Prefers metric units" in prompt
        # The section sits in the preamble, before the message-format rules.
        assert prompt.index(SECTION) < prompt.index("**Message format:**")
        # The rule tells the model the memories are already here.
        assert 'already in this prompt (the "Saved Memories" section' in prompt

    @pytest.mark.parametrize("content", ["", "   \n"])
    def test_no_section_without_content(self, content):
        prompt = get_system_prompt(API_KEY, base_url=BASE_URL, memories_content=content)
        assert SECTION not in prompt
        assert "<memory " not in prompt

    def test_default_prompt_unchanged(self):
        assert get_system_prompt(API_KEY, base_url=BASE_URL) == get_system_prompt(
            API_KEY, base_url=BASE_URL, memories_content="",
        )


# ---------------------------------------------------------------------------
# run_conversation_turn wiring
# ---------------------------------------------------------------------------

_TURN_USER = {"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"}


def _fake_provider():
    from unittest.mock import MagicMock

    provider = MagicMock()
    provider.create_session = lambda *a, **kw: object()
    provider.repair_session_history = lambda _s: 0
    provider.get_pending_tool_uses = lambda _s: []
    provider.get_usage = lambda _s: MagicMock(
        input_tokens=0, output_tokens=0, cached_tokens=0, total_tokens=0,
    )
    provider.save_history = lambda _s: []
    provider.format_tool_results = lambda *_a, **_kw: []

    async def _stream(*_a, **_kw):
        if False:  # pragma: no cover - makes this an async generator
            yield None

    provider.send_message_stream = _stream
    return provider


@pytest.fixture()
def turn_env(tmp_path, monkeypatch):
    """Stub run_conversation_turn's collaborators and capture the prompt
    builders' kwargs; returns the dict the captures land in."""
    import importlib

    # Other suites reload chat.storage (in place); look it up at call time.
    storage = importlib.import_module("chat.storage")
    monkeypatch.setattr(storage, "CHATS_DIR", tmp_path / "chats", raising=True)
    monkeypatch.setattr(storage, "PROJECTS_DIR", tmp_path / "projects", raising=True)
    # No conversation directory is created: the prompt write is a no-op.
    monkeypatch.setattr(storage.ChatStorage, "set_system_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(conv_mod, "load_server_config", lambda: {"gemini": {"model": "fake"}})
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())
    monkeypatch.setattr(conv_mod, "get_provider_instance", lambda _n, _i=None: _fake_provider())
    monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: object())
    monkeypatch.setattr(conv_mod, "_read_chat_history", AsyncMock(return_value={}))
    monkeypatch.setattr(conv_mod, "_resolve_workspace_notice_flags", lambda *a: {})
    import config.model_selection as model_selection
    monkeypatch.setattr(model_selection, "is_model_allowed", lambda *a, **kw: True)
    import db.conversation_store as conv_store
    monkeypatch.setattr(conv_store, "update_conversation_model", AsyncMock())
    import db.skill_store as skill_store
    monkeypatch.setattr(skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr(skill_store, "get_project_autoloaded_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr(skill_store, "get_routine_autoloaded_skills", AsyncMock(return_value=[]))
    import api.instructions as instructions
    monkeypatch.setattr(instructions, "get_user_connected_services", lambda _u: {})
    import db.memory_store as store
    monkeypatch.setattr(store, "list_memories", AsyncMock(return_value=[_memory(1, "Likes tea")]))

    captured: dict = {}

    def _capture(*_a, **kw):
        captured.clear()
        captured.update(kw)
        return "system prompt"

    import chat.gemini_api.system_prompt as sp_mod
    monkeypatch.setattr(conv_mod, "get_system_prompt", _capture)
    for name in (
        "get_public_project_system_prompt",
        "get_user_subagent_system_prompt",
        "get_inference_api_system_prompt",
    ):
        monkeypatch.setattr(sp_mod, name, _capture)
    return captured


def _run_turn(**kwargs):
    async def _on_event(_e):
        return None

    asyncio.run(conv_mod.run_conversation_turn(
        app=None, user=_TURN_USER, message="hi", conversation_id="c1",
        timezone="UTC", model="fake", on_event=_on_event, **kwargs,
    ))


class TestTurnWiring:
    @pytest.mark.parametrize("kwargs", [
        {},
        {"routine_id": "r1"},
        {"origin": "slack"},
    ])
    def test_standard_runs_get_memories(self, turn_env, kwargs):
        _run_turn(**kwargs)
        assert "Likes tea" in turn_env["memories_content"]

    def test_public_project_gets_none(self, turn_env, monkeypatch):
        import db.project_store as project_store
        monkeypatch.setattr(
            project_store, "get_project",
            AsyncMock(return_value={"id": "p1", "guide": "", "public": True}),
        )
        _run_turn(project_id="p1")
        assert "memories_content" not in turn_env

    def test_user_subagent_gets_none(self, turn_env, monkeypatch):
        import db.user_store as user_store
        import db.user_subagent_run_store as run_store
        monkeypatch.setattr(
            run_store, "get_run_by_subagent_conversation",
            AsyncMock(return_value={"caller_user_id": 2, "skill_ids": []}),
        )
        monkeypatch.setattr(
            user_store, "get_user_by_id",
            AsyncMock(return_value={"id": 2, "name": "C", "email": "c@example.com"}),
        )
        _run_turn(origin="user_subagent")
        assert "memories_content" not in turn_env

    def test_inference_api_gets_none(self, turn_env):
        _run_turn(origin="inference_api")
        assert "memories_content" not in turn_env

    def test_store_failure_does_not_fail_the_turn(self, turn_env, monkeypatch):
        import db.memory_store as store
        monkeypatch.setattr(store, "list_memories", AsyncMock(side_effect=RuntimeError("down")))
        _run_turn()
        assert turn_env["memories_content"] == ""
