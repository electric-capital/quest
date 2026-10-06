"""Shared incremental flush of structured messages to chat_history.json.

Three callers (web WebSocket dispatch, Slack-driven dispatch, headless
wait-handle resume) all need the same behaviour: as the model emits
structured events, slice newly-appended messages from a shared buffer,
stamp missing timestamps, and append them to the conversation's
chat_history.json. Centralising the closure here keeps the event-set
constant in one place and avoids drift between call sites.
"""

import asyncio
import logging
from typing import Awaitable, Callable

from chat.storage import ChatStorage, _publish_appended_to_bus, utc_timestamp

logger = logging.getLogger(__name__)


# The set of event types after which a complete structured message (or
# batch of them) has been appended to messages_out. Flushing on these
# preserves ordering and minimises redundant disk writes compared to
# flushing on every streaming text chunk. The boundary save hook in
# run_conversation_turn keys off the same set so chat_history.json and
# sdk_history.json advance together.
FLUSH_EVENT_TYPES: frozenset[str] = frozenset({
    "tool_use",
    "tool_result",
    "action_request",
    "stats",
    # A run-fatal error is appended as a durable structured message by the
    # top-level handler in run_conversation_turn; flushing on it persists the
    # error bubble so it survives reloads and expired WS subscriptions.
    "error",
    # A mid-turn refusal-fallback model switch (Anthropic safety
    # classifiers declined; a fallback model continued -- see
    # refusal_fallback_models in chat/llm/config.py). _stream_turn appends
    # the pre-switch partial text plus the notice row before emitting
    # this, so flushing persists both in order.
    "model_fallback",
    # A routine run ended without calling routine_completed and the driver
    # (chat/routine_runs.py) is about to run a follow-up turn; the notice
    # row is appended to messages_out before this is emitted so it lands in
    # the transcript ahead of the nudge turn's messages.
    "routine_nudge",
})


FlushFn = Callable[[], Awaitable[None]]


def make_flush_callback(
    conversation_id: str,
    messages_out: list[dict],
    *,
    log_prefix: str,
) -> tuple[FlushFn, asyncio.Lock]:
    """Build the async flush closure for a single dispatch.

    Returns the closure plus its companion ``asyncio.Lock`` so callers can
    share it with related code paths if needed (e.g. a final flush in a
    ``finally`` block just calls the closure again).

    The closure slices ``messages_out[flushed_count:]``, stamps any
    missing ``timestamp`` field with ``utc_timestamp()``, calls
    ``ChatStorage.append_structured_messages`` for the conversation, and
    advances the running counter. Failures are logged via
    ``logger.exception`` with ``log_prefix`` and swallowed.

    On a successful disk write the closure also publishes a
    ``message_appended`` event for each newly-stamped (seq, message) pair
    to the persistent-WS bus and pushes them into the per-conversation
    replay ring buffer so reconnecting clients can catch up. The publishes
    are best-effort: a publish failure does not roll back the disk write.
    """
    flush_lock = asyncio.Lock()
    flushed_count = 0

    async def flush() -> None:
        nonlocal flushed_count
        async with flush_lock:
            if flushed_count >= len(messages_out):
                return
            new_msgs = messages_out[flushed_count:]
            now_ts = utc_timestamp()
            for msg in new_msgs:
                if "timestamp" not in msg:
                    msg["timestamp"] = now_ts
            try:
                appended = await ChatStorage.append_structured_messages(
                    conversation_id=conversation_id,
                    messages=new_msgs,
                )
                flushed_count += len(new_msgs)
            except Exception:
                logger.exception(
                    "%s Incremental flush failed (conversation=%s)",
                    log_prefix, conversation_id,
                )
                return

            # Fan out ``message_appended`` to the persistent-WS bus and
            # push into the per-conversation replay ring buffer. Shared
            # with the single-message path in ``ChatStorage.append_message``
            # so every appended message reaches onlooker tabs the same way.
            _publish_appended_to_bus(conversation_id, appended)

    return flush, flush_lock
