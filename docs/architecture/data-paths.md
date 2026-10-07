# Data Paths

## Overview

All data-directory path constants are centralized in `config/paths.py`. Modules that need to reference files inside the data directory import from this module instead of computing their own paths.

## Key Files

| File | Description |
|------|-------------|
| `config/paths.py` | Defines `PROJECT_ROOT`, `DATA_DIR`, `DATABASE_PATH`, `CHATS_DIR`, `PROJECTS_DIR`, `DOCS_DIR`, `SECRET_KEY_FILE`, `LOG_DIR`, `SERVICE_CREDENTIALS_DIR` |
| `server_config.json` | Optional `data_dir` key to relocate the data directory outside the source tree |
| `db/engine.py` | Imports `DATABASE_PATH` from `config/paths` |
| `auth/config.py` | Imports `SECRET_KEY_FILE` and `PROJECT_ROOT` from `config/paths` |
| `chat/storage.py` | Imports and re-exports `CHATS_DIR` and `PROJECTS_DIR` from `config/paths` (backward compat); hosts the ONLY id-to-path resolvers (`ChatStorage.get_conversation_dir` / `get_project_dir` / `get_project_db_path` / `get_conversation_workspace_root` / `get_project_workspace_root` / `get_doc_dir`) and `InvalidStorageIdError` |
| `chat/conversation_access.py` | Ownership-aware accessors for HTTP routes (`require_owned_conversation`, `resolve_owned_workspace`, `require_owned_project`, `resolve_owned_project_workspace`) that fuse the DB ownership lookup with the validated path |
| `chat/logging_config.py` | Imports `LOG_DIR` from `config/paths` |
| `quest.py` | Imports `DATA_DIR` from `config/paths` |
| `alembic/env.py` | Imports `DATABASE_PATH` from `config/paths` to override `alembic.ini` URL |
| `run.py` | Has its own `get_data_dir()` (stdlib-only, pre-import) that reads the same `data_dir` key from `server_config.json` |

## Data Directory Resolution

The data directory defaults to `PROJECT_ROOT / "data"` but can be overridden by setting the `data_dir` key in `server_config.json`. Relative values are resolved against `PROJECT_ROOT`; absolute values are used as-is. The resolution logic is in `_resolve_data_dir()` in `config/paths.py`.

`run.py` has an independent `get_data_dir()` function that mirrors the same resolution logic using only the standard library, because it runs before heavyweight packages are imported.

## Exported Constants

| Constant | Default Value | Used For |
|----------|--------------|----------|
| `PROJECT_ROOT` | Repository checkout directory | Base for resolving relative paths |
| `DATA_DIR` | `PROJECT_ROOT / "data"` | Root of all persistent application data |
| `DATABASE_PATH` | `DATA_DIR / "quest.db"` | SQLite database file |
| `CHATS_DIR` | `DATA_DIR / "chats"` | Per-conversation directories |
| `PROJECTS_DIR` | `DATA_DIR / "projects"` | Per-project directories (shared workspaces) |
| `DOCS_DIR` | `DATA_DIR / "docs"` | Per-doc directories (`doc.md`, `assets/`, `revisions/`; see [Quest Docs](quest-docs.md)) |
| `SECRET_KEY_FILE` | `DATA_DIR / "secret_key"` | Cookie signing key file |
| `LOG_DIR` | `DATA_DIR / "logs"` | Application log files |
| `SERVICE_CREDENTIALS_DIR` | `DATA_DIR / "service_credentials"` | Per-service upstream credential files (see [Service Credentials](service-credentials.md)) |

## Conversation, Project and Doc Path Resolution

Turning a `conversation_id` / `project_id` / `doc_id` into an on-disk path happens in exactly one place: the resolvers on `ChatStorage` in `chat/storage.py`. Production code never joins `CHATS_DIR` / `PROJECTS_DIR` / `DOCS_DIR` with an id itself (Alembic migrations keep their own historical joins -- they run over trusted rows and directory listings, not request input).

| Resolver | Returns |
|----------|---------|
| `ChatStorage.get_conversation_dir(conversation_id)` | `CHATS_DIR / conversation_id` (the legacy `_get_conversation_dir` name is an alias) |
| `ChatStorage.get_project_dir(project_id)` | `PROJECTS_DIR / project_id` |
| `ChatStorage.get_project_db_path(project_id)` | `PROJECTS_DIR / project_id / project.db` |
| `ChatStorage.get_conversation_workspace_root(conversation_id)` | `CHATS_DIR / conversation_id / workspace` -- the conversation workspace, the browsable root itself, for every conversation incl. project conversations (not created) |
| `ChatStorage.get_project_workspace_root(project_id)` | `PROJECTS_DIR / project_id / workspace / workspace` -- the project workspace shared by the project's conversations (the doubled segment is the historical layout; not created) |
| `ChatStorage.get_doc_dir(doc_id)` | `DOCS_DIR / doc_id` (not created; `chat/docs/files.py` gets every doc path through it via `doc_paths()`) |

Every resolver enforces two checks and raises `InvalidStorageIdError` (a `ValueError` subclass, so callers that already treat a bad id as a cache miss or a 4xx keep working):

1. **Canonical id** -- the id must be a single path segment (`Path(id).name == id`): empty, `.`, `..`, embedded separators (`<id>/workspace/cache` -- the vector in security finding #279217) and absolute paths are rejected. No id format beyond that is enforced (UUIDs in production, short names in fixtures).
2. **Containment** -- the joined path must resolve under the root, which catches a planted `CHATS_DIR/<id>` symlink pointing outside the tree (dangling ones included); the two workspace-root resolvers re-check the full root, so a symlinked `workspace` segment below a real id dir is refused too. A symlinked data directory itself is fine because the root is resolved first. The plain join, not the resolved path, is what gets returned.

The workspace roots are what `chat/file_storage.py` functions take (`validate_path(root, rel)` and friends); no caller appends `"workspace"` to a resolver result.

The resolvers deliberately know nothing about users: schedulers, migrations, the Slack runtime and cross-user subagents resolve paths with no "current user". Ownership is a separate, composable guard for request-serving code in `chat/conversation_access.py`:

| Accessor | Behaviour |
|----------|-----------|
| `require_owned_conversation(user_id, conversation_id)` | `get_conversation_meta` lookup; 404 `conversation_not_found` when missing or another user's |
| `resolve_owned_workspace(user_id, conversation_id)` | The above plus `get_conversation_workspace_root` (also for project conversations); returns `(meta, root)` |
| `require_owned_project(user_id, project_id)` | `get_project` lookup; 404 `not_found` otherwise |
| `resolve_owned_project_workspace(user_id, project_id)` | The above plus `get_project_workspace_root`; returns `(project, root)` |

Routes that serve conversation- or project-scoped files go through these accessors rather than calling the store and the resolver separately:

- the file browser routes (`chat/file_routes.py`),
- the conversation routes (`chat/routes/conversations.py`),
- the project-table routes (`chat/project_db_routes.py`),
- the Gmail Simple URL-lookup routes, and
- the Gmail draft route's workspace attachments (`api/gmail/draft_endpoints.py` -- the body-supplied `conversation_id` is ownership-checked before any attachment is read).

The Quest Docs routes (`chat/docs/routes.py` and its sibling `share_routes.py` / `edit_routes.py` / `history_routes.py`) do not use these accessors. Doc visibility and ownership are decided by the Quest Docs access rule (`resolve_doc_access`, see [Quest Docs](quest-docs.md)), and the path still comes only from `get_doc_dir`. The URL-supplied path parts below it are validated before any path is built: asset names by `files._validate_asset_name()`, revision ids by the fullmatch pattern in `chat/docs/history.py` (and they must name a snapshot `files.list_revisions()` currently lists).

Tests: `tests/test_storage_path_resolvers.py` (canonical-id, containment and symlink cases, plus user-A-cannot-target-user-B route regressions), `tests/test_gmail_url_cache_security.py`, and `tests/test_docs_storage.py` (`get_doc_dir`).

## Design Decisions

**Why centralize paths in a single module?**
Previously, each module computed its own paths relative to `PROJECT_ROOT`. Centralizing them in `config/paths.py` ensures all modules agree on the data directory location, and makes it possible to relocate the entire data directory via a single configuration key.

**Why allow overriding the data directory?**
Moving the data directory outside the source tree is useful for deployments where the checkout is read-only, ephemeral, or shared across environments. The `data_dir` key in `server_config.json` supports both absolute paths and paths relative to the project root.

**Why does config/paths.py use only the standard library?**
The module is imported very early in the application lifecycle -- before SQLAlchemy, FastAPI, or other heavyweight packages are loaded. Using only `pathlib` and `json` avoids import-time side effects and circular dependencies.

**Why does run.py have its own path resolution?**
`run.py` runs as a standalone launcher that invokes uvicorn as a subprocess. It needs to create directories (like `data/logs/`) before the application code is imported. Duplicating the resolution logic with stdlib-only code avoids importing `config/paths` (which would pull in package-level dependencies indirectly) and keeps the launcher self-contained.

**Why validate ids inside the resolver instead of at each call site?**
Security finding #279217 was an RCE reachable because one helper built `CHATS_DIR / conversation_id` itself from a request value with no check. With ~12 call sites the checks were ad hoc -- some routes verified ownership, some did not, and none validated the id. Putting the canonical-id and containment checks inside the only resolver makes every present and future caller safe by construction, and deleting the independent joins means there is genuinely one path.

**Why keep path safety and ownership as two separate pieces?**
Fusing them would force a "current user" onto callers that legitimately have none (the scheduler, migrations, the Slack Socket Mode runtime, cross-user subagents). The resolver is unconditional and user-free; `chat/conversation_access.py` layers the DB ownership lookup on top for HTTP routes so ownership and path safety travel together where request input is involved.

**Why does chat/storage.py re-export CHATS_DIR and PROJECTS_DIR?**
Other modules already import these constants from `chat/storage.py`. The re-exports maintain backward compatibility so existing import sites continue to work without modification.
