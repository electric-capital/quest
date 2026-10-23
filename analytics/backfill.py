"""One-shot imports of history that predates a ledger.

A backfill reads the filesystem and can take minutes, so it is not a
migration: it runs as a lifespan background task after the store is open,
in a worker thread, and records itself in the ``_backfills`` marker table
when done. Each import runs in a single transaction together with its
marker row, so a crash mid-way leaves nothing behind and the next boot
simply runs it again; a present marker means "never again". The reports
show partial data until a backfill finishes and carry its status.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Iterable, Optional

from analytics.ledger import SKILL_LOADS_COLUMNS, TOOL_CALLS_COLUMNS, effective_tool_name
from analytics.store import AnalyticsStore, get_store, utcnow

logger = logging.getLogger(__name__)

SKILL_READS_BACKFILL = "skill_reads"
TOOL_CALLS_BACKFILL = "tool_calls_transcripts"


def is_backfill_done(cur, name: str) -> bool:
    return cur.execute(
        "SELECT 1 FROM _backfills WHERE name = ? LIMIT 1", [name],
    ).fetchone() is not None


def backfill_skill_reads(
    store: AnalyticsStore,
    conversations: Iterable[dict],
    read_ids: Callable[[str], list[str]],
) -> Optional[int]:
    """Import every conversation's ``skill_reads.json`` into ``skill_loads``.

    ``conversations`` are the surviving conversation rows (``id``,
    ``user_id``, ``project_id``, ``routine_id``, ``created_at``);
    ``read_ids`` returns the sidecar's skill ids for a conversation id
    (``ChatStorage.get_skill_read_ids``). The sidecar is a set without
    timestamps or provenance, so every id becomes one row with
    ``source = 'backfill'`` stamped at the conversation's ``created_at``.
    Returns the rows written, or None when the marker says it already ran.
    """
    with store.cursor() as cur:
        if is_backfill_done(cur, SKILL_READS_BACKFILL):
            return None
        rows: list[tuple] = []
        for conv in conversations:
            ids = read_ids(conv["id"])
            if not ids:
                continue
            created = conv.get("created_at") or utcnow()
            if isinstance(created, datetime) and created.tzinfo is not None:
                created = (created - created.utcoffset()).replace(tzinfo=None)
            rows.extend(
                (created, conv["id"], int(conv["user_id"]), sid, "backfill",
                 conv.get("project_id"), conv.get("routine_id"))
                for sid in ids if sid
            )
        placeholders = ", ".join("?" for _ in SKILL_LOADS_COLUMNS)
        cur.execute("BEGIN TRANSACTION")
        try:
            if rows:
                cur.executemany(
                    f"INSERT INTO skill_loads ({', '.join(SKILL_LOADS_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
            cur.execute(
                "INSERT INTO _backfills (name, completed_at, row_count) VALUES (?, ?, ?)",
                [SKILL_READS_BACKFILL, utcnow(), len(rows)],
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return len(rows)


def _parse_transcript_timestamp(value) -> Optional[datetime]:
    """The ``Z``-suffixed ISO stamps chat_history.json carries, as naive UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = (parsed - parsed.utcoffset()).replace(tzinfo=None)
    return parsed


def backfill_tool_calls(
    store: AnalyticsStore,
    conversations: Iterable[dict],
    read_calls: Callable[[str], list[dict]],
    cutoff: Optional[datetime] = None,
) -> Optional[int]:
    """Import every conversation's transcript tool calls into ``tool_calls``.

    ``conversations`` are the surviving conversation rows (``id``,
    ``user_id``, ``project_id``, ``routine_id``, ``created_at``);
    ``read_calls`` returns a transcript's calls for a conversation id
    (``ChatStorage.list_transcript_tool_calls``). Each becomes one row with
    ``source = 'backfill'`` stamped at the message's own timestamp (a call
    without one takes the conversation's ``created_at``); calls stamped at
    or after ``cutoff`` -- the moment the live ledger started recording --
    are skipped so a conversation active across the deploy is not counted
    twice. Returns the rows written, or None when the marker says it
    already ran.
    """
    with store.cursor() as cur:
        if is_backfill_done(cur, TOOL_CALLS_BACKFILL):
            return None
        rows: list[tuple] = []
        for conv in conversations:
            calls = read_calls(conv["id"])
            if not calls:
                continue
            created = conv.get("created_at") or utcnow()
            if isinstance(created, datetime) and created.tzinfo is not None:
                created = (created - created.utcoffset()).replace(tzinfo=None)
            for call in calls:
                stamp = _parse_transcript_timestamp(call.get("timestamp")) or created
                if cutoff is not None and stamp >= cutoff:
                    continue
                name, dynamic = effective_tool_name(call["tool_name"], call.get("tool_input"))
                rows.append((
                    stamp, conv["id"], int(conv["user_id"]), name, dynamic,
                    call["call_type"], "backfill", call.get("tool_id"),
                    call.get("agent_name"), call.get("sub_agent_level"),
                    call.get("parent_tool_id"), conv.get("project_id"),
                    conv.get("routine_id"),
                ))
        placeholders = ", ".join("?" for _ in TOOL_CALLS_COLUMNS)
        cur.execute("BEGIN TRANSACTION")
        try:
            if rows:
                cur.executemany(
                    f"INSERT INTO tool_calls ({', '.join(TOOL_CALLS_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
            cur.execute(
                "INSERT INTO _backfills (name, completed_at, row_count) VALUES (?, ?, ?)",
                [TOOL_CALLS_BACKFILL, utcnow(), len(rows)],
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return len(rows)


async def run_backfills() -> None:
    """Lifespan task: run every pending one-shot import, in order, each
    guarded so one failure does not block the next (see module doc)."""
    cutoff = utcnow()
    await run_skill_reads_backfill()
    await run_tool_calls_backfill(cutoff)


async def run_tool_calls_backfill(cutoff: Optional[datetime] = None) -> None:
    """Lifespan task: import the pre-ledger transcript tool calls once."""
    store = get_store()
    if store is None:
        return
    from chat.storage import ChatStorage
    from db.conversation_store import list_conversation_activity_rows

    try:
        with store.cursor() as cur:
            if is_backfill_done(cur, TOOL_CALLS_BACKFILL):
                return
        conversations = await list_conversation_activity_rows()
        written = await asyncio.to_thread(
            backfill_tool_calls, store, conversations,
            ChatStorage.list_transcript_tool_calls, cutoff,
        )
        if written is not None:
            logger.info(
                "Backfilled tool_calls from chat_history.json: %d row(s) across "
                "%d conversation(s) scanned", written, len(conversations),
            )
    except Exception:
        logger.exception("tool_calls backfill failed; will retry next boot")


async def run_skill_reads_backfill() -> None:
    """Lifespan task: import the pre-ledger sidecars once (see module doc)."""
    store = get_store()
    if store is None:
        return
    from chat.storage import ChatStorage
    from db.conversation_store import list_conversation_activity_rows

    try:
        with store.cursor() as cur:
            if is_backfill_done(cur, SKILL_READS_BACKFILL):
                return
        conversations = await list_conversation_activity_rows()
        written = await asyncio.to_thread(
            backfill_skill_reads, store, conversations, ChatStorage.get_skill_read_ids,
        )
        if written is not None:
            logger.info(
                "Backfilled skill_loads from skill_reads.json: %d row(s) across "
                "%d conversation(s) scanned", written, len(conversations),
            )
    except Exception:
        logger.exception("skill_reads backfill failed; will retry next boot")
