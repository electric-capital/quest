"""Memory handlers: memory_search, memory_list, memory_get and the
non-blocking suggest_memory (chat/memory_suggestions.py), plus their
project-scoped twins project_memory_search, project_memory_list,
project_memory_get and suggest_project_memory (chat/project_memories.py).
"""

import json

from db.memory_store import get_memory, list_memories, search_memories

# Most ids one memory_get / project_memory_get call fetches: the prompt's
# memory index (chat/memory_prompt.py) is what the ids come from, and a
# call for more than this is a disguised list.
MAX_MEMORY_GET_IDS = 20


def _normalize_ids(ids: object) -> list[str] | str:
    """The ``ids`` argument as a de-duplicated list of non-empty strings,
    or an error message. A single string is accepted as a one-item list."""
    if isinstance(ids, str):
        ids = [ids]
    if not isinstance(ids, list) or not ids:
        return "ids must be a non-empty list of memory ids."
    seen: list[str] = []
    for item in ids:
        if not isinstance(item, str) or not item.strip():
            return "ids must contain memory id strings only."
        if item.strip() not in seen:
            seen.append(item.strip())
    if len(seen) > MAX_MEMORY_GET_IDS:
        return f"At most {MAX_MEMORY_GET_IDS} ids per call."
    return seen


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


async def _handle_memory_get(user_id: int, ids: object) -> str:
    """Fetch memories by id (the ids the prompt's memory index shows).

    Args:
        user_id: User's integer ID.
        ids: List of memory UUID strings (a single string is accepted).

    Returns:
        JSON string with the memories found, in the order asked, and the
        ids that matched nothing of this user's; or ``error``.
    """
    normalized = _normalize_ids(ids)
    if isinstance(normalized, str):
        return json.dumps({"error": normalized})
    found: list[dict] = []
    missing: list[str] = []
    for memory_id in normalized:
        m = await get_memory(user_id, memory_id)
        if m is None:
            missing.append(memory_id)
            continue
        found.append({
            "id": m["id"],
            "content": m["content"],
            "created_at": m["created_at"],
            "updated_at": m.get("updated_at"),
            "archived": bool(m.get("archived")),
        })
    return json.dumps({"memories": found, "missing": missing})


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



# ---------------------------------------------------------------------------
# Project memory tool handlers (chat/project_memories.py)
# ---------------------------------------------------------------------------

_NO_PROJECT_ERROR = (
    "Project memories exist only in project conversations; this "
    "conversation does not belong to a project."
)


def _project_memory_view(m: dict) -> dict:
    return {
        "id": m["id"],
        "content": m["content"],
        "created_at": m["created_at"],
        "updated_at": m.get("updated_at"),
    }


async def _handle_project_memory_search(project_id: str | None, query: str) -> str:
    """FTS5 search over the conversation's project memories.

    Args:
        project_id: The conversation's project; None is refused.
        query: FTS5 query, same syntax as memory_search.

    Returns:
        JSON string with the matching project memories.
    """
    if not project_id:
        return json.dumps({"error": _NO_PROJECT_ERROR})
    from db.project_memory_store import search_project_memories
    try:
        results = await search_project_memories(project_id, query)
    except Exception as e:
        return json.dumps({"error": f"Search failed: {e}"})
    return json.dumps({
        "match_count": len(results),
        "project_memories": [_project_memory_view(m) for m in results],
    })


async def _handle_project_memory_list(project_id: str | None) -> str:
    """All active memories of the conversation's project, newest first."""
    if not project_id:
        return json.dumps({"error": _NO_PROJECT_ERROR})
    from db.project_memory_store import list_project_memories
    results = await list_project_memories(project_id)
    return json.dumps({
        "memory_count": len(results),
        "project_memories": [_project_memory_view(m) for m in results],
    })


async def _handle_project_memory_get(project_id: str | None, ids: object) -> str:
    """Fetch the conversation's project memories by id (the ids the
    prompt's project memory index shows); same shape as memory_get."""
    if not project_id:
        return json.dumps({"error": _NO_PROJECT_ERROR})
    normalized = _normalize_ids(ids)
    if isinstance(normalized, str):
        return json.dumps({"error": normalized})
    from db.project_memory_store import get_project_memory
    found: list[dict] = []
    missing: list[str] = []
    for memory_id in normalized:
        m = await get_project_memory(project_id, memory_id)
        if m is None:
            missing.append(memory_id)
            continue
        found.append({**_project_memory_view(m), "archived": bool(m.get("archived"))})
    return json.dumps({"project_memories": found, "missing": missing})


async def _handle_suggest_project_memory(
    user_id: int,
    project_id: str | None,
    conversation_id: str | None,
    content: object,
    reasoning: object = None,
    replaces_memory_id: object = None,
) -> str:
    """Register a project memory suggestion for the user's review.

    Returns:
        JSON string: ``status`` suggested / already_saved /
        already_suggested with ids and the pending count, or ``error``.
    """
    if not project_id:
        return json.dumps({"error": _NO_PROJECT_ERROR})
    if not conversation_id:
        return json.dumps({
            "error": "suggest_project_memory needs a conversation context.",
        })
    from chat.project_memories import register_suggestion
    try:
        result = await register_suggestion(
            user_id, project_id, conversation_id, content,
            reasoning=reasoning, replaces_memory_id=replaces_memory_id,
        )
    except Exception as e:
        return json.dumps({"error": f"Failed to register suggestion: {e}"})
    return json.dumps(result)
