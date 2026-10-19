"""Live dictation: streaming speech-to-text on a local Qwen3-ASR model.

The composer's microphone button has two modes (docs/architecture/voice-input.md).
The clip mode records a whole utterance and POSTs it to ``/transcribe`` for a
Gemini model on Vertex (``chat/transcription.py``). This module is the other
mode: the browser streams raw PCM over ``WS /app/api/transcribe/live``
(``chat/routes/transcribe_live.py``) and the text lands in the textarea
while the user is still talking, decoded by a Qwen3-ASR checkpoint running
in this process. Nothing leaves the machine.

Why not qwen-asr's own streaming API: it exists only for the vLLM backend,
which needs a CUDA GPU. The transformers backend runs anywhere (a 1.7B
model decodes a one-second step in about a second on a 16-core CPU in
bfloat16), so :class:`LiveTranscriptionSession` re-implements the same
algorithm on top of it:

* audio is accumulated into a **segment**, and every ``STEP_SECONDS`` of new
  audio the whole segment is decoded again with the previous output, minus
  its last ``UNFIXED_TOKENS`` tokens, fed back as a prompt prefix (so the
  model only has to produce the tail, and the text shown so far cannot
  flicker beyond those few tokens);
* the first ``UNFIXED_CHUNKS`` steps of a segment use no prefix at all, so a
  wrong guess on half a second of audio (language included) is discarded
  rather than locked in;
* a segment ends on ``SILENCE_SECONDS`` of trailing silence, at
  ``MAX_SEGMENT_SECONDS`` (cut at the quietest recent frame), or when the
  client stops. Its last decode is **final**: the text is committed and the
  next segment starts from nothing, which keeps every decode's audio
  bounded and the prefix short. Segment boundaries come from a cheap
  energy detector with an adaptive noise floor (:class:`_EnergyGate`);
  silence is never decoded, so a quiet room costs no CPU and the model is
  never asked to invent speech for it.

Decoding runs on one worker thread (:data:`_executor`), so concurrent
sessions share the model and queue behind each other; the route caps them
at ``MAX_LIVE_SESSIONS``. Weights load once per process (``preload`` in the
config loads them at startup, otherwise the first session pays), in the
same worker so a load never races a decode.

Configuration is the ``local_asr`` section of ``server_config.json``
(``config/server_config.py``): the model id or path, torch device and dtype,
CPU thread count and the preload flag. :func:`live_transcription_availability`
is what the admin feature-gate check and ``GET /me`` consult; the packages
(``uv sync --extra asr``) are imported lazily so a deployment without them
pays nothing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# --- streaming decode cadence ---------------------------------------------
# New audio (seconds) a segment must gain before it is decoded again.
STEP_SECONDS = 1.0
# A segment's first decode waits for this much audio: the model reads a
# half-second fragment as noise or the wrong language more often than not.
MIN_FIRST_DECODE_SECONDS = 1.2
# Steps of a segment decoded without a prompt prefix, and the tokens rolled
# back from the previous output when there is one (qwen-asr's streaming
# defaults).
UNFIXED_CHUNKS = 2
UNFIXED_TOKENS = 5
# Per-decode generation cap. With the prefix the model produces only the
# segment's new tail; the first prefix-free steps see at most ~3 s of audio.
MAX_NEW_TOKENS = 160

# --- segmentation -----------------------------------------------------------
FRAME_SECONDS = 0.02
# Trailing silence that closes a segment.
SILENCE_SECONDS = 0.8
# Longest segment: bounds the audio re-read on every step.
MAX_SEGMENT_SECONDS = 30.0
# The window before a forced cut searched for its quietest frame.
CUT_SEARCH_SECONDS = 3.0
# Silence kept before a segment's first speech and after its last.
PRE_ROLL_SECONDS = 0.3
TAIL_SILENCE_KEEP_SECONDS = 0.3
# Frame RMS (float samples in -1..1) below which nothing counts as speech
# whatever the noise floor says, and the ceiling of the adaptive threshold.
MIN_SPEECH_RMS = 0.01
MAX_SPEECH_RMS = 0.08
# Speech must exceed the tracked noise floor by this factor.
SPEECH_OVER_FLOOR = 4.0
# How fast the noise floor may creep up per frame (it drops instantly).
FLOOR_RISE_PER_FRAME = 1.02

# --- limits -------------------------------------------------------------------
# A session is one dictation; the client reconnects for the next one.
MAX_SESSION_SECONDS = 600
# Concurrent live sessions per process (they share one decode thread).
MAX_LIVE_SESSIONS = 3
# Largest binary frame accepted from the client (~1 s of 16 kHz PCM16 is
# 32 KB; the browser sends about five frames a second).
MAX_FRAME_BYTES = 64 * 1024

LANGUAGES: tuple[str, ...] = (
    "Chinese", "English", "Cantonese", "Arabic", "German", "French",
    "Spanish", "Portuguese", "Indonesian", "Italian", "Korean", "Russian",
    "Thai", "Vietnamese", "Japanese", "Turkish", "Hindi", "Malay", "Dutch",
    "Swedish", "Danish", "Finnish", "Polish", "Czech", "Filipino",
    "Persian", "Greek", "Romanian", "Hungarian", "Macedonian",
)

EventSender = Callable[[dict], Awaitable[None]]


class LiveTranscriptionUnavailable(RuntimeError):
    """The local model is not configured, not installed, or failed to load."""


class LiveTranscriptionLimit(RuntimeError):
    """The client exceeded a session limit (audio length, frame size)."""


# ---------------------------------------------------------------------------
# Configuration and availability
# ---------------------------------------------------------------------------


def local_asr_config() -> dict:
    """The ``local_asr`` section of the server config with defaults applied."""
    from config.server_config import load_server_config

    section = dict(load_server_config().get("local_asr") or {})
    section["model"] = str(section.get("model") or "").strip()
    section["device"] = str(section.get("device") or "cpu")
    section["dtype"] = str(section.get("dtype") or "bfloat16")
    try:
        section["threads"] = int(section.get("threads") or 0)
    except (TypeError, ValueError):
        section["threads"] = 0
    section["preload"] = bool(section.get("preload", True))
    return section


def packages_installed() -> bool:
    """Whether the ``asr`` extra (qwen-asr + torch) is importable."""
    return (
        importlib.util.find_spec("qwen_asr") is not None
        and importlib.util.find_spec("torch") is not None
    )


def live_transcription_availability() -> tuple[bool, Optional[str]]:
    """Whether live dictation CAN run here: ``(available, reason_if_not)``.

    A config-and-install check, plus the cached verdict of a load that
    already failed. It never loads the model itself.
    """
    config = local_asr_config()
    if not config["model"]:
        return False, (
            "Live dictation needs a local speech model: set local_asr.model "
            "in server_config.json (for example \"Qwen/Qwen3-ASR-1.7B\") and "
            "install the speech packages with `uv sync --extra asr`."
        )
    if not packages_installed():
        return False, (
            f"local_asr.model is set to {config['model']!r} but the speech "
            "packages are not installed: run `uv sync --extra asr` and "
            "restart."
        )
    engine = _engine_if_created()
    if engine is not None and engine.status == "failed":
        return False, f"The local speech model failed to load: {engine.error}"
    return True, None


# ---------------------------------------------------------------------------
# Engine: one loaded model per process, one decode thread
# ---------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-asr")
_engine: Optional["LocalASREngine"] = None
_engine_lock = threading.Lock()


def _engine_if_created() -> Optional["LocalASREngine"]:
    return _engine


def get_engine() -> "LocalASREngine":
    """The process-wide engine for the configured model (created lazily)."""
    global _engine
    with _engine_lock:
        config = local_asr_config()
        if _engine is None or _engine.model_id != config["model"]:
            _engine = LocalASREngine(config)
        return _engine


def reset_engine_for_tests() -> None:
    global _engine
    with _engine_lock:
        _engine = None


class LocalASREngine:
    """A Qwen3-ASR checkpoint on the transformers backend.

    ``status`` moves ``unloaded`` -> ``loading`` -> ``ready`` | ``failed``.
    :meth:`ensure_loaded` is idempotent and safe to call concurrently; a
    failed load is remembered (``error``) rather than retried per session,
    so a broken checkpoint reports itself in Settings instead of spinning.
    """

    def __init__(self, config: dict):
        self.model_id: str = config["model"]
        self.device: str = config["device"]
        self.dtype_name: str = config["dtype"]
        self.threads: int = config["threads"]
        self.status = "unloaded"
        self.error: Optional[str] = None
        self._asr: Any = None
        self._prompt_cache: dict[Optional[str], str] = {}
        self._load_future: Optional[asyncio.Future] = None
        self._load_lock = asyncio.Lock()
        self._generate_kwargs: dict = {}

    # -- loading --------------------------------------------------------------

    async def ensure_loaded(self) -> None:
        """Load the weights once; raises :class:`LiveTranscriptionUnavailable`."""
        if self.status == "ready":
            return
        if self.status == "failed":
            raise LiveTranscriptionUnavailable(self.error or "model failed to load")
        async with self._load_lock:
            if self.status == "ready":
                return
            if self.status == "failed":
                raise LiveTranscriptionUnavailable(self.error or "model failed to load")
            if self._load_future is None:
                self.status = "loading"
                loop = asyncio.get_running_loop()
                self._load_future = loop.run_in_executor(_executor, self._load)
            future = self._load_future
        try:
            await asyncio.shield(future)
        except Exception as exc:
            if self.status != "failed":
                self.status = "failed"
                self.error = str(exc)
            raise LiveTranscriptionUnavailable(self.error) from exc

    def _load(self) -> None:
        started = time.monotonic()
        try:
            import torch
            from qwen_asr import Qwen3ASRModel

            if self.threads > 0:
                torch.set_num_threads(self.threads)
            dtype = getattr(torch, self.dtype_name, None)
            if not isinstance(dtype, torch.dtype):
                raise ValueError(f"unknown torch dtype {self.dtype_name!r}")
            kwargs = dict(dtype=dtype, device_map=self.device, max_new_tokens=MAX_NEW_TOKENS)
            try:
                # Prefer the local cache so a deployment without internet
                # access (or with a slow Hub) never waits on a network check.
                asr = Qwen3ASRModel.from_pretrained(self.model_id, local_files_only=True, **kwargs)
            except Exception as local_exc:
                logger.info(
                    "Local ASR model %s not in the local cache (%s); fetching",
                    self.model_id, local_exc,
                )
                asr = Qwen3ASRModel.from_pretrained(self.model_id, **kwargs)
            tokenizer = asr.processor.tokenizer
            pad_id = tokenizer.pad_token_id
            if pad_id is None:
                pad_id = tokenizer.eos_token_id
            self._generate_kwargs = {
                "max_new_tokens": MAX_NEW_TOKENS,
                "do_sample": False,
                "pad_token_id": pad_id,
            }
            self._asr = asr
            self.status = "ready"
            self.error = None
            logger.info(
                "Local ASR model %s loaded on %s (%s) in %.1fs",
                self.model_id, self.device, self.dtype_name, time.monotonic() - started,
            )
        except Exception as exc:
            self.status = "failed"
            self.error = f"{type(exc).__name__}: {exc}"
            logger.exception("Local ASR model %s failed to load", self.model_id)
            raise

    # -- helpers used by the session --------------------------------------------

    def normalize_language(self, language: Optional[str]) -> Optional[str]:
        """Canonical language name for a forced language, or None for auto.

        Raises ``ValueError`` for a name the model does not know.
        """
        if language is None or not str(language).strip():
            return None
        wanted = str(language).strip().lower()
        for name in LANGUAGES:
            if name.lower() == wanted:
                return name
        raise ValueError(f"unsupported language: {language}")

    def rollback_prefix(self, raw: str, tokens: int = UNFIXED_TOKENS) -> str:
        """``raw`` minus its last ``tokens`` tokens, on a clean character boundary."""
        if not raw:
            return ""
        tokenizer = self._asr.processor.tokenizer
        ids = tokenizer.encode(raw)
        k = tokens
        while True:
            end = max(0, len(ids) - k)
            if end == 0:
                return ""
            prefix = tokenizer.decode(ids[:end])
            if "�" not in prefix:
                return prefix
            k += 1

    def parse_output(self, raw: str, language: Optional[str]) -> tuple[str, str]:
        """``(language, text)`` from the model's raw ``language X<asr_text>...`` output."""
        from qwen_asr.inference.utils import parse_asr_output

        return parse_asr_output(raw, user_language=language)

    async def decode(self, audio: np.ndarray, prefix: str, language: Optional[str]) -> str:
        """Decode ``audio`` (16 kHz float32 mono) continuing ``prefix``; returns the full raw output."""
        if self.status != "ready":
            raise LiveTranscriptionUnavailable(self.error or "model not loaded")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, self._decode_sync, audio, prefix, language)

    def _prompt(self, language: Optional[str]) -> str:
        if language not in self._prompt_cache:
            self._prompt_cache[language] = self._asr._build_text_prompt(context="", force_language=language)
        return self._prompt_cache[language]

    def _decode_sync(self, audio: np.ndarray, prefix: str, language: Optional[str]) -> str:
        import torch

        asr = self._asr
        prompt = self._prompt(language) + prefix
        inputs = asr.processor(text=[prompt], audio=[audio], return_tensors="pt", padding=True)
        inputs = inputs.to(asr.model.device).to(asr.model.dtype)
        with torch.inference_mode():
            out = asr.model.generate(**inputs, **self._generate_kwargs)
        generated = asr.processor.batch_decode(
            out.sequences[:, inputs["input_ids"].shape[1]:],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]
        return prefix + generated


def preload_if_configured() -> None:
    """Start loading the configured model in the background (FastAPI lifespan)."""
    config = local_asr_config()
    if not config["model"] or not config["preload"]:
        return
    if not packages_installed():
        logger.warning(
            "local_asr.model is set to %r but qwen-asr/torch are not installed; "
            "live dictation is unavailable until `uv sync --extra asr`",
            config["model"],
        )
        return
    engine = get_engine()

    async def _load() -> None:
        try:
            await engine.ensure_loaded()
        except LiveTranscriptionUnavailable:
            # Already logged with the traceback by _load(); the admin UI
            # shows the reason through live_transcription_availability().
            pass

    asyncio.get_running_loop().create_task(_load())


# ---------------------------------------------------------------------------
# Energy gate (speech / silence per 20 ms frame)
# ---------------------------------------------------------------------------


class _EnergyGate:
    """Frame-level speech detector with an adaptive noise floor.

    The floor drops to any quieter frame at once and rises slowly, so a
    room that gets louder is followed within seconds while a word never
    lifts it. A frame is speech when its RMS exceeds both the absolute
    minimum and the floor by ``SPEECH_OVER_FLOOR``.
    """

    def __init__(self) -> None:
        self.floor = MAX_SPEECH_RMS

    def is_speech(self, rms: float) -> bool:
        self.floor = min(self.floor * FLOOR_RISE_PER_FRAME + 1e-6, max(rms, 1e-6))
        threshold = min(max(self.floor * SPEECH_OVER_FLOOR, MIN_SPEECH_RMS), MAX_SPEECH_RMS)
        return rms >= threshold


def frame_rms(audio: np.ndarray, frame: int) -> np.ndarray:
    """RMS of each full ``frame``-sample window of ``audio`` (a trailing partial frame is dropped)."""
    n = len(audio) // frame
    if n == 0:
        return np.zeros((0,), dtype=np.float32)
    windows = audio[: n * frame].reshape(n, frame).astype(np.float32)
    return np.sqrt(np.mean(windows * windows, axis=1))


def pcm16_to_float(data: bytes) -> np.ndarray:
    """Little-endian 16-bit PCM bytes -> float32 samples in -1..1 (an odd trailing byte is dropped)."""
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return np.zeros((0,), dtype=np.float32)
    return np.frombuffer(data[:usable], dtype="<i2").astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class LiveTranscriptionSession:
    """One dictation: audio in through :meth:`feed`, events out through ``send``.

    Events (dicts, sent in order):

    * ``{"type": "partial", "text"}`` -- the current segment's text so far;
      replaces the previous partial.
    * ``{"type": "final", "text"}`` -- a segment is done; the client appends
      the text to what it has committed and clears the partial. Empty
      segments send nothing.
    * ``{"type": "done"}`` -- sent once after :meth:`end_of_audio` when the
      last segment has been finalized; :meth:`run` returns right after.

    The engine is any object with the ``decode`` / ``rollback_prefix`` /
    ``parse_output`` / ``normalize_language`` methods of
    :class:`LocalASREngine` (tests pass a fake).
    """

    def __init__(self, engine: Any, send: EventSender, language: Optional[str] = None):
        self.engine = engine
        self.send = send
        self.language = engine.normalize_language(language)
        self.frame = int(SAMPLE_RATE * FRAME_SECONDS)
        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        self._total_samples = 0
        self._stopped = False
        self._wake = asyncio.Event()
        self._gate = _EnergyGate()
        self._reset_segment()
        self.decodes = 0
        self.segments = 0

    # -- input ----------------------------------------------------------------

    def feed(self, pcm16: bytes) -> None:
        """Queue a binary frame of 16 kHz mono PCM16 for the decoder loop."""
        if len(pcm16) > MAX_FRAME_BYTES:
            raise LiveTranscriptionLimit("audio frame too large")
        samples = pcm16_to_float(pcm16)
        if len(samples) == 0:
            return
        self._total_samples += len(samples)
        if self._total_samples > MAX_SESSION_SECONDS * SAMPLE_RATE:
            raise LiveTranscriptionLimit(
                f"dictation longer than {MAX_SESSION_SECONDS // 60} minutes"
            )
        self._pending.append(samples)
        self._pending_samples += len(samples)
        self._wake.set()

    def end_of_audio(self) -> None:
        """No more audio will arrive: finish the current segment and send ``done``."""
        self._stopped = True
        self._wake.set()

    # -- segment state --------------------------------------------------------

    def _reset_segment(self) -> None:
        self._segment = np.zeros((0,), dtype=np.float32)
        self._seg_speech = False
        self._seg_raw = ""
        self._seg_steps = 0
        self._decoded_samples = 0
        self._trailing_silence = 0
        self._speech_since_decode = False
        self._carry = np.zeros((0,), dtype=np.float32)

    def _absorb(self, audio: np.ndarray) -> None:
        """Run the energy gate over new audio and append it to the segment."""
        audio = np.concatenate([self._carry, audio]) if len(self._carry) else audio
        n_frames = len(audio) // self.frame
        self._carry = audio[n_frames * self.frame:]
        if n_frames == 0:
            return
        frames = audio[: n_frames * self.frame]
        for rms in frame_rms(frames, self.frame):
            speech = self._gate.is_speech(float(rms))
            if speech:
                self._seg_speech = True
                self._speech_since_decode = True
                self._trailing_silence = 0
            else:
                self._trailing_silence += self.frame
        self._segment = np.concatenate([self._segment, frames])
        if not self._seg_speech:
            # Nothing said yet: keep only a short pre-roll so the segment
            # never starts with seconds of silence.
            keep = int(PRE_ROLL_SECONDS * SAMPLE_RATE)
            if len(self._segment) > keep:
                self._segment = self._segment[-keep:]
            self._trailing_silence = min(self._trailing_silence, keep)

    def _prefix(self) -> str:
        if self._seg_steps < UNFIXED_CHUNKS or not self._seg_raw:
            return ""
        return self.engine.rollback_prefix(self._seg_raw, UNFIXED_TOKENS)

    async def _decode_segment(self, audio: np.ndarray) -> str:
        raw = await self.engine.decode(audio, self._prefix(), self.language)
        self._seg_raw = raw
        self._seg_steps += 1
        self.decodes += 1
        _, text = self.engine.parse_output(raw, self.language)
        return text.strip()

    def _cut_point(self) -> int:
        """Sample index to split an over-long segment at: its quietest recent frame."""
        window = int(CUT_SEARCH_SECONDS * SAMPLE_RATE)
        start = max(0, len(self._segment) - window)
        start -= start % self.frame
        energies = frame_rms(self._segment[start:], self.frame)
        if len(energies) == 0:
            return len(self._segment)
        return start + int(np.argmin(energies)) * self.frame

    async def _finalize_segment(self, cut: Optional[int] = None) -> None:
        """Decode the segment one last time, emit ``final``, start the next one."""
        remainder = np.zeros((0,), dtype=np.float32)
        audio = self._segment
        if cut is not None:
            audio, remainder = self._segment[:cut], self._segment[cut:]
        else:
            keep = int(TAIL_SILENCE_KEEP_SECONDS * SAMPLE_RATE)
            trim = max(0, self._trailing_silence - keep)
            if trim:
                audio = audio[:-trim]
        text = ""
        if self._seg_speech and len(audio) >= self.frame:
            text = await self._decode_segment(audio)
        self.segments += 1
        self._reset_segment()
        if text:
            await self.send({"type": "final", "text": text})
        if len(remainder):
            self._absorb(remainder)

    # -- decoder loop ----------------------------------------------------------

    async def run(self) -> None:
        """Consume fed audio until :meth:`end_of_audio` and the last segment is done.

        A decode failure is reported once as ``{"type": "error", "code":
        "decode_failed"}`` and ends the loop; the client stops on it.
        """
        try:
            await self._loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Live transcription decode failed")
            await self.send({
                "type": "error",
                "code": "decode_failed",
                "message": "Transcription failed on the server.",
            })

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._pending:
                audio = np.concatenate(self._pending)
                self._pending = []
                self._pending_samples = 0
                self._absorb(audio)

            if self._seg_speech and len(self._segment) >= MAX_SEGMENT_SECONDS * SAMPLE_RATE:
                await self._finalize_segment(cut=self._cut_point())
                # Re-evaluate at once: the remainder may already hold speech,
                # and a stop that arrived meanwhile must not wait for audio.
                self._wake.set()
                continue
            if self._seg_speech and self._trailing_silence >= SILENCE_SECONDS * SAMPLE_RATE:
                await self._finalize_segment()
                self._wake.set()
                continue

            if self._stopped and not self._pending:
                if self._seg_speech:
                    await self._finalize_segment()
                await self.send({"type": "done"})
                return

            if self._seg_speech and self._speech_since_decode:
                new = len(self._segment) - self._decoded_samples
                minimum = MIN_FIRST_DECODE_SECONDS if self._seg_steps == 0 else STEP_SECONDS
                if new >= minimum * SAMPLE_RATE:
                    self._decoded_samples = len(self._segment)
                    self._speech_since_decode = False
                    text = await self._decode_segment(self._segment)
                    await self.send({"type": "partial", "text": text})
                    # Audio that arrived during the decode is waiting in
                    # _pending; loop straight back for it.
                    if self._pending or self._stopped:
                        self._wake.set()
