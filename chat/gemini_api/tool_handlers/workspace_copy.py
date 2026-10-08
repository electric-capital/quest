"""``copy_file``: copy a file or folder between (or within) file spaces.

``src`` and ``dest`` are both scheme-qualified (``chat://`` / ``proj://``,
see ``file_paths``); every direction is allowed, same-space copies
included (devplan 00009, revised 2026-10-08: scheme-qualified file
tools). The copy runs ``chat.file_storage.copy_entry`` in a worker thread
-- the same engine and error codes as the ``/files/copy-to-project`` /
``/files/copy-from-project`` routes in ``chat/file_routes.py`` -- never
moves, and publishes ``file_list_changed`` for the destination space only
(a copy never changes its source).

Every refusal comes back as JSON ``{"error": <message>, "code": <code>}``
(never raised): ``invalid_path`` (unparseable ``src`` / ``dest``),
``no_project`` (``proj://`` outside a project), ``invalid_project`` (the
project id does not resolve), ``forbidden_source`` (a ``.responses/``,
``.subagent_responses/`` or ``pasted/`` source copied from ``chat://`` into
``proj://``),
``copy_failed`` (I/O error), or one of the ``CopyEntryError`` codes
(``invalid_path``, ``not_found``, ``not_a_regular_file``,
``destination_exists``, ``invalid_destination``).
"""

import asyncio
import json
import logging

from chat.file_storage import CopyEntryError, copy_entry, is_scratch_source
from chat.gemini_api.tool_handlers._common import _publish_file_list_changed
from chat.gemini_api.tool_handlers.file_paths import (
    SPACE_CHAT,
    SPACE_PROJECT,
    SpacePathError,
    format_space_path,
    parse_space_path,
)
from chat.gemini_api.tool_handlers.workspace import resolve_space_root

logger = logging.getLogger(__name__)

_SCOPES = {SPACE_CHAT: "conversation", SPACE_PROJECT: "project"}
_SPACE_LABELS = {SPACE_CHAT: "conversation workspace", SPACE_PROJECT: "project workspace"}


def _error(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code})


async def _handle_copy_file(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    src,
    dest,
    overwrite: bool = False,
    include_hidden: bool = False,
) -> str:
    """Copy ``src`` to ``dest`` (both ``chat://`` / ``proj://`` paths).

    Rules (``copy_entry``): a file or a folder; symlink / special-file
    sources refused, symlinks and special files inside a folder skipped,
    dot-entries inside a folder skipped unless ``include_hidden``; an
    existing destination is ``destination_exists`` unless ``overwrite``
    (then a file replaces a file and a folder merges); a destination equal
    to or inside the source is ``invalid_destination``. Conversation
    scratch roots are refused as a source for a ``chat://`` -> ``proj://``
    copy (checked after the roots resolve, so a standalone conversation
    gets ``no_project`` first).
    """
    try:
        src_space, src_rel = parse_space_path(src, allow_root=False)
    except SpacePathError as exc:
        return _error(exc.code, f"src: {exc.message}")
    try:
        dst_space, dst_rel = parse_space_path(dest, allow_root=False)
    except SpacePathError as exc:
        return _error(exc.code, f"dest: {exc.message}")

    src_root, err = await resolve_space_root(src_space, conversation_id, project_id)
    if err is not None:
        return err
    dst_root, err = await resolve_space_root(dst_space, conversation_id, project_id)
    if err is not None:
        return err

    # Conversation scratch roots are never promoted into the project; a
    # project's own folders of those names copy like any other.
    if (
        src_space == SPACE_CHAT and dst_space == SPACE_PROJECT
        and is_scratch_source(src_rel)
    ):
        return _error(
            "forbidden_source",
            ".responses/, .subagent_responses/ and pasted/ hold conversation "
            "scratch files and cannot be copied to the project workspace",
        )
    dest_scope = _SCOPES[dst_space]

    try:
        result = await asyncio.to_thread(
            copy_entry, src_root, src_rel, dst_root, dst_rel,
            overwrite=bool(overwrite),
            include_hidden=bool(include_hidden),
            move=False,
        )
    except CopyEntryError as e:
        message = e.message
        if e.code == "destination_exists":
            message += " (pass overwrite: true to replace it or merge into it)"
        return _error(e.code, message)
    except OSError as e:
        logger.warning(
            "copy_file failed (conversation=%s, src=%r, dest=%r): %s",
            conversation_id, src, dest, e,
        )
        # Part of a folder merge may have been written.
        _publish_file_list_changed(user_id, dest_scope, conversation_id, project_id)
        return _error("copy_failed", f"The copy failed: {e.strerror or e}")

    _publish_file_list_changed(user_id, dest_scope, conversation_id, project_id)
    shown = format_space_path(dst_space, result.get("path", "").lstrip("/"))
    is_folder = result.get("type") == "folder"
    message = (
        f"Copied {'folder' if is_folder else 'file'} to {shown} in the "
        f"{_SPACE_LABELS[dst_space]}"
    )
    if is_folder:
        message += f" ({result.get('files_copied', 0)} file(s) copied"
        skipped = result.get("skipped", 0)
        if skipped:
            message += (
                f", {skipped} skipped: hidden entries, symlinks or special "
                "files"
            )
        message += ")"
    message += "."
    return json.dumps({**result, "path": shown, "message": message})
