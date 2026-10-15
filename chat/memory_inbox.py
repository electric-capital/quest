"""The inbox's Memories tab: every suggested memory of a user in one list.

Personal (``suggest_memory``, chat/memory_suggestions.py) and project
(``suggest_project_memory``, chat/project_memories.py) suggestions live in
two tables with the same lifecycle. The composer bubbles show one
conversation's pending rows of one kind; the inbox shows all of a user's
rows of both kinds, tagged ``kind`` (``user`` / ``project``) and carrying
the conversation they came from, so the user can review everything that is
waiting without opening each chat. Accept / dismiss still go through the
kind's own route.

Also the per-user pending summary the Slack reminder counts (chat/slack_notifier.py).
"""

from __future__ import annotations

import heapq
from typing import Optional

from db import memory_suggestion_store as user_store
from db import project_memory_suggestion_store as project_store
from db.memory_suggestion_store import (
    decode_resolved_cursor,
    encode_resolved_cursor,
)

# Page size bounds of the resolved list.
DEFAULT_RESOLVED_PAGE_SIZE = 20
MAX_RESOLVED_PAGE_SIZE = 100


async def _with_context(rows: list[dict]) -> list[dict]:
    """Attach the "from" line fields to each row: ``conversation_title``,
    ``project_id`` and ``project_name`` (None when the conversation is gone).
    A project suggestion keeps its own ``project_id``."""
    context = await user_store.conversation_context(
        list({row["conversation_id"] for row in rows}),
    )
    out = []
    for row in rows:
        ctx = context.get(row["conversation_id"]) or {}
        out.append({
            **row,
            "conversation_title": ctx.get("conversation_title"),
            "project_id": row.get("project_id") or ctx.get("project_id"),
            "project_name": ctx.get("project_name"),
        })
    return out


def _tag(rows: list[dict], kind: str) -> list[dict]:
    return [{**row, "kind": kind} for row in rows]


async def count_pending(user_id: int) -> dict:
    """``{"user": n, "project": m, "pending_count": n + m}``."""
    user_pending = await user_store.count_pending_for_user(user_id)
    project_pending = await project_store.count_pending_for_user(user_id)
    return {
        "user": user_pending,
        "project": project_pending,
        "pending_count": user_pending + project_pending,
    }


async def list_pending(user_id: int) -> list[dict]:
    """All pending suggestions of both kinds, oldest first."""
    rows = _tag(await user_store.list_pending_for_user(user_id), "user")
    rows += _tag(await project_store.list_pending_for_user(user_id), "project")
    rows.sort(key=lambda r: (r["created_at"] or "", r["id"]))
    return await _with_context(rows)


async def list_resolved(
    user_id: int, limit: int, cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """One page of accepted / dismissed suggestions of both kinds, most
    recently resolved first, with the cursor of the next page (None at the
    end). Raises ValueError on a malformed cursor.

    Each table is paged with the same ``(resolved_at, id)`` keyset, so taking
    the top ``limit`` of the two ``limit``-row pages and continuing from the
    last returned row is exact: nothing is skipped or repeated.
    """
    decoded = decode_resolved_cursor(cursor)
    user_rows = _tag(await user_store.list_resolved_for_user(user_id, limit, decoded), "user")
    project_rows = _tag(
        await project_store.list_resolved_for_user(user_id, limit, decoded), "project",
    )
    key = lambda r: (r["resolved_at"], r["id"])  # noqa: E731
    merged = list(heapq.merge(user_rows, project_rows, key=key, reverse=True))
    page = merged[:limit]
    # Rows may remain past this page when the merge overflowed it, or when a
    # table filled its own page (its next row is unknown); the latter can
    # cost one trailing empty page, which the client treats as the end.
    has_more = (
        len(merged) > limit
        or len(user_rows) == limit
        or len(project_rows) == limit
    )
    next_cursor = encode_resolved_cursor(page[-1]) if page and has_more else None
    return await _with_context(page), next_cursor


async def list_pending_summaries() -> list[dict]:
    """One row per user with pending suggestions of either kind:
    ``[{"user_id", "pending_count", "oldest_created_at"}]``."""
    by_user: dict[int, dict] = {}
    for summary in (
        await user_store.list_pending_summaries()
        + await project_store.list_pending_summaries()
    ):
        current = by_user.get(summary["user_id"])
        if current is None:
            by_user[summary["user_id"]] = dict(summary)
            continue
        current["pending_count"] += summary["pending_count"]
        current["oldest_created_at"] = min(
            current["oldest_created_at"], summary["oldest_created_at"],
        )
    return list(by_user.values())
