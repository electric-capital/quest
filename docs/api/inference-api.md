# Inference API

One-shot, non-streaming inference endpoint for internal applications: send a single prompt, it
runs headlessly as a Quest user (their connected services, workspace tools, and skills), and the
model's final markdown answer comes back in the HTTP response. Used by other internal apps that
need Quest's data reach but render results on their own surfaces.

This file covers the server-side implementation. The consumer-facing integration guide (auth,
request/response contract, error codes, prompt-writing guidance, client examples) is the
repo-root [INFERENCE_API.md](../../INFERENCE_API.md) -- keep the two in sync when the contract
changes.

## API keys (Settings > Inference API)

- Table: `inference_api_keys` in `db/models.py` (`InferenceApiKey`) -- named per-user keys storing
  only the SHA-256 `token_hash` plus a display `token_hint`; raw tokens (`qst_` prefix) are shown
  exactly once at creation.
- Store + token helpers: `db/inference_api_key_store.py` (`generate_token`, `hash_token`,
  `MAX_KEYS_PER_USER`).
- CRUD routes: `chat/routes/inference_api_keys.py` --
  `GET/POST /app/api/inference-api-keys`, `DELETE /app/api/inference-api-keys/{id}` (cookie/API-key
  auth like every other settings endpoint). The POST response is the only place the raw `token`
  appears.
- Settings UI: `frontend/src/components/settings/InferenceApiSection.tsx` (all-users section,
  registered in `SettingsModal.tsx`); create-once token panel with copy button, delete with
  confirm.

## POST /api/inference

- Endpoint + driver: `chat/inference_api.py` (`inference_endpoint`, `run_inference`), mounted in
  `quest.py`. Blocked from LLM `curl_proxy_*` dispatch via `_BLOCKED_PROXY_PATHS` in
  `chat/route_dispatch.py` (recursion guard).
- Auth: `get_current_user_inference_token` -- ONLY inference API keys are accepted (not session
  cookies, not `users.api_key`), with the standard `check_user_allowed` domain gate. Successful
  auth bumps the key's `last_used_at`.
- Body: `{"prompt": "...", "model": "<optional MODEL_REGISTRY id>"}`. Model defaults to the user's
  `settings.default_model`, then the server-config default. Errors follow the standard
  `{"error", "message"}` detail shape (`invalid_prompt`, `invalid_model`, `no_response` 502,
  `inference_timeout` 504, `inference_failed` 502).
- Success: `{"response": "<markdown>", "conversation_id": "...", "model": "..."}`.

## Run mechanics

Mirrors the cross-user subagent runtime (`chat/user_subagent.py`), minus approval cards:

1. Each call creates a fresh conversation with `origin="inference_api"`
   (`ChatStorage.create_inference_api_conversation` in `chat/storage.py`); it is a read-only
   transcript (send guard in `chat/realtime/socket.py`, same `read_only_conversation` rejection
   as `user_subagent`; composer notice via `isReadOnly`/`readOnlyNotice` in `ChatPanel.tsx`).
   Like Slack conversations, inference runs are **hidden from the sidebar by default** -- a
   client-side origin filter in `Sidebar.tsx` (`applyConversationFilters`, "Show Inference API
   Runs" toggle in the filter menu) with a zap icon (`InferenceConversationIcon`) when shown.
   They cannot be converted to projects (`POST /projects/from-conversation` rejects the origin
   in `chat/project_routes.py`).
2. `run_conversation_turn` picks the `INFERENCE_API_TOOLS` tier (`chat/llm/tool_schemas.py`):
   `BASE_TOOLS` + `return_final_response`, deliberately no `create_action_request` (nobody is
   watching to approve a card) and no `agent_task*` spawners. **Inference runs must not change
   anything outside their own workspace**, so the approval-free mutating tools are refused as
   well. The classification lives on the tool specs: a `TOOL_CALL_REGISTRY` entry (or a
   `PluginTool`) marked `mutating` -- today the four Gmail write tools (`archive_gmail_message`,
   `modify_gmail_labels`, `create_gmail_draft`, `send_gmail_to_self`), the four Quest Docs
   writes (`create_doc`, `edit_doc`, `append_to_doc`, `add_doc_image` -- also refused by the doc
   access rule's read-only `inference_api` run kind, see
   [Quest Docs](../architecture/quest-docs.md)) plus the plugin
   self-send/mailbox tools (`send_slack_dm_to_self`, `twilio_send_self_sms`,
   `m365_create_mail_draft`, `m365_send_mail_to_self`, `m365_archive_mail_message`) -- is
   collected by `mutating_tool_call_tools()`, and `MUTATING_PROXY_PATHS` names the
   state-changing internal routes (`/api/gmail-simple/drafts`, `/api/gmail-simple/send-self`,
   `/api/reset-api-key`). Enforcement is at three chokepoints, none of them prompt trimming:
   `_dispatch_tool_call(..., is_inference_api=True)` rejects a mutating `tool_call` and passes
   `block_mutating=True` to `execute_tool_call` so `curl_proxy_*` cannot reach a mutating path;
   `run_script`/`run_python` mint their sandbox lease with `block_mutating_tools=True`, and the
   sandbox tool API (`get_current_sandbox_user` + the `POST /api/tool-call` bridge via
   `get_sandbox_lease`) refuses the same tools and routes with 403 for that lease; and the
   inference system prompt omits the mutating tools from its Dynamic Tools section and states
   the read-only boundary. Workspace writes (`write_workspace_file`, `edit_workspace_file`,
   sandbox output) stay allowed: the run's conversation owns a fresh workspace. Every core
   dynamic tool must be classified -- `tests/test_inference_api.py` pins the full roster.
3. The system prompt (`get_inference_api_system_prompt` in `chat/gemini_api/system_prompt.py`)
   forbids intermediate assistant text -- the caller only ever receives the markdown passed to
   `return_final_response`. The user's custom system prompt and guides are excluded; user
   auto-load skills are included. `run_inference` drives the turn with a copy of the owner's user
   dict whose `api_key` is blank: an inference token is narrower than the reusable `users.api_key`
   (which authenticates the whole app surface), and everything the model can see -- the proxy
   preamble and the system-skill docs rendered by `load_skills` -- is deliverable to the token
   holder through the final response. In-process tools (`tool_call`, `curl_proxy_*` via route
   dispatch) authenticate from the user dict and are unaffected, and `run_script`/`run_python`
   containers keep the loopback tool-API bridge: their `QUEST_API_KEY` is an ephemeral per-run
   sandbox token minted from the user id (`chat/sandbox_tokens.py`), not the scrubbed field --
   see [Script Runner Architecture](../architecture/script-runner.md#sandbox-tokens).
4. The `return_final_response` dispatch handler in `chat/gemini_api/turn_tools.py` appends the
   closing `tool_result` and ends the run via the `FinishInferenceResponse` sentinel (a successful
   completion, unlike the suspend sentinels); the driver extracts the response text from the
   persisted `tool_use` in `messages_out` (`extract_final_response`).
5. If the loop ends without a return call the driver nudges once (persisted user message), then
   fails with `no_response`. A wall-clock cap (`INFERENCE_TIMEOUT_SECONDS`) cancels hung runs.
6. Transcripts persist incrementally via `make_flush_callback`, so failed or timed-out runs remain
   inspectable in the web UI.
