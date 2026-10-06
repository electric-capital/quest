# Quest Docs Architecture

## Overview

A **Quest Doc** is a markdown document that lives entirely inside Quest. It is owned by a user (a **user doc**) or by a project (a **project doc**, owned by the project owner). Each doc is a `docs` row plus a directory holding `doc.md`, embedded raster images and revision snapshots. Models reach docs only through seven purpose-built dynamic tools (list, search, read, create, search/replace edit, append, add image). One access function decides, for each conversation, whether a write is approval-free, needs a `write_doc` action request, or is impossible. The whole feature sits behind the off-by-default, per-user-capable `docs` [feature gate](feature-gates.md). The HTTP surface (`/app/api/docs*`) is documented in [Quest Docs API](../api/quest-docs-api.md), and the view-only UI at `/docs` under [Frontend](#frontend). Google Docs is a separate feature ([Docs API](../api/docs-api.md), `system:docs`).

Docs come in two **modes** that mirror [public projects](public-projects.md). **Private** docs hold internal information; only private conversations can read or write them, and public conversations are never told they exist. **Public** docs hold only content that came from the internet-enabled public sandbox (or that a human typed in the UI). Public conversations read and write them; private conversations may read them but never write, so internal data cannot flow into a doc that a public conversation could later exfiltrate.

## Key Files

| File | Description |
|------|-------------|
| `chat/docs/constants.py` | Every cap and the retention numbers (`DOC_MAX_*`, `DOC_READ_MAX_CHARS`, `DOC_REVISION_RETENTION_DAYS`, `DOC_REVISION_MAX_COUNT`, `DOC_SEARCH_*`), `DOC_MODES`, `DOC_SHARE_PERMISSIONS`, the prompt pseudo-key `DOCS_SERVICE_KEY`, and the two shared texts: `doc_not_found_message()` (THE not-found text) and `docs_disabled_message()` |
| `chat/docs/access.py` | `resolve_doc_access()` (the only place the matrix lives), `DocAccess`, `HIDDEN`, `RUN_KINDS` / `READ_ONLY_RUN_KINDS`, the `DENY_*` reason constants, `APPROVAL_WRITE_NOTE`, `effective_share()`, `write_note()`, `creation_mode()` |
| `chat/docs/files.py` | On-disk layer: `doc_paths()` (via `ChatStorage.get_doc_dir`), the per-doc `doc_lock()`, `init_doc` / `read_body` / `modify_body` / `write_body`, revision snapshot + prune, assets (`sniff_image_type`, `sanitize_asset_name`, `add_asset`, `asset_path`, `read_asset`), `build_zip`, `delete_doc_dir` |
| `chat/docs/service.py` | The one read/write path shared by tools, routes and the action request: `Caller`, the `DocError` / `DocApprovalRequired` / `DocDisabled` / `DocRequestError` errors, `list_docs` / `search_docs` / `read_doc` / `create_doc` / `create_doc_from_ui` / `edit_doc` / `append_to_doc` / `add_doc_image`, `apply_write_operation`, `preview_write_operation`, and the locked UI mutations `switch_doc_mode` / `delete_doc_from_ui` |
| `chat/docs/events.py` | `publish_doc_list_changed()` / `publish_doc_changed()` (best-effort per-user globals, event-loop thread only); the module docstring records who publishes |
| `chat/docs/routes.py` | The `/app/api/docs*` router, mounted in `quest.py`; `_get_doc_for_ui()` adds the hidden-public-project 404 |
| `db/doc_store.py` | Async metadata CRUD returning dicts (`_doc_to_dict` documents the shape), `_title_taken()`, `list_accessible_docs()` (keyset candidates), `update_doc_metadata()` (`StaleDocError`), `set_doc_mode()`, `update_after_write()`, `delete_doc()`, `list_doc_ids_for_project/user()`, and the share helpers `add_share` / `remove_share` / `list_shares` |
| `db/models.py` | `Doc`, `DocShare`, `ActionRequestType.WRITE_DOC` |
| `alembic/versions/55983a10e266_create_docs_and_doc_shares.py` | Creates `docs` + `doc_shares` and their indexes |
| `config/paths.py` | `DOCS_DIR = DATA_DIR / "docs"` |
| `chat/storage.py` | `ChatStorage.get_doc_dir()` (sole id-to-path resolver) and the `doc_reads.json` sidecar (`get_doc_read_ids` / `add_doc_read_ids`) |
| `chat/gemini_api/tool_handlers/docs.py` | The seven tool handlers: argument coercion, gate check, error-shape mapping |
| `chat/gemini_api/tool_dispatch.py` | `ToolContext.run_kind`, `_doc_caller()`, the seven `TOOL_CALL_HANDLERS` entries |
| `chat/llm/tool_schemas.py` | The seven `TOOL_CALL_REGISTRY` specs (`requires_service: "docs"`, writes `mutating`) and their `PUBLIC_TOOL_CALL_ALLOWLIST` entries |
| `chat/gemini_api/script_tool_call.py` | `SCRIPT_TOOL_CALL_ALLOWLIST` gains the three reads; the bridge dispatches with `is_script=True` |
| `chat/action_request_types/write_doc.py` / `doc_precard.py` | The `write_doc` handler and its pre-card check (called from `_handle_create_action_request` in `chat/gemini_api/turn_tools.py`) |
| `chat/action_request_types/_skill_content_edit.py` | `apply_content_edit()` (shared with `edit_skill`; docs pass `max_size`, `noun="doc"` and a `read_doc` re-read hint) and `build_bounded_content_diff()` (the size- and time-bounded card diff `write_doc` uses; `edit_skill` keeps the full `build_content_diff()`) |
| `config/feature_gates.py` | `FEATURE_DOCS`, `docs_enabled_for()` |
| `config/plugins.py` | `validate_plugin()` reserves the `PSEUDO_SERVICE_KEYS` ids (`docs`) |
| `chat/system_skills/loader.py` | `load_system_skills()` returns `docs_disabled_message()` for `system:quest_docs` while the gate is closed |
| `api/instructions.py` | `get_user_connected_services()` sets the `docs` pseudo-key; `PSEUDO_SERVICE_KEYS` |
| `chat/system_skills/catalog.py` | The `system:quest_docs` skill (`_quest_docs_content`) |
| `chat/gemini_api/system_prompt.py` | `_PUBLIC_DOCS_SECTION`, the `docs_enabled` parameter of `get_public_project_system_prompt()`, `_doc_tool_names()` / `_doc_write_tool_names()` |
| `chat/realtime/events.py` | `make_doc_list_changed()` / `make_doc_changed()` |
| `chat/project_routes.py` / `chat/routes/user.py` | Directory sweeps on project delete and account delete |

## Concepts and Invariants

| Term | Meaning |
|------|---------|
| Owner | `docs.owner_id`. A project doc is owned by the project owner. |
| Scope | Derived, never stored: `project` when `project_id` is set, else `user`. |
| Mode | `private` or `public`, stored on every row. A user doc changes mode only through the owner's UI route (`service.switch_doc_mode`). A project doc copies `projects.public` at creation and can never be switched. |
| Share | A `doc_shares` row granting `read` or `write` to one user, or to everyone on the install (`user_id` NULL). |
| Private conversation | Any conversation outside a public project: standalone, private project, routine, Slack, sub-agent, inference API, cross-user subagent. |
| Public conversation | A conversation inside a public project (`is_public` in `run_conversation_turn`). |

Invariants the code keeps:

1. **Taint.** Content enters a public doc only from public conversations or from the UI. A private conversation never gets a non-denied write verdict on a public doc. `creation_mode()` makes every conversation create docs in its own mode, and `create_doc(target="project")` refuses when the project's mode disagrees with the conversation's. The action request re-resolves access at Approve, so a doc switched to public while a card sat open refuses. Switching a user doc from private to public is a deliberate human act through the UI route, not a model path, and it runs under the same per-doc write lock as model writes: a write that passed its access check before the flip either lands first (while the doc is still private) or re-resolves after the flip and is refused. It can never land after the doc became public.
2. **Invisibility.** In every tool, a doc the caller may not see behaves exactly like a nonexistent id. The text is the same (`doc_not_found_message`) and so is the work: `doc_store.get_doc` runs its shares query for a missing id too, so both cost two queries and neither touches the files. Hidden docs never appear in list or search output. Title uniqueness is scoped per mode so that the `create_doc` collision error cannot reveal a private title to a public conversation (see Design Decisions).
3. **Single rule.** Every read/write decision goes through `resolve_doc_access()`. Tools, routes, the pre-card, the approve-time execute, `list_docs` / `search_docs` and the UI `access` object consume its `DocAccess`; none re-derive the rule.
4. **Paths.** `ChatStorage.get_doc_dir()` is the only id-to-path resolver (canonical id + containment check, see [Data Paths](data-paths.md)). `chat/docs/files.py` gets every path through `doc_paths()`.
5. **No full replace.** No model-facing path replaces a whole body in one call. Edits are exact search/replace (`apply_content_edit`), plus append and image add. `files.write_body()` exists for a future UI editor and has no production caller.

## Storage

### On-disk layout

Everything for one doc lives under `DOCS_DIR/<doc_id>/` (layout documented at the top of `chat/docs/files.py`):

| Entry | Content |
|-------|---------|
| `doc.md` | The current body: UTF-8, CRLF normalized to LF, no front matter (all metadata is in the DB, so a download is clean) |
| `assets/<name>.<ext>` | Embedded raster images (png, jpg, gif, webp). The extension always comes from `sniff_image_type()` (magic bytes), never from the caller's file name. The body references them relatively as `![alt](assets/<name>)` |
| `revisions/<YYYYMMDDTHHMMSSZ>-<n>.md` | Snapshots of `doc.md` taken before each body write |
| `doc.meta.json` | Who wrote the current `doc.md` and when: `{written_at, source, size}` (see Revision policy) |
| `revisions/<YYYYMMDDTHHMMSSZ>-<n>.json` | The snapshot's metadata sidecar: a copy of the replaced body's `doc.meta.json` |

Safety rules applied everywhere in `files.py`: leaves are opened `O_NOFOLLOW`, then re-checked as regular files on the descriptor (`fstat` + `S_ISREG`). Reads also pass `O_NONBLOCK`, so a planted FIFO can never block. Body writes are atomic (`doc.md.tmp` + `os.replace`). Assets are written to a temp file and hard-linked into place (`os.link` fails on an existing name, which drives the `-2`, `-3` suffixing). Only `init_doc()` creates a doc root, so a write racing a delete fails instead of resurrecting a directory whose row is gone. A doc directory is never mounted into a sandbox.

Concurrency uses two locks. `files.doc_lock()` is a per-doc `threading.Lock` held for one file call. `modify_body()` reads, transforms and writes `doc.md` in a single critical section, because two separate calls would let concurrent writers clobber each other. The service additionally holds a per-doc `asyncio.Lock` across the whole write (see [Write path](#write-path)); the UI mode switch and delete take the same lock ([UI mutations](#ui-mutations)). Both locks are process-local, and each lock table holds weak values.

### Revision policy

`_write_body_locked()` in `chat/docs/files.py` snapshots the current body before every body write, then prunes:

- **Identical-body writes take no snapshot.** If the new bytes equal the current ones, there is nothing to restore.
- **Pruning at write time** (`_prune_revisions()`) deletes snapshots older than `DOC_REVISION_RETENTION_DAYS` (30, by file mtime). It also deletes the oldest snapshots beyond `DOC_REVISION_MAX_COUNT` (200). The cap bounds the disk a looping writer can use inside the window, since each snapshot can be up to 1 MB.
- **The newest snapshot is always kept**, however old. An idle doc keeps one restore point.

- **Every snapshot gets a metadata sidecar.** Each doc keeps `doc.meta.json` = `{"written_at": <ISO UTC, ms, Z>, "source": <the write source of the CURRENT body: "conversation:<id>" | "ui" | "action_request:<id>" | null>, "size": <bytes>}`, written by `init_doc` and after every body write (`doc.meta.json.tmp` created exclusively with O_NOFOLLOW, then `os.replace`; read with O_NOFOLLOW + S_ISREG). `write_body` / `modify_body` / `init_doc` take `write_source`, which the service fills with the current writer's source. Asset-only writes (`add_asset`, incl. `placement: "none"`) never touch it, so it is NOT the row's `last_write_source`: that field names whoever wrote last, including image adds. Before a body write replaces `doc.md`, the snapshot's sidecar `revisions/<ts>-<n>.json` is a copy of `doc.meta.json`, so `written_at` is when that body was written and `source` who wrote it. Fallback when the meta is missing, unreadable or its `size` disagrees with the body (legacy docs, a body edited by hand): `{"written_at": <doc.md mtime, ISO Z>, "source": null, "size": <bytes>}`. A failed meta write is logged, the stale file removed (the next snapshot then falls back to a null source) and the save still succeeds. Name allocation skips leftover `.json` names. Pruning deletes a `.json` together with its `.md` under both rules and sweeps orphan `.json` files (an undeletable one is logged and skipped; a symlink at a sidecar name is unlinked, not followed). A snapshot without a sidecar is tolerated: `files.read_revision_meta(path)` returns the dict or None (missing, malformed, wrong shape, negative size, or not a regular file); `read_doc_meta(doc_id)` reads the current one. `doc.meta.json` is not an asset and not part of the zip download. This is the attribution the later History view reads; no API or UI exposes it yet.

Creation (`init_doc`) takes no snapshot. Metadata changes (rename, description, mode) do not touch files. An `add_doc_image` with `placement: "none"` stores the asset without writing the body, so it takes no snapshot either. Revisions are not exposed anywhere yet: `files.list_revisions()`, `read_revision_meta()` and `read_doc_meta()` have no production caller.

### Tables

`docs` and `doc_shares` are created by migration `55983a10e266` (models in `db/models.py`, store in `db/doc_store.py`):

- **`docs`** holds the owner (`ON DELETE CASCADE`), the nullable `project_id` (`ON DELETE CASCADE`, NULL = user doc), title (1..200), description (0..500), mode, the cached `content_size` / `asset_count` (for list views), `last_write_source` (`conversation:<id>` from tools, `ui` from routes, `action_request:<id>` from `write_doc`), and `created_at` / `updated_at`. `updated_at` doubles as the optimistic-concurrency token for `PUT /docs/{id}`.
- **`doc_shares`** grants `read` / `write` to a user (`ON DELETE CASCADE`) or, with `user_id` NULL, to everyone on the install. The unique `(doc_id, user_id)` index cannot stop duplicate NULL rows in SQLite, so the partial unique index `ix_doc_shares_everyone` (`WHERE user_id IS NULL`) allows at most one everyone row per doc. `add_share()` upserts and refuses the owner as a recipient (a share row would turn the owner's own writes into approval-gated ones).
- **Title uniqueness** is case-insensitive per **(owner, project, mode)**, enforced by `_title_taken()` in `db/doc_store.py`, not by an index. A NULL `project_id` is its own scope. The comparison uses Python `casefold()` because SQLite `lower()` folds ASCII only. The check runs on create, on rename (`update_doc_metadata`), and on mode switch (`set_doc_mode` raises `DuplicateDocTitleError` when the target mode already has the title; `switch_doc_mode` maps it to `DocRequestError("duplicate_title")` and the route returns 409). It is check-then-insert like the skills store, with no lock against a simultaneous create.

### Read sidecar

`CHATS_DIR/<conversation_id>/doc_reads.json` (`ChatStorage.get_doc_read_ids` / `add_doc_read_ids`) records the docs this conversation has read. It is the read-before-edit gate for `edit_doc` and for `write_doc` with `operation: "edit"`. `read_doc` and `create_doc` feed it; `search_docs` snippets do not count. The service writes it on the event loop (never in a thread), relying on the same single-event-loop guarantee as the workspace and skill sidecars. Sub-agents dispatch with the parent's `conversation_id`, so a sub-agent's `read_doc` counts for the parent. Scripts have no conversation and record nothing.

## Access Rule

`resolve_doc_access(doc, *, user_id, is_public, project_id, run_kind)` in `chat/docs/access.py` returns `DocAccess(visible, can_read, write, deny_reason)`, where `write` is `free`, `approval` or `denied`. `doc` must carry its `shares` list; a dict fetched without shares raises instead of being read as unshared.

The matrix (from the `access.py` module docstring; "recipient" = a share recipient; the last column covers `sub_agent`, `inference_api`, `user_subagent` and `script`):

| Doc | Private owner | Private recipient | Public owner | Public recipient | Read-only run kinds |
|-----|---------------|-------------------|--------------|------------------|---------------------|
| Private, unshared | Free | n/a (Hidden) | Hidden | n/a (Hidden) | Read |
| Private, shared (any) | Approval | read: Read; write: Approval | Hidden | Hidden | Read |
| Public, unshared | Read | n/a (Hidden) | Free | n/a (Hidden) | Read |
| Public, shared | Read | Read | Free | read: Read; write: Free | Read |

The rules layered on the matrix are applied in order:

1. No owner match and no share (the user's own row or the everyone row, higher permission wins in `effective_share()`) -> Hidden.
2. Project docs are visible only from conversations of that same project, and from the UI. Standalone conversations, other projects and scripts see Hidden.
3. A public conversation never sees a private doc, even the owner's, even with a write share.
4. Read-only run kinds read what they can see and never write. Each gets its own `DENY_*` reason.
5. `ui` (the routes): Free for the owner or a write share, else Read. Ownership-only operations are a separate check, not part of the matrix: in the route for rename, in `switch_doc_mode` / `delete_doc_from_ui` for mode switch and delete (`DocRequestError("forbidden")`). Also outside the matrix, `_get_doc_for_ui()` in `chat/docs/routes.py` hides a public project's docs (`project_id` set and `mode == "public"`, since a project doc mirrors its project's flag) while the `public_projects` gate is closed for the user. Every by-id route then 404s with the missing-doc body, matching the project routes' hidden-public-project rule.
6. `slack`: an Approval verdict becomes a denial (`DENY_SLACK_NEEDS_APPROVAL`), because Slack-driven runs cannot open action requests.

`write_note()` turns a verdict into the short `write_note` that `list_docs` / `read_doc` return: none for free, `APPROVAL_WRITE_NOTE` for approval, `deny_reason` otherwise. `tests/test_docs_access.py` covers every cell.

### run_kind derivation

`ToolContext.run_kind` in `chat/gemini_api/tool_dispatch.py` picks the first match: `is_script` -> `script`, `is_sub_agent` -> `sub_agent`, `is_inference_api` -> `inference_api`, `is_user_subagent` -> `user_subagent`, `is_slack` -> `slack`, else `top_level`. Routine runs are `top_level`, and whether the conversation is public is the separate `is_public` field. `run_conversation_turn` passes `is_user_subagent` and `is_slack` (the conversation origin) into `_dispatch_tool_call`. `chat/gemini_api/sub_agent.py` passes `is_sub_agent=True`. The script bridge passes `is_script=True`.

`_doc_caller()` builds the service `Caller` from the context. For scripts it forces `conversation_id=None`, `project_id=None` and `is_public=False`. Other callers build their own: the routes use `run_kind="ui"` with no project and `is_public=False`. The `write_doc` pre-card and execute use `run_kind="top_level"`, with `is_public` re-derived from the project row (`project_is_public()` in `write_doc.py`).

## Service Layer

`chat/docs/service.py` is the only write path. Every operation follows the same pipeline: gate (`require_enabled`, before any DB work) -> lookup -> `resolve_doc_access` -> file op in `asyncio.to_thread` -> `doc_store.update_after_write` -> read sidecar -> events. `DocError` messages are model/user-facing. Unexpected `OSError`s become a generic message that names no path.

### Read side

- `list_docs`: candidates come from `doc_store.list_accessible_docs()` (owner or share, keyset-paged on `(updated_at, id)` descending). Every row still passes the access rule, and the store is paged past hidden rows so they never use up `limit`. A public caller adds a SQL `mode = 'public'` filter. Scope `project` is empty outside a project. Rows carry a `shared` flag but never the share roster.
- `search_docs`: case-insensitive substring match on title, description and body. Bodies of visible docs are read newest-first until the next one would push the scanned total past `DOC_SEARCH_MAX_SCAN_BYTES` (50 MB). After that, only titles and descriptions are matched and `truncated` is true. Up to `DOC_SEARCH_SNIPPETS_PER_DOC` non-overlapping ~200-char snippets per doc, with 1-based line numbers. A metadata-only hit has `matches: []`.
- `read_doc`: the whole body or a 1-based inclusive line range, capped at `DOC_READ_MAX_CHARS` and cut at a line boundary. A cut sets `truncated` and a paging `note`. Lines split on LF only, so they agree with search's line numbers. Records the read.

### Create

`create_doc` (conversations) lays out the directory under a fresh uuid, then inserts the row. A failed insert removes the directory again; a crash in between leaves an invisible orphan directory, never a row without a body. Only `top_level` and `slack` callers may create; creation is never gated because a new doc has no shares. The new id is recorded as read. `create_doc_from_ui` (`POST /docs`) creates an empty doc with an explicit mode (a project doc always takes its project's mode) and `last_write_source="ui"`.

### Write path

`apply_write_operation(caller, doc_id, operation, params, *, write_source, bypass_approval=False)` serves `edit` / `append` / `add_image` for both the tools and the action request:

1. **First pass, outside any lock** (`_resolve_write`): gate, parameter normalization, visibility (hidden == missing), `denied` -> `deny_reason`, then the read-sidecar check for `edit` and the conversation requirement for `add_image`. Hidden, denied or unread docs never take or create a lock.
2. **Approval**: unless `bypass_approval`, an `approval` verdict first dry-runs the operation (`_compute_preview`), so a stale `old_string`, a bad image or an oversized body fails now. It then raises `DocApprovalRequired` carrying `suggested_request`.
3. **Locked write**: under the per-doc `asyncio.Lock`, access is resolved again, so a share added or a mode flipped while this write queued changes the verdict. The writer then runs (`files.modify_body` for edit and append; `add_asset` followed by an optional body append for images), and `update_after_write` bumps `content_size` / `asset_count` / `last_write_source` / `updated_at` in write order. Finally `doc_changed` + `doc_list_changed` go to the owner.
4. **Shielded**: the locked part runs via `_shielded()`, so a cancelled turn (Stop mid-write) cannot strand a landed file write without its DB bump and events. A late failure is logged.

`add_image` reads the workspace file through `resolve_workspace_file()` (path guards) plus an `O_NOFOLLOW` / `S_ISREG` descriptor read capped at `DOC_MAX_IMAGE_SIZE`, then validates it by magic bytes. With `placement: "append"` the body cap is pre-checked using the provisional asset name before the asset is stored. If the body append still fails, the asset counter is recorded anyway (assets are additive) and the error is raised. When the params carry `expected_sha256` (set only by the `write_doc` execute, from the card's `image_preview.sha256`; `_suggested_request()` never emits it), a file whose sha256 differs is refused. A project workspace is shared by sibling conversations, which could swap the file while the card sat open.

### UI mutations

`switch_doc_mode(user, doc_id, mode)` and `delete_doc_from_ui(user, doc_id)` serve `PUT /docs/{id}/mode` and `DELETE /docs/{id}`. Each resolves visibility as `ui`, then checks ownership (`forbidden`). `switch_doc_mode` also checks the mode (`invalid_mode`), refuses project docs (`project_doc_mode_inherited`) and returns the doc unchanged when it already has that mode. The mutation itself (`doc_store.set_doc_mode`, or `doc_store.delete_doc` plus the directory removal) runs under the per-doc `asyncio.Lock` and `_shielded()`, so it waits for any in-flight model write. The mode switch publishes `doc_changed` + `doc_list_changed`; delete publishes `doc_list_changed`. Rename stays a plain metadata update in the route (`doc_store.update_doc_metadata`).

## Model-Facing Tools

The seven tools are `TOOL_CALL_REGISTRY` entries in `chat/llm/tool_schemas.py` (dispatched through `tool_call`). Each carries `requires_service: "docs"`, and the four writes are marked `"mutating": True`. Their descriptions are written to stand alone, because the public prompt carries no skills. Handlers are thin wrappers in `chat/gemini_api/tool_handlers/docs.py`.

| Tool | Available to | Success result |
|------|--------------|----------------|
| `list_docs(scope?, limit?)` | every tier incl. scripts | `{docs: [{id, title, description, mode, scope, project_id, content_size, asset_count, updated_at, shared, writable, write_note}], count}` |
| `search_docs(query, scope?, limit?)` | every tier incl. scripts | `{results: [{id, title, mode, scope, matches: [{line, snippet}]}], truncated}` |
| `read_doc(doc_id, start_line?, end_line?)` | every tier incl. scripts | `{id, title, mode, scope, total_lines, content, shared, writable, write_note, start_line, end_line, truncated, note?}` |
| `create_doc(title, content, description?, target?)` | top-level conversations (private, public, routine) and Slack | `{id, title, mode, scope, project_id, content_size, updated_at}` |
| `edit_doc(doc_id, old_string, new_string, replace_all?)` | top-level conversations (private, public, routine) and Slack | `{replaced, total_lines, updated_at}` |
| `append_to_doc(doc_id, content, ensure_blank_line?)` | top-level conversations (private, public, routine) and Slack | `{appended_lines, total_lines, updated_at}` |
| `add_doc_image(doc_id, workspace_path, alt?, placement?)` | top-level conversations (private, public, routine) and Slack | `{asset, markdown, appended, asset_count, updated_at, total_lines?}` |

Error results come in three shapes:

- `{"error": "docs_disabled", "message"}` when the gate is closed (checked before anything else).
- `{"error": "approval_required", "message", "suggested_request": {"request_type": "write_doc", "params": {"operation", "doc_id", ...}}}` for a shared private doc. The model forwards `suggested_request` unchanged through `create_action_request`. The `params` sets are pinned to what `write_doc.validate_params` accepts (edit: `old_string`, `new_string`, `replace_all`; append: `content`, `ensure_blank_line`; add_image: `workspace_path`, `alt`, `placement`).
- `{"error": "<text>"}` for every other refusal: missing or hidden doc (the one not-found text), a denied write (`deny_reason`), an unread doc, a failed match, bad arguments, caps.

Enforcement outside the access rule:

- **Public conversations**: all seven are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private doc there, and public conversations only ever get `free` or `denied` on public docs, so the blocked `create_action_request` is never needed.
- **Inference API runs**: the `mutating` flag puts the four writes in `mutating_tool_call_tools()`, which is hard-rejected at dispatch and refused for the run's sandbox lease (see [Inference API](../api/inference-api.md)). The access rule would deny them anyway.
- **Sub-agents and cross-user subagents**: `get_sub_agent_system_prompt()` and `get_user_subagent_system_prompt()` drop `_doc_write_tool_names()` from their Dynamic Tools section. If called anyway, `create_doc` refuses through `_CREATE_DENY_REASONS` and the other writes through the access rule.

## The write_doc Action Request

`WriteDocHandler` (`chat/action_request_types/write_doc.py`, registered in `chat/action_request_types/__init__.py`) is the approval form of the three write tools. Its single type takes an `operation` of `edit`, `append` or `add_image`. There is no action-request form of `create_doc`. The pipeline follows `edit_skill`:

- **`validate_params`**: shape only. It rejects unknown keys and keys belonging to another operation, fills the optional defaults, and drops any model-supplied copy of the server-injected keys (`SERVER_INJECTED_KEYS`).
- **Pre-card** (`doc_precard_check()` in `doc_precard.py`, called from the `create_action_request` arm in `turn_tools.py` after the skill and routine pre-cards): delegates to `service.preview_write_operation()`, which applies the same access, read-sidecar, live-body dry-run and cap checks as the tool. A `free` verdict is rejected ("call the tool instead"), and a denied, hidden, unread, stale or gate-closed case gets the service's own text verbatim. All rejections surface as same-turn `Invalid parameters` with no card. On `approval` it injects `current_title`, `doc_mode`, `doc_scope`, `share_summary` (counts only, never names), `content_diff` (for `add_image` only when the body changes) and, for `add_image`, `image_preview` `{workspace_path, asset_name, markdown, size_bytes, sha256}`.
- **Card diff**: `content_diff` is `build_bounded_content_diff()` of the live body vs the result, computed in `asyncio.to_thread`. The full-body `build_content_diff()` that `edit_skill` uses is quadratic on a 1 MB doc. The bounded version:
  - trims the common prefix and suffix lines first;
  - runs `SequenceMatcher` only on the changed middle, falling back to a plain removed-then-added listing when the middle exceeds 5000 lines;
  - emits 3 context lines around the change;
  - caps the output at 400 lines (`truncated: true`; the `added` / `removed` counts stay exact);
  - adds `total_old_lines` / `total_new_lines` to the usual `{added, removed, lines}` dict.

  Line numbers match `read_doc`'s. `lines` is therefore NOT the whole body; the card renderer marks the rest from the line numbers and the two totals (see [Frontend -- write_doc card](#write_doc-card)).
- **`render_preview`**: Doc / Mode / Scope / Shares / Operation rows. The diff reuses the `skill_content_diff` field type, rendered by `SkillContentDiffPreview.tsx`. `add_image` adds a `doc_image`-typed Image field carrying `{workspace_path, asset_name, markdown, size_bytes}`, rendered by `DocImagePreview.tsx`. Without an injected diff, edit and append fall back to raw Replace / With / Append rows.
- **`execute`**: `chat/action_request_routes.py` passes `request_id=` to this one handler type. `execute` calls `apply_write_operation(..., bypass_approval=True, write_source="action_request:<id>")`, passing the card's `image_preview.sha256` as `expected_sha256` for `add_image`, so a workspace file swapped after the proposal is refused. This re-resolves access, so a doc that became public or hidden refuses and a doc whose shares were all removed is simply written. It also re-checks the read sidecar for `edit` and re-applies against the live body; that is the TOCTOU close, so a changed `old_string` fails the Approve and the request stays open. The service publishes the realtime events itself.
- **Fail closed on a missing project**: `project_is_public()` raises `RuntimeError` when the conversation has a `project_id` but `project_store.get_project` finds no row for the user, rather than treating the conversation as private. At Approve this surfaces as the resolve route's 500 `execution_failed` with the request left open. At proposal time the same error is not a `ValueError`, so it escapes the pre-card as a run-fatal error instead of `Invalid parameters`.
- **Labels**: `display_name` "Write Doc", `approve_label` "Apply", `resolved_label` "Applied", and a neutral `summary_snippet` ("Edit 'Title'", "Append to 'Title'", "Add image to 'Title'") because the snippet also shows on stopped and denied cards.

## Feature Gate

`FEATURE_DOCS = "docs"` (label "Quest Docs") is in `KNOWN_FEATURES` and `PER_USER_ACCESS_FEATURES`, off by default. Every enforcement point calls `docs_enabled_for(email)`. While the gate is closed for a user:

- **Tools**: every doc tool returns `docs_disabled` before any DB work. Dispatch still routes them (they are allowlisted in public conversations), so a routine that calls them gets the same structured error.
- **Prompt**: `get_user_connected_services()` sets the pseudo-key `docs` (`DOCS_SERVICE_KEY`) from the gate. Registry specs with `requires_service: "docs"` and the `system:quest_docs` skill (`requires="docs"`) then drop out like a disconnected service. An explicit `load_skills(["system:quest_docs"])` gets `docs_disabled_message()` (Settings > Features) instead of the connector text (`load_system_skills()` in `chat/system_skills/loader.py`). `validate_plugin()` in `config/plugins.py` refuses a plugin whose id is a pseudo-key, because the gate would overwrite that plugin's connected state. `PSEUDO_SERVICE_KEYS` in `api/instructions.py` marks the key as a capability rather than a connection, and `GET /me`'s `has_any_service_connected` skips it. The public prompt has no connected-services map, so `run_conversation_turn` passes `docs_enabled=docs_enabled_for(...)` to `get_public_project_system_prompt()`.
- **Routes**: every `/app/api/docs*` route returns 403 `docs_disabled` first.
- **Frontend**: reads the gate from `enabled_features` on `GET /me` (`DOCS_FEATURE` in `frontend/src/api/docsApi.ts`). The Sidebar Docs blocks are not rendered, the doc hooks fetch nothing (`enabled: false`), and a `/docs*` URL shows `DocsGateClosed.tsx` instead of the view (see [Frontend](#frontend)).
- **Action request**: the pre-card and the approve-time execute both refuse through the service gate.
- Nothing is deleted. Rows and files survive, and reopening the gate restores everything.

## Prompting

- **`system:quest_docs`** (name "Quest Docs"; `system:docs` is the Google Docs skill) covers when a doc beats a workspace file, the tool set, modes and verdicts (quoting the `DENY_*` constants), read-before-edit, the `approval_required` -> `write_doc` handoff with the three `params` shapes, the Slack limitation, images, paging, the routine pattern (one doc, `append_to_doc` under a dated heading per run, never a new doc per run), and the script bridge. Caps in the text are rendered from `chat/docs/constants.py`.
- **Public prompt**: `_PUBLIC_DOCS_SECTION` follows the Boundaries block only while `docs_enabled`, and stands in for the skill. It may not name internal-only tools or skills (`tests/test_public_projects.py::test_no_internal_tool_docs`), which is why the write-tool descriptions say "write_doc action request" rather than spelling out `create_action_request(`.
- No doc titles are ever injected into a prompt. Discovery is tools-only.
- `system:action_requests` and the `create_action_request` description route `write_doc` to `system:quest_docs`.

## Realtime

Two per-user globals ([Realtime](realtime.md)), built by `make_doc_list_changed()` / `make_doc_changed()` in `chat/realtime/events.py` and published best-effort through `chat/docs/events.py`, always to the doc's **owner** (share-recipient fan-out is not implemented):

- `doc_list_changed` (no payload) after create, rename/description change, mode switch, delete, every body or asset write, and a project delete that removed docs.
- `doc_changed {doc_id, updated_at}` after create and every body/asset write, rename and mode switch. Doc delete emits only `doc_list_changed`.

Who publishes (recorded in the `chat/docs/events.py` docstring):

- `chat/docs/service.py` publishes for every body/asset write and for the UI mutations it owns (create, mode switch, delete);
- the doc routes publish for the metadata-only rename, and only when something changed;
- the project-delete route publishes `doc_list_changed` once, after its directory sweep;
- tools never publish.

Publishing is event-loop-thread only, because `bus.publish_to_user` is not thread-safe. Neither envelope carries a `conversation_id`, so they reach `PersistentWebSocket`'s global handlers without an allow-list entry. The frontend consumers are `useDocs`, `useProjectDocsIndex` and `useDoc` (see [Frontend -- Realtime consumers](#realtime-consumers)).

Because only the owner receives the events, a share recipient's open sidebar, All Docs view or viewer does not update live when the owner or a model writes the doc; it catches up on the next mount or any refresh its own `doc_list_changed` triggers. The frontend cannot fix this: it needs the share-recipient fan-out listed under [Not Implemented](#not-implemented).

## Frontend

A view-only UI: browse, read, create empty docs, rename, switch mode, download and delete. Content changes still come only from models (tools and `write_doc`). The only backend change it needed is the SPA route plus the Swagger UI move it forced. Paths below are under `frontend/src/` unless noted.

| File | Role |
|------|------|
| `api/docsApi.ts` | Client for `/app/api/docs*`, the cookie-authed URL builders (`docAssetBase`, `docAssetUrl`, `docDownloadUrl`), `DOCS_FEATURE`, and `isStaleUpdateError` / `staleUpdateCurrent` for the flat 409 |
| `api/types.ts` | `Doc`, `DocDetail`, `ListDocsResponse`, `DocImagePreview`; `PreviewField.image` and the optional `truncated` / `total_old_lines` / `total_new_lines` on `SkillContentDiff` |
| `utils/docsRoute.ts` | `parseDocsRoute()`, `docsListPath()`, `docViewerPath()` |
| `hooks/useDocs.ts` | One keyset-paged list (the user's docs, or one project's) with `loadMore` and a silent `refresh` |
| `hooks/useProjectDocsIndex.ts` | Per-project fan-out for the unfiltered All Docs view |
| `hooks/useDoc.ts` | One doc (row + body) for the viewer, `notFound`, `applyRow()` |
| `components/sidebar/DocsSection.tsx` + `utils/sidebarDocs.ts` | The sidebar Docs block and its pure row-order / count / time helpers |
| `components/docs/DocsListView.tsx` + `utils/allDocsGrouping.ts` | The All Docs view and its pure search / grouping / scope-label / size helpers |
| `components/docs/NewDocModal.tsx` | Creates an empty doc (`POST /docs`) |
| `components/docs/DocViewer.tsx`, `DocHeader.tsx`, `DocModeSwitchDialog.tsx` | The viewer, its title unit and menu, and the mode-switch dialog plus the shared `DocConfirmDialog` (also used for the Delete confirm) |
| `components/docs/DocModeBadge.tsx` | Private / Public pill on sidebar rows, All Docs rows and the viewer header |
| `components/docs/DocsGateClosed.tsx` | Notice on a `/docs*` URL while the gate is closed |
| `components/Message.tsx` | `MarkdownWorkspaceContext.assetBase` and the doc branch of `MarkdownImage` |
| `components/ActionRequestPreviewFields.tsx`, `DocImagePreview.tsx`, `SkillContentDiffPreview.tsx` | `write_doc` card rendering |
| `App.tsx`, `components/MobileShell.tsx`, `components/Sidebar.tsx`, `components/sidebar/ProjectPanel.tsx` | Route rendering and sidebar mounting |
| `quest.py` (repo root) | `serve_spa_docs` and the moved Swagger UI / ReDoc URLs |

### Routes and state

| URL | View |
|-----|------|
| `/docs` | All Docs |
| `/docs?project=<id>` | All Docs filtered to one project |
| `/docs/<id>` | Doc viewer |

The URL is the only state. There is no `NavigationContext` field: `App.tsx` and the Sidebar run `parseDocsRoute(location.pathname, location.search)` on every render. A viewer id must match `[A-Za-z0-9_-]{1,64}`, so a decoded `/` or `..` is never spliced into an `/app/api/docs/<id>/...` URL. Paths deeper than `/docs/<id>` are not docs routes. A docs route is a full main-pane takeover like the `/inbox` RequestsView: `AppContent` renders the view inside `.main-content.docs-main` with no RightPanel, and `activeConversationId` is null there, so no conversation row stays highlighted. `showRequestsView` (the legacy `NavigationContext` flag behind `/inbox`) still wins the render, so the Sidebar clears it before navigating to a docs view.

On the server, `serve_spa_docs` in `quest.py` serves `index.html` for `/docs` and `/docs/{rest:path}`, registered before the `/{filename}` static fallback. FastAPI's interactive docs moved out of the way: `FastAPI(...)` sets `docs_url="/api-docs"` (Swagger UI), `redoc_url="/api-redoc"` and `swagger_ui_oauth2_redirect_url="/api-docs/oauth2-redirect"`, so no non-SPA route is left under `/docs/`. `/openapi.json` is unchanged, and the `run.py` banner prints `/api-docs`.

### Sidebar

`Sidebar.tsx` mounts two `useDocs` instances with limit `SIDEBAR_DOC_LIMIT` (5): the user's docs, and the drilled project's docs (enabled only while drilled). Both render through `DocsSection`, only while the gate is open:

- on the main panel, between Projects and Conversations, with a header that opens `/docs`;
- in `ProjectPanel`'s drill-down, below Routines (order: Routines, Docs, Conversations), with a header that opens `/docs?project=<id>`.

The whole header is a button with a count ("5+" when the server has more). Rows show the title, a small `DocModeBadge` and a compact time, newest `updated_at` first (`deriveSidebarDocItems`). The doc open in the viewer is highlighted, from the URL. The empty state reads "Ask Quest to create a doc". After navigating, the Sidebar calls its `onNavigateAway` prop, which `MobileShell` sets to close the phone drawer; the desktop layout passes nothing.

### All Docs view

`DocsListView.tsx` shows a header (title "All Docs", or the project's name with a folder icon, a public badge and an "All docs" back link when filtered), a search box, a New Doc button, and grouped rows: title + description, mode badge, scope, relative update time (absolute on hover), and size + image count. The paged group gets a "Load more" footer. Search filters everything loaded, client-side, on title and description.

The backend has no cross-project list: `GET /docs` without `project_id` returns only user docs (owned or shared). So the unfiltered view combines two sources:

- "Your docs": `useDocs({limit: 50})`, server-paged. A doc shared with the user shows the scope "Shared with you" (`docScopeLabel`).
- One group per project (archived ones included, with an "Archived" chip on the heading: archiving has no effect on docs) from `useProjectDocsIndex`. It runs `fetchDocs({projectId, limit: 200})` for every project in parallel (`Promise.allSettled`) once the project list has loaded. A failed project's group is omitted and a single warning line shows. The fan-out is keyed on the sorted project-id set, so a `ProjectsContext` reload or rename does not re-fetch, and a changed set keeps the previous groups on screen while it re-fans-out (`loaded`). `doc_list_changed` re-runs it behind a 600 ms trailing debounce, so a burst of model writes costs one fan-out. Groups follow "Your docs", ordered by project name (`groupDocs`).

The filtered view (`?project=<id>`) uses only `useDocs({projectId, limit: 50})`. The trade-off of the fan-out is one request per project on open and per debounced `doc_list_changed` burst, and at most 200 docs per project in the unfiltered view (the filtered view pages). Between 769px and 1024px the Scope and Size columns are hidden so the title keeps room.

`NewDocModal.tsx` collects a title, an optional description and a location ("Your docs" or a non-archived project, preselected to the filtered project). A user doc gets a mode radio. A project doc shows its project's mode read-only and sends no `mode`, which avoids `project_doc_mode_inherited`. On success the view navigates to the new doc's viewer.

### Viewer

`DocViewer.tsx` (`useDoc`) has loading, not-found ("This doc no longer exists." with an All docs link; also what a deleted doc turns into), error-with-Retry and empty-doc states. The doc renders in a centered column (820px max).

`DocHeader.tsx` follows `ConversationHeader.tsx`: the title plus a chevron opens a dropdown, single-key hints act while it is open, and Rename swaps in an inline input. The items follow the row's `access` flags:

- **Rename** (R, `can_rename`): sends `expected_updated_at`. A `stale_update` 409 applies the error's `current` row through `useDoc.applyRow()` and shows a notice. `duplicate_title` / `invalid_title` keep the input open.
- **Switch to public / private** (P, `can_switch_mode`, so user docs only, and only while the `public_projects` gate is open for the user or the doc is already public -- see Private-only below): opens `DocModeSwitchDialog`.
- **Download Markdown** and **Download with images (.zip)**: plain `<a download>` links on `docDownloadUrl`, always offered.
- **Delete** (D, `can_delete`): behind a confirm, then navigates to the doc's list (`docsListPath(project_id)`).

A non-owner therefore gets only the downloads. A marked comment in the menu reserves the slot for the Share, History and Edit items that are not built. Beside the title sit the `DocModeBadge` and, for a project doc, a folder chip linking to `/docs?project=<id>`. On the right, a Show source / Show rendered toggle (remembered per doc id) swaps the body for a `<pre>` of the raw markdown.

`DocModeSwitchDialog.tsx` confirms before `PUT /docs/{id}/mode`. Private -> public shows the spec's warning text verbatim, plus a sentence that write-share recipients lose the approval gate when `shares` holds a write share. Public -> private is a plain confirm. `duplicate_title` shows inside the dialog.

The body is `ReactMarkdown` with the same plugin set as `FileViewerModal` and the chat's shared `markdownComponents`, inside `.message-content` for the chat typography, wrapped in `MarkdownWorkspaceContext.Provider` with `assetBase = docAssetBase(id)`. With `assetBase` set, `MarkdownImage` changes its resolution rule:

- only `assets/<name>` resolves (exactly one segment, no leading dot, after the usual percent-decode and `./` / `/` / `workspace/` strip), to the cookie-authed `GET /app/api/docs/{id}/assets/{name}`;
- any other relative src renders the missing-image chip, and `conversationId` is ignored;
- external srcs stay click-through links and are never auto-fetched, as in chat;
- SVG never renders, because the asset store holds only magic-byte-sniffed raster images and the asset route serves only the raster `_INLINE_IMAGE_MIMES` types.

The viewer passes no `onOpenImage`, so doc images are not click-to-enlarge.

The footer reads "Last written by X · Updated <relative>", with X from `last_write_source`:

- `ui`: "you";
- `action_request:<n>`: "action request #n";
- `conversation:<id>`: the conversation's title from the row's `last_write_conversation` (`{id, title, project_id}`, resolved by `GET /docs/{id}` from one conversations-row lookup -- the viewer never fetches the chat history for a title), linked to `/chats/<id>` or `/projects/<pid>/<id>`; null (the conversation is gone) reads "a deleted conversation";
- null: the writer phrase is left out. That includes every non-owner, since the API blanks the field for them.

### Private-only without public projects

Public docs exist for public-project conversations, so a user for whom the `public_projects` gate is closed (`_public_projects_enabled_for(user)`, the same per-user check the project routes use) gets private docs only: `POST /docs` creates user docs private and refuses an explicit `mode: "public"` with 400 `public_projects_disabled`, `PUT /docs/{id}/mode` refuses a public target with the same 400 (a private target stays allowed, so a public doc left over from before the gate closed still has a way back), and `access.can_switch_mode` is false unless the doc is already public. The frontend follows the same signal (`public_projects` absent from `enabled_features`, `utils/docMode.ts`): no mode radio in New Doc (no `mode` sent; the read-only "inherited from the project" line is also dropped for a private project), no switch item (the server flag drives it), and no mode badge on private docs -- in the sidebar rows, All Docs rows (the "Mode" column label is blank when no visible row shows a badge) and the viewer header. A public doc always keeps its badge, so a public state is never hidden (`shouldShowDocModeBadge(mode, publicProjectsEnabled)` = `mode === "public" || publicProjectsEnabled`).

### Assets panel

`GET /docs/{id}` lists the doc's images as `assets: [{name, size, mime}]` (regular files directly in `assets/`, sorted by name, hidden names / symlinks / directories skipped, names outside `[A-Za-z0-9._-]+` -- all `add_asset` ever produces -- skipped so a hand-planted name can never break the JSON or the asset route's headers, MIME from `_INLINE_IMAGE_MIMES` by extension, built in a thread). It rides on the detail payload rather than a separate endpoint because the per-doc cap is 200 assets (a few KB at most) and the viewer always needs both. `DocAssetsPanel.tsx` renders them as a `.right-panel-card` in a 280px right gutter beside the doc column (the chat RightPanel's footprint): a lazy 40px thumbnail from `GET /docs/{id}/assets/{name}` (an icon when it fails), the filename and size, "No images in this doc." when empty; a click opens `DocImageLightbox.tsx` (ModalShell, the full image, name + size, a Download link). At 1024px and below the card drops under the body; on phones (`useIsMobile`) a collapsed "Assets (N)" `<details>` section replaces it so the document stays first.

### write_doc card

Both card renderers (`ActionRequestMessage.tsx` and `RequestsView.tsx`) go through `ActionRequestPreviewFields.tsx`:

- **`doc_image` field**: `DocImagePreview.tsx`. At card time the image is still a workspace file of the proposing conversation (project conversations share the project workspace, so the card's conversation id always resolves it), so the lazy thumbnail loads from `GET /conversations/{id}/files/download`. A load failure falls back to a path chip, and a click opens `FileViewerModal`. The caption shows the asset name, the size and "Stored as `assets/<name>`". The markdown line is labeled "Appended:", or "Markdown:" for `placement: "none"` (read from the request params).
- **Bounded `skill_content_diff`**: `SkillContentDiffPreview.tsx` detects the window by the presence of `total_old_lines` / `total_new_lines`. It draws "N lines above / below not shown" edge separators computed from the first and last emitted line numbers and the totals. The toggle reads "Show context lines", since expanding can only reveal the window's own context lines. A `truncated` diff adds a note that the +/- counts are still exact. Whole-body diffs (`edit_skill`, `edit_routine`; no totals) render as before.

The Mode row stays plain text. The resolved "Applied" label and the snippet come from the server, so they need no frontend code.

### Realtime consumers

All three subscribe through `persistentWebSocket.onGlobalEvent`, only while enabled:

- `useDocs`: a silent `refresh()` of the loaded window (one request, `limit = min(200, max(page size, loaded))`) on every `doc_list_changed`.
- `useProjectDocsIndex`: re-runs the fan-out on every `doc_list_changed`. A project whose refresh fails keeps the rows it had.
- `useDoc`: re-fetches on `doc_changed` for its doc when `updated_at` differs from the one shown, and on every `doc_list_changed`. The second case is how a delete (which sends only `doc_list_changed`) reaches an open viewer: the re-fetch 404s and the viewer shows `notFound`.

The owner-only delivery limit is described under [Realtime](#realtime).

### Phone

`MobileShell.tsx` receives the parsed `docsRoute` and `docsEnabled` from `App.tsx` and renders the same three views (or `DocsGateClosed`) in `.mobile-main`. It closes the nav drawer and the workspace drawer whenever the docs route changes, and shows no workspace button on docs routes. The views use 16px gutters at phone width, and All Docs rows stack.

## Lifecycle

- **Doc delete** (`DELETE /docs/{id}`, owner only, `service.delete_doc_from_ui` under the per-doc write lock): row first (shares go with it), then a best-effort `delete_doc_dir()`.
- **Project delete** (`delete_user_project` in `chat/project_routes.py`): `list_doc_ids_for_project()` runs before the project row goes. The `docs.project_id` CASCADE removes the rows, then `_delete_doc_dirs()` sweeps the directories (failures logged, never raises) and publishes `doc_list_changed`. The sweep runs BEFORE the unguarded conversation-directory `rmtree` loop, so a failure there cannot leave doc files behind.
- **Account delete** (`delete_account` in `chat/routes/user.py`): `list_doc_ids_for_user()` (user and project docs alike) runs before any row delete. The directories are swept in a thread after the cascades and before the unguarded conversation/project `rmtree` loops, for the same reason. Share rows granted to the deleted user cascade away; an everyone row on another user's doc survives.
- **Project archived**: no effect. **Conversation deleted**: no effect, and `last_write_source` becomes a dead reference. **Convert chat to project / duplicate workspace**: user docs stay user docs.
- Orphan directories (a crash between layout and insert, or a failed best-effort sweep) are not swept by anything; they are invisible because no row points at them.

## Script Bridge

Sandbox scripts (`run_script` / `run_python` through `POST /api/tool-call`, see [Gemini API -- Script tool-call bridge](gemini-api.md)) get the three reads only (`SCRIPT_TOOL_CALL_ALLOWLIST`). The bridge dispatches with `is_script=True` -> `run_kind="script"`, and `_doc_caller()` gives scripts no conversation, no project and `is_public=False`. As a result:

- scripts see only user docs (project docs are Hidden);
- every write is denied (`DENY_SCRIPT`);
- reads are not recorded in any sidecar;
- scripts are always private callers.

The last point is safe because public containers get no sandbox token, so they have no bridge at all. The doc routes live under `/app/api`, outside both `curl_proxy_*` reach (which is limited to `/api/*`) and the sandbox port's route roster, so neither the model nor a script can use the `ui` run kind.

## Constraints

- Caps (`chat/docs/constants.py`): body 1 MB, image 5 MB, 200 images / 100 MB per doc, `read_doc` page 200,000 chars, title 200 / description 500 chars, search scan 50 MB, 3 snippets of ~200 chars per doc, `list_docs` limit 1..200 (default 50), `search_docs` limit 1..50 (default 20).
- Raster images only (png, jpg/jpeg, gif, webp), sniffed by content. SVG and anything else is refused.
- The write locks are in-process (one `asyncio.Lock` and one `threading.Lock` per doc id), which is correct for the single-process server.
- Doc bodies, assets, revisions and the `doc.meta.json` / sidecar attribution files are plaintext on disk, like workspaces (see [Encryption at Rest](encryption-at-rest.md)).

## Design Decisions

**Why scope title uniqueness per mode?** If private and public user docs shared one title namespace, a public conversation could probe `create_doc(title=...)` and learn from the collision error whether a private doc with that title exists. That would break invisibility. With per-mode scoping, a public conversation only ever collides with public titles. The cost is that a mode switch can collide, which is why `PUT /docs/{id}/mode` returns 409 `duplicate_title`.

**Why does a shared private doc need approval, but a shared public doc does not?** The approval card protects internal data that has several stakeholders. A public doc only ever holds sandbox-originated content, every writer is itself a credential-less public conversation, and the recipients opted into `write`. Consequence: switching a shared private doc to public removes the approval gate for its write-share recipients.

**Why one access function?** The matrix has enough cells (mode x relationship x conversation kind x run kind) that re-deriving any part of it at a call site would drift. Tools, routes, the pre-card, the execute and the prompt all consume one `DocAccess`, and one table-driven test pins it.

**Why are sub-agents, inference runs, cross-user subagents and scripts read-only?** Writes stay with the run that owns the conversation and its approval surface. Sub-agents cannot open action requests, so they return content to the top-level agent, which writes it. Inference and cross-user subagent runs must not change anything outside their own workspace, the same rule that refuses the other mutating tools there. Scripts have no conversation context to scope or attribute a write to.

**Why search/replace + append instead of full replace?** A full replace lets a model silently drop content it never read. An exact match keeps every change local and reviewable (the same diff powers the approval card), and append covers the main aggregation use case without a read.

**Why files on disk instead of DB rows?** A doc is a portable artifact: `doc.md` plus `assets/` downloads as-is (md or zip) with working relative image links, and revisions are plain files.

**Why move Swagger UI instead of picking another SPA path?** `/docs` and `/docs/<id>` are the user-facing deep links. FastAPI registers its docs routes inside the constructor, ahead of every `@app.get`, so the default `docs_url` would always shadow the SPA route. Nothing but the `run.py` banner pointed at Swagger UI, and `/openapi.json` stays where it was.

**Why is the URL the only docs state?** Deep links, reload and back/forward then work without a URL-to-context sync effect. `showRequestsView` behind `/inbox` is the legacy exception that the docs routes deliberately did not copy.

**Why does All Docs fan out per project?** The list endpoint returns either user docs or one project's docs, and the UI was built without backend changes beyond the SPA route. One `fetchDocs` per project reuses the existing endpoint and its access rule, and lets one failing project degrade to a missing group instead of failing the view. A cross-project list endpoint would replace `useProjectDocsIndex` without changing `DocsListView`'s grouping.

## Not Implemented

These are designed for but not built:

- **Sharing routes/UI**: `doc_store.add_share` / `remove_share` / `list_shares` exist, but no route or tool creates shares. Today the approval path and every share cell of the matrix are reachable only through rows written directly. Share-recipient event fan-out (see [Realtime](#realtime)) and a separate shared-with-me group in All Docs are part of the same work (today a shared user doc sits in "Your docs" with the scope "Shared with you"), as is the viewer's Share menu item (its slot is reserved by a comment in `DocHeader.tsx`).
- **Revisions route** (`GET /docs/{id}/revisions`), restore, and the viewer's History item. Snapshots are written and pruned but not served.
- **Human editing** (`PUT /docs/{id}/content` with `expected_updated_at`, image upload into `assets/`) and the viewer's Edit item. `files.write_body()` is the primitive this will use. The UI also never edits a description, although `PUT /docs/{id}` accepts one.
- **FTS5** behind `search_docs`. Search is the bounded scan above.

## Testing

`tests/test_docs_access.py` (every matrix cell and edge rule), `tests/test_docs_store.py` (metadata store, title scoping, shares), `tests/test_docs_storage.py` + `tests/test_docs_storage_hardening.py` (resolver, sidecar, files layer, symlinks, revisions), `tests/test_docs_service.py` + `tests/test_docs_service_hardening.py` (service pipeline, locking, cancellation), `tests/test_docs_tools.py` (handlers, dispatch, registry, allowlists), `tests/test_write_doc_action_request.py` (handler, pre-card, round-trip, TOCTOU), `tests/test_docs_prompting.py` (gate-aware prompts and skill, account-delete sweep), `tests/test_docs_routes.py` (HTTP routes, project-delete sweep), `tests/test_docs_review_followups.py` (bounded card diff, locked mode switch/delete incl. the queued-write taint race, hidden public-project docs on every by-id route, the image sha256 pin, the missing-project fail-closed). Also `tests/test_public_projects.py` (public prompt with and without docs), `tests/test_inference_api.py` (mutating roster) and `tests/test_spa_docs_routes.py` (the `/docs` SPA routes ahead of the static fallback, Swagger UI / ReDoc / OAuth2 redirect moved, `/openapi.json` unchanged; runs `quest` in a subprocess).

Frontend (vitest, `npm test`, files under `frontend/src/`): `api/docsApi.test.ts` (URL builders, stale_update helpers), `hooks/useDocs.test.tsx` and `hooks/useDoc.test.tsx` (paging, refresh on the realtime events, superseded responses, `applyRow`), `utils/docsRoute.test.ts`, `utils/sidebarDocs.test.ts`, `utils/allDocsGrouping.test.ts`, `components/docs/DocsListView.test.tsx` (grouping, search, failed-project warning, filtered view), `components/docs/DocHeader.test.tsx` (menu items by access flags, rename incl. the 409 paths, mode switch, delete), `components/docs/DocViewer.test.tsx` (states, Show source, footer writers), `components/MarkdownImage.test.tsx` (`assetBase` resolution vs the conversation workspace), `components/DocImagePreview.test.tsx` (incl. the `ActionRequestPreviewFields` branch) and `components/SkillContentDiffPreview.test.tsx` (bounded window vs whole-body diffs).
