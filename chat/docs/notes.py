"""Doc notes gate: who must be shown a doc's notes, and the notes block.

Pure functions over the notes state of a doc (docs/architecture/doc-notes.md)
and the per-conversation seen record (``ChatStorage.get_doc_notes_seen()``).
No I/O here: callers load the state and the record, decide with these
functions, and write the record back.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NotesSeen:
    """One doc's entry in a conversation's seen record: the instructions and
    memories versions this conversation was last shown."""

    instructions: int
    memories: int
