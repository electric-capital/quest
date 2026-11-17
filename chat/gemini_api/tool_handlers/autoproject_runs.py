"""AutoProject run log read tools: ``list_autoproject_runs`` and ``read_autoproject_run``.

Both read the log of the AutoProject the calling conversation is an AutoProject run
of (spec 00012 sections 5 and 6.2). The AutoProject comes from ``ToolContext``
(``autoproject_id``), never from an argument, so a call can only ever name runs of
its own AutoProject: ``read_autoproject_run(run=N)`` looks the number up within that AutoProject,
and run N of another AutoProject is simply not found. Callers: the run's top-level
model, its sub-agents and its sandbox scripts (the script bridge passes the
lease's AutoProject ids); outside an AutoProject run both refuse with ``not_autoproject_run``.

Hidden vs missing (spec section 10, invariant 7): an AutoProject the caller does not
own answers exactly like a missing one (``autoproject_not_found`` with the one
not-found text), and a run number that was never allocated answers exactly
like one whose entry was deleted (``run_not_found``).

An ACTIVE run (``running`` / ``waiting``) has no published log entry yet. A
``brief_conflict`` (chat/autoprojects/runs.py) can leave a pending summary, result
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
from chat.autoprojects.constants import (
    AUTOPROJECT_LOG_INDEX_SUMMARY_CHARS,
    AUTOPROJECT_RUN_ACTIVE_STATUSES,
    AUTOPROJECT_RUN_LIST_PAGE_SIZE,
    autoproject_not_found_message,
    autoprojects_disabled_message,
)
from chat.autoprojects.questions import list_run_questions
from db import autoproject_store

if TYPE_CHECKING:  # pragma: no cover
    from chat.gemini_api.tool_dispatch import ToolContext

logger = logging.getLogger(__name__)

_NOT_AUTOPROJECT_RUN_MESSAGE = (
    "list_autoproject_runs and read_autoproject_run are only available in an AutoProject run "
    "(and its sub-agents and scripts)."
)
_IN_PROGRESS_NOTE = (
    "This run is still in progress: it has no log entry yet."
)
_CURRENT_NOTE = (
    "This is the current run (the one you are part of): it has no log "
    "entry until complete_autoproject_run accepts it."
)


def _error(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code})


def _run_not_found(seq: Any) -> str:
    """The ONE run-not-found body: the same for a number never allocated
    and for a run whose entry was deleted."""
    return _error(
        "run_not_found",
        f"Run #{seq} not found in this AutoProject. Use list_autoproject_runs "
        "to see the existing run numbers.",
    )


async def _owned_autoproject(ctx: "ToolContext") -> tuple[dict | None, str | None]:
    """The calling run's AutoProject, or ``(None, error JSON)``.

    Order: outside an AutoProject run (``not_autoproject_run``), gate closed for the user
    (``autoprojects_disabled``), AutoProject missing or not the caller's
    (``autoproject_not_found``, one body for both).
    """
    if not ctx.autoproject_id:
        return None, _error("not_autoproject_run", _NOT_AUTOPROJECT_RUN_MESSAGE)
    from config.feature_gates import autoprojects_enabled_for

    if not autoprojects_enabled_for(ctx.user.get("email") or ""):
        return None, _error("autoprojects_disabled", autoprojects_disabled_message())
    autoproject = await autoproject_store.get_autoproject(ctx.user["id"], ctx.autoproject_id)
    if autoproject is None:
        return None, _error(
            "autoproject_not_found", autoproject_not_found_message(ctx.autoproject_id),
        )
    return autoproject, None


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
    if run["status"] not in AUTOPROJECT_RUN_ACTIVE_STATUSES:
        return {}
    if run["id"] == ctx.autoproject_run_id:
        return {"in_progress": True, "current": True, "note": _CURRENT_NOTE}
    return {"in_progress": True, "note": _IN_PROGRESS_NOTE}


async def handle_list_autoproject_runs(ctx: "ToolContext", args: dict) -> str:
    """``list_autoproject_runs(offset?)``: one page of the AutoProject's run log.

    Newest first, ``AUTOPROJECT_RUN_LIST_PAGE_SIZE`` entries a page. Each entry is
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
        autoproject, error = await _owned_autoproject(ctx)
        if error is not None:
            return error
        total = await autoproject_store.count_runs(autoproject["id"])
        rows = await autoproject_store.list_run_index(
            autoproject["id"], offset=offset, limit=AUTOPROJECT_RUN_LIST_PAGE_SIZE,
            prompt_chars=AUTOPROJECT_LOG_INDEX_SUMMARY_CHARS * 4,
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
                    row["prompt_head"], AUTOPROJECT_LOG_INDEX_SUMMARY_CHARS,
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
            "[list_autoproject_runs] failed (autoproject=%s, run=%s, user=%s)",
            ctx.autoproject_id, ctx.autoproject_run_id, ctx.user.get("email"),
        )
        return _error("internal_error", "Could not read the AutoProject run log; try again.")


async def handle_read_autoproject_run(ctx: "ToolContext", args: dict) -> str:
    """``read_autoproject_run(run)``: one run of the AutoProject, by its number.

    Returns ``{run, status, started_at, finished_at, prompt, summary,
    result, notes, handoff_missing, error, questions}`` (``questions``
    oldest first, each ``{handle_id, question, options, answer, status,
    asked_at, answered_at}``), plus ``in_progress`` / ``current`` /
    ``note`` for an active run, whose ``summary``, ``result`` and ``notes``
    are null. A number this AutoProject has no run for is ``run_not_found``.
    """
    try:
        if "run" not in args or args.get("run") is None:
            return _error("invalid_argument", "run is required: the AutoProject run number (#N).")
        seq = _coerce_int(args.get("run"), name="run", minimum=1)
        if isinstance(seq, str):
            return _error("invalid_argument", seq)
        autoproject, error = await _owned_autoproject(ctx)
        if error is not None:
            return error
        run = await autoproject_store.get_run_by_seq(autoproject["id"], seq)
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
            "[read_autoproject_run] failed (autoproject=%s, run=%s, user=%s)",
            ctx.autoproject_id, ctx.autoproject_run_id, ctx.user.get("email"),
        )
        return _error("internal_error", "Could not read the AutoProject run; try again.")
