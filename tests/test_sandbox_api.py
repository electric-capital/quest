"""Tests for the sandbox tool API server (chat/sandbox_api.py).

Script containers are confined to a dedicated loopback-only port that
serves ONLY the script-facing endpoints. These tests pin that surface:
the exact route roster of the sandbox app, the sandbox-port resolution,
the container env injection (``QUEST_PORT`` points at the sandbox port),
and that the in-process server actually serves and shuts down cleanly
without touching signal handlers.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from chat.sandbox_api import (
    _NoSignalServer,
    create_sandbox_app,
    start_sandbox_server,
    stop_sandbox_server,
)
from chat.gemini_api.constants import get_sandbox_port


def _route_set(app) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if method != "HEAD"
    }


# The complete surface reachable from script containers. Adding a route to
# register_sandbox_api_routes() means exposing it to sandboxed code --
# update this set deliberately.
EXPECTED_SANDBOX_ROUTES = {
    ("POST", "/api/tool-call"),
    ("POST", "/api/authed-get"),
    ("POST", "/api/authed-post"),
    ("GET", "/api/gmail-simple/messages/{message_id}"),
    ("GET", "/api/gmail-simple/messages"),
    ("GET", "/api/gmail-simple/labels"),
    ("GET", "/api/gmail-simple/labels/{label_id}"),
    ("GET", "/api/gmail-simple/urls/{message_id}/{identifiers}"),
    ("GET", "/api/gmail-simple/urls/{message_id}"),
    ("POST", "/api/gmail-simple/drafts"),
    ("POST", "/api/gmail-simple/send-self"),
    ("GET", "/health"),
}


class TestSandboxAppSurface:
    def test_exact_route_roster(self):
        """The sandbox app serves the script-facing endpoints and nothing else."""
        assert _route_set(create_sandbox_app()) == EXPECTED_SANDBOX_ROUTES

    def test_no_docs_endpoints(self):
        app = create_sandbox_app()
        assert app.docs_url is None
        assert app.openapi_url is None

    def test_main_app_serves_the_same_roster(self):
        """The main app must keep the shared routes: the LLM's in-process
        curl_proxy_* dispatch resolves against quest.app's route table.

        Runs in a subprocess because importing quest loads the plugins into
        the process-global registries, which would pollute other tests.
        """
        import json
        import subprocess
        import sys
        from pathlib import Path

        script = (
            "import json, quest\n"
            "routes = sorted({(m, r.path) for r in quest.app.routes"
            " if hasattr(r, 'methods') for m in r.methods})\n"
            "print(json.dumps(routes))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        assert proc.returncode == 0, proc.stderr
        main_routes = {tuple(r) for r in json.loads(proc.stdout)}
        for method_path in EXPECTED_SANDBOX_ROUTES - {("GET", "/health")}:
            assert method_path in main_routes, method_path


class TestSandboxPort:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("QUEST_SANDBOX_PORT", "12345")
        assert get_sandbox_port() == "12345"

    def test_defaults_to_main_port_plus_one(self, monkeypatch):
        monkeypatch.delenv("QUEST_SANDBOX_PORT", raising=False)
        monkeypatch.setenv("QUEST_PORT", "9300")
        assert get_sandbox_port() == "9301"

    def test_container_env_uses_sandbox_port(self, monkeypatch):
        """Script containers get QUEST_PORT pointed at the sandbox port, so
        the entrypoint's socat/iptables confine them to that port."""
        from chat.gemini_api.tool_handlers import _build_script_podman_cmd

        monkeypatch.setenv("QUEST_PORT", "9300")
        monkeypatch.setenv("QUEST_SANDBOX_PORT", "9301")
        cmd = _build_script_podman_cmd("/ws", ["python3", "x.py"], "sk-key")
        assert "QUEST_PORT=9301" in cmd
        assert "QUEST_PORT=9300" not in cmd

    def test_validate_url_accepts_sandbox_port(self, monkeypatch):
        from chat.route_dispatch import validate_url

        monkeypatch.setenv("QUEST_PORT", "9300")
        monkeypatch.setenv("QUEST_SANDBOX_PORT", "9301")
        path, _ = validate_url("http://localhost:9301/api/tool-call")
        assert path == "/api/tool-call"
        with pytest.raises(ValueError):
            validate_url("http://localhost:9302/api/tool-call")

    @pytest.mark.parametrize("path", [
        "/app/api/tables",
        "/app/api/tables/x",
        "/app/api/tables/x/rows",
        "/app/api/tables/x/schema",
        "/app/api/tables/x/restore",
    ])
    def test_validate_url_refuses_ui_table_routes(self, path):
        """curl_proxy can never reach the Quest Tables UI routes: they live
        under /app/api, and validate_url admits /api/* only (so, like the
        docs routes, they need no _BLOCKED_PROXY_PATHS entry)."""
        from chat.route_dispatch import validate_url

        with pytest.raises(ValueError):
            validate_url(f"http://localhost:8000{path}")


class TestSandboxServerRuntime:
    def test_serves_health_and_stops_cleanly(self):
        """The in-process server binds loopback, answers /health, and shuts
        down via stop_sandbox_server."""

        async def _run() -> None:
            import socket

            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]

            server, task = start_sandbox_server(port)
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.05)
                assert server.started, "sandbox server never started"
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/health")
                assert resp.status_code == 200
                assert resp.json() == {"status": "ok", "server": "sandbox"}
                # A non-sandbox path must not exist on this port.
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://127.0.0.1:{port}/api/conversations"
                    )
                assert resp.status_code == 404
            finally:
                await stop_sandbox_server(server, task)
            assert task.done()

        asyncio.run(_run())

    def test_capture_signals_is_noop(self):
        """The subclassed server must not install process signal handlers
        (it runs beside the main uvicorn server)."""
        import signal

        import uvicorn

        server = _NoSignalServer(uvicorn.Config(create_sandbox_app()))
        before = signal.getsignal(signal.SIGTERM)
        with server.capture_signals():
            assert signal.getsignal(signal.SIGTERM) is before
