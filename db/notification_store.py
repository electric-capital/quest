"""User notification data access: the rows behind the inbox's Other tab.

A notification is a one-way system message for one user (see
chat/notifications.py): created open, dismissed once by the user, never
edited. Rows cascade with their user.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from db.engine import AsyncSessionLocal
from db.models import UserNotification

MAX_NOTIFICATION_TITLE_LENGTH = 255


async def create_notification(
    user_id: int,
    kind: str,
    title: str,
    body: str = "",
    data: Optional[dict] = None,
) -> dict:
    """Create an open notification for ``user_id``."""
    if not title or not title.strip():
        raise ValueError("Notification title cannot be empty.")
    if len(title) > MAX_NOTIFICATION_TITLE_LENGTH:
        raise ValueError(
            f"Notification title exceeds {MAX_NOTIFICATION_TITLE_LENGTH} characters."
        )
    async with AsyncSessionLocal() as db:
        row = UserNotification(
            user_id=user_id, kind=kind, title=title.strip(), body=body, data=data,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_dict(row)


async def list_for_user(user_id: int, limit: int = 100) -> list[dict]:
    """The user's newest ``limit`` notifications, open and dismissed alike,
    newest first."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(UserNotification)
            .where(UserNotification.user_id == user_id)
            .order_by(UserNotification.created_at.desc(), UserNotification.id.desc())
            .limit(limit)
        )
        return [_to_dict(row) for row in result.scalars().all()]


async def count_open(user_id: int) -> int:
    """How many of the user's notifications are not dismissed yet."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(func.count(UserNotification.id)).where(
                UserNotification.user_id == user_id,
                UserNotification.dismissed_at.is_(None),
            )
        )
        return int(result.scalar_one())


async def dismiss(user_id: int, notification_id: str) -> Optional[dict]:
    """Mark the user's notification dismissed and return it; None when it
    is not theirs or does not exist. Dismissing twice keeps the first time."""
    async with AsyncSessionLocal() as db:
        row = await db.get(UserNotification, notification_id)
        if row is None or row.user_id != user_id:
            return None
        if row.dismissed_at is None:
            row.dismissed_at = datetime.now(timezone.utc)
            await db.commit()
            await db.refresh(row)
        return _to_dict(row)


def _to_dict(row: UserNotification) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "data": row.data,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "dismissed_at": row.dismissed_at.isoformat() if row.dismissed_at else None,
    }
