"""Doc notes gate functions, notes block and its text copy
(chat/docs/notes.py), pure functions with no database.

Covers phase A2 of spec 00011 (docs/architecture/doc-notes.md):

1. The section 5 grid: ``needs_delivery()`` and ``write_refused()`` over
   four seen states (no record, behind on memories only, behind on
   instructions, current) x four notes states (no notes, memories only,
   instructions only, both), ``records_delivery()`` over every
   ``RUN_KINDS`` value and conversation ids, and the phase-A3 read and
   write decisions they combine into. Every cell runs with the doc-dict
   shape, the ``get_notes_state()`` shape and a minimal dict carrying only
   the instructions text. Expected values are literal tables.
2. Gate edge cases: a seen record ahead of the doc, a doc that never had
   notes, the instructions text winning over ``has_instructions``,
   control characters counting as instructions, a dict without either
   instructions key, missing counters, an unknown run kind, a
   conversation id that is not a string. ``suggestion_allowed()`` (the
   suggestion taint rule) over every deny cause and the admitting verdicts.
3. ``build_notes_block()``: keys and their order, the note text, memory
   items reduced to id and content, the empty current-state block, the
   delivery budget measured in JSON characters (non-ASCII counting once),
   memories as a prefix, the progress rule for over-budget blocks,
   ``memory_offset`` pages and paging loops, and the refused inputs.
4. ``render_notes_text()``: a pinned rendering, the empty forms,
   multi-line memories, instructions fitting exactly, the instructions
   cut and its marker, ``instructions_whole``, the memory prefix and
   trailer counts (singular too), and the pre-card budget under
   randomized pathological blocks.
"""

import json
import random
import uuid

import pytest

from chat.artifact_access import DENY_CAUSES
from chat.docs.access import RUN_KINDS
from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_NOTES_DELIVERY_MAX_CHARS,
    DOC_NOTES_PRECARD_MAX_CHARS,
)
from chat.docs.notes import (
    DOC_NOTES_BLOCK_NOTE,
    RECORDING_RUN_KINDS,
    SLACK_DENY_CAUSES,
    NotesSeen,
    NotesText,
    build_notes_block,
    has_notes,
    needs_delivery,
    records_delivery,
    render_notes_text,
    suggestion_allowed,
    write_refused,
)

NOTE_TEXT = (
    "These are the doc's current notes. Instructions are its owner's rules "
    "for changing it: follow them whenever you change the doc. Memories are "
    "background facts about it, kept by its editors. Propose changes with "
    "suggest_doc_note where it is offered."
)
INSTRUCTIONS_HEADER = "Instructions (follow them whenever you change this doc):"


# ---------------------------------------------------------------------------
# 1. The section 5 grid
# ---------------------------------------------------------------------------

SEEN_STATES = {
    "no_record": None,
    "behind_on_memories": NotesSeen(instructions=3, memories=6),
    "behind_on_instructions": NotesSeen(instructions=2, memories=7),
    "current": NotesSeen(instructions=3, memories=7),
}

# (has instructions, memory count); every fixture has versions 3 / 7.
NOTES_STATES = {
    "no_notes": (False, 0),
    "memories_only": (False, 2),
    "instructions_only": (True, 0),
    "both": (True, 2),
}

NEEDS_DELIVERY = {
    "no_record": {
        "no_notes": False, "memories_only": True,
        "instructions_only": True, "both": True,
    },
    "behind_on_memories": {
        "no_notes": True, "memories_only": True,
        "instructions_only": True, "both": True,
    },
    "behind_on_instructions": {
        "no_notes": True, "memories_only": True,
        "instructions_only": True, "both": True,
    },
    "current": {
        "no_notes": False, "memories_only": False,
        "instructions_only": False, "both": False,
    },
}

WRITE_REFUSED = {
    "no_record": {
        "no_notes": False, "memories_only": True,
        "instructions_only": True, "both": True,
    },
    "behind_on_memories": {
        "no_notes": False, "memories_only": False,
        "instructions_only": False, "both": False,
    },
    "behind_on_instructions": {
        "no_notes": False, "memories_only": False,
        "instructions_only": True, "both": True,
    },
    "current": {
        "no_notes": False, "memories_only": False,
        "instructions_only": False, "both": False,
    },
}

# How a read in each run kind gets the block (spec 6.1): "record" attaches
# it when needs_delivery() holds and records the delivery; "unrecorded"
# attaches it whenever the doc has notes, exactly what a conversation with
# no record gets, never suppressed by a record and recording nothing
# (sub-agents, inference-API runs); "never" attaches it to no read_doc
# (scripts get counts only; the UI has its own routes and no tool reads). A
# "record" run kind without a conversation reads like "unrecorded".
DELIVERY_MODES = {
    "top_level": "record",
    "slack": "record",
    "user_subagent": "record",
    "sub_agent": "unrecorded",
    "inference_api": "unrecorded",
    "script": "never",
    "ui": "never",
}

# records_delivery(run_kind, "conv-1").
RECORDS_DELIVERY = {
    "top_level": True,
    "slack": True,
    "user_subagent": True,
    "sub_agent": False,
    "script": False,
    "inference_api": False,
    "ui": False,
}

INSTRUCTIONS_TEXT = "Newest entry first.\nNever change the Risks section."


def _doc_dict(has_instructions: bool, memory_count: int) -> dict:
    """A db/doc_store.py doc dict: notes keys, never the instructions text."""
    return {
        "id": "doc-1",
        "owner_id": 1,
        "project_id": None,
        "title": "Plan",
        "has_instructions": has_instructions,
        "memory_count": memory_count,
        "instructions_version": 3,
        "memories_version": 7,
    }


def _notes_state(has_instructions: bool, memory_count: int) -> dict:
    """A get_notes_state() dict: the text and the doc-dict keys."""
    return {
        "doc_id": "doc-1",
        "instructions": INSTRUCTIONS_TEXT if has_instructions else "",
        "has_instructions": has_instructions,
        "instructions_version": 3,
        "instructions_updated_at": None,
        "instructions_updated_by": None,
        "memories_version": 7,
        "memory_count": memory_count,
    }


def _text_only(has_instructions: bool, memory_count: int) -> dict:
    """The instructions text and no has_instructions key."""
    return {
        "instructions": INSTRUCTIONS_TEXT if has_instructions else "",
        "memory_count": memory_count,
        "instructions_version": 3,
        "memories_version": 7,
    }


SHAPES = {
    "doc_dict": _doc_dict,
    "notes_state": _notes_state,
    "text_only": _text_only,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("notes_name", list(NOTES_STATES))
@pytest.mark.parametrize("seen_name", list(SEEN_STATES))
def test_needs_delivery_and_write_refused_grid(seen_name, notes_name, shape):
    doc = SHAPES[shape](*NOTES_STATES[notes_name])
    seen = SEEN_STATES[seen_name]

    assert needs_delivery(doc, seen) is NEEDS_DELIVERY[seen_name][notes_name]
    assert write_refused(doc, seen) is WRITE_REFUSED[seen_name][notes_name]


@pytest.mark.parametrize("conversation_id", ["conv-1", None])
@pytest.mark.parametrize("run_kind", RUN_KINDS)
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("notes_name", list(NOTES_STATES))
@pytest.mark.parametrize("seen_name", list(SEEN_STATES))
def test_phase_a3_decisions_grid(seen_name, notes_name, shape, run_kind, conversation_id):
    """How phase A3 combines the gate: in a recording run a read attaches
    the block when the notes need delivery and a write is refused when
    write_refused() holds; outside one, a read attaches it exactly when it
    would without a seen record (the doc has notes), whatever the record
    says, except for the "never" run kinds, and no write is refused."""
    doc = SHAPES[shape](*NOTES_STATES[notes_name])
    seen = SEEN_STATES[seen_name]
    mode = DELIVERY_MODES[run_kind]
    records = mode == "record" and conversation_id is not None

    if records:
        expected_attach = NEEDS_DELIVERY[seen_name][notes_name]
        expected_refused = WRITE_REFUSED[seen_name][notes_name]
    elif mode == "never":
        expected_attach = False
        expected_refused = False
    else:
        expected_attach = NEEDS_DELIVERY["no_record"][notes_name]
        expected_refused = False

    recording = records_delivery(run_kind, conversation_id)
    assert recording is records
    if mode == "never":
        attach = False
    elif recording:
        attach = needs_delivery(doc, seen)
    else:
        attach = has_notes(doc)
    refused = recording and write_refused(doc, seen)
    assert attach is expected_attach
    assert refused is expected_refused


def test_records_delivery_table_covers_every_run_kind():
    assert set(RECORDS_DELIVERY) == set(RUN_KINDS)
    assert set(DELIVERY_MODES) == set(RUN_KINDS)
    assert RECORDING_RUN_KINDS == {k for k, v in RECORDS_DELIVERY.items() if v}
    assert RECORDING_RUN_KINDS == {k for k, m in DELIVERY_MODES.items() if m == "record"}


@pytest.mark.parametrize("run_kind", RUN_KINDS)
def test_records_delivery_with_a_conversation(run_kind):
    assert records_delivery(run_kind, "conv-1") is RECORDS_DELIVERY[run_kind]


@pytest.mark.parametrize("conversation_id", [None, ""])
@pytest.mark.parametrize("run_kind", RUN_KINDS)
def test_records_delivery_without_a_conversation(run_kind, conversation_id):
    assert records_delivery(run_kind, conversation_id) is False


# ---------------------------------------------------------------------------
# 2. Gate edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("notes_name", list(NOTES_STATES))
def test_seen_record_ahead_of_the_doc_counts_as_current(notes_name, shape):
    doc = SHAPES[shape](*NOTES_STATES[notes_name])
    ahead = NotesSeen(instructions=5, memories=9)
    assert needs_delivery(doc, ahead) is False
    assert write_refused(doc, ahead) is False


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_doc_that_never_had_notes(shape):
    doc = SHAPES[shape](False, 0)
    doc["instructions_version"] = 0
    doc["memories_version"] = 0
    assert has_notes(doc) is False
    assert needs_delivery(doc, None) is False
    assert write_refused(doc, None) is False
    assert needs_delivery(doc, NotesSeen(0, 0)) is False
    assert write_refused(doc, NotesSeen(0, 0)) is False
    # A recording run reads without the block and writes freely.
    assert records_delivery("top_level", "conv-1") is True


@pytest.mark.parametrize("notes_name", list(NOTES_STATES))
def test_has_notes_per_notes_state(notes_name):
    expected = {
        "no_notes": False, "memories_only": True,
        "instructions_only": True, "both": True,
    }[notes_name]
    for make in SHAPES.values():
        assert has_notes(make(*NOTES_STATES[notes_name])) is expected


def test_instructions_text_wins_over_has_instructions():
    doc = _notes_state(False, 0)
    doc["instructions"] = "Keep it short."
    assert has_notes(doc) is True
    doc = _notes_state(True, 0)
    doc["instructions"] = ""
    assert has_notes(doc) is False
    assert write_refused(doc, NotesSeen(2, 7)) is False


def test_none_instructions_text_means_none():
    doc = _text_only(False, 0)
    doc["instructions"] = None
    assert has_notes(doc) is False
    assert needs_delivery(doc, None) is False


def test_control_characters_count_as_instructions():
    # normalize_instructions() refuses only NUL.
    doc = _text_only(False, 0)
    doc["instructions"] = "\x01"
    assert has_notes(doc) is True
    assert write_refused(doc, None) is True
    assert write_refused(doc, NotesSeen(2, 7)) is True


def test_dict_without_either_instructions_key_raises():
    doc = {"memory_count": 2, "instructions_version": 3, "memories_version": 7}
    for call in (
        lambda: has_notes(doc),
        lambda: needs_delivery(doc, None),
        lambda: needs_delivery(doc, NotesSeen(3, 7)),
        lambda: write_refused(doc, None),
        lambda: write_refused(doc, NotesSeen(3, 7)),
    ):
        with pytest.raises(ValueError, match="instructions"):
            call()


@pytest.mark.parametrize("missing", ["memory_count", "instructions_version", "memories_version"])
def test_missing_counter_raises(missing):
    doc = _doc_dict(True, 2)
    del doc[missing]
    with pytest.raises(KeyError):
        needs_delivery(doc, NotesSeen(3, 7))
    if missing != "memories_version":
        with pytest.raises(KeyError):
            write_refused(doc, NotesSeen(3, 7))


@pytest.mark.parametrize("conversation_id", ["c", None])
def test_records_delivery_refuses_an_unknown_run_kind(conversation_id):
    with pytest.raises(ValueError, match="bogus"):
        records_delivery("bogus", conversation_id)


@pytest.mark.parametrize("conversation_id", [uuid.UUID(int=1), 7, b"conv-1"])
@pytest.mark.parametrize("run_kind", RUN_KINDS)
def test_records_delivery_refuses_a_non_str_conversation_id(run_kind, conversation_id):
    # Silently not recording would also skip the write refusal.
    with pytest.raises(TypeError):
        records_delivery(run_kind, conversation_id)


# The suggestion taint rule (invariant 4): free and approval admit; a denial
# admits only for the two Slack causes. Every deny cause is listed, so a
# cause added to chat/artifact_access.py fails here until it is placed.
_SUGGESTION_ALLOWED_BY_CAUSE = {
    "sub_agent": False,
    "inference_api": False,
    "user_subagent": False,
    "script": False,
    "read_only_share": False,
    "public_from_private": False,
    "outside_project": False,
    "slack_needs_approval": True,
    "slack_approval_required": True,
    "ui_read_only": False,
}


def test_suggestion_allowed_table_covers_every_deny_cause():
    assert set(_SUGGESTION_ALLOWED_BY_CAUSE) == set(DENY_CAUSES)
    assert SLACK_DENY_CAUSES == {
        cause for cause, allowed in _SUGGESTION_ALLOWED_BY_CAUSE.items() if allowed
    }


@pytest.mark.parametrize("cause", sorted(_SUGGESTION_ALLOWED_BY_CAUSE))
def test_suggestion_allowed_for_a_denied_verdict(cause):
    assert suggestion_allowed("denied", cause) is _SUGGESTION_ALLOWED_BY_CAUSE[cause]


@pytest.mark.parametrize("write", ["free", "approval"])
def test_suggestion_allowed_for_free_and_approval(write):
    assert suggestion_allowed(write, None) is True


def test_suggestion_refused_for_a_denial_without_a_cause():
    assert suggestion_allowed("denied", None) is False


# ---------------------------------------------------------------------------
# 3. build_notes_block()
# ---------------------------------------------------------------------------


def _state(instructions: str | None = "Keep it short.", version: int = 3) -> dict:
    return {
        "doc_id": "doc-1",
        "instructions": instructions,
        "has_instructions": bool(instructions),
        "instructions_version": version,
        "instructions_updated_at": None,
        "instructions_updated_by": None,
        "memories_version": 7,
        "memory_count": 0,
    }


def _memory(index: int, content: str) -> dict:
    """A list_doc_memories() row."""
    return {
        "id": f"mem-{index:03d}",
        "doc_id": "doc-1",
        "content": content,
        "source": "user:1",
        "created_by": 1,
        "updated_by": None,
        "created_at": "2026-10-29T00:00:00",
        "updated_at": "2026-10-29T00:00:00",
    }


def _memories(count: int, length: int = 1000, char: str = "x") -> list[dict]:
    """``count`` memories, newest first, each ``length`` characters."""
    return [_memory(i, f"{i:03d}" + char * (length - 3)) for i in range(count)]


def _size(block: dict) -> int:
    return len(json.dumps(block, ensure_ascii=False))


def _with_next(block: dict, memory: dict) -> dict:
    """``block`` with one more memory taken from the omitted ones."""
    return {
        **block,
        "memories": block["memories"] + [{"id": memory["id"], "content": memory["content"]}],
        "memories_omitted": block["memories_omitted"] - 1,
    }


def test_block_keys_order_and_note():
    block = build_notes_block(_state(), _memories(2, 20))
    assert list(block) == [
        "instructions", "instructions_version", "memories", "memories_omitted", "note",
    ]
    assert block["note"] == NOTE_TEXT
    assert DOC_NOTES_BLOCK_NOTE == NOTE_TEXT


def test_block_memory_items_carry_only_id_and_content():
    memories = _memories(3, 20)
    block = build_notes_block(_state("Rule.", version=42), memories)
    assert block["instructions"] == "Rule."
    assert block["instructions_version"] == 42
    assert block["memories"] == [{"id": m["id"], "content": m["content"]} for m in memories]
    assert all(list(item) == ["id", "content"] for item in block["memories"])
    assert block["memories_omitted"] == 0


def test_block_with_zero_memories():
    block = build_notes_block(_state("Rule."), [])
    assert block["memories"] == []
    assert block["memories_omitted"] == 0


@pytest.mark.parametrize("instructions", ["", None])
def test_block_after_everything_was_cleared(instructions):
    block = build_notes_block(_state(instructions, version=5), [])
    assert block == {
        "instructions": "",
        "instructions_version": 5,
        "memories": [],
        "memories_omitted": 0,
        "note": NOTE_TEXT,
    }


def test_block_with_doc_dict_raises():
    with pytest.raises(ValueError, match="get_notes_state"):
        build_notes_block(_doc_dict(True, 2), _memories(2, 20))


@pytest.mark.parametrize("offset", [-1, True, False, 1.0, "1", None])
def test_block_refuses_a_bad_offset(offset):
    with pytest.raises(ValueError, match="memory_offset"):
        build_notes_block(_state(), _memories(2, 20), memory_offset=offset)


def test_block_budget_with_many_full_memories():
    memories = _memories(30)
    block = build_notes_block(_state(), memories)
    shown = len(block["memories"])
    assert 0 < shown < 30
    assert block["memories"][0]["id"] == "mem-000"
    assert block["memories_omitted"] == 30 - shown
    assert _size(block) <= DOC_NOTES_DELIVERY_MAX_CHARS
    assert _size(_with_next(block, memories[shown])) > DOC_NOTES_DELIVERY_MAX_CHARS


def test_block_budget_with_max_instructions():
    memories = _memories(30)
    block = build_notes_block(_state("i" * 4000), memories)
    shown = len(block["memories"])
    assert block["instructions"] == "i" * 4000
    assert 0 < shown < 30
    assert block["memories_omitted"] == 30 - shown
    assert _size(block) <= DOC_NOTES_DELIVERY_MAX_CHARS
    assert _size(_with_next(block, memories[shown])) > DOC_NOTES_DELIVERY_MAX_CHARS


def test_block_memories_are_a_prefix():
    """A memory that does not fit stops the page even when an older,
    smaller one would fit after it."""
    probe = build_notes_block(_state("i" * 4000), _memories(30))
    shown = len(probe["memories"])
    memories = _memories(shown + 1) + [_memory(999, "z")]
    block = build_notes_block(_state("i" * 4000), memories)
    assert [m["id"] for m in block["memories"]] == [m["id"] for m in memories[:shown]]
    assert block["memories_omitted"] == 2
    # Skipping the big one would have fit the small one.
    skipped = {**block, "memories": block["memories"] + [{"id": "mem-999", "content": "z"}]}
    assert _size(skipped) <= DOC_NOTES_DELIVERY_MAX_CHARS


@pytest.mark.parametrize("instructions", ["\x01" * 4000, "y" * 20000])
def test_block_progress_rule_with_instructions_over_budget(instructions):
    memories = _memories(5, 50)
    block = build_notes_block(_state(instructions), memories)
    assert block["instructions"] == instructions
    assert _size({**block, "memories": [], "memories_omitted": 5}) > DOC_NOTES_DELIVERY_MAX_CHARS
    assert block["memories"] == [{"id": "mem-000", "content": memories[0]["content"]}]
    assert block["memories_omitted"] == 4


def test_block_over_budget_instructions_and_no_memories():
    block = build_notes_block(_state("y" * 20000), [])
    assert block["memories"] == []
    assert block["memories_omitted"] == 0


def test_block_counts_non_ascii_once():
    ascii_block = build_notes_block(_state(), _memories(30, char="x"))
    for char in ("é", "\U0001f600"):
        block = build_notes_block(_state(), _memories(30, char=char))
        assert len(block["memories"]) == len(ascii_block["memories"])
        assert _size(block) <= DOC_NOTES_DELIVERY_MAX_CHARS
        # ASCII escaping would have blown the budget.
        assert len(json.dumps(block)) > DOC_NOTES_DELIVERY_MAX_CHARS


def test_block_counts_escapes():
    # Each control character takes six JSON characters, so pages hold one.
    block = build_notes_block(_state(), _memories(5, char="\x01"))
    assert len(block["memories"]) == 1
    assert block["memories_omitted"] == 4
    assert _size(block) <= DOC_NOTES_DELIVERY_MAX_CHARS


def test_block_offsets():
    memories = _memories(30)
    first = build_notes_block(_state(), memories, memory_offset=0)
    assert first["memories"][0]["id"] == "mem-000"

    middle = build_notes_block(_state(), memories, memory_offset=15)
    shown = len(middle["memories"])
    assert middle["memories"][0]["id"] == "mem-015"
    assert middle["memories_omitted"] == 30 - 15 - shown
    assert middle["instructions"] == "Keep it short."

    last = build_notes_block(_state(), memories, memory_offset=29)
    assert [m["id"] for m in last["memories"]] == ["mem-029"]
    assert last["memories_omitted"] == 0

    for offset in (30, 31, 100):
        past = build_notes_block(_state(), memories, memory_offset=offset)
        assert past["memories"] == []
        assert past["memories_omitted"] == 0
        assert past["instructions"] == "Keep it short."


def _page_through(state: dict, memories: list[dict]) -> list[list[str]]:
    pages = []
    offset = 0
    for _ in range(len(memories) + 1):
        block = build_notes_block(state, memories, memory_offset=offset)
        assert block["memories_omitted"] == len(memories) - offset - len(block["memories"])
        if not block["memories"]:
            return pages
        pages.append([m["id"] for m in block["memories"]])
        offset += len(block["memories"])
    raise AssertionError("paging did not terminate")


@pytest.mark.parametrize(
    ("instructions", "memories"),
    [
        ("Keep it short.", _memories(30)),
        ("i" * 4000, _memories(50)),
        ("Keep it short.", _memories(50, 20)),
        ("Keep it short.", _memories(7, char="\x01")),
        ("\x01" * 4000, _memories(6, 30)),
        ("y" * 20000, _memories(6)),
    ],
)
def test_block_paging_visits_every_memory_once(instructions, memories):
    pages = _page_through(_state(instructions), memories)
    visited = [memory_id for page in pages for memory_id in page]
    assert visited == [m["id"] for m in memories]


# ---------------------------------------------------------------------------
# 4. render_notes_text()
# ---------------------------------------------------------------------------


def _render(block: dict) -> str:
    """The text of render_notes_text(), checked to be a NotesText."""
    rendered = render_notes_text(block)
    assert isinstance(rendered, NotesText)
    return rendered.text


def _block(instructions: str, contents: list[str], omitted: int = 0) -> dict:
    return {
        "instructions": instructions,
        "instructions_version": 3,
        "memories": [{"id": f"mem-{i:03d}", "content": c} for i, c in enumerate(contents)],
        "memories_omitted": omitted,
        "note": NOTE_TEXT,
    }


def test_render_small_block():
    block = _block(
        "Newest entry first.\nNever change the Risks section.",
        ["The Q3 numbers come from the finance sheet.", "Sections:\nRisks, Plan"],
        omitted=3,
    )
    assert _render(block) == (
        "Instructions (follow them whenever you change this doc):\n"
        "Newest entry first.\n"
        "Never change the Risks section.\n"
        "\n"
        "Memories (newest first):\n"
        "- The Q3 numbers come from the finance sheet.\n"
        "- Sections:\n"
        "  Risks, Plan\n"
        "(3 more memories not shown; list_doc_notes pages them.)"
    )


def test_render_all_memories_has_no_trailer():
    text = _render(_block("Rule.", ["One.", "Two."]))
    assert text == (
        "Instructions (follow them whenever you change this doc):\n"
        "Rule.\n"
        "\n"
        "Memories (newest first):\n"
        "- One.\n"
        "- Two."
    )


def test_render_empty_forms():
    assert _render(_block("", [])) == "Instructions: none.\n\nMemories: none."
    assert _render(_block("", ["Fact."])) == (
        "Instructions: none.\n\nMemories (newest first):\n- Fact."
    )
    assert _render(_block("Rule.", [])) == (
        "Instructions (follow them whenever you change this doc):\nRule.\n\nMemories: none."
    )


def test_render_none_instructions():
    block = _block("", [])
    block["instructions"] = None
    assert _render(block) == "Instructions: none.\n\nMemories: none."


def test_render_leaves_the_note_out():
    assert "suggest_doc_note" not in _render(_block("Rule.", ["Fact."]))


def test_render_multi_line_memory():
    text = _render(_block("", ["first line\nsecond line\n\nfourth", "next"]))
    assert text == (
        "Instructions: none.\n"
        "\n"
        "Memories (newest first):\n"
        "- first line\n"
        "  second line\n"
        "  \n"
        "  fourth\n"
        "- next"
    )


def test_render_singular_trailer():
    assert _render(_block("", [], omitted=1)) == (
        "Instructions: none.\n\n"
        "Memories (newest first):\n"
        "(1 more memory not shown; list_doc_notes pages them.)"
    )
    # The second memory does not fit next to the first.
    half = DOC_NOTES_PRECARD_MAX_CHARS // 2
    text = _render(_block("Rule.", ["a" * half, "b" * half]))
    assert text.endswith("- " + "a" * half + "\n(1 more memory not shown; list_doc_notes pages them.)")
    assert len(text) <= DOC_NOTES_PRECARD_MAX_CHARS


def test_render_instructions_fit_exactly():
    tail = "\n\nMemories: none."
    length = DOC_NOTES_PRECARD_MAX_CHARS - len(INSTRUCTIONS_HEADER + "\n") - len(tail)
    instructions = "i" * length
    text = _render(_block(instructions, []))
    assert text == INSTRUCTIONS_HEADER + "\n" + instructions + tail
    assert len(text) == DOC_NOTES_PRECARD_MAX_CHARS

    over = _render(_block(instructions + "i", []))
    assert "[Instructions cut here: " in over
    assert len(over) <= DOC_NOTES_PRECARD_MAX_CHARS


def test_render_instructions_cut():
    block = _block("a" * 5000, ["One.", "Two.", "Three."], omitted=2)
    text = _render(block)
    assert text == (
        INSTRUCTIONS_HEADER + "\n"
        + "a" * 3977 + "\n"
        "[Instructions cut here: 1023 more characters. "
        "list_doc_notes returns them in full.]\n"
        "\n"
        "Memories (newest first):\n"
        "(5 more memories not shown; list_doc_notes pages them.)"
    )
    assert len(text) == DOC_NOTES_PRECARD_MAX_CHARS


def test_render_instructions_cut_without_memories():
    text = _render(_block("a" * 5000, []))
    kept = text.split("\n")[1]
    assert set(kept) == {"a"}
    assert text.endswith(
        f"\n[Instructions cut here: {5000 - len(kept)} more characters. "
        "list_doc_notes returns them in full.]\n\nMemories: none."
    )
    assert len(text) <= DOC_NOTES_PRECARD_MAX_CHARS


# The longest instructions that come whole beside "Memories: none.".
_WHOLE_MAX = (
    DOC_NOTES_PRECARD_MAX_CHARS - len(INSTRUCTIONS_HEADER + "\n") - len("\n\nMemories: none.")
)


@pytest.mark.parametrize(
    "instructions, contents, whole",
    [
        pytest.param("", [], True, id="empty"),
        pytest.param("", ["Fact."] * 3, True, id="empty-with-memories"),
        pytest.param("Rule.", ["Fact."], True, id="short"),
        pytest.param("i" * _WHOLE_MAX, [], True, id="longest-whole"),
        pytest.param("i" * (_WHOLE_MAX + 1), [], False, id="one-over"),
        pytest.param("a" * 5000, ["One."], False, id="over-the-field-cap"),
    ],
)
def test_render_reports_whether_the_instructions_came_whole(instructions, contents, whole):
    rendered = render_notes_text(_block(instructions, contents))
    assert rendered.instructions_whole is whole
    assert ("[Instructions cut here: " in rendered.text) is not whole


def test_render_max_size_instructions_come_whole_without_memories():
    instructions = "i" * DOC_INSTRUCTIONS_MAX_CHARS
    rendered = render_notes_text(_block(instructions, []))
    assert rendered.instructions_whole is True
    assert rendered.text == INSTRUCTIONS_HEADER + "\n" + instructions + "\n\nMemories: none."
    assert len(rendered.text) <= DOC_NOTES_PRECARD_MAX_CHARS


@pytest.mark.parametrize("hidden", range(DOC_MAX_MEMORIES + 1))
def test_render_max_size_instructions_come_whole_with_any_memory_count(hidden):
    """The cut path is unreachable for instructions within the field cap:
    the widest trailer (every memory of a full doc left out) still fits."""
    instructions = "\n" * DOC_INSTRUCTIONS_MAX_CHARS
    rendered = render_notes_text(_block(instructions, [], omitted=hidden))
    assert rendered.instructions_whole is True
    assert rendered.text.startswith(INSTRUCTIONS_HEADER + "\n" + instructions + "\n\n")
    assert len(rendered.text) <= DOC_NOTES_PRECARD_MAX_CHARS


def test_render_max_caps_block_from_build_notes_block_is_whole():
    block = build_notes_block(_state("i" * DOC_INSTRUCTIONS_MAX_CHARS), _memories(DOC_MAX_MEMORIES))
    assert render_notes_text(block).instructions_whole is True


def test_render_max_caps_block_counts_every_memory():
    memories = _memories(50)
    block = build_notes_block(_state("i" * 4000), memories)
    assert block["memories"]
    text = _render(block)
    assert "\n- " not in text
    assert text.endswith("(50 more memories not shown; list_doc_notes pages them.)")
    assert len(text) <= DOC_NOTES_PRECARD_MAX_CHARS


def test_render_memories_prefix_and_trailer():
    contents = [f"{i}" + "m" * 899 for i in range(10)]
    text = _render(_block("Keep it short.", contents, omitted=4))
    assert text == (
        INSTRUCTIONS_HEADER + "\n"
        "Keep it short.\n"
        "\n"
        "Memories (newest first):\n"
        + "".join(f"- {c}\n" for c in contents[:4])
        + "(10 more memories not shown; list_doc_notes pages them.)"
    )


def test_render_last_memory_replaces_the_trailer():
    """Rendering the only memory drops the trailer, so it fits where the
    trailer alone would not, and the instructions stay whole."""
    tail = "\n\nMemories (newest first):\n- x"
    length = DOC_NOTES_PRECARD_MAX_CHARS - len(INSTRUCTIONS_HEADER + "\n") - len(tail)
    instructions = "i" * length
    text = _render(_block(instructions, ["x"]))
    assert text == INSTRUCTIONS_HEADER + "\n" + instructions + tail
    trailer_tail = "\n\nMemories (newest first):\n(1 more memory not shown; list_doc_notes pages them.)"
    assert len(INSTRUCTIONS_HEADER + "\n" + instructions + trailer_tail) > DOC_NOTES_PRECARD_MAX_CHARS


_ALPHABET = "abc xyz-()[]:\n\n\t\x01\x1fé\U0001f600"


def _random_text(rng: random.Random, length: int) -> str:
    return "".join(rng.choices(_ALPHABET, k=length))


def test_render_stays_within_budget_for_pathological_blocks():
    rng = random.Random(187)
    for _ in range(400):
        instructions = _random_text(
            rng, rng.choice([0, 1, 50, 2900, 3000, 4000, 4074, 4126, 4127, 4140, 4200, 20000, 100000])
        )
        count = rng.choice([0, 1, 2, 5, 50, 1000, 3000])
        if count <= 50:
            contents = [_random_text(rng, rng.choice([0, 1, 3, 100, 999, 1000, 1500, 2999]))
                        for _ in range(count)]
        else:
            contents = [_random_text(rng, rng.randint(0, 20)) for _ in range(count)]
        if count <= 50 and rng.random() < 0.5:
            memories = [_memory(i, c) for i, c in enumerate(contents)]
            block = build_notes_block(
                _state(instructions), memories, memory_offset=rng.randint(0, count)
            )
        else:
            block = _block(
                instructions, contents,
                omitted=rng.choice([0, 1, 2, 9, 10, 99_999, 10**9]),
            )
        text = _render(block)
        assert len(text) <= DOC_NOTES_PRECARD_MAX_CHARS
        assert text.startswith("Instructions")
        assert not text.endswith("\n")
