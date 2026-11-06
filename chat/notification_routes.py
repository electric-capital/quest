"""REST endpoints for system notifications (the inbox's Other tab).

See chat/notifications.py for what a notification is.
"""

from fastapi import APIRouter, Depends, HTTPException

from chat import notifications
from chat.auth import get_current_user_cookie_or_apikey_checked
from db import notification_store

router = APIRouter(
    prefix="/app/api",
    tags=["notifications"],
)


def _public(notification: dict) -> dict:
    """A notification as the API sends it (the owner is the caller)."""
    return {key: value for key, value in notification.items() if key != "user_id"}


@router.get("/notifications")
async def list_notifications(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The user's notifications, newest first (open and dismissed, capped
    at the newest 100), with the number still open."""
    rows = await notification_store.list_for_user(user["id"])
    return {
        "notifications": [_public(row) for row in rows],
        "open_count": await notification_store.count_open(user["id"]),
    }


@router.get("/notifications/count")
async def count_notifications(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """How many of the user's notifications are open (the sidebar inbox
    badge adds it to the open action requests and pending suggestions)."""
    return {"open_count": await notification_store.count_open(user["id"])}


@router.post("/notifications/{notification_id}/dismiss")
async def dismiss_notification(
    notification_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Dismiss one of the user's notifications. Dismissing an already
    dismissed one succeeds and changes nothing."""
    result = await notifications.dismiss(user["id"], notification_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Notification not found"},
        )
    notification, open_count = result
    return {"notification": _public(notification), "open_count": open_count}
