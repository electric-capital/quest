# AutoProjects API

## Overview

The caller contract of [AutoProjects](../architecture/autoprojects.md): the HTTP routes under `/app/api/autoprojects`, how a run is started over the persistent WebSocket and why a start is refused, the AutoProject workspace and copy routes, and the realtime events. Models never reach these routes (tool calls are limited to `/api/` paths, [Route Dispatch](../architecture/route-dispatch.md)); the model-facing tools are in [AutoProjects -- Model-Facing Surface](../architecture/autoprojects.md#model-facing-surface).

## Key Files

| File | Role |
|------|------|
| `chat/autoprojects/routes.py` | The router; its module docstring lists every route with its body, response and refusals |
| `chat/autoprojects/http.py` | `_STATUS_BY_CODE` (code to status), the error body, `require_autoproject_for_route()` and `resolve_autoproject_workspace_for_route()` |
| `chat/autoprojects/views.py` | The response shapes: list row, detail, brief state and versions, the runs page |
| `chat/autoprojects/service.py` | The rules each handler delegates to |
| `chat/realtime/socket.py` | `_handle_send_message()` (the run start) and `_handle_stop()` |
| `chat/file_routes.py` | The `/autoprojects/{id}/files...` routes and the `copy-to-autoproject` / `copy-from-autoproject` routes |
| `frontend/src/api/autoprojectsApi.ts` / `frontend/src/services/autoprojectRuns.ts` | The client and the two-step start |

## Conventions

- **Auth and gate.** Every route authenticates with `get_current_user_cookie_or_apikey_checked` and checks the gate first: 403 `autoprojects_disabled` while the `autoprojects` gate is closed for the user, before any DB work. FastAPI's 422 for an ill-typed body or query parameter comes before it; every integer a route takes (a seq, a version, a limit) has an upper bound, so an oversized value is a 422 as well.
- **Ownership.** Every `/autoprojects/{id}...` route resolves the AutoProject with `require_autoproject_for_route()`: 404 `autoproject_not_found` with the same body for a missing AutoProject and for one owned by someone else. A run of another AutoProject is 404 `autoproject_run_not_found`; a question that is missing, of another kind, another user's or another AutoProject's is 404 `question_not_found`, all with one body.
- **Errors.** Every refusal is an `AutoProjectStoreError` with a stable `code`, sent as `{"detail": {"error": code, "message", ...details}}`. `_STATUS_BY_CODE` in `chat/autoprojects/http.py` maps the non-400 codes; every validation code is a 400. The details are what a client needs to recover without another call: `current` (the brief state) on 409 `stale_brief`, `active_run` (`{id, seq, status}`) on 409 `autoproject_run_active`, `cap` and `actual` on the size refusals.
- **Archived AutoProjects** stay readable: every read route and the file routes accept them; only a new run is refused (409 `autoproject_archived`).

## Shapes

The builders in `chat/autoprojects/views.py` define every shape (their docstrings list the fields); the frontend types are `AutoProject`, `AutoProjectRun` and their neighbours in `frontend/src/api/types.ts`. What the field lists do not say:

- **List row** (`autoproject_list_row()`): `active_run` and the unread seqs, so the sidebar shows the working, "Needs you" and unread marks without a detail fetch.
- **Detail** (`build_autoproject_detail()`): carries the whole brief, so a brief edit needs no second read. `brief_source` / `brief_run_seq` name the current version's writer (`run_seq` is null once that run was deleted), so the UI can say "Updated by run N" or "Edited by you".
- **Runs page** (`build_runs_page()`): newest first, keyset-paged by `before_seq`, with `next_before_seq` null on the last page. A run view carries its questions (oldest first) and, for an active run, its open approval and connect cards as the transcript rows the conversation detail holds, so the page renders them with the chat's card components. An active run's `summary`, `result` and `notes` are null even when a `brief_conflict` saved a pending entry.
- **Question view** (`question_view()` in `chat/autoprojects/questions.py`): `status` is `pending`, `answered` or `cancelled`.

## Endpoints

The full list with bodies and refusals is the `chat/autoprojects/routes.py` module docstring. Rules a caller cannot read off the signatures:

- **POST `/autoprojects`** (201, detail). The title is the body's `title`, else cut from `prompt` (the New AutoProject composer's first message, used only to name the AutoProject and not stored), else a default; only an explicit title clears `title_is_default`. A default model unticked for private conversations is 400 `model_not_allowed`. 409 `autoprojects_cap_reached` past `MAX_ACTIVE_AUTOPROJECTS_PER_USER` unarchived AutoProjects.
- **PATCH `/autoprojects/{id}`**. Only the fields present are applied; null clears `model` / `thinking_effort`, a null `title` is 400 `invalid_title`. Any title set here clears `title_is_default`, so no run renames the AutoProject afterwards.
- **PUT `/autoprojects/{id}/brief`** `{brief, expected_brief_version}`. Allowed while a run is active (the run's own write then gets `brief_conflict`). 409 `stale_brief` with `current` when the version moved.
- **POST `/autoprojects/{id}/brief/versions/{version}/restore`** `{expected_brief_version}`. Writes the stored text as a new version with source `restore:<version>`, through the same token as an edit; 404 `brief_version_not_found` for a pruned or never-written version.
- **GET `/autoprojects/{id}/runs`** `?before_seq=&limit=`. See [Shapes](#shapes).
- **POST `/autoprojects/{id}/runs`** (201, `{conversation_id}`). Creates the run's conversation only; see [Starting a Run](#starting-a-run). 409 `autoproject_run_active` / `autoproject_archived`.
- **DELETE `/autoprojects/{id}/runs/{run_id}`**. A finished run only (409 `autoproject_run_active` otherwise); deletes the entry and its conversation.
- **POST `/autoprojects/{id}/questions/{handle_id}/answer`** `{answer}`. Returns the answered question view. 400 `invalid_answer` (blank, not a string, over `AUTOPROJECT_ANSWER_MAX_CHARS`), 409 `question_resolved` with its `status` and `answer` for one already answered or cancelled. The generic `POST /wait-handles/{id}/resolve` refuses this kind with 409 `use_autoproject_answer_route`.
- **POST `/autoprojects/{id}/read`** `{seq}`. The acknowledgement of a client showing the newest update in a focused window ([AutoProjects -- Unread](../architecture/autoprojects.md#unread)); a no-op for an impersonating admin.
- **PUT `/autoprojects/{id}/archive`** / **`/unarchive`**. Archive is 409 `autoproject_run_active` while a run is active and marks the AutoProject read; unarchive is 409 `autoprojects_cap_reached` at the cap.
- **DELETE `/autoprojects/{id}`**. Stops an active run first; the sweep is in [AutoProjects -- Lifecycle](../architecture/autoprojects.md#lifecycle).

## Starting a Run

A run starts in two steps, the way a one-click routine run does (`frontend/src/services/autoprojectRuns.ts` drives both):

1. `POST /autoprojects/{id}/runs` creates the run conversation (`origin: "autoproject"`, `autoproject_id` set, no run row yet) after an early check of the gate, ownership, the archive flag and the active run. Attachments are uploaded to that conversation's workspace with the ordinary conversation file routes and composer-attachment route.
2. `{op: "send_message", conversation_id, message, model?, thinking_effort?, ...}` on the persistent socket. `_handle_send_message()` sees the conversation's `autoproject_id` and calls `start_run()` before appending the user row; on success the sender gets `send_message_accepted` as for any send and the turn runs through the run driver.

A refused start is `send_message_rejected` whose `reason` is the store's code, with `message` and, for an active run, `active_run`. The reasons beyond the generic ones of [Chat API -- Send Message](chat-api.md#send-message): `autoprojects_disabled`, `autoproject_not_found`, `autoproject_archived`, `autoproject_run_active`, `autoproject_run_single_message` (the conversation already took its one message), `model_not_allowed`, `invalid_prompt` (blank or over `AUTOPROJECT_PROMPT_MAX_CHARS`), and `autoproject_run_start_failed` for an unexpected error. A refused send leaves nothing in the transcript.

A conversation whose send failed or went unconfirmed is spent, since the server takes one message per run conversation and an unconfirmed send may have landed: the client starts every attempt on a fresh conversation. A conversation never sent on is deleted by a later start of the same AutoProject once older than `AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE`, or by the startup sweep.

**Stop** is the ordinary `{op: "stop", conversation_id}` on the run conversation; it also marks a `waiting` run `stopped`. Stop on one of a run's approval cards ends the run as well.

## Files

`/app/api/autoprojects/{id}/files`, `/files/upload`, `/files/content`, `/files/download`, `/files/download-sanitized`, `/files/download-folder`, `/files/info`, `DELETE /files`, `/files/create-folder` and `/files/save-to-drive` mirror the project workspace routes with the same handlers and error shapes, over `resolve_autoproject_workspace_for_route()`: the gate and the AutoProject 404 come first, and the root is created on first access ([File Browser API -- AutoProject workspace routes](file-browser-api.md#autoproject-workspace-routes)).

`POST /conversations/{conversation_id}/files/copy-to-autoproject` and `/copy-from-autoproject` copy or move between a run's conversation workspace and its AutoProject's workspace, with the body, rules and errors of the project copy routes plus 400 `not_an_autoproject_run_conversation` for a conversation that is not a run.

## Events

- `autoproject_list_changed` (no payload) and `autoproject_changed {autoproject_id, event_seq, brief_version, active_run}`: per-user globals to the owner, from `chat/autoprojects/events.py` ([AutoProjects -- Realtime Events](../architecture/autoprojects.md#realtime-events)). A deleted AutoProject sends a last `autoproject_changed` with no active run, whose re-fetch 404s.
- `file_list_changed` with `scope: "autoproject"` and `autoproject_id` for writes to the AutoProject workspace; a copy route publishes one event per scope.
- The run conversation's own events (`message_appended`, `tool_started`, `text_delta`, `send_message_finished`, `wait_handle_resolved`) are the live progress of a run.

## SPA Routes

`serve_spa_autoprojects` in `quest.py` serves the SPA for `/autoprojects`, `/autoprojects/new`, `/autoprojects/<id>` and `/autoprojects/<id>/runs/<conversation id>` ([AutoProjects Frontend -- Routes](../architecture/autoprojects-frontend.md#routes)).

## Design Decisions

**Why create the run conversation over REST and start the run on the send?**
The send path already does the append, the delivery receipt, the run registry, stop and the streaming plumbing. Allocating the run inside `start_run()` before the append makes the send the one authoritative check; the REST step only gives the composer an early refusal and a conversation to upload attachments into.

**Why a dedicated answer route instead of the generic wait-handle resolve?**
An answer needs the gate, the AutoProject ownership, answer validation and the transcript rewrite; the generic route knows none of them, so it refuses the kind instead of resolving it half-way.
