# Slack Pending-Request Notifier

## Overview

A background daemon that reminds users over Slack about whatever is "unanswered" in their inbox: action requests that are still `open` and waiting for an Approve / Revise / Stop click in the web UI, and suggestions still awaiting review in the inbox's Memories tab (personal memories, project memories and Quest Doc notes). While a user has at least one such item, Quest's Slack bot DMs them a reminder at most once per interval (default 60 minutes). The interval is tunable per user and the reminders can be disabled entirely from Settings > Slack.

The notifier only needs the shared Slack bot token (`chat.postMessage`) and the user's per-user Slack OAuth record (for the `U...` user id to DM) -- Socket Mode is not required. On deployments without Slack credentials, or for users who never connected Slack, the loop is inert.

## Key Files

- [`chat/slack_notifier.py`](../../chat/slack_notifier.py) -- the daemon: `notifier_loop()` polling every `POLL_INTERVAL_SECONDS`, `_collect_pending()` merging the two summary queries per user, per-user due-checking, DM composition (`_build_reminder`), and the in-process `_last_notified` throttle map. Constants: `DEFAULT_INTERVAL_MINUTES = 60`, `MIN_INTERVAL_MINUTES = 5`, `MAX_INTERVAL_MINUTES = 1440`
- [`db/action_request_store.py`](../../db/action_request_store.py) -- `list_open_request_summaries()`: one row per user with open requests (`user_id`, `open_count`, `oldest_created_at`), riding the `ix_action_requests_user_id_status` index
- [`chat/memory_inbox.py`](../../chat/memory_inbox.py) -- `list_pending_summaries()`: the same shape (`pending_count`) for pending suggestions of the inbox's three kinds; see [Memory Suggestions](memory-suggestions.md)
- [`chat/routes/user.py`](../../chat/routes/user.py) -- `PUT /settings` validation for the two `users.settings` keys (see below)
- [`quest.py`](../../quest.py) -- `lifespan()` starts `notifier_loop()` as an asyncio task next to the routine scheduler and cancels it on shutdown
- [`auth/config.py`](../../auth/config.py) -- `load_slack_bot_token()`, loaded fresh each poll cycle so admin credential changes apply without a restart
- [`frontend/src/components/settings/SlackSection.tsx`](../../frontend/src/components/settings/SlackSection.tsx) -- the enable checkbox and interval input in Settings > Slack (section listed only while the user's Slack connector row is connected)
- [`frontend/src/api/types.ts`](../../frontend/src/api/types.ts) -- `UserSettings` keys
- Tests: [`tests/test_slack_pending_notifier.py`](../../tests/test_slack_pending_notifier.py), [`tests/test_action_request_open_summaries.py`](../../tests/test_action_request_open_summaries.py), [`tests/test_user_settings_slack_notifications.py`](../../tests/test_user_settings_slack_notifications.py)

## Per-User Settings

Two keys in the `users.settings` JSON column (same pattern as `slack_default_model`; no schema migration):

- `slack_pending_notifications_enabled` -- absent/`null` means **enabled** (the feature is on by default); `false` is the only stored off state. A boolean survives the `exclude_none` filter in `PUT /settings`, so the FE can send `false` directly.
- `slack_pending_notification_interval_minutes` -- reminder cadence; absent/`null` means the server default (60). `PUT /settings` rejects values outside `[MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES]` with `invalid_interval` and treats `0` as "clear" (mirroring the empty-string convention on string settings). The FE sends the default value as `0` so users on the default track any future server-default change. The notifier additionally clamps whatever is stored via `resolve_interval_minutes()` (defense against hand-edited rows).

## Poll Cycle

`_poll_once()` in `chat/slack_notifier.py`, every 60 seconds:

1. `_collect_pending()` -- `list_open_request_summaries()` (all users with open action requests) and `list_pending_summaries()` (all users with pending suggestions of any kind), two grouped queries merged into one row per user: `open_count`, `suggestion_count` and the `oldest_created_at` of either kind
2. Prune `_last_notified` entries for users no longer in the set (so a user's *next* pending item starts a fresh interval)
3. Load the bot token; return quietly when Slack is not configured
4. Per user: skip when the user row is gone, `slack_pending_notifications_enabled` is `false`, or the Slack OAuth record has no `user_id`/`access_token` (blob shape mirrors `get_user_by_slack_user_id()` in `db/user_store.py`)
5. Due check (both gates use the user's interval):
   - **age gate**: the oldest unanswered item must itself be at least one interval old -- a freshly-proposed card the user is probably already looking at is never insta-DMed
   - **throttle gate**: at least one interval since the last reminder to this user (`_last_notified`)
6. Send the DM (`chat.postMessage` with the bare `U...` id as `channel`, Block Kit section + a context line pointing at Settings > Slack), recording `_last_notified` **before** the send so a persistently failing send waits out a full interval instead of retrying every poll tick. Per-user failures are logged and do not block other users. The text names whichever counts are non-zero ("2 unanswered requests and 1 suggested memory"). When `app_base_url` is configured (`config/server_config.py`), the DM carries an "Open your inbox" link to `<app_base_url>/inbox` -- or `/inbox/memories` when only suggestions wait -- the deep-linkable URLs of the inbox's tabs (see below); without it the DM is link-free.

`created_at` values come back naive from the SQLite `DateTime` column and are normalized to aware UTC (`_as_aware_utc`).

## The /inbox Deep Links

The inbox (`RequestsView`) has a URL per tab so the reminder DM can land the user directly on it:

- `quest.py` -- `serve_spa_inbox()` serves `index.html` for `GET /inbox` and `GET /inbox/memories` (explicit routes; the `/{filename}` static fallback would 404 them)
- `frontend/src/App.tsx` -- `/inbox` and `/inbox/memories` routes rendering `AppContent`, plus a URL->state sync effect: `showRequestsView` is set from the pathname being one of the two, so browser back/forward and any in-app navigation close the view naturally; `RequestsView` reads the pathname to pick the tab
- `frontend/src/components/Sidebar.tsx` -- the Inbox button navigates to `/inbox` (and sets the state directly for an instant open); every existing close path already navigates elsewhere, which flips the state via the sync effect

## Constraints

- `_last_notified` is per-process state. A restart forgets it; the age gate still applies, so the worst case is one reminder arriving early after a redeploy. Nothing is persisted per notification.
- Reminders count **open action requests** (`action_requests.status == "open"`) and **pending suggestions** of every inbox kind, doc note suggestions included whatever the user's `docs` and `doc_notes` gates ([Doc Note Suggestions -- Design Decisions](doc-notes-review.md#design-decisions)); the DM calls them all suggested memories. Pending `slack_reply` wait handles are answered in Slack itself and are deliberately not included.
- The DM is sent from the shared bot (no user attribution) because it is a system notification, not a user-authored message -- unlike `send_slack_message` action requests, which carry the Quest attribution footer from `plugins/slack/handlers.py`.
- The notifier lives in core `chat/` and does not import the Slack plugin; it uses `slack_sdk.AsyncWebClient` + `load_slack_bot_token()` like the Socket Mode worker and reads the oauth_blob shape directly (core stays plugin-agnostic).

## Design Decisions

**Why an age gate on the oldest unanswered item instead of notifying on the first poll?**
A user who just watched the model propose a card doesn't need a DM seconds later. Requiring the oldest open item to be a full interval old makes the DM a genuine "you forgot about this" reminder, and composes with the throttle gate so the steady state is exactly one DM per interval while anything stays open.

**Why one reminder for requests and suggested memories?**
Both are the same chore from the user's side -- things waiting in the inbox -- and two daemons would mean two DMs per interval. One merged row per user keeps the gates, the throttle and the opt-out setting shared, and the DM just names whichever counts are non-zero.

**Why in-process throttle state instead of a DB column?**
The cost of losing the state (one possibly-early reminder after a restart) is far below the cost of a migration plus per-notification writes. This mirrors `chat/scheduler.py`'s module-level `_last_reconversion_date`.

**Why default-on?**
The reminder only fires for users who have connected Slack and who have left requests unanswered for an hour -- exactly the audience that benefits. The Settings > Slack checkbox (and the context line in every DM pointing at it) makes opting out one click.
