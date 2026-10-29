"""Model-proposed memories reviewed on the side of a conversation.

The ``suggest_memory`` dynamic tool registers a pending row in
``memory_suggestions`` and returns at once -- the turn keeps going, unlike
the blocking ``create_memory`` action request. The web UI shows the
conversation's pending count in a bubble above the composer and lets the
user accept (creating the memory) or dismiss each suggestion from a panel;
every change is pushed to the user's tabs as a ``memory_suggestions_changed``
per-user global carrying the new pending count.

This module is the one place that both writes the rows (via
db/memory_suggestion_store.py) and publishes the event, used by the tool
handler and the REST routes alike.
"""

from __future__ import annotations

import logging
from typing import Optional

from db import memory_suggestion_store as store
from db.memory_suggestion_store import (
    MAX_PENDING_PER_CONVERSATION,
    SuggestionAlreadyResolvedError,
)

logger = logging.getLogger(__name__)


async def publish_changed(user_id: int, conversation_id: Optional[str]) -> int:
    """Publish ``memory_suggestions_changed`` with the fresh pending counts
    (the conversation's, and the user's total for the inbox badge).

    ``conversation_id`` None is a dream's suggestion (chat/dreaming.py): no
    composer bubble moves, so the conversation count is 0 and only
    ``total_pending`` matters. Returns the conversation's count. Publishing
    is best-effort: a bus failure is logged and never fails the write that
    triggered it.
    """
    pending_count = (
        await store.count_pending(user_id, conversation_id) if conversation_id else 0
    )
    total_pending = await store.count_pending_for_user(user_id)
    try:
        from chat.realtime.bus import bus
        from chat.realtime.events import make_memory_suggestions_changed

        bus.publish_to_user(
            user_id,
            make_memory_suggestions_changed(conversation_id, pending_count, total_pending),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "[memory-suggestions] publish failed (conversation=%s)", conversation_id,
        )
    return pending_count


async def register_suggestion(
    user_id: int,
    conversation_id: str,
    content: object,
    reasoning: object = None,
    replaces_memory_id: object = None,
) -> dict:
    """The ``suggest_memory`` tool body; returns the tool's JSON-able result.

    Cheap duplicate guards keep the panel clean without a model round
    trip: text already saved as an active memory answers
    ``already_saved``, text already pending in this conversation answers
    ``already_suggested``, and a conversation at
    ``MAX_PENDING_PER_CONVERSATION`` pending rows gets a structured error.
    """
    try:
        text = store.normalize_content(content)
    except ValueError as exc:
        return {"error": str(exc)}
    if replaces_memory_id is not None and not isinstance(replaces_memory_id, str):
        return {"error": "replaces_memory_id must be a memory id string."}

    existing_memory_id = await store.find_active_memory_with_content(user_id, text)
    if existing_memory_id is not None:
        return {
            "status": "already_saved",
            "memory_id": existing_memory_id,
            "message": "An active memory with exactly this text already exists.",
        }

    duplicate = await store.find_pending_with_content(user_id, conversation_id, text)
    if duplicate is not None:
        return {
            "status": "already_suggested",
            "suggestion_id": duplicate["id"],
            "pending_count": await store.count_pending(user_id, conversation_id),
        }

    pending_count = await store.count_pending(user_id, conversation_id)
    if pending_count >= MAX_PENDING_PER_CONVERSATION:
        return {
            "error": (
                f"This conversation already has {pending_count} pending memory "
                "suggestions awaiting the user's review. Do not suggest more "
                "until they have reviewed them."
            ),
        }

    try:
        suggestion = await store.create_suggestion(
            user_id, conversation_id, text,
            reasoning=reasoning if isinstance(reasoning, str) else None,
            replaces_memory_id=replaces_memory_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    pending_count = await publish_changed(user_id, conversation_id)
    logger.info(
        "[memory-suggestions] suggested %s in conversation %s (user_id=%s, pending=%d)",
        suggestion["id"], conversation_id, user_id, pending_count,
    )
    return {
        "status": "suggested",
        "suggestion_id": suggestion["id"],
        "pending_count": pending_count,
        "message": (
            "Registered for the user's review in the suggestions panel; "
            "nothing is saved until they accept it. Keep going."
        ),
    }


async def accept_suggestion(
    user_id: int, suggestion_id: str, content: Optional[str] = None,
) -> Optional[tuple[dict, dict]]:
    """Accept + publish. Same contract as the store function."""
    result = await store.accept_suggestion(user_id, suggestion_id, content)
    if result is not None:
        await publish_changed(user_id, result[0]["conversation_id"])
    return result


async def dismiss_suggestion(user_id: int, suggestion_id: str) -> Optional[dict]:
    """Dismiss + publish. Same contract as the store function."""
    suggestion = await store.dismiss_suggestion(user_id, suggestion_id)
    if suggestion is not None:
        await publish_changed(user_id, suggestion["conversation_id"])
    return suggestion


__all__ = [
    "MAX_PENDING_PER_CONVERSATION",
    "SuggestionAlreadyResolvedError",
    "accept_suggestion",
    "dismiss_suggestion",
    "publish_changed",
    "register_suggestion",
]
