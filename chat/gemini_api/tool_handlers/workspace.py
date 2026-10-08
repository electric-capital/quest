"""Workspace file handlers: the conversation-workspace ``*_workspace_file``
tools, the space-aware ``list_files`` / ``read_file`` / ``write_file`` /
``edit_file`` tools, and load_gmail_attachment.

A project conversation has two file spaces (devplan 00009, revised
2026-10-08: scheme-qualified file tools): its own conversation workspace
(``chat://``, the default target of every file-producing tool) and the
project workspace shared by every conversation of the project
(``proj://``). The ``*_workspace_file`` tools take bare paths in the
conversation workspace; the space-aware tools take a mandatory scheme
(parsed by ``file_paths.parse_space_path``). Both families are thin
wrappers over the root-parameterised internals ``_list_files`` /
``_read_file`` / ``_write_file`` / ``_edit_file``; the ``_FileSpace`` they
pass fixes the read-sidecar keys, the ``file_list_changed`` scope and the
wording of results and errors. ``copy_file`` lives in ``workspace_copy.py``.

Read-before-edit sidecar keys: the ``*_workspace_file`` tools use bare
paths (``notes.md``), the space-aware tools the canonical scheme path
(``chat://notes.md``, ``proj://reports/q3.md``). A read or write of a
conversation file records BOTH keys, so either tool family licenses an
edit through either; ``proj://`` keys never collide with a bare key.
"""

import asyncio
import json
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from chat.storage import ChatStorage
from chat.gemini_api.constants import (
    _TEXT_INLINE_LIMIT,
    _TEXT_EXTENSIONS,
    _TEXT_MIME_PREFIXES,
    _UNSUPPORTED_GEMINI_MIME_TYPES,
    _WRITE_FILE_MAX_SIZE,
)
from chat.gemini_api.tool_handlers._common import (
    _invalid_project_result,
    _no_project_result,
    _publish_file_list_changed,
    conversation_workspace_dir,
    project_workspace_dir,
)
from chat.gemini_api.tool_handlers.file_paths import (
    SPACE_CHAT,
    SPACE_PROJECT,
    SpacePathError,
    format_space_path,
    parse_space_path,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FileSpace:
    """How the shared internals address one file space for one tool family."""

    # SPACE_CHAT or SPACE_PROJECT.
    space: str
    # True for the space-aware tools: paths are shown scheme-qualified.
    qualified: bool
    # file_list_changed scope published after a write / edit.
    scope: str
    # Words used in error text.
    noun: str          # "File" / "Project file"
    root_label: str    # "workspace" / "project workspace"
    get_tool: str
    write_tool: str

    def show(self, clean_path: str) -> str:
        """The path as the model should see it in results."""
        if self.qualified:
            return format_space_path(self.space, clean_path)
        return clean_path

    def read_keys(self, canonical_path: str) -> list[str]:
        """Sidecar keys recorded for a read / write, and accepted by the
        edit gate. Conversation files carry both the bare and the
        ``chat://`` key (cross-family licensing); project files only the
        ``proj://`` key."""
        if self.space == SPACE_PROJECT:
            return [format_space_path(SPACE_PROJECT, canonical_path)]
        chat_key = format_space_path(SPACE_CHAT, canonical_path)
        if self.qualified:
            return [chat_key, canonical_path]
        return [canonical_path, chat_key]


# *_workspace_file tools: bare conversation-workspace paths.
_WORKSPACE_TOOLS_SPACE = _FileSpace(
    space=SPACE_CHAT,
    qualified=False,
    scope="conversation",
    noun="File",
    root_label="workspace",
    get_tool="get_workspace_file",
    write_tool="write_workspace_file",
)

# Space-aware tools on chat:// paths.
_CHAT_SPACE = _FileSpace(
    space=SPACE_CHAT,
    qualified=True,
    scope="conversation",
    noun="File",
    root_label="conversation workspace",
    get_tool="read_file",
    write_tool="write_file",
)

# Space-aware tools on proj:// paths.
_PROJECT_SPACE = _FileSpace(
    space=SPACE_PROJECT,
    qualified=True,
    scope="project",
    noun="Project file",
    root_label="project workspace",
    get_tool="read_file",
    write_tool="write_file",
)

_SPACES = {SPACE_CHAT: _CHAT_SPACE, SPACE_PROJECT: _PROJECT_SPACE}


def _mark_workspace_file_read(conversation_id: str, keys: list[str]) -> None:
    """Best-effort: record that the model has seen this file's contents in
    this conversation (read or written), licensing later edits of it.

    ``keys`` comes from ``_FileSpace.read_keys``. Failures never break the
    read/write.
    """
    try:
        ChatStorage.add_workspace_read_paths(conversation_id, keys)
    except Exception:
        logger.debug(
            "[tool_handlers] failed to record workspace read "
            "(conversation_id=%s, keys=%s)",
            conversation_id, keys, exc_info=True,
        )


def _format_mb(size_bytes: int) -> str:
    """Render a byte count as MB with one decimal (for error messages)."""
    return f"{size_bytes / (1024 * 1024):.1f}"


def _project_sandbox_hint(space: _FileSpace, clean_path: str) -> str:
    """Extra sentence for suggestions about a project file: scripts see it
    under ``/project`` and their output belongs in ``/workspace``."""
    if space.space != SPACE_PROJECT:
        return ""
    return (
        f" In scripts this project file is /project/{clean_path}; write the "
        "smaller pieces to /workspace and read them with read_file on "
        "chat://<piece>."
    )


def _oversize_suggestion(
    mime_type: str, is_text: bool, limit_bytes: int,
    read_tool: str = "get_workspace_file",
) -> str:
    """Return an actionable suggestion for a file too large to attach."""
    limit_mb = _format_mb(limit_bytes)
    if is_text:
        return (
            "Use run_python or run_script to process the file in the sandbox "
            "instead (grep, slice, aggregate, or split it into smaller files "
            f"under {limit_mb} MB and read those)."
        )
    if mime_type == "application/pdf":
        # pypdf is preinstalled in the script-runner image.
        return (
            "Use run_python or run_script to shrink the file first -- the "
            "sandbox has pypdf preinstalled. For example, split the PDF into "
            "chunks of pages (pypdf.PdfReader / PdfWriter) and write each "
            "chunk to the workspace, or extract the text layer to a .txt/.md "
            f"file, then call {read_tool} on the smaller pieces one at "
            f"a time. Each piece must stay under {limit_mb} MB."
        )
    if mime_type.startswith("image/"):
        return (
            "Use run_python or run_script to downscale or re-encode the image "
            "(e.g. with Pillow) and write the smaller copy to the workspace, "
            f"then call {read_tool} on it. The result must stay under "
            f"{limit_mb} MB."
        )
    return (
        "Use run_python or run_script to extract or reduce the file contents "
        f"programmatically, writing pieces under {limit_mb} MB to the "
        "workspace and reading those instead."
    )


def _is_text_file(file_path: Path) -> bool:
    """Determine if a file is likely a text file based on extension and MIME type."""
    if file_path.suffix.lower() in _TEXT_EXTENSIONS:
        return True
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type and any(mime_type.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES):
        return True
    return False


def _resolve_in_root(
    root: Path, path: str, space: _FileSpace, *, allow_empty: bool,
) -> tuple[str, Path | None, str | None]:
    """Validate ``path`` against ``root``.

    Returns ``(clean_path, resolved_file_path, None)`` or
    ``(clean_path, None, error_json)``. Errors of the space-aware tools
    carry ``"code": "invalid_path"``; the ``*_workspace_file`` tools keep
    their historical code-less shape.
    """
    def _err(message: str) -> str:
        body = {"error": message}
        if space.qualified:
            body["code"] = "invalid_path"
        return json.dumps(body)

    clean_path = path.lstrip("/").lstrip("\\")
    if not clean_path and not allow_empty:
        return clean_path, None, _err("Invalid path: path cannot be empty")
    if ".." in clean_path:
        return clean_path, None, _err("Invalid path: path traversal not allowed")
    file_path = (root / clean_path).resolve()
    try:
        file_path.relative_to(root.resolve())
    except ValueError:
        return clean_path, None, _err(
            f"Invalid path: outside {space.root_label} directory"
        )
    return clean_path, file_path, None


def _probe_file(file_path: Path, root: Path) -> tuple[str | None, int, str]:
    """Sync stat of a resolved path (run in a thread).

    Returns ``(kind, size, canonical_path)``: kind None when missing,
    ``"file"`` for a regular file, ``"other"`` otherwise; the canonical
    root-relative form ('./a.txt', 'a.txt', '/a.txt' all normalize
    identically) is what read tracking (the edit gate) keys on.
    """
    canonical_path = str(file_path.relative_to(root.resolve()))
    if not file_path.exists():
        return None, 0, canonical_path
    if not file_path.is_file():
        return "other", 0, canonical_path
    return "file", file_path.stat().st_size, canonical_path


def _read_text_lenient(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8", errors="replace")


def _write_text_with_parents(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Root-parameterised internals (shared by both tool families)
# ---------------------------------------------------------------------------


def _list_files_sync(root: Path, start: Path | None = None) -> str:
    files = []
    # ``start`` comes from _resolve_in_root (resolved), so compare it
    # against the resolved root.
    base = root if start is None else root.resolve()
    for file_path in sorted((start or root).rglob("*")):
        if file_path.is_file():
            relative = str(file_path.relative_to(base))
            stat = file_path.stat()
            files.append({
                "path": relative,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })

    return json.dumps({
        "file_count": len(files),
        "files": files,
    })


async def _list_files(root: Path, start: Path | None = None) -> str:
    """List every file below ``start`` (default: ``root``), JSON
    ``file_count`` + ``files`` with paths relative to ``root``.

    The tree walk is unbounded, so it runs in a worker thread.
    """
    return await asyncio.to_thread(_list_files_sync, root, start)


async def _read_file(
    provider,
    user_id: int,
    conversation_id: str,
    root: Path,
    path: str,
    space: _FileSpace,
    model: str = "",
) -> tuple[str, list]:
    """Return a file below ``root`` to the model (see ``_handle_get_workspace_file``).

    Records the file's ``_FileSpace.read_keys`` in the conversation's read sidecar on
    every successful read. Blocking file I/O runs in worker threads; the
    sidecar update stays synchronous (see ``add_workspace_read_paths``).
    """
    clean_path, file_path, err = _resolve_in_root(
        root, path, space, allow_empty=True,
    )
    if err is not None:
        return err, []

    kind, file_size, canonical_path = await asyncio.to_thread(
        _probe_file, file_path, root,
    )
    shown = space.show(clean_path)
    asked = shown if space.qualified else path
    if kind is None:
        return json.dumps({"error": f"{space.noun} not found: {asked}"}), []

    if kind != "file":
        return json.dumps({"error": f"Not a file: {asked}"}), []

    is_text = _is_text_file(file_path)
    keys = space.read_keys(canonical_path)

    # Small text files: return contents inline in the tool response
    if is_text and file_size <= _TEXT_INLINE_LIMIT:
        try:
            content = await asyncio.to_thread(_read_text_lenient, file_path)
            _mark_workspace_file_read(conversation_id, keys)
            return json.dumps({
                "path": shown,
                "size_bytes": file_size,
                "content": content,
            }), []
        except Exception as e:
            return json.dumps({"error": f"Failed to read file: {e}"}), []

    # Large or binary files: upload via provider
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "application/octet-stream"

    # Pre-check: reject MIME types known to be unsupported by the Gemini
    # content generation API (only relevant for Gemini provider).
    from chat.llm.gemini_provider import GeminiProvider
    if isinstance(provider, GeminiProvider) and mime_type in _UNSUPPORTED_GEMINI_MIME_TYPES:
        return json.dumps({
            "error": "File could not be uploaded: this file type is not supported by the Gemini API for direct analysis.",
            "path": shown,
            "size_bytes": file_size,
            "mime_type": mime_type,
            "suggestion": (
                "Use run_script to extract the file contents programmatically "
                "(e.g., python-docx for .docx files, openpyxl for .xlsx files)."
                + _project_sandbox_hint(space, clean_path)
            ),
        }), []

    # Pre-flight size check: reject files too large to attach to a request
    # for the model actually being called (conversation or sub-agent model)
    # BEFORE any upload/inline attempt. Without this, the oversized part
    # would be appended to session history and blow the provider's payload
    # limit on every subsequent turn, permanently breaking the conversation.
    # Covers both the binary upload path and the large-text inline fallback
    # below (both attach the bytes to the request).
    from chat.llm.file_limits import get_attach_limit_for_model
    limit_bytes, backend_label = get_attach_limit_for_model(model, mime_type)
    if file_size > limit_bytes:
        return json.dumps({
            "error": (
                f"File is too large to attach to the model request: "
                f"{clean_path} is {_format_mb(file_size)} MB, but the current "
                f"model ({model or 'unknown'}, {backend_label}) accepts at "
                f"most {_format_mb(limit_bytes)} MB per attached file. "
                "The request was not sent, so the conversation is unaffected."
            ),
            "path": shown,
            "size_bytes": file_size,
            "limit_bytes": limit_bytes,
            "mime_type": mime_type,
            "model": model,
            "backend": backend_label,
            "suggestion": (
                _oversize_suggestion(
                    mime_type, is_text, limit_bytes, read_tool=space.get_tool,
                )
                + _project_sandbox_hint(space, clean_path)
            ),
        }), []

    try:
        uploaded_file = await provider.upload_file(
            file_path=str(file_path),
            mime_type=mime_type,
            display_name=file_path.name,
            model=model,
        )

        if uploaded_file is None:
            # Provider does not support file upload (e.g., Anthropic).
            # For text-like files that are just too large, try reading anyway.
            if is_text:
                try:
                    content = await asyncio.to_thread(_read_text_lenient, file_path)
                    _mark_workspace_file_read(conversation_id, keys)
                    return json.dumps({
                        "path": shown,
                        "size_bytes": file_size,
                        "content": content,
                        "note": "File was returned inline (large text file).",
                    }), []
                except Exception:
                    pass
            return json.dumps({
                "error": "File upload is not supported by this model provider.",
                "path": shown,
                "size_bytes": file_size,
                "mime_type": mime_type,
                "suggestion": (
                    "Use run_python to read and process the file contents "
                    "programmatically instead."
                    + _project_sandbox_hint(space, clean_path)
                ),
            }), []

        # Build a content part so the model can see the file contents
        file_part = provider.make_file_part(uploaded_file)

        _mark_workspace_file_read(conversation_id, keys)
        return json.dumps({
            "path": shown,
            "size_bytes": file_size,
            "mime_type": getattr(uploaded_file, 'mime_type', mime_type),
            "uploaded": True,
            "note": "The file has been uploaded and is now available for you to analyze. It is included in this response.",
        }), [file_part]
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as e:
        logger.warning(
            "Failed to upload %s file "
            "(user_id=%s, conversation=%s, path=%s, mime_type=%s): [%s] %s",
            space.root_label, user_id, conversation_id, clean_path, mime_type,
            type(e).__name__, e,
            exc_info=True,
        )
        return json.dumps({
            "error": "File could not be read directly and could not be uploaded.",
            "path": shown,
            "size_bytes": file_size,
            "mime_type": mime_type,
            "api_error_type": type(e).__name__,
            "api_error_detail": str(e),
            "suggestion": (
                "Consider using run_script to extract the file contents "
                "programmatically (e.g., python-docx for .docx files, "
                "openpyxl for .xlsx files)."
                + _project_sandbox_hint(space, clean_path)
            ),
        }), []


async def _write_file(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    root: Path,
    path: str,
    content: str,
    space: _FileSpace,
) -> str:
    """Create or overwrite a text file below ``root`` (see
    ``_handle_write_workspace_file``); publishes ``file_list_changed`` for
    ``space.scope`` and records the write as a read."""
    clean_path, file_path, err = _resolve_in_root(
        root, path, space, allow_empty=False,
    )
    if err is not None:
        return err

    # Validate content size
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > _WRITE_FILE_MAX_SIZE:
        return json.dumps({
            "error": f"Content too large: {len(content_bytes)} bytes (maximum is {_WRITE_FILE_MAX_SIZE} bytes / {_WRITE_FILE_MAX_SIZE // 1024}KB)"
        })

    # Prevent writing to directories that exist as files and vice versa
    if await asyncio.to_thread(file_path.is_dir):
        return json.dumps({"error": f"Cannot write file: '{space.show(clean_path)}' is a directory"})

    try:
        # Create parent directories if needed, then write the file
        await asyncio.to_thread(_write_text_with_parents, file_path, content)

        _publish_file_list_changed(user_id, space.scope, conversation_id, project_id)
        # Writing counts as having read the file (the model authored the
        # full content), licensing later edits.
        _mark_workspace_file_read(
            conversation_id,
            space.read_keys(str(file_path.relative_to(root.resolve()))),
        )
        return json.dumps({
            "path": space.show(clean_path),
            "size_bytes": len(content_bytes),
            "status": "written",
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to write file: {e}"})


async def _edit_file(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    root: Path,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    space: _FileSpace,
) -> str:
    """Exact string replacement in a text file below ``root`` (see
    ``_handle_edit_workspace_file``), gated on the file's
    ``_FileSpace.read_keys`` in the read sidecar."""
    clean_path, file_path, err = _resolve_in_root(
        root, path, space, allow_empty=False,
    )
    if err is not None:
        return err

    # Validate arguments
    if not old_string:
        return json.dumps({"error": "old_string must not be empty"})

    if old_string == new_string:
        return json.dumps({
            "error": "old_string and new_string are identical -- nothing to change"
        })

    # Existence checks -- unlike write, edit never creates files or parents
    kind, file_size, canonical_path = await asyncio.to_thread(
        _probe_file, file_path, root,
    )
    shown = space.show(clean_path)
    asked = shown if space.qualified else path
    if kind is None:
        return json.dumps({"error": f"{space.noun} not found: {asked}"})

    if kind != "file":
        return json.dumps({"error": f"Not a file: {asked}"})

    # Read-before-edit gate: the model must have seen this file's contents
    # earlier in this conversation, through either tool family for a
    # conversation file (see ``_FileSpace.read_keys``).
    keys = space.read_keys(canonical_path)
    seen = set(ChatStorage.get_workspace_read_paths(conversation_id))
    if not seen.intersection(keys):
        return json.dumps({
            "error": (
                f"{space.noun} has not been read in this conversation: "
                f"{shown}. Read it with {space.get_tool} (or create it "
                f"with {space.write_tool}) before editing it."
            )
        })

    # Size guard: editing very large files is out of scope for this tool
    if file_size > _WRITE_FILE_MAX_SIZE:
        return json.dumps({
            "error": f"File too large to edit: {file_size} bytes (maximum is {_WRITE_FILE_MAX_SIZE} bytes / {_WRITE_FILE_MAX_SIZE // 1024}KB)",
            "suggestion": (
                "Use run_python or run_script to modify large files "
                "programmatically."
            ),
        })

    # Strict UTF-8 decode -- errors='replace' would corrupt the file on
    # write-back, so binary/undecodable files are rejected outright.
    try:
        content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
    except UnicodeDecodeError:
        return json.dumps({
            "error": f"File is not valid UTF-8 text: {shown}",
            "suggestion": (
                "Use run_python to modify binary or non-UTF-8 files "
                "programmatically."
            ),
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to read file: {e}"})

    # Match old_string (str.count is non-overlapping, matching str.replace)
    occurrences = content.count(old_string)
    if occurrences == 0:
        return json.dumps({
            "error": (
                "old_string not found in file. The file contents may have "
                f"changed -- re-read the file with {space.get_tool} and "
                "retry with the exact current text."
            )
        })

    if occurrences > 1 and not replace_all:
        return json.dumps({
            "error": (
                f"old_string appears {occurrences} times in the file. "
                "Include more surrounding context to make the match unique, "
                "or pass replace_all: true to replace every occurrence."
            )
        })

    replacements = occurrences if replace_all else 1
    new_content = content.replace(
        old_string, new_string, -1 if replace_all else 1
    )

    # Post-replacement size guard
    new_content_bytes = new_content.encode("utf-8")
    if len(new_content_bytes) > _WRITE_FILE_MAX_SIZE:
        return json.dumps({
            "error": f"Content too large: {len(new_content_bytes)} bytes (maximum is {_WRITE_FILE_MAX_SIZE} bytes / {_WRITE_FILE_MAX_SIZE // 1024}KB)"
        })

    try:
        await asyncio.to_thread(file_path.write_text, new_content, encoding="utf-8")
    except Exception as e:
        return json.dumps({"error": f"Failed to write file: {e}"})

    _publish_file_list_changed(user_id, space.scope, conversation_id, project_id)
    _mark_workspace_file_read(conversation_id, keys)
    return json.dumps({
        "path": shown,
        "size_bytes": len(new_content_bytes),
        "status": "edited",
        "replacements": replacements,
    })


# ---------------------------------------------------------------------------
# Conversation-space tools (*_workspace_file)
# ---------------------------------------------------------------------------


async def _handle_list_workspace_files(user_id: int, conversation_id: str, project_id: str | None = None) -> str:
    """List all files in the conversation workspace.

    Returns a JSON object with a list of relative file paths and metadata.
    This is a local tool -- no HTTP involved. ``project_id`` is accepted
    for call-site compatibility and does not affect the root: project
    files are listed by ``_handle_list_files`` (``proj://``).
    """
    root = await conversation_workspace_dir(conversation_id)
    return await _list_files(root)


async def _handle_get_workspace_file(
    provider,
    user_id: int,
    conversation_id: str,
    path: str,
    project_id: str | None = None,
    model: str = "",
) -> tuple[str, list]:
    """Retrieve a conversation-workspace file, returning contents or
    uploading it for analysis.

    This is a local tool -- no HTTP involved. Small text files come back
    inline; binary/large files go through the provider's upload_file()
    method so the model can see them, after a pre-flight size check
    against the attach cap of ``model``. For providers without file upload
    support (e.g. Anthropic), large binary files return an error with a
    suggestion to use run_python.

    Args:
        provider: LLMProvider instance (for file uploads).
        user_id: User's integer ID (for logging).
        conversation_id: Conversation ID.
        path: Relative path within the conversation workspace.
        project_id: Accepted for call-site compatibility; does not affect
            the root (project files: ``_handle_read_file`` on ``proj://``).
        model: The model actually being called (attach-cap pre-flight).

    Returns:
        Tuple of (result_json_string, extra_parts) where extra_parts is a
        list of provider-specific content parts to include alongside the
        function response (e.g., Part.from_uri for uploaded binary files).
    """
    root = await conversation_workspace_dir(conversation_id)
    return await _read_file(
        provider, user_id, conversation_id, root, path,
        _WORKSPACE_TOOLS_SPACE, model=model,
    )


async def _handle_load_gmail_attachment(
    provider,
    user: dict,
    message_id: str,
    attachment_id: str,
    filename: str = "attachment",
    mime_type: str = "application/octet-stream",
    model: str = "",
) -> tuple[str, list]:
    """Fetch a Gmail attachment and upload it for analysis.

    Fetches attachment bytes from the Gmail API using the user's Google
    Services credentials, writes them to a temporary file, uploads via
    the provider's upload_file() method, and returns a content part
    reference so the model can analyze the file directly.

    Args:
        provider: LLMProvider instance (for file uploads).
        user: Authenticated user dict (must have google_services_oauth).
        message_id: Gmail message ID containing the attachment.
        attachment_id: Gmail attachment ID from the message's attachments array.
        filename: Filename for the attachment (used as display name).
        mime_type: MIME type of the attachment.

    Returns:
        Tuple of (result_json_string, extra_parts) where extra_parts is a
        list of provider-specific content parts. On success, extra_parts
        contains a single file reference part.
    """
    import tempfile
    import os

    # Fetch the attachment bytes from Gmail
    try:
        from api.gmail import _resolve_gmail_attachment

        resolved = await _resolve_gmail_attachment(user, message_id, attachment_id, filename, mime_type)
        data = resolved["data"]
        final_filename = resolved["filename"] or filename or "attachment"
        final_mime_type = resolved["mime_type"] or mime_type or "application/octet-stream"
    except Exception as e:
        # Handle both HTTPException (from _resolve_gmail_attachment) and other errors
        try:
            from fastapi import HTTPException as _HTTPException
            if isinstance(e, _HTTPException):
                detail = e.detail
                if isinstance(detail, dict):
                    error_msg = detail.get("message", str(detail))
                else:
                    error_msg = str(detail)
                if e.status_code == 401:
                    return json.dumps({"error": "Google Services not connected. Please connect via Settings > Data Connections."}), []
                return json.dumps({"error": f"Failed to fetch Gmail attachment: {error_msg}"}), []
        except ImportError:
            pass
        logger.warning("[load_gmail_attachment] Failed to fetch attachment (message_id=%s, attachment_id=%s): %s", message_id, attachment_id, e)
        return json.dumps({"error": f"Failed to fetch Gmail attachment: {e}"}), []

    # Pre-check: reject MIME types known to be unsupported (Gemini-specific).
    from chat.llm.gemini_provider import GeminiProvider
    if isinstance(provider, GeminiProvider) and final_mime_type in _UNSUPPORTED_GEMINI_MIME_TYPES:
        return json.dumps({
            "error": "Attachment could not be uploaded: this file type is not supported by the Gemini API for direct analysis.",
            "filename": final_filename,
            "size_bytes": len(data),
            "mime_type": final_mime_type,
            "suggestion": (
                "Use run_script to extract the file contents programmatically "
                "(e.g., python-docx for .docx files, openpyxl for .xlsx files)."
            ),
        }), []

    # Pre-flight size check against the limit for the model actually being
    # called -- Gmail attachments can reach ~25 MB raw, above e.g. the
    # Anthropic-on-Vertex effective cap. Rejecting here keeps the oversized
    # bytes out of session history (see _handle_get_workspace_file).
    from chat.llm.file_limits import get_attach_limit_for_model
    limit_bytes, backend_label = get_attach_limit_for_model(model, final_mime_type)
    if len(data) > limit_bytes:
        return json.dumps({
            "error": (
                f"Attachment is too large to attach to the model request: "
                f"{final_filename} is {_format_mb(len(data))} MB, but the "
                f"current model ({model or 'unknown'}, {backend_label}) "
                f"accepts at most {_format_mb(limit_bytes)} MB per attached "
                "file. The request was not sent, so the conversation is "
                "unaffected."
            ),
            "filename": final_filename,
            "size_bytes": len(data),
            "limit_bytes": limit_bytes,
            "mime_type": final_mime_type,
            "model": model,
            "backend": backend_label,
            "suggestion": (
                "The attachment cannot be attached inline for this model. "
                "Ask the user to download the attachment themselves, or -- "
                "if a copy exists in the workspace via another route -- "
                "process it there with run_python or run_script (e.g. split "
                "a PDF into smaller chunks with the preinstalled pypdf)."
            ),
        }), []

    # Write bytes to a temporary file and upload via provider
    suffix = Path(final_filename).suffix or ".bin"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(data)

        uploaded_file = await provider.upload_file(
            file_path=tmp_path,
            mime_type=final_mime_type,
            display_name=final_filename,
            model=model,
        )

        if uploaded_file is None:
            # Provider does not support file upload (e.g., Anthropic)
            return json.dumps({
                "error": "File upload is not supported by this model provider.",
                "filename": final_filename,
                "size_bytes": len(data),
                "mime_type": final_mime_type,
                "suggestion": (
                    "Use run_python to process the attachment contents "
                    "programmatically instead."
                ),
            }), []

        file_part = provider.make_file_part(uploaded_file)

        return json.dumps({
            "message_id": message_id,
            "attachment_id": attachment_id,
            "filename": final_filename,
            "size_bytes": len(data),
            "mime_type": getattr(uploaded_file, 'mime_type', final_mime_type),
            "uploaded": True,
            "note": "The attachment has been uploaded and is now available for you to analyze. It is included in this response.",
        }), [file_part]

    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except BaseException as e:
        logger.warning(
            "[load_gmail_attachment] Failed to upload attachment "
            "(message_id=%s, filename=%s, mime_type=%s): [%s] %s",
            message_id, final_filename, final_mime_type,
            type(e).__name__, e,
            exc_info=True,
        )
        return json.dumps({
            "error": "Attachment could not be uploaded.",
            "filename": final_filename,
            "size_bytes": len(data),
            "mime_type": final_mime_type,
            "api_error_type": type(e).__name__,
            "api_error_detail": str(e),
            "suggestion": (
                "Consider using run_script to extract the file contents "
                "programmatically (e.g., python-docx for .docx files, "
                "openpyxl for .xlsx files)."
            ),
        }), []
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def _handle_write_workspace_file(
    user_id: int,
    conversation_id: str,
    path: str,
    content: str,
    project_id: str | None = None,
) -> str:
    """Write a file to the conversation workspace.

    Creates or overwrites a file at the specified path within the workspace.
    Parent directories are created automatically if they don't exist.

    Args:
        user_id: User's integer ID (for file_list_changed publishing).
        conversation_id: Conversation UUID.
        path: Relative file path within workspace (e.g., 'output.txt', 'src/main.py').
        content: File content as a string.
        project_id: Carried on the ``file_list_changed`` event only; does
            not affect the root (project files: ``_handle_write_file``).

    Returns:
        JSON string with the result (success with file metadata, or error).
    """
    root = await conversation_workspace_dir(conversation_id)
    return await _write_file(
        user_id, conversation_id, project_id, root, path, content,
        _WORKSPACE_TOOLS_SPACE,
    )


async def _handle_edit_workspace_file(
    user_id: int,
    conversation_id: str,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    project_id: str | None = None,
) -> str:
    """Perform an exact string replacement in a conversation-workspace text file.

    Replaces ``old_string`` with ``new_string`` in an existing file.
    ``old_string`` must be found in the file and must be unique unless
    ``replace_all`` is true. The model must have read the file
    (get_workspace_file) or written it (write_workspace_file, or a
    previous successful edit) earlier in this conversation -- tracked via
    the bare-key entries of the per-conversation workspace_reads.json
    sidecar -- otherwise an error instructs it to read the file first.

    Args:
        user_id: User's integer ID (for file_list_changed publishing).
        conversation_id: Conversation UUID.
        path: Relative file path within workspace (e.g., 'report.md').
        old_string: Exact text to replace.
        new_string: Replacement text (must differ from old_string).
        replace_all: Replace every occurrence instead of requiring a
            unique match.
        project_id: Carried on the ``file_list_changed`` event only; does
            not affect the root (project files: ``_handle_edit_file``).

    Returns:
        JSON string with the result (success with file metadata and the
        replacement count, or error).
    """
    root = await conversation_workspace_dir(conversation_id)
    return await _edit_file(
        user_id, conversation_id, project_id, root, path,
        old_string, new_string, replace_all, _WORKSPACE_TOOLS_SPACE,
    )


# ---------------------------------------------------------------------------
# Space-aware tools (chat:// and proj:// paths)
# ---------------------------------------------------------------------------


def _path_error(exc: SpacePathError) -> str:
    return json.dumps({"error": exc.message, "code": exc.code})


async def resolve_space_root(
    space: str, conversation_id: str, project_id: str | None,
) -> tuple[Path | None, str | None]:
    """Root directory of ``space`` for this conversation (created if
    missing), or ``(None, error_json)``: ``no_project`` for ``proj://``
    outside a project, ``invalid_project`` for an unresolvable project id."""
    if space == SPACE_CHAT:
        return await conversation_workspace_dir(conversation_id), None
    if not project_id:
        return None, _no_project_result()
    try:
        return await project_workspace_dir(project_id), None
    except ValueError:  # InvalidStorageIdError
        return None, _invalid_project_result()


async def _open_space(
    raw_path, conversation_id: str, project_id: str | None, *, allow_root: bool,
) -> tuple[_FileSpace | None, str, Path | None, str | None]:
    """Parse ``raw_path`` and resolve its root:
    ``(space, rel, root, None)`` or ``(None, "", None, error_json)``."""
    try:
        space_name, rel = parse_space_path(raw_path, allow_root=allow_root)
    except SpacePathError as exc:
        return None, "", None, _path_error(exc)
    root, err = await resolve_space_root(space_name, conversation_id, project_id)
    if err is not None:
        return None, "", None, err
    return _SPACES[space_name], rel, root, None


async def _handle_list_files(
    user_id: int, conversation_id: str, project_id: str | None, path,
) -> str:
    """List the files below a space root or one of its folders
    (``chat://``, ``proj://``, ``proj://reports``).

    Same output shape as ``list_workspace_files``: paths are relative to
    the SPACE root (prefix them with the scheme to address them), so
    ``chat://`` alone matches ``list_workspace_files`` exactly.
    """
    space, rel, root, err = await _open_space(
        path, conversation_id, project_id, allow_root=True,
    )
    if err is not None:
        return err
    if not rel:
        return await _list_files(root)
    _clean, start, err = _resolve_in_root(root, rel, space, allow_empty=False)
    if err is not None:
        return err
    shown = space.show(rel)
    if not await asyncio.to_thread(start.exists):
        return json.dumps({"error": f"Folder not found: {shown}", "code": "not_found"})
    if not await asyncio.to_thread(start.is_dir):
        return json.dumps({
            "error": f"Not a folder: {shown} (use read_file to read a file)",
            "code": "not_a_folder",
        })
    return await _list_files(root, start)


async def _handle_read_file(
    provider,
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    path,
    model: str = "",
) -> tuple[str, list]:
    """Read a ``chat://`` or ``proj://`` file: every behaviour of
    ``_handle_get_workspace_file`` (inline text, image/PDF parts,
    attach-cap pre-flight, unsupported-type hint). A ``chat://`` read also
    licenses edit_workspace_file of the same file, and vice versa."""
    space, rel, root, err = await _open_space(
        path, conversation_id, project_id, allow_root=False,
    )
    if err is not None:
        return err, []
    return await _read_file(
        provider, user_id, conversation_id, root, rel, space, model=model,
    )


async def _handle_write_file(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    path,
    content: str,
) -> str:
    """Create or overwrite a ``chat://`` or ``proj://`` text file (same rules
    as ``write_workspace_file``); publishes ``file_list_changed`` for the
    file's space."""
    space, rel, root, err = await _open_space(
        path, conversation_id, project_id, allow_root=False,
    )
    if err is not None:
        return err
    if not isinstance(content, str):
        return json.dumps({"error": "content must be a string", "code": "invalid_content"})
    return await _write_file(
        user_id, conversation_id, project_id, root, rel, content, space,
    )


async def _handle_edit_file(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    path,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Exact string replacement in a ``chat://`` or ``proj://`` text file
    (same rules as ``edit_workspace_file``), gated on the read sidecar:
    the file must have been read or written earlier in THIS conversation
    (for a conversation file, through either tool family)."""
    space, rel, root, err = await _open_space(
        path, conversation_id, project_id, allow_root=False,
    )
    if err is not None:
        return err
    return await _edit_file(
        user_id, conversation_id, project_id, root, rel,
        old_string, new_string, replace_all, space,
    )
