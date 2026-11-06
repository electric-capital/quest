"""Unread state of conversations: the run-end hook and the realtime event.

A conversation is unread while ``conversations.unread_seq > read_seq``
(``db/conversation_store.py``). The run paths call
:func:`mark_unread_after_run` when a run ends; a focused client showing the
conversation acknowledges through ``POST /conversations/{id}/read``. Both
publish ``conversation_unread_changed`` to the owner's sockets. See
docs/architecture/conversation-unread.md.
"""

from __future__ import annotations

import logging

from chat.realtime import events as realtime_events
from chat.realtime.bus import bus
from db import conversation_store

logger = logging.getLogger(__name__)


def publish_changed(meta: dict) -> None:
    """Best-effort ``conversation_unread_changed`` to the conversation's
    owner, from a ``conversation_store`` row dict."""
    try:
        bus.publish_to_user(
            meta["user_id"],
            realtime_events.make_conversation_unread_changed(
                conversation_id=meta["id"],
                unread_seq=meta["unread_seq"],
                read_seq=meta["read_seq"],
            ),
        )
    except Exception:
        logger.debug(
            "[unread] publish conversation_unread_changed failed "
            "(conversation=%s)", meta.get("id"), exc_info=True,
        )


async def mark_unread_after_run(conversation_id: str) -> None:
    """Mark a conversation unread because a run on it just ended: the model
    finished its output, failed, or suspended waiting for the user (an
    action request or another wait handle).

    Call it after the run's final flush, so ``last_message_seq`` covers
    everything the run appended, and not for a run the user stopped (they
    were looking at it). Never raises: unread state must not break a run's
    teardown.
    """
    try:
        meta = await conversation_store.mark_conversation_unread(conversation_id)
    except Exception:
        logger.warning(
            "[unread] marking conversation %s unread failed",
            conversation_id, exc_info=True,
        )
        return
    if meta is not None:
        publish_changed(meta)
