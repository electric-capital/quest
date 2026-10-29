# Quest Docs Architecture

## Overview

A **Quest Doc** is a markdown document that lives entirely inside Quest, owned by a user (a **user doc**) or by a project (a **project doc**, owned by the project owner). Each doc is a `docs` row plus a directory holding `doc.md`, embedded raster images and revision snapshots. Models reach docs only through seven dynamic tools (list, search, read, create, search/replace edit, append, add image) and the `write_doc` action request. One access function decides, for each conversation, whether a write is approval-free, needs a `write_doc` card, or is impossible. People browse, create, share, edit and restore docs in the UI at `/docs` ([Frontend](#frontend)); the HTTP surface is [Quest Docs API](../api/quest-docs-api.md). The feature sits behind the off-by-default, per-user `docs` [feature gate](feature-gates.md). A doc can also carry notes (instructions and memories) outside its body ([Doc Notes](doc-notes.md)). Google Docs is a separate feature ([Docs API](../api/docs-api.md), `system:docs`).

Docs come in two **modes** that mirror [public projects](public-projects.md). **Private** docs may hold internal information: only private conversations read or write them, and public conversations are never told they exist. **Public** docs hold only content from the internet-enabled public sandbox or typed by a person in the UI. Public conversations read and write them; the conversations of a private project may read, never write, the public docs of the public projects listed in its Project Access setting ([Projects -- Project Access](projects.md#project-access-doc-sources)). So internal data cannot flow into a doc that a public conversation could later exfiltrate. Only the docs of a public project are public ([Mode is fixed at creation](#mode-is-fixed-at-creation)).

## Key Files

| File | Description |
|------|-------------|
| `chat/docs/constants.py` | Every cap and retention number (`DOC_*`), the mode and share-permission sets, the prompt pseudo-key `DOCS_SERVICE_KEY`, and the shared user-facing texts, incl. `doc_not_found_message()` (the one not-found text) |
| `chat/docs/access.py` | `resolve_doc_access()`, the access rule and its doc wording; the module docstring holds the matrix and the rules layered on it |
| `chat/artifact_access.py` | `resolve_core()`, the procedure that applies the matrix, shared with [Quest Tables](quest-tables.md#access) |
| `chat/docs/files.py` | On-disk layer: body, revisions, `doc.meta.json`, assets, zip; layout and safety rules in the module docstring |
| `chat/docs/service.py` | The one read/write path shared by tools, routes and the action request (pipeline and invariants in the module docstring), incl. the locked UI delete and `mode_switch_refusal()` |
| `chat/docs/ui_writes.py` | Human (UI) writes: editor save, History restore, image upload and delete; `referenced_asset_names()`, the one matcher of the `assets/<name>` references in a body |
| `chat/docs/history.py` | Revision list / read / diff, restore and copy; the Version shape and the per-viewer labels are in its module docstring |
| `chat/docs/events.py` | Realtime publishing to a doc's audience; the module docstring records who publishes what |
| `chat/docs/routes.py` | The base `/app/api/docs*` router; `_ui_row()` / `_ui_rows()` build every UI row |
| `chat/docs/share_routes.py` / `edit_routes.py` / `history_routes.py` | The sharing, editing and history routers, all mounted in `quest.py` |
| `db/doc_store.py` | Async metadata CRUD, title uniqueness (`_title_taken()`), the keyset candidate lists, the share helpers |
| `db/project_store.py` | The Project Access helpers over `project_doc_sources` ([Projects -- Project Access](projects.md#project-access-doc-sources)) |
| `db/user_store.py` | `get_users_by_ids()` for rosters and attribution; `find_user_by_email_ci()` for share-by-email |
| `db/conversation_store.py` / `db/action_request_store.py` | `get_conversations_meta()` (History titles) / `get_action_request_owners()` (who proposed an approved `write_doc` card) |
| `chat/realtime/bus.py` | `connected_user_ids()`, the install-wide fan-out for everyone shares |
| `db/models.py` | `Doc`, `DocShare`, `ActionRequestType.WRITE_DOC` |
| `alembic/versions/55983a10e266_create_docs_and_doc_shares.py` | Creates `docs` + `doc_shares` and their indexes |
| `config/paths.py` / `chat/storage.py` | `DOCS_DIR`; `ChatStorage.get_doc_dir()` (sole id-to-path resolver) and the `doc_reads.json` sidecar (`get_doc_read_ids` / `add_doc_read_ids`) |
| `chat/gemini_api/tool_handlers/docs.py` | The seven tool handlers: argument coercion, gate check, error-shape mapping |
| `chat/gemini_api/tool_dispatch.py` | `ToolContext.run_kind`, `_doc_caller()`, the seven `TOOL_CALL_HANDLERS` entries |
| `chat/llm/tool_schemas.py` | The seven `TOOL_CALL_REGISTRY` specs (`requires_service: "docs"`, writes `mutating`) and their `PUBLIC_TOOL_CALL_ALLOWLIST` entries |
| `chat/gemini_api/script_tool_call.py` | `SCRIPT_TOOL_CALL_ALLOWLIST`, which holds the three reads |
| `chat/action_request_types/write_doc.py` / `doc_precard.py` | The `write_doc` handler and its pre-card check |
| `chat/action_request_types/_skill_content_edit.py` | `apply_content_edit()` (shared with `edit_skill`) and `build_bounded_content_diff()` (the bounded card diff) |
| `config/feature_gates.py` / `api/instructions.py` | `FEATURE_DOCS`, `docs_enabled_for()`; the `docs` pseudo-key in `get_user_connected_services()` and `PSEUDO_SERVICE_KEYS` |
| `chat/system_skills/catalog.py` / `chat/system_skills/loader.py` | The `system:quest_docs` skill (`_quest_docs_content`) and its gate-closed load in `load_system_skills()` |
| `chat/gemini_api/system_prompt.py` | `_PUBLIC_DOCS_SECTION`, and `_doc_tool_names()` / `_doc_write_tool_names()` for the tier prompts |
| `chat/realtime/events.py` | `make_doc_list_changed()` / `make_doc_changed()` |
| `chat/project_routes.py` / `chat/routes/user.py` | Directory sweeps on project delete and account delete |

## Concepts and Invariants

| Term | Meaning |
|------|---------|
| Owner | `docs.owner_id`. A project doc is owned by the project owner. |
| Scope | Derived, never stored: `project` when `project_id` is set, else `user`. |
| Mode | `private` or `public`, stored on every row and fixed at creation ([Mode is fixed at creation](#mode-is-fixed-at-creation)). |
| Share | A `doc_shares` row granting `read` or `write` to one user, or to everyone on the install (`user_id` NULL). |
| Private conversation | Any conversation outside a public project: standalone, private project, routine, Slack, sub-agent, inference API, cross-user subagent. |
| Public conversation | A conversation inside a public project (`is_public` in `run_conversation_turn`). |

Invariants the code keeps (code comments cite them by number):

1. **Taint.** Content enters a public doc only from public conversations or from a person in the UI. A private conversation never gets a non-denied write verdict on a public doc, every doc is created in its final mode and keeps it, and History's copy always creates a private doc (public -> private is safe, the reverse never happens). The action request re-resolves access at Approve, so a doc that became hidden while its card sat open (share revoked, doc deleted) refuses.
2. **Invisibility.** In every tool, a doc the caller may not see behaves exactly like a nonexistent id. The text is the same (`doc_not_found_message`) and so is the work: `doc_store.get_doc` runs its shares query for a missing id too, so both cost two queries and neither touches the files. Hidden docs never appear in list or search output, and title uniqueness is scoped per mode so a `create_doc` collision cannot reveal a private title to a public conversation ([Tables](#tables)).
3. **Single rule.** Every read/write decision goes through `resolve_doc_access()`. Tools, routes, the pre-card, the approve-time execute, `list_docs` / `search_docs` and the UI `access` object consume its `DocAccess`; none re-derive the rule.
4. **Paths.** `ChatStorage.get_doc_dir()` is the only id-to-path resolver (canonical id + containment check, see [Data Paths](data-paths.md)). `chat/docs/files.py` gets every path through `doc_paths()`.
5. **No full replace.** No model-facing path replaces a whole body in one call. Edits are exact search/replace (`apply_content_edit`), plus append and image add. `files.write_body()` has one production caller, `ui_writes.replace_body_from_ui()` (the editor's save and History's restore), which only the `/app/api` routes reach; no model-driven channel can address them ([Human Editing](#human-editing)).

### Mode is fixed at creation

A user doc (`project_id` NULL) is always private, a project doc copies the immutable `projects.public` at creation, and no doc's mode ever changes, by a model or in the UI. A doc becomes public only by being born in a public project, so no internal content can be promoted into the public partition and the taint invariant needs no switch-time re-check ([Design Decisions](#design-decisions)).

Every creation path enforces it. `doc_store.create_doc()` refuses a public user doc (`DocValidationError`). The `create_doc` tool (`service.create_doc()`) refuses `target="user"` from a public conversation before anything is written (`user_doc_in_public_conversation_message()`), and gives a project doc the conversation's own mode (`creation_mode()`), refusing a project whose mode disagrees. `service.create_doc_from_ui()` refuses a public user doc and a `mode` that disagrees with the project. `PUT /docs/{id}/mode` stays registered so an old client gets a clear error, and refuses every doc through `service.mode_switch_refusal()`; `access.can_switch_mode` is always false, kept only for the frontend type.

Two backstops: `resolve_doc_access()` evaluates any doc with `project_id` NULL as private whatever its stored `mode`, so public conversations never see a user doc, and `_visible_docs()` in the service leaves user docs out of the SQL query for a public caller.

## Storage

### On-disk layout

Everything for one doc lives under `DOCS_DIR/<doc_id>/`: `doc.md`, `doc.meta.json`, `assets/` and `revisions/` (snapshot `.md` files with `.json` sidecars). The layout and the safety rules every file call keeps (`O_NOFOLLOW` plus an `S_ISREG` re-check on the descriptor, `O_NONBLOCK` reads so a planted FIFO never blocks, atomic writes) are in the `chat/docs/files.py` module docstring. `doc.md` carries no front matter, since all metadata is in the DB, so a download is clean. An asset's extension always comes from `sniff_image_type()` (magic bytes), never from the caller's file name, and the body references images relatively as `![alt](assets/<name>)`. Only `init_doc()` creates a doc root, so a write racing a delete fails instead of resurrecting a directory whose row is gone. A doc directory is never mounted into a sandbox.

Concurrency uses two process-local locks, each table holding weak values. `files.doc_lock()` is a per-doc `threading.Lock` held for one file call, and `modify_body()` reads, transforms and writes `doc.md` in one critical section, because two separate calls would let concurrent writers clobber each other. The service additionally holds a per-doc `asyncio.Lock` across a whole write ([Write path](#write-path)) and the UI delete ([UI mutations](#ui-mutations)).

### Revision policy

`_write_body_locked()` in `chat/docs/files.py` snapshots the current body into `revisions/` before every body write, then prunes:

- **Identical-body writes take no snapshot**: there is nothing to restore.
- **Pruning at write time** (`_prune_revisions()`) deletes snapshots older than `DOC_REVISION_RETENTION_DAYS` and the oldest beyond `DOC_REVISION_MAX_COUNT`. The count cap bounds the disk a looping writer can use inside the window, since each snapshot can be up to `DOC_MAX_CONTENT_SIZE`.
- **The newest snapshot is always kept**, however old, so an idle doc keeps one restore point.

A version is a body. Creation takes no snapshot, metadata and share changes touch no files, and asset-only writes (`add_doc_image` with `placement: "none"`, a UI image upload or delete) change only `assets/`, which every version shares ([Known Limitations](#known-limitations)).

**Attribution.** `doc.meta.json` records when the current body was written, by which write source (the forms of the row's `last_write_source`, see [Tables](#tables)) and its size (`_body_meta()`). Before a body write, the snapshot's `.json` sidecar is a copy of the replaced body's `doc.meta.json`; that is what [History](#history) reads. Asset-only writes never touch it, so it can differ from the row's `last_write_source`, which also names a model's image add (UI image uploads and deletes leave the row's source unchanged). When the meta is missing, unreadable or its size disagrees with the body, `_snapshot_meta()` falls back to the file's mtime and an unknown source; a failed meta write never fails the save. `doc.meta.json` is not an asset and not part of the zip download.

### Tables

`docs` and `doc_shares` are modeled in `db/models.py` and stored through `db/doc_store.py`; the column summary is in [Database -- Doc and DocShare Models](database.md#doc-and-docshare-models). What the columns do not say:

- `content_size` / `asset_count` are cached for list views. `last_write_source` is `conversation:<id>` from tools, `ui:<user_id>` from the UI (a legacy plain `ui` means the owner) or `action_request:<id>` from `write_doc`. `updated_at` doubles as the optimistic-concurrency token for rename, the content save and restore; flipping the owner's `require_approval` switch (access rule 7) through `update_doc_metadata()` bumps it like a rename.
- In `doc_shares`, `user_id` NULL grants everyone on the install. The unique `(doc_id, user_id)` index cannot stop duplicate NULL rows in SQLite, so the partial unique index `ix_doc_shares_everyone` allows at most one everyone row per doc. `add_share()` upserts and refuses the owner as a recipient, because a share row would turn the owner's own writes into approval-gated ones; a doc or recipient deleted between the caller's checks and the insert raises `ShareTargetGoneError`. Share changes never bump `updated_at`, so an open editor's token survives them.
- **Title uniqueness** is case-insensitive per (owner, project, mode), enforced by `_title_taken()` rather than an index: a NULL `project_id` is its own scope, the comparison uses Python `casefold()` because SQLite `lower()` folds ASCII only, and it is check-then-insert like the skills store, with no lock against a simultaneous create.

### Read sidecar

`CHATS_DIR/<conversation_id>/doc_reads.json` (`ChatStorage.get_doc_read_ids` / `add_doc_read_ids`) lists the docs this conversation has read, the read-before-edit gate for `edit_doc` and for `write_doc` with `operation: "edit"`. `read_doc` and `create_doc` feed it; `search_docs` snippets do not count. The service writes it on the event loop, under the workspace and skill sidecars' single-event-loop guarantee. A sub-agent's `read_doc` counts for the parent, whose `conversation_id` it shares; scripts have no conversation and record nothing. The [doc notes seen record](doc-notes.md#seen-record) never credits a sub-agent and is deleted on model history rebuilds.

## Access Rule

`resolve_doc_access()` in `chat/docs/access.py` returns a `DocAccess` whose `write` is one of `WRITE_VERDICTS` (free / approval / denied). The matrix (the doc's mode and share state x who asks from which conversation kind, with cells Free / Approval / Read / Hidden) and the seven rules layered on it are written down once, in the module docstring; this doc refers to its rows, cells and rules by those names. What spans other files:

- The procedure that applies the matrix is `resolve_core()` in `chat/artifact_access.py`, shared with [Quest Tables](quest-tables.md#access) so both artifacts follow one rule. It returns a wording-free `CoreVerdict`: a denied verdict carries a machine cause from `DENY_CAUSES`, and each resolver maps every cause to its own text (`_DENY_TEXT`, asserted complete at import). Only `resolve_doc_access()` and `resolve_table_access()` call it.

- `doc` must carry its `shares` list; a dict fetched without shares raises instead of being read as unshared. `doc_source_project_ids` are loaded by the service from `project_doc_sources` for a private project's conversations and are empty for every other caller.
- Rule 2 (project docs outside their project): the owner's standalone conversations (`project_id` None, not public) see the docs of all of the owner's projects read-only, with `DENY_OUTSIDE_PROJECT` on a private doc and `DENY_PUBLIC_DOC_FROM_PRIVATE` on a public one; the service's `_reaches_own_projects()` is the listing-side counterpart, and `doc_store.list_accessible_docs(include_owned_project_docs=True)` supplies the candidates. Ownership is the only reach: a project doc shared with the caller stays Hidden outside its project. A sandbox script carries the project and mode of the conversation that launched it, so it reaches exactly what that conversation reaches ([Script Bridge](#script-bridge)).
- Rule 5 (`ui`): Free is what the row's `access.can_edit` reports and what the content save, image upload, restore and every History route require. Ownership-only operations are not part of the matrix: the routes check them for rename, share management and asset delete, and `delete_doc_from_ui` for delete.
- Rule 7 (the owner's `docs.require_approval` switch): `required_by_owner` on the verdict only changes the wording the model sees (`APPROVAL_REQUIRED_WRITE_NOTE`, `service.approval_required_message()`). The switch is read from the live row on every decision, so flipping it takes effect on the next write of every conversation, and the pre-card injects it so the card says the owner requires approval.
- `write_note()` turns a verdict into the short `write_note` that `list_docs` / `read_doc` return.

### run_kind derivation

`ToolContext.run_kind` in `chat/gemini_api/tool_dispatch.py` derives one of `RUN_KINDS` from the context's `is_*` flags, first match wins (the order is the property's body; `top_level` is the fall-through). Routine runs are `top_level`, and whether the conversation is public is the separate `is_public` field. `run_conversation_turn` passes `is_user_subagent` and `is_slack` (the conversation origin) into `_dispatch_tool_call`, `chat/gemini_api/sub_agent.py` passes `is_sub_agent=True`, and the script bridge passes `is_script=True` with the `project_id` and `is_public` recorded on the container's sandbox lease.

`_doc_caller()` builds the service `Caller` straight from the context (a script's context has `conversation_id=None` and the launching conversation's `project_id` and `is_public`). Other callers build their own: the routes (and `ui_writes.ui_caller()`) use `run_kind="ui"` with no project and `is_public=False`, and the `write_doc` pre-card and execute use `run_kind="top_level"` with `is_public` re-derived from the project row (`project_is_public()` in `write_doc.py`).

## Service Layer

`chat/docs/service.py` is the only write path; its module docstring gives the pipeline (gate before any DB work -> lookup -> `resolve_doc_access` -> file op in `asyncio.to_thread` -> DB bump -> read sidecar -> events) and the invariants it keeps. `DocError` messages are model/user-facing; unexpected `OSError`s become a generic message that names no path.

### Read side

- `list_docs`: candidates come from `doc_store.list_accessible_docs()` (owner or share, keyset-paged on `(updated_at, id)` descending). Every row still passes the access rule, and the store is paged past hidden rows so they never use up `limit`. A public caller adds a SQL `mode = 'public'` filter, so it lists only its own project's docs. Scope `project` is this project's docs inside a project and, from a standalone conversation, the read-only docs of all of the user's own projects (rule 2). Rows carry a `shared` flag but never the share roster.
- `search_docs`: case-insensitive substring match on title, description and body. Bodies of visible docs are read newest-first until the next one would push the scanned total past `DOC_SEARCH_MAX_SCAN_BYTES`; after that only titles and descriptions are matched and `truncated` is true. Snippets carry 1-based line numbers, and a metadata-only hit has `matches: []`.
- `read_doc`: the whole body or a 1-based inclusive line range, capped at `DOC_READ_MAX_CHARS` and cut at a line boundary (`truncated` plus a paging `note`). Lines split on LF only, so they agree with search's line numbers. Records the read.

### Create

`create_doc` lays out the directory under a fresh uuid, then inserts the row: a failed insert removes the directory again, and a crash in between leaves an invisible orphan directory, never a row without a body. Only the `_CREATE_RUN_KINDS` callers (top-level and Slack) may create; creation is never approval-gated because a new doc has no shares. The new id is recorded as read. `create_doc_from_ui` (`POST /docs`) creates an empty doc written as `ui:<user_id>`. Modes: [Mode is fixed at creation](#mode-is-fixed-at-creation).

### Write path

`apply_write_operation()` serves every `WRITE_OPERATIONS` member (edit, append, image add) for both the tools and the action request:

1. **First pass, outside any lock** (`_resolve_write`): gate, parameter normalization, visibility (hidden == missing), `denied` -> `deny_reason`, then the read-sidecar check for `edit` and the conversation requirement for `add_image`. Hidden, denied or unread docs never take or create a lock.
2. **Approval**: unless `bypass_approval`, an `approval` verdict first dry-runs the operation (`_compute_preview`), so a stale `old_string`, a bad image or an oversized body fails now. It then raises `DocApprovalRequired` carrying `suggested_request`.
3. **Locked write**: under the per-doc `asyncio.Lock`, access is resolved again, so a share added or revoked while this write queued changes the verdict. The writer then runs (`files.modify_body` for edit and append; `add_asset` followed by an optional body append for images), and `update_after_write` bumps `content_size` / `asset_count` / `last_write_source` / `updated_at` in write order. Finally `doc_changed` + `doc_list_changed` go to the doc's audience ([Realtime](#realtime)).
4. **Shielded**: the locked part runs via `_shielded()`, so a cancelled turn (Stop mid-write) cannot strand a landed file write without its DB bump and events. A late failure is logged.

`add_image` reads the image only from the calling conversation's own workspace, never the project workspace (a project file is copied in first with `copy_file` from `proj://` to `chat://`, and a not-found error in a project conversation says so). The read goes through `resolve_workspace_file()` in `chat/action_request_types/_io_attachments.py` plus an `O_NOFOLLOW` / `S_ISREG` descriptor read capped at `DOC_MAX_IMAGE_SIZE`, then a magic-byte check. With `placement: "append"` the body cap is pre-checked before the asset is stored; if the append still fails, the asset count is recorded anyway (assets are additive). The `write_doc` execute passes the card's `image_preview.sha256` as `expected_sha256` and a file whose hash differs is refused, because the user (through the Chat Files card) and the model can both change the file while the card sits open.

### UI mutations

`delete_doc_from_ui()` serves `DELETE /docs/{id}`: visibility as `ui`, then ownership (`forbidden`), then the row delete and a best-effort directory removal under the per-doc `asyncio.Lock` and `_shielded()`. It therefore waits for any in-flight model write and re-reads the row with its shares under the lock to capture the audience before the cascade removes them. Rename is a plain metadata update in the route (`doc_store.update_doc_metadata`). Share changes, body saves, restores and asset changes all take the same per-doc lock ([Sharing](#sharing), [Human Editing](#human-editing), [History](#history)).

## Sharing

The owner shares a doc as `read` or `write` with one Quest user, named by email, or with everyone on the install; the routes, their check order and errors are in the `chat/docs/share_routes.py` module docstring. Grants upsert, so sharing again changes the permission. Both routes run under the per-doc write lock, shielded, so a model write queued behind a share change re-resolves its verdict with the new roster. The email lookup (`find_user_by_email_ci`) prefers an exact match, then an ASCII case-insensitive one, and refuses an ambiguous set of case-variant accounts rather than guessing.

What a share grants in the UI (a direct and an everyone row combine, the higher permission winning, `effective_share()`):

| | Owner | Write share | Read share |
|---|---|---|---|
| View, download | yes | yes | yes, but only the images the current body references |
| Edit the body, upload images | yes | yes | no |
| History: list, read, diff, restore, copy | yes | yes | no |
| Rename, delete, delete images, manage shares | yes | no | no |

The access rule itself is unchanged; sharing only makes its share cells reachable. Consequences:

- **The owner's first share on a private doc turns the owner's own conversation writes into `write_doc` approval cards** (matrix row "Private, shared"). Removing the last share makes them free again, unless the owner's require-approval switch (rule 7) is on. The Share dialog says so (`shareDialogNotes()` in `frontend/src/utils/docSharing.ts`).
- **Recipients' conversations reach shared user docs only.** From the recipient's private conversations a shared user doc is readable (read share) or writable through a `write_doc` card the recipient approves in their own conversation (write share). A shared project doc stays Hidden to all of the recipient's conversations, because outside its project a project doc is reached only by its owner's standalone conversations (rule 2), projects are not shared, and only the owner's own public projects can be a Project Access source. **Project-doc shares are therefore UI-only for recipients**, and since every public doc is a project doc, so are shares of public docs.
- **Revocation is immediate.** A `write_doc` card the recipient proposed is refused at Approve, because execute re-resolves access. The recipient's editor, History view and viewer lose access on their next request; an open viewer re-fetches on the `doc_changed {updated_at: null}` the revocation sends.

**UI rows.** `GET /docs` serves three independent keyset streams: the caller's own user docs, one project's docs, and the docs shared with the caller (`doc_store.list_docs_shared_with()`), so a user doc shared with the caller appears only in the shared stream. `_ui_row()` / `_ui_rows()` build every row with at most one batched user lookup per response; non-owners never receive the share roster, the raw `last_write_source` or the writer's identity. Shapes: [Quest Docs API -- Response Shapes](../api/quest-docs-api.md#response-shapes).

**Read-only viewers see only referenced images.** For a viewer whose UI verdict is not Free, only the images the current body references count (`ui_writes.referenced_asset_names()`, escaped spellings included): the detail's `assets` list, the asset route and the zip are all filtered. An image the owner removed from the text before sharing therefore stays private. Owners and write shares see every asset; the row's `asset_count` is not filtered ([Known Limitations](#known-limitations)).

## Human Editing

The owner and write-share recipients edit the markdown in the viewer's editor. The routes are in `chat/docs/edit_routes.py` and the writes in `chat/docs/ui_writes.py`, whose module docstring gives the pipeline, the write source and why invariant 5 holds; request shapes and errors are in [Quest Docs API -- Editing](../api/quest-docs-api.md#editing).

- **Body save** is a whole-body replace carrying `expected_updated_at`. The body is validated before the lock; under it, visibility, the write verdict and the token are re-checked against a fresh row, so a share revoked or another write landing while the request queued wins (flat 409 `stale_update`). An identical body writes nothing; any other replaced body is snapshotted first ([Revision policy](#revision-policy)).
- **Not approval-gated**, also for a write-share recipient of a private doc ([Design Decisions](#design-decisions)).
- **Unreachable from models.** The whole-body replace, the image routes, sharing and restore live under `/app/api` on the main port, which no model-driven channel can address. They accept a session cookie or the user's API key, so the protection is the unreachability, not the auth scheme.
- **Images.** An upload never touches the body: the response carries the `![alt](assets/<name>)` markdown for the editor to insert and save, plus `previous_updated_at`, the token just before this write, so an editor holding exactly that token may adopt the new one without a false conflict. Delete is owner-only and refused (409 `asset_in_use`) while the current body references the image (`asset_referenced()`). Both are asset-only writes: no snapshot, and `last_write_source` keeps naming the body's writer.
- **Bounded request parsing.** The handlers read their own bodies only after the gate, visibility and write checks, so neither a stranger nor an oversized request makes the server buffer an arbitrary body. The limits are the doc caps plus the allowances that are the module constants of `chat/docs/edit_routes.py`; a bad body is 400 `invalid_request`, never FastAPI's 422.

## History

Editors (the owner or a write share) browse, diff, restore and copy a doc's versions. The routes are in `chat/docs/history_routes.py` and the logic in `chat/docs/history.py`, whose module docstring defines the Version shape, the per-viewer `source_label` rules, the revision-id checks and the access re-checks; request shapes and errors are in [Quest Docs API -- History](../api/quest-docs-api.md#history).

- **Editors only**, because earlier revisions can hold text removed before sharing ([Design Decisions](#design-decisions)). Every function re-checks access against a fresh row after its file reads (`_recheck_history_access()`), so a share revoked or downgraded mid-request never leaks a version.
- **Attribution** comes from the [Revision policy](#revision-policy) metadata. An approved `write_doc` card is attributed to its proposer (`get_action_request_owners()`), so a share recipient's approved change carries their name; only the owner receives raw sources, conversation links and action request ids.
- **Diff** (`?diff=current`) is the same `build_bounded_content_diff()` the `write_doc` card uses, computed in a thread.
- **Restore** goes through `ui_writes.replace_body_from_ui()` with the client's `expected_updated_at`, so it gets the content save's lock, re-checks, snapshot and `ui:<user_id>` source.
- **Copy** always creates a private user doc owned by the caller, also from a project or public-project doc ([Design Decisions](#design-decisions)), with the description and every asset the copied body references that the source still holds. The build is shielded and removes the row and directory again on any failure.

## Model-Facing Tools

The seven tools are `TOOL_CALL_REGISTRY` entries in `chat/llm/tool_schemas.py` (dispatched through `tool_call`). Each carries `requires_service: "docs"`, and the four writes are marked `"mutating": True`. Their descriptions are written to stand alone, because the public prompt carries no skills. Handlers are thin wrappers in `chat/gemini_api/tool_handlers/docs.py`.

| Tool | Available to |
|------|--------------|
| `list_docs`, `search_docs`, `read_doc` (the reads) | every tier incl. scripts |
| `create_doc`, `edit_doc`, `append_to_doc`, `add_doc_image` (the writes) | top-level conversations (private, public, routine) and Slack |

Parameters are the registry specs, and success results are the dicts the service functions return (`chat/docs/service.py`). The list / search `scope` values are `DOC_SCOPES` there (`public` means the doc sources' public docs from a private project, and a public conversation's own docs); the limit defaults and maxima are `LIST_DEFAULT_LIMIT` / `LIST_MAX_LIMIT` / `SEARCH_DEFAULT_LIMIT` / `SEARCH_MAX_LIMIT`.

Error results come in three shapes:

- `{"error": "docs_disabled", "message"}` when the gate is closed (checked before anything else).
- `{"error": "approval_required", "message", "suggested_request": {"request_type": "write_doc", "params": {...}}}` when the verdict is Approval. The model forwards `suggested_request` unchanged through `create_action_request`; the `params` sets are pinned to what `write_doc.validate_params` accepts per operation (`OPERATION_PARAM_KEYS` in `chat/action_request_types/write_doc.py`).
- `{"error": "<text>"}` for every other refusal: missing or hidden doc (the one not-found text), a denied write (`deny_reason`), an unread doc, a failed match, bad arguments, caps.

Enforcement outside the access rule:

- **Public conversations**: all seven are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private doc there; a public doc is `free` or `denied`, or `approval` under the owner's require-approval switch, served by the `write_doc` card (one of the four action request types a public conversation can open, [Public Projects -- Action requests](public-projects.md#action-requests)).
- **Inference API runs**: the `mutating` flag puts the four writes in `mutating_tool_call_tools()`, which is hard-rejected at dispatch and refused for the run's sandbox lease (see [Inference API](../api/inference-api.md)). The access rule would deny them anyway.
- **Sub-agents and cross-user subagents**: `get_sub_agent_system_prompt()` and `get_user_subagent_system_prompt()` exclude `_doc_write_tool_names()` from their prompts (the `system:quest_docs` enumeration line names only the reads). If called anyway, `create_doc` refuses through `_CREATE_DENY_REASONS` and the other writes through the access rule.

## The write_doc Action Request

`WriteDocHandler` (`chat/action_request_types/write_doc.py`, registered in `chat/action_request_types/__init__.py`) is the approval form of the three write tools: one type whose `operation` is a `WRITE_OPERATIONS` member. `create_doc` has no action-request form. The pipeline follows `edit_skill`:

- **`validate_params`** checks shape only. It rejects unknown keys and keys of another operation (`OPERATION_PARAM_KEYS`), fills the optional defaults, and drops any model-supplied copy of a server-injected key (`SERVER_INJECTED_KEYS`).
- **Pre-card** (`doc_precard_check()` in `doc_precard.py`, called from the `create_action_request` arm in `chat/gemini_api/turn_tools.py` after the skill and routine pre-cards) delegates to `service.preview_write_operation()`: the same access, read-sidecar, live-body dry-run and cap checks as the tool. A `free` verdict is rejected ("call the tool instead"), and a denied, hidden, unread, stale or gate-closed case gets the service's own text, all as same-turn `Invalid parameters` with no card. On `approval` it injects the `SERVER_INJECTED_KEYS` preview: the share summary carries counts only, never names, and an `add_image` card gets an `image_preview` whose `sha256` pins the file.
- **Card diff**: `build_bounded_content_diff()` of the live body vs the result, computed in `asyncio.to_thread`, because the full-body `build_content_diff()` that `edit_skill` uses is quadratic on a large doc (the function's docstring gives the bounding steps and its keyword defaults the thresholds). Line numbers match `read_doc`'s, and `total_old_lines` / `total_new_lines` let the renderer mark what the window leaves out ([write_doc card](#write_doc-card)).
- **`execute`**: the resolve route in `chat/action_request_routes.py` passes `request_id=` to the types in `REQUEST_ID_TYPES`, this one and the [table cards](../api/tables-api.md#action-requests). `execute` calls `apply_write_operation(..., bypass_approval=True, write_source="action_request:<id>")` with the pinned `expected_sha256` for `add_image`. That re-resolves access (a hidden doc refuses, a doc whose shares were all removed is written), re-checks the read sidecar and re-applies against the live body: the TOCTOU close, so a stale `old_string` fails the Approve and the request stays open. The service publishes the realtime events.
- **Attribution**: a card is proposed and approved in its proposer's own conversation. `get_action_request_owners()` credits a write-share recipient's card to them in the owner's row (`last_write_user`) and [History](#history); the owner's own card links that conversation (`get_action_request_conversations()`).
- **Fail closed on a missing project**: `project_is_public()` raises `RuntimeError` when the conversation has a `project_id` but `project_store.get_project` finds no row for the user, rather than treating the conversation as private. At Approve this surfaces as the resolve route's 500 `execution_failed` with the request left open; at proposal time it is not a `ValueError`, so it escapes the pre-card as a run-fatal error instead of `Invalid parameters`.

`render_preview`, the labels and `summary_snippet` are on `WriteDocHandler`, with their rationale in the code comments; the diff reuses the `skill_content_diff` field type and the image preview is the `doc_image` type.

## Feature Gate

`FEATURE_DOCS` (`config/feature_gates.py`) is a per-user-capable gate, off by default, and every enforcement point calls `docs_enabled_for(email)`. The layers (tools, prompt through the `docs` connected-services pseudo-key, routes, the action request, the frontend) are listed in [Feature Gates -- Enforcement Layers (Quest Docs)](feature-gates.md#enforcement-layers-quest-docs). Closing the gate deletes nothing; reopening it restores every doc.

## Prompting

- **`system:quest_docs`** (`_quest_docs_content` in `chat/system_skills/catalog.py`, name "Quest Docs"; `system:docs` is the Google Docs skill) is the model's guide to the tools, modes, verdicts, read-before-edit, revising in place, the `approval_required` -> `write_doc` handoff and the routine pattern: one doc appended to under a dated heading per run, never a new doc per run. Caps and the History window come from `chat/docs/constants.py`.
- **Revise in place**: History keeps every replaced body ([Revision policy](#revision-policy)), so the skill, the `create_doc` / `edit_doc` descriptions and `_PUBLIC_DOCS_SECTION` tell the model to change an existing doc, not create a copy, unless the user asks for one.
- **Public prompt**: `_PUBLIC_DOCS_SECTION` follows the Boundaries block only while `docs_enabled`, and stands in for the skill (only this project's public docs, always created with `create_doc(target="project")`). It may not name internal-only tools or skills (`tests/test_public_projects.py::test_no_internal_tool_docs`), which is why the write-tool descriptions say "write_doc action request" rather than spelling out `create_action_request(`.
- No doc titles are ever injected into a prompt. Discovery is tools-only.
- `system:action_requests` and the `create_action_request` description route `write_doc` to `system:quest_docs`.

## Realtime

Two per-user globals ([Realtime](realtime.md)), built by `make_doc_list_changed()` / `make_doc_changed()` in `chat/realtime/events.py` and published best-effort through `chat/docs/events.py`, whose module docstring records who publishes what:

- `doc_list_changed` (no payload): list views re-fetch their window.
- `doc_changed {doc_id, updated_at}`: an open viewer of that doc re-fetches unless it already shows that `updated_at`. `updated_at: null` means "this doc's existence or your access to it changed; re-fetch". Doc delete, share changes and project delete send it, always before that change's `doc_list_changed`, because they do not bump `updated_at` and a token could not signal them.

The **audience** is the owner plus every direct share recipient, plus every connected user (`bus.connected_user_ids()`, resolved at publish time) when the doc has an everyone row. A delete captures it before the cascade removes the share rows. Every change an open viewer must notice reaches it as a `doc_changed` for its own doc, so viewers ignore `doc_list_changed`, which can be install-wide traffic.

Publishing is event-loop-thread only, because `bus.publish_to_user` is not thread-safe. The service publishes for every write; tool handlers never publish. Neither envelope carries a `conversation_id`, so they reach `PersistentWebSocket`'s global handlers without an allow-list entry. Consumers: [Realtime consumers](#realtime-consumers); limits: [Known Limitations](#known-limitations).

## Frontend

The UI at `/docs`: browse, read, create empty docs, rename, download and delete; share (owner); edit the markdown, add images and use History (owner and write shares). Each component's header comment describes its own behaviour; this section lists the files and the rules that span them. Paths are under `frontend/src/` unless noted.

| File | Role |
|------|------|
| `api/docsApi.ts` | Client for `/app/api/docs*`, the cookie-authed asset and download URL builders, `DOCS_FEATURE`, and the helpers for the flat 409 `stale_update` |
| `api/types.ts` | `Doc`, `DocDetail` and the share, editing and history types; `CreateDocRequest` has no `mode` |
| `utils/docsRoute.ts` | `parseDocsRoute()` and the docs path builders |
| `hooks/useDocs.ts` / `hooks/useProjectDocsIndex.ts` / `hooks/useDoc.ts` | One keyset-paged list; the per-project fan-out for All Docs; one doc (row + body) for the viewer |
| `components/sidebar/DocsSection.tsx` + `utils/sidebarDocs.ts` | The sidebar Docs blocks (main panel and project drill-down) |
| `components/docs/DocsListView.tsx` + `utils/allDocsGrouping.ts`, `components/docs/NewDocModal.tsx` | All Docs and New Doc |
| `components/docs/DocViewer.tsx`, `DocHeader.tsx`, `DocAssetsPanel.tsx`, `DocImageLightbox.tsx` | The viewer, its title unit and its Assets list |
| `components/docs/DocEditor.tsx` + `utils/markdownFormatting.ts`, `hooks/useUnsavedChangesGuard.ts`, `utils/docDraftBackup.ts`, `components/docs/DocDraftRecovery.tsx` | The body editor, its formatting edits, its leave guard and the per-user draft backup |
| `components/docs/DocHistory.tsx` | The History view |
| `components/docs/DocShareDialog.tsx` + `utils/docSharing.ts` | The owner's Share dialog and the sharing texts |
| `components/docs/DocConfirmDialog.tsx` | The shared confirm, on `ModalShell` |
| `components/docs/DocModeBadge.tsx` + `utils/docMode.ts` | The mode pill; `shouldShowDocModeBadge()` shows it on public docs only |
| `components/docs/DocsGateClosed.tsx` | Shown on a `/docs*` URL while the gate is closed |
| `components/Message.tsx` | `MarkdownWorkspaceContext.assetBase` and the doc branch of `MarkdownImage` |
| `components/ActionRequestPreviewFields.tsx`, `components/DocImagePreview.tsx`, `components/SkillContentDiffPreview.tsx` | `write_doc` card rendering |
| `components/settings/SignOutSection.tsx` | Clears every draft backup on each sign-out path |
| `App.tsx`, `components/MobileShell.tsx`, `components/Sidebar.tsx`, `components/sidebar/ProjectPanel.tsx` | Route rendering and sidebar mounting |
| `quest.py` (repo root) | `serve_spa_docs` |

### Routes and state

`/docs` is All Docs, `/docs?project=<id>` the same view filtered to one project, and `/docs/<id>` the viewer. The URL is the only state ([Design Decisions](#design-decisions)): `App.tsx` and the Sidebar run `parseDocsRoute()` on every render, and `NavigationContext` has no docs field. A viewer id must match `DOC_ID_RE` in `utils/docsRoute.ts`, so a decoded `/` or `..` is never spliced into an `/app/api/docs/<id>/...` URL. A docs route is a full main-pane takeover like `/inbox`, with no RightPanel and no active conversation; the `showRequestsView` flag behind `/inbox` still wins the render, so the Sidebar clears it before navigating to a docs view. `MobileShell.tsx` renders the same views, and the Sidebar's `onNavigateAway` lets it close the phone drawer after a docs click. On the server, `serve_spa_docs` serves `index.html` for `/docs` and `/docs/{rest:path}` ([Quest Docs API -- SPA Routes](../api/quest-docs-api.md#spa-routes)).

### Lists

The sidebar shows the user's own user docs and, while drilled into a project, that project's docs; docs other people shared appear only in All Docs' "Shared with you". All Docs combines three sources because `GET /docs` has no cross-project stream: "Your docs" and "Shared with you" are two server-paged `useDocs` streams, and each project gets a group from `useProjectDocsIndex`, archived projects included since archiving does not affect docs. The view waits for all three before choosing between rows and the empty state, so a user whose docs are all project or shared docs never sees the empty state flash. Search runs client-side over everything loaded. New Doc never sends a `mode`: a user doc is always private and a project doc takes its project's mode.

### Viewer

`DocViewer.tsx` (`useDoc`) swaps its body between three modes kept per doc id: `view`, `edit` ([Editor](#editor)) and `history` ([History view](#history-view)). Rules that span its parts:

- **Header actions follow the row's `access` flags** (`DocHeader.tsx`), and `access.can_switch_mode` is ignored. The require-approval checkbox PUTs through the rename endpoint without `expected_updated_at`, because a body edit landing meanwhile is no reason to refuse a settings flip.
- **Doc images.** With `MarkdownWorkspaceContext.assetBase` set, `MarkdownImage` resolves only `assets/<name>` (one segment, no leading dot) to the cookie-authed asset route; every other relative src renders the missing-image chip, external srcs stay click-through links that are never auto-fetched, and SVG never renders because the asset store and route hold only magic-byte-sniffed raster types.
- **Row and body merges.** `useDoc.applyRow()` (rename, share, a 409's `current`) re-fetches whenever the row's `updated_at` is newer than the shown one, so a new token never pairs with a body this tab has not seen; `applyContent()` takes a save or restore response that carries its body.
- **Footer.** `lastWriter()` derives "Last written by" from `last_write_source`, `last_write_user` and `last_write_conversation`. The viewer is matched to `last_write_user` by email because `GET /me` exposes no user id, and a non-owner sees no writer because the API blanks those fields.
- **Saved drafts.** A draft backup that differs from the doc is offered back in view mode or, once the doc 404s or edit access is gone, shown read-only in `DocDraftRecovery`, so the text is never silently lost.
- **Assets.** `DocAssetsPanel.tsx` lists the detail's `assets`, which ride on the detail rather than their own endpoint because `DOC_MAX_ASSETS` keeps the list small and the viewer always needs both. Only the owner in view mode can delete an image: while editing, the draft may be about to reference it.

### Editor

`DocEditor.tsx` (`access.can_edit`) saves the whole body with the `updated_at` it started from. On a 409 it re-reads the doc and re-saves silently once only when the server body still equals the body its token stood for (only metadata moved), unless an image the draft added was deleted meanwhile; otherwise the user chooses between overwriting (the replaced version stays in History) and discarding. An upload's new token is adopted only when the response's `previous_updated_at` equals the token held, which is why uploads run one at a time.

Under a plain `BrowserRouter`, browser back / forward and button-driven `navigate()` cannot be intercepted; `useUnsavedChangesGuard` covers only unload and same-origin `<a href>` clicks. The draft is therefore also backed up in localStorage (`utils/docDraftBackup.ts`), keyed per user by email because `GET /me` exposes no user id, pruned by age, and cleared on every sign-out path (`SignOutSection.tsx`) so no draft outlives the session in a shared browser.

### History view

`DocHistory.tsx` restores with the token of the current body as it was when the confirm opened, so a write landing behind the dialog is a 409, never a silent overwrite; after a `stale_update` it shows the fetched current body, and uses its token, until the viewer catches up. Losing edit access (a 403, or `can_edit` turning false) replaces the view. Revision images resolve against the doc's current assets, so a deleted image shows the missing-image chip.

### Share dialog

`DocShareDialog.tsx` (owner only) puts every widening of the everyone grant through a confirm, because a native select fires `change` on arrow keys and keyboard browsing must never grant install-wide access by itself. Text without an "@" is never sent as an email. One request runs at a time and the dialog cannot close meanwhile, so no result goes unreported. The add row's permission resets to read after each grant, so edit access chosen for one person is never carried over to the next. A deleted account's grant can only be removed, since grants upsert by email.

### write_doc card

Both card renderers go through `ActionRequestPreviewFields.tsx`. `DocImagePreview.tsx` renders the `doc_image` field, loading the thumbnail from the proposing conversation's own workspace (`add_image` reads only that workspace, so the card's conversation id always resolves it). `SkillContentDiffPreview.tsx` recognizes the bounded diff window by `total_old_lines` / `total_new_lines` and marks the lines outside it; whole-body skill and routine diffs carry no totals.

### Realtime consumers

`useDocs` and `useProjectDocsIndex` re-fetch on `doc_list_changed` behind a trailing debounce (`DOCS_REFRESH_DEBOUNCE_MS`, and the longer `REFRESH_DEBOUNCE_MS` for the per-project fan-out), so a burst of writes to a doc shared with everyone costs one re-fetch per list. `useDoc` re-fetches only on a `doc_changed` for its own doc whose `updated_at` differs from the shown one, which a null always does: that is how a delete, a revoked share or a project delete reaches an open viewer as its not-found state. Share recipients get the same events as the owner.

## Lifecycle

- **Doc delete**: [UI mutations](#ui-mutations).
- **Share revoked**: access ends at once ([Sharing](#sharing)); the recipient's History copies are their own docs and stay.
- **Image deleted**: gone from `assets/` for every version ([Known Limitations](#known-limitations)).
- **Project delete** (`delete_user_project` in `chat/project_routes.py`): `list_docs_for_project()` (rows with their shares, for the audience) runs before the project row goes. The `docs.project_id` CASCADE removes the rows with their shares and notes, `_delete_doc_dirs()` sweeps the directories (failures logged, never raised), then the events go out ([Realtime](#realtime)). The sweep runs before the unguarded conversation-directory `rmtree` loop, so a failure there cannot leave doc files behind.
- **Account delete** (`delete_account` in `chat/routes/user.py`): `list_doc_ids_for_user()` (user and project docs alike) runs before any row delete, and the directories are swept in a thread after the cascades and before the unguarded `rmtree` loops, for the same reason. Share rows granted to the deleted user cascade away; an everyone row on another user's doc survives. Rosters and History labels name a deleted user as such, and recipients are not notified ([Known Limitations](#known-limitations)).
- **Project archived**, **convert chat to project**, **duplicate workspace**: no effect. **Conversation deleted**: its doc-note suggestions cascade, and `last_write_source` becomes a dead reference.
- Orphan directories (a crash between layout and insert, a failed best-effort sweep) are swept by nothing; no row points at them, so they are invisible.

## Script Bridge

Sandbox scripts (`run_script` / `run_python` through `POST /api/tool-call`, see [Gemini API -- Script tool-call bridge](gemini-api.md)) get the three reads only (`SCRIPT_TOOL_CALL_ALLOWLIST`). The container's sandbox lease (`SandboxLease` in `chat/sandbox_tokens.py`) records the launching conversation's `project_id` and `is_public`; the bridge dispatches with `is_script=True`, those two and `conversation_id=None`. A script therefore sees exactly the docs the conversation that ran it sees ([Access Rule](#access-rule)). Every write is denied (`DENY_SCRIPT`), and no read is recorded, so a script's `read_doc` never satisfies the agent's read-before-edit gate. A lease-less call (tests only) behaves like a standalone conversation.

Public containers get no sandbox token and so have no bridge at all, so `is_public` is never set on a lease a script presents. The doc routes are out of reach for the same reason as for models ([Human Editing](#human-editing)), so neither the model nor a script can use the `ui` run kind.

## Constraints

- Caps: every size, count and length limit is a `DOC_*` constant in `chat/docs/constants.py`; the tool limit defaults and maxima are the `LIST_*_LIMIT` / `SEARCH_*_LIMIT` constants in `chat/docs/service.py`.
- Raster images only (`DOC_ASSET_EXTENSIONS`), sniffed by content (`sniff_image_type()`); SVG and anything else is refused.
- The write locks (one `asyncio.Lock` and one `threading.Lock` per doc id) and the everyone-share fan-out (`bus.connected_user_ids()`, the connections of this process) are in-process, which is correct for the single-process server.
- Doc bodies, assets, revisions and the attribution files are plaintext on disk, like workspaces (see [Encryption at Rest](encryption-at-rest.md)).

## Design Decisions

**Why can a user doc never be public?** A user doc is reachable from every private conversation and may hold internal data. A user-doc mode switch was rejected: keeping taint safe would need a switch-time re-check ordered against queued writes, dropped approval gates for write-share recipients and title-collision handling in the target mode, whereas with the mode fixed at creation the invariant holds by construction. A user who wants a public doc creates it in a public project.

**Why scope title uniqueness per mode?** One title namespace per (owner, project) regardless of mode would let a public conversation probe `create_doc(title=...)` and learn from the collision error that a private doc with that title exists, breaking invisibility. Since every project's docs share the project's mode, the mode key is defence in depth.

**Why does a shared private doc need approval, but a shared public doc does not?** The approval card protects internal data that has several stakeholders. A public doc only ever holds sandbox-originated content, every writer is itself a credential-less public conversation, and the recipients opted into `write`. The owner's require-approval switch is the exception: it gates a public doc in its public conversations too, since the `write_doc` card is one of the action requests a public conversation can open.

**Why one access function?** Re-deriving parts of a matrix this size (mode x relationship x conversation kind x run kind) at each call site would drift. Tools, routes, the pre-card, the execute and the prompt all consume one `DocAccess`, and one table-driven test pins it.

**Why are sub-agents, inference runs, cross-user subagents and scripts read-only?** Writes stay with the run that owns the conversation and its approval surface. Sub-agents cannot open action requests, so they return content to the top-level agent, which writes it. Inference and cross-user subagent runs must not change anything outside their own workspace, and a script has no conversation of its own to attribute a write to or to open an approval card in.

**Why search/replace + append instead of full replace?** A full replace lets a model silently drop content it never read. An exact match keeps every change local and reviewable (the same diff powers the approval card), and append covers the main aggregation use case without a read.

**Why files on disk instead of DB rows?** A doc is a portable artifact: `doc.md` plus `assets/` downloads as-is (md or zip) with working relative image links, and revisions are plain files.

**Why do Swagger UI and ReDoc live outside `/docs`?** `/docs` and `/docs/<id>` are the user-facing deep links, and FastAPI registers its docs routes in the constructor, ahead of every `@app.get`, so its default `docs_url` would shadow the SPA route (comment above the `FastAPI(...)` call in `quest.py`). Giving the docs UI another path was rejected: `/docs` is the natural link to share, and relocating Swagger UI cost only the `run.py` banner line.

**Why is the URL the only docs state?** A `NavigationContext` field with a URL-to-context sync effect, the pattern of `showRequestsView` behind `/inbox`, was rejected: with the URL as the only state, deep links, reload and back/forward work without sync code.

**Why does All Docs fan out per project?** A cross-project "all my projects" list endpoint was not built: one `fetchDocs` per project reuses the existing endpoint and its access rule, and lets one failing project degrade to a missing group instead of failing the view. The cost is one request per project on open and per debounced `doc_list_changed` burst, and at most one maximum page per project. The shared docs are a server-side keyset stream instead, because they span other users' projects the viewer cannot list.

**Why are human edits not approval-gated when model writes are?** The `write_doc` card exists so a person reviews what a model is about to write. A UI save is already a person acting directly, the owner or someone the owner granted write access, so a card would only ask them to approve themselves. The full-replace ban (invariant 5) is about models.

**Why is the editor a whole-body replace with an optimistic token, not operational edits?** One human editor at a time is the common case. `expected_updated_at` (the same token as rename) turns a concurrent write into a 409 the editor can resolve, and every replaced body is snapshotted, so an overwrite is always recoverable from History.

**Why is History for editors only?** A revision can hold text the owner removed before sharing, which a read recipient who could list, read and copy revisions would see and could keep forever. Write recipients can already change the whole doc, so they get the full history, like Google Docs editors; for the same reason a read-only viewer sees only the images the current body references.

**Why does a copy always become a private user doc of the caller?** A recipient cannot write into the owner's project, and the caller is the one who asked for it. A private user doc is taint-safe from any source, so copying out of a public-project doc opens no path for internal content into the public partition.

**Why does an everyone share broadcast to every connected user?** Enumerating the audience per recipient, with each recipient's gates, was rejected because it would cost a user lookup per write. `bus.connected_user_ids()` is an in-process snapshot, the events carry only an id and a timestamp, and the client's follow-up fetch goes through the access rule.

## Known Limitations

- **Account deletion sends no event to recipients.** Their lists refresh on their next `doc_list_changed` or reload. An open viewer of one of the deleted user's docs keeps showing it until it re-fetches (a reload, or any action that hits the server), since no `doc_changed` comes for it; the re-fetch then 404s.
- **The everyone-share broadcast ignores gates and costs one list refetch per connected user per write burst.** A user who cannot open the doc learns only its id and `updated_at`, never content or a title, because the follow-up fetch 404s. Open viewers are not affected: `useDoc` ignores `doc_list_changed`, and only a tab showing that very doc re-fetches on its `doc_changed`.
- **A read-only viewer's row still carries the doc's total `asset_count`.** The detail, asset route and zip are filtered to the images the current body references, but the row (shown in All Docs' size column) counts every image in `assets/`. It reveals a count, never content.
- **Button navigation is not intercepted by the editor guard.** Browser back / forward and `navigate()` from buttons leave the editor without a confirm; the per-user draft backup covers them, but only in that browser, for `DOC_DRAFT_MAX_AGE_MS` or until logout.
- **An asset delete can break older revisions.** `asset_in_use` checks only the current body. A revision that referenced the deleted image shows a missing image in History, and restoring it brings back a broken reference; a History copy skips the missing file.

## Testing

Backend coverage is `tests/test_docs_*.py`, `tests/test_write_doc_action_request.py`, `tests/test_project_doc_sources.py` and `tests/test_spa_docs_routes.py` (`tests/test_docs_access.py` is the table-driven matrix test), and the frontend tests sit beside their files under `frontend/src/` as `*.test.ts(x)`.
