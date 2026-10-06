"""Google Sheets API instructions for the system prompt.

Sheets read operations are handled via the ``authed_get`` tool, which
makes authenticated GET requests directly to the Google Sheets API at
``https://sheets.googleapis.com/v4/...``.

Listing Google Spreadsheets uses the Google Drive API
(``https://www.googleapis.com/drive/v3/files``) with a mimeType filter,
which is also accessed via ``authed_get`` (Drive is already registered).

Exporting a whole spreadsheet to the workspace as a file (xlsx / ods /
pdf / csv / tsv / zip) is the ``google_export_sheet`` tool call
(``_handle_google_export_sheet`` in
``chat/gemini_api/tool_handlers/drive.py``), which drives the Drive
``files.export`` endpoint.
"""

SHEETS_API_BASE = "https://sheets.googleapis.com/v4"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


# ---------------------------------------------------------------------------
# Instruction text for the Sheets API
# ---------------------------------------------------------------------------

def get_instructions(base_url: str) -> str:
    """Sheets API documentation section (accessed via authed_get)."""
    return f"""## Google Sheets API (via authed_get)

Access the Google Sheets API using `authed_get` with the full Google Sheets API URL. Authentication is handled automatically.

**Base URL:** `{SHEETS_API_BASE}`

**Key API Paths:**

| Path | Description |
|------|-------------|
| `/v4/spreadsheets/{{spreadsheetId}}` | Get a Google Spreadsheet by ID (metadata and content) |
| `/v4/spreadsheets/{{spreadsheetId}}/values/{{range}}` | Get values from a specific range |
| `/v4/spreadsheets/{{spreadsheetId}}/values:batchGet` | Batch get values from multiple ranges |

**Get Spreadsheet Parameters:**
- `includeGridData`: Include grid data (cell values) in response (boolean, e.g., `true`)
- `ranges`: Specific ranges to include (can be repeated, e.g., `ranges=Sheet1!A1:B5&ranges=Sheet2!C1:D5`)
- `fields`: Fields to include in response (e.g., 'sheets.properties'). Use to reduce response size and save tokens.

**Get Values Parameters:**
- `majorDimension`: Major dimension: ROWS or COLUMNS
- `valueRenderOption`: Value render option: FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA
- `dateTimeRenderOption`: DateTime render option: SERIAL_NUMBER or FORMATTED_STRING

**Batch Get Values Parameters:**
- `ranges`: Ranges to retrieve (required, can be repeated, e.g., `ranges=Sheet1!A1:B5&ranges=Sheet2!C1:D5`)
- `majorDimension`: Major dimension: ROWS or COLUMNS
- `valueRenderOption`: Value render option: FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA
- `dateTimeRenderOption`: DateTime render option: SERIAL_NUMBER or FORMATTED_STRING

**Listing Google Spreadsheets (via Google Drive API):**

To list or search for Google Spreadsheets, use the Google Drive API with a mimeType filter. The Drive API base URL is `{DRIVE_API_BASE}`.

| Path | Description |
|------|-------------|
| `/drive/v3/files` | List/search files (use `q` parameter to filter by mimeType) |

**List Parameters:**
- `q`: Query string (must include `mimeType='application/vnd.google-apps.spreadsheet'` to filter for Sheets)
- `pageSize`: Maximum number of files per page (1-1000, default 100)
- `pageToken`: Token for pagination
- `orderBy`: Sort order (e.g., 'modifiedTime desc', 'name')
- `fields`: Fields to include in response (e.g., 'files(id,name,modifiedTime)'). Use to reduce response size and save tokens.

**Example tool calls:**

```
# Get a spreadsheet by ID (metadata)
tool_call(tool_name="authed_get", arguments={{"url": "{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID"}})

# Get a spreadsheet with grid data (cell values)
tool_call(tool_name="authed_get", arguments={{"url": "{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID?includeGridData=true"}})

# Get a spreadsheet with specific ranges only
tool_call(tool_name="authed_get", arguments={{"url": "{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID?ranges=Sheet1!A1:D10&includeGridData=true"}})

# Get values from a specific range (e.g., Sheet1!A1:D10)
tool_call(tool_name="authed_get", arguments={{"url": "{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID/values/Sheet1!A1:D10"}})

# Get values with specific render options
tool_call(tool_name="authed_get", arguments={{"url": "{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID/values/Sheet1!A1:D10?valueRenderOption=UNFORMATTED_VALUE"}})

# Batch get values from multiple ranges
tool_call(tool_name="authed_get", arguments={{"url": "{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID/values:batchGet?ranges=Sheet1!A1:B5&ranges=Sheet2!C1:D5"}})

# List all Google Spreadsheets (via Drive API)
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.spreadsheet%27"}})

# List Spreadsheets with name search
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.spreadsheet%27%20and%20name%20contains%20%27budget%27"}})

# List recent Spreadsheets
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.spreadsheet%27&orderBy=modifiedTime%20desc&pageSize=10"}})

# List Spreadsheets with specific fields
tool_call(tool_name="authed_get", arguments={{"url": "{DRIVE_API_BASE}/files?q=mimeType%20%3D%20%27application%2Fvnd.google-apps.spreadsheet%27&fields=files(id,name,modifiedTime)"}})
```

**Exporting a whole spreadsheet to the workspace (via google_export_sheet):**

Google Sheets have no raw bytes, so `download_drive_file` cannot fetch them. To get a spreadsheet as a file -- for the user to keep, to upload elsewhere, to convert, or to analyze every tab at once with pandas/openpyxl -- use the `google_export_sheet` tool call. It converts the spreadsheet through the Drive `files.export` endpoint, writes the result to the conversation workspace, and returns the filename.

| `format` | Export MIME type | Notes |
|----------|------------------|-------|
| `xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | Excel workbook, every tab; read with `openpyxl` / `pandas.read_excel` in `run_python` |
| `ods` | `application/vnd.oasis.opendocument.spreadsheet` | OpenDocument spreadsheet, every tab |
| `pdf` | `application/pdf` | Every tab, print layout |
| `csv` | `text/csv` | **First tab only** |
| `tsv` | `text/tab-separated-values` | **First tab only** |
| `zip` | `application/zip` | Zipped HTML, one page per tab |

```
# Export as Excel (every tab) and read it with pandas in the sandbox
tool_call(tool_name="google_export_sheet", arguments={{"spreadsheet_id": "SPREADSHEET_ID", "format": "xlsx"}})

# Export the first tab as CSV under a chosen filename
tool_call(tool_name="google_export_sheet", arguments={{"spreadsheet_id": "SPREADSHEET_ID", "format": "csv", "filename": "budget.csv"}})

# Export as PDF to send to someone
tool_call(tool_name="google_export_sheet", arguments={{"spreadsheet_id": "SPREADSHEET_ID", "format": "pdf"}})
```

The result carries `filename`, `size_bytes`, `format`, `export_mime_type`, and `document_title`. When `filename` is omitted the file is named `<spreadsheet title>.<ext>`.

- Prefer the Sheets API `values` endpoints above when you only need some cells or one tab -- they are cheaper and tab-addressable. Prefer `google_export_sheet` when you need the whole workbook as a file (every tab, or a deliverable).
- `csv` / `tsv` always carry the first tab. For another tab, read it via `values/<Tab>!A1:Z` through `authed_get`, or export `xlsx` and open the sheet by name in `run_python`.
- Only native Google Sheets (`mimeType = application/vnd.google-apps.spreadsheet`) are supported. Uploaded `.xlsx` / `.csv` files stored in Drive go through `download_drive_file`; Docs and Slides have their own export tools (`google_export_doc`, `google_export_slides`).
- Google caps exports at 10 MB of exported content; a huge workbook may need `csv` of the first tab or a ranged read instead.
- Do NOT call `/drive/v3/files/{{id}}/export` through `authed_get` -- the tool call path rejects it and points you back here.

**Important Notes:**
- Spreadsheet IDs can be found in Google Sheets URLs (e.g., `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit`)
- Range notation uses A1 notation (e.g., 'Sheet1!A1:D10', 'A1:B5', 'Sheet1')
- To list Google Spreadsheets, you MUST include `mimeType='application/vnd.google-apps.spreadsheet'` in the `q` parameter of the Drive API query. Use URL encoding for query strings (spaces become %20, quotes become %27).
- Use the `fields` parameter to request only the data you need, which reduces response size and saves tokens.

---

## Google Sheets Write Operations (via create_action_request)

Editing spreadsheet cells goes through `create_action_request` so the
user can review exactly which cells change before anything is written.
Never POST/PUT to the Sheets API directly.
`create_action_request` is top-level only -- if you are running as a
sub-agent, do not call it; return the proposed `request_type` and
`params` to the parent via `agent_task_response` instead.

### `edit_google_spreadsheet` — overwrite a cell range in a Google Spreadsheet

Overwrites a rectangular cell range on one tab with new values (applied
with `valueInputOption=USER_ENTERED`, so strings like `=SUM(A1:A5)`,
`2026-01-31`, or `$1,234` are parsed the way a user typing them would
be).

**You MUST read the target range immediately before proposing an edit**
and pass exactly what you read as `current_values`. Read with
`valueRenderOption=FORMULA` (e.g. `authed_get` on
`{SHEETS_API_BASE}/spreadsheets/SPREADSHEET_ID/values/Tab!B2:D5?valueRenderOption=FORMULA`)
so formula cells come back as their formula text: **a formula cell must
be claimed by its formula (e.g. `"=SUM(C2:C7)"`), not its computed
value** -- claiming the computed value is rejected. The server compares
`current_values` against the live sheet when you propose the edit AND
again when the user approves it; any mismatch rejects the write. This
prevents overwriting cells that were never read (or that changed while
the approval card was open).

Parameters:
- `spreadsheet_id` (string, required): The spreadsheet id from the URL
  (`docs.google.com/spreadsheets/d/<id>/...`), not the full URL.
- `tab` (string, required): The sheet (tab) title, case-sensitive.
- `range` (string, required): Bounded A1 rectangle WITHOUT the sheet
  prefix, e.g. `"B2:D5"` or a single cell `"B2"`. Unbounded ranges
  (`A:A`, `1:3`) are rejected -- the values grid must match the range
  dimensions exactly. Limits: 200 rows x 52 columns, 1000 cells total;
  split larger edits into multiple requests.
- `values` (2d array, required): Replacement values, row-major, exactly
  matching the range dimensions. Cells are strings, numbers, or
  booleans. Use `""` to clear a cell; `null` is rejected (the Sheets
  API would leave the cell untouched instead of clearing it). To keep a
  cell unchanged, pass its current value.
- `current_values` (2d array, required): The values currently in the
  range, exactly as you just read them. Formula cells MUST carry the
  formula text (read with `valueRenderOption=FORMULA`); plain cells may
  be the formatted, unformatted, or formula rendering -- all are
  accepted. Same dimensions as `values`; use `null` or `""` for empty
  cells.

Example params (update two forecast cells in `Sheet1!B2:C3`):
```json
{{
  "spreadsheet_id": "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg",
  "tab": "Sheet1",
  "range": "B2:C3",
  "values": [["Q3 forecast", 125000], ["Q4 forecast", 150000]],
  "current_values": [["Q3 forecast", "100,000"], ["Q4 forecast", ""]]
}}
```

The approval card renders a spreadsheet-style diff table (row numbers,
column letters, up to 2 rows/cols of surrounding context) highlighting
the cells that change, so make the range as tight as possible around
the cells you actually intend to modify.

On success the result includes `spreadsheet_id`, the updated range,
`updated_cells`, and the spreadsheet `url`.

Best practices:
- Read the range (with `valueRenderOption=FORMULA`) in the same turn you
  propose the edit; stale reads get rejected with a per-cell diff.
- Propose one coherent edit per request (a row update, a column of new
  values); don't bundle unrelated changes across distant ranges.
- Formulas: existing formula cells must appear in `current_values` as
  their formula text, and new formula strings in `values` are entered
  as formulas (USER_ENTERED). The approval card shows formula cells by
  their formulas, old and new."""
