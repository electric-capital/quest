# Feature Gates

## Overview

Feature gates are **server-global, admin-controlled on/off switches for optional features**. Every gated feature is off by default; an admin turns it on for the whole instance from the desktop-only **Settings > Features** section. Gated features: `user_subagents` (cross-user subagents, [user-subagents.md](user-subagents.md)), `public_project_routines` (routines in [public projects](public-projects.md); the projects themselves are a default feature with no gate since October 2026), `guides` (the deprecated legacy Guides feature kept alive for installs still migrating to skills, [guides.md](guides.md)), `voice_input` (dictated prompts transcribed server-side on Vertex, [voice-input.md](voice-input.md)), and `docs` (Quest Docs, [quest-docs.md](quest-docs.md)).

Features in `PER_USER_ACCESS_FEATURES` (currently `public_project_routines`, `guides`, `voice_input` and `docs`) additionally support **per-user access**: an enabled gate can be open to all users or restricted to an `allowed_users` email list (case-insensitive). For a user outside the list the gate behaves exactly as if it were closed, at every enforcement point. `user_subagents` deliberately stays all-or-nothing — a run involves two users (caller and target) and a per-user list would be ambiguous about which side it restricts.

For features that ride on a per-conversation flag ([conversation-flags.md](conversation-flags.md)), the gate is a second switch ON TOP of the flag, not a replacement: the flag still opts an individual conversation in, but only while the admin gate is open. The feature key deliberately matches the flag name so the send path can drop globally-disabled flags generically.

## Key Files

| File | Description |
|------|-------------|
| `config/feature_gates.py` | The registry + store. `KNOWN_FEATURES` (tuple of gateable feature keys), `PER_USER_ACCESS_FEATURES` (features whose gate accepts an allowed-user list), `FEATURE_LABELS` (admin-UI label + description per feature), `read_feature_gates()` (normalized `{feature: {"enabled": bool, "allowed_users": list \| None}}`; missing/malformed file = all off; a present-but-malformed `allowed_users` fails closed to nobody), `is_feature_enabled()` (on at all, ignores the list), `is_feature_enabled_for_user()` / `enabled_features(user_email)` (per-user reads; `allowed_users: None` = all users, emails compared case-insensitively), `guides_enabled_for(user_email)` (convenience wrapper for the many guide enforcement points), `docs_enabled_for(user_email)` (same for Quest Docs), `public_project_routines_enabled_for(user_email)` (same for routines in public projects), `set_feature_enabled()` (preserves the stored list across off/on toggles) + `set_feature_allowed_users()` (atomic writes, `write_service_credentials`-style temp-file + `os.replace`), `filter_gated_flags()` (drops conversation flags whose matching gate is closed). Import-light (stdlib only) like `config/service_credentials.py` |
| `config/paths.py` | `FEATURE_GATES_FILE` (`data/feature_gates.json`; per feature either a legacy bool = enabled for everyone, or `{"enabled": bool, "allowed_users": [emails]}`) |
| `chat/routes/admin.py` | `GET /admin/feature-gates` (all features + state incl. `allowed_users`, `supports_user_access`, and `available` + `unavailable_reason` from `_feature_availability()` — False when the server lacks something the feature needs, today only `voice_input` without a configured Gemini Vertex model) and `PUT /admin/feature-gates/{feature}` (`{"enabled": bool, "allowed_users"?: [emails] \| null}` — omitted list = keep stored, null = all users, list only accepted for `PER_USER_ACCESS_FEATURES` else 400; 404 `unknown_feature`; `enabled: true` on an unavailable feature = 400 `feature_unavailable`, turning off always allowed), both `_require_admin`-gated. `GET /admin/users` grows `include_self=true` so the access picker can list the requesting admin (the impersonation picker keeps the default self-filtered roster) |
| `chat/routes/user.py` | `GET /me` returns `enabled_features` (the feature keys currently on **for this user**) so the FE can hide feature-gated composer flags and gated UI sections |
| `chat/realtime/socket.py` | `_handle_send_message` runs `filter_gated_flags()` over the merged first-message flag set (popover + `%%flags` line) before persistence, so a closed gate silently drops the flag — matching the forgiving unknown-flag behavior |
| `chat/gemini_api/conversation.py` (resolution) + `chat/gemini_api/turn_tools.py` (reject) | `user_subagents_enabled` = conversation flag AND open gate; a closed gate rejects `run_user_subagent` proposals same-turn with a distinct "disabled server-wide" error (covers conversations whose flag was persisted before an admin turned the feature off) |
| `chat/action_request_types/run_user_subagent.py` | `_require_feature_enabled()` re-checked in `validate_against_upstream` and `execute`, so a card approved after an admin closes the gate still fails |
| `frontend/src/components/settings/FeatureGatesSection.tsx` | Admin-only "Features" section: one card per feature with an Enabled/Disabled badge (restricted gates read "Enabled for N users") and an immediate-save toggle (reuses the `svc-cred-*` chrome; the toggle is disabled with the `unavailable_reason` shown while a gate is off and `available` is false). Gates with `supports_user_access` show a "Who has access" editor while enabled: All users / Only specific users radios plus a checkbox roster from `GET /admin/users?include_self=true` (allowed emails with no matching user row stay listed for revocation; an empty selection shows a nobody-has-access warning), every change saved immediately. Plain on/off toggles omit `allowed_users` so the stored list survives, and the FE remembers the last list so switching to All users and back restores it. After a save it calls `refreshEnabledFeatures()` so the admin's own composer updates without a reload |
| `frontend/src/contexts/AuthContext.tsx` | `enabledFeatures` (from `GET /me` at session check) + `refreshEnabledFeatures()` |
| `frontend/src/constants/flags.ts` | `FlagDefinition.feature` links a composer flag to its gate; `getVisibleFlags(enabledFeatures)` filters the popover list |
| `frontend/src/components/Composer.tsx` | Renders only `getVisibleFlags(...)` in the Flags popover and hides the Flags button entirely when nothing is visible |

## Adding a Gated Feature

1. Define a `FEATURE_*` constant in `config/feature_gates.py`, add it to `KNOWN_FEATURES` and `FEATURE_LABELS`. The admin endpoints and Settings UI pick it up automatically.
2. Consume `is_feature_enabled(FEATURE_*)` wherever the feature activates server-side (off must be fully inert).
3. If the feature rides on a conversation flag, name the feature key the same as the flag — `filter_gated_flags()` and the FE `feature` link then work without changes beyond `constants/flags.ts` gaining `feature: '<key>'` on the flag entry.
4. If the feature depends on server configuration the admin might not have (a credential, a provider), add a branch to `_feature_availability()` in `chat/routes/admin.py` returning `(False, reason)` while it is missing. The PUT then refuses to enable the gate and the Settings toggle explains why; `config/feature_gates.py` itself stays stdlib-only and knows nothing about availability.

## Enforcement Layers (voice input)

`voice_input` gates one endpoint and one button ([voice-input.md](voice-input.md)); it supports per-user access:

1. **Enablement** — `PUT /admin/feature-gates/voice_input` with `enabled: true` runs `chat/transcription.py` `transcription_availability()` and refuses with 400 `feature_unavailable` unless a Gemini Vertex project id is configured and at least one Gemini model is enabled. The stored list and an already-on gate are unaffected; turning off never needs Vertex.
2. **Request** — `POST /app/api/transcribe` (`chat/routes/transcribe.py`) checks `is_feature_enabled_for_user(FEATURE_VOICE_INPUT, email)` and returns 403 `voice_input_disabled` otherwise; it also re-checks availability per request (503 `transcription_unavailable`) so a gate left on after Vertex was unconfigured degrades cleanly.
3. **UI** — `Composer.tsx` shows the mic button only when `voice_input` is in the per-user `enabled_features` (and the browser can capture audio in this context).

## Enforcement Layers (cross-user subagents)

A closed `user_subagents` gate is enforced at four points, so no path relies on the FE hiding the checkbox:

1. **Send** — `filter_gated_flags()` drops the flag from both first-message activation paths before persistence.
2. **Proposal** — the `create_action_request` dispatch arm rejects `run_user_subagent` same-turn with a "disabled server-wide" error even when the conversation row already carries the flag (persisted while the gate was open).
3. **Same-turn validation** — `validate_against_upstream` raises before any card is shown.
4. **Approve** — `execute()` re-checks, covering cards that sat open across a gate change (TOCTOU, mirroring the existing target/skill re-check).

Already-running subagent runs are not killed by closing the gate; only new flag persistence, proposals, and approvals are blocked.

## Enforcement Layers (routines in public projects)

`public_project_routines` is a project-row gate, not a conversation flag (`filter_gated_flags()` never sees it): a routine in a public project runs in the internet-enabled sandbox, and a scheduled one does so unattended, so public projects have **no routines** unless an admin opts in. (Public projects themselves are ungated -- see [public-projects.md](public-projects.md).) It supports per-user access, and every layer goes through `public_project_routines_enabled_for(email)` with the acting user (the routine owner for scheduled runs). Private projects are never affected. While closed for a user:

1. **Routine + schedule API** -- every by-id routine endpoint in `chat/routine_routes.py` and every schedule endpoint in `chat/schedule_routes.py` resolves the project through `get_routine_project()`, which returns 400 `public_project_routines_disabled` for a public project; `GET /projects/{id}/routines` returns an empty list instead (the sidebar fetches it for every drilled project).
2. **One-click run** -- `POST /projects/{id}/conversations` with a `routine_id` (`create_project_conversation()` in `chat/project_routes.py`) returns the same 400 via `require_project_routines_allowed()`; plain conversations in the project are unaffected.
3. **Scheduler** -- `_poll_and_execute()` in `chat/scheduler.py` skips schedules whose routine is gated (`_public_routine_gated()`, fed by the `project_public` / `user_email` fields `db/schedule_store.py` joins into the routine summary): no conversation, no ledger row. `_skip_gated_schedule()` moves an anchored schedule's `next_due_at` past the due occurrence (logged once per occurrence), so reopening the gate resumes at the next regular occurrence rather than catching up; interval schedules fire on the first poll after the gate reopens. Interrupted runs are not retried while gated.
4. **Runtime** -- `run_conversation_turn` raises a durable error for any turn in a routine-created conversation (`routine_id` set) of a public project, the backstop for every path above.
5. **UI** -- `Sidebar.tsx` shows the drill-down Routines section for a public project only while `public_project_routines` is in the per-user `enabled_features`.

Nothing is deleted while closed: routine rows, schedules and the run ledger survive and become reachable again when access is restored.

## Enforcement Layers (guides)

`guides` keeps the deprecated Guides feature ([guides.md](guides.md)) available on an existing install while its users convert guides to skills; a fresh install leaves it off and guides are fully inert. It is neither a conversation flag nor a project-row gate: it gates a set of API routes plus the two runtime paths that apply guide content. It supports per-user access, so every layer checks `guides_enabled_for(user_email)` with the acting user (the routine owner for scheduled runs). Nothing is deleted while closed — guide rows, routine `guide_id` references, and conversation snapshots all survive, so reopening the gate restores the previous behavior exactly:

1. **API** — every `/app/api/guides*` route in `chat/guide_routes.py` (list/get/update/delete/convert-to-skill, and the already-retired POST) returns 403 `guides_disabled` while closed for the user.
2. **Routine guide overrides** — `POST`/`PUT /projects/{id}/routines` in `chat/routine_routes.py` reject a `guide_id` with 403 `guides_disabled`; `clear_guide` stays allowed so leftovers can be tidied. The agent-side `create_routine`/`edit_routine` action requests never accepted a guide, so nothing changes there.
3. **Runtime** — `run_conversation_turn()` in `chat/gemini_api/conversation.py` skips guide resolution entirely: an explicit `guide_id` (routine override from the web auto-send or the scheduler) is ignored and logged, and an existing conversation `guide_snapshot` is neither read nor written. The conversation runs with no custom prompt (project instructions and auto-loaded skills are unaffected).
4. **Settings sync** — the legacy `custom_system_prompt` → default-guide mirror in `PUT /settings` (`chat/routes/user.py`) is skipped, so a closed gate never writes to guides either.

FE: `SettingsModal.tsx` lists the Guides section only while `guides` is in the per-user `enabled_features` (and falls back to Data Connections if the gate closes while it is selected); `GuidesContext.tsx` fetches `GET /guides` only while the gate is on (and clears the list otherwise); `NewRoutineModal.tsx` hides the Guide Override picker and sends `guide_id: null`; `RoutineSettingsModal.tsx` still shows a leftover override (labelled "Guide (disabled)" with a note that it is ignored at run time) so the user can clear it.

Deliberately NOT gated: the admin System Reports > Guides deprecation tracker (`GET /admin/system-monitor/guides-report`, [admin-system-monitor.md](admin-system-monitor.md)) stays available regardless, since it is the tool an admin uses to see who still has guides before and after closing the gate; account deletion still purges guide rows (`delete_all_user_guides`).

## Enforcement Layers (Quest Docs)

`docs` gates [Quest Docs](quest-docs.md) and supports per-user access. Every layer calls `docs_enabled_for(email)` with the acting user. While closed for a user:

1. **Tools**: each of the seven doc tools returns the structured `{"error": "docs_disabled", ...}` before any DB work (`service.require_enabled()`, checked first in `chat/gemini_api/tool_handlers/docs.py`), so a routine that calls them fails cleanly.
2. **Prompt**: `get_user_connected_services()` in `api/instructions.py` sets the capability pseudo-key `docs` from the gate. The registry specs (`requires_service: "docs"`) and the `system:quest_docs` skill (`requires="docs"`) then drop out like a disconnected service. An explicit `load_skills` of `system:quest_docs` returns `docs_disabled_message()` (pointing at Settings > Features) rather than the connector text (`load_system_skills()` in `chat/system_skills/loader.py`). `PSEUDO_SERVICE_KEYS` keeps the key out of `GET /me`'s `has_any_service_connected` and is a reserved plugin id (`validate_plugin()` in `config/plugins.py`). The public-project prompt has no connected-services map, so `run_conversation_turn` passes the gate as `docs_enabled` to `get_public_project_system_prompt()`.
3. **API**: every `/app/api/docs*` route returns 403 `docs_disabled` first.
4. **Action request**: the `write_doc` pre-card and the approve-time `execute()` both go through the service gate, so a card approved after the gate closed fails.

Nothing is deleted. Doc rows, files and revisions survive, and restoring access brings them back.

## Scope Notes

- Gate state is read fresh from disk on each check (tiny file, no cache); toggles take effect for the next turn/proposal without a restart.
- Non-admin sessions learn the gate state only via `enabled_features` on `GET /me`, refreshed on session check — other users' composers pick up a toggle on their next load.
- The `system:user_subagents` system skill stays listed regardless of the gate (the `SystemSkill.requires` mechanism only understands connected services); loading it while the gate is closed just leads to the same-turn rejection above. Quest Docs works around the same limitation with a connected-services pseudo-key (`docs`, see above), so its skill and tools do disappear with the gate.
