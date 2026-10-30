# The write_doc Request

## Overview

`write_doc` is the approval form of the [Quest Docs](quest-docs.md) write tools. When a doc's access verdict is `approval` ([Quest Docs -- Access Rule](quest-docs.md#access-rule)), `edit_doc` / `append_to_doc` / `add_doc_image` refuse with `approval_required` and a `suggested_request` the model forwards unchanged through `create_action_request`; one request type covers the four `WRITE_OPERATIONS` (`edit`, `multi_edit` for `edit_doc`'s [grouped form](quest-docs.md#grouped-edits), `append`, `add_image`). The card is one of the four a public conversation may open ([Public Projects -- Action requests](public-projects.md#action-requests)); the generic mechanics (suspend, resolve, Revise and Stop) are in [Action Requests](action-requests.md).

## Key Files

| File | Description |
|------|-------------|
| `chat/action_request_types/write_doc.py` | `WriteDocHandler`: `validate_params`, `render_preview`, `execute`; `OPERATION_PARAM_KEYS`, `EDIT_ITEM_KEYS`, `SERVER_INJECTED_KEYS` |
| `chat/action_request_types/doc_precard.py` | `doc_precard_check()` and `edit_previews()` |
| `chat/action_request_types/_skill_content_edit.py` | `apply_content_edit()` / `apply_content_edits()` and `build_bounded_content_diff()` |
| `chat/docs/service.py` | `preview_write_operation()` (the pre-card's dry run) and `apply_write_operation(..., bypass_approval=True)` (execute) |
| `frontend/src/components/ActionRequestPreviewFields.tsx`, `SkillContentDiffPreview.tsx`, `DocEditGroupPreview.tsx`, `DocImagePreview.tsx` | Card rendering |

## Pipeline

`WriteDocHandler` is registered in `chat/action_request_types/__init__.py`; `create_doc` has no action-request form. The pipeline follows `edit_skill`:

- **`validate_params`** checks shape only. It rejects unknown keys and keys of another operation (`OPERATION_PARAM_KEYS`; the items of a `multi_edit`'s `edits` are held to `EDIT_ITEM_KEYS` and an item's error names its position), fills the optional defaults, and drops any model-supplied copy of a server-injected key (`SERVER_INJECTED_KEYS`).
- **Pre-card** (`doc_precard_check()` in `doc_precard.py`, called from the `create_action_request` arm in `chat/gemini_api/turn_tools.py` after the skill and routine pre-cards) delegates to `service.preview_write_operation()`: the same access, read-sidecar, live-body dry-run and cap checks as the tool. A `free` verdict is rejected ("call the tool instead"), a denied, hidden, unread, stale or gate-closed case gets the service's own text, and unseen doc notes the refusal followed by the notes as text, all as same-turn `Invalid parameters` with no card. On `approval` it injects the `SERVER_INJECTED_KEYS` preview: the share summary carries counts only, never names, an `add_image` card gets an `image_preview` whose `sha256` pins the file, and with the `doc_notes` gate open for the proposer the doc's `instructions_version` pins its instructions.
- **Card diff**: `build_bounded_content_diff()` of the live body vs the result, computed in `asyncio.to_thread`, because the full-body `build_content_diff()` that `edit_skill` uses is quadratic on a large doc (the function's docstring gives the bounding steps and its keyword defaults the thresholds). Line numbers match `read_doc`'s, and `total_old_lines` / `total_new_lines` let the renderer mark what the window leaves out ([Card rendering](#card-rendering)). A `multi_edit` card gets no combined diff but `edit_previews` (`doc_precard.edit_previews()`): one such window per edit, in the request's order, each of the live body against that edit applied alone and capped at `EDIT_PREVIEW_MAX_LINES`, so the card shows the group change by change with the doc's own line numbers.
- **`execute`**: the resolve route in `chat/action_request_routes.py` passes `request_id=` to the types in `REQUEST_ID_TYPES`, this one and the [table cards](../api/tables-api.md#action-requests). `execute` calls `apply_write_operation(..., bypass_approval=True, write_source="action_request:<id>")` with the pinned `expected_sha256` for `add_image` and the instructions pin as `expected_instructions_version`. That re-resolves access (a hidden doc refuses, a doc whose shares were all removed is written), re-checks the read sidecar and re-applies against the live body: the TOCTOU close, so a stale `old_string`, or instructions changed since the proposal ([Doc Notes](doc-notes.md#instructions-pin-on-write_doc-cards)), fails the Approve and the request stays open. The service publishes the realtime events.
- **Attribution**: a card is proposed and approved in its proposer's own conversation. `get_action_request_owners()` credits a write-share recipient's card to them in the owner's row (`last_write_user`) and [History](quest-docs.md#history); the owner's own card links that conversation (`get_action_request_conversations()`).
- **Fail closed on a missing project**: `project_is_public()` raises `RuntimeError` when the conversation has a `project_id` but `project_store.get_project` finds no row for the user, rather than treating the conversation as private. The pre-card turns it into `Invalid parameters`; at Approve it is the resolve route's 500 `execution_failed` with the request left open.

`render_preview`, the labels and `summary_snippet` are on `WriteDocHandler`, with their rationale in the code comments; the diff reuses the `skill_content_diff` field type, a grouped edit is the `doc_edit_group` type (its value line sums the per-edit counts; without injected previews it falls back to numbered `Replace N` / `With N` rows) and the image preview is the `doc_image` type.

## Card rendering

Both card renderers go through `ActionRequestPreviewFields.tsx`. `DocImagePreview.tsx` renders the `doc_image` field, loading the thumbnail from the proposing conversation's own workspace (`add_image` reads only that workspace, so the card's conversation id always resolves it). `SkillContentDiffPreview.tsx` recognizes the bounded diff window by `total_old_lines` / `total_new_lines` and marks the lines outside it; whole-body skill and routine diffs carry no totals. `DocEditGroupPreview.tsx` renders the `doc_edit_group` field of a `multi_edit` card as one numbered block per edit (its position, where it lands by the doc's line numbers, the model's note, its own diff window).

## Testing

`tests/test_write_doc_action_request.py` (validation, the tool payload round-trip, the pre-card, execute, labels, end to end through the arm and the resolve route) and `tests/test_docs_grouped_edits.py` (`apply_content_edits`, the `edits` tool argument, the `multi_edit` card); frontend tests sit beside their components.
