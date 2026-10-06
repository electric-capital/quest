"""Routine run driver: run a routine prompt until the model signals completion.

A routine run (scheduled by chat/scheduler.py, or the one-click first
message of a routine conversation sent over the persistent WebSocket) is
unattended, and weaker models sometimes end their stream before the
routine's work is actually done -- a manual follow-up message would get
them to finish. To make that automatic, routine conversations expose the
``routine_completed`` tool (ROUTINE_TOP_LEVEL_TOOLS / PUBLIC_ROUTINE_TOOLS
in chat/llm/tool_schemas.py) and the system prompt instructs the model to
call it as its last tool call. :func:`drive_routine_run` runs the prompt
turn, and when the run ends WITHOUT that call, appends a durable
``routine_nudge`` notice to the transcript and runs one more turn whose
user-facing input is :data:`ROUTINE_COMPLETION_NUDGE`, asking the model to
verify all requested work was done and call the tool.

The nudge is deliberately NOT sent when the run ended on a suspend
(``create_action_request`` / ``wait_for_handles`` / Slack reply): the
dangling tool_use is closed by the wait-handle resume later, and a nudge
turn would race it. Nor is it sent when the run raised -- the exception
propagates to the caller, which records the failure.

Both drivers share this module so the completion rule cannot drift between
scheduled and one-click runs.
"""

import logging
from typing import Awaitable, Callable

from chat.storage import utc_timestamp

logger = logging.getLogger(__name__)

ROUTINE_COMPLETED_TOOL = "routine_completed"

# How many follow-up turns a run gets after ending without the completion
# call. One matches the observed failure mode (one manual "please continue"
# gets the model to finish); every extra turn costs a full context re-read.
MAX_ROUTINE_COMPLETION_NUDGES = 1

# The follow-up turn's input. It is delivered to the model as the next
# user-turn message (inside the usual message_metadata envelope) and shown
# in the transcript as a system notice (``routine_nudge`` message), not as a
# user bubble.
ROUTINE_COMPLETION_NUDGE = (
    "SYSTEM: The routine run ended without calling the routine_completed "
    "tool, so it is being treated as unfinished. Go back over the routine "
    "prompt and make sure every piece of requested work has actually been "
    "done -- finish anything that is still outstanding. When everything is "
    "complete, write your final summary and then call routine_completed as "
    "your last tool call. If part of the task cannot be completed, call "
    "routine_completed anyway and explain what is missing in its summary."
)

# Durable structured message type for the transcript notice.
ROUTINE_NUDGE_MESSAGE_TYPE = "routine_nudge"

# ``run_turn(message)`` runs one run_conversation_turn with that message
# text; the caller binds every other argument (user, conversation, model,
# on_event, messages_out, ...).
RunTurn = Callable[[str], Awaitable[None]]
EventSink = Callable[[dict], Awaitable[None]]


def _is_routine_completed_use(msg: dict) -> bool:
    if msg.get("type") != "tool_use":
        return False
    if msg.get("tool_name") == ROUTINE_COMPLETED_TOOL:
        return True
    # Weaker models route it through the generic tool_call router
    # (``tool_call(tool_name="routine_completed")``); the loop arm accepts
    # that spelling too (ROUTINE_COMPLETED_VIA_TOOL_CALL_KEY in
    # chat/gemini_api/turn_tools.py), so it counts here as well.
    if msg.get("tool_name") == "tool_call":
        tool_input = msg.get("tool_input") or {}
        return isinstance(tool_input, dict) and (
            tool_input.get("tool_name") == ROUTINE_COMPLETED_TOOL
        )
    return False


def routine_completed_called(messages: list[dict]) -> bool:
    """Whether *messages* contain a ``routine_completed`` tool_use (either
    the top-level call or the ``tool_call``-routed spelling)."""
    return any(_is_routine_completed_use(msg) for msg in messages)


def run_ended_suspended(messages: list[dict]) -> bool:
    """Whether the run left a dangling tool_use (a suspend sentinel unwound it).

    ``run_conversation_turn`` returns normally on the suspend sentinels
    (action request card, wait_for_handles, Slack reply); the only trace in
    the structured messages is a ``tool_use`` with no matching
    ``tool_result``. A later wait-handle resume closes it, so the run must
    NOT be nudged.
    """
    result_ids = {
        msg.get("tool_id") for msg in messages if msg.get("type") == "tool_result"
    }
    return any(
        msg.get("type") == "tool_use" and msg.get("tool_id") not in result_ids
        for msg in messages
    )


def make_routine_nudge_message(attempt: int) -> dict:
    """Build the durable transcript notice for one nudge turn."""
    return {
        "type": ROUTINE_NUDGE_MESSAGE_TYPE,
        "role": "system",
        "content": ROUTINE_COMPLETION_NUDGE,
        "attempt": attempt,
        "timestamp": utc_timestamp(),
    }


async def drive_routine_run(
    run_turn: RunTurn,
    *,
    prompt: str,
    messages_out: list[dict],
    on_event: EventSink,
    conversation_id: str,
    log_prefix: str,
) -> bool:
    """Run a routine prompt, nudging the model until it calls routine_completed.

    Args:
        run_turn: Runs one ``run_conversation_turn`` with the given message
            text, appending its structured messages to *messages_out*.
        prompt: The routine prompt (the first turn's message).
        messages_out: The shared structured-message list every turn appends
            to; the nudge notice is appended here too, in order.
        on_event: The caller's event sink. The nudge notice is emitted
            through it as a ``routine_nudge`` event (in FLUSH_EVENT_TYPES)
            so callers that flush incrementally persist it right away.
        conversation_id: For logging only.
        log_prefix: Caller tag for log lines (``[scheduler]`` / ``[realtime]``).

    Returns:
        True when the model called ``routine_completed`` (in the prompt
        turn or a nudge turn); False when the run suspended on a wait
        handle or exhausted its nudges without the call. Exceptions from
        ``run_turn`` propagate unchanged.
    """
    turn_start = len(messages_out)
    await run_turn(prompt)

    for attempt in range(1, MAX_ROUTINE_COMPLETION_NUDGES + 2):
        produced = messages_out[turn_start:]
        if routine_completed_called(produced):
            if attempt > 1:
                logger.info(
                    "%s Routine run completed after nudge %d (conversation=%s)",
                    log_prefix, attempt - 1, conversation_id,
                )
            return True
        if run_ended_suspended(produced):
            logger.info(
                "%s Routine run suspended on a wait handle; not nudging "
                "(conversation=%s)", log_prefix, conversation_id,
            )
            return False
        if attempt > MAX_ROUTINE_COMPLETION_NUDGES:
            break

        logger.info(
            "%s Routine run ended without routine_completed; nudging "
            "(attempt %d/%d, conversation=%s)",
            log_prefix, attempt, MAX_ROUTINE_COMPLETION_NUDGES, conversation_id,
        )
        notice = make_routine_nudge_message(attempt)
        messages_out.append(notice)
        await on_event({**notice, "conversation_id": conversation_id})

        turn_start = len(messages_out)
        await run_turn(ROUTINE_COMPLETION_NUDGE)

    logger.warning(
        "%s Routine run still ended without routine_completed after %d "
        "nudge(s) (conversation=%s)",
        log_prefix, MAX_ROUTINE_COMPLETION_NUDGES, conversation_id,
    )
    return False
