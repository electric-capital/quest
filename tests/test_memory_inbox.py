"""The inbox's Memories tab (chat/memory_inbox.py) and what feeds it.

Covers the per-user store queries of both suggestion kinds (pending across
conversations, keyset-paged resolved rows, the cross-user pending
summaries, conversation context), the merged inbox listing with its
cursor, the ``total_pending`` event field, the ``/app/api/memory-inbox``
routes, and the Slack reminder's merged counts and wording.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat import memory_inbox, slack_notifier


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated DB: alice has a standalone chat and a project chat, bob a
    standalone chat; every suggestion store points at it, events captured."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.conversation_store as conversation_store
    import db.memory_store as memory_store
    import db.memory_suggestion_store as user_store
    import db.models as models
    import db.project_memory_store as project_memory_store
    import db.project_memory_suggestion_store as project_store
    import db.project_store as projects

    models.Base.metadata.create_all(sync_engine)
    for mod in (
        conversation_store, memory_store, user_store, project_memory_store,
        project_store, projects,
    ):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

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

    users = _run(_seed())
    alice, bob = users["alice"]["id"], users["bob"]["id"]
    project = _run(projects.create_project(alice, "Launch"))
    chat_id, project_chat_id, bob_chat_id = (str(uuid.uuid4()) for _ in range(3))
    _run(conversation_store.create_conversation(
        alice, chat_id, datetime.now(timezone.utc), custom_name="Coffee chat",
    ))
    _run(conversation_store.create_conversation(
        alice, project_chat_id, datetime.now(timezone.utc), project_id=project["id"],
    ))
    _run(conversation_store.create_conversation(bob, bob_chat_id, datetime.now(timezone.utc)))

    class Env:
        pass

    e = Env()
    e.users = users
    e.alice, e.bob = alice, bob
    e.project_id = project["id"]
    e.chat_id, e.project_chat_id, e.bob_chat_id = chat_id, project_chat_id, bob_chat_id
    e.published = published
    e.session_local = session_local
    e.models = models
    e.user_store = user_store
    e.project_store = project_store
    return e


def _backdate(env, model, row_id: str, **fields):
    """Set created_at / resolved_at on a row directly (naive UTC as stored)."""
    async def _go():
        async with env.session_local() as db:
            row = await db.get(model, row_id)
            for name, value in fields.items():
                setattr(row, name, value)
            await db.commit()
    _run(_go())


def _naive_utc(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


def _user_suggestion(env, content, conversation_id=None, who=None):
    return _run(env.user_store.create_suggestion(
        who or env.alice, conversation_id or env.chat_id, content,
    ))


def _project_suggestion(env, content):
    return _run(env.project_store.create_suggestion(
        env.alice, env.project_id, env.project_chat_id, content,
    ))


# ---------------------------------------------------------------------------
# Store: per-user queries
# ---------------------------------------------------------------------------


class TestStores:
    def test_pending_across_conversations_and_users(self, env):
        a = _user_suggestion(env, "A")
        b = _user_suggestion(env, "B", conversation_id=env.project_chat_id)
        _user_suggestion(env, "Bob's", conversation_id=env.bob_chat_id, who=env.bob)
        p = _project_suggestion(env, "P")

        assert [s["id"] for s in _run(env.user_store.list_pending_for_user(env.alice))] == [a["id"], b["id"]]
        assert _run(env.user_store.count_pending_for_user(env.alice)) == 2
        assert _run(env.user_store.count_pending_for_user(env.bob)) == 1
        assert [s["id"] for s in _run(env.project_store.list_pending_for_user(env.alice))] == [p["id"]]
        assert _run(env.project_store.count_pending_for_user(env.alice)) == 1
        assert _run(env.project_store.count_pending_for_user(env.bob)) == 0

        # Resolving drops the row from the pending lists.
        _run(env.user_store.dismiss_suggestion(env.alice, a["id"]))
        assert _run(env.user_store.count_pending_for_user(env.alice)) == 1

    def test_pending_rows_carry_replaces_content(self, env):
        import db.memory_store as memory_store
        old = _run(memory_store.create_memory(env.alice, "Drinks drip coffee"))
        _run(env.user_store.create_suggestion(
            env.alice, env.chat_id, "Drinks pour-over", replaces_memory_id=old["id"],
        ))
        [row] = _run(env.user_store.list_pending_for_user(env.alice))
        assert row["replaces_content"] == "Drinks drip coffee"

    def test_resolved_keyset_paging(self, env):
        rows = [_user_suggestion(env, f"fact {i}") for i in range(5)]
        for i, row in enumerate(rows):
            _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(
                env, env.models.MemorySuggestion, row["id"],
                resolved_at=_naive_utc(minutes=10 - i),
            )
        pending = _user_suggestion(env, "still pending")

        first = _run(env.user_store.list_resolved_for_user(env.alice, 2))
        assert [r["content"] for r in first] == ["fact 4", "fact 3"]
        assert pending["id"] not in {r["id"] for r in first}

        cursor = env.user_store.decode_resolved_cursor(
            env.user_store.encode_resolved_cursor(first[-1]),
        )
        second = _run(env.user_store.list_resolved_for_user(env.alice, 2, cursor))
        assert [r["content"] for r in second] == ["fact 2", "fact 1"]
        cursor = env.user_store.decode_resolved_cursor(
            env.user_store.encode_resolved_cursor(second[-1]),
        )
        third = _run(env.user_store.list_resolved_for_user(env.alice, 2, cursor))
        assert [r["content"] for r in third] == ["fact 0"]
        # Another user sees none of it.
        assert _run(env.user_store.list_resolved_for_user(env.bob, 10)) == []

    def test_resolved_cursor_ties_on_resolved_at(self, env):
        same = _naive_utc(minutes=5)
        rows = [_user_suggestion(env, f"tie {i}") for i in range(3)]
        for row in rows:
            _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(env, env.models.MemorySuggestion, row["id"], resolved_at=same)
        seen = []
        cursor = None
        while True:
            page = _run(env.user_store.list_resolved_for_user(env.alice, 1, cursor))
            if not page:
                break
            seen.extend(r["id"] for r in page)
            cursor = env.user_store.decode_resolved_cursor(
                env.user_store.encode_resolved_cursor(page[-1]),
            )
        assert sorted(seen) == sorted(r["id"] for r in rows)

    def test_cursor_round_trip_and_junk(self, env):
        store = env.user_store
        assert store.decode_resolved_cursor(None) is None
        assert store.decode_resolved_cursor("") is None
        decoded = store.decode_resolved_cursor("2026-10-15T12:00:00.000001|abc")
        assert decoded == (datetime(2026, 10, 15, 12, 0, 0, 1), "abc")
        with pytest.raises(ValueError):
            store.decode_resolved_cursor("no-separator")
        with pytest.raises(ValueError):
            store.decode_resolved_cursor("not-a-date|abc")

    def test_pending_summaries_per_user(self, env):
        old = _naive_utc(hours=3)
        a = _user_suggestion(env, "A")
        _backdate(env, env.models.MemorySuggestion, a["id"], created_at=old)
        _user_suggestion(env, "B")
        _user_suggestion(env, "Bob's", conversation_id=env.bob_chat_id, who=env.bob)
        resolved = _user_suggestion(env, "gone")
        _run(env.user_store.dismiss_suggestion(env.alice, resolved["id"]))
        _project_suggestion(env, "P")

        summaries = {s["user_id"]: s for s in _run(env.user_store.list_pending_summaries())}
        assert summaries[env.alice]["pending_count"] == 2
        assert abs((summaries[env.alice]["oldest_created_at"] - old).total_seconds()) < 1
        assert summaries[env.bob]["pending_count"] == 1
        project_summaries = _run(env.project_store.list_pending_summaries())
        assert [(s["user_id"], s["pending_count"]) for s in project_summaries] == [(env.alice, 1)]

    def test_conversation_context(self, env):
        context = _run(env.user_store.conversation_context(
            [env.chat_id, env.project_chat_id, "missing"],
        ))
        assert context[env.chat_id] == {
            "conversation_title": "Coffee chat", "project_id": None, "project_name": None,
        }
        assert context[env.project_chat_id] == {
            "conversation_title": None, "project_id": env.project_id, "project_name": "Launch",
        }
        assert "missing" not in context
        assert _run(env.user_store.conversation_context([])) == {}


# ---------------------------------------------------------------------------
# The merged inbox (chat/memory_inbox.py)
# ---------------------------------------------------------------------------


class TestInbox:
    def test_counts(self, env):
        assert _run(memory_inbox.count_pending(env.alice)) == {
            "user": 0, "project": 0, "pending_count": 0,
        }
        _user_suggestion(env, "A")
        _project_suggestion(env, "P")
        _project_suggestion(env, "Q")
        assert _run(memory_inbox.count_pending(env.alice)) == {
            "user": 1, "project": 2, "pending_count": 3,
        }

    def test_pending_merges_kinds_with_context(self, env):
        p = _project_suggestion(env, "P")
        a = _user_suggestion(env, "A")
        _backdate(env, env.models.MemorySuggestion, a["id"], created_at=_naive_utc(hours=1))
        _user_suggestion(env, "Bob's", conversation_id=env.bob_chat_id, who=env.bob)

        rows = _run(memory_inbox.list_pending(env.alice))
        assert [(r["id"], r["kind"]) for r in rows] == [(a["id"], "user"), (p["id"], "project")]
        assert rows[0]["conversation_title"] == "Coffee chat"
        assert rows[0]["project_id"] is None and rows[0]["project_name"] is None
        assert rows[0]["replaces_content"] is None
        assert rows[1]["project_id"] == env.project_id
        assert rows[1]["project_name"] == "Launch"
        assert rows[1]["conversation_title"] is None

    def test_resolved_pages_across_both_tables(self, env):
        # Interleave the two kinds by resolved_at so the merge order shows.
        expected = []
        for i in range(6):
            if i % 2:
                row = _project_suggestion(env, f"P{i}")
                _run(env.project_store.accept_suggestion(env.alice, row["id"]))
                model = env.models.ProjectMemorySuggestion
            else:
                row = _user_suggestion(env, f"U{i}")
                _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
                model = env.models.MemorySuggestion
            _backdate(env, model, row["id"], resolved_at=_naive_utc(minutes=60 - i))
            expected.append(row["id"])
        expected.reverse()  # newest first
        _user_suggestion(env, "pending")

        page, cursor = _run(memory_inbox.list_resolved(env.alice, 4))
        assert [r["id"] for r in page] == expected[:4]
        assert [r["kind"] for r in page] == ["project", "user", "project", "user"]
        assert [r["status"] for r in page] == ["accepted", "dismissed", "accepted", "dismissed"]
        assert page[0]["project_name"] == "Launch"
        assert page[1]["conversation_title"] == "Coffee chat"
        assert cursor is not None

        page2, cursor2 = _run(memory_inbox.list_resolved(env.alice, 4, cursor))
        assert [r["id"] for r in page2] == expected[4:]
        # A short page from both tables ends the stream.
        assert cursor2 is None

    def test_resolved_full_single_table_page_keeps_cursor(self, env):
        for i in range(3):
            row = _user_suggestion(env, f"U{i}")
            _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(
                env, env.models.MemorySuggestion, row["id"],
                resolved_at=_naive_utc(minutes=10 - i),
            )
        page, cursor = _run(memory_inbox.list_resolved(env.alice, 3))
        assert len(page) == 3
        # Exactly one table-sized page: the next page may be empty, but the
        # stream must not stop early.
        assert cursor is not None
        page2, cursor2 = _run(memory_inbox.list_resolved(env.alice, 3, cursor))
        assert page2 == [] and cursor2 is None

    def test_resolved_rejects_junk_cursor(self, env):
        with pytest.raises(ValueError):
            _run(memory_inbox.list_resolved(env.alice, 5, "junk"))

    def test_pending_summaries_merge_kinds(self, env):
        old = _naive_utc(hours=2)
        p = _project_suggestion(env, "P")
        _backdate(env, env.models.ProjectMemorySuggestion, p["id"], created_at=old)
        _user_suggestion(env, "A")
        _user_suggestion(env, "Bob's", conversation_id=env.bob_chat_id, who=env.bob)

        summaries = {s["user_id"]: s for s in _run(memory_inbox.list_pending_summaries())}
        assert summaries[env.alice]["pending_count"] == 2
        assert abs((summaries[env.alice]["oldest_created_at"] - old).total_seconds()) < 1
        assert summaries[env.bob]["pending_count"] == 1

    def test_events_carry_the_user_total(self, env):
        from chat.memory_suggestions import register_suggestion
        from chat.project_memories import register_suggestion as register_project
        _run(register_suggestion(env.alice, env.chat_id, "A"))
        _run(register_suggestion(env.alice, env.project_chat_id, "B"))
        _run(register_project(env.alice, env.project_id, env.project_chat_id, "P"))
        totals = [
            (ev["type"], ev["pending_count"], ev["total_pending"])
            for _uid, ev in env.published
        ]
        assert totals == [
            ("memory_suggestions_changed", 1, 1),
            ("memory_suggestions_changed", 1, 2),
            ("project_memory_suggestions_changed", 1, 1),
        ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _client(env, who="alice"):
    from chat import memory_routes

    app = FastAPI()
    app.include_router(memory_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[memory_routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


class TestRoutes:
    def test_counts_pending_and_resolved(self, env):
        a = _user_suggestion(env, "A")
        _project_suggestion(env, "P")
        done = _user_suggestion(env, "done")
        _run(env.user_store.accept_suggestion(env.alice, done["id"]))
        client = _client(env)

        resp = client.get("/app/api/memory-inbox/counts")
        assert resp.status_code == 200
        assert resp.json() == {"user": 1, "project": 1, "pending_count": 2}

        resp = client.get("/app/api/memory-inbox/pending")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pending_count"] == 2
        assert body["suggestions"][0]["id"] == a["id"]
        assert body["suggestions"][0]["kind"] == "user"
        assert body["suggestions"][0]["conversation_title"] == "Coffee chat"
        assert body["suggestions"][1]["kind"] == "project"
        assert body["suggestions"][1]["project_name"] == "Launch"

        resp = client.get("/app/api/memory-inbox/resolved", params={"limit": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert [s["id"] for s in body["suggestions"]] == [done["id"]]
        assert body["suggestions"][0]["status"] == "accepted"
        assert body["next_cursor"] is None

        # Bob's inbox is empty.
        assert _client(env, "bob").get("/app/api/memory-inbox/counts").json()["pending_count"] == 0
        assert _client(env, "bob").get("/app/api/memory-inbox/pending").json()["suggestions"] == []

    def test_resolved_paging_and_bad_input(self, env):
        for i in range(3):
            row = _user_suggestion(env, f"U{i}")
            _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(
                env, env.models.MemorySuggestion, row["id"],
                resolved_at=_naive_utc(minutes=10 - i),
            )
        client = _client(env)
        first = client.get("/app/api/memory-inbox/resolved", params={"limit": 2}).json()
        assert [s["content"] for s in first["suggestions"]] == ["U2", "U1"]
        second = client.get(
            "/app/api/memory-inbox/resolved",
            params={"limit": 2, "cursor": first["next_cursor"]},
        ).json()
        assert [s["content"] for s in second["suggestions"]] == ["U0"]
        assert second["next_cursor"] is None

        resp = client.get("/app/api/memory-inbox/resolved", params={"cursor": "junk"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "invalid_cursor"
        assert client.get("/app/api/memory-inbox/resolved", params={"limit": 0}).status_code == 422


# ---------------------------------------------------------------------------
# Slack reminder: merged counts and wording
# ---------------------------------------------------------------------------


def _naive(age_minutes):
    return _naive_utc(minutes=age_minutes)


class TestReminder:
    def test_collect_pending_merges_requests_and_suggestions(self):
        requests = [
            {"user_id": 1, "open_count": 2, "oldest_created_at": _naive(30)},
            {"user_id": 2, "open_count": 1, "oldest_created_at": _naive(90)},
        ]
        suggestions = [
            {"user_id": 1, "pending_count": 3, "oldest_created_at": _naive(120)},
            {"user_id": 3, "pending_count": 1, "oldest_created_at": _naive(5)},
        ]
        with patch(
            "db.action_request_store.list_open_request_summaries",
            AsyncMock(return_value=requests),
        ), patch(
            "chat.memory_inbox.list_pending_summaries",
            AsyncMock(return_value=suggestions),
        ):
            rows = {r["user_id"]: r for r in asyncio.run(slack_notifier._collect_pending())}
        assert rows[1]["open_count"] == 2 and rows[1]["suggestion_count"] == 3
        assert rows[1]["oldest_created_at"] == suggestions[0]["oldest_created_at"]
        assert rows[2] == {
            "user_id": 2, "open_count": 1, "suggestion_count": 0,
            "oldest_created_at": requests[1]["oldest_created_at"],
        }
        assert rows[3] == {
            "user_id": 3, "open_count": 0, "suggestion_count": 1,
            "oldest_created_at": suggestions[1]["oldest_created_at"],
        }

    def test_wording_covers_each_combination(self):
        assert slack_notifier._describe_pending(1, 0) == "1 unanswered request"
        assert slack_notifier._describe_pending(0, 1) == "1 suggested memory"
        assert slack_notifier._describe_pending(0, 4) == "4 suggested memories"
        assert slack_notifier._describe_pending(2, 3) == (
            "2 unanswered requests and 3 suggested memories"
        )

    def test_memories_only_reminder_links_to_the_memories_tab(self):
        with patch(
            "config.server_config.get_app_base_url", lambda: "https://quest.example.com",
        ):
            fallback, blocks = slack_notifier._build_reminder(0, 2)
            _, mixed_blocks = slack_notifier._build_reminder(1, 2)
        assert fallback == "You have 2 suggested memories in Quest waiting for your review."
        assert "<https://quest.example.com/inbox/memories|Open your inbox>" in blocks[0]["text"]["text"]
        assert "1 unanswered request and 2 suggested memories" in mixed_blocks[0]["text"]["text"]
        assert "<https://quest.example.com/inbox|Open your inbox>" in mixed_blocks[0]["text"]["text"]
