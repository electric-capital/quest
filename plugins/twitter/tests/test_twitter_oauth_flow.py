"""Tests for the twitter plugin's OAuth router (plugins/twitter/oauth.py).

The repo's only PKCE flow: the start route's state cookie carries the
S256 code_verifier alongside the CSRF token, the callback exchanges the
code (sending the verifier) and stores the blob into
user_service_credentials.oauth_blob, CSRF/verifier rejection, disconnect,
and the quest.py mount step.
"""

import asyncio
import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

import auth.oauth_state as oauth_state
import plugins.twitter.oauth as oauth_mod
from plugins.twitter.upstream import twitter_needs_reauth


def _run(coro):
    return asyncio.run(coro)


USER = {"id": 7, "email": "u@example.com"}


@pytest.fixture(autouse=True)
def _fixed_secret_key():
    """Sign state cookies with a fixed key so tests never touch the data dir."""
    with patch.object(oauth_state, "get_secret_key", return_value="test-secret"):
        yield


def _signed_state(payload: dict) -> str:
    return oauth_state._serializer().dumps(payload)

_FULL_GRANT = "dm.read dm.write tweet.read users.read bookmark.read offline.access"


class _FakeRequest:
    def __init__(self, cookies=None):
        self.cookies = cookies if cookies is not None else {}


def _patch_authed_user():
    async def _fake(_cookie):
        return USER
    return patch.object(oauth_mod, "get_user_from_cookie", _fake)


def _patch_client_config():
    return patch.object(
        oauth_mod, "load_twitter_client_config",
        return_value={"client_id": "cid", "client_secret": "csec"},
    )


def _patch_token_exchange(token_data: dict):
    """Patch httpx.AsyncClient inside the oauth module: POST returns the
    given token payload.
    """
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json = MagicMock(return_value=token_data)

    client = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch.object(oauth_mod.httpx, "AsyncClient", return_value=ctx), client


# ---------------------------------------------------------------------------
# Start route
# ---------------------------------------------------------------------------

def test_start_redirects_with_pkce_and_state_cookie():
    request = _FakeRequest(cookies={oauth_mod.COOKIE_NAME: "signed"})
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "oauth_base_url",
                         return_value="https://quest.example"):
        response = _run(oauth_mod.auth_twitter(request, popup="1"))

    assert response.status_code == 307
    url = urlsplit(response.headers["location"])
    # x.com, not legacy twitter.com: the consent page calls api.x.com, and
    # a cross-site origin there breaks X's ct0 CSRF cookie check (code 353)
    # under third-party-cookie blocking.
    assert url.scheme == "https" and url.netloc == "x.com"
    params = parse_qs(url.query)
    assert params["client_id"] == ["cid"]
    assert params["redirect_uri"] == ["https://quest.example/auth/twitter/callback"]
    assert params["code_challenge_method"] == ["S256"]
    state = params["state"][0]
    challenge = params["code_challenge"][0]
    assert state and challenge

    cookie_header = response.headers.get("set-cookie", "")
    assert "twitter_oauth_state=" in cookie_header
    assert "HttpOnly" in cookie_header and "SameSite=lax" in cookie_header
    # The cookie is a signed payload bound to the session user, carrying the
    # same nonce as the ``state`` param plus the PKCE verifier.
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(cookie_header)
    payload = oauth_state.read_oauth_state(
        _FakeRequest(cookies={"twitter_oauth_state": jar["twitter_oauth_state"].value}),
        "twitter_oauth_state",
    )
    assert payload["csrf"] == state
    assert payload["uid"] == USER["id"]
    assert payload["popup"] is True
    digest = hashlib.sha256(payload["code_verifier"].encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_start_without_session_cookie_shows_auth_required():
    response = _run(oauth_mod.auth_twitter(_FakeRequest(cookies={})))
    assert b"Authentication Required" in response.body


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

def _state_cookie(
    state: str, verifier: str = "v3rifier", popup: bool = True, uid: int = USER["id"],
) -> str:
    return _signed_state({
        "csrf": state, "uid": uid, "code_verifier": verifier, "popup": popup,
    })


def test_callback_stores_oauth_blob_and_sends_verifier():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "twitter_oauth_state": _state_cookie("st4te"),
    })
    upsert = AsyncMock()
    exchange_patch, client = _patch_token_exchange({
        "access_token": "at",
        "refresh_token": "rt",
        "token_type": "bearer",
        "expires_in": 7200,
        "scope": _FULL_GRANT,
    })
    with _patch_authed_user(), _patch_client_config(), exchange_patch, \
            patch.object(oauth_mod, "oauth_base_url",
                         return_value="https://quest.example"), \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        response = _run(oauth_mod.auth_twitter_callback(
            request, code="c0de", state="st4te",
        ))

    # Popup success page + state cookie cleared.
    assert response.status_code == 200
    assert b"oauth_callback_success" in response.body
    assert 'twitter_oauth_state="";' in response.headers.get("set-cookie", "")

    # The token exchange carried the PKCE verifier from the state cookie.
    _, post_kwargs = client.post.await_args
    assert post_kwargs["data"]["code_verifier"] == "v3rifier"

    upsert.assert_awaited_once()
    args, kwargs = upsert.await_args
    assert args == (USER["id"], "twitter")
    blob = kwargs["oauth_blob"]
    assert blob["access_token"] == "at"
    assert blob["refresh_token"] == "rt"
    assert blob["scope"] == _FULL_GRANT
    assert blob["authorized_at"]

    # A full grant does not flag needs_reauth; a partial one does.
    assert twitter_needs_reauth({"oauth_blob": blob}) is False
    assert twitter_needs_reauth(
        {"oauth_blob": {"access_token": "at", "scope": "dm.read tweet.read"}}
    ) is True


def test_callback_rejects_state_mismatch_without_storing():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "twitter_oauth_state": _state_cookie("expected"),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_twitter_callback(
            request, code="c0de", state="attacker",
        ))

    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_rejects_missing_verifier_without_storing():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "twitter_oauth_state": _signed_state(
            {"csrf": "st4te", "uid": USER["id"], "popup": False},
        ),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_twitter_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_rejects_unsigned_cookie_carrying_verifier():
    """A plaintext cookie -- what the flow used to accept -- is refused, so
    an attacker who can set cookies cannot supply their own verifier."""
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "twitter_oauth_state": json.dumps({
            "csrf": "st4te", "code_verifier": "evil", "popup": False,
        }),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_twitter_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_rejects_state_minted_for_another_session():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "twitter_oauth_state": _state_cookie("st4te", uid=USER["id"] + 1),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_twitter_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_provider_error_shows_error_page():
    request = _FakeRequest(cookies={})
    response = _run(oauth_mod.auth_twitter_callback(
        request, error="access_denied",
    ))
    assert b"Twitter Authentication Error" in response.body


def test_callback_exchange_failure_popup_posts_error():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "twitter_oauth_state": _state_cookie("st4te"),
    })
    upsert = AsyncMock()
    exchange_patch, _client = _patch_token_exchange({"error": "invalid_request"})
    with _patch_authed_user(), _patch_client_config(), exchange_patch, \
            patch.object(oauth_mod, "oauth_base_url",
                         return_value="https://quest.example"), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_twitter_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"oauth_callback_error" in response.body
    assert 'twitter_oauth_state="";' in response.headers.get("set-cookie", "")
    upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

def test_disconnect_deletes_credential_row():
    request = _FakeRequest(cookies={oauth_mod.COOKIE_NAME: "signed"})
    delete = AsyncMock()
    with _patch_authed_user(), \
            patch.object(oauth_mod, "delete_credential", delete), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        result = _run(oauth_mod.disconnect_twitter(request))
    assert result == {"success": True}
    delete.assert_awaited_once_with(USER["id"], "twitter")


def test_disconnect_requires_session():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(oauth_mod.disconnect_twitter(_FakeRequest(cookies={})))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Mount step (quest.py)
# ---------------------------------------------------------------------------

def test_mount_plugin_oauth_routers_mounts_twitter_routes(twitter_plugin):
    from fastapi import FastAPI
    from chat.route_table import iter_app_routes
    from config import plugins as plugins_mod

    app = FastAPI()
    with patch.object(plugins_mod, "_LOADED", [twitter_plugin]):
        plugins_mod.mount_plugin_oauth_routers(app)

    paths = {route.path for route in iter_app_routes(app)}
    assert "/auth/twitter" in paths
    assert "/auth/twitter/callback" in paths
    assert "/auth/twitter/disconnect" in paths
