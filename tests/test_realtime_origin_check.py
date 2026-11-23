"""The persistent WebSocket only honours the session cookie same-origin.

Browsers send cookies on a WebSocket handshake from any page on the same
site and do not apply the same-origin policy to it, so the server compares
the handshake's Origin with its own address before trusting the cookie
(``_cookie_origin_allowed`` / ``_authenticate`` in chat/realtime/socket.py).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from chat.realtime import socket as socket_mod


def _run(coro):
    return asyncio.run(coro)


def _ws(headers: dict, cookie: str | None = "signed"):
    cookies = {socket_mod.COOKIE_NAME: cookie} if cookie else {}
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()}, cookies=cookies,
    )


@pytest.fixture()
def config(monkeypatch):
    """Server config seen by the check: {} unless a test fills it."""
    import config.server_config as server_config

    values: dict = {}
    monkeypatch.setattr(server_config, "load_server_config", lambda: values)
    monkeypatch.delenv("QUEST_APP_BASE_URL", raising=False)
    return values


@pytest.mark.parametrize("headers", [
    # Not a browser page: no Origin at all.
    {"Host": "quest.example.com"},
    # Same origin, with and without a port, any scheme, any case.
    {"Host": "quest.example.com", "Origin": "https://quest.example.com"},
    {"Host": "10.1.2.3:9100", "Origin": "http://10.1.2.3:9100"},
    {"Host": "Quest.Example.com", "Origin": "https://quest.example.COM"},
    # A proxy that rewrites Host but forwards the original one.
    {"Host": "127.0.0.1:8000", "X-Forwarded-Host": "quest.example.com",
     "Origin": "https://quest.example.com"},
])
def test_same_origin_handshakes_are_allowed(config, headers):
    assert socket_mod._cookie_origin_allowed(_ws(headers)) is True


@pytest.mark.parametrize("headers", [
    # Another site entirely.
    {"Host": "quest.example.com", "Origin": "https://evil.example.org"},
    # Same site, different origin: sibling subdomain and another port.
    {"Host": "quest.example.com", "Origin": "https://other.example.com"},
    {"Host": "10.1.2.3:9100", "Origin": "http://10.1.2.3:8080"},
    {"Host": "localhost:9100", "Origin": "http://localhost:3000"},
    # Host as a prefix or suffix of the origin is not a match.
    {"Host": "quest.example.com", "Origin": "https://quest.example.com.evil.org"},
    {"Host": "quest.example.com", "Origin": "https://notquest.example.com"},
    # Opaque origins.
    {"Host": "quest.example.com", "Origin": "null"},
])
def test_cross_origin_handshakes_are_refused(config, headers):
    assert socket_mod._cookie_origin_allowed(_ws(headers)) is False


def test_configured_public_url_and_oauth_hostname_are_allowed(config):
    headers = {"Host": "127.0.0.1:8000", "Origin": "https://quest.example.com"}
    assert socket_mod._cookie_origin_allowed(_ws(headers)) is False

    config["app_base_url"] = "https://quest.example.com/"
    assert socket_mod._cookie_origin_allowed(_ws(headers)) is True

    config.clear()
    config["oauth_hostname"] = "dev.example.com"
    dev = {"Host": "10.1.2.3:9100", "Origin": "http://dev.example.com:9100"}
    assert socket_mod._cookie_origin_allowed(_ws(dev)) is True
    other = {"Host": "10.1.2.3:9100", "Origin": "http://dev.example.com.evil.org:9100"}
    assert socket_mod._cookie_origin_allowed(_ws(other)) is False


def _stub_auth(monkeypatch):
    cookie_user = {"id": 1, "email": "cookie@example.com"}
    key_user = {"id": 2, "email": "key@example.com"}

    async def from_cookie(signed):
        return cookie_user

    async def by_api_key(key):
        return key_user if key == "good-key" else None

    monkeypatch.setattr(socket_mod, "get_user_from_cookie", from_cookie)
    monkeypatch.setattr(socket_mod, "get_user_by_api_key", by_api_key)
    monkeypatch.setattr(socket_mod, "check_user_allowed", lambda email: True)
    return cookie_user, key_user


def test_authenticate_ignores_the_cookie_cross_origin(config, monkeypatch):
    cookie_user, key_user = _stub_auth(monkeypatch)

    same = _ws({"Host": "quest.example.com", "Origin": "https://quest.example.com"})
    assert _run(socket_mod._authenticate(same, None)) is cookie_user

    cross = _ws({"Host": "quest.example.com", "Origin": "https://other.example.com"})
    assert _run(socket_mod._authenticate(cross, None)) is None
    # An explicit API key is not ambient, so it still authenticates.
    assert _run(socket_mod._authenticate(cross, "good-key")) is key_user

    # Non-browser client with a cookie jar and no Origin keeps working.
    plain = _ws({"Host": "quest.example.com"})
    assert _run(socket_mod._authenticate(plain, None)) is cookie_user


def test_real_handshake_same_origin_connects_and_cross_origin_is_closed(
    config, monkeypatch,
):
    """Through a real ASGI WebSocket handshake on the mounted router."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    _stub_auth(monkeypatch)
    app = FastAPI()
    app.include_router(socket_mod.router)
    path = "/app/api/stream"
    client = TestClient(app, cookies={socket_mod.COOKIE_NAME: "signed"})

    # TestClient's Host is "testserver".
    with client.websocket_connect(path, headers={"Origin": "http://testserver"}) as ws:
        ws.send_json({"op": "ping"})
        assert ws.receive_json() == {"type": "pong"}

    with client.websocket_connect(
        path, headers={"Origin": "http://evil.testserver"},
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == socket_mod.CLOSE_AUTH_FAILED
