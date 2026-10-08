"""Shared helpers for action-request attachments (file + link).

Used by the core ``UploadToDriveHandler`` and by plugin handlers whose
action requests carry file/link attachments (loaded via
QUEST_PLUGIN_PATH).

File attachments (``[{path, filename?}]``) are workspace paths Quest
reads off disk and uploads as multipart parts on Approve -- the upstream
``/validate/`` endpoint never sees them. All file-path validation
therefore lives here. The inner ``reject_unknown_params`` calls on the
non-file attachment shapes are also local-only: upstream silently
ignores unknown inner keys (Django's ``data.get(...)`` pattern), so the
inner allow-lists are the only way the model gets a same-turn signal
for a typo.

The 50 MB upload cap mirrors the download-side cap plugin file tools
enforce; keep the two in sync when changing either.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from chat.action_request_types._param_validation import reject_unknown_params

logger = logging.getLogger(__name__)


_ATTACHMENT_ENTRY_ALLOWED = frozenset({"path", "filename"})
_LINK_ATTACHMENT_ENTRY_ALLOWED = frozenset({"url", "name"})
_RENAME_ATTACHMENT_ENTRY_ALLOWED = frozenset({"id", "filename"})
_RENAME_LINK_ATTACHMENT_ENTRY_ALLOWED = frozenset({"id", "url", "name"})


_ATTACHMENT_MAX_SIZE_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Sync validators (called from handler.validate_params)
# ---------------------------------------------------------------------------


def validate_attachments_param(params: dict, key: str) -> list[dict]:
    """Validate a ``[{path, filename?}]`` list (workspace file uploads).

    Upstream never sees these -- they ride as multipart parts on Approve
    and only Quest reads the actual file bytes. Field-level validation
    therefore stays local in full: required ``path``, non-empty strings,
    optional ``filename``, and rejecting unknown inner keys.
    """
    if key not in params:
        return []
    raw = params[key]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list of {{path, filename?}} objects")

    out: list[dict] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"{key}[{idx}] must be an object with a `path` field"
            )
        reject_unknown_params(
            key,
            entry,
            _ATTACHMENT_ENTRY_ALLOWED,
            path=f"{key}[{idx}]",
        )
        path_raw = entry.get("path")
        if path_raw is None or not isinstance(path_raw, str):
            raise ValueError(
                f"{key}[{idx}] is missing required string field `path`"
            )
        path_stripped = path_raw.strip()
        if not path_stripped:
            raise ValueError(
                f"{key}[{idx}].path must be a non-empty string"
            )

        normalized: dict = {"path": path_stripped}

        filename_raw = entry.get("filename")
        if filename_raw is not None:
            if not isinstance(filename_raw, str):
                raise ValueError(
                    f"{key}[{idx}].filename must be a string if provided"
                )
            filename_stripped = filename_raw.strip()
            if filename_stripped:
                normalized["filename"] = filename_stripped

        out.append(normalized)

    return out


def reject_unknown_link_attachment_keys(params: dict, key: str) -> None:
    """Reject typo'd inner keys on a ``link_attachments`` /
    ``add_link_attachments`` entry. Upstream's ``_validate_link_attachments``
    only reads ``url`` and ``name`` via ``.get(...)``, so a stray field
    would be silently dropped without this local pre-check.
    """
    if key not in params:
        return
    raw = params[key]
    if not isinstance(raw, list):
        return
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        reject_unknown_params(
            key,
            entry,
            _LINK_ATTACHMENT_ENTRY_ALLOWED,
            path=f"{key}[{idx}]",
        )


def reject_unknown_rename_attachment_keys(params: dict, key: str) -> None:
    """Inner allow-list for ``[{id, filename}]`` rename arrays."""
    if key not in params:
        return
    raw = params[key]
    if not isinstance(raw, list):
        return
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        reject_unknown_params(
            key,
            entry,
            _RENAME_ATTACHMENT_ENTRY_ALLOWED,
            path=f"{key}[{idx}]",
        )


def reject_unknown_rename_link_attachment_keys(params: dict, key: str) -> None:
    """Inner allow-list for ``[{id, url?, name?}]`` rename arrays."""
    if key not in params:
        return
    raw = params[key]
    if not isinstance(raw, list):
        return
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        reject_unknown_params(
            key,
            entry,
            _RENAME_LINK_ATTACHMENT_ENTRY_ALLOWED,
            path=f"{key}[{idx}]",
        )


# ---------------------------------------------------------------------------
# Async disk reader (called from handler.execute)
# ---------------------------------------------------------------------------


def _project_copy_hint(project_id: str | None) -> str:
    """Suffix for a not-found error in a project conversation: paths name
    this conversation's workspace, never the shared project workspace."""
    if not project_id:
        return ""
    return (
        " (paths are in this conversation's workspace; project files must "
        "be copied into it first with copy_project_file)"
    )


async def resolve_workspace_file(
    conversation_id: str | None,
    project_id: str | None,
    raw_path: str,
    *,
    max_size_bytes: int = _ATTACHMENT_MAX_SIZE_BYTES,
) -> Path:
    """Resolve and validate a single workspace file, returning its ``Path``.

    Runs all the guards without reading the bytes: absolute paths and
    ``..`` segments are rejected, the resolved path must stay inside the
    conversation workspace, and the file must exist, be a regular file,
    and be at or under ``max_size_bytes``. Raises ``RuntimeError`` on any
    violation so the failure surfaces out of ``handler.execute()`` and
    leaves the action request open for retry.

    Callers that want a pre-flight pass over many files before performing
    any external mutation (e.g. multi-file ``upload_to_drive``) call this
    per file up front, then read each file's bytes one at a time so the
    whole batch is never held in memory.

    ``conversation_id`` is required (workspace resolution needs it); a
    ``None`` value raises ``RuntimeError``, mirroring
    :func:`read_workspace_attachments`. ``project_id`` never changes the
    root: every conversation, project conversations included, resolves to
    its own conversation workspace (``conversation_workspace_dir``); it only
    adds the ``copy_project_file`` hint to a not-found error.
    """
    if not conversation_id:
        raise RuntimeError(
            "Cannot read workspace file: conversation_id is missing."
        )

    # Lazy import to avoid a circular dependency: tool_handlers itself
    # imports from chat.storage and various API helpers.
    from chat.gemini_api.tool_handlers import conversation_workspace_dir

    workspace_dir = await conversation_workspace_dir(conversation_id)
    workspace_root = workspace_dir.resolve()

    candidate_path = Path(raw_path)

    if candidate_path.is_absolute():
        raise RuntimeError(
            f"Invalid path {raw_path!r}: absolute paths are not allowed. "
            "Provide a workspace-relative path."
        )

    if any(part == ".." for part in candidate_path.parts):
        raise RuntimeError(
            f"Invalid path {raw_path!r}: parent-directory traversal "
            "('..') is not allowed."
        )

    file_path = (workspace_root / candidate_path).resolve()
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        raise RuntimeError(
            f"Invalid path {raw_path!r}: resolved destination is outside "
            "the conversation workspace."
        )

    if not file_path.exists():
        raise RuntimeError(
            f"File not found in workspace: {raw_path}"
            + _project_copy_hint(project_id)
        )
    if not file_path.is_file():
        raise RuntimeError(f"Path is not a regular file: {raw_path}")

    size_bytes = file_path.stat().st_size
    if size_bytes > max_size_bytes:
        raise RuntimeError(
            f"File {raw_path!r} is {size_bytes} bytes; uploads are capped "
            f"at {max_size_bytes} bytes (50 MB)."
        )

    return file_path


def read_resolved_file_bytes(file_path: Path, raw_path: str) -> tuple[str, bytes, str]:
    """Read a path already validated by :func:`resolve_workspace_file` and
    return ``(basename, bytes, content_type)``. ``raw_path`` is only used
    in the error message."""
    try:
        file_bytes = file_path.read_bytes()
    except Exception as exc:
        raise RuntimeError(f"Failed to read file {raw_path!r}: {exc}")

    content_type, _ = mimetypes.guess_type(str(file_path))
    if not content_type:
        content_type = "application/octet-stream"

    return (file_path.name, file_bytes, content_type)


async def read_workspace_file_bytes(
    conversation_id: str | None,
    project_id: str | None,
    raw_path: str,
    *,
    max_size_bytes: int = _ATTACHMENT_MAX_SIZE_BYTES,
) -> tuple[str, bytes, str]:
    """Read a single workspace file off disk and return
    ``(basename, bytes, content_type)``.

    Resolve-and-read convenience over :func:`resolve_workspace_file` +
    :func:`read_resolved_file_bytes`; shares the same traversal guards as
    :func:`read_workspace_attachments`.
    """
    file_path = await resolve_workspace_file(
        conversation_id,
        project_id,
        raw_path,
        max_size_bytes=max_size_bytes,
    )
    return read_resolved_file_bytes(file_path, raw_path)


async def read_workspace_attachments(
    conversation_id: str | None,
    project_id: str | None,
    attachments: list[dict],
) -> list[tuple[str, bytes, str]]:
    """Read each attachment off the conversation workspace and return
    ``(filename, bytes, content_type)`` tuples ready for an httpx
    multipart body.

    Raises ``RuntimeError`` if any path is invalid, missing, oversize,
    or resolves outside the workspace. Errors propagate out of
    ``handler.execute()`` and leave the action request open so the user
    can fix the workspace and retry approval -- there is no
    partial-success path on this side. (The upstream service may still
    report ``file_errors`` for internal failures we cannot predict.)

    Args:
        conversation_id: Conversation UUID for workspace resolution.
            Required when at least one attachment is supplied.
        project_id: Never changes the root: attachments always resolve
            against the conversation workspace root
            (``conversation_workspace_dir``), project conversations
            included; only adds the ``copy_project_file`` hint to a
            not-found error.
        attachments: Pre-validated entries from
            :func:`validate_attachments_param` -- each has a ``path``
            and an optional ``filename`` override.
    """
    if not attachments:
        return []

    if not conversation_id:
        raise RuntimeError(
            "Cannot read workspace attachments: conversation_id is missing."
        )

    # Lazy import to avoid a circular dependency: tool_handlers itself
    # imports from chat.storage and various API helpers.
    from chat.gemini_api.tool_handlers import (
        conversation_workspace_dir,
        _sanitize_workspace_filename,
    )

    workspace_dir = await conversation_workspace_dir(conversation_id)
    workspace_root = workspace_dir.resolve()

    out: list[tuple[str, bytes, str]] = []
    for entry in attachments:
        raw_path = entry["path"]
        candidate_path = Path(raw_path)

        if candidate_path.is_absolute():
            raise RuntimeError(
                f"Invalid attachment path {raw_path!r}: absolute paths "
                "are not allowed. Provide a workspace-relative path."
            )

        if any(part == ".." for part in candidate_path.parts):
            raise RuntimeError(
                f"Invalid attachment path {raw_path!r}: parent-directory "
                "traversal ('..') is not allowed."
            )

        file_path = (workspace_root / candidate_path).resolve()
        try:
            file_path.relative_to(workspace_root)
        except ValueError:
            raise RuntimeError(
                f"Invalid attachment path {raw_path!r}: resolved "
                "destination is outside the conversation workspace."
            )

        if not file_path.exists():
            raise RuntimeError(
                f"Attachment file not found in workspace: {raw_path}"
                + _project_copy_hint(project_id)
            )
        if not file_path.is_file():
            raise RuntimeError(
                f"Attachment path is not a regular file: {raw_path}"
            )

        size_bytes = file_path.stat().st_size
        if size_bytes > _ATTACHMENT_MAX_SIZE_BYTES:
            raise RuntimeError(
                f"Attachment {raw_path!r} is {size_bytes} bytes; uploads are "
                f"capped at {_ATTACHMENT_MAX_SIZE_BYTES} bytes "
                "(50 MB)."
            )

        try:
            file_bytes = file_path.read_bytes()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to read attachment {raw_path!r}: {exc}"
            )

        # Resolve the multipart filename: caller's `filename` override
        # (sanitized) wins; otherwise fall back to the resolved file's
        # basename. If the override sanitizes to empty, also fall back.
        override = entry.get("filename")
        resolved_filename: str | None = None
        if override:
            candidate = _sanitize_workspace_filename(override)
            if candidate:
                resolved_filename = candidate
        if not resolved_filename:
            resolved_filename = file_path.name

        # Upstream stores arbitrary bytes; the content-type is informational
        # but a sensible guess helps when the file is later served back.
        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"

        out.append((resolved_filename, file_bytes, content_type))

    return out
