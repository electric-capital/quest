"""Tests for pre-flight attachment size limits (plan 00126).

Covers:

* ``chat/llm/file_limits.py`` -- per-backend attachment caps and the
  ``get_attach_limit_for_model`` resolver (per-model caps, image min,
  conservative fallback for unknown/empty models, derivation sanity).
* ``_handle_get_workspace_file`` pre-flight: oversized binary/text files
  return a structured tool error (with actionable suggestion) BEFORE any
  ``upload_file`` call or inline read, so the oversized bytes never reach
  session history; under-limit files proceed unchanged.
* ``_handle_load_gmail_attachment`` pre-flight: same gate on fetched
  attachment bytes.
* ``AnthropicProvider.upload_file`` defense-in-depth: oversized PDFs (and
  images, pre-existing behavior) return ``None`` instead of building an
  oversized base64 block.
* Dispatch threading: the ``model`` passed to ``_dispatch_tool_call``
  (e.g. a sub-agent's model) decides which cap applies.

No DB, LLM, or network access -- workspace and sidecar paths are patched
to pytest tmp_path; large files are created sparse via ``truncate``.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.llm.file_limits import (
    ANTHROPIC_IMAGE_MAX_BYTES,
    ANTHROPIC_VERTEX_MAX_ATTACH_BYTES,
    ANTHROPIC_VERTEX_MAX_REQUEST_BYTES,
    GEMINI_VERTEX_MAX_ATTACH_BYTES,
    get_attach_limit_for_model,
)
from chat.gemini_api.constants import _TEXT_INLINE_LIMIT
from chat.gemini_api.tool_handlers import (
    _handle_get_workspace_file,
    _handle_load_gmail_attachment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


USER_ID = 42
CONV_ID = "conv-size-1"

ANTHROPIC_MODEL = "claude-opus-4-8"
GEMINI_VERTEX_MODEL = "gemini-3.5-flash"


def _patch_workspace_dir(tmp_path: Path):
    """Patch ``conversation_workspace_dir`` so handlers operate under ``tmp_path``."""
    async def fake_workspace(*args, **kwargs):
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    return patch(
        "chat.gemini_api.tool_handlers.workspace.conversation_workspace_dir",
        new=fake_workspace,
    )


def _chat_storage():
    """Resolve ChatStorage from the live module at call time.

    Some test modules (e.g. test_storage_seq.py) reload ``chat.storage``,
    replacing the class object; an import-time binding would patch/read a
    stale class whose method bodies resolve the reloaded one.
    """
    import chat.storage as storage_mod
    return storage_mod.ChatStorage


def _patch_conversation_dir(tmp_path: Path):
    """Patch ``ChatStorage._get_conversation_dir`` so the
    ``workspace_reads.json`` sidecar lands under ``tmp_path``."""
    return patch.object(
        _chat_storage(),
        "_get_conversation_dir",
        new=lambda conversation_id: tmp_path,
    )


def _read_paths() -> list[str]:
    return _chat_storage().get_workspace_read_paths(CONV_ID)


@pytest.fixture()
def workspace(tmp_path):
    """Workspace dir + sidecar both rooted at tmp_path; yields the
    workspace directory Path."""
    with _patch_workspace_dir(tmp_path), _patch_conversation_dir(tmp_path):
        yield tmp_path / "workspace"


def _make_sparse_file(workspace: Path, rel_path: str, size: int) -> Path:
    """Create a file of exactly ``size`` bytes without allocating memory."""
    file_path = workspace / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "wb") as f:
        f.truncate(size)
    return file_path


def _mock_provider(upload_return=None):
    """A provider mock with an AsyncMock upload_file and passthrough parts."""
    provider = MagicMock()
    provider.upload_file = AsyncMock(return_value=upload_return)
    provider.make_file_part = MagicMock(side_effect=lambda ref: ref)
    return provider


def _uploaded_ref(mime_type: str = "application/pdf"):
    ref = MagicMock()
    ref.mime_type = mime_type
    return ref


def _get_file(provider, path, model):
    return _run(_handle_get_workspace_file(
        provider, USER_ID, CONV_ID, path, model=model,
    ))


# ---------------------------------------------------------------------------
# Resolver unit tests
# ---------------------------------------------------------------------------


class TestResolver:
    def test_anthropic_model(self):
        limit, label = get_attach_limit_for_model(ANTHROPIC_MODEL, "application/pdf")
        assert limit == ANTHROPIC_VERTEX_MAX_ATTACH_BYTES
        assert label == "Anthropic on Vertex AI"

    def test_anthropic_image_uses_min_with_image_cap(self):
        limit, label = get_attach_limit_for_model(ANTHROPIC_MODEL, "image/png")
        assert limit == min(
            ANTHROPIC_VERTEX_MAX_ATTACH_BYTES, ANTHROPIC_IMAGE_MAX_BYTES,
        )
        assert limit == ANTHROPIC_IMAGE_MAX_BYTES
        assert label == "Anthropic on Vertex AI"

    def test_gemini_vertex_model(self):
        limit, label = get_attach_limit_for_model(
            GEMINI_VERTEX_MODEL, "application/pdf",
        )
        assert limit == GEMINI_VERTEX_MAX_ATTACH_BYTES
        assert label == "Gemini (Vertex AI)"

    @pytest.mark.parametrize("model", ["totally-bogus-model", ""])
    def test_unknown_or_empty_model_falls_back_to_smallest_cap(self, model):
        limit, label = get_attach_limit_for_model(model, "application/pdf")
        assert limit == min(
            ANTHROPIC_VERTEX_MAX_ATTACH_BYTES,
            GEMINI_VERTEX_MAX_ATTACH_BYTES,
        )
        assert label == "unknown model backend"

    def test_resolver_never_raises(self):
        # Even pathological inputs resolve without raising.
        limit, _ = get_attach_limit_for_model("", "")
        assert limit > 0

    def test_anthropic_derivation_sanity(self):
        # The effective cap must be below the raw base64 budget
        # (30 MB * 3/4), i.e. the safety factor must actually bite.
        assert ANTHROPIC_VERTEX_MAX_ATTACH_BYTES < (
            ANTHROPIC_VERTEX_MAX_REQUEST_BYTES * 3 // 4
        )
        assert ANTHROPIC_VERTEX_MAX_ATTACH_BYTES > 0

    def test_gemini_provider_constant_is_unified(self):
        from chat.llm.gemini_provider import _GEMINI_VERTEX_INLINE_MAX_SIZE
        assert _GEMINI_VERTEX_INLINE_MAX_SIZE == GEMINI_VERTEX_MAX_ATTACH_BYTES


# ---------------------------------------------------------------------------
# get_workspace_file pre-flight -- PDFs
# ---------------------------------------------------------------------------


class TestGetWorkspaceFilePdf:
    def test_oversized_pdf_anthropic_rejected_before_upload(self, workspace):
        _make_sparse_file(
            workspace, "report.pdf", ANTHROPIC_VERTEX_MAX_ATTACH_BYTES + 1,
        )
        provider = _mock_provider()

        result, extra_parts = _get_file(provider, "report.pdf", ANTHROPIC_MODEL)

        parsed = json.loads(result)
        assert "too large to attach" in parsed["error"]
        assert parsed["path"] == "report.pdf"
        assert parsed["size_bytes"] == ANTHROPIC_VERTEX_MAX_ATTACH_BYTES + 1
        assert parsed["limit_bytes"] == ANTHROPIC_VERTEX_MAX_ATTACH_BYTES
        assert parsed["mime_type"] == "application/pdf"
        assert parsed["model"] == ANTHROPIC_MODEL
        assert parsed["backend"] == "Anthropic on Vertex AI"
        assert "run_python" in parsed["suggestion"]
        assert "pypdf" in parsed["suggestion"]
        assert extra_parts == []
        provider.upload_file.assert_not_awaited()
        # The model never saw the contents: the edit gate must not be marked.
        assert _read_paths() == []

    def test_under_limit_pdf_uploads_and_marks_read(self, workspace):
        _make_sparse_file(workspace, "small.pdf", 1024)
        provider = _mock_provider(upload_return=_uploaded_ref())

        result, extra_parts = _get_file(provider, "small.pdf", ANTHROPIC_MODEL)

        parsed = json.loads(result)
        assert "error" not in parsed
        assert parsed["uploaded"] is True
        assert len(extra_parts) == 1
        provider.upload_file.assert_awaited_once()
        assert _read_paths() == ["small.pdf", "chat://small.pdf"]

    def test_file_exactly_at_limit_passes(self, workspace):
        # Strict > comparison: a file of exactly the cap is allowed.
        _make_sparse_file(
            workspace, "exact.pdf", ANTHROPIC_VERTEX_MAX_ATTACH_BYTES,
        )
        provider = _mock_provider(upload_return=_uploaded_ref())

        result, extra_parts = _get_file(provider, "exact.pdf", ANTHROPIC_MODEL)

        assert "error" not in json.loads(result)
        provider.upload_file.assert_awaited_once()

    def test_vertex_gemini_uses_vertex_cap(self, workspace):
        _make_sparse_file(
            workspace, "big.pdf", GEMINI_VERTEX_MAX_ATTACH_BYTES + 1,
        )
        provider = _mock_provider()

        result, _ = _get_file(provider, "big.pdf", GEMINI_VERTEX_MODEL)

        parsed = json.loads(result)
        assert "too large to attach" in parsed["error"]
        assert parsed["limit_bytes"] == GEMINI_VERTEX_MAX_ATTACH_BYTES
        assert parsed["backend"] == "Gemini (Vertex AI)"
        provider.upload_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_workspace_file pre-flight -- text files
# ---------------------------------------------------------------------------


class TestGetWorkspaceFileText:
    def test_oversized_text_file_rejected_not_inlined(self, workspace):
        _make_sparse_file(
            workspace, "big.csv", ANTHROPIC_VERTEX_MAX_ATTACH_BYTES + 1,
        )
        # Provider whose upload_file returns None (Anthropic behavior for
        # text) -- previously this fell through to an unconditional inline
        # read; now the pre-flight rejects first.
        provider = _mock_provider(upload_return=None)

        result, extra_parts = _get_file(provider, "big.csv", ANTHROPIC_MODEL)

        parsed = json.loads(result)
        assert "too large to attach" in parsed["error"]
        assert "content" not in parsed
        assert parsed["limit_bytes"] == ANTHROPIC_VERTEX_MAX_ATTACH_BYTES
        assert "run_python" in parsed["suggestion"]
        assert extra_parts == []
        provider.upload_file.assert_not_awaited()
        assert _read_paths() == []

    def test_medium_text_file_still_falls_back_to_inline(self, workspace):
        # Over _TEXT_INLINE_LIMIT (100 KB) but under the cap: the existing
        # large-text inline fallback is preserved.
        size = 2 * _TEXT_INLINE_LIMIT
        file_path = workspace / "medium.csv"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("x" * size, encoding="utf-8")
        provider = _mock_provider(upload_return=None)

        result, extra_parts = _get_file(provider, "medium.csv", ANTHROPIC_MODEL)

        parsed = json.loads(result)
        assert "error" not in parsed
        assert parsed["content"] == "x" * size
        assert "inline" in parsed["note"]
        assert extra_parts == []
        assert _read_paths() == ["medium.csv", "chat://medium.csv"]

    def test_small_text_file_untouched(self, workspace):
        workspace.mkdir(parents=True, exist_ok=True)
        file_path = workspace / "notes.txt"
        file_path.write_text("hello", encoding="utf-8")
        provider = _mock_provider()

        result, extra_parts = _get_file(provider, "notes.txt", ANTHROPIC_MODEL)

        parsed = json.loads(result)
        assert parsed["content"] == "hello"
        assert extra_parts == []
        provider.upload_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# load_gmail_attachment pre-flight
# ---------------------------------------------------------------------------


class TestLoadGmailAttachment:
    def _resolve_patch(self, data: bytes, filename="big.pdf",
                       mime_type="application/pdf"):
        return patch(
            "api.gmail._resolve_gmail_attachment",
            new=AsyncMock(return_value={
                "data": data,
                "filename": filename,
                "mime_type": mime_type,
            }),
        )

    def test_oversized_attachment_rejected_before_upload(self):
        data = bytes(ANTHROPIC_VERTEX_MAX_ATTACH_BYTES + 1)
        provider = _mock_provider()

        with self._resolve_patch(data):
            result, extra_parts = _run(_handle_load_gmail_attachment(
                provider, {"id": USER_ID}, "msg-1", "att-1",
                filename="big.pdf", mime_type="application/pdf",
                model=ANTHROPIC_MODEL,
            ))

        parsed = json.loads(result)
        assert "too large to attach" in parsed["error"]
        assert parsed["filename"] == "big.pdf"
        assert parsed["size_bytes"] == len(data)
        assert parsed["limit_bytes"] == ANTHROPIC_VERTEX_MAX_ATTACH_BYTES
        assert parsed["backend"] == "Anthropic on Vertex AI"
        assert extra_parts == []
        provider.upload_file.assert_not_awaited()

    def test_under_limit_attachment_uploads(self):
        data = b"%PDF-1.4 small"
        provider = _mock_provider(upload_return=_uploaded_ref())

        with self._resolve_patch(data):
            result, extra_parts = _run(_handle_load_gmail_attachment(
                provider, {"id": USER_ID}, "msg-1", "att-1",
                filename="small.pdf", mime_type="application/pdf",
                model=ANTHROPIC_MODEL,
            ))

        parsed = json.loads(result)
        assert "error" not in parsed
        assert parsed["uploaded"] is True
        assert len(extra_parts) == 1
        provider.upload_file.assert_awaited_once()


# ---------------------------------------------------------------------------
# AnthropicProvider.upload_file backstop
# ---------------------------------------------------------------------------


class TestAnthropicProviderBackstop:
    def _upload(self, provider, path, mime_type):
        return _run(provider.upload_file(
            file_path=str(path), mime_type=mime_type, model=ANTHROPIC_MODEL,
        ))

    def test_oversized_pdf_returns_none(self, tmp_path):
        from chat.llm.anthropic_provider import AnthropicProvider

        path = tmp_path / "big.pdf"
        with open(path, "wb") as f:
            f.truncate(ANTHROPIC_VERTEX_MAX_ATTACH_BYTES + 1)

        provider = AnthropicProvider()
        assert self._upload(provider, path, "application/pdf") is None

    def test_small_pdf_returns_document_block(self, tmp_path):
        from chat.llm.anthropic_provider import AnthropicProvider

        path = tmp_path / "small.pdf"
        path.write_bytes(b"%PDF-1.4 tiny")

        provider = AnthropicProvider()
        block = self._upload(provider, path, "application/pdf")
        assert block["type"] == "document"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "application/pdf"

    def test_oversized_image_returns_none(self, tmp_path):
        # Pre-existing image cap retained.
        from chat.llm.anthropic_provider import AnthropicProvider

        path = tmp_path / "big.png"
        with open(path, "wb") as f:
            f.truncate(ANTHROPIC_IMAGE_MAX_BYTES + 1)

        provider = AnthropicProvider()
        assert self._upload(provider, path, "image/png") is None

    def test_unsupported_mime_returns_none(self, tmp_path):
        from chat.llm.anthropic_provider import AnthropicProvider

        path = tmp_path / "data.bin"
        path.write_bytes(b"\x00\x01")

        provider = AnthropicProvider()
        assert self._upload(provider, path, "application/octet-stream") is None


# ---------------------------------------------------------------------------
# Dispatch threading: the passed (sub-agent) model decides the cap
# ---------------------------------------------------------------------------


class TestDispatchThreading:
    def test_sub_agent_model_cap_decides_outcome(self, workspace):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call

        # 10 MB PDF: over the Gemini-Vertex inline cap (7 MB), under the
        # Anthropic effective cap (~15.75 MB) -- so the outcome proves
        # which model's cap was applied.
        _make_sparse_file(workspace, "report.pdf", 10 * 1024 * 1024)

        def dispatch(model):
            provider = _mock_provider(upload_return=_uploaded_ref())
            result, extra_parts = _run(_dispatch_tool_call(
                app=MagicMock(),
                provider=provider,
                user={"id": USER_ID, "email": "u@example.com"},
                conversation_id=CONV_ID,
                timezone="UTC",
                tool_name="tool_call",
                args={
                    "tool_name": "get_workspace_file",
                    "arguments": {"path": "report.pdf"},
                },
                model=model,
            ))
            return json.loads(result), extra_parts, provider

        # Gemini sub-agent model: rejected with the Gemini-Vertex cap.
        parsed, extra_parts, provider = dispatch(GEMINI_VERTEX_MODEL)
        assert "too large to attach" in parsed["error"]
        assert parsed["limit_bytes"] == GEMINI_VERTEX_MAX_ATTACH_BYTES
        assert extra_parts == []
        provider.upload_file.assert_not_awaited()

        # Anthropic sub-agent model: same file passes pre-flight.
        parsed, extra_parts, provider = dispatch(ANTHROPIC_MODEL)
        assert "error" not in parsed
        provider.upload_file.assert_awaited_once()
