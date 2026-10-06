"""Persistent multiplexed WebSocket endpoint.

One socket per browser session. Reader / writer / heartbeat coroutines per
connection. Phase 1 wires only the per-user global events plus subscribe /
unsubscribe / heartbeat plumbing; ``send_message`` / ``stop`` are stubbed to
return a structured "not yet implemented" error so the wire format is
locked in before Phase 3 turns them on.

Auth model (mirrors ``chat/routes/websocket.py``):

* On open, try the signed session cookie first, then the ``?api_key=`` query
  param. Either yields a ``user`` dict or we close with code 4401.
* On every heartbeat (every 25s ping, with a 60s pong deadline) we
  re-validate the cookie. A revoked / rotated / expired cookie causes a
  4401 close so the client can probe ``GET /app/api/me`` and either
  reconnect or trigger the unauth flow.

Subscriptions are time-bounded by a server-side TTL. ``subscribe`` doubles as
a refresh op: every call extends the deadline by ``SUBSCRIPTION_TTL_SECONDS``.
A per-connection sweep loop runs every ``SUBSCRIPTION_SWEEP_INTERVAL_SECONDS``
and unsubscribes (plus emits ``subscription_expired``) any conversation whose
deadline has passed. The explicit ``unsubscribe`` op is still honored for
deliberate teardown flows. On close the reader unwinds every remaining
subscription. Per-connection bounded queue (256 events) drives backpressure:
a conversation overflow downgrades that conversation's stream to ``resync``;
a user-channel overflow closes the connection with code 4503.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from auth.config import COOKIE_NAME
from auth.session import get_user_from_cookie
from db.user_store import get_user_by_api_key, get_user_by_id

from chat.auth import check_user_allowed
from chat.realtime.bus import SubscriberQueue, bus
from chat.realtime.replay_buffer import replay_buffer
from chat._flush_helper import FLUSH_EVENT_TYPES, make_flush_callback
from chat.gemini_api import (
    cancel_pending_wait_handles_for_conversation,
    remove_chat_session,
    run_conversation_turn,
)
from chat.routes._helpers import _save_interrupted_sdk_history
from chat.storage import ChatStorage, utc_timestamp

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/app/api",
    tags=["realtime"],
)


# Heartbeat / auth-revalidation tunings.
PING_INTERVAL_SECONDS = 25
PONG_DEADLINE_SECONDS = 60
AUTH_REVALIDATE_INTERVAL_SECONDS = 300

# Subscription TTL tunings. Each ``subscribe`` op (initial or refresh) sets a
# deadline ``SUBSCRIPTION_TTL_SECONDS`` ahead. A sweep loop running every
# ``SUBSCRIPTION_SWEEP_INTERVAL_SECONDS`` evicts any subscription whose
# deadline has passed. The frontend refresh interval (3 min) is well inside
# the 5 min TTL even after background-tab ``setTimeout`` throttling.
SUBSCRIPTION_TTL_SECONDS = 300
SUBSCRIPTION_SWEEP_INTERVAL_SECONDS = 30

# Close codes (custom 4xxx range so they don't collide with WS reserved codes).
CLOSE_AUTH_FAILED = 4401
CLOSE_HEARTBEAT_TIMEOUT = 4408
CLOSE_QUEUE_OVERFLOW = 4503
CLOSE_INTERNAL = 4500


class _Connection:
    """Per-WS state container.

    Holds the outbound queue, the active set of subscriptions, and the
    timestamps the heartbeat loop watches. Created in the request-scoped
    handler; never escapes it.
    """

    def __init__(self, websocket: WebSocket, user: dict) -> None:
        self.websocket = websocket
        self.user = user
        self.user_id: int = user["id"]
        self.queue: SubscriberQueue = SubscriberQueue()
        # Conversation subscriptions held by this connection. Keys are
        # conversation ids; values are monotonic-clock deadlines after which
        # the sweep loop will evict the entry. ``subscribe`` extends the
        # deadline; explicit ``unsubscribe`` removes immediately.
        self.conversation_subs: dict[str, float] = {}
        # In-flight model run tasks owned by this connection, keyed by
        # conversation_id. Used by ``stop`` to cancel only the run started
        # from this connection (the cross-tab send lock is enforced at the
        # module level by ``_active_send_runs``).
        self.send_tasks: dict[str, asyncio.Task] = {}
        # Timestamps for heartbeat / auth revalidation.
        self.last_pong_at: float = time.monotonic()
        self.last_auth_check_at: float = time.monotonic()
        # Set when any coroutine wants the connection to be closed; the
        # writer loop observes this and exits cleanly.
        self.shutdown: asyncio.Event = asyncio.Event()


# Module-level per-conversation send registry. The presence of an in-flight
# task in this map blocks a second tab's ``send_message`` for the same
# conversation; the rejected sender gets ``send_message_rejected`` and the
# composer can re-enable when it observes a ``message_appended`` from the
# winning run. Cleared on task completion (success, error, or cancel).
_active_send_runs: dict[str, asyncio.Task] = {}


def get_active_send_run(conversation_id: str) -> Optional[asyncio.Task]:
    """Return the in-flight WS send task for a conversation, if any.

    Used by ``chat.wait_handles.resume`` as the other half of the
    send-vs-resume cross-guard: a headless resume must not start while a
    send run is driving the same shared in-memory session.
    """
    task = _active_send_runs.get(conversation_id)
    if task is not None and not task.done():
        return task
    return None


def is_run_active(conversation_id: str) -> bool:
    """True while a model run is streaming on this conversation.

    Covers the WS ``send_message`` run and a headless wait-handle resume
    that has passed its hold gates (``chat.wait_handles.resume``). Reported
    as ``run_active`` on every ``subscribed`` ack so a client that (re)opens
    the channel can reconcile its streaming state: the run-lifecycle
    envelopes (``resume_started`` / ``send_message_finished``) are
    transient, so one published while the client's socket was down is
    gone -- without this field a tab that reconnects across the end of a
    run keeps its "generating" spinner and locked composer until reload.

    Both registries drop a run in the same event-loop step that publishes
    its ``send_message_finished`` (no await between the publish and the
    task returning), so an ack computed after that publish never reports a
    finished run as live.
    """
    if get_active_send_run(conversation_id) is not None:
        return True
    try:
        from chat.wait_handles import resume as wait_resume
        return wait_resume.is_resume_streaming(conversation_id)
    except Exception:
        logger.debug(
            "[realtime] resume liveness check failed (conversation=%s)",
            conversation_id, exc_info=True,
        )
        return False


@router.websocket("/stream")
async def stream_endpoint(websocket: WebSocket, api_key: Optional[str] = None) -> None:
    """The single persistent WS for a browser session.

    Args:
        websocket: FastAPI WebSocket handle.
        api_key: Optional API key (used by non-cookie clients).
    """
    await websocket.accept()

    user = await _authenticate(websocket, api_key)
    if user is None:
        try:
            await websocket.close(code=CLOSE_AUTH_FAILED)
        except Exception:
            pass
        return

    conn = _Connection(websocket, user)
    bus.subscribe_user(conn.user_id, conn.queue)

    reader_task = asyncio.create_task(_reader_loop(conn))
    writer_task = asyncio.create_task(_writer_loop(conn))
    heartbeat_task = asyncio.create_task(_heartbeat_loop(conn))
    sweep_task = asyncio.create_task(_subscription_sweep_loop(conn))

    try:
        # Wait until any one of the coroutines exits, then unwind the rest.
        # We deliberately do not call .result() so cancellation propagates
        # cleanly without raising into the request scope.
        done, pending = await asyncio.wait(
            {reader_task, writer_task, heartbeat_task, sweep_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        bus.unsubscribe_user(conn.user_id, conn.queue)
        for conv_id in list(conn.conversation_subs.keys()):
            bus.unsubscribe_conversation(conv_id, conn.queue)
        conn.conversation_subs.clear()
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def _authenticate(
    websocket: WebSocket, api_key: Optional[str],
) -> Optional[dict]:
    """Return the authenticated user dict, or ``None`` to refuse the socket."""
    user: Optional[dict] = None

    signed_cookie = websocket.cookies.get(COOKIE_NAME)
    if signed_cookie:
        try:
            user = await get_user_from_cookie(signed_cookie)
        except Exception:
            logger.exception("[realtime] cookie auth raised")
            user = None

    if user is None and api_key:
        try:
            user = await get_user_by_api_key(api_key)
        except Exception:
            logger.exception("[realtime] api_key auth raised")
            user = None

    if user is None:
        return None

    if not check_user_allowed(user.get("email", "")):
        return None

    return user


async def _revalidate_auth(conn: _Connection) -> bool:
    """Re-check the WS's session cookie. Returns True if still valid."""
    signed_cookie = conn.websocket.cookies.get(COOKIE_NAME)
    if not signed_cookie:
        # Cookie may have been deleted (logout); refuse.
        return False
    try:
        user = await get_user_from_cookie(signed_cookie)
    except Exception:
        logger.exception("[realtime] heartbeat auth check raised")
        return False
    if user is None:
        return False
    # Identity change (impersonation rotated underneath us): refuse so the
    # client opens a fresh socket with the new identity.
    if user["id"] != conn.user_id:
        return False
    return True


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


async def _reader_loop(conn: _Connection) -> None:
    """Read JSON messages from the client and dispatch to op handlers."""
    try:
        while True:
            data = await conn.websocket.receive_json()
            op = data.get("op")
            if op == "subscribe":
                await _handle_subscribe(conn, data)
            elif op == "unsubscribe":
                await _handle_unsubscribe(conn, data)
            elif op == "pong":
                conn.last_pong_at = time.monotonic()
            elif op == "ping":
                # Client-originated ping (its own watchdog). Reply pong so
                # the client's lastFrameAt deadline gets reset and so any
                # reverse-proxy idle timer sees traffic in both directions.
                # Also count it as liveness from the client side.
                conn.last_pong_at = time.monotonic()
                await _send_to_client(conn, {"type": "pong"})
            elif op == "send_message":
                await _handle_send_message(conn, data)
            elif op == "stop":
                await _handle_stop(conn, data)
            else:
                await _send_to_client(conn, {
                    "type": "error",
                    "code": 4400,
                    "error": "unknown_op",
                    "message": f"Unknown op: {op!r}",
                })
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("[realtime] reader loop crashed")
    finally:
        conn.shutdown.set()


async def _handle_subscribe(conn: _Connection, data: dict) -> None:
    conv_id = data.get("conversation_id")
    if not isinstance(conv_id, str) or not conv_id:
        await _send_to_client(conn, {
            "type": "error",
            "code": 4400,
            "error": "invalid_subscribe",
            "message": "conversation_id is required",
        })
        return

    # Verify ownership before subscribing so the channel can't be used for
    # cross-user snooping. Best-effort: a missing row aborts the subscribe.
    from db.conversation_store import get_conversation_meta
    try:
        meta = await get_conversation_meta(conn.user_id, conv_id)
    except Exception:
        logger.exception(
            "[realtime] conversation_meta lookup failed (conversation=%s)",
            conv_id,
        )
        meta = None
    if meta is None:
        await _send_to_client(conn, {
            "type": "error",
            "code": 4404,
            "error": "conversation_not_found",
            "conversation_id": conv_id,
        })
        return

    if conv_id not in conn.conversation_subs:
        bus.subscribe_conversation(conv_id, conn.queue)
    conn.conversation_subs[conv_id] = time.monotonic() + SUBSCRIPTION_TTL_SECONDS

    client_last_seq = data.get("last_seq", 0)
    if not isinstance(client_last_seq, int) or client_last_seq < 0:
        client_last_seq = 0

    # Read the cached high-water seq from the conversations row. Falls
    # back to 0 (== "treat the conversation as empty / fresh") on a missing
    # row, which we already gated on above.
    server_seq = int(meta.get("last_message_seq") or 0)

    # Whether a run is streaming right now, so the client can reconcile
    # its streaming/stop state on (re)subscribe -- see ``is_run_active``.
    run_active = is_run_active(conv_id)

    if client_last_seq >= server_seq:
        await _send_to_client(conn, {
            "type": "subscribed",
            "conversation_id": conv_id,
            "current_seq": server_seq,
            "mode": "up_to_date",
            "run_active": run_active,
        })
        return

    # Try to fulfil from the per-conversation replay ring buffer. ``slice``
    # returns ``None`` when the gap is too large for the buffer to cover --
    # in that case the client must resync via REST.
    sliced = replay_buffer.slice(conv_id, client_last_seq)
    if sliced is None:
        await _send_to_client(conn, {
            "type": "subscribed",
            "conversation_id": conv_id,
            "current_seq": server_seq,
            "mode": "resync",
            "reason": "buffer_evicted",
            "run_active": run_active,
        })
        return

    # ``sliced`` may be empty if the replay buffer has been evicted but
    # the DB cache still shows the higher seq (e.g. process restart).
    # In that case force a resync so the client refetches the body.
    if not sliced and server_seq > client_last_seq:
        await _send_to_client(conn, {
            "type": "subscribed",
            "conversation_id": conv_id,
            "current_seq": server_seq,
            "mode": "resync",
            "reason": "buffer_unavailable",
            "run_active": run_active,
        })
        return

    catchup_messages = [
        {**msg, "seq": seq}
        for (seq, msg) in sliced
    ]
    await _send_to_client(conn, {
        "type": "subscribed",
        "conversation_id": conv_id,
        "current_seq": server_seq,
        "mode": "catchup",
        "messages": catchup_messages,
        "run_active": run_active,
    })


async def _handle_send_message(conn: _Connection, data: dict) -> None:
    """Kick off a model run on the persistent WS control plane.

    Mirrors ``_handle_api_mode`` from the per-turn dispatcher: appends the
    user message, builds the flush callback, and runs ``run_conversation_turn``
    as a cancellable task. The on_event closure additionally publishes
    transient ``text_delta`` / ``tool_started`` / ``sub_agent_*`` events
    on the per-conversation channel so every subscriber sees the live
    streaming UX (not just the sender).
    """
    conv_id = data.get("conversation_id")
    if not isinstance(conv_id, str) or not conv_id:
        await _send_to_client(conn, {
            "type": "error",
            "code": 4400,
            "error": "invalid_send_message",
            "message": "conversation_id is required",
        })
        return

    user_message = data.get("message")
    if not isinstance(user_message, str):
        await _send_to_client(conn, {
            "type": "error",
            "code": 4400,
            "error": "invalid_send_message",
            "message": "message must be a string",
        })
        return

    # Composer attachments (pasted images). Validated below before being
    # forwarded into the model run. An empty-text send is allowed when at
    # least one attachment is present.
    raw_attachments = data.get("attachments")
    attachments: list[dict] = []
    if raw_attachments is not None:
        if not isinstance(raw_attachments, list):
            await _send_to_client(conn, {
                "type": "error",
                "code": 4400,
                "error": "invalid_send_message",
                "message": "attachments must be a list",
            })
            return
        if len(raw_attachments) > 10:
            # Defensive cap; the FE enforces the same limit for UX.
            await _send_to_client(conn, {
                "type": "error",
                "code": 4400,
                "error": "invalid_send_message",
                "message": "attachments may not exceed 10 items",
            })
            return
        _ALLOWED_MIMES = {"image/png", "image/jpeg"}
        for entry in raw_attachments:
            if not isinstance(entry, dict):
                await _send_to_client(conn, {
                    "type": "error",
                    "code": 4400,
                    "error": "invalid_send_message",
                    "message": "attachments must be objects",
                })
                return
            mime_type = entry.get("mime_type")
            workspace_path = entry.get("workspace_path")
            if (
                mime_type not in _ALLOWED_MIMES
                or not isinstance(workspace_path, str)
                or not workspace_path
                or ".." in workspace_path
            ):
                await _send_to_client(conn, {
                    "type": "error",
                    "code": 4400,
                    "error": "invalid_send_message",
                    "message": "attachment is malformed",
                })
                return
            attachments.append({
                "attachment_id": entry.get("attachment_id") or "",
                "filename": entry.get("filename") or "",
                "workspace_path": workspace_path,
                "mime_type": mime_type,
                "size_bytes": int(entry.get("size_bytes") or 0),
            })

    if not user_message.strip() and not attachments:
        await _send_to_client(conn, {
            "type": "error",
            "code": 4400,
            "error": "invalid_send_message",
            "message": "message must be a non-empty string when no attachments are provided",
        })
        return

    # Generic workspace files attached to THIS message (already uploaded
    # out-of-band to /files/upload before the send; only their names ride the
    # frame). Surfaced in the triggering turn's <message_metadata> block so the
    # model knows what was just attached. Absent/empty is fine; a non-list or
    # non-string entries are rejected to keep the metadata builder clean.
    raw_attached_filenames = data.get("attached_filenames")
    attached_filenames: list[str] = []
    if raw_attached_filenames is not None:
        if not isinstance(raw_attached_filenames, list):
            await _send_to_client(conn, {
                "type": "error",
                "code": 4400,
                "error": "invalid_send_message",
                "message": "attached_filenames must be a list",
            })
            return
        for entry in raw_attached_filenames:
            if not isinstance(entry, str) or not entry:
                await _send_to_client(conn, {
                    "type": "error",
                    "code": 4400,
                    "error": "invalid_send_message",
                    "message": "attached_filenames must be a list of strings",
                })
                return
            attached_filenames.append(entry)

    timezone_str = data.get("timezone") or "UTC"
    selected_model = data.get("model")
    guide_id = data.get("guide_id")
    skill_ids = data.get("skill_ids")

    # Out-of-band flag selection (composer Flags popover). Validated below and,
    # on the first message only, merged with any ``%%flags`` magic-line result
    # before being persisted onto the conversation row. Malformed shapes are
    # ignored (treated as no flags), matching the forgiving magic-line parser.
    from chat.conversation_flags import KNOWN_FLAGS
    raw_oob_flags = data.get("flags")
    oob_flags: list[str] = []
    if isinstance(raw_oob_flags, list):
        seen_oob: set[str] = set()
        for entry in raw_oob_flags:
            if not isinstance(entry, str):
                continue
            name = entry.strip().lower()
            if name and name in KNOWN_FLAGS and name not in seen_oob:
                oob_flags.append(name)
                seen_oob.add(name)

    # Verify ownership before doing any work.
    from db.conversation_store import get_conversation_meta
    try:
        meta = await get_conversation_meta(conn.user_id, conv_id)
    except Exception:
        logger.exception(
            "[realtime] get_conversation_meta failed (conversation=%s)",
            conv_id,
        )
        meta = None
    if meta is None:
        await _send_to_client(conn, {
            "type": "error",
            "code": 4404,
            "error": "conversation_not_found",
            "conversation_id": conv_id,
        })
        return

    # Cross-user subagent conversations are read-only for their owner: the
    # loop is driven headlessly by chat/user_subagent.py and interaction
    # happens only through the subagent_return approval card. Inference API
    # conversations (driven headlessly by chat/inference_api.py behind a
    # single blocking HTTP request) are likewise read-only transcripts.
    if (meta.get("origin") or "web") in ("user_subagent", "inference_api"):
        await _send_to_client(conn, {
            "type": "send_message_rejected",
            "conversation_id": conv_id,
            "reason": "read_only_conversation",
        })
        return

    project_id = meta.get("project_id")
    routine_id = meta.get("routine_id")

    # First-message flag parsing. A brand-new conversation has
    # last_message_seq == 0 (the user message has not been appended yet), which
    # is a reliable "first message" signal here. On the first message only, a
    # leading ``%%flags[...]`` magic line activates per-conversation flags and is
    # stripped from the message before it reaches the model / chat_history.json.
    # A ``%%flags`` line in any later message is treated as literal text.
    parsed_flags: list[str] = []
    is_first_message = int(meta.get("last_message_seq") or 0) == 0
    # ``original_message`` is the text we persist/display (the magic ``%%flags``
    # line kept intact so it survives copy-paste into a new conversation).
    # ``model_message`` is what we hand to the model (the magic line stripped, so
    # the LLM is never instructed by it). For non-first-messages and messages
    # without a magic line these two are identical.
    original_message = user_message
    model_message = user_message
    if is_first_message:
        from chat.conversation_flags import parse_flags_line
        recognized, stripped_message = parse_flags_line(user_message)
        if recognized or stripped_message != user_message:
            # The magic line matched; the model sees the stripped body while the
            # persisted/displayed copy (``original_message``) keeps the magic
            # line. Remember the recognized flags so we can persist them just
            # before the append (after the empty-after-strip re-check below, so
            # a rejected send never leaves stray flags on the conversation row).
            model_message = stripped_message
            parsed_flags = recognized
            # Empty-after-strip: if stripping the magic line leaves no real
            # prompt and there are no attachments, reject the send (and do NOT
            # persist flags). The user should put a real prompt after the line.
            if not model_message.strip() and not attachments:
                await _send_to_client(conn, {
                    "type": "error",
                    "code": 4400,
                    "error": "invalid_send_message",
                    "message": (
                        "message must include a prompt after the %%flags[...] "
                        "line (or an attachment)"
                    ),
                })
                return

    # Per-conversation send lock: a second tab racing the same send must
    # be told no rather than smashing chat_history.json + sdk_history.json
    # twice.
    existing = _active_send_runs.get(conv_id)
    if existing is not None and not existing.done():
        await _send_to_client(conn, {
            "type": "send_message_rejected",
            "conversation_id": conv_id,
            "reason": "already_running",
        })
        return

    # Cross-guard against a headless wait-handle resume: approving an
    # action request kicks a background run on this same conversation
    # (chat/wait_handles/resume.py) and the composer unlocks the moment
    # the handle resolves, so a user send can land while that resume is
    # still streaming. Both runs would append to the same in-memory
    # session; interleaved appends orphan tool_use blocks and every
    # later Anthropic call is rejected with a 400. Reject exactly like
    # a concurrent tab send.
    from chat.wait_handles import resume as wait_resume
    if wait_resume.is_resuming(conv_id):
        await _send_to_client(conn, {
            "type": "send_message_rejected",
            "conversation_id": conv_id,
            "reason": "already_running",
        })
        return

    # Reject attachments-during-resume: a pasted-image send into a
    # conversation with a dangling tool_use would need to be folded into
    # the synthesized tool_results, which is delicate and out of scope.
    if attachments:
        try:
            from chat.gemini_api.history import _load_sdk_history
            from chat.llm.config import get_provider_for_model, get_provider_instance

            disk_result = _load_sdk_history(conv_id)
            if disk_result is not None:
                disk_history, disk_provider = disk_result
                effective_provider_name = (
                    get_provider_for_model(selected_model)
                    if selected_model else disk_provider
                )
                if (
                    disk_history
                    and disk_provider == effective_provider_name
                ):
                    provider = get_provider_instance(effective_provider_name)
                    pending = provider.get_pending_tool_use_args_from_history(
                        list(disk_history),
                    )
                    if pending:
                        await _send_to_client(conn, {
                            "type": "send_message_rejected",
                            "conversation_id": conv_id,
                            "reason": "attachments_unsupported_during_resume",
                        })
                        return
        except Exception:
            # The resume-detection is purely defensive; on any failure
            # fall through and let the normal path handle the send.
            logger.debug(
                "[realtime] resume-detection failed (conversation=%s)",
                conv_id, exc_info=True,
            )

    # Expensive-resume gate: resuming a long-idle, long-context conversation
    # on a costly model re-reads the whole context at uncached rates, so the
    # server refuses the turn until the client explicitly acknowledges the
    # cost (the FE shows a warning card and resends with the ack flag after
    # the user clicks "Continue anyway"). The checker's DB-free pre-check
    # (model + idle time from ``meta``) keeps the common send path free of
    # any llm_calls read; check_expensive_resume returns None on any
    # internal failure, so this gate can never block a healthy send.
    if (
        not is_first_message
        and data.get("expensive_resume_acknowledged") is not True
    ):
        from chat.expensive_resume import check_expensive_resume
        if await check_expensive_resume(conv_id, meta) is not None:
            await _send_to_client(conn, {
                "type": "send_message_rejected",
                "conversation_id": conv_id,
                "reason": "expensive_resume_unacknowledged",
            })
            return

    # Merge the recognized first-message flags from the two activation paths:
    # the ``%%flags`` magic line (``parsed_flags``) and the out-of-band composer
    # popover field (``oob_flags``). Out-of-band flags are honored on the first
    # message only; on a later message they are ignored (the setter is idempotent
    # and would no-op anyway, but ignoring early avoids confusion). Both paths are
    # rarely used together, but a defensive union keeps either one working.
    merged_flags: list[str] = list(parsed_flags)
    if is_first_message:
        seen_merged = set(merged_flags)
        for name in oob_flags:
            if name not in seen_merged:
                merged_flags.append(name)
                seen_merged.add(name)

    # Server-global feature gates: a flag whose feature an admin has not
    # enabled (Settings > Features) is dropped here -- covering both
    # activation paths before persistence -- matching the forgiving
    # unknown-flag behavior of the %%flags parser.
    if merged_flags:
        from config.feature_gates import filter_gated_flags
        merged_flags = filter_gated_flags(merged_flags)

    # Persist any recognized first-message flags before the append. This runs
    # only after the empty-after-strip re-check and the send-lock / resume
    # gates above have passed, so a rejected send never leaves stray flags.
    # set_conversation_flags is a no-op when the list is empty or already set.
    if merged_flags:
        from db.conversation_store import set_conversation_flags
        try:
            await set_conversation_flags(conv_id, merged_flags)
        except Exception:
            logger.exception(
                "[realtime] set_conversation_flags failed (conversation=%s)",
                conv_id,
            )
            # Non-fatal: fall through and run with the in-memory merged_flags so
            # the turn still honors the requested behavior.

    # Persist the user message before kicking the run so the new bubble
    # is visible to all subscribers via ``message_appended``.
    user_timestamp = utc_timestamp()
    try:
        user_seq, _user_row = await ChatStorage.append_message(
            conversation_id=conv_id,
            role="user",
            # Persist/display the ORIGINAL text (magic ``%%flags`` line intact)
            # so the visible bubble is copy-paste-able. The sidebar auto-title is
            # derived from a flags-stripped copy inside append_message.
            content=original_message,
            timestamp=user_timestamp,
            attachments=attachments or None,
        )
    except Exception:
        logger.exception(
            "[realtime] append user message failed (conversation=%s)",
            conv_id,
        )
        await _send_to_client(conn, {
            "type": "error",
            "code": CLOSE_INTERNAL,
            "error": "append_failed",
            "conversation_id": conv_id,
        })
        return

    # Delivery receipt for the SENDING tab: the row is on disk, here is its
    # seq. ``message_appended`` fans out to every subscriber, but the sender
    # needs a direct, unambiguous signal that its frame was received --
    # a half-open mobile socket accepts ``ws.send`` into the OS buffer and
    # never delivers it, and the client has no other way to tell that
    # apart from a slow run. ``client_send_id`` is echoed verbatim so the
    # client can match the receipt to the optimistic bubble it stamped.
    ack: dict[str, Any] = {
        "type": "send_message_accepted",
        "conversation_id": conv_id,
        "seq": user_seq,
    }
    client_send_id = data.get("client_send_id")
    if isinstance(client_send_id, str) and client_send_id:
        ack["client_send_id"] = client_send_id
    await _send_to_client(conn, ack)

    # Re-read the user fresh from the DB at the start of each turn.
    # Credential/connection-gated decisions in the run (system:* skill
    # gates, connected-services enumeration, authed_get token loaders,
    # sub-agent spawns) must NEVER depend on the connect-time ``conn.user``
    # snapshot, which goes stale when a service is connected/disconnected
    # mid-session (the persistent WS is held open for the whole browser
    # session and is never re-handshaked on an OAuth connect). This
    # restores parity with the deleted per-turn endpoint, which
    # re-authenticated (and thus re-read the row) on every turn.
    try:
        fresh_user = await get_user_by_id(conn.user_id)
    except Exception:
        # Transient DB error: fall back to the cached snapshot with a
        # logged warning. This matches today's behavior (using conn.user)
        # rather than dropping the user's send.
        logger.warning(
            "[realtime] fresh user re-load failed; falling back to cached "
            "snapshot (user_id=%s)", conn.user_id, exc_info=True,
        )
        fresh_user = conn.user
    else:
        if fresh_user is None:
            # User row is gone (deleted). Reject the send rather than
            # silently running with a stale dict.
            await _send_to_client(conn, {
                "type": "send_message_rejected",
                "conversation_id": conv_id,
                "reason": "user_not_found",
            })
            return
        # Keep the connection snapshot from rotting so any future consumer
        # of conn.user benefits from the fresh read too.
        conn.user = fresh_user

    task = asyncio.create_task(
        _run_send_message(
            app=conn.websocket.app,
            user=fresh_user,
            conversation_id=conv_id,
            # The model receives the STRIPPED text (magic line removed) so it is
            # never instructed by the ``%%flags`` syntax on the first turn.
            user_message=model_message,
            timezone_str=timezone_str,
            selected_model=selected_model,
            guide_id=guide_id,
            project_id=project_id,
            routine_id=routine_id,
            skill_ids=skill_ids,
            attachments=attachments or None,
            # Effective flags for this run: existing row flags win (set on a
            # prior first message), else the flags just merged off this message
            # (magic ``%%flags`` line + out-of-band composer selection).
            flags=(meta.get("flags") or merged_flags) or None,
            attached_filenames=attached_filenames or None,
            # The first message of a routine conversation is the routine
            # prompt auto-sent by the one-click run (ChatPanel's
            # pendingRoutineMessage effect): drive it like the scheduler
            # does, nudging the model once if it ends without calling
            # routine_completed. Later messages are the user's own
            # follow-ups and run as ordinary turns.
            routine_run=bool(routine_id) and is_first_message,
        )
    )
    _active_send_runs[conv_id] = task
    conn.send_tasks[conv_id] = task

    def _cleanup(t: asyncio.Task, cid: str = conv_id) -> None:
        if _active_send_runs.get(cid) is t:
            _active_send_runs.pop(cid, None)
        conn.send_tasks.pop(cid, None)

    task.add_done_callback(_cleanup)


async def _handle_stop(conn: _Connection, data: dict) -> None:
    """Cancel an in-flight run for the conversation if any, plus pending
    wait handles. Mirrors the per-turn ``stop`` semantics verbatim.
    """
    conv_id = data.get("conversation_id")
    if not isinstance(conv_id, str) or not conv_id:
        await _send_to_client(conn, {
            "type": "error",
            "code": 4400,
            "error": "invalid_stop",
            "message": "conversation_id is required",
        })
        return

    # Cancel the task only if this user owns it. Cross-user cancellation
    # is impossible because the lock is keyed by conversation_id and the
    # ownership check on subscribe / send already gates user access.
    task = _active_send_runs.get(conv_id)
    if task is not None and not task.done():
        task.cancel()

    # A headless wait-handle resume (the continuation after an
    # action-request Approve / Revise / Deny) is a stoppable run too: the
    # FE shows the same stop button while it streams, so stop must cancel
    # it the same way it cancels a send run.
    try:
        from chat.wait_handles import resume as wait_resume
        resume_task = wait_resume.get_active_resume(conv_id)
    except Exception:
        resume_task = None
    if resume_task is not None:
        resume_task.cancel()

    try:
        await cancel_pending_wait_handles_for_conversation(
            conn.user_id, conv_id,
        )
    except Exception:
        logger.exception(
            "[realtime] cancel_pending_wait_handles failed "
            "(conversation=%s, user_id=%s)", conv_id, conn.user_id,
        )

    await _send_to_client(conn, {
        "type": "stop_acknowledged",
        "conversation_id": conv_id,
    })


async def _run_send_message(
    *,
    app,
    user: dict,
    conversation_id: str,
    user_message: str,
    timezone_str: str,
    selected_model: Optional[str],
    guide_id: Optional[str],
    project_id: Optional[str],
    routine_id: Optional[str],
    skill_ids: Optional[list[str]],
    attachments: Optional[list[dict]] = None,
    flags: Optional[list[str]] = None,
    attached_filenames: Optional[list[str]] = None,
    routine_run: bool = False,
) -> None:
    """Background task that drives ``run_conversation_turn`` for a persistent-WS
    send. Kept symmetrical with ``_handle_api_mode`` so the regression
    surface is the same: sentinel suspends close themselves via the
    boundary save hook, cancellations save an interrupted SDK history
    plus cancel pending wait handles, and a final flush captures any
    trailing messages.

    ``routine_run`` marks the one-click routine run (the routine prompt as
    the conversation's first message): the turn is driven through
    ``chat.routine_runs.drive_routine_run`` so a run that ends without the
    ``routine_completed`` call gets one follow-up turn, exactly like a
    scheduled run. ``send_message_finished`` is published only after the
    whole run, so the composer stays locked across the nudge turn.
    """
    user_id = user["id"]
    shared_messages: list[dict] = []
    stop_requested_event = asyncio.Event()
    run_failed = False

    flush_new_messages, _ = make_flush_callback(
        conversation_id, shared_messages, log_prefix="[realtime]",
    )

    async def on_event(event: dict) -> None:
        # Durable flush boundary: ``message_appended`` fan-out is handled
        # by make_flush_callback after the disk write succeeds.
        if event.get("type") in FLUSH_EVENT_TYPES:
            await flush_new_messages()

        # Transient mirroring on the conversation channel.
        try:
            _publish_transient_event(conversation_id, event)
        except Exception:
            logger.debug(
                "[realtime] publish_transient_event failed "
                "(conversation=%s, type=%s)",
                conversation_id, event.get("type"),
                exc_info=True,
            )

    turns_started = 0

    async def run_turn(message: str) -> None:
        nonlocal turns_started
        # Attachments ride on the user's own message (always the first
        # turn); a routine nudge turn carries none.
        first_turn = turns_started == 0
        turns_started += 1
        await run_conversation_turn(
            app=app,
            user=user,
            message=message,
            conversation_id=conversation_id,
            timezone=timezone_str,
            model=selected_model,
            on_event=on_event,
            messages_out=shared_messages,
            guide_id=guide_id,
            project_id=project_id,
            routine_id=routine_id,
            skill_ids=skill_ids,
            attachments=attachments if first_turn else None,
            flags=flags,
            attached_filenames=attached_filenames if first_turn else None,
        )

    try:
        if routine_run:
            from chat.routine_runs import drive_routine_run
            await drive_routine_run(
                run_turn,
                prompt=user_message,
                messages_out=shared_messages,
                on_event=on_event,
                conversation_id=conversation_id,
                log_prefix="[realtime]",
            )
        else:
            await run_turn(user_message)
    except asyncio.CancelledError:
        stop_requested_event.set()
        logger.info(
            "[realtime] send_message run cancelled (conversation=%s, user=%s)",
            conversation_id, user.get("email"),
        )
        try:
            _save_interrupted_sdk_history(
                user_id, conversation_id, shared_messages,
            )
            remove_chat_session(user_id, conversation_id)
            await cancel_pending_wait_handles_for_conversation(
                user_id, conversation_id,
            )
        except Exception:
            logger.exception(
                "[realtime] interrupted-cancel cleanup failed "
                "(conversation=%s)", conversation_id,
            )
    except Exception:
        run_failed = True
        logger.exception(
            "[realtime] send_message run failed (conversation=%s)",
            conversation_id,
        )
    finally:
        if stop_requested_event.is_set():
            shared_messages.append({
                "type": "interrupted",
                "timestamp": utc_timestamp(),
            })
        try:
            await flush_new_messages()
        except Exception:
            logger.exception(
                "[realtime] final flush failed (conversation=%s)",
                conversation_id,
            )
        # Tell every subscribed client (sender + onlookers) that this run
        # is done so they can drain any remaining streaming-text buffer
        # and stop showing a "thinking" spinner.
        try:
            bus.publish_to_conversation(conversation_id, {
                "type": "send_message_finished",
                "conversation_id": conversation_id,
                "interrupted": stop_requested_event.is_set(),
                # True when run_conversation_turn raised; the durable error bubble
                # arrives separately via message_appended -- this flag lets
                # clients distinguish "died" from "finished cleanly" (e.g.
                # to skip the success desktop notification).
                "error": run_failed,
            })
        except Exception:
            logger.debug(
                "[realtime] publish send_message_finished failed",
                exc_info=True,
            )


def _publish_transient_event(conversation_id: str, event: dict) -> None:
    """Translate a model-loop event into a transient envelope on the
    per-conversation channel, when applicable.

    Durable events (in ``FLUSH_EVENT_TYPES``) are NOT mirrored here -- they
    fan out as ``message_appended`` from the flush callback. Streaming
    text deltas, sub-agent updates, and the boundary "tool started" hint
    are forwarded verbatim with the conversation_id stamped on the
    envelope so subscribers can route on it.
    """
    event_type = event.get("type")
    if not event_type or event_type in FLUSH_EVENT_TYPES:
        return

    if event_type == "text":
        chunk = event.get("content") or event.get("text") or ""
        if not chunk:
            return
        bus.publish_to_conversation(conversation_id, {
            "type": "text_delta",
            "conversation_id": conversation_id,
            "content": chunk,
        })
        return

    if event_type in (
        "sub_agent_tool_use",
        "sub_agent_tool_result",
        "sub_agent_finished",
    ):
        envelope = {**event, "conversation_id": conversation_id}
        bus.publish_to_conversation(conversation_id, envelope)
        return

    if event_type == "conversation_updated":
        envelope = {**event, "conversation_id": conversation_id}
        bus.publish_to_conversation(conversation_id, envelope)
        return

    # Other event types (errors, anything we don't expect) flow through to
    # the conversation channel verbatim so the client gets visibility
    # without us hard-coding every shape here.
    envelope = {**event, "conversation_id": conversation_id}
    bus.publish_to_conversation(conversation_id, envelope)


async def _handle_unsubscribe(conn: _Connection, data: dict) -> None:
    conv_id = data.get("conversation_id")
    if not isinstance(conv_id, str) or not conv_id:
        return
    if conv_id in conn.conversation_subs:
        conn.conversation_subs.pop(conv_id, None)
        bus.unsubscribe_conversation(conv_id, conn.queue)
    await _send_to_client(conn, {
        "type": "unsubscribed",
        "conversation_id": conv_id,
    })


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


async def _writer_loop(conn: _Connection) -> None:
    """Drain the per-connection queue to the wire."""
    try:
        while True:
            if conn.shutdown.is_set():
                return
            event = await conn.queue.get()

            # User-channel overflow -> close the socket; the client reconnects
            # and recovers. We send a single error first so the close code
            # has a hint when surfaced in dev tools.
            if conn.queue.overflowed and not conn.queue.overflowed_conversations:
                await _send_to_client(conn, {
                    "type": "error",
                    "code": CLOSE_QUEUE_OVERFLOW,
                    "error": "queue_overflow",
                })
                conn.shutdown.set()
                return

            # Per-conversation overflow -> emit resync for each affected
            # conversation, then drain normally.
            if conn.queue.overflowed_conversations:
                for conv_id in list(conn.queue.overflowed_conversations):
                    await _send_to_client(conn, {
                        "type": "resync",
                        "conversation_id": conv_id,
                        "reason": "queue_overflow",
                    })
                conn.queue.overflowed_conversations.clear()
                conn.queue.overflowed = False

            await _send_to_client(conn, event)
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("[realtime] writer loop crashed")
    finally:
        conn.shutdown.set()


async def _send_to_client(conn: _Connection, event: dict[str, Any]) -> None:
    """Best-effort send. Failures set the shutdown flag."""
    try:
        await conn.websocket.send_json(event)
    except WebSocketDisconnect:
        conn.shutdown.set()
    except Exception:
        logger.debug(
            "[realtime] send_json failed (user_id=%s)",
            conn.user_id, exc_info=True,
        )
        conn.shutdown.set()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


async def _heartbeat_loop(conn: _Connection) -> None:
    """Send pings, watch for pong timeout, periodically re-validate auth."""
    try:
        while not conn.shutdown.is_set():
            try:
                await asyncio.wait_for(
                    conn.shutdown.wait(),
                    timeout=PING_INTERVAL_SECONDS,
                )
                # If shutdown fired during the wait, exit cleanly.
                return
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if now - conn.last_pong_at > PONG_DEADLINE_SECONDS:
                logger.info(
                    "[realtime] heartbeat timeout (user_id=%s); closing socket",
                    conn.user_id,
                )
                try:
                    await conn.websocket.close(code=CLOSE_HEARTBEAT_TIMEOUT)
                except Exception:
                    pass
                conn.shutdown.set()
                return

            await _send_to_client(conn, {"type": "ping"})

            if now - conn.last_auth_check_at >= AUTH_REVALIDATE_INTERVAL_SECONDS:
                conn.last_auth_check_at = now
                ok = await _revalidate_auth(conn)
                if not ok:
                    logger.info(
                        "[realtime] auth revalidation failed "
                        "(user_id=%s); closing socket", conn.user_id,
                    )
                    await _send_to_client(conn, {
                        "type": "error",
                        "code": CLOSE_AUTH_FAILED,
                        "error": "auth_revoked",
                    })
                    try:
                        await conn.websocket.close(code=CLOSE_AUTH_FAILED)
                    except Exception:
                        pass
                    conn.shutdown.set()
                    return
    except Exception:
        logger.exception("[realtime] heartbeat loop crashed")
    finally:
        conn.shutdown.set()


# ---------------------------------------------------------------------------
# Subscription sweep
# ---------------------------------------------------------------------------


async def _sweep_expired_subscriptions(
    conn: _Connection, *, now: float,
) -> list[str]:
    """One sweep pass. Returns the list of conv_ids that were evicted.

    Pops the entry from ``conn.conversation_subs`` *before* calling
    ``bus.unsubscribe_conversation`` so a racing ``subscribe`` for the same
    conversation always sees a clean "first-time" state and re-adds the bus
    subscription correctly.
    """
    expired = [
        conv_id
        for conv_id, deadline in conn.conversation_subs.items()
        if deadline <= now
    ]
    for conv_id in expired:
        if conn.conversation_subs.pop(conv_id, None) is None:
            continue
        try:
            bus.unsubscribe_conversation(conv_id, conn.queue)
        except Exception:
            logger.exception(
                "[realtime] sweep unsubscribe failed "
                "(conversation=%s, user_id=%s)",
                conv_id, conn.user_id,
            )
        logger.info(
            "[realtime] subscription expired "
            "(user_id=%s, conversation=%s)",
            conn.user_id, conv_id,
        )
        await _send_to_client(conn, {
            "type": "subscription_expired",
            "conversation_id": conv_id,
        })
    return expired


async def _subscription_sweep_loop(conn: _Connection) -> None:
    """Evict per-conversation subscriptions whose deadline has elapsed."""
    try:
        while not conn.shutdown.is_set():
            try:
                await asyncio.wait_for(
                    conn.shutdown.wait(),
                    timeout=SUBSCRIPTION_SWEEP_INTERVAL_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                pass
            await _sweep_expired_subscriptions(conn, now=time.monotonic())
    except Exception:
        logger.exception("[realtime] subscription sweep loop crashed")
    finally:
        conn.shutdown.set()
