"""Shared mock for the suites that drive ``_make_authed_request``.

The ``authed_get`` service tests (core and plugin) all need the same
thing: ``httpx.AsyncClient`` inside ``chat.gemini_api.authed_get`` replaced
by a client that records each call and returns one canned response, so no
test touches the network. Plugin suites import it the same way they import
``tests.plugin_support``.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch


def mock_httpx_client(
    captured_get: list,
    captured_post: list | None = None,
    *,
    status_code: int = 200,
    body=None,
):
    """Patch ``httpx.AsyncClient`` inside authed_get, capturing its calls.

    ``client.get(url, headers=...)`` appends ``(url, headers)`` to
    ``captured_get``. When ``captured_post`` is given,
    ``client.post(url, headers=..., json=...)`` appends
    ``(url, headers, json)`` to it. Every call returns a response with
    ``status_code`` and ``body`` (default ``{"ok": True}``).
    """
    if body is None:
        body = {"ok": True}

    response_mock = MagicMock()
    response_mock.status_code = status_code
    response_mock.text = json.dumps(body)
    response_mock.json = MagicMock(return_value=body)

    async def fake_get(url, headers=None):
        captured_get.append((str(url), dict(headers or {})))
        return response_mock

    client_mock = MagicMock()
    client_mock.get = AsyncMock(side_effect=fake_get)

    if captured_post is not None:
        async def fake_post(url, headers=None, json=None):
            captured_post.append((str(url), dict(headers or {}), json))
            return response_mock

        client_mock.post = AsyncMock(side_effect=fake_post)

    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client_mock)
    async_ctx.__aexit__ = AsyncMock(return_value=None)

    return patch(
        "chat.gemini_api.authed_get.httpx.AsyncClient",
        return_value=async_ctx,
    )
