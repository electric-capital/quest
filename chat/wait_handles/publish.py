"""Publish ``wait_handle_resolved`` for a wait-handle row that left pending.

Every path that resolves a handle (the REST routes, the Slack reply flush,
the timers, a Stop, an AutoProject answer, a cross-user subagent return)
tells the owner's open clients with the same event, built from the updated
``tool_wait_handles`` row. The publish is best effort: the row is already
the source of truth, so a failure is logged at debug level and never
reaches the caller.
"""

import logging
from typing import Optional

from chat.realtime import bus, events as realtime_events

logger = logging.getLogger(__name__)


def publish_wait_handle_resolved(
    user_id: int,
    row: dict,
    *,
    response: Optional[dict] = None,
    default_kind: str = "",
    request_id: Optional[int] = None,
    conversation_id: Optional[str] = None,
) -> None:
    """Publish ``wait_handle_resolved`` for ``row`` on ``user_id``'s channel.

    ``row`` is the handle row as the store returned it after the status
    change. ``default_kind`` is used when the row carries no kind.
    ``response`` and ``request_id`` are included in the event when given
    (see :func:`chat.realtime.events.make_wait_handle_resolved`).
    ``conversation_id`` overrides the row's own, for a caller that already
    holds the conversation the event belongs to.
    """
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_wait_handle_resolved(
                conversation_id=conversation_id or row["conversation_id"],
                handle_id=row["id"],
                kind=row.get("kind") or default_kind,
                status=row["status"],
                request_id=request_id,
                response=response,
            ),
        )
    except Exception:
        logger.debug(
            "publish wait_handle_resolved failed (handle=%s, kind=%s)",
            row.get("id"), row.get("kind") or default_kind,
            exc_info=True,
        )
