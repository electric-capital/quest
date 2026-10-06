"""Google Slides API instructions for the system prompt.

Slides read operations are handled via the ``authed_get`` tool, which
makes authenticated GET requests directly to the Google Slides API at
``https://slides.googleapis.com/v1/...``.

Listing Google Slides presentations uses the Google Drive API
(``https://www.googleapis.com/drive/v3/files``) with a mimeType filter,
which is also accessed via ``authed_get`` (Drive is already registered).

Exporting a deck to the workspace as a file (pptx / odp / pdf / txt /
png / jpeg / svg) is the ``google_export_slides`` tool call
(``_handle_google_export_slides`` in
``chat/gemini_api/tool_handlers/drive.py``), which drives the Drive
``files.export`` endpoint.
"""

SLIDES_API_BASE = "https://slides.googleapis.com/v1"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


# ---------------------------------------------------------------------------
# Instruction text for the Slides API
# ---------------------------------------------------------------------------

def get_instructions(base_url: str) -> str:
    """Slides API documentation section (accessed via authed_get)."""
    return f"""## Google Slides API (via authed_get)

Access the Google Slides API using `authed_get` with the full Google Slides API URL. Authentication is handled automatically. Read-only: only the GET endpoints below are reachable.

**Base URL:** `{SLIDES_API_BASE}`

**Key API Paths:**

| Path | Description |
|------|-------------|
| `/v1/presentations/{{presentationId}}` | Get a presentation by ID (full deck: slides, masters, layouts, page elements) |
| `/v1/presentations/{{presentationId}}/pages/{{pageObjectId}}` | Get a single page (slide/master/layout) by its `objectId` |

**Get Presentation Parameters:**
- `fields`: Fields to include in response (e.g., 'title,slides.objectId'). Full presentations are large and will commonly exceed the response size limit, so use `fields` to trim the payload and save tokens. For very large decks, pass `output_file` to stream the response to the workspace instead.

**Listing Google Slides (via Google Drive API):**

To list or search for Google Slides presentations, use the Google Drive API with a mimeType filter. The Drive API base URL is `{DRIVE_API_BASE}`.

| Path | Description |
|------|-------------|
| `/drive/v3/files` | List/search files (use `q` parameter to filter by mimeType) |

**List Parameters:**
- `q`: Query string (must include `mimeType='application/vnd.google-apps.presentation'` to filter for Slides)
- `pageSize`: Maximum number of files per page (1-1000, default 100)
- `pageToken`: Token for pagination
- `orderBy`: Sort order (e.g., 'modifiedTime desc', 'name')
- `fields`: Fields to include in response (e.g., 'files(id,name,modifiedTime)'). Use to reduce response size and save tokens.

**Example tool calls:**

```
# Get a presentation by ID
tool_call(tool_name="authed_get", arguments={{"url": "{SLIDES_API_BASE}/presentations/PRESENTATION_ID"}})

# Get a presentation trimmed with fields (recommended to avoid the size limit)
tool_call(tool_name="authed_get", arguments={{"url": "{SLIDES_API_BASE}/presentations/PRESENTATION_ID?fields=title,slides.objectId,slides.pageElements"}})

# Get a single page (slide) by its object id
tool_call(tool_name="authed_get", arguments={{"url": "{SLIDES_API_BASE}/presentations/PRESENTATION_ID/pages/PAGE_OBJECT_ID"}})

# List all Google Slides presentations (via Drive API)
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.presentation%27"}})

# List Google Slides presentations with name search
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.presentation%27%20and%20name%20contains%20%27deck%27"}})

# List recent Google Slides presentations
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.presentation%27&orderBy=modifiedTime%20desc&pageSize=10"}})
```

**Exporting a presentation to the workspace (via google_export_slides):**

Google Slides have no raw bytes, so `download_drive_file` cannot fetch them. To get a deck as a file -- for the user to keep, to upload elsewhere, to convert, or simply to read its text without paging through the Slides API JSON -- use the `google_export_slides` tool call. It converts the deck through the Drive `files.export` endpoint, writes the result to the conversation workspace, and returns the filename.

| `format` | Export MIME type | Notes |
|----------|------------------|-------|
| `pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | PowerPoint deck; read with `python-pptx` in `run_python` |
| `odp` | `application/vnd.oasis.opendocument.presentation` | OpenDocument presentation |
| `pdf` | `application/pdf` | Every slide |
| `txt` | `text/plain` | Plain text of every slide -- the cheapest way to read a deck |
| `png` | `image/png` | **First slide only** |
| `jpeg` | `image/jpeg` | **First slide only** |
| `svg` | `image/svg+xml` | **First slide only** |

```
# Read a deck's text cheaply
tool_call(tool_name="google_export_slides", arguments={{"presentation_id": "PRESENTATION_ID", "format": "txt"}})

# Export as PowerPoint (e.g. to upload elsewhere or edit with python-pptx)
tool_call(tool_name="google_export_slides", arguments={{"presentation_id": "PRESENTATION_ID", "format": "pptx"}})

# Export as PDF under a chosen filename
tool_call(tool_name="google_export_slides", arguments={{"presentation_id": "PRESENTATION_ID", "format": "pdf", "filename": "q3-review.pdf"}})
```

The result carries `filename`, `size_bytes`, `format`, `export_mime_type`, and `document_title`. When `filename` is omitted the file is named `<presentation title>.<ext>`.

- Prefer `txt` when YOU need to read the content; use the Slides API above when you need structure (object ids, layouts, element positions) -- e.g. to describe a particular slide.
- The image formats render only the first slide (Drive's export takes no page selector). For images of every slide, export `pdf` and rasterize pages in `run_python` (`pypdfium2` / LibreOffice `--convert-to png`).
- Only native Google Slides (`mimeType = application/vnd.google-apps.presentation`) are supported. Uploaded `.pptx` files stored in Drive go through `download_drive_file`; Docs and Sheets have their own export tools (`google_export_doc`, `google_export_sheet`).
- Google caps exports at 10 MB of exported content; a heavy deck may need `txt` or `pdf` instead of `pptx`.
- Do NOT call `/drive/v3/files/{{id}}/export` through `authed_get` -- the tool call path rejects it and points you back here.

**Important Notes:**
- Presentation IDs can be found in Google Slides URLs (e.g., `https://docs.google.com/presentation/d/PRESENTATION_ID/edit`).
- `pageObjectId` is the `objectId` of a slide/master/layout found in the presentation's `slides[]`, `masters[]`, or `layouts[]`.
- The Slides API has no list endpoint; to list presentations you MUST use the Drive API with `mimeType='application/vnd.google-apps.presentation'` in the `q` parameter. Use URL encoding for query strings (spaces become %20, quotes become %27).
- Use the `fields` parameter to request only the data you need, which reduces response size and saves tokens. A full untrimmed presentation will often exceed the response size limit."""
