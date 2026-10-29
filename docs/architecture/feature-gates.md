# Feature Gates

## Overview

Feature gates are **server-global, admin-controlled on/off switches for optional features**. Every gated feature is off by default; an admin turns it on for the whole instance from the desktop-only **Settings > Features** section. Gated features: `user_subagents` (cross-user subagents, [user-subagents.md](user-subagents.md)), `guides` (the deprecated legacy Guides feature kept alive for installs still migrating to skills, [guides.md](guides.md)), `voice_input` (dictated prompts transcribed server-side on Vertex, [voice-input.md](voice-input.md)), `docs` (Quest Docs, [quest-docs.md](quest-docs.md)), `doc_notes` (per-doc instructions and memories, [doc-notes.md](doc-notes.md)), `tables` (Quest Tables, [quest-tables.md](quest-tables.md)), and `browser` (the headless browser for public projects, [public-browser.md](public-browser.md)).

Features in `PER_USER_ACCESS_FEATURES` (`config/feature_gates.py`) additionally support **per-user access**: an enabled gate can be open to all users or restricted to an `allowed_users` email list (case-insensitive). For a user outside the list the gate behaves exactly as if it were closed, at every enforcement point. `user_subagents` deliberately stays all-or-nothing — a run involves two users (caller and target) and a per-user list would be ambiguous about which side it restricts.

A gate can **require another gate** (`FEATURE_REQUIRES` in `config/feature_gates.py`): it is then open for a user only while the gate it requires is open for that same user too. `is_feature_enabled_for_user()` applies the requirement, and so do `enabled_features()` (GET /me) and every `*_enabled_for()` wrapper built on it. The raw switch stays each gate's own: `read_feature_gates()`, `is_feature_enabled()` and the admin panel show it, so an admin can turn a dependent gate on while its prerequisite is off (it has no effect until the prerequisite opens), and the dependent gate's `FEATURE_LABELS` description names its prerequisite.

For features that ride on a per-conversation flag ([conversation-flags.md](conversation-flags.md)), the gate is a second switch ON TOP of the flag, not a replacement: the flag still opts an individual conversation in, but only while the admin gate is open. The feature key deliberately matches the flag name so the send path can drop globally-disabled flags generically.

## Key Files

| File | Description |
|------|-------------|
| `config/feature_gates.py` | The registry + store. `KNOWN_FEATURES` (tuple of gateable feature keys), `PER_USER_ACCESS_FEATURES` (features whose gate accepts an allowed-user list), `FEATURE_LABELS` (admin-UI label + description per feature), `read_feature_gates()` (normalized `{feature: {"enabled": bool, "allowed_users": list \| None}}`; missing/malformed file = all off; a present-but-malformed `allowed_users` fails closed to nobody), `FEATURE_REQUIRES` (gates that require another), `is_feature_enabled()` (the gate's own switch: ignores the list and `FEATURE_REQUIRES`), `is_feature_enabled_for_user()` / `enabled_features(user_email)` (per-user reads with requirements applied; `allowed_users: None` = all users, emails compared case-insensitively), `guides_enabled_for(user_email)` (convenience wrapper for the many guide enforcement points) and the other per-feature `*_enabled_for()` wrappers, `set_feature_enabled()` (preserves the stored list across off/on toggles) + `set_feature_allowed_users()` (atomic writes, `write_service_credentials`-style temp-file + `os.replace`), `filter_gated_flags()` (drops conversation flags whose matching gate is closed). Import-light (stdlib only) like `config/service_credentials.py` |
| `config/paths.py` | `FEATURE_GATES_FILE` (`data/feature_gates.json`; per feature either a legacy bool = enabled for everyone, or `{"enabled": bool, "allowed_users": [emails]}`) |
| `chat/routes/admin.py` | `GET /admin/feature-gates` (all features + state incl. `allowed_users`, `supports_user_access`, and `available` + `unavailable_reason` from `_feature_availability()` — False when the server lacks something the feature needs, today only `voice_input` without a configured Gemini Vertex model) and `PUT /admin/feature-gates/{feature}` (`{"enabled": bool, "allowed_users"?: [emails] \| null}` — omitted list = keep stored, null = all users, list only accepted for `PER_USER_ACCESS_FEATURES` else 400; 404 `unknown_feature`; `enabled: true` on an unavailable feature = 400 `feature_unavailable`, turning off always allowed), both `_require_admin`-gated. `GET /admin/users` grows `include_self=true` so the access picker can list the requesting admin (the impersonation picker keeps the default self-filtered roster) |
| `chat/routes/user.py` | `GET /me` returns `enabled_features` (the feature keys currently on **for this user**, requirements applied) so the FE can hide feature-gated composer flags and gated UI sections |
| `chat/realtime/socket.py` | `_handle_send_message` runs `filter_gated_flags()` over the merged first-message flag set (popover + `%%flags` line) before persistence, so a closed gate silently drops the flag — matching the forgiving unknown-flag behavior |
| `chat/gemini_api/conversation.py` (resolution) + `chat/gemini_api/turn_tools.py` (reject) | `user_subagents_enabled` = conversation flag AND open gate; a closed gate rejects `run_user_subagent` proposals same-turn with a distinct "disabled server-wide" error (covers conversations whose flag was persisted before an admin turned the feature off) |
| `chat/action_request_types/run_user_subagent.py` | `_require_feature_enabled()` re-checked in `validate_against_upstream` and `execute`, so a card approved after an admin closes the gate still fails |
| `frontend/src/components/settings/FeatureGatesSection.tsx` | Admin-only "Features" section: one card per feature with an Enabled/Disabled badge (restricted gates read "Enabled for N users") and an immediate-save toggle (reuses the `svc-cred-*` chrome; the toggle is disabled with the `unavailable_reason` shown while a gate is off and `available` is false). Gates with `supports_user_access` show a "Who has access" editor while enabled: All users / Only specific users radios plus a checkbox roster from `GET /admin/users?include_self=true` (allowed emails with no matching user row stay listed for revocation; an empty selection shows a nobody-has-access warning), every change saved immediately. Plain on/off toggles omit `allowed_users` so the stored list survives, and the FE remembers the last list so switching to All users and back restores it. After a save it calls `refreshEnabledFeatures()` so the admin's own composer updates without a reload |
| `frontend/src/contexts/AuthContext.tsx` | `enabledFeatures` (from `GET /me` at session check) + `refreshEnabledFeatures()` |
| `frontend/src/constants/flags.ts` | `FlagDefinition.feature` links a composer flag to its gate; `getVisibleFlags(enabledFeatures)` filters the popover list |
| `frontend/src/components/Composer.tsx` | Renders only `getVisibleFlags(...)` in the Flags popover and hides the Flags button entirely when nothing is visible |

## Adding a Gated Feature

1. Define a `FEATURE_*` constant in `config/feature_gates.py`, add it to `KNOWN_FEATURES` and `FEATURE_LABELS`. The admin endpoints and Settings UI pick it up automatically. Add it to `PER_USER_ACCESS_FEATURES` if access can be limited to some users, and to `FEATURE_REQUIRES` if it only works while another gate is open for the user (its description then says so).
2. Check the gate wherever the feature activates server-side (off must be fully inert). Enforcement points acting for a user call `is_feature_enabled_for_user(FEATURE_*, email)` or a `*_enabled_for()` wrapper with the acting user, which applies the allowed-user list and `FEATURE_REQUIRES`; the raw `is_feature_enabled()` fits only an all-or-nothing gate with no requirement, such as `user_subagents`.
3. If the feature rides on a conversation flag, name the feature key the same as the flag — `filter_gated_flags()` and the FE `feature` link then work without changes beyond `constants/flags.ts` gaining `feature: '<key>'` on the flag entry.
4. If the feature depends on server configuration the admin might not have (a credential, a provider, a container image), add a branch to `_feature_availability()` in `chat/routes/admin.py` returning `(False, reason)` while it is missing. The PUT then refuses to enable the gate and the Settings toggle explains why; `config/feature_gates.py` itself stays stdlib-only and knows nothing about availability.

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
2. **Prompt**: `get_user_connected_services()` in `api/instructions.py` sets the capability pseudo-key `docs` from the gate. The registry specs (`requires_service: "docs"`) and the `system:quest_docs` skill (`requires="docs"`) then drop out like a disconnected service. An explicit `load_skills` of `system:quest_docs` returns `docs_disabled_message()` (pointing at Settings > Features) rather than the connector text (`load_system_skills()` in `chat/system_skills/loader.py`). `PSEUDO_SERVICE_KEYS` keeps the key out of `GET /me`'s `has_any_service_connected` and is a reserved plugin id (`validate_plugin()` in `config/plugins.py`), since the gate would overwrite such a plugin's connected state. The public-project prompt has no connected-services map, so `run_conversation_turn` passes the gate as `docs_enabled` to `get_public_project_system_prompt()`.
3. **API**: every `/app/api/docs*` route returns 403 `docs_disabled` first.
4. **Action request**: the `write_doc` pre-card and the approve-time `execute()` both go through the service gate, so a card approved after the gate closed fails.
5. **Frontend**: the gate arrives as `enabled_features` on `GET /me` (`DOCS_FEATURE` in `frontend/src/api/docsApi.ts`). The sidebar Docs blocks are not rendered, the doc hooks fetch nothing (`enabled: false`), and a `/docs*` URL renders `DocsGateClosed.tsx` instead of the view.

Nothing is deleted. Doc rows, files and revisions survive, and restoring access brings them back.

## Enforcement Layers (Quest Doc notes)

`doc_notes` gates [Doc Notes](doc-notes.md), supports per-user access and requires `docs` (`FEATURE_REQUIRES`, above); callers pass the acting user's email, never the doc owner's. While it is closed for a user, `get_user_connected_services()` and `get_public_connected_services()` in `api/instructions.py` set the capability pseudo-key `doc_notes` (`DOC_NOTES_SERVICE_KEY` in `chat/docs/constants.py`, listed in `PSEUDO_SERVICE_KEYS`) to false, so anything declaring `requires_service: "doc_notes"` drops out like a disconnected service. The doc tools check it through `notes_enabled()` in `chat/docs/service.py`: while it is closed for the caller, no read carries notes counts or the notes block, `create_doc` writes no seen record, and no write or `write_doc` card is refused or pinned for notes, so every doc tool returns the payload it returns without notes ([Doc Notes -- Delivery on Reads](doc-notes.md#delivery-on-reads)).

The two notes tools, `list_doc_notes` and `suggest_doc_note`, declare the pseudo-key, so no prompt lists them while it is false, and both answer `{"error": "doc_notes_disabled", ...}` before anything else, for every run kind. The public-project prompt has no connected-services map, so `run_conversation_turn` passes the gate as `doc_notes_enabled` to `get_public_project_system_prompt()` ([Doc Notes Tools -- Registry and Gate](doc-notes-tools.md#registry-and-gate)).

Nothing is deleted, and the review of suggestions already filed is never gated ([Doc Notes -- Review](doc-notes.md#review)).

## Enforcement Layers (Quest Tables)

`tables` gates [Quest Tables](quest-tables.md) and supports per-user access. Every layer calls `tables_enabled_for(email)` with the acting user, and `tables_disabled_message()` in `chat/tables/constants.py` is the one gate-closed text. While closed for a user:

1. **Tools and service**: each of the seven table tools returns `{"error": "tables_disabled", ...}` before any DB work (`service.require_enabled()`, checked first in `chat/gemini_api/tool_handlers/tables.py`); every operation of `chat/tables/service.py` checks it too, so every other caller of the service fails the same way.
2. **Prompt**: `get_user_connected_services()` sets the capability pseudo-key `tables` from the gate, so registry specs with `requires_service: "tables"` and the `system:tables` skill (`requires="tables"`) drop out like a disconnected service, through the same `PSEUDO_SERVICE_KEYS` handling as `docs`. An explicit `load_skills` of `system:tables` returns `tables_disabled_message()` rather than the connector text (`load_system_skills()`). The public-project prompt gets the gate as `tables_enabled` on `get_public_project_system_prompt()`, which leaves out the table tools and the "Quest Tables" paragraph while it is false, and the project-database note pointing at tables appears only while the pseudo-key is set ([Quest Tables -- Prompting](quest-tables.md#prompting)).
3. **HTTP routes**: every table route under `/app/api` answers 403 `tables_disabled` first, before any DB work ([Quest Tables API -- HTTP Routes](../api/tables-api.md#http-routes)).

Nothing is deleted: table rows and files survive, and restoring access brings them back.

## Scope Notes

- Gate state is read fresh from disk on each check (tiny file, no cache); toggles take effect for the next turn/proposal without a restart.
- Non-admin sessions learn the gate state only via `enabled_features` on `GET /me`, refreshed on session check — other users' composers pick up a toggle on their next load.
- The `system:user_subagents` system skill stays listed regardless of the gate (the `SystemSkill.requires` mechanism only understands connected services); loading it while the gate is closed just leads to the same-turn rejection above. The per-user gates of features with their own tools and skills work around the same limitation with connected-services pseudo-keys (`PSEUDO_SERVICE_KEYS` in `api/instructions.py`, see above), so those skills and tools do disappear with the gate.

## Enforcement Layers (browser)

`browser` gates the `browser_*` tools of public projects ([public-browser.md](public-browser.md)) and supports per-user access. The gate can only be turned on while podman and the sidecar image are present (`browser_availability()` in `chat/browser_sessions.py` behind `_feature_availability()`; turning it off always works). While closed for a user, every handler returns `browser_disabled` before any container work, the public `load_skills` arm reports the same message as the `system:browser` load error, and `run_conversation_turn` passes `browser_enabled_for(email)` to the public prompt so the System Skills block and the `load_skills` tool are left out. The tools stay in the public dispatch allowlist regardless (the handler is the gate), and no private conversation ever sees them: the connected-services pseudo-key `browser` is always False there. Closing the gate does not stop sessions already running; they end on idle, `browser_close`, archive or shutdown.
