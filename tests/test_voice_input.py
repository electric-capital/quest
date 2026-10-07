"""Tests for the composer voice-input feature (server-side transcription).

Covers the ``voice_input`` feature gate (registry entry, admin availability
check refusing to enable the gate without a Gemini Vertex model), the
transcription helpers in chat/transcription.py (MIME normalization, magic
sniffing, model choice, failure mapping) and the ``POST /transcribe`` route
(gate 403, validation 400s, availability 503, provider failure 502, success).

No database, no network: the model universe and the provider call are
monkeypatched.
"""

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

import config.feature_gates as fg
from chat import transcription
from chat.routes import transcribe as transcribe_routes


def _run(coro):
    return asyncio.run(coro)


ADMIN_USER = {"id": 1, "email": "admin@example.com"}
USER = {"id": 2, "email": "user@example.com"}

WEBM_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
OGG_HEAD = b"OggS" + b"\x00" * 64
MP4_HEAD = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
WAV_HEAD = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64


@pytest.fixture
def gates_file(tmp_path, monkeypatch):
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.fixture
def gate_open(gates_file):
    fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
    return gates_file


def _upload(data: bytes, content_type: str | None, filename="clip.webm") -> UploadFile:
    headers = Headers({"content-type": content_type}) if content_type else Headers()
    return UploadFile(BytesIO(data), filename=filename, headers=headers)


def _models(monkeypatch, available: list[str], configured: list[str] | None = None):
    """Pin the model universe transcription chooses from."""
    monkeypatch.setattr(
        "chat.llm.config.get_available_models", lambda: list(available)
    )
    monkeypatch.setattr(
        "chat.llm.config.get_configured_models",
        lambda: list(configured if configured is not None else available),
    )


class TestRegistry:
    def test_registered_off_by_default_and_per_user(self, gates_file):
        assert fg.FEATURE_VOICE_INPUT in fg.KNOWN_FEATURES
        assert fg.FEATURE_VOICE_INPUT in fg.PER_USER_ACCESS_FEATURES
        assert fg.FEATURE_VOICE_INPUT in fg.FEATURE_LABELS
        assert not fg.is_feature_enabled(fg.FEATURE_VOICE_INPUT)
        assert fg.FEATURE_VOICE_INPUT not in fg.enabled_features(USER["email"])

    def test_per_user_access(self, gate_open):
        fg.set_feature_allowed_users(fg.FEATURE_VOICE_INPUT, ["User@Example.com"])
        assert fg.is_feature_enabled_for_user(fg.FEATURE_VOICE_INPUT, USER["email"])
        assert not fg.is_feature_enabled_for_user(
            fg.FEATURE_VOICE_INPUT, ADMIN_USER["email"]
        )


class TestMimeHelpers:
    @pytest.mark.parametrize("declared,expected", [
        ("audio/webm;codecs=opus", "audio/webm"),
        ("AUDIO/WEBM", "audio/webm"),
        ("video/webm", "audio/webm"),
        ("audio/x-m4a", "audio/mp4"),
        ("audio/mp3", "audio/mpeg"),
        ("audio/x-wav", "audio/wav"),
        (None, ""),
        ("", ""),
    ])
    def test_normalize(self, declared, expected):
        assert transcription.normalize_audio_mime(declared) == expected

    @pytest.mark.parametrize("data,expected", [
        (WEBM_HEAD, "audio/webm"),
        (OGG_HEAD, "audio/ogg"),
        (MP4_HEAD, "audio/mp4"),
        (WAV_HEAD, "audio/wav"),
        (b"fLaC" + b"\x00" * 16, "audio/flac"),
        (b"ID3\x04\x00" + b"\x00" * 16, "audio/mpeg"),
        (b"\xff\xfb\x90\x00" + b"\x00" * 16, "audio/mpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, None),
        (b"hello world, not audio at all", None),
        (b"", None),
    ])
    def test_sniff(self, data, expected):
        assert transcription.sniff_audio_mime(data) == expected

    def test_every_sniffable_type_is_allowed(self):
        for data in (WEBM_HEAD, OGG_HEAD, MP4_HEAD, WAV_HEAD):
            assert transcription.sniff_audio_mime(data) in transcription.ALLOWED_AUDIO_MIME_TYPES

    def test_size_cap_under_vertex_inline_limit(self):
        from chat.llm.file_limits import GEMINI_VERTEX_MAX_ATTACH_BYTES

        assert transcription.MAX_AUDIO_BYTES <= GEMINI_VERTEX_MAX_ATTACH_BYTES


class TestModelChoice:
    def test_prefers_listed_flash_models(self, monkeypatch):
        _models(monkeypatch, [
            "claude-haiku-4.5", "gemini-3.5-flash-lite", "gemini-3.8-flash",
        ])
        assert transcription.transcription_model() == "gemini-3.8-flash"
        assert transcription.transcription_availability() == (True, None)

    def test_falls_back_to_any_gemini_model(self, monkeypatch):
        # Nothing from the preference list, but a Gemini model exists.
        _models(monkeypatch, ["claude-haiku-4.5", "gemini-3.6-flash"])
        assert transcription.transcription_model() == "gemini-3.6-flash"

    def test_ignores_non_gemini_and_instance_models(self, monkeypatch):
        _models(monkeypatch, ["claude-haiku-4.5", "openrouter:google/gemini-3.8-flash"])
        assert transcription.transcription_model() is None
        available, reason = transcription.transcription_availability()
        assert available is False
        assert "Gemini" in reason

    def test_unhealthy_models_fall_back_to_configured(self, monkeypatch):
        # Every Gemini model has a failing health verdict (absent from
        # available) but is still configured: keep transcribing rather than
        # switching the feature off on a stale verdict.
        _models(
            monkeypatch,
            available=["claude-haiku-4.5"],
            configured=["claude-haiku-4.5", "gemini-3.7-flash"],
        )
        assert transcription.transcription_model() == "gemini-3.7-flash"
        assert transcription.transcription_availability() == (True, None)


class TestTranscribeHelper:
    def test_unavailable_without_gemini(self, monkeypatch):
        _models(monkeypatch, ["claude-haiku-4.5"])
        with pytest.raises(transcription.TranscriptionUnavailable):
            _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))

    def test_success_uses_gemini_provider(self, monkeypatch):
        _models(monkeypatch, ["gemini-3.8-flash"])
        provider = type("P", (), {})()
        provider.transcribe_audio = AsyncMock(return_value="  hello world  ")
        with patch("chat.llm.config.get_provider_instance", return_value=provider) as gpi:
            result = _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))
        gpi.assert_called_once_with("gemini")
        provider.transcribe_audio.assert_awaited_once_with(
            "gemini-3.8-flash", WEBM_HEAD, "audio/webm"
        )
        assert result["text"] == "hello world"
        assert result["model"] == "gemini-3.8-flash"
        assert isinstance(result["duration_ms"], int)

    def test_provider_error_maps_to_failed(self, monkeypatch):
        _models(monkeypatch, ["gemini-3.8-flash"])
        provider = type("P", (), {})()
        provider.transcribe_audio = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("chat.llm.config.get_provider_instance", return_value=provider):
            with pytest.raises(transcription.TranscriptionFailed) as exc_info:
                _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))
        # The provider's message never leaks into the client-facing error.
        assert "boom" not in str(exc_info.value)

    def test_transient_error_is_retried(self, monkeypatch):
        _models(monkeypatch, ["gemini-3.8-flash"])
        monkeypatch.setattr(transcription, "TRANSCRIBE_RETRY_BACKOFF_SECONDS", (0.0,))

        class RateLimited(Exception):
            code = 429

        provider = type("P", (), {})()
        provider.transcribe_audio = AsyncMock(
            side_effect=[RateLimited("429 RESOURCE_EXHAUSTED"), "second try"]
        )
        with patch("chat.llm.config.get_provider_instance", return_value=provider):
            result = _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))
        assert result["text"] == "second try"
        assert provider.transcribe_audio.await_count == 2

    def test_retries_are_bounded_and_non_transient_not_retried(self, monkeypatch):
        _models(monkeypatch, ["gemini-3.8-flash"])
        monkeypatch.setattr(transcription, "TRANSCRIBE_RETRY_BACKOFF_SECONDS", (0.0,))

        class RateLimited(Exception):
            code = 429

        class BadRequest(Exception):
            code = 400

        provider = type("P", (), {})()
        provider.transcribe_audio = AsyncMock(side_effect=RateLimited("429"))
        with patch("chat.llm.config.get_provider_instance", return_value=provider):
            with pytest.raises(transcription.TranscriptionFailed):
                _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))
        assert provider.transcribe_audio.await_count == transcription.TRANSCRIBE_MAX_ATTEMPTS

        provider.transcribe_audio = AsyncMock(side_effect=BadRequest("400 bad"))
        with patch("chat.llm.config.get_provider_instance", return_value=provider):
            with pytest.raises(transcription.TranscriptionFailed):
                _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))
        assert provider.transcribe_audio.await_count == 1

    def test_timeout_maps_to_failed(self, monkeypatch):
        _models(monkeypatch, ["gemini-3.8-flash"])
        monkeypatch.setattr(transcription, "TRANSCRIBE_TIMEOUT_SECONDS", 0.01)

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(1)
            return "late"

        provider = type("P", (), {})()
        provider.transcribe_audio = slow
        with patch("chat.llm.config.get_provider_instance", return_value=provider):
            with pytest.raises(transcription.TranscriptionFailed):
                _run(transcription.transcribe_audio(WEBM_HEAD, "audio/webm"))


class TestTranscribeRoute:
    def _call(self, data, content_type, user=USER):
        return _run(transcribe_routes.transcribe_composer_audio(
            audio=_upload(data, content_type), user=user,
        ))

    def _expect(self, status, error, data, content_type, user=USER):
        with pytest.raises(HTTPException) as exc_info:
            self._call(data, content_type, user=user)
        assert exc_info.value.status_code == status
        assert exc_info.value.detail["error"] == error

    def test_gate_closed_403(self, gates_file):
        with patch.object(transcription, "transcribe_audio", AsyncMock()) as ta:
            self._expect(403, "voice_input_disabled", WEBM_HEAD, "audio/webm")
        ta.assert_not_awaited()

    def test_gate_open_for_other_user_only_403(self, gate_open):
        fg.set_feature_allowed_users(fg.FEATURE_VOICE_INPUT, [ADMIN_USER["email"]])
        self._expect(403, "voice_input_disabled", WEBM_HEAD, "audio/webm", user=USER)

    def test_empty_400(self, gate_open):
        self._expect(400, "empty_audio", b"", "audio/webm")

    def test_too_large_400(self, gate_open, monkeypatch):
        monkeypatch.setattr(transcribe_routes, "MAX_AUDIO_BYTES", 32)
        self._expect(400, "audio_too_large", WEBM_HEAD, "audio/webm")

    def test_unrecognized_bytes_400(self, gate_open):
        self._expect(400, "unsupported_audio", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "audio/webm")

    def test_declared_non_audio_type_400(self, gate_open):
        # Bytes look like WebM but the client claims text/plain: refuse.
        self._expect(400, "unsupported_audio", WEBM_HEAD, "text/plain")

    def test_unavailable_503(self, gate_open):
        with patch.object(
            transcribe_routes, "transcribe_audio",
            AsyncMock(side_effect=transcription.TranscriptionUnavailable("no model")),
        ):
            self._expect(503, "transcription_unavailable", WEBM_HEAD, "audio/webm")

    def test_provider_failure_502(self, gate_open):
        with patch.object(
            transcribe_routes, "transcribe_audio",
            AsyncMock(side_effect=transcription.TranscriptionFailed("Transcription failed")),
        ):
            self._expect(502, "transcription_failed", WEBM_HEAD, "audio/webm")

    def test_success_strips_codec_params_and_uses_sniffed_type(self, gate_open):
        mock = AsyncMock(return_value={"text": "hi there", "model": "gemini-3.8-flash", "duration_ms": 5})
        with patch.object(transcribe_routes, "transcribe_audio", mock):
            result = self._call(WEBM_HEAD, "audio/webm;codecs=opus")
        mock.assert_awaited_once_with(WEBM_HEAD, "audio/webm")
        assert result == {"text": "hi there", "model": "gemini-3.8-flash"}

    def test_missing_content_type_ok_when_bytes_recognizable(self, gate_open):
        mock = AsyncMock(return_value={"text": "ok", "model": "m", "duration_ms": 1})
        with patch.object(transcribe_routes, "transcribe_audio", mock):
            self._call(MP4_HEAD, None)
        mock.assert_awaited_once_with(MP4_HEAD, "audio/mp4")

    def test_allowed_declared_type_disagreeing_with_bytes_trusts_bytes(self, gate_open):
        # Safari labels some recordings audio/mp4 while emitting WebM-like
        # bytes in edge cases; both are accepted containers, so the sniffed
        # type wins and the request proceeds.
        mock = AsyncMock(return_value={"text": "ok", "model": "m", "duration_ms": 1})
        with patch.object(transcribe_routes, "transcribe_audio", mock):
            self._call(OGG_HEAD, "audio/webm")
        mock.assert_awaited_once_with(OGG_HEAD, "audio/ogg")


class TestAdminGateAvailability:
    @pytest.fixture
    def admin_routes(self, monkeypatch, gates_file):
        from chat.routes import admin

        monkeypatch.setattr(admin, "is_admin", lambda email: email == ADMIN_USER["email"])
        return admin

    def test_list_reports_availability(self, admin_routes, monkeypatch):
        _models(monkeypatch, ["claude-haiku-4.5"])
        listed = _run(admin_routes.admin_list_feature_gates(user=ADMIN_USER))
        by_key = {f["feature"]: f for f in listed["features"]}
        voice = by_key[fg.FEATURE_VOICE_INPUT]
        assert voice["available"] is False
        assert "Gemini" in voice["unavailable_reason"]
        assert voice["supports_user_access"] is True
        # Gates without a server dependency are always available.
        assert by_key[fg.FEATURE_DOCS]["available"] is True
        assert by_key[fg.FEATURE_DOCS]["unavailable_reason"] is None

    def test_enable_refused_without_vertex(self, admin_routes, monkeypatch):
        _models(monkeypatch, ["claude-haiku-4.5"])
        with pytest.raises(HTTPException) as exc_info:
            _run(admin_routes.admin_update_feature_gate(
                fg.FEATURE_VOICE_INPUT,
                admin_routes.FeatureGateUpdate(enabled=True),
                user=ADMIN_USER,
            ))
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "feature_unavailable"
        assert not fg.is_feature_enabled(fg.FEATURE_VOICE_INPUT)

    def test_enable_allowed_with_vertex(self, admin_routes, monkeypatch):
        _models(monkeypatch, ["gemini-3.8-flash"])
        updated = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_VOICE_INPUT,
            admin_routes.FeatureGateUpdate(enabled=True),
            user=ADMIN_USER,
        ))
        assert updated["enabled"] is True
        assert updated["available"] is True
        assert fg.is_feature_enabled(fg.FEATURE_VOICE_INPUT)

    def test_disable_always_allowed(self, admin_routes, monkeypatch, gates_file):
        # Enabled while Vertex was configured, then Vertex went away: the
        # admin can still turn the gate off.
        fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
        _models(monkeypatch, ["claude-haiku-4.5"])
        updated = _run(admin_routes.admin_update_feature_gate(
            fg.FEATURE_VOICE_INPUT,
            admin_routes.FeatureGateUpdate(enabled=False),
            user=ADMIN_USER,
        ))
        assert updated["enabled"] is False
        assert updated["available"] is False


class TestGeminiResponseParsing:
    def test_parse(self):
        from chat.llm.gemini_provider import _parse_transcription_response as parse

        assert parse('{"speech_detected": true, "transcript": "  hi there "}') == "hi there"
        assert parse('{"speech_detected": false, "transcript": "made up"}') == ""
        assert parse('{"speech_detected": true, "transcript": 5}') == ""
        assert parse("not json") == ""
        assert parse("") == ""
        assert parse(None) == ""
        assert parse("[1, 2]") == ""
