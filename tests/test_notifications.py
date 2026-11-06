"""System notifications: the inbox's Other tab.

Covers the store (create, list order, open count, dismiss and its
ownership check), chat/notifications.py (kinds, the
``notifications_changed`` per-user global) and the
``/app/api/notifications`` routes.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat import notifications


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated DB with alice and bob; published per-user events captured."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models
    import db.notification_store as store

    models.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(store, "AsyncSessionLocal", session_local)

    published: list[tuple[int, dict]] = []
    from chat.realtime import bus
    monkeypatch.setattr(
        bus, "publish_to_user", lambda user_id, ev: published.append((user_id, ev)),
    )

    async def _seed():
        async with session_local() as db:
            users = [
                models.User(email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for name in ("alice", "bob")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            return {u.email.split("@")[0]: {"id": u.id, "email": u.email} for u in users}

    class Env:
        pass

    e = Env()
    e.users = _run(_seed())
    e.alice, e.bob = e.users["alice"]["id"], e.users["bob"]["id"]
    e.published = published
    e.store = store
    e.session_local = session_local
    e.models = models
    yield e
    _run(async_engine.dispose())
    sync_engine.dispose()


def _notify(env, user_id, title="Your guides were converted to skills", **kw):
    return _run(notifications.notify(
        user_id, notifications.NOTIFICATION_GUIDES_CONVERTED, title, **kw,
    ))


class TestStore:
    def test_create_list_and_count(self, env):
        first = _notify(env, env.alice, "First", body="one\ntwo", data={"skills": []})
        second = _notify(env, env.alice, "Second")
        _notify(env, env.bob, "Bob's")

        assert first["kind"] == "guides_converted"
        assert first["body"] == "one\ntwo" and first["data"] == {"skills": []}
        assert first["created_at"] and first["dismissed_at"] is None
        assert second["body"] == "" and second["data"] is None

        listed = _run(env.store.list_for_user(env.alice))
        assert [n["title"] for n in listed] == ["Second", "First"]  # newest first
        assert _run(env.store.count_open(env.alice)) == 2
        assert _run(env.store.count_open(env.bob)) == 1
        assert [n["title"] for n in _run(env.store.list_for_user(env.alice, limit=1))] == ["Second"]

    def test_dismiss_is_scoped_to_the_owner_and_idempotent(self, env):
        note = _notify(env, env.alice)

        assert _run(env.store.dismiss(env.bob, note["id"])) is None
        assert _run(env.store.dismiss(env.alice, "no-such-id")) is None
        assert _run(env.store.count_open(env.alice)) == 1

        dismissed = _run(env.store.dismiss(env.alice, note["id"]))
        assert dismissed["dismissed_at"] is not None
        again = _run(env.store.dismiss(env.alice, note["id"]))
        assert again["dismissed_at"] == dismissed["dismissed_at"]
        assert _run(env.store.count_open(env.alice)) == 0
        # A dismissed notification stays listed.
        assert [n["id"] for n in _run(env.store.list_for_user(env.alice))] == [note["id"]]

    def test_title_is_required_and_capped(self, env):
        with pytest.raises(ValueError):
            _run(env.store.create_notification(env.alice, "guides_converted", "  "))
        with pytest.raises(ValueError):
            _run(env.store.create_notification(env.alice, "guides_converted", "x" * 256))

    def test_rows_go_with_their_user(self, env):
        _notify(env, env.alice)
        _notify(env, env.bob)

        async def _delete_alice():
            async with env.session_local() as db:
                await db.delete(await db.get(env.models.User, env.alice))
                await db.commit()

        _run(_delete_alice())
        assert _run(env.store.list_for_user(env.alice)) == []
        assert _run(env.store.count_open(env.bob)) == 1


class TestEvents:
    def test_notify_and_dismiss_publish_the_open_count(self, env):
        first = _notify(env, env.alice, "First")
        _notify(env, env.alice, "Second")
        assert env.published == [
            (env.alice, {"type": "notifications_changed", "open_count": 1}),
            (env.alice, {"type": "notifications_changed", "open_count": 2}),
        ]

        env.published.clear()
        notification, open_count = _run(notifications.dismiss(env.alice, first["id"]))
        assert notification["dismissed_at"] is not None and open_count == 1
        assert env.published == [
            (env.alice, {"type": "notifications_changed", "open_count": 1}),
        ]

        env.published.clear()
        assert _run(notifications.dismiss(env.bob, first["id"])) is None
        assert env.published == []

    def test_unknown_kind_is_refused(self, env):
        with pytest.raises(ValueError):
            _run(notifications.notify(env.alice, "made_up", "Title"))
        assert _run(env.store.list_for_user(env.alice)) == []


def _client(env, who="alice"):
    from chat import notification_routes

    app = FastAPI()
    app.include_router(notification_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[
        notification_routes.get_current_user_cookie_or_apikey_checked
    ] = _current
    return TestClient(app)


class TestRoutes:
    def test_list_count_and_dismiss(self, env):
        old = _notify(env, env.alice, "Old", body="b", data={"skills": [{"skill_id": "s1"}]})
        new = _notify(env, env.alice, "New")
        _notify(env, env.bob, "Bob's")
        client = _client(env)

        body = client.get("/app/api/notifications").json()
        assert body["open_count"] == 2
        assert [n["id"] for n in body["notifications"]] == [new["id"], old["id"]]
        assert body["notifications"][1] == {
            "id": old["id"], "kind": "guides_converted", "title": "Old", "body": "b",
            "data": {"skills": [{"skill_id": "s1"}]},
            "created_at": old["created_at"], "dismissed_at": None,
        }
        assert client.get("/app/api/notifications/count").json() == {"open_count": 2}

        resp = client.post(f"/app/api/notifications/{old['id']}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["open_count"] == 1
        assert resp.json()["notification"]["dismissed_at"] is not None
        assert "user_id" not in resp.json()["notification"]
        # Again: still a success, nothing changes.
        assert client.post(f"/app/api/notifications/{old['id']}/dismiss").json()["open_count"] == 1

        body = client.get("/app/api/notifications").json()
        assert body["open_count"] == 1
        assert [bool(n["dismissed_at"]) for n in body["notifications"]] == [False, True]

    def test_another_users_notification_is_not_found(self, env):
        note = _notify(env, env.alice)
        bob = _client(env, "bob")

        assert bob.get("/app/api/notifications").json() == {
            "notifications": [], "open_count": 0,
        }
        resp = bob.post(f"/app/api/notifications/{note['id']}/dismiss")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "not_found"
        assert _run(env.store.count_open(env.alice)) == 1
