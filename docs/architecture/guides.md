# Guides Architecture

This document describes the Guides feature, which extends the single custom system prompt into a multi-guide system. Guides are named system prompt presets that users can edit, delete, and select per conversation.

**Guides are deprecated in favor of [skills](skill-library.md).** Creating new guides is disabled (`POST /app/api/guides` returns 410 `guides_deprecated`), the composer no longer offers a guide selector, and new conversations no longer fall back to the default guide -- a new conversation only gets a guide when a routine with a guide override starts it. Existing conversations keep their snapshotted guide content, and each existing guide offers a one-click conversion to a skill in Settings (see [Migration to Skills](#migration-to-skills)), which stays available so users have time to convert.

**The whole feature sits behind the admin `guides` feature gate** ([feature-gates.md](feature-gates.md)), off by default. A fresh install never sees guides at all; an existing install turns the gate on (for everyone or for an `allowed_users` list) to keep guides alive while people convert them to skills, then turns it off once the [Guides report](admin-system-monitor.md) shows nothing left. See [Feature Gate](#feature-gate) below.

Note: the "Project Guide" (`projects.guide`, labeled **Project Instructions** in the UI) is an unrelated free-text field on projects and is NOT deprecated or gated -- see [Projects Architecture](projects.md).

## Overview

Previously, users had a single "Custom System Prompt" text field in Settings. The Guides feature replaced this with a multi-guide system where each guide is a named set of custom instructions. A default guide was created for each user (migrated from the old custom system prompt), and users selected a guide per conversation from a composer dropdown. Once the first message was sent, the guide was snapshotted into the conversation, preserving the instructions even if the guide is later edited or deleted.

With deprecation, the composer selector and the default-guide fallback are gone. Today the only way a NEW conversation gets a guide is a routine guide override (the `guide_id` on the routine, sent via the routine auto-send path or applied directly by the scheduler). Snapshots written before the deprecation keep applying to their conversations.

## Feature Gate

`FEATURE_GUIDES = "guides"` in `config/feature_gates.py` (in `PER_USER_ACCESS_FEATURES`, so an admin can keep guides on only for the users who have not migrated yet). `guides_enabled_for(user_email)` is the check every enforcement point uses. While the gate is closed for a user:

- Every `/app/api/guides*` route returns 403 `guides_disabled` (`_require_guides_enabled()` in `chat/guide_routes.py`), including convert-to-skill -- the migration flow is part of the feature, so an admin who wants people to finish converting keeps the gate open for them.
- Routines cannot be given a guide override: `POST`/`PUT /projects/{id}/routines` reject `guide_id` with 403 `guides_disabled` (`clear_guide` still works).
- `run_conversation_turn()` applies NO guide: an explicit `guide_id` is ignored (logged) and an existing `guide_snapshot` is neither read nor written. The snapshot file content is untouched, so reopening the gate makes old conversations pick their guide back up.
- The `custom_system_prompt` → default-guide settings sync is skipped.
- The FE hides the Settings Guides section and the New Routine guide-override picker, never fetches `GET /guides`, and shows a leftover routine override as "Guide (disabled)" with a Clear button.

The gate never deletes anything. The admin System Reports > Guides tracker stays available regardless of the gate so it can be used to decide when to close it.

## Migration to Skills

`POST /app/api/guides/{guide_id}/convert-to-skill` (in `chat/guide_routes.py`) converts a guide into a skill:

1. Creates a **private** skill copying the guide's name and content, with description `Converted from the '<name>' guide.` On a name collision with an existing skill, retries with numbered `(converted)` suffixes; if all candidates collide, returns 409 `duplicate_name` and the guide is kept.
2. If the guide is the **default** guide, enables user-level auto-load on the new skill (`set_user_skill_autoload`), so its instructions keep applying to every conversation the way the default guide did.
3. Deletes the guide (the default guide included -- `delete_guide` no longer protects it).

Guides with empty content cannot be converted (400 `empty_guide`); they can be deleted instead. Existing conversations are unaffected because their guide content is snapshotted (see below). Guide content (max 16KB) always fits the skill content limit (64KB).

A deleted default guide stays gone: `ensure_default_guide()` was removed, `GET /app/api/guides` no longer auto-creates a default, the conversation flow runs without a custom prompt when no guide resolves, and the legacy `custom_system_prompt` settings sync only updates a default guide that still exists.

The Settings "Guides" section shows a deprecation notice linking to the Skills section, offers a "Convert to Skill" button per guide (hidden for empty guides), and no longer offers guide creation.

## Guide Model

The `Guide` model in `db/models.py` maps to the `guides` table. See [Database Architecture](database.md) for the column-level schema. Key constraints: composite unique index on `(user_id, name)` prevents duplicate guide names per user, and `ON DELETE CASCADE` on `user_id` automatically removes guides when the user is deleted.

## Data Access Layer

`db/guide_store.py` provides CRUD operations and default guide management. All functions are `async` and use `AsyncSessionLocal` from `db/engine.py`. Callers must `await` every store call. See [Database Architecture](database.md) for the store summary.

Key behaviors: the default guide cannot be renamed, but (since guides are deprecated) it can be deleted like any other guide and is never auto-recreated. Guides are listed with the default first, then alphabetically by name.

## Guide Snapshot Mechanism

When the first message of a conversation carries an explicit `guide_id` (routine guide override -- the only remaining source), that guide's content is snapshotted into the conversation's `chat_history.json`. This ensures:

- The conversation's system prompt is preserved even if the guide is later edited or deleted
- Subsequent messages in the same conversation always use the snapshotted content

**Snapshot storage** is in `chat_history.json` as top-level fields:
- `guide_id`: UUID of the guide that was used
- `guide_snapshot`: Object with `name` and `content` keys

**Snapshot persistence** is handled by `ChatStorage.set_guide_snapshot()` and `ChatStorage.guide_snapshot_from()` (applied to the turn-start `chat_history.json` read) in `chat/storage.py`. The `set_guide_snapshot()` method is idempotent -- it only writes if no snapshot exists yet.

## Guide Resolution Flow

The guide resolution logic lives in `run_conversation_turn()` in `chat/gemini_api/conversation.py`:

```
0. If the `guides` feature gate is closed for the user: skip everything
   below -- no snapshot read, no guide_id lookup, no snapshot write
   (logged), custom prompt stays empty
1. Check if conversation already has a snapshotted guide
   - If yes: use the snapshotted content (preserves guide across edits/deletions)
2. If no snapshot AND an explicit guide_id was provided (routine guide
   override): look up that guide and snapshot it into the conversation
3. Otherwise: run with no custom prompt and snapshot nothing. There is NO
   default-guide fallback anymore -- guides are deprecated and the composer
   sends no guide_id.
4. Pass the guide content as custom_system_prompt to get_system_prompt()
```

The `guide_id` parameter still flows through the persistent-WS `send_message` payload into `chat/realtime/socket.py:_handle_send_message` and on into `run_conversation_turn()`, but the only frontend path that sets it is the routine auto-send (`pendingRoutineMessage` in `ChatPanel.tsx`); the scheduler passes the routine's `guide_id` directly. See [Realtime Architecture](realtime.md) and [Routines Architecture](routines.md).

## Backward Compatibility

The Guides feature maintains full backward compatibility with the existing custom system prompt:

- **Migration**: The Alembic migration `4e8960c4dacc` creates a default guide for each existing user, copying their `custom_system_prompt` content from the `settings` JSON column
- **Settings sync**: When the custom system prompt is updated via `PUT /app/api/settings`, the `update_settings()` handler in `chat/routes/user.py` also updates the default guide's content, keeping them in sync. If the default guide no longer exists (converted to a skill or deleted), the sync is skipped -- it is not recreated. Note the default guide no longer applies to new conversations either way
- **Old conversations**: Conversations with a `guide_snapshot` in their `chat_history.json` keep using the snapshotted content on every turn; conversations without one run with no custom prompt

## Frontend Architecture

### Guide State Management

`frontend/src/contexts/GuidesContext.tsx` keeps only the guide LIST (`guides`, `guidesLoaded`, `loadGuides()`), consumed by the Settings Guides section refresh, the routine guide-override dropdown in `NewRoutineModal.tsx`, and the read-only guide display in `RoutineSettingsModal.tsx` (the settings modal no longer offers guide selection -- a routine that still has a `guide_id` shows the guide's name with a Clear button, and routines without one show no guide UI at all; see [Routines Architecture](routines.md)).

Per-conversation guide selection, guide locking, and the associated localStorage keys (`quest_conversation_guides`, `quest_default_guide`, `quest_locked_conversations`) were removed along with the composer guide selector; stale localStorage entries are simply ignored.

### Chat UI

The composer renders no guide UI. The only guide-carrying frontend path is the routine auto-send: `Sidebar.tsx` stashes the routine's `guide_id` in `pendingRoutineMessage`, and `ChatPanel.tsx`'s auto-send effect forwards it in the `send_message` envelope.

### Settings UI

The "Guides" section in `settings/GuidesSection.tsx` manages existing guides only:

- Deprecation notice at the top ("Skills now replace guides") with an in-modal link to the Skills section (via the `onNavigateToSkills` prop wired in `SettingsModal.tsx`)
- List all guides with name and content preview (first 150 characters)
- Default guide shows a "Default" badge and cannot be renamed
- "Convert to Skill" button per guide (hidden for empty guides) with a confirmation dialog; on success the guide disappears from the list and the status line names the created skill
- Edit existing guides inline (name and content)
- Delete any guide, the default included (with confirmation dialog)
- Save status feedback ("Guide saved" / error message)
- After any mutation (update/convert/delete), `refreshContextGuides()` is called to update the guide list in `GuidesContext` (which feeds the `NewRoutineModal` guide-override dropdown and the `RoutineSettingsModal` read-only guide display)

## Design Decisions

**Why snapshot guides into conversations instead of referencing them by ID?**
If guides were referenced by ID, editing a guide would retroactively change the system prompt for all conversations using it, potentially altering the context of ongoing conversations. Snapshotting on first message preserves the instructions as they were when the conversation started, providing predictable behavior.

**Why a default guide instead of making guides entirely optional?**
The default guide provided a migration path from the old single custom system prompt: the Alembic migration created one per user, so behavior was identical to before the feature was added. Since guides are deprecated, a missing default guide is no longer recreated and conversations simply run without a custom prompt.

**Why can't the default guide be renamed?**
The "Default" name clearly identifies the fallback guide in the UI. (It used to be undeletable too, so a valid fallback always existed; deprecation removed that protection so the default guide can be converted to a skill and deleted.)

**Why does converting the default guide auto-load the resulting skill?**
The default guide applied to every conversation. A user-level auto-loaded skill is the skills-world equivalent -- its content is merged into the system prompt of every conversation -- so conversion preserves behavior without the user re-enabling anything.

**Why delete the guide after conversion instead of keeping both?**
Keeping both would inject the same instructions twice (guide snapshot + auto-loaded skill) and leave users maintaining two copies. Conversion is a migration, not a copy; existing conversations are safe because they hold snapshots.

**Why sync the settings custom_system_prompt with the default guide?**
The old Settings panel has a "Custom Instructions" section with a textarea. To maintain backward compatibility, updating this textarea also updates the default guide's content. This ensures users who interact with the old UI path see consistent behavior. (With the default-guide fallback removed, this sync only affects routines that explicitly override to the default guide -- which the routine dropdowns never offered -- so it is effectively legacy.)

**Why 16KB max guide content instead of 4KB like memories?**
System prompts need to be more detailed than memories. The 16KB limit (`MAX_GUIDE_CONTENT_SIZE` in `db/guide_store.py`) provides enough room for comprehensive instructions while preventing abuse.

**Why remove the default-guide fallback instead of keeping it alongside the removed selector?**
With no selector, a silently-applied default guide would be invisible behavior the user can neither see nor change from the composer. Removing the fallback makes the deprecation legible: new conversations run without guides, and users who want their default instructions back convert the guide to a user-auto-loaded skill (the conversion flow does this automatically for the default guide).

## Constraints

- Everything below applies only while the `guides` feature gate is open for the user; while closed, all `/app/api/guides*` routes return 403 `guides_disabled`, routine guide overrides are rejected, and no guide content (explicit or snapshotted) reaches a conversation
- New guides cannot be created: `POST /app/api/guides` returns 410 `guides_deprecated`
- New conversations never resolve a guide unless started with an explicit `guide_id` (routine guide override); there is no default-guide fallback and the composer sends no `guide_id`
- Each user's guide names must be unique (enforced by the `ix_guides_user_id_name` composite unique index)
- Guide name maximum length: 100 characters (enforced in `db/guide_store.py` via `MAX_GUIDE_NAME_LENGTH`)
- Guide content maximum size: 16KB (enforced in `db/guide_store.py` via `MAX_GUIDE_CONTENT_SIZE`)
- The default guide cannot be renamed (enforced in `db/guide_store.py`); it can be deleted or converted like any other guide
- Guides with empty content cannot be converted to skills (400 `empty_guide`)
- Guides are deleted when the user account is deleted (via `ON DELETE CASCADE` foreign key and explicit `delete_all_user_guides()` in the account deletion flow)
- Deleting a guide sets `routines.guide_id` to NULL for any routines referencing it (via `ON DELETE SET NULL` FK); the routines themselves are not deleted
- The `guides` table and its indexes are created by the Alembic migration `4e8960c4dacc`
- Guide content is injected into the system prompt via the existing `custom_system_prompt` parameter path in `get_system_prompt()` and `get_sub_agent_system_prompt()` in `chat/gemini_api/system_prompt.py`
