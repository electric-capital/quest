# Quest Docs Architecture

## Overview

A **Quest Doc** is a markdown document that lives entirely inside Quest. It is owned by a user (a **user doc**) or by a project (a **project doc**, owned by the project owner). Each doc is a `docs` row plus a directory holding `doc.md`, embedded raster images and revision snapshots. Models reach docs only through seven purpose-built dynamic tools (list, search, read, create, search/replace edit, append, add image). One access function decides, for each conversation, whether a write is approval-free, needs a `write_doc` action request, or is impossible. The whole feature sits behind the off-by-default, per-user-capable `docs` [feature gate](feature-gates.md). People work with docs in the UI at `/docs`: browse, create, rename, download and delete, share a doc with other users or with everyone ([Sharing](#sharing)), edit the markdown directly ([Human Editing](#human-editing)), and browse, restore or copy earlier versions ([History](#history)). The HTTP surface (`/app/api/docs*`) is documented in [Quest Docs API](../api/quest-docs-api.md), and the UI under [Frontend](#frontend). Google Docs is a separate feature ([Docs API](../api/docs-api.md), `system:docs`).

Docs come in two **modes** that mirror [public projects](public-projects.md). **Private** docs hold internal information; only private conversations can read or write them, and public conversations are never told they exist. **Public** docs hold only content that came from the internet-enabled public sandbox (or that a human typed in the UI). Public conversations read and write them; private conversations may read them but never write, so internal data cannot flow into a doc that a public conversation could later exfiltrate. A user doc is always private: the only public docs are the docs of a public project, which inherit `projects.public` at creation, and no doc's mode ever changes ([Mode is fixed at creation](#mode-is-fixed-at-creation)).

## Key Files

| File | Description |
|------|-------------|
| `chat/docs/constants.py` | Every cap and the retention numbers (`DOC_MAX_*`, `DOC_READ_MAX_CHARS`, `DOC_REVISION_RETENTION_DAYS`, `DOC_REVISION_MAX_COUNT`, `DOC_SEARCH_*`), `DOC_MODES`, `DOC_SHARE_PERMISSIONS`, the prompt pseudo-key `DOCS_SERVICE_KEY`, and the shared texts: `doc_not_found_message()` (THE not-found text), `docs_disabled_message()`, `user_doc_mode_private_message()` (the UI's 400 `user_doc_mode_private`) and `user_doc_in_public_conversation_message()` (the `create_doc` refusal of `target="user"` in a public conversation) |
| `chat/docs/access.py` | `resolve_doc_access()` (the only place the matrix lives), `DocAccess`, `HIDDEN`, `RUN_KINDS` / `READ_ONLY_RUN_KINDS`, the `DENY_*` reason constants, `APPROVAL_WRITE_NOTE`, `effective_share()`, `write_note()`, `creation_mode()` |
| `chat/docs/files.py` | On-disk layer: `doc_paths()` (via `ChatStorage.get_doc_dir`), the per-doc `doc_lock()`, `init_doc` / `read_body` / `modify_body` / `write_body`, revision snapshot + prune, `list_revisions` / `read_revision_meta` / `read_doc_meta`, assets (`sniff_image_type`, `sanitize_asset_name`, `add_asset`, `delete_asset`, `asset_path`, `read_asset`, `list_assets`, `asset_stats`), `build_zip` (optional `asset_filter`), `delete_doc_dir` |
| `chat/docs/service.py` | The one read/write path shared by tools, routes and the action request: `Caller`, the `DocError` / `DocApprovalRequired` / `DocDisabled` / `DocRequestError` errors, `list_docs` / `search_docs` / `read_doc` / `create_doc` / `create_doc_from_ui` / `edit_doc` / `append_to_doc` / `add_doc_image`, `apply_write_operation`, `preview_write_operation`, the locked UI delete `delete_doc_from_ui`, `mode_switch_refusal()` (the error every `PUT /docs/{id}/mode` gets), and the write plumbing the UI modules reuse (`_write_lock`, `_shielded`, `_finish_write`, `_files`) |
| `chat/docs/ui_writes.py` | Human (UI) writes: `replace_body_from_ui` (editor save, History restore), `add_asset_from_ui`, `delete_asset_from_ui`, `referenced_asset_names()` (the one "which `assets/<name>` does the body reference" matcher, one pass over the `_reference_variants()` decodings; `asset_referenced()` is a membership test on it), `ui_write_source()`; the module docstring argues why invariant 5 still holds |
| `chat/docs/history.py` | Revision list / read / diff, `restore_revision`, `copy_revision`; the Version shape and the per-viewer `source_label` rules are in its module docstring |
| `chat/docs/events.py` | Realtime publishing to a doc's audience: `doc_audience()` / `docs_audience()`, `publish_doc_write()`, `publish_doc_list_changed_for/_to()`, `publish_doc_changed_for/_to()`, `publish_doc_gone_or_access_changed_to()` (`doc_changed` with `updated_at: null`), `publish_share_changed()` (best-effort per-user globals, event-loop thread only); the module docstring records who publishes |
| `chat/docs/routes.py` | The base `/app/api/docs*` router (list, create, read, rename, mode, asset, download, delete), mounted in `quest.py`; `_ui_row()` / `_ui_rows()` build every UI row; `_get_doc_for_ui()` adds the hidden-public-project 404; `_stale_conflict()` builds the flat 409 |
| `chat/docs/share_routes.py` / `edit_routes.py` / `history_routes.py` | The sharing, editing and history routers, each mounted in `quest.py` beside the base one |
| `db/doc_store.py` | Async metadata CRUD returning dicts (`_doc_to_dict` documents the shape), `create_doc()` (refuses a public user doc with `DocValidationError`), `_title_taken()`, `list_accessible_docs()` (keyset candidates; `owned_only` for the UI's own-docs stream), `list_docs_shared_with()` (the shared-with-me stream), `update_doc_metadata()` (`StaleDocError`), `update_after_write()`, `delete_doc()`, `list_doc_ids_for_project/user()`, `list_docs_for_project()` (with shares, for the project-delete audience), and the share helpers `add_share` (`ShareTargetGoneError`) / `remove_share` / `list_shares` |
| `db/user_store.py` | `get_users_by_ids()` (only id / email / name, batched) for share rosters, owners and attribution; `find_user_by_email_ci()` (`AmbiguousUserEmailError`) for share-by-email |
| `db/conversation_store.py` / `db/action_request_store.py` | `get_conversations_meta()` (batched owner-scoped titles for History) / `get_action_request_owners()` (who proposed an approved `write_doc` card, any user) |
| `chat/realtime/bus.py` | `connected_user_ids()`, the install-wide fan-out for everyone shares |
| `db/models.py` | `Doc`, `DocShare`, `ActionRequestType.WRITE_DOC` |
| `alembic/versions/55983a10e266_create_docs_and_doc_shares.py` | Creates `docs` + `doc_shares` and their indexes |
| `alembic/versions/e1b7c4d9a2f6_user_docs_always_private.py` | Sets `mode='private'` on every user doc, renaming a flipped row on a title clash ([Mode is fixed at creation](#mode-is-fixed-at-creation)) |
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
| Mode | `private` or `public`, stored on every row and fixed at creation. A user doc is always `private`. A project doc copies `projects.public` at creation. No doc's mode can be switched ([Mode is fixed at creation](#mode-is-fixed-at-creation)). |
| Share | A `doc_shares` row granting `read` or `write` to one user, or to everyone on the install (`user_id` NULL). |
| Private conversation | Any conversation outside a public project: standalone, private project, routine, Slack, sub-agent, inference API, cross-user subagent. |
| Public conversation | A conversation inside a public project (`is_public` in `run_conversation_turn`). |

Invariants the code keeps:

1. **Taint.** Content enters a public doc only from public conversations or from a person in the UI. A private conversation never gets a non-denied write verdict on a public doc, and History's copy always creates a private doc (public -> private is safe, the reverse never happens). Every doc is created in its final mode and keeps it: a user doc is always private, and a public conversation cannot create one at all (`create_doc(target="user")` is refused there); a project doc takes `creation_mode()`, the conversation's own mode, and `create_doc(target="project")` refuses when the project's mode disagrees with the conversation's. No doc's mode ever changes, so no internal content can be promoted into the public partition and nothing needs a switch-time re-check. The action request still re-resolves access at Approve, so a doc that became hidden while a card sat open (share revoked, doc deleted) refuses.
2. **Invisibility.** In every tool, a doc the caller may not see behaves exactly like a nonexistent id. The text is the same (`doc_not_found_message`) and so is the work: `doc_store.get_doc` runs its shares query for a missing id too, so both cost two queries and neither touches the files. Hidden docs never appear in list or search output. Title uniqueness is scoped per mode so that the `create_doc` collision error cannot reveal a private title to a public conversation (see Design Decisions).
3. **Single rule.** Every read/write decision goes through `resolve_doc_access()`. Tools, routes, the pre-card, the approve-time execute, `list_docs` / `search_docs` and the UI `access` object consume its `DocAccess`; none re-derive the rule.
4. **Paths.** `ChatStorage.get_doc_dir()` is the only id-to-path resolver (canonical id + containment check, see [Data Paths](data-paths.md)). `chat/docs/files.py` gets every path through `doc_paths()`.
5. **No full replace.** No model-facing path replaces a whole body in one call. Edits are exact search/replace (`apply_content_edit`), plus append and image add. `files.write_body()` has one production caller, `ui_writes.replace_body_from_ui()` (the editor's save and History's restore), which only the `/app/api` routes reach; no model-driven channel can address them ([Human Editing](#human-editing)).

### Mode is fixed at creation

A user doc (`project_id` NULL) can never be public. The only way to have a public doc is to create it inside a public project, where the mode is inherited from the immutable `projects.public`. No doc's mode is ever switched, by a model or in the UI. The reason: a doc may only become public by being born in a public project, so no internal content can ever be promoted into the public partition, and the taint invariant needs no switch-time re-check (see Design Decisions). Spec sections 4.2 (a user-doc mode switch through an explicit UI action) and 8.4 (its mode-switch dialog) are deliberately not built.

Every creation path enforces it:

- `doc_store.create_doc()` refuses a public user doc with `DocValidationError`.
- `service.create_doc()` (the `create_doc` tool) makes every user doc private and refuses `target="user"` (the default) from a public conversation with `user_doc_in_public_conversation_message()`, before any directory or row is written. A public conversation creates docs only with `target="project"`. The tool description, the `system:quest_docs` skill and `_PUBLIC_DOCS_SECTION` all say so.
- `service.create_doc_from_ui()` (`POST /docs`) creates a user doc private and refuses `mode: "public"` without a `project_id` with 400 `user_doc_mode_private`. A project doc takes its project's mode (400 `project_doc_mode_inherited` on a disagreeing `mode`).

Nothing switches a mode. `PUT /docs/{id}/mode` stays registered (so an old client gets a clear error) but refuses every doc after its 404 / 403 checks, through `service.mode_switch_refusal()`: 400 `user_doc_mode_private` for a user doc, 400 `project_doc_mode_inherited` for a project doc. `access.can_switch_mode` is always false, kept only for the frontend type. With no switch, no `doc_changed` / `doc_list_changed` is ever published for a mode change.

The access rule backs this up: `resolve_doc_access()` evaluates a doc with `project_id` NULL as private for every decision, whatever its stored `mode`, so public conversations never see a user doc. `_visible_docs()` in the service goes further and drops user docs from the SQL query for a public caller, so `list_docs` / `search_docs` never load them there.

Migration `e1b7c4d9a2f6` (down_revision `55983a10e266`) sets `mode='private'` on every user doc. Titles are unique per (owner, project, mode), so a flipped row whose title would collide with one of the owner's private user docs is renamed `<title> (formerly public)`, then `(formerly public 2)` and so on, cut to fit the 200-char title cap (casefold comparison, like `_title_taken()`). `updated_at` is left untouched, since this is a data correction rather than an edit. The downgrade is a no-op, because the previous mode is not recoverable.

In the frontend there is no Switch item in the viewer menu and no mode picker in New Doc (`CreateDocRequest` has no `mode`). The mode badge shows only on public docs (`shouldShowDocModeBadge()` in `utils/docMode.ts` is `mode === 'public'`): private is the unremarkable default, and a public doc's exposure to public conversations is never hidden.

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

Concurrency uses two locks. `files.doc_lock()` is a per-doc `threading.Lock` held for one file call. `modify_body()` reads, transforms and writes `doc.md` in a single critical section, because two separate calls would let concurrent writers clobber each other. The service additionally holds a per-doc `asyncio.Lock` across the whole write (see [Write path](#write-path)); the UI delete takes the same lock ([UI mutations](#ui-mutations)). Both locks are process-local, and each lock table holds weak values.

### Revision policy

`_write_body_locked()` in `chat/docs/files.py` snapshots the current body before every body write, then prunes:

- **Identical-body writes take no snapshot.** If the new bytes equal the current ones, there is nothing to restore.
- **Pruning at write time** (`_prune_revisions()`) deletes snapshots older than `DOC_REVISION_RETENTION_DAYS` (30, by file mtime). It also deletes the oldest snapshots beyond `DOC_REVISION_MAX_COUNT` (200). The cap bounds the disk a looping writer can use inside the window, since each snapshot can be up to 1 MB.
- **The newest snapshot is always kept**, however old. An idle doc keeps one restore point.

- **Every snapshot gets a metadata sidecar.** Each doc keeps `doc.meta.json` = `{"written_at": <ISO UTC, ms, Z>, "source": <the write source of the CURRENT body: "conversation:<id>" | "ui:<user_id>" | "ui" (legacy, meaning the owner) | "action_request:<id>" | null>, "size": <bytes>}`, written by `init_doc` and after every body write (`doc.meta.json.tmp` created exclusively with O_NOFOLLOW, then `os.replace`; read with O_NOFOLLOW + S_ISREG). `write_body` / `modify_body` / `init_doc` take `write_source`, which the service fills with the current writer's source. Asset-only writes (`add_asset` incl. `placement: "none"`, the UI image upload and the UI asset delete) never touch it, so it is NOT the row's `last_write_source`: that field names whoever wrote last, including a model's image add. UI image uploads and deletes keep the row's `last_write_source` as it was, so it keeps naming the current body's writer. Before a body write replaces `doc.md`, the snapshot's sidecar `revisions/<ts>-<n>.json` is a copy of `doc.meta.json`, so `written_at` is when that body was written and `source` who wrote it. Fallback when the meta is missing, unreadable or its `size` disagrees with the body (legacy docs, a body edited by hand): `{"written_at": <doc.md mtime, ISO Z>, "source": null, "size": <bytes>}`. A failed meta write is logged, the stale file removed (the next snapshot then falls back to a null source) and the save still succeeds. Name allocation skips leftover `.json` names. Pruning deletes a `.json` together with its `.md` under both rules and sweeps orphan `.json` files (an undeletable one is logged and skipped; a symlink at a sidecar name is unlinked, not followed). A snapshot without a sidecar is tolerated: `files.read_revision_meta(path)` returns the dict or None (missing, malformed, wrong shape, negative size, or not a regular file); `read_doc_meta(doc_id)` reads the current one. `doc.meta.json` is not an asset and not part of the zip download. This is the attribution [History](#history) reads.

Creation (`init_doc`) takes no snapshot. Metadata changes (rename, description) and share changes do not touch files. An `add_doc_image` with `placement: "none"`, a UI image upload and a UI asset delete change only `assets/`, so they take no snapshot either: a version is a body, and images are shared by every version (see [Known Limitations](#known-limitations) for what that means after an asset delete). [History](#history) serves the snapshots through `files.list_revisions()`, `read_revision_meta()` and `read_doc_meta()`.

### Tables

`docs` and `doc_shares` are created by migration `55983a10e266` (models in `db/models.py`, store in `db/doc_store.py`). Migration `e1b7c4d9a2f6` later set every user doc's `mode` to `private`, renaming a flipped row on a title clash ([Mode is fixed at creation](#mode-is-fixed-at-creation)).

- **`docs`** holds the owner (`ON DELETE CASCADE`), the nullable `project_id` (`ON DELETE CASCADE`, NULL = user doc), title (1..200), description (0..500), mode (always `private` for a user doc; a project doc's copy of `projects.public`), the cached `content_size` / `asset_count` (for list views), `last_write_source` (`conversation:<id>` from tools, `ui:<user_id>` from the UI, `action_request:<id>` from `write_doc`; rows created before human editing may hold a plain `ui`, which means the owner), and `created_at` / `updated_at`. `updated_at` doubles as the optimistic-concurrency token for rename, the content save and restore. `require_approval` (migration `a9c2e7f4b1d3`, default off) is the owner's switch from access rule 7; `update_doc_metadata()` flips it (a real bool only) and bumps `updated_at` like a rename.
- **`doc_shares`** grants `read` / `write` to a user (`ON DELETE CASCADE`) or, with `user_id` NULL, to everyone on the install. The unique `(doc_id, user_id)` index cannot stop duplicate NULL rows in SQLite, so the partial unique index `ix_doc_shares_everyone` (`WHERE user_id IS NULL`) allows at most one everyone row per doc. `add_share()` upserts and refuses the owner as a recipient (a share row would turn the owner's own writes into approval-gated ones). When the doc or the recipient was deleted between the caller's checks and the insert, the foreign key refuses the row and `add_share()` raises `ShareTargetGoneError`. Neither `add_share()` nor `remove_share()` bumps `updated_at`, so an open editor's token survives a share change.
- **Title uniqueness** is case-insensitive per **(owner, project, mode)**, enforced by `_title_taken()` in `db/doc_store.py`, not by an index. A NULL `project_id` is its own scope. The comparison uses Python `casefold()` because SQLite `lower()` folds ASCII only. The check runs on create and on rename (`update_doc_metadata`). It is check-then-insert like the skills store, with no lock against a simultaneous create.

### Read sidecar

`CHATS_DIR/<conversation_id>/doc_reads.json` (`ChatStorage.get_doc_read_ids` / `add_doc_read_ids`) records the docs this conversation has read. It is the read-before-edit gate for `edit_doc` and for `write_doc` with `operation: "edit"`. `read_doc` and `create_doc` feed it; `search_docs` snippets do not count. The service writes it on the event loop (never in a thread), relying on the same single-event-loop guarantee as the workspace and skill sidecars. Sub-agents dispatch with the parent's `conversation_id`, so a sub-agent's `read_doc` counts for the parent. Scripts have no conversation and record nothing.

## Access Rule

`resolve_doc_access(doc, *, user_id, is_public, project_id, run_kind)` in `chat/docs/access.py` returns `DocAccess(visible, can_read, write, deny_reason, required_by_owner)`, where `write` is `free`, `approval` or `denied` and `required_by_owner` marks an `approval` verdict produced by the owner's require-approval switch (rule 7; it only changes the wording the model sees). `doc` must carry its `shares` list; a dict fetched without shares raises instead of being read as unshared.

The matrix (from the `access.py` module docstring, with the Public rows' `script` cell spelled out; "recipient" = a share recipient; the last column covers `sub_agent`, `inference_api`, `user_subagent` and `script`):

| Doc | Private owner | Private recipient | Public owner | Public recipient | Read-only run kinds |
|-----|---------------|-------------------|--------------|------------------|---------------------|
| Private, unshared | Free | n/a (Hidden) | Hidden | n/a (Hidden) | Read |
| Private, shared (any) | Approval | read: Read; write: Approval | Hidden | Hidden | Read |
| Public, unshared | Read | n/a (Hidden) | Free | n/a (Hidden) | Read (`script`: Hidden) |
| Public, shared | Read | Read | Free | read: Read; write: Free | Read (`script`: Hidden) |

A user doc (`project_id` NULL) is always private: step 0 of `resolve_doc_access()` evaluates it as `private` for every decision, whatever its stored `mode` (defensive; after migration `e1b7c4d9a2f6` no public user doc exists). So the Public rows describe only docs of a public project, seen from that project's conversations (rule 2), and every Public owner / Public recipient cell of a user doc is Hidden. Every Private cell is unchanged. The `script` cell of the Public rows is Hidden: scripts never see project docs (rule 2), and every public doc is a project doc, so no script can read a public doc.

The rules layered on the matrix are applied in order:

1. No owner match and no share (the user's own row or the everyone row, higher permission wins in `effective_share()`) -> Hidden.
2. Project docs are visible only from conversations of that same project, and from the UI. Standalone conversations, other projects and scripts see Hidden.
3. A public conversation never sees a private doc, even the owner's, even with a write share. That includes every user doc.
4. Read-only run kinds read what they can see and never write. Each gets its own `DENY_*` reason.
5. `ui` (the routes): Free for the owner or a write share, else Read (`DENY_UI_READ_ONLY`). Free is what the row's `access.can_edit` reports and what the content save, image upload, restore and every History route require. Ownership-only operations are a separate check, not part of the matrix: in the route for rename, share management and asset delete, in `delete_doc_from_ui` for delete (`DocRequestError("forbidden")`). No doc's mode can be switched: the mode route checks ownership, then refuses every doc (see [Mode is fixed at creation](#mode-is-fixed-at-creation)).
6. `slack`: an Approval verdict becomes a denial (`DENY_SLACK_NEEDS_APPROVAL`), because Slack-driven runs cannot open action requests.
7. **The owner's require-approval switch** (`docs.require_approval`, migration `a9c2e7f4b1d3`; the "Require approval for agent writes" checkbox in the doc header menu, owner only). While it is on, every Free verdict a *conversation* would get becomes Approval with `required_by_owner=True`, the owner's own unshared private doc included, and a share-based Approval keeps its verdict but takes the switch's wording. Where no approval card can be opened the write is denied instead: Slack-driven runs get `DENY_SLACK_APPROVAL_REQUIRED`, and public-project conversations, which have no action requests at all, get `DENY_PUBLIC_APPROVAL_REQUIRED`, so on a public doc the switch leaves the agent read-only. Read cells, Hidden cells, the read-only run kinds and the `ui` column (a person editing is never approval-gated, invariant 5) are unchanged. The switch is read from the live row on every decision, so flipping it takes effect on the next write of every conversation; the approve-time re-resolve of a `write_doc` card still applies the change (`bypass_approval`), and the pre-card injects `require_approval` so the card shows an "Approval: required by the owner for every change" line. A missing `require_approval` key on a hand-built dict counts as off, like a fresh row.

`write_note()` turns a verdict into the short `write_note` that `list_docs` / `read_doc` return: none for free, `APPROVAL_WRITE_NOTE` for approval (`APPROVAL_REQUIRED_WRITE_NOTE` when `required_by_owner`), `deny_reason` otherwise; the tools' `approval_required` message likewise comes from `service.approval_required_message()`. `tests/test_docs_access.py` covers every cell, with and without the switch.

### run_kind derivation

`ToolContext.run_kind` in `chat/gemini_api/tool_dispatch.py` picks the first match: `is_script` -> `script`, `is_sub_agent` -> `sub_agent`, `is_inference_api` -> `inference_api`, `is_user_subagent` -> `user_subagent`, `is_slack` -> `slack`, else `top_level`. Routine runs are `top_level`, and whether the conversation is public is the separate `is_public` field. `run_conversation_turn` passes `is_user_subagent` and `is_slack` (the conversation origin) into `_dispatch_tool_call`. `chat/gemini_api/sub_agent.py` passes `is_sub_agent=True`. The script bridge passes `is_script=True`.

`_doc_caller()` builds the service `Caller` from the context. For scripts it forces `conversation_id=None`, `project_id=None` and `is_public=False`. Other callers build their own: the routes (and `ui_writes.ui_caller()`) use `run_kind="ui"` with no project and `is_public=False`. The `write_doc` pre-card and execute use `run_kind="top_level"`, with `is_public` re-derived from the project row (`project_is_public()` in `write_doc.py`).

## Service Layer

`chat/docs/service.py` is the only write path. Every operation follows the same pipeline: gate (`require_enabled`, before any DB work) -> lookup -> `resolve_doc_access` -> file op in `asyncio.to_thread` -> `doc_store.update_after_write` -> read sidecar -> events. `DocError` messages are model/user-facing. Unexpected `OSError`s become a generic message that names no path.

### Read side

- `list_docs`: candidates come from `doc_store.list_accessible_docs()` (owner or share, keyset-paged on `(updated_at, id)` descending). Every row still passes the access rule, and the store is paged past hidden rows so they never use up `limit`. A public caller never queries user docs (`_visible_docs()` passes `include_user_docs=False`, since user docs are always private) and adds a SQL `mode = 'public'` filter, so it lists only its own project's docs. Scope `project` is empty outside a project. Rows carry a `shared` flag but never the share roster.
- `search_docs`: case-insensitive substring match on title, description and body. Bodies of visible docs are read newest-first until the next one would push the scanned total past `DOC_SEARCH_MAX_SCAN_BYTES` (50 MB). After that, only titles and descriptions are matched and `truncated` is true. Up to `DOC_SEARCH_SNIPPETS_PER_DOC` non-overlapping ~200-char snippets per doc, with 1-based line numbers. A metadata-only hit has `matches: []`.
- `read_doc`: the whole body or a 1-based inclusive line range, capped at `DOC_READ_MAX_CHARS` and cut at a line boundary. A cut sets `truncated` and a paging `note`. Lines split on LF only, so they agree with search's line numbers. Records the read.

### Create

`create_doc` (conversations) lays out the directory under a fresh uuid, then inserts the row. A failed insert removes the directory again; a crash in between leaves an invisible orphan directory, never a row without a body. Only `top_level` and `slack` callers may create; creation is never gated because a new doc has no shares. The new id is recorded as read. A user doc is always created private, and `target="user"` from a public conversation is refused before the directory is laid out; a project doc takes its project's mode. `create_doc_from_ui` (`POST /docs`) creates an empty doc with `last_write_source="ui:<user_id>"` (also recorded in `doc.meta.json`): a user doc private (a `mode: "public"` is `user_doc_mode_private`), a project doc in its project's mode. See [Mode is fixed at creation](#mode-is-fixed-at-creation).

### Write path

`apply_write_operation(caller, doc_id, operation, params, *, write_source, bypass_approval=False)` serves `edit` / `append` / `add_image` for both the tools and the action request:

1. **First pass, outside any lock** (`_resolve_write`): gate, parameter normalization, visibility (hidden == missing), `denied` -> `deny_reason`, then the read-sidecar check for `edit` and the conversation requirement for `add_image`. Hidden, denied or unread docs never take or create a lock.
2. **Approval**: unless `bypass_approval`, an `approval` verdict first dry-runs the operation (`_compute_preview`), so a stale `old_string`, a bad image or an oversized body fails now. It then raises `DocApprovalRequired` carrying `suggested_request`.
3. **Locked write**: under the per-doc `asyncio.Lock`, access is resolved again, so a share added or revoked while this write queued changes the verdict. The writer then runs (`files.modify_body` for edit and append; `add_asset` followed by an optional body append for images), and `update_after_write` bumps `content_size` / `asset_count` / `last_write_source` / `updated_at` in write order. Finally `doc_changed` + `doc_list_changed` go to the doc's audience ([Realtime](#realtime)).
4. **Shielded**: the locked part runs via `_shielded()`, so a cancelled turn (Stop mid-write) cannot strand a landed file write without its DB bump and events. A late failure is logged.

`add_image` reads the workspace file through `resolve_workspace_file()` (path guards) plus an `O_NOFOLLOW` / `S_ISREG` descriptor read capped at `DOC_MAX_IMAGE_SIZE`, then validates it by magic bytes. With `placement: "append"` the body cap is pre-checked using the provisional asset name before the asset is stored. If the body append still fails, the asset counter is recorded anyway (assets are additive) and the error is raised. When the params carry `expected_sha256` (set only by the `write_doc` execute, from the card's `image_preview.sha256`; `_suggested_request()` never emits it), a file whose sha256 differs is refused. A project workspace is shared by sibling conversations, which could swap the file while the card sat open.

### UI mutations

`delete_doc_from_ui(user, doc_id)` serves `DELETE /docs/{id}`. It resolves visibility as `ui`, then checks ownership (`forbidden`). The deletion itself (`doc_store.delete_doc` plus the directory removal) runs under the per-doc `asyncio.Lock` and `_shielded()`, so it waits for any in-flight model write. Under the same lock it re-reads the row with its shares and captures the audience before the cascade removes them, then publishes `doc_list_changed` to that captured audience. Rename stays a plain metadata update in the route (`doc_store.update_doc_metadata`) that publishes to the doc's audience when something changed. Share changes, body saves, restores and asset changes are covered in [Sharing](#sharing), [Human Editing](#human-editing) and [History](#history); every one of them takes the same per-doc lock.

There is no mode mutation. `mode_switch_refusal(doc)` only builds the `DocRequestError` that `PUT /docs/{id}/mode` raises after the route's own 404 / 403 checks (`user_doc_mode_private` for a user doc, `project_doc_mode_inherited` for a project doc). It takes no lock, changes nothing and publishes nothing ([Mode is fixed at creation](#mode-is-fixed-at-creation)).

## Sharing

The owner shares a doc from the viewer's Share dialog. The routes are in `chat/docs/share_routes.py`, whose module docstring gives the full check order. `POST /docs/{id}/shares` grants one recipient `read` or `write`: either a Quest user named by email (`user_store.find_user_by_email_ci`, where an exact match wins, then an ASCII case-insensitive one; several case-variant accounts with no exact match are 409 `ambiguous_user`) or everyone on the install. It upserts through `doc_store.add_share`, so sharing again changes the permission. `DELETE /docs/{id}/shares/{share_id}` removes one row. Only the owner manages shares (403 `forbidden` for a visible recipient). Both routes run under the per-doc write lock, shielded, so a model write queued behind a share change re-resolves its verdict with the new roster, and the doc delete reads the roster it notifies under the same lock. Both return the owner's row.

What a share grants in the UI (a direct and an everyone row combine, the higher permission winning, `effective_share()`):

| | Owner | Write share | Read share |
|---|---|---|---|
| View, download | yes | yes | yes, but only the images the current body references |
| Edit the body, upload images | yes | yes | no |
| History: list, read, diff, restore, copy | yes | yes | no |
| Rename, delete, delete images, manage shares | yes | no | no |

The access rule itself is unchanged; sharing only makes its share cells reachable. Consequences:

- **The owner's first share on a private doc turns the owner's own conversation writes into `write_doc` approval cards** (matrix row "Private, shared"). Removing the last share makes them free again, unless the owner's require-approval switch (rule 7) is on, which gates them regardless of shares. The Share dialog says so (`shareDialogNotes()` in `frontend/src/utils/docSharing.ts`).
- **Recipients' conversations reach shared user docs only.** From the recipient's private conversations, a shared user doc is readable (read share) or writable through a `write_doc` card that the recipient approves in their own conversation (write share; every user doc is private). A shared project doc stays Hidden to all of the recipient's conversations, because project docs are reachable only from their own project's conversations (rule 2) and projects are not shared. **Project-doc shares are therefore UI-only for recipients**, and since every public doc is a project doc, so are shares of public docs.
- **Revocation is immediate.** A `write_doc` card the recipient proposed is refused at Approve, because execute re-resolves access. The recipient's editor, History view and viewer lose access on their next request (their viewer re-fetches on the `doc_changed {updated_at: null}` the revocation sends).

**Listing.** `GET /docs` serves three independent keyset streams (`_visible_docs_page()` in `chat/docs/routes.py`): the caller's own user docs (the default, `list_accessible_docs(owned_only=True)`), one of the caller's projects (`project_id`), and the docs shared with the caller (`shared=true`, `doc_store.list_docs_shared_with()`: not owned, with a share row for the caller or for everyone; user and project docs). A user doc shared with the caller appears only in the shared stream; the owners of other users' docs are looked up once per store batch and reused for the rows.

**Rows.** Every UI response builds its rows through `_ui_row()` / `_ui_rows()`, with at most one `get_users_by_ids` call per response (plus one `get_action_request_owners` call when an owner row was last written by an approved card). The row adds `shared_with_me`, `permission`, `owner` (non-owners only), `shares[].user` (owner only), `last_write_user` (owner only: the user of a `ui:<user_id>` source, null when that user is gone; or the proposer of an approved `write_doc` card when that is not the owner, kept as `{id, name: null, email: null}` when the proposer is gone) and the `can_edit` / `can_share` / `can_delete_assets` access flags. Non-owners still get `last_write_source` blanked. Shapes: [Quest Docs API -- Response Shapes](../api/quest-docs-api.md#response-shapes).

**Read-only viewers see only referenced images.** For a viewer whose UI verdict is not Free (a read or everyone-read share), only the images the current body references count (`ui_writes.referenced_asset_names()`, escaped spellings included, computed once per body and then tested by membership):

- the detail's `assets` list is filtered (the pass runs in a thread and primes the asset route's cache);
- the asset route 404s `asset_not_found` for any other name, reading the name set from a 64-entry LRU keyed `(doc_id, updated_at)` (`_referenced_names()` in `chat/docs/routes.py`), so one body pass serves every image of a page and every body write starts a new key;
- the zip passes a memoizing filter (`_referenced_asset_filter()`) as `files.build_zip()`'s `asset_filter`, evaluated once against the very body it archives under the doc lock.

Owners and write shares see every asset. An image the owner removed from the text before sharing therefore stays private. The row's `asset_count` is not filtered ([Known Limitations](#known-limitations)).

## Human Editing

The owner and write-share recipients edit the markdown in the viewer's editor. The routes are in `chat/docs/edit_routes.py` and the writes in `chat/docs/ui_writes.py`, which follow the service pipeline: gate -> visible doc -> access -> per-doc write lock, shielded -> file op in a thread -> `service._finish_write()` (DB bump + events).

- **Body save** (`PUT /docs/{id}/content`, `replace_body_from_ui()`): a whole-body replace carrying `expected_updated_at`. The body is validated before the lock (a string, UTF-8 encodable, CRLF -> LF, at most `DOC_MAX_CONTENT_SIZE`). Under the lock, visibility, the write verdict and the token are re-checked against a fresh row, so a share revoked or another write landing while the request queued wins (a token mismatch is the flat 409 `stale_update`). An identical body writes nothing: no snapshot, no bump, no event, `changed: false`. Otherwise `files.write_body()` snapshots the replaced body into `revisions/` and records `ui:<user_id>` in `doc.meta.json` and `last_write_source`. A missing, symlinked or special `doc.md` is a 400 `invalid_doc_files` (`_body_files()`), as for restore.
- **Not approval-gated.** A write-share recipient's human edit of a shared private doc needs no card: the `write_doc` card guards model-initiated writes, and this is a person the owner granted write access. Taint holds because a person in the UI is an allowed source even for a public doc.
- **Invariant 5 still holds.** The whole-body replace is reachable only from the `/app/api` routes on the main port, which no model-driven channel can address. The `curl_proxy_*` tools dispatch `/api/*` only (`route_dispatch.validate_url`). Restricted sandboxes reach only the sandbox tool API port, with a `qsb_` token that authenticates nothing on the main port. Public sandboxes get no credential at all. The protection is that the routes cannot be reached, not the auth scheme: they accept a session cookie or the user's API key.
- **Write source.** Every UI body write records `ui:<user_id>` (`ui_write_source()`): UI create, content saves, restores and History copies. A legacy plain `ui` means the owner. Image uploads and deletes change no body, so they keep the row's `last_write_source`, and the footer keeps naming the body's writer.
- **Image upload** (`POST /docs/{id}/assets`, `add_asset_from_ui()`, needs `can_edit`): validated like `add_doc_image` (magic-byte sniff with SVG refused, `DOC_MAX_IMAGE_SIZE`, then under the lock `DOC_MAX_ASSETS` / `DOC_MAX_ASSETS_TOTAL_BYTES` against the files on disk), stored as `sanitize_asset_name()` plus the sniffed extension with `-2`, `-3` suffixing. The body is not touched: the response carries the `![alt](assets/<name>)` markdown (`image_markdown()` escapes the alt) for the editor to insert and save with the next content PUT, so there is no snapshot. The row gets an asset-only bump (`asset_count`, `updated_at`; `content_size` and `last_write_source` kept). The response returns `updated_at` and `previous_updated_at` (the token just before this write), so an editor holding exactly that token may adopt the new one.
- **Image delete** (`DELETE /docs/{id}/assets/{name}`, `delete_asset_from_ui()`): owner only. It is 409 `asset_in_use` while the current body references `assets/<name>` (`asset_referenced()`, run in a thread: a name is the maximal run of name characters after `assets/`, so `assets/chart.png.png` does not use `chart.png`, and percent-, entity- and backslash-escaped spellings count too). An unreadable `doc.md` is 400 `invalid_doc_files`. `files.delete_asset()` opens `assets/` as an `O_NOFOLLOW` directory and unlinks the leaf relative to it after an `lstat` regular-file check, so a symlink is never followed and nothing outside `assets/` is reachable. Every bad, hidden, missing or non-regular name gets the one 404 `asset_not_found`. Asset-only write, no snapshot, `last_write_source` kept.
- **Bounded request parsing.** The handlers read their own bodies after the gate, visibility and write checks, so neither a stranger nor an oversized request makes the server buffer an arbitrary body. A declared `Content-Length` over the cap is refused before reading, and the streamed bytes are counted as they arrive (`_bounded_stream`). The content PUT accepts JSON only, up to `DOC_MAX_CONTENT_SIZE` x 6 (JSON escaping) + 64 KiB. The upload accepts `multipart/form-data` only, up to `DOC_MAX_IMAGE_SIZE` + 64 KiB, with one file part, 8 KiB per text field, at most `DOC_MAX_IMAGE_SIZE` + 1 file bytes read, and every spooled temp file closed (`_close_parser_files`). Bad bodies are 400 `invalid_request`, never FastAPI's 422.

## History

Editors (the owner or a write share) browse, diff, restore and copy a doc's versions. The routes are in `chat/docs/history_routes.py` and the logic in `chat/docs/history.py`, whose module docstring defines the Version shape and the label rules.

- **Editors only.** Right after visibility, a read-only viewer gets 403 `forbidden` on every history route, and the viewer offers no History item. Earlier revisions can hold text the owner removed before sharing, which a read recipient could otherwise read and copy for good. Write-share recipients see the full history, like Google Docs editors. Every function re-checks access against a fresh row after its file reads (`_recheck_history_access()`: visibility, then the editor check), so a share revoked or downgraded mid-request never leaks a version.
- **Versions.** The current body (`doc.md` + `doc.meta.json`) plus the snapshots, newest first and unpaged (at most `DOC_REVISION_MAX_COUNT`), read under the doc's file lock so they describe one moment. Missing or mismatched metadata falls back to the file's mtime and an unknown source. A revision id must fullmatch `[0-9]{8}T[0-9]{6}Z-[0-9]+` and name a snapshot `files.list_revisions()` lists right now. The file is opened O_NOFOLLOW + O_NONBLOCK and checked S_ISREG on the descriptor. Every failure is the one 404 `revision_not_found`.
- **Labels per viewer** (`source_label`). `ui:<viewer id>` (and the legacy `ui` for the owner) reads "you". Another user's `ui:<id>` reads that user's name or email ("a deleted user"), and the legacy `ui` reads "the owner" for anyone but the owner. A conversation reads as its title for the owner ("a deleted conversation" once gone) and "a conversation" for anyone else. An approved `write_doc` card is attributed to its proposer (`get_action_request_owners()`). For the owner it reads "action request #<id>" when the owner proposed it and "<name> (approved change)" when a share recipient did; for anyone else it reads "you (approved change)" or "an approved change". Only the owner receives the raw `source` and the `conversation` link. Editors' names are shown to every editor; names and emails are already discoverable through `/users/search`. Lookups are batched: at most one each of `get_action_request_owners`, `get_users_by_ids` and `get_conversations_meta` per response.
- **Diff.** `?diff=current` adds `build_bounded_content_diff(old=<revision>, new=<current body>)`, the bounded diff the `write_doc` card uses, computed in a thread. The UI labels it "Changes since this version".
- **Restore** goes through `ui_writes.replace_body_from_ui()` with the client's `expected_updated_at`, so access and the token are re-checked under the per-doc write lock, and a write that landed after the client read the token always wins (flat 409 `stale_update`). The current body is snapshotted first and the restored one is recorded as `ui:<user_id>`. An identical body writes nothing.
- **Copy** creates a NEW private user doc owned by the caller from a revision, also when the source is a project or public-project doc. It is never a project doc: a recipient cannot write into the owner's project, and a private user doc is always taint-safe. The title is the given one (409 `duplicate_title` when the caller already has a user doc with it) or `<title> (copy)`, `(copy 2)`, and so on, recomputed if a concurrent create takes it. The description is copied. The body is written with `ui:<user_id>` and no revisions. Every asset the copied body references (History's `referenced_asset_names()`: the validated names from `ui_writes.referenced_asset_names()`, the same matcher as the in-use rule, escaped spellings included) that the source still holds as a regular raster file within the caps is copied under the same name. The build is shielded and removes the row and the directory again on any failure. `doc_list_changed` goes to the caller only.

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

- **Public conversations**: all seven are in `PUBLIC_TOOL_CALL_ALLOWLIST`. The access rule hides every private doc there (every user doc included), `create_doc` refuses `target="user"` there, and public conversations only ever get `free` or `denied` on public docs, so the blocked `create_action_request` is never needed.
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
- **`execute`**: `chat/action_request_routes.py` passes `request_id=` to this one handler type. `execute` calls `apply_write_operation(..., bypass_approval=True, write_source="action_request:<id>")`, passing the card's `image_preview.sha256` as `expected_sha256` for `add_image`, so a workspace file swapped after the proposal is refused. This re-resolves access, so a doc that became hidden (the share revoked, the doc deleted) refuses and a doc whose shares were all removed is simply written. It also re-checks the read sidecar for `edit` and re-applies against the live body; that is the TOCTOU close, so a changed `old_string` fails the Approve and the request stays open. The service publishes the realtime events itself.
- **Attribution**: a card is proposed and approved in the proposer's own conversation, so a write-share recipient's card is approved by that recipient. `action_request:<id>` names the card, and `get_action_request_owners()` resolves its proposer: the owner's row gets `last_write_user` = the recipient, and History labels the version "<name> (approved change)" ([History](#history)).
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

- **`system:quest_docs`** (name "Quest Docs"; `system:docs` is the Google Docs skill) covers when a doc beats a workspace file, the tool set, modes (user docs always private, public docs only in public projects, no mode switch, `target="user"` refused in a public conversation) and verdicts (quoting the `DENY_*` constants), read-before-edit, the `approval_required` -> `write_doc` handoff with the three `params` shapes, the Slack limitation, images, paging, the routine pattern (one doc, `append_to_doc` under a dated heading per run, never a new doc per run), and the script bridge. Caps in the text are rendered from `chat/docs/constants.py`.
- **Public prompt**: `_PUBLIC_DOCS_SECTION` follows the Boundaries block only while `docs_enabled`, and stands in for the skill. It says the conversation sees and creates only its own project's public docs, so it always creates with `create_doc(target="project")`. It may not name internal-only tools or skills (`tests/test_public_projects.py::test_no_internal_tool_docs`), which is why the write-tool descriptions say "write_doc action request" rather than spelling out `create_action_request(`.
- No doc titles are ever injected into a prompt. Discovery is tools-only.
- `system:action_requests` and the `create_action_request` description route `write_doc` to `system:quest_docs`.

## Realtime

Two per-user globals ([Realtime](realtime.md)), built by `make_doc_list_changed()` / `make_doc_changed()` in `chat/realtime/events.py` and published best-effort through `chat/docs/events.py` to the doc's **audience**: the owner plus every direct share recipient, plus every connected user (`bus.connected_user_ids()`, resolved at publish time) when the doc has an everyone row. User ids are deduped. `doc_audience()` derives the audience from a doc dict that carries its shares. A delete captures it before the cascade removes the share rows, then publishes to the captured audience.

- `doc_list_changed` (no payload): list views re-fetch their window. Sent after create, rename/description change, delete, every body or asset write (model, `write_doc` or UI), restore, share change, History copy, and a project delete that removed docs.
- `doc_changed {doc_id, updated_at}`: an open viewer of that doc re-fetches unless it already shows that `updated_at`. Sent after create, every body/asset write (restore included) and rename, with the row's new token.
- `doc_changed {doc_id, updated_at: null}`: "this doc's existence or the receiver's access to it changed; re-fetch". Sent by `publish_doc_gone_or_access_changed_to()` on doc delete, share add / change / remove and project delete (per doc), always before that change's `doc_list_changed`. These changes do not bump `updated_at`, so a token could not signal them.

Every change an open viewer must notice therefore reaches it as a `doc_changed` for its own doc, and viewers ignore `doc_list_changed`, which can be install-wide traffic. No mode switch exists, so neither event is ever sent for a mode change. Who publishes (recorded in the `chat/docs/events.py` docstring):

- `chat/docs/service.py`: every body/asset write through `_finish_write()` -> `publish_doc_write()` (`doc_changed` then `doc_list_changed`; the model tools, `write_doc`, and the UI writes in `ui_writes.py`, which reuse it), plus UI create; the UI delete sends `doc_changed {updated_at: null}` then `doc_list_changed` to the audience captured before the delete; the model's `create_doc` reaches only the owner, since a new doc has no shares;
- the rename route, only when something changed;
- `chat/docs/share_routes.py`: `publish_share_changed(doc_id, owner_id, recipient_user_id)` sends `doc_changed {updated_at: null}` then `doc_list_changed` to the owner and the affected recipient, or to every connected user for the everyone row. Other recipients' view of the doc is unchanged, so they are not notified, and sharing again with the same permission publishes nothing. The owner's open viewer re-reads the roster and a recipient's re-reads its permission, so a revocation turns into its not-found state;
- `chat/docs/history.py`: a copy sends `doc_list_changed` to the caller only, the one user who can see the new doc;
- the project-delete route, after its directory sweep: `doc_changed {updated_at: null}` per deleted doc to that doc's own audience, then one `doc_list_changed` to the combined audience of the project's docs (`doc_audience()` / `docs_audience()` over `doc_store.list_docs_for_project()`, collected before the delete);
- tools never publish.

Publishing is event-loop-thread only, because `bus.publish_to_user` is not thread-safe. Neither envelope carries a `conversation_id`, so they reach `PersistentWebSocket`'s global handlers without an allow-list entry. The frontend consumers are `useDocs`, `useProjectDocsIndex` and `useDoc` (see [Frontend -- Realtime consumers](#realtime-consumers)). The everyone-share broadcast is not filtered per recipient, and an account deletion notifies no one ([Known Limitations](#known-limitations)).

## Frontend

Browse, read, create empty docs, rename, download and delete; share (owner), edit the markdown and add images (owner and write shares), and History (owner and write shares). There is no mode switch ([Mode is fixed at creation](#mode-is-fixed-at-creation)). Paths below are under `frontend/src/` unless noted.

| File | Role |
|------|------|
| `api/docsApi.ts` | Client for `/app/api/docs*` (list with `shared`, sharing, content save, multipart `uploadDocAsset`, asset delete, revisions / restore / copy), the cookie-authed URL builders (`docAssetBase`, `docAssetUrl`, `docDownloadUrl`), `DOCS_FEATURE`, and `isStaleUpdateError` / `staleUpdateCurrent` for the flat 409 of rename, save and restore |
| `api/types.ts` | `Doc` (incl. `shared_with_me`, `permission`, `owner`, `last_write_user`, the access flags), `DocDetail`, `DocShare`, `DocUserRef`, `ListDocsResponse`, the editing / history request and response types (`DocContentResponse`, `DocAssetUploadResponse`, `DocVersion`, `DocRevisionsResponse`, `DocRevisionDetail`, ...), `DocImagePreview`; `PreviewField.image` and the optional `truncated` / `total_old_lines` / `total_new_lines` on `SkillContentDiff` |
| `utils/docsRoute.ts` | `parseDocsRoute()`, `docsListPath()`, `docViewerPath()` |
| `utils/docSharing.ts` | Pure sharing text: header chips (`ownerShareSummary`, `recipientShareSummary`, `shareChipTitle`), the Share dialog's roster helpers and `shareDialogNotes()`, the All Docs "Shared by" scope label and owner search text |
| `hooks/useDocs.ts` | One keyset-paged list (the user's own user docs, one project's, or `shared: true` for the docs shared with the user) with `loadMore` and a debounced silent `refresh` |
| `hooks/useProjectDocsIndex.ts` | Per-project fan-out for the unfiltered All Docs view |
| `hooks/useDoc.ts` | One doc (row + body) for the viewer, `notFound`, `applyRow()` (a row without a body: rename, share, a 409's `current`; re-fetches whenever the row's `updated_at` is newer than the shown one, so a new token never pairs with a body this tab has not seen) and `applyContent()` (a row with its body: save, restore) |
| `hooks/useUnsavedChangesGuard.ts` | The editor's leave guard (`beforeunload` + capture-phase in-app link interception) under a plain `BrowserRouter` |
| `utils/docDraftBackup.ts` | The editor's per-user localStorage draft backup: key `quest_doc_draft:<encoded lower-cased email>:<docId>` (per user because `GET /me` exposes no user id; `docDraftScope()`), value `{content, base, saved_at, title}`, `pruneDocDrafts()` (legacy unscoped keys, unreadable values, drafts older than 30 days), `clearAllDocDrafts()` |
| `components/settings/SignOutSection.tsx` | Calls `clearAllDocDrafts()` after logout, logout-and-disconnect and account delete |
| `components/sidebar/DocsSection.tsx` + `utils/sidebarDocs.ts` | The sidebar Docs block and its pure row-order / count / time helpers |
| `components/docs/DocsListView.tsx` + `utils/allDocsGrouping.ts` | The All Docs view and its pure search / grouping / scope-label / size helpers |
| `components/docs/NewDocModal.tsx` | Creates an empty doc (`POST /docs`); no mode picker, never sends `mode` |
| `components/docs/DocViewer.tsx`, `DocHeader.tsx` | The viewer with its `view` / `edit` / `history` body modes, its title unit, menu and chips |
| `components/docs/DocConfirmDialog.tsx` | The shared confirm (delete, image delete, discard, restore, copy, everyone grant), on `ModalShell`: named by its title (`aria-labelledby`, via ModalShell's optional `ariaLabelledBy` prop), focus on Cancel when it opens (unless a child input took it), back to the opener when it closes |
| `components/docs/DocShareDialog.tsx` | The owner's Share dialog |
| `components/docs/DocEditor.tsx` | The body editor (textarea + live preview, image upload, conflict handling, draft backup) |
| `components/docs/DocDraftRecovery.tsx` | A read-only saved draft with Copy, Download .md and Discard, shown when the editor can no longer take it back |
| `components/docs/DocHistory.tsx` | The History view (version list, rendered version, diff, restore, copy) |
| `components/docs/DocModeBadge.tsx` + `utils/docMode.ts` | The Private / Public pill and the one display rule `shouldShowDocModeBadge(mode)` (`mode === 'public'`), so only public docs carry it: sidebar rows, All Docs rows, the viewer header, and the New Doc inherited-mode line |
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

`Sidebar.tsx` mounts two `useDocs` instances with limit `SIDEBAR_DOC_LIMIT` (5): the user's own user docs, and the drilled project's docs (enabled only while drilled). Docs other people shared with the user are listed only in All Docs' "Shared with you". Both render through `DocsSection`, only while the gate is open:

- on the main panel, between Projects and Conversations, with a header that opens `/docs`;
- in `ProjectPanel`'s drill-down, below Routines (order: Routines, Docs, Conversations), with a header that opens `/docs?project=<id>`.

The whole header is a button (no doc count: the list is a preview, not an inventory). Rows show the title, a small `DocModeBadge` (public docs only) and a compact time, newest `updated_at` first (`deriveSidebarDocItems`). The doc open in the viewer is highlighted, from the URL. The empty state reads "Ask Quest to create a doc". After navigating, the Sidebar calls its `onNavigateAway` prop, which `MobileShell` sets to close the phone drawer; the desktop layout passes nothing.

### All Docs view

`DocsListView.tsx` shows a header (title "All Docs", or the project's name with a folder icon, a public badge and an "All docs" back link when filtered), a search box, a New Doc button, and grouped rows: title + description, mode badge (public docs only; the "Mode" column label shows only while a visible row is public, the column slot stays either way), scope, relative update time (absolute on hover), and size + image count. Each server-paged group gets its own "Load more" footer. Search filters everything loaded, client-side, on title and description, and for a shared row also on its owner's name and email (`sharedOwnerSearchText`). When a search matches nothing, the no-match line's "Load more" pages every stream that has more.

The backend has no cross-project list of the user's own docs: `GET /docs` returns the user's own user docs, one project's docs, or (`shared=true`) the docs other people shared with the user. So the unfiltered view combines three sources:

- "Your docs": `useDocs({limit: 50})`, server-paged, the user's own user docs.
- "Shared with you": `useDocs({shared: true, limit: 50})`, its own keyset stream and "Load more", user and project docs alike. The group is hidden while empty, unless its first page failed (then it shows the error). The scope cell reads "Shared by <owner>", with the permission in its tooltip (`docScopeLabel` / `docScopeTitle`).
- One group per project (archived ones included, with an "Archived" chip on the heading: archiving has no effect on docs) from `useProjectDocsIndex`. It runs `fetchDocs({projectId, limit: 200})` for every project in parallel (`Promise.allSettled`) once the project list has loaded. A failed project's group is omitted and a single warning line shows. The fan-out is keyed on the sorted project-id set, so a `ProjectsContext` reload or rename does not re-fetch, and a changed set keeps the previous groups on screen while it re-fans-out (`loaded`). `doc_list_changed` re-runs it behind a 600 ms trailing debounce, so a burst of model writes costs one fan-out. Project groups follow "Your docs" and "Shared with you", ordered by project name (`groupDocs`).

The view waits for the first page of both paged lists and for the project index before choosing between rows and the empty state, so a user whose docs are all project or shared docs never sees "No docs yet." flash. The filtered view (`?project=<id>`) uses only `useDocs({projectId, limit: 50})` and has no shared group. The trade-off of the fan-out is one request per project on open and per debounced `doc_list_changed` burst, and at most 200 docs per project in the unfiltered view (the filtered view pages). Between 769px and 1024px the Scope and Size columns are hidden so the title keeps room.

`NewDocModal.tsx` collects a title, an optional description and a location ("Your docs" or a non-archived project, preselected to the filtered project). There is no mode picker and no `mode` is ever sent (`CreateDocRequest` in `api/types.ts` has none): a user doc is always private and a project doc takes its project's mode, so `project_doc_mode_inherited` cannot happen. Only a public project gets the read-only "Mode: Public — inherited from the project" line. On success the view navigates to the new doc's viewer.

### Viewer

`DocViewer.tsx` (`useDoc`) has loading, not-found ("This doc no longer exists." with an All docs link; also what a deleted doc or a revoked share turns into), error-with-Retry and empty-doc states. The doc renders in a centered column (820px max).

The body area has three modes, kept per doc id (opening another doc starts in `view`), with the root class `doc-viewer doc-viewer--<mode>`: `view` (the rendered doc or its source), `edit` (`DocEditor`, see [Editor](#editor)) and `history` (`DocHistory`, see [History view](#history-view)). A save or restore response carries the new body and is merged with `useDoc.applyContent()`, which also re-fetches when `last_write_source` changed (for the footer's conversation link) or a fetch was in flight; the viewer then returns to `view` and focus goes back to the title button. The editor's `onStale` (a 409) makes the viewer re-fetch. A History copy navigates to the new doc. `DocShareDialog` (owner only, `can_share`) opens over any mode.

**Saved drafts.** While the editor is closed, the viewer re-reads this doc's draft backup (`utils/docDraftBackup.ts`; also on a `storage` event from another tab, and `pruneDocDrafts()` on mount):

- in view mode, with edit access, a draft that differs from the doc shows "You have unsaved changes from <time>." with Resume editing;
- without edit access any more (a share downgraded), or when the doc 404s (deleted, share revoked), the draft shows in `DocDraftRecovery`: the text read-only, with Copy, Download .md (a client-side Blob named after the doc's last known title) and Discard behind a confirm, so the text is never silently lost;
- the owner's own Delete (after its confirm) removes the doc's draft.

An empty doc's text depends on reach: "Ask Quest to add to it" only when the viewer's conversations can write the doc (the owner, or a write-share recipient of a user doc; a read share's conversations can only read it, and a shared project doc stays hidden outside its project), plus the Edit hint with edit access.

`DocHeader.tsx` follows `ConversationHeader.tsx`: the title plus a chevron opens a dropdown, single-key hints act while it is open, and Rename swaps in an inline input. The items follow the row's `access` flags:

- **Rename** (R, `can_rename`): sends `expected_updated_at`. A `stale_update` 409 applies the error's `current` row through `useDoc.applyRow()` and shows a notice. `duplicate_title` / `invalid_title` keep the input open.
- **History** (H, `can_edit`, view mode only; a read share gets no History, see [History](#history)).
- **Require approval for agent writes** (A, `can_require_approval`, owner only): a `menuitemcheckbox` mirroring the row's `require_approval` (access rule 7). It PUTs the wanted value through the rename endpoint **without** `expected_updated_at` (a body edit landing meanwhile is no reason to refuse a settings flip; the same value twice is a server-side no-op), applies the returned row, shows a notice and keeps the menu open so the new state is visible. While the switch is on, every viewer sees an "Approval required" chip beside the title. On a public doc the item's tooltip adds that public-project conversations cannot open approval cards, so the switch leaves the doc read-only for them.
- **Download Markdown** and **Download with images (.zip)**: plain `<a download>` links on `docDownloadUrl`, always offered.
- **Delete** (D, `can_delete`): behind a `DocConfirmDialog`, then navigates to the doc's list (`docsListPath(project_id)`).

Edit and Share are not menu items: they are the two actions a reader reaches for most, so they sit as buttons on the right of the header (see below). There is no mode-switch item for any doc: the header ignores `access.can_switch_mode`, which the server always sends as false ([Mode is fixed at creation](#mode-is-fixed-at-creation)). A read-share recipient therefore gets only the downloads. Beside the title sit:

- the `DocModeBadge` (public docs only);
- for a project doc, a folder chip that links to `/docs?project=<id>` when the project is in the viewer's own project list, and is plain text otherwise ("Project doc" for a doc shared from someone else's project, whose docs list would 404);
- a share chip (`utils/docSharing.ts`): for the owner of a shared doc, "Shared with N people" / "Shared with everyone" (every grant in the tooltip), which opens the Share dialog; for a recipient, "Shared by <owner> · Can edit / Can view" as plain text.

On the right sit the header's action buttons, in one outlined style (icons only at phone widths): **Edit** (`can_edit`, view mode only), **Share** (`can_share`, every mode) and, in view mode only, the Show source / Show rendered toggle (remembered per doc id), which swaps the body for a `<pre>` of the raw markdown.

The body is `ReactMarkdown` with the same plugin set as `FileViewerModal` and the chat's shared `markdownComponents`, inside `.message-content` for the chat typography, wrapped in `MarkdownWorkspaceContext.Provider` with `assetBase = docAssetBase(id)`. With `assetBase` set, `MarkdownImage` changes its resolution rule:

- only `assets/<name>` resolves (exactly one segment, no leading dot, after the usual percent-decode and `./` / `/` / `workspace/` strip), to the cookie-authed `GET /app/api/docs/{id}/assets/{name}`;
- any other relative src renders the missing-image chip, and `conversationId` is ignored;
- external srcs stay click-through links and are never auto-fetched, as in chat;
- SVG never renders, because the asset store holds only magic-byte-sniffed raster images and the asset route serves only the raster `_INLINE_IMAGE_MIMES` types.

The viewer passes no `onOpenImage`, so doc images are not click-to-enlarge.

The footer reads "Last written by X · Updated <relative>", with X from `last_write_source` and `last_write_user` (`lastWriter()` in `DocViewer.tsx`):

- `ui` (legacy, the owner): "you";
- `ui:<user_id>`: "you" when `last_write_user` is the viewer (compared by email, since `GET /me` exposes no user id), else that person's name or email, "a deleted user" when `last_write_user` is null;
- `action_request:<n>`: "<name> (approved change)" when `last_write_user` is set (a share recipient's approved card; "a deleted user (approved change)" when its name and email are null because the proposer is gone), else "action request #n";
- `conversation:<id>`: the conversation's title from the row's `last_write_conversation` (`{id, title, project_id}`, resolved by `GET /docs/{id}` from one conversations-row lookup -- the viewer never fetches the chat history for a title), linked to `/chats/<id>` or `/projects/<pid>/<id>`; null (the conversation is gone) reads "a deleted conversation";
- null or anything else: the writer phrase is left out. Null covers every non-owner, since the API blanks both fields for them.

### Share dialog

`DocShareDialog.tsx` (ModalShell; rendered by the viewer only for `can_share`) has four parts:

- a "Name or email" field with a debounced (300 ms, at least 2 characters) `/users/search` typeahead, a Can view / Can edit select and Add. Text without an "@" is never sent: a single suggestion is picked, otherwise the user is asked to pick one or type the full email;
- an "Everyone on this install" select (No access / Can view / Can edit). Widening it (a new everyone grant, or view -> edit) goes through a `DocConfirmDialog`, because a native select fires `change` on arrow keys and keyboard browsing must never grant install-wide access by itself;
- the roster of per-person grants, each with a permission select and a remove button. A deleted account's grant can only be removed, since grants upsert by email;
- notes from `shareDialogNotes(doc)` explaining the matrix consequences for this doc: approval for conversation writes once a private doc is shared, recipients' conversations never using project docs, and edit access including History from before the share.

Every write returns the owner's row. It goes to `useDoc.applyRow()` and shows at once from a local roster that yields as soon as `doc.shares` changes. Only one request runs at a time: while it is in flight the controls are `aria-disabled` (not `disabled`, which would drop focus) and the dialog cannot be closed, so no result goes unreported. A failure shows the server's message. After each successful grant the add row's permission resets to "Can view", so edit access chosen for one person is never carried over to the next. Focus starts in the field, returns to it after Add, moves to the next row's remove button after a removal and to the everyone select after its confirm, and goes back to the opener on close.

### Editor

`DocEditor.tsx` (edit mode, `can_edit`): a toolbar (Image, Preview, Cancel, Save) and a formatting bar over a monospace textarea and a live preview rendered exactly like the viewer body. The formatting bar (`role="toolbar"`, hidden on the phone Preview pane) has icon buttons for Heading 1-3, Bold, Italic, Strikethrough, Inline code, Link, Bulleted list, Numbered list and Quote; Bold, Italic and Link also answer Ctrl/Cmd+B, I and K in the textarea. The edits are pure functions in `utils/markdownFormatting.ts` (unit-tested without a DOM) and every one toggles: inline styles wrap the selection -- edge whitespace stays outside the markers, a wrapped selection or markers just around it unwrap, a bare caret gets a selected placeholder; a line style is applied to every line the selection touches (blank lines skipped, an existing heading / list / quote prefix replaced, numbered lists renumbered) or removed when every touched line already has it; Link makes `[text](url)` with the url placeholder selected, or, for a selected URL, the text placeholder. Ctrl/Cmd+S saves while focus is in the editor or on the bare page, never from another field (such as the header's rename input) or under a dialog. The preview follows the draft 250 ms after typing stops, so half-typed image names cause no asset requests. Above ~200 KB it updates only on "Refresh preview". On desktop the two sit side by side and the preview can be hidden, a choice remembered per browser in localStorage under `quest_doc_editor_preview`; phones switch between Write and Preview and never autofocus. Tab / Shift+Tab indent and outdent, Escape releases the Tab trap, and a read-only textarea never traps Tab. Programmatic edits go through `execCommand('insertText')` so browser undo keeps working.

- **Token**: the doc's `updated_at` when editing started, sent as `expected_updated_at`; the editor also remembers the body that token stands for. On a 409 `stale_update` it re-reads the doc and tells the viewer to re-fetch (`onStale`). When the server body still equals the body its token belongs to, only metadata moved (a rename, someone else's image upload or delete), so it saves once more with the fresh token and shows nothing. That silent retry is skipped when an image the draft added (referenced now, not in the starting body) is no longer among the doc's assets, e.g. deleted by the owner meanwhile: the editor says "An image you added was deleted meanwhile: …", keeps the draft and the old token, so the next save runs the check again. Otherwise a conflict banner offers "Overwrite with my version" (the replaced version stays in History) or "Discard my changes". When the doc's body moves on while the editor is dirty (a realtime re-fetch), a non-blocking banner offers "Discard my changes" (reload the latest version) or "Keep editing", but not while an upload or save is in flight; an untouched editor simply follows the new version. A 403, or a re-fetched row without `can_edit`, locks the editor with a "no longer have edit access" banner.
- **Images**: the Image button, pasting or dropping an image file uploads it (`uploadDocAsset`). A file over 5 MB is refused client-side before any request. Otherwise an `![Uploading <name>…]()` placeholder goes in at the cursor and is swapped for the returned markdown when the upload lands. If the placeholder was deleted meanwhile, the markdown is appended at the end with a notice; on failure the placeholder is removed. Uploads run one at a time, and the editor adopts the response's `updated_at` (as its token and as a pending conflict's token) only when `previous_updated_at` equals the token it holds, so its own upload never causes a false conflict.
- **Unsaved changes** (the draft differs from the starting body, or an upload is in flight): `useUnsavedChangesGuard` adds the browser's leave prompt on unload and intercepts same-origin `<a href>` clicks in the capture phase. Cancel, an intercepted link and both banners' "Discard my changes" all go through one "Discard your unsaved changes?" confirm. Browser back / forward and button-driven navigation (e.g. the Sidebar rows, which call `navigate()`) cannot be intercepted under a plain `BrowserRouter`, so the draft is also backed up per user in localStorage (`utils/docDraftBackup.ts`, key `quest_doc_draft:<encoded lower-cased email>:<docId>`), written about 500 ms after the last change and when the editor unmounts or the page is hidden, and cleared on save or discard. Drafts older than 30 days are pruned, and every draft is cleared on logout, logout-and-disconnect and account delete (`SignOutSection.tsx`), so none outlives the session in a shared browser. Opening the editor with a backup that differs from the doc offers "Restore unsaved draft from <time>" (confirmed when the editor already has changes). Until the user decides, the offer stays in memory and the key backs up the live draft; leaving puts the offered draft back. Restoring also restores the backup's token, so saving over a doc that moved on goes through the conflict flow. A draft the editor can no longer take back is shown by the viewer ([Viewer](#viewer)).

### History view

`DocHistory.tsx` (history mode, `can_edit`) shows a version list ("Current version" first, then the revisions newest first, each with when, who -- `source_label`, linked to its conversation when the server sent one -- and size) beside the selected version, rendered like the viewer body with images resolved against the doc's current assets (a deleted image shows the missing-image chip). Once a revision's body has loaded, it offers:

- "Show changes": the server's bounded diff to the current body, rendered by `SkillContentDiffPreview`.
- "Restore this version": a confirm, then restore with the token of the current body as it was when the confirm opened, so a write landing behind the dialog is a 409, never a silent overwrite. After a `stale_update`, History fetches the doc itself and shows that body (and uses its token) as "Current version" until the doc prop catches up, so a retry never sends a token for a body the user has not seen. A pruned revision closes the confirm with a notice and refreshes the list.
- "Copy to a new doc": a title dialog where empty means the server's default, then the viewer navigates to the new doc.

The list re-fetches whenever the current token moves, and a selected revision that drops out of a refreshed list is reported gone. Revision bodies are immutable, so they are cached by id (the last `BODY_CACHE_SIZE`) and their fetch does not depend on the token; diffs are cached per current-body token, and numbered fetches drop late answers. A 403 from any call, or `can_edit` turning false, replaces the view (and closes any dialog) with a "no longer have edit access" state. Phones show the list alone and open a version full width with an "All versions" control. Escape steps back (phone detail -> list -> doc). `DocHistory.css` widens the viewer column and hides the Assets aside while History is open.

### Assets panel

`GET /docs/{id}` lists the doc's images as `assets: [{name, size, mime}]` (regular files directly in `assets/`, sorted by name, hidden names / symlinks / directories skipped, names outside `[A-Za-z0-9._-]+` -- all `add_asset` ever produces -- skipped so a hand-planted name can never break the JSON or the asset route's headers, MIME from `_INLINE_IMAGE_MIMES` by extension, built in a thread). It rides on the detail payload rather than a separate endpoint because the per-doc cap is 200 assets (a few KB at most) and the viewer always needs both. `DocAssetsPanel.tsx` renders them as a `.right-panel-card` in a 280px right gutter beside the doc column (the chat RightPanel's footprint): a lazy 40px thumbnail from `GET /docs/{id}/assets/{name}` (an icon when it fails), the filename and size, "No images in this doc." when empty; a click opens `DocImageLightbox.tsx` (ModalShell, the full image, name + size, a Download link). At 1024px and below the card drops under the body; on phones (`useIsMobile`) a collapsed "Assets (N)" `<details>` section replaces it so the document stays first. A read-only viewer's list holds only the images the current body references ([Sharing](#sharing)). For the owner in view mode (`can_delete_assets`; not while editing, since the draft may be about to reference the image) each row has a delete button: a `DocConfirmDialog` (warning that older versions using the image will show it missing), then `deleteDocAsset`, then a viewer re-fetch. A 409 `asset_in_use` shows "Remove it from the text first" inside the dialog.

### write_doc card

Both card renderers (`ActionRequestMessage.tsx` and `RequestsView.tsx`) go through `ActionRequestPreviewFields.tsx`:

- **`doc_image` field**: `DocImagePreview.tsx`. At card time the image is still a workspace file of the proposing conversation (project conversations share the project workspace, so the card's conversation id always resolves it), so the lazy thumbnail loads from `GET /conversations/{id}/files/download`. A load failure falls back to a path chip, and a click opens `FileViewerModal`. The caption shows the asset name, the size and "Stored as `assets/<name>`". The markdown line is labeled "Appended:", or "Markdown:" for `placement: "none"` (read from the request params).
- **Bounded `skill_content_diff`**: `SkillContentDiffPreview.tsx` detects the window by the presence of `total_old_lines` / `total_new_lines`. It draws "N lines above / below not shown" edge separators computed from the first and last emitted line numbers and the totals. The toggle reads "Show context lines", since expanding can only reveal the window's own context lines. A `truncated` diff adds a note that the +/- counts are still exact. Whole-body diffs (`edit_skill`, `edit_routine`; no totals) render as before.

The Mode row stays plain text. The resolved "Applied" label and the snippet come from the server, so they need no frontend code.

### Realtime consumers

All three subscribe through `persistentWebSocket.onGlobalEvent`, only while enabled:

- `useDocs`: a silent `refresh()` of the loaded window (one request, `limit = min(200, max(page size, loaded))`) `DOCS_REFRESH_DEBOUNCE_MS` (300 ms) after the last `doc_list_changed`, so a burst (e.g. a conversation writing a doc shared with everyone, which reaches every connected user) costs one re-fetch per list.
- `useProjectDocsIndex`: re-runs the fan-out behind its own 600 ms debounce. A project whose refresh fails keeps the rows it had.
- `useDoc`: re-fetches on `doc_changed` for its own doc when `updated_at` differs from the one shown, which a `null` always does. That is how a delete, a revoked share or a project delete (`doc_changed {updated_at: null}`) reaches an open viewer: the re-fetch 404s and the viewer shows `notFound`. It ignores `doc_list_changed`, which is broadcast install-wide for writes to everyone-shared docs and is meant for the lists.

Share recipients get the same events as the owner ([Realtime](#realtime)), so their lists and viewers update live too.

### Phone

`MobileShell.tsx` receives the parsed `docsRoute` and `docsEnabled` from `App.tsx` and renders the same three views (or `DocsGateClosed`) in `.mobile-main`. It closes the nav drawer and the workspace drawer whenever the docs route changes, and shows no workspace button on docs routes. The views use 16px gutters at phone width, and All Docs rows stack.

## Lifecycle

- **Doc delete** (`DELETE /docs/{id}`, owner only, `service.delete_doc_from_ui` under the per-doc write lock): the audience is captured, then the row goes (shares with it), then a best-effort `delete_doc_dir()`, then `doc_changed {updated_at: null}` and `doc_list_changed` to the captured audience.
- **Share revoked**: the recipient loses access at once (see [Sharing](#sharing)). Their History copies are their own docs and stay.
- **Image deleted** (owner, not referenced by the current body): gone from `assets/` for every version; older revisions that referenced it render a missing image.
- **Project delete** (`delete_user_project` in `chat/project_routes.py`): `list_docs_for_project()` (rows with their shares) runs before the project row goes. The `docs.project_id` CASCADE removes the rows and shares, then `_delete_doc_dirs()` sweeps the directories (failures logged, never raises), and the route publishes `doc_changed {updated_at: null}` per doc to its own audience, then `doc_list_changed` to the owner plus every recipient of those docs (every connected user if one had an everyone share). The sweep runs BEFORE the unguarded conversation-directory `rmtree` loop, so a failure there cannot leave doc files behind.
- **Account delete** (`delete_account` in `chat/routes/user.py`): `list_doc_ids_for_user()` (user and project docs alike) runs before any row delete. The directories are swept in a thread after the cascades and before the unguarded conversation/project `rmtree` loops, for the same reason. Share rows granted to the deleted user cascade away; an everyone row on another user's doc survives. No event goes to the recipients of the deleted user's docs ([Known Limitations](#known-limitations)). A roster entry or History label for a deleted user reads "Deleted user" / "a deleted user".
- **Project archived**: no effect. **Conversation deleted**: no effect, and `last_write_source` becomes a dead reference. **Convert chat to project / duplicate workspace**: user docs stay user docs.
- Orphan directories (a crash between layout and insert, or a failed best-effort sweep) are not swept by anything; they are invisible because no row points at them.

## Script Bridge

Sandbox scripts (`run_script` / `run_python` through `POST /api/tool-call`, see [Gemini API -- Script tool-call bridge](gemini-api.md)) get the three reads only (`SCRIPT_TOOL_CALL_ALLOWLIST`). The bridge dispatches with `is_script=True` -> `run_kind="script"`, and `_doc_caller()` gives scripts no conversation, no project and `is_public=False`. As a result:

- scripts see only user docs (project docs are Hidden), so never a public doc, since every public doc is a project doc;
- every write is denied (`DENY_SCRIPT`);
- reads are not recorded in any sidecar;
- scripts are always private callers.

The last point is safe because public containers get no sandbox token, so they have no bridge at all. The doc routes (all four routers) live under `/app/api`, outside both `curl_proxy_*` reach (which is limited to `/api/*`) and the sandbox port's route roster, so neither the model nor a script can use the `ui` run kind, and so neither can reach the UI's whole-body replace, image upload, asset delete, sharing or restore.

## Constraints

- Caps (`chat/docs/constants.py`): body 1 MB, image 5 MB, 200 images / 100 MB per doc, `read_doc` page 200,000 chars, title 200 / description 500 chars, search scan 50 MB, 3 snippets of ~200 chars per doc, `list_docs` limit 1..200 (default 50), `search_docs` limit 1..50 (default 20).
- Raster images only (png, jpg/jpeg, gif, webp), sniffed by content. SVG and anything else is refused.
- The write locks are in-process (one `asyncio.Lock` and one `threading.Lock` per doc id), which is correct for the single-process server. So is the everyone-share fan-out, which lists the connections of this process (`bus.connected_user_ids()`).
- UI request bodies: content PUT up to `DOC_MAX_CONTENT_SIZE` x 6 + 64 KiB of JSON, image upload up to `DOC_MAX_IMAGE_SIZE` + 64 KiB of multipart (see [Human Editing](#human-editing)).
- History lists at most `DOC_REVISION_MAX_COUNT` (200) snapshots of the last 30 days plus the newest one, unpaged.
- Doc bodies, assets, revisions and the `doc.meta.json` / sidecar attribution files are plaintext on disk, like workspaces (see [Encryption at Rest](encryption-at-rest.md)).

## Design Decisions

**Why can a user doc never be public?** A doc may only become public by being born in a public project, so no internal content can ever be promoted into the public partition. A user doc is reachable from every private conversation and may hold internal data. Switching it to public would hand that body to the internet-enabled public sandbox, and it would take a switch-time re-check to keep taint safe: ordering the flip against queued model writes under the doc lock, dropping the approval gate for write-share recipients, and resolving title collisions in the target mode. With the mode fixed at creation, the taint invariant holds by construction and none of that is needed. The cost is that spec 4.2's user-doc mode switch and its 8.4 dialog are not offered; a user who wants a public doc creates it in a public project.

**Why scope title uniqueness per mode?** If private and public user docs shared one title namespace, a public conversation could probe `create_doc(title=...)` and learn from the collision error whether a private doc with that title exists. That would break invisibility. With per-mode scoping, a public conversation only ever collides with public titles. Now that user docs are always private and every project's docs share the project's mode, each (owner, project) scope holds a single mode and the mode key is a belt-and-braces no-op for new rows. It mattered only for legacy public user docs, which is why migration `e1b7c4d9a2f6` renames a flipped row whose title would collide with a private one.

**Why does a shared private doc need approval, but a shared public doc does not?** The approval card protects internal data that has several stakeholders. A public doc only ever holds sandbox-originated content, every writer is itself a credential-less public conversation, and the recipients opted into `write`. No doc's mode changes, so a private doc never loses its approval gate.

**Why one access function?** The matrix has enough cells (mode x relationship x conversation kind x run kind) that re-deriving any part of it at a call site would drift. Tools, routes, the pre-card, the execute and the prompt all consume one `DocAccess`, and one table-driven test pins it.

**Why are sub-agents, inference runs, cross-user subagents and scripts read-only?** Writes stay with the run that owns the conversation and its approval surface. Sub-agents cannot open action requests, so they return content to the top-level agent, which writes it. Inference and cross-user subagent runs must not change anything outside their own workspace, the same rule that refuses the other mutating tools there. Scripts have no conversation context to scope or attribute a write to.

**Why search/replace + append instead of full replace?** A full replace lets a model silently drop content it never read. An exact match keeps every change local and reviewable (the same diff powers the approval card), and append covers the main aggregation use case without a read.

**Why files on disk instead of DB rows?** A doc is a portable artifact: `doc.md` plus `assets/` downloads as-is (md or zip) with working relative image links, and revisions are plain files.

**Why move Swagger UI instead of picking another SPA path?** `/docs` and `/docs/<id>` are the user-facing deep links. FastAPI registers its docs routes inside the constructor, ahead of every `@app.get`, so the default `docs_url` would always shadow the SPA route. Nothing but the `run.py` banner pointed at Swagger UI, and `/openapi.json` stays where it was.

**Why is the URL the only docs state?** Deep links, reload and back/forward then work without a URL-to-context sync effect. `showRequestsView` behind `/inbox` is the legacy exception that the docs routes deliberately did not copy.

**Why does All Docs fan out per project?** The list endpoint returns the user's own user docs, one project's docs, or the docs shared with the user, but has no "all my projects" stream. One `fetchDocs` per project reuses the existing endpoint and its access rule, and lets one failing project degrade to a missing group instead of failing the view. A cross-project list endpoint would replace `useProjectDocsIndex` without changing `DocsListView`'s grouping. The shared docs, by contrast, are a server-side keyset stream, because they span other users' projects the viewer cannot list.

**Why are human edits not approval-gated when model writes are?** The `write_doc` card exists so a person reviews what a model is about to write into a doc others rely on. A UI save is already a person acting directly, either the owner or someone the owner granted write access, so a card would only ask them to approve themselves. The full-replace ban (invariant 5) is about models, and the replace route stays out of every model-driven channel's reach.

**Why is the editor a whole-body replace with an optimistic token, not operational edits?** One human editor at a time is the common case. `expected_updated_at` (the same token as rename) turns a concurrent write into a 409 the editor can resolve (overwrite or discard), and every replaced body is snapshotted, so an overwrite is always recoverable from History. Model writes, which are search/replace or append, land between saves and surface through the token.

**Why is History for editors only?** A revision can hold text the owner removed before sharing. A read recipient who could list, read and copy revisions would see it and could keep it forever. Write recipients can already change the whole doc, so they get the full history, like Google Docs editors. For the same reason a read-only viewer sees only the images the current body references.

**Why does a copy always become a private user doc of the caller?** A recipient cannot write into the owner's project, and the caller is the one who asked for it. A private user doc is taint-safe from any source (public -> private is allowed, the reverse never is), so copying out of a public-project doc creates no path for internal content into the public partition.

**Why does an everyone share broadcast to every connected user?** The audience of an everyone row is the whole install, and enumerating it per recipient (with each recipient's gates) would cost a user lookup per write. `bus.connected_user_ids()` is an in-process snapshot, the events carry only an id and a timestamp, and the client's follow-up fetch goes through the access rule. The cost is described under [Known Limitations](#known-limitations).

## Not Implemented

These are designed for but not built:

- **FTS5** behind `search_docs`. Search is the bounded scan above.
- **Pinned docs**: a per-project list of docs auto-injected into the prompt (the autoload-skill analog), if tool-only discovery proves insufficient.
- **Doc-to-doc links**: `quest-doc://<id>` resolved by the renderer.

The UI also never edits a description, although `PUT /docs/{id}` accepts one.

Deliberately not built (not planned): spec section 4.2's user-doc mode switch and section 8.4's mode-switch dialog. User docs are always private and no doc's mode can change ([Mode is fixed at creation](#mode-is-fixed-at-creation)); `PUT /docs/{id}/mode` remains only as a route that refuses every doc.

## Known Limitations

- **Account deletion sends no event to recipients.** Their lists refresh on their next `doc_list_changed` or reload. An open viewer of one of the deleted user's docs keeps showing it until it re-fetches (a reload, or any action that hits the server), since no `doc_changed` comes for it; the re-fetch then 404s.
- **The everyone-share broadcast ignores gates and costs one list refetch per connected user per write burst.** `doc_list_changed` / `doc_changed` for a doc shared with everyone go to every connected user without consulting their `docs` gate. A user who cannot open the doc learns only its id and `updated_at`, never content or a title, because the follow-up fetch 404s. Every write to such a doc makes every connected user's open doc lists re-fetch (`useDocs` debounced 300 ms, the All Docs project index 600 ms). Open viewers are not affected: `useDoc` ignores `doc_list_changed`, and only a tab showing that very doc re-fetches on its `doc_changed`.
- **A read-only viewer's row still carries the doc's total `asset_count`.** The detail, asset route and zip are filtered to the images the current body references, but the row (shown in All Docs' size column) counts every image in `assets/`. It reveals a count, never content.
- **Button navigation is not intercepted by the editor guard.** Under a plain `BrowserRouter` there is no `useBlocker`, so browser back / forward and `navigate()` from buttons (e.g. the Sidebar rows) leave the editor without a confirm. The per-user localStorage draft backup (`quest_doc_draft:<encoded email>:<docId>`) covers them: the viewer offers Resume editing, and reopening the editor offers to restore the draft. The backup lives only in that browser, for 30 days or until logout.
- **An asset delete can break older revisions.** `asset_in_use` checks only the current body. A revision that referenced the deleted image shows a missing image in History, and restoring it brings back a broken reference; a History copy skips the missing file.

## Testing

`tests/test_docs_access.py` (every matrix cell and edge rule; the Public rows are public-project docs, plus legacy public user-doc rows that must evaluate as private), `tests/test_docs_store.py` (metadata store, title scoping, shares, the public-user-doc refusal, and migration `e1b7c4d9a2f6` incl. the `(formerly public)` renames), `tests/test_docs_storage.py` + `tests/test_docs_storage_hardening.py` (resolver, sidecar, files layer, symlinks, revisions), `tests/test_docs_service.py` + `tests/test_docs_service_hardening.py` (service pipeline, locking, cancellation, user docs always private incl. the public-conversation `create_doc` refusal), `tests/test_docs_tools.py` (handlers, dispatch, registry, allowlists, public dispatch refusing user docs), `tests/test_write_doc_action_request.py` (handler, pre-card, round-trip, TOCTOU), `tests/test_docs_prompting.py` (gate-aware prompts and skill, account-delete sweep), `tests/test_docs_routes.py` (HTTP routes incl. `user_doc_mode_private` on create and the always-refused mode route, project-delete sweep and its per-doc null `doc_changed`), `tests/test_docs_review_followups.py` (bounded card diff, the locked delete waiting for an in-flight write, the mode route refusing every doc, the image sha256 pin, the missing-project fail-closed), `tests/test_docs_sharing.py` (share routes and their error codes, the enriched rows and access flags, the `shared=true` stream, recipients' reach from their conversations incl. a recipient `write_doc` card end to end and the owner's writes flipping to approval, the realtime audience incl. the null-`updated_at` `doc_changed`, deleted users and gone card proposers, read-only image filtering with its `(doc_id, updated_at)` cache, image writes keeping `last_write_source`, the in-use check off the event loop, `invalid_doc_files` from the content PUT and asset delete on a broken `doc.md`), `tests/test_docs_editing.py` (content PUT incl. tokens, identical bodies, snapshots, concurrent saves and a share revoked mid-save; image upload validation, naming and capped reads; asset delete incl. traversal / symlinks and `asset_in_use`), `tests/test_docs_history.py` (listing and every `source_label` rule per viewer, revision reads and diffs, malformed / pruned / symlinked ids, editor-only access, restore incl. TOCTOU, copy incl. titles, assets and cleanup) and `tests/test_docs_revision_meta.py` (the `doc.meta.json` / sidecar attribution). Also `tests/test_public_projects.py` (public prompt with and without docs), `tests/test_inference_api.py` (mutating roster) and `tests/test_spa_docs_routes.py` (the `/docs` SPA routes ahead of the static fallback, Swagger UI / ReDoc / OAuth2 redirect moved, `/openapi.json` unchanged; runs `quest` in a subprocess).

Frontend (vitest, `npm test`, files under `frontend/src/`): `api/docsApi.test.ts` (URL builders, stale_update helpers), `hooks/useDocs.test.tsx` and `hooks/useDoc.test.tsx` (paging, the shared stream, debounced refresh on the realtime events, superseded responses, `useDoc` ignoring `doc_list_changed` and turning a `doc_changed` with a null `updated_at` into `notFound`, `applyRow` re-fetching on a newer token but not for a share change, `applyContent`), `hooks/useUnsavedChangesGuard.test.tsx` (which link clicks are intercepted), `utils/docsRoute.test.ts`, `utils/sidebarDocs.test.ts`, `utils/allDocsGrouping.test.ts` (incl. the "Shared with you" group and owner search), `utils/docSharing.test.ts`, `utils/docMode.test.ts` (the public-only badge rule), `components/sidebar/DocsSection.test.tsx` (public-only row badges), `components/docs/DocsListView.test.tsx` (grouping, the shared group and its paging, search, failed-project warning, filtered view, public-only badges and the Mode column label, New Doc with no mode picker sending no `mode`), `components/docs/DocHeader.test.tsx` (the Edit / Share header buttons and menu items by access flags, the share and project chips, no mode switch even when the flag says otherwise, rename incl. the 409 paths, delete, the public-only badge), `components/docs/DocViewer.test.tsx` (states, modes, Show source, footer writers incl. `ui:<id>` and recipients' approved changes, saved drafts: resume notice, read-only recovery after a 404 or lost edit access, the owner's delete dropping the draft), `components/docs/DocShareDialog.test.tsx`, `components/docs/DocEditor.test.tsx` (save, conflicts incl. the silent metadata-only retry and its skip for a deleted added image, uploads, the formatting bar and its shortcuts, guard and draft backup), `utils/markdownFormatting.test.ts` (the toggling inline / line / link edits), `components/docs/DocHistory.test.tsx` (list, detail, diff, restore, copy, lost access), `components/docs/DocAssetsPanel.test.tsx` (incl. the owner's delete), `components/docs/DocConfirmDialog.test.tsx` (naming and focus), `utils/docDraftBackup.test.ts` (per-user keys, pruning, clearing), `components/settings/SignOutSection.test.tsx` (drafts cleared on every sign-out path), `components/MarkdownImage.test.tsx` (`assetBase` resolution vs the conversation workspace), `components/DocImagePreview.test.tsx` (incl. the `ActionRequestPreviewFields` branch) and `components/SkillContentDiffPreview.test.tsx` (bounded window vs whole-body diffs).
