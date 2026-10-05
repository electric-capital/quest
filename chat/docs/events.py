"""Realtime publishing for Quest Docs.

Two per-user globals on the persistent WebSocket, built by
chat/realtime/events.py: ``doc_list_changed`` (list views re-fetch) and
``doc_changed`` (an open viewer of that doc re-fetches). Both go to the
doc's owner; share-recipient fan-out arrives with the sharing UI.

Only chat/docs/service.py (and the doc routes' metadata paths through it)
should call these -- never tools or routes directly -- so every write path
emits exactly once. Publishing is best-effort: a failure is logged at debug
and never undoes the write.
"""

import logging
from typing import Optional

from chat.realtime import bus, events as realtime_events

logger = logging.getLogger(__name__)


def publish_doc_list_changed(user_id: int) -> None:
    """Best-effort: tell the user's tabs their doc list may have changed."""
    try:
        bus.publish_to_user(user_id, realtime_events.make_doc_list_changed())
    except Exception:
        logger.debug(
            "[docs] publish doc_list_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )


def publish_doc_changed(
    user_id: int, doc_id: str, updated_at: Optional[str],
) -> None:
    """Best-effort: tell the user's tabs a doc's body or assets changed."""
    try:
        bus.publish_to_user(
            user_id, realtime_events.make_doc_changed(doc_id, updated_at),
        )
    except Exception:
        logger.debug(
            "[docs] publish doc_changed failed (user_id=%s, doc_id=%s)",
            user_id, doc_id, exc_info=True,
        )
