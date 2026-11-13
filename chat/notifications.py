"""System notifications: the inbox's Other tab.

A notification tells one user about something the system did on their
behalf that needs no decision from them, unlike an action request or a
suggestion: a title, a plain-text body and, per kind, structured ``data``
the UI may link from. It stays open until the user dismisses it; open
notifications count toward the sidebar's inbox badge. Rows live in
``user_notifications`` (db/notification_store.py).

Every change is pushed to the user's tabs as a ``notifications_changed``
per-user global carrying the new open count. A row written outside the
server process (an Alembic data migration) publishes nothing; the tabs
read the count when they load.

Kinds:

- ``guides_converted``: written once per user by Alembic migration
  ``c5a9e3f7d2b1``, which turned the user's guides into private skills.
  ``data`` is ``{"skills": [{"skill_id", "skill_name", "guide_name",
  "was_default"}], "routines": [{"routine_id", "routine_name",
  "project_id", "project_name", "skill_id", "skill_name"}]}``, the
  routines being those that had one of the guides attached.
"""

from __future__ import annotations

import logging
from typing import Optional

from db import notification_store as store

logger = logging.getLogger(__name__)

NOTIFICATION_GUIDES_CONVERTED = "guides_converted"

# Every kind a notification row may carry.
NOTIFICATION_KINDS = frozenset({NOTIFICATION_GUIDES_CONVERTED})


async def publish_changed(user_id: int) -> int:
    """Publish ``notifications_changed`` with the user's fresh open count.

    Returns the count; a bus failure is logged and never fails the write.
    """
    open_count = await store.count_open(user_id)
    try:
        from chat.realtime.bus import bus
        from chat.realtime.events import make_notifications_changed

        bus.publish_to_user(user_id, make_notifications_changed(open_count))
    except Exception:  # pragma: no cover - defensive
        logger.exception("[notifications] publish failed (user_id=%s)", user_id)
    return open_count


async def notify(
    user_id: int,
    kind: str,
    title: str,
    body: str = "",
    data: Optional[dict] = None,
) -> dict:
    """Create an open notification for the user and tell their tabs."""
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"Unknown notification kind: {kind!r}")
    notification = await store.create_notification(user_id, kind, title, body, data)
    await publish_changed(user_id)
    return notification


async def dismiss(user_id: int, notification_id: str) -> Optional[tuple[dict, int]]:
    """Dismiss the user's notification: ``(notification, open_count)``, or
    None when it is not theirs. Already dismissed is not an error."""
    notification = await store.dismiss(user_id, notification_id)
    if notification is None:
        return None
    return notification, await publish_changed(user_id)
