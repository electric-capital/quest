"""The write path of the analytics ledgers.

``record_*`` functions are synchronous and never block: they validate,
stamp the time and append a row to an in-memory queue, so they are safe to
call from the conversation loop, from tool handlers and from the
synchronous system-prompt build. A single writer thread drains the queue
every ``flush_interval`` seconds (or sooner once ``max_batch`` rows are
waiting) and inserts each table's rows in one transaction; lifespan
shutdown flushes what is left. A hard crash loses the unflushed window,
which is acceptable for an analytics ledger and is why nothing that gates
behaviour (the ``edit_skill`` read gate, say) may be served from here.

Without an installed ledger (tests, scripts, a boot where the store failed
to open) the module-level ``record_*`` helpers drop the row and log once
at debug level: analytics loss must never break a turn.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from contextlib import closing
from datetime import datetime
from typing import Optional

from analytics.store import AnalyticsStore, utcnow

logger = logging.getLogger(__name__)

# Where a skill's body was put in front of the model. Plain strings in the
# ledger (no DB enum), so adding one here is the whole change.
SKILL_LOAD_SOURCES = (
    "autoload_user",      # the user's auto-loaded skills, in the system prompt
    "autoload_project",   # the project's auto-loaded skills
    "autoload_routine",   # the routine's auto-loaded skills
    "subagent_run",       # the caller-specified skills of a cross-user subagent run
    "composer",           # loaded through the composer's Skill button (skill_ids on a send)
    "tool_load_skills",   # the model called load_skills
    "tool_get_skill",     # the model called get_skill
    "create_skill",       # the model authored the content (approved create_skill)
    "backfill",           # imported from a pre-ledger skill_reads.json; source unknown
)

SKILL_LOADS_COLUMNS = (
    "created_at", "conversation_id", "user_id", "skill_id", "source",
    "project_id", "routine_id",
)

# Who issued a tool call (the ``call_type`` column of ``tool_calls``).
TOOL_CALL_TYPES = (
    "top_level",    # the conversation's own model turn (ApiCallType.TOP_LEVEL)
    "sub_agent",    # a sub-agent spawned by agent_task* (ApiCallType.SUB_AGENT)
    "script",       # sandbox code calling POST /api/tool-call on the sandbox tool API
)

# How the row got into the ledger (the ``source`` column of ``tool_calls``).
TOOL_CALL_SOURCES = (
    "live",         # recorded by the loop as the call was dispatched
    "backfill",     # imported from a pre-ledger chat_history.json transcript
)

TOOL_CALLS_COLUMNS = (
    "created_at", "conversation_id", "user_id", "tool_name", "dynamic",
    "call_type", "source", "tool_id", "agent_name", "sub_agent_level",
    "parent_tool_id", "project_id", "routine_id",
)


def effective_tool_name(tool_name: str, args: Optional[dict]) -> tuple[str, bool]:
    """``(name, dynamic)`` for a tool call as the ledger records it.

    A call through the ``tool_call`` meta-tool is recorded under the inner
    ``tool_name`` it routed to with ``dynamic = True``; a ``tool_call``
    whose arguments name no inner tool stays ``tool_call`` (the dispatch
    rejects it, but the attempt is still a call). Every other tool keeps
    its own name.
    """
    if tool_name == "tool_call" and isinstance(args, dict):
        inner = args.get("tool_name")
        if isinstance(inner, str) and inner:
            return inner, True
    return tool_name, False


class Ledger:
    """Queue rows for the writer thread; see the module docstring."""

    def __init__(
        self,
        store: AnalyticsStore,
        *,
        flush_interval: float = 0.5,
        max_batch: int = 1000,
        max_queue: int = 100_000,
    ):
        self._store = store
        self._flush_interval = flush_interval
        self._max_batch = max_batch
        self._max_queue = max_queue
        self._queue: deque[tuple[str, tuple[str, ...], tuple]] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0

    # -- recording ---------------------------------------------------------

    def record_skill_load(
        self,
        *,
        conversation_id: str,
        user_id: int,
        skill_ids: list[str],
        source: str,
        project_id: Optional[str] = None,
        routine_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> int:
        """Queue one ``skill_loads`` row per skill id; returns how many."""
        if source not in SKILL_LOAD_SOURCES:
            raise ValueError(f"unknown skill load source: {source!r}")
        if not conversation_id or user_id is None:
            raise ValueError("skill load needs a conversation_id and a user_id")
        ids = [sid for sid in skill_ids if sid]
        if not ids:
            return 0
        stamp = created_at or utcnow()
        rows = [
            (stamp, conversation_id, int(user_id), sid, source, project_id, routine_id)
            for sid in ids
        ]
        self._enqueue("skill_loads", SKILL_LOADS_COLUMNS, rows)
        return len(rows)

    def record_tool_call(
        self,
        *,
        user_id: int,
        tool_name: str,
        dynamic: bool,
        call_type: str,
        conversation_id: Optional[str] = None,
        source: str = "live",
        tool_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        sub_agent_level: Optional[int] = None,
        parent_tool_id: Optional[str] = None,
        project_id: Optional[str] = None,
        routine_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> None:
        """Queue one ``tool_calls`` row (``tool_name`` is the effective
        name; see :func:`effective_tool_name`)."""
        if call_type not in TOOL_CALL_TYPES:
            raise ValueError(f"unknown tool call type: {call_type!r}")
        if source not in TOOL_CALL_SOURCES:
            raise ValueError(f"unknown tool call source: {source!r}")
        if not tool_name or user_id is None:
            raise ValueError("tool call needs a tool_name and a user_id")
        row = (
            created_at or utcnow(), conversation_id, int(user_id), tool_name,
            bool(dynamic), call_type, source, tool_id, agent_name,
            int(sub_agent_level) if sub_agent_level is not None else None,
            parent_tool_id, project_id, routine_id,
        )
        self._enqueue("tool_calls", TOOL_CALLS_COLUMNS, [row])

    def _enqueue(self, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
        with self._lock:
            room = self._max_queue - len(self._queue)
            if room < len(rows):
                # The writer is dead or hopelessly behind; keep memory bounded.
                self._dropped += len(rows) - max(room, 0)
                rows = rows[:max(room, 0)]
            for row in rows:
                self._queue.append((table, columns, row))
            pending = len(self._queue)
        if pending >= self._max_batch:
            self._wake.set()

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def dropped(self) -> int:
        """Rows discarded because the queue was full (never reset)."""
        return self._dropped

    # -- flushing ----------------------------------------------------------

    def flush(self) -> int:
        """Insert every queued row now, on the calling thread; returns the count.

        The writer thread calls this on its own cursor; tests and shutdown
        call it directly.
        """
        with closing(self._store.cursor()) as cur:
            return self._flush_with(cur)

    def _flush_with(self, cur) -> int:
        total = 0
        while True:
            with self._lock:
                if not self._queue:
                    return total
                batch = [self._queue.popleft() for _ in range(min(self._max_batch, len(self._queue)))]
            by_table: dict[tuple[str, tuple[str, ...]], list[tuple]] = {}
            for table, columns, row in batch:
                by_table.setdefault((table, columns), []).append(row)
            cur.execute("BEGIN TRANSACTION")
            try:
                for (table, columns), rows in by_table.items():
                    placeholders = ", ".join("?" for _ in columns)
                    cur.executemany(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        rows,
                    )
                cur.execute("COMMIT")
            except Exception:
                # Drop the batch rather than wedge the writer on a poison row;
                # the loss is logged with its size.
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
                logger.exception("analytics ledger flush failed; %d row(s) dropped", len(batch))
                self._dropped += len(batch)
                continue
            total += len(batch)

    # -- the writer thread -------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="analytics-ledger", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        with closing(self._store.cursor()) as cur:
            while not self._stop.is_set():
                self._wake.wait(self._flush_interval)
                self._wake.clear()
                try:
                    self._flush_with(cur)
                except Exception:
                    # A cursor-level failure (store closed under us): back off
                    # to the next interval rather than spin.
                    logger.exception("analytics ledger writer iteration failed")
            try:
                self._flush_with(cur)
            except Exception:
                logger.exception("analytics ledger final flush failed")

    def stop(self) -> None:
        """Stop the writer thread after a final flush (no-op if not started)."""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        self._wake.set()
        thread.join(timeout=10)
        self._thread = None
        if thread.is_alive():
            logger.warning("analytics ledger writer did not stop within 10s")


_current: Optional[Ledger] = None


def get_ledger() -> Optional[Ledger]:
    return _current


def set_ledger(ledger: Optional[Ledger]) -> None:
    """Install (or clear) the process-wide ledger."""
    global _current
    _current = ledger


def record_skill_load(
    *,
    conversation_id: Optional[str],
    user_id: Optional[int],
    skill_ids: list[str],
    source: str,
    project_id: Optional[str] = None,
    routine_id: Optional[str] = None,
) -> None:
    """Best-effort ``skill_loads`` write through the process-wide ledger.

    Drops the rows (debug log) when no ledger is installed or the ids are
    empty; logs a warning on a bad call (an unknown ``source`` is a
    programming error, but it must not break the turn that hit it).
    """
    ledger = _current
    if ledger is None or not skill_ids or not conversation_id or user_id is None:
        if ledger is None and skill_ids:
            logger.debug("analytics ledger not installed; skill load not recorded")
        return
    try:
        ledger.record_skill_load(
            conversation_id=conversation_id, user_id=user_id, skill_ids=skill_ids,
            source=source, project_id=project_id, routine_id=routine_id,
        )
    except Exception:
        logger.warning(
            "failed to record skill load (conversation_id=%s, source=%s)",
            conversation_id, source, exc_info=True,
        )


def record_tool_call(
    *,
    user_id: Optional[int],
    tool_name: str,
    args: Optional[dict],
    call_type: str,
    conversation_id: Optional[str] = None,
    tool_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    sub_agent_level: Optional[int] = None,
    parent_tool_id: Optional[str] = None,
    project_id: Optional[str] = None,
    routine_id: Optional[str] = None,
) -> None:
    """Best-effort ``tool_calls`` write through the process-wide ledger.

    Takes the provider-visible ``tool_name`` and the call's ``args`` and
    resolves the effective name itself (``effective_tool_name``), so every
    recording site makes the same one-line call. Drops the row (debug log)
    when no ledger is installed; logs a warning on a bad call -- analytics
    loss must never break the turn that hit it.
    """
    ledger = _current
    if ledger is None or user_id is None or not tool_name:
        if ledger is None and tool_name:
            logger.debug("analytics ledger not installed; tool call not recorded")
        return
    name, dynamic = effective_tool_name(tool_name, args)
    try:
        ledger.record_tool_call(
            user_id=user_id, tool_name=name, dynamic=dynamic, call_type=call_type,
            conversation_id=conversation_id, tool_id=tool_id, agent_name=agent_name,
            sub_agent_level=sub_agent_level, parent_tool_id=parent_tool_id,
            project_id=project_id, routine_id=routine_id,
        )
    except Exception:
        logger.warning(
            "failed to record tool call (conversation_id=%s, tool=%s, call_type=%s)",
            conversation_id, name, call_type, exc_info=True,
        )
