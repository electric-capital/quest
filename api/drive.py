"""Google Drive API instructions for the system prompt.

Drive *metadata* read operations are handled via the ``authed_get`` tool,
which makes authenticated GET requests directly to the Google Drive API at
``https://www.googleapis.com/drive/v3/...``.

Binary file-content downloads are handled via the ``download_drive_file``
tool call, which downloads the file to the conversation workspace.  The
LLM can then use ``get_workspace_file`` to read or analyze the downloaded
file.  The ``POST /api/authed-get`` endpoint also supports ``alt=media``
for Python scripts that need direct binary access.
"""

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


# ---------------------------------------------------------------------------
# Instruction text for the Drive API
# ---------------------------------------------------------------------------

def get_instructions(base_url: str) -> str:
    """Drive API documentation section (accessed via authed_get)."""
    return f"""## Google Drive API (via authed_get)

Access the Google Drive API using `authed_get` with the full Google Drive API URL. Authentication is handled automatically.

**Base URL:** `{DRIVE_API_BASE}`

**Key API Paths:**

| Path | Description |
|------|-------------|
| `/drive/v3/files` | List files in the user's Drive |
| `/drive/v3/files/{{fileId}}` | Get a specific file's metadata |
| `/drive/v3/files/{{fileId}}/permissions` | Who has access to a file (one entry per user/group/domain/anyone, with `role`) |
| `/drive/v3/files/{{fileId}}/permissions/{{permissionId}}` | One permission entry |
| `/drive/v3/files/{{fileId}}/revisions` | Version history of a file (who modified it, when, size) |
| `/drive/v3/files/{{fileId}}/revisions/{{revisionId}}` | One revision's metadata |
| `/drive/v3/files/{{fileId}}/comments` | Comment threads on a Doc/Sheet/Slides file (`fields` is REQUIRED) |
| `/drive/v3/files/{{fileId}}/comments/{{commentId}}` | One comment (`fields` is REQUIRED) |
| `/drive/v3/files/{{fileId}}/comments/{{commentId}}/replies` | Replies on a comment (`fields` is REQUIRED) |
| `/drive/v3/changes/startPageToken` | The current change cursor (save it, then poll `/changes` later) |
| `/drive/v3/changes` | Everything that changed since a page token (`pageToken` is REQUIRED) |
| `/drive/v3/about` | Storage quota and the connected Google account (`fields` is REQUIRED) |
| `/drive/v3/drives` | List shared drives |
| `/drive/v3/drives/{{driveId}}` | One shared drive's metadata |

**File List Parameters:**
- `q`: Query string for searching files (see Drive API query syntax below)
- `pageSize`: Maximum number of files per page (1-1000, default 100)
- `pageToken`: Token for pagination
- `orderBy`: Sort order (e.g., 'modifiedTime desc', 'name')
- `fields`: Fields to include in response (e.g., 'files(id,name,mimeType)'). Use to reduce response size and save tokens.
- `corpora`: Which corpora to search ('user', 'drive', 'domain', 'allDrives')
- `driveId`: ID of shared drive to search (requires corpora='drive')
- `includeItemsFromAllDrives`: Include items from shared drives (true/false)
- `supportsAllDrives`: Whether the app supports shared drives (true/false)

**Get File Parameters:**
- `fields`: Fields to include in response
- `supportsAllDrives`: Whether the app supports shared drives (true/false)

**Shared Drives List Parameters:**
- `pageSize`: Maximum number of drives per page (1-100)
- `pageToken`: Token for pagination
- `q`: Query string for filtering drives

**Permissions Parameters:** (`files/{{fileId}}/permissions`)
- `fields`: e.g. `permissions(id,type,role,emailAddress,domain,displayName,deleted)`. `type` is `user`, `group`, `domain` or `anyone`; `role` is `owner`, `organizer`, `fileOrganizer`, `writer`, `commenter` or `reader`. A `type=anyone` entry means the file is link-shared.
- `supportsAllDrives`: Required (`true`) for shared-drive files.
- `pageSize` / `pageToken`: Pagination (default 100).

**Revisions Parameters:** (`files/{{fileId}}/revisions`)
- `fields`: e.g. `revisions(id,modifiedTime,lastModifyingUser(displayName,emailAddress),size,keepForever)`. Revisions come oldest-first; the last entry is the current version.
- `pageSize` / `pageToken`: Pagination (default 200). Revision history exists for Workspace files and uploaded binaries alike; `alt=media` on a revision is blocked here like any other binary download.

**Comments Parameters:** (`files/{{fileId}}/comments`, `.../replies`)
- `fields`: REQUIRED by Google -- the call fails without it. Use `*` for everything or e.g. `comments(id,content,author(displayName,emailAddress),createdTime,resolved,quotedFileContent(value),replies(content,author(displayName),createdTime))`.
- `includeDeleted`: Include deleted comments (default false).
- `startModifiedTime`: Only comments modified after this RFC 3339 timestamp.
- `pageSize` / `pageToken`: Pagination (default 20, max 100).

**Changes Parameters:** (`changes`, `changes/startPageToken`)
- `pageToken`: REQUIRED on `/changes` -- get the first one from `/changes/startPageToken`, then persist the `newStartPageToken` from each `/changes` response (e.g. in a workspace file or the project DB) for the next poll. Tokens are per-user and per-drive.
- `fields`: e.g. `newStartPageToken,nextPageToken,changes(fileId,removed,time,changeType,file(name,mimeType,modifiedTime,lastModifyingUser(displayName),trashed,parents))`.
- `includeRemoved`: Include deletions/permission losses (default true).
- `restrictToMyDrive`: Only My Drive changes, excluding files shared with the user (default false).
- `driveId` + `supportsAllDrives=true` + `includeItemsFromAllDrives=true`: Track a shared drive instead of My Drive.
- `pageSize`: Up to 1000.

**About Parameters:** (`about`)
- `fields`: REQUIRED by Google -- e.g. `user(displayName,emailAddress),storageQuota(limit,usage,usageInDrive,usageInDriveTrash)`. `storageQuota` values are byte counts as strings; `limit` is absent for unlimited accounts.

**Query Syntax Examples:**
The `q` parameter supports powerful search queries:
- `name contains 'report'` - Files with 'report' in the name
- `mimeType = 'application/pdf'` - PDF files only
- `mimeType = 'application/vnd.google-apps.folder'` - Folders only
- `mimeType = 'application/vnd.google-apps.document'` - Google Docs
- `mimeType = 'application/vnd.google-apps.spreadsheet'` - Google Sheets
- `modifiedTime > '2025-01-01T00:00:00'` - Modified after date
- `'FOLDER_ID' in parents` - Files in a specific folder
- `trashed = false` - Exclude trashed files
- Combine with `and`: `name contains 'report' and mimeType = 'application/pdf'`

**Example tool calls:**

```
# List recent files
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files"}})

# List files with specific fields
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?fields=files(id,name,mimeType,modifiedTime)"}})

# Search for files by name
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=name%20contains%20%27report%27"}})

# List PDF files only
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fpdf%27"}})

# List files in a specific folder
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=%27FOLDER_ID%27%20in%20parents"}})

# Get specific file metadata
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files/FILE_ID"}})

# Get file with all fields
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files/FILE_ID?fields=*"}})

# List shared drives
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/drives"}})

# List files including shared drives
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?includeItemsFromAllDrives=true&supportsAllDrives=true"}})

# Who has access to a file
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files/FILE_ID/permissions?fields=permissions(type,role,emailAddress,domain,displayName)&supportsAllDrives=true"}})

# Version history of a file
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files/FILE_ID/revisions?fields=revisions(id,modifiedTime,lastModifyingUser(displayName),size)"}})

# Comment threads on a Doc (fields is required)
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files/FILE_ID/comments?fields=comments(id,content,author(displayName),createdTime,resolved,quotedFileContent(value),replies(content,author(displayName),createdTime))"}})

# What changed since the last poll: first get a cursor ...
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/changes/startPageToken"}})
# ... then later list changes from it (persist newStartPageToken for the next poll)
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/changes?pageToken=SAVED_TOKEN&fields=newStartPageToken,nextPageToken,changes(fileId,removed,time,file(name,mimeType,modifiedTime,lastModifyingUser(displayName),trashed))"}})

# Storage quota and connected account (fields is required)
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/about?fields=user(displayName,emailAddress),storageQuota(limit,usage,usageInDrive,usageInDriveTrash)"}})

# One shared drive
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/drives/DRIVE_ID"}})
```

**Downloading file content (binary):**

To download the actual binary content of a Drive file (PDF, image, spreadsheet, etc.), use the `download_drive_file` tool call. It downloads the file to the conversation workspace so you can then read it with `get_workspace_file`.

```
# Download a file by its Drive file ID
tool_call(tool_name="download_drive_file", arguments={{"file_id": "YOUR_FILE_ID"}})

# Optionally specify a filename
tool_call(tool_name="download_drive_file", arguments={{"file_id": "YOUR_FILE_ID", "filename": "report.pdf"}})

# Then read the downloaded file
tool_call(tool_name="get_workspace_file", arguments={{"path": "report.pdf"}})
```

**Important Notes:**
- File IDs can be found in Google Drive URLs or from the list endpoint
- Use URL encoding for query strings (spaces become %20, quotes become %27)
- To access shared drive files, use `includeItemsFromAllDrives=true` and `supportsAllDrives=true`
- Use the `fields` parameter to request only the data you need, which reduces response size and saves tokens. Example: `fields=files(id,name,mimeType)` for file listings.
- For file **metadata** (name, size, mimeType, etc.), use `authed_get`. For file **content** (the actual bytes), use `download_drive_file` as shown above.
- **Converting a Drive document (e.g. a `.docx` to PDF):** `download_drive_file` it, then convert with headless LibreOffice in the sandbox -- `soffice --headless --convert-to pdf --outdir /workspace report.docx` via `run_python` (see `system:workspace`) -- never with a PDF built from `python-docx` output.
- **Native Google Workspace files have no bytes to download.** `download_drive_file` fails on them; use the export tools instead, which convert through Drive and write the result to the workspace: `google_export_doc` (`document_id`; pdf, docx, odt, rtf, txt, md, html, epub, zip), `google_export_sheet` (`spreadsheet_id`; xlsx, ods, pdf, csv, tsv, zip -- csv/tsv are the first tab only), `google_export_slides` (`presentation_id`; pptx, odp, pdf, txt, png, jpeg, svg -- the images are the first slide only). Load `system:docs` / `system:sheets` / `system:slides` for details. A `files/{{id}}` metadata read tells you the kind: `mimeType` `application/vnd.google-apps.document` / `.spreadsheet` / `.presentation`.
- A Drive **shortcut** (`mimeType = application/vnd.google-apps.shortcut`) has no content of its own; read `shortcutDetails.targetId` from its metadata and use that id instead.
- The `comments` and `about` endpoints REQUIRE a `fields` parameter and `/changes` REQUIRES `pageToken`; Google rejects the call without them.

---

## Google Drive Write Operations (via create_action_request)

Uploading a workspace file to the user's Google Drive or creating a
Drive folder goes through `create_action_request` so the user can
approve before anything is written to their Drive. Never POST to the
Drive upload or files API directly.
`create_action_request` is top-level only -- if you are running as a
sub-agent, do not call it; return the proposed `request_type` and
`params` to the parent via `agent_task_response` instead.

### `upload_to_drive` — upload workspace files to Google Drive

Uploads one or more files from the conversation workspace to Drive
**as-is** (the raw bytes are stored; no conversion to a Google Doc). The
files must already exist in the workspace -- write them with
`write_workspace_file` or download them first (e.g. `download_drive_file`,
a script, etc.).

**Batch your uploads.** Every `create_action_request` call is a round
trip for the user (they must click Approve on each card). When several
files go to the same destination, put them ALL in one request's `files`
list -- never propose one request per file. Use `new_folder_name` to
create the destination folder in the same request instead of a separate
`create_drive_folder` request first. Only split into multiple requests
when you must: more than 10 files (chunk them), or different destination
folders (one request per destination).

Parameters:
- `files` (list, required): 1-10 entries of `{{"path": ..., "filename":
  ...}}`. All files in one request land in the same destination folder.
  Per entry:
  - `path` (string, required): Workspace-relative path of the file to
    upload (e.g. `report.pdf` or `out/summary.csv`). No absolute paths,
    no `..` traversal, no duplicate paths within one request.
  - `filename` (string, optional): Name to give the file in Drive.
    Defaults to the basename of `path`.
- `folder_id` (string, optional): Target Drive folder id. When omitted,
  the files land in Drive root ("My Drive"). To target a specific folder,
  first look up its id via `authed_get` on
  `{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.folder%27`
  (URL-encoded `mimeType = 'application/vnd.google-apps.folder'`). Shared
  drive folders are supported. Mutually exclusive with `new_folder_name`.
- `new_folder_name` (string, optional): Create a new Drive folder with
  this name when the user approves and upload the file(s) into it.
  Mutually exclusive with `folder_id`.
- `new_folder_parent_id` (string, optional): Parent folder id for the new
  folder (shared drives supported). Only valid together with
  `new_folder_name`; when omitted the new folder lands in Drive root.

Example params (several files into an existing folder -- ONE request):
```json
{{
  "files": [
    {{"path": "reports/q1-summary.pdf", "filename": "Q1 Summary.pdf"}},
    {{"path": "reports/q1-data.csv"}},
    {{"path": "charts/q1-revenue.png"}}
  ],
  "folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"
}}
```

Example params (create the destination folder on the fly, same request):
```json
{{
  "files": [
    {{"path": "reports/q1-summary.pdf"}},
    {{"path": "reports/q1-data.csv"}}
  ],
  "new_folder_name": "Q1 Reports",
  "new_folder_parent_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"
}}
```

The result carries `count` and a `files` list of `{{file_id, name, url}}`
per uploaded file. When the request created a new folder, the result also
includes `folder_id`, `folder_name`, and `folder_url` -- use that
`folder_id` for any follow-up upload batches into the same folder.

Best practices:
- Make sure every file exists in the workspace before proposing the
  upload; a single missing or oversize file fails the whole batch up
  front (nothing is uploaded).
- Files are capped at 50 MB each, 10 files per request.
- Uploaded files are stored verbatim (not converted to Google Docs).
  To save markdown as a native Google Doc, use the file browser's
  "Save to Drive" feature instead.

### `create_drive_folder` — create a folder in Google Drive

Creates a folder in the user's Drive (shared drives supported). You
rarely need this before an upload: `upload_to_drive` can create the
destination folder itself via `new_folder_name` in the same approval,
which saves the user a round trip. Reserve `create_drive_folder` for
creating a folder with no immediate uploads (or an empty folder
structure).

Parameters:
- `name` (string, required): Name of the folder to create.
- `parent_folder_id` (string, optional): Parent Drive folder id. When
  omitted, the folder is created in Drive root ("My Drive"). Shared
  drive parents are supported.

Example params:
```json
{{
  "name": "Q1 Reports",
  "parent_folder_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz"
}}
```

The result includes the new `folder_id` and a
`https://drive.google.com/drive/folders/<id>` URL; use the `folder_id`
for follow-up `upload_to_drive` requests.

Best practices:
- Drive allows multiple sibling folders with the same name. Search for an
  existing folder first via `authed_get` with
  `q=mimeType = 'application/vnd.google-apps.folder' and name = '...'`
  (URL-encoded) to avoid creating duplicates."""
