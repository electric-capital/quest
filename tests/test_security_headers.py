"""SecurityHeadersMiddleware (chat/security_headers.py) and its mount in quest.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.testclient import TestClient

from chat.security_headers import SecurityHeadersMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    @app.get("/own")
    async def own():
        return PlainTextResponse(
            "x",
            headers={
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @app.get("/stream")
    async def stream():
        async def chunks():
            yield b"a"
            yield b"b"

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    return app


def test_headers_added_to_ordinary_and_error_responses():
    client = TestClient(_app())
    for path, status in (("/plain", 200), ("/missing", 404)):
        r = client.get(path)
        assert r.status_code == status
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_route_supplied_header_is_kept():
    r = TestClient(_app()).get("/own")
    assert r.headers.get_list("x-frame-options") == ["SAMEORIGIN"]
    assert r.headers.get_list("content-security-policy") == ["default-src 'none'"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_streaming_body_and_websocket_pass_through():
    client = TestClient(_app())
    r = client.get("/stream")
    assert r.text == "ab"
    assert r.headers["x-frame-options"] == "DENY"
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "hi"


def test_quest_app_sends_the_headers():
    # Subprocess: importing quest loads the plugins into the process-global
    # registries (same pattern as tests/route_table_support.py).
    script = (
        "import json, quest\n"
        "from starlette.testclient import TestClient\n"
        "r = TestClient(quest.app).get('/health')\n"
        "print(json.dumps(dict(r.headers)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    headers = json.loads(proc.stdout.strip().splitlines()[-1])
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == "frame-ancestors 'none'"
