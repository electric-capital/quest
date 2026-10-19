"""Tests for live dictation (chat/live_transcription.py + the live socket).

Covers the ``local_asr`` config section and availability verdicts, the PCM
and energy helpers, the streaming session's segmentation and event
sequence against a fake engine (synthetic tone bursts stand in for speech,
so no model, torch or network is involved), the combined ``voice_input``
availability the admin gate check uses, and ``WS /app/api/transcribe/live``
through Starlette's test client (auth close, gate refusal, unavailable
refusal, the busy cap, a full partial / final / done exchange).
"""

import asyncio
import json
from typing import Optional

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config.feature_gates as fg
from chat import live_transcription as lt
from chat import transcription
from chat.routes import transcribe_live as live_routes

SR = lt.SAMPLE_RATE
USER = {"id": 2, "email": "user@example.com"}


def _run(coro):
    return asyncio.run(coro)


def tone(seconds: float, amplitude: float = 0.3, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float, amplitude: float = 0.0005) -> np.ndarray:
    rng = np.random.default_rng(0)
    return (amplitude * rng.standard_normal(int(seconds * SR))).astype(np.float32)


def pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


class FakeEngine:
    """Decodes audio by its length: one word per second, after a language tag.

    Records every decode (audio seconds, prefix, language) so tests can
    check the cadence and the prefix rollback.
    """

    def __init__(self, fail_after: Optional[int] = None):
        self.calls: list[tuple[float, str, Optional[str]]] = []
        self.status = "ready"
        self.fail_after = fail_after

    async def ensure_loaded(self):
        self.status = "ready"

    def normalize_language(self, language):
        if not language:
            return None
        if language.lower() == "english":
            return "English"
        raise ValueError(f"unsupported language: {language}")

    def rollback_prefix(self, raw: str, tokens: int = lt.UNFIXED_TOKENS) -> str:
        words = raw.split(" ")
        return " ".join(words[:-1]) if len(words) > 1 else ""

    def parse_output(self, raw: str, language):
        if raw.startswith("language English<asr_text>"):
            return "English", raw[len("language English<asr_text>"):]
        return "", raw

    async def decode(self, audio: np.ndarray, prefix: str, language) -> str:
        self.calls.append((len(audio) / SR, prefix, language))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("boom")
        await asyncio.sleep(0)
        words = ["w%d" % i for i in range(max(1, int(len(audio) / SR)))]
        text = " ".join(words)
        head = "language English<asr_text>"
        if prefix:
            # Continue from the prefix the way the model would.
            done = prefix[len(head):].split(" ") if prefix.startswith(head) else prefix.split(" ")
            text = " ".join(done + words[len(done):]) if len(words) > len(done) else " ".join(done)
        return head + text


class Recorder:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(dict(event))

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


async def drive(session: lt.LiveTranscriptionSession, audio: np.ndarray, chunk_seconds: float = 0.2):
    """Feed ``audio`` in chunks, yielding to the decoder between them, then stop."""
    runner = asyncio.create_task(session.run())
    step = int(chunk_seconds * SR)
    for i in range(0, len(audio), step):
        session.feed(pcm(audio[i:i + step]))
        # Let the decoder loop absorb and (maybe) decode before more arrives.
        for _ in range(4):
            await asyncio.sleep(0)
    session.end_of_audio()
    await asyncio.wait_for(runner, timeout=5)


# ---------------------------------------------------------------------------
# Config + availability
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_when_section_absent(self, monkeypatch):
        monkeypatch.setattr("config.server_config.load_server_config", lambda: {})
        cfg = lt.local_asr_config()
        assert cfg == {"model": "", "device": "cpu", "dtype": "bfloat16", "threads": 0, "preload": True}

    def test_section_merged_and_coerced(self, monkeypatch):
        monkeypatch.setattr(
            "config.server_config.load_server_config",
            lambda: {"local_asr": {"model": " Qwen/Qwen3-ASR-1.7B ", "threads": "8", "preload": False, "device": "cuda:0"}},
        )
        cfg = lt.local_asr_config()
        assert cfg["model"] == "Qwen/Qwen3-ASR-1.7B"
        assert cfg["threads"] == 8
        assert cfg["preload"] is False
        assert cfg["device"] == "cuda:0"

    def test_env_override_and_loader(self, monkeypatch, tmp_path):
        from config import server_config

        path = tmp_path / "server_config.json"
        path.write_text(json.dumps({"local_asr": {"model": "a/b", "dtype": "float32"}}))
        monkeypatch.setattr(server_config, "SERVER_CONFIG_FILE", path)
        monkeypatch.delenv("QUEST_LOCAL_ASR_MODEL", raising=False)
        cfg = server_config.load_server_config()["local_asr"]
        assert cfg["model"] == "a/b"
        assert cfg["dtype"] == "float32"
        assert cfg["device"] == "cpu"
        monkeypatch.setenv("QUEST_LOCAL_ASR_MODEL", "")
        assert server_config.load_server_config()["local_asr"]["model"] == ""
        monkeypatch.setenv("QUEST_LOCAL_ASR_MODEL", "c/d")
        assert server_config.load_server_config()["local_asr"]["model"] == "c/d"


class TestAvailability:
    def test_unconfigured(self, monkeypatch):
        monkeypatch.setattr("config.server_config.load_server_config", lambda: {})
        ok, reason = lt.live_transcription_availability()
        assert ok is False
        assert "local_asr.model" in reason

    def test_packages_missing(self, monkeypatch):
        monkeypatch.setattr("config.server_config.load_server_config", lambda: {"local_asr": {"model": "x/y"}})
        monkeypatch.setattr(lt, "packages_installed", lambda: False)
        ok, reason = lt.live_transcription_availability()
        assert ok is False
        assert "uv sync --extra asr" in reason

    def test_failed_load_reported(self, monkeypatch):
        monkeypatch.setattr("config.server_config.load_server_config", lambda: {"local_asr": {"model": "x/y"}})
        monkeypatch.setattr(lt, "packages_installed", lambda: True)
        engine = lt.LocalASREngine(lt.local_asr_config())
        engine.status = "failed"
        engine.error = "no such checkpoint"
        monkeypatch.setattr(lt, "_engine", engine)
        ok, reason = lt.live_transcription_availability()
        assert ok is False
        assert "no such checkpoint" in reason

    def test_available(self, monkeypatch):
        monkeypatch.setattr("config.server_config.load_server_config", lambda: {"local_asr": {"model": "x/y"}})
        monkeypatch.setattr(lt, "packages_installed", lambda: True)
        monkeypatch.setattr(lt, "_engine", None)
        assert lt.live_transcription_availability() == (True, None)

    def test_voice_input_availability_is_either_mode(self, monkeypatch):
        monkeypatch.setattr(transcription, "transcription_availability", lambda: (False, "No Gemini."))
        monkeypatch.setattr(lt, "live_transcription_availability", lambda: (False, "No model."))
        ok, reason = transcription.voice_input_availability()
        assert ok is False
        assert "No Gemini." in reason and "No model." in reason
        monkeypatch.setattr(lt, "live_transcription_availability", lambda: (True, None))
        assert transcription.voice_input_availability() == (True, None)
        monkeypatch.setattr(lt, "live_transcription_availability", lambda: (False, "No model."))
        monkeypatch.setattr(transcription, "transcription_availability", lambda: (True, None))
        assert transcription.voice_input_availability() == (True, None)

    def test_preload_skips_without_config(self, monkeypatch):
        monkeypatch.setattr("config.server_config.load_server_config", lambda: {})
        calls = []
        monkeypatch.setattr(lt, "get_engine", lambda: calls.append(1))

        async def go():
            lt.preload_if_configured()

        _run(go())
        assert calls == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_pcm16_to_float(self):
        data = np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes() + b"\x01"
        out = lt.pcm16_to_float(data)
        assert out.dtype == np.float32
        assert np.allclose(out, [0, 0.5, -1.0, 32767 / 32768])
        assert len(lt.pcm16_to_float(b"\x01")) == 0

    def test_frame_rms(self):
        audio = np.concatenate([np.zeros(320), np.full(320, 0.5), np.ones(100)]).astype(np.float32)
        out = lt.frame_rms(audio, 320)
        assert out.shape == (2,)
        assert out[0] == 0 and abs(out[1] - 0.5) < 1e-6

    def test_energy_gate_adapts_but_has_floor(self):
        gate = lt._EnergyGate()
        # Quiet room: silence is silence, speech is speech.
        for _ in range(50):
            assert gate.is_speech(0.001) is False
        assert gate.is_speech(0.05) is True
        # Below the absolute minimum nothing is speech, however quiet the room.
        assert gate.is_speech(0.005) is False
        # A noisy room raises the threshold: 0.02 is speech when the floor is
        # 0.001 but not when the floor sits at 0.01.
        quiet = lt._EnergyGate()
        for _ in range(50):
            quiet.is_speech(0.001)
        assert quiet.is_speech(0.02) is True
        noisy = lt._EnergyGate()
        for _ in range(200):
            noisy.is_speech(0.01)
        assert noisy.is_speech(0.02) is False


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class TestSession:
    def test_silence_only_never_decodes(self):
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        _run(drive(session, silence(3.0)))
        assert engine.calls == []
        assert rec.types() == ["done"]

    def test_partials_then_final_then_done(self):
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        _run(drive(session, np.concatenate([silence(0.5), tone(3.0), silence(1.5)])))
        types = rec.types()
        assert types[0] == "partial"
        assert types[-2:] == ["final", "done"]
        assert "final" not in types[:-2]
        # The first decode waited for MIN_FIRST_DECODE_SECONDS of audio.
        assert engine.calls[0][0] >= lt.MIN_FIRST_DECODE_SECONDS
        # The first UNFIXED_CHUNKS decodes ran without a prefix, later ones with.
        assert all(prefix == "" for _, prefix, _ in engine.calls[:lt.UNFIXED_CHUNKS])
        assert any(prefix for _, prefix, _ in engine.calls[lt.UNFIXED_CHUNKS:])
        # Leading silence beyond the pre-roll was dropped and the final decode
        # kept at most TAIL_SILENCE_KEEP_SECONDS of the trailing silence
        # (plus one 0.2 s feed chunk of slack at either end).
        final_seconds = engine.calls[-1][0]
        assert 3.0 <= final_seconds <= 3.0 + lt.PRE_ROLL_SECONDS + lt.TAIL_SILENCE_KEEP_SECONDS + 0.4
        assert rec.events[-2]["text"].startswith("w0")
        assert session.segments == 1

    def test_pause_splits_segments(self):
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        _run(drive(session, np.concatenate([tone(2.0), silence(1.2), tone(2.0), silence(0.2)])))
        finals = [e for e in rec.events if e["type"] == "final"]
        assert len(finals) == 2
        assert session.segments == 2
        # The second segment started from nothing: a prefix-free decode of
        # roughly its own audio only.
        second_first = engine.calls[[i for i, c in enumerate(engine.calls) if c[1] == ""][-1]]
        assert second_first[0] < 3.0

    def test_long_speech_is_cut_at_max_segment(self, monkeypatch):
        monkeypatch.setattr(lt, "MAX_SEGMENT_SECONDS", 4.0)
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        # 7 s of continuous "speech" with one quiet 100 ms dip at 3.5 s.
        audio = np.concatenate([tone(3.5), silence(0.1), tone(3.4), silence(1.0)])
        _run(drive(session, audio))
        finals = [e for e in rec.events if e["type"] == "final"]
        assert len(finals) == 2
        # Every decode saw at most the segment cap.
        assert max(c[0] for c in engine.calls) <= 4.0 + 0.3
        # The cut landed at the dip, so the first segment is about 3.5 s.
        first_final_seconds = engine.calls[[i for i, e in enumerate(rec.events) if e["type"] == "final"][0] + 0][0]
        assert first_final_seconds <= 3.7

    def test_stop_finalizes_open_segment(self):
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        _run(drive(session, tone(2.5)))
        assert rec.types()[-2:] == ["final", "done"]

    def test_forced_language_passed_through(self):
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec, language="english")
        assert session.language == "English"
        _run(drive(session, tone(2.0)))
        assert all(lang == "English" for _, _, lang in engine.calls)
        with pytest.raises(ValueError):
            lt.LiveTranscriptionSession(engine, send=rec, language="Klingon")

    def test_decode_failure_reported_once(self):
        engine, rec = FakeEngine(fail_after=1), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        _run(drive(session, np.concatenate([tone(4.0), silence(1.5)])))
        errors = [e for e in rec.events if e["type"] == "error"]
        assert len(errors) == 1
        assert errors[0]["code"] == "decode_failed"
        assert "done" not in rec.types()

    def test_limits(self):
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)
        with pytest.raises(lt.LiveTranscriptionLimit):
            session.feed(b"\x00" * (lt.MAX_FRAME_BYTES + 2))
        frame = pcm(silence(1.0))
        for _ in range(lt.MAX_SESSION_SECONDS):
            session.feed(frame)
        with pytest.raises(lt.LiveTranscriptionLimit):
            session.feed(frame)

    def test_audio_arriving_during_decode_is_absorbed(self):
        """Frames fed while a decode is in flight are decoded on the next step."""
        engine, rec = FakeEngine(), Recorder()
        session = lt.LiveTranscriptionSession(engine, send=rec)

        async def go():
            runner = asyncio.create_task(session.run())
            session.feed(pcm(tone(1.5)))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            # Decode of the first 1.5 s is running (or just ran); pile on.
            session.feed(pcm(tone(2.0)))
            session.feed(pcm(silence(1.5)))
            session.end_of_audio()
            await asyncio.wait_for(runner, timeout=5)

        _run(go())
        assert rec.types()[-2:] == ["final", "done"]
        assert engine.calls[-1][0] >= 3.4


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@pytest.fixture
def gates_file(tmp_path, monkeypatch):
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(live_routes.router)
    return application


@pytest.fixture
def authed(monkeypatch):
    async def fake_auth(websocket, api_key):
        return dict(USER)

    monkeypatch.setattr(live_routes, "authenticate_websocket", fake_auth)


class TestRoute:
    def test_unauthenticated_closed(self, app, monkeypatch):
        async def no_auth(websocket, api_key):
            return None

        monkeypatch.setattr(live_routes, "authenticate_websocket", no_auth)
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live") as ws:
            with pytest.raises(Exception) as exc_info:
                ws.receive_json()
        assert "4401" in str(exc_info.value) or getattr(exc_info.value, "code", None) == 4401

    def test_gate_closed(self, app, authed, gates_file):
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live") as ws:
            event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "voice_input_disabled"

    def test_unavailable(self, app, authed, gates_file, monkeypatch):
        fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
        monkeypatch.setattr(live_routes, "live_transcription_availability", lambda: (False, "no model"))
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live") as ws:
            event = ws.receive_json()
        assert event == {"type": "error", "code": "live_transcription_unavailable", "message": "no model"}

    def test_bad_language(self, app, authed, gates_file, monkeypatch):
        fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
        monkeypatch.setattr(live_routes, "live_transcription_availability", lambda: (True, None))
        monkeypatch.setattr(live_routes, "get_engine", lambda: FakeEngine())
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live?language=Klingon") as ws:
            event = ws.receive_json()
        assert event["code"] == "bad_language"

    def test_busy(self, app, authed, gates_file, monkeypatch):
        fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
        monkeypatch.setattr(live_routes, "live_transcription_availability", lambda: (True, None))
        monkeypatch.setattr(live_routes, "_active_sessions", live_routes.MAX_LIVE_SESSIONS)
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live") as ws:
            event = ws.receive_json()
        assert event["code"] == "busy"
        assert live_routes.active_session_count() == live_routes.MAX_LIVE_SESSIONS

    def test_full_exchange(self, app, authed, gates_file, monkeypatch):
        fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
        monkeypatch.setattr(live_routes, "live_transcription_availability", lambda: (True, None))
        engine = FakeEngine()
        engine.status = "unloaded"
        loaded = []

        async def ensure_loaded():
            loaded.append(1)
            engine.status = "ready"

        engine.ensure_loaded = ensure_loaded
        monkeypatch.setattr(live_routes, "get_engine", lambda: engine)
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live?language=English") as ws:
            assert ws.receive_json() == {"type": "loading"}
            assert ws.receive_json() == {"type": "ready"}
            audio = np.concatenate([tone(2.5), silence(0.3)])
            data = pcm(audio)
            step = SR * 2 // 5
            for i in range(0, len(data), step):
                ws.send_bytes(data[i:i + step])
            ws.send_text(json.dumps({"op": "stop"}))
            events = []
            while True:
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "done":
                    break
        assert loaded == [1]
        types = [e["type"] for e in events]
        assert "partial" in types
        assert types[-2:] == ["final", "done"]
        assert all(lang == "English" for _, _, lang in engine.calls)
        assert live_routes.active_session_count() == 0

    def test_frame_too_large(self, app, authed, gates_file, monkeypatch):
        fg.set_feature_enabled(fg.FEATURE_VOICE_INPUT, True)
        monkeypatch.setattr(live_routes, "live_transcription_availability", lambda: (True, None))
        monkeypatch.setattr(live_routes, "get_engine", lambda: FakeEngine())
        client = TestClient(app)
        with client.websocket_connect("/app/api/transcribe/live") as ws:
            assert ws.receive_json() == {"type": "ready"}
            ws.send_bytes(b"\x00" * (lt.MAX_FRAME_BYTES + 2))
            event = ws.receive_json()
        assert event["code"] == "limit"
        assert live_routes.active_session_count() == 0
