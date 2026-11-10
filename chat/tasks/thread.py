"""What the Quest Task routes return: the list row, the task detail, the
brief state and versions, and the thread's pages of runs (spec issue #213,
section 7).

Every builder takes rows the caller has already scoped to the user (the
route guard ``require_task_for_route`` or the service did the gate and
ownership checks) and shapes them for the frontend. Store-internal fields
(``user_id``, ``next_run_seq``, a run's frozen ``context_brief_version``)
never leave this module.

Brief authorship: every brief version row carries a ``source`` (``ui``,
``run:<run_id>``, ``restore:<n>``); views add ``run_seq``, the seq of that
run while it still exists, so the UI can say "Updated by run N" or "Edited
by you" without a second call.

A run's view hides the pending entry of an ACTIVE run (``summary``,
``result``, ``notes`` saved by a ``complete_task_run`` that hit a brief
conflict, chat/tasks/runs.py) and carries its questions (one query per
page, chat/tasks/questions.py) and its open approval and connect cards as
the transcript rows the conversation detail holds, so the thread renders
them with the same components as a chat.

Does not import ``chat.realtime``; route modules may import it at module
top.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from chat.tasks.constants import TASK_RUN_ACTIVE_STATUSES
from chat.tasks.questions import list_run_questions
from db import task_store, tool_wait_handle_store
from db.models import ToolWaitHandleKind
from db.task_store import BriefVersionNotFoundError

logger = logging.getLogger(__name__)

# The wait-handle kinds whose cards the thread shows inline (the question
# kind has its own ``questions`` list).
_OPEN_REQUEST_HANDLE_KINDS = (
    ToolWaitHandleKind.ACTION_REQUEST.value,
    ToolWaitHandleKind.SERVICE_CONNECTION.value,
)
# The transcript message types of those cards (``ActionRequestMessage`` and
# ``ConnectRequestMessage`` in the frontend).
_OPEN_REQUEST_MESSAGE_TYPES = frozenset({"action_request", "connect_request"})


# ---------------------------------------------------------------------------
# Brief authorship
# ---------------------------------------------------------------------------


def source_run_id(source: Optional[str]) -> Optional[str]:
    """The run id of a ``run:<run_id>`` brief source, else None."""
    if isinstance(source, str) and source.startswith("run:"):
        return source[len("run:"):] or None
    return None


async def _brief_authorship(task_id: str, brief_version: int) -> tuple[Optional[str], Optional[int]]:
    """``(brief_source, brief_run_seq)`` of the task's current brief version:
    both None for version 0 or a pruned row; the seq None unless the source
    is a run that still exists."""
    if not brief_version:
        return None, None
    source = await task_store.get_brief_version_source(task_id, brief_version)
    run_id = source_run_id(source)
    if run_id is None:
        return source, None
    seqs = await task_store.get_run_seqs(task_id, [run_id])
    return source, seqs.get(run_id)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def task_list_row(task: dict) -> dict:
    """A ``GET /tasks`` row from a ``task_store.list_tasks`` row:
    ``{id, title, archived, last_activity_at, active_run, unread, event_seq,
    unread_seq, read_seq}``."""
    return {
        "id": task["id"],
        "title": task["title"],
        "archived": task["archived"],
        "last_activity_at": task["last_activity_at"],
        "active_run": task.get("active_run"),
        "unread": task["unread"],
        "event_seq": task["event_seq"],
        "unread_seq": task["unread_seq"],
        "read_seq": task["read_seq"],
    }


async def build_task_detail(task: dict) -> dict:
    """The task detail from a task dict (``GET /tasks/{id}`` and every
    route that returns the task).

    Shape::

        {id, title, title_is_default, archived, instructions, brief,
         brief_version, brief_updated_at, brief_source, brief_run_seq,
         model, thinking_effort, run_count, active_run: {id, seq, status}
         | None, event_seq, unread_seq, read_seq, unread, created_at,
         updated_at, last_activity_at}
    """
    task_id = task["id"]
    active = await task_store.get_active_run(task_id)
    run_count = await task_store.count_runs(task_id)
    brief_source, brief_run_seq = await _brief_authorship(task_id, task["brief_version"])
    return {
        "id": task_id,
        "title": task["title"],
        "title_is_default": task["title_is_default"],
        "archived": task["archived"],
        "instructions": task["instructions"],
        "brief": task["brief"],
        "brief_version": task["brief_version"],
        "brief_updated_at": task["brief_updated_at"],
        "brief_source": brief_source,
        "brief_run_seq": brief_run_seq,
        "model": task["model"],
        "thinking_effort": task["thinking_effort"],
        "run_count": run_count,
        "active_run": (
            {"id": active["id"], "seq": active["seq"], "status": active["status"]}
            if active is not None else None
        ),
        "event_seq": task["event_seq"],
        "unread_seq": task["unread_seq"],
        "read_seq": task["read_seq"],
        "unread": task["unread"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "last_activity_at": task["last_activity_at"],
    }


def read_state(task: dict) -> dict:
    """``{event_seq, unread_seq, read_seq, unread}`` of a task dict (the
    read acknowledgement's response)."""
    return {
        "event_seq": task["event_seq"],
        "unread_seq": task["unread_seq"],
        "read_seq": task["read_seq"],
        "unread": task["unread"],
    }


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


async def build_brief_state(task_id: str, state: dict) -> dict:
    """The brief state from a store ``{brief, brief_version,
    brief_updated_at}``: ``{brief, brief_version, brief_updated_at,
    brief_source, brief_run_seq}``."""
    brief_source, brief_run_seq = await _brief_authorship(task_id, state["brief_version"])
    return {
        "brief": state["brief"],
        "brief_version": state["brief_version"],
        "brief_updated_at": state["brief_updated_at"],
        "brief_source": brief_source,
        "brief_run_seq": brief_run_seq,
    }


async def build_brief_versions(task_id: str) -> list[dict]:
    """The stored versions newest first: ``[{version, source, run_seq,
    created_at, excerpt}]``, the run seqs resolved in one query."""
    versions = await task_store.list_brief_versions(task_id)
    seqs = await task_store.get_run_seqs(
        task_id, [rid for v in versions if (rid := source_run_id(v["source"]))],
    )
    return [
        {
            "version": v["version"],
            "source": v["source"],
            "run_seq": seqs.get(source_run_id(v["source"]) or ""),
            "created_at": v["created_at"],
            "excerpt": v["excerpt"],
        }
        for v in versions
    ]


async def build_brief_version(task_id: str, version: int) -> dict:
    """One stored version: ``{version, source, run_seq, created_at,
    content}``.

    Raises:
        BriefVersionNotFoundError: never written, or pruned.
    """
    row = await task_store.get_brief_version(task_id, version)
    if row is None:
        raise BriefVersionNotFoundError(version)
    run_id = source_run_id(row["source"])
    run_seq = None
    if run_id is not None:
        run_seq = (await task_store.get_run_seqs(task_id, [run_id])).get(run_id)
    return {
        "version": row["version"],
        "source": row["source"],
        "run_seq": run_seq,
        "created_at": row["created_at"],
        "content": row["content"],
    }


# ---------------------------------------------------------------------------
# Thread pages
# ---------------------------------------------------------------------------


def _run_view(run: dict, questions: list[dict], open_requests: list[dict]) -> dict:
    active = run["status"] in TASK_RUN_ACTIVE_STATUSES
    return {
        "id": run["id"],
        "seq": run["seq"],
        "conversation_id": run["conversation_id"],
        "prompt": run["prompt"],
        "status": run["status"],
        # An active run's entry is a pending one (a brief conflict being
        # merged): not the run's answer yet.
        "summary": None if active else run["summary"],
        "result": None if active else run.get("result"),
        "notes": None if active else run["notes"],
        "handoff_missing": run["handoff_missing"],
        "error": run["error"],
        "model": run["model"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "questions": questions,
        "open_requests": open_requests,
    }


async def _read_transcript_messages(conversation_id: str) -> list:
    """The transcript's messages, read off the event loop; a failed threaded
    read (a concurrent writer mid-write) is retried once on the loop, as
    ``_read_transcript`` in chat/routes/conversations.py does. A missing
    transcript reads as no messages."""
    from chat.storage import ChatStorage

    try:
        conversation = await asyncio.to_thread(ChatStorage.get_conversation, conversation_id)
    except Exception:
        logger.debug(
            "[tasks] threaded transcript read failed for %s; retrying on the loop",
            conversation_id, exc_info=True,
        )
        try:
            conversation = ChatStorage.get_conversation(conversation_id)
        except Exception:
            logger.warning(
                "[tasks] reading the transcript of %s failed", conversation_id,
                exc_info=True,
            )
            return []
    return (conversation or {}).get("messages") or []


async def list_open_requests(runs: list[dict]) -> dict[str, list[dict]]:
    """The open approval and connect cards of a page of runs, keyed by run
    id (every run present; ``[]`` for a finished run or one with none).

    Only active runs are looked at: one query for the pending
    ``action_request`` / ``service_connection`` handles of their
    conversations, then a transcript read only for the conversations that
    have any, keeping the card rows whose ``wait_handle_id`` is pending, in
    transcript order.
    """
    by_run: dict[str, list[dict]] = {run["id"]: [] for run in runs}
    run_by_conversation = {
        run["conversation_id"]: run["id"]
        for run in runs
        if run["status"] in TASK_RUN_ACTIVE_STATUSES
    }
    if not run_by_conversation:
        return by_run
    handles = await tool_wait_handle_store.list_pending_for_conversations(
        list(run_by_conversation), kinds=_OPEN_REQUEST_HANDLE_KINDS,
    )
    pending: dict[str, set[str]] = {}
    for handle in handles:
        pending.setdefault(handle["conversation_id"], set()).add(handle["id"])
    if not pending:
        return by_run
    conversation_ids = list(pending)
    transcripts = await asyncio.gather(
        *(_read_transcript_messages(cid) for cid in conversation_ids)
    )
    for conversation_id, messages in zip(conversation_ids, transcripts):
        handle_ids = pending[conversation_id]
        by_run[run_by_conversation[conversation_id]] = [
            m for m in messages
            if isinstance(m, dict)
            and m.get("type") in _OPEN_REQUEST_MESSAGE_TYPES
            and m.get("wait_handle_id") in handle_ids
        ]
    return by_run


async def build_runs_page(
    task_id: str, *, before_seq: Optional[int] = None, limit: int = 20,
) -> dict:
    """One page of the thread (``GET /tasks/{id}/runs``): ``{"runs": [run
    view, ...], "next_before_seq": int | None}``, newest first.

    Run view::

        {id, seq, conversation_id, prompt, status, summary, result, notes,
         handoff_missing, error, model, started_at, finished_at,
         questions: [question_view, ...],          # oldest first
         open_requests: [transcript card row, ...]}
    """
    page = await task_store.list_runs_page(task_id, before_seq=before_seq, limit=limit)
    runs = page["runs"]
    questions = await list_run_questions(runs)
    open_requests = await list_open_requests(runs)
    return {
        "runs": [
            _run_view(run, questions.get(run["id"], []), open_requests.get(run["id"], []))
            for run in runs
        ],
        "next_before_seq": page["next_before_seq"],
    }
