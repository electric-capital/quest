# Microsoft 365 Mail API (Outlook)

## Overview

Outlook (Microsoft 365 / Office 365) email access over Microsoft Graph, packaged as the in-tree `plugins/m365` plugin (plugin id `m365`; see [Plugins](../../../docs/architecture/plugins.md)). Mirrors the Gmail integration's capability set ([gmail-api.md](../../../docs/api/gmail-api.md)): dedicated markdown-rendering read tools, draft creation, send-to-self, archive, attachment download, and raw read-only API access via `authed_get`. Per-user OAuth with expiring tokens (proactive refresh + `retry_on_401`); setup in [Office 365 App Setup](office365-setup.md).

Mail only for now — the Graph allow-list and tool surface deliberately cover just `/me/messages` and `/me/mailFolders`; calendar/OneDrive/etc. would extend the same plugin.

## Dedicated Tools (`plugins/m365/tools.py`)

All are `tool_call` tools gated on `connected_services["m365"]` (server configured AND user connected); handlers return structured `{"error": ...}` JSON instead of raising.

| Tool | Description |
|------|-------------|
| `m365_get_mail_messages` | Fetch 1-50 messages by Graph message ID (concurrency-capped at 4 — Graph's per-mailbox concurrent limit), rendered as a markdown document: labelled header block (`Graph Message ID`, `Conversation ID`, `Message-ID` header, categories, read state), HTML body converted via the shared markdownify pipeline with long-URL `(#N#)` replacement cached per conversation (`api/gmail/helpers.py` URL cache, keyed by Graph id), and an attachments section with each `attachmentId`. Batch output is a `# Outlook messages batch` document, errors-first. Options: `include_html`, `include_urls` |
| `m365_list_mail_folders` | Top-level mail folders (id, name, unread/total counts), or one folder + child folders via `folder_id` (well-known names like `inbox`, `archive`, `sentitems` accepted) |
| `m365_get_mail_message_urls` | Resolve `(#N#)` identifiers from message bodies against the per-conversation URL-mapping cache |
| `m365_create_mail_draft` | Create (never send) a draft: new (`to` + `subject` required), reply (`reply_to_message_id`, Graph id — threading via Graph `createReply`), or forward (`forward_of_message_id` — original attachments copied by `createForward`); the composed body **replaces** the auto-quoted original. `body` plain text or `body_md` markdown→HTML (single-part Graph body, `body_md` wins; rendered through the shared `convert_markdown_to_html()` in `api/gmail/helpers.py`, whose `sanitize_email_html()` pass removes every remote reference -- `<img>` only with a `cid:` source, remote images degrade to alt text, no `style`/`<style>`/`<script>` -- see the Gmail API doc's "Outgoing HTML Sanitization"). Attachments from the conversation workspace (path-traversal-guarded) or other Outlook messages (`fileAttachment` only), each sent with a filename-derived Graph `contentId` (shared `attachment_content_id()` from `api/gmail/helpers.py`) and `isInline: true` for image types, reported back as `attachments[].content_id`, so `body_md` can embed an attached image as `![caption](cid:<filename>)`; ≤3MB attach via simple POST, larger via Graph upload session in 320KiB-multiple chunks against the pre-authorized `uploadUrl` (no bearer leak); 25MB total |
| `m365_send_mail_to_self` | Immediately email the connected mailbox itself (`[Quest] ` subject prefix, `body_md` → HTML through the same sanitized renderer as the draft tool -- no remote images, since this tool needs no approval). Recipient is the account captured at OAuth time (`oauth_blob.account.email`, falling back to a live `/me` lookup) — NOT the Quest login email, which can differ |
| `m365_archive_mail_message` | Apply the `Quest archived` category (the `[Quest]/archived` Gmail-label analog), then `move` to the Archive well-known folder. Moving changes the Graph message id; the result carries the new id |
| `m365_save_mail_attachment` | Download a file attachment into the conversation workspace (default `outlook-attachments/`, `path` override with github_get_job_log semantics); metadata via `$select`, bytes via the attachment `$value` endpoint; 50MB cap; `itemAttachment`/`referenceAttachment` rejected |

Script bridge (`POST /api/tool-call`): `m365_get_mail_messages` and `m365_list_mail_folders` only. Script calls carry no conversation context, so URL replacement degrades to full inline URLs (same caveat as the Gmail Simple script path).

## Raw API (via authed_get)

The `graph.microsoft.com` service entry (registered by the plugin) is the search/list surface — the dedicated tools deliberately have no query support, matching Gmail where search rides on the raw API. Read-only by construction: GET-only allow-list, no `allowed_post_endpoints`.

| Graph URL (`https://graph.microsoft.com`) | Description |
|---|---|
| `/v1.0/me` | Connected mailbox identity |
| `/v1.0/me/messages` | List/search messages (`$search` KQL, `$filter`, `$top`, `$select`, `@odata.nextLink` paging) |
| `/v1.0/me/messages/{id}` | Single message (raw JSON) |
| `/v1.0/me/messages/{id}/attachments`, `.../attachments/{id}` | Attachment metadata / single attachment |
| `/v1.0/me/mailFolders`, `.../{id}`, `.../{id}/messages`, `.../{id}/childFolders` | Folder browsing and per-folder listing |

Credential loader `load_m365_credentials` returns a proactively-refreshed token (`get_m365_token`); `retry_on_401` backstops; missing credentials surface `m365_oauth_required`.

## Auth & Tokens

- Entra ID authorization-code flow with `offline_access User.Read Mail.ReadWrite Mail.Send`; router in `plugins/m365/oauth.py` (`/auth/m365?popup=1` connect convention, CSRF state cookie, shared popup pages).
- Token JSON in `user_service_credentials` `oauth_blob`: access/refresh tokens, `expires_at`, granted `scope`/`scopes`, `account` (mailbox id/name/email captured from `/me` at callback), `authorized_at`.
- `get_m365_token()` refreshes within 5 minutes of expiry under a per-user asyncio lock (Microsoft rotates refresh tokens; the rotated token is persisted, a refresh response without one keeps the prior). Failed refresh ⇒ `None` ⇒ the standard reconnect error.
- `needs_reauth` hook: granted scopes (normalized — full `https://graph.microsoft.com/...` URIs and short names both accepted, `offline_access` excluded) no longer covering `M365_SCOPES` shows the "Update Available" badge.

## Skill

`system:m365` (`plugins/m365/instructions.md`), gated on `m365`: tool reference, message/batch format, Graph search syntax and caveats (`$search` vs `$filter` exclusivity, `@odata.nextLink` paging, ids changing on folder moves), reply/forward body-replacement warning, script-bridge usage.

## Key Files

- `plugins/m365/manifest.py` — manifest: credential schema (tenant/client id/secret), oauth user connection, Graph service entry, skill, tools
- `plugins/m365/upstream.py` — scopes, config loader, token refresh, `graph_request` (401-retry), hooks
- `plugins/m365/oauth.py` — OAuth router
- `plugins/m365/tools.py` — tool handlers + specs
- `plugins/m365/render.py` — markdown rendering (shares `api/gmail/helpers.py` pure conversion helpers and URL cache)
- `plugins/m365/tests/test_m365_plugin_upstream.py`, `plugins/m365/tests/test_m365_render.py` — unit tests

## Gmail-Parity Gaps (deliberate)

- No Quest-managed label tools (`list_gmail_quest_labels`/`modify_gmail_labels`): those hang off the per-user `gmail_labels` setting and its Settings > Gmail UI; an Outlook-categories equivalent would need a settings surface plugins don't have. Archive does apply the fixed `Quest archived` category.
- No Drive-type draft attachments (Google-specific); conversation-workspace and Outlook-message attachments are supported (project files must be copied in first with `copy_file`).
- No batch HTTP endpoint (`$batch` not allow-listed); the dedicated tool batches with bounded concurrency instead.
