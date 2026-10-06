"""Google Drive handlers: download_drive_file and the three Google Workspace
export tools (google_export_doc / google_export_sheet / google_export_slides).
"""

import json
import urllib.parse

from chat.gemini_api.tool_handlers._common import (
    _get_workspace_dir,
    _publish_file_list_changed,
    _sanitize_workspace_filename,
)


# ---------------------------------------------------------------------------
# Drive file download handler
# ---------------------------------------------------------------------------

async def _handle_download_drive_file(
    user: dict,
    conversation_id: str,
    file_id: str,
    filename: str | None = None,
    project_id: str | None = None,
) -> str:
    """Download a Google Drive file to the conversation workspace.

    Fetches file metadata (to determine the filename) and then downloads
    the binary content using alt=media.  The file is saved to the
    workspace so the LLM can read it with get_workspace_file.

    Args:
        user: Authenticated user dict (for Google credentials).
        conversation_id: Conversation UUID.
        file_id: Google Drive file ID.
        filename: Optional filename override.  When None, the original
            filename from Drive metadata is used.
        project_id: Optional project UUID for workspace resolution.

    Returns:
        JSON string with the result (success with filename and path,
        or error details).
    """
    from chat.gemini_api.authed_get import _make_authed_request

    DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

    # --- Step 1: Get file metadata to determine filename if not provided ---
    if not filename:
        metadata_url = (
            f"{DRIVE_API_BASE}/files/{file_id}"
            "?fields=name,mimeType&supportsAllDrives=true"
        )
        metadata_result = await _make_authed_request(
            metadata_url, user=user, raw_response=False,
        )
        # metadata_result is a JSON string on success or error
        try:
            metadata = json.loads(metadata_result)
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"error": f"Failed to parse Drive metadata: {metadata_result}"})

        if "error" in metadata:
            return json.dumps({"error": f"Failed to get file metadata: {metadata.get('error')}"})

        filename = metadata.get("name", f"drive_file_{file_id}")

    # --- Step 2: Download binary content with alt=media ---
    download_url = (
        f"{DRIVE_API_BASE}/files/{file_id}"
        "?alt=media&supportsAllDrives=true"
    )
    download_result = await _make_authed_request(
        download_url, user=user, raw_response=True,
    )

    # If we got a string back, it's an error
    if isinstance(download_result, str):
        try:
            err = json.loads(download_result)
        except (json.JSONDecodeError, TypeError):
            err = download_result
        return json.dumps({"error": f"Failed to download file: {err}"})

    # download_result is an httpx.Response with binary content
    file_bytes = download_result.content
    content_type = download_result.headers.get("content-type", "application/octet-stream")

    # --- Step 3: Save to workspace ---
    workspace_dir = await _get_workspace_dir(conversation_id, project_id=project_id)

    # Sanitize filename: strip path separators to prevent traversal
    clean_filename = filename.replace("/", "_").replace("\\", "_").strip()
    if not clean_filename:
        clean_filename = f"drive_file_{file_id}"

    file_path = (workspace_dir / clean_filename).resolve()

    # Ensure resolved path is within workspace
    try:
        file_path.relative_to(workspace_dir.resolve())
    except ValueError:
        return json.dumps({"error": "Invalid filename: results in path outside workspace"})

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(file_bytes)
    except Exception as e:
        return json.dumps({"error": f"Failed to save file to workspace: {e}"})

    _publish_file_list_changed(user["id"], conversation_id, project_id)
    return json.dumps({
        "status": "success",
        "filename": clean_filename,
        "size_bytes": len(file_bytes),
        "content_type": content_type,
        "message": (
            f"File '{clean_filename}' ({len(file_bytes)} bytes) has been saved to the workspace. "
            f"Use get_workspace_file to read or analyze it: "
            f'tool_call(tool_name="get_workspace_file", arguments={{"path": "{clean_filename}"}})'
        ),
    })


# ---------------------------------------------------------------------------
# Google Workspace export handlers (Docs / Sheets / Slides)
# ---------------------------------------------------------------------------

_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_GOOGLE_SLIDES_MIME = "application/vnd.google-apps.presentation"

# Every export format Google Drive supports for native Google Docs
# (Drive API "Export MIME types for Google Workspace documents").
# Keyed by the short format name the model passes; value is
# (export MIME type, file extension).
GOOGLE_DOC_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "pdf": ("application/pdf", ".pdf"),
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "odt": ("application/vnd.oasis.opendocument.text", ".odt"),
    "rtf": ("application/rtf", ".rtf"),
    "txt": ("text/plain", ".txt"),
    "md": ("text/markdown", ".md"),
    "html": ("text/html", ".html"),
    "epub": ("application/epub+zip", ".epub"),
    # Zipped HTML (the web-page export bundle with images).
    "zip": ("application/zip", ".zip"),
}

# Every export format for native Google Sheets. ``csv`` and ``tsv`` export
# the FIRST sheet (tab) only -- Drive ``files.export`` takes no tab
# selector; per-tab values come from the Sheets API via authed_get.
GOOGLE_SHEET_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
    "pdf": ("application/pdf", ".pdf"),
    "csv": ("text/csv", ".csv"),
    "tsv": ("text/tab-separated-values", ".tsv"),
    # Zipped HTML (one HTML file per sheet).
    "zip": ("application/zip", ".zip"),
}

# Every export format for native Google Slides. The three image formats
# render the FIRST slide only (Drive ``files.export`` takes no page
# selector).
GOOGLE_SLIDES_EXPORT_FORMATS: dict[str, tuple[str, str]] = {
    "pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "odp": ("application/vnd.oasis.opendocument.presentation", ".odp"),
    "pdf": ("application/pdf", ".pdf"),
    "txt": ("text/plain", ".txt"),
    "png": ("image/png", ".png"),
    "jpeg": ("image/jpeg", ".jpg"),
    "svg": ("image/svg+xml", ".svg"),
}

# Short-name aliases the model may plausibly pass, per format table.
_EXPORT_FORMAT_ALIASES: dict[str, str] = {
    "markdown": "md",
    "text": "txt",
    "plain": "txt",
    "htm": "html",
    "jpg": "jpeg",
    "excel": "xlsx",
    "powerpoint": "pptx",
}


def _resolve_export_format(
    fmt: str | None, formats: dict[str, tuple[str, str]],
) -> tuple[str, str, str] | None:
    """Resolve a model-supplied format against *formats* to ``(name, mime, ext)``.

    Accepts the short name (``"pdf"``), the same with a leading dot or in
    any case (``".PDF"``), a known alias (``"markdown"``, ``"jpg"``), or the
    exact export MIME type. Returns None when the value is not a supported
    export format for that table.
    """
    if not fmt or not isinstance(fmt, str):
        return None
    key = fmt.strip()
    by_mime = {mime: name for name, (mime, _ext) in formats.items()}
    if key in by_mime:
        key = by_mime[key]
    key = key.lower().lstrip(".")
    key = _EXPORT_FORMAT_ALIASES.get(key, key)
    entry = formats.get(key)
    if entry is None:
        return None
    return key, entry[0], entry[1]


def _resolve_doc_export_format(fmt: str | None) -> tuple[str, str, str] | None:
    """Resolve a Google Docs export format (see ``_resolve_export_format``)."""
    return _resolve_export_format(fmt, GOOGLE_DOC_EXPORT_FORMATS)


def _resolve_sheet_export_format(fmt: str | None) -> tuple[str, str, str] | None:
    """Resolve a Google Sheets export format (see ``_resolve_export_format``)."""
    return _resolve_export_format(fmt, GOOGLE_SHEET_EXPORT_FORMATS)


def _resolve_slides_export_format(fmt: str | None) -> tuple[str, str, str] | None:
    """Resolve a Google Slides export format (see ``_resolve_export_format``)."""
    return _resolve_export_format(fmt, GOOGLE_SLIDES_EXPORT_FORMATS)


# Per-kind wording for the shared export routine: the source MIME type the
# tool accepts, the format table, the human label used in messages, the
# id parameter name, the tool's own name, and the sibling export tools a
# wrong-kind Workspace file should be pointed at.
_EXPORT_KINDS: dict[str, dict] = {
    "doc": {
        "mime": _GOOGLE_DOC_MIME,
        "formats": GOOGLE_DOC_EXPORT_FORMATS,
        "label": "Google Doc",
        "id_param": "document_id",
        "tool": "google_export_doc",
        "default_stem": "google_doc",
    },
    "sheet": {
        "mime": _GOOGLE_SHEET_MIME,
        "formats": GOOGLE_SHEET_EXPORT_FORMATS,
        "label": "Google Sheet",
        "id_param": "spreadsheet_id",
        "tool": "google_export_sheet",
        "default_stem": "google_sheet",
    },
    "slides": {
        "mime": _GOOGLE_SLIDES_MIME,
        "formats": GOOGLE_SLIDES_EXPORT_FORMATS,
        "label": "Google Slides presentation",
        "id_param": "presentation_id",
        "tool": "google_export_slides",
        "default_stem": "google_slides",
    },
}

# Source MIME type -> the export tool that handles it (for cross-pointers).
_EXPORT_TOOL_BY_MIME: dict[str, str] = {
    spec["mime"]: spec["tool"] for spec in _EXPORT_KINDS.values()
}


def _wrong_kind_hint(kind: str, source_mime: str, file_id: str) -> str:
    """Hint for a file whose MIME type does not match the export *kind*."""
    spec = _EXPORT_KINDS[kind]
    sibling = _EXPORT_TOOL_BY_MIME.get(source_mime)
    if sibling is not None:
        sibling_param = next(
            s["id_param"] for s in _EXPORT_KINDS.values() if s["tool"] == sibling
        )
        return (
            f"This tool only exports native {spec['label']}s. Use {sibling} "
            f"instead: tool_call(tool_name=\"{sibling}\", arguments={{\"{sibling_param}\": "
            f"\"{file_id}\", \"format\": \"pdf\"}})."
        )
    if source_mime.startswith("application/vnd.google-apps."):
        return (
            f"This tool only exports native {spec['label']}s. Other Google "
            "Workspace file types are not supported by it."
        )
    return (
        "This is a regular (non-Google-Workspace) file with its own bytes; "
        "use download_drive_file instead: "
        'tool_call(tool_name="download_drive_file", arguments={"file_id": "'
        + file_id + '"}).'
    )


async def _export_workspace_file(
    kind: str,
    user: dict,
    conversation_id: str,
    file_id: str,
    format: str,
    filename: str | None = None,
    project_id: str | None = None,
) -> str:
    """Shared body of the three Google Workspace export tools.

    Google Docs / Sheets / Slides have no downloadable bytes of their own
    (``alt=media`` on ``files/{id}`` fails for Google Workspace files), so
    the content has to be converted through the Drive ``files.export``
    endpoint. This routine validates the requested format against the
    kind's format table, fetches the file's metadata to confirm it really
    is that kind of Workspace file (and to derive the default filename),
    runs the export, and writes the bytes to the conversation / project
    workspace. Read the result back with ``get_workspace_file``.

    Returns a JSON string: success receipt (filename, size, format, mime)
    or an ``error`` object.
    """
    from chat.gemini_api.authed_get import _make_authed_request

    DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
    spec = _EXPORT_KINDS[kind]
    formats: dict[str, tuple[str, str]] = spec["formats"]
    label: str = spec["label"]

    file_id = (file_id or "").strip()
    if not file_id:
        return json.dumps({"error": f"{spec['id_param']} is required."})

    resolved = _resolve_export_format(format, formats)
    if resolved is None:
        return json.dumps({
            "error": (
                f"Unsupported export format {format!r}. Supported formats: "
                + ", ".join(sorted(formats))
                + "."
            ),
            "supported_formats": {
                name: mime for name, (mime, _ext) in formats.items()
            },
        })
    format_name, export_mime, ext = resolved

    # --- Step 1: Metadata -- confirm the kind, get the title --------------
    metadata_url = (
        f"{DRIVE_API_BASE}/files/{file_id}"
        "?fields=name,mimeType&supportsAllDrives=true"
    )
    metadata_result = await _make_authed_request(
        metadata_url, user=user, raw_response=False,
    )
    try:
        metadata = json.loads(metadata_result)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({"error": f"Failed to parse Drive metadata: {metadata_result}"})
    if not isinstance(metadata, dict):
        return json.dumps({"error": f"Unexpected Drive metadata response: {metadata_result}"})
    if "error" in metadata:
        return json.dumps({"error": f"Failed to get {label} metadata: {metadata.get('error')}"})

    source_mime = metadata.get("mimeType", "")
    if source_mime != spec["mime"]:
        hint = _wrong_kind_hint(kind, source_mime, file_id)
        return json.dumps({
            "error": (
                f"File '{metadata.get('name', file_id)}' is not a {label} "
                f"(mimeType {source_mime or 'unknown'}). {hint}"
            )
        })

    title = metadata.get("name") or f"{spec['default_stem']}_{file_id}"

    # --- Step 2: Export via files.export ---------------------------------
    export_url = (
        f"{DRIVE_API_BASE}/files/{file_id}/export"
        f"?mimeType={urllib.parse.quote(export_mime, safe='')}"
    )
    export_result = await _make_authed_request(
        export_url, user=user, raw_response=True,
    )
    if isinstance(export_result, str):
        try:
            err = json.loads(export_result)
        except (json.JSONDecodeError, TypeError):
            err = export_result
        return json.dumps({
            "error": f"Failed to export {label} as {format_name}: {err}",
            "hint": (
                "Google caps exports at 10 MB of exported content; very large "
                "files may need a lighter format (txt, md, csv) or a "
                "narrower read through the service's own API."
            ),
        })

    file_bytes = export_result.content
    content_type = export_result.headers.get("content-type", export_mime)

    # --- Step 3: Save to workspace ---------------------------------------
    workspace_dir = await _get_workspace_dir(conversation_id, project_id=project_id)

    if filename:
        clean_filename = _sanitize_workspace_filename(filename)
    else:
        clean_filename = _sanitize_workspace_filename(title)
        if clean_filename:
            clean_filename += ext
    if not clean_filename:
        clean_filename = f"{spec['default_stem']}_{file_id}{ext}"

    file_path = (workspace_dir / clean_filename).resolve()
    try:
        file_path.relative_to(workspace_dir.resolve())
    except ValueError:
        return json.dumps({"error": "Invalid filename: results in path outside workspace"})

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(file_bytes)
    except Exception as e:
        return json.dumps({"error": f"Failed to save file to workspace: {e}"})

    _publish_file_list_changed(user["id"], conversation_id, project_id)
    return json.dumps({
        "status": "success",
        "filename": clean_filename,
        "size_bytes": len(file_bytes),
        "format": format_name,
        "export_mime_type": export_mime,
        "content_type": content_type,
        "document_title": title,
        "message": (
            f"{label} '{title}' exported as {format_name} to "
            f"'{clean_filename}' ({len(file_bytes)} bytes) in the workspace. "
            f"Use get_workspace_file to read or analyze it: "
            f'tool_call(tool_name="get_workspace_file", arguments={{"path": "{clean_filename}"}})'
        ),
    })


async def _handle_google_export_doc(
    user: dict,
    conversation_id: str,
    document_id: str,
    format: str,
    filename: str | None = None,
    project_id: str | None = None,
) -> str:
    """Export a native Google Doc to the workspace in a chosen format.

    Args:
        user: Authenticated user dict (for Google credentials).
        conversation_id: Conversation UUID.
        document_id: Google Doc id (the ``/document/d/<id>/`` URL segment).
        format: Short export format name (``pdf``, ``docx``, ``odt``,
            ``rtf``, ``txt``, ``md``, ``html``, ``epub``, ``zip``) or the
            exact export MIME type.
        filename: Optional workspace filename override. When None, the
            Doc's title plus the format's extension is used.
        project_id: Optional project UUID for workspace resolution.

    Returns:
        JSON string: success receipt (filename, size, format, mime) or an
        ``error`` object. See ``_export_workspace_file``.
    """
    return await _export_workspace_file(
        "doc", user, conversation_id, document_id, format,
        filename=filename, project_id=project_id,
    )


async def _handle_google_export_sheet(
    user: dict,
    conversation_id: str,
    spreadsheet_id: str,
    format: str,
    filename: str | None = None,
    project_id: str | None = None,
) -> str:
    """Export a native Google Sheet to the workspace in a chosen format.

    Args:
        user: Authenticated user dict (for Google credentials).
        conversation_id: Conversation UUID.
        spreadsheet_id: Google Sheet id (the ``/spreadsheets/d/<id>/`` URL
            segment).
        format: Short export format name (``xlsx``, ``ods``, ``pdf``,
            ``csv``, ``tsv``, ``zip``) or the exact export MIME type.
            ``csv`` / ``tsv`` carry the first tab only.
        filename: Optional workspace filename override. When None, the
            spreadsheet's title plus the format's extension is used.
        project_id: Optional project UUID for workspace resolution.

    Returns:
        JSON string: success receipt (filename, size, format, mime) or an
        ``error`` object. See ``_export_workspace_file``.
    """
    return await _export_workspace_file(
        "sheet", user, conversation_id, spreadsheet_id, format,
        filename=filename, project_id=project_id,
    )


async def _handle_google_export_slides(
    user: dict,
    conversation_id: str,
    presentation_id: str,
    format: str,
    filename: str | None = None,
    project_id: str | None = None,
) -> str:
    """Export a native Google Slides deck to the workspace in a chosen format.

    Args:
        user: Authenticated user dict (for Google credentials).
        conversation_id: Conversation UUID.
        presentation_id: Google Slides id (the ``/presentation/d/<id>/``
            URL segment).
        format: Short export format name (``pptx``, ``odp``, ``pdf``,
            ``txt``, ``png``, ``jpeg``, ``svg``) or the exact export MIME
            type. The image formats render the first slide only.
        filename: Optional workspace filename override. When None, the
            deck's title plus the format's extension is used.
        project_id: Optional project UUID for workspace resolution.

    Returns:
        JSON string: success receipt (filename, size, format, mime) or an
        ``error`` object. See ``_export_workspace_file``.
    """
    return await _export_workspace_file(
        "slides", user, conversation_id, presentation_id, format,
        filename=filename, project_id=project_id,
    )
