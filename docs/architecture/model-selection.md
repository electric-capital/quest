# Model Selection (Admin Settings)

## Overview

Admin-only Settings > Model Selection section: a table with one row per enabled, non-deprecated model (Vertex registry models plus every provider instance's models, see [Inference Providers](inference-providers.md)) carrying three per-model *presentation and usage* settings that sit on top of the provider layer's "does this model exist and is it enabled" state:

- **Top-level slot** (`slot`, `1..MAX_TOP_LEVEL_SLOTS` = 5, or none): whether -- and where -- the model is pinned to the top level of the composer's model menu, above the "All models" flyout, in private conversations. Slots are unique across models; the section's slot dropdown swaps two models when a taken slot is picked so nothing silently drops out of the top level.
- **Public top-level slot** (`public_slot`, same range): the same for the separate top level shown in public-project conversations. Private and public slots are independent number spaces.
- **Descriptor** (`descriptor`, free text up to `MAX_DESCRIPTOR_LENGTH` = 60 chars): the label shown for a slotted model in either top level ("Smart ($$$)"), with the model's own name as the sublabel; empty shows the bare model name. The app never parses it -- admins put cost markers or anything else in there.
- **Allowed in private / public conversations** (`allow_private` / `allow_public`): whether the model may be used in private conversations (everything outside a public project: standalone and project chats, routines, Slack, subagent and inference API runs) and in public-project conversations (internet-enabled sandbox, see [Public Projects](public-projects.md)). A model not allowed for a visibility cannot hold that visibility's slot (normalization clears it; the UI disables the dropdown and vacates the slot when the box is unticked).

**Layout.** Public projects are a default feature ([public-projects.md](public-projects.md)), so the table always shows the public slot column and the two "Allowed" checkbox columns under grouped "Private conversations" / "Public conversations" headers, with a **private menu preview and a public menu preview** side by side above it. Each preview renders the draft as the composer menu would show it (slotted models in slot order, then "All models"), greying out models that are currently unavailable. Edits accumulate in a draft and save as one full replacement. (Until October 2026 the public columns appeared only while the since-removed `public_projects` gate was on; `public_mode_enabled()` and the `public_mode_enabled` response field went with it.)

Unlike the Inference Providers enable checkbox -- which only hides a model from the picker while existing conversations keep running -- the allow flags are **usage rules**: the composer hides a disallowed model from the menu and disables Send while a disallowed model is selected, and `run_conversation_turn` refuses the turn with a durable error message, so an existing conversation on a model that was later disallowed cannot continue until the user switches models.

## Storage

`config/model_selection.py` (standard library only, like `config/feature_gates.py`) persists `DATA_DIR / "model_selection.json"` (`MODEL_SELECTION_FILE` in `config/paths.py`) as `{"version": 1, "models": {"<model_id>": {slot, public_slot, descriptor, allow_private, allow_public}}}` with atomic writes. Only entries that differ from `UNSET_ENTRY` (no slots, empty descriptor, allowed everywhere) are written. `SLOT_FIELDS` maps each visibility to its slot field.

- `read_model_selection()` -- a **missing file** yields `DEFAULT_MODEL_SELECTION`, the historical hardcoded picks in both menus (Claude Opus 4.8 "Smart ($$$)", Claude Sonnet 5 "Faster ($$)", Gemini 3.8 Flash "Fastest ($)"); once an admin saves, the file is the whole truth, so clearing every slot stays cleared. A malformed file yields an empty selection (never the defaults). Duplicate slots from hand-editing are resolved per menu by dropping the later one.
- `validate_model_selection()` / `save_model_selection()` -- normalize (`normalize_entry`: out-of-range slots become none, a slot for a disallowed visibility is cleared, descriptors trimmed and capped, non-bool flags default to allowed) and reject duplicate slots within a menu with `ValueError` before writing.
- `selection_for(model_id)` -- entry for a stored id; a bare legacy OpenRouter id is looked up under its qualified `openrouter:` form (see `canonical_model_id` in `config/inference_providers.py`).
- `is_model_allowed(model_id, public=...)` -- the usage check; unknown ids are allowed (the provider layer decides whether they exist).

## Enforcement and data flow

1. **Catalog** -- `public_model_catalog()` in `chat/llm/config.py` adds `slot` / `public_slot` / `descriptor` / `allow_private` / `allow_public` to every entry of `models` on the unauthenticated `GET /app/api/config` (`chat/routes/user.py`), so nothing is mirrored by hand on the client.
2. **Frontend catalog** -- `setModelCatalog()` in `frontend/src/constants/models.ts` folds the fields into `ModelInfo` (`slot`, `publicSlot`, `descriptor`, `allowPrivate`, `allowPublic`); the pre-config `BUILTIN_MODELS` fallback carries the same defaults as `DEFAULT_MODEL_SELECTION`. `getTopLevelModels(visibility)` returns the models slotted in that visibility's menu in slot order (`slotFor()` picks the field); `getSelectableModels(visibility)` (default `'private'`) drops deprecated models and models not allowed for that visibility -- this default covers the routine pickers (`NewRoutineModal.tsx`, `RoutineSettingsModal.tsx`) and the Slack default-model picker (`settings/SlackSection.tsx`) unchanged; `isModelAllowedFor()` is the per-model check.
3. **Composer** -- `frontend/src/components/Composer.tsx` derives `modelVisibility` from its `isPublicProject` prop (passed by `ChatPanel.tsx` after fetching the project, and by `HomeComposer.tsx` from the drilled project's `public` flag), filters the menu with `getSelectableModels(modelVisibility)`, disables Send while the selected model is a known-but-disallowed one, and passes `visibility` to `ModelSelector.tsx`, which renders `getTopLevelModels(visibility)` (descriptor over model name) at the top level and suffixes a not-offered current selection with "(deprecated)" / "(not allowed here)" / "(no credentials)". The selection itself is per visibility: the per-user last-used model is stored as `users.settings.default_model` (private) / `public_default_model` (public), and a new-chat composer that switches context (the home composer drilled into / out of a public project, an empty conversation whose project fetch reveals it is public) re-resolves the pick for the new context via `refreshDefaultModel(visibility)` instead of keeping a model that may not be allowed there -- see the ConversationModelsContext notes in [Frontend](frontend.md).
4. **Server** -- `run_conversation_turn` in `chat/gemini_api/conversation.py` calls `is_model_allowed(model, public=is_public)` right after the public-projects gate check and raises `RuntimeError` (surfaced as the durable error bubble) for a disallowed model. This is the only server-side enforcement point; every driver (`chat/realtime/socket.py`, `chat/scheduler.py`, `chat/slack_socket_mode.py`, `chat/user_subagent.py`, `chat/inference_api.py`, `chat/wait_handles/resume.py`) goes through it. Sub-agent models chosen inside a turn are not gated.
5. **Admin save** -- `ModelSelectionSection.tsx` PUTs the full table and then calls `refreshModelCatalog()` from `AppConfigContext.tsx` (a `GET /app/api/config` re-fetch) so the admin's own composer menu updates without a reload; other tabs pick the change up on their next load.

## Admin API

`chat/routes/admin.py`, admin-gated via `_require_admin` (403 otherwise):

- `GET /admin/model-selection` -- `{max_slots, max_descriptor_length, models: [row]}` via `_model_selection_view()`: one row per enabled, non-deprecated model in `list_model_specs()` order with `id`, `wire_id`, `display_name`, `provider_label`, `instance_id`, the stored selection fields, and `available` / `unavailable_reason` (`not_configured` when the model is outside `get_configured_models()`, `failing` when a health verdict hides it from `get_available_models()`, else null). Entries stored for models that are currently disabled or removed are left in the file by reads.
- `PUT /admin/model-selection` -- full replacement (`ModelSelectionUpdate`: `models: [{id, slot, public_slot, descriptor, allow_private, allow_public}]`); unlisted models are reset to unset. 400 `unknown_model` for an id `resolve_model()` cannot resolve, 400 `invalid_params` for a model listed twice, a slot outside `1..max_slots`, an over-long descriptor, or duplicate slots within a menu; a slot for a disallowed visibility is cleared, not rejected; nothing is written on rejection. Returns the GET shape.

Frontend client: `fetchModelSelection` / `updateModelSelection` in `frontend/src/api/client.ts` (`adminModelSelection` in `frontend/src/api/config.ts`), types `ModelSelectionRow` / `ModelSelectionListResponse` / `ModelSelectionUpdate` in `frontend/src/api/types.ts`.

## Key Files

- `config/model_selection.py` -- store, normalization, defaults, `is_model_allowed()`
- `config/paths.py` -- `MODEL_SELECTION_FILE`
- `chat/llm/config.py` -- `public_model_catalog()` selection fields
- `chat/gemini_api/conversation.py` -- turn-level usage check
- `chat/routes/admin.py` -- `/admin/model-selection` endpoints, `_model_selection_view()`
- `frontend/src/constants/models.ts` -- `ModelInfo` selection fields, `getTopLevelModels()`, `getSelectableModels(visibility)`, `isModelAllowedFor()`
- `frontend/src/components/ModelSelector.tsx` -- top level from `getTopLevelModels()`, `visibility` prop; desktop popover + flyout, full-screen sheet on phone widths
- `frontend/src/components/Composer.tsx`, `HomeComposer.tsx` -- visibility filtering, Send gating, `isPublicProject` plumbing
- `frontend/src/components/settings/ModelSelectionSection.tsx` / `.css` -- the table (single-menu vs public-mode column sets), per-menu slot swapping, the menu previews (reuse the `.model-menu*` classes from `ChatPanel.css`; side panel in single-menu mode, a row above the table in public mode)
- `frontend/src/contexts/AppConfigContext.tsx` -- `refreshModelCatalog()`
- `tests/test_model_selection.py` -- store, catalog fields, admin endpoints; `tests/conftest.py` redirects the store to tmp_path for every test

## Design Decisions

- **Separate file from `inference_providers.json`.** Provider configuration says what exists; selection says how it is presented and where it may be used. Keeping them apart lets the provider store stay strictly normalized and lets a model's selection entry survive being disabled and re-enabled.
- **Absent file = historical defaults, saved file = whole truth.** New installs keep the "Smart / Faster / Fastest" top level the menu always had; an admin who clears every slot must not see them come back.
- **Full-replacement PUT.** The section edits many cells at once; one atomic save of the whole table is simpler and avoids partial states between per-cell saves.
- **Usage flags are enforced per turn, not just at selection.** Unticking Public for a model is meant to keep it out of internet-enabled sandboxes, which selection-time filtering alone cannot guarantee for conversations already on the model.
- **Default visibility is private.** Every model picker outside a public project (routines, Slack default, composer) calls `getSelectableModels()` without arguments, so a new picker is private-filtered unless it opts into public.
