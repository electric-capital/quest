"""Saved memories rendered into the top-level system prompt.

Every top-level conversation starts with the user's active memories in its
system prompt, so the model applies them without a ``memory_search`` round
trip. The block is rendered once per session (the prompt is the session's
system instruction) from the newest memories first, under a size budget:
when a user has more than fit, the block names how many were left out so
the model knows to reach for ``memory_list`` / ``memory_search``.

``run_conversation_turn`` (chat/gemini_api/conversation.py) calls
:func:`load_memories_content` for the standard prompt only; public-project,
cross-user subagent and inference-API runs never see memories.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Upper bounds for the rendered block. Memories are at most
# ``db.memory_store.MAX_MEMORY_SIZE`` (4 KB) each, so the character budget
# is the binding limit for users with long memories and the count cap for
# users with many short ones. ~24K characters is roughly 6K tokens.
MAX_INJECTED_MEMORIES = 50
MAX_INJECTED_MEMORY_CHARS = 24_000


def _created_date(memory: dict) -> str:
    """``YYYY-MM-DD`` from the memory's ISO ``created_at``, or ``""``."""
    created = memory.get("created_at") or ""
    return created[:10] if isinstance(created, str) else ""


def _render_entry(memory: dict) -> str:
    created = _created_date(memory)
    attrs = f' id="{memory["id"]}"'
    if created:
        attrs += f' created="{created}"'
    content = (memory.get("content") or "").strip()
    return f"<memory{attrs}>\n{content}\n</memory>"


def build_memories_content(memories: list[dict]) -> str:
    """Render ``memories`` (newest first, as ``list_memories`` returns them)
    as the prompt's memory entries, or ``""`` when there are none.

    Entries are included in order until either cap is hit; a trailing line
    then states how many memories were left out. The first memory is always
    included even when it alone exceeds the character budget, so a user
    with memories never gets an empty block.
    """
    if not memories:
        return ""
    entries: list[str] = []
    used = 0
    for memory in memories:
        if len(entries) >= MAX_INJECTED_MEMORIES:
            break
        entry = _render_entry(memory)
        if entries and used + len(entry) > MAX_INJECTED_MEMORY_CHARS:
            break
        entries.append(entry)
        used += len(entry)
    text = "\n\n".join(entries)
    omitted = len(memories) - len(entries)
    if omitted:
        text += (
            f"\n\n{omitted} older "
            f"{'memory is' if omitted == 1 else 'memories are'} not shown "
            f"({len(entries)} of {len(memories)} fit here). Call "
            "`memory_list` for the complete set or `memory_search` for a "
            "topic before assuming the user never told you something."
        )
    return text


async def load_memories_content(user_id: int) -> str:
    """The rendered memory entries for ``user_id``, or ``""``.

    Never raises: a store failure is logged and the prompt goes out without
    memories (the tools still work), since a memory read must not fail a
    turn.
    """
    from db.memory_store import list_memories

    try:
        memories = await list_memories(user_id, include_archived=False)
    except Exception:
        logger.exception("[memory-prompt] loading memories failed (user=%s)", user_id)
        return ""
    return build_memories_content(memories)
