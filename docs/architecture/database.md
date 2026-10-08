# Database Architecture

This document describes the SQLite database layer that stores user data, replacing the previous `data/users.json` file-based approach.

## Overview

User data (accounts, API keys, OAuth tokens, settings), user memories, guides, projects, routines, routine schedules, conversation metadata, action requests, skills, skill shares, user skill auto-loads, project skill auto-loads, routine skill auto-loads, tool wait handles, and LLM API call usage are stored in a SQLite database at `data/quest.db`, managed by SQLAlchemy ORM with Alembic for schema migrations. The `db/` package provides the engine configuration, ORM models, and data access layers that other modules use to read and write user records, memories, guides, projects, routines, routine schedules, conversations, action requests, skills, skill shares, and API call analytics.

Chat message history remains in JSON files (`data/chats/{conversation_id}/chat_history.json`). Conversation ownership, timing metadata (user association, `created_at`, `last_message_at`), custom names, and model selection now live in SQLite instead of being derived from the filesystem.

## Database Location

The SQLite database file defaults to `data/quest.db`. The path is defined as `DATABASE_PATH` in `config/paths.py` and imported by `db/engine.py`. When a custom `data_dir` is configured in `server_config.json`, the database moves to `{data_dir}/quest.db`. See [Data Paths](data-paths.md) for details.

The `alembic.ini` URL (`sqlite:///data/quest.db`) is overridden at runtime by `alembic/env.py`, which imports `DATABASE_PATH` from `config/paths` to ensure migrations target the configured data directory.

## User Model

The `User` model in `db/models.py` maps to the `users` table with these columns:

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `Integer` | Primary key, auto-increment | Auto-incrementing integer ID |
| `email` | `String(255)` | Unique, indexed, not null | User's email address |
| `name` | `String(255)` | Default `""` | Display name from Google |
| `api_key` | `String(64)` (`EncryptedText`) | Unique, indexed | Bearer token for API auth, encrypted at rest (ciphertext exceeds the declared length; SQLite ignores it) |
| `api_key_hash` | `String(64)` | Unique, indexed, nullable | SHA-256 hex of `api_key` (`config.encryption.hash_api_key`), the actual lookup key for Bearer auth; maintained by the `@validates("api_key")` hook whenever `api_key` is set |
| `created_at` | `DateTime` | Default `utcnow` | Account creation timestamp |
| `settings` | `JSON` | Default `{}` | User settings blob. Known keys: `custom_system_prompt` (kept in sync with the default guide's content), `slack_default_model` (per-user default LLM model for new Slack-driven conversations -- see [Slack Socket Mode](slack-socket-mode.md)), `default_model` (per-user "last-used" default LLM model for new private web/composer conversations, written only on the first send of a new chat; null when unset, FE falls back to Opus 4.8 -- separate from `slack_default_model`), `public_default_model` (the public-project twin: last-used model of a chat started inside a public project, tracked apart because the admin Model Selection allow-list differs per visibility), and `gmail_labels` (list of Quest-manageable Gmail label names configured in Settings > Gmail, each mapping to a `[Quest]/<name>` Gmail label; validated/normalized by `api/gmail/quest_labels.py` -- see [Gmail API](../api/gmail-api.md)), and `theme` (Settings > Appearance colour scheme, `"light"` or `"dark"`; null = auto/follow the OS, which is also what `"auto"`/empty writes normalise to) |
| `google_oauth` | `JSON` (`EncryptedJSON`) | Nullable | Google login OAuth tokens, encrypted at rest |
| `google_services_oauth` | `JSON` (`EncryptedJSON`) | Nullable | Google Services OAuth tokens (Gmail, Drive, etc.), encrypted at rest |
| `password_hash` | `String(255)` | Nullable | Email/password sign-in: scrypt hash string from `config/password_hashing.py` (a hash, so not encrypted). NULL for accounts without a password. Never on user dicts -- `to_dict()` exposes only the `password_fp` fingerprint used to bind session cookies. See [Auth](auth.md#sign-in-methods) |

One-time set-password links live in the separate `password_tokens` table (`PasswordToken` in `db/models.py`, data access in `db/password_store.py`, migration `e5b2c8f14a37`): lowercased `email` (not a user FK -- invites and sign-ups precede the account), unique SHA-256 `token_hash`, `purpose` (`invite` | `reset`), `created_at`, `expires_at`, nullable `used_at`.

The `id` column is an auto-incrementing integer primary key. The `email` column has a unique index for fast lookup by email. API-key lookups (`get_user_by_api_key`, used on every Bearer-authenticated request) go through the unique `api_key_hash` index, because the encrypted `api_key` ciphertext carries a random nonce and cannot be queried by value.

OAuth tokens (`google_oauth`, `google_services_oauth`) are stored as JSON columns, preserving the same dict structure used by the OAuth callback handlers. Every per-user secret column (`api_key`, the OAuth blobs, `airtable_token`, `ramp_oauth`) uses the `EncryptedText` / `EncryptedJSON` types from `db/encrypted_types.py`, so the ORM sees plaintext while the file holds `qenc1:` envelopes -- see [Encryption at Rest](encryption-at-rest.md).

Per-user Slack OAuth data (`access_token`, `default_team_id`, `user_id`, `authorized_at`) moved from the dropped `users.slack_oauth` column into a `user_service_credentials` row (service `slack`, `oauth_blob`; migration `f3a9c5d81b42`) when Slack became the in-tree plugin -- the shared bot token stays an admin credential in the `slack` service credential store entry, not per-user.

The Telethon session followed when Telegram became a plugin: the dropped `users.telegram_session` column became a `telegram` row whose `oauth_blob` is `{"session": "<StringSession>", "phone": ..., "connected_at": ...}` (migration `d7a1f3c9e2b4` decrypts under the old column's label and re-encrypts under the row's, since the AES-GCM associated data differs per column); an in-flight login sits in the same blob under `pending`.

The `User.to_dict()` method converts a model instance to a plain dict (including the `id` field) matching the format expected by code that receives user dicts, ensuring backward compatibility.

## UserServiceCredential Model

The `UserServiceCredential` model maps to the `user_service_credentials` table: per-user credentials for **plugin-contributed** services (one row per `(user, service)`, unique composite index). Core integrations keep their dedicated `users` columns (`ramp_oauth`, `airtable_token`, ...); plugins cannot add columns, so their per-user connections live here.

- A legacy pre-plugin per-user key column was folded in by migration `d9f4b82a61c7`,
- the GitHub plugin's OAuth token JSON is a row with service `github` in `oauth_blob` (migration `a7c3e91b52d8` did the same for `users.github_oauth`),
- the Twitter/X plugin's token JSON is a row with service `twitter` (migration `e2f8a4c61b93` did the same for `users.twitter_oauth`),
- and the Slack plugin's token JSON is a row with service `slack` (migration `f3a9c5d81b42` did the same for `users.slack_oauth`; the core Slack Socket Mode worker's `get_user_by_slack_user_id()` reverse lookup loads the Slack rows and matches `user_id` after the ORM has decrypted the blobs -- `oauth_blob` is ciphertext in SQL, so `json_extract` cannot see inside it).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `Integer` | Primary key, auto-increment | Row ID |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID |
| `service` | `String(64)` | Not null; unique with `user_id` | The plugin id (e.g. `acme_tracker`) |
| `secret` | `Text` (`EncryptedText`) | Nullable | API key for the `api_key` connection kind, encrypted at rest |
| `oauth_blob` | `JSON` (`EncryptedJSON`) | Nullable | Token JSON, encrypted at rest for the `oauth` connection kind (e.g. the GitHub plugin's access token + granted scopes) |
| `created_at` / `updated_at` | `DateTime` | Not null | Timestamps |

Both secret columns are encrypted at rest like the per-user credential columns ([Encryption at Rest](encryption-at-rest.md)). Access goes through `db/user_service_credential_store.py` (get/list/upsert/delete plus `delete_all_credentials()` used by the logout-and-disconnect flow; account deletion relies on the FK cascade).

`db/user_store.py` attaches the rows to every returned user dict as `user["service_credentials"]` (a service -> row map, present only when non-empty, and only queried while a loaded plugin declares a `user_connection`) so the sync `get_user_connected_services()` can evaluate plugin connection predicates without a DB round-trip.

Keys are written by the generic routes `POST /auth/service-key/{service}` and `POST /auth/service-key/{service}/remove` (`auth/service_key.py`), which serve every loaded plugin with an `api_key`-kind `UserConnectionSpec`.

## Memory Model

The `Memory` model in `db/models.py` maps to the `memories` table. Memories are markdown text blobs (up to 4KB) associated with a user, supporting inline editing, soft-delete via archiving, and full-text search.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID |
| `content` | `Text` | Not null | Markdown text content (max 4KB enforced at application layer in `db/memory_store.py`) |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |
| `updated_at` | `DateTime` | Nullable, default `None` | Last modification timestamp (set on content update, archive, or unarchive) |
| `archived` | `Boolean` | Default `False`, server default `"0"` | Soft-delete flag; archived memories are excluded from list/search by default |

A composite index `ix_memories_user_id_archived` on `(user_id, archived)` (declared in `__table_args__` on the model) speeds up the common query pattern of listing non-archived memories for a user. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so memories are automatically deleted when the parent user record is removed.

### Full-Text Search (FTS5)

Memory content is searchable via an FTS5 virtual table (`memories_fts`) kept in sync by database triggers. The FTS5 table and triggers are created in the Alembic migration `alembic/versions/94c49d92fea7_create_memories_table.py`.

**FTS5 virtual table**: `memories_fts` is a content-sync FTS5 table (`content='memories'`, `content_rowid='rowid'`) that indexes the `content` column.

**Sync triggers**: Three triggers maintain the FTS index:
- `memories_ai` (AFTER INSERT) -- adds new content to the FTS index
- `memories_ad` (AFTER DELETE) -- removes deleted content from the FTS index
- `memories_au` (AFTER UPDATE) -- removes old content and adds new content to the FTS index

**Search capabilities**: The `search_memories()` function in `db/memory_store.py` supports FTS5 query syntax including simple words, quoted phrases, prefix matching (`meet*`), and boolean operators (`AND`, `OR`, `NOT`). Results are ordered by FTS5 relevance rank.

### Cascade Deletion

When a user account is deleted, all of that user's memories are automatically removed via the database-level `ON DELETE CASCADE` foreign key from `memories.user_id` to `users.id`. Additionally, `delete_all_user_memories()` in `db/memory_store.py` is available for explicit bulk deletion at the application layer (e.g., during account deletion flows that need to perform additional cleanup).

## Guide Model

The `Guide` model in `db/models.py` maps to the `guides` table. Guides are named system prompt presets associated with a user, used to provide custom instructions to the LLM. Each user has a default guide (migrated from the old `custom_system_prompt` setting).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID |
| `name` | `String(255)` | Not null | Guide display name (max 100 chars enforced at application layer in `db/guide_store.py`) |
| `content` | `Text` | Not null, default `""` | System prompt text (max 16KB enforced at application layer in `db/guide_store.py`) |
| `is_default` | `Boolean` | Default `False`, server default `"0"` | Whether this is the user's default guide |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |
| `updated_at` | `DateTime` | Nullable, default `None` | Last modification timestamp (set on name or content update) |

A composite unique index `ix_guides_user_id_name` on `(user_id, name)` (declared in `__table_args__` on the model) prevents duplicate guide names per user. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so guides are automatically deleted when the parent user record is removed.

### Data Migration

The Alembic migration `alembic/versions/4e8960c4dacc_create_guides_table.py` creates the `guides` table and migrates existing data:

1. Creates the `guides` table with all columns and indexes
2. Creates the `ix_guides_user_id_name` composite unique index
3. For each existing user, creates a default guide (`is_default=True`, `name="Default"`) with the content copied from the user's `settings.custom_system_prompt` field (if present)

### Cascade Deletion

When a user account is deleted, all of that user's guides are automatically removed via the database-level `ON DELETE CASCADE` foreign key from `guides.user_id` to `users.id`. Additionally, `delete_all_user_guides()` in `db/guide_store.py` is available for explicit bulk deletion at the application layer (called during account deletion flows in `chat/routes/user.py`).

## Project Model

The `Project` model in `db/models.py` maps to the `projects` table. Projects group related conversations with a shared workspace and optional project guide (custom instructions injected into the system prompt). See [Projects Architecture](projects.md) for the full feature description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer via `uuid.uuid4()`) |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID |
| `name` | `String(255)` | Not null | Project display name (max 100 chars enforced at application layer in `db/project_store.py`) |
| `guide` | `Text` | Not null, default `""` | Project-specific custom instructions (max 16KB enforced at application layer in `db/project_store.py`) |
| `public` | `Boolean` | Not null, server default `false` | Public mode: internet-enabled sandbox, no internal data access. Set at creation only; immutable afterwards (`update_project()` never reads it). See [Public Projects Architecture](public-projects.md) |
| `archived` | `Boolean` | Not null, server default `false` | Soft-hide flag, the twin of `conversations.archived`: dropped from the default project list, scheduled routines paused, everything kept. Flipped by `set_project_archived()`; migration `c9e1f4a7b2d8`. See [Projects Architecture](projects.md#archiving-a-project) |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |
| `updated_at` | `DateTime` | Nullable, default `None` | Last modification timestamp (set on name or guide update) |

A composite unique index `ix_projects_user_id_name` on `(user_id, name)` (declared in `__table_args__` on the model) prevents duplicate project names per user. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so projects are automatically deleted when the parent user record is removed.

### Cascade Deletion

When a user account is deleted, all of that user's projects are automatically removed via the database-level `ON DELETE CASCADE` foreign key from `projects.user_id` to `users.id`. Additionally, `delete_all_user_projects()` in `db/project_store.py` is available for explicit bulk deletion at the application layer (called during account deletion flows in `chat/routes/user.py`). Deleting a project cascades to its conversation metadata rows via the `ON DELETE CASCADE` FK on `conversations.project_id`.

## Routine Model

The `Routine` model in `db/models.py` maps to the `routines` table. Routines are canned prompts attached to projects that can be run in one click, combining a prompt, an optional guide override, and an optional model specification. See [Routines Architecture](routines.md) for the full feature description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer via `uuid.uuid4()`) |
| `project_id` | `String(36)` | FK to `projects.id` (CASCADE), indexed, not null | Project this routine belongs to |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID (denormalized for fast per-user queries) |
| `name` | `String(255)` | Not null | Routine display name (max 100 chars enforced at application layer in `db/routine_store.py`) |
| `prompt` | `Text` | Not null | The prompt text sent as the first message when the routine runs |
| `guide_id` | `String(36)` | FK to `guides.id` (SET NULL), nullable | Optional guide override; NULL means use the user's default guide |
| `model` | `String(100)` | Nullable | Optional AI model to use; NULL means use the user's current model selection (manual runs) or server config default (scheduled runs) |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |
| `updated_at` | `DateTime` | Nullable, default `None` | Stamped on create and on every value-changing update; doubles as the optimistic-concurrency token for `update_routine()` (see [Routines Architecture](routines.md), Optimistic Concurrency). Legacy NULL rows are backfilled from `created_at` by Alembic migration `a9e2be6b9caa` |

An index `ix_routines_project_id` on `project_id` and a composite unique index `ix_routines_project_id_name` on `(project_id, name)` are declared in `__table_args__` on the model. The composite unique index prevents duplicate routine names within a project. The `project_id` column has a foreign key to `projects.id` with `ON DELETE CASCADE`, so routines are automatically deleted when the parent project is removed. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`. The `guide_id` column uses `ON DELETE SET NULL` so that deleting a guide sets the routine's guide reference to NULL rather than deleting the routine.

### Cascade Deletion

Routines and their schedules are cleaned up in multiple scenarios:

- **Project deletion**: `ON DELETE CASCADE` FK on `routines.project_id` removes all routines when the parent project is deleted. Schedules are then removed via `ON DELETE CASCADE` FK on `routine_schedules.routine_id`
- **User account deletion**: `delete_all_user_routines()` in `db/routine_store.py` is called explicitly during account deletion in `chat/routes/user.py`; `ON DELETE CASCADE` FKs on `routines.user_id` and `routine_schedules.user_id` also provide database-level cleanup
- **Guide deletion**: `ON DELETE SET NULL` FK on `routines.guide_id` sets the guide reference to NULL, preserving the routine

## RoutineSchedule Model

The `RoutineSchedule` model in `db/models.py` maps to the `routine_schedules` table. Routine schedules allow routines to run automatically on a timer. Each routine can have at most one schedule (one-to-one relationship). See [Scheduling Architecture](scheduling.md) for the full feature description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer via `uuid.uuid4()`) |
| `routine_id` | `String(36)` | FK to `routines.id` (CASCADE), not null | One-to-one link to the parent routine (uniqueness enforced by `ix_routine_schedules_routine_id` unique index in `__table_args__`) |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID (denormalized for fast per-user and scheduler queries) |
| `schedule_type` | `String(20)` | Not null | Discriminator: `'daily'`, `'weekly'`, `'hourly'`, or `'every_n_minutes'` |
| `daily_time_utc` | `String(5)` | Nullable | `"HH:MM"` in UTC (derived from `daily_time_local` + `timezone`) |
| `daily_time_local` | `String(5)` | Nullable | `"HH:MM"` in the user's local timezone |
| `timezone` | `String(64)` | Nullable | IANA timezone string (e.g., `"America/New_York"`) |
| `weekly_days` | `String(20)` | Nullable | Weekly only: comma-separated weekday numbers, 0=Monday .. 6=Sunday (e.g. `"0,2,4"`) |
| `hourly_minute` | `Integer` | Nullable | Minute offset from the top of each hour (0--59) |
| `interval_minutes` | `Integer` | Nullable | Run interval in minutes (1--1440) |
| `is_enabled` | `Boolean` | Default `True`, server default `"1"` | Whether the schedule is active |
| `next_due_at` | `DateTime` | Nullable | Next occurrence (UTC) of an anchored (daily/weekly/hourly) schedule; the scheduler fires once the clock passes it and advances it on claim. NULL for interval schedules; NULL on an anchored row means "initialize from now" (migration `a3d6f8b2c917`) |
| `last_run_started_at` | `DateTime` | Nullable | When the scheduler last started a run |
| `last_run_completed_at` | `DateTime` | Nullable | When the last run completed successfully |
| `is_running` | `Boolean` | Default `False`, server default `"0"` | Whether a run is currently in progress |
| `last_conversation_id` | `String(36)` | Nullable | Conversation ID of the most recent scheduled run |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |
| `updated_at` | `DateTime` | Nullable | Stamped on create and on every value-changing update; doubles as the optimistic-concurrency token for `update_schedule()` (see [Routines Architecture](routines.md), Optimistic Concurrency) |

The unique index `ix_routine_schedules_routine_id` on `routine_id` and the composite index `ix_routine_schedules_enabled_type` on `(is_enabled, schedule_type)` are declared in `__table_args__` on the model. The unique index enforces the one-to-one relationship. An index `ix_routine_schedules_user_id` on `user_id` (declared on the column) supports fast per-user queries. The enabled/type index supports efficient scheduler polling. The `routine_id` column has a foreign key to `routines.id` with `ON DELETE CASCADE`, so schedules are automatically deleted when the parent routine is removed. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`.

## RoutineScheduleRun Model

The `RoutineScheduleRun` model maps to the `routine_schedule_runs` table (migration `a3d6f8b2c917`): the run ledger, one row per scheduled occurrence. See [Scheduling Architecture](scheduling.md#run-ledger).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string |
| `schedule_id` | `String(36)` | FK to `routine_schedules.id` (CASCADE), not null | Owning schedule |
| `occurrence_at` | `DateTime` | Not null | Nominal scheduled instant (UTC); claim time for interval schedules |
| `status` | `String(20)` | Not null, indexed | `running` / `completed` / `failed` / `interrupted` / `missed` |
| `attempt` | `Integer` | Not null, default 0 | Starts of this occurrence (0 for missed, 2 after one retry) |
| `conversation_id` | `String(36)` | Nullable | Conversation of the latest attempt |
| `started_at` / `finished_at` | `DateTime` | Nullable | Latest attempt's start / end |
| `created_at` | `DateTime` | Default `utcnow` | Row creation |

The unique index `ix_routine_schedule_runs_schedule_occurrence` on `(schedule_id, occurrence_at)` guarantees an occurrence is claimed at most once. Rows are pruned to the newest 100 per schedule.

### Cascade Deletion

Schedules are cleaned up in multiple scenarios:

- **Routine deletion**: `ON DELETE CASCADE` FK on `routine_schedules.routine_id` removes the schedule when the parent routine is deleted, and `routine_schedule_runs.schedule_id` cascades on to its ledger rows
- **Project deletion**: Cascades through routines (project -> routine -> schedule -> runs)
- **User account deletion**: `ON DELETE CASCADE` FK on `routine_schedules.user_id` provides database-level cleanup

## Conversation Model

The `Conversation` model in `db/models.py` maps to the `conversations` table. It stores lightweight metadata about each conversation: ownership, timing, optional project association, optional custom name, and the LLM model used. The chat message content itself continues to reside in `data/chats/{conversation_id}/chat_history.json`.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `Text` | Primary key | UUID string (generated at application layer) |
| `user_id` | `Text` | FK to `users.id`, indexed, not null | Owner's user ID (references the integer `users.id` stored as text for FK flexibility) |
| `project_id` | `String(36)` | FK to `projects.id` (CASCADE), indexed, nullable | Project this conversation belongs to (NULL for standalone conversations) |
| `routine_id` | `String(36)` | FK to `routines.id` (SET NULL), indexed, nullable | Routine that created this conversation (NULL for manually created conversations). Set when a routine is run manually or by the scheduler |
| `custom_name` | `String(100)` | Nullable | User-assigned custom name for the conversation. When set, takes priority over the cached `auto_title` and over the file-derived fallback. NULL means use the auto-generated title. Max 100 characters enforced at the application layer |
| `auto_title` | `String(100)` | Nullable | Cached slice of the first user message (max 50 chars + `"..."` if truncated) used as the sidebar title when `custom_name` is NULL. Populated lazily by `ChatStorage.append_message` / `ChatStorage.append_structured_messages` on the first user-message append via `set_conversation_auto_title()` (idempotent: no-op once set). Mirrors the `last_message_seq` cache pattern so the conversation-list endpoint resolves titles from the DB without opening `chat_history.json`. Added by Alembic migration `8fb2c1a4d7e5`; backfilled on startup in `quest.py:lifespan` (`_backfill_conversation_auto_title`). The list endpoint falls back to a one-shot `chat_history.json` read when the row is still NULL but `last_message_seq > 0` (legacy rows in the boot window between deploy and backfill) |
| `created_at` | `DateTime` | Default `utcnow` | Conversation creation timestamp |
| `last_message_at` | `DateTime` | Default `utcnow` | Timestamp of the most recent message; updated by `update_last_message_at()` after each message is saved |
| `archived` | `Boolean` | Default `False`, server default `"0"` | Soft-delete flag; archived conversations are excluded from list results by default |
| `model` | `String(100)` | Nullable | LLM model used for this conversation (e.g., `"claude-sonnet-4-6"`, `"gemini-3.5-flash-lite"`). NULL means no model has been set yet (new conversation before first message). Persisted on first message by `run_conversation_turn()` via `update_conversation_model()`, and updated explicitly by the PATCH endpoint via `set_conversation_model()`. When a routine specifies a model, it is set at conversation creation time |
| `origin` | `String(20)` | Nullable | Where the conversation was created. Known values: `"web"` (default UI, also represented by NULL for pre-migration rows) and `"slack"` (created by the Slack Socket Mode worker from a DM). Application code treats NULL as `"web"`. The `GET /conversations/{id}` endpoint uses this to gate the web UI composer -- see [Slack Socket Mode](slack-socket-mode.md) |
| `last_message_seq` | `Integer` | Not null, default 0, server default `"0"` | Monotonic high-water mark for the per-conversation seq stamped on each message in `chat_history.json`. Cached here so the persistent-WS subscribe handler can answer `up_to_date` / `catchup` / `resync` without reading the file. Advanced by `update_last_message_seq()` after each successful append in `ChatStorage.append_message` and `ChatStorage.append_structured_messages`. The JSON file is the source of truth for the actual seq stamped on each message; this column is a denormalised cache. Added by Alembic migration `2a4c7e9b1d3f`; backfilled in `quest.py:lifespan` for pre-migration rows. See [Realtime Architecture](realtime.md) |
| `flags` | `JSON` | Nullable, default `None` | Per-conversation opt-in behaviors set at the *start* of the conversation, stored as a JSON array of enabled flag-name strings (e.g. `["nested_subagents"]`). Application code treats NULL/missing as "no flags" (empty set), mirroring the `origin` NULL == `"web"` precedent. Parsed from a magic `%%flags[...]` first line on the first web message and persisted by `set_conversation_flags()` (set-only-if-not-already-set; start-of-conversation only). The known flag set and the parser live in `chat/conversation_flags.py`. Added by Alembic migration `bf3a48a208a6` (no backfill needed -- NULL == no flags). See [Conversation Flags](conversation-flags.md) |

Before this migration, user→conversation ownership was inferred from the directory structure (`data/chats/{user_id}/{conversation_id}/`). It is now stored explicitly in the `conversations` table, and `ChatStorage` path helpers no longer take a `user_id` argument -- ownership is checked via the DB.

The `user_id` column has a foreign key to `users.id`. An index `ix_conversations_user_id` on `user_id` and a composite index `ix_conversations_user_id_last_message` on `(user_id, last_message_at)` are declared in `__table_args__` on the model. The `routine_id` column has a foreign key to `routines.id` with `ON DELETE SET NULL`, so deleting a routine does not delete conversations it created -- the `routine_id` is simply set to NULL. An index on `routine_id` (declared on the column) supports efficient grouping queries in the sidebar.

## SlackConversation Model

The `SlackConversation` model in `db/models.py` maps to the `slack_conversations` table. Each row links a `(slack_channel_id, slack_thread_ts)` pair to a Quest `Conversation` (one-to-one), so the Slack Socket Mode worker can route threaded replies to the same Quest conversation. See [Slack Socket Mode](slack-socket-mode.md) for the full feature description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `conversation_id` | `String(36)` | FK to `conversations.id` (CASCADE), unique, not null | Quest conversation this Slack thread maps to |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID (denormalised for fast per-user lookup) |
| `slack_channel_id` | `String(32)` | Not null | Slack DM channel id (e.g., `D012ABCDEF`) |
| `slack_thread_ts` | `String(32)` | Not null | Slack `ts` of the top-level user DM that started the conversation |
| `slack_user_id` | `String(32)` | Nullable | Slack user id of the DM partner at creation time |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |

A composite unique index `ix_slack_conversations_channel_thread` on `(slack_channel_id, slack_thread_ts)` enforces one-to-one routing from a Slack thread to a Quest conversation. An index `ix_slack_conversations_user_id` supports per-user listing. The unique constraint on `conversation_id` enforces the one-to-one relationship from the Quest side. Both FKs are `ON DELETE CASCADE`.

Rows are created by `ChatStorage.create_slack_conversation()` (see `chat/storage.py`) when the Socket Mode worker receives a top-level DM, and read by `chat/slack_conversation_store.get_slack_conversation()` when routing threaded replies.

## ActionRequest Model

The `ActionRequest` model in `db/models.py` maps to the `action_requests` table. Action requests represent agent-proposed write operations to external services (e.g., sending a Slack message, Telegram message, or creating a calendar invite) that require explicit user approval before execution. See [Action Requests Architecture](action-requests.md) for the full feature description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `Integer` | Primary key, auto-increment | Auto-incrementing integer ID |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | Owner's integer ID |
| `conversation_id` | `String(36)` | Not null, indexed | UUID of the conversation where the request was created |
| `request_type` | `String(100)` | Not null | Handler type discriminator (e.g., `"send_slack_message"`) |
| `params` | `JSON` | Not null | Type-specific parameters (e.g., `channel_id` and `message` for `send_slack_message`) |
| `reasoning` | `Text` | Not null | Agent's explanation of why the action is proposed, shown to the user |
| `status` | `String(20)` | Not null, default `"open"` | Request lifecycle state: `"open"`, `"denied"` (Revise, or the legacy bare deny), `"executed"`, or `"stopped"` (the card's Stop button: discarded AND the conversation halted until the user's next message) |
| `result` | `JSON` | Nullable | Handler return value on successful execution; on a denial it stores `{"denied": true}` (legacy Deny) or `{"denied": true, "feedback": "<user text>"}` (Revise); `{"stopped": true}` on Stop; NULL while open |
| `created_at` | `DateTime` | Default `utcnow` | When the request was created |
| `resolved_at` | `DateTime` | Nullable | When the request was approved, revised, or stopped; NULL while open |

The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so action requests are automatically deleted when the parent user record is removed. Indexes declared in `__table_args__` include `ix_action_requests_user_id` (per-user listing), `ix_action_requests_conversation_id` (per-conversation queries), `ix_action_requests_user_id_status` (per-user status filter / counts), and `ix_action_requests_user_status_created` on `(user_id, status, created_at)` for the Requests-pane status-filtered listings ordered by `created_at DESC` (added by Alembic migration `511e66a8b1fd`).

### Cascade Deletion

When a user account is deleted, all of that user's action requests are automatically removed via the database-level `ON DELETE CASCADE` foreign key from `action_requests.user_id` to `users.id`.

## UserSubagentRun Model

The `UserSubagentRun` model in `db/models.py` maps to the `user_subagent_runs` table (Alembic migration `e4b1a7c92f05`). Each row links one approved `run_user_subagent` action request to the subagent conversation it launched in the target user's account:

- `caller_user_id` / `target_user_id` (plain integers with **no FK**, mirroring the `llm_calls_*` analytics tables),
- `caller_conversation_id` / `subagent_conversation_id` (no FK, mirroring `action_requests`),
- the caller-side `wait_handle_id`,
- the approved `prompt`,
- the autoloaded `skill_ids` (JSON),
- a `status` lifecycle column (`running` / `awaiting_return` / `returned` / `denied` / `failed`; terminal transitions are first-write-wins in `db/user_subagent_run_store.py`),
- and an `error` detail for failures.

Rows deliberately survive deletion of either user or conversation because they are the durable cost-attribution linkage: the total cost of a caller conversation = its own `llm_calls_*` rows plus those of every `subagent_conversation_id` whose run row points at it (one level deep by construction -- subagents cannot launch further runs). `caller_conversation_id` is indexed for exactly that rollup join. See [Cross-User Subagents](user-subagents.md).

## Skill Model

The `Skill` model in `db/models.py` maps to the `skills` table. Skills are reusable instruction/prompt definitions that can be shared across users. Unlike guides (per-user, CASCADE delete), skills use SET NULL on creator deletion so they persist for other users. See [Skill Library Architecture](skill-library.md) for the full feature description.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `creator_id` | `Integer` | FK to `users.id` (SET NULL), indexed, nullable | Creator's integer ID; NULL if creator has been deleted |
| `project_id` | `String(36)` | FK to `projects.id` (no cascade), indexed, nullable | Project this skill belongs to; NULL for user-level skills |
| `name` | `String(255)` | Not null | Skill display name (max 100 chars enforced at application layer in `db/skill_store.py`) |
| `description` | `String(500)` | Not null, default `""` | Short description (max 500 chars enforced at application layer in `db/skill_store.py`) |
| `content` | `Text` | Not null | Full skill instructions (max 64KB enforced at application layer in `db/skill_store.py`) |
| `visibility` | `String(20)` | Not null, default `"private"` | Visibility level: `"private"`, `"shared"`, `"public"`, or `"project"` (see `SkillVisibility` enum) |
| `created_at` | `DateTime` | Default `utcnow` | Creation timestamp |
| `updated_at` | `DateTime` | Nullable, default `None` | Last modification timestamp (set on update) |

A composite unique index `ix_skills_creator_id_name` on `(creator_id, name)` (declared in `__table_args__` on the model) prevents duplicate skill names per creator. An index `ix_skills_visibility` on `visibility` supports efficient filtering. An index `ix_skills_project_id` on `project_id` supports efficient project-scoped queries. The `creator_id` column has a foreign key to `users.id` with `ON DELETE SET NULL`, so skills persist when the creator is deleted (the `creator_id` is set to NULL). The `project_id` column has a non-cascading foreign key to `projects.id`; project skills must be explicitly deleted before the project is deleted (handled in `db/project_store.py`).

### Cascade Behavior

When a user account is deleted, skills created by that user are **not** deleted. Instead, `creator_id` is set to NULL via the `ON DELETE SET NULL` foreign key. This preserves skills for other users who have access (shared or public skills). Any `skill_shares` rows where the deleted user was a share recipient are removed via `ON DELETE CASCADE` on `skill_shares.user_id`.

## SkillShare Model

The `SkillShare` model in `db/models.py` maps to the `skill_shares` table. This is a junction table that tracks which users have access to skills with `visibility="shared"`.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `skill_id` | `String(36)` | FK to `skills.id` (CASCADE), indexed, not null | The shared skill |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | The user the skill is shared with |
| `created_at` | `DateTime` | Default `utcnow` | When the share was created |

A composite unique index `ix_skill_shares_skill_id_user_id` on `(skill_id, user_id)` (declared in `__table_args__` on the model) prevents duplicate shares. Individual indexes on `skill_id` and `user_id` support efficient lookups from both directions. The `skill_id` column has a foreign key to `skills.id` with `ON DELETE CASCADE`, so shares are removed when the skill is deleted. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so shares are removed when the shared-with user is deleted.

## Doc and DocShare Models

`Doc` (`docs`) and `DocShare` (`doc_shares`) in `db/models.py` (Alembic migration `55983a10e266`) hold the metadata of [Quest Docs](quest-docs.md); bodies, assets and revisions live on disk under `DOCS_DIR`.

- `docs.owner_id` cascades with the user and the nullable `docs.project_id` cascades with the project (NULL = a user doc).
- `docs.mode` is always `private` for a user doc (`doc_store.create_doc()` refuses a public one; migration `e1b7c4d9a2f6` flipped the leftovers); a project doc copies `projects.public`.
- `docs.require_approval` (migration `a9c2e7f4b1d3`, default false) is the owner's switch that makes every model-initiated write an approval card (access rule 7 in `chat/docs/access.py`).
- `doc_shares.user_id` NULL means everyone on the install; the partial unique index `ix_doc_shares_everyone` allows one such row per doc.
- Title uniqueness (case-insensitive per owner, project and mode) is enforced in `db/doc_store.py`, not by an index.
- `ProjectDocSource` (`project_doc_sources`, migration `b4d7e2a9c6f1`) links a private project to a public project whose docs its conversations may read (composite PK `(project_id, source_project_id)`, both cascading with their project); see [Projects -- Docs Access](projects.md#docs-access-doc-sources).

See [Quest Docs -- Tables](quest-docs.md#tables).

## UserSkillAutoload Model

The `UserSkillAutoload` model in `db/models.py` maps to the `user_skill_autoloads` table. This is a junction table that tracks which skills a user has auto-loaded (automatically included in every conversation).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `user_id` | `Integer` | FK to `users.id` (CASCADE), indexed, not null | The user who auto-loaded the skill |
| `skill_id` | `String(36)` | FK to `skills.id` (CASCADE), indexed, not null | The auto-loaded skill |
| `created_at` | `DateTime` | Default `utcnow` | When the auto-load was created |

A composite unique index `ix_user_skill_autoloads_user_id_skill_id` on `(user_id, skill_id)` (declared in `__table_args__` on the model) prevents duplicate auto-load entries. Individual indexes on `user_id` and `skill_id` support efficient lookups from both directions. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`, so auto-load entries are removed when the user is deleted. The `skill_id` column has a foreign key to `skills.id` with `ON DELETE CASCADE`, so auto-load entries are removed when the skill is deleted.

## ProjectSkillAutoload Model

The `ProjectSkillAutoload` model in `db/models.py` maps to the `project_skill_autoloads` table. This is a junction table that tracks which skills a project has auto-loaded (automatically included in every conversation within that project, in addition to user-level auto-loads).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `project_id` | `String(36)` | FK to `projects.id` (CASCADE), indexed, not null | The project |
| `skill_id` | `String(36)` | FK to `skills.id` (CASCADE), indexed, not null | The auto-loaded skill |
| `created_at` | `DateTime` | Default `utcnow` | When the auto-load was created |

A composite unique index `ix_project_skill_autoloads_project_id_skill_id` on `(project_id, skill_id)` (declared in `__table_args__` on the model) prevents duplicate auto-load entries. Individual indexes on `project_id` and `skill_id` support efficient lookups from both directions. The `project_id` column has a foreign key to `projects.id` with `ON DELETE CASCADE`, so auto-load entries are removed when the project is deleted. The `skill_id` column has a foreign key to `skills.id` with `ON DELETE CASCADE`, so auto-load entries are removed when the skill is deleted.

## RoutineSkillAutoload Model

The `RoutineSkillAutoload` model in `db/models.py` maps to the `routine_skill_autoloads` table. This is a junction table that tracks which skills a routine has auto-loaded (automatically included in every conversation created by that routine, in addition to user-level and project-level auto-loads). See [Skill Library Architecture](skill-library.md) for the auto-load tier model and resolution order, and [Routines Architecture](routines.md) for the UI.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `String(36)` | Primary key | UUID string (generated at application layer) |
| `routine_id` | `String(36)` | FK to `routines.id` (CASCADE), indexed, not null | The routine |
| `skill_id` | `String(36)` | FK to `skills.id` (CASCADE), indexed, not null | The auto-loaded skill |
| `created_at` | `DateTime` | Default `utcnow` | When the auto-load was created |

A composite unique index `ix_routine_skill_autoloads_routine_id_skill_id` on `(routine_id, skill_id)` (declared in `__table_args__` on the model) prevents duplicate auto-load entries. Individual indexes on `routine_id` and `skill_id` support efficient lookups from both directions. The `routine_id` column has a foreign key to `routines.id` with `ON DELETE CASCADE`, so auto-load entries are removed when the routine is deleted. The `skill_id` column has a foreign key to `skills.id` with `ON DELETE CASCADE`, so auto-load entries are removed when the skill is deleted. The table is created by the Alembic migration `41e59b874c9d`.

## ToolWaitHandle Model

The `ToolWaitHandle` model in `db/models.py` maps to the `tool_wait_handles` table. Each row is the durable handle for a tool call that is awaiting human resolution: the Slack-driven `send_slack_reply_and_get_response` thread suspend (`kind="slack_reply"`) and the inline Approve / Revise / Deny card emitted by `create_action_request` (`kind="action_request"`). Both kinds always block the agent loop. The DB row is the source of truth -- there is no in-process future registry -- so a conversation suspended on a Slack thread or on an action request can be resumed across a server restart. See [Wait Handles Architecture](wait-handles.md) for the full mechanism.

The schema (id, user_id, conversation_id, kind, tool_id, status, payload, response, correlation_kind, correlation_id, expires_at, created_at, resolved_at) and indexes (`ix_tool_wait_handles_user_id`, `ix_tool_wait_handles_conversation_id`, `ix_tool_wait_handles_user_id_status`, `ix_tool_wait_handles_tool_id`) are declared on the model. The `user_id` column has a foreign key to `users.id` with `ON DELETE CASCADE`. The `conversation_id` column intentionally has no FK so audit rows survive deletion of the parent conversation, mirroring the `action_requests` table.

Status values come from `ToolWaitHandleStatus` (`pending` -> `accepted` | `rejected` | `cancelled` | `timed_out` | `stopped` -- the last is terminal for the UI but tells the resume machinery to wait for the user's next message instead of waking the model); kind values come from `ToolWaitHandleKind` (`slack_reply`, `action_request`). The `kind` column is `String(50)` so adding a new kind requires no migration. `correlation_kind` / `correlation_id` are populated at create time for `action_request` (the `action_requests.id` is known up front).

Data access is in `db/tool_wait_handle_store.py`. The table is created by the Alembic migration `c1f2a3d4e5b6_create_tool_wait_handles.py`. The follow-up data migration `3b9d4f7c2e10_cancel_pending_memory_suggestions.py` cancels any historical `pending` rows of retired kinds so they cannot strand resumes.

## Enums

`db/models.py` defines the following enums:

**`ActionRequestType`** -- Known action request type identifiers, one `StrEnum` member per CORE request type: calendar invites, Drive uploads and folder creation, memory saves, skill and routine create/edit, spreadsheet edits, Quest Doc writes (`write_doc`), and the subagent types.

The enum is deliberately not the full universe: `action_requests.request_type` is a plain string and plugin-registered types (`<plugin id>_`-prefixed names plus the grandfathered `send_twitter_dm`, `send_slack_message` / `send_slack_dm`, and `send_telegram_message`) are equally valid.

The handler registry in `chat/action_request_types/registry.py` is the source of truth, and the `create_action_request` schema's enum (`ACTION_REQUEST_TYPE_ENUM` in `chat/llm/tool_schemas.py`) starts from the core enum and is extended in place by plugin registration.

**`ToolWaitHandleKind`** and **`ToolWaitHandleStatus`** -- Discriminator and lifecycle status for `tool_wait_handles` rows. See `db/models.py` for the authoritative enum definitions and [Wait Handles Architecture](wait-handles.md) for usage.

**`SkillVisibility`** -- Visibility levels for skills in the skill library:

| Value | String |
|-------|--------|
| `PRIVATE` | `"private"` |
| `SHARED` | `"shared"` |
| `PUBLIC` | `"public"` |
| `PROJECT` | `"project"` |

Two `StrEnum` enums are used by the LLM call analytics models (`LlmCallGemini` / `LlmCallAnthropic` / `LlmCallOpenRouter`):

**`ModelId`** -- Known LLM model identifiers across all providers:

| Value | String | Notes |
|-------|--------|-------|
| `GEMINI_3_1_PRO` | `"gemini-3.1-pro-preview"` | Deprecated (hidden from the model selector; existing conversations/routines still run it, now Vertex-backed). Remains the `server_config.json` schema default |
| `GEMINI_3_1_FLASH_LITE` | `"gemini-3.1-flash-lite-preview"` | Deprecated (hidden from the model selector; existing conversations/routines still run it) |
| `GEMINI_3_PRO` | `"gemini-3-pro-preview"` | Deprecated; preserved for historical analysis of past API calls. Silently remapped to `GEMINI_3_1_PRO` at runtime |
| `GEMINI_3_FLASH` | `"gemini-3-flash-preview"` | Deprecated (hidden from the model selector; existing conversations/routines still run it) |
| `GEMINI_3_5_FLASH` | `"gemini-3.5-flash"` | Deprecated (hidden from the model selector; existing conversations/routines still run it) |
| `GEMINI_3_5_FLASH_LITE` | `"gemini-3.5-flash-lite"` | Vertex-backed Gemini Flash-Lite; cheapest/fastest option for the simplest tasks |
| `GEMINI_3_6_FLASH` | `"gemini-3.6-flash"` | Vertex-backed Gemini Flash |
| `GEMINI_3_7_FLASH` | `"gemini-3.7-flash"` | Vertex-backed Gemini Flash |
| `GEMINI_3_8_FLASH` | `"gemini-3.8-flash"` | Vertex-backed Gemini Flash; newest Flash-class model |
| `CLAUDE_HAIKU_4_5` | `"claude-haiku-4.5"` | Anthropic Claude Haiku on Vertex AI |
| `CLAUDE_HAIKU_5_5` | `"claude-haiku-5-5"` | Anthropic Claude Haiku 5.5 on Vertex AI (1M-token input window) |
| `CLAUDE_SONNET_4_6` | `"claude-sonnet-4-6"` | Anthropic Claude Sonnet on Vertex AI |
| `CLAUDE_OPUS_4_6` | `"claude-opus-4-6"` | Anthropic Claude Opus 4.6 on Vertex AI |
| `CLAUDE_OPUS_4_7` | `"claude-opus-4-7"` | Anthropic Claude Opus 4.7 on Vertex AI |
| `CLAUDE_OPUS_4_8` | `"claude-opus-4-8"` | Anthropic Claude Opus 4.8 on Vertex AI (1M-token input window) |
| `CLAUDE_SONNET_5` | `"claude-sonnet-5"` | Anthropic Claude Sonnet 5 on Vertex AI (1M-token input window) |
| `CLAUDE_SONNET_5_5` | `"claude-sonnet-5-5"` | Anthropic Claude Sonnet 5.5 on Vertex AI (1M-token input window) |
| `CLAUDE_OPUS_5` | `"claude-opus-5"` | Anthropic Claude Opus 5 on Vertex AI (1M-token input window) |
| `CLAUDE_OPUS_5_5` | `"claude-opus-5-5"` | Anthropic Claude Opus 5.5 on Vertex AI (1M-token input window) |
| `DEEPSEEK_V4_FLASH_0731` | `"deepseek/deepseek-v4-flash-0731"` | DeepSeek V4 Flash 0731 snapshot served via OpenRouter (API-key backend; 1.31M-token input window) |
| `QWEN_3_8_27B` | `"qwen/qwen3.8-27b"` | Qwen3.8 27B served via OpenRouter (API-key backend; 1M-token input window) |

**`ApiCallType`** -- Discriminator for the type of LLM API call:

| Value | String |
|-------|--------|
| `TOP_LEVEL` | `"top_level"` |
| `SUB_AGENT` | `"sub_agent"` |

These enums are imported by `db/llm_call_store.py` and the `chat/gemini_api/` package (`conversation.py` and `sub_agent.py`) to categorize API calls when recording usage.

## LlmCallGemini and LlmCallAnthropic Models

The `LlmCallGemini`, `LlmCallAnthropic`, and `LlmCallOpenRouter` models in `db/models.py` map to the `llm_calls_gemini`, `llm_calls_anthropic`, and `llm_calls_openrouter` tables -- the per-provider raw token analytics log (the first two replace the former single coalesced `gemini_api_calls` table; the OpenRouter table was added with the OpenRouter backend, migration `b3e7f2a91c04`, no backfill needed).

Each row represents one streaming turn (one `send_message_stream` call) from either the top-level agent or a sub-agent. Multiple rows per conversation message are expected when the model makes tool calls (each tool-call turn is a separate API call) or spawns sub-agents. All tables are append-only analytics logs -- rows are never cascade-deleted when the parent conversation or user is removed.

Storage is raw-first: each table's usage columns mirror that provider's NATIVE usage fields verbatim -- no normalization, no coalescing at write time. All interpretation (billing buckets, dashboard display, $ estimation) lives in the read-side queries in `db/llm_call_store.py` plus the static price table in `db/llm_pricing.py`. There is no `provider` column -- the table itself is the provider discriminator.

The tables share the same dimension columns (`conversation_id`, `user_id`, `model`, `call_type`, `agent_name`, `backend`, `level`, `duration_ms`, `created_at`) plus a `raw_usage` JSON catch-all carrying the provider usage fields verbatim (insurance for future provider fields that predate their own column). The native usage columns differ per table:

- **`llm_calls_gemini`**: `prompt_token_count` (INCLUDES cached tokens), `candidates_token_count` (EXCLUDES thoughts), `cached_content_token_count` (cache-hit subset of prompt), `thoughts_token_count` (reasoning, billed at the output rate), `tool_use_prompt_token_count`, `total_token_count` (provider-reported: prompt + candidates + thoughts + tool_use_prompt).
- **`llm_calls_anthropic`**: `input_tokens` (uncached input only -- EXCLUDES cache read/creation), `output_tokens`, `cache_read_input_tokens` (billed ~0.1x; 0.05x on Opus 5.5), `cache_creation_input_tokens` (billed above the input rate), and the TTL split `cache_creation_5m_input_tokens` / `cache_creation_1h_input_tokens` (5m writes bill 1.25x, 1h writes 2x). Anthropic reports no single total field.
- **`llm_calls_openrouter`**: `prompt_tokens` (INCLUDES cached tokens -- the Gemini-style convention), `completion_tokens` (INCLUDES reasoning tokens), `cached_prompt_tokens` (cache-hit subset of prompt; the API's `prompt_tokens_details.cached_tokens`, flattened at capture time), `reasoning_tokens` (`completion_tokens_details.reasoning_tokens`), `total_tokens` (provider-reported: prompt + completion), plus the accounting fields OpenRouter reports when the request opts in with `usage: {"include": true}`: `cost` (USD OpenRouter charged the account; on a bring-your-own-key request only its fee), `upstream_inference_cost` (`cost_details.upstream_inference_cost`, the upstream provider's charge -- observed populated, equal to `cost`, on ordinary requests too) and `is_byok`. All NULL on rows recorded before capture existed (migration `f7d2a9c41e58`, no backfill possible) -- read queries prefer the reported amount (`cost`, plus `upstream_inference_cost` only when `is_byok`, see `_openrouter_reported_cost()`) over the list-price estimate whenever `cost` is present.

All native usage columns are nullable: NULL means "the SDK did not populate the field" (faithful to `raw_usage`, which omits unset keys); read queries COALESCE to 0. See the model docstrings in `db/models.py` for the authoritative per-field semantics, including how context-tier pricing (e.g. the Gemini Pro >200k prompt-token tier) keys on per-row values.

Each table declares indexes on `conversation_id`, `user_id`, and `created_at` in `__table_args__` on the model.

**No foreign key constraints**: The `conversation_id` and `user_id` columns intentionally do not have FK constraints to the `conversations` and `users` tables. This ensures API call records survive if the parent conversation or user is deleted, preserving the analytics log. The tables are append-only -- no rows are ever updated or deleted.

**Backfill caveat for interpreting historical data**: the Alembic migration `05cdf20e3f2f` created both tables, backfilled all `gemini_api_calls` history, and dropped the old table. Rows that carried `raw_usage` (everything since the column was introduced) were backfilled exactly from it.

Older rows fell back to the coalesced columns: exact for Gemini (the coalesced values were verbatim copies of the native fields; `thoughts` / `tool_use_prompt` / `total` stay NULL), but approximate for Anthropic -- the legacy `cached_tokens` merged cache read + creation and was attributed entirely to `cache_read_input_tokens` (the merged total is exact; the read/creation split is not). The Anthropic TTL-split columns are NULL for all backfilled rows (never captured before). See the migration docstring in `alembic/versions/05cdf20e3f2f_split_raw_per_provider_llm_call_tables.py`.

## Data Access Layer

All store modules are async, use `AsyncSessionLocal` from `db/engine.py`, and follow the same pattern: each function opens a fresh session, performs its operation, and returns plain dicts. Callers must `await` every store call. Queries use SQLAlchemy 2.0-style `select()` and `delete()` with `await db.execute()`.

Each store module provides a `_*_to_dict()` helper that converts ORM instances to plain dicts.

**`db/user_store.py`** -- User CRUD, replacing the old `load_users()` / `save_users()` file-based pattern with granular operations. `update_user_settings()` performs a partial merge into the existing settings JSON. `search_users()` provides case-insensitive substring matching for the skill sharing type-ahead.

**`db/memory_store.py`** -- Memory CRUD with archiving (soft-delete) and FTS5 full-text search. `search_memories()` uses raw SQL via `text()` to join with `memories_fts` for FTS5 MATCH queries, excluding archived memories.

**`db/guide_store.py`** -- Guide CRUD with default guide management. The default guide cannot be renamed or deleted. `ensure_default_guide()` creates one if needed.

**`db/conversation_store.py`** -- Conversation metadata CRUD. Ownership and last-message timestamps are managed here; actual message content stays in `chat_history.json`. `update_conversation_model()` is a conditional set (no-op if model already set), while `set_conversation_model()` always overwrites. `set_conversation_flags()` is an idempotent set-only-if-not-already-set writer (empty list / already-set / missing row are all no-ops) and `get_conversation_flags()` reads the array NULL-safely (`[]` when unset). `_conversation_to_dict()` emits `flags` as `conv.flags or []`. Supports archiving, custom naming, and project/routine association.

**`db/project_store.py`** -- Project CRUD. `delete_project()` explicitly deletes project skills first (non-cascading FK on `skills.project_id`), then deletes the project row (CASCADE removes conversation rows and routines).

**`db/routine_store.py`** -- Routine CRUD scoped to projects. Uses ellipsis sentinels for nullable `guide_id` and `model` fields in updates. `update_routine()` accepts an `expected_updated_at` token and raises `StaleRoutineError` (carrying the fresh row as `.current`) on optimistic-concurrency mismatch; no-op saves short-circuit without bumping `updated_at`.

**`db/schedule_store.py`** -- Schedule CRUD and scheduler helpers. `list_enabled_schedules()` joins with routine data for the scheduler daemon. Provides `mark_run_started()`, `mark_run_completed()`, `mark_run_failed()` for run state tracking, and `clear_stale_running_flags()` for recovery from stuck runs. `update_schedule()` accepts an `expected_updated_at` token and raises `StaleScheduleError` (carrying the fresh row as `.current`) on optimistic-concurrency mismatch; no-op saves short-circuit without bumping `updated_at`.

**`db/llm_call_store.py`** -- Append-only analytics store for per-provider raw LLM call token usage (rows are never deleted). `record_api_call()` is called after each streaming response in `chat/gemini_api/conversation.py` (top-level) and `chat/gemini_api/sub_agent.py` (sub-agent).

It dispatches on `provider` to the matching raw table (`llm_calls_gemini` / `llm_calls_anthropic`) and copies the `raw_usage` keys into that table's native columns verbatim (absent keys stay NULL; a missing `provider` is inferred from the model prefix, and an unknown provider is logged and skipped rather than raising).

When `raw_usage` is empty (the provider returned no usage object), the coalesced `input_tokens` / `output_tokens` / `cached_tokens` parameters serve as a degraded-stream fallback -- that is their only purpose; no normalized column is ever written. Recording failures are caught and logged as warnings -- they never interrupt the conversation.

`get_usage_by_model_for_conversations(conversation_ids)` issues one batched `GROUP BY (conversation_id, model, long_context_flag, has_reported_cost_flag)` query per provider table (three queries total for the whole batch -- no N+1) and merges the results in Python into a per-conversation, per-model breakdown of provider-discriminated rows (`{model, provider, call_count, total_tokens, estimated_cost_usd, cost_source, metrics: {<native fields>}}`, sorted heaviest-first) plus a coarse conversation-level `{call_count, total_tokens, estimated_cost_usd, cost_source}` total, used by the admin System Reports page -- see [Admin System Reports](admin-system-monitor.md).

The same grouped-query core (`_collect_usage_buckets()`, which also takes an optional `created_at` window) backs `get_most_expensive_conversations(start, end, limit)` (date-range top-N ranking by known cost for the Cost Analysis report), and `get_latest_context_tokens_for_conversations()` returns each conversation's latest top-level call context size in one grouped `MAX(id)` subquery per provider table.

The long-context flag (per-call context above the model's tier threshold -- 200K tokens, or 100K for Claude Haiku 5.5 via the pricing entry's `long_context_threshold`, applied in SQL as a per-model CASE; Gemini keys on `prompt_token_count`, Anthropic on input + cache_read + cache_creation) exists because aggregated sums destroy the per-call context size that tier pricing keys on -- each same-tier bucket is priced via the static list-price table in `db/llm_pricing.py` (cost is linear in the token fields within a tier), then the buckets merge back into one entry per model. The has-reported-cost flag (OpenRouter rows with a non-NULL `cost`) splits off the calls whose cost the provider itself reported: those buckets sum the per-row reported amount (`_openrouter_reported_cost()`: `cost`, plus `upstream_inference_cost` on BYOK rows) instead of being estimated. `_add_cost()` folds bucket costs into every aggregate with the null-on-unpriced convention while tracking a `cost_source` (`"reported"` / `"estimated"` / `"mixed"`, `None` beside a `None` figure) per model entry, per conversation/user total, and per split of the user report. Models with neither reported amounts nor a pricing entry surface `estimated_cost_usd: null` and null out the conversation total.

**`db/action_request_store.py`** -- Action request CRUD and resolution. See [Action Requests Architecture](action-requests.md) for the full feature description. `resolve_action_request()` transitions status to `executed`, `denied` (Revise), or `stopped` (Stop). `list_action_requests_enriched()` joins with conversation and routine data for enriched display.

**`db/tool_wait_handle_store.py`** -- Wait-handle CRUD plus the bulk reads (`bulk_get_handles_by_ids`) and bulk writes (`mark_timed_out`, `cancel_pending_for_conversation`) used by the live `wait_for_handles` arm and the resume path. See [Wait Handles Architecture](wait-handles.md).

**`db/skill_store.py`** -- Skill CRUD, sharing management, access checks, and auto-load management. See [Skill Library Architecture](skill-library.md) for the full feature description. Access control follows four visibility levels (private, shared, public, project). `get_user_autoloaded_skills()`, `get_project_autoloaded_skills()`, and `get_routine_autoloaded_skills()` join with their respective auto-load junction tables and apply access control filtering, returning skills ordered by name for deterministic prompt ordering. Routine auto-load list/toggle helpers are `list_routine_autoloaded_skill_ids()` and `set_routine_skill_autoload()`.

**`db/doc_store.py`** -- Quest Docs metadata: CRUD, per-mode title uniqueness (`_title_taken()`), the keyset candidate list `list_accessible_docs()`, `update_doc_metadata()` (raises `StaleDocError` on an `expected_updated_at` mismatch), `update_after_write()`, the id collectors used by the project- and account-delete directory sweeps, and the share helpers. Access decisions are NOT made here (see `chat/docs/access.py`); body writes go through `chat/docs/service.py`. See [Quest Docs](quest-docs.md).

## Engine and Sessions

`db/engine.py` provides two engines that share the same SQLite database file (path from `DATABASE_PATH` in `config/paths.py`, defaults to `data/quest.db`):

### Sync Engine

Used by Alembic migrations only. All store modules have been ported to the async engine.

- **`engine`**: `create_engine("sqlite:///...")` with `check_same_thread=False` (required for FastAPI's threaded request handling)
- **Connect PRAGMAs**: `@event.listens_for(engine, "connect")` (`_set_sqlite_pragma`) runs the per-connection PRAGMAs described in [Connection PRAGMAs](#connection-pragmas) on every new DBAPI connection
- **`SessionLocal`**: `sessionmaker` factory with `autocommit=False`, `autoflush=False`
- **`get_db()`**: FastAPI dependency that yields a sync `Session` and closes it in a `finally` block

### Async Engine

Used by all store modules and their FastAPI endpoint callers. Every store function is `async` and uses `AsyncSessionLocal`.

- **`async_engine`**: `create_async_engine("sqlite+aiosqlite:///...")` -- uses the `aiosqlite` driver for non-blocking I/O. The pool is tuned explicitly (`pool_size=10`, `max_overflow=20`, `pool_timeout=30`); for a file-based aiosqlite URL SQLAlchemy uses `AsyncAdaptedQueuePool`, so these settings are honored. The pool governs how many concurrent read sessions can be open before callers queue (sized above the default 5 to accommodate FastAPI endpoints + the scheduler daemon + the Slack worker sharing the engine); writes still serialize at SQLite's single-writer lock regardless of pool size. See the comments in `db/engine.py` for the rationale
- **Connect PRAGMAs**: `@event.listens_for(async_engine.sync_engine, "connect")` (`_set_sqlite_pragma_async`) runs the same per-connection PRAGMAs (see [Connection PRAGMAs](#connection-pragmas)). The listener is attached to `.sync_engine` because SQLAlchemy async engines delegate connection events to the underlying sync engine
- **`AsyncSessionLocal`**: `async_sessionmaker` factory with `expire_on_commit=False`
- **`get_async_db()`**: FastAPI dependency that yields an `AsyncSession` via `async with AsyncSessionLocal() as db`

### Connection PRAGMAs

Both engines' connect listeners (`_set_sqlite_pragma` / `_set_sqlite_pragma_async` in `db/engine.py`) set the same four PRAGMAs on every new DBAPI connection:

- `PRAGMA foreign_keys=ON` -- enforces the schema's `ON DELETE CASCADE` / `SET NULL` constraints (SQLite does not enforce FKs by default)
- `PRAGMA journal_mode=WAL` -- lets readers proceed concurrently with a single writer (the default rollback journal blocks readers during a write). This setting persists in the DB file
- `PRAGMA busy_timeout=5000` -- a connection waits up to 5s for the writer lock instead of immediately raising "database is locked" under contention. Per-connection, so re-asserted on every connect
- `PRAGMA synchronous=NORMAL` -- the standard companion to WAL, trading a small power-loss durability window for materially fewer fsyncs. Per-connection

This brings the main DB in line with the WAL + busy_timeout settings already used for per-project DBs (see [Project DB](project-db.md)). It was added to fix concurrency stalls under load (`QueuePool limit reached` and `database is locked` errors when many sub-agents hit the DB while a blocking upstream call pinned the single event loop).

## Migrations (Alembic)

Schema changes are managed by Alembic. Migration scripts live in `alembic/versions/`.

`alembic/env.py` imports `Base` from `db/models.py` and sets `target_metadata = Base.metadata`, enabling Alembic's autogenerate feature to detect schema changes. All indexes are declared in `__table_args__` on the ORM models so that `Base.metadata` stays in sync with the actual database schema, allowing `alembic revision --autogenerate` to produce empty migrations when no model changes exist.

### Foreign Key Enforcement in Migrations

`run_migrations_online()` in `alembic/env.py` executes `PRAGMA foreign_keys=ON` on the migration connection before running migrations. This ensures that any DELETE operations within migration scripts (e.g., data migrations that remove parent rows) correctly cascade to child rows, matching the runtime behavior configured in `db/engine.py`.

### FTS5 Table Exclusion

`alembic/env.py` registers an `include_name` callback (passed to `context.configure()` in both online and offline modes) that filters out FTS5 virtual tables and their shadow tables during `alembic revision --autogenerate`. Without this, autogenerate would propose spurious `op.drop_table()` calls for FTS5 tables on every run, since FTS5 virtual tables are created via raw SQL in migrations (not via SQLAlchemy ORM models) and are invisible to SQLAlchemy's metadata introspection.

The `_FTS_VIRTUAL_TABLES` set in `alembic/env.py` lists the FTS5 virtual table names to exclude (currently `memories_fts`). Shadow tables are automatically excluded by matching the FTS table name with standard FTS5 shadow suffixes (`_config`, `_data`, `_docsize`, `_idx`). To add a new FTS5 table, add its name to `_FTS_VIRTUAL_TABLES` in `alembic/env.py`.

### PK Nullable Suppression (SQLite Quirk)

`alembic/env.py` registers a `process_revision_directives` hook (passed to `context.configure()` in both online and offline modes) that suppresses spurious `alter_column(nullable=False)` operations that Alembic autogenerate proposes for PRIMARY KEY columns.

This is needed because of a well-known SQLite quirk: SQLite's `PRAGMA table_info` reports `notnull=0` for PRIMARY KEY columns even though PKs are inherently NOT NULL. Alembic reads this metadata and detects a mismatch between the model (which declares the PK as non-nullable) and the database (which reports nullable), proposing an `alter_column` operation on every autogenerate run.

The behavior differs by PK column type:

- **`INTEGER PRIMARY KEY` columns** (e.g., `users.id`): SQLite enforces NOT NULL through the rowid alias mechanism regardless of the `notnull` pragma value. There is no actual danger -- the column cannot store NULL values.
- **Non-integer PRIMARY KEY columns** (e.g., `memories.id` which is `VARCHAR(36) PRIMARY KEY`): SQLite has a documented quirk where it actually allows NULL values in non-integer PK columns unless `NOT NULL` is explicitly declared in the DDL.
  - The SQLite documentation states: "Due to a bug in some early versions, this is not the case in SQLite. Unless the column is an INTEGER PRIMARY KEY or the table is a WITHOUT ROWID table or the column is declared NOT NULL, SQLite allows NULL values in a PRIMARY KEY column."
  - In this codebase, `memories.id` falls into this category -- the DDL from the original migration does not have explicit `NOT NULL` on it. In practice this is safe because SQLAlchemy always generates UUIDs via `uuid.uuid4()`, but the DB does not enforce it at the schema level.

The `_process_revision_directives` callback in `alembic/env.py` collects all PK column names from `Base.metadata` and strips any `AlterColumnOp` that is purely a nullable change on a PK column. This is implemented via `_is_pk_nullable_op()` (identifies PK nullable ops) and `_filter_pk_nullable_ops()` (filters them from both upgrade and downgrade op lists, including nested `ModifyTableOps`).

### NOT NULL Constraint Fix (Migration `9fb10fc3827d`)

The migration `9fb10fc3827d_fix_nullable_constraints_on_users_and_.py` fixes NOT NULL constraints on `users.name`, `users.api_key`, `users.created_at`, and `memories.created_at` that were lost during the PK migration (`a1b2c3d4e5f6`). The raw SQL `CREATE TABLE` in that migration omitted `NOT NULL` on these columns even though the SQLAlchemy models declare them as non-nullable.

The migration uses `batch_alter_table` for SQLite compatibility (SQLite does not support `ALTER COLUMN` directly; batch mode rebuilds the table). Before applying `NOT NULL`, it fills any existing NULL values with safe defaults (`''` for strings, `datetime('now')` for timestamps).

Because SQLite's `batch_alter_table` rebuilds the table by creating a new table and copying data, it can drop triggers attached to the original table. The migration includes FTS5 trigger safety checks: after altering the `memories` table, it verifies that the three FTS5 sync triggers (`memories_ai`, `memories_ad`, `memories_au`) still exist and re-creates any that are missing.

**Running migrations**: `uv run alembic upgrade head`

**Creating a new migration**: `uv run alembic revision --autogenerate -m "description"`

**Production**: `run.py` runs `uv run alembic upgrade head` as step 3 before starting the server, ensuring the database schema is always up to date.

## Design Decisions

**Why SQLite instead of PostgreSQL?**
SQLite requires no separate database server, fits the single-server deployment model, and the database file lives alongside the rest of the `data/` directory. The user table is small (tens of users for an internal tool) so SQLite's write lock is not a concern. If the project scales to multiple servers, PostgreSQL can be swapped by changing `DATABASE_URL` in `db/engine.py` and `alembic.ini`.

**Why a dual sync/async engine even though all stores are async?**
The async engine (`sqlite+aiosqlite`) avoids blocking the asyncio event loop during database I/O, which matters for stores called from FastAPI async endpoints and from the conversation loop. All 9 store modules now use `AsyncSessionLocal`. The sync engine (`SessionLocal`) is retained solely for Alembic migrations, which only support sync engines. Both engines share the same database file and the same `PRAGMA foreign_keys=ON` enforcement.

**Why SQLAlchemy ORM instead of raw SQL?**
The ORM provides type-safe column definitions, automatic JSON serialization for token columns, and a clean migration path via Alembic autogenerate. The `User` model serves as the single source of truth for the schema.

**Why Alembic for migrations?**
Alembic tracks schema versions and applies incremental migrations, making it safe to add columns or change types without manual SQL. The `run.py` integration ensures migrations run automatically on deploy.

**Why granular update functions instead of load/modify/save?**
The old pattern loaded all users into memory, modified one, and saved the entire file back. This was simple but had race conditions under concurrent requests. The new functions use database transactions and only touch the specific user record being modified.

**Why store OAuth tokens as JSON columns?**
OAuth token dicts have varying structures (different fields for Google vs Slack vs Google Services). JSON columns preserve the exact dict structure without requiring separate tables or flattening the data. SQLite's JSON support handles this cleanly through SQLAlchemy's `JSON` column type.

**Why keep `to_dict()` on the User model?**
Many existing modules receive user data as plain dicts. The `to_dict()` method provides backward compatibility without requiring all consumers to be rewritten at once. It only includes optional fields (OAuth tokens, connector tokens) when they are non-null, matching the previous behavior where these keys were absent from `users.json` until set.

**Why FTS5 for memory search instead of LIKE queries?**
FTS5 provides relevance-ranked results, supports boolean query syntax (AND, OR, NOT, prefix matching), and scales much better than `LIKE '%term%'` for text search. The content-sync FTS5 table with triggers keeps the index automatically in sync with the `memories` table without requiring application-level index management.

**Why auto-incrementing integer ID instead of email as primary key?**
Integer primary keys are more efficient for foreign key relationships, indexing, and storage. Email addresses can change (though rarely), and using them as PKs would require cascading updates across all referencing tables. The integer ID is stable, compact, and used as the FK target for the `memories` table and as the key for session cookies, storage paths, and cache keys.

**Why add archive instead of hard-delete by default?**
Archiving provides a soft-delete mechanism that lets users recover memories. The `archived` boolean column with a composite index on `(user_id, archived)` makes filtering efficient. Hard-delete remains available for permanent removal. The FTS5 `memories_au` (AFTER UPDATE) trigger handles re-indexing when content is edited.

**Why a foreign key from memories.user_id to users.id?**
With the integer primary key on the `users` table, a proper FK constraint with `ON DELETE CASCADE` is used. This ensures referential integrity and automatic cleanup of orphaned memories when a user is deleted.

**Why enable `PRAGMA foreign_keys=ON` on every connection?**
SQLite does not enforce foreign key constraints by default -- `ON DELETE CASCADE`, `ON DELETE SET NULL`, and referential integrity checks are silently ignored unless `PRAGMA foreign_keys=ON` is set. This pragma must be issued per-connection (it is not a persistent database setting). The `@event.listens_for(engine, "connect")` handler in `db/engine.py` ensures every application connection has enforcement enabled.

Without it, deleting a user would leave orphaned rows in `memories`, `guides`, `projects`, `routines`, `routine_schedules`, `conversations`, `action_requests`, and `skill_shares` instead of cascading, and `skills.creator_id` would not be set to NULL. The same pragma is set in `alembic/env.py` (`run_migrations_online()`) so that any DELETE operations within migration scripts also respect cascade constraints.

**Why move conversation ownership to SQLite instead of inferring it from the filesystem?**
Deriving ownership from the path `data/chats/{user_id}/{conversation_id}/` coupled storage layout to business logic. Moving ownership to a `conversations` table makes it explicit, enables indexed queries (`list_conversations_meta` does a single DB lookup instead of directory traversal), and decouples the on-disk layout from access control. The path is now flat (`data/chats/{conversation_id}/`), which simplifies `ChatStorage` helpers.

**Why snapshot guide content into conversations instead of referencing by ID?**
If guides were referenced by ID, editing a guide would retroactively change the system prompt for all conversations using it. Snapshotting the guide content on first message (in `chat_history.json` via `ChatStorage.set_guide_snapshot()`) preserves the instructions as they were when the conversation started.

**Why a composite unique index on (user_id, name) for guides?**
Guide names should be unique per user to avoid confusion in the dropdown selector. The database-level constraint prevents race conditions that application-level checks alone could miss.

**Why declare indexes in `__table_args__` on the model instead of only in migrations?**
Alembic autogenerate compares `Base.metadata` (the ORM models) against the actual database schema. If indexes exist in the database but are not declared on the models, autogenerate proposes dropping them on every run. Declaring all indexes in `__table_args__` keeps `Base.metadata` in sync with the DB, so `alembic revision --autogenerate` produces empty migrations when no model changes exist. This replaced an earlier convention where indexes were created only in Alembic migration scripts.

**Why suppress PK nullable detection in `alembic/env.py`?**
SQLite's `PRAGMA table_info` reports `notnull=0` for PRIMARY KEY columns. Alembic interprets this as the column being nullable and proposes an `alter_column(nullable=False)` operation on every autogenerate run. The `process_revision_directives` hook strips these no-op directives. For `INTEGER PRIMARY KEY` columns this is purely cosmetic (SQLite enforces NOT NULL via the rowid mechanism). For non-integer PK columns like `memories.id` (`VARCHAR(36)`), SQLite has a documented bug-turned-feature where NULL values are actually allowed unless `NOT NULL` is explicit in the DDL, but in practice this codebase always generates UUIDs at the application layer.

**Why exclude FTS5 tables from Alembic autogenerate?**
SQLite FTS5 virtual tables and their shadow tables (suffixed `_config`, `_data`, `_docsize`, `_idx`) are created via raw SQL in Alembic migrations, not through SQLAlchemy ORM models. SQLAlchemy's metadata reflection does not recognize them as model-backed tables, so autogenerate treats them as extraneous and proposes `op.drop_table()` calls for each one. The `include_name` callback in `alembic/env.py` filters them out, eliminating the need to manually edit every generated migration to remove these spurious drop operations.

**Why denormalize `user_id` on the routines table?**
The `user_id` column is technically derivable from `project_id` (via the project's `user_id`), but storing it directly on the routine enables fast per-user queries (e.g., `delete_all_user_routines()` during account deletion) without joining through the projects table.

**Why `ON DELETE SET NULL` for `routines.guide_id` instead of `CASCADE`?**
Deleting a guide should not delete routines that reference it. Setting `guide_id` to NULL gracefully degrades the routine to use the default guide, preserving the routine's prompt and other settings.

**Why a separate `routine_schedules` table instead of columns on `routines`?**
A separate table keeps the scheduling concern isolated from existing routine CRUD, avoids widening the routines table with many nullable columns (schedule type, daily time, timezone, hourly minute, interval, running state, etc.), and makes it easy to query "all schedules that are due" across all users. The one-to-one relationship is enforced by a unique index on `routine_id`.

**Why `ON DELETE SET NULL` for `conversations.routine_id` instead of `CASCADE`?**
Deleting a routine should not delete conversations that were created by it. The conversations contain valuable chat history and results. Setting `routine_id` to NULL preserves the conversations while removing the grouping association.

**Why a nullable `custom_name` column alongside a cached `auto_title`?**
The user-provided override and the auto-derived title serve different roles: `custom_name` is set by the rename endpoint and only when the user opts in, so a NULL value means "fall back to the auto-derived title." The auto-derived title is a slice of the first user message and never changes after the first append, so caching it on `auto_title` lets the sidebar list query resolve titles entirely from the DB.

The cache is filled in once on the first user-message append (idempotent in `set_conversation_auto_title`) and never invalidated, which matches the immutability of the underlying first-message slice. The list endpoint still falls back to a per-row file read for legacy rows where `auto_title` is NULL but `last_message_seq > 0`, so a deploy is correct before the lifespan backfill (`_backfill_conversation_auto_title`) finishes.

**Why denormalize `user_id` on the `routine_schedules` table?**
The `user_id` is derivable from the routine's `user_id`, but storing it directly on the schedule enables fast per-user queries and efficient scheduler polling without joining through the routines table.

**Why no FK constraints on the `llm_calls_*` tables?**
The `llm_calls_gemini` / `llm_calls_anthropic` tables are append-only analytics logs that should survive the deletion of the parent conversation or user. FK constraints with `CASCADE` would automatically delete usage records when conversations or users are removed, losing historical analytics data. Omitting FKs keeps the tables fully independent.

**Why per-provider raw tables instead of one normalized table?**
The former `gemini_api_calls` table normalized every call into an `input_tokens` / `output_tokens` / `cached_tokens` triple, which was lossy and provider-inconsistent: Gemini's `prompt_token_count` includes cached tokens (so "in" double-counted cache), while Anthropic's `input_tokens` excludes cache read/creation (so "in" undercounted), and the differently-priced Anthropic cache buckets (read ~0.1x vs creation 1.25x/2x) were merged.

Storing each provider's native fields verbatim keeps every billing-relevant distinction, and putting all calculation in read-side queries (`db/llm_call_store.py`) lets pricing logic evolve without rewrites. Context-window price tiers (e.g. Gemini Pro above 200k prompt tokens) are deducible per-row from the raw columns but destroyed by aggregation, so cost math for tier-priced models must run per-row before summing.

**Why record per-turn usage instead of per-conversation aggregates?**
Per-turn granularity allows distinguishing top-level agent usage from sub-agent usage, identifying which sub-agents consumed the most tokens, and computing both per-turn and per-conversation summaries from the same data. An aggregate-only approach would lose the ability to break down usage by call type and agent name.

## Constraints

- `data/users.json` is no longer used by the application. User data is exclusively in `data/quest.db`.
- The SQLite database must exist and have the correct schema before the server starts. In production, `run.py` handles this via `alembic upgrade head`. In development, run `uv run alembic upgrade head` after pulling schema changes.
- SQLAlchemy's `check_same_thread=False` is required on the sync engine because FastAPI processes requests across threads. This is safe for the read-heavy, low-write workload of user account and memory management.
- The async engine requires the `aiosqlite` driver (`sqlite+aiosqlite://` URL scheme). This is a Python dependency managed in `pyproject.toml`.
- SQLite foreign key enforcement (`PRAGMA foreign_keys=ON`) is set on every connection for both the sync engine and async engine via event listeners in `db/engine.py`, and explicitly in `alembic/env.py` for migrations. Without this, `ON DELETE CASCADE` and `ON DELETE SET NULL` constraints are silently ignored by SQLite. This is a per-connection setting, not a persistent database property.
- All store functions are `async` and must be `await`ed by callers. Calling them without `await` returns a coroutine object instead of the result.
- The `api_key` unique index means API key collisions will raise an `IntegrityError`. The key generation function produces cryptographically random keys, making collisions effectively impossible.
- Memory content is capped at 4KB (`MAX_MEMORY_SIZE` in `db/memory_store.py`), enforced at both the data access layer and the API validation layer (`chat/memory_routes.py`).
- The `memories_fts` virtual table and its sync triggers must exist for search to work. They are created by the Alembic migration `94c49d92fea7`.
- The `updated_at` column, `archived` column, and `ix_memories_user_id_archived` composite index are added by the Alembic migration `861a7abe9a23`.
- The `guides` table with `ix_guides_user_id` and `ix_guides_user_id_name` indexes is created by the Alembic migration `4e8960c4dacc`, which also migrates existing `custom_system_prompt` data to default guides.
- Guide content is capped at 16KB (`MAX_GUIDE_CONTENT_SIZE` in `db/guide_store.py`), enforced at both the data access layer and the API validation layer (`chat/guide_routes.py`).
- Guide names are capped at 100 characters (`MAX_GUIDE_NAME_LENGTH` in `db/guide_store.py`).
- The default guide cannot be renamed or deleted (enforced in `db/guide_store.py`).
- The `conversations` table must exist before the server accepts requests. It is created by an Alembic migration; `run.py` runs `alembic upgrade head` on startup to ensure the schema is current.
- The `projects` table with `ix_projects_user_id` and `ix_projects_user_id_name` indexes, and the `conversations.project_id` FK with `ix_conversations_project_id` index, are created by the Alembic migration `f09302acbdf1`.
- Project name is capped at 100 characters (`MAX_PROJECT_NAME_LENGTH` in `db/project_store.py`).
- Project guide is capped at 16KB (`MAX_PROJECT_GUIDE_SIZE` in `db/project_store.py`).
- Deleting a project cascades to conversation metadata rows via `ON DELETE CASCADE` on `conversations.project_id`. The caller must separately delete filesystem directories (project workspace and conversation chat directories).
- The `routines` table with `ix_routines_project_id`, `ix_routines_user_id`, and `ix_routines_project_id_name` indexes is created by the Alembic migration `6766d7c126ba`.
- Routine name maximum length: 100 characters (enforced in `db/routine_store.py` via `MAX_ROUTINE_NAME_LENGTH`).
- Routine prompt maximum size: 16KB (enforced in `db/routine_store.py` via `MAX_ROUTINE_PROMPT_SIZE`).
- Routine prompt cannot be empty (enforced at both the data access layer and API validation layer).
- Deleting a project cascades to routines via `ON DELETE CASCADE` on `routines.project_id`.
- Deleting a guide sets `routines.guide_id` to NULL via `ON DELETE SET NULL` (does not delete the routine).
- The `routine_schedules` table with `ix_routine_schedules_routine_id` (unique), `ix_routine_schedules_user_id`, and `ix_routine_schedules_enabled_type` indexes is created by an Alembic migration.
- Each routine can have at most one schedule (enforced by the unique index on `routine_schedules.routine_id`).
- Deleting a routine cascades to its schedule via `ON DELETE CASCADE` on `routine_schedules.routine_id`.
- Deleting a routine sets `conversations.routine_id` to NULL via `ON DELETE SET NULL` (does not delete conversations created by the routine).
- Deleting a user cascades to schedules via `ON DELETE CASCADE` on `routine_schedules.user_id`.
- The `conversations.routine_id` FK, index, and `ON DELETE SET NULL` behavior are created by the Alembic migration `c7e3f1a2b4d6`.
- Valid `schedule_type` values: `'daily'`, `'hourly'`, `'every_n_minutes'` (enforced at application layer in `db/schedule_store.py`).
- The `conversations.archived` column is added by the Alembic migration `498112eb3e5d`. Existing conversations default to `archived=False`.
- The `conversations.custom_name` column is added by an Alembic migration. Existing conversations default to `custom_name=NULL` (auto-generated title). Custom names are capped at 100 characters (enforced at the application layer in `db/conversation_store.py`). Setting an empty string clears the custom name (stored as NULL).
- The `conversations.auto_title` column is added by the Alembic migration `8fb2c1a4d7e5`. Existing rows default to `auto_title=NULL` and are filled in lazily by the `_backfill_conversation_auto_title` lifespan hook in `quest.py`; the conversation-list endpoint falls back to a one-shot `chat_history.json` read for any row still NULL.
- The `conversations.model` column is added by the Alembic migration `d4a8e2f1c3b5`. Existing conversations default to `model=NULL`.
- The `conversations.origin` column and the `slack_conversations` table (with `ix_slack_conversations_channel_thread` unique index and `ix_slack_conversations_user_id` index) are created by the Alembic migration `e58a2bf14c01`. Existing conversations default to `origin=NULL`, which application code treats as `"web"`.
- NOT NULL constraints on `users.name`, `users.api_key`, `users.created_at`, and `memories.created_at` are fixed by the Alembic migration `9fb10fc3827d`. This migration uses `batch_alter_table` for SQLite compatibility and includes FTS5 trigger safety checks.
- The `llm_calls_gemini` and `llm_calls_anthropic` tables (each with indexes on `conversation_id`, `user_id`, and `created_at`) are created by the Alembic migration `05cdf20e3f2f`, which also backfills all `gemini_api_calls` history and drops that table (see the backfill caveat in [LlmCallGemini and LlmCallAnthropic Models](#llmcallgemini-and-llmcallanthropic-models)).
- The `llm_calls_*` tables have no FK constraints -- they are append-only analytics logs that survive deletion of parent conversations or users.
- The `llm_calls_openrouter.cost` / `upstream_inference_cost` / `is_byok` columns (OpenRouter-reported accounting fields) are added by the Alembic migration `f7d2a9c41e58`; earlier rows keep NULL and stay list-price estimated.
- The `tool_wait_handles` table is created by the Alembic migration `c1f2a3d4e5b6`. Rows cascade on user deletion via `ON DELETE CASCADE` on `user_id`; `conversation_id` has no FK so rows survive conversation deletion (analytics / audit). See [Wait Handles Architecture](wait-handles.md) for `expires_at` enforcement caveats.
- `ChatStorage` path helpers no longer accept a `user_id` argument. Ownership is always checked via `db/conversation_store.py`.
- The on-disk chat directory structure changed from `data/chats/{user_id}/{conversation_id}/` to `data/chats/{conversation_id}/` (flat layout). Existing conversations under the old nested layout require a data migration to move directories.
