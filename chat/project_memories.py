"""Project memories: facts attached to a project, and the model's
non-blocking ``suggest_project_memory`` proposals for them.

The project-scoped twin of chat/memory_suggestions.py. A project memory
records something about the project itself -- its conventions, decisions,
stakeholders, recurring pitfalls -- rather than about the user, and is read
by the model only in that project's (private) conversations through
``project_memory_search`` / ``project_memory_list``. The suggest tool
registers a pending row in ``project_memory_suggestions`` and returns at
once; the web UI shows a separate "suggested project memories" bubble above
the composer, and every change is pushed to the user's tabs as a
``project_memory_suggestions_changed`` per-user global.

This module both writes the suggestion rows (via
db/project_memory_suggestion_store.py) and publishes the event, used by the
tool handler and the REST routes alike.
"""

from __future__ import annotations

import logging
from typing import Optional

from db import project_memory_suggestion_store as store
from db.project_memory_suggestion_store import (
    MAX_PENDING_PER_CONVERSATION,
    SuggestionAlreadyResolvedError,
)

logger = logging.getLogger(__name__)


async def publish_changed(user_id: int, conversation_id: str) -> int:
    """Publish ``project_memory_suggestions_changed`` with the fresh count.

    Returns the count; a bus failure is logged and never fails the write.
    """
    pending_count = await store.count_pending(user_id, conversation_id)
    total_pending = await store.count_pending_for_user(user_id)
    try:
        from chat.realtime.bus import bus
        from chat.realtime.events import make_project_memory_suggestions_changed

        bus.publish_to_user(
            user_id,
            make_project_memory_suggestions_changed(
                conversation_id, pending_count, total_pending,
            ),
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "[project-memories] publish failed (conversation=%s)", conversation_id,
        )
    return pending_count


async def register_suggestion(
    user_id: int,
    project_id: str,
    conversation_id: str,
    content: object,
    reasoning: object = None,
    replaces_memory_id: object = None,
) -> dict:
    """The ``suggest_project_memory`` tool body; returns the JSON-able result.

    Same duplicate guards as ``suggest_memory``: text already saved as an
    active memory of the project answers ``already_saved``, text already
    pending in this conversation ``already_suggested``, and a conversation
    at ``MAX_PENDING_PER_CONVERSATION`` pending rows gets an error.
    """
    try:
        text = store.normalize_content(content)
    except ValueError as exc:
        return {"error": str(exc)}
    if replaces_memory_id is not None and not isinstance(replaces_memory_id, str):
        return {"error": "replaces_memory_id must be a project memory id string."}

    existing_memory_id = await store.find_active_memory_with_content(project_id, text)
    if existing_memory_id is not None:
        return {
            "status": "already_saved",
            "memory_id": existing_memory_id,
            "message": "An active project memory with exactly this text already exists.",
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
                f"This conversation already has {pending_count} pending project "
                "memory suggestions awaiting the user's review. Do not suggest "
                "more until they have reviewed them."
            ),
        }

    try:
        suggestion = await store.create_suggestion(
            user_id, project_id, conversation_id, text,
            reasoning=reasoning if isinstance(reasoning, str) else None,
            replaces_memory_id=replaces_memory_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    pending_count = await publish_changed(user_id, conversation_id)
    logger.info(
        "[project-memories] suggested %s in conversation %s (project=%s, pending=%d)",
        suggestion["id"], conversation_id, project_id, pending_count,
    )
    return {
        "status": "suggested",
        "suggestion_id": suggestion["id"],
        "pending_count": pending_count,
        "message": (
            "Registered for the user's review in the project memory "
            "suggestions panel; nothing is saved until they accept it. "
            "Keep going."
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
