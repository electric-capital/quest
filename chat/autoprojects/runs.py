"""AutoProject run driver and completion: how a run of an AutoProject finishes.

An AutoProject run (spec issue #213, sections 6.2 and 6.3) is one conversation
that answers one prompt and must end by calling ``complete_autoproject_run``,
which writes the run's log entry (and optionally a new brief) straight into
the AutoProject. Two pieces live here:

- :func:`complete_autoproject_run`, the body of the ``complete_autoproject_run`` loop
  tool (``_handle_complete_autoproject_run`` in chat/gemini_api/turn_tools.py).
  Validates the whole call before writing anything, applies the brief
  against the version the run's prompt was built from (a brief the user
  edited during the run is a ``brief_conflict``: the entry is saved and the
  run stays open for a merge), completes the run and tells the loop to end
  with no further model call.
- :func:`drive_autoproject_run`, modelled on ``drive_routine_run``
  (chat/routine_runs.py), which wraps the first turn of a run
  (``_run_send_message`` in chat/realtime/socket.py) and every wait-handle
  resume turn (``_run_resume`` in chat/wait_handles/resume.py) and sets the
  run's status from what the turn left behind:

  ===========================================  ==============================
  The turn ended                                Run becomes
  ===========================================  ==============================
  with ``complete_autoproject_run`` accepted           ``completed`` (by the tool)
  suspended (``run_ended_suspended()``)         ``waiting``
  without either, nudges left                   one more turn with
                                                :data:`AUTOPROJECT_COMPLETION_NUDGE`
  without either after the nudge                ``completed`` with
                                                ``handoff_missing``
  on a run-fatal error                          ``failed`` with ``error``
  by the user's Stop (cancelled)                ``stopped``
  ===========================================  ==============================

  The driver decides from the run row, not from the transcript: the tool
  writes the row, so a re-read after each turn says whether it finished.

:func:`stop_autoproject_run` is the user's Stop for a run with no live turn to
cancel (a ``waiting`` run, or Stop on an approval card).

Every status change bumps the AutoProject's ``event_seq`` (in the store) and
publishes ``autoproject_changed`` and ``autoproject_list_changed``
(:func:`_after_status_change`). Completed, failed and waiting runs mark the
AutoProject unread; a stopped one does not (the user was looking at it; spec 10).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from chat.routine_runs import run_ended_suspended
from chat.storage import utc_timestamp
from chat.autoprojects.constants import (
    MAX_AUTOPROJECT_COMPLETION_NUDGES,
    AUTOPROJECT_RUN_ACTIVE_STATUSES,
    AUTOPROJECT_RUN_FINISHED_STATUSES,
    AUTOPROJECT_RUN_RESULT_MAX_CHARS,
    AUTOPROJECT_RUN_STATUS_FAILED,
    AUTOPROJECT_RUN_STATUS_RUNNING,
    AUTOPROJECT_RUN_STATUS_STOPPED,
    AUTOPROJECT_RUN_STATUS_WAITING,
    AUTOPROJECT_RUN_SUMMARY_MAX_CHARS,
)
from chat.autoprojects.events import publish_autoproject_state
from db import autoproject_store
from db.autoproject_store import (
    StaleBriefError,
    AutoProjectNotFoundError,
    AutoProjectRunFinishedError,
    AutoProjectRunNotFoundError,
    AutoProjectStoreError,
    AutoProjectValidationError,
)

logger = logging.getLogger(__name__)

# Durable structured message type of the transcript notice for a nudge turn
# (in FLUSH_EVENT_TYPES, chat/_flush_helper.py).
AUTOPROJECT_NUDGE_MESSAGE_TYPE = "autoproject_nudge"

# The nudge turn's input when the run ended without complete_autoproject_run. It
# reaches the model as the next user-turn message (inside the usual
# message_metadata envelope) and shows in the transcript as a system notice
# (``autoproject_nudge`` message), not as a user bubble.
AUTOPROJECT_COMPLETION_NUDGE = (
    "SYSTEM: Your turn ended without calling complete_autoproject_run, so this "
    "AutoProject run has not saved its result or its handoff "
    "notes, and the user has seen nothing of it: the AutoProject thread shows only "
    "the result of complete_autoproject_run. Finish any work the user's prompt "
    "asked for that is still outstanding, then call complete_autoproject_run as "
    "your last tool call with summary (one line), result (your answer to "
    "the user), notes (the handoff to the next run) and, if the state "
    "of the work changed, brief (the whole brief, rewritten). If you cannot "
    "go on without the user's input, call ask_autoproject_question instead. This "
    "is the only reminder: if this turn also ends without either call, your "
    "last reply is saved as the result, with no notes."
)

# The nudge turn's input when the run has a pending entry: complete_autoproject_run
# returned brief_conflict and the model never retried.
AUTOPROJECT_BRIEF_CONFLICT_NUDGE = (
    "SYSTEM: This AutoProject run is not finished yet. Your last "
    "complete_autoproject_run call returned brief_conflict because the user edited "
    "the brief during this run: your log entry is saved, but the run "
    "stays open until the brief is settled. Merge your changes into the "
    "current brief from that brief_conflict result, keeping the user's "
    "edits where they overlap, and call complete_autoproject_run again with the "
    "merged brief, brief_expected_version set to that result's "
    "brief_version, and the same summary, result and notes. If your changes "
    "to the brief no longer apply, call it again without brief."
)

# What a refused complete_autoproject_run argument should do next, by error code
# (appended to the validation message; nothing was saved and the run stays
# open). Codes not listed get _REFUSAL_HINT_DEFAULT.
_REFUSAL_HINTS: dict[str, str] = {
    "summary_too_long": "Shorten summary to one short line",
    "result_too_long": (
        "Shorten result: put long material in an autoproject:// file, a Quest Doc "
        "or a Quest Table and point at it from the result"
    ),
    "notes_too_long": (
        "Shorten notes to facts and pointers: put long material in a "
        "autoproject:// file and point at it from the notes"
    ),
    "brief_too_long": (
        "Shorten brief to the current state of the work: drop history (the "
        "log keeps it) and put long material in an autoproject:// file the brief "
        "points at"
    ),
    "invalid_summary": "Pass summary as a string",
    "invalid_result": "Pass result as a string",
    "invalid_notes": "Pass notes as a string",
    "invalid_brief": "Pass brief as a string, or leave it out",
}
_REFUSAL_HINT_DEFAULT = "Fix the argument"

# ``run_turn(message)`` runs one run_conversation_turn with that message
# text; the caller binds every other argument. An empty message is a
# wait-handle resume turn.
RunTurn = Callable[[str], Awaitable[None]]
EventSink = Callable[[dict], Awaitable[None]]

# Leading characters stripped from the first line of a run's final text to
# make the summary of an entry the driver writes itself (markdown headings,
# quotes, list bullets).
_SUMMARY_LEADING_MARKERS = "#>*- \t"

# ``failed`` runs keep at most this much of the exception text.
_RUN_ERROR_MAX_CHARS = 2000


def make_autoproject_nudge_message(attempt: int, content: str) -> dict:
    """Build the durable transcript notice for one nudge turn."""
    return {
        "type": AUTOPROJECT_NUDGE_MESSAGE_TYPE,
        "role": "system",
        "content": content,
        "attempt": attempt,
        "timestamp": utc_timestamp(),
    }


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------


async def _after_status_change(run: dict, *, mark_unread: bool) -> None:
    """Follow-up of every run status change: mark the AutoProject unread when the
    run left something for the user (``mark_unread``; after the status
    write, since it copies the bumped ``event_seq``), then publish
    ``autoproject_changed`` and ``autoproject_list_changed`` (the row's ``active_run``
    and maybe ``unread`` moved). Best-effort: never raises."""
    autoproject_id = run["autoproject_id"]
    if mark_unread:
        try:
            await autoproject_store.mark_autoproject_unread(autoproject_id)
        except Exception:
            logger.warning(
                "[autoproject-run] marking AutoProject %s unread failed", autoproject_id, exc_info=True,
            )
    await publish_autoproject_state(autoproject_id)


async def _cancel_run_wait_handles(user_id: int, conversation_id: str) -> None:
    """Cancel the pending wait handles of a run's conversation (a card or
    question still open on a run that just finished). Best-effort."""
    from chat.gemini_api.session import cancel_pending_wait_handles_for_conversation

    try:
        await cancel_pending_wait_handles_for_conversation(user_id, conversation_id)
    except Exception:
        logger.warning(
            "[autoproject-run] cancelling pending wait handles failed (conversation=%s)",
            conversation_id, exc_info=True,
        )


async def stop_autoproject_run(user_id: int, conversation_id: str) -> Optional[dict]:
    """The user's Stop for an AutoProject run: mark it ``stopped`` and cancel its
    pending wait handles.

    Covers what cancelling the live turn cannot: a ``waiting`` run (no turn
    is running) and the Stop button of an approval card, which for an AutoProject
    run ends the run instead of waiting for a next message it never gets.
    Idempotent and a no-op for a conversation that is not an AutoProject run, a run
    that already finished, and a run of an AutoProject ``user_id`` does not own.

    Returns:
        The run after the change, or None when nothing was stopped. Never
        raises.
    """
    try:
        run = await autoproject_store.get_run_by_conversation(conversation_id)
        if run is None:
            return None
        autoproject = await autoproject_store.get_autoproject_by_id(run["autoproject_id"])
        if autoproject is None or autoproject["user_id"] != user_id:
            return None
        stopped = await autoproject_store.set_run_status(run["id"], AUTOPROJECT_RUN_STATUS_STOPPED)
    except Exception:
        logger.exception(
            "[autoproject-run] stopping the AutoProject run failed (conversation=%s)",
            conversation_id,
        )
        return None
    await _cancel_run_wait_handles(user_id, conversation_id)
    if stopped is None:
        return None
    logger.info(
        "[autoproject-run] AutoProject run stopped by the user (autoproject=%s, run=%s, seq=%s)",
        stopped["autoproject_id"], stopped["id"], stopped["seq"],
    )
    await _after_status_change(stopped, mark_unread=False)
    return stopped


# ---------------------------------------------------------------------------
# complete_autoproject_run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionOutcome:
    """What the ``complete_autoproject_run`` handler returns to the loop."""

    # The tool-result JSON string.
    result: str
    # True: the run is completed and the loop must end now, with no
    # further model call.
    ended: bool


def _tool_error(message: str, code: str, **details: Any) -> CompletionOutcome:
    return CompletionOutcome(
        result=json.dumps({"error": message, "code": code, **details}),
        ended=False,
    )


def _store_error(exc: AutoProjectStoreError) -> CompletionOutcome:
    return _tool_error(exc.message, exc.code, **exc.details)


def _refusal(exc: AutoProjectValidationError) -> CompletionOutcome:
    """A refused argument, with what to do next: the store's validation
    texts are shared with the HTTP routes, so the model-facing hint is
    added here."""
    hint = _REFUSAL_HINTS.get(exc.code, _REFUSAL_HINT_DEFAULT)
    message = (
        f"{exc.message} Nothing was saved and this run is still open. "
        f"{hint}, then call complete_autoproject_run again."
    )
    return _tool_error(message, exc.code, **exc.details)


def _parse_brief_expected_version(value: Any) -> Optional[int]:
    """None when absent; else a non-negative int (bool excluded).

    Raises:
        AutoProjectValidationError ``invalid_brief_expected_version``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AutoProjectValidationError(
            "invalid_brief_expected_version",
            "brief_expected_version must be a non-negative integer: the "
            "brief_version from the brief_conflict result.",
        )
    return value


async def complete_autoproject_run(
    *,
    autoproject_id: str,
    run_id: str,
    context_brief_version: Optional[int],
    args: dict,
) -> CompletionOutcome:
    """Finish the calling AutoProject run with its log entry (``complete_autoproject_run``).

    The run is the calling conversation's own (invariant 5): ``autoproject_id`` and
    ``run_id`` come from the run context, never from ``args``.

    Order: validate every argument before writing anything (a refusal
    leaves the run open so the model can retry); apply ``brief`` when given
    (against ``brief_expected_version``, else the version the run's prompt
    was built from); write the entry and mark the run ``completed``; apply
    ``autoproject_title`` while the title is still the default one (invariant 4);
    cancel the run's still-pending wait handles; mark the AutoProject unread and
    publish.

    A brief the user changed during the run (``StaleBriefError``) saves the
    entry without finishing the run and returns ``brief_conflict`` with the
    current brief, so the model can merge and call again.

    Args:
        autoproject_id: The run's AutoProject (``RunContext.autoproject_id``).
        run_id: The run (``RunContext.autoproject_run_id``).
        context_brief_version: The brief version the run's ``## AutoProject``
            section was built from (``RunContext.autoproject_context_brief_version``).
        args: The tool's arguments: ``summary``, ``result``, ``notes``,
            optional ``brief``, ``brief_expected_version``, ``autoproject_title``.

    Returns:
        The tool result and whether the loop must end. Refusals are tool
        errors ``{error, code, ...}`` with ``ended=False``. Unexpected
        exceptions (a store failure) propagate; the handler turns them into
        a generic tool error.
    """
    # 1. Validate everything first.
    try:
        summary, result, notes = autoproject_store.validate_run_entry(
            args.get("summary"), args.get("result"), args.get("notes"),
        )
        raw_brief = args.get("brief")
        brief: Optional[str] = None
        # A blank brief is treated as omitted, never as "clear the brief".
        if raw_brief is not None and not (
            isinstance(raw_brief, str) and not raw_brief.strip()
        ):
            brief = autoproject_store.validate_brief(raw_brief)
        brief_expected_version = _parse_brief_expected_version(
            args.get("brief_expected_version"),
        )
    except AutoProjectValidationError as exc:
        return _refusal(exc)

    # 2. The run must still be active.
    run = await autoproject_store.get_run(run_id)
    if run is None or run["autoproject_id"] != autoproject_id:
        return _store_error(AutoProjectRunNotFoundError(run_id))
    if run["status"] not in AUTOPROJECT_RUN_ACTIVE_STATUSES:
        return _store_error(AutoProjectRunFinishedError(run_id, run["status"]))
    autoproject = await autoproject_store.get_autoproject_by_id(autoproject_id)
    if autoproject is None:
        return _store_error(AutoProjectNotFoundError(autoproject_id))
    brief_version = autoproject["brief_version"]

    # 3. The brief, against the version the run started from.
    if brief is not None:
        expected = brief_expected_version
        if expected is None:
            expected = context_brief_version
        if expected is None:
            expected = run.get("context_brief_version")
        if expected is None:
            logger.warning(
                "[autoproject-run] run %s has no starting brief version; writing its "
                "brief against the current version %s", run_id, brief_version,
            )
            expected = brief_version
        try:
            state = await autoproject_store.set_brief(
                autoproject_id, brief, expected_version=expected, source=f"run:{run_id}",
            )
        except StaleBriefError as exc:
            try:
                await autoproject_store.save_run_entry(
                    run_id, summary=summary, result=result, notes=notes,
                )
            except AutoProjectStoreError as save_exc:
                return _store_error(save_exc)
            current = exc.current
            logger.info(
                "[autoproject-run] brief_conflict for run %s (expected version %s, "
                "current %s); entry saved, run left open",
                run_id, expected, current.get("brief_version"),
            )
            return CompletionOutcome(
                result=json.dumps({
                    "status": "brief_conflict",
                    "entry_saved": True,
                    "brief_conflict": {
                        "brief": current.get("brief"),
                        "brief_version": current.get("brief_version"),
                    },
                    "message": (
                        "The user edited the brief of this AutoProject during "
                        "this run. Your log entry is saved, but the run "
                        "stays open until the brief is settled. Merge your "
                        "changes into the current brief in brief_conflict, "
                        "keeping the user's edits where they overlap, and "
                        "call complete_autoproject_run again with the merged brief, "
                        "brief_expected_version="
                        f"{current.get('brief_version')} and the same "
                        "summary, result and notes. If your changes to the "
                        "brief no longer apply, call it again without brief."
                    ),
                }),
                ended=False,
            )
        except AutoProjectStoreError as exc:
            return _store_error(exc)
        brief_version = state["brief_version"]
        # The brief moved; the list row did not.
        await publish_autoproject_state(autoproject_id, list_changed=False)

    # 4. The entry, and the run is completed.
    try:
        completed = await autoproject_store.complete_run(
            run_id, summary=summary, result=result, notes=notes,
        )
    except AutoProjectStoreError as exc:
        return _store_error(exc)

    # 5. The title, only while it is still the one cut from the first prompt.
    title_set = False
    raw_title = args.get("autoproject_title")
    if isinstance(raw_title, str) and raw_title.strip():
        try:
            title_set = await autoproject_store.set_title_from_run(
                autoproject_id, autoproject_store.title_from_prompt(raw_title),
            )
        except Exception:
            logger.warning(
                "[autoproject-run] applying autoproject_title of run %s failed", run_id,
                exc_info=True,
            )

    # 6. A card or question opened in the same batch stays open otherwise.
    await _cancel_run_wait_handles(autoproject["user_id"], run["conversation_id"])

    # 7. Unread and events.
    await _after_status_change(completed, mark_unread=True)

    logger.info(
        "[autoproject-run] AutoProject run completed via complete_autoproject_run (autoproject=%s, run=%s, "
        "seq=%s, brief_written=%s, title_set=%s)",
        autoproject_id, run_id, completed["seq"], brief is not None, title_set,
    )
    payload: dict[str, Any] = {
        "status": "completed",
        "run": completed["seq"],
        "brief_version": brief_version,
    }
    if title_set:
        payload["title_set"] = True
    return CompletionOutcome(result=json.dumps(payload), ended=True)


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


def _last_assistant_text(messages: list[dict]) -> Optional[str]:
    """The content of the last assistant ``text`` message, or None."""
    for msg in reversed(messages):
        if (
            isinstance(msg, dict)
            and msg.get("type") == "text"
            and msg.get("role") == "assistant"
            and isinstance(msg.get("content"), str)
            and msg["content"].strip()
        ):
            return msg["content"]
    return None


async def _final_text_from_history(conversation_id: str) -> Optional[str]:
    """The last assistant text of the run's saved transcript. Best-effort."""
    from chat.storage import ChatStorage

    try:
        data = await asyncio.to_thread(ChatStorage.get_conversation, conversation_id)
    except Exception:
        logger.debug(
            "[autoproject-run] reading the transcript failed (conversation=%s)",
            conversation_id, exc_info=True,
        )
        return None
    messages = (data or {}).get("messages") or []
    return _last_assistant_text(messages if isinstance(messages, list) else [])


def _cut(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def _summary_from_text(text: str) -> str:
    """The first non-empty line of ``text`` with leading markdown markers
    stripped and whitespace collapsed, cut to the summary cap."""
    for line in text.splitlines():
        clean = " ".join(line.lstrip(_SUMMARY_LEADING_MARKERS).split())
        if clean:
            return _cut(clean, AUTOPROJECT_RUN_SUMMARY_MAX_CHARS)
    return ""


async def _finish_without_completion(
    run: dict, messages_out: list[dict], *, log_prefix: str,
) -> Optional[dict]:
    """Complete a run whose nudges ran out without an accepted
    ``complete_autoproject_run``.

    A pending entry (a ``brief_conflict`` never retried) is kept as the
    log entry; the user's brief edit wins and the run's brief is dropped.
    Otherwise the entry is written with ``handoff_missing``: the run's final
    text as the result, its first line as the summary, no notes.

    Returns:
        The run after the change (or as it is, when it finished meanwhile).
    """
    run_id = run["id"]
    try:
        if run.get("summary") is not None:
            completed = await autoproject_store.complete_run(
                run_id,
                summary=run["summary"],
                result=run.get("result") or "",
                notes=run.get("notes") or "",
            )
            logger.info(
                "%s AutoProject run %s completed with its saved entry; its "
                "conflicting brief was dropped (conversation=%s)",
                log_prefix, run_id, run["conversation_id"],
            )
        else:
            text = _last_assistant_text(messages_out)
            if text is None:
                text = await _final_text_from_history(run["conversation_id"])
            text = (text or "").strip()
            completed = await autoproject_store.complete_run(
                run_id,
                summary=_summary_from_text(text),
                result=_cut(text, AUTOPROJECT_RUN_RESULT_MAX_CHARS),
                notes="",
                handoff_missing=True,
            )
            logger.warning(
                "%s AutoProject run %s ended without complete_autoproject_run after %d "
                "nudge(s); completed with handoff_missing (conversation=%s)",
                log_prefix, run_id, MAX_AUTOPROJECT_COMPLETION_NUDGES,
                run["conversation_id"],
            )
    except (AutoProjectRunFinishedError, AutoProjectRunNotFoundError):
        logger.info(
            "%s AutoProject run %s finished before the driver could complete it",
            log_prefix, run_id,
        )
        return await autoproject_store.get_run(run_id)
    await _after_status_change(completed, mark_unread=True)
    return completed


async def drive_autoproject_run(
    run_turn: RunTurn,
    *,
    run: dict,
    prompt: Optional[str],
    messages_out: list[dict],
    on_event: EventSink,
    log_prefix: str,
) -> Optional[dict]:
    """Run one stretch of an AutoProject run and set its status from how it ended.

    Wraps the first turn (``prompt`` is the user's message) and every
    wait-handle resume turn (``prompt`` None: re-checks the run first; a
    run no longer active, e.g. stopped or completed while a card was open,
    runs no turn at all; a ``waiting`` run goes back to ``running``). After
    each turn the run row decides (see the module docstring's table); a
    run that ended without ``complete_autoproject_run`` gets up to
    ``MAX_AUTOPROJECT_COMPLETION_NUDGES`` follow-up turns per call, each announced
    by a durable ``autoproject_nudge`` notice.

    Args:
        run_turn: Runs one ``run_conversation_turn`` with the given message
            text, appending its structured messages to ``messages_out``.
        run: The run dict (``autoproject_store``); its id and conversation are used.
        prompt: The first turn's message, or None for a resume turn.
        messages_out: The shared structured-message list every turn appends
            to; the nudge notice is appended here too, in order.
        on_event: The caller's event sink; the nudge notice is emitted
            through it (``autoproject_nudge`` is in FLUSH_EVENT_TYPES, so it is
            flushed before the nudge turn starts).
        log_prefix: Caller tag for log lines (``[realtime]`` /
            ``[wait-resume]``).

    Returns:
        The run after this stretch, or None when the row is gone.

    Raises:
        Whatever ``run_turn`` raises, after the run is marked ``stopped``
        (``asyncio.CancelledError``) or ``failed`` (any other exception),
        so the caller's own failure handling runs unchanged.
    """
    run_id = run["id"]
    conversation_id = run["conversation_id"]
    try:
        if prompt is None:
            current = await autoproject_store.get_run(run_id)
            if current is None:
                logger.warning(
                    "%s AutoProject run %s is gone; not resuming (conversation=%s)",
                    log_prefix, run_id, conversation_id,
                )
                return None
            if current["status"] not in AUTOPROJECT_RUN_ACTIVE_STATUSES:
                logger.info(
                    "%s AutoProject run %s is %s; not resuming (conversation=%s)",
                    log_prefix, run_id, current["status"], conversation_id,
                )
                return current
            if current["status"] == AUTOPROJECT_RUN_STATUS_WAITING:
                resumed = await autoproject_store.set_run_status(
                    run_id, AUTOPROJECT_RUN_STATUS_RUNNING,
                    expected_from={AUTOPROJECT_RUN_STATUS_WAITING},
                )
                if resumed is not None:
                    await _after_status_change(resumed, mark_unread=False)
        turn_start = len(messages_out)
        await run_turn(prompt or "")

        nudges = 0
        while True:
            current = await autoproject_store.get_run(run_id)
            if current is None:
                logger.warning(
                    "%s AutoProject run %s vanished during its turn (conversation=%s)",
                    log_prefix, run_id, conversation_id,
                )
                return None
            if current["status"] in AUTOPROJECT_RUN_FINISHED_STATUSES:
                # complete_autoproject_run (or a Stop) already wrote and published.
                if nudges:
                    logger.info(
                        "%s AutoProject run %s %s after %d nudge(s) (conversation=%s)",
                        log_prefix, run_id, current["status"], nudges,
                        conversation_id,
                    )
                return current
            if run_ended_suspended(messages_out[turn_start:]):
                waiting = await autoproject_store.set_run_status(
                    run_id, AUTOPROJECT_RUN_STATUS_WAITING,
                    expected_from={AUTOPROJECT_RUN_STATUS_RUNNING},
                )
                logger.info(
                    "%s AutoProject run %s suspended on a wait handle; waiting "
                    "(conversation=%s)", log_prefix, run_id, conversation_id,
                )
                if waiting is None:
                    return await autoproject_store.get_run(run_id)
                await _after_status_change(waiting, mark_unread=True)
                return waiting
            if nudges >= MAX_AUTOPROJECT_COMPLETION_NUDGES:
                break

            nudges += 1
            pending_entry = current.get("summary") is not None
            nudge_text = (
                AUTOPROJECT_BRIEF_CONFLICT_NUDGE if pending_entry else AUTOPROJECT_COMPLETION_NUDGE
            )
            logger.info(
                "%s AutoProject run %s ended without complete_autoproject_run; nudging "
                "(attempt %d/%d, pending_entry=%s, conversation=%s)",
                log_prefix, run_id, nudges, MAX_AUTOPROJECT_COMPLETION_NUDGES,
                pending_entry, conversation_id,
            )
            notice = make_autoproject_nudge_message(nudges, nudge_text)
            messages_out.append(notice)
            await on_event({**notice, "conversation_id": conversation_id})
            turn_start = len(messages_out)
            await run_turn(nudge_text)

        return await _finish_without_completion(
            current, messages_out, log_prefix=log_prefix,
        )
    except asyncio.CancelledError:
        try:
            stopped = await autoproject_store.set_run_status(run_id, AUTOPROJECT_RUN_STATUS_STOPPED)
            if stopped is not None:
                logger.info(
                    "%s AutoProject run %s stopped (conversation=%s)",
                    log_prefix, run_id, conversation_id,
                )
                await _after_status_change(stopped, mark_unread=False)
        except Exception:
            logger.exception(
                "%s Marking cancelled AutoProject run %s stopped failed",
                log_prefix, run_id,
            )
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:_RUN_ERROR_MAX_CHARS]
        try:
            failed = await autoproject_store.set_run_status(
                run_id, AUTOPROJECT_RUN_STATUS_FAILED, error=error,
            )
            if failed is not None:
                logger.info(
                    "%s AutoProject run %s failed: %s (conversation=%s)",
                    log_prefix, run_id, error, conversation_id,
                )
                await _after_status_change(failed, mark_unread=True)
        except Exception:
            logger.exception(
                "%s Recording the failure of AutoProject run %s failed",
                log_prefix, run_id,
            )
        raise
