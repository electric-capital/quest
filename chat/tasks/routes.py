"""HTTP routes for Quest Tasks (spec issue #213, section 7).

Everything is under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked`` (which enforces
``require_user_allowed``). Every route starts with the gate: 403
``tasks_disabled`` while the ``tasks`` feature gate is closed for the user,
then, for ``/tasks/{id}...``, 404 ``task_not_found`` with the same body for
a missing task and for one owned by someone else
(``chat.tasks.http.require_task_for_route``). FastAPI's 422 for an
ill-typed body or query parameter comes before the handler runs.

Handlers stay thin: the rules are in chat/tasks/service.py, the response
shapes in chat/tasks/thread.py. Every refusal is a ``TaskStoreError``
mapped through ``task_http_exception`` to ``{"detail": {"error": code,
"message", **details}}``.

Routes
------

- ``GET /tasks?archived=false|true`` -> ``{"tasks": [list row]}``, newest
  activity first.
- ``POST /tasks`` ``{title?, prompt?, instructions?, model?,
  thinking_effort?}`` -> 201 task detail. 400 ``invalid_title`` /
  ``invalid_instructions`` / ``invalid_model`` / ``invalid_thinking_effort``
  / ``model_not_allowed``, 409 ``tasks_cap_reached``.
- ``GET /tasks/{id}`` -> task detail.
- ``PATCH /tasks/{id}`` ``{title?, instructions?, model?,
  thinking_effort?}`` (absent stays; null clears model / thinking_effort;
  null title is 400 ``invalid_title``) -> task detail.
- ``PUT /tasks/{id}/brief`` ``{brief, expected_brief_version}`` -> brief
  state. 409 ``stale_brief`` with ``current``; 400 ``brief_too_long`` /
  ``invalid_brief``.
- ``GET /tasks/{id}/brief/versions`` -> ``{"versions": [...]}``.
- ``GET /tasks/{id}/brief/versions/{version}`` -> one version with its
  ``content``; 404 ``brief_version_not_found``.
- ``POST /tasks/{id}/brief/versions/{version}/restore``
  ``{expected_brief_version}`` -> brief state.
- ``GET /tasks/{id}/runs?before_seq=&limit=`` -> ``{"runs": [run view],
  "next_before_seq"}``.
- ``POST /tasks/{id}/runs`` -> 201 ``{"conversation_id"}``; 409
  ``task_run_active`` / ``task_archived``.
- ``DELETE /tasks/{id}/runs/{run_id}`` -> ``{"success": true}``; 404
  ``task_run_not_found``, 409 ``task_run_active``.
- ``POST /tasks/{id}/questions/{handle_id}/answer`` ``{answer}`` -> the
  question view; 404 ``question_not_found``, 409 ``question_resolved``, 400
  ``invalid_answer``.
- ``POST /tasks/{id}/read`` ``{seq}`` -> ``{event_seq, unread_seq,
  read_seq, unread}``.
- ``PUT /tasks/{id}/archive`` / ``unarchive`` -> task detail; 409
  ``task_run_active`` / ``tasks_cap_reached``.
- ``DELETE /tasks/{id}`` -> ``{"success": true}``.

The task workspace file routes live in chat/file_routes.py.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, StrictInt

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.tasks import service, thread
from chat.tasks.http import require_task_for_route, task_http_exception
from db import task_store
from db.task_store import TaskStoreError

router = APIRouter(prefix="/app/api", tags=["tasks"])


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    title: Optional[str] = None
    # The New Task composer's first message: names the task only.
    prompt: Optional[str] = None
    instructions: Optional[str] = None
    model: Optional[str] = None
    thinking_effort: Optional[str] = None


class UpdateTaskRequest(BaseModel):
    # Only the fields present in the body are applied (model_fields_set).
    title: Optional[str] = None
    instructions: Optional[str] = None
    model: Optional[str] = None
    thinking_effort: Optional[str] = None


class SetBriefRequest(BaseModel):
    brief: str
    expected_brief_version: StrictInt = Field(ge=0)


class RestoreBriefRequest(BaseModel):
    expected_brief_version: StrictInt = Field(ge=0)


class AnswerQuestionRequest(BaseModel):
    answer: Any = None


class MarkTaskReadRequest(BaseModel):
    # The task's event_seq the client is showing.
    seq: int = Field(ge=0)


def _require_enabled(user: dict) -> None:
    try:
        service.require_tasks_enabled(user)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_tasks_endpoint(
    archived: bool = Query(False),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The caller's tasks, newest activity first (archived ones with
    ``?archived=true``)."""
    _require_enabled(user)
    rows = await task_store.list_tasks(user["id"], archived=archived)
    return {"tasks": [thread.task_list_row(row) for row in rows]}


@router.post("/tasks", status_code=201)
async def create_task_endpoint(
    body: CreateTaskRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create a task; returns its detail."""
    _require_enabled(user)
    try:
        task = await service.create_task(
            user,
            title=body.title,
            prompt=body.prompt,
            instructions=body.instructions,
            model=body.model,
            thinking_effort=body.thinking_effort,
        )
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return await thread.build_task_detail(task)


@router.get("/tasks/{task_id}")
async def get_task_endpoint(
    task_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The task detail."""
    task = await require_task_for_route(user, task_id)
    return await thread.build_task_detail(task)


@router.patch("/tasks/{task_id}")
async def update_task_endpoint(
    task_id: str,
    body: UpdateTaskRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Edit title, instructions, default model or thinking level."""
    await require_task_for_route(user, task_id)
    fields = {
        name: getattr(body, name)
        for name in ("title", "instructions", "model", "thinking_effort")
        if name in body.model_fields_set
    }
    try:
        task = await service.update_task(user, task_id, **fields)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return await thread.build_task_detail(task)


@router.put("/tasks/{task_id}/archive")
async def archive_task_endpoint(
    task_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Archive the task (refused while a run is active); marks it read."""
    await require_task_for_route(user, task_id)
    try:
        task = await service.set_task_archived(user, task_id, True)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return await thread.build_task_detail(task)


@router.put("/tasks/{task_id}/unarchive")
async def unarchive_task_endpoint(
    task_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Unarchive the task (counts against the task cap)."""
    await require_task_for_route(user, task_id)
    try:
        task = await service.set_task_archived(user, task_id, False)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return await thread.build_task_detail(task)


@router.delete("/tasks/{task_id}")
async def delete_task_endpoint(
    task_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete the task, its runs and their conversations, and its files
    (stops an active run first)."""
    await require_task_for_route(user, task_id)
    try:
        await service.delete_task(user, task_id)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return {"success": True}


@router.post("/tasks/{task_id}/read")
async def mark_task_read_endpoint(
    task_id: str,
    body: MarkTaskReadRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Acknowledge the thread up to ``seq`` (focused client only).
    ``read_seq`` only moves forward and is clamped to ``event_seq``."""
    await require_task_for_route(user, task_id)
    try:
        task = await service.mark_task_read(user, task_id, seq=body.seq)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return thread.read_state(task)


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


@router.put("/tasks/{task_id}/brief")
async def set_brief_endpoint(
    task_id: str,
    body: SetBriefRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Save the user's brief edit (allowed while a run is active)."""
    await require_task_for_route(user, task_id)
    try:
        state = await service.set_brief(
            user, task_id, body.brief, expected_version=body.expected_brief_version,
        )
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return await thread.build_brief_state(task_id, state)


@router.get("/tasks/{task_id}/brief/versions")
async def list_brief_versions_endpoint(
    task_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The stored brief versions, newest first."""
    await require_task_for_route(user, task_id)
    return {"versions": await thread.build_brief_versions(task_id)}


@router.get("/tasks/{task_id}/brief/versions/{version}")
async def get_brief_version_endpoint(
    task_id: str,
    version: int,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One stored brief version with its full text."""
    await require_task_for_route(user, task_id)
    try:
        return await thread.build_brief_version(task_id, version)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc


@router.post("/tasks/{task_id}/brief/versions/{version}/restore")
async def restore_brief_version_endpoint(
    task_id: str,
    version: int,
    body: RestoreBriefRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Write a stored version's text as a new brief version."""
    await require_task_for_route(user, task_id)
    try:
        state = await service.restore_brief(
            user, task_id, version, expected_version=body.expected_brief_version,
        )
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return await thread.build_brief_state(task_id, state)


# ---------------------------------------------------------------------------
# Runs and questions
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}/runs")
async def list_runs_endpoint(
    task_id: str,
    before_seq: Optional[int] = Query(None, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One page of the thread, newest first."""
    await require_task_for_route(user, task_id)
    return await thread.build_runs_page(task_id, before_seq=before_seq, limit=limit)


@router.post("/tasks/{task_id}/runs", status_code=201)
async def create_run_endpoint(
    task_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create the next run's conversation; the run starts with the first
    ``send_message`` on it."""
    await require_task_for_route(user, task_id)
    try:
        conversation_id = await service.create_run_conversation(user, task_id)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return {"conversation_id": conversation_id}


@router.delete("/tasks/{task_id}/runs/{run_id}")
async def delete_run_endpoint(
    task_id: str,
    run_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a finished run's log entry and its conversation."""
    await require_task_for_route(user, task_id)
    try:
        await service.delete_run(user, task_id, run_id)
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
    return {"success": True}


@router.post("/tasks/{task_id}/questions/{handle_id}/answer")
async def answer_question_endpoint(
    task_id: str,
    handle_id: str,
    body: AnswerQuestionRequest,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Answer a run's open question and continue the run."""
    await require_task_for_route(user, task_id)
    try:
        return await service.answer_question(
            user, task_id, handle_id, body.answer, app=request.app,
        )
    except TaskStoreError as exc:
        raise task_http_exception(exc) from exc
