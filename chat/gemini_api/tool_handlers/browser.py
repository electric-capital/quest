"""Browser tool handlers (public projects only): the ``browser_*`` dynamic
tools, each one agent action on the conversation's browser session
(``chat/browser_sessions.py``).

Every handler first refuses anything that is not a public-project
conversation and then applies the ``browser`` feature gate for the acting
user, before any container work. See docs/architecture/public-browser.md.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from chat.browser_sessions import BrowserError, browser_sessions
from chat.gemini_api.tool_handlers._common import (
    conversation_workspace_dir,
    _publish_file_list_changed,
)

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = "screenshots"
_SCREENSHOT_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def browser_disabled_message() -> str:
    return (
        "The browser is not enabled for you on this server (Settings > "
        "Features > Browser in public projects)."
    )


def _gate(ctx) -> str | None:
    """The error JSON refusing this call, or None when it may proceed."""
    if not ctx.is_public:
        return json.dumps({
            "error": "browser_unavailable",
            "message": (
                "The browser tools are only available in conversations of a "
                "public project (they drive an internet-only browser with no "
                "access to internal data)."
            ),
        })
    if not ctx.conversation_id:
        return json.dumps({
            "error": "browser_unavailable",
            "message": "The browser needs a conversation to belong to.",
        })
    from config.feature_gates import browser_enabled_for
    if not browser_enabled_for(ctx.user.get("email") or ""):
        return json.dumps({
            "error": "browser_disabled",
            "message": browser_disabled_message(),
        })
    return None


async def _call(ctx, action: str, params: dict[str, Any]) -> dict | str:
    """Run ``action`` on the conversation's session; a BrowserError
    becomes the error JSON the model sees."""
    try:
        return await browser_sessions.call(
            ctx.conversation_id, ctx.user["id"], action, params,
        )
    except BrowserError as exc:
        return json.dumps({"error": "browser_error", "message": str(exc)})


async def _simple(ctx, action: str, params: dict[str, Any]) -> str:
    refused = _gate(ctx)
    if refused:
        return refused
    result = await _call(ctx, action, params)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _str(args: dict, key: str) -> str:
    value = args.get(key)
    return value if isinstance(value, str) else ""


async def handle_browser_navigate(ctx, args: dict) -> str:
    return await _simple(ctx, "navigate", {"url": _str(args, "url")})


async def handle_browser_snapshot(ctx, args: dict) -> str:
    return await _simple(ctx, "snapshot", {})


async def handle_browser_click(ctx, args: dict) -> str:
    return await _simple(ctx, "click", {
        "ref": _str(args, "ref"), "double": bool(args.get("double")),
    })


async def handle_browser_type(ctx, args: dict) -> str:
    return await _simple(ctx, "type", {
        "ref": _str(args, "ref"),
        "text": args.get("text"),
        "submit": bool(args.get("submit")),
        "append": bool(args.get("append")),
    })


async def handle_browser_select(ctx, args: dict) -> str:
    return await _simple(ctx, "select", {
        "ref": _str(args, "ref"), "values": args.get("values"),
    })


async def handle_browser_press(ctx, args: dict) -> str:
    return await _simple(ctx, "press", {"key": _str(args, "key")})


async def handle_browser_hover(ctx, args: dict) -> str:
    return await _simple(ctx, "hover", {"ref": _str(args, "ref")})


async def handle_browser_scroll(ctx, args: dict) -> str:
    params: dict[str, Any] = {"amount": args.get("amount", 600)}
    if args.get("ref"):
        params["ref"] = _str(args, "ref")
    return await _simple(ctx, "scroll", params)


async def handle_browser_back(ctx, args: dict) -> str:
    return await _simple(ctx, "back", {})


async def handle_browser_wait(ctx, args: dict) -> str:
    params: dict[str, Any] = {}
    if args.get("text"):
        params["text"] = _str(args, "text")
    if args.get("seconds") is not None:
        params["seconds"] = args.get("seconds")
    return await _simple(ctx, "wait", params)


async def handle_browser_evaluate(ctx, args: dict) -> str:
    return await _simple(ctx, "evaluate", {"expression": _str(args, "expression")})


async def handle_browser_close(ctx, args: dict) -> str:
    refused = _gate(ctx)
    if refused:
        return refused
    stopped = await browser_sessions.stop(ctx.conversation_id, reason="browser_close")
    return json.dumps({
        "closed": stopped,
        "note": (
            "The browser session was closed; logins are kept for the next call."
            if stopped else "No browser session was open."
        ),
    })


def _screenshot_name(title: str, url: str, n: int) -> str:
    base = title.strip() or url.split("//", 1)[-1].split("/", 1)[0] or "page"
    base = _SCREENSHOT_NAME_RE.sub("-", base).strip("-.")[:60] or "page"
    return f"{SCREENSHOT_DIR}/{base}-{n}.png"


async def handle_browser_screenshot(ctx, args: dict) -> tuple[str, list]:
    """Capture the page, write the PNG into the conversation workspace and
    hand it to the model as an inline image part."""
    refused = _gate(ctx)
    if refused:
        return refused, []
    result = await _call(ctx, "screenshot", {"full_page": bool(args.get("full_page"))})
    if isinstance(result, str):
        return result, []
    if "error" in result:
        return json.dumps(result, ensure_ascii=False), []

    png = base64.b64decode(result.get("png_base64") or "")
    root = await conversation_workspace_dir(ctx.conversation_id)
    requested = _str(args, "path").strip().lstrip("/").lstrip("\\")
    if requested:
        if ".." in requested:
            return json.dumps({"error": "Invalid path: path traversal not allowed"}), []
        if not requested.lower().endswith(".png"):
            requested += ".png"
        clean_path = requested
    else:
        session = browser_sessions.get(ctx.conversation_id)
        n = session.calls if session else 0
        clean_path = _screenshot_name(
            result.get("title") or "", result.get("url") or "", n,
        )
    file_path = (root / clean_path).resolve()
    try:
        file_path.relative_to(root.resolve())
    except ValueError:
        return json.dumps({"error": "Invalid path: outside workspace directory"}), []
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(png)
    _publish_file_list_changed(
        ctx.user["id"], "conversation", ctx.conversation_id, ctx.project_id,
    )

    extra_parts: list = []
    try:
        uploaded = await ctx.provider.upload_file(
            file_path=str(file_path), mime_type="image/png",
            display_name=file_path.name, model=ctx.model,
        )
        if uploaded is not None:
            extra_parts.append(ctx.provider.make_file_part(uploaded))
    except Exception:
        logger.warning(
            "Screenshot could not be attached inline (conversation=%s)",
            ctx.conversation_id, exc_info=True,
        )
    body = {
        "path": clean_path,
        "size_bytes": len(png),
        "url": result.get("url"),
        "full_page": bool(result.get("full_page")),
    }
    if extra_parts:
        body["note"] = (
            "The screenshot is attached to this result for you to look at. "
            f"Show it to the user with ![...]({clean_path})."
        )
    else:
        body["note"] = (
            "The screenshot was saved to the workspace but could not be "
            f"attached inline for this model; show it with ![...]({clean_path})."
        )
    return json.dumps(body, ensure_ascii=False), extra_parts
