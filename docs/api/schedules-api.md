# Schedules API Documentation

This document describes the REST API endpoints for managing routine schedules. Schedules allow routines to run automatically on a timer.

## Overview

The Schedules API provides CRUD operations for routine schedules. All schedule endpoints are nested under a routine path (`/app/api/projects/{project_id}/routines/{routine_id}/schedule`) and are defined in `chat/schedule_routes.py`. The data access layer is in `db/schedule_store.py`. The `RoutineSchedule` model is defined in `db/models.py`.

Each routine can have at most one schedule. Four schedule types are supported: `daily` (at a specific local time), `weekly` (at a specific local time on the `weekly_days` weekdays, 0=Monday .. 6=Sunday), `hourly` (at a minute offset from the top of each hour), and `every_n_minutes` (at a fixed interval, skipping if the previous run is still in progress). Runs due while the server was down are caught up within a grace window, and runs cut off by a restart are retried once -- see [Scheduling Architecture](../architecture/scheduling.md#restart-resilience). When a schedule fires, the backend scheduler creates a new conversation in the project and runs the routine's prompt through `run_conversation_turn()` headlessly.

## Authentication

All schedule endpoints support dual authentication: session cookie OR API key Bearer token (same as other `/app/api/*` endpoints). See [Chat API Authentication](chat-api.md) for details. All endpoints validate project and routine ownership via `_validate_ownership()` in `chat/schedule_routes.py`.

## Schedule Endpoints

All schedule endpoints are defined in `chat/schedule_routes.py`. Request/response models (Pydantic) are in the same file. Data access is in `db/schedule_store.py`. For `daily` and `weekly` schedules, the backend converts `daily_time_local` + `timezone` to the informational `daily_time_utc` via `_local_time_to_utc()` in `chat/schedule_routes.py`.

Every schedule response carries `next_due_at` (ISO UTC; the stored next occurrence for anchored types, derived from the last run + interval for `every_n_minutes`, null when not computable) and `weekly_days` (list, weekly only; null otherwise).

- **GET `.../schedule`** -- Get schedule for a routine (`get_routine_schedule()`). Returns 404 if no schedule exists. Also returns `recent_runs`: the newest ten run-ledger rows (`id`, `occurrence_at`, `status` running/completed/failed/interrupted/missed, `attempt`, `conversation_id`, `started_at`, `finished_at`).
- **POST `.../schedule`** -- Create schedule (`create_routine_schedule()`). Fails if a schedule already exists. Required fields vary by `schedule_type` -- see the Pydantic model `CreateScheduleRequest`.
- **PUT `.../schedule`** -- Update schedule (`update_routine_schedule()`). Partial update; recalculates `daily_time_utc` when daily/weekly params change, recomputes `next_due_at` from now when a timing field changes or the schedule is re-enabled, and validates the merged row (400 `invalid_schedule`, e.g. a weekly schedule left without days). Accepts an optional `expected_updated_at` token for optimistic concurrency (see below).
- **DELETE `.../schedule`** -- Delete schedule (`delete_routine_schedule()`).

All endpoints are nested under `/app/api/projects/{project_id}/routines/{routine_id}/schedule`.

## Optimistic Concurrency

Schedule responses include `updated_at` (ISO string, stamped on create and on every value-changing update). Clients pass that token back as `expected_updated_at` on `PUT` to guard against concurrent overwrites. On mismatch the endpoint returns `409` with a flat JSON body `{"error": "stale_update", "message": ..., "current": <fresh schedule row>}` -- emitted via `JSONResponse` so the body has no FastAPI `detail` wrapper. Other error paths still use `HTTPException`. Omitting `expected_updated_at` skips the check. See [`StaleScheduleError`](../../db/schedule_store.py) and [Routines Architecture](../architecture/routines.md) (Optimistic Concurrency section) for the shared mechanism.

---
