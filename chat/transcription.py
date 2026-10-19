"""Server-side speech-to-text for the composer's voice input.

The browser records a short clip with ``MediaRecorder`` and POSTs it to
``POST /app/api/transcribe`` (``chat/routes/transcribe.py``); this module
picks the Gemini Vertex model to transcribe with, validates the clip, and
runs the one-shot ``GeminiProvider.transcribe_audio`` call. The transcript
goes back to the composer for the user to edit before sending -- nothing
here touches a conversation, and the audio is never written to disk.

Why Gemini on Vertex: every deployment already carries Vertex credentials
for chat inference, so transcription needs no new secret, and audio stays
inside the deployment's own Google Cloud project instead of a third-party
speech service (the browser's cloud-routed Web Speech API would ship it to
the browser vendor). The whole feature sits behind the admin ``voice_input``
feature gate (``config/feature_gates.py``); :func:`transcription_availability`
is what the admin endpoint consults before letting the gate be turned on,
and the route re-checks it per request so a gate left on after Vertex was
unconfigured degrades to a clean 503 rather than a provider stack trace.
"""

import asyncio
import logging
import time

from chat.llm.file_limits import GEMINI_VERTEX_MAX_ATTACH_BYTES

logger = logging.getLogger(__name__)


class TranscriptionUnavailable(RuntimeError):
    """No Gemini Vertex model can serve transcription right now."""


class TranscriptionFailed(RuntimeError):
    """The provider call failed or timed out."""


# Gemini models tried first, cheapest-and-newest first. Any other enabled,
# non-deprecated Gemini Vertex model is a fallback (registry order), so a
# deployment that disables every Flash model still transcribes -- just on a
# pricier model. Anthropic and OpenRouter models take no audio input and
# are never considered.
TRANSCRIPTION_MODEL_PREFERENCE: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
)

# Per-clip byte cap: under the Vertex inline-part ceiling with headroom for
# the prompt. ~2.5 minutes of 48 kHz Opus at MediaRecorder's default
# bitrate; the frontend stops a recording at MAX_RECORDING_SECONDS anyway.
MAX_AUDIO_BYTES = min(6 * 1024 * 1024, GEMINI_VERTEX_MAX_ATTACH_BYTES)

# Longest clip the composer will record before auto-stopping (mirrored in
# frontend/src/hooks/useVoiceRecorder.ts).
MAX_RECORDING_SECONDS = 120

# Wall-clock budget for one transcription attempt.
TRANSCRIBE_TIMEOUT_SECONDS = 60.0

# google-genai performs no retries of its own, so a single transient 429 /
# 5xx from Vertex would otherwise fail the clip outright. Retry those a
# couple of times with a short backoff; the user is waiting, so keep it
# brief and never retry a 4xx that will not change (bad request, denied).
TRANSCRIBE_MAX_ATTEMPTS = 3
TRANSCRIBE_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Container types accepted from the browser. MediaRecorder produces
# audio/webm (Chrome, Firefox, Edge) or audio/mp4 (Safari); the rest cover
# files a client might reasonably send through the same endpoint. Every
# entry is a Gemini-supported audio MIME.
ALLOWED_AUDIO_MIME_TYPES: frozenset[str] = frozenset({
    "audio/webm",
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/flac",
    "audio/aac",
})

# Declared-type spellings folded onto the canonical entry above.
_MIME_ALIASES = {
    "video/webm": "audio/webm",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-pn-wav": "audio/wav",
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/mp3": "audio/mpeg",
    "audio/x-flac": "audio/flac",
    "audio/x-aac": "audio/aac",
}


def normalize_audio_mime(declared: str | None) -> str:
    """Canonical MIME for a declared content type (params dropped, aliases folded).

    ``audio/webm;codecs=opus`` -> ``audio/webm``. Returns ``""`` for a
    missing type; the caller decides whether the sniffed type can stand in.
    """
    base = (declared or "").split(";", 1)[0].strip().lower()
    return _MIME_ALIASES.get(base, base)


def sniff_audio_mime(data: bytes) -> str | None:
    """Infer the container from magic bytes, or None when unrecognized.

    Fail-closed companion to the declared content type: the route refuses
    a clip whose bytes do not look like one of the accepted containers, so
    a tampered request cannot pass arbitrary bytes to the provider under an
    audio label. Only distinguishes containers, not codecs.
    """
    head = data[:16]
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML header: WebM / Matroska (MediaRecorder's audio/webm).
        return "audio/webm"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        # ISO base media file: MP4 / M4A (Safari's MediaRecorder output).
        return "audio/mp4"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # Raw MPEG audio frame sync (MP3 without an ID3 tag) -- ADTS AAC
        # shares the sync word and is folded in here; Gemini accepts both.
        return "audio/mpeg"
    return None


def _is_retryable(exc: BaseException) -> bool:
    """Transient provider failure (rate limit / server error) worth a retry.

    google-genai raises ``errors.APIError`` subclasses carrying ``code``;
    fall back to a status-code sniff of the message for anything else.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in _RETRYABLE_STATUS_CODES
    if getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES:
        return True
    message = str(exc)
    return any(str(c) in message[:12] for c in _RETRYABLE_STATUS_CODES)


def _gemini_model_ids(model_ids: list[str]) -> list[str]:
    """Gemini Vertex models among ``model_ids``, preference order first."""
    from chat.llm.config import resolve_model

    gemini = [
        model_id for model_id in model_ids
        if (spec := resolve_model(model_id)) is not None
        and spec.provider == "gemini"
        and spec.instance_id is None
    ]
    preferred = [m for m in TRANSCRIPTION_MODEL_PREFERENCE if m in gemini]
    rest = [m for m in gemini if m not in preferred]
    return preferred + rest


def transcription_availability() -> tuple[bool, str | None]:
    """Whether transcription CAN run here: ``(available, reason_if_not)``.

    A config-presence check (no live probe), the same universe the health
    sweep starts from: a Gemini Vertex project id must be configured and at
    least one non-deprecated Gemini model must be enabled under Settings >
    Inference Providers. The admin endpoint refuses to turn the
    ``voice_input`` gate on while this is False, and the GET view carries
    the reason so the Settings toggle can explain itself.
    """
    from chat.llm.config import get_configured_models

    if _gemini_model_ids(get_configured_models()):
        return True, None
    return False, (
        "Voice input needs a Gemini model on Vertex AI: set "
        "gemini_vertex.vertex_project_id in server_config.json and enable "
        "at least one Gemini model under Settings > Inference Providers."
    )


def voice_input_availability() -> tuple[bool, str | None]:
    """Whether the ``voice_input`` gate can be turned on: ``(available, reason)``.

    Either mode of the feature makes it available: the clip mode on a
    Gemini Vertex model (:func:`transcription_availability`) or live
    dictation on a local model (``chat/live_transcription.py``). The reason
    lists what each mode is missing when neither can run.
    """
    from chat.live_transcription import live_transcription_availability

    clip_ok, clip_reason = transcription_availability()
    live_ok, live_reason = live_transcription_availability()
    if clip_ok or live_ok:
        return True, None
    return False, f"{clip_reason} Or, for live dictation: {live_reason}"


def transcription_model() -> str | None:
    """The model id transcription will use right now, or None.

    Prefers configured models without a failing health verdict
    (:func:`get_available_models`); falls back to any configured Gemini
    model so a stale health verdict cannot switch the feature off by
    itself (the call then fails at the provider and surfaces as
    ``transcription_failed``).
    """
    from chat.llm.config import get_available_models, get_configured_models

    candidates = _gemini_model_ids(get_available_models())
    if not candidates:
        candidates = _gemini_model_ids(get_configured_models())
    return candidates[0] if candidates else None


async def transcribe_audio(data: bytes, mime_type: str) -> dict:
    """Transcribe one validated clip; returns ``{"text", "model", "duration_ms"}``.

    ``mime_type`` must already be one of :data:`ALLOWED_AUDIO_MIME_TYPES`
    and ``data`` within :data:`MAX_AUDIO_BYTES` -- the route validates,
    this runs. Raises :class:`TranscriptionUnavailable` when no Gemini
    model can serve and :class:`TranscriptionFailed` on a provider error
    or timeout (the underlying exception is logged, never returned to the
    client verbatim).
    """
    from chat.llm.config import get_provider_instance

    model = transcription_model()
    if model is None:
        raise TranscriptionUnavailable(transcription_availability()[1])
    provider = get_provider_instance("gemini")
    started = time.monotonic()
    text = ""
    for attempt in range(1, TRANSCRIBE_MAX_ATTEMPTS + 1):
        try:
            text = await asyncio.wait_for(
                provider.transcribe_audio(model, data, mime_type),
                timeout=TRANSCRIBE_TIMEOUT_SECONDS,
            )
            break
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Transcription timed out after %.0fs (model=%s, bytes=%d)",
                TRANSCRIBE_TIMEOUT_SECONDS, model, len(data),
            )
            raise TranscriptionFailed("Transcription timed out") from exc
        except Exception as exc:
            retryable = _is_retryable(exc) and attempt < TRANSCRIBE_MAX_ATTEMPTS
            logger.warning(
                "Transcription %s (model=%s, mime=%s, bytes=%d, attempt %d/%d): %s",
                "will retry" if retryable else "failed",
                model, mime_type, len(data), attempt, TRANSCRIBE_MAX_ATTEMPTS, exc,
            )
            if not retryable:
                raise TranscriptionFailed("Transcription failed") from exc
            await asyncio.sleep(
                TRANSCRIBE_RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(TRANSCRIBE_RETRY_BACKOFF_SECONDS) - 1)
                ]
            )
    text = (text or "").strip()
    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "Transcribed %d bytes of %s with %s in %dms (%d chars)",
        len(data), mime_type, model, duration_ms, len(text),
    )
    return {"text": text, "model": model, "duration_ms": duration_ms}
