"""Tests for the Google Workspace Admin plugin's OAuth router (plugins/google_admin/oauth.py).

The start route's state cookie + authorize URL (read-only scopes only,
offline access, no incremental grant), the callback's token exchange
storing the blob with the GRANTED scopes and the connected account, CSRF
rejection, a disabled plugin, disconnect, and the quest.py mount step.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

import auth.oauth_state as oauth_state
import plugins.google_admin.oauth as oauth_mod
from plugins.google_admin.upstream import (
    GOOGLE_ADMIN_IDENTITY_SCOPES,
    GOOGLE_ADMIN_SCOPES,
    GOOGLE_TOKEN_URL,
    google_admin_needs_reauth,
)

_STATE_COOKIE = "google_admin_oauth_state"
_FULL_GRANT = " ".join(GOOGLE_ADMIN_SCOPES + GOOGLE_ADMIN_IDENTITY_SCOPES)


def _run(coro):
    return asyncio.run(coro)


USER = {"id": 7, "email": "u@example.com"}


@pytest.fixture(autouse=True)
def _fixed_secret_key():
    """Sign state cookies with a fixed key so tests never touch the data dir."""
    with patch.object(oauth_state, "get_secret_key", return_value="test-secret"):
        yield


class _FakeRequest:
    def __init__(self, cookies=None):
        self.cookies = cookies if cookies is not None else {}


def _patch_authed_user():
    async def _fake(_cookie):
        return USER
    return patch.object(oauth_mod, "get_user_from_cookie", _fake)


def _patch_client_config():
    return patch.object(
        oauth_mod, "load_google_admin_client_config",
        return_value={"client_id": "cid", "client_secret": "csec"},
    )


def _patch_base_url():
    return patch.object(oauth_mod, "oauth_base_url", return_value="https://quest.example")


def _patch_google(token_data: dict, *, token_status=200, userinfo=None, userinfo_status=200):
    """Patch httpx.AsyncClient inside the oauth module: POST is the token
    exchange, GET the userinfo lookup. Returns (patcher, client mock)."""
    post_resp = MagicMock()
    post_resp.status_code = token_status
    post_resp.json = MagicMock(return_value=token_data)
    post_resp.text = json.dumps(token_data)

    get_resp = MagicMock()
    get_resp.status_code = userinfo_status
    get_resp.json = MagicMock(return_value=userinfo if userinfo is not None else {
        "email": "admin@example.com", "hd": "example.com",
    })

    client = MagicMock()
    client.post = AsyncMock(return_value=post_resp)
    client.get = AsyncMock(return_value=get_resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return patch.object(oauth_mod.httpx, "AsyncClient", return_value=ctx), client


_TOKEN_RESPONSE = {
    "access_token": "ya29.admin",
    "refresh_token": "1//refresh",
    "expires_in": 3599,
    "scope": _FULL_GRANT,
    "token_type": "Bearer",
}


def _state_cookie(state: str, popup: bool = True, uid: int = USER["id"]) -> str:
    return oauth_state._serializer().dumps({"csrf": state, "uid": uid, "popup": popup})


def _callback_request(state: str = "st4te", **cookie_kwargs) -> _FakeRequest:
    return _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        _STATE_COOKIE: _state_cookie(state, **cookie_kwargs),
    })


# ---------------------------------------------------------------------------
# Start route
# ---------------------------------------------------------------------------

def test_start_redirects_to_google_with_read_only_scopes_and_state_cookie():
    request = _FakeRequest(cookies={oauth_mod.COOKIE_NAME: "signed"})
    with _patch_authed_user(), _patch_client_config(), _patch_base_url():
        response = _run(oauth_mod.auth_google_admin(request, popup="1"))

    assert response.status_code == 307
    url = urlsplit(response.headers["location"])
    assert url.scheme == "https" and url.netloc == "accounts.google.com"
    params = parse_qs(url.query)
    assert params["client_id"] == ["cid"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["https://quest.example/auth/google-admin/callback"]
    assert params["access_type"] == ["offline"]
    assert "consent" in params["prompt"][0].split()
    # Never an incremental grant: the token must not inherit the scopes of
    # the user's Google Services connection.
    assert "include_granted_scopes" not in params

    requested = params["scope"][0].split()
    assert set(requested) == set(GOOGLE_ADMIN_SCOPES) | set(GOOGLE_ADMIN_IDENTITY_SCOPES)
    admin_scopes = [s for s in requested if s not in GOOGLE_ADMIN_IDENTITY_SCOPES]
    assert all(s.endswith(".readonly") for s in admin_scopes)

    state = params["state"][0]
    from http.cookies import SimpleCookie
    jar = SimpleCookie()
    jar.load(response.headers.get("set-cookie", ""))
    payload = oauth_state.read_oauth_state(
        _FakeRequest(cookies={_STATE_COOKIE: jar[_STATE_COOKIE].value}), _STATE_COOKIE,
    )
    assert payload["csrf"] == state
    assert payload["popup"] is True
    assert payload["uid"] == USER["id"]


def test_start_without_session_cookie_shows_auth_required():
    response = _run(oauth_mod.auth_google_admin(_FakeRequest(cookies={})))
    assert b"Authentication Required" in response.body


def test_start_while_disabled_shows_configuration_error():
    request = _FakeRequest(cookies={oauth_mod.COOKIE_NAME: "signed"})
    with _patch_authed_user(), _patch_base_url(), patch.object(
        oauth_mod, "load_google_admin_client_config",
        side_effect=HTTPException(status_code=500, detail="Google Workspace Admin is not enabled."),
    ):
        response = _run(oauth_mod.auth_google_admin(request, popup="1"))
    assert response.status_code == 200
    assert b"Configuration Error" in response.body
    assert b"not enabled" in response.body


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

def test_callback_stores_blob_with_granted_scopes_and_account():
    upsert = AsyncMock()
    google, client = _patch_google(_TOKEN_RESPONSE)
    with _patch_authed_user(), _patch_client_config(), _patch_base_url(), google, \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions") as invalidate:
        response = _run(oauth_mod.auth_google_admin_callback(
            _callback_request(), code="c0de", state="st4te",
        ))

    # Popup success page + state cookie cleared.
    assert response.status_code == 200
    assert b"oauth_callback_success" in response.body
    assert f'{_STATE_COOKIE}="";' in response.headers.get("set-cookie", "")

    assert client.post.await_args.args[0] == GOOGLE_TOKEN_URL
    assert client.post.await_args.kwargs["data"] == {
        "client_id": "cid",
        "client_secret": "csec",
        "grant_type": "authorization_code",
        "code": "c0de",
        "redirect_uri": "https://quest.example/auth/google-admin/callback",
    }

    upsert.assert_awaited_once()
    args, kwargs = upsert.await_args
    assert args == (USER["id"], "google_admin")
    blob = kwargs["oauth_blob"]
    assert blob["access_token"] == "ya29.admin"
    assert blob["refresh_token"] == "1//refresh"
    assert blob["scopes"] == _FULL_GRANT.split()
    assert blob["expires_at"] and blob["authorized_at"]
    # The connected account is recorded, and may differ from the session user.
    assert blob["account"] == {"email": "admin@example.com", "domain": "example.com"}
    assert google_admin_needs_reauth({"oauth_blob": blob}) is False
    invalidate.assert_called_once_with(USER["id"])


def test_callback_records_a_partial_grant_as_needing_reauth():
    granted = " ".join(s for s in _FULL_GRANT.split() if "device" not in s)
    upsert = AsyncMock()
    google, _ = _patch_google({**_TOKEN_RESPONSE, "scope": granted})
    with _patch_authed_user(), _patch_client_config(), _patch_base_url(), google, \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        _run(oauth_mod.auth_google_admin_callback(
            _callback_request(), code="c0de", state="st4te",
        ))

    blob = upsert.await_args.kwargs["oauth_blob"]
    assert blob["scopes"] == granted.split()
    assert google_admin_needs_reauth({"oauth_blob": blob}) is True


def test_callback_still_connects_when_the_account_lookup_fails():
    upsert = AsyncMock()
    google, _ = _patch_google(_TOKEN_RESPONSE, userinfo_status=503)
    with _patch_authed_user(), _patch_client_config(), _patch_base_url(), google, \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        response = _run(oauth_mod.auth_google_admin_callback(
            _callback_request(), code="c0de", state="st4te",
        ))

    assert b"oauth_callback_success" in response.body
    assert upsert.await_args.kwargs["oauth_blob"]["account"] is None


def test_callback_non_popup_redirects_home():
    upsert = AsyncMock()
    google, _ = _patch_google(_TOKEN_RESPONSE)
    with _patch_authed_user(), _patch_client_config(), _patch_base_url(), google, \
            patch.object(oauth_mod, "upsert_credential", upsert), \
            patch("chat.gemini_api.invalidate_user_sessions"):
        response = _run(oauth_mod.auth_google_admin_callback(
            _callback_request(popup=False), code="c0de", state="st4te",
        ))
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_callback_rejects_state_mismatch_without_storing():
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_google_admin_callback(
            _callback_request("expected"), code="c0de", state="attacker",
        ))
    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_rejects_state_minted_for_another_session():
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_google_admin_callback(
            _callback_request(uid=USER["id"] + 1), code="c0de", state="st4te",
        ))
    assert b"Security Error" in response.body
    assert f'{_STATE_COOKIE}="";' in response.headers.get("set-cookie", "")
    upsert.assert_not_awaited()


def test_callback_rejects_unsigned_state_cookie():
    request = _FakeRequest(cookies={
        oauth_mod.COOKIE_NAME: "signed",
        _STATE_COOKIE: json.dumps({"csrf": "st4te", "popup": True}),
    })
    upsert = AsyncMock()
    with _patch_authed_user(), _patch_client_config(), \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_google_admin_callback(
            request, code="c0de", state="st4te",
        ))
    assert b"Security Error" in response.body
    upsert.assert_not_awaited()


def test_callback_provider_error_shows_error_page():
    response = _run(oauth_mod.auth_google_admin_callback(
        _FakeRequest(cookies={}), error="access_denied",
    ))
    assert b"Google Workspace Admin Authentication Error" in response.body


def test_callback_exchange_failure_popup_posts_error():
    upsert = AsyncMock()
    google, _ = _patch_google(
        {"error": "invalid_grant", "error_description": "Bad Request"},
        token_status=400,
    )
    with _patch_authed_user(), _patch_client_config(), _patch_base_url(), google, \
            patch.object(oauth_mod, "upsert_credential", upsert):
        response = _run(oauth_mod.auth_google_admin_callback(
            _callback_request(), code="c0de", state="st4te",
        ))
    assert b"oauth_callback_error" in response.body
    assert f'{_STATE_COOKIE}="";' in response.headers.get("set-cookie", "")
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
        result = _run(oauth_mod.disconnect_google_admin(request))
    assert result == {"success": True}
    delete.assert_awaited_once_with(USER["id"], "google_admin")


def test_disconnect_requires_session():
    with pytest.raises(HTTPException) as exc:
        _run(oauth_mod.disconnect_google_admin(_FakeRequest(cookies={})))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Mount step (quest.py)
# ---------------------------------------------------------------------------

def test_mount_plugin_oauth_routers_mounts_the_routes(google_admin_plugin):
    from fastapi import FastAPI
    from config import plugins as plugins_mod

    app = FastAPI()
    with patch.object(plugins_mod, "_LOADED", [google_admin_plugin]):
        plugins_mod.mount_plugin_oauth_routers(app)

    paths = {route.path for route in app.routes}
    assert "/auth/google-admin" in paths
    assert "/auth/google-admin/callback" in paths
    assert "/auth/google-admin/disconnect" in paths
