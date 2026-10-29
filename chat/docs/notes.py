"""Doc notes gate: who must be shown a doc's notes, and the notes block.

Pure functions over the notes state of a doc (docs/architecture/doc-notes.md)
and the per-conversation seen record (``ChatStorage.get_doc_notes_seen()``).
No I/O here: callers load the state and the record, decide with these
functions, and write the record back.

The module owns:

- The gate: :func:`has_notes`, :func:`needs_delivery`,
  :func:`write_refused` and :func:`records_delivery` (with
  :data:`RECORDING_RUN_KINDS`). The first three take either a doc dict
  from ``db/doc_store.py`` (``has_instructions``, never the text) or the
  ``get_notes_state()`` dict (the ``instructions`` text too).
- The taint rule of suggestions (invariant 4):
  :func:`suggestion_allowed` over a doc's write verdict, with
  :data:`SLACK_DENY_CAUSES`. ``register_suggestion()``
  (chat/docs/note_suggestions.py) refuses with it, and ``list_doc_notes``
  derives its ``can_suggest`` from it.
- The notes block (:func:`build_notes_block`): the ``doc_notes`` object a
  tool result carries, with the whole instructions and as many memories,
  newest first, as fit in ``DOC_NOTES_DELIVERY_MAX_CHARS``.
- Its text copy (:func:`render_notes_text`) for the ``write_doc`` pre-card
  refusal, within ``DOC_NOTES_PRECARD_MAX_CHARS``, which also says whether
  the instructions came whole, and the copy strings both use, gathered
  below so they can be reviewed in one place, together with the write
  refusal's message (:data:`DOC_NOTES_UNSEEN_MESSAGE`) and the approve-time
  instructions-changed error (:data:`DOC_INSTRUCTIONS_CHANGED_MESSAGE`),
  the one string a person reads rather than the model.
- The ``suggest_doc_note`` copy (the ``DOC_NOTE_*`` constants): the
  ``message`` of each result status and the text of each refusal that
  chat/docs/note_suggestions.py returns to the model, and the dispatch
  backstop's refusal for the run kinds that may not suggest
  (:data:`DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE`).

Callers load ``get_notes_state()`` BEFORE ``list_doc_memories()``: a memory
change between the two reads then shows the model memories at least as new
as the ``memories_version`` it records, never older.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from chat.docs.access import RUN_KINDS
from chat.docs.constants import (
    DOC_NOTES_DELIVERY_MAX_CHARS,
    DOC_NOTES_PRECARD_MAX_CHARS,
)


@dataclass(frozen=True)
class NotesSeen:
    """One doc's entry in a conversation's seen record: the instructions and
    memories versions this conversation was last shown."""

    instructions: int
    memories: int


# Run kinds whose deliveries update the conversation's seen record, and so
# the only ones whose writes can be refused for unseen notes. ``top_level``
# includes routine runs (ordinary conversations).
RECORDING_RUN_KINDS = frozenset({"top_level", "slack", "user_subagent"})

# Write verdict causes that deny a write only because Slack cannot open an
# approval card (access rule 6, and the owner's require-approval switch,
# rule 7): the person could write from the web UI, and a suggestion already
# waits for a person, so these still admit a suggestion
# (:func:`suggestion_allowed`).
SLACK_DENY_CAUSES = frozenset({"slack_needs_approval", "slack_approval_required"})

# --- Copy (the model reads every string below but
# DOC_INSTRUCTIONS_CHANGED_MESSAGE, which the approver reads) ---
#
# The block ``note`` and the two unseen-notes messages stand alone: they
# reach models that never loaded the system:doc_notes skill (public
# conversations cannot load it) and runs that are not offered
# suggest_doc_note (routine runs, sub-agents, cross-user subagents,
# inference API runs; scripts read the block through list_doc_notes). So
# they name no skill, and name suggest_doc_note only where the reader is
# sure to have it or with "where it is offered".

# The ``note`` of every notes block. Short: it rides on every delivery and
# counts against DOC_NOTES_DELIVERY_MAX_CHARS. "Current" tells a model that
# an emptied block means a rule or fact it saw before is gone.
DOC_NOTES_BLOCK_NOTE = (
    "These are the doc's current notes. Instructions are its owner's rules "
    "for changing it: follow them whenever you change the doc. Memories are "
    "background facts about it, kept by its editors. Propose changes with "
    "suggest_doc_note where it is offered."
)

# render_notes_text(): the text copy of a block.
NOTES_TEXT_INSTRUCTIONS_HEADER = "Instructions (follow them whenever you change this doc):"
NOTES_TEXT_NO_INSTRUCTIONS = "Instructions: none."
# A cut always removes more characters than this marker line holds (80+),
# so the count is never 1 and needs no singular form.
NOTES_TEXT_INSTRUCTIONS_CUT = (
    "[Instructions cut here: {count} more characters. "
    "list_doc_notes returns them in full.]"
)
NOTES_TEXT_MEMORIES_HEADER = "Memories (newest first):"
NOTES_TEXT_NO_MEMORIES = "Memories: none."
NOTES_TEXT_MEMORY_BULLET = "- "
NOTES_TEXT_MEMORY_INDENT = "  "
NOTES_TEXT_ONE_MORE_MEMORY = "(1 more memory not shown; list_doc_notes pages them.)"
NOTES_TEXT_MORE_MEMORIES = "({count} more memories not shown; list_doc_notes pages them.)"

# The write refusal (``DocNotesUnseen`` in chat/docs/service.py): the
# ``message`` of a ``doc_notes_unseen`` tool result, and the first line of
# the write_doc pre-card's ``Invalid parameters`` text (the text copy of
# the block follows it). It says the retry passes, since the refusal
# recorded the delivery, so a model neither loops on reads nor gives up.
DOC_NOTES_UNSEEN_MESSAGE = (
    "Doc '{title}' has notes you have not seen; they are below. Adjust the "
    "change if they require it, then repeat the call: it will not be "
    "refused for these notes again."
)

# Approve-time execute of a write_doc card whose pinned instructions version
# is behind the doc's (the request stays open). Read by the person who
# clicked Approve, after the approve route's "Failed to execute request: "
# (the card and the inbox show it; the model never sees it), so it names
# the card's Revise button, whose feedback sends the model back to propose
# again, rather than telling the reader to propose anything.
DOC_INSTRUCTIONS_CHANGED_MESSAGE = (
    "The doc's instructions changed after this change was proposed. Use "
    "Revise to ask the agent to propose it again under the current "
    "instructions."
)

# suggest_doc_note (chat/docs/note_suggestions.py). Result messages: a
# registered suggestion (the second when it replaced the user's earlier
# pending instructions suggestion for the doc), and the two guards that
# answer with a status instead of an error.
DOC_NOTE_SUGGESTED_MESSAGE = (
    "Registered for the user's review in the doc note suggestions panel; "
    "nothing is saved until they accept it. Keep going."
)
DOC_NOTE_SUGGESTED_SUPERSEDED_MESSAGE = (
    "Registered for the user's review in the doc note suggestions panel, in "
    "place of the earlier instructions suggestion for this doc that was "
    "still waiting for their review; nothing is saved until they accept it. "
    "Keep going."
)
DOC_NOTE_ALREADY_SAVED_MEMORY_MESSAGE = (
    "This doc already has a memory with exactly this text; nothing to suggest."
)
DOC_NOTE_ALREADY_SAVED_INSTRUCTIONS_MESSAGE = (
    "The doc's instructions already read exactly like this; nothing to suggest."
)
DOC_NOTE_ALREADY_SUGGESTED_MESSAGE = (
    "A suggestion with exactly this text is already waiting for the user's "
    "review; do not suggest it again."
)

# suggest_doc_note refusals. The unseen-notes message is the ``message`` of
# the ``doc_notes_unseen`` result, followed by the notes block; an
# instructions suggestion made blind must be rebuilt on the current text.
DOC_NOTE_SUGGEST_UNSEEN_MESSAGE = (
    "Doc '{title}' has notes you have not seen; they are below. If they do "
    "not already say it, suggest the change again (for instructions, the "
    "current text with your change applied): it will not be refused for "
    "these notes again."
)
DOC_NOTE_INSTRUCTIONS_OWNER_ONLY_MESSAGE = (
    "Only the doc's owner can change its instructions, and this conversation's "
    "user is not the owner. Put the proposed rule in your reply so the user "
    "can pass it to the owner."
)
DOC_NOTE_MEMORY_NOT_FOUND_MESSAGE = (
    "Memory '{memory_id}' is not a memory of this doc. Call list_doc_notes for "
    "the ids of its memories."
)
DOC_NOTE_EMPTY_INSTRUCTIONS_MESSAGE = (
    "An instructions suggestion is the complete new instructions text, so it "
    "cannot be empty. Only the user can clear the instructions, in the doc's "
    "Notes panel."
)
DOC_NOTE_CONVERSATION_CAP_MESSAGE = (
    "This conversation already has {count} doc note suggestions waiting for "
    "the user's review. Do not suggest more until they have reviewed them."
)
DOC_NOTE_DOC_CAP_MESSAGE = (
    "The user already has {count} note suggestions for this doc waiting for "
    "their review. Do not suggest more for it until they have reviewed them."
)
DOC_NOTE_MEMORIES_CAP_MESSAGE = (
    "This doc already has the maximum of {max_memories} memories. Suggest "
    "replacing an outdated one with replaces_memory_id instead."
)
DOC_NOTE_INVALID_KIND_MESSAGE = 'kind must be "instructions" or "memory".'
DOC_NOTE_INVALID_CONTENT_MESSAGE = "content must be a string."
DOC_NOTE_INVALID_REASONING_MESSAGE = "reasoning must be a string."
DOC_NOTE_INVALID_REPLACES_MESSAGE = (
    "replaces_memory_id must be the id of one of the doc's memories, a string."
)
DOC_NOTE_REPLACES_WITH_INSTRUCTIONS_MESSAGE = (
    "replaces_memory_id applies to memory suggestions only; an instructions "
    "suggestion carries the complete new text of the instructions."
)

# The ``suggest_not_available`` refusal of the suggest_doc_note dispatch
# backstop (chat/gemini_api/tool_dispatch.py): a run kind with no
# conversation of the user's own to review the suggestion in.
DOC_NOTE_SUGGEST_NOT_AVAILABLE_MESSAGE = (
    "suggest_doc_note is only available to the top-level agent of a user's "
    "own conversation, not to sub-agents, cross-user subagents, inference "
    "API runs, sandbox scripts or routine runs. Include the proposed note in "
    "your response instead."
)


# --- Gate ---


def _has_instructions(doc: Mapping[str, Any]) -> bool:
    """Whether ``doc`` has non-empty instructions, from the text when the
    dict carries it, else from ``has_instructions``.

    Raises:
        ValueError: the dict has neither key (never guess "none").
    """
    if "instructions" in doc:
        return (doc["instructions"] or "") != ""
    if "has_instructions" in doc:
        return bool(doc["has_instructions"])
    raise ValueError(
        "Doc notes state needs 'instructions' or 'has_instructions'; "
        "pass a doc dict or the get_notes_state() dict."
    )


def has_notes(doc: Mapping[str, Any]) -> bool:
    """Non-empty instructions or at least one memory."""
    has_instructions = _has_instructions(doc)
    memory_count = doc["memory_count"]
    return has_instructions or memory_count > 0


def needs_delivery(doc: Mapping[str, Any], seen: NotesSeen | None) -> bool:
    """Whether a read must carry the notes block in a recording run.

    With no seen record, exactly when the doc has notes. With one, when
    either version moved past it, also when the change emptied the notes
    (the block then shows the current state, so an old rule is dropped). A
    record ahead of the doc counts as current.
    """
    notes = has_notes(doc)
    instructions_version = doc["instructions_version"]
    memories_version = doc["memories_version"]
    if seen is None:
        return notes
    return seen.instructions < instructions_version or seen.memories < memories_version


def write_refused(doc: Mapping[str, Any], seen: NotesSeen | None) -> bool:
    """Whether a model write must be refused in a recording run until the
    notes are delivered (invariant 1, notes before writes).

    With no seen record, exactly when the doc has notes. With one, only
    when the instructions moved past it and are non-empty: a memory change
    or cleared instructions never block a write. A record ahead of the doc
    counts as current.
    """
    notes = has_notes(doc)
    instructions_version = doc["instructions_version"]
    if seen is None:
        return notes
    return seen.instructions < instructions_version and _has_instructions(doc)


def records_delivery(run_kind: str, conversation_id: str | None) -> bool:
    """Whether deliveries in this run update its conversation's seen record.

    True only for :data:`RECORDING_RUN_KINDS` with a non-empty
    ``conversation_id``. The other run kinds never record:

    - ``sub_agent`` shares its parent's ``conversation_id``, but the parent
      model never sees what the sub-agent read (invariant 2).
    - ``script`` has no conversation of its own; its reads go to code.
    - ``inference_api`` gets a fresh conversation per call, so there is
      nothing to remember: it always gets the block and writes no record.
    - ``ui`` is a person in the web UI, not a model run: there is no model
      context to record.

    Raises:
        ValueError: ``run_kind`` is not one of ``RUN_KINDS``. A run kind
            added later must be placed here explicitly; silently not
            recording would also skip the write refusal.
        TypeError: ``conversation_id`` is neither ``None`` nor a ``str``
            (e.g. a ``UUID``), for the same reason.
    """
    if run_kind not in RUN_KINDS:
        raise ValueError(f"Unknown run kind: {run_kind!r}")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TypeError(
            f"conversation_id must be a str or None, got {type(conversation_id).__name__}"
        )
    return run_kind in RECORDING_RUN_KINDS and bool(conversation_id)


def suggestion_allowed(write: str, deny_cause: str | None) -> bool:
    """Whether a conversation with this write verdict on a doc may suggest a
    change to its notes (invariant 4, taint).

    True for a ``free`` or ``approval`` verdict, and for ``denied`` with one
    of :data:`SLACK_DENY_CAUSES`; every other denial refuses, so a private
    conversation can never put text into a public doc's notes. Run kinds
    are not judged here: the read-only ones are denied by their verdict,
    and the suggest_doc_note dispatch backstop keeps the rest out. Instructions
    ownership is a separate check.

    Args:
        write: ``DocAccess.write`` (``"free"``, ``"approval"`` or
            ``"denied"``).
        deny_cause: ``DocAccess.deny_cause`` (None unless denied).
    """
    if write in ("free", "approval"):
        return True
    return write == "denied" and deny_cause in SLACK_DENY_CAUSES


# --- Notes block ---


def _json_len(value: Any) -> int:
    """Characters of ``value``'s JSON text: escapes included, a non-ASCII
    character counting once."""
    return len(json.dumps(value, ensure_ascii=False))


def _check_offset(memory_offset: Any) -> None:
    if (
        isinstance(memory_offset, bool)
        or not isinstance(memory_offset, int)
        or memory_offset < 0
    ):
        raise ValueError("memory_offset must be a non-negative integer.")


def build_notes_block(
    doc: Mapping[str, Any],
    memories: Sequence[Mapping[str, Any]],
    memory_offset: int = 0,
) -> dict[str, Any]:
    """The notes block: the value of a tool result's ``doc_notes`` key.

    Keys, in order: ``instructions`` (whole, ``""`` for none),
    ``instructions_version``, ``memories`` (``{"id", "content"}`` items),
    ``memories_omitted`` and ``note`` (:data:`DOC_NOTES_BLOCK_NOTE`). It
    always shows the current state, whatever the seen record says: cleared
    instructions give ``""`` and no memories give ``[]``.

    Args:
        doc: the ``get_notes_state()`` dict (needs ``instructions`` and
            ``instructions_version``).
        memories: ``list_doc_memories()``, newest first; the order is
            trusted.
        memory_offset: skip this many of the newest memories first (the
            ``list_doc_notes`` page start). Past the end gives no memories
            and ``memories_omitted`` 0.

    Budget: the block's JSON text (``json.dumps(..., ensure_ascii=False)``)
    stays within ``DOC_NOTES_DELIVERY_MAX_CHARS``. The memories from the
    offset are a prefix, newest first, stopping at the first one that does
    not fit; ``memories_omitted`` counts the rest after the offset.

    Progress rule: when memories remain after the offset but none fits,
    the block still carries exactly the newest remaining one (going over
    the budget), so paging with ``memory_offset += len(block["memories"])``
    always advances. Under the real caps only escape-heavy text (a control
    character escapes to six characters) gets here.

    Raises:
        ValueError: ``doc`` has no ``instructions`` (a doc dict, not the
            notes state), or ``memory_offset`` is not an int >= 0.
    """
    if "instructions" not in doc:
        raise ValueError(
            "The notes block needs the instructions text: pass the "
            "get_notes_state() dict, not a doc dict."
        )
    _check_offset(memory_offset)
    items = [{"id": m["id"], "content": m["content"]} for m in memories[memory_offset:]]
    block: dict[str, Any] = {
        "instructions": doc["instructions"] or "",
        "instructions_version": doc["instructions_version"],
        "memories": [],
        "memories_omitted": len(items),
        "note": DOC_NOTES_BLOCK_NOTE,
    }
    count = 0
    while count < len(items):
        candidate = {
            **block,
            "memories": items[: count + 1],
            "memories_omitted": len(items) - count - 1,
        }
        if _json_len(candidate) > DOC_NOTES_DELIVERY_MAX_CHARS:
            break
        count += 1
    if count == 0 and items:
        count = 1
    block["memories"] = items[:count]
    block["memories_omitted"] = len(items) - count
    return block


# --- Text copy of the block ---


class NotesText(NamedTuple):
    """The text copy of a notes block (:func:`render_notes_text`).

    ``instructions_whole`` is False when the instructions were cut to fit
    the pre-card budget: the model was then not shown the whole rules, so
    a caller must not record that instructions version as seen.
    """

    text: str
    instructions_whole: bool


def _render_memory(content: str) -> str:
    """One bulleted memory; continuation lines indented under the bullet."""
    return NOTES_TEXT_MEMORY_BULLET + content.replace("\n", "\n" + NOTES_TEXT_MEMORY_INDENT)


def _more_memories(count: int) -> str:
    if count == 1:
        return NOTES_TEXT_ONE_MORE_MEMORY
    return NOTES_TEXT_MORE_MEMORIES.format(count=count)


def _memories_section(rendered: Sequence[str], hidden: int) -> str:
    if not rendered and hidden == 0:
        return NOTES_TEXT_NO_MEMORIES
    lines = [NOTES_TEXT_MEMORIES_HEADER, *rendered]
    if hidden:
        lines.append(_more_memories(hidden))
    return "\n".join(lines)


def _fitting_memory_count(head_len: int, rendered: Sequence[str], total: int) -> int | None:
    """The most memories (a prefix of ``rendered``) that fit after a head of
    ``head_len`` characters, or None when not even zero fit.

    Not always the first failure: rendering the last memory drops the
    trailer, which can make the text shorter.
    """
    budget = DOC_NOTES_PRECARD_MAX_CHARS - head_len - len("\n\n")
    if total == 0:
        return 0 if len(NOTES_TEXT_NO_MEMORIES) <= budget else None
    best = None
    used = len(NOTES_TEXT_MEMORIES_HEADER)
    for count in range(len(rendered) + 1):
        if count:
            used += 1 + len(rendered[count - 1])
        if used > budget:
            break
        hidden = total - count
        trailer = 1 + len(_more_memories(hidden)) if hidden else 0
        if used + trailer <= budget:
            best = count
    return best


def _cut_instructions(instructions: str, tail_len: int) -> str:
    """The instructions section cut to leave ``tail_len`` characters (the
    blank line before it excluded) for the memories section."""
    worst_marker = NOTES_TEXT_INSTRUCTIONS_CUT.format(count=len(instructions))
    keep = max(
        0,
        DOC_NOTES_PRECARD_MAX_CHARS
        - len(NOTES_TEXT_INSTRUCTIONS_HEADER) - 1
        - 1 - len(worst_marker)
        - len("\n\n") - tail_len,
    )
    marker = NOTES_TEXT_INSTRUCTIONS_CUT.format(count=len(instructions) - keep)
    return f"{NOTES_TEXT_INSTRUCTIONS_HEADER}\n{instructions[:keep]}\n{marker}"


def render_notes_text(block: Mapping[str, Any]) -> NotesText:
    """The plain-text copy of a notes block for the ``write_doc`` pre-card
    refusal, at most ``DOC_NOTES_PRECARD_MAX_CHARS`` characters::

        Instructions (follow them whenever you change this doc):
        <instructions, as is>

        Memories (newest first):
        - <memory; continuation lines indented by two spaces>
        (N more memories not shown; list_doc_notes pages them.)

    ``Instructions: none.`` and ``Memories: none.`` stand in for empty
    sections. The instructions come whole when they fit beside at least
    the memories header and trailer; the memories then render whole or
    not at all, the longest prefix of ``block["memories"]`` that fits.
    Otherwise the instructions are cut and end in a marker line giving the
    characters cut, and no memory is rendered. The trailer counts the
    block's ``memories_omitted`` plus its memories not rendered. The
    ``note`` is left out: the caller writes its own message above.

    Returns:
        :class:`NotesText`: the text, and whether the instructions came
        whole (always True for empty instructions).
    """
    instructions = block["instructions"] or ""
    rendered = [_render_memory(m["content"]) for m in block["memories"]]
    total = len(rendered) + block["memories_omitted"]
    if instructions:
        head = f"{NOTES_TEXT_INSTRUCTIONS_HEADER}\n{instructions}"
    else:
        head = NOTES_TEXT_NO_INSTRUCTIONS
    shown = _fitting_memory_count(len(head), rendered, total)
    if shown is not None:
        text = f"{head}\n\n{_memories_section(rendered[:shown], total - shown)}"
        return NotesText(text, instructions_whole=True)
    tail = _memories_section([], total)
    text = f"{_cut_instructions(instructions, len(tail))}\n\n{tail}"
    return NotesText(text, instructions_whole=False)
