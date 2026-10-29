"""Client singleton and session management for the LLM integration.

Manages the in-memory chat session store. Suspended tools are tracked
via ``tool_wait_handles`` rows in the DB; the resume path in
``chat.wait_handles.resume`` re-queries the DB on the next run so the
conversation continues across a server restart.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory chat session store
# ---------------------------------------------------------------------------

_active_chats: dict[tuple[int, str], tuple[str, Any]] = {}


def get_or_create_chat(
    provider, user_id, conversation_id, model, system_prompt,
    history=None, disk_history=None, disk_provider=None,
    tools=None, thinking_effort=None,
):
    """Return an existing chat session or create a new one.

    Sessions are keyed by (user_id, conversation_id) and held in memory.
    On server restart, the SDK session history is loaded from the saved
    sdk_history.json file and passed to provider.create_session() so the
    model retains full context from prior turns.

    Whenever the new session starts with no history, the doc notes seen
    record (``doc_notes_seen.json``) is deleted: a session without history
    holds no notes block, so any record could only vouch for blocks the
    model no longer has. That is the cross-provider switch with no
    compatible disk copy (also after a restart, when the caller discarded
    another provider's disk history), a failed ``save_history`` with no
    compatible disk copy, and a first session, where it is a no-op. A
    thinking-effort rebuild, a same-provider switch and a session seeded
    from compatible disk history carry the history over and keep it.

    Args:
        provider: LLMProvider instance.
        user_id: User's integer ID.
        conversation_id: Conversation UUID.
        model: Model name string.
        thinking_effort: The conversation's thinking level (validated by
            the caller; None = the model's default). A cached session on
            the same model is switched in place when the provider allows
            it, otherwise rebuilt from its history like a same-provider
            model switch.
        system_prompt: System instruction text.
        history: Optional list of history dicts to initialize the session
            with (loaded from sdk_history.json on restart).
        disk_history: Optional list of history dicts loaded from disk,
            used as a fallback when the in-memory session is discarded
            due to a model change.
        disk_provider: Provider name ('gemini' or 'anthropic') of the
            disk_history data, so we can check format compatibility.
        tools: Optional list of ToolSpec dicts to expose to the session.
            Defaults to TOP_LEVEL_TOOLS. Slack-driven conversations pass
            SLACK_TOP_LEVEL_TOOLS so the model can call
            send_slack_reply_and_get_response.

    Returns:
        Session object (type varies by provider).
    """
    from chat.llm.config import get_provider_for_model

    key = (user_id, conversation_id)
    existing = _active_chats.get(key)
    if existing is not None:
        stored_model, session = existing
        if stored_model == model:
            if _session_thinking_effort(session) == thinking_effort:
                return session
            if provider.set_thinking_effort(session, thinking_effort):
                return session
            logger.info(
                "Thinking effort changed to %r for conversation %s; rebuilding session.",
                thinking_effort, conversation_id,
            )
        else:
            # Model changed — determine if old and new models use the same provider.
            logger.info(
                "Model changed from %r to %r for conversation %s; replacing session.",
                stored_model,
                model,
                conversation_id,
            )
        try:
            old_provider_name = get_provider_for_model(stored_model)
        except ValueError:
            old_provider_name = None
        try:
            new_provider_name = get_provider_for_model(model)
        except ValueError:
            new_provider_name = None

        if old_provider_name and old_provider_name == new_provider_name:
            # Same provider (e.g., Haiku -> Sonnet): history format is compatible.
            # Extract the in-memory session's history for the new session.
            try:
                history = provider.save_history(session)
                logger.info(
                    "Same-provider switch (%s); preserving %d history entries "
                    "for conversation %s",
                    old_provider_name, len(history), conversation_id,
                )
            except Exception:
                logger.warning(
                    "Failed to extract history from old session for conversation %s; "
                    "falling back to disk history",
                    conversation_id,
                    exc_info=True,
                )
                # Fall back to disk history if compatible
                if disk_history and disk_provider == new_provider_name:
                    history = disk_history
                else:
                    history = None
        else:
            # Cross-provider switch (e.g., Gemini -> Claude): formats are incompatible.
            # Try disk history as a fallback if the provider matches.
            if disk_history and disk_provider == new_provider_name:
                history = disk_history
                logger.info(
                    "Cross-provider switch; using compatible disk history "
                    "(%d entries, provider=%s) for conversation %s",
                    len(disk_history), disk_provider, conversation_id,
                )
            else:
                history = None
                logger.info(
                    "Cross-provider switch with no compatible disk history "
                    "for conversation %s; starting fresh.",
                    conversation_id,
                )
        del _active_chats[key]
    from chat.llm.tool_schemas import TOP_LEVEL_TOOLS
    session_tools = tools if tools is not None else TOP_LEVEL_TOOLS
    loaded_history = None
    if history:
        loaded_history = provider.load_history(history)
        logger.info(
            "Creating session for conversation %s (model=%s) with %d "
            "history entries.",
            conversation_id, model, len(history),
        )
    else:
        logger.info(
            "Creating session for conversation %s (model=%s) with no "
            "history.",
            conversation_id, model,
        )
    if not history:
        _clear_doc_notes_seen(conversation_id)
    new_session = provider.create_session(
        model=model,
        system_prompt=system_prompt,
        tools=session_tools,
        history=loaded_history,
        thinking_effort=thinking_effort,
    )
    _active_chats[key] = (model, new_session)
    return new_session


def _clear_doc_notes_seen(conversation_id: str) -> None:
    """Delete the doc notes seen record before a session that starts with
    no history; never raises."""
    try:
        from chat.storage import ChatStorage

        if ChatStorage.clear_doc_notes_seen(conversation_id):
            logger.info(
                "Cleared doc notes seen record for conversation %s: the new "
                "session starts with no history.",
                conversation_id,
            )
    except Exception:
        logger.warning(
            "Failed to clear doc notes seen record for conversation %s",
            conversation_id,
            exc_info=True,
        )


def _session_thinking_effort(session) -> str | None:
    """The level a cached session was created with / switched to: the
    dataclass field of the stateless providers' sessions, or the attribute
    the Gemini provider stamps on its AsyncChat."""
    for attr in ("thinking_effort", "_llm_thinking_effort"):
        if hasattr(session, attr):
            return getattr(session, attr)
    return None


def remove_chat_session(user_id: int, conversation_id: str) -> bool:
    """Remove an in-memory chat session.

    Useful when a conversation is interrupted or corrupted and should
    start fresh on the next message. Pending wait handles for this
    conversation are cancelled in the DB by the caller (see
    ``cancel_pending_wait_handles_for_conversation``); we keep this
    function synchronous.

    Args:
        user_id: User's integer ID.
        conversation_id: Conversation UUID.

    Returns:
        True if a session was removed, False if none existed.
    """
    key = (user_id, conversation_id)
    return _active_chats.pop(key, None) is not None


async def cancel_pending_wait_handles_for_conversation(
    user_id: int, conversation_id: str,
) -> None:
    """Cancel all pending wait handles for a conversation.

    Called when a session is removed (e.g. cancellation, stop) so any
    other run that later picks up a dangling ``wait_for_handles`` doesn't
    hang. The DB write is the entire mechanism: there is no in-process
    awaiter to notify, and the dangling ``wait_for_handles`` ``tool_use``
    will be closed by the resume bucket on the next run with the
    ``cancelled`` status visible.

    Publishes a ``wait_handle_resolved`` event per cancelled row so any
    FE tabs viewing the conversation can unlock their composer.
    """
    from db import tool_wait_handle_store
    from chat.realtime import bus, events as realtime_events

    cancelled = await tool_wait_handle_store.cancel_pending_for_conversation(
        user_id, conversation_id,
    )
    for row in cancelled:
        try:
            request_id_str = (
                row.get("correlation_id")
                if row.get("correlation_kind") == "action_request"
                else None
            )
            request_id_int = int(request_id_str) if request_id_str else None
            bus.publish_to_user(
                user_id,
                realtime_events.make_wait_handle_resolved(
                    conversation_id=row["conversation_id"],
                    handle_id=row["id"],
                    kind=row.get("kind") or "",
                    status=row["status"],
                    request_id=request_id_int,
                    response=row.get("response"),
                ),
            )
        except Exception:
            logger.debug(
                "[session] publish wait_handle_resolved (cancelled) failed",
                exc_info=True,
            )


def invalidate_user_sessions(user_id: int) -> int:
    """Remove all cached chat sessions for a user.

    Called when user settings change (e.g., custom system prompt)
    so that the next message creates a fresh session with updated config.

    Args:
        user_id: User's integer ID.

    Returns:
        Number of sessions removed.
    """
    keys_to_remove = [k for k in _active_chats if k[0] == user_id]
    for key in keys_to_remove:
        del _active_chats[key]
    return len(keys_to_remove)
