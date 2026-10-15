"""Slack Socket Mode background worker.

Connects to Slack via Socket Mode, filters DMs to the bot, and routes
them into Quest conversations. Top-level DMs create a new conversation
and kick off a headless model run; threaded replies are either routed
into an existing run (via the pending-future machinery in
``slack_driven_runtime``) or start a fresh turn on the same
conversation.

Mirrors the lifecycle pattern of chat/scheduler.py -- started/stopped
from the FastAPI lifespan in quest.py.
"""

import asyncio
import logging

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from auth.config import load_slack_bot_token, load_slack_socket_mode_token
from chat import slack_driven_runtime
from chat._flush_helper import FLUSH_EVENT_TYPES, make_flush_callback
from chat.slack_conversation_store import (
    get_slack_conversation,
)
from chat.storage import ChatStorage, utc_timestamp
from db.user_store import get_user_by_slack_user_id

logger = logging.getLogger(__name__)

# Max chars of message text to include in a log line. Truncated with "..."
# appended when exceeded so log output stays readable.
_MAX_TEXT_LOG_CHARS = 500

# Module-level state -- a single Socket Mode client per process.
_socket_client: SocketModeClient | None = None
_bot_user_id: str | None = None
_app_ref = None


async def start(app) -> None:
    """Connect the Socket Mode client and install the DM handler.

    Called from the FastAPI lifespan during startup. Logs and returns on any
    configuration problem so the rest of the server can still boot.
    """
    global _socket_client, _bot_user_id, _app_ref

    app_token = load_slack_socket_mode_token()
    if not app_token:
        logger.info("[slack-socket] App-Level Token not configured; Socket Mode disabled")
        return

    # load_slack_bot_token() raises HTTPException when missing; for Socket
    # Mode we treat that as "disabled" rather than fatal.
    try:
        bot_token = load_slack_bot_token()
    except Exception:
        logger.warning("[slack-socket] bot_token missing; Socket Mode disabled")
        return
    if not bot_token:
        logger.warning("[slack-socket] bot_token missing; Socket Mode disabled")
        return

    web_client = AsyncWebClient(token=bot_token)
    slack_driven_runtime.set_web_client(web_client)
    # Cache the app reference so the slack_driven_runtime debounce flush
    # can call wait_resume.maybe_kick_resume on slack_reply resolution.
    slack_driven_runtime.set_app_ref(app)

    # Resolve the bot's own user ID so the handler can drop self-echoes.
    # Best-effort: if auth_test fails we continue with _bot_user_id=None.
    try:
        auth = await web_client.auth_test()
        _bot_user_id = auth.get("user_id")
    except Exception:
        logger.exception("[slack-socket] auth_test failed; continuing without self user_id")
        _bot_user_id = None

    _app_ref = app
    _socket_client = SocketModeClient(app_token=app_token, web_client=web_client)
    _socket_client.socket_mode_request_listeners.append(_handle_socket_mode_request)

    await _socket_client.connect()
    logger.info("[slack-socket] Connected in Socket Mode (bot_user_id=%s)", _bot_user_id)


async def shutdown() -> None:
    """Close the Socket Mode client and reset module state.

    Safe to call when start() never ran or returned early; no-ops in that case.
    """
    global _socket_client, _bot_user_id, _app_ref

    # Drain pending futures / debounces / active runs before closing the
    # socket so suspended model runs unblock and terminate cleanly.
    try:
        await slack_driven_runtime.shutdown()
    except Exception:
        logger.exception("[slack-socket] slack_driven_runtime.shutdown raised")

    if _socket_client is None:
        _app_ref = None
        return

    try:
        await _socket_client.close()
    except Exception:
        logger.exception("[slack-socket] Error closing Socket Mode client")
    finally:
        _socket_client = None
        _bot_user_id = None
        _app_ref = None
        slack_driven_runtime.set_web_client(None)

    logger.info("[slack-socket] Socket Mode client closed")


async def _handle_socket_mode_request(
    client: SocketModeClient, req: SocketModeRequest
) -> None:
    """Ack every envelope, filter for plain user DMs, and dispatch.

    Slack redelivers and eventually drops connections for unacked envelopes,
    so the ack must go out before we do any work. Exceptions from filter or
    handler are caught so they cannot leak back into the SDK.
    """
    # Ack first -- Slack requires a response within ~3 seconds regardless
    # of whether we care about the envelope.
    try:
        response = SocketModeResponse(envelope_id=req.envelope_id)
        await client.send_socket_mode_response(response)
    except Exception:
        logger.exception("[slack-socket] Failed to ack envelope")
        # Fall through: even if ack failed, don't throw -- the SDK will
        # reconnect on its own and we shouldn't process the event either.
        return

    try:
        if req.type != "events_api":
            return

        payload = req.payload or {}
        event = payload.get("event") or {}

        if event.get("type") != "message":
            return
        if event.get("channel_type") != "im":
            return
        # subtype is set for edits, deletions, file_share, bot_message,
        # channel_join, etc. Only plain user-sent DMs have no subtype.
        if event.get("subtype"):
            return
        if event.get("bot_id"):
            return
        if _bot_user_id and event.get("user") == _bot_user_id:
            return

        slack_user_id = event.get("user") or ""
        channel = event.get("channel") or ""
        ts = event.get("ts") or ""
        thread_ts = event.get("thread_ts")
        text = event.get("text") or ""

        log_text = text
        if len(log_text) > _MAX_TEXT_LOG_CHARS:
            log_text = log_text[:_MAX_TEXT_LOG_CHARS] + "..."
        logger.info(
            "[slack-socket] DM received channel=%s user=%s ts=%s thread_ts=%s text=%r",
            channel, slack_user_id, ts, thread_ts, log_text,
        )

        # Resolve the Quest user whose slack_oauth.user_id matches.
        user = await get_user_by_slack_user_id(slack_user_id)
        if user is None:
            logger.warning(
                "[slack-socket] No Quest user linked to slack_user_id=%s; dropping DM",
                slack_user_id,
            )
            return

        is_top_level = (thread_ts is None) or (thread_ts == ts)

        if is_top_level:
            await _handle_top_level_dm(
                user=user,
                channel=channel,
                ts=ts,
                text=text,
                slack_user_id=slack_user_id,
            )
        else:
            await _handle_threaded_reply(
                user=user,
                channel=channel,
                ts=ts,
                thread_ts=thread_ts,
                text=text,
                slack_user_id=slack_user_id,
            )
    except Exception:
        logger.exception("[slack-socket] Handler error")


async def _handle_top_level_dm(
    user: dict, channel: str, ts: str, text: str, slack_user_id: str,
) -> None:
    """Create a new Slack-driven conversation and kick off a model run."""
    # Honour the user's preferred model for Slack-driven conversations.
    # Unknown/stale IDs are treated as "unset" so the server default applies.
    from chat.llm.config import resolve_model
    slack_model = (user.get("settings") or {}).get("slack_default_model")
    if slack_model and resolve_model(slack_model) is None:
        slack_model = None

    try:
        conversation_id = await ChatStorage.create_slack_conversation(
            user_id=user["id"],
            slack_channel_id=channel,
            slack_thread_ts=ts,
            slack_user_id=slack_user_id,
            model=slack_model,
        )
    except Exception:
        logger.exception(
            "[slack-socket] Failed to create Slack conversation for user=%s",
            user.get("email"),
        )
        return

    try:
        from chat.realtime import bus, events as realtime_events
        bus.publish_to_user(
            user["id"],
            realtime_events.make_conversation_list_changed(
                conversation_id=conversation_id,
                action="created",
            ),
        )
    except Exception:
        logger.debug(
            "[slack-socket] publish conversation_list_changed failed",
            exc_info=True,
        )

    try:
        await ChatStorage.append_message(
            conversation_id=conversation_id,
            role="user",
            content=text,
            timestamp=utc_timestamp(),
        )
    except Exception:
        logger.exception(
            "[slack-socket] Failed to append initial user message (conversation=%s)",
            conversation_id,
        )

    # Show the typing indicator (best-effort) before kicking off the run.
    try:
        await slack_driven_runtime.set_typing(channel, ts, "Quest is thinking...")
    except Exception:
        pass

    _start_model_run(
        user=user,
        conversation_id=conversation_id,
        channel=channel,
        thread_ts=ts,
        text=text,
    )


async def _handle_threaded_reply(
    user: dict, channel: str, ts: str, thread_ts: str, text: str,
    slack_user_id: str,
) -> None:
    """Route a threaded reply to either a pending future or a new turn."""
    row = await get_slack_conversation(channel, thread_ts)
    if row is None:
        # Thread root is not tracked -- likely a reply to a bot-authored
        # message or an unrelated thread. Ignore silently per the spec.
        logger.info(
            "[slack-socket] Dropping threaded reply to untracked thread channel=%s thread_ts=%s",
            channel, thread_ts,
        )
        return

    if row["user_id"] != user["id"]:
        logger.warning(
            "[slack-socket] Threaded reply from user %s does not own conversation "
            "%s (owned by user_id=%s); dropping",
            user.get("email"), row["conversation_id"], row["user_id"],
        )
        return

    conversation_id = row["conversation_id"]

    # If the model is currently suspended inside
    # send_slack_reply_and_get_response, route the text through the debounce
    # machinery which will collate and resolve the future. We also append the
    # user's message to chat_history.json so the web UI's refresh view shows
    # the full back-and-forth -- without this the Slack reply is only visible
    # inside the tool_result JSON and never as a standalone user bubble.
    resolved = await slack_driven_runtime.enqueue_user_reply(channel, thread_ts, text)
    if resolved:
        try:
            await ChatStorage.append_message(
                conversation_id=conversation_id,
                role="user",
                content=text,
                timestamp=utc_timestamp(),
            )
        except Exception:
            logger.exception(
                "[slack-socket] Failed to persist threaded user message "
                "to chat history (conversation=%s)",
                conversation_id,
            )
        return

    # No pending future. If a run is still in-flight (e.g. it has just
    # finished a tool call but hasn't yet called send_slack_reply_..., or
    # it returned without calling the tool at all), the simplest safe
    # behaviour is to drop the reply to avoid a concurrent second run
    # against the same conversation. We log it so we can observe the case.
    if slack_driven_runtime.is_run_active(conversation_id):
        logger.info(
            "[slack-socket] Dropping threaded reply: active run in-flight without "
            "pending future (conversation=%s)",
            conversation_id,
        )
        return

    # No active run -- the previous turn has fully completed (or the
    # server was restarted while it was suspended). sdk_history.json
    # should exist either from the end-of-loop save (normal completion)
    # or from the save run_conversation_turn makes right after the model
    # response that called send_slack_reply_and_get_response (before the
    # tool is dispatched and suspends). If it is missing the
    # model will see an empty session, so log a warning to make the case
    # visible in production logs.
    try:
        from chat.gemini_api.constants import _SDK_HISTORY_FILENAME
        sdk_history_path = ChatStorage._get_conversation_dir(
            conversation_id,
        ) / _SDK_HISTORY_FILENAME
        if not sdk_history_path.exists():
            logger.warning(
                "[slack-socket] Resuming Slack thread (conversation=%s, "
                "channel=%s, thread_ts=%s) with no sdk_history.json on disk; "
                "the model will see no prior context for this turn.",
                conversation_id, channel, thread_ts,
            )
    except Exception:
        logger.debug(
            "[slack-socket] Failed sdk_history.json existence check "
            "(conversation=%s)",
            conversation_id,
            exc_info=True,
        )

    # Persist the user message and start a fresh turn on the same conversation.
    try:
        await ChatStorage.append_message(
            conversation_id=conversation_id,
            role="user",
            content=text,
            timestamp=utc_timestamp(),
        )
    except Exception:
        logger.exception(
            "[slack-socket] Failed to append threaded user message (conversation=%s)",
            conversation_id,
        )
        return

    try:
        await slack_driven_runtime.set_typing(channel, thread_ts, "Quest is working...")
    except Exception:
        pass

    _start_model_run(
        user=user,
        conversation_id=conversation_id,
        channel=channel,
        thread_ts=thread_ts,
        text=text,
    )


def _start_model_run(
    user: dict,
    conversation_id: str,
    channel: str,
    thread_ts: str,
    text: str,
) -> None:
    """Kick off a headless model run as an asyncio task."""
    app = _app_ref
    if app is None:
        logger.error(
            "[slack-socket] App reference missing; cannot dispatch model run for "
            "conversation=%s",
            conversation_id,
        )
        return

    task = asyncio.create_task(
        _dispatch_slack_model_run(
            app=app,
            user=user,
            conversation_id=conversation_id,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
        )
    )
    slack_driven_runtime.register_run(conversation_id, task)
    task.add_done_callback(
        lambda t, cid=conversation_id: slack_driven_runtime.unregister_run(cid)
    )


async def _dispatch_slack_model_run(
    app,
    user: dict,
    conversation_id: str,
    channel: str,
    thread_ts: str,
    text: str,
) -> None:
    """Run a headless Gemini/Claude turn for a Slack-driven conversation.

    Mirrors chat/scheduler.py:_execute_scheduled_run but:
    - Uses origin="slack" so the system prompt and tools reflect Slack mode.
    - Passes slack_context so the send_slack_reply_and_get_response tool
      knows where to post.
    - Persists messages_out to chat_history.json incrementally as the
      model produces structured events. Incremental persistence is
      critical here because the model suspends inside
      ``send_slack_reply_and_get_response`` awaiting the user's next
      reply; without it the web UI would show only the initial user
      message until the conversation eventually exits (which for a
      long-running Slack thread may be hours or never).
    """
    from chat.gemini_api import run_conversation_turn
    from db.conversation_store import get_conversation_meta

    meta = await get_conversation_meta(user["id"], conversation_id)
    model = (meta or {}).get("model")

    messages_out: list[dict] = []

    _flush_new_messages, _ = make_flush_callback(
        conversation_id, messages_out, log_prefix="[slack-socket]",
    )

    async def on_event(event: dict) -> None:
        if event.get("type") in FLUSH_EVENT_TYPES:
            await _flush_new_messages()

    suspended_for_slack_reply = False
    try:
        await run_conversation_turn(
            app=app,
            user=user,
            message=text,
            conversation_id=conversation_id,
            timezone="UTC",
            model=model,
            on_event=on_event,
            messages_out=messages_out,
            origin="slack",
            slack_context={"channel_id": channel, "thread_ts": thread_ts},
        )
        # Detect a slack_reply suspend (run_conversation_turn swallows the
        # sentinel into a normal return) by inspecting the saved
        # transcript for a dangling send_slack_reply_and_get_response
        # tool_use. If suspended, Slack already auto-cleared the typing
        # indicator via chat.postMessage and we must not race the resume
        # bucket. Otherwise, the run truly ended and we clear the
        # indicator below to avoid a stale "Quest is working..." sticking.
        try:
            from chat.wait_handles.resume import (
                _conversation_has_dangling_slack_reply,
            )
            suspended_for_slack_reply = _conversation_has_dangling_slack_reply(
                conversation_id,
            )
        except Exception:
            suspended_for_slack_reply = False
    except asyncio.CancelledError:
        logger.info(
            "[slack-socket] Model run cancelled (conversation=%s)",
            conversation_id,
        )
        # Save the in-memory SDK session history before the process tears
        # down. Without this, a shutdown that cancels the run while the
        # model is suspended inside send_slack_reply_and_get_response
        # leaves sdk_history.json either absent or out of date, so the
        # next user reply in this thread (on the restarted server) would
        # rebuild an empty session. We use the same interrupted-save
        # helper the web path uses so the cancellation marker keeps the
        # assistant-role alternation valid.
        try:
            from chat.routes._helpers import _save_interrupted_sdk_history
            from chat.gemini_api.session import remove_chat_session
            _save_interrupted_sdk_history(
                user["id"], conversation_id, messages_out,
            )
            # Discard the in-memory session so any follow-up turn in this
            # process rebuilds cleanly from the just-saved sdk_history.json
            # (mirrors the web-mode cancellation behaviour).
            remove_chat_session(user["id"], conversation_id)
        except Exception:
            logger.exception(
                "[slack-socket] Failed to save interrupted SDK history "
                "(conversation=%s)",
                conversation_id,
            )
        # Still persist what we have below, then re-raise.
        raise
    except Exception:
        logger.exception(
            "[slack-socket] Model run failed (conversation=%s, user=%s)",
            conversation_id, user.get("email"),
        )
    finally:
        try:
            await _flush_new_messages()
        except Exception:
            logger.exception(
                "[slack-socket] Final flush failed (conversation=%s)",
                conversation_id,
            )
        # Clear the Slack typing indicator unless the model suspended on
        # another slack_reply wait handle (in which case Slack already
        # cleared it on the chat.postMessage and the resume task will
        # re-set it when processing actually resumes).
        if not suspended_for_slack_reply:
            try:
                await slack_driven_runtime.set_typing(channel, thread_ts, "")
            except Exception:
                logger.debug(
                    "[slack-socket] set_typing clear on dispatch end failed",
                    exc_info=True,
                )
