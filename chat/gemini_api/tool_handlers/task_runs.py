"""Quest Task run log read tools: ``list_task_runs`` and ``read_task_run``.

Both read the log of the Quest Task the calling conversation is a task run
of (spec 00012 sections 5 and 6.2). The task comes from ``ToolContext``
(``task_id``), never from an argument, so a call can only ever name runs of
its own task: ``read_task_run(run=N)`` looks the number up within that task,
and run N of another task is simply not found. Callers: the run's top-level
model, its sub-agents and its sandbox scripts (the script bridge passes the
lease's task ids); outside a task run both refuse with ``not_task_run``.

Hidden vs missing (spec section 10, invariant 7): a task the caller does not
own answers exactly like a missing one (``task_not_found`` with the one
not-found text), and a run number that was never allocated answers exactly
like one whose entry was deleted (``run_not_found``).

An ACTIVE run (``running`` / ``waiting``) has no published log entry yet. A
``brief_conflict`` (chat/tasks/runs.py) can leave a pending summary, result
and notes saved on a run that is still active; both tools show such a run
as in progress with ``summary``, ``result`` and ``notes`` null, and mark the
calling run as the current one.

Every refusal is JSON ``{"error": <message>, "code": <code>}``; the handlers
never raise.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from chat.memory_prompt import index_snippet
from chat.tasks.constants import (
    TASK_LOG_INDEX_SUMMARY_CHARS,
    TASK_RUN_ACTIVE_STATUSES,
    TASK_RUN_LIST_PAGE_SIZE,
    task_not_found_message,
    tasks_disabled_message,
)
from chat.tasks.questions import list_run_questions
from db import task_store

if TYPE_CHECKING:  # pragma: no cover
    from chat.gemini_api.tool_dispatch import ToolContext

logger = logging.getLogger(__name__)

_NOT_TASK_RUN_MESSAGE = (
    "list_task_runs and read_task_run are only available in a task run of a "
    "Quest Task (and its sub-agents and scripts)."
)
_IN_PROGRESS_NOTE = (
    "This task run is still in progress: it has no log entry yet."
)
_CURRENT_NOTE = (
    "This is the current task run (the one you are part of): it has no log "
    "entry until complete_task_run accepts it."
)


def _error(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code})


def _run_not_found(seq: Any) -> str:
    """The ONE run-not-found body: the same for a number never allocated
    and for a run whose entry was deleted."""
    return _error(
        "run_not_found",
        f"Task run #{seq} not found in this Quest Task. Use list_task_runs "
        "to see the existing run numbers.",
    )


async def _owned_task(ctx: "ToolContext") -> tuple[dict | None, str | None]:
    """The calling run's task, or ``(None, error JSON)``.

    Order: outside a task run (``not_task_run``), gate closed for the user
    (``tasks_disabled``), task missing or not the caller's
    (``task_not_found``, one body for both).
    """
    if not ctx.task_id:
        return None, _error("not_task_run", _NOT_TASK_RUN_MESSAGE)
    from config.feature_gates import tasks_enabled_for

    if not tasks_enabled_for(ctx.user.get("email") or ""):
        return None, _error("tasks_disabled", tasks_disabled_message())
    task = await task_store.get_task(ctx.user["id"], ctx.task_id)
    if task is None:
        return None, _error("task_not_found", task_not_found_message(ctx.task_id))
    return task, None


def _coerce_int(value: Any, *, name: str, minimum: int) -> int | str:
    """``value`` as an int >= ``minimum`` (an int, or a numeric string,
    optionally ``#``-prefixed), or an error message."""
    if isinstance(value, bool):
        return f"{name} must be an integer."
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str):
        text = value.strip().lstrip("#").strip()
        try:
            value = int(text)
        except ValueError:
            return f"{name} must be an integer."
    if not isinstance(value, int):
        return f"{name} must be an integer."
    if value < minimum:
        return f"{name} must be at least {minimum}."
    return value


def _date(started_at: str | None) -> str | None:
    """``YYYY-MM-DD`` (UTC) of an ISO timestamp."""
    return started_at[:10] if isinstance(started_at, str) else None


def _state_fields(run: dict, ctx: "ToolContext") -> dict:
    """``in_progress`` / ``current`` / ``note`` for an active run, else {}."""
    if run["status"] not in TASK_RUN_ACTIVE_STATUSES:
        return {}
    if run["id"] == ctx.task_run_id:
        return {"in_progress": True, "current": True, "note": _CURRENT_NOTE}
    return {"in_progress": True, "note": _IN_PROGRESS_NOTE}


async def handle_list_task_runs(ctx: "ToolContext", args: dict) -> str:
    """``list_task_runs(offset?)``: one page of the task's run log.

    Newest first, ``TASK_RUN_LIST_PAGE_SIZE`` entries a page. Each entry is
    ``{run, date, status, summary}`` plus, when they apply,
    ``handoff_missing``, ``prompt_excerpt`` (a run without a summary),
    ``in_progress`` / ``current`` / ``note`` (an active run, whose summary
    is null). Returns ``{"runs", "offset", "next_offset", "total"}``;
    ``next_offset`` is null on the last page.
    """
    try:
        raw_offset = args.get("offset", 0)
        offset = _coerce_int(0 if raw_offset is None else raw_offset, name="offset", minimum=0)
        if isinstance(offset, str):
            return _error("invalid_argument", offset)
        task, error = await _owned_task(ctx)
        if error is not None:
            return error
        total = await task_store.count_runs(task["id"])
        rows = await task_store.list_run_index(
            task["id"], offset=offset, limit=TASK_RUN_LIST_PAGE_SIZE,
            prompt_chars=TASK_LOG_INDEX_SUMMARY_CHARS * 4,
        )
        entries = []
        for row in rows:
            state = _state_fields(row, ctx)
            summary = None if state else row["summary"]
            entry: dict[str, Any] = {
                "run": row["seq"],
                "date": _date(row["started_at"]),
                "status": row["status"],
                "summary": summary,
            }
            if row["handoff_missing"]:
                entry["handoff_missing"] = True
            if summary is None and row["prompt_head"]:
                entry["prompt_excerpt"] = index_snippet(
                    row["prompt_head"], TASK_LOG_INDEX_SUMMARY_CHARS,
                )
            entry.update(state)
            entries.append(entry)
        next_offset = offset + len(rows)
        return json.dumps({
            "runs": entries,
            "offset": offset,
            "next_offset": next_offset if rows and next_offset < total else None,
            "total": total,
        })
    except Exception:
        logger.exception(
            "[list_task_runs] failed (task=%s, run=%s, user=%s)",
            ctx.task_id, ctx.task_run_id, ctx.user.get("email"),
        )
        return _error("internal_error", "Could not read the task run log; try again.")


async def handle_read_task_run(ctx: "ToolContext", args: dict) -> str:
    """``read_task_run(run)``: one run of the task, by its number.

    Returns ``{run, status, started_at, finished_at, prompt, summary,
    result, notes, handoff_missing, error, questions}`` (``questions``
    oldest first, each ``{handle_id, question, options, answer, status,
    asked_at, answered_at}``), plus ``in_progress`` / ``current`` /
    ``note`` for an active run, whose ``summary``, ``result`` and ``notes``
    are null. A number this task has no run for is ``run_not_found``.
    """
    try:
        if "run" not in args or args.get("run") is None:
            return _error("invalid_argument", "run is required: the task run number (#N).")
        seq = _coerce_int(args.get("run"), name="run", minimum=1)
        if isinstance(seq, str):
            return _error("invalid_argument", seq)
        task, error = await _owned_task(ctx)
        if error is not None:
            return error
        run = await task_store.get_run_by_seq(task["id"], seq)
        if run is None:
            return _run_not_found(seq)
        questions = (await list_run_questions([run])).get(run["id"], [])
        state = _state_fields(run, ctx)
        body: dict[str, Any] = {
            "run": run["seq"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "prompt": run["prompt"],
            "summary": None if state else run["summary"],
            "result": None if state else run["result"],
            "notes": None if state else run["notes"],
            "handoff_missing": bool(run["handoff_missing"]),
            "error": run["error"],
            "questions": questions,
        }
        body.update(state)
        return json.dumps(body)
    except Exception:
        logger.exception(
            "[read_task_run] failed (task=%s, run=%s, user=%s)",
            ctx.task_id, ctx.task_run_id, ctx.user.get("email"),
        )
        return _error("internal_error", "Could not read the task run; try again.")
