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
5. The ``system:doc_notes`` skill (phase A7): its registration, listed and
   enumerated only while the pseudo-key is true (Quest Docs on either
   way), its line never naming ``suggest_doc_note`` (routine and read-only
   prompts list it too), loading it while closed answers exactly
   ``doc_notes_disabled_message()``, its content covers spec section 6.5
   and quotes the refusal texts from their constants; and the
   ``system:quest_docs`` pointer to it.
6. The notes line of the public Quest Docs paragraph: present only while
   both public flags are on, appended after the unchanged
   ``_PUBLIC_DOCS_SECTION``, without ``suggest_doc_note`` in a routine run,
   and naming no skill or internal-only tool.
7. The copy every run kind meets without the skill (the block ``note``
   and the unseen-notes messages) names no skill, and names
   ``suggest_doc_note`` only where the reader may lack it with a
   condition.

Both tools are core in phase A5 (a full Dynamic Tools bullet, on no system
skill line); the move onto the ``system:doc_notes`` line comes with their
tier change.
"""

import re

import pytest

import config.feature_gates as fg
from chat.docs import notes as notes_mod
from chat.docs.access import DENY_OUTSIDE_PROJECT
from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_MEMORY_MAX_CHARS,
    DOC_NOTE_MAX_PENDING_PER_CONVERSATION,
    DOC_NOTE_MAX_PENDING_PER_DOC,
    DOC_NOTES_SERVICE_KEY,
    DOCS_SERVICE_KEY,
    doc_notes_disabled_message,
)
from chat.gemini_api import conversation as conv_mod
from chat.gemini_api.system_prompt import (
    _PUBLIC_DOC_NOTES_ROUTINE,
    _PUBLIC_DOC_NOTES_SENTENCE,
    _PUBLIC_DOC_NOTES_SUGGEST,
    _PUBLIC_DOCS_SECTION,
    _PUBLIC_TABLES_SECTION,
    _doc_note_tool_names,
    _doc_tool_names,
    _public_doc_notes_line,
    get_inference_api_system_prompt,
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
    get_user_subagent_system_prompt,
)
from chat.system_skills import (
    CATALOG,
    build_system_skills_enumeration,
    list_system_skills,
    load_system_skills,
)
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

SKILL = "system:doc_notes"

OPEN = {DOCS_SERVICE_KEY: True, DOC_NOTES_SERVICE_KEY: True}
# Gate closed for the user: the key false (Quest Docs on), or missing.
CLOSED_MAPS = [
    {DOCS_SERVICE_KEY: True, DOC_NOTES_SERVICE_KEY: False},
    {},
]
CLOSED_IDS = ["notes_off", "empty"]
# Gate closed with Quest Docs open: the key false, or missing.
DOCS_ONLY_MAPS = [
    {DOCS_SERVICE_KEY: True, DOC_NOTES_SERVICE_KEY: False},
    {DOCS_SERVICE_KEY: True},
]
DOCS_ONLY_IDS = ["notes_off", "notes_missing"]


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

    def test_needs_the_docs_flag_too(self):
        # The notes tools never appear without the doc tools they point to.
        _assert_none_named(_public(docs_enabled=False, doc_notes_enabled=True))

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


# ---------------------------------------------------------------------------
# 5. The system:doc_notes skill
# ---------------------------------------------------------------------------


def _skill_line(prompt: str) -> str | None:
    """The ``system:doc_notes`` enumeration line of a prompt, or None."""
    match = re.search(r"^- `system:doc_notes` -- .*$", prompt, re.M)
    return match.group(0) if match else None


def _skill_content() -> str:
    return CATALOG[SKILL].content_builder(BASE_URL, API_KEY)


def _flat(text: str) -> str:
    """*text* with every whitespace run as one space (line wrapping aside)."""
    return " ".join(text.split())


class TestDocNotesSkill:
    def test_registration(self):
        skill = CATALOG[SKILL]
        assert skill.name == "Quest Doc notes"
        assert skill.requires == DOC_NOTES_SERVICE_KEY
        assert skill.requires_project is False
        assert 0 < len(skill.description) <= 120
        # Right after the Quest Docs skill it extends.
        ids = list(CATALOG)
        assert ids.index(SKILL) == ids.index("system:quest_docs") + 1

    @pytest.mark.parametrize("has_project", [False, True])
    def test_listed_only_while_open(self, has_project):
        on = {s.id for s in list_system_skills(OPEN, has_project)}
        assert SKILL in on and "system:quest_docs" in on
        for services in DOCS_ONLY_MAPS:
            off = {s.id for s in list_system_skills(services, has_project)}
            assert SKILL not in off, services
            # Quest Docs stays, pointing at a skill it cannot know is gated.
            assert "system:quest_docs" in off, services

    @pytest.mark.parametrize("services", DOCS_ONLY_MAPS, ids=DOCS_ONLY_IDS)
    def test_enumeration_follows_the_gate(self, services):
        assert f"`{SKILL}`" in build_system_skills_enumeration(OPEN)
        closed = build_system_skills_enumeration(services)
        assert "`system:quest_docs`" in closed
        assert SKILL not in closed

    @pytest.mark.parametrize("prompt_kind", ["standalone", "project", "slack"])
    def test_on_the_top_level_prompts_while_open(self, prompt_kind):
        kw = {"standalone": {}, "project": {"has_project": True},
              "slack": {"is_slack": True}}[prompt_kind]
        assert _skill_line(_top_level(OPEN, **kw)) is not None
        for services in CLOSED_MAPS:
            assert SKILL not in _top_level(services, **kw)

    @pytest.mark.parametrize("has_project", [False, True])
    def test_line_never_names_the_suggest_tool(self, has_project):
        # The routine and read-only prompts list the skill but are not
        # offered suggest_doc_note, so its line must not name it in the
        # description or trigger (a Tools: tail drops excluded tools).
        skill = CATALOG[SKILL]
        assert SUGGEST not in skill.description + skill.when_to_load
        for prompt in (
            _top_level(OPEN, has_project=has_project, is_routine=True),
            _sub_agent(OPEN, has_project=has_project),
        ):
            line = _skill_line(prompt)
            assert line is not None
            assert SUGGEST not in line

    def test_load_while_open(self):
        [loaded] = load_system_skills([SKILL], OPEN, BASE_URL, API_KEY)
        assert loaded["id"] == SKILL
        assert "error" not in loaded
        assert loaded["content"].startswith("## Quest Doc notes")

    @pytest.mark.parametrize("services", CLOSED_MAPS + DOCS_ONLY_MAPS[1:],
                             ids=CLOSED_IDS + DOCS_ONLY_IDS[1:])
    def test_load_while_closed_names_the_gate(self, services):
        [refused] = load_system_skills([SKILL], services, BASE_URL, API_KEY)
        assert refused["error"] == doc_notes_disabled_message()
        assert "content" not in refused

    def test_content_covers_the_spec(self):
        content = _flat(_skill_content())
        # What the notes are, and the tools and results that carry them.
        for term in (
            "**Instructions**", "**Memories**", "Notes panel",
            "read_doc", "list_docs", "search_docs", LIST, SUGGEST,
            "doc_notes", "`notes` counts", "memories_omitted", "memory_offset",
            "can_suggest",
        ):
            assert term in content, term
        # The write refusal and its retry, on every write path.
        for term in (
            "doc_notes_unseen", "edit_doc", "append_to_doc", "add_doc_image",
            "write_doc", "retry goes through", "memory changes never block a write",
            "fails at Approve",
        ):
            assert term in content, term
        # When and how to suggest (system:memory's guidance mirrored).
        for term in (
            '"from now on, in this doc always ..."', "COMPLETE new instructions text",
            "One self-contained fact per suggestion", "replaces_memory_id",
            "a handful per conversation", "never say a suggestion was saved",
            "do not restate it", "already_saved", "already_suggested",
            "**What NOT to suggest.**", "**Which store?**", "**Before suggesting**",
            "Never the same fact in two stores",
        ):
            assert term in content, term
        # Who may suggest and from where.
        for term in (
            "instructions_owner_only", "only the doc owner's conversations",
            "from a conversation inside that project", "Slack",
            "their own public project's conversations",
            "routine runs, sub-agents, cross-user subagents, inference API runs or sandbox scripts",
            "include the proposed note in your response instead",
        ):
            assert term in content, term
        # The caps come from the constants.
        for value in (
            f"{DOC_INSTRUCTIONS_MAX_CHARS:,} characters",
            f"{DOC_MEMORY_MAX_CHARS:,} characters",
            f"{DOC_MAX_MEMORIES} memories",
            f"{DOC_NOTE_MAX_PENDING_PER_CONVERSATION} per conversation",
            f"{DOC_NOTE_MAX_PENDING_PER_DOC} per doc",
        ):
            assert value in content, value

    def test_quotes_the_refusal_constants(self):
        # Verbatim, unwrapped: each quote sits on one line of the content.
        content = _skill_content()
        for text in (
            notes_mod.DOC_NOTES_UNSEEN_MESSAGE.format(title="<title>"),
            notes_mod.DOC_NOTE_SUGGEST_UNSEEN_MESSAGE.format(title="<title>"),
            notes_mod.DOC_NOTE_INSTRUCTIONS_OWNER_ONLY_MESSAGE,
            DENY_OUTSIDE_PROJECT,
        ):
            assert text in content, text
        # The backstop's advice, word for word (case and wrapping aside).
        advice = notes_mod.DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE.rsplit(". ", 1)[1]
        assert advice.lower() in _flat(content).lower()


def test_quest_docs_skill_points_at_doc_notes():
    content = _flat(CATALOG["system:quest_docs"].content_builder(BASE_URL, API_KEY))
    assert f"`{SKILL}`" in content
    for term in ("`notes`", "`doc_notes`", "doc_notes_unseen"):
        assert term in content, term
    # Phrased for a gate the skill cannot see: true while it is closed too.
    assert "While Quest Doc notes are enabled for the user" in content


# ---------------------------------------------------------------------------
# 6. The notes line of the public Quest Docs paragraph
# ---------------------------------------------------------------------------


def _docs_paragraph(prompt: str) -> str:
    """The public Quest Docs paragraph, up to the next blank line."""
    start = prompt.index("**Quest Docs:**")
    end = prompt.find("\n\n", start)
    return prompt[start:end if end != -1 else len(prompt)]


class TestPublicNotesLine:
    def test_present_while_open(self):
        prompt = _public(docs_enabled=True, doc_notes_enabled=True)
        line = _PUBLIC_DOC_NOTES_SENTENCE + _PUBLIC_DOC_NOTES_SUGGEST + "\n"
        assert _public_doc_notes_line(False) == line
        # Closes the unchanged docs paragraph.
        assert _PUBLIC_DOCS_SECTION + line in prompt
        assert prompt.count(_PUBLIC_DOC_NOTES_SENTENCE) == 1
        assert _PUBLIC_DOC_NOTES_ROUTINE not in prompt
        assert f"`{SUGGEST}`" in _docs_paragraph(prompt)

    @pytest.mark.parametrize("is_routine", [False, True])
    @pytest.mark.parametrize("tables_enabled", [False, True])
    def test_absent_while_notes_closed(self, is_routine, tables_enabled):
        prompt = _public(
            docs_enabled=True, doc_notes_enabled=False,
            is_routine=is_routine, tables_enabled=tables_enabled,
        )
        assert _PUBLIC_DOC_NOTES_SENTENCE not in prompt
        assert "doc_notes" not in prompt
        # The docs paragraph is today's, with nothing appended to it.
        after = _PUBLIC_TABLES_SECTION if tables_enabled else "\n**"
        assert _PUBLIC_DOCS_SECTION + after in prompt

    @pytest.mark.parametrize("is_routine", [False, True])
    def test_absent_while_docs_closed(self, is_routine):
        prompt = _public(docs_enabled=False, doc_notes_enabled=True, is_routine=is_routine)
        assert "**Quest Docs:**" not in prompt
        assert _PUBLIC_DOC_NOTES_SENTENCE not in prompt
        assert "doc_notes" not in prompt

    def test_routine_variant_never_names_the_suggest_tool(self):
        prompt = _public(docs_enabled=True, doc_notes_enabled=True, is_routine=True)
        line = _PUBLIC_DOC_NOTES_SENTENCE + _PUBLIC_DOC_NOTES_ROUTINE + "\n"
        assert _public_doc_notes_line(True) == line
        assert _PUBLIC_DOCS_SECTION + line in prompt
        assert _PUBLIC_DOC_NOTES_SUGGEST not in prompt
        assert SUGGEST not in line
        assert SUGGEST not in _docs_paragraph(prompt)

    @pytest.mark.parametrize("is_routine", [False, True])
    def test_names_no_skill_or_internal_tool(self, is_routine):
        # The test_no_internal_tool_docs terms, on the line and on the
        # whole notes-open prompt (whose get_response_content description
        # cross-references authed_get: only its bullet is internal).
        line = _public_doc_notes_line(is_routine)
        prompt = _public(
            docs_enabled=True, doc_notes_enabled=True, is_routine=is_routine,
            tables_enabled=True,
        )
        assert "authed_get" not in line
        assert core_bullet("authed_get") not in prompt
        assert "suggest_memory" not in line
        for text in (line, prompt):
            for term in (
                "system:", "load_skills", "authed_post", "agent_task(",
                "load_gmail_attachment", "wait_for_handles",
            ):
                assert term not in text, term
            assert not re.search(r"(?<!project_)memory_search", text)
        # Only tools the public prompt offers.
        for name in re.findall(r"`([a-z_]+)`", line):
            if name in ("doc_notes", "doc_notes_unseen"):
                continue
            assert core_bullet(name) in prompt, name


# ---------------------------------------------------------------------------
# 7. Copy every run kind meets without the skill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "DOC_NOTES_BLOCK_NOTE", "DOC_NOTES_UNSEEN_MESSAGE",
    "DOC_NOTE_SUGGEST_UNSEEN_MESSAGE", "DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE",
])
def test_standalone_copy_names_no_skill(name):
    text = getattr(notes_mod, name)
    assert "system:" not in text
    assert "load_skills" not in text


def test_copy_offers_suggesting_only_where_it_is_offered():
    # The block note reaches runs without suggest_doc_note (routine runs,
    # sub-agents, cross-user subagents, inference API runs, and scripts
    # through list_doc_notes); the write refusal reaches routine runs.
    assert f"{SUGGEST} where it is offered" in notes_mod.DOC_NOTES_BLOCK_NOTE
    assert SUGGEST not in notes_mod.DOC_NOTES_UNSEEN_MESSAGE
    # Both unseen messages say the retry goes through.
    for text in (notes_mod.DOC_NOTES_UNSEEN_MESSAGE,
                 notes_mod.DOC_NOTE_SUGGEST_UNSEEN_MESSAGE):
        assert "will not be refused for these notes again" in text
