"""Saved memories rendered into the system prompt, in tiers.

Every top-level conversation starts with the user's active memories in its
system prompt, so the model applies them without a ``memory_search`` round
trip, and every project conversation starts with the project's memories the
same way. A block is rendered once per session (the prompt is the session's
system instruction) from the newest memories first, in three tiers under
separate budgets:

1. **Full text** for the newest memories (``<memory id created>`` entries).
2. **An index** of the next ones: one line each with the id, the date and
   the start of the text, so the model knows what exists and can fetch a
   memory by id (``memory_get``) or search for it by topic, instead of only
   learning that "N older memories are not shown".
3. **A count** of whatever did not fit either tier, pointing the model at
   the list and search tools.

``run_conversation_turn`` (chat/gemini_api/conversation.py) calls
:func:`load_memories_content` for the standard prompt only (public-project,
cross-user subagent and inference-API runs never see personal memories)
and :func:`load_project_memories_content` for every project conversation's
top-level prompt, public projects included.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryBudget:
    """Caps for one rendered block. Memories are at most
    ``db.memory_store.MAX_MEMORY_SIZE`` (4 KB) each, so the character caps
    are the binding limits for users with long memories and the count caps
    for users with many short ones. An index line is roughly 150
    characters (a UUID, a date and ``INDEX_SNIPPET_CHARS`` of text)."""

    full_count: int
    full_chars: int
    index_count: int
    index_chars: int


# Personal memories: ~12K + ~16K characters, roughly 7K tokens all in.
PERSONAL_MEMORY_BUDGET = MemoryBudget(
    full_count=30, full_chars=12_000, index_count=200, index_chars=16_000,
)
# Project memories sit beside the personal block in project prompts, so
# they get a smaller budget.
PROJECT_MEMORY_BUDGET = MemoryBudget(
    full_count=20, full_chars=8_000, index_count=150, index_chars=12_000,
)

# Characters of memory text on an index line.
INDEX_SNIPPET_CHARS = 100


def _created_date(memory: dict) -> str:
    """``YYYY-MM-DD`` from the memory's ISO ``created_at``, or ``""``."""
    created = memory.get("created_at") or ""
    return created[:10] if isinstance(created, str) else ""


def _attrs(memory: dict) -> str:
    attrs = f' id="{memory["id"]}"'
    created = _created_date(memory)
    if created:
        attrs += f' created="{created}"'
    return attrs


def _render_entry(memory: dict) -> str:
    content = (memory.get("content") or "").strip()
    return f"<memory{_attrs(memory)}>\n{content}\n</memory>"


def index_snippet(content: str, limit: int = INDEX_SNIPPET_CHARS) -> str:
    """The start of ``content`` for an index line: whitespace collapsed to
    single spaces (a multi-line memory contributes more than its first
    line), cut at ``limit`` characters -- on a word boundary when one is
    close -- with an ellipsis when anything was dropped."""
    text = " ".join(content.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _render_index_line(memory: dict) -> str:
    snippet = index_snippet(memory.get("content") or "")
    return f"<ref{_attrs(memory)}>{snippet}</ref>"


def _plural(noun: str) -> str:
    return noun[:-1] + "ies" if noun.endswith("y") else noun + "s"


def _count(n: int, noun: str, adjective: str = "") -> str:
    """``"1 older memory"`` / ``"3 older project memories"``."""
    word = noun if n == 1 else _plural(noun)
    return f"{n} {adjective + ' ' if adjective else ''}{word}"


def render_memories(
    memories: list[dict],
    budget: MemoryBudget,
    *,
    tool_prefix: str = "memory",
    noun: str = "memory",
) -> str:
    """Render ``memories`` (newest first, as the list functions return
    them) as the prompt's memory block, or ``""`` when there are none.

    Full entries are included in order until either full-tier cap is hit;
    the next memories go into a ``<memory-index>`` of one-line references
    until either index cap is hit; a trailing line then counts the
    memories left out of both tiers. The first memory is always included
    in full even when it alone exceeds the character budget, so a user
    with memories never gets an empty block. ``tool_prefix`` names the
    read tools (``memory_get`` / ``project_memory_get``, ...) and ``noun``
    the kind of memory in the trailing lines.
    """
    if not memories:
        return ""
    get_tool = f"`{tool_prefix}_get`"
    search_tool = f"`{tool_prefix}_search`"
    list_tool = f"`{tool_prefix}_list`"

    entries: list[str] = []
    used = 0
    for memory in memories:
        if len(entries) >= budget.full_count:
            break
        entry = _render_entry(memory)
        if entries and used + len(entry) > budget.full_chars:
            break
        entries.append(entry)
        used += len(entry)
    text = "\n\n".join(entries)

    lines: list[str] = []
    used = 0
    for memory in memories[len(entries):]:
        if len(lines) >= budget.index_count:
            break
        line = _render_index_line(memory)
        if used + len(line) > budget.index_chars:
            break
        lines.append(line)
        used += len(line)
    if lines:
        text += (
            f"\n\n<memory-index>\n"
            f"{_count(len(lines), noun, 'older')}, one line each: the id, the "
            f"date and the start of the text. {get_tool} returns the full "
            f"text of any of them by id; {search_tool} finds one by topic.\n"
            + "\n".join(lines)
            + "\n</memory-index>"
        )

    omitted = len(memories) - len(entries) - len(lines)
    if omitted:
        shown = f"{len(entries)} in full"
        if lines:
            shown += f" and {len(lines)} indexed"
        text += (
            f"\n\n{_count(omitted, noun, 'older')} "
            f"{'is' if omitted == 1 else 'are'} not shown at all "
            f"({shown}, of {len(memories)}). Call "
            f"{list_tool} for the complete set or {search_tool} for a "
            "topic before assuming the user never told you something."
        )
    return text


def build_memories_content(memories: list[dict]) -> str:
    """The personal-memory block: :func:`render_memories` under
    ``PERSONAL_MEMORY_BUDGET`` with the ``memory_*`` tools."""
    return render_memories(memories, PERSONAL_MEMORY_BUDGET)


def build_project_memories_content(memories: list[dict]) -> str:
    """The project-memory block: :func:`render_memories` under
    ``PROJECT_MEMORY_BUDGET`` with the ``project_memory_*`` tools."""
    return render_memories(
        memories, PROJECT_MEMORY_BUDGET,
        tool_prefix="project_memory", noun="project memory",
    )


async def load_memories_content(user_id: int) -> str:
    """The rendered memory block for ``user_id``, or ``""``.

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


async def load_project_memories_content(project_id: str) -> str:
    """The rendered project-memory block for ``project_id``, or ``""``.
    Never raises, like :func:`load_memories_content`."""
    from db.project_memory_store import list_project_memories

    try:
        memories = await list_project_memories(project_id)
    except Exception:
        logger.exception(
            "[memory-prompt] loading project memories failed (project=%s)", project_id,
        )
        return ""
    return build_project_memories_content(memories)
