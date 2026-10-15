"""Internal helpers and constants for chat routes.

Not routes themselves -- imported by the route submodules that need them.
"""

import logging

from chat.gemini_api.history import _load_sdk_history, _write_sdk_history_file
# Re-exported for backward compatibility; the canonical capture lives in
# config/version.py alongside the release-tag resolution.
from config.version import GIT_COMMIT_HASH  # noqa: F401

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Interrupted SDK history helper
# ---------------------------------------------------------------------------

def _save_interrupted_sdk_history(
    user_id: int,
    conversation_id: str,
    shared_messages: list[dict],
) -> None:
    """Append an interruption marker to the on-disk SDK history if safe.

    ``run_conversation_turn`` writes ``sdk_history.json`` after every
    model response that requested tools (before dispatching them) and at
    turn end, never on cancel, so the on-disk file holds the state as of
    the last such save. The only state this helper still needs to
    capture is partial assistant text that was streamed AFTER that save
    but BEFORE the user cancelled (e.g. mid-stream cancel during the
    first text block of a turn, before any tool_use fires).

    Behaviour:

    1. If ``sdk_history.json`` does not exist on disk, do nothing -- the
       turn was cancelled before anything was persisted and the
       in-memory session is being discarded anyway.
    2. If the on-disk history ends on a dangling tool_use (no matching
       tool_result), do nothing -- writing text after a tool_use would
       break Anthropic's tool_use/tool_result alternation, and the
       resume path will close the tool_use with the user's next reply.
    3. If there is no partial assistant text in ``shared_messages``,
       do nothing -- the on-disk save is the latest known-good state.
    4. Otherwise, append the partial text plus an interruption marker
       to the last assistant entry's last text block (or as a fresh
       text block if none exists), and write the envelope back.

    Provider format (Gemini vs Anthropic) is detected from the saved
    envelope's ``provider`` field, not from any in-memory session
    object -- this helper no longer touches the live session.
    """
    loaded = _load_sdk_history(conversation_id)
    if loaded is None:
        return
    serialized, provider_name = loaded

    partial_text_parts = [
        m["content"]
        for m in shared_messages
        if m.get("type") == "text" and m.get("role") == "assistant"
    ]
    if not partial_text_parts:
        return

    if not serialized:
        return
    last_entry = serialized[-1]
    if not isinstance(last_entry, dict):
        return

    # Detect a dangling tool_use on the on-disk last entry. If the last
    # assistant turn ends with a tool_use whose tool_result has not yet
    # been appended (the resume bucket will fill it in), DO NOT append
    # interruption text -- that would leave Anthropic with a tool_use
    # not immediately followed by a tool_result and the next API call
    # would be rejected.
    if _last_entry_has_dangling_tool_use(last_entry, provider_name):
        return

    interruption_marker = "\n\n[Response interrupted by user before completion]"
    combined_text = "\n".join(partial_text_parts) + interruption_marker

    if provider_name == "gemini":
        if last_entry.get("role") != "model":
            serialized.append({
                "role": "model",
                "parts": [{"text": combined_text}],
            })
        else:
            parts = last_entry.get("parts")
            if not isinstance(parts, list):
                parts = []
                last_entry["parts"] = parts
            text_appended = False
            for part in reversed(parts):
                if isinstance(part, dict) and "text" in part:
                    part["text"] = (part.get("text") or "") + combined_text
                    text_appended = True
                    break
            if not text_appended:
                parts.append({"text": combined_text})
    elif provider_name == "anthropic":
        if last_entry.get("role") != "assistant":
            serialized.append({
                "role": "assistant",
                "content": [{"type": "text", "text": combined_text}],
            })
        else:
            content = last_entry.get("content")
            if isinstance(content, str):
                last_entry["content"] = content + combined_text
            else:
                if not isinstance(content, list):
                    content = []
                    last_entry["content"] = content
                text_appended = False
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["text"] = (block.get("text") or "") + combined_text
                        text_appended = True
                        break
                if not text_appended:
                    content.append({"type": "text", "text": combined_text})
    elif provider_name == "openrouter":
        # OpenAI chat format: assistant content is a plain string (or None
        # when the turn was tool-calls only).
        if last_entry.get("role") != "assistant":
            serialized.append({
                "role": "assistant",
                "content": combined_text,
            })
        else:
            existing = last_entry.get("content")
            base = existing if isinstance(existing, str) else ""
            last_entry["content"] = base + combined_text
    else:
        # Unknown provider: skip rather than guessing a shape.
        return

    try:
        _write_sdk_history_file(conversation_id, serialized, provider_name)
        logger.info(
            "Appended interruption marker to SDK history (%d entries, provider=%s) "
            "for conversation %s",
            len(serialized), provider_name, conversation_id,
        )
    except Exception:
        logger.warning(
            "Failed to write interrupted SDK history for conversation %s",
            conversation_id,
            exc_info=True,
        )


def _last_entry_has_dangling_tool_use(last_entry: dict, provider_name: str) -> bool:
    """Return True if the on-disk last entry ends on an unmatched tool_use."""
    if provider_name == "anthropic":
        if last_entry.get("role") != "assistant":
            return False
        content = last_entry.get("content")
        if not isinstance(content, list):
            return False
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        return False
    if provider_name == "gemini":
        if last_entry.get("role") != "model":
            return False
        parts = last_entry.get("parts")
        if not isinstance(parts, list):
            return False
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("function_call") or part.get("functionCall"):
                return True
        return False
    if provider_name == "openrouter":
        return bool(
            last_entry.get("role") == "assistant"
            and last_entry.get("tool_calls")
        )
    return False


# NOTE: ``_handle_api_mode`` and the legacy WS ``confirm_action_result``
# bridge were removed in devplan 00062 phase 3. API-mode runs are now
# serviced by ``chat/realtime/socket.py:_handle_send_message``; user
# input on blocking tools is resolved exclusively through the REST
# endpoints (``POST /app/api/wait-handles/{id}/resolve``,
# ``POST /app/api/action-requests/{id}/resolve``).
#
# The Docker-based Gemini CLI chat mode (``_handle_docker_mode`` and the
# ``chat_mode`` config knob) was removed entirely when gemini-cli was
# deprecated; all runs go through the API-mode path.
