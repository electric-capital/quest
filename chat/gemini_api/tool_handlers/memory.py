"""Memory handlers: memory_search, memory_list and the non-blocking
suggest_memory (chat/memory_suggestions.py).
"""

import json

from db.memory_store import list_memories, search_memories


# ---------------------------------------------------------------------------
# Memory tool handlers
# ---------------------------------------------------------------------------

async def _handle_memory_search(user_id: int, query: str) -> str:
    """Search user's memories using FTS5 full-text search.

    Args:
        user_id: User's integer ID.
        query: FTS5 search query string. Supports simple words,
            phrases ("exact phrase"), prefix (meet*), and boolean
            (AND, OR, NOT).

    Returns:
        JSON string with matching memories.
    """
    try:
        results = await search_memories(user_id, query)
        return json.dumps({
            "match_count": len(results),
            "memories": [
                {
                    "id": m["id"],
                    "content": m["content"],
                    "created_at": m["created_at"],
                    "updated_at": m.get("updated_at"),
                }
                for m in results
            ],
        })
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}"})


async def _handle_memory_list(user_id: int) -> str:
    """List all active (non-archived) memories for a user.

    Returns memories ordered by creation time (newest first).

    Args:
        user_id: User's integer ID.

    Returns:
        JSON string with all memories.
    """
    results = await list_memories(user_id, include_archived=False)
    return json.dumps({
        "memory_count": len(results),
        "memories": [
            {
                "id": m["id"],
                "content": m["content"],
                "created_at": m["created_at"],
                "updated_at": m.get("updated_at"),
            }
            for m in results
        ],
    })


async def _handle_suggest_memory(
    user_id: int,
    conversation_id: str | None,
    content: object,
    reasoning: object = None,
    replaces_memory_id: object = None,
) -> str:
    """Register a memory suggestion for the user's review and return at once.

    Args:
        user_id: User's integer ID.
        conversation_id: The conversation the suggestion belongs to; None
            (no conversation context) is refused.
        content: Proposed memory text.
        reasoning: Optional one-line justification shown to the user.
        replaces_memory_id: Optional active memory the suggestion supersedes
            (archived when the user accepts).

    Returns:
        JSON string: ``status`` suggested / already_saved /
        already_suggested with ids and the pending count, or ``error``.
    """
    if not conversation_id:
        return json.dumps({
            "error": "suggest_memory needs a conversation context.",
        })
    from chat.memory_suggestions import register_suggestion
    try:
        result = await register_suggestion(
            user_id, conversation_id, content,
            reasoning=reasoning, replaces_memory_id=replaces_memory_id,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to register suggestion: {e}"})
    return json.dumps(result)

