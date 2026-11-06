# Notifications

System notifications tell one user about something the system did on their behalf that needs no decision from them. They are the inbox's Other tab, beside the Requests tab ([Action Requests](action-requests.md)) and the Memories tab ([Memory Suggestions](memory-suggestions.md)).

## Overview

A notification is a one-way message: a `kind`, a title, a plain-text body and, per kind, structured `data` the UI may link from. It is created open and stays open until its user dismisses it; a dismissed notification stays listed. Nothing is ever approved, edited or resolved through one.

Open notifications count toward the sidebar's inbox badge and the Other tab's label, next to the open action requests and the pending suggestions.

## Key Files

- `chat/notifications.py` -- the feature's module: the kinds (`NOTIFICATION_KINDS`, each described in the module docstring with the shape of its `data`), `notify()` (create and publish), `dismiss()` and `publish_changed()`.
- `db/notification_store.py` -- data access over `user_notifications` (the `UserNotification` model in `db/models.py`): create, the per-user list, the open count and the owner-scoped dismiss.
- `chat/notification_routes.py` -- the REST routes, listed in [Chat API -- Notification Endpoints](../api/chat-api.md#notification-endpoints).
- `chat/realtime/events.py` -- `make_notifications_changed(open_count)`, the per-user global.
- `frontend/src/components/NotificationsInbox.tsx` -- the Other tab; its header comment gives the read ordering.
- `frontend/src/hooks/useOpenNotificationCount.ts` -- the open count behind the tab label (`RequestsView.tsx`) and the badge (`frontend/src/components/sidebar/RequestsBadge.tsx`).
- `alembic/versions/c5a9e3f7d2b1_convert_guides_to_skills.py` -- creates the table and writes the `guides_converted` rows.

## Data Model

One `user_notifications` row per notification, cascading with its user. `dismissed_at` is the only state: NULL while open. `title` and `body` always stand on their own, so a row of a kind the running frontend does not know still renders; `data` only adds what a known kind links to. The columns are on the `UserNotification` class.

## Flows

**Creating one.** Server code calls `notify()` in `chat/notifications.py`, which refuses a kind outside `NOTIFICATION_KINDS`, writes the row and publishes `notifications_changed`. A row written outside the server process, such as by an Alembic data migration, publishes nothing: the server is not running the bus then, and every tab reads the count when it loads.

**Reading and dismissing.**

1. The Other tab at `/inbox/other` → `frontend/src/components/NotificationsInbox.tsx` reads `GET /app/api/notifications`.
2. Dismiss → `POST /app/api/notifications/{id}/dismiss` → `dismiss()` in `chat/notifications.py` → `db/notification_store.py`. Dismissing twice keeps the first time; another user's id is a 404.
3. `publish_changed()` sends `notifications_changed {open_count}` to all of the user's tabs → `useOpenNotificationCount()` replaces its count and `NotificationsInbox` re-reads its list.

**Counts.** `useOpenNotificationCount()` is seeded from `GET /app/api/notifications/count` and then follows the event, like `usePendingSuggestionCounts()` does for suggestions; its header comment gives how a late seed never overwrites a newer event. `RequestsBadge` adds it to the open requests and pending suggestions.

**Links per kind.** `KIND_SETTINGS_LINKS` in `NotificationsInbox.tsx` maps a kind to the Settings section its card opens, through `settingsInitialSection` on `NavigationContext`.

## Constraints

- The list route returns the newest rows up to the store's `limit` default, with no paging: notifications are rare system messages, not an activity stream.
- A notification never gates anything. The Slack pending-request reminder ([Slack Pending-Request Notifier](slack-pending-request-notifier.md)) counts action requests and suggestions only.
- A new kind is a constant in `NOTIFICATION_KINDS` plus its entry in the module docstring; the frontend needs a change only to offer a link for it.

## Design Decisions

**Why a separate table and tab instead of an action request or a suggestion?**
Both of those wait for a decision and carry a lifecycle built around one (approve, revise, accept, dismiss with an outcome). A notification reports something already done, so it has one state change and no conversation, and mixing it into the Requests or Memories lists would put rows with nothing to approve among rows that block work.

**Why a plain-text body with structured `data` beside it?**
The row must read correctly on any frontend build, including one older than the kind, and from a database client. So the server writes the whole message as text, and `data` is an optional extra for linking. Rendering the body from `data` in the frontend would leave unknown kinds blank.

**Why does a dismissed notification stay listed?**
A notification can be the only record of a change the user did not make themselves. Dismissing clears it from the counts without removing the explanation.

## Migration Notes

Alembic migration `c5a9e3f7d2b1` converted every guide (a per-user system-prompt preset) with content into a private skill of its owner and wrote each owner one `guides_converted` notification. What a user or operator still has to act on:

- The converted skills are private and **not auto-loaded**. A user who wants one applied turns on its auto-load in Settings > Skills ([Skill Library](skill-library.md)) or loads it per conversation.
- A routine that had a guide attached runs without it. The notification lists those routines; adding the skill to the routine's auto-loaded skills ([Routines](routines.md)) restores the instructions.
- `guide_id` and `guide_snapshot` keys in existing `chat_history.json` files are ignored: those conversations run without the snapshotted text.
- A `guides` key left in `feature_gates.json` is ignored.
- The downgrade restores an empty `guides` schema only; the skills stay skills.

Naming on collisions, what is skipped, and why `routines` is edited in place are in the migration's docstring.
