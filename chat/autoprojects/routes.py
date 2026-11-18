"""HTTP routes for AutoProjects.

Everything is under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked`` (which enforces
``require_user_allowed``). Every route starts with the gate: 403
``autoprojects_disabled`` while the ``autoprojects`` feature gate is closed for
the user, then, for ``/autoprojects/{id}...``, 404 ``autoproject_not_found``
with the same body for a missing AutoProject and for one owned by someone else
(``chat.autoprojects.http.require_autoproject_for_route``). FastAPI's 422 for an
ill-typed body or query parameter comes before the handler runs.

Handlers stay thin: the rules are in chat/autoprojects/service.py, the response
shapes in chat/autoprojects/views.py. Every refusal is an ``AutoProjectStoreError``
mapped through ``autoproject_http_exception`` to ``{"detail": {"error": code,
"message", **details}}``.

Routes
------

- ``GET /autoprojects?archived=false|true`` -> ``{"autoprojects": [list row]}``, newest
  activity first.
- ``POST /autoprojects`` ``{title?, prompt?, model?, thinking_effort?}`` -> 201
  AutoProject detail. 400 ``invalid_title`` / ``invalid_model`` /
  ``invalid_thinking_effort`` / ``model_not_allowed``, 409
  ``autoprojects_cap_reached``.
- ``GET /autoprojects/{id}`` -> AutoProject detail.
- ``PATCH /autoprojects/{id}`` ``{title?, model?, thinking_effort?}`` (absent
  stays; null clears model / thinking_effort; null title is 400
  ``invalid_title``) -> AutoProject detail.
- ``PUT /autoprojects/{id}/brief`` ``{brief, expected_brief_version}`` -> brief
  state. 409 ``stale_brief`` with ``current``; 400 ``brief_too_long`` /
  ``invalid_brief``.
- ``GET /autoprojects/{id}/brief/versions`` -> ``{"versions": [...]}``.
- ``GET /autoprojects/{id}/brief/versions/{version}`` -> one version with its
  ``content``; 404 ``brief_version_not_found``.
- ``POST /autoprojects/{id}/brief/versions/{version}/restore``
  ``{expected_brief_version}`` -> brief state.
- ``GET /autoprojects/{id}/runs?before_seq=&limit=`` -> ``{"runs": [run view],
  "next_before_seq"}``.
- ``POST /autoprojects/{id}/runs`` -> 201 ``{"conversation_id"}``; 409
  ``autoproject_run_active`` / ``autoproject_archived``.
- ``DELETE /autoprojects/{id}/runs/{run_id}`` -> ``{"success": true}``; 404
  ``autoproject_run_not_found``, 409 ``autoproject_run_active``.
- ``POST /autoprojects/{id}/questions/{handle_id}/answer`` ``{answer}`` -> the
  question view; 404 ``question_not_found``, 409 ``question_resolved``, 400
  ``invalid_answer``.
- ``POST /autoprojects/{id}/read`` ``{seq}`` -> ``{event_seq, unread_seq,
  read_seq, unread}``.
- ``PUT /autoprojects/{id}/archive`` / ``unarchive`` -> AutoProject detail; 409
  ``autoproject_run_active`` / ``autoprojects_cap_reached``.
- ``DELETE /autoprojects/{id}`` -> ``{"success": true}``.

The AutoProject workspace file routes live in chat/file_routes.py.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field, StrictInt

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.autoprojects import service, views
from chat.autoprojects.http import require_autoproject_for_route, autoproject_http_exception
from db import autoproject_store
from db.autoproject_store import AutoProjectStoreError

router = APIRouter(prefix="/app/api", tags=["autoprojects"])

# Upper bound of every integer a route accepts (seqs and versions are small
# counters): a larger value is a 422 instead of an overflow deeper down.
_MAX_INT = 2**31 - 1


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class CreateAutoProjectRequest(BaseModel):
    title: Optional[str] = None
    # The New AutoProject composer's first message: names the AutoProject only.
    prompt: Optional[str] = None
    model: Optional[str] = None
    thinking_effort: Optional[str] = None


class UpdateAutoProjectRequest(BaseModel):
    # Only the fields present in the body are applied (model_fields_set).
    title: Optional[str] = None
    model: Optional[str] = None
    thinking_effort: Optional[str] = None


class SetBriefRequest(BaseModel):
    brief: str
    expected_brief_version: StrictInt = Field(ge=0, le=_MAX_INT)


class RestoreBriefRequest(BaseModel):
    expected_brief_version: StrictInt = Field(ge=0, le=_MAX_INT)


class AnswerQuestionRequest(BaseModel):
    answer: Any = None


class MarkAutoProjectReadRequest(BaseModel):
    # The AutoProject's event_seq the client is showing.
    seq: int = Field(ge=0, le=_MAX_INT)


def _require_enabled(user: dict) -> None:
    try:
        service.require_autoprojects_enabled(user)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc


# ---------------------------------------------------------------------------
# AutoProjects
# ---------------------------------------------------------------------------


@router.get("/autoprojects")
async def list_autoprojects_endpoint(
    archived: bool = Query(False),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The caller's AutoProjects, newest activity first (archived ones with
    ``?archived=true``)."""
    _require_enabled(user)
    rows = await autoproject_store.list_autoprojects(user["id"], archived=archived)
    return {"autoprojects": [views.autoproject_list_row(row) for row in rows]}


@router.post("/autoprojects", status_code=201)
async def create_autoproject_endpoint(
    body: CreateAutoProjectRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create an AutoProject; returns its detail."""
    _require_enabled(user)
    try:
        autoproject = await service.create_autoproject(
            user,
            title=body.title,
            prompt=body.prompt,
            model=body.model,
            thinking_effort=body.thinking_effort,
        )
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return await views.build_autoproject_detail(autoproject)


@router.get("/autoprojects/{autoproject_id}")
async def get_autoproject_endpoint(
    autoproject_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The AutoProject detail."""
    autoproject = await require_autoproject_for_route(user, autoproject_id)
    return await views.build_autoproject_detail(autoproject)


@router.patch("/autoprojects/{autoproject_id}")
async def update_autoproject_endpoint(
    autoproject_id: str,
    body: UpdateAutoProjectRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Edit title, default model or thinking level."""
    await require_autoproject_for_route(user, autoproject_id)
    fields = {
        name: getattr(body, name)
        for name in ("title", "model", "thinking_effort")
        if name in body.model_fields_set
    }
    try:
        autoproject = await service.update_autoproject(user, autoproject_id, **fields)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return await views.build_autoproject_detail(autoproject)


@router.put("/autoprojects/{autoproject_id}/archive")
async def archive_autoproject_endpoint(
    autoproject_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Archive the AutoProject (refused while a run is active); marks it read."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        autoproject = await service.set_autoproject_archived(user, autoproject_id, True)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return await views.build_autoproject_detail(autoproject)


@router.put("/autoprojects/{autoproject_id}/unarchive")
async def unarchive_autoproject_endpoint(
    autoproject_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Unarchive the AutoProject (counts against the AutoProject cap)."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        autoproject = await service.set_autoproject_archived(user, autoproject_id, False)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return await views.build_autoproject_detail(autoproject)


@router.delete("/autoprojects/{autoproject_id}")
async def delete_autoproject_endpoint(
    autoproject_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete the AutoProject, its runs and their conversations, and its files
    (stops an active run first)."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        await service.delete_autoproject(user, autoproject_id)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return {"success": True}


@router.post("/autoprojects/{autoproject_id}/read")
async def mark_autoproject_read_endpoint(
    autoproject_id: str,
    body: MarkAutoProjectReadRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Mark the AutoProject read up to ``seq`` (focused client only).
    ``read_seq`` only moves forward and is clamped to ``event_seq``."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        autoproject = await service.mark_autoproject_read(user, autoproject_id, seq=body.seq)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return views.read_state(autoproject)


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


@router.put("/autoprojects/{autoproject_id}/brief")
async def set_brief_endpoint(
    autoproject_id: str,
    body: SetBriefRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Save the user's brief edit (allowed while a run is active)."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        state = await service.set_brief(
            user, autoproject_id, body.brief,
            expected_brief_version=body.expected_brief_version,
        )
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return await views.build_brief_state(autoproject_id, state)


@router.get("/autoprojects/{autoproject_id}/brief/versions")
async def list_brief_versions_endpoint(
    autoproject_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The stored brief versions, newest first."""
    await require_autoproject_for_route(user, autoproject_id)
    return {"versions": await views.build_brief_versions(autoproject_id)}


@router.get("/autoprojects/{autoproject_id}/brief/versions/{version}")
async def get_brief_version_endpoint(
    autoproject_id: str,
    version: Annotated[int, Path(le=_MAX_INT)],
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One stored brief version with its full text."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        return await views.build_brief_version(autoproject_id, version)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc


@router.post("/autoprojects/{autoproject_id}/brief/versions/{version}/restore")
async def restore_brief_version_endpoint(
    autoproject_id: str,
    version: Annotated[int, Path(le=_MAX_INT)],
    body: RestoreBriefRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Write a stored version's text as a new brief version."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        state = await service.restore_brief(
            user, autoproject_id, version,
            expected_brief_version=body.expected_brief_version,
        )
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return await views.build_brief_state(autoproject_id, state)


# ---------------------------------------------------------------------------
# Runs and questions
# ---------------------------------------------------------------------------


@router.get("/autoprojects/{autoproject_id}/runs")
async def list_runs_endpoint(
    autoproject_id: str,
    before_seq: Optional[int] = Query(None, ge=1, le=_MAX_INT),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """One page of the AutoProject's runs, newest first."""
    await require_autoproject_for_route(user, autoproject_id)
    return await views.build_runs_page(autoproject_id, before_seq=before_seq, limit=limit)


@router.post("/autoprojects/{autoproject_id}/runs", status_code=201)
async def create_run_endpoint(
    autoproject_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create the next run's conversation; the run starts with the first
    ``send_message`` on it."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        conversation_id = await service.create_run_conversation(user, autoproject_id)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return {"conversation_id": conversation_id}


@router.delete("/autoprojects/{autoproject_id}/runs/{run_id}")
async def delete_run_endpoint(
    autoproject_id: str,
    run_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a finished run's log entry and its conversation."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        await service.delete_run(user, autoproject_id, run_id)
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
    return {"success": True}


@router.post("/autoprojects/{autoproject_id}/questions/{handle_id}/answer")
async def answer_question_endpoint(
    autoproject_id: str,
    handle_id: str,
    body: AnswerQuestionRequest,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Answer a run's open question and continue the run."""
    await require_autoproject_for_route(user, autoproject_id)
    try:
        return await service.answer_question(
            user, autoproject_id, handle_id, body.answer, app=request.app,
        )
    except AutoProjectStoreError as exc:
        raise autoproject_http_exception(exc) from exc
