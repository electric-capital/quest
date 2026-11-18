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
* ``resolve_owned_workspace`` / ``resolve_owned_project_workspace`` -- the
  same lookup plus the validated workspace root (the conversation workspace
  for any conversation, standalone or in a project; the shared project
  workspace for a project), in one call.

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
