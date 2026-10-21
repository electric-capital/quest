#!/usr/bin/env python3
"""Browser sidecar agent: the process inside a public conversation's
browser container (see Dockerfile.browser-sidecar and
docs/architecture/public-browser.md).

It owns one headless Chromium (Playwright) and answers JSON requests on a
Unix socket in the mounted ``/browser-state`` directory. The host side
(chat/browser_sessions.py) connects to that socket; nothing listens on
TCP, and the container has no credential and no route to the host.

Protocol: ``POST /<action>`` with a JSON object body; the response is a
JSON object. Every action except ``health`` and ``screenshot`` answers
with the page state (``url``, ``title``, ``snapshot``) after the action
settled, so the model sees the page it is now on without a second call.
Errors come back as ``{"error": "..."}`` with HTTP 400 (bad request) or
200 (the action ran but failed, e.g. a navigation timeout) so the host
can relay them to the model verbatim.

The server is single-threaded on purpose: Playwright's sync API is bound
to one thread, and one conversation drives one browser one step at a
time. The host serialises calls per session as well.

Cookies, local storage and session storage are saved to
``/browser-state/storage_state.json`` after every action and restored when
the agent starts, so logins survive the container's idle reap. Nothing
else of the profile is persisted.
"""

import base64
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import socketserver
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

logging.basicConfig(
    level=logging.INFO, stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("browser-agent")

STATE_DIR = os.environ.get("QUEST_BROWSER_STATE_DIR", "/browser-state")
SOCKET_PATH = os.path.join(STATE_DIR, "agent.sock")
STORAGE_STATE_PATH = os.path.join(STATE_DIR, "storage_state.json")

VIEWPORT = {"width": 1280, "height": 900}
# Navigation / action timeouts (ms). Kept well under the host's per-call
# timeout so a hung page surfaces as a Playwright error, not a dead socket.
NAVIGATION_TIMEOUT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
SETTLE_TIMEOUT_MS = 5_000
MAX_WAIT_SECONDS = 15
# Snapshot and evaluate result caps (characters) -- the text goes straight
# into the model's context.
SNAPSHOT_MAX_CHARS = 40_000
EVALUATE_MAX_CHARS = 20_000
MAX_BODY_BYTES = 256 * 1024
# Refs look like "e12"; inside an iframe they carry a frame prefix ("f1e12").
REF_RE = re.compile(r"^(?:f\d+)*e\d+$")
ALLOWED_SCHEMES = ("http", "https")

CHROMIUM_ARGS = [
    # The container is the sandbox: the entrypoint cleared every
    # capability and set no-new-privs, so Chromium's own setuid /
    # namespace sandboxes cannot start.
    "--no-sandbox",
    "--disable-setuid-sandbox",
    # /dev/shm is small in a container; use /tmp for shared memory.
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


class AgentError(Exception):
    """A request error worth relaying to the model as-is."""


class Browser:
    """One Chromium, one context, the currently active page."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(args=CHROMIUM_ARGS)
        context_kwargs = {"viewport": VIEWPORT, "accept_downloads": False}
        if os.path.exists(STORAGE_STATE_PATH):
            try:
                context_kwargs["storage_state"] = STORAGE_STATE_PATH
            except Exception:  # pragma: no cover - defensive
                pass
        try:
            self._context = self._browser.new_context(**context_kwargs)
        except Exception:
            # A corrupt storage_state.json must not keep the browser from
            # starting; begin with a clean context instead.
            logger.exception("Could not restore storage state; starting fresh")
            context_kwargs.pop("storage_state", None)
            self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        self._context.on("page", self._on_new_page)
        self.page = self._context.new_page()
        self._install_page_hooks(self.page)
        self.last_dialog = None
        self.started_at = time.time()

    # -- page bookkeeping -------------------------------------------------

    def _on_new_page(self, page):
        # A link that opened a new tab (target=_blank, window.open): follow
        # it, as a person would, so the next snapshot shows it.
        self._install_page_hooks(page)
        self.page = page

    def _install_page_hooks(self, page):
        def _dialog(dialog):
            # Accept alerts / confirms / prompts so they never block the
            # page; the message is reported with the next page state.
            self.last_dialog = {"type": dialog.type, "message": dialog.message}
            try:
                dialog.accept()
            except Exception:
                pass
        page.on("dialog", _dialog)

    def _ensure_page(self):
        if self.page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()]
            self.page = pages[-1] if pages else self._context.new_page()
            if not pages:
                self._install_page_hooks(self.page)
        return self.page

    def _settle(self):
        page = self._ensure_page()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass

    def _save_state(self):
        try:
            self._context.storage_state(path=STORAGE_STATE_PATH)
        except Exception:
            logger.warning("Could not save storage state", exc_info=True)

    def _locator(self, ref):
        if not isinstance(ref, str) or not REF_RE.match(ref):
            raise AgentError(
                "ref must be an element reference from the latest snapshot, "
                "like 'e12'."
            )
        return self._ensure_page().locator(f"aria-ref={ref}")

    def page_state(self):
        page = self._ensure_page()
        try:
            snapshot = page.locator("body").aria_snapshot(mode="ai")
        except Exception as exc:
            snapshot = f"(snapshot unavailable: {exc})"
        truncated = False
        if len(snapshot) > SNAPSHOT_MAX_CHARS:
            snapshot = snapshot[:SNAPSHOT_MAX_CHARS]
            truncated = True
        try:
            title = page.title()
        except Exception:
            title = ""
        state = {
            "url": page.url,
            "title": title,
            "snapshot": snapshot,
        }
        if truncated:
            state["snapshot_truncated"] = True
            state["note"] = (
                f"The snapshot was cut at {SNAPSHOT_MAX_CHARS} characters. "
                "Use browser_evaluate to extract specific content, or "
                "navigate to a more specific page."
            )
        if self.last_dialog is not None:
            state["dialog"] = dict(self.last_dialog, handled="accepted")
            self.last_dialog = None
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        if len(open_pages) > 1:
            state["open_tabs"] = len(open_pages)
        return state

    # -- actions ----------------------------------------------------------

    def health(self, params):
        return {
            "ok": True,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "url": self._ensure_page().url,
        }

    def navigate(self, params):
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            raise AgentError("url is required.")
        url = url.strip()
        if "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            raise AgentError("Only http and https URLs can be opened.")
        # The container's network rules reject every private destination;
        # a literal private address would only time out, so refuse it with
        # a clear message instead. (A hostname resolving to one is rejected
        # by the same rules at connect time.)
        try:
            address = ipaddress.ip_address(parts.hostname or "")
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise AgentError(
                "Private, loopback and link-local addresses are not reachable "
                "from this browser; only the public internet is."
            )
        page = self._ensure_page()
        page.goto(url, wait_until="domcontentloaded")
        self._settle()
        self._save_state()
        return self.page_state()

    def back(self, params):
        page = self._ensure_page()
        page.go_back(wait_until="domcontentloaded")
        self._settle()
        return self.page_state()

    def snapshot(self, params):
        return self.page_state()

    def click(self, params):
        locator = self._locator(params.get("ref"))
        if params.get("double"):
            locator.dblclick()
        else:
            locator.click()
        self._settle()
        self._save_state()
        return self.page_state()

    def hover(self, params):
        self._locator(params.get("ref")).hover()
        return self.page_state()

    def type(self, params):
        text = params.get("text")
        if not isinstance(text, str):
            raise AgentError("text is required.")
        locator = self._locator(params.get("ref"))
        if params.get("append"):
            locator.click()
            locator.press_sequentially(text)
        else:
            locator.fill(text)
        if params.get("submit"):
            locator.press("Enter")
            self._settle()
        self._save_state()
        return self.page_state()

    def select(self, params):
        values = params.get("values")
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not values or \
                not all(isinstance(v, str) for v in values):
            raise AgentError("values must be a non-empty list of option values or labels.")
        locator = self._locator(params.get("ref"))
        try:
            locator.select_option(values)
        except Exception:
            locator.select_option(label=values)
        self._settle()
        return self.page_state()

    def press(self, params):
        key = params.get("key")
        if not isinstance(key, str) or not key.strip():
            raise AgentError("key is required (for example 'Enter', 'Tab', 'PageDown').")
        self._ensure_page().keyboard.press(key.strip())
        self._settle()
        self._save_state()
        return self.page_state()

    def scroll(self, params):
        amount = params.get("amount", 600)
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise AgentError("amount must be an integer number of pixels.")
        amount = max(-5000, min(5000, amount))
        page = self._ensure_page()
        ref = params.get("ref")
        if ref:
            self._locator(ref).scroll_into_view_if_needed()
        else:
            page.mouse.wheel(0, amount)
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass
        return self.page_state()

    def screenshot(self, params):
        page = self._ensure_page()
        full_page = bool(params.get("full_page"))
        png = page.screenshot(type="png", full_page=full_page)
        try:
            title = page.title()
        except Exception:
            title = ""
        return {
            "url": page.url,
            "title": title,
            "png_base64": base64.b64encode(png).decode("ascii"),
            "full_page": full_page,
        }

    def evaluate(self, params):
        expression = params.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise AgentError("expression is required.")
        page = self._ensure_page()
        result = page.evaluate(expression)
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        out = {"result": result if len(text) <= EVALUATE_MAX_CHARS else None}
        if len(text) > EVALUATE_MAX_CHARS:
            out["result_text"] = text[:EVALUATE_MAX_CHARS]
            out["truncated"] = True
            out["note"] = f"The result was cut at {EVALUATE_MAX_CHARS} characters."
        return out

    def wait(self, params):
        page = self._ensure_page()
        text = params.get("text")
        seconds = params.get("seconds")
        if text:
            if not isinstance(text, str):
                raise AgentError("text must be a string.")
            page.get_by_text(text).first.wait_for(
                state="visible", timeout=MAX_WAIT_SECONDS * 1000,
            )
        elif seconds is not None:
            try:
                seconds = float(seconds)
            except (TypeError, ValueError):
                raise AgentError("seconds must be a number.")
            page.wait_for_timeout(max(0.0, min(seconds, MAX_WAIT_SECONDS)) * 1000)
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=MAX_WAIT_SECONDS * 1000)
            except Exception:
                pass
        return self.page_state()

    def close_tab(self, params):
        page = self._ensure_page()
        open_pages = [p for p in self._context.pages if not p.is_closed()]
        if len(open_pages) <= 1:
            page.goto("about:blank")
        else:
            page.close()
        return self.page_state()


ACTIONS = {
    "health", "navigate", "back", "snapshot", "click", "hover", "type",
    "select", "press", "scroll", "screenshot", "evaluate", "wait",
    "close_tab",
}


class UnixHTTPServer(socketserver.UnixStreamServer, HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.UnixStreamServer.server_bind(self)
        self.server_name = "browser"
        self.server_port = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    browser: Browser = None

    def log_message(self, fmt, *args):  # quiet: the host logs calls
        pass

    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._run("health", {})
        self._send(404, {"error": "unknown action"})

    def do_POST(self):
        action = self.path.strip("/")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            return self._send(413, {"error": "request body too large"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            params = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "request body must be JSON"})
        if not isinstance(params, dict):
            return self._send(400, {"error": "request body must be a JSON object"})
        self._run(action, params)

    def _run(self, action, params):
        if action not in ACTIONS:
            return self._send(404, {"error": f"unknown action '{action}'"})
        try:
            result = getattr(self.browser, action)(params)
        except AgentError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # Playwright errors: relay the message
            message = str(exc).split("\nCall log:")[0].strip()
            logger.info("action %s failed: %s", action, message)
            return self._send(200, {"error": f"{type(exc).__name__}: {message}"})
        self._send(200, result)


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    logger.info("Starting Chromium")
    Handler.browser = Browser()
    server = UnixHTTPServer(SOCKET_PATH, Handler)
    os.chmod(SOCKET_PATH, 0o660)
    logger.info("Browser agent listening on %s", SOCKET_PATH)

    # The agent is PID 1 in the container (the entrypoint exec's it), and
    # PID 1 ignores signals it has no handler for: without this, podman
    # stop would wait out its grace period and SIGKILL the browser.
    def _terminate(signum, frame):
        logger.info("Signal %s: shutting down", signum)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    try:
        server.serve_forever()
    finally:
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
