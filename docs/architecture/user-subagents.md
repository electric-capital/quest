# Cross-User Subagents

Run a read-only subagent inside **another** user's account, with double
approval: the caller approves the exact prompt before it crosses the user
boundary, and the target user approves the exact response (and files)
before anything comes back. Registry-level pieces: two new
`ActionRequestType`s (`run_user_subagent`, `subagent_return`), a new
`ToolWaitHandleKind` (`user_subagent`), a new conversation `origin`
(`user_subagent`), the `user_subagent_runs` table, and the runtime module
`chat/user_subagent.py`.

## Lifecycle

1. **Proposal (caller side).** Doubly gated: the server-global
   `user_subagents` admin feature gate must be on
   ([feature-gates.md](feature-gates.md); off by default, toggled in
   Settings > Features) AND the caller conversation must have been started
   with the `user_subagents` conversation flag
   ([conversation-flags.md](conversation-flags.md)) or the dispatch arm
   rejects the request type same-turn (with a distinct "disabled
   server-wide" error when the gate is the blocker); both gate only this
   proposal side, never the launched subagent conversation. The handler
   also re-checks the feature gate in `validate_against_upstream` and
   `execute`, so a card approved after an admin closes the gate fails too. The caller's model calls
   `create_action_request(request_type="run_user_subagent", params={target_user_email, prompt, skill_ids?, model?})`
   (spec'd in the `system:user_subagents` system skill). Validation in
   `chat/action_request_types/run_user_subagent.py` fails immediately —
   same-turn `Invalid parameters`, no card — when the target user does not
   exist or cannot access any listed skill
   (`db/skill_store.py:user_can_access_skill` per skill against the
   *target*); `system:*` ids are rejected. The card shows the full prompt
   (the caller is verifying what information crosses the account
   boundary), the target user, resolved skill names, and the model.
2. **Launch (approve).** `execute()` re-runs the target/skill checks
   (TOCTOU), creates the subagent conversation via
   `ChatStorage.create_user_subagent_conversation` (owned by the target,
   `origin="user_subagent"`, custom name "Subagent for <caller>"), a
   caller-side wait handle (kind `user_subagent`, 14-day `expires_at`
   backstop), and the `user_subagent_runs` row linking everything, seeds
   the approved prompt as the first user message, publishes
   `conversation_list_changed` to the target, and starts the headless run.
   The execute result instructs the caller's model to block on
   `wait_for_handles(handle_ids=[wait_handle_id], ...)` — the existing
   generic wait machinery ([wait-handles.md](wait-handles.md)) delivers
   the eventual response.
3. **Run (target side).** `chat/user_subagent.py:_drive_run` mirrors
   `chat/wait_handles/resume.py:_run_resume` (flush callback, transient
   event mirroring, paired lifecycle envelopes) but runs as the target
   user. `run_conversation_turn` with `origin="user_subagent"` uses the
   `USER_SUBAGENT_TOOLS` tier (`BASE_TOOLS` + `return_to_caller`; no
   `create_action_request`, no `agent_task*`), the dedicated
   `get_user_subagent_system_prompt` (no guide, no custom prompt — the
   target is not driving), and loads ONLY the caller-specified skills
   (resolved for the target user) instead of any autoload tier. Usage is
   recorded against the target user and the subagent conversation; the
   caller conversation's *total* cost is reconstructible by joining
   `user_subagent_runs` on `caller_conversation_id` (indexed for this)
   and summing the linked subagent conversations' `llm_calls_*` rows —
   one level deep by construction, and run rows deliberately survive
   user/conversation deletion (no FKs) so the attribution outlives both
   (see [database.md](database.md)).
4. **Return call.** The subagent calls `return_to_caller(response,
   files?)`. Its dispatch handler (in `chat/gemini_api/turn_tools.py`,
   modeled on the `create_action_request` handler) validates via
   `chat/action_request_types/subagent_return.py` +
   `prepare_return_params` (files must exist in the subagent workspace,
   max 10 × 50 MB; entries enriched with name/size for the card), creates
   a `subagent_return` action request + `action_request` wait handle for
   the target user, flips the run to `awaiting_return`, and suspends via
   `SuspendForActionRequest`. The card shows the full response text and a
   files list with per-file preview (FE `SubagentReturnFilesPreview` →
   `FileViewerModal` against the subagent conversation's workspace).
   The generic `create_action_request` path rejects
   `request_type="subagent_return"` (and the type is excluded from the
   tool-schema enum) — only the arm can mint one.
5. **Resolution (target side)** via the standard
   `POST /action-requests/{id}/resolve`:
   - **Approve** — `execute()` re-verifies the files, copies them into
     the caller conversation's own conversation workspace under
     `.subagent_responses/` (no-clobber `-2`/`-3` suffixing; conversation-
     scope `file_list_changed` published to the caller) -- never the
     caller's project workspace, even when the caller is a project
     conversation (`copy_file` also refuses that dir as a source toward
     `proj://`), marks the run `returned`, and resolves the caller
     handle `accepted` with `{status: "returned", response, files,
     from_user}` — waking the caller conversation via
     `maybe_kick_resume`. The resumed subagent-side tool result tells the
     model the run is complete.
   - **Revise** (deny + feedback) — the deny branch in
     `chat/action_request_routes.py` suppresses the generic resume kick
     and calls `user_subagent.on_return_revised`, which flips the run
     back to `running` and re-drives the loop so the model reads the
     feedback off the closed `return_to_caller` tool_use and can propose
     another return call.
   - **Deny** (no feedback) — `user_subagent.finalize_denied_return`
     ends the run immediately (`denied`; the subagent conversation is
     NOT resumed) and resolves the caller handle `rejected` with
     `{status: "denied"}`.
6. **Failure paths.** A run exception, or a loop that ends without a
   return call despite one nudge, marks the run `failed` and resolves the
   caller handle with `{status: "failed", error}`. If the process dies
   mid-run, the caller handle's `expires_at` sweep eventually times the
   wait out (documented limitation: the orphaned run is not auto-restarted).

## Read-only conversation

The subagent conversation appears in the target user's sidebar with a
subagent badge. It is read-only end to end: the FE composer disables for
`origin === 'user_subagent'` (banner names the caller, sourced from the
`subagent_run` block on `GET /conversations/{id}`), the WS
`send_message` handler rejects sends with `read_only_conversation`, and
project conversion is rejected (`chat/project_routes.py`). The pending
`subagent_return` card is the only interaction point.

## Files

- `chat/user_subagent.py` — runtime (launch/resume/nudge/finalize; module
  `_app_ref` installed in `quest.py` lifespan)
- `chat/action_request_types/run_user_subagent.py`, `subagent_return.py`
- `db/user_subagent_run_store.py`, `UserSubagentRun` in `db/models.py`,
  migration `e4b1a7c92f05`
- `chat/gemini_api/system_prompt.py:get_user_subagent_system_prompt`
- `chat/llm/tool_schemas.py:USER_SUBAGENT_TOOLS` / `_RETURN_TO_CALLER`
- FE: `SubagentReturnFilesPreview.tsx`, plus origin/banner/badge wiring
