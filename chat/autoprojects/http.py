"""HTTP plumbing shared by every AutoProject route.

The one place an AutoProject refusal becomes an HTTP response: every
``db.autoproject_store.AutoProjectStoreError`` subclass (store, service, questions)
carries a stable ``code``, and :func:`autoproject_http_exception` maps that code to
a status and the error body ``{"error": code, "message": ..., **details}``
(details such as ``active_run``, ``current``, ``cap`` / ``actual``).

:func:`require_autoproject_for_route` is the guard every ``/autoprojects/{id}...`` route
(chat/autoprojects/routes.py and the AutoProject file routes in chat/file_routes.py)
calls first: 403 ``autoprojects_disabled`` while the gate is closed for the user
(spec 9), then 404 ``autoproject_not_found`` for a missing AutoProject and for one owned
by someone else, with the same body (invariant 7).

Imports nothing from ``chat.realtime``, so route modules may import it at
module top.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from db.autoproject_store import AutoProjectStoreError, AutoProjectValidationError

# Codes that are not 400. Anything else (every AutoProjectValidationError code,
# model_not_allowed) is a 400.
_STATUS_BY_CODE: dict[str, int] = {
    "autoprojects_disabled": 403,
    "autoproject_not_found": 404,
    "autoproject_run_not_found": 404,
    "question_not_found": 404,
    "brief_version_not_found": 404,
    "autoprojects_cap_reached": 409,
    "autoproject_archived": 409,
    "autoproject_run_active": 409,
    "autoproject_run_finished": 409,
    "autoproject_run_single_message": 409,
    "stale_brief": 409,
    "question_resolved": 409,
}


def autoproject_error_status(exc: AutoProjectStoreError) -> int:
    """The HTTP status of an AutoProject refusal (400 unless listed above)."""
    if isinstance(exc, AutoProjectValidationError):
        return 400
    return _STATUS_BY_CODE.get(exc.code, 400)


def autoproject_error_body(exc: AutoProjectStoreError) -> dict:
    """``{"error": code, "message": message, **details}``."""
    body = {"error": exc.code, "message": exc.message}
    for key, value in exc.details.items():
        body.setdefault(key, value)
    return body


def autoproject_http_exception(exc: AutoProjectStoreError) -> HTTPException:
    """The ``HTTPException`` for an AutoProject refusal; ``raise ... from exc``."""
    return HTTPException(
        status_code=autoproject_error_status(exc), detail=autoproject_error_body(exc),
    )


async def require_autoproject_for_route(
    user: dict, autoproject_id: str, *, allow_archived: bool = True,
) -> dict:
    """Gate, then ownership: the AutoProject dict, or 403 ``autoprojects_disabled`` / 404
    ``autoproject_not_found`` (same body for missing and foreign) / 409
    ``autoproject_archived`` when ``allow_archived`` is False."""
    from chat.conversation_access import require_owned_autoproject
    from chat.autoprojects.service import AutoProjectsDisabledError, require_autoprojects_enabled

    try:
        require_autoprojects_enabled(user)
    except AutoProjectsDisabledError as exc:
        raise autoproject_http_exception(exc) from exc
    return await require_owned_autoproject(user, autoproject_id, allow_archived=allow_archived)


async def resolve_autoproject_workspace_for_route(
    user: dict, autoproject_id: str,
) -> tuple[dict, Path]:
    """:func:`require_autoproject_for_route` plus the AutoProject workspace root, created
    if missing (an AutoProject that never wrote a file lists as empty). Archived
    AutoProjects are allowed: the thread and its files stay readable."""
    import asyncio

    from chat.storage import ChatStorage

    autoproject = await require_autoproject_for_route(user, autoproject_id)
    root = ChatStorage.get_autoproject_workspace_root(autoproject_id)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    return autoproject, root
