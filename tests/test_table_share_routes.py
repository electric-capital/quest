"""Quest Tables sharing routes (chat/tables/share_routes.py, phase A16).

Same harness as tests/test_table_routes.py (isolated ``env`` of
tests/test_table_service.py, events captured from ``bus.publish_to_user``).

Covers: gate first, owner only (read and write recipients 403, hidden
404), share by email (case-insensitive), everyone share, upsert (same
permission publishes nothing; a new permission updates the row), refusals
(``invalid_request``, ``invalid_permission``, ``user_not_found``,
``ambiguous_user``, ``cannot_share_with_owner``, ``share_not_found``),
events to old and new audiences, ``updated_at`` unchanged, the removed
recipient's GET going 404.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.tables.constants import table_not_found_message, tables_disabled_message
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    env,
    events,
    fetch,
    make_table,
    uid,
)


def _app(env, who="alice"):
    from chat.tables import routes, share_routes

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(share_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return app


def client(env, who="alice"):
    return TestClient(_app(env, who))


def detail(resp):
    return resp.json()["detail"]


def share(env, table, payload, who="alice"):
    return client(env, who).post(f"/app/api/tables/{table['id']}/shares", json=payload)


def unshare(env, table, share_id, who="alice"):
    return client(env, who).delete(f"/app/api/tables/{table['id']}/shares/{share_id}")


class TestAccess:
    def test_gate_first(self, env, monkeypatch):
        table = make_table(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(env.store, "get_table", boom)
        for resp in (
            share(env, table, {"user_email": "bob@example.com", "permission": "read"}),
            unshare(env, table, 1),
        ):
            assert resp.status_code == 403
            assert detail(resp) == {"error": "tables_disabled",
                                    "message": tables_disabled_message()}

    @pytest.mark.parametrize("permission", ["read", "write"])
    def test_recipients_cannot_manage(self, env, permission):
        table = make_table(env, shares=[("bob", permission)])
        share_id = fetch(env, table)["shares"][0]["id"]
        resp = share(env, table, {"user_email": "carol@example.com", "permission": "read"},
                     who="bob")
        assert resp.status_code == 403 and detail(resp)["error"] == "forbidden"
        resp = unshare(env, table, share_id, who="bob")
        assert resp.status_code == 403 and detail(resp)["error"] == "forbidden"
        assert len(fetch(env, table)["shares"]) == 1
        assert env.published == []

    def test_hidden_is_404(self, env):
        table = make_table(env)
        body = {"detail": {"error": "table_not_found",
                           "message": table_not_found_message(table["id"])}}
        resp = share(env, table, {"everyone": True, "permission": "read"}, who="bob")
        assert resp.status_code == 404 and resp.json() == body
        resp = unshare(env, table, 1, who="bob")
        assert resp.status_code == 404 and resp.json() == body
        random_id = str(uuid.uuid4())
        resp = client(env, "bob").post(f"/app/api/tables/{random_id}/shares",
                                       json={"everyone": True, "permission": "read"})
        assert resp.status_code == 404


class TestAddShare:
    def test_share_by_email(self, env):
        table = make_table(env)
        before = fetch(env, table)
        resp = share(env, table, {"user_email": "  BOB@example.com ", "permission": "read"})
        assert resp.status_code == 200, resp.text
        row = resp.json()
        assert row["id"] == table["id"] and "schema" not in row
        assert row["shared"] is True
        bob = uid(env, "bob")
        (entry,) = row["shares"]
        assert entry["user_id"] == bob and entry["permission"] == "read"
        assert entry["user"] == {"id": bob, "name": "", "email": "bob@example.com"}
        assert fetch(env, table)["updated_at"] == before["updated_at"]
        alice = uid(env, "alice")
        assert sorted(events(env)) == sorted([
            (alice, "table_changed", table["id"], True),
            (bob, "table_changed", table["id"], True),
            (alice, "table_list_changed", None, True),
            (bob, "table_list_changed", None, True),
        ])
        # The recipient now sees it.
        assert client(env, "bob").get(f"/app/api/tables/{table['id']}").status_code == 200

    def test_everyone_share(self, env):
        table = make_table(env)
        env.connected.extend([uid(env, "carol")])
        resp = share(env, table, {"everyone": True, "permission": "read"})
        assert resp.status_code == 200
        (entry,) = resp.json()["shares"]
        assert entry["user_id"] is None and entry["user"] is None
        assert {u for u, *_ in events(env)} == {uid(env, "alice"), uid(env, "carol")}
        assert client(env, "carol").get(f"/app/api/tables/{table['id']}").status_code == 200
        # A second everyone share is an upsert, not a conflict.
        env.published.clear()
        resp = share(env, table, {"everyone": True, "permission": "write"})
        assert resp.status_code == 200
        assert [s["permission"] for s in resp.json()["shares"]] == ["write"]
        assert env.published

    def test_reshare_same_and_new_permission(self, env):
        table = make_table(env)
        share(env, table, {"user_email": "bob@example.com", "permission": "read"})
        env.published.clear()
        resp = share(env, table, {"user_email": "bob@example.com", "permission": "read"})
        assert resp.status_code == 200
        assert env.published == []
        resp = share(env, table, {"user_email": "bob@example.com", "permission": "write"})
        assert resp.status_code == 200
        shares = resp.json()["shares"]
        assert len(shares) == 1 and shares[0]["permission"] == "write"
        assert {u for u, *_ in events(env)} == {uid(env, "alice"), uid(env, "bob")}

    @pytest.mark.parametrize("payload,status,code", [
        ({}, 400, "invalid_request"),
        ({"permission": "read"}, 400, "invalid_request"),
        ({"user_email": "bob@example.com", "everyone": True, "permission": "read"},
         400, "invalid_request"),
        ({"user_email": 5, "permission": "read"}, 400, "invalid_request"),
        ({"everyone": "yes", "permission": "read"}, 400, "invalid_request"),
        ({"user_email": "bob@example.com", "permission": "admin"}, 400, "invalid_permission"),
        ({"user_email": "bob@example.com"}, 400, "invalid_permission"),
        ({"user_email": "nobody@example.com", "permission": "read"}, 404, "user_not_found"),
        ({"user_email": "alice@example.com", "permission": "read"},
         400, "cannot_share_with_owner"),
    ])
    def test_refusals(self, env, payload, status, code):
        table = make_table(env)
        resp = share(env, table, payload)
        assert resp.status_code == status, resp.text
        assert detail(resp)["error"] == code
        assert fetch(env, table)["shares"] == []
        assert env.published == []

    def test_ambiguous_user(self, env, monkeypatch):
        from db import user_store

        async def ambiguous(_email):
            raise user_store.AmbiguousUserEmailError("two")

        monkeypatch.setattr(user_store, "find_user_by_email_ci", ambiguous)
        table = make_table(env)
        resp = share(env, table, {"user_email": "Bob@example.com", "permission": "read"})
        assert resp.status_code == 409 and detail(resp)["error"] == "ambiguous_user"


class TestRemoveShare:
    def test_remove(self, env):
        table = make_table(env, shares=[("bob", "write"), ("carol", "read")])
        before = fetch(env, table)
        bob_share = next(s for s in before["shares"] if s["user_id"] == uid(env, "bob"))
        assert client(env, "bob").get(f"/app/api/tables/{table['id']}").status_code == 200
        resp = unshare(env, table, bob_share["id"])
        assert resp.status_code == 200, resp.text
        assert [s["user_id"] for s in resp.json()["shares"]] == [uid(env, "carol")]
        assert fetch(env, table)["updated_at"] == before["updated_at"]
        # Old audience (incl. bob) and new one are told.
        assert {u for u, *_ in events(env)} == {
            uid(env, "alice"), uid(env, "bob"), uid(env, "carol"),
        }
        assert (uid(env, "bob"), "table_changed", table["id"], True) in events(env)
        gone = client(env, "bob").get(f"/app/api/tables/{table['id']}")
        assert gone.status_code == 404
        assert detail(gone)["error"] == "table_not_found"

    @pytest.mark.parametrize("share_id", ["abc", "999", "-1", "1.0"])
    def test_share_not_found(self, env, share_id):
        table = make_table(env, shares=[("bob", "read")])
        resp = unshare(env, table, share_id)
        assert resp.status_code == 404
        assert detail(resp) == {"error": "share_not_found", "message": "Share not found."}
        assert len(fetch(env, table)["shares"]) == 1

    def test_share_of_another_table(self, env):
        mine = make_table(env, "Mine")
        other = make_table(env, "Other", shares=[("bob", "read")])
        other_share = fetch(env, other)["shares"][0]["id"]
        resp = unshare(env, mine, other_share)
        assert resp.status_code == 404 and detail(resp)["error"] == "share_not_found"
        assert len(fetch(env, other)["shares"]) == 1
