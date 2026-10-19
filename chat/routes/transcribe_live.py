"""``WS /app/api/transcribe/live``: streaming speech-to-text for live dictation.

The composer's live-dictation hook (``frontend/src/hooks/useLiveDictation.ts``)
opens this socket, streams the microphone as **binary frames of 16 kHz mono
little-endian PCM16**, and receives JSON events that update the textarea
while the user talks. The decoding is ``chat/live_transcription.py``: a
local Qwen3-ASR model, so the audio never leaves the server.

Protocol, in order:

1. The server authenticates the socket (session cookie or ``?api_key=``;
   ``chat/realtime/ws_auth.py``), checks the per-user ``voice_input`` gate
   and the model's availability, and admits at most ``MAX_LIVE_SESSIONS``
   dictations at once. A refusal is one ``{"type": "error", "code",
   "message"}`` event followed by a close with the matching ``CLOSE_*``
   code; an unauthenticated socket is closed without an event.
2. ``{"type": "loading"}`` while the weights are still loading (first
   session after startup without ``preload``), then ``{"type": "ready"}``.
   The client starts sending audio only after ``ready``.
3. Binary frames in, ``partial`` / ``final`` events out (see
   ``LiveTranscriptionSession``). An optional ``?language=`` query parameter
   forces the language (``LANGUAGES``); the default is auto-detection.
4. The client sends the text frame ``{"op": "stop"}`` when the microphone is
   released; the server finalizes the open segment, sends ``{"type":
   "done"}`` and closes normally. A socket that just disconnects is
   abandoned: whatever was not yet final is dropped.

Session limits (``MAX_SESSION_SECONDS``, ``MAX_FRAME_BYTES``) end the socket
with an ``error`` event and ``CLOSE_LIMIT``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from chat.live_transcription import (
    MAX_LIVE_SESSIONS,
    LiveTranscriptionLimit,
    LiveTranscriptionSession,
    LiveTranscriptionUnavailable,
    get_engine,
    live_transcription_availability,
)
from chat.realtime.ws_auth import authenticate_websocket
from chat.routes import router
from config.feature_gates import FEATURE_VOICE_INPUT, is_feature_enabled_for_user

logger = logging.getLogger(__name__)

CLOSE_BAD_REQUEST = 4400
CLOSE_AUTH_FAILED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_LIMIT = 4413
CLOSE_INTERNAL = 4500
CLOSE_UNAVAILABLE = 4503
CLOSE_BUSY = 4509

# How long the server waits for the last segment to decode after ``stop``.
FINISH_TIMEOUT_SECONDS = 60.0

_active_sessions = 0


def active_session_count() -> int:
    return _active_sessions


async def _refuse(websocket: WebSocket, code: int, error: str, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "code": error, "message": message})
    except Exception:
        pass
    try:
        await websocket.close(code=code)
    except Exception:
        pass


@router.websocket("/transcribe/live")
async def live_transcription_socket(
    websocket: WebSocket,
    api_key: Optional[str] = None,
    language: Optional[str] = None,
) -> None:
    global _active_sessions

    await websocket.accept()

    user = await authenticate_websocket(websocket, api_key)
    if user is None:
        try:
            await websocket.close(code=CLOSE_AUTH_FAILED)
        except Exception:
            pass
        return

    if not is_feature_enabled_for_user(FEATURE_VOICE_INPUT, user["email"]):
        await _refuse(
            websocket, CLOSE_FORBIDDEN, "voice_input_disabled",
            "Voice input is not enabled for your account.",
        )
        return

    available, reason = live_transcription_availability()
    if not available:
        await _refuse(websocket, CLOSE_UNAVAILABLE, "live_transcription_unavailable", reason or "")
        return

    if _active_sessions >= MAX_LIVE_SESSIONS:
        await _refuse(
            websocket, CLOSE_BUSY, "busy",
            "Too many people are dictating right now. Try again in a moment.",
        )
        return

    _active_sessions += 1
    decoder: Optional[asyncio.Task] = None
    try:
        engine = get_engine()
        if engine.status != "ready":
            await websocket.send_json({"type": "loading"})
        try:
            await engine.ensure_loaded()
        except LiveTranscriptionUnavailable as exc:
            await _refuse(
                websocket, CLOSE_UNAVAILABLE, "live_transcription_unavailable",
                f"The local speech model failed to load: {exc}",
            )
            return

        closed = False

        async def send(event: dict) -> None:
            nonlocal closed
            if closed:
                return
            try:
                await websocket.send_json(event)
            except Exception:
                closed = True

        try:
            session = LiveTranscriptionSession(engine, send=send, language=language)
        except ValueError as exc:
            await _refuse(websocket, CLOSE_BAD_REQUEST, "bad_language", str(exc))
            return

        await websocket.send_json({"type": "ready"})
        decoder = asyncio.create_task(session.run())
        logger.info("Live dictation started for %s (language=%s)", user["email"], session.language or "auto")

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                logger.info(
                    "Live dictation socket closed by %s after %d decodes / %d segments",
                    user["email"], session.decodes, session.segments,
                )
                return
            data = message.get("bytes")
            if data is not None:
                try:
                    session.feed(data)
                except LiveTranscriptionLimit as exc:
                    await _refuse(websocket, CLOSE_LIMIT, "limit", str(exc))
                    return
                continue
            text = message.get("text")
            if not text:
                continue
            try:
                op = json.loads(text).get("op")
            except (ValueError, AttributeError):
                op = None
            if op == "stop":
                session.end_of_audio()
                try:
                    await asyncio.wait_for(decoder, timeout=FINISH_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    await _refuse(
                        websocket, CLOSE_INTERNAL, "finish_timeout",
                        "The last part of the dictation took too long to transcribe.",
                    )
                    return
                except Exception:
                    logger.exception("Live dictation decoder failed for %s", user["email"])
                    await _refuse(
                        websocket, CLOSE_INTERNAL, "decode_failed",
                        "Transcription failed on the server.",
                    )
                    return
                logger.info(
                    "Live dictation finished for %s: %d decodes / %d segments",
                    user["email"], session.decodes, session.segments,
                )
                try:
                    await websocket.close()
                except Exception:
                    pass
                return
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("Live dictation socket failed for %s", user["email"])
        await _refuse(websocket, CLOSE_INTERNAL, "internal", "Live dictation failed on the server.")
    finally:
        _active_sessions -= 1
        if decoder is not None and not decoder.done():
            decoder.cancel()
            try:
                await decoder
            except (asyncio.CancelledError, Exception):
                pass
