"""Tests for ``google_export_sheet`` / ``google_export_slides`` and the
extended Drive read allow-list.

Both tools share ``_export_workspace_file`` with ``google_export_doc``
(``tests/test_google_export_doc_handler.py`` covers the shared body in
depth -- filename sanitisation, upstream failures, output_file
interaction). Covered here:

* Per-kind format tables (complete Google export sets, schema enums pinned
  to them, aliases, cross-table rejection).
* Happy path per tool: metadata + export calls, default ``<title><ext>``
  filename, bytes on disk, receipt shape.
* Kind check: a Doc passed to the Sheet tool (and vice versa) gets a
  cross-pointer naming the right tool and id parameter; regular files
  point at ``download_drive_file``; nothing exported.
* Registry / dispatch wiring, allow-list exclusions, skill text.
* Drive ``_SERVICE_REGISTRY`` admits the new read-only paths (permissions,
  revisions, comments/replies, changes, about, drives/{id}) and still
  refuses write-shaped / nested paths.
"""

import asyncio
import contextlib
import json
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.tool_handlers import (
    GOOGLE_DOC_EXPORT_FORMATS,
    GOOGLE_SHEET_EXPORT_FORMATS,
    GOOGLE_SLIDES_EXPORT_FORMATS,
    _handle_google_export_sheet,
    _handle_google_export_slides,
    _resolve_sheet_export_format,
    _resolve_slides_export_format,
)


def _run(coro):
    return asyncio.run(coro)


USER = {"id": 7, "email": "u@example.com"}
FILE_ID = "1AbC_dEf-GhI"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"


def _response(content: bytes, content_type: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.headers = {"content-type": content_type}
    return resp


@contextlib.contextmanager
def _patch_workspace(tmp_path: Path):
    async def fake_workspace(*args, **kwargs):
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    with patch(
        "chat.gemini_api.tool_handlers.drive.conversation_workspace_dir",
        new=fake_workspace,
    ), patch(
        "chat.gemini_api.tool_handlers.conversation_workspace_dir",
        new=fake_workspace,
    ):
        yield


def _fake_requests(metadata, export):
    calls: list[tuple[str, dict]] = []

    async def side_effect(url, *args, **kwargs):
        calls.append((url, kwargs))
        if "/export" in urllib.parse.urlparse(url).path:
            return export
        return metadata

    mock = AsyncMock(side_effect=side_effect)
    mock.calls = calls
    return mock


def _export(handler, tmp_path, *, fmt, metadata, export, filename=None):
    req = _fake_requests(metadata, export)
    publish = MagicMock()
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    with patch("chat.gemini_api.authed_get._make_authed_request", req), \
         _patch_workspace(tmp_path), \
         patch("chat.gemini_api.tool_handlers.drive._publish_file_list_changed", publish):
        result = json.loads(_run(handler(
            USER, "conv-1", FILE_ID, fmt, filename=filename,
        )))
    return result, req, publish, tmp_path / "workspace"


def _export_sheet(tmp_path, *, fmt="xlsx", metadata=None, export=None, filename=None):
    metadata = metadata if metadata is not None else json.dumps(
        {"name": "Budget 2026", "mimeType": SHEET_MIME}
    )
    export = export if export is not None else _response(b"PK\x03\x04 xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return _export(_handle_google_export_sheet, tmp_path, fmt=fmt, metadata=metadata,
                   export=export, filename=filename)


def _export_slides(tmp_path, *, fmt="pptx", metadata=None, export=None, filename=None):
    metadata = metadata if metadata is not None else json.dumps(
        {"name": "Q3 Review", "mimeType": SLIDES_MIME}
    )
    export = export if export is not None else _response(b"PK\x03\x04 pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    return _export(_handle_google_export_slides, tmp_path, fmt=fmt, metadata=metadata,
                   export=export, filename=filename)


# ---------------------------------------------------------------------------
# Format tables
# ---------------------------------------------------------------------------


class TestFormatTables:
    def test_complete_google_sheets_export_set(self):
        assert set(GOOGLE_SHEET_EXPORT_FORMATS) == {"xlsx", "ods", "pdf", "csv", "tsv", "zip"}

    def test_complete_google_slides_export_set(self):
        assert set(GOOGLE_SLIDES_EXPORT_FORMATS) == {
            "pptx", "odp", "pdf", "txt", "png", "jpeg", "svg",
        }

    def test_schema_enums_match_format_tables(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        sheet_enum = TOOL_CALL_REGISTRY["google_export_sheet"]["parameters"]["properties"]["format"]["enum"]
        slides_enum = TOOL_CALL_REGISTRY["google_export_slides"]["parameters"]["properties"]["format"]["enum"]
        assert set(sheet_enum) == set(GOOGLE_SHEET_EXPORT_FORMATS)
        assert set(slides_enum) == set(GOOGLE_SLIDES_EXPORT_FORMATS)

    def test_every_short_name_and_mime_resolves(self):
        for name, (mime, ext) in GOOGLE_SHEET_EXPORT_FORMATS.items():
            assert _resolve_sheet_export_format(name) == (name, mime, ext)
            assert _resolve_sheet_export_format(mime)[0] == name
        for name, (mime, ext) in GOOGLE_SLIDES_EXPORT_FORMATS.items():
            assert _resolve_slides_export_format(name) == (name, mime, ext)
            assert _resolve_slides_export_format(mime)[0] == name

    def test_aliases_and_tolerance(self):
        assert _resolve_sheet_export_format(".XLSX")[0] == "xlsx"
        assert _resolve_sheet_export_format("excel")[0] == "xlsx"
        assert _resolve_slides_export_format("jpg")[0] == "jpeg"
        assert _resolve_slides_export_format("powerpoint")[0] == "pptx"
        assert _resolve_slides_export_format("text")[0] == "txt"

    def test_tables_do_not_bleed_into_each_other(self):
        # A Docs-only format is not a Sheets format and vice versa.
        assert _resolve_sheet_export_format("docx") is None
        assert _resolve_sheet_export_format("md") is None
        assert _resolve_slides_export_format("xlsx") is None
        assert _resolve_slides_export_format("csv") is None
        assert "pptx" not in GOOGLE_DOC_EXPORT_FORMATS
        assert _resolve_sheet_export_format(None) is None


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestSheetHappyPath:
    def test_xlsx_export_default_filename(self, tmp_path):
        result, req, publish, ws = _export_sheet(tmp_path)
        assert result["status"] == "success"
        assert result["filename"] == "Budget 2026.xlsx"
        assert result["format"] == "xlsx"
        assert result["export_mime_type"] == GOOGLE_SHEET_EXPORT_FORMATS["xlsx"][0]
        assert result["document_title"] == "Budget 2026"
        assert "Google Sheet" in result["message"]
        assert (ws / "Budget 2026.xlsx").read_bytes() == b"PK\x03\x04 xlsx"
        publish.assert_called_once_with(USER["id"], "conversation", "conv-1", None)

        assert len(req.calls) == 2
        meta_url, meta_kwargs = req.calls[0]
        export_url, export_kwargs = req.calls[1]
        assert meta_url.startswith(f"https://www.googleapis.com/drive/v3/files/{FILE_ID}?")
        assert meta_kwargs["raw_response"] is False
        assert export_kwargs["raw_response"] is True
        parsed = urllib.parse.urlparse(export_url)
        assert parsed.path == f"/drive/v3/files/{FILE_ID}/export"
        assert urllib.parse.parse_qs(parsed.query) == {
            "mimeType": [GOOGLE_SHEET_EXPORT_FORMATS["xlsx"][0]],
        }

    def test_csv_with_filename_override(self, tmp_path):
        result, _req, _publish, ws = _export_sheet(
            tmp_path, fmt="csv", filename="budget.csv",
            export=_response(b"a,b\n1,2\n", "text/csv"),
        )
        assert result["filename"] == "budget.csv"
        assert (ws / "budget.csv").read_text() == "a,b\n1,2\n"


class TestSlidesHappyPath:
    def test_pptx_export_default_filename(self, tmp_path):
        result, req, publish, ws = _export_slides(tmp_path)
        assert result["status"] == "success"
        assert result["filename"] == "Q3 Review.pptx"
        assert result["format"] == "pptx"
        assert result["document_title"] == "Q3 Review"
        assert "Google Slides" in result["message"]
        assert (ws / "Q3 Review.pptx").exists()
        publish.assert_called_once()
        export_url = req.calls[1][0]
        assert (
            "mimeType=application%2Fvnd.openxmlformats-officedocument.presentationml.presentation"
            in export_url
        )

    def test_jpeg_uses_jpg_extension(self, tmp_path):
        result, _req, _publish, ws = _export_slides(
            tmp_path, fmt="jpg", export=_response(b"\xff\xd8", "image/jpeg"),
        )
        assert result["format"] == "jpeg"
        assert result["filename"] == "Q3 Review.jpg"
        assert (ws / "Q3 Review.jpg").exists()


# ---------------------------------------------------------------------------
# Kind checks / rejections
# ---------------------------------------------------------------------------


class TestKindChecks:
    def test_doc_passed_to_sheet_tool_points_to_doc_tool(self, tmp_path):
        result, req, publish, ws = _export_sheet(
            tmp_path, metadata=json.dumps({"name": "Spec", "mimeType": DOC_MIME}),
        )
        assert "not a Google Sheet" in result["error"]
        assert "google_export_doc" in result["error"]
        assert f'"document_id": "{FILE_ID}"' in result["error"]
        assert len(req.calls) == 1
        publish.assert_not_called()
        assert not any(ws.iterdir())

    def test_slides_passed_to_sheet_tool_points_to_slides_tool(self, tmp_path):
        result, _req, _publish, _ws = _export_sheet(
            tmp_path, metadata=json.dumps({"name": "Deck", "mimeType": SLIDES_MIME}),
        )
        assert "google_export_slides" in result["error"]
        assert f'"presentation_id": "{FILE_ID}"' in result["error"]

    def test_sheet_passed_to_slides_tool_points_to_sheet_tool(self, tmp_path):
        result, _req, _publish, _ws = _export_slides(
            tmp_path, metadata=json.dumps({"name": "Budget", "mimeType": SHEET_MIME}),
        )
        assert "not a Google Slides presentation" in result["error"]
        assert "google_export_sheet" in result["error"]
        assert f'"spreadsheet_id": "{FILE_ID}"' in result["error"]

    def test_regular_file_points_to_download_drive_file(self, tmp_path):
        result, req, _publish, _ws = _export_slides(
            tmp_path, metadata=json.dumps({"name": "deck.pptx", "mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
        )
        assert "download_drive_file" in result["error"]
        assert len(req.calls) == 1

    def test_unknown_format_no_upstream_call(self, tmp_path):
        result, req, _publish, _ws = _export_sheet(tmp_path, fmt="docx")
        assert "Unsupported export format" in result["error"]
        assert set(result["supported_formats"]) == set(GOOGLE_SHEET_EXPORT_FORMATS)
        assert req.calls == []

    def test_missing_id(self, tmp_path):
        req = _fake_requests("{}", None)
        with patch("chat.gemini_api.authed_get._make_authed_request", req):
            result = json.loads(_run(_handle_google_export_slides(USER, "conv-1", " ", "pdf")))
        assert "presentation_id is required" in result["error"]
        assert req.calls == []

    def test_export_error_short_circuits_with_hint(self, tmp_path):
        result, req, publish, ws = _export_sheet(
            tmp_path, export=json.dumps({"error": "HTTP 403: This file is too large to be exported."}),
        )
        assert "Failed to export Google Sheet as xlsx" in result["error"]
        assert "10 MB" in result["hint"]
        assert len(req.calls) == 2
        publish.assert_not_called()
        assert not any(ws.iterdir())


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    @pytest.mark.parametrize("tool,id_param", [
        ("google_export_sheet", "spreadsheet_id"),
        ("google_export_slides", "presentation_id"),
    ])
    def test_registered_in_registry_and_dispatch_table(self, tool, id_param):
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        assert tool in TOOL_CALL_REGISTRY
        assert tool in TOOL_CALL_HANDLERS
        assert TOOL_CALL_REGISTRY[tool]["parameters"]["required"] == [id_param, "format"]

    @pytest.mark.parametrize("tool", ["google_export_sheet", "google_export_slides"])
    def test_not_in_public_or_script_allow_lists(self, tool):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST
        assert tool not in PUBLIC_TOOL_CALL_ALLOWLIST
        assert tool not in SCRIPT_TOOL_CALL_ALLOWLIST

    def test_dispatch_forwards_sheet_arguments(self):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        handler = AsyncMock(return_value=json.dumps({"status": "success"}))
        with patch("chat.gemini_api.tool_dispatch._handle_google_export_sheet", handler):
            result, _parts = _run(_dispatch_tool_call(
                app=None, provider=None, user=USER, conversation_id="conv-1",
                timezone="UTC", tool_name="tool_call",
                args={"tool_name": "google_export_sheet",
                      "arguments": {"spreadsheet_id": FILE_ID, "format": "csv",
                                    "filename": "x.csv", "intent_message": "Export"}},
            ))
        assert json.loads(result)["status"] == "success"
        handler.assert_awaited_once_with(
            USER, "conv-1", FILE_ID, "csv", filename="x.csv", project_id=None,
        )

    def test_dispatch_forwards_slides_arguments(self):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        handler = AsyncMock(return_value=json.dumps({"status": "success"}))
        with patch("chat.gemini_api.tool_dispatch._handle_google_export_slides", handler):
            result, _parts = _run(_dispatch_tool_call(
                app=None, provider=None, user=USER, conversation_id="conv-1",
                timezone="UTC", tool_name="tool_call",
                args={"tool_name": "google_export_slides",
                      "arguments": {"presentation_id": FILE_ID, "format": "txt"}},
            ))
        assert json.loads(result)["status"] == "success"
        handler.assert_awaited_once_with(
            USER, "conv-1", FILE_ID, "txt", filename=None, project_id=None,
        )

    def test_skills_mention_export_tools(self):
        from api.drive import get_instructions as drive_text
        from api.sheets import get_instructions as sheets_text
        from api.slides import get_instructions as slides_text
        sheets = sheets_text("http://localhost")
        slides = slides_text("http://localhost")
        assert "google_export_sheet" in sheets
        assert "google_export_slides" in slides
        for name, (mime, _ext) in GOOGLE_SHEET_EXPORT_FORMATS.items():
            assert f"`{name}`" in sheets and mime in sheets
        for name, (mime, _ext) in GOOGLE_SLIDES_EXPORT_FORMATS.items():
            assert f"`{name}`" in slides and mime in slides
        drive = drive_text("http://localhost")
        for tool in ("google_export_doc", "google_export_sheet", "google_export_slides"):
            assert tool in drive


# ---------------------------------------------------------------------------
# Drive read allow-list
# ---------------------------------------------------------------------------


class TestDriveReadAllowList:
    @pytest.fixture
    def patterns(self):
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        return _SERVICE_REGISTRY["www.googleapis.com/drive/v3"]["_allowed_endpoints"]

    @pytest.mark.parametrize("path", [
        "/drive/v3/files",
        f"/drive/v3/files/{FILE_ID}",
        f"/drive/v3/files/{FILE_ID}/export",
        f"/drive/v3/files/{FILE_ID}/permissions",
        f"/drive/v3/files/{FILE_ID}/permissions/anyoneWithLink",
        f"/drive/v3/files/{FILE_ID}/revisions",
        f"/drive/v3/files/{FILE_ID}/revisions/42",
        f"/drive/v3/files/{FILE_ID}/comments",
        f"/drive/v3/files/{FILE_ID}/comments/AAAA",
        f"/drive/v3/files/{FILE_ID}/comments/AAAA/replies",
        f"/drive/v3/files/{FILE_ID}/comments/AAAA/replies/BBBB",
        "/drive/v3/changes",
        "/drive/v3/changes/startPageToken",
        "/drive/v3/about",
        "/drive/v3/drives",
        "/drive/v3/drives/0ABcdEf",
    ])
    def test_read_paths_admitted(self, patterns, path):
        assert any(p.match(path) for p in patterns), path

    @pytest.mark.parametrize("path", [
        f"/drive/v3/files/{FILE_ID}/copy",
        f"/drive/v3/files/{FILE_ID}/watch",
        f"/drive/v3/files/{FILE_ID}/modifyLabels",
        f"/drive/v3/files/{FILE_ID}/listLabels",
        f"/drive/v3/files/{FILE_ID}/permissions/p/extra",
        f"/drive/v3/files/{FILE_ID}/comments/AAAA/replies/BBBB/extra",
        "/drive/v3/changes/watch",
        "/drive/v3/drives/0ABcdEf/hide",
        "/drive/v3/drives/0ABcdEf/unhide",
        "/drive/v3/teamdrives",
        "/drive/v3/apps",
    ])
    def test_other_paths_refused(self, patterns, path):
        assert not any(p.match(path) for p in patterns), path

    def test_revision_alt_media_still_blocked_in_tool_path(self):
        # The generic alt=media gate covers revision downloads too.
        from chat.gemini_api.authed_get import handle_authed_get
        make = AsyncMock()
        with patch("chat.gemini_api.authed_get._make_authed_request", make):
            result = json.loads(_run(handle_authed_get(
                f"https://www.googleapis.com/drive/v3/files/{FILE_ID}/revisions/42?alt=media",
                user=USER, conversation_id="conv-1",
            )))
        assert "alt=media" in result["error"]
        make.assert_not_called()
