"""HTTP plumbing shared by every Quest Task route.

The one place a task refusal becomes an HTTP response: every
``db.task_store.TaskStoreError`` subclass (store, service, questions)
carries a stable ``code``, and :func:`task_http_exception` maps that code to
a status and the error body ``{"error": code, "message": ..., **details}``
(details such as ``active_run``, ``current``, ``cap`` / ``actual``).

:func:`require_task_for_route` is the guard every ``/tasks/{id}...`` route
(chat/tasks/routes.py and the task file routes in chat/file_routes.py)
calls first: 403 ``tasks_disabled`` while the gate is closed for the user
(spec 9), then 404 ``task_not_found`` for a missing task and for one owned
by someone else, with the same body (invariant 7).

Imports nothing from ``chat.realtime``, so route modules may import it at
module top.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from db.task_store import TaskStoreError, TaskValidationError

# Codes that are not 400. Anything else (every TaskValidationError code,
# model_not_allowed) is a 400.
_STATUS_BY_CODE: dict[str, int] = {
    "tasks_disabled": 403,
    "task_not_found": 404,
    "task_run_not_found": 404,
    "question_not_found": 404,
    "brief_version_not_found": 404,
    "tasks_cap_reached": 409,
    "task_archived": 409,
    "task_run_active": 409,
    "task_run_finished": 409,
    "task_run_single_message": 409,
    "stale_brief": 409,
    "question_resolved": 409,
}


def task_error_status(exc: TaskStoreError) -> int:
    """The HTTP status of a task refusal (400 unless listed above)."""
    if isinstance(exc, TaskValidationError):
        return 400
    return _STATUS_BY_CODE.get(exc.code, 400)


def task_error_body(exc: TaskStoreError) -> dict:
    """``{"error": code, "message": message, **details}``."""
    body = {"error": exc.code, "message": exc.message}
    for key, value in exc.details.items():
        body.setdefault(key, value)
    return body


def task_http_exception(exc: TaskStoreError) -> HTTPException:
    """The ``HTTPException`` for a task refusal; ``raise ... from exc``."""
    return HTTPException(status_code=task_error_status(exc), detail=task_error_body(exc))


async def require_task_for_route(
    user: dict, task_id: str, *, allow_archived: bool = True,
) -> dict:
    """Gate, then ownership: the task dict, or 403 ``tasks_disabled`` / 404
    ``task_not_found`` (same body for missing and foreign) / 409
    ``task_archived`` when ``allow_archived`` is False."""
    from chat.conversation_access import require_owned_task
    from chat.tasks.service import TasksDisabledError, require_tasks_enabled

    try:
        require_tasks_enabled(user)
    except TasksDisabledError as exc:
        raise task_http_exception(exc) from exc
    return await require_owned_task(user, task_id, allow_archived=allow_archived)


async def resolve_task_workspace_for_route(
    user: dict, task_id: str,
) -> tuple[dict, Path]:
    """:func:`require_task_for_route` plus the task workspace root, created
    if missing (a task that never wrote a file lists as empty). Archived
    tasks are allowed: the thread and its files stay readable."""
    import asyncio

    from chat.storage import ChatStorage

    task = await require_task_for_route(user, task_id)
    root = ChatStorage.get_task_workspace_root(task_id)
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    return task, root
