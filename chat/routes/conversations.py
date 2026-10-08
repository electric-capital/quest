"""Conversation CRUD endpoints."""

import logging
from datetime import datetime

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.conversation_access import require_owned_conversation
from chat.realtime import bus, events as realtime_events
from chat.storage import ChatStorage

from chat.routes import router

logger = logging.getLogger(__name__)


class RenameConversationRequest(BaseModel):
    custom_name: str | None = None


class UpdateModelRequest(BaseModel):
    model: str


def _publish_list_changed(user_id: int, conversation_id: str, action: str) -> None:
    """Best-effort: publish ``conversation_list_changed`` to the user's WS."""
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_conversation_list_changed(
                conversation_id=conversation_id,
                action=action,
            ),
        )
    except Exception:
        logger.debug(
            "[conversations] publish conversation_list_changed failed "
            "(user_id=%s, action=%s)", user_id, action, exc_info=True,
        )


@router.get("/search")
async def search_conversations(
    q: str = Query(..., min_length=2),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Search across all conversation messages for the authenticated user.

    Args:
        q: Search query string (minimum 2 characters).
        user: Authenticated user dictionary.

    Returns:
        Dictionary with results list, total_matches count, and query string.
    """
    user_id = user["id"]
    results = await ChatStorage.search_conversations(user_id, q)
    return results


def _parse_conversation_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a ``<last_message_at_iso>|<conversation_id>`` keyset cursor.

    The cursor is opaque to clients: it's whatever ``next_cursor`` the
    previous page response carried. 400 on anything that doesn't parse.
    """
    ts_part, sep, id_part = cursor.partition("|")
    if not sep or not id_part:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_cursor", "message": "Malformed cursor."},
        )
    try:
        ts = datetime.fromisoformat(ts_part)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_cursor", "message": "Malformed cursor."},
        )
    return ts, id_part


@router.get("/conversations")
async def list_conversations(
    include_archived: bool = Query(False),
    exclude_projects: bool = Query(False),
    include_slack: bool = Query(True),
    include_inference: bool = Query(True),
    limit: int | None = Query(None, ge=1, le=200),
    cursor: str | None = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List conversations for the authenticated user.

    With no query params this returns the user's full list (legacy
    behavior). The sidebar passes server-side filters plus ``limit`` for a
    paged load; a page response carries ``next_cursor`` (opaque keyset
    cursor) which the client echoes back via ``cursor`` to fetch the next
    page. ``has_more``/``next_cursor`` are always present so clients don't
    need shape-sniffing (null/false on unpaged or final-page responses).

    Args:
        include_archived: If true, include archived conversations.
        exclude_projects: If true, drop project-linked conversations.
        include_slack: If false, drop origin="slack" conversations.
        include_inference: If false, drop origin="inference_api" conversations.
        limit: Optional page size (1..200). Absent means no paging.
        cursor: Opaque keyset cursor from a previous response's next_cursor.
        user: Authenticated user dictionary from get_current_user_cookie_or_apikey_checked

    Returns:
        Dictionary with conversations list, has_more flag, and next_cursor.
    """
    user_id = user["id"]
    before = _parse_conversation_cursor(cursor) if cursor else None

    # Fetch one extra row past the page so has_more is exact without a
    # second COUNT query.
    fetch_limit = limit + 1 if limit is not None else None
    conversations = await ChatStorage.list_conversations(
        user_id,
        include_archived=include_archived,
        exclude_projects=exclude_projects,
        include_slack=include_slack,
        include_inference=include_inference,
        limit=fetch_limit,
        before=before,
    )

    has_more = limit is not None and len(conversations) > limit
    if has_more:
        conversations = conversations[:limit]
    next_cursor = None
    if has_more and conversations:
        last = conversations[-1]
        next_cursor = f"{last['last_message_at']}|{last['id']}"
    return {
        "conversations": conversations,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@router.post("/conversations")
async def create_conversation(user: dict = Depends(get_current_user_cookie_or_apikey_checked)):
    """Create a new conversation for authenticated user.

    Args:
        user: Authenticated user dictionary from get_current_user_cookie_or_apikey_checked

    Returns:
        Dictionary with conversation id and created_at timestamp
    """
    user_id = user["id"]
    conversation_id, created_at_str = await ChatStorage.create_conversation(user_id)

    _publish_list_changed(user_id, conversation_id, "created")

    # Return seed-friendly fields so the FE can prime its conversation
    # store and skip the GET /conversations/<id> round trip on the
    # new-chat critical path. Mirrors the shape of GET /conversations/<id>
    # for the just-created (always empty) conversation.
    return {
        "id": conversation_id,
        "created_at": created_at_str,
        "last_message_seq": 0,
        "origin": "web",
        "project_id": None,
        "model": None,
    }


@router.post("/conversations/{conversation_id}/duplicate-workspace")
async def duplicate_conversation_workspace(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a new empty conversation seeded with a copy of this conversation's workspace files.

    Args:
        conversation_id: UUID of the source conversation.
        user: Authenticated user dictionary.

    Returns:
        Same seed-friendly shape as POST /conversations for the new conversation.

    Raises:
        HTTPException: 404 if the source conversation is not found.
    """
    user_id = user["id"]

    meta = await require_owned_conversation(user_id, conversation_id)

    new_conversation_id, created_at_str = await ChatStorage.create_conversation(user_id)
    # The source's CONVERSATION workspace only, also for a project
    # conversation (the shared project files are not copied).
    await ChatStorage.copy_workspace_files(
        ChatStorage.get_conversation_workspace_root(conversation_id),
        ChatStorage.get_conversation_workspace_root(new_conversation_id),
    )

    _publish_list_changed(user_id, new_conversation_id, "created")

    return {
        "id": new_conversation_id,
        "created_at": created_at_str,
        "last_message_seq": 0,
        "origin": "web",
        "project_id": None,
        "model": None,
    }


@router.post("/conversations/{conversation_id}/compact")
async def compact_conversation_endpoint(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Compact this conversation's model-facing history (see chat/compaction.py).

    Summarizes the older portion of the history with a one-off LLM call,
    keeps the recent messages verbatim, archives the pre-compaction
    ``sdk_history.json``, and appends a ``compaction`` marker message. The
    FE offers this from the expensive-resume warning card.

    Returns:
        The compaction result dict (message counts, token estimates,
        archive filename, and the summary text).

    Raises:
        HTTPException: 404 unknown conversation; 409 when the conversation
        cannot be compacted right now (no model, pending wait handles, no
        history, nothing to compact, provider mismatch, summarization
        failure).
    """
    user_id = user["id"]

    meta = await require_owned_conversation(user_id, conversation_id)

    model = meta.get("model")
    if not model:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_model",
                "message": "This conversation has no recorded model to summarize with.",
            }
        )

    # A suspended run (action request / Slack reply pending) owns the
    # history; refuse to rewrite it underneath.
    from db import tool_wait_handle_store
    pending = await tool_wait_handle_store.list_pending_for_conversation(
        user_id, conversation_id,
    )
    if pending:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "conversation_busy",
                "message": "This conversation is waiting on a pending action and cannot be compacted right now.",
            }
        )

    from chat.compaction import CompactionError, compact_conversation
    try:
        result = await compact_conversation(user_id, conversation_id, model)
    except CompactionError as e:
        raise HTTPException(
            status_code=409,
            detail={"error": e.code, "message": str(e)},
        )
    return result


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user: dict = Depends(get_current_user_cookie_or_apikey_checked)):
    """Get a specific conversation for authenticated user.

    Args:
        conversation_id: UUID of the conversation
        user: Authenticated user dictionary from get_current_user_cookie_or_apikey_checked

    Returns:
        Conversation data dictionary

    Raises:
        HTTPException: 404 if conversation not found
    """
    user_id = user["id"]

    meta = await require_owned_conversation(user_id, conversation_id)

    conversation = ChatStorage.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "conversation_not_found",
                "message": f"Conversation {conversation_id} not found for user {user['email']}"
            }
        )

    # Merge server-side metadata into the response (model, routine_id)
    if meta.get("model"):
        conversation["model"] = meta["model"]
    if meta.get("routine_id"):
        conversation["routine_id"] = meta["routine_id"]
    if meta.get("project_id"):
        conversation["project_id"] = meta["project_id"]
    # Surface the cached high-water seq so the persistent-WS client can
    # subscribe with a usable last_seq right after hydration.
    conversation["last_message_seq"] = meta.get("last_message_seq") or 0

    # Surface the origin discriminator so the frontend can gate the composer.
    # NULL origin is equivalent to "web".
    conversation["origin"] = meta.get("origin") or "web"

    # Surface persisted per-conversation flags so the FE can render the
    # read-only "N flags enabled" composer label after the first message
    # (and across reloads). NULL flags is equivalent to an empty list.
    conversation["flags"] = meta.get("flags") or []

    # Surface the sidebar title fields so the chat header can render the
    # same title (and its rename / archive state) the sidebar row shows.
    conversation["custom_name"] = meta.get("custom_name")
    conversation["title"] = ChatStorage._resolve_list_title(conversation_id, meta)
    conversation["archived"] = bool(meta.get("archived"))

    # For Slack-driven conversations, look up and expose the channel/thread
    # pair so the web UI can show where it's being driven from.
    if meta.get("origin") == "slack":
        from chat.slack_conversation_store import list_slack_conversations_for_user
        slack_rows = await list_slack_conversations_for_user(user_id)
        for row in slack_rows:
            if row["conversation_id"] == conversation_id:
                conversation["slack_channel_id"] = row["slack_channel_id"]
                conversation["slack_thread_ts"] = row["slack_thread_ts"]
                break
        # The per-user Slack OAuth blob lives in the generic
        # user_service_credentials row written by the Slack plugin's
        # OAuth callback (attached to the user dict by db/user_store.py).
        slack_row = (user.get("service_credentials") or {}).get("slack") or {}
        slack_oauth = slack_row.get("oauth_blob") or {}
        conversation["slack_team_id"] = slack_oauth.get("default_team_id")

    # For cross-user subagent conversations, expose who launched the run
    # and its lifecycle status so the read-only view can explain itself.
    if meta.get("origin") == "user_subagent":
        from db.user_subagent_run_store import get_run_by_subagent_conversation
        from db.user_store import get_user_by_id
        run = await get_run_by_subagent_conversation(conversation_id)
        if run is not None:
            caller = await get_user_by_id(run["caller_user_id"])
            conversation["subagent_run"] = {
                "status": run["status"],
                "caller_email": (caller or {}).get("email", ""),
                "caller_name": (caller or {}).get("name", ""),
                "created_at": run.get("created_at"),
            }

    # Surface pending wait handles so the FE can lock the composer on load
    # even when the agent is suspended (action_request awaiting approval,
    # slack_reply awaiting Slack DM). Resolved on the FE by the existing
    # ``wait_handle_resolved`` global event.
    from db import tool_wait_handle_store
    pending = await tool_wait_handle_store.list_pending_for_conversation(
        user_id, conversation_id,
    )
    conversation["pending_wait_handles"] = [
        {
            "id": h["id"],
            "kind": h["kind"],
            "correlation_id": h.get("correlation_id"),
            "created_at": h.get("created_at"),
        }
        for h in pending
    ]

    # Surface the expensive-resume verdict (long-idle, long-context, costly
    # model) so the FE can block the composer with a warning card until the
    # user picks an alternative or acknowledges the cost. None means "no
    # warning"; the checker is best-effort and swallows its own failures.
    from chat.expensive_resume import check_expensive_resume
    conversation["expensive_resume"] = await check_expensive_resume(
        conversation_id, meta,
    )

    return conversation


@router.get("/conversations/{conversation_id}/tail")
async def get_conversation_tail(
    conversation_id: str,
    after_seq: int = Query(0, ge=0),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Return the messages in a conversation with ``seq > after_seq``.

    Used by the persistent-WS client to fetch the new tail after a
    ``message_appended`` event without re-downloading the entire
    conversation. Response shape:

    ``{messages: [...], last_message_seq: N}``
    """
    user_id = user["id"]

    meta = await require_owned_conversation(user_id, conversation_id)

    conversation = ChatStorage.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "conversation_not_found",
                "message": f"Conversation {conversation_id} not found",
            },
        )

    all_messages = conversation.get("messages") or []
    tail = [
        m for m in all_messages
        if isinstance(m, dict)
        and isinstance(m.get("seq"), int)
        and m["seq"] > after_seq
    ]
    return {
        "messages": tail,
        "last_message_seq": meta.get("last_message_seq") or 0,
    }


@router.get("/conversations/{conversation_id}/system-prompt")
async def get_conversation_system_prompt(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get the saved system prompt for a conversation.

    Returns the full system prompt text, or null if no messages have been sent yet.
    """
    user_id = user["id"]

    await require_owned_conversation(user_id, conversation_id)

    text = ChatStorage.get_system_prompt_text(conversation_id)
    return {"system_prompt": text}


@router.get("/conversations/{conversation_id}/loaded-skills")
async def get_conversation_loaded_skills(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Get the list of manually loaded skill IDs for a conversation.

    Returns an empty list if no skills have been loaded yet.
    """
    user_id = user["id"]

    await require_owned_conversation(user_id, conversation_id)

    skill_ids = ChatStorage.get_loaded_skill_ids(conversation_id)
    return {"skill_ids": skill_ids}


@router.put("/conversations/{conversation_id}/archive")
async def archive_conversation_endpoint(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Archive a conversation (soft-delete from sidebar)."""
    from db.conversation_store import archive_conversation
    result = await archive_conversation(user["id"], conversation_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Conversation not found"},
        )
    _publish_list_changed(user["id"], conversation_id, "archived")
    return result


@router.put("/conversations/{conversation_id}/unarchive")
async def unarchive_conversation_endpoint(
    conversation_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Unarchive a conversation (restore to sidebar)."""
    from db.conversation_store import unarchive_conversation
    result = await unarchive_conversation(user["id"], conversation_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Conversation not found"},
        )
    _publish_list_changed(user["id"], conversation_id, "unarchived")
    return result


@router.put("/conversations/{conversation_id}/rename")
async def rename_conversation_endpoint(
    conversation_id: str,
    body: RenameConversationRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Set or clear a custom name for a conversation."""
    from db.conversation_store import rename_conversation, MAX_CONVERSATION_NAME_LENGTH

    # Validate and normalize the name
    if body.custom_name is not None:
        name = body.custom_name.strip()
        if not name:
            # Treat empty/whitespace-only string as "clear custom name"
            name = None
        elif len(name) > MAX_CONVERSATION_NAME_LENGTH:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_name",
                    "message": f"Name must be at most {MAX_CONVERSATION_NAME_LENGTH} characters",
                },
            )
    else:
        name = None

    result = await rename_conversation(user["id"], conversation_id, name)
    if not result:
        raise HTTPException(
            status_code=404,
            detail={"error": "conversation_not_found", "message": "Conversation not found"},
        )
    _publish_list_changed(user["id"], conversation_id, "renamed")
    return result


@router.patch("/conversations/{conversation_id}/model")
async def update_conversation_model_endpoint(
    conversation_id: str,
    body: UpdateModelRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Update the model for a conversation.

    Sets the model on the conversation record.  This is used by the frontend
    to persist model changes when the user switches models.
    """
    from db.conversation_store import set_conversation_model

    await require_owned_conversation(user["id"], conversation_id)

    await set_conversation_model(conversation_id, body.model)
    _publish_list_changed(user["id"], conversation_id, "model_changed")
    return {"ok": True}
