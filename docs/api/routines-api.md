# Routines API Documentation

This document describes the REST API endpoints for managing routines. Routines are canned prompts attached to projects that can be run in one click or on an automatic schedule.

## Overview

The Routines API provides CRUD operations for project routines. All routine endpoints are nested under a project path (`/app/api/projects/{project_id}/routines`) and are defined in `chat/routine_routes.py`. The data access layer is in `db/routine_store.py`. The `Routine` model is defined in `db/models.py`.

Each routine combines a prompt (sent as the first message when the routine runs), an optional guide override (a specific guide to use instead of the user's default), and an optional model specification (which AI model to use). Running a routine manually is handled entirely by the frontend -- it creates a new conversation and auto-sends the prompt. Routines can also have automatic schedules attached; see [Schedules API](schedules-api.md) for the schedule CRUD endpoints.

The agent has its own project-scoped surfaces over the same data: the read-only `list_routines` tool and the `create_routine` / `edit_routine` action requests (name / prompt / model / schedule / auto-loaded skills, user-approved). These do not go through the REST endpoints below; see [Routines Architecture -- Agent Tools](../architecture/routines.md#agent-tools).

## Authentication

All routine endpoints support dual authentication: session cookie OR API key Bearer token (same as other `/app/api/*` endpoints). See [Chat API Authentication](chat-api.md) for details. All endpoints validate that the authenticated user owns the specified project via `get_routine_project()` in `chat/routine_routes.py`, which also returns 400 `public_project_routines_disabled` for a public project unless the admin `public_project_routines` feature gate is open for the user (the list endpoint returns an empty list instead; see [feature-gates.md](../architecture/feature-gates.md)). In a public project, enabling a skill auto-load 400s `public_project_no_skills`.

## Routine Endpoints

All routine endpoints are defined in `chat/routine_routes.py`. Request/response models (Pydantic) are in the same file. Data access is in `db/routine_store.py`.

- **GET `/app/api/projects/{project_id}/routines`** -- List routines alphabetically (`list_project_routines()`). Each routine includes an inline `schedule` object (via left-join in `db/routine_store.py`) so no separate call is needed.
- **POST `/app/api/projects/{project_id}/routines`** -- Create a routine (`create_project_routine()`). Accepts name, prompt, optional guide_id override (403 `guides_disabled` unless the admin `guides` feature gate is open for the user -- see [feature-gates.md](../architecture/feature-gates.md)), and optional model. Delegates to `create_routine()` in `db/routine_store.py`.
- **GET `/app/api/projects/{project_id}/routines/{routine_id}`** -- Get a routine (`get_project_routine()`). Validates both project ownership and routine membership.
- **PUT `/app/api/projects/{project_id}/routines/{routine_id}`** -- Update a routine (`update_project_routine()`). Uses `clear_guide`/`clear_model` boolean flags to distinguish "field absent" from "set to null" (JSON limitation). Setting a `guide_id` requires the `guides` feature gate to be open for the user (403 `guides_disabled` otherwise); `clear_guide` is always accepted. Translates to the ellipsis sentinel pattern used by `update_routine()` in `db/routine_store.py`. Accepts an optional `expected_updated_at` token for optimistic concurrency (see below).
- **DELETE `/app/api/projects/{project_id}/routines/{routine_id}`** -- Delete a routine (`delete_project_routine()`).
- **GET `/app/api/projects/{project_id}/routines/{routine_id}/costs`** -- Inference cost report for the routine (`get_project_routine_costs()`, backing the Costs section of `RoutineSettingsModal`). Same project-ownership and routine-membership checks as the single-routine GET (404 `not_found`). Delegates to `get_routine_cost_report()` in `chat/routine_costs.py`. Response:

  ```json
  {
    "routine_id": "...",
    "routine_created_at": "2026-01-01T00:00:00+00:00",
    "generated_at": "...",
    "windows": [
      {"days": 7,  "current": {<bucket>}, "previous": {<bucket>}},
      {"days": 28, "current": {<bucket>}, "previous": {<bucket>}}
    ],
    "lifetime": {<bucket>},
    "recent_runs": [
      {"conversation_id": "...", "title": "...", "started_at": "...",
       "models": ["claude-opus-4-8"], "call_count": 3, "total_tokens": 12000,
       "cost_usd": 0.42, "cost_source": "estimated"}
    ]
  }
  ```

  where `<bucket>` is `{"run_count", "call_count", "total_tokens", "cost_usd", "cost_source"}`. One run = one conversation the routine created (`conversations.routine_id`); a run's cost is that conversation's whole recorded usage from the raw `llm_calls_*` tables (sub-agent calls and every model included), and every figure buckets runs by the run's **start time** (`conversations.created_at`), not by call time: `current` covers runs started in `[now - days, now)`, `previous` the same-length period before it (the period-over-period base), `lifetime` every surviving run, `recent_runs` the newest 10. This differs from the admin Users report, which slices routine spend by call time. `cost_usd` follows the repo-wide null-on-unpriced convention (null when any run in the bucket used a model with no pricing entry and no provider-reported amount; a run with no recorded calls is a known `0`), with the usual `cost_source` provenance tag (`reported` / `estimated` / `mixed` / null -- see [Admin System Monitor API](admin-system-monitor-api.md)). Runs whose conversation was deleted are not included (the call rows survive but lose their routine link).

## Routine Auto-load Skill Endpoints

These endpoints back the Skills section of `RoutineSettingsModal` and persist toggles into the `routine_skill_autoloads` table. Both verify project ownership and routine membership; the PUT endpoint also gates the toggle on `user_can_access_skill()` from `db/skill_store.py`. See [Routines Architecture](../architecture/routines.md) and [Skill Library Architecture](../architecture/skill-library.md) for the merge order with user and project auto-loads at conversation time.

- **GET `/app/api/projects/{project_id}/routines/{routine_id}/skills/autoloaded`** -- Return the routine's auto-loaded skill IDs (`get_routine_autoloaded_skills_route()`). Response is `{"skill_ids": [<uuid>, ...]}`. Delegates to `list_routine_autoloaded_skill_ids()` in `db/skill_store.py`.
- **PUT `/app/api/projects/{project_id}/routines/{routine_id}/skills/{skill_id}/autoload`** -- Toggle auto-load (`toggle_routine_skill_autoload()`). Body: `RoutineAutoloadRequest` (`{"enabled": bool}`). Delegates to `set_routine_skill_autoload()` in `db/skill_store.py`. Returns `{"success": true}`. Returns `404 not_found` if the project, routine, or skill is missing (or the skill is inaccessible to the user).

## Optimistic Concurrency

Routine responses include `updated_at` (ISO string, stamped on create and on every value-changing update). Clients pass that token back as `expected_updated_at` on `PUT` to guard against concurrent overwrites.

When the supplied token does not match the row's current `updated_at`, the endpoint short-circuits with `409` and a flat JSON body `{"error": "stale_update", "message": ..., "current": <fresh routine row>}` -- returned via `JSONResponse` rather than `HTTPException` so the body has no FastAPI `detail` wrapper. Other error paths still use `HTTPException`.

Omitting `expected_updated_at` skips the check (used by the "overwrite anyway" path after the user resolves a conflict, and as a back-compat path for legacy rows). See [`StaleRoutineError`](../../db/routine_store.py) for the underlying exception and [Routines Architecture](../architecture/routines.md) for the end-to-end behavior, including the no-op short-circuit that avoids spurious token invalidation.

---
