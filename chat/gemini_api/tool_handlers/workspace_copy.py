"""Copy tools between a project conversation's two file spaces.

``copy_file_to_project`` copies a file or directory from the conversation
workspace into the project workspace; ``copy_project_file`` copies the
other way (devplan 00009 section 6.3). Both run ``chat.file_storage.copy_entry``
in a worker thread -- the same engine and error codes as the
``/files/copy-to-project`` / ``/files/copy-from-project`` routes in
``chat/file_routes.py`` -- never move, and publish ``file_list_changed``
for the destination scope only.

Every refusal comes back as JSON ``{"error": <message>, "code": <code>}``
(never raised): ``not_a_project_conversation``, ``forbidden_source``
(scratch roots as a copy-to-project source), ``invalid_project`` (the
project id does not resolve), ``copy_failed`` (I/O error),
or one of the ``CopyEntryError`` codes (``invalid_path``, ``not_found``,
``not_a_regular_file``, ``destination_exists``, ``invalid_destination``).
"""

import asyncio
import json
import logging

from chat.file_storage import CopyEntryError, copy_entry, is_scratch_source
from chat.gemini_api.tool_handlers._common import (
    _invalid_project_result,
    _not_a_project_conversation_result,
    _publish_file_list_changed,
    conversation_workspace_dir,
    project_workspace_dir,
)

logger = logging.getLogger(__name__)


def _error(code: str, message: str) -> str:
    return json.dumps({"error": message, "code": code})


async def _copy_between_spaces(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    path: str,
    dest: str | None,
    overwrite: bool,
    include_hidden: bool,
    *,
    to_project: bool,
) -> str:
    if not project_id:
        return _not_a_project_conversation_result()
    if not isinstance(path, str) or not path.strip("/ "):
        return _error("invalid_path", "path must be a string naming a file or folder")
    if dest is not None and not isinstance(dest, str):
        return _error("invalid_destination", "dest must be a string path")

    if to_project and is_scratch_source(path):
        return _error(
            "forbidden_source",
            ".responses/, .subagent_responses/ and pasted/ hold conversation "
            "scratch files and cannot be copied to the project",
        )

    conversation_root = await conversation_workspace_dir(conversation_id)
    try:
        project_root = await project_workspace_dir(project_id)
    except ValueError:  # InvalidStorageIdError
        return _invalid_project_result()
    src_root, dst_root = (
        (conversation_root, project_root) if to_project
        else (project_root, conversation_root)
    )
    dest_scope = "project" if to_project else "conversation"
    target = dest if dest is not None and dest.strip("/ ") else path

    try:
        result = await asyncio.to_thread(
            copy_entry, src_root, path, dst_root, target,
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
            "copy %s project failed (conversation=%s, path=%r): %s",
            "to" if to_project else "from", conversation_id, path, e,
        )
        # Part of a directory merge may have been written.
        _publish_file_list_changed(user_id, dest_scope, conversation_id, project_id)
        return _error("copy_failed", f"The copy failed: {e.strerror or e}")

    _publish_file_list_changed(user_id, dest_scope, conversation_id, project_id)
    where = "project workspace" if to_project else "conversation workspace"
    what = "folder" if result.get("type") == "folder" else "file"
    message = f"Copied {what} to {result.get('path')} in the {where}"
    if result.get("type") == "folder":
        message += f" ({result.get('files_copied', 0)} file(s) copied"
        skipped = result.get("skipped", 0)
        if skipped:
            message += (
                f", {skipped} skipped: hidden entries, symlinks or special "
                "files"
            )
        message += ")"
    message += "."
    return json.dumps({**result, "message": message})


async def _handle_copy_file_to_project(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    path: str,
    dest: str | None = None,
    overwrite: bool = False,
    include_hidden: bool = False,
) -> str:
    """Copy ``path`` from the conversation workspace to ``dest`` (default:
    the same relative path) in the project workspace.

    Scratch roots (``.responses/``, ``.subagent_responses/``, ``pasted/``)
    are refused as a source (``forbidden_source``). Publishes
    ``file_list_changed`` with scope ``"project"``.
    """
    return await _copy_between_spaces(
        user_id, conversation_id, project_id, path, dest,
        overwrite, include_hidden, to_project=True,
    )


async def _handle_copy_project_file(
    user_id: int,
    conversation_id: str,
    project_id: str | None,
    path: str,
    dest: str | None = None,
    overwrite: bool = False,
    include_hidden: bool = False,
) -> str:
    """Copy ``path`` from the project workspace to ``dest`` (default: the
    same relative path) in the conversation workspace. Publishes
    ``file_list_changed`` with scope ``"conversation"``.
    """
    return await _copy_between_spaces(
        user_id, conversation_id, project_id, path, dest,
        overwrite, include_hidden, to_project=False,
    )
