"""The inbox's Memories tab: every suggested memory of a user in one list.

Three kinds of suggestion live in three tables with the same lifecycle
(``pending`` -> ``accepted`` | ``dismissed``) and the same resolved keyset
cursor: personal (``suggest_memory``, chat/memory_suggestions.py), project
(``suggest_project_memory``, chat/project_memories.py) and doc notes
(``suggest_doc_note``, chat/docs/note_suggestions.py). The composer bubbles
show one conversation's pending rows of one kind; the inbox shows all of a
user's rows of the three kinds, tagged ``kind`` (``user`` / ``project`` /
``doc``) and carrying the conversation they came from, so the user can
review everything that is waiting without opening each chat. Accept /
dismiss still go through the kind's own route (a doc row through
``/doc-note-suggestions/{id}/accept`` and ``/dismiss``).

A doc row is the review row of chat/docs/note_suggestions.py
(``review_rows()``) as ``chat.docs.note_payloads.public_suggestion()``
sends it: the store's own ``kind`` renamed ``note_kind`` (``instructions``
/ ``memory``), plus ``doc_title`` (``""`` for a doc the user can no longer
see), ``replaces_content``, ``current_instructions`` and
``instructions_moved``. Its ``project_id`` is the conversation's, since a
doc suggestion carries none.

Never gated: a doc row stays counted and listed while the ``docs`` or
``doc_notes`` gate is closed for the user (reviewing is a person acting),
and the per-user pending summary the Slack reminder counts
(chat/slack_notifier.py) includes it, so a reminder never counts an item
the inbox would not show.
"""

from __future__ import annotations

import heapq
from typing import Optional

from chat.docs.note_payloads import public_suggestion
from chat.docs.note_suggestions import review_rows
from db import doc_note_suggestion_store as doc_note_store
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


async def _doc_rows(user_id: int, rows: list[dict]) -> list[dict]:
    """Store rows of ``doc_note_suggestions`` as inbox rows, in the same
    order: reviewed (``review_rows()``, one lookup per distinct doc), the
    store ``kind`` moved to ``note_kind``, then tagged ``doc``."""
    if not rows:
        return []
    return _tag(
        [public_suggestion(row) for row in await review_rows(user_id, rows)], "doc",
    )


async def count_pending(user_id: int) -> dict:
    """``{"user": n, "project": m, "doc": d, "pending_count": n + m + d}``."""
    user_pending = await user_store.count_pending_for_user(user_id)
    project_pending = await project_store.count_pending_for_user(user_id)
    doc_pending = await doc_note_store.count_pending_for_user(user_id)
    return {
        "user": user_pending,
        "project": project_pending,
        "doc": doc_pending,
        "pending_count": user_pending + project_pending + doc_pending,
    }


async def list_pending(user_id: int) -> list[dict]:
    """All pending suggestions of the three kinds, oldest first."""
    rows = _tag(await user_store.list_pending_for_user(user_id), "user")
    rows += _tag(await project_store.list_pending_for_user(user_id), "project")
    rows += await _doc_rows(user_id, await doc_note_store.list_pending_for_user(user_id))
    rows.sort(key=lambda r: (r["created_at"] or "", r["id"]))
    return await _with_context(rows)


async def list_resolved(
    user_id: int, limit: int, cursor: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """One page of accepted / dismissed suggestions of the three kinds, most
    recently resolved first, with the cursor of the next page (None at the
    end). Raises ValueError on a malformed cursor.

    Each table is paged with the same ``(resolved_at, id)`` keyset, so taking
    the top ``limit`` of the three ``limit``-row pages and continuing from
    the last returned row is exact: nothing is skipped or repeated. The merge
    runs on the store rows (each paired with its kind, since a doc row's own
    ``kind`` is its note kind); only the doc rows that made the page are
    then reviewed, so a page never costs lookups for rows it drops.
    """
    decoded = decode_resolved_cursor(cursor)
    tables = {
        "user": await user_store.list_resolved_for_user(user_id, limit, decoded),
        "project": await project_store.list_resolved_for_user(user_id, limit, decoded),
        "doc": await doc_note_store.list_resolved_for_user(user_id, limit, decoded),
    }
    key = lambda pair: (pair[1]["resolved_at"], pair[1]["id"])  # noqa: E731
    merged = list(heapq.merge(
        *([(kind, row) for row in rows] for kind, rows in tables.items()),
        key=key, reverse=True,
    ))
    page = merged[:limit]
    # Rows may remain past this page when the merge overflowed it, or when a
    # table filled its own page (its next row is unknown); the latter can
    # cost one trailing empty page, which the client treats as the end.
    has_more = len(merged) > limit or any(len(rows) == limit for rows in tables.values())
    next_cursor = encode_resolved_cursor(page[-1][1]) if page and has_more else None

    doc_rows = {
        row["id"]: row
        for row in await _doc_rows(user_id, [row for kind, row in page if kind == "doc"])
    }
    rows = [
        doc_rows[row["id"]] if kind == "doc" else {**row, "kind": kind}
        for kind, row in page
    ]
    return await _with_context(rows), next_cursor


async def list_pending_summaries() -> list[dict]:
    """One row per user with pending suggestions of any of the three kinds:
    ``[{"user_id", "pending_count", "oldest_created_at"}]``, the counts
    summed and the oldest the minimum across the kinds."""
    by_user: dict[int, dict] = {}
    for summary in (
        await user_store.list_pending_summaries()
        + await project_store.list_pending_summaries()
        + await doc_note_store.list_pending_summaries()
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
