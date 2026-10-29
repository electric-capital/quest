"""Saved memories injected into the system prompt in tiers
(chat/memory_prompt.py, the "Saved Memories" section of get_system_prompt,
the "Project Memories" paragraph with the project's block, and the
run_conversation_turn wiring).

Covers the rendering (newest first, ids and dates, the full tier, the
index of one-line references, the trailing count when both tiers are
capped, the first memory always included), the index snippet, the failure
path (a store error yields no section, never an exception), the prompt
sections (present with content, absent without, the memory_get /
memory_search rules pointing at them) and which runs get memories from
``run_conversation_turn`` (personal: standard, routine and Slack, never
public, cross-user subagent or inference-API runs; project: every project
conversation, public ones included).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from chat import memory_prompt
from chat.gemini_api import conversation as conv_mod
from chat.gemini_api.system_prompt import get_public_project_system_prompt, get_system_prompt
from chat.memory_prompt import (
    INDEX_SNIPPET_CHARS,
    PERSONAL_MEMORY_BUDGET,
    PROJECT_MEMORY_BUDGET,
    MemoryBudget,
    build_memories_content,
    build_project_memories_content,
    index_snippet,
    load_memories_content,
    load_project_memories_content,
    render_memories,
)

API_KEY = "test-api-key"
BASE_URL = "http://localhost:8000"
SECTION = "## Saved Memories"
FULL = PERSONAL_MEMORY_BUDGET.full_count
INDEXED = PERSONAL_MEMORY_BUDGET.index_count


def _memory(i: int, content: str | None = None, created: str | None = None) -> dict:
    return {
        "id": f"m{i}",
        "user_id": 1,
        "content": content if content is not None else f"fact {i}",
        "created_at": created if created is not None else f"2026-10-{i % 28 + 1:02d}T10:00:00",
        "updated_at": None,
        "archived": False,
    }


def _tiers(text: str) -> tuple[int, int]:
    """(full entries, index lines) in a rendered block."""
    return text.count("<memory "), text.count("<ref ")


# ---------------------------------------------------------------------------
# index_snippet
# ---------------------------------------------------------------------------

class TestIndexSnippet:
    def test_short_text_unchanged(self):
        assert index_snippet("Prefers metric units") == "Prefers metric units"

    def test_collapses_whitespace_across_lines(self):
        assert index_snippet("## Travel\n\nAisle  seats,\tplease") == "## Travel Aisle seats, please"

    def test_cuts_on_a_word_boundary_with_ellipsis(self):
        text = "word " * 40
        snippet = index_snippet(text)
        assert snippet.endswith("\u2026")
        assert len(snippet) <= INDEX_SNIPPET_CHARS + 1
        assert not snippet[:-1].endswith(" ")
        assert snippet[:-1] == ("word " * 20).strip()

    def test_cuts_mid_word_when_no_space_is_near(self):
        text = "x" * 300
        assert index_snippet(text) == "x" * INDEX_SNIPPET_CHARS + "\u2026"

    def test_custom_limit(self):
        assert index_snippet("abcdef ghij", limit=8) == "abcdef\u2026"


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
        assert "<memory-index>" not in text and "not shown" not in text

    def test_missing_date_and_whitespace(self):
        text = build_memories_content([_memory(1, "  padded  \n", created="")])
        assert text == '<memory id="m1">\npadded\n</memory>'

    def test_count_cap_moves_the_rest_into_the_index(self):
        memories = [_memory(i, f"fact {i} about\nsomething") for i in range(FULL + 7)]
        text = build_memories_content(memories)
        assert _tiers(text) == (FULL, 7)
        assert f'<memory id="m{FULL - 1}"' in text
        assert f'<memory id="m{FULL}"' not in text
        index = text[text.index("<memory-index>"):]
        assert index.endswith("</memory-index>")
        assert "7 older memories, one line each" in index
        assert "`memory_get` returns the full text" in index
        assert "`memory_search` finds one by topic" in index
        assert f'<ref id="m{FULL}" created="2026-10-{FULL % 28 + 1:02d}">fact {FULL} about something</ref>' in index
        assert f'<ref id="m{FULL + 6}"' in index
        # Nothing was left out of both tiers.
        assert "not shown at all" not in text

    def test_index_lines_are_snippets(self):
        long = "z" * 500
        memories = [_memory(i) for i in range(FULL)] + [_memory(99, long)]
        text = build_memories_content(memories)
        assert f'<ref id="m99" created="2026-10-16">{"z" * INDEX_SNIPPET_CHARS}\u2026</ref>' in text
        assert long not in text

    def test_char_cap_moves_the_rest_into_the_index(self):
        big = "x" * 4000
        memories = [_memory(i, big) for i in range(10)]
        text = build_memories_content(memories)
        full, indexed = _tiers(text)
        assert 0 < full < 10 and full + indexed == 10
        entries_len = len(text.split("\n\n<memory-index>")[0])
        assert entries_len <= PERSONAL_MEMORY_BUDGET.full_chars
        assert "not shown at all" not in text

    def test_both_caps_report_omitted(self):
        memories = [_memory(i) for i in range(FULL + INDEXED + 7)]
        text = build_memories_content(memories)
        assert _tiers(text) == (FULL, INDEXED)
        assert (
            f"7 older memories are not shown at all ({FULL} in full and "
            f"{INDEXED} indexed, of {FULL + INDEXED + 7}). Call `memory_list` "
            "for the complete set or `memory_search` for a topic"
        ) in text
        assert text.index("</memory-index>") < text.index("not shown at all")

    def test_index_char_cap(self):
        budget = MemoryBudget(full_count=1, full_chars=10_000, index_count=100, index_chars=200)
        memories = [_memory(i, "w" * 80) for i in range(10)]
        text = render_memories(memories, budget)
        full, indexed = _tiers(text)
        assert full == 1 and 0 < indexed < 9
        index = text[text.index("<ref "):text.index("</memory-index>")]
        assert len(index.strip()) <= 200
        assert f"{9 - indexed} older memories are not shown at all (1 in full and {indexed} indexed, of 10)." in text

    def test_single_omitted_memory_wording(self):
        memories = [_memory(i) for i in range(FULL + INDEXED + 1)]
        text = build_memories_content(memories)
        assert f"1 older memory is not shown at all ({FULL} in full and {INDEXED} indexed, of {FULL + INDEXED + 1})." in text
        memories = [_memory(i) for i in range(FULL + 1)]
        assert "1 older memory, one line each" in build_memories_content(memories)

    def test_first_memory_always_included(self):
        huge = "y" * (PERSONAL_MEMORY_BUDGET.full_chars + 100)
        text = build_memories_content([_memory(1, huge), _memory(2, "small")])
        assert _tiers(text) == (1, 1)
        assert 'id="m1"' in text and huge in text
        assert '<ref id="m2" created="2026-10-03">small</ref>' in text

    def test_count_without_index(self):
        budget = MemoryBudget(full_count=2, full_chars=10_000, index_count=0, index_chars=0)
        text = render_memories([_memory(i) for i in range(5)], budget)
        assert _tiers(text) == (2, 0)
        assert "<memory-index>" not in text
        assert "3 older memories are not shown at all (2 in full, of 5)." in text


class TestBuildProjectMemoriesContent:
    def test_names_the_project_tools_and_noun(self):
        n = PROJECT_MEMORY_BUDGET.full_count + PROJECT_MEMORY_BUDGET.index_count + 2
        text = build_project_memories_content([_memory(i) for i in range(n)])
        assert _tiers(text) == (PROJECT_MEMORY_BUDGET.full_count, PROJECT_MEMORY_BUDGET.index_count)
        assert f"{PROJECT_MEMORY_BUDGET.index_count} older project memories, one line each" in text
        assert "`project_memory_get` returns the full text" in text
        assert "`project_memory_search` finds one by topic" in text
        assert "2 older project memories are not shown at all" in text
        assert "`project_memory_list` for the complete set" in text
        assert "`memory_get`" not in text and "`memory_list`" not in text

    def test_smaller_budget_than_personal(self):
        assert PROJECT_MEMORY_BUDGET.full_chars < PERSONAL_MEMORY_BUDGET.full_chars
        assert PROJECT_MEMORY_BUDGET.index_chars < PERSONAL_MEMORY_BUDGET.index_chars


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


class TestLoadProjectMemoriesContent:
    def test_renders_project_memories(self, monkeypatch):
        import db.project_memory_store as store
        listed = AsyncMock(return_value=[_memory(1, "Figures in EUR")])
        monkeypatch.setattr(store, "list_project_memories", listed)
        text = asyncio.run(load_project_memories_content("p1"))
        assert "Figures in EUR" in text
        listed.assert_awaited_once_with("p1")

    def test_store_failure_yields_empty(self, monkeypatch):
        import db.project_memory_store as store
        monkeypatch.setattr(
            store, "list_project_memories", AsyncMock(side_effect=RuntimeError("db down")),
        )
        assert asyncio.run(load_project_memories_content("p1")) == ""


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
        # The rule tells the model the memories are already here and how
        # to read an indexed one.
        assert 'already in this prompt (the "Saved Memories" section' in prompt
        assert 'tool_call(tool_name="memory_get", arguments={"ids": ["..."]})' in prompt
        assert "read an indexed memory in full with `memory_get`" in prompt

    @pytest.mark.parametrize("content", ["", "   \n"])
    def test_no_section_without_content(self, content):
        prompt = get_system_prompt(API_KEY, base_url=BASE_URL, memories_content=content)
        assert SECTION not in prompt
        assert "<memory " not in prompt

    def test_default_prompt_unchanged(self):
        assert get_system_prompt(API_KEY, base_url=BASE_URL) == get_system_prompt(
            API_KEY, base_url=BASE_URL, memories_content="",
        )


PROJECT_PARAGRAPH = "**Project Memories:**"


class TestProjectMemoriesParagraph:
    content = build_project_memories_content([_memory(1, "Figures in EUR")])

    def test_private_project_carries_the_block(self):
        prompt = get_system_prompt(
            API_KEY, base_url=BASE_URL, has_project=True,
            project_memories_content=self.content,
        )
        start = prompt.index(PROJECT_PARAGRAPH)
        paragraph = prompt[start:prompt.index("Keep the two kinds apart", start)]
        assert "Figures in EUR" in paragraph and '<memory id="m1"' in paragraph
        assert "memories as of the start of this session follow" in paragraph
        assert 'tool_call(tool_name="project_memory_get", arguments={"ids": ["..."]})' in paragraph
        assert "`project_memory_search` / `project_memory_list` for anything saved after" in paragraph
        # The tools-only wording is replaced, not duplicated.
        assert "Read them with" not in paragraph
        # The suggestion guidance still follows.
        assert "suggest_project_memory" in prompt[start:]

    @pytest.mark.parametrize("content", ["", "  \n"])
    def test_without_content_the_paragraph_names_the_tools(self, content):
        prompt = get_system_prompt(
            API_KEY, base_url=BASE_URL, has_project=True,
            project_memories_content=content,
        )
        assert PROJECT_PARAGRAPH in prompt
        assert "Read them with" in prompt
        assert "<memory " not in prompt
        assert prompt == get_system_prompt(API_KEY, base_url=BASE_URL, has_project=True)

    def test_ignored_without_a_project(self):
        prompt = get_system_prompt(
            API_KEY, base_url=BASE_URL, project_memories_content=self.content,
        )
        assert PROJECT_PARAGRAPH not in prompt and "Figures in EUR" not in prompt

    def test_public_project_carries_the_block(self):
        prompt = get_public_project_system_prompt(project_memories_content=self.content)
        assert "Figures in EUR" in prompt and '<memory id="m1"' in prompt
        assert "project_memory_get" in prompt
        # No personal-memory tool is named.
        assert "`memory_get`" not in prompt and "Saved Memories" not in prompt
        assert get_public_project_system_prompt() == get_public_project_system_prompt(
            project_memories_content="",
        )

    def test_routine_keeps_the_block_without_suggesting(self):
        prompt = get_system_prompt(
            API_KEY, base_url=BASE_URL, has_project=True, is_routine=True,
            project_memories_content=self.content,
        )
        assert "Figures in EUR" in prompt
        assert "suggest_project_memory" not in prompt[prompt.index(PROJECT_PARAGRAPH):]


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
    import db.project_memory_store as project_memories
    monkeypatch.setattr(
        project_memories, "list_project_memories",
        AsyncMock(return_value=[_memory(2, "Figures in EUR")]),
    )

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
        # No project: no project block.
        assert turn_env["project_memories_content"] == ""

    def test_project_conversation_gets_both(self, turn_env, monkeypatch):
        import db.project_store as project_store
        monkeypatch.setattr(
            project_store, "get_project",
            AsyncMock(return_value={"id": "p1", "guide": "", "public": False}),
        )
        _run_turn(project_id="p1")
        assert "Likes tea" in turn_env["memories_content"]
        assert "Figures in EUR" in turn_env["project_memories_content"]

    def test_public_project_gets_project_memories_only(self, turn_env, monkeypatch):
        import db.project_store as project_store
        monkeypatch.setattr(
            project_store, "get_project",
            AsyncMock(return_value={"id": "p1", "guide": "", "public": True}),
        )
        _run_turn(project_id="p1")
        assert "memories_content" not in turn_env
        assert "Figures in EUR" in turn_env["project_memories_content"]

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
        assert "project_memories_content" not in turn_env

    def test_inference_api_gets_none(self, turn_env):
        _run_turn(origin="inference_api")
        assert "memories_content" not in turn_env
        assert "project_memories_content" not in turn_env

    def test_store_failure_does_not_fail_the_turn(self, turn_env, monkeypatch):
        import db.memory_store as store
        monkeypatch.setattr(store, "list_memories", AsyncMock(side_effect=RuntimeError("down")))
        _run_turn()
        assert turn_env["memories_content"] == ""

    def test_project_store_failure_does_not_fail_the_turn(self, turn_env, monkeypatch):
        import db.project_memory_store as project_memories
        import db.project_store as project_store
        monkeypatch.setattr(
            project_store, "get_project",
            AsyncMock(return_value={"id": "p1", "guide": "", "public": False}),
        )
        monkeypatch.setattr(
            project_memories, "list_project_memories",
            AsyncMock(side_effect=RuntimeError("down")),
        )
        _run_turn(project_id="p1")
        assert "Likes tea" in turn_env["memories_content"]
        assert turn_env["project_memories_content"] == ""
