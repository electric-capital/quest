"""Workspace helpers shared by every handler module and by callers outside
the package (authed_get, Gmail drafts, plugin file-download tools).
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _get_workspace_dir(conversation_id: str, project_id: str | None = None) -> Path:
    """Return the conversation workspace root, creating it if missing.

    Resolves to ``ChatStorage.get_conversation_workspace_root`` for EVERY
    conversation, project conversations included, so tools and the HTTP
    file routes agree on where a conversation's files live. ``project_id``
    is accepted for call-site compatibility and ignored; the shared project
    workspace is not reachable through this helper. Phase 2 of the
    per-conversation-workspace change replaces it with explicit
    conversation / project workspace helpers.
    """
    from chat.storage import ChatStorage
    workspace_dir = ChatStorage.get_conversation_workspace_root(conversation_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def _publish_file_list_changed(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
) -> None:
    """Best-effort: notify the user's WS subscribers that a workspace
    listing has changed so the file browser can silent-refresh.

    Always ``scope="conversation"``: tool writes land in the conversation
    workspace (see ``_get_workspace_dir``), even in project conversations.
    ``project_id`` is still carried on the event.
    """
    try:
        from chat.realtime import bus, events as realtime_events
        bus.publish_to_user(
            user_id,
            realtime_events.make_file_list_changed(
                conversation_id=conversation_id,
                project_id=project_id,
                scope="conversation",
            ),
        )
    except Exception:
        logger.debug(
            "[tool_handlers] publish file_list_changed failed "
            "(user_id=%s, conversation_id=%s)",
            user_id, conversation_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Shared download-to-workspace helpers (used by authed_get, Gmail drafts,
# and plugin file-download tools)
# ---------------------------------------------------------------------------


def _parse_content_disposition_filename(header_value: str) -> str | None:
    """Extract a filename from a Content-Disposition header.

    Implements a small subset of RFC 6266 sufficient for endpoints that
    set either ``filename="..."`` (for ``attachment``) or ``filename="..."``
    inside an ``inline; ...`` disposition for PDFs. Prefers the
    RFC 5987 ``filename*=UTF-8''<percent-encoded>`` form when present
    so non-ASCII filenames round-trip correctly.

    Returns None if no filename can be extracted.
    """
    if not header_value:
        return None

    from email.message import Message
    from urllib.parse import unquote

    # email.message.Message handles the basic ``key="value"`` parsing for us.
    msg = Message()
    msg["Content-Disposition"] = header_value

    # RFC 5987-encoded filename* takes precedence.
    star = msg.get_param("filename*", header="Content-Disposition")
    if star is not None:
        # ``get_param`` returns either a 3-tuple ``(charset, lang, value)``
        # for RFC 2231 / 5987 encoded values, or a plain string.
        if isinstance(star, tuple):
            charset, _lang, value = star
            try:
                return unquote(value, encoding=charset or "utf-8")
            except Exception:
                return unquote(value)
        return star

    plain = msg.get_param("filename", header="Content-Disposition")
    if isinstance(plain, tuple):
        # Unusual but possible. Same handling as above.
        charset, _lang, value = plain
        try:
            return unquote(value, encoding=charset or "utf-8")
        except Exception:
            return unquote(value)
    return plain


def _sanitize_workspace_filename(name: str) -> str:
    """Strip path separators and leading dots from a candidate filename."""
    cleaned = name.replace("/", "_").replace("\\", "_").strip()
    # Drop leading dots so we never write hidden files via the upstream name.
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    return cleaned

