# Quest Docs API

## Overview

REST endpoints for Quest Docs (user- and project-owned markdown documents; see [Quest Docs Architecture](../architecture/quest-docs.md)). This is the UI surface. Models never call it: they use the seven doc tools and the `write_doc` action request described in the architecture doc. The router is `chat/docs/routes.py` (mounted in `quest.py`), and request models (`CreateDocRequest`, `UpdateDocRequest`, `SetDocModeRequest`) are in the same file. Every handler goes through `chat/docs/service.py` and `db/doc_store.py`. Not to be confused with the Google Docs integration ([Docs API](docs-api.md)).

## Authentication, Gate and Visibility

- **Auth**: `get_current_user_cookie_or_apikey_checked`, so either the session cookie or a Bearer `users.api_key` works, and the admission check (`require_user_allowed`) runs on every request (see [Chat API](chat-api.md)). The routes live under `/app/api`, so the model's `curl_proxy_*` tools (`/api/*` only) cannot reach them. The sandbox tool API port does not serve them either.
- **Gate**: every handler first returns 403 `docs_disabled` while the `docs` feature gate is closed for the user ([Feature Gates](../architecture/feature-gates.md)).
- **Visibility**: every by-id route resolves the doc through `_get_doc_for_ui()`, which calls `service.get_visible_doc()` with `run_kind="ui"`. A doc the user cannot see (not the owner, no share) is 404 `doc_not_found` with exactly the body a nonexistent id gets. So is a doc of a public project while the `public_projects` gate is closed for the user (`project_id` set and `mode == "public"`; a project doc mirrors its project's flag). That covers GET, PUT, mode, assets, download and DELETE alike, matching the project routes' hidden-public-project rule, and access comes back when the gate reopens. Rename, mode switch and delete then also require ownership, so a visible doc the user does not own is 403 `forbidden` (checked in the route for rename, and in `service.switch_doc_mode` / `service.delete_doc_from_ui` for the other two).

## Response Shapes

**Doc row** (`_doc_row()`): the store's doc dict (`_doc_to_dict()` in `db/doc_store.py`: `id`, `owner_id`, `project_id`, `title`, `description`, `mode`, `content_size`, `asset_count`, `last_write_source`, `created_at`, `updated_at`) plus:

- `scope`: `user` or `project`;
- `shared`: bool;
- `shares`: the roster `[{id, user_id, permission, created_at}]`, where `user_id` null means everyone. Present **for the owner only**;
- `last_write_source`: blanked to null for non-owners, since the owner's conversation ids are not a share recipient's business;
- `access`: what the UI may offer. `can_rename` and `can_delete` are true for the owner; `can_switch_mode` is true for the owner on user docs only, and only while the `public_projects` gate is open for the user (`_public_projects_enabled_for`) or the doc is already public (so a leftover public doc can still be made private); `write` is the `ui` verdict, `free` for the owner or a write share and `denied` otherwise.

Timestamps are naive-UTC ISO strings. `updated_at` is also the optimistic-concurrency token.

**Errors** are `HTTPException(detail={"error": <code>, "message": ...})`, so the JSON body is `{"detail": {"error", "message"}}`. The one exception is the optimistic-concurrency conflict, which is a **flat** 409 body `{"error": "stale_update", "message", "current": <doc row>}` (no `detail` wrapper, like `PUT /routines/{id}`). Pydantic or query validation failures, such as `limit` outside 1..200 or a missing `title`, are FastAPI's standard 422.

## Endpoints

- **GET `/app/api/docs`** (`list_user_docs`). Query: `project_id?`, `limit?` (1..200), `cursor?`. Without `project_id` it lists user docs (no project) that the user owns or holds a share on, including everyone shares. With `project_id` it lists that project's docs; the project must be the user's and visible through `_get_visible_project()` (so a public project hidden by the `public_projects` gate counts as missing), else 404 `project_not_found`. Sorted by `updated_at` descending, then `id` descending, and every row passes the access rule. Without `limit` the whole list is returned. With `limit`, keyset paging: `next_cursor` is `<updated_at>|<doc_id>` built from the last row, echoed back as `cursor`; a malformed cursor is 400 `invalid_cursor`. The response is always `{docs: [row...], has_more, next_cursor}`, with `next_cursor` null when there are no more rows.
- **POST `/app/api/docs`** (`create_ui_doc`, 201). Body: `title`, `description?`, `mode?`, `project_id?`. Creates an **empty** doc owned by the user with `last_write_source="ui"` (`service.create_doc_from_ui`). A user doc takes `mode`, default `private`; while the `public_projects` gate is closed for the user an explicit `mode: "public"` is 400 `public_projects_disabled` (checked before title validation) and an omitted mode creates a private doc. A project doc always takes its project's mode, and a supplied `mode` that disagrees is 400 `project_doc_mode_inherited`. Errors: 404 `project_not_found` (checked first, then again in the service); 400 `invalid_title` / `invalid_description` / `invalid_mode` / `invalid_request`; 409 `duplicate_title` (titles are unique case-insensitively per owner, project **and mode**); 500 `doc_storage_error`. Returns the row. Publishes `doc_list_changed` + `doc_changed`.
- **GET `/app/api/docs/{doc_id}`** (`get_ui_doc`). Returns the row plus `content` (the whole `doc.md`), `assets` (the doc's images for the viewer's Assets panel: `[{name, size, mime}]`, regular files directly in `assets/` sorted by name, hidden names / symlinks / directories skipped, names outside `[A-Za-z0-9._-]+` skipped (`_LISTED_ASSET_NAME_RE`, fullmatch; all `add_asset` ever produces), MIME from `_INLINE_IMAGE_MIMES` by lowercased extension, built via `files.list_assets` in a thread; the list endpoint does not carry it; 400 `invalid_doc_files` when `assets/` is a symlink or not a directory) and `last_write_conversation`: `{id, title, project_id}` of the conversation named by `last_write_source` (`_last_write_conversation()`: one `get_conversation_meta` lookup plus `ChatStorage._resolve_list_title`), or null when the source is not a conversation, the conversation no longer exists, or the caller is not the owner (blanked together with `last_write_source`). The viewer's "Last written by" footer reads it instead of fetching the conversation. Errors: 404 `doc_not_found`; 400 `invalid_doc_files` (missing body, or a symlinked / special `doc.md`); 500 `doc_storage_error`.
- **PUT `/app/api/docs/{doc_id}`** (`update_ui_doc`, owner only). Body: `title?`, `description?`, `expected_updated_at?`. Renames and/or changes the description. When `expected_updated_at` is given it must equal the row's current `updated_at` string exactly, else the flat 409 `stale_update`; omitting it skips the check. `updated_at` is bumped, and `doc_list_changed` + `doc_changed` published, only when a field actually changes. A no-op save keeps the token. Errors: 403 `forbidden`; 400 `invalid_title` / `invalid_description` / `invalid_request`; 409 `duplicate_title`; 404 `doc_not_found`. Returns the row, without `content`.
- **PUT `/app/api/docs/{doc_id}/mode`** (`set_ui_doc_mode`, owner only, delegating to `service.switch_doc_mode`). Body: `mode` (`private` or `public`). Switches a **user** doc's mode. Checks run in this order: 404 `doc_not_found`; 403 `forbidden`; 400 `public_projects_disabled` for a `public` target while the `public_projects` gate is closed for the user (a `private` target is always allowed); 400 `invalid_mode` (also when `mode` is missing or not a string); 400 `project_doc_mode_inherited` for a project doc; same mode is a no-op that returns the row without events; 409 `duplicate_title` when the target mode already has a doc with this title (`set_doc_mode` checks the per-mode title namespace). The flip runs under the per-doc write lock that model writes hold, so a write that passed its access check before the flip can never land after the doc became public. On success it bumps `updated_at` and the service publishes both events. Going private -> public moves the doc into the partition public conversations can read and write, and removes the approval gate for its write-share recipients.
- **GET `/app/api/docs/{doc_id}/assets/{name}`** (`get_ui_doc_asset`). Serves one embedded image from bytes read by `files.read_asset()`, which validates the name and opens the leaf `O_NOFOLLOW` + `S_ISREG` (unlike `FileResponse`, which would follow a symlink at open time). Headers: the real MIME type from `_INLINE_IMAGE_MIMES` in `chat/file_routes.py` (png/jpeg/gif/webp), `Content-Disposition: inline; filename="<name>"`, `Cache-Control: private`, `X-Content-Type-Options: nosniff`. 404 `asset_not_found` for anything that is not a regular, non-symlink file directly inside the doc's `assets/` with a raster extension (traversal and dot names included).
- **GET `/app/api/docs/{doc_id}/download`** (`download_ui_doc`). Query: `format` = `md` (default) or `zip`; anything else is 400 `invalid_format`. `md` returns `doc.md` as `text/markdown; charset=utf-8`. `zip` returns `doc.md` + `assets/<name>` (`files.build_zip()`, run in a thread, temp file deleted after the response), and refuses with 400 `invalid_doc_files` when `assets/` holds a symlink or special file. Both are `attachment`s named `<title>.<ext>` (`_download_filename()`: control characters and path/Windows-reserved punctuation stripped, 120 chars max, `doc` fallback), with an ASCII fallback plus an RFC 5987 `filename*`.
- **DELETE `/app/api/docs/{doc_id}`** (`delete_ui_doc`, owner only, delegating to `service.delete_doc_from_ui`). Under the per-doc write lock, deletes the row (shares go with it), then the directory, best-effort and logged. Returns `{"deleted": true}`. Errors: 404 `doc_not_found`; 403 `forbidden`. The service publishes `doc_list_changed` only.

### Not implemented

The schema and storage are ready for these, but the routes do not exist:

- `GET /app/api/docs/{id}/revisions` (history and restore; snapshots are written under `revisions/`);
- `POST` / `DELETE /app/api/docs/{id}/shares` (sharing; the `doc_shares` table and `doc_store` share helpers exist);
- `PUT /app/api/docs/{id}/content` (human editing with `expected_updated_at`).

## SPA Routes

`GET /docs` and `GET /docs/{rest:path}` (outside `/app/api`) are not API routes: `serve_spa_docs` in `quest.py` returns the frontend's `index.html` so the Quest Docs deep links (`/docs`, `/docs?project=<id>`, `/docs/<id>`) survive a reload; the views are described in [Quest Docs Architecture -- Frontend](../architecture/quest-docs.md#frontend). To free the path, FastAPI's interactive docs moved off their defaults: Swagger UI is at `/api-docs` (OAuth2 redirect `/api-docs/oauth2-redirect`) and ReDoc at `/api-redoc`, while `/openapi.json` is unchanged (`tests/test_spa_docs_routes.py`).

## Error Codes

| Code | Status | Raised by |
|------|--------|-----------|
| `docs_disabled` | 403 | every route, gate closed for the user |
| `doc_not_found` | 404 | per-doc routes; missing docs, hidden docs and docs of a gated-off public project get identical bodies |
| `forbidden` | 403 | rename / mode / delete on a visible doc the user does not own |
| `project_not_found` | 404 | list or create with a `project_id` that is not the user's visible project |
| `invalid_cursor` | 400 | list with a malformed `cursor` |
| `invalid_title` / `invalid_description` | 400 | create, rename (`service.validate_doc_metadata()`) |
| `invalid_mode` | 400 | create, mode switch |
| `invalid_request` | 400 | create, rename (other store validation failures) |
| `public_projects_disabled` | 400 | create with `mode: "public"` or mode switch to `public` while the `public_projects` gate is closed for the user |
| `project_doc_mode_inherited` | 400 | create with a mode that disagrees with the project; mode switch of a project doc |
| `duplicate_title` | 409 | create, rename, mode switch |
| `stale_update` | 409 (flat body with `current`) | rename with a stale `expected_updated_at` |
| `asset_not_found` | 404 | asset route |
| `invalid_format` | 400 | download with a format other than `md` / `zip` |
| `invalid_doc_files` | 400 | read / download when the doc directory holds a missing body, symlink or special file |
| `doc_storage_error` | 500 | unexpected filesystem error (message names no path) |

## Realtime Events

Two per-user globals on the persistent WebSocket, sent to the doc's owner (see [Realtime](../architecture/realtime.md)):

- `doc_list_changed` (no payload): list views re-fetch their window;
- `doc_changed {doc_id, updated_at}`: an open viewer re-fetches, and can skip the fetch when it already shows that `updated_at`.

`chat/docs/service.py` publishes for every model or action-request write and for the UI mutations it owns (create, mode switch, delete). The rename route publishes for itself, only when something changed. The project-delete route publishes `doc_list_changed` once after its directory sweep.

Frontend consumers: `useDocs` (`frontend/src/hooks/useDocs.ts`, the sidebar Docs blocks and All Docs) and `useProjectDocsIndex` (the All Docs per-project fan-out) refresh on `doc_list_changed`. `useDoc` (the viewer) re-fetches on `doc_changed` for its doc when `updated_at` differs, and on every `doc_list_changed`, which is how a delete surfaces as a 404. Share recipients receive neither event, so their open views do not update live (see [Quest Docs Architecture -- Realtime](../architecture/quest-docs.md#realtime)).

## Agent Tools

Models read and write docs only through `list_docs`, `search_docs`, `read_doc`, `create_doc`, `edit_doc`, `append_to_doc`, `add_doc_image` (`TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py`) and the `write_doc` action request. Sandbox scripts get the three reads over the tool-call bridge. See [Quest Docs Architecture -- Model-Facing Tools](../architecture/quest-docs.md#model-facing-tools).
