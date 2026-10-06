"""Tests for the ``google_export_doc`` tool (``_handle_google_export_doc``).

Native Google Docs have no ``alt=media`` bytes, so the tool converts them
through the Drive ``files.export`` endpoint and writes the result to the
workspace. Covered here:

* Format resolution: every short name in ``GOOGLE_DOC_EXPORT_FORMATS``,
  case/dot tolerance, exact export MIME types, and rejection of unknown
  formats before any upstream call.
* Happy path: metadata + export calls, default ``<title><ext>`` filename,
  bytes on disk, receipt shape, ``_publish_file_list_changed`` invoked.
* Filename override + sanitisation (path separators, hidden-file dots).
* Non-Doc sources: regular files point at ``download_drive_file``; other
  Workspace types (Sheets) get a not-supported error; no export call made.
* Upstream failures on metadata and on export short-circuit with no file.
* ``handle_authed_get`` blocks the ``/export`` path without ``output_file``
  (mirrors the alt=media block) and the Drive allow-list admits it.
* Registry / dispatch table wiring and allow-list exclusions.

All HTTP calls are mocked -- no real network traffic.
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
    _handle_google_export_doc,
    _resolve_doc_export_format,
)


def _run(coro):
    return asyncio.run(coro)


USER = {"id": 7, "email": "u@example.com"}
DOC_ID = "1AbC_dEf-GhI"
DOC_MIME = "application/vnd.google-apps.document"


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

    # Two distinct attributes reference the real function: the drive module's
    # own binding (used by the export handler) and the tool_handlers package
    # re-export (read at call time by authed_get's lazy ``from ... import``).
    # Patching only the drive one leaves the authed_get path hitting the real
    # resolver -- which happened to pass or fail depending on whichever DB
    # engine binding earlier tests left behind.
    with patch(
        "chat.gemini_api.tool_handlers.drive._get_workspace_dir",
        new=fake_workspace,
    ), patch(
        "chat.gemini_api.tool_handlers._get_workspace_dir",
        new=fake_workspace,
    ):
        yield


def _fake_requests(metadata, export):
    """Return an AsyncMock that answers the metadata call then the export call.

    ``metadata`` is the JSON-string reply for ``files/{id}?fields=...``;
    ``export`` is the reply for ``files/{id}/export`` (an httpx-like
    response or a JSON error string). The mock records every URL so tests
    can assert on the exact upstream calls.
    """
    calls: list[tuple[str, dict]] = []

    async def side_effect(url, *args, **kwargs):
        calls.append((url, kwargs))
        if "/export" in urllib.parse.urlparse(url).path:
            return export
        return metadata

    mock = AsyncMock(side_effect=side_effect)
    mock.calls = calls
    return mock


def _export(tmp_path, *, fmt="pdf", filename=None, metadata=None, export=None,
            project_id=None):
    metadata = metadata if metadata is not None else json.dumps(
        {"name": "Q3 Report", "mimeType": DOC_MIME}
    )
    export = export if export is not None else _response(b"%PDF-1.7 fake", "application/pdf")
    req = _fake_requests(metadata, export)
    publish = MagicMock()
    # Pre-create so "nothing was written" assertions can iterate the dir even
    # when the handler short-circuits before resolving the workspace.
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    with patch("chat.gemini_api.authed_get._make_authed_request", req), \
         _patch_workspace(tmp_path), \
         patch("chat.gemini_api.tool_handlers.drive._publish_file_list_changed", publish):
        result = json.loads(_run(_handle_google_export_doc(
            USER, "conv-1", DOC_ID, fmt, filename=filename, project_id=project_id,
        )))
    return result, req, publish, tmp_path / "workspace"


# ---------------------------------------------------------------------------
# Format resolution
# ---------------------------------------------------------------------------


class TestFormatResolution:
    def test_every_short_name_resolves_to_itself(self):
        for name, (mime, ext) in GOOGLE_DOC_EXPORT_FORMATS.items():
            assert _resolve_doc_export_format(name) == (name, mime, ext)

    def test_case_and_leading_dot_tolerated(self):
        assert _resolve_doc_export_format(".PDF")[0] == "pdf"
        assert _resolve_doc_export_format(" Docx ")[0] == "docx"

    def test_exact_mime_type_accepted(self):
        for name, (mime, _ext) in GOOGLE_DOC_EXPORT_FORMATS.items():
            assert _resolve_doc_export_format(mime)[0] == name

    def test_aliases(self):
        assert _resolve_doc_export_format("markdown")[0] == "md"
        assert _resolve_doc_export_format("text")[0] == "txt"
        assert _resolve_doc_export_format("htm")[0] == "html"

    def test_unknown_rejected(self):
        assert _resolve_doc_export_format("xlsx") is None
        assert _resolve_doc_export_format("") is None
        assert _resolve_doc_export_format(None) is None

    def test_complete_google_docs_export_set(self):
        # The full set Google lists for Documents -- pin it so a format can
        # never silently drop out of the tool's enum.
        assert set(GOOGLE_DOC_EXPORT_FORMATS) == {
            "pdf", "docx", "odt", "rtf", "txt", "md", "html", "epub", "zip",
        }

    def test_schema_enum_matches_format_table(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        enum = TOOL_CALL_REGISTRY["google_export_doc"]["parameters"]["properties"]["format"]["enum"]
        assert set(enum) == set(GOOGLE_DOC_EXPORT_FORMATS)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_pdf_export_default_filename(self, tmp_path):
        result, req, publish, ws = _export(tmp_path, fmt="pdf")

        assert result["status"] == "success"
        assert result["filename"] == "Q3 Report.pdf"
        assert result["format"] == "pdf"
        assert result["export_mime_type"] == "application/pdf"
        assert result["document_title"] == "Q3 Report"
        assert result["size_bytes"] == len(b"%PDF-1.7 fake")
        assert "get_workspace_file" in result["message"]
        assert (ws / "Q3 Report.pdf").read_bytes() == b"%PDF-1.7 fake"
        publish.assert_called_once_with(USER["id"], "conv-1", None)

        # Two upstream calls: metadata (JSON) then export (raw).
        assert len(req.calls) == 2
        meta_url, meta_kwargs = req.calls[0]
        export_url, export_kwargs = req.calls[1]
        assert meta_url.startswith(f"https://www.googleapis.com/drive/v3/files/{DOC_ID}?")
        assert "fields=name,mimeType" in meta_url
        assert meta_kwargs["raw_response"] is False
        assert export_kwargs["raw_response"] is True
        parsed = urllib.parse.urlparse(export_url)
        assert parsed.path == f"/drive/v3/files/{DOC_ID}/export"
        assert urllib.parse.parse_qs(parsed.query) == {"mimeType": ["application/pdf"]}

    def test_markdown_export_mime_is_url_encoded(self, tmp_path):
        result, req, _publish, ws = _export(
            tmp_path, fmt="docx",
            export=_response(b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
        assert result["filename"] == "Q3 Report.docx"
        export_url = req.calls[1][0]
        assert (
            "mimeType=application%2Fvnd.openxmlformats-officedocument.wordprocessingml.document"
            in export_url
        )

    def test_filename_override_used_verbatim(self, tmp_path):
        result, _req, _publish, ws = _export(tmp_path, fmt="md", filename="spec.md",
                                            export=_response(b"# Hi", "text/markdown"))
        assert result["filename"] == "spec.md"
        assert (ws / "spec.md").read_text() == "# Hi"

    def test_filename_override_sanitized(self, tmp_path):
        result, _req, _publish, ws = _export(
            tmp_path, fmt="txt", filename="../.secret/notes.txt",
            export=_response(b"x", "text/plain"),
        )
        assert result["status"] == "success"
        # Separators become underscores, leading dots stripped, stays in ws.
        assert "/" not in result["filename"]
        assert not result["filename"].startswith(".")
        assert (ws / result["filename"]).exists()
        assert not (tmp_path / ".secret").exists()

    def test_title_with_slash_sanitized(self, tmp_path):
        result, _req, _publish, ws = _export(
            tmp_path, fmt="txt",
            metadata=json.dumps({"name": "2026/Q3 notes", "mimeType": DOC_MIME}),
            export=_response(b"x", "text/plain"),
        )
        assert result["filename"] == "2026_Q3 notes.txt"
        assert (ws / "2026_Q3 notes.txt").exists()

    def test_project_id_forwarded_to_publish(self, tmp_path):
        _result, _req, publish, _ws = _export(tmp_path, project_id="proj-9")
        publish.assert_called_once_with(USER["id"], "conv-1", "proj-9")


# ---------------------------------------------------------------------------
# Rejections / upstream failures
# ---------------------------------------------------------------------------


class TestRejections:
    def test_unknown_format_no_upstream_call(self, tmp_path):
        result, req, publish, ws = _export(tmp_path, fmt="xlsx")
        assert "Unsupported export format" in result["error"]
        assert set(result["supported_formats"]) == set(GOOGLE_DOC_EXPORT_FORMATS)
        assert req.calls == []
        publish.assert_not_called()
        assert not any(ws.iterdir())

    def test_missing_document_id(self, tmp_path):
        req = _fake_requests("{}", None)
        with patch("chat.gemini_api.authed_get._make_authed_request", req):
            result = json.loads(_run(_handle_google_export_doc(USER, "conv-1", "  ", "pdf")))
        assert "document_id is required" in result["error"]
        assert req.calls == []

    def test_regular_file_points_to_download_drive_file(self, tmp_path):
        result, req, publish, ws = _export(
            tmp_path,
            metadata=json.dumps({"name": "scan.pdf", "mimeType": "application/pdf"}),
        )
        assert "not a Google Doc" in result["error"]
        assert "download_drive_file" in result["error"]
        assert DOC_ID in result["error"]
        assert len(req.calls) == 1  # metadata only, no export attempt
        publish.assert_not_called()
        assert not any(ws.iterdir())

    def test_sheet_points_to_sheet_export_tool(self, tmp_path):
        result, req, _publish, _ws = _export(
            tmp_path,
            metadata=json.dumps({"name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"}),
        )
        assert "not a Google Doc" in result["error"]
        assert "download_drive_file" not in result["error"]
        assert "google_export_sheet" in result["error"]
        assert f'"spreadsheet_id": "{DOC_ID}"' in result["error"]
        assert len(req.calls) == 1

    def test_other_workspace_type_not_supported(self, tmp_path):
        result, req, _publish, _ws = _export(
            tmp_path,
            metadata=json.dumps({"name": "Form", "mimeType": "application/vnd.google-apps.form"}),
        )
        assert "not a Google Doc" in result["error"]
        assert "download_drive_file" not in result["error"]
        assert "not supported" in result["error"]
        assert len(req.calls) == 1

    def test_metadata_error_short_circuits(self, tmp_path):
        result, req, publish, ws = _export(
            tmp_path, metadata=json.dumps({"error": "HTTP 404: File not found"}),
        )
        assert "Failed to get Google Doc metadata" in result["error"]
        assert len(req.calls) == 1
        publish.assert_not_called()
        assert not any(ws.iterdir())

    def test_metadata_auth_required_passthrough(self, tmp_path):
        result, _req, _publish, _ws = _export(
            tmp_path, metadata=json.dumps({"error": "google_services_auth_required", "message": "..."}),
        )
        assert "google_services_auth_required" in result["error"]

    def test_export_error_short_circuits_with_hint(self, tmp_path):
        result, req, publish, ws = _export(
            tmp_path, export=json.dumps({"error": "HTTP 403: This file is too large to be exported."}),
        )
        assert "Failed to export Google Doc as pdf" in result["error"]
        assert "too large" in result["error"]
        assert "10 MB" in result["hint"]
        assert len(req.calls) == 2
        publish.assert_not_called()
        assert not any(ws.iterdir())


# ---------------------------------------------------------------------------
# authed_get interaction
# ---------------------------------------------------------------------------


class TestAuthedGetInteraction:
    def test_export_path_admitted_by_drive_allow_list(self):
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        patterns = _SERVICE_REGISTRY["www.googleapis.com/drive/v3"]["_allowed_endpoints"]
        assert any(p.match(f"/drive/v3/files/{DOC_ID}/export") for p in patterns)
        # The :-verb and nested paths stay out.
        assert not any(p.match(f"/drive/v3/files/{DOC_ID}/export/extra") for p in patterns)
        assert not any(p.match(f"/drive/v3/files/{DOC_ID}/copy") for p in patterns)

    def test_authed_get_blocks_export_without_output_file(self):
        from chat.gemini_api.authed_get import handle_authed_get
        make = AsyncMock()
        with patch("chat.gemini_api.authed_get._make_authed_request", make):
            result = json.loads(_run(handle_authed_get(
                f"https://www.googleapis.com/drive/v3/files/{DOC_ID}/export?mimeType=application%2Fpdf",
                user=USER, conversation_id="conv-1",
            )))
        assert "google_export_doc" in result["error"]
        assert "google_export_sheet" in result["error"]
        assert "google_export_slides" in result["error"]
        make.assert_not_called()

    def test_authed_get_allows_export_with_output_file(self, tmp_path):
        from chat.gemini_api.authed_get import handle_authed_get
        resp = _response(b"%PDF", "application/pdf")
        make = AsyncMock(return_value=resp)
        with patch("chat.gemini_api.authed_get._make_authed_request", make), \
             _patch_workspace(tmp_path), \
             patch("chat.gemini_api.tool_handlers.drive._publish_file_list_changed", MagicMock()):
            result = json.loads(_run(handle_authed_get(
                f"https://www.googleapis.com/drive/v3/files/{DOC_ID}/export?mimeType=application%2Fpdf",
                user=USER, conversation_id="conv-1", output_file="doc.pdf",
            )))
        assert "error" not in result
        assert result["path"] == ".responses/doc.pdf"
        make.assert_awaited_once()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestWiring:
    def test_registered_in_registry_and_dispatch_table(self):
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        assert "google_export_doc" in TOOL_CALL_REGISTRY
        assert "google_export_doc" in TOOL_CALL_HANDLERS
        schema = TOOL_CALL_REGISTRY["google_export_doc"]
        assert schema["parameters"]["required"] == ["document_id", "format"]

    def test_not_in_public_or_script_allow_lists(self):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST
        assert "google_export_doc" not in PUBLIC_TOOL_CALL_ALLOWLIST
        assert "google_export_doc" not in SCRIPT_TOOL_CALL_ALLOWLIST

    def test_dispatch_forwards_arguments(self):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        handler = AsyncMock(return_value=json.dumps({"status": "success"}))
        with patch("chat.gemini_api.tool_dispatch._handle_google_export_doc", handler):
            result, _parts = _run(_dispatch_tool_call(
                app=None, provider=None, user=USER, conversation_id="conv-1",
                timezone="UTC", tool_name="tool_call",
                args={"tool_name": "google_export_doc",
                      "arguments": {"document_id": DOC_ID, "format": "md",
                                    "filename": "x.md", "intent_message": "Export"}},
            ))
        assert json.loads(result)["status"] == "success"
        handler.assert_awaited_once_with(
            USER, "conv-1", DOC_ID, "md", filename="x.md", project_id=None,
        )

    def test_docs_skill_mentions_export_tool(self):
        from api.docs import get_instructions
        text = get_instructions("http://localhost")
        assert "google_export_doc" in text
        for name, (mime, _ext) in GOOGLE_DOC_EXPORT_FORMATS.items():
            assert f"`{name}`" in text
            assert mime in text
