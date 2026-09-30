"""Tests for the github plugin's OAuth router (plugins/github/oauth.py).

The first oauth-kind plugin user connection: the start route's state
cookie + encoded authorize URL, the callback's token exchange storing the
blob (with GRANTED scopes) into user_service_credentials.oauth_blob, CSRF
rejection, disconnect, and the quest.py mount step
(config.plugins.mount_plugin_oauth_routers).
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

import auth.oauth_state as oauth_state
import plugins.github.oauth as oauth_mod
from plugins.github.upstream import github_needs_reauth


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


class _FakeRequest:
    def __init__(self, cookies=None):
        self.cookies = cookies if cookies is not None else {}


def _patch_authed_user():
    async def _fake(_cookie):
        return USER
    return patch.object(oauth_mod, "get_user_from_cookie", _fake)


def _patch_client_config():
    return patch.object(
        oauth_mod, "load_github_client_config",
        return_value={"client_id": "cid", "client_secret": "csec"},
    )


def _patch_token_exchange(token_data: dict):
    """Patch httpx.AsyncClient inside the oauth module: POST returns the
    given token payload, GET (the username lookup) returns a stub user.
    """
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json = MagicMock(return_value=token_data)

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json = MagicMock(return_value={"login": "octocat"})

    client = MagicMock()
    client.post = AsyncMock(return_value=post_resp)
    client.get = AsyncMock(return_value=get_resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch.object(oauth_mod.httpx, "AsyncClient", return_value=ctx)


# ---------------------------------------------------------------------------
# Start route
# ---------------------------------------------------------------------------

def test_start_redirects_with_encoded_params_and_state_cookie():
    request = _FakeRequest(cookies={oauth_mod.COOKIE_NAME: "signed"})
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "oauth_base_url",
                         return_value="https://quest.example"):
        response = _run(oauth_mod.auth_github(request, popup="1"))

    assert response.status_code == 307
    url = urlsplit(response.headers["location"])
    assert url.scheme == "https" and url.netloc == "github.com"
    params = parse_qs(url.query)
    assert params["client_id"] == ["cid"]
    assert params["redirect_uri"] == ["https://quest.example/auth/github/callback"]
    assert params["scope"] == ["repo read:org"]
    state = params["state"][0]
    assert state

    cookie_header = response.headers.get("set-cookie", "")
    assert "github_oauth_state=" in cookie_header
    assert "HttpOnly" in cookie_header and "SameSite=lax" in cookie_header
    # The cookie is a signed payload bound to the session user and carrying
    # the same nonce as the ``state`` query parameter.
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(cookie_header)
    payload = oauth_state.read_oauth_state(
        _FakeRequest(cookies={"github_oauth_state": jar["github_oauth_state"].value}),
        "github_oauth_state",
    )
    assert payload["csrf"] == state
    assert payload["popup"] is True
    assert payload["uid"] == USER["id"]


def test_start_without_session_cookie_shows_auth_required():
    response = _run(oauth_mod.auth_github(_FakeRequest(cookies={})))
    assert b"Authentication Required" in response.body


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

def _state_cookie(state: str, popup: bool = True, uid: int = USER["id"]) -> str:
    return _signed_state({"csrf": state, "uid": uid, "popup": popup})


def test_callback_stores_oauth_blob_with_granted_scopes():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "github_oauth_state": _state_cookie("st4te"),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            _patch_token_exchange({
                "access_token": "gho_new",
                "token_type": "bearer",
                "scope": "repo,read:org",
            }), \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        response = _run(oauth_mod.auth_github_callback(
            request, code="c0de", state="st4te",
        ))

    # Popup success page + state cookie cleared.
    assert response.status_code == 200
    assert b"oauth_callback_success" in response.body
    assert 'github_oauth_state="";' in response.headers.get("set-cookie", "")

    upsert.assert_awaited_once()
    args, kwargs = upsert.await_args
    assert args == (USER["id"], "github")
    blob = kwargs["oauth_blob"]
    assert blob["access_token"] == "gho_new"
    assert blob["scope"] == "repo,read:org"
    assert blob["scopes"] == ["repo", "read:org"]
    assert blob["authorized_at"]
    # Classic OAuth App tokens never expire: nothing to refresh.
    assert "refresh_token" not in blob
    assert "expires_at" not in blob

    # A full grant does not flag needs_reauth; a partial one does.
    assert github_needs_reauth({"oauth_blob": blob}) is False
    assert github_needs_reauth(
        {"oauth_blob": {"access_token": "gho_x", "scope": "repo"}}
    ) is True


def test_callback_keeps_refresh_token_for_expiring_github_app_tokens():
    # GitHub Apps (expiring user tokens on) return an 8-hour ghu_ token plus
    # a ghr_ refresh token; dropping the refresh token strands routines.
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "github_oauth_state": _state_cookie("st4te"),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            _patch_token_exchange({
                "access_token": "ghu_new",
                "expires_in": 28800,
                "refresh_token": "ghr_new",
                "refresh_token_expires_in": 15897600,
                "scope": "",
                "token_type": "bearer",
            }), \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        _run(oauth_mod.auth_github_callback(request, code="c0de", state="st4te"))

    blob = upsert.await_args.kwargs["oauth_blob"]
    assert blob["access_token"] == "ghu_new"
    assert blob["refresh_token"] == "ghr_new"
    assert blob["expires_at"]
    assert blob["refresh_token_expires_at"]
    assert github_needs_reauth({"oauth_blob": blob}) is False


def test_needs_reauth_skips_scope_check_for_github_app_tokens():
    # GitHub App user tokens (ghu_) carry no OAuth scopes -- the exchange
    # returns scope "" -- so the subset check must not flag them.
    assert github_needs_reauth(
        {"oauth_blob": {"access_token": "ghu_x", "scope": "", "scopes": []}}
    ) is False
    # Classic OAuth App tokens (gho_) still get the subset check.
    assert github_needs_reauth(
        {"oauth_blob": {"access_token": "gho_x", "scope": "", "scopes": []}}
    ) is True


def test_callback_rejects_state_mismatch_without_storing():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "github_oauth_state": _state_cookie("expected"),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_github_callback(
            request, code="c0de", state="attacker",
        ))

    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_rejects_state_minted_for_another_session():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "github_oauth_state": _state_cookie("st4te", uid=USER["id"] + 1),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_github_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"Security Error" in response.body
    assert 'github_oauth_state="";' in response.headers.get("set-cookie", "")
    upsert.assert_not_awaited()


def test_callback_rejects_unsigned_state_cookie():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "github_oauth_state": json.dumps({"csrf": "st4te", "popup": True}),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_github_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_provider_error_shows_error_page():
    request = _FakeRequest(cookies={})
    response = _run(oauth_mod.auth_github_callback(
        request, error="access_denied",
    ))
    assert b"GitHub Authentication Error" in response.body


def test_callback_exchange_failure_popup_posts_error():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        "github_oauth_state": _state_cookie("st4te"),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            _patch_token_exchange({"error": "bad_verification_code"}), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_github_callback(
            request, code="c0de", state="st4te",
        ))

    assert b"oauth_callback_error" in response.body
    assert 'github_oauth_state="";' in response.headers.get("set-cookie", "")
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
        result = _run(oauth_mod.disconnect_github(request))
    assert result == {"success": True}
    delete.assert_awaited_once_with(USER["id"], "github")


def test_disconnect_requires_session():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _run(oauth_mod.disconnect_github(_FakeRequest(cookies={})))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Mount step (quest.py)
# ---------------------------------------------------------------------------

def test_mount_plugin_oauth_routers_mounts_github_routes(github_plugin):
    from fastapi import FastAPI
    from config import plugins as plugins_mod

    app = FastAPI()
    with patch.object(plugins_mod, "_LOADED", [github_plugin]):
        plugins_mod.mount_plugin_oauth_routers(app)

    paths = {route.path for route in app.routes}
    assert "/auth/github" in paths
    assert "/auth/github/callback" in paths
    assert "/auth/github/disconnect" in paths


def test_mount_skips_api_key_plugins(example_plugin):
    from fastapi import FastAPI
    from config import plugins as plugins_mod

    app = FastAPI()
    baseline = len(app.routes)
    with patch.object(plugins_mod, "_LOADED", [example_plugin]):
        plugins_mod.mount_plugin_oauth_routers(app)
    assert len(app.routes) == baseline
