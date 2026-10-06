"""
Quest - API Proxy with OAuth & Access Control

A HTTP proxy that sits in front of backend APIs, managing OAuth tokens
and implementing granular access policies.
"""

import asyncio
import logging
import os
import signal
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Auth submodule
from auth import (
    generate_api_key,
    get_current_user_cookie_or_apikey,
    # Routers
    google_login_router,
    google_services_router,
    airtable_router,
    ramp_router,
    service_key_router,
    dev_login_router,
    password_login_router,
)

logger = logging.getLogger("quest")

# Configuration
PROJECT_ROOT = Path(__file__).parent
from config.paths import DATA_DIR, migrate_legacy_database_file
# Instruction text aggregation (moved to api/instructions.py)
from api.instructions import get_instructions_content


async def _backfill_conversation_seq() -> None:
    """One-shot backfill of ``conversations.last_message_seq`` from disk.

    Iterates rows with ``last_message_seq == 0`` and sets it to the highest
    seq stamped in chat_history.json (or, for pre-migration files with no
    seq stamps, ``len(messages)`` so the next append starts at the right
    boundary). Idempotent across boots; subsequent runs are no-ops once
    the cache is aligned.
    """
    from sqlalchemy import select

    from db.engine import AsyncSessionLocal
    from db.models import Conversation
    from chat.storage import ChatStorage

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation).where(
                (Conversation.last_message_seq == 0)
                | (Conversation.last_message_seq.is_(None))
            )
        )
        rows = result.scalars().all()
        updated = 0
        for conv in rows:
            try:
                high_water = ChatStorage._read_seq_high_water(conv.id)
            except Exception:
                continue
            if high_water > 0:
                conv.last_message_seq = high_water
                updated += 1
        if updated:
            await db.commit()
            logger.info(
                "Backfilled last_message_seq for %d conversation(s)", updated,
            )


async def _backfill_conversation_auto_title() -> None:
    """One-shot backfill of ``conversations.auto_title`` from disk.

    For rows where ``auto_title`` is still NULL and ``custom_name`` is also
    NULL (i.e. the sidebar would otherwise have to open chat_history.json
    to derive the title), opens the file once and caches the slice of the
    first user message. Skips rows that already have a custom name (no
    auto-title needed) or already have one cached. Idempotent across boots.
    """
    import json
    from sqlalchemy import select

    from db.engine import AsyncSessionLocal
    from db.models import Conversation
    from chat.storage import ChatStorage, _compute_auto_title

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation).where(
                Conversation.auto_title.is_(None),
                Conversation.custom_name.is_(None),
            )
        )
        rows = result.scalars().all()
        updated = 0
        for conv in rows:
            chat_file = ChatStorage._get_chat_history_file(conv.id)
            try:
                with open(chat_file, "r") as f:
                    chat_data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                continue
            for msg in chat_data.get("messages") or []:
                if msg.get("role") == "user":
                    content = msg.get("content")
                    title = _compute_auto_title(content) if isinstance(content, str) else ""
                    if title:
                        conv.auto_title = title
                        updated += 1
                    break
        if updated:
            await db.commit()
            logger.info(
                "Backfilled auto_title for %d conversation(s)", updated,
            )

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown tasks."""
    # Startup: Ensure data directory and subdirectories exist
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "logs").mkdir(exist_ok=True)

    # Pick up a database created before the praixy -> quest project rename
    # (covers manual uvicorn startups that bypass run.py).
    migrate_legacy_database_file()

    # Unlock the encryption-at-rest key up front so a missing/wrong password
    # fails the boot with a clear message instead of surfacing as a decrypt
    # error on the first credential read. run.py already created/verified
    # the key file (config.encryption init); this covers manual uvicorn
    # startups. Local mode auto-creates the file under the dev sentinel.
    from config import encryption
    try:
        encryption.get_data_key()
    except encryption.EncryptionError as exc:
        logger.critical("Encryption at rest is not available: %s", exc)
        raise

    # Encrypt any credential store file still in plaintext (written by the
    # stdlib-only bootstrap wizard / local pre-baking before the key was
    # available). Runs before the legacy migration below so migrated
    # sections are written encrypted too.
    from config.service_credentials import encrypt_plaintext_credential_files
    from config.inference_providers import encrypt_plaintext_inference_files
    try:
        encrypt_plaintext_credential_files()
        encrypt_plaintext_inference_files()
    except Exception:
        logger.exception("Credential file encryption sweep failed; continuing")

    # One-time copy of known server_credentials.json sections into the
    # per-service credential store (data/service_credentials/*.json).
    # Idempotent: services that already have a per-service file are skipped,
    # so credentials saved through the admin UI are never overwritten.
    from config.service_credentials import migrate_legacy_credentials
    try:
        migrate_legacy_credentials()
    except Exception:
        logger.exception("Service credential migration failed; continuing")

    # Flag ADC-vs-config Vertex project drift (key without configured
    # project id, or configured id pointing at a different project than the
    # key) so misconfigurations surface at boot instead of at send time.
    from config.server_config import check_vertex_credentials_consistency
    try:
        check_vertex_credentials_consistency()
    except Exception:
        logger.exception("Vertex credentials consistency check failed; continuing")
    # Kick off per-model health checks in the background: one minimal live
    # inference call per configured model (none when no provider is
    # configured), recorded in the model-health store that the admin
    # Inference Providers panel reads and re-triggers. Backgrounded so a
    # slow provider never delays boot.
    from chat.llm.health import run_startup_model_checks
    model_health_task = asyncio.create_task(run_startup_model_checks())

    # Start the routine scheduler background task
    from chat.scheduler import scheduler_loop
    scheduler_task = asyncio.create_task(scheduler_loop(app))
    logger.info("Routine scheduler background task started")

    # Start the Slack pending-request notifier (bot DM reminders about
    # open action requests). Inert when Slack is not configured.
    from chat.slack_notifier import notifier_loop
    slack_notifier_task = asyncio.create_task(notifier_loop())
    logger.info("Slack pending-request notifier background task started")

    # Cache the app reference for the cross-user subagent runtime so
    # handler execute() paths and background run tasks can kick headless
    # resumes (mirrors slack_driven_runtime.set_app_ref, but installed
    # unconditionally -- user subagents don't depend on Slack being on).
    from chat import user_subagent
    user_subagent.set_app_ref(app)

    # Apply admin passwords the prod bootstrap wizard hashed before the
    # database existed (<data_dir>/pending_admin_passwords.json), then drop
    # expired/used set-password links. Best-effort.
    from auth.password_login import apply_pending_admin_passwords
    from db.password_store import purge_expired_password_tokens
    try:
        await apply_pending_admin_passwords(DATA_DIR)
        await purge_expired_password_tokens()
    except Exception:
        logger.exception("Password sign-in startup tasks failed; continuing")

    # Start the Slack Socket Mode client (plumbing-only: logs DMs).
    # Guarded so any Slack-side problem cannot prevent the server from booting.
    from chat import slack_socket_mode
    try:
        await slack_socket_mode.start(app)
    except Exception:
        logger.exception("Slack Socket Mode start failed; continuing without it")

    # Backfill ``conversations.last_message_seq`` for rows that still show 0
    # but whose chat_history.json has messages on disk. Idempotent: a row
    # that has already been bumped by a write since the migration is
    # skipped on subsequent boots. Best-effort; a failure here must not
    # stop the server from booting.
    try:
        await _backfill_conversation_seq()
    except Exception:
        logger.exception("last_message_seq backfill failed; continuing")

    # Backfill cached ``auto_title`` for conversations created before the
    # auto_title column existed. Falls back to a per-row file read in
    # ``ChatStorage.list_conversations`` if this hasn't completed yet, so
    # the sidebar is always correct -- this just removes the per-list cost.
    try:
        await _backfill_conversation_auto_title()
    except Exception:
        logger.exception("auto_title backfill failed; continuing")

    # Remove any symlinks from the conversation/project data trees. The
    # sandbox seccomp profile bans creating new ones (see
    # chat/gemini_api/sandbox_seccomp.py); this sweep clears any planted
    # before that fix landed or restored from an old backup. Off-thread so
    # a large install's walk never delays boot; best-effort.
    from chat.workspace_symlinks import scrub_workspace_symlinks
    try:
        removed_links = await asyncio.to_thread(scrub_workspace_symlinks)
        if removed_links:
            logger.warning(
                "Workspace symlink scrub removed %d banned symlink(s)",
                removed_links,
            )
    except Exception:
        logger.exception("Workspace symlink scrub failed; continuing")

    # Resolve (and log) the sandbox OCI runtime up front so the first
    # run_script/run_python call doesn't pay the crun probe.
    from chat.gemini_api.sandbox_runtime import describe_sandbox_runtime
    try:
        logger.info(
            "Sandbox OCI runtime: %s",
            await asyncio.to_thread(describe_sandbox_runtime),
        )
    except Exception:
        logger.exception("Sandbox runtime detection failed; continuing")

    # Start the replay-buffer idle eviction sweep.
    from chat.realtime.replay_buffer import eviction_loop
    eviction_task = asyncio.create_task(eviction_loop())

    # Start the loopback-only sandbox tool API server: the sole surface
    # run_script/run_python containers can reach (their QUEST_PORT points
    # here, and their iptables rules block every other host port). Guarded
    # so a port conflict degrades to unavailable script API access instead
    # of preventing boot.
    from chat.gemini_api.constants import get_sandbox_port
    from chat.sandbox_api import start_sandbox_server, stop_sandbox_server
    sandbox_server = sandbox_server_task = None
    try:
        sandbox_server, sandbox_server_task = start_sandbox_server(
            int(get_sandbox_port())
        )
    except Exception:
        logger.exception(
            "Sandbox tool API server failed to start; "
            "run_script/run_python API access will be unavailable"
        )

    # Create a shutdown event so the admin API can trigger server shutdown
    shutdown_event = asyncio.Event()
    app.state.shutdown_event = shutdown_event

    async def _wait_for_shutdown_event():
        """Background task that waits for the shutdown event and sends SIGTERM."""
        await shutdown_event.wait()
        logger.info("Shutdown event received, sending SIGTERM to self after brief delay")
        await asyncio.sleep(1)  # Allow HTTP response to be delivered
        os.kill(os.getpid(), signal.SIGTERM)

    shutdown_waiter = asyncio.create_task(_wait_for_shutdown_event())

    yield

    # Shutdown: stop the sandbox tool API server
    if sandbox_server is not None:
        await stop_sandbox_server(sandbox_server, sandbox_server_task)

    # Shutdown: cancel any still-running startup model health checks
    model_health_task.cancel()
    try:
        await model_health_task
    except asyncio.CancelledError:
        pass

    # Shutdown: cancel the shutdown waiter (no longer needed)
    shutdown_waiter.cancel()
    try:
        await shutdown_waiter
    except asyncio.CancelledError:
        pass

    # Shutdown: cancel the scheduler loop
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    logger.info("Routine scheduler background task stopped")

    # Shutdown: cancel the Slack pending-request notifier loop
    slack_notifier_task.cancel()
    try:
        await slack_notifier_task
    except asyncio.CancelledError:
        pass

    # Shutdown: cancel the replay-buffer eviction sweep.
    eviction_task.cancel()
    try:
        await eviction_task
    except asyncio.CancelledError:
        pass

    # Shutdown: cancel all in-progress scheduled execution tasks
    from chat import scheduler
    await scheduler.shutdown()

    # Shutdown: close the Slack Socket Mode client (no-op if never started)
    await slack_socket_mode.shutdown()

    # Wait-handle suspends are entirely DB-driven now -- there are no
    # in-process futures to drain. Pending rows survive the restart and
    # the resume bucket on the next run picks up the dangling tool_use.

    # Shutdown: plugin-owned teardown (e.g. the Telegram plugin closes its
    # long-lived Telethon client connections) via each plugin's
    # on_shutdown hook. Never raises.
    from config.plugins import shutdown_plugins
    await shutdown_plugins()

# Swagger UI / ReDoc live at /api-docs and /api-redoc instead of FastAPI's
# default /docs and /redoc: /docs and /docs/<id> are the SPA's Quest Docs
# routes (served below), and FastAPI registers its docs routes first, so the
# defaults would shadow them. /openapi.json is unchanged.
app = FastAPI(
    title="Quest",
    description="API Proxy with OAuth & Access Control",
    docs_url="/api-docs",
    redoc_url="/api-redoc",
    lifespan=lifespan
)

# Database-backed user data access
from db.user_store import update_user_field

# Register auth routers (before API routes and catch-all static routes)
app.include_router(google_login_router)
app.include_router(google_services_router)
# The Slack OAuth router and the Telegram login router are plugin-provided
# (plugins/slack/oauth.py, plugins/telegram/auth.py), mounted by
# mount_plugin_oauth_routers() below.
app.include_router(airtable_router)
app.include_router(ramp_router)
# Generic per-user API key routes for plugin services (auth/service_key.py)
app.include_router(service_key_router)
app.include_router(dev_login_router)
# Email/password sign-in (active only when server_config.json login_method
# is "password"; auth/password_login.py)
app.include_router(password_login_router)


@app.get("/api/instructions")
async def get_instructions(request: Request, user: dict = Depends(get_current_user_cookie_or_apikey)):
    """Return detailed instructions on how to use the proxy.

    Shows all API docs regardless of connection status (connected_services=None).
    """
    base_url = str(request.base_url).rstrip("/")
    api_key = user["api_key"]

    instructions = get_instructions_content(base_url, api_key)
    return {"instructions": instructions}


@app.post("/api/reset-api-key")
async def api_reset_api_key(user: dict = Depends(get_current_user_cookie_or_apikey)):
    """Reset the user's API key via Bearer token auth and return the new key."""
    email = user["email"]
    new_key = generate_api_key()
    result = await update_user_field(email, api_key=new_key)
    if not result:
        raise HTTPException(status_code=404, detail={"error": "user_not_found", "message": "User not found."})

    logger.info("[Auth] API key reset via API (user=%s)", email)

    return {"api_key": new_key}


# Register API routes
from api import gmail

# Gmail Raw API routes (GET endpoints migrated to authed_get; only batch POST remains)
app.post("/api/gmail-raw/v1/batch")(gmail.batch_request)

# Gmail Simple API routes are registered by register_sandbox_api_routes()
# below (shared with the sandbox tool API server).

# Slack is dedicated dynamic tools only (list_slack_conversations,
# send_slack_dm_to_self, etc. via tool_call), registered by the in-tree
# Slack plugin (plugins/slack) with no HTTP routes; sandbox scripts go
# through the /api/tool-call bridge.

# Telegram is dedicated dynamic tools only (telegram_get_me,
# telegram_list_dialogs, telegram_get_messages, telegram_list_contacts via
# tool_call), registered by the in-tree Telegram plugin (plugins/telegram)
# with no HTTP routes; sandbox scripts go through the /api/tool-call bridge
# and the /api/telegram prefix is blocked in chat/route_dispatch.py.

# Twitter/X access is plugin-provided (plugins/twitter): the
# api.twitter.com authed_get service entry replaced the old /api/twitter/*
# proxy routes, and the plugin's oauth_router serves /auth/twitter.

# Script-facing routes (authed-get/authed-post proxies + the tool-call
# bridge; the Gmail Simple routes above moved here too): registered via the
# shared helper so the main app and the sandbox tool API server
# (chat/sandbox_api.py, the ONLY surface script containers can reach) serve
# the exact same roster. The main app keeps them because the LLM's
# in-process curl_proxy_* dispatch resolves against this app's routes.
from chat.sandbox_api import register_sandbox_api_routes
register_sandbox_api_routes(app)

# One-shot inference API: run a single prompt as the bearer token's user
# and return the final markdown (no streaming). Authenticated ONLY by the
# named keys from Settings > Inference API; blocked from LLM curl_proxy_*
# dispatch (see chat/route_dispatch.py) so a run cannot recurse into
# itself.
from chat.inference_api import inference_endpoint
app.post("/api/inference")(inference_endpoint)

# Chat API routes
from chat.routes import router as chat_router
from chat.file_routes import router as file_router
from chat.memory_routes import router as memory_router
from chat.guide_routes import router as guide_router
from chat.project_routes import router as project_router
from chat.routine_routes import router as routine_router
from chat.schedule_routes import router as schedule_router
from chat.action_request_routes import router as action_request_router
from chat.skill_routes import router as skill_router
from chat.project_skill_routes import router as project_skill_router
from chat.project_db_routes import router as project_db_router
from chat.wait_handle_routes import router as wait_handle_router
from chat.docs.routes import router as docs_router
from chat.realtime import realtime_router

app.include_router(chat_router)
app.include_router(file_router)
app.include_router(memory_router)
app.include_router(guide_router)
app.include_router(project_router)
app.include_router(routine_router)
app.include_router(schedule_router)
app.include_router(action_request_router)
app.include_router(skill_router)
app.include_router(project_skill_router)
app.include_router(project_db_router)
app.include_router(wait_handle_router)
app.include_router(docs_router)
app.include_router(realtime_router)

# Discover and register filesystem plugins (plugins/*/plugin.py) AFTER the
# core registries above are populated: each plugin fans out into the system
# skill catalog, action-request handler registry, tool_call registry,
# authed_get service registry, and script allowlist. Plugins never mount
# /api/* HTTP routes -- bespoke behavior lives in their tool handlers, so
# the LLM surface stays behind the gated tool registry. The one routing
# exception is mounted right after load: oauth-kind user connections'
# browser-facing routers, confined by loader validation to each plugin's
# /auth/<id> namespace (and mounted before the SPA catch-all below).
# Broken plugins are logged and skipped inside load_plugins -- a bad
# plugin never prevents boot. Plugin-owned startup work (e.g. legacy
# credential-store migrations) runs inside load_plugins via each
# plugin's post_load hook.
from config.plugins import load_plugins, mount_plugin_oauth_routers
load_plugins()
mount_plugin_oauth_routers(app)


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}


# Serve React chat app
FRONTEND_BUILD_DIR = PROJECT_ROOT / "frontend" / "dist"

# Mount static files for chat app first (before routes)
if FRONTEND_BUILD_DIR.exists():
    # Mount assets directory (JS, CSS bundles)
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_BUILD_DIR / "assets")),
        name="chat-assets"
    )

# Backward-compatible redirect: /chat* -> /*
@app.get("/chat")
@app.get("/chat/")
@app.get("/chat/{rest:path}")
async def redirect_chat_to_root(rest: str = ""):
    """Redirect old /chat URLs to root for backward compatibility."""
    return RedirectResponse(f"/{rest}" if rest else "/", status_code=301)

@app.get("/")
async def serve_chat_app(request: Request):
    """Serve React chat app (handles auth state internally)."""
    index_path = FRONTEND_BUILD_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Chat app not built. Run 'npm run build' in frontend/"
        )
    return FileResponse(index_path)

# SPA catch-all routes for deep links (must be before /{filename} fallback)
@app.get("/chats/{rest:path}")
async def serve_spa_chats(rest: str):
    """Serve SPA for /chats/* deep links."""
    return FileResponse(FRONTEND_BUILD_DIR / "index.html")

@app.get("/projects/{rest:path}")
async def serve_spa_projects(rest: str):
    """Serve SPA for /projects/* deep links."""
    return FileResponse(FRONTEND_BUILD_DIR / "index.html")

@app.get("/admin/{rest:path}")
async def serve_spa_admin(rest: str):
    """Serve SPA for /admin/* deep links (e.g. /admin/system-reports)."""
    return FileResponse(FRONTEND_BUILD_DIR / "index.html")

@app.get("/inbox")
async def serve_spa_inbox():
    """Serve SPA for the /inbox deep link (the requests inbox view --
    linked from Slack pending-request reminder DMs)."""
    return FileResponse(FRONTEND_BUILD_DIR / "index.html")

@app.get("/docs")
@app.get("/docs/{rest:path}")
async def serve_spa_docs(rest: str = ""):
    """Serve SPA for the Quest Docs deep links (/docs All Docs view,
    /docs?project=<id>, /docs/<id> viewer). Swagger UI moved to /api-docs
    to free this path (see the FastAPI(...) constructor)."""
    return FileResponse(FRONTEND_BUILD_DIR / "index.html")

@app.get("/set-password")
async def serve_spa_set_password():
    """Serve SPA for set-password links (invites, sign-up and password
    resets; the one-time token rides in the URL fragment)."""
    return FileResponse(FRONTEND_BUILD_DIR / "index.html")

# Serve other static files (vite.svg, etc.)
@app.get("/{filename}")
async def serve_chat_static(filename: str):
    """Serve static files from chat app root."""
    file_path = FRONTEND_BUILD_DIR / filename
    if file_path.exists() and file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404)


if __name__ == "__main__":
    import uvicorn
    from chat.logging_config import get_log_config
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=get_log_config())
