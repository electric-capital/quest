"""Quest Docs sharing (Phase 3, package 1): share routes, the enriched UI
row, the ``shared=true`` list stream, recipients' reach, and the realtime
audience.

Runs the doc routers in a bare FastAPI app with the auth dependency
overridden (the tests/test_docs_routes.py pattern, plus the share router)
on the isolated ``docs_env`` fixture from tests/test_docs_service.py (tmp
SQLite with foreign keys on, tmp DOCS_DIR / CHATS_DIR / PROJECTS_DIR, docs
gate open, realtime ``publish_to_user`` captured). Users: alice (owns the three projects), bob, carol.

Covers:

1. ``POST /docs/{id}/shares`` / ``DELETE /docs/{id}/shares/{share_id}``:
   owner-only (403 for a visible non-owner, 404 identical to a missing id
   for a hidden doc), case-insensitive email lookup, everyone row, upsert
   (one row per recipient), every 400/404 code, ``updated_at`` untouched.
2. Rows: ``shared_with_me`` / ``permission`` / ``owner`` /
   ``shares[].user`` / ``last_write_user`` and the ``access`` flags for the
   owner, a read share and a write share; recipients can never rename,
   delete or manage shares; one ``get_users_by_ids`` call per response.
3. ``GET /docs?shared=true``: keyset paging with an exact ``has_more``,
   everyone rows for every other user, own docs never, project docs
   included, public-project docs hidden while the gate is closed,
   ``shared=true&project_id`` 400; the default list = own user docs only.
4. Recipients' conversations (the unchanged access matrix): shared user
   docs readable, shared project docs hidden, a write share on a private
   doc needs a ``write_doc`` card that then applies end to end; the owner's
   own conversation writes flip to approval after the first share.
5. Deleted users: their share rows cascade away, the everyone row stays.
6. Realtime: writes, rename, delete, project delete and share changes reach
   the owner + direct recipients, an everyone row reaches every connected
   user (deduped); ``Bus.connected_user_ids``.
"""

import io
import os
import uuid
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from chat.docs import files
from chat.docs.access import DENY_READ_ONLY_SHARE
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from chat.docs.service import DocApprovalRequired, DocError
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    GIF,
    PNG,
    _run,
    body,
    docs_env,
    event_types,
    make_caller,
    seed_doc,
    svc,
)
from tests.test_write_doc_action_request import (  # noqa: F401  (ar_env is a fixture)
    _arm,
    append_params,
    ar_env,
    edit_params,
    execute,
    propose,
    read,
    tool,
)


def client(env, who="alice"):
    from chat.docs import routes, share_routes

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(share_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def detail(resp):
    return resp.json()["detail"]


def uid(env, who):
    return env.users[who]["id"]


def email(env, who):
    return env.users[who]["email"]


def set_names(env, **names):
    """Give the seeded users display names (they are created without)."""
    async def _update():
        async with env.doc_store.AsyncSessionLocal() as db:
            for who, name in names.items():
                await db.execute(
                    text("UPDATE users SET name = :n WHERE id = :i"),
                    {"n": name, "i": uid(env, who)},
                )
            await db.commit()

    _run(_update())


def add_user(env, address):
    """Insert one more user with exactly this email; returns its id."""
    from db.models import User

    async def _add():
        async with env.doc_store.AsyncSessionLocal() as db:
            user = User(email=address, api_key=f"k-{uuid.uuid4().hex}")
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user.id

    return _run(_add())


def set_updated_at(env, doc_ids, when):
    async def _update():
        async with env.doc_store.AsyncSessionLocal() as db:
            for doc_id in doc_ids:
                await db.execute(
                    text("UPDATE docs SET updated_at = :t WHERE id = :i"),
                    {"t": when, "i": doc_id},
                )
            await db.commit()

    _run(_update())


class _Req:
    app = None


def resolve(request_id, user):
    from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request

    return _run(resolve_user_action_request(
        request_id, ResolveRequestBody(action="execute"), _Req(), user,
    ))


def ref(env, who, name=""):
    return {"id": uid(env, who), "name": name, "email": email(env, who)}


def share(c, doc_id, **payload):
    return c.post(f"/app/api/docs/{doc_id}/shares", json=payload)


def connected(monkeypatch, *user_ids):
    """Pretend ``user_ids`` have a live WebSocket right now."""
    from chat.realtime import bus

    monkeypatch.setattr(bus, "connected_user_ids", lambda: list(user_ids))


def recipients(env, event_type):
    return [u for u, ev in env.published if ev["type"] == event_type]


def published(env):
    """``(user_id, type, doc_id, updated_at)`` for every captured event."""
    return [
        (u, ev["type"], ev.get("doc_id"), ev.get("updated_at"))
        for u, ev in env.published
    ]


def access_changed(doc_id, *user_ids):
    """What a share change or a delete publishes: ``doc_changed`` with
    ``updated_at`` null (open viewers re-fetch), then ``doc_list_changed``,
    each to ``user_ids`` in order."""
    return [(u, "doc_changed", doc_id, None) for u in user_ids] + [
        (u, "doc_list_changed", None, None) for u in user_ids
    ]


OWNER_ONLY_FLAGS = ("can_rename", "can_delete", "can_share", "can_delete_assets")


# ---------------------------------------------------------------------------
# POST /docs/{id}/shares
# ---------------------------------------------------------------------------


class TestAddShare:
    def test_share_with_user_by_email(self, docs_env):
        set_names(docs_env, alice="Alice A", bob="Bob B")
        doc = seed_doc(docs_env, "Plan")
        resp = share(
            client(docs_env), doc["id"],
            user_email="  BOB@Example.COM ", permission="read",
        )
        assert resp.status_code == 200
        row = resp.json()
        # The owner's row: the roster, with the recipient resolved.
        assert row["id"] == doc["id"]
        assert row["shared"] is True and row["shared_with_me"] is False
        assert row["permission"] is None and row["owner"] is None
        assert len(row["shares"]) == 1
        entry = row["shares"][0]
        assert entry["user_id"] == uid(docs_env, "bob")
        assert entry["permission"] == "read"
        assert entry["user"] == ref(docs_env, "bob", "Bob B")
        assert set(entry) == {"id", "user_id", "permission", "created_at", "user"}
        # Not a content change.
        stored = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert stored["updated_at"] == doc["updated_at"] == row["updated_at"]
        assert published(docs_env) == access_changed(
            doc["id"], uid(docs_env, "alice"), uid(docs_env, "bob"),
        )

    def test_reshare_updates_the_one_row(self, docs_env):
        doc = seed_doc(docs_env, "Plan")
        c = client(docs_env)
        first = share(c, doc["id"], user_email=email(docs_env, "bob"), permission="read").json()
        docs_env.published.clear()
        row = share(c, doc["id"], user_email="Bob@example.com", permission="write").json()
        assert [(s["id"], s["permission"]) for s in row["shares"]] == [
            (first["shares"][0]["id"], "write"),
        ]
        assert published(docs_env) == access_changed(
            doc["id"], uid(docs_env, "alice"), uid(docs_env, "bob"),
        )
        # Same permission again: nothing changes, nothing is published.
        docs_env.published.clear()
        again = share(c, doc["id"], user_email=email(docs_env, "bob"), permission="write").json()
        assert again["shares"] == row["shares"]
        assert docs_env.published == []
        assert len(_run(docs_env.doc_store.list_shares(doc["id"]))) == 1
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["updated_at"] == doc["updated_at"]

    def test_everyone_row(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, "Plan")
        c = client(docs_env)
        connected(monkeypatch, uid(docs_env, "carol"), uid(docs_env, "alice"), 4242)
        row = share(c, doc["id"], everyone=True, permission="read").json()
        assert [(s["user_id"], s["permission"], s["user"]) for s in row["shares"]] == [
            (None, "read", None),
        ]
        # Broadcast: the owner first, then every connected user, deduped.
        assert published(docs_env) == access_changed(
            doc["id"], uid(docs_env, "alice"), uid(docs_env, "carol"), 4242,
        )
        # At most one everyone row: re-sharing updates it.
        row = share(c, doc["id"], everyone=True, user_email="", permission="write").json()
        assert [(s["user_id"], s["permission"]) for s in row["shares"]] == [(None, "write")]
        assert len(_run(docs_env.doc_store.list_shares(doc["id"]))) == 1

    def test_user_and_everyone_rows_coexist(self, docs_env):
        set_names(docs_env, carol="Carol C")
        doc = seed_doc(docs_env, "Plan")
        c = client(docs_env)
        share(c, doc["id"], everyone=True, permission="read")
        row = share(c, doc["id"], user_email=email(docs_env, "carol"), permission="write").json()
        assert [(s["user_id"], s["user"]) for s in row["shares"]] == [
            (None, None), (uid(docs_env, "carol"), ref(docs_env, "carol", "Carol C")),
        ]

    @pytest.mark.parametrize("payload", [
        {"permission": "read"},
        {"user_email": "", "permission": "read"},
        {"user_email": "   ", "permission": "read"},
        {"everyone": False, "permission": "read"},
        {"user_email": "bob@example.com", "everyone": True, "permission": "read"},
        {"user_email": 5, "permission": "read"},
        {"user_email": ["bob@example.com"], "permission": "read"},
        {"everyone": "true", "permission": "read"},
        {"everyone": 1, "permission": "read"},
    ])
    def test_exactly_one_target(self, docs_env, payload):
        doc = seed_doc(docs_env, "Plan")
        resp = share(client(docs_env), doc["id"], **payload)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []
        assert docs_env.published == []

    def test_missing_body(self, docs_env):
        doc = seed_doc(docs_env, "Plan")
        resp = client(docs_env).post(f"/app/api/docs/{doc['id']}/shares")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    @pytest.mark.parametrize("permission", [None, "", "admin", "READ", 1, ["read"]])
    def test_invalid_permission(self, docs_env, permission):
        doc = seed_doc(docs_env, "Plan")
        payload = {"user_email": email(docs_env, "bob")}
        if permission is not None:
            payload["permission"] = permission
        resp = share(client(docs_env), doc["id"], **payload)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_permission"
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []

    def test_unknown_email_404(self, docs_env):
        doc = seed_doc(docs_env, "Plan")
        resp = share(client(docs_env), doc["id"], user_email="nobody@example.com",
                     permission="read")
        assert resp.status_code == 404
        assert detail(resp) == {
            "error": "user_not_found", "message": "No Quest user has that email.",
        }
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []

    def test_owner_email_400(self, docs_env):
        doc = seed_doc(docs_env, "Plan")
        resp = share(client(docs_env), doc["id"], user_email="ALICE@example.com",
                     permission="write")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "cannot_share_with_owner"
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []

    @pytest.mark.parametrize("permission", ["read", "write"])
    def test_recipient_cannot_share(self, docs_env, permission):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", permission)])
        resp = share(client(docs_env, "bob"), doc["id"],
                     user_email=email(docs_env, "carol"), permission="write")
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        # Not even with the everyone row, nor upgrading their own grant.
        resp = share(client(docs_env, "bob"), doc["id"], everyone=True, permission="write")
        assert resp.status_code == 403
        assert [(s["user_id"], s["permission"]) for s in
                _run(docs_env.doc_store.list_shares(doc["id"]))] == [
            (uid(docs_env, "bob"), permission),
        ]

    def test_hidden_doc_404_equals_missing(self, docs_env):
        doc = seed_doc(docs_env, "Secret")
        c = client(docs_env, "bob")
        hidden = share(c, doc["id"], user_email=email(docs_env, "carol"), permission="read")
        missing_id = str(uuid.uuid4())
        missing = share(c, missing_id, user_email=email(docs_env, "carol"), permission="read")
        assert hidden.status_code == missing.status_code == 404
        assert detail(hidden) == {
            "error": "doc_not_found", "message": doc_not_found_message(doc["id"]),
        }
        assert detail(missing) == {
            "error": "doc_not_found", "message": doc_not_found_message(missing_id),
        }

    def test_public_project_doc_shareable(self, docs_env):
        doc = seed_doc(docs_env, "Open", mode="public", project_id=docs_env.public_project)
        resp = share(client(docs_env), doc["id"], user_email=email(docs_env, "bob"),
                     permission="read")
        assert resp.status_code == 200

    def test_gate_closed(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "read")])
        share_id = _run(docs_env.doc_store.list_shares(doc["id"]))[0]["id"]
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_DOCS, False)
        c = client(docs_env)
        for resp in (
            share(c, doc["id"], user_email=email(docs_env, "carol"), permission="read"),
            c.delete(f"/app/api/docs/{doc['id']}/shares/{share_id}"),
            c.get("/app/api/docs?shared=true"),
        ):
            assert resp.status_code == 403
            assert detail(resp) == {
                "error": "docs_disabled", "message": docs_disabled_message(),
            }
        assert len(_run(docs_env.doc_store.list_shares(doc["id"]))) == 1
        assert docs_env.published == []


# ---------------------------------------------------------------------------
# DELETE /docs/{id}/shares/{share_id}
# ---------------------------------------------------------------------------


class TestRemoveShare:
    def test_owner_removes(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "write"), ("carol", "read")])
        bob_share, carol_share = _run(docs_env.doc_store.list_shares(doc["id"]))
        c = client(docs_env)
        resp = c.delete(f"/app/api/docs/{doc['id']}/shares/{bob_share['id']}")
        assert resp.status_code == 200
        row = resp.json()
        assert [s["id"] for s in row["shares"]] == [carol_share["id"]]
        assert row["shared"] is True
        # Only the affected recipient (bob) and the owner; carol's view of
        # the doc is unchanged.
        assert published(docs_env) == access_changed(
            doc["id"], uid(docs_env, "alice"), uid(docs_env, "bob"),
        )
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["updated_at"] == doc["updated_at"]
        # Bob lost access at once.
        assert client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").status_code == 404
        # Removing the last share makes the doc unshared again.
        row = c.delete(f"/app/api/docs/{doc['id']}/shares/{carol_share['id']}").json()
        assert row["shares"] == [] and row["shared"] is False

    def test_everyone_row_removal_broadcasts(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, "Plan", shares=[(None, "read")])
        everyone = _run(docs_env.doc_store.list_shares(doc["id"]))[0]
        connected(monkeypatch, uid(docs_env, "bob"), uid(docs_env, "carol"))
        client(docs_env).delete(f"/app/api/docs/{doc['id']}/shares/{everyone['id']}")
        assert published(docs_env) == access_changed(
            doc["id"], uid(docs_env, "alice"), uid(docs_env, "bob"), uid(docs_env, "carol"),
        )

    @pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "0", "99999", "1e3"])
    def test_unknown_or_malformed_share_404(self, docs_env, bad):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "read")])
        resp = client(docs_env).delete(f"/app/api/docs/{doc['id']}/shares/{bad}")
        assert resp.status_code == 404
        assert detail(resp) == {"error": "share_not_found", "message": "Share not found."}
        assert len(_run(docs_env.doc_store.list_shares(doc["id"]))) == 1

    def test_share_of_another_doc_404(self, docs_env):
        mine = seed_doc(docs_env, "Mine", shares=[("carol", "read")])
        other = seed_doc(docs_env, "Other", shares=[("bob", "read")])
        foreign = _run(docs_env.doc_store.list_shares(other["id"]))[0]
        resp = client(docs_env).delete(f"/app/api/docs/{mine['id']}/shares/{foreign['id']}")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "share_not_found"
        assert len(_run(docs_env.doc_store.list_shares(other["id"]))) == 1
        # Twice is not found either.
        own = _run(docs_env.doc_store.list_shares(mine["id"]))[0]
        c = client(docs_env)
        assert c.delete(f"/app/api/docs/{mine['id']}/shares/{own['id']}").status_code == 200
        assert c.delete(f"/app/api/docs/{mine['id']}/shares/{own['id']}").status_code == 404

    @pytest.mark.parametrize("permission", ["read", "write"])
    def test_recipient_403_stranger_404(self, docs_env, permission):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", permission)])
        own = _run(docs_env.doc_store.list_shares(doc["id"]))[0]
        url = f"/app/api/docs/{doc['id']}/shares/{own['id']}"
        resp = client(docs_env, "bob").delete(url)
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        resp = client(docs_env, "carol").delete(url)
        assert resp.status_code == 404
        assert detail(resp)["error"] == "doc_not_found"
        assert len(_run(docs_env.doc_store.list_shares(doc["id"]))) == 1


# ---------------------------------------------------------------------------
# Rows and access flags
# ---------------------------------------------------------------------------


class TestRows:
    def test_read_share_recipient(self, docs_env):
        set_names(docs_env, alice="Alice A")
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "read")])
        _run(docs_env.doc_store.update_after_write(
            doc["id"], content_size=3, last_write_source=f"ui:{uid(docs_env, 'alice')}",
        ))
        row = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").json()
        assert row["shared_with_me"] is True
        assert row["permission"] == "read"
        assert row["owner"] == ref(docs_env, "alice", "Alice A")
        assert "shares" not in row
        assert row["last_write_source"] is None and row["last_write_user"] is None
        assert row["access"] == {
            "can_rename": False, "can_switch_mode": False, "can_delete": False,
            "can_edit": False, "can_share": False, "can_delete_assets": False,
            "write": "denied",
        }

    def test_write_share_recipient(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "write")])
        row = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").json()
        assert row["permission"] == "write"
        assert row["access"]["write"] == "free"
        assert row["access"]["can_edit"] is True
        for flag in OWNER_ONLY_FLAGS:
            assert row["access"][flag] is False, flag

    def test_effective_permission_is_the_higher_grant(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[(None, "read"), ("bob", "write")])
        rows = {
            who: client(docs_env, who).get(f"/app/api/docs/{doc['id']}").json()
            for who in ("bob", "carol")
        }
        assert rows["bob"]["permission"] == "write" and rows["bob"]["access"]["can_edit"]
        assert rows["carol"]["permission"] == "read"
        assert rows["carol"]["access"]["can_edit"] is False

    def test_owner_row(self, docs_env):
        set_names(docs_env, bob="Bob B")
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "write"), (None, "read")])
        _run(docs_env.doc_store.update_after_write(
            doc["id"], content_size=3, last_write_source=f"ui:{uid(docs_env, 'bob')}",
        ))
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["shared_with_me"] is False
        assert row["permission"] is None and row["owner"] is None
        assert [s["user"] for s in row["shares"]] == [ref(docs_env, "bob", "Bob B"), None]
        assert row["last_write_source"] == f"ui:{uid(docs_env, 'bob')}"
        assert row["last_write_user"] == ref(docs_env, "bob", "Bob B")
        assert row["access"] == {
            "can_rename": True, "can_switch_mode": False, "can_delete": True,
            "can_edit": True, "can_share": True, "can_delete_assets": True,
            "write": "free",
        }

    @pytest.mark.parametrize("source", [
        "ui", "conversation:abc", "action_request:7", None, "ui:", "ui:x", "ui:999999",
    ])
    def test_last_write_user_only_for_known_ui_writers(self, docs_env, source):
        doc = seed_doc(docs_env, "Plan")
        _run(docs_env.doc_store.update_after_write(
            doc["id"], content_size=3, last_write_source=source,
        ))
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_user"] is None

    def test_recipient_cannot_rename_or_delete(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "write")])
        c = client(docs_env, "bob")
        assert c.put(f"/app/api/docs/{doc['id']}", json={"title": "Mine"}).status_code == 403
        assert c.delete(f"/app/api/docs/{doc['id']}").status_code == 403
        stored = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert stored["title"] == "Plan"

    def test_one_user_lookup_per_response(self, docs_env, monkeypatch):
        from db import user_store

        calls = []
        real = user_store.get_users_by_ids

        async def _counting(ids):
            calls.append(set(ids))
            return await real(ids)

        monkeypatch.setattr(user_store, "get_users_by_ids", _counting)
        for i in range(3):
            seed_doc(docs_env, f"Alice {i}", shares=[("carol", "read")])
            seed_doc(docs_env, f"Bob {i}", owner="bob", shares=[("carol", "write")])
        data = client(docs_env, "carol").get("/app/api/docs?shared=true").json()
        assert len(data["docs"]) == 6
        assert calls == [{uid(docs_env, "alice"), uid(docs_env, "bob")}]
        owners = {r["owner"]["email"] for r in data["docs"]}
        assert owners == {email(docs_env, "alice"), email(docs_env, "bob")}

        calls.clear()
        data = client(docs_env).get("/app/api/docs").json()
        assert len(data["docs"]) == 3
        assert calls == [{uid(docs_env, "carol")}]

        # Nothing to resolve -> no lookup at all.
        calls.clear()
        client(docs_env, "carol").get("/app/api/docs")
        assert calls == []


# ---------------------------------------------------------------------------
# GET /docs?shared=true and the default list
# ---------------------------------------------------------------------------


class TestSharedList:
    def test_default_list_is_own_user_docs_only(self, docs_env):
        mine = seed_doc(docs_env, "Mine")
        seed_doc(docs_env, "Proj", project_id=docs_env.private_project)
        seed_doc(docs_env, "Bob's", owner="bob", shares=[("alice", "write")])
        seed_doc(docs_env, "Carol's", owner="carol", shares=[(None, "read")])
        shared_mine = seed_doc(docs_env, "Mine shared", shares=[("bob", "read")])
        ids = [r["id"] for r in client(docs_env).get("/app/api/docs").json()["docs"]]
        assert ids == [shared_mine["id"], mine["id"]]
        # Bob's default list holds none of alice's docs.
        bob_ids = [r["id"] for r in client(docs_env, "bob").get("/app/api/docs").json()["docs"]]
        assert shared_mine["id"] not in bob_ids and len(bob_ids) == 1

    def test_shared_stream(self, docs_env):
        direct = seed_doc(docs_env, "Direct", owner="bob", shares=[("alice", "read")])
        everyone = seed_doc(docs_env, "Everyone", owner="carol", shares=[(None, "write")])
        newest = seed_doc(docs_env, "Bob write share", owner="bob", shares=[("alice", "write")])
        seed_doc(docs_env, "Not shared", owner="bob")
        seed_doc(docs_env, "Shared elsewhere", owner="bob", shares=[("carol", "read")])
        own_shared = seed_doc(docs_env, "Mine", shares=[("bob", "read"), (None, "read")])

        data = client(docs_env).get("/app/api/docs?shared=true").json()
        rows = {r["id"]: r for r in data["docs"]}
        assert [r["id"] for r in data["docs"]] == [newest["id"], everyone["id"], direct["id"]]
        assert own_shared["id"] not in rows
        assert data["has_more"] is False and data["next_cursor"] is None
        assert rows[direct["id"]]["permission"] == "read"
        assert rows[everyone["id"]]["permission"] == "write"
        for row in rows.values():
            assert row["shared_with_me"] is True
            assert row["owner"]["id"] == row["owner_id"]
            assert "shares" not in row

    def test_everyone_row_reaches_every_other_user(self, docs_env):
        doc = seed_doc(docs_env, "For all", shares=[(None, "read")])
        for who in ("bob", "carol"):
            ids = [r["id"] for r in
                   client(docs_env, who).get("/app/api/docs?shared=true").json()["docs"]]
            assert ids == [doc["id"]], who
        # Never the owner's shared stream; it is in their own list.
        assert client(docs_env).get("/app/api/docs?shared=true").json()["docs"] == []
        assert [r["id"] for r in client(docs_env).get("/app/api/docs").json()["docs"]] == [
            doc["id"],
        ]

    def test_shared_project_docs_listed(self, docs_env):
        doc = seed_doc(docs_env, "Proj", project_id=docs_env.private_project,
                       shares=[("bob", "read")])
        rows = client(docs_env, "bob").get("/app/api/docs?shared=true").json()["docs"]
        assert [(r["id"], r["scope"], r["project_id"]) for r in rows] == [
            (doc["id"], "project", docs_env.private_project),
        ]
        # The project itself is still not bob's.
        resp = client(docs_env, "bob").get(
            f"/app/api/docs?project_id={docs_env.private_project}",
        )
        assert resp.status_code == 404

    def test_paging(self, docs_env):
        expected = []
        for i in range(7):
            owner = "bob" if i % 2 else "carol"
            grant = ("alice", "read") if i % 3 else (None, "write")
            expected.append(seed_doc(docs_env, f"S{i}", owner=owner, shares=[grant])["id"])
            seed_doc(docs_env, f"Own {i}")  # interleaved own docs never appear
        expected.reverse()  # newest first
        c = client(docs_env)
        full = [r["id"] for r in c.get("/app/api/docs?shared=true").json()["docs"]]
        assert full == expected

        seen, cursor, pages = [], None, 0
        while True:
            params = {"shared": "true", "limit": 3}
            if cursor:
                params["cursor"] = cursor
            data = c.get("/app/api/docs", params=params).json()
            pages += 1
            seen.extend(r["id"] for r in data["docs"])
            if not data["has_more"]:
                assert data["next_cursor"] is None
                break
            assert len(data["docs"]) == 3
            last = data["docs"][-1]
            assert data["next_cursor"] == f"{last['updated_at']}|{last['id']}"
            cursor = data["next_cursor"]
        assert pages == 3
        assert seen == expected

    def test_exact_page_has_no_more(self, docs_env):
        for i in range(2):
            seed_doc(docs_env, f"S{i}", owner="bob", shares=[("alice", "read")])
        data = client(docs_env).get("/app/api/docs?shared=true&limit=2").json()
        assert len(data["docs"]) == 2
        assert data["has_more"] is False and data["next_cursor"] is None
        data = client(docs_env).get("/app/api/docs?shared=true&limit=1").json()
        assert len(data["docs"]) == 1 and data["has_more"] is True

    def test_public_project_doc_listed_and_readable(self, docs_env):
        public = seed_doc(docs_env, "Open", mode="public",
                          project_id=docs_env.public_project, shares=[("bob", "read")])
        older = seed_doc(docs_env, "Older", owner="carol", shares=[("bob", "read")])
        # Make the public-project doc the newest so it leads the page.
        _run(docs_env.doc_store.update_after_write(
            public["id"], content_size=1, last_write_source="ui",
        ))
        bob = client(docs_env, "bob")

        def shared_ids(**params):
            data = bob.get("/app/api/docs", params={"shared": "true", **params}).json()
            return [r["id"] for r in data["docs"]], data["has_more"]

        assert shared_ids() == ([public["id"], older["id"]], False)
        assert shared_ids(limit=1) == ([public["id"]], True)
        assert bob.get(f"/app/api/docs/{public['id']}").status_code == 200

    def test_shared_with_project_id_400(self, docs_env):
        resp = client(docs_env).get(
            f"/app/api/docs?shared=true&project_id={docs_env.private_project}",
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    def test_invalid_cursor(self, docs_env):
        resp = client(docs_env).get("/app/api/docs?shared=true&limit=2&cursor=garbage")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_cursor"


# ---------------------------------------------------------------------------
# Recipients' conversations: the (unchanged) access matrix
# ---------------------------------------------------------------------------


class TestRecipientConversations:
    def test_read_share_user_doc_readable_not_writable(self, docs_env):
        doc = seed_doc(docs_env, "Plan", "alpha\n")
        share(client(docs_env), doc["id"], user_email=email(docs_env, "bob"), permission="read")
        bob = make_caller(docs_env, who="bob")
        result = _run(svc().read_doc(bob, doc["id"]))
        assert result["content"] == "alpha\n"
        assert result["writable"] == "denied"
        with pytest.raises(DocError) as exc:
            _run(svc().append_to_doc(bob, doc["id"], "more"))
        assert str(exc.value) == DENY_READ_ONLY_SHARE
        # Only the top-level private column: a public conversation never
        # sees a private doc.
        public_bob = make_caller(docs_env, who="bob", public=True)
        with pytest.raises(DocError) as exc:
            _run(svc().read_doc(public_bob, doc["id"]))
        assert str(exc.value) == doc_not_found_message(doc["id"])

    @pytest.mark.parametrize("permission", ["read", "write"])
    def test_shared_project_doc_hidden_from_recipient_conversations(self, docs_env,
                                                                    permission):
        from db import project_store

        doc = seed_doc(docs_env, "Proj", project_id=docs_env.private_project)
        share(client(docs_env), doc["id"], user_email=email(docs_env, "bob"),
              permission=permission)
        bob_project = _run(project_store.create_project(uid(docs_env, "bob"), "Bob's"))["id"]
        for caller in (
            make_caller(docs_env, who="bob"),
            make_caller(docs_env, who="bob", project=bob_project),
        ):
            with pytest.raises(DocError) as exc:
                _run(svc().read_doc(caller, doc["id"]))
            assert str(exc.value) == doc_not_found_message(doc["id"])
        # ... while the UI shows it.
        assert client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").status_code == 200

    def test_write_share_needs_a_card_that_applies_end_to_end(self, docs_env):
        doc = seed_doc(docs_env, "Plan", "alpha beta\n")
        share(client(docs_env), doc["id"], user_email=email(docs_env, "bob"),
              permission="write")
        bob = make_caller(docs_env, who="bob")
        with pytest.raises(DocApprovalRequired) as exc:
            _run(svc().append_to_doc(bob, doc["id"], "from bob"))
        suggested = exc.value.suggested_request
        assert suggested["request_type"] == "write_doc"
        read(bob, doc["id"])
        with pytest.raises(DocApprovalRequired):
            _run(svc().edit_doc(bob, doc["id"], "alpha", "ALPHA"))
        assert body(doc["id"]) == "alpha beta\n"

        # The model forwards the suggested request: pre-card, then approve.
        params = propose(bob, suggested["params"])
        assert "content_diff" in params
        execute(params, bob, request_id=91)
        assert body(doc["id"]) == "alpha beta\n\nfrom bob\n"
        params = propose(bob, edit_params(doc["id"]))
        execute(params, bob, request_id=92)
        assert body(doc["id"]) == "ALPHA beta\n\nfrom bob\n"
        stored = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert stored["last_write_source"] == "action_request:92"

    def test_owner_writes_flip_to_approval_after_first_share(self, docs_env):
        doc = seed_doc(docs_env, "Plan", "x\n")
        alice = make_caller(docs_env)
        _run(svc().append_to_doc(alice, doc["id"], "free"))
        c = client(docs_env)
        row = share(c, doc["id"], user_email=email(docs_env, "carol"), permission="read").json()
        with pytest.raises(DocApprovalRequired):
            _run(svc().append_to_doc(alice, doc["id"], "gated"))
        # The card route still works for the owner.
        params = propose(alice, append_params(doc["id"], content="approved"))
        execute(params, alice, request_id=5)
        assert body(doc["id"]) == "x\n\nfree\n\napproved\n"
        # Unsharing makes the owner's writes free again.
        c.delete(f"/app/api/docs/{doc['id']}/shares/{row['shares'][0]['id']}")
        _run(svc().append_to_doc(alice, doc["id"], "free again"))
        assert body(doc["id"]).endswith("free again\n")

    def test_revoked_share_refuses_a_pending_card(self, docs_env):
        doc = seed_doc(docs_env, "Plan", "x\n")
        row = share(client(docs_env), doc["id"], user_email=email(docs_env, "bob"),
                    permission="write").json()
        bob = make_caller(docs_env, who="bob")
        params = propose(bob, append_params(doc["id"], content="late"))
        client(docs_env).delete(f"/app/api/docs/{doc['id']}/shares/{row['shares'][0]['id']}")
        with pytest.raises(RuntimeError) as exc:
            execute(params, bob, request_id=8)
        assert str(exc.value) == doc_not_found_message(doc["id"])
        assert body(doc["id"]) == "x\n"


# ---------------------------------------------------------------------------
# Deleted users
# ---------------------------------------------------------------------------


class TestDeletedUser:
    def test_recipient_shares_cascade_everyone_row_survives(self, docs_env):
        from db import user_store

        doc = seed_doc(docs_env, "Plan")
        c = client(docs_env)
        share(c, doc["id"], user_email=email(docs_env, "bob"), permission="write")
        share(c, doc["id"], user_email=email(docs_env, "carol"), permission="read")
        share(c, doc["id"], everyone=True, permission="read")
        assert _run(user_store.delete_user(email(docs_env, "bob"))) is True

        assert [s["user_id"] for s in _run(docs_env.doc_store.list_shares(doc["id"]))] == [
            uid(docs_env, "carol"), None,
        ]
        row = c.get(f"/app/api/docs/{doc['id']}").json()
        assert [s["user"] and s["user"]["email"] for s in row["shares"]] == [
            email(docs_env, "carol"), None,
        ]


# ---------------------------------------------------------------------------
# Realtime audience
# ---------------------------------------------------------------------------


class TestRealtime:
    def test_write_reaches_owner_and_direct_recipients(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[("bob", "read"), ("carol", "write")])
        docs_env.published.clear()
        _run(svc().apply_write_operation(
            make_caller(docs_env), doc["id"], "append", {"content": "x"},
            write_source=None, bypass_approval=True,
        ))
        people = [uid(docs_env, w) for w in ("alice", "bob", "carol")]
        assert recipients(docs_env, "doc_changed") == people
        assert recipients(docs_env, "doc_list_changed") == people
        stored = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert {ev["updated_at"] for _u, ev in docs_env.published
                if ev["type"] == "doc_changed"} == {stored["updated_at"]}

    def test_recipient_write_reaches_owner(self, docs_env):
        doc = seed_doc(docs_env, "Plan", "x\n", shares=[("bob", "write")])
        bob = make_caller(docs_env, who="bob")
        params = propose(bob, append_params(doc["id"], content="hi"))
        docs_env.published.clear()
        execute(params, bob, request_id=3)
        assert recipients(docs_env, "doc_changed") == [uid(docs_env, "alice"), uid(docs_env, "bob")]

    def test_everyone_row_broadcasts_to_connected_users(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, "Plan", shares=[(None, "read"), ("bob", "write")])
        connected(monkeypatch, uid(docs_env, "carol"), uid(docs_env, "bob"),
                  uid(docs_env, "alice"), 777)
        docs_env.published.clear()
        _run(svc().apply_write_operation(
            make_caller(docs_env), doc["id"], "append", {"content": "x"},
            write_source=None, bypass_approval=True,
        ))
        expected = [uid(docs_env, "alice"), uid(docs_env, "bob"), uid(docs_env, "carol"), 777]
        assert recipients(docs_env, "doc_changed") == expected
        assert recipients(docs_env, "doc_list_changed") == expected

    def test_unshared_write_reaches_owner_only(self, docs_env, monkeypatch):
        connected(monkeypatch, uid(docs_env, "bob"))
        doc = seed_doc(docs_env, "Plan")
        _run(svc().append_to_doc(make_caller(docs_env), doc["id"], "x"))
        assert event_types(docs_env) == [
            (uid(docs_env, "alice"), "doc_changed"),
            (uid(docs_env, "alice"), "doc_list_changed"),
        ]

    def test_rename_reaches_the_audience(self, docs_env):
        doc = seed_doc(docs_env, "Plan", shares=[("carol", "read")])
        client(docs_env).put(f"/app/api/docs/{doc['id']}", json={"title": "Renamed"})
        assert sorted(event_types(docs_env)) == sorted([
            (uid(docs_env, w), t)
            for w in ("alice", "carol") for t in ("doc_list_changed", "doc_changed")
        ])

    def test_ui_create_is_owner_only_with_ui_source(self, docs_env, monkeypatch):
        connected(monkeypatch, uid(docs_env, "bob"))
        row = client(docs_env).post("/app/api/docs", json={"title": "New"}).json()
        assert row["last_write_source"] == f"ui:{uid(docs_env, 'alice')}"
        assert files.read_doc_meta(row["id"])["source"] == f"ui:{uid(docs_env, 'alice')}"
        assert {u for u, _t in event_types(docs_env)} == {uid(docs_env, "alice")}

    def test_delete_reaches_captured_audience(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, "Plan", shares=[("carol", "write"), (None, "read")])
        connected(monkeypatch, uid(docs_env, "bob"), 555)
        assert client(docs_env).delete(f"/app/api/docs/{doc['id']}").status_code == 200
        assert published(docs_env) == access_changed(
            doc["id"], *(uid(docs_env, w) for w in ("alice", "carol", "bob")), 555,
        )

    def test_project_delete_reaches_recipients(self, docs_env, monkeypatch):
        from chat.project_routes import delete_user_project

        one = seed_doc(docs_env, "One", project_id=docs_env.private_project,
                       shares=[("carol", "read")])
        two = seed_doc(docs_env, "Two", project_id=docs_env.private_project,
                       shares=[("bob", "write"), ("carol", "write")])
        elsewhere = seed_doc(docs_env, "Elsewhere", project_id=docs_env.other_project,
                             shares=[(None, "read")])
        alice, bob, carol = (uid(docs_env, w) for w in ("alice", "bob", "carol"))
        _run(delete_user_project(docs_env.private_project, user=docs_env.users["alice"]))
        seen = published(docs_env)
        # Per deleted doc, its own audience (docs visited in id order) ...
        assert sorted(seen[:5]) == sorted([
            (alice, "doc_changed", one["id"], None),
            (carol, "doc_changed", one["id"], None),
            (alice, "doc_changed", two["id"], None),
            (bob, "doc_changed", two["id"], None),
            (carol, "doc_changed", two["id"], None),
        ])
        # ... then one list refresh for the combined audience.
        assert sorted(seen[5:]) == sorted(
            (u, "doc_list_changed", None, None) for u in (alice, bob, carol)
        )

        # A project doc shared with everyone: every connected user hears.
        docs_env.published.clear()
        connected(monkeypatch, bob, 909)
        _run(delete_user_project(docs_env.other_project, user=docs_env.users["alice"]))
        assert published(docs_env) == access_changed(elsewhere["id"], alice, bob, 909)


class TestBusConnectedUsers:
    def test_connected_user_ids(self):
        from chat.realtime.bus import Bus, SubscriberQueue

        bus = Bus()
        assert bus.connected_user_ids() == []
        q1, q2, q3 = SubscriberQueue(), SubscriberQueue(), SubscriberQueue()
        bus.subscribe_user(1, q1)
        bus.subscribe_user(1, q2)
        bus.subscribe_user(2, q3)
        assert sorted(bus.connected_user_ids()) == [1, 2]
        bus.unsubscribe_user(1, q1)
        assert sorted(bus.connected_user_ids()) == [1, 2]
        bus.unsubscribe_user(1, q2)
        assert bus.connected_user_ids() == [2]
        # A snapshot: publishing while iterating is safe.
        snapshot = bus.connected_user_ids()
        bus.unsubscribe_user(2, q3)
        assert snapshot == [2] and bus.connected_user_ids() == []


# ---------------------------------------------------------------------------
# Review round 1
# ---------------------------------------------------------------------------


class TestEmailMatching:
    def test_unicode_folding_never_matches_another_spelling(self, docs_env):
        add_user(docs_env, "strasse@example.com")
        doc = seed_doc(docs_env, "Plan")
        resp = share(client(docs_env), doc["id"], user_email="straße@example.com",
                     permission="read")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "user_not_found"
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []

    def test_ascii_case_insensitive_match(self, docs_env):
        from db import user_store

        strasse = add_user(docs_env, "strasse@example.com")
        assert _run(user_store.find_user_by_email_ci(" STRASSE@Example.com "))["id"] == strasse
        assert _run(user_store.find_user_by_email_ci("STRAßE@example.com")) is None
        assert _run(user_store.find_user_by_email_ci("")) is None
        assert _run(user_store.find_user_by_email_ci(None)) is None

    def test_exact_match_wins_over_case_variants(self, docs_env):
        lower = add_user(docs_env, "dave@example.com")
        upper = add_user(docs_env, "Dave@example.com")
        c = client(docs_env)
        doc = seed_doc(docs_env, "Plan")
        row = share(c, doc["id"], user_email="dave@example.com", permission="read").json()
        assert [s["user_id"] for s in row["shares"]] == [lower]
        row = share(c, doc["id"], user_email=" Dave@example.com", permission="write").json()
        assert [(s["user_id"], s["permission"]) for s in row["shares"]] == [
            (lower, "read"), (upper, "write"),
        ]

    def test_case_variants_without_exact_match_409(self, docs_env):
        add_user(docs_env, "dave@example.com")
        add_user(docs_env, "Dave@example.com")
        doc = seed_doc(docs_env, "Plan")
        resp = share(client(docs_env), doc["id"], user_email="DAVE@example.com",
                     permission="read")
        assert resp.status_code == 409
        assert detail(resp) == {
            "error": "ambiguous_user",
            "message": "Several Quest accounts match that email; type it exactly.",
        }
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []
        assert docs_env.published == []


class TestGetUsersByIds:
    def test_safe_fields_only(self, docs_env):
        from db import user_store

        set_names(docs_env, bob="Bob B")
        found = _run(user_store.get_users_by_ids(
            [uid(docs_env, "bob"), uid(docs_env, "bob"), 999999, True, "1", None],
        ))
        assert found == {uid(docs_env, "bob"): ref(docs_env, "bob", "Bob B")}
        assert _run(user_store.get_users_by_ids([])) == {}


class TestPublicProjectDocShares:
    def test_write_share_recipient_can_edit(self, docs_env):
        doc = seed_doc(docs_env, "Open", mode="public", project_id=docs_env.public_project,
                       shares=[("bob", "write")])
        bob = client(docs_env, "bob")
        row = bob.get(f"/app/api/docs/{doc['id']}").json()
        assert row["access"]["can_edit"] is True
        data = bob.get("/app/api/docs", params={"shared": "true", "limit": 1}).json()
        assert [r["id"] for r in data["docs"]] == [doc["id"]]
        assert client(docs_env).get(f"/app/api/docs/{doc['id']}").status_code == 200

    def test_shared_stream_includes_public_project_docs(self, docs_env):
        public = seed_doc(docs_env, "Open", mode="public", project_id=docs_env.public_project,
                          shares=[("bob", "write")])
        private_proj = seed_doc(docs_env, "Proj", project_id=docs_env.private_project,
                                shares=[("bob", "read")])
        user_doc = seed_doc(docs_env, "Mine", shares=[("bob", "read")])
        store = docs_env.doc_store
        bob_id = uid(docs_env, "bob")
        assert {d["id"] for d in _run(store.list_docs_shared_with(bob_id))} == {
            public["id"], private_proj["id"], user_doc["id"],
        }


class TestShareRaces:
    def test_store_raises_share_target_gone_for_a_missing_recipient(self, docs_env):
        store = docs_env.doc_store
        doc = seed_doc(docs_env, "Plan")
        with pytest.raises(store.ShareTargetGoneError):
            _run(store.add_share(doc["id"], 999999, "read"))
        assert _run(store.list_shares(doc["id"])) == []

    def test_recipient_deleted_mid_request_404(self, docs_env, monkeypatch):
        from db import user_store

        async def _gone(_email):
            return {"id": 999999, "email": "ghost@example.com", "name": ""}

        monkeypatch.setattr(user_store, "find_user_by_email_ci", _gone)
        doc = seed_doc(docs_env, "Plan")
        resp = share(client(docs_env), doc["id"], user_email="ghost@example.com",
                     permission="read")
        assert resp.status_code == 404
        assert detail(resp) == {
            "error": "user_not_found", "message": "No Quest user has that email.",
        }
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []
        assert docs_env.published == []

    def test_doc_deleted_mid_request_404(self, docs_env, monkeypatch):
        store = docs_env.doc_store
        doc = seed_doc(docs_env, "Plan")

        async def _raced(doc_id, user_id, permission):
            await store.delete_doc(doc_id)
            raise store.ShareTargetGoneError("gone")

        monkeypatch.setattr(store, "add_share", _raced)
        resp = share(client(docs_env), doc["id"], user_email=email(docs_env, "bob"),
                     permission="read")
        assert resp.status_code == 404
        assert detail(resp) == {
            "error": "doc_not_found", "message": doc_not_found_message(doc["id"]),
        }


class TestCardAttribution:
    def _card(self, env, who, doc_id):
        return _run(env.action_request_store.create_action_request(
            uid(env, who), str(uuid.uuid4()), "write_doc",
            {"operation": "append", "doc_id": doc_id}, "r",
        ))["id"]

    def test_recipient_card_names_the_recipient_for_the_owner(self, ar_env):
        set_names(ar_env, bob="Bob B")
        doc = seed_doc(ar_env, "Plan", shares=[("bob", "write")])
        bob_card = self._card(ar_env, "bob", doc["id"])
        _run(ar_env.doc_store.update_after_write(
            doc["id"], content_size=1, last_write_source=f"action_request:{bob_card}",
        ))
        row = client(ar_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_source"] == f"action_request:{bob_card}"
        assert row["last_write_user"] == ref(ar_env, "bob", "Bob B")
        # The recipient sees neither.
        row = client(ar_env, "bob").get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_source"] is None and row["last_write_user"] is None

    def test_owner_card_or_unknown_request_stays_null(self, ar_env):
        doc = seed_doc(ar_env, "Plan", shares=[("bob", "write")])
        for source in (f"action_request:{self._card(ar_env, 'alice', doc['id'])}",
                       "action_request:424242", "action_request:x"):
            _run(ar_env.doc_store.update_after_write(
                doc["id"], content_size=1, last_write_source=source,
            ))
            row = client(ar_env).get(f"/app/api/docs/{doc['id']}").json()
            assert row["last_write_source"] == source
            assert row["last_write_user"] is None, source

    def test_one_card_lookup_per_response(self, ar_env, monkeypatch):
        store = ar_env.action_request_store
        calls = []
        real = store.get_action_request_owners

        async def _spy(ids):
            calls.append(set(ids))
            return await real(ids)

        monkeypatch.setattr(store, "get_action_request_owners", _spy)
        cards = []
        for i in range(3):
            doc = seed_doc(ar_env, f"D{i}", shares=[("bob", "write")])
            cards.append(self._card(ar_env, "bob", doc["id"]))
            _run(ar_env.doc_store.update_after_write(
                doc["id"], content_size=1, last_write_source=f"action_request:{cards[-1]}",
            ))
        rows = client(ar_env).get("/app/api/docs").json()["docs"]
        assert calls == [set(cards)]
        assert {r["last_write_user"]["id"] for r in rows} == {uid(ar_env, "bob")}
        # A recipient's list never looks cards up (no source to attribute).
        calls.clear()
        client(ar_env, "bob").get("/app/api/docs?shared=true")
        assert calls == []


class TestWriteShareEndToEnd:
    def _bob_card(self, env, doc_id, content):
        from chat.gemini_api.turn_tools import SuspendForActionRequest
        from db import conversation_store
        from datetime import datetime, timezone

        bob = make_caller(env, who="bob")
        _run(conversation_store.create_conversation(
            user_id=bob.user["id"], conversation_id=bob.conversation_id,
            created_at=datetime.now(timezone.utc),
        ))
        payload = tool("append_to_doc", bob, {"doc_id": doc_id, "content": content})
        assert payload["error"] == "approval_required"
        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(bob, payload["suggested_request"]["params"])
        return bob, suspended.value.request_id

    def test_share_route_then_card_then_resolve_route(self, ar_env):
        set_names(ar_env, bob="Bob B")
        doc = seed_doc(ar_env, "Plan", "alpha\n")
        row = share(client(ar_env), doc["id"], user_email=email(ar_env, "bob"),
                    permission="write").json()
        assert row["shares"][0]["permission"] == "write"

        bob, request_id = self._bob_card(ar_env, doc["id"], "from bob")
        card = _run(ar_env.action_request_store.get_action_request(bob.user["id"], request_id))
        assert card["status"] == "open" and card["params"]["share_summary"] == (
            "shared with 1 user"
        )
        assert body(doc["id"]) == "alpha\n"

        resolved = resolve(request_id, bob.user)
        assert resolved["status"] == "executed"
        assert body(doc["id"]) == "alpha\n\nfrom bob\n"
        owner_row = client(ar_env).get(f"/app/api/docs/{doc['id']}").json()
        assert owner_row["last_write_source"] == f"action_request:{request_id}"
        assert owner_row["last_write_user"] == ref(ar_env, "bob", "Bob B")
        assert set(recipients(ar_env, "doc_changed")) == {
            uid(ar_env, "alice"), uid(ar_env, "bob"),
        }

    def test_downgrade_while_pending_refuses_at_approve(self, ar_env):
        from fastapi import HTTPException

        doc = seed_doc(ar_env, "Plan", "alpha\n")
        c = client(ar_env)
        share(c, doc["id"], user_email=email(ar_env, "bob"), permission="write")
        bob, request_id = self._bob_card(ar_env, doc["id"], "late")
        share(c, doc["id"], user_email=email(ar_env, "bob"), permission="read")

        with pytest.raises(HTTPException) as exc:
            resolve(request_id, bob.user)
        assert exc.value.status_code == 500
        assert DENY_READ_ONLY_SHARE in exc.value.detail["message"]
        card = _run(ar_env.action_request_store.get_action_request(bob.user["id"], request_id))
        assert card["status"] == "open"
        assert body(doc["id"]) == "alpha\n"


class TestSharedListEqualTimestamps:
    def test_keyset_paging_with_equal_updated_at(self, docs_env):
        ids = [
            seed_doc(docs_env, f"S{i}", owner="bob", shares=[("alice", "read")])["id"]
            for i in range(5)
        ]
        set_updated_at(docs_env, ids, "2026-10-01 12:00:00.000000")
        expected = sorted(ids, reverse=True)  # ties break on id DESC
        c = client(docs_env)
        assert [r["id"] for r in c.get("/app/api/docs?shared=true").json()["docs"]] == expected
        seen, cursor, pages = [], None, 0
        while True:
            params = {"shared": "true", "limit": 2}
            if cursor:
                params["cursor"] = cursor
            data = c.get("/app/api/docs", params=params).json()
            pages += 1
            seen.extend(r["id"] for r in data["docs"])
            if not data["has_more"]:
                break
            cursor = data["next_cursor"]
        assert pages == 3
        assert seen == expected


class TestRemoveShareHiddenDoc:
    def test_hidden_doc_404_equals_missing(self, docs_env):
        doc = seed_doc(docs_env, "Secret", shares=[("carol", "read")])
        share_id = _run(docs_env.doc_store.list_shares(doc["id"]))[0]["id"]
        c = client(docs_env, "bob")
        hidden = c.delete(f"/app/api/docs/{doc['id']}/shares/{share_id}")
        missing_id = str(uuid.uuid4())
        missing = c.delete(f"/app/api/docs/{missing_id}/shares/{share_id}")
        assert hidden.status_code == missing.status_code == 404
        assert detail(hidden) == {
            "error": "doc_not_found", "message": doc_not_found_message(doc["id"]),
        }
        assert detail(missing) == {
            "error": "doc_not_found", "message": doc_not_found_message(missing_id),
        }
        assert len(_run(docs_env.doc_store.list_shares(doc["id"]))) == 1


# ---------------------------------------------------------------------------
# Review round 2: read-only viewers see only referenced images
# ---------------------------------------------------------------------------

# chart.png referenced plainly, logo.gif only through an escaped spelling
# (an HTML character reference for the dot), old.png not at all (removed
# from the text before sharing).
REFERENCING_BODY = "# Report\n![Chart](assets/chart.png)\n![Logo](assets/logo&#46;gif)\n"
ALL_IMAGES = ["chart.png", "logo.gif", "old.png"]
REFERENCED = ["chart.png", "logo.gif"]


class TestReadOnlyViewersSeeReferencedImagesOnly:
    def _doc(self, env, body=REFERENCING_BODY):
        doc = seed_doc(env, "Pics", body, shares=[("bob", "read"), ("carol", "write")])
        for name, data in (("chart.png", PNG), ("logo.gif", GIF), ("old.png", PNG)):
            assert files.add_asset(doc["id"], name, data).name == name
        return doc

    def _everyone_read_doc(self, env):
        doc = seed_doc(env, "Pics", REFERENCING_BODY, shares=[(None, "read")])
        for name, data in (("chart.png", PNG), ("logo.gif", GIF), ("old.png", PNG)):
            files.add_asset(doc["id"], name, data)
        return doc

    @staticmethod
    def _listed(c, doc_id):
        return [a["name"] for a in c.get(f"/app/api/docs/{doc_id}").json()["assets"]]

    @staticmethod
    def _zip_names(c, doc_id):
        resp = c.get(f"/app/api/docs/{doc_id}/download?format=zip")
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            return sorted(zf.namelist()), zf.read("doc.md")

    def test_detail_lists_only_referenced_images(self, docs_env):
        doc = self._doc(docs_env)
        assert self._listed(client(docs_env, "bob"), doc["id"]) == REFERENCED
        # Owner and write share: everything (old.png may be in history).
        assert self._listed(client(docs_env), doc["id"]) == ALL_IMAGES
        assert self._listed(client(docs_env, "carol"), doc["id"]) == ALL_IMAGES

    def test_everyone_read_share_is_read_only_too(self, docs_env):
        doc = self._everyone_read_doc(docs_env)
        c = client(docs_env, "carol")
        assert self._listed(c, doc["id"]) == REFERENCED
        assert c.get(f"/app/api/docs/{doc['id']}/assets/old.png").status_code == 404

    def test_unreferenced_asset_404_like_a_missing_one(self, docs_env):
        doc = self._doc(docs_env)
        bob = client(docs_env, "bob")
        base = f"/app/api/docs/{doc['id']}/assets"
        unreferenced = bob.get(f"{base}/old.png")
        missing = bob.get(f"{base}/never.png")
        assert unreferenced.status_code == missing.status_code == 404
        assert detail(unreferenced) == {
            "error": "asset_not_found", "message": "Image not found: old.png",
        }
        assert detail(missing) == {
            "error": "asset_not_found", "message": "Image not found: never.png",
        }
        for name, data in (("chart.png", PNG), ("logo.gif", GIF)):
            resp = bob.get(f"{base}/{name}")
            assert resp.status_code == 200 and resp.content == data, name
        # Owner and write share still fetch the unreferenced image.
        for who in ("alice", "carol"):
            resp = client(docs_env, who).get(f"{base}/old.png")
            assert resp.status_code == 200 and resp.content == PNG, who

    def test_unreferenced_after_an_edit_disappears(self, docs_env):
        doc = self._doc(docs_env)
        files.write_body(doc["id"], "no images any more\n", write_source="ui")
        bob = client(docs_env, "bob")
        assert self._listed(bob, doc["id"]) == []
        resp = bob.get(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "asset_not_found"

    def test_zip_holds_only_referenced_images(self, docs_env):
        doc = self._doc(docs_env)
        names, body_bytes = self._zip_names(client(docs_env, "bob"), doc["id"])
        assert names == ["assets/chart.png", "assets/logo.gif", "doc.md"]
        assert body_bytes == REFERENCING_BODY.encode()
        for who in ("alice", "carol"):
            names, _ = self._zip_names(client(docs_env, who), doc["id"])
            assert names == [f"assets/{n}" for n in ALL_IMAGES] + ["doc.md"], who

    @pytest.mark.parametrize("spelling", [
        "assets/ch%61rt.png", "assets%2Fchart.png", "assets/chart&#46;png",
        "assets/chart\\.png", '<img src="assets/chart.png?v=2">',
    ])
    def test_escaped_spellings_count_as_referenced(self, docs_env, spelling):
        doc = self._doc(docs_env, body=f"see {spelling}\n")
        bob = client(docs_env, "bob")
        assert self._listed(bob, doc["id"]) == ["chart.png"]
        assert bob.get(f"/app/api/docs/{doc['id']}/assets/chart.png").status_code == 200
        names, _ = self._zip_names(bob, doc["id"])
        assert names == ["assets/chart.png", "doc.md"]

    def test_lookalike_name_is_not_a_reference(self, docs_env):
        doc = self._doc(docs_env, body="![x](assets/chart.png.png)\n")
        assert self._listed(client(docs_env, "bob"), doc["id"]) == []


class TestBuildZipFilter:
    def test_filter_sees_the_archived_body(self, docs_env):
        doc = seed_doc(docs_env, "Pics", "![a](assets/a.png)\n")
        files.add_asset(doc["id"], "a.png", PNG)
        files.add_asset(doc["id"], "b.png", PNG)
        seen = []

        def keep(body, name):
            seen.append((body, name))
            return name == "a.png"

        path = files.build_zip(doc["id"], "Pics", keep)
        try:
            with zipfile.ZipFile(path) as zf:
                assert sorted(zf.namelist()) == ["assets/a.png", "doc.md"]
                archived = zf.read("doc.md").decode()
        finally:
            os.unlink(path)
        assert seen == [(archived, "a.png"), (archived, "b.png")]

    def test_no_filter_is_unchanged(self, docs_env):
        doc = seed_doc(docs_env, "Pics", "nothing\n")
        files.add_asset(doc["id"], "a.png", PNG)
        path = files.build_zip(doc["id"], "Pics")
        try:
            with zipfile.ZipFile(path) as zf:
                assert sorted(zf.namelist()) == ["assets/a.png", "doc.md"]
        finally:
            os.unlink(path)

    def test_excluded_symlink_still_refuses(self, docs_env):
        doc = seed_doc(docs_env, "Pics", "nothing\n")
        outside = docs_env.tmp / "outside.png"
        outside.write_bytes(PNG)
        os.symlink(outside, docs_env.dirs["docs"] / doc["id"] / "assets" / "link.png")
        with pytest.raises(files.DocFileError):
            files.build_zip(doc["id"], "Pics", lambda body, name: False)


# ---------------------------------------------------------------------------
# Final cross-package review
# ---------------------------------------------------------------------------


def edit_client(env, who="alice"):
    """The base router + the share router + the editor's routes."""
    from chat.docs import edit_routes, routes, share_routes

    app = FastAPI()
    for r in (routes.router, share_routes.router, edit_routes.router):
        app.include_router(r)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def ui_writes_mod():
    from chat.docs import ui_writes
    return ui_writes


class TestReferencedNamesComputedOncePerBody:
    """200 images + a ~1 MB body full of ``%`` / ``&`` escapes: the
    reference set is computed a bounded number of times per request."""

    def _big_doc(self, env):
        from chat.docs import constants

        names = [f"img{i:03d}.png" for i in range(constants.DOC_MAX_ASSETS)]
        refs = "".join(f"![{n}](assets/{n})\n" for n in names[::2])  # half
        filler_line = "100% of &amp; costs &#46; and %41%42 here\n"
        filler = filler_line * ((900_000 - len(refs)) // len(filler_line))
        doc = seed_doc(env, "Big", refs + filler, shares=[("bob", "read")])
        for name in names:
            files.add_asset(doc["id"], name, PNG)
        assert len(files.list_assets(doc["id"])) == len(names)
        return doc, names

    def _count_variants(self, monkeypatch):
        mod = ui_writes_mod()
        calls = []
        real = mod._reference_variants

        def _spy(body):
            calls.append(len(body))
            return real(body)

        monkeypatch.setattr(mod, "_reference_variants", _spy)
        return calls

    def test_detail_zip_and_asset_route(self, docs_env, monkeypatch):
        doc, names = self._big_doc(docs_env)
        calls = self._count_variants(monkeypatch)
        bob = client(docs_env, "bob")

        listed = [a["name"] for a in bob.get(f"/app/api/docs/{doc['id']}").json()["assets"]]
        assert listed == names[::2]
        assert len(calls) == 1  # one pass for 200 candidates

        # The detail primed the asset route's cache for this updated_at.
        for name in names[:6]:
            resp = bob.get(f"/app/api/docs/{doc['id']}/assets/{name}")
            assert resp.status_code == (200 if name in listed else 404), name
        assert len(calls) == 1

        resp = bob.get(f"/app/api/docs/{doc['id']}/download?format=zip")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert len(zf.namelist()) == 1 + len(listed)
        assert len(calls) == 2  # the zip: one pass inside build_zip

    def test_asset_route_cold_cache_computes_once(self, docs_env, monkeypatch):
        doc, names = self._big_doc(docs_env)
        calls = self._count_variants(monkeypatch)
        bob = client(docs_env, "bob")
        for name in names[:10]:
            bob.get(f"/app/api/docs/{doc['id']}/assets/{name}")
        assert len(calls) == 1

    def test_cache_follows_body_writes(self, docs_env):
        doc = seed_doc(docs_env, "Pics", "![c](assets/chart.png)\n",
                       shares=[("bob", "read")])
        files.add_asset(doc["id"], "chart.png", PNG)
        bob = client(docs_env, "bob")
        url = f"/app/api/docs/{doc['id']}/assets/chart.png"
        assert bob.get(url).status_code == 200
        token = _run(docs_env.doc_store.get_doc(doc["id"]))["updated_at"]
        _run(ui_writes_mod().replace_body_from_ui(
            docs_env.users["alice"], doc["id"], "no image\n", expected_updated_at=token,
        ))
        assert bob.get(url).status_code == 404

    def test_cache_is_bounded(self, docs_env):
        from chat.docs import routes

        for i in range(routes._REFERENCED_NAMES_CACHE_SIZE + 5):
            routes._remember_referenced_names(
                {"id": f"doc-{i}", "updated_at": "t"}, frozenset(),
            )
        assert len(routes._REFERENCED_NAMES_CACHE) == routes._REFERENCED_NAMES_CACHE_SIZE
        assert ("doc-0", "t") not in routes._REFERENCED_NAMES_CACHE

    def test_delete_in_use_check_runs_off_the_event_loop(self, docs_env, monkeypatch):
        import asyncio

        mod = ui_writes_mod()
        on_loop = []
        real = mod.asset_referenced

        def _spy(body, name):
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            return real(body, name)

        monkeypatch.setattr(mod, "asset_referenced", _spy)
        doc = seed_doc(docs_env, "Pics", "no images\n")
        files.add_asset(doc["id"], "chart.png", PNG)
        _run(mod.delete_asset_from_ui(docs_env.users["alice"], doc["id"], "chart.png"))
        assert on_loop == [False]

    def test_set_matches_the_single_name_check(self):
        mod = ui_writes_mod()
        body = (
            "![a](assets/a.png) assets/b.png.png assets%2Fc.png assets/d&#46;png "
            "assets/e\\.png assets/f.png?v=2 assets/assets/g.png x/assets/h.png"
        )
        names = mod.referenced_asset_names(body)
        for name in ("a.png", "b.png.png", "c.png", "d.png", "e.png", "f.png",
                     "g.png", "h.png"):
            assert name in names and mod.asset_referenced(body, name), name
        for name in ("b.png", "z.png", "f.png?v=2"):
            assert name not in names and not mod.asset_referenced(body, name), name


class TestImageOnlyWritesKeepTheBodyWriter:
    def test_upload_and_delete_keep_last_write_source(self, docs_env):
        set_names(docs_env, bob="Bob B")
        mod = ui_writes_mod()
        doc = seed_doc(docs_env, "Plan", "x\n", shares=[("bob", "write")])
        token = _run(docs_env.doc_store.get_doc(doc["id"]))["updated_at"]
        _run(mod.replace_body_from_ui(
            docs_env.users["bob"], doc["id"], "bob wrote this\n", expected_updated_at=token,
        ))
        bob_source = f"ui:{uid(docs_env, 'bob')}"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] == bob_source

        before = _run(docs_env.doc_store.get_doc(doc["id"]))
        result = _run(mod.add_asset_from_ui(
            docs_env.users["alice"], doc["id"], "chart.png", PNG,
        ))
        after = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert after["updated_at"] == result["updated_at"] != before["updated_at"]
        assert after["asset_count"] == 1
        assert after["last_write_source"] == bob_source
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_user"] == ref(docs_env, "bob", "Bob B")

        _run(mod.delete_asset_from_ui(docs_env.users["alice"], doc["id"], "chart.png"))
        after = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert after["asset_count"] == 0
        assert after["last_write_source"] == bob_source


class TestCardProposerGone:
    def test_gone_proposer_keeps_the_id(self, ar_env, monkeypatch):
        from db import user_store

        doc = seed_doc(ar_env, "Plan", shares=[("bob", "write")])
        card = _run(ar_env.action_request_store.create_action_request(
            uid(ar_env, "bob"), str(uuid.uuid4()), "write_doc",
            {"operation": "append", "doc_id": doc["id"]}, "r",
        ))["id"]
        _run(ar_env.doc_store.update_after_write(
            doc["id"], content_size=1, last_write_source=f"action_request:{card}",
        ))
        real = user_store.get_users_by_ids
        bob_id = uid(ar_env, "bob")

        async def _without_bob(ids):
            return {k: v for k, v in (await real(ids)).items() if k != bob_id}

        # The account vanished between the card lookup and the user lookup
        # (a deleted user's cards normally cascade away with them).
        monkeypatch.setattr(user_store, "get_users_by_ids", _without_bob)
        row = client(ar_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_user"] == {"id": bob_id, "name": None, "email": None}


class TestBrokenBodyIs400:
    @pytest.mark.parametrize("breakage", ["missing", "symlink"])
    def test_content_put(self, docs_env, breakage):
        doc = seed_doc(docs_env, "Plan", "x\n")
        body_path = docs_env.dirs["docs"] / doc["id"] / "doc.md"
        body_path.unlink()
        if breakage == "symlink":
            outside = docs_env.tmp / "outside.md"
            outside.write_text("secret\n")
            os.symlink(outside, body_path)
        token = _run(docs_env.doc_store.get_doc(doc["id"]))["updated_at"]
        resp = edit_client(docs_env).put(
            f"/app/api/docs/{doc['id']}/content",
            json={"content": "new\n", "expected_updated_at": token},
        )
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_doc_files"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["updated_at"] == token
        if breakage == "symlink":
            assert (docs_env.tmp / "outside.md").read_text() == "secret\n"

    def test_asset_delete(self, docs_env):
        doc = seed_doc(docs_env, "Plan", "x\n")
        files.add_asset(doc["id"], "chart.png", PNG)
        (docs_env.dirs["docs"] / doc["id"] / "doc.md").unlink()
        resp = edit_client(docs_env).delete(f"/app/api/docs/{doc['id']}/assets/chart.png")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_doc_files"
        assert [a["name"] for a in files.list_assets(doc["id"])] == ["chart.png"]
