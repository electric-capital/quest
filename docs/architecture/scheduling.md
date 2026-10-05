# Routine Scheduling Architecture

This document describes the Routine Scheduling feature, which allows routines to run automatically on a timer without user interaction.

## Overview

Routine Scheduling extends the existing Routines feature with automatic execution. A routine can have at most one schedule attached (one-to-one relationship). When a schedule fires, the backend creates a new conversation in the project, sends the routine's prompt as the first message, and runs the Gemini conversation to completion -- all without a WebSocket connection or any user interaction.

When a user is drilled into a project in the sidebar, the frontend automatically polls for new conversations every 30 seconds, so scheduled conversations appear without requiring a manual refresh. See [Frontend Architecture](frontend.md) (Sidebar Component section) for polling implementation details.

Four schedule types are supported:

- **Daily** -- runs at a specific local time (stored with an IANA timezone)
- **Weekly** -- runs at a specific local time on chosen weekdays (`weekly_days`, 0=Monday .. 6=Sunday, stored as `"0,2,4"`; the time reuses `daily_time_local` + `timezone`)
- **Hourly** -- runs at a given minute offset (0--59) from the top of each UTC hour
- **Every N minutes** -- runs periodically at a fixed interval, skipping a run if the previous one is still in progress

Routines without a schedule continue to work as before (manual one-click invocation from the sidebar).

## RoutineSchedule Model

The `RoutineSchedule` model in `db/models.py` maps to the `routine_schedules` table. See [Database Architecture](database.md) for the column-level schema. Key constraints: unique index on `routine_id` enforces the one-to-one relationship (each routine can have at most one schedule), and a composite index on `(is_enabled, schedule_type)` supports efficient scheduler polling queries. The table stores schedule type, type-specific timing fields, `next_due_at` (the next occurrence of an anchored schedule, see below), run state tracking fields (`is_running`, `last_run_started_at`, `last_run_completed_at`), and the most recent conversation ID.

### Run Ledger

`RoutineScheduleRun` (`routine_schedule_runs`) records one row per scheduled occurrence: `occurrence_at` (the nominal scheduled instant; the claim time for interval schedules), `status` (`running` / `completed` / `failed` / `interrupted` / `missed`), `attempt`, `conversation_id`, `started_at`, `finished_at`. A unique index on `(schedule_id, occurrence_at)` makes a double fire of the same occurrence impossible. Rows cascade with the schedule and are pruned to the newest `RUNS_KEPT_PER_SCHEDULE` (100) per schedule on each claim. `GET .../schedule` returns the newest ten as `recent_runs`.

## Scheduler Daemon

The scheduler runs as an asyncio background task within the FastAPI process, started via the `lifespan` context manager in `quest.py`.

### Lifecycle

1. On FastAPI startup, the `lifespan` handler in `quest.py` spawns `scheduler_loop(app)` via `asyncio.create_task()`
2. Before the first poll, `_recover_interrupted_runs()` runs the startup sweep (see [Restart Resilience](#restart-resilience))
3. The scheduler sleeps for `POLL_INTERVAL_SECONDS` (30 seconds) between poll cycles
4. On FastAPI shutdown (Ctrl+C, SIGTERM, or admin-triggered shutdown via `POST /app/api/admin/shutdown`):
   - The scheduler loop task is cancelled
   - All in-progress execution tasks tracked in `_active_execution_tasks` are cancelled via `scheduler.shutdown()`
   - `_execute_scheduled_run()` handles `CancelledError` by marking the ledger row `interrupted`, persisting partial output plus an interruption notice, and re-raising

### Poll Cycle

Each poll cycle in `_poll_and_execute()`:

1. Clears stale `is_running` flags older than `STALE_THRESHOLD_HOURS` (2 hours) via `clear_stale_running_flags()` (guards a run stuck inside a live process; restarts clear every flag at boot)
2. Retries eligible interrupted runs (`_retry_interrupted_runs()`)
3. Queries `list_enabled_schedules()` to get all enabled schedules with their routine data
4. Checks each schedule: `_check_anchored()` for daily / weekly / hourly, `_check_interval()` for every_n_minutes. A schedule whose routine is paused is passed over instead -- a routine in a public project while the `public_project_routines` [feature gate](feature-gates.md) is closed for its owner (`_public_routine_gated()`), or any routine in an archived project (`project_archived` on the routine summary, see [Projects](projects.md)); both share `_schedule_paused_reason()` / `_skip_paused_schedule()`: no run, no ledger row, an anchored `next_due_at` moves to the next occurrence so nothing is caught up when the pause lifts, and interrupted runs are not retried meanwhile
5. A due occurrence is first **claimed** (`claim_occurrence()` in `db/schedule_store.py`: one transaction inserts the `running` ledger row, sets `is_running` + `last_run_started_at`, and advances `next_due_at`), then `_execute_scheduled_run()` is spawned as an independent asyncio task (tracked in `_active_execution_tasks` so it can be cancelled during shutdown)

### Due-Checking Logic

Occurrence math lives in `db/schedule_timing.py` (pure, no I/O): `occurrence_after()`, `occurrence_at_or_before()`, `occurrences_between()`.

- **Anchored (daily / weekly / hourly)**: the schedule is due once `now >= next_due_at`. There is no match window, so a poll that arrives late still finds the run. The scheduler then takes the latest occurrence at or before now:
  - within the type's catch-up grace (`CATCH_UP_GRACE`: hourly 45 min, daily 6 h, weekly 24 h) it is claimed and run, logged as a catch-up when more than two poll intervals late
  - past the grace it is recorded as `missed`
  - any older occurrences between `next_due_at` and the latest one (a backlog after a long outage) are recorded as `missed` (at most `MAX_MISSED_RECORDED` = 24 rows) -- a backlog collapses into one run of the newest occurrence
  - `next_due_at` advances to the first occurrence after now
  - a NULL `next_due_at` (rows that existed before the column, or a failed computation) is initialized to the next occurrence after now without firing, so upgrades never trigger retroactive runs
- **Every N minutes**: fires if enough time has elapsed since `last_run_completed_at` (or `last_run_started_at` if never completed), or if it never ran. Skips while `is_running` is `True`. Interval schedules never catch up or retry -- they simply run again on the next interval.

`next_due_at` is computed on create, and recomputed from now whenever a timing field changes or a paused schedule is re-enabled (`update_schedule()`), so occurrences that passed while paused are not caught up.

### Restart Resilience

The server is expected to restart routinely, so a run interrupted mid-flight must not be lost silently:

- **Graceful shutdown**: the cancelled run task marks its ledger row `interrupted` and appends an `error` message to its conversation ("interrupted by a server restart", plus "will retry" when eligible) after any partial output.
- **Hard kill**: at startup `mark_running_runs_interrupted()` turns every `running` row into `interrupted` (only one server process runs schedules, so nothing can be running at boot), clears every schedule's `is_running` flag, and `_recover_interrupted_runs()` adds the same notice to each interrupted conversation.
- **Retry**: `_retry_interrupted_runs()` restarts an interrupted run in a NEW conversation when `_retry_eligible()` holds: anchored type, schedule still enabled, fewer than `MAX_RUN_ATTEMPTS` (2) starts, not edited since the occurrence, and still within the catch-up grace. `claim_retry()` flips the same ledger row back to `running` with `attempt + 1`.

Retrying can repeat side effects of approval-free tools the routine already called before the interruption (e.g. a self-DM or self-email). Losing a weekly run was judged worse than a rare duplicate.

### Decision Logging

Only events are logged; a schedule that is simply not due yet logs nothing, at any level, so the 30-second poll is silent (local mode runs `chat.*` loggers at DEBUG, so DEBUG lines would still flood it). The next due time is visible as `next_due_at` on the schedule API and in Routine Settings instead.

- INFO `[scheduler] <routine>: FIRE -- <type> occurrence <iso> (schedule=<id>)` (or `FIRE (catch-up) -- <type> occurrence <N>m late`, or `FIRE -- interval elapsed (...)` for interval schedules)
- INFO `[scheduler] <routine>: RETRY -- interrupted run of <iso> (attempt <N>, schedule=<id>)`
- WARNING `[scheduler] <routine>: MISSED ...` for occurrences past the grace or skipped in a backlog
- WARNING `[scheduler] Run interrupted by restart: ...` from the startup sweep

### Execution Flow

When a scheduled run fires (`_execute_scheduled_run()` in `chat/scheduler.py`):

1. Look up the user record via `get_user_by_id()` from `db/user_store.py`
2. Create a new conversation in the project via `ChatStorage.create_project_conversation()`, passing the `routine_id` and the routine's `model` (if specified) so the conversation is linked to the routine and has the model set from creation
3. Link the conversation to the ledger row and the schedule via `set_run_conversation()` (the run was already marked running when it was claimed)
4. Save the routine prompt as a user message via `ChatStorage.append_message()`
5. Call `run_conversation_turn()` with a no-op `on_event` callback (no WebSocket to stream to), passing the routine's `model` if specified (read via `routine.get("model")` from the routine data joined in `list_enabled_schedules()`) and `routine_id` so the routine's auto-load skills are resolved and merged with user/project auto-loads (see [Skill Library Architecture](skill-library.md))
6. Persist the response messages via `_persist_scheduler_messages()` (a thin wrapper over `ChatStorage.append_structured_messages()`)
7. Mark the run as completed via `finish_run(..., "completed")` (clears `is_running`, stamps `last_run_completed_at`)
8. On `CancelledError` (server shutdown): mark the run `interrupted` and persist partial output plus the interruption notice (see [Restart Resilience](#restart-resilience)), then re-raise
9. On other errors: mark the run `failed` via `finish_run()`, then persist whatever `messages_out` accumulated before the failure -- including the durable `{"type": "error"}` message `run_conversation_turn()` appends on a fatal exception -- so the conversation shows why the routine stopped instead of ending silently after the prompt (see [Conversation Loop -- Error Surfacing](gemini-api.md#error-surfacing))

The resulting conversation appears in the project's sidebar grouped under the routine's collapsible entry, picked up automatically by the sidebar's 30-second polling mechanism when the user is drilled into the project (see [Routines Architecture](routines.md) for sidebar conversation grouping details and [Frontend Architecture](frontend.md) for polling details).

If a scheduled run issues `create_action_request`, the call blocks until the user resolves the card -- so the routine task suspends inside the dispatch arm and parks until a human approves or revises it (or until the underlying wait-handle expiry fires at ~14 days). Stop discards the request and kicks no resume: the routine conversation stays halted until someone sends it a message.

The suspend uses the same DB-backed pattern as web conversations: the sentinel unwinds the run cleanly, and `wait_resume.maybe_kick_resume()` on the resolve endpoint drives a fresh `run_conversation_turn()` to close out the dangling tool_use.

Routines that need fire-and-forget writes should prefer `send_slack_dm_to_self`-style paths instead of `create_action_request`; see [Action Requests](action-requests.md) and the `system:action_requests` skill prose.

### Concurrency

- The scheduler and normal user requests share the same asyncio event loop
- Multiple scheduled runs can execute concurrently (each is its own asyncio task)
- The `is_running` flag (set at claim time, before the task is spawned) prevents interval-based schedules from spawning overlapping runs
- The ledger's unique `(schedule_id, occurrence_at)` index prevents an anchored occurrence from firing twice
- SQLite write contention is minimal: schedule state updates are small and fast

## Timezone Handling for Daily and Weekly Schedules

1. The frontend sends `daily_time_local` (e.g., `"09:00"`) and the auto-detected `timezone` (e.g., `"America/New_York"`) from `Intl.DateTimeFormat().resolvedOptions().timeZone`, plus `weekly_days` for weekly schedules
2. Occurrences are resolved per date with `zoneinfo` in `db/schedule_timing.py`, so DST needs no special pass: `"09:00 America/New_York"` is 14:00 UTC in winter and 13:00 UTC in summer. Weekdays are local weekdays (Sunday 20:00 in Los Angeles fires Monday 03:00 UTC)
3. A local time in a spring-forward gap resolves one gap-length later (02:30 on the transition day runs at 03:30); a time repeated by a fall-back fold uses its first occurrence
4. `daily_time_utc` is still stored (via `_local_time_to_utc()` in `chat/schedule_routes.py`) and refreshed once per day by `_reconvert_daily_utc_times()`, but it is informational only -- firing uses `next_due_at`

## Skip-if-Running for Interval Schedules

### Mechanism

1. Before starting an interval-based run, `_is_due()` checks `schedule["is_running"]`
2. If `True`, the run is skipped (logged at INFO level with the reason "still running")
3. On claim: `claim_occurrence()` sets `is_running=True` and `last_run_started_at`
4. On run complete: `finish_run(..., "completed")` sets `is_running=False` and `last_run_completed_at`
5. On error or interruption: `finish_run()` sets `is_running=False` but does not update `last_run_completed_at`

### Staleness Protection

`clear_stale_running_flags()` runs at the start of each poll cycle. If `is_running=True` and `last_run_started_at` is more than 2 hours ago, the flag is cleared. This prevents a stuck run from permanently blocking future runs. A crash or restart does not wait for this threshold: the startup sweep clears every flag at boot. The 2-hour threshold is configurable via `STALE_THRESHOLD_HOURS`.

## Frontend

### RoutineSettingsModal Schedule UI

The RoutineSettingsModal uses a left navigation sidebar with Prompt, Schedule, and Delete sections (see [Routines Architecture](routines.md) for the full modal layout). The Schedule section contains:

1. **"Run on a schedule" checkbox** -- a single checkbox that controls whether the schedule is active. When unchecked, the schedule configuration inputs below remain visible but are grayed out and disabled, preserving the configuration for when the user re-enables the schedule
2. **Schedule Type** dropdown -- Daily / Weekly / Hourly / Every N minutes
3. **Type-specific inputs**:
   - Daily: time picker (`HH:MM`) with the user's local timezone auto-detected via `Intl.DateTimeFormat().resolvedOptions().timeZone`
   - Weekly: Mon..Sun pill toggles (at least one required; defaults to today) plus the same time picker
   - Hourly: number input for minute offset (0--59) with label "Minute of each hour"
   - Every N minutes: number input for interval (1--1440) with "skip if still running" note
4. **Catch-up hint** (anchored types) -- how late a run may still start after downtime, and that interrupted runs are retried once
5. **Last / next run info** -- read-only last completed run (plus "currently running") and the next due time (`next_due_at`)
6. **Recent scheduled runs** -- the `recent_runs` ledger rows with their status (Completed / Failed / Interrupted by restart / Missed (server was down) / Running, "(retry)" for a second attempt)

Schedule state is loaded when the modal opens via `fetchRoutineSchedule()`. Schedule save/update/delete is integrated into the routine save flow, and schedule edits are guarded by the same `expected_updated_at` optimistic-concurrency token as routine edits -- see [Routines Architecture](routines.md) (Optimistic Concurrency).

Styles for the schedule section are in `frontend/src/components/RoutineSettingsModal.css` (`.routine-settings-schedule-fields`, `.routine-settings-last-run`, `.routine-settings-weekdays`, `.routine-settings-recent-runs`).

### Sidebar Schedule Indicator

`frontend/src/components/Sidebar.tsx` shows a small clock icon next to routines that have an active schedule (`schedule.is_enabled`). The routine listing API includes schedule data (via a left-join in `db/routine_store.py`), avoiding N+1 API calls. Styles are in `frontend/src/components/Sidebar.css` (`.routine-schedule-indicator`).

## Cascade Deletion

Schedules are automatically cleaned up in multiple scenarios:

- **Routine deletion**: `ON DELETE CASCADE` FK on `routine_schedules.routine_id` removes the schedule when the parent routine is deleted
- **Project deletion**: Cascades through routines (project -> routine -> schedule)
- **User account deletion**: `ON DELETE CASCADE` FK on `routine_schedules.user_id` provides database-level cleanup

## Design Decisions

**Why a separate `routine_schedules` table instead of columns on `routines`?**
A separate table keeps the scheduling concern isolated from existing routine CRUD, avoids widening the routines table with many nullable columns, and makes it easy to query "all schedules that are due" across all users. The one-to-one relationship is enforced by a unique index on `routine_id`.

**Why a background asyncio task instead of Celery/APScheduler/cron?**
The app runs as a single uvicorn process, so an asyncio background task avoids external dependencies. The scheduler is started via FastAPI's `lifespan` event and shares the same event loop as normal requests.

**Why a stored `next_due_at` instead of matching the current time against a window?**
A window match (fire if a poll lands within 30 seconds of the target) silently drops the run whenever the server is down for that minute, and the server restarts routinely. Firing when the clock passes a stored next occurrence makes a late poll still find the run, and the catch-up grace bounds how stale a caught-up run may be.

**Why a run ledger?**
Claiming an occurrence by inserting a uniquely keyed row gives exactly-once firing, lets a restart tell which runs were cut off, and makes missed / interrupted runs visible in the UI instead of silent.

**Why retry interrupted runs only once, and only within the grace?**
A second restart during the retry is more likely a problem with the run than bad luck, and a retry far past the scheduled time is usually unwanted. Interval schedules never retry because their next interval comes soon anyway.

**Why store both local time and UTC time for daily schedules?**
Historical: the scheduler used to compare against `daily_time_utc`. It now fires from `next_due_at`; `daily_time_utc` stays as an informational API field and is kept current across DST by the daily reconversion.

**Why fire-and-forget for scheduled run execution?**
Each scheduled run is spawned as an independent asyncio task so the scheduler loop continues polling without waiting for Gemini to complete. This allows multiple runs to execute concurrently. Tasks are tracked in `_active_execution_tasks` so they can be cleanly cancelled during server shutdown (both Ctrl+C and admin-triggered).

**Why create headless conversations instead of a separate execution model?**
Scheduled runs create real conversations that appear in the project's sidebar, grouped under the routine that created them (via `routine_id`). This reuses the existing conversation infrastructure (message persistence, workspace access, guide resolution) and gives users visibility into what the scheduler produced.

**Why skip-if-running only for interval schedules?**
Daily, weekly and hourly schedules have natural non-overlap (they fire at most once per occurrence). Interval schedules with short intervals (e.g., every 5 minutes) could easily overlap if a Gemini conversation takes longer than the interval, so the skip-if-running guard prevents resource waste.

## Constraints

- Each routine can have at most one schedule (enforced by the unique index on `routine_schedules.routine_id`)
- Deleting a routine cascades to its schedule via `ON DELETE CASCADE` on `routine_schedules.routine_id`
- Deleting a user cascades to schedules via `ON DELETE CASCADE` on `routine_schedules.user_id`
- The scheduler runs in-process as an asyncio background task (no external process manager needed)
- Schedule checking granularity is approximately 30 seconds (configurable via `POLL_INTERVAL_SECONDS`)
- The skip-if-running mechanism applies only to `every_n_minutes` schedules
- Routines in public projects run on a schedule only while the `public_project_routines` feature gate is open for their owner
- Routines in an archived project do not run on a schedule until the project is unarchived; the pause uses the same skip path as the public-project gate
- Daily and weekly schedules require a valid IANA timezone string from the frontend; weekly schedules require at least one weekday
- Catch-up grace: hourly 45 minutes, daily 6 hours, weekly 24 hours (`CATCH_UP_GRACE` in `db/schedule_timing.py`); an interrupted anchored run is started at most twice (`MAX_RUN_ATTEMPTS`)
- Editing a schedule's timing or re-enabling it recomputes `next_due_at` from now (paused-time occurrences are not caught up) and cancels the retry of an interrupted run
- The `daily_time_utc` reconversion runs once per day at midnight UTC
- Stale `is_running` flags are cleared after 2 hours (configurable via `STALE_THRESHOLD_HOURS`)
- Scheduled runs create real conversations linked to the routine via `routine_id` that appear grouped under the routine in the project's sidebar
- Scheduled runs use the routine's specified model if set; otherwise fall back to the server config default model
- Scheduled runs load the routine's auto-load skills (from `routine_skill_autoloads`) in addition to the owner's user-level and project-level auto-loads; see [Skill Library Architecture](skill-library.md) for the merge order
- Valid `schedule_type` values: `'daily'`, `'weekly'`, `'hourly'`, `'every_n_minutes'`
- `hourly_minute` must be between 0 and 59
- `interval_minutes` must be between 1 and 1440 (24 hours)
- `daily_time_local` and `daily_time_utc` must be in `"HH:MM"` format
- `weekly_days` entries must be integers 0--6 (0=Monday)
- No new external dependencies -- uses `zoneinfo` (Python standard library), `asyncio`, and existing project dependencies

