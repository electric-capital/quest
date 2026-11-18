"""Workspace helpers shared by every handler module and by callers outside
the package (authed_get, Gmail drafts, plugin file-download tools).
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def conversation_workspace_dir(conversation_id: str) -> Path:
    """Return the conversation workspace root, creating it if missing.

    ``ChatStorage.get_conversation_workspace_root`` for EVERY conversation,
    standalone or in a project: the default target of every file-producing
    tool, and what the conversation file routes and ``/workspace`` in the
    sandbox see.
    """
    from chat.storage import ChatStorage
    workspace_dir = ChatStorage.get_conversation_workspace_root(conversation_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


async def project_workspace_dir(project_id: str) -> Path:
    """Return the project workspace root, creating it if missing.

    ``ChatStorage.get_project_workspace_root``: the workspace shared by all
    conversations of the project, reached only through ``proj://`` paths
    of the space-aware file tools (``list_files`` / ``read_file`` /
    ``write_file`` / ``edit_file`` / ``copy_file``), ``/project`` in the
    sandbox and the project file routes.
    """
    from chat.storage import ChatStorage
    workspace_dir = ChatStorage.get_project_workspace_root(project_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


async def autoproject_workspace_dir(autoproject_id: str) -> Path:
    """Return the AutoProject workspace root, creating it if missing.

    ``ChatStorage.get_autoproject_workspace_root``: the workspace shared by every
    run of an AutoProject, reached only through ``autoproject://`` paths of
    the space-aware file tools, ``/autoproject`` in the sandbox and the AutoProject file
    routes. The resolver never creates it; the first tool use does, here.

    Raises:
        InvalidStorageIdError: ``autoproject_id`` is not a valid storage id.
    """
    from chat.storage import ChatStorage
    workspace_dir = ChatStorage.get_autoproject_workspace_root(autoproject_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    return workspace_dir


def _no_project_result() -> str:
    """Structured refusal of a ``proj://`` path outside a project.

    The space-aware file tools are only offered in project conversations;
    this guards a call that reaches dispatch anyway (a standalone
    conversation, a stale schema, a hallucinated path).
    """
    return json.dumps({
        "error": (
            "This conversation is not part of a project, so proj:// paths "
            "do not exist here. Use chat:// paths for this conversation's "
            "workspace."
        ),
        "code": "no_project",
    })


def _invalid_project_result() -> str:
    """Structured refusal when the conversation's project id does not
    resolve to a valid project workspace (``InvalidStorageIdError``)."""
    return json.dumps({
        "error": "This conversation's project workspace could not be resolved.",
        "code": "invalid_project",
    })


def _no_autoproject_result() -> str:
    """Structured refusal of an ``autoproject://`` path outside an AutoProject run.

    ``parse_space_path`` already refuses ``autoproject://`` for a conversation
    without an AutoProject id; this guards a direct ``resolve_space_root`` call.
    """
    return json.dumps({
        "error": (
            "This conversation is not an AutoProject run, so autoproject:// "
            "paths do not exist here."
        ),
        "code": "no_autoproject",
    })


def _invalid_autoproject_result() -> str:
    """Structured refusal when the conversation's AutoProject id does not resolve
    to a valid AutoProject workspace (``InvalidStorageIdError``)."""
    return json.dumps({
        "error": "This run's AutoProject workspace could not be resolved.",
        "code": "invalid_autoproject",
    })


def _publish_file_list_changed(
    user_id: int,
    scope: str,
    conversation_id: str | None,
    project_id: str | None,
    autoproject_id: str | None = None,
) -> None:
    """Best-effort: notify the user's WS subscribers that a workspace
    listing has changed so the file browser can silent-refresh.

    ``scope`` is the workspace that changed, set explicitly by the caller:
    ``"conversation"`` for the conversation workspace (every tool write by
    default), ``"project"`` for the project workspace (project file and
    copy tools), whose events reach every sibling conversation's tab, and
    ``"autoproject"`` for the AutoProject workspace of ``autoproject_id`` (required for that
    scope). Same argument order as the ``chat.file_routes`` helper.
    """
    try:
        from chat.realtime import bus, events as realtime_events
        bus.publish_to_user(
            user_id,
            realtime_events.make_file_list_changed(
                conversation_id=conversation_id,
                project_id=project_id,
                scope=scope,
                autoproject_id=autoproject_id,
            ),
        )
    except Exception:
        logger.debug(
            "[tool_handlers] publish file_list_changed failed "
            "(user_id=%s, scope=%s, conversation_id=%s, project_id=%s, "
            "autoproject_id=%s)",
            user_id, scope, conversation_id, project_id, autoproject_id,
            exc_info=True,
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

