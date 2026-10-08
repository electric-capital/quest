"""Instruction text generation for the Gmail API (Simple, Raw, and Batch)."""


def _gmail_simple_instructions(base_url: str) -> str:
    """Gmail Simple tools documentation section."""
    return """## Gmail Simple Tools (Recommended for Gmail)

Read and work with Gmail through dedicated dynamic tools invoked via `tool_call`:

| Tool | Description |
|------|-------------|
| `get_gmail_messages` | Fetch 1-50 messages by Gmail message ID, rendered as a markdown document with decoded body and key headers. Options: `include_html`, `include_urls` |
| `list_gmail_labels` | List all labels in simplified form; pass `label_id` to fetch a single label |
| `get_gmail_message_urls` | Look up full URLs for the numeric `(#N#)` identifiers substituted into message bodies |
| `create_gmail_draft` | Create a draft email (does NOT send). Body is markdown (`body_md`, rendered to HTML; plain-text `body` only on explicit request), with reply/forward threading and attachments |
| `send_gmail_to_self` | Send an email to yourself immediately (subject auto-prefixed with [Quest], `body_md` markdown rendered to HTML, same attachments + inline `cid:` images as drafts) |

**Important:**
- These tools do NOT support search queries. To search emails, use the Gmail Raw API via authed_get: `tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=SEARCH_QUERY"})`, then fetch the returned message IDs with `get_gmail_messages`.
- `get_gmail_messages` has a **hard limit of 50 IDs per call**. If you have more than 50 message IDs, split them into multiple calls.
- By default, long URLs in HTML message bodies are replaced with short identifiers like `(#1#)` to save tokens. A `Replaced URL count: N` line appears in the header block when URLs were replaced. Pass `"include_urls": true` to keep the original full URLs inline in the body.
- `get_gmail_messages` returns a **markdown document**, not JSON. The structured values you need for follow-up tool calls -- Gmail message id, thread id, `Message-ID` header, and each `attachmentId` -- appear as labelled lines you can parse directly.

**Example calls:**

```
# Get a message (default: long URLs replaced with identifiers)
tool_call(tool_name="get_gmail_messages", arguments={"message_ids": ["MESSAGE_ID"]})

# Get a message with original full URLs
tool_call(tool_name="get_gmail_messages", arguments={"message_ids": ["MESSAGE_ID"], "include_urls": true})

# Get an HTML message with raw HTML instead of markdown
tool_call(tool_name="get_gmail_messages", arguments={"message_ids": ["MESSAGE_ID"], "include_html": true})

# Fetch multiple messages in one call (max 50)
tool_call(tool_name="get_gmail_messages", arguments={"message_ids": ["ID_1", "ID_2", "ID_3"]})

# List all labels
tool_call(tool_name="list_gmail_labels", arguments={})

# Get a specific label
tool_call(tool_name="list_gmail_labels", arguments={"label_id": "INBOX"})
```

**Message Format (single message):**

`get_gmail_messages` with one id returns a markdown document shaped like this:

```
Subject: <Subject or "(no subject)">
From: Ada <ada@example.com>
To: grace@example.com
Cc: somebody@example.com            (line omitted when empty)
Bcc: hidden@example.com             (line omitted when empty)
Date: Mon, 27 Jan 2025 10:00:00 -0800
Message-ID: <CAA...@mail.gmail.com>
Gmail Message ID: 19500abcdef01234
Thread ID: 19500abcdef01234
Labels: INBOX, UNREAD
Replaced URL count: 2               (present only when URL replacement found >=1 URL)

---

## Body

Hey -- here are the two links we discussed:

- [first link](#1#)
- [second link](#2#)

Best,
Ada

---

## Attachments

- **report.pdf** -- `application/pdf`, 12,345 bytes, attachmentId: `ANGjd123...`
```

Notes on the format:
- `Subject:` is a plain labelled line at the top of the document (not a markdown heading).
- `Message-ID:` is the email `Message-ID` header; use it as `in_reply_to_message_id` when creating reply drafts. `(none)` is emitted when the header is missing.
- `Gmail Message ID:` is the Gmail API id (use it in follow-up tool calls and for URL lookups); `Thread ID:` is the Gmail thread id.
- `Labels: (none)` is emitted when the message has no labels.
- Plain-text-only messages show the body verbatim under `## Body`.
- Messages with no body on either side show `## Body\n\n_(empty body)_`.
- Messages with no attachments show `## Attachments\n\n_No attachments._`.

With `"include_urls": true` the body keeps full URLs inline and the `Replaced URL count:` line is omitted:

```
## Body

Check out [this link](https://very-long-tracking-url...) for details.
```

With `"include_html": true` the body section becomes `## Body (HTML)` containing a fenced `html` code block with the raw HTML:

````
## Body (HTML)

```html
<html><h1>Heading</h1><p>HTML body...</p></html>
```
````

**Batch Message Format:**

With multiple ids, `get_gmail_messages` also returns a single markdown document. It opens with a `# Gmail messages batch` heading and a counts line, then (when any id failed) a `## Batch errors` section listing failures first, then `===`-separated per-message sections rendered the same way as the single-message format. Order matches the order of ids in the request.

```
# Gmail messages batch

Fetched 2 message(s). 1 error(s).

## Batch errors

- **B** -- `not_found`: Message not found

===

<single-message markdown for id A>

===

<single-message markdown for id C>
```

When all ids succeed, the `## Batch errors` section is omitted. When all ids fail, only the header, counts line, and `## Batch errors` section are emitted (no per-message sections, no `===` separators).

### URL Lookup

When message bodies contain `(#N#)` identifiers, use `get_gmail_message_urls` to retrieve the actual URLs. You can fetch multiple identifiers in one call:

```
# Look up specific URLs by identifier
tool_call(tool_name="get_gmail_message_urls", arguments={"message_id": "MESSAGE_ID", "identifiers": [1, 2, 3]})

# Get all URL mappings for a message (omit identifiers)
tool_call(tool_name="get_gmail_message_urls", arguments={"message_id": "MESSAGE_ID"})
```

### Create Draft (create_gmail_draft)

Creates a draft email in the user's Gmail account. Does **not** send the email. Write the body as markdown in `body_md` (rendered to HTML). Use the plain-text `body` field instead only when the user explicitly asks for a plain-text email.

**Arguments:**

| Field | Required | Description |
|-------|----------|-------------|
| `to` | Yes | Recipient email address(es), comma-separated |
| `subject` | Yes | Email subject line |
| `body_md` | Conditional | Markdown-formatted email body, rendered to HTML. **Use this by default**, even for short or simple emails. If `body` is also provided, it is used as the plain text part; otherwise the raw markdown is the plain text fallback |
| `body` | Conditional | Plain text email body. Only use instead of `body_md` when the user explicitly asks for a plain-text email. Required if `body_md` is not provided. When both are provided, `body` is used as the plain text part of the email |
| `cc` | No | CC recipients, comma-separated |
| `bcc` | No | BCC recipients, comma-separated |
| `in_reply_to_message_id` | No | `Message-ID` header of the message being replied to (the `Message-ID:` line, not the Gmail message id) |
| `references` | No | `References` header value for threading |
| `thread_id` | No | Gmail thread ID to attach the draft to an existing thread |
| `attachments` | No | Array of attachment objects (see below) |

**Attachment Object:**

| Field | Required | Description |
|-------|----------|-------------|
| `type` | Yes | `"drive"` for Google Drive files, `"workspace"` for workspace files, `"gmail"` for attachments from an existing Gmail message |
| `drive_file_id` | Conditional | Google Drive file ID (required when type="drive") |
| `workspace_path` | Conditional | Relative path within conversation workspace (required when type="workspace"). Uses same paths as `list_workspace_files` |
| `message_id` | Conditional | Gmail message ID containing the attachment (required when type="gmail") |
| `attachment_id` | Conditional | Gmail attachment ID from the message's attachments list (required when type="gmail") |
| `filename` | No | Override the attachment filename |

**Important attachment notes:**
- **Native Google file types (Docs, Sheets, Slides, Forms, etc.) cannot be attached.** If you need to share a Google Doc/Sheet/Slide, include a link to it in the email body instead (e.g., `https://docs.google.com/document/d/FILE_ID/edit`).
- Total attachment size is limited to 25MB.
- Workspace file paths use the same paths returned by `list_workspace_files` (this conversation's workspace). In a project conversation, project files must be copied into this conversation's workspace first (`copy_project_file`).
- At least one of `body` or `body_md` is required. Default to `body_md` -- it supports rich formatting (headings, bold, lists, tables, code blocks) and renders cleanly in modern mail clients. Reserve `body` for when the user explicitly asks for a plain-text email. If both are provided, `body` is used as the plain text part and `body_md` is rendered to HTML.
- The rendered HTML never references remote resources: `![alt](https://...)` images are replaced by their alt text and raw HTML is reduced to plain formatting tags. Use a regular link (`[caption](https://...)`) when the reader should see an external image.
- **Inline images:** attach the image file and reference it in `body_md` as `![caption](cid:<filename>)`. Every attachment gets a Content-ID equal to its filename (runs of characters outside `A-Za-z0-9._-` become `_`, so `my chart.png` is `cid:my_chart.png`); the draft result lists each attachment's `content_id`. Attached images are marked inline so they render in place.
- **Do not hard-wrap the text** at a fixed column width (in either field). Every newline is rendered as a line break, so wrapped lines show up as ragged, oddly broken paragraphs in modern mail clients. Write each paragraph as one unwrapped line and separate paragraphs with a blank line.

**Example calls:**

```
# Create a simple draft (markdown body, one unwrapped line per paragraph)
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Hello", "body_md": "Hi Sam,\\n\\nThanks for the update -- I've reviewed the numbers and they look good to me.\\n\\nBest,\\nAlex"})

# Create a reply draft (fetch original message first to get Message-ID and Thread ID)
tool_call(tool_name="create_gmail_draft", arguments={"to": "sender@example.com", "subject": "Re: Original Subject", "body_md": "Confirmed for Tuesday, thanks.", "in_reply_to_message_id": "<original-message-id@mail.gmail.com>", "thread_id": "THREAD_ID"})

# Draft with a Google Drive file attachment
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Report", "body_md": "Please see the attached report.", "attachments": [{"type": "drive", "drive_file_id": "DRIVE_FILE_ID"}]})

# Draft with a workspace file attachment
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Analysis", "body_md": "Attached are the results.", "attachments": [{"type": "workspace", "workspace_path": "output/results.csv"}]})

# Draft with an inline image from the workspace (referenced by cid:)
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Weekly chart", "body_md": "Here is this week's trend:\n\n![Weekly trend](cid:chart.png)\n\nDetails below.", "attachments": [{"type": "workspace", "workspace_path": "output/chart.png"}]})

# Draft with rich markdown formatting (headings, lists, bold)
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Project Update", "body_md": "# Project Update\\n\\n- **Backend**: complete\\n- **Frontend**: in progress"})

# Plain-text draft -- ONLY when the user explicitly asked for plain text
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Hello", "body": "Draft body text"})
```

**Creating reply drafts:**
1. Fetch the original message with `get_gmail_messages`
2. Use the `Message-ID:` line from the response as `in_reply_to_message_id`
3. Use the `Thread ID:` line from the response as `thread_id`
4. Prefix the subject with "Re: " if not already present

**Creating forward drafts:**
1. Fetch the original message with `get_gmail_messages`
2. Note the `## Attachments` section in the response (each entry has a filename, mime type, size, and `attachmentId`)
3. Prefix the subject with "Fwd: " if not already present
4. Include the original message body in the new body, quoted with an attribution header:
   ```
   ---------- Forwarded message ---------
   From: <original from>
   Date: <original date>
   Subject: <original subject>
   To: <original to>

   <original body>
   ```
5. For each attachment in the original message, include a `{"type": "gmail", "message_id": "<original_gmail_message_id>", "attachment_id": "<attachmentId>", "filename": "<filename>"}` entry in the `attachments` array
6. Set `thread_id` to the original message's `Thread ID:` so the forward stays in the same thread

```
# Forward a message with its attachments
tool_call(tool_name="create_gmail_draft", arguments={"to": "recipient@example.com", "subject": "Fwd: Original Subject", "body": "FYI see below.\\n\\n---------- Forwarded message ---------\\nFrom: sender@example.com\\nDate: Mon, 27 Jan 2025 10:00:00 -0800\\nSubject: Original Subject\\nTo: me@example.com\\n\\nOriginal message body here.", "thread_id": "THREAD_ID", "attachments": [{"type": "gmail", "message_id": "ORIGINAL_MSG_ID", "attachment_id": "ATT_ID_1", "filename": "report.pdf"}]})
```

### Send Email to Self (send_gmail_to_self)

Sends an email from the user to themselves, immediately (not a draft). Useful for delivering reports, summaries, reminders, charts, or any output the user wants in their inbox. The subject line is automatically prefixed with `[Quest]`. The `body_md` field should be markdown-formatted -- it will be rendered to HTML for a nicely formatted email. Attachments take the same `attachments` list as `create_gmail_draft` (workspace files, Drive files, Gmail attachments; 25MB total) and an attached image can be embedded inline with `![caption](cid:<filename>)`.

**Arguments:**

| Field | Required | Description |
|-------|----------|-------------|
| `subject` | Yes | Email subject (auto-prefixed with [Quest]) |
| `body_md` | Yes | Markdown-formatted email body (rendered to HTML for the email) |
| `attachments` | No | Same list shape as `create_gmail_draft` (`type` = `workspace` / `drive` / `gmail` plus the matching id/path, optional `filename`). Each attachment gets a Content-ID equal to its filename (non `[A-Za-z0-9._-]` runs become `_`), reported back as `attachments[].content_id` |

**Examples:**

```
# Plain report
tool_call(tool_name="send_gmail_to_self", arguments={"subject": "Daily Summary", "body_md": "# Daily Summary\\n\\n- **Meeting at 2pm** with the design team\\n- PR #42 was merged"})

# Workspace file attached
tool_call(tool_name="send_gmail_to_self", arguments={"subject": "Analysis", "body_md": "Results attached.", "attachments": [{"type": "workspace", "workspace_path": "output/results.csv"}]})

# Attached chart shown inline in the body
tool_call(tool_name="send_gmail_to_self", arguments={"subject": "Weekly chart", "body_md": "Here is this week's trend:\n\n![Weekly trend](cid:chart.png)\n\nDetails below.", "attachments": [{"type": "workspace", "workspace_path": "output/chart.png"}]})
```

**Notes:**
- The email is sent immediately (not a draft)
- The `body_md` field is markdown -- use headings, bold, lists, tables, code blocks, etc. for rich formatting in the email
- Remote images (`![alt](https://...)`) are replaced by their alt text and raw HTML is stripped from the rendered email; to show an image, attach it and reference it with `![caption](cid:<filename>)`
- The recipient is always the authenticated user's email address
- Requires Google Services connection via Settings > Data Connections

### Archive Message

To archive a Gmail message (remove from inbox and apply a `[Quest]/archived` label), use the `archive_gmail_message` dynamic tool via `tool_call`:

```
tool_call(tool_name="archive_gmail_message", arguments={"message_id": "MESSAGE_ID"})
```

This removes the INBOX label and applies a `[Quest]/archived` label so the user can find archived messages under the [Quest] label group in Gmail. The message is not deleted.

To also apply Quest-managed labels while archiving (avoiding a separate `modify_gmail_labels` call), pass `add_labels` with short configured names:

```
tool_call(tool_name="archive_gmail_message", arguments={"message_id": "MESSAGE_ID", "add_labels": ["receipts"]})
```

### Quest-Managed Labels

The user can configure a list of label names under Settings > Gmail that you are allowed to add/remove on their emails. Each configured name maps to a nested Gmail label `[Quest]/<name>` (same label group as `[Quest]/archived`). This is separate from `list_gmail_labels`, which lists ALL Gmail labels read-only.

List the labels you can use:

```
tool_call(tool_name="list_gmail_quest_labels", arguments={})
```

Add and/or remove labels on one or many messages in a single call (up to 100 message IDs; use the short configured names, not the `[Quest]/` prefix):

```
tool_call(tool_name="modify_gmail_labels", arguments={"message_ids": ["ID1", "ID2"], "add_labels": ["receipts"], "remove_labels": ["follow-up"]})
```

Labels being added are created in Gmail automatically if missing. This tool does not archive or delete messages -- use `archive_gmail_message` to archive. If a label you need is not configured, tell the user to add it under Settings > Gmail.

### Gmail Simple API from scripts (run_python / run_script)

Code running in the script sandbox cannot use `tool_call`, but the same operations are available as HTTP endpoints on the local API proxy. Authenticate with the injected environment variables `QUEST_PORT` and `QUEST_API_KEY`:

| Method | Endpoint | Equivalent tool |
|--------|----------|-----------------|
| GET | `/api/gmail-simple/messages/{id}` | `get_gmail_messages` (single; query params `include_html`, `include_urls`) |
| GET | `/api/gmail-simple/messages?ids=ID1,ID2,...` | `get_gmail_messages` (batch, max 50) |
| GET | `/api/gmail-simple/labels` | `list_gmail_labels` |
| GET | `/api/gmail-simple/labels/{id}` | `list_gmail_labels` with `label_id` |
| POST | `/api/gmail-simple/drafts` | `create_gmail_draft` (JSON body with the same fields) |
| POST | `/api/gmail-simple/send-self` | `send_gmail_to_self` (JSON body with the same fields) |

```python
import os, urllib.request

req = urllib.request.Request(
    f"http://localhost:{os.environ['QUEST_PORT']}/api/gmail-simple/messages?ids=ID1,ID2",
    headers={"Authorization": f"Bearer {os.environ['QUEST_API_KEY']}"},
)
print(urllib.request.urlopen(req).read().decode())
```

Script-path caveats:
- URL replacement is tied to conversation context, which HTTP calls don't carry: message bodies keep their original full URLs, and the `/api/gmail-simple/urls/...` lookup endpoints won't have mappings for messages fetched from a script.
- Workspace attachments also require conversation context -- send emails or create drafts with workspace attachments via the `send_gmail_to_self` / `create_gmail_draft` tools, not from a script."""


def _gmail_raw_instructions(base_url: str) -> str:
    """Gmail Raw API documentation section (authed_get pattern)."""
    return """## Gmail Raw API (via authed_get)

Access raw Gmail API responses directly using the `authed_get` tool with `https://gmail.googleapis.com/gmail/v1/...` URLs. Authentication is handled automatically.

**Available Endpoints:**

| Gmail API URL | Description | Query Parameters |
|---------------|-------------|-----------------|
| `https://gmail.googleapis.com/gmail/v1/users/me/messages` | List/search messages | `q`, `maxResults`, `pageToken` |
| `https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}` | Get a specific message (raw format) | `format`, `metadataHeaders` |
| `https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/attachments/{aid}` | Get a specific attachment | (none) |
| `https://gmail.googleapis.com/gmail/v1/users/me/threads` | List/search threads | `q`, `maxResults`, `pageToken` |
| `https://gmail.googleapis.com/gmail/v1/users/me/threads/{id}` | Get a specific thread | `format`, `metadataHeaders` |
| `https://gmail.googleapis.com/gmail/v1/users/me/labels` | List all labels (raw format) | (none) |
| `https://gmail.googleapis.com/gmail/v1/users/me/labels/{id}` | Get a specific label (raw format) | (none) |

**Example requests:**

```
# List recent messages (returns just IDs and threadIds)
tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/messages"})

# Search messages
tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=from:someone@example.com"})

# Search unread messages (limit 5)
tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=is:unread&maxResults=5"})

# Get specific message in raw format
tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/messages/MESSAGE_ID"})

# List labels
tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/labels"})
```

**Workflow Recommendation:**
Use the raw API to list/search for message IDs, then fetch the full messages using the simple tools:
1. Search: `tool_call(tool_name="authed_get", arguments={"url": "https://gmail.googleapis.com/gmail/v1/users/me/messages?q=is:unread"})`
2. Fetch details: `tool_call(tool_name="get_gmail_messages", arguments={"message_ids": ["ID1", "ID2"]})` (max 50 IDs per call -- split into multiple calls if needed)"""


def _batch_instructions(base_url: str) -> str:
    """Batch requests documentation section."""
    return f"""## Batch Requests (via curl_proxy_post)

The batch endpoint is a POST endpoint that still uses `curl_proxy_post` through the local proxy (unlike the Gmail Raw GET endpoints which use `authed_get`).

The batch endpoint accepts a multipart/mixed request body for fetching multiple resources in one HTTP request.

**Important:**
- Use full Gmail API paths (e.g., `/gmail/v1/users/me/messages/{{id}}`)
- Keep batches under 25 requests
- Only GET requests to allowed endpoints

Example:
```
curl_proxy_post(url="{base_url}/api/gmail-raw/v1/batch", headers={{"Content-Type": "multipart/mixed; boundary=batch_boundary"}}, body="--batch_boundary\\nContent-Type: application/http\\nContent-ID: <item1>\\n\\nGET /gmail/v1/users/me/messages/MESSAGE_ID_1\\n\\n--batch_boundary\\nContent-Type: application/http\\nContent-ID: <item2>\\n\\nGET /gmail/v1/users/me/messages/MESSAGE_ID_2\\n\\n--batch_boundary--")
```"""


def get_instructions(base_url: str) -> str:
    """Return combined Gmail instruction text (Simple, Raw, and Batch sections)."""
    return "\n\n---\n\n".join([
        _gmail_simple_instructions(base_url),
        _gmail_raw_instructions(base_url),
        _batch_instructions(base_url),
    ])
