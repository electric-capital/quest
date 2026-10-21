# Service Connect Requests

## Overview

A connect request is the inline "Connect Google Services" card the model opens when a user asks for something that needs a service they have not connected yet, but which the admin has configured. The user asks for an email search while Google Services is unconnected; instead of pointing at Settings, the model calls the `request_service_connection` tool, the card appears in the conversation with the model's one-line reason and **Connect** / **Not now** buttons, and the turn suspends. Connect runs the same flow Settings > Data Connections would run (the OAuth popup, or the Settings add flow for key and form services); once the server verifies the credential the model resumes with `status: "connected"` and carries on with the original request. Not now resumes it with `status: "declined"`.

The mechanism is a fourth [wait handle](wait-handles.md) kind, `service_connection`, with its own card message, resolve endpoint and resume-bucket arm. It never touches the `action_requests` table: a connect request is not a proposed write, so it has no Requests-pane row, no Slack pending-request notification and no Approve / Revise semantics.

## Key Files

| File | Description |
|------|-------------|
| `chat/connect_requests.py` | Names (`TOOL_NAME`, `MESSAGE_TYPE`, `HANDLE_KIND`), row resolution (`connectable_rows`, `find_row`, `find_connect_target`, `row_is_connected`), `card_fields`, `build_connectable_services_paragraph` |
| `chat/routes/user.py` | `build_connector_rows(user)`: the Settings row list, shared with `GET /connectors` |
| `chat/llm/tool_schemas.py` | The `request_service_connection` registry spec (core tool, rides on `tool_call`) |
| `chat/gemini_api/turn_tools.py` | `REQUEST_SERVICE_CONNECTION_KEY`, `SuspendForServiceConnection`, `_handle_request_service_connection` |
| `chat/gemini_api/conversation.py` | Per-call catch in the dispatch batch, top-level catch, the `service_connection` resume-bucket arm, the prompt paragraph gate |
| `chat/connect_request_routes.py` | `POST /app/api/connect-requests/{handle_id}/resolve` |
| `chat/storage.py` | `update_connect_request_message` (card rewrite on resolve) |
| `chat/gemini_api/system_prompt.py` | `connectable_services` argument of `get_system_prompt`; the tool is excluded from every other prompt |
| `chat/system_skills/loader.py` | The not-connected skill error names the tool |
| `frontend/src/components/ConnectRequestMessage.tsx` | The card |
| `frontend/src/api/client.ts` | `resolveConnectRequest` |
| `tests/test_connect_requests.py`, `frontend/src/components/ConnectRequestMessage.test.tsx` | Tests |

## What the Model Sees

`build_connectable_services_paragraph(user)` renders a **Connectable services** paragraph after the Connected accounts paragraph of the top-level prompt. It lists every available connector row that is not connected, or is connected but `needs_reauth`, as `Label (\`id\`: description)`, and tells the model to call the tool rather than send the user to Settings. The paragraph is empty, and the prompt unchanged, when nothing is connectable. `run_conversation_turn` builds it only for a web, non-routine, non-public top-level run; the sub-agent, cross-user subagent, inference and Slack prompts never list the tool, and `tool_dispatch.py` answers a sub-agent call with an error.

The rows are `build_connector_rows(user)`, the list `GET /connectors` returns, so the paragraph, the tool and the Settings picker always agree on what an admin has configured: a row with `available: false` (no server-side credentials) is invisible to the model too.

A `load_skills` call for a skill whose `requires` service is unconnected returns an error that spells out the tool call with the right service id, so a model that reaches for the skill first still finds the card.

## Tool and Card

`request_service_connection(service, reason)` is a core `tool_call` tool. `_handle_request_service_connection` refuses Slack-driven, cross-user subagent, inference and routine runs with a structured error before anything is written (a scheduled routine run would sit suspended until someone opened the conversation), and the public-project blocklist rejects it as defense in depth. `find_connect_target` then answers `missing_service`, `unknown_service` (with the current `connectable_services` ids) or `already_connected` without a card.

On success the arm inserts a `pending` wait handle of kind `service_connection` whose `tool_id` is the provider-native tool_use id and whose `payload` is `card_fields(row, reason)`: `service`, `label`, `description`, `kind`, `needs_reauth`, the clipped `reason` (`MAX_REASON_LEN`) and, for the oauth kind only, the row's `connect_url`. Never a key hint or key route. It appends the durable `connect_request` message (the same fields plus `wait_handle_id` and `status: "open"`) through `_emit_durable`, so the card is on disk before the sentinel, and raises `SuspendForServiceConnection`. The dispatch loop records it per call exactly like `SuspendForActionRequest`, so the other calls of a parallel batch still run, and the resume gate in `chat/wait_handles/resume.py` counts `service_connection` among the one-row-per-tool_use kinds.

`connect_request` is in `FLUSH_EVENT_TYPES` and in the tail route's rewritten-in-place set, so the card flushes like an action-request card and a tail that holds one is read from disk after a resolve.

## Resolution

The card never asserts that a service is connected. `POST /app/api/connect-requests/{handle_id}/resolve` takes `{"action": "check"}` or `{"action": "decline"}` and answers 404 (`connect_request_not_found`) for another user's row or a handle of another kind, 400 (`already_resolved`, with the row's `status` and `response`) once terminal, and 409 on a lost race.

- **check** re-reads the user from the DB and looks the service up in `build_connector_rows` again. A row that is connected and does not need re-authorization resolves the handle `accepted` with `{"status": "connected", "service", "label", "note"}`, drops the user's cached sessions (so the resume rebuilds the prompt with the service's tools and skill), rewrites the card to `connected`, publishes `wait_handle_resolved` and kicks the headless resume. Otherwise the handle stays `pending` and the response is `{"resolved": false, "status": "not_connected"}`, so the card can offer a retry.
- **decline** resolves the handle `rejected` with `{"status": "declined", ...}`, rewrites the card, publishes and kicks.

The resume bucket closes the dangling tool_use with the row's `response` verbatim. A row without a response (cancelled by the WS stop handler, or timed out) reads as `{"status": "cancelled", "service", "note"}` so the model treats the service as not connected; a still-pending row reads as `still_waiting`.

## Frontend

`ConnectRequestMessage.tsx` renders the open card with the brand icon, `Connect <label>` (or `Re-authorize <label>` when `needs_reauth`), the reason, the description and the buttons. **Connect** opens the popup at `connect_url` with `openOAuthPopup` and listens for the callback's `oauth_callback_success` / `oauth_callback_error` message, polling `popup.closed` as the fallback ([OAuth Popup Flow](oauth-popup.md)); either way it then calls `check`. A blocked popup shows the direct link. For the `api_key` and `form` kinds the button reads **Open Settings**: it opens the Settings modal (the Data Connections add flow) and the card checks when the modal closes. A `not_connected` answer keeps the card open with a notice and a **Check again** button. **Not now** declines. A connected outcome also calls `refreshConnectionStatus` so the app-level flags update.

A resolved card collapses to `Connected: <label>` / `Not now: <label>` / `Cancelled: <label>`. The card re-reads its wait handle on mount and subscribes to `wait_handle_resolved` by `handle_id`, so a resolve from another tab or a stop flips it in place. `useConversation` derives a pending wait of kind `service_connection` from any open `connect_request` row, which locks the composer with the placeholder "Connect the service above, or choose Not now, to continue."

## Design Decisions

**Why a blocking tool rather than a fire-and-forget card?** The model needs the outcome to act on it: after a connect it should load the skill and finish the request, after a decline it should move on and not ask again. Blocking on a wait handle hands it the verdict as the tool result, the same contract `create_action_request` uses, and keeps the suspend restart-safe.

**Why a new wait-handle kind instead of an action request type?** An action request is a proposed write with Approve / Revise / Stop, a Requests-pane row and Slack notifications. A connect card has none of those, and its approval is an OAuth round trip the server must verify, so reusing the type would mean carving exceptions into every action-request surface.

**Why does the server verify instead of trusting the popup's success message?** The `postMessage` is a same-origin cue, not evidence; a closed popup tells the card nothing. Re-reading the credential row server-side is the only way to be sure, and it costs one query.

**Why list connectable services in the prompt?** Without it the model only learns a service is missing when a tool errors, and the Settings gating that hides unconnected skills and tools would leave it believing the service does not exist. The paragraph is the connectable complement of the Connected accounts paragraph and reuses the Settings rows, so it is empty whenever Settings would show nothing to add.
