"""SDK history persistence for the LLM integration.

Saves and loads the session history to/from disk so that
conversations survive server restarts. Provider-aware: includes
a 'provider' field in the saved JSON so the correct provider
can deserialize on load.
"""

import json
import logging

from chat.storage import ChatStorage, write_json_atomic
from chat.gemini_api.constants import _SDK_HISTORY_FILENAME

logger = logging.getLogger(__name__)


def _provider_name(provider) -> str:
    """Return a canonical name ('gemini'|'anthropic'|'openrouter'|'unknown')."""
    from chat.llm.gemini_provider import GeminiProvider
    from chat.llm.anthropic_provider import AnthropicProvider
    from chat.llm.openrouter_provider import OpenRouterProvider
    if isinstance(provider, GeminiProvider):
        return "gemini"
    if isinstance(provider, AnthropicProvider):
        return "anthropic"
    if isinstance(provider, OpenRouterProvider):
        return "openrouter"
    return "unknown"


def _write_sdk_history_file(
    conversation_id: str, serialized: list, provider_name: str,
) -> None:
    """Write the provider-named envelope to sdk_history.json.

    Compact JSON, replaced atomically (``write_json_atomic``), so a reader
    such as the cancel handler's ``_save_interrupted_sdk_history`` never
    sees a partially written file while a threaded save is in flight.
    """
    envelope = {
        "provider": provider_name,
        "history": serialized,
    }
    conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
    history_file = conversation_dir / _SDK_HISTORY_FILENAME
    write_json_atomic(history_file, envelope)


def _save_sdk_history(conversation_id: str, session, provider) -> None:
    """Serialize the session history to disk using the provider.

    Saves the history as a JSON file alongside chat_history.json.
    Includes a 'provider' field so _load_sdk_history knows which
    provider format to expect.

    Args:
        conversation_id: Conversation UUID.
        session: The session object (type varies by provider).
        provider: The LLMProvider instance (for serialization).
    """
    try:
        serialized = provider.save_history(session)
        provider_name = _provider_name(provider)
        _write_sdk_history_file(conversation_id, serialized, provider_name)
        logger.debug(
            "Saved SDK history (%d entries, provider=%s) for conversation %s",
            len(serialized), provider_name, conversation_id,
        )
    except Exception:
        logger.warning(
            "Failed to save SDK history for conversation %s",
            conversation_id,
            exc_info=True,
        )


def _load_sdk_history(conversation_id: str) -> tuple[list[dict], str] | None:
    """Load previously saved SDK history from disk.

    Handles both the new envelope format (with 'provider' field) and
    the legacy format (plain list of Content dicts from Gemini).

    Args:
        conversation_id: Conversation UUID.

    Returns:
        Tuple of (history_list, provider_name) where provider_name is
        'gemini' or 'anthropic', or None if unavailable.
    """
    try:
        conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
        history_file = conversation_dir / _SDK_HISTORY_FILENAME
        if not history_file.exists():
            return None
        with open(history_file, "r") as f:
            data = json.load(f)

        # New envelope format: {"provider": "...", "history": [...]}
        if isinstance(data, dict) and "history" in data:
            history = data["history"]
            provider_name = data.get("provider", "gemini")
            if not isinstance(history, list):
                logger.warning(
                    "SDK history file has unexpected format for conversation %s",
                    conversation_id,
                )
                return None
            logger.info(
                "Loaded SDK history (%d entries, provider=%s) for conversation %s",
                len(history), provider_name, conversation_id,
            )
            return (history, provider_name)

        # Legacy format: plain list (assumed Gemini)
        if isinstance(data, list):
            logger.info(
                "Loaded legacy SDK history (%d entries) for conversation %s",
                len(data), conversation_id,
            )
            return (data, "gemini")

        logger.warning(
            "SDK history file has unexpected format for conversation %s",
            conversation_id,
        )
        return None
    except Exception:
        logger.warning(
            "Failed to load SDK history for conversation %s, "
            "starting with empty history",
            conversation_id,
            exc_info=True,
        )
        return None
