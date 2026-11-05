"""Plugin oauth Disconnect URL and best-effort revoke hook.

Covers ``UserConnectionSpec.disconnect_url`` (the /connectors row field and
its load-time validation) and ``UserConnectionSpec.revoke`` (called by
logout-and-disconnect and account deletion before the rows are removed,
never blocking them). Async work is driven via ``asyncio.run``.
"""

import asyncio
import logging
import uuid

import pytest
from fastapi import APIRouter

import auth.connections as connections_mod
import chat.routes.user as user_routes
from config import plugins as plugins_mod
from config.plugin_types import QuestPlugin, UserConnectionSpec


def _run(coro):
    return asyncio.run(coro)


def _router(*routes):
    router = APIRouter()
    for method, path in routes:
        router.add_api_route(path, lambda: {}, methods=[method])
    return router


def _oauth_plugin(pid="acme", router=None, **spec_kwargs):
    return QuestPlugin(
        id=pid, label=pid.title(),
        user_connection=UserConnectionSpec(
            kind="oauth",
            connected=lambda row: bool(row.get("oauth_blob")),
            oauth_router=router or _router(("GET", f"/auth/{pid}")),
            **spec_kwargs,
        ),
    )


# ---------------------------------------------------------------------------
# Row field and validation
# ---------------------------------------------------------------------------

def _connector_rows(monkeypatch, plugin):
    monkeypatch.setattr(plugins_mod, "_LOADED", [plugin])
    monkeypatch.setattr(
        "config.service_credentials.read_service_credentials",
        lambda *a, **k: None,
    )
    user = {
        "email": "u@example.com",
        "service_credentials": {
            plugin.id: {"service": plugin.id, "oauth_blob": {"access_token": "t"}},
        },
    }
    rows = _run(user_routes.get_connectors(user=user))["connectors"]
    return {r["service"]: r for r in rows}


def test_oauth_row_carries_disconnect_url_only_when_declared(monkeypatch):
    with_url = _oauth_plugin(
        router=_router(("GET", "/auth/acme"), ("POST", "/auth/acme/disconnect")),
        disconnect_url="/auth/acme/disconnect",
    )
    assert _connector_rows(monkeypatch, with_url)["acme"]["disconnect_url"] \
        == "/auth/acme/disconnect"

    without = _connector_rows(monkeypatch, _oauth_plugin())
    assert "disconnect_url" not in without["acme"]


class TestDisconnectUrlValidation:
    def test_accepts_post_route_under_the_plugin_namespace(self):
        plugins_mod.validate_plugin(
            _oauth_plugin(
                router=_router(("POST", "/auth/acme/disconnect")),
                disconnect_url="/auth/acme/disconnect",
            ),
            set(),
        )

    @pytest.mark.parametrize("url, routes", [
        ("/auth/acme/gone", [("POST", "/auth/acme/disconnect")]),
        ("/auth/acme/disconnect", [("GET", "/auth/acme/disconnect")]),
    ])
    def test_rejects_url_that_is_not_a_post_route_of_the_router(self, url, routes):
        with pytest.raises(ValueError, match="disconnect_url"):
            plugins_mod.validate_plugin(
                _oauth_plugin(
                    router=_router(("GET", "/auth/acme"), *routes),
                    disconnect_url=url,
                ),
                set(),
            )

    def test_rejected_on_api_key_kind(self):
        plugin = QuestPlugin(
            id="acme", label="Acme",
            user_connection=UserConnectionSpec(
                kind="api_key", connected=lambda row: True,
                disconnect_url="/auth/acme/disconnect",
            ),
        )
        with pytest.raises(ValueError, match="disconnect_url"):
            plugins_mod.validate_plugin(plugin, set())

    def test_revoke_must_be_callable(self):
        with pytest.raises(ValueError, match="revoke is not callable"):
            plugins_mod.validate_plugin(_oauth_plugin(revoke="nope"), set())


# ---------------------------------------------------------------------------
# Revoke hook on logout-and-disconnect and account deletion
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(isolated_db):
    import db.user_service_credential_store as store
    import db.user_store as user_store

    user = _run(user_store.create_user(
        email=f"test-{uuid.uuid4().hex}@example.com", name="T",
        api_key=f"k-{uuid.uuid4().hex}",
    ))
    return store, user_store, user


def _plugins(monkeypatch, *plugins):
    monkeypatch.setattr(plugins_mod, "_LOADED", list(plugins))


def test_logout_and_disconnect_calls_revoke_with_the_decrypted_row(env, monkeypatch):
    store, _, user = env
    seen = []

    async def revoke(row):
        seen.append(row)

    _plugins(monkeypatch, _oauth_plugin(revoke=revoke))
    _run(store.upsert_credential(
        user["id"], "acme", oauth_blob={"refresh_token": "r-1"},
    ))

    result = _run(user_routes.logout_and_disconnect(request=None, user=user))

    assert result.status_code == 200
    assert len(seen) == 1
    assert seen[0]["service"] == "acme"
    assert seen[0]["oauth_blob"] == {"refresh_token": "r-1"}
    assert _run(store.list_credentials(user["id"])) == {}


def test_delete_account_calls_revoke_before_the_rows_cascade(env, monkeypatch):
    store, user_store, user = env
    seen = []

    async def revoke(row):
        # The stored row still exists while the hook runs.
        still_stored = await store.get_credential(user["id"], "acme")
        seen.append((row["oauth_blob"], still_stored is not None))

    _plugins(monkeypatch, _oauth_plugin(revoke=revoke))
    _run(store.upsert_credential(
        user["id"], "acme", oauth_blob={"refresh_token": "r-2"},
    ))

    result = _run(user_routes.delete_account(request=None, user=user))

    assert result.status_code == 200
    assert seen == [({"refresh_token": "r-2"}, True)]
    assert _run(user_store.get_user_by_email(user["email"])) is None
    assert _run(store.list_credentials(user["id"])) == {}


def test_revoke_runs_once_per_row_of_a_multi_connection_plugin(env, monkeypatch):
    store, _, user = env
    accounts = []

    async def revoke(row):
        accounts.append(row["account"])

    _plugins(monkeypatch, QuestPlugin(
        id="acme", label="Acme",
        user_connection=UserConnectionSpec(
            kind="oauth", connected=lambda row: True,
            oauth_router=_router(("GET", "/auth/acme")),
            multi_connection=True, account_from=lambda row: row["account"],
            revoke=revoke,
        ),
    ))
    for account in ("a@example.com", "b@example.com"):
        _run(store.create_connection(
            user["id"], "acme", account=account, oauth_blob={"t": account},
        ))

    _run(connections_mod.revoke_user_credentials(user["id"]))

    assert sorted(accounts) == ["a@example.com", "b@example.com"]


def test_plugins_without_a_hook_are_untouched(env, monkeypatch):
    store, _, user = env
    _plugins(monkeypatch, _oauth_plugin())
    _run(store.upsert_credential(user["id"], "acme", oauth_blob={"t": "x"}))

    result = _run(user_routes.logout_and_disconnect(request=None, user=user))

    assert result.status_code == 200
    assert _run(store.list_credentials(user["id"])) == {}


def test_failing_and_hanging_hooks_do_not_block_deletion(env, monkeypatch, caplog):
    store, _, user = env
    monkeypatch.setattr(connections_mod, "REVOKE_TIMEOUT_SECONDS", 0.05)
    healthy_calls = []

    async def raising(row):
        raise RuntimeError(f"provider said no for {row['oauth_blob']['t']}")

    async def hanging(row):
        await asyncio.sleep(3600)

    async def healthy(row):
        healthy_calls.append(row["service"])

    def _plugin(pid, hook):
        return _oauth_plugin(pid, revoke=hook)

    _plugins(
        monkeypatch,
        _plugin("raiser", raising), _plugin("hanger", hanging),
        _plugin("healthy", healthy),
    )
    for pid in ("raiser", "hanger", "healthy"):
        _run(store.upsert_credential(
            user["id"], pid, oauth_blob={"t": "SECRET-TOKEN"},
        ))

    with caplog.at_level(logging.INFO):
        result = _run(user_routes.logout_and_disconnect(request=None, user=user))

    assert result.status_code == 200
    assert healthy_calls == ["healthy"]
    assert _run(store.list_credentials(user["id"])) == {}
    text = caplog.text
    assert "raiser" in text and "hanger" in text and "timed out" in text
    assert "SECRET-TOKEN" not in text
