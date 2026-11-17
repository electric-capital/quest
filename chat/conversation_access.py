"""Ownership-aware accessors for request-serving code.

``ChatStorage.get_conversation_dir`` / ``get_project_dir`` make an id safe to
turn into a path but deliberately know nothing about users -- schedulers,
migrations and cross-user subagents resolve paths with no "current user".
HTTP routes must ALSO prove the caller owns the row, and historically that
was left to whoever remembered (the file-upload route checked, the Gmail
URL-lookup routes did not -- security finding #279217). These helpers fuse
the two steps for routes so ownership + path safety travel together:

* ``require_owned_conversation`` / ``require_owned_project`` -- DB ownership
  lookup, 404 ``conversation_not_found`` / ``not_found`` otherwise. The 404
  shape matches what the routes already returned, so clients see no change.
* ``require_owned_autoproject`` -- the same for an AutoProject: 404
  ``autoproject_not_found`` for a missing AutoProject and for one owned by someone else,
  with the same body (a foreign AutoProject behaves exactly like a missing one).
* ``resolve_owned_workspace`` / ``resolve_owned_project_workspace`` /
  ``resolve_owned_autoproject_workspace`` -- the same lookup plus the validated
  workspace root (the conversation workspace for any conversation,
  standalone or in a project; the shared project workspace for a project;
  the shared AutoProject workspace for an AutoProject), in one call.

A non-canonical id never reaches the filesystem: the DB lookup misses first
(404), and the resolver would reject it anyway.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from chat.storage import ChatStorage


def _conversation_not_found(conversation_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "conversation_not_found",
            "message": f"Conversation {conversation_id} not found",
        },
    )


def _project_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": "not_found", "message": "Project not found"},
    )


async def require_owned_conversation(user_id: int, conversation_id: str) -> dict:
    """Return the conversation row owned by ``user_id`` or raise 404.

    Import of the store is deferred so tests can monkeypatch
    ``db.conversation_store.get_conversation_meta`` the way route tests
    already do.
    """
    from db.conversation_store import get_conversation_meta

    meta = await get_conversation_meta(user_id, conversation_id)
    if not meta:
        raise _conversation_not_found(conversation_id)
    return meta


async def resolve_owned_workspace(
    user_id: int, conversation_id: str,
) -> tuple[dict, Path]:
    """Return ``(meta, root)`` for a conversation the user owns.

    ``root`` is ``ChatStorage.get_conversation_workspace_root`` -- the
    conversation's own workspace for every conversation, including project
    conversations (the shared project workspace is reached through
    ``resolve_owned_project_workspace``). Raises 404 when the conversation
    does not exist or belongs to someone else.
    """
    meta = await require_owned_conversation(user_id, conversation_id)
    return meta, ChatStorage.get_conversation_workspace_root(conversation_id)


async def require_owned_project(user_id: int, project_id: str) -> dict:
    """Return the project row owned by ``user_id`` or raise 404."""
    from db.project_store import get_project

    project = await get_project(user_id, project_id)
    if not project:
        raise _project_not_found()
    return project


async def resolve_owned_project_workspace(
    user_id: int, project_id: str,
) -> tuple[dict, Path]:
    """Return ``(project, root)`` for a project the user owns, else 404.

    ``root`` is ``ChatStorage.get_project_workspace_root``, the workspace
    shared by every conversation of the project.
    """
    project = await require_owned_project(user_id, project_id)
    return project, ChatStorage.get_project_workspace_root(project_id)


def _autoproject_not_found(autoproject_id: str) -> HTTPException:
    from chat.autoprojects.constants import autoproject_not_found_message

    return HTTPException(
        status_code=404,
        detail={"error": "autoproject_not_found", "message": autoproject_not_found_message(autoproject_id)},
    )


def _autoproject_archived() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": "autoproject_archived",
            "message": "This AutoProject is archived. Unarchive it to start a run.",
        },
    )


async def require_owned_autoproject(
    user: dict, autoproject_id: str, *, allow_archived: bool = True,
) -> dict:
    """Return the AutoProject row owned by ``user`` or raise.

    404 ``autoproject_not_found`` for a missing AutoProject and for one owned by someone
    else, with the same body. With ``allow_archived=False`` an archived
    AutoProject is refused with 409 ``autoproject_archived`` (only after ownership is
    proven, so a stranger still sees the 404). Ownership only: the
    ``autoprojects`` feature gate is the caller's check. Import of the store is
    deferred so tests can monkeypatch ``db.autoproject_store.get_autoproject``.
    """
    from db.autoproject_store import get_autoproject

    autoproject = await get_autoproject(user["id"], autoproject_id)
    if not autoproject:
        raise _autoproject_not_found(autoproject_id)
    if not allow_archived and autoproject.get("archived"):
        raise _autoproject_archived()
    return autoproject


async def resolve_owned_autoproject_workspace(
    user: dict, autoproject_id: str, *, allow_archived: bool = True,
) -> tuple[dict, Path]:
    """Return ``(AutoProject, root)`` for an AutoProject the user owns, else 404.

    ``root`` is ``ChatStorage.get_autoproject_workspace_root``, the workspace
    shared by every run of the AutoProject. The directory is not created here.
    """
    autoproject = await require_owned_autoproject(user, autoproject_id, allow_archived=allow_archived)
    return autoproject, ChatStorage.get_autoproject_workspace_root(autoproject_id)
