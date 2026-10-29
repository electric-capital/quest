"""Quest Doc notes prompting (issue #187).

Covers the prompt layer of the two doc-notes model tools, ``list_doc_notes``
and ``suggest_doc_note`` (phase A5):

1. ``_doc_note_tool_names()`` finds exactly the two tools (the registry
   specs gated on the ``doc_notes`` pseudo-key), apart from the seven
   Quest Docs tools.
2. Which prompts offer them while the pseudo-key is true: both in the
   top-level prompt (standalone, project and Slack runs), only
   ``list_doc_notes`` in a routine run and in the read-only tiers
   (sub-agents, cross-user subagents, inference API runs). While the key is
   false or missing, neither name appears in any prompt; an unknown context
   (``connected_services=None``) shows everything, as for every gated tool.
3. The public-project prompt's ``doc_notes_enabled`` flag: both tools
   inlined in full while it is on, neither named while it is off, and a
   public routine run reads only.
4. ``run_conversation_turn`` passes ``doc_notes_enabled_for()`` (which folds
   in the Quest Docs gate) into the public prompt.

Both tools are core in phase A5 (a full Dynamic Tools bullet, on no system
skill line). Phase A7 extends this module with the ``system:doc_notes``
skill (hidden when gated, present when open; the tools then move onto its
line) and the notes sentence of the public Quest Docs paragraph.
"""

import pytest

import config.feature_gates as fg
from chat.docs.constants import DOC_NOTES_SERVICE_KEY, DOCS_SERVICE_KEY
from chat.gemini_api import conversation as conv_mod
from chat.gemini_api.system_prompt import (
    _doc_note_tool_names,
    _doc_tool_names,
    get_inference_api_system_prompt,
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
    get_user_subagent_system_prompt,
)
from chat.system_skills import CATALOG
from tests.prompt_support import core_bullet, skill_tools, tool_offered
from tests.test_conversation_workspace_prompts import (
    _ASSISTANT_MSG,
    _USER_MSG,
    _patch_turn_collaborators,
    _run_turn,
    _storage,
    _write_history,
)

LIST = "list_doc_notes"
SUGGEST = "suggest_doc_note"
NOTE_TOOLS = (LIST, SUGGEST)

API_KEY = "test-api-key"
BASE_URL = "http://localhost:8000"

OPEN = {DOCS_SERVICE_KEY: True, DOC_NOTES_SERVICE_KEY: True}
# Gate closed for the user: the key false (Quest Docs on), or missing.
CLOSED_MAPS = [
    {DOCS_SERVICE_KEY: True, DOC_NOTES_SERVICE_KEY: False},
    {},
]
CLOSED_IDS = ["notes_off", "empty"]


def _offered(prompt: str) -> set[str]:
    """The doc-notes tools the prompt offers, in either form."""
    return {name for name in NOTE_TOOLS if tool_offered(prompt, name)}


def _assert_none_named(prompt: str) -> None:
    """Gate closed: neither tool is named anywhere in the prompt, not even
    in another tool's description (spec section 9)."""
    for name in NOTE_TOOLS:
        assert name not in prompt, name


def _top_level(services, **kw):
    return get_system_prompt(API_KEY, base_url=BASE_URL, connected_services=services, **kw)


def _sub_agent(services, **kw):
    return get_sub_agent_system_prompt(
        "helper", API_KEY, base_url=BASE_URL, connected_services=services, **kw,
    )


def _user_subagent(services):
    return get_user_subagent_system_prompt(
        API_KEY, base_url=BASE_URL, connected_services=services,
        target_user_email="t@example.com", caller_email="c@example.com",
    )


def _inference(services):
    return get_inference_api_system_prompt(
        API_KEY, base_url=BASE_URL, connected_services=services,
        user_email="u@example.com",
    )


def _public(**kw):
    return get_public_project_system_prompt(
        user_name="Ada", user_email="ada@example.com",
        project_guide="Track BTC prices.", **kw,
    )


# ---------------------------------------------------------------------------
# 1. The roster helper
# ---------------------------------------------------------------------------


class TestRoster:
    def test_exactly_the_two_tools(self):
        assert _doc_note_tool_names() == frozenset(NOTE_TOOLS)

    def test_apart_from_the_doc_tools(self):
        # The notes have their own gate, so the seven doc tools (gated on
        # the docs key) never include them.
        assert not (_doc_note_tool_names() & _doc_tool_names())
        assert len(_doc_tool_names()) == 7


# ---------------------------------------------------------------------------
# 2. Regular prompt builders (connected-services gating)
# ---------------------------------------------------------------------------


class TestTopLevel:
    @pytest.mark.parametrize("has_project", [False, True])
    def test_both_offered_when_open(self, has_project):
        prompt = _top_level(OPEN, has_project=has_project)
        assert _offered(prompt) == set(NOTE_TOOLS)

    @pytest.mark.parametrize("services", CLOSED_MAPS, ids=CLOSED_IDS)
    @pytest.mark.parametrize("has_project", [False, True])
    def test_none_when_closed(self, services, has_project):
        _assert_none_named(_top_level(services, has_project=has_project))

    @pytest.mark.parametrize("has_project", [False, True])
    def test_routine_reads_but_never_suggests(self, has_project):
        prompt = _top_level(OPEN, has_project=has_project, is_routine=True)
        assert _offered(prompt) == {LIST}

    def test_slack_runs_may_suggest(self):
        # Slack runs suggest too; the user reviews in the web UI or inbox.
        prompt = _top_level(OPEN, is_slack=True)
        assert _offered(prompt) == set(NOTE_TOOLS)

    @pytest.mark.parametrize("services", CLOSED_MAPS, ids=CLOSED_IDS)
    def test_routine_and_slack_closed(self, services):
        _assert_none_named(_top_level(services, is_routine=True))
        _assert_none_named(_top_level(services, is_slack=True))

    def test_unknown_context_shows_everything(self):
        # connected_services=None keeps the show-everything behaviour.
        assert _offered(_top_level(None)) == set(NOTE_TOOLS)
        assert _offered(_top_level(None, is_routine=True)) == {LIST}


class TestReadOnlyTiers:
    @pytest.mark.parametrize("can_nest", [False, True])
    @pytest.mark.parametrize("has_project", [False, True])
    def test_sub_agent_reads_only(self, has_project, can_nest):
        prompt = _sub_agent(OPEN, has_project=has_project, can_nest=can_nest)
        assert _offered(prompt) == {LIST}

    @pytest.mark.parametrize("services", CLOSED_MAPS, ids=CLOSED_IDS)
    @pytest.mark.parametrize("has_project", [False, True])
    def test_sub_agent_none_when_closed(self, services, has_project):
        _assert_none_named(_sub_agent(services, has_project=has_project))

    @pytest.mark.parametrize("build", [_user_subagent, _inference],
                             ids=["user_subagent", "inference"])
    def test_headless_tiers_read_only(self, build):
        assert _offered(build(OPEN)) == {LIST}
        assert _offered(build(None)) == {LIST}

    @pytest.mark.parametrize("services", CLOSED_MAPS, ids=CLOSED_IDS)
    @pytest.mark.parametrize("build", [_user_subagent, _inference],
                             ids=["user_subagent", "inference"])
    def test_headless_tiers_none_when_closed(self, build, services):
        _assert_none_named(build(services))


class TestCoreTier:
    """Phase A5: both tools are core, so a prompt documents them as full
    Dynamic Tools bullets and no system skill line names them (A7 moves
    them onto the ``system:doc_notes`` line)."""

    @pytest.mark.parametrize("prompt_kind", [
        "standalone", "project", "slack", "sub_agent", "user_subagent", "inference",
    ])
    def test_bullets_never_on_a_skill_line(self, prompt_kind):
        prompt = {
            "standalone": lambda: _top_level(OPEN),
            "project": lambda: _top_level(OPEN, has_project=True),
            "slack": lambda: _top_level(OPEN, is_slack=True),
            "sub_agent": lambda: _sub_agent(OPEN),
            "user_subagent": lambda: _user_subagent(OPEN),
            "inference": lambda: _inference(OPEN),
        }[prompt_kind]()
        for name in _offered(prompt):
            assert core_bullet(name) in prompt, name
        for skill_id in CATALOG:
            for name in NOTE_TOOLS:
                assert name not in (skill_tools(prompt, skill_id) or []), (skill_id, name)


# ---------------------------------------------------------------------------
# 3. The public-project prompt (explicit gate flag)
# ---------------------------------------------------------------------------


class TestPublicPrompt:
    def test_both_inlined_when_open(self):
        prompt = _public(docs_enabled=True, doc_notes_enabled=True)
        for name in NOTE_TOOLS:
            assert core_bullet(name) in prompt, name
        # The public prompt carries no regular skill machinery.
        assert "system:" not in prompt

    @pytest.mark.parametrize("docs_enabled", [False, True])
    @pytest.mark.parametrize("is_routine", [False, True])
    def test_none_named_when_closed(self, docs_enabled, is_routine):
        # Default flag (gate closed), with or without the doc tools.
        _assert_none_named(_public(docs_enabled=docs_enabled, is_routine=is_routine))
        _assert_none_named(_public(
            docs_enabled=docs_enabled, doc_notes_enabled=False,
            tables_enabled=True, browser_enabled=True, project_db_enabled=True,
            is_routine=is_routine,
        ))

    def test_routine_reads_but_never_suggests(self):
        prompt = _public(docs_enabled=True, doc_notes_enabled=True, is_routine=True)
        assert core_bullet(LIST) in prompt
        assert not tool_offered(prompt, SUGGEST)

    def test_flag_is_independent_of_the_other_gates(self):
        # Tables and browser on, notes off: still none; notes on beside
        # them: both, and never on the public System Skills block.
        prompt = _public(
            docs_enabled=True, doc_notes_enabled=True,
            tables_enabled=True, browser_enabled=True,
        )
        for name in NOTE_TOOLS:
            assert core_bullet(name) in prompt, name
            assert name not in (skill_tools(prompt, "system:browser") or []), name


# ---------------------------------------------------------------------------
# 4. run_conversation_turn threads the gate into the public prompt
# ---------------------------------------------------------------------------


@pytest.fixture()
def chats_dir(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    monkeypatch.setattr(_storage(), "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(_storage(), "PROJECTS_DIR", tmp_path / "projects", raising=True)
    return chats


@pytest.fixture()
def gates_file(tmp_path, monkeypatch):
    """Per-test feature gates file (missing initially = every gate closed)."""
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.mark.parametrize("docs,notes,expected", [
    (False, False, False),
    (True, False, False),
    # The notes gate requires Quest Docs (doc_notes_enabled_for folds it in).
    (False, True, False),
    (True, True, True),
])
def test_public_turn_passes_the_notes_gate(
    chats_dir, gates_file, monkeypatch, docs, notes, expected,
):
    _write_history(chats_dir, "c1", {
        "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
    })
    fg.set_feature_enabled(fg.FEATURE_DOCS, docs)
    fg.set_feature_enabled(fg.FEATURE_DOC_NOTES, notes)
    captured: dict = {}
    standard_calls: list = []

    def _capture(*_a, **kw):
        captured.update(kw)
        return "system prompt"

    def _standard(*_a, **kw):
        standard_calls.append(kw)
        return "system prompt"

    _patch_turn_collaborators(monkeypatch, public=True)
    import chat.gemini_api.system_prompt as sp_mod
    monkeypatch.setattr(sp_mod, "get_public_project_system_prompt", _capture)
    monkeypatch.setattr(conv_mod, "get_system_prompt", _standard)
    monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: object())

    _run_turn()
    assert standard_calls == []
    assert captured["docs_enabled"] is docs
    assert captured["doc_notes_enabled"] is expected
