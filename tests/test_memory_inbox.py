"""The inbox's Memories tab (chat/memory_inbox.py) and what feeds it.

Covers the per-user store queries of the personal and project suggestion
kinds (pending across conversations, keyset-paged resolved rows, the
cross-user pending summaries, conversation context), the inbox listing
merged across the three tables (personal, project and doc notes: the doc
row shape, hidden docs, the three-way resolved merge with its keyset
cursor, doc rows staying listed while the doc-notes gate is closed), the
``total_pending`` event field, the ``/app/api/memory-inbox`` routes, and
the Slack reminder's merged counts and wording.
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
    standalone chat; every suggestion store (and the doc stores the doc
    review rows read) points at it, events captured."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.conversation_store as conversation_store
    import db.doc_memory_store as doc_memory_store
    import db.doc_note_suggestion_store as doc_note_store
    import db.doc_store as doc_store
    import db.memory_store as memory_store
    import db.memory_suggestion_store as user_store
    import db.models as models
    import db.project_memory_store as project_memory_store
    import db.project_memory_suggestion_store as project_store
    import db.project_store as projects

    models.Base.metadata.create_all(sync_engine)
    for mod in (
        conversation_store, memory_store, user_store, project_memory_store,
        project_store, projects, doc_store, doc_memory_store, doc_note_store,
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
    e.doc_store = doc_store
    e.doc_memory_store = doc_memory_store
    e.doc_note_store = doc_note_store
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


def _doc(env, title, owner=None) -> str:
    """A private user doc (a row only; the inbox never reads doc files)."""
    return _run(env.doc_store.create_doc(owner or env.alice, title, mode="private"))["id"]


def _doc_memory(env, doc_id, content, who=None):
    who = who or env.alice
    return _run(env.doc_memory_store.create_doc_memory(
        doc_id, content, source=env.doc_memory_store.ui_source(who), user_id=who,
    ))


def _doc_suggestion(env, doc_id, note_kind, content, conversation_id=None, who=None, **kw):
    """A pending doc note suggestion (an instructions one needs
    ``base_instructions_version``)."""
    row, _superseded = _run(env.doc_note_store.create_suggestion(
        who or env.alice, doc_id, conversation_id or env.chat_id, note_kind, content, **kw,
    ))
    return row


# Every key of a doc inbox row: the store row with ``kind`` renamed
# ``note_kind``, the review fields, the inbox ``kind`` and the context.
DOC_ROW_KEYS = {
    "id", "user_id", "doc_id", "conversation_id", "note_kind", "content",
    "reasoning", "base_instructions_version", "replaces_memory_id", "status",
    "memory_id", "created_at", "resolved_at",
    "doc_title", "replaces_content", "current_instructions", "instructions_moved",
    "kind", "conversation_title", "project_id", "project_name",
}


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
            "user": 0, "project": 0, "doc": 0, "pending_count": 0,
        }
        _user_suggestion(env, "A")
        _project_suggestion(env, "P")
        _project_suggestion(env, "Q")
        assert _run(memory_inbox.count_pending(env.alice)) == {
            "user": 1, "project": 2, "doc": 0, "pending_count": 3,
        }

    def test_counts_all_three_kinds(self, env):
        doc_id = _doc(env, "Runbook")
        _user_suggestion(env, "A")
        _project_suggestion(env, "P")
        _doc_suggestion(env, doc_id, "memory", "Deploys on Fridays")
        _doc_suggestion(
            env, doc_id, "instructions", "Be brief.", conversation_id=env.project_chat_id,
            base_instructions_version=0,
        )
        done = _doc_suggestion(env, doc_id, "memory", "Resolved")
        _run(env.doc_note_store.dismiss_suggestion(env.alice, done["id"]))
        # Bob's doc suggestion on alice's doc is his, not hers.
        _doc_suggestion(env, doc_id, "memory", "Bob's", conversation_id=env.bob_chat_id, who=env.bob)

        assert _run(memory_inbox.count_pending(env.alice)) == {
            "user": 1, "project": 1, "doc": 2, "pending_count": 4,
        }
        assert _run(memory_inbox.count_pending(env.bob)) == {
            "user": 0, "project": 0, "doc": 1, "pending_count": 1,
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

    def test_pending_merges_three_kinds_with_doc_rows(self, env):
        doc_id = _doc(env, "Runbook")
        old = _doc_memory(env, doc_id, "Deploys on Fridays")
        p = _project_suggestion(env, "P")
        a = _user_suggestion(env, "A")
        _backdate(env, env.models.MemorySuggestion, a["id"], created_at=_naive_utc(hours=1))
        di = _doc_suggestion(
            env, doc_id, "instructions", "Be brief.", conversation_id=env.project_chat_id,
            base_instructions_version=0,
        )
        _backdate(env, env.models.DocNoteSuggestion, di["id"], created_at=_naive_utc(hours=2))
        dm = _doc_suggestion(
            env, doc_id, "memory", "Deploys on Mondays", replaces_memory_id=old["id"],
        )
        _backdate(env, env.models.DocNoteSuggestion, dm["id"], created_at=_naive_utc(minutes=30))
        # The instructions moved past the version the proposal was written against.
        _run(env.doc_store.update_doc_instructions(
            doc_id, "Be thorough.", expected_version=0, user_id=env.alice,
        ))
        _doc_suggestion(env, doc_id, "memory", "Bob's", conversation_id=env.bob_chat_id, who=env.bob)

        rows = _run(memory_inbox.list_pending(env.alice))
        assert [(r["id"], r["kind"]) for r in rows] == [
            (di["id"], "doc"), (a["id"], "user"), (dm["id"], "doc"), (p["id"], "project"),
        ]
        doc_rows = [r for r in rows if r["kind"] == "doc"]
        assert all(set(r) == DOC_ROW_KEYS for r in doc_rows)

        instructions = rows[0]
        assert instructions["note_kind"] == "instructions"
        assert instructions["doc_id"] == doc_id
        assert instructions["doc_title"] == "Runbook"
        assert instructions["content"] == "Be brief."
        assert instructions["status"] == "pending"
        assert instructions["base_instructions_version"] == 0
        assert instructions["current_instructions"] == "Be thorough."
        assert instructions["instructions_moved"] is True
        assert instructions["replaces_content"] is None
        # A doc row carries no project of its own: the conversation's.
        assert instructions["conversation_title"] is None
        assert instructions["project_id"] == env.project_id
        assert instructions["project_name"] == "Launch"

        memory = rows[2]
        assert memory["note_kind"] == "memory"
        assert memory["doc_title"] == "Runbook"
        assert memory["replaces_memory_id"] == old["id"]
        assert memory["replaces_content"] == "Deploys on Fridays"
        assert memory["current_instructions"] is None
        assert memory["instructions_moved"] is False
        assert memory["conversation_title"] == "Coffee chat"
        assert memory["project_id"] is None
        assert memory["project_name"] is None

    def test_hidden_doc_rows_stay_listed_with_a_blank_title(self, env):
        # Bob's doc, shared with alice for writing: her conversation
        # suggests replacing one of its memories.
        shared = _doc(env, "Bob's plan", owner=env.bob)
        fact = _doc_memory(env, shared, "Ship in May", who=env.bob)
        share = _run(env.doc_store.add_share(shared, env.alice, "write"))
        dm = _doc_suggestion(env, shared, "memory", "Ship in June", replaces_memory_id=fact["id"])
        # Another of bob's docs alice never could see, with new instructions.
        private = _doc(env, "Bob's diary", owner=env.bob)
        di = _doc_suggestion(env, private, "instructions", "Be kind.", base_instructions_version=0)
        _backdate(env, env.models.DocNoteSuggestion, di["id"], created_at=_naive_utc(hours=1))
        _run(env.doc_store.update_doc_instructions(
            private, "Secret text", expected_version=0, user_id=env.bob,
        ))

        rows = {r["id"]: r for r in _run(memory_inbox.list_pending(env.alice))}
        assert rows[dm["id"]]["doc_title"] == "Bob's plan"
        assert rows[dm["id"]]["replaces_content"] == "Ship in May"
        hidden = rows[di["id"]]
        assert hidden["doc_title"] == ""
        assert hidden["current_instructions"] is None
        assert hidden["instructions_moved"] is False
        assert hidden["content"] == "Be kind."  # her own conversation wrote it

        # Share revoked: the row stays listed and counted, the doc blanked.
        assert _run(env.doc_store.remove_share(shared, share["id"]))
        rows = _run(memory_inbox.list_pending(env.alice))
        assert [r["id"] for r in rows] == [di["id"], dm["id"]]
        revoked = rows[1]
        assert revoked["doc_title"] == ""
        assert revoked["replaces_content"] is None
        assert revoked["content"] == "Ship in June"
        assert set(revoked) == DOC_ROW_KEYS
        assert _run(memory_inbox.count_pending(env.alice)) == {
            "user": 0, "project": 0, "doc": 2, "pending_count": 2,
        }

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

    def test_resolved_three_way_merge_and_keyset_paging(self, env):
        doc_id = _doc(env, "Runbook")
        base = _naive_utc(hours=2)
        made = []  # (resolved_at, id)

        def resolve(kind, label, resolved_at):
            if kind == "user":
                row = _user_suggestion(env, label)
                _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
                model = env.models.MemorySuggestion
            elif kind == "project":
                row = _project_suggestion(env, label)
                _run(env.project_store.accept_suggestion(env.alice, row["id"]))
                model = env.models.ProjectMemorySuggestion
            else:
                row = _doc_suggestion(env, doc_id, "memory", label)
                _run(env.doc_note_store.accept_suggestion(env.alice, row["id"]))
                model = env.models.DocNoteSuggestion
            _backdate(env, model, row["id"], resolved_at=resolved_at)
            made.append((resolved_at, row["id"]))
            return row

        kinds = ("user", "project", "doc")
        for i in range(9):
            resolve(kinds[i % 3], f"{kinds[i % 3]} {i}", base + timedelta(minutes=i))
        # One row per table resolved at the same instant, mid-stream: the
        # merge and the cursor break the tie on id across tables.
        tie = base + timedelta(minutes=4, seconds=30)
        for kind in kinds:
            resolve(kind, f"{kind} tie", tie)
        # A dismissed instructions row, then the instructions move: a
        # resolved row never reads as moved.
        di = _doc_suggestion(env, doc_id, "instructions", "Be brief.", base_instructions_version=0)
        _run(env.doc_note_store.dismiss_suggestion(env.alice, di["id"]))
        _backdate(env, env.models.DocNoteSuggestion, di["id"], resolved_at=base + timedelta(minutes=20))
        made.append((base + timedelta(minutes=20), di["id"]))
        _run(env.doc_store.update_doc_instructions(
            doc_id, "Be thorough.", expected_version=0, user_id=env.alice,
        ))
        # Neither pending rows nor another user's resolved rows show.
        _doc_suggestion(env, doc_id, "memory", "still pending")
        _user_suggestion(env, "pending")
        bobs = _doc_suggestion(env, doc_id, "memory", "Bob's", conversation_id=env.bob_chat_id, who=env.bob)
        _run(env.doc_note_store.dismiss_suggestion(env.bob, bobs["id"]))

        expected = [row_id for _, row_id in sorted(made, reverse=True)]
        assert len(expected) == 13

        first, cursor = _run(memory_inbox.list_resolved(env.alice, 4))
        assert [r["id"] for r in first] == expected[:4]
        assert [r["kind"] for r in first] == ["doc", "doc", "project", "user"]
        moved = first[0]
        assert moved["id"] == di["id"]
        assert set(moved) == DOC_ROW_KEYS
        assert moved["note_kind"] == "instructions"
        assert moved["status"] == "dismissed"
        assert moved["doc_title"] == "Runbook"
        assert moved["current_instructions"] is None
        assert moved["instructions_moved"] is False
        assert moved["conversation_title"] == "Coffee chat"
        accepted = first[1]
        assert accepted["note_kind"] == "memory"
        assert accepted["status"] == "accepted"
        assert accepted["memory_id"] is not None
        assert accepted["replaces_content"] is None
        assert cursor is not None

        # Every page size walks the whole stream: nothing skipped or repeated.
        for limit in (1, 2, 3, 4, 5, 7, 13, 20):
            seen, cursor = [], None
            while True:
                page, cursor = _run(memory_inbox.list_resolved(env.alice, limit, cursor))
                assert len(page) <= limit
                seen.extend(r["id"] for r in page)
                if cursor is None:
                    break
            assert seen == expected, limit

        # The tie group sits in id order, whatever table each row came from.
        tied = [row_id for resolved_at, row_id in made if resolved_at == tie]
        start = expected.index(max(tied))
        assert expected[start:start + 3] == sorted(tied, reverse=True)

    def test_resolved_full_doc_page_keeps_cursor(self, env):
        doc_id = _doc(env, "Runbook")
        for i in range(3):
            row = _doc_suggestion(env, doc_id, "memory", f"D{i}")
            _run(env.doc_note_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(
                env, env.models.DocNoteSuggestion, row["id"],
                resolved_at=_naive_utc(minutes=10 - i),
            )
        page, cursor = _run(memory_inbox.list_resolved(env.alice, 3))
        assert [r["content"] for r in page] == ["D2", "D1", "D0"]
        assert [r["kind"] for r in page] == ["doc", "doc", "doc"]
        # Only the doc table filled its page: the stream must not stop early.
        assert cursor is not None
        page2, cursor2 = _run(memory_inbox.list_resolved(env.alice, 3, cursor))
        assert page2 == [] and cursor2 is None

    def test_resolved_reviews_only_the_doc_rows_on_the_page(self, env, monkeypatch):
        doc_id = _doc(env, "Runbook")
        doc_ids = []
        for i in range(2):
            row = _doc_suggestion(env, doc_id, "memory", f"D{i}")
            _run(env.doc_note_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(
                env, env.models.DocNoteSuggestion, row["id"],
                resolved_at=_naive_utc(minutes=30 - i),
            )
            doc_ids.append(row["id"])
        for i in range(2):
            row = _user_suggestion(env, f"U{i}")
            _run(env.user_store.dismiss_suggestion(env.alice, row["id"]))
            _backdate(
                env, env.models.MemorySuggestion, row["id"],
                resolved_at=_naive_utc(minutes=10 - i),
            )
        reviewed = []
        real_review_rows = memory_inbox.review_rows

        async def spy(user_id, rows):
            rows = list(rows)
            reviewed.append([r["id"] for r in rows])
            return await real_review_rows(user_id, rows)

        monkeypatch.setattr(memory_inbox, "review_rows", spy)

        # Two user rows fill the page: the doc rows fetched for the merge
        # are dropped unreviewed.
        page, _ = _run(memory_inbox.list_resolved(env.alice, 2))
        assert [r["kind"] for r in page] == ["user", "user"]
        assert reviewed == []
        # Three: only the newest doc row made the page and is reviewed.
        page, _ = _run(memory_inbox.list_resolved(env.alice, 3))
        assert [r["kind"] for r in page] == ["user", "user", "doc"]
        assert reviewed == [[doc_ids[1]]]
        assert page[2]["doc_title"] == "Runbook"

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

    def test_pending_summaries_merge_three_kinds(self, env):
        doc_id = _doc(env, "Runbook")
        oldest = _naive_utc(hours=3)
        _user_suggestion(env, "A")
        p = _project_suggestion(env, "P")
        _backdate(env, env.models.ProjectMemorySuggestion, p["id"], created_at=_naive_utc(hours=1))
        d = _doc_suggestion(env, doc_id, "memory", "Fact")
        _backdate(env, env.models.DocNoteSuggestion, d["id"], created_at=oldest)
        resolved = _doc_suggestion(env, doc_id, "memory", "Resolved")
        _run(env.doc_note_store.dismiss_suggestion(env.alice, resolved["id"]))
        # Bob has nothing but a doc note suggestion.
        bobs = _doc_suggestion(env, doc_id, "memory", "Bob's", conversation_id=env.bob_chat_id, who=env.bob)

        summaries = {s["user_id"]: s for s in _run(memory_inbox.list_pending_summaries())}
        assert set(summaries) == {env.alice, env.bob}
        assert summaries[env.alice]["pending_count"] == 3
        assert abs((summaries[env.alice]["oldest_created_at"] - oldest).total_seconds()) < 1
        assert summaries[env.bob]["pending_count"] == 1
        assert summaries[env.bob]["oldest_created_at"] == datetime.fromisoformat(bobs["created_at"])

    def test_doc_rows_listed_and_counted_with_the_gate_closed(self, env, tmp_path, monkeypatch):
        import config.feature_gates as fg

        # No gates file: every gate, docs and doc_notes included, is closed.
        monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
        email = env.users["alice"]["email"]
        assert not fg.docs_enabled_for(email)
        assert not fg.doc_notes_enabled_for(email)

        doc_id = _doc(env, "Runbook")
        pending = _doc_suggestion(env, doc_id, "memory", "Fact")
        done = _doc_suggestion(env, doc_id, "memory", "Other")
        _run(env.doc_note_store.dismiss_suggestion(env.alice, done["id"]))

        assert _run(memory_inbox.count_pending(env.alice)) == {
            "user": 0, "project": 0, "doc": 1, "pending_count": 1,
        }
        rows = _run(memory_inbox.list_pending(env.alice))
        assert [(r["id"], r["kind"], r["doc_title"]) for r in rows] == [
            (pending["id"], "doc", "Runbook"),
        ]
        page, cursor = _run(memory_inbox.list_resolved(env.alice, 5))
        assert [(r["id"], r["kind"]) for r in page] == [(done["id"], "doc")]
        assert cursor is None
        # The Slack reminder counts exactly what the inbox shows.
        summaries = {s["user_id"]: s for s in _run(memory_inbox.list_pending_summaries())}
        assert summaries[env.alice]["pending_count"] == 1

        client = _client(env)
        assert client.get("/app/api/memory-inbox/counts").json() == {
            "user": 0, "project": 0, "doc": 1, "pending_count": 1,
        }
        assert [s["id"] for s in client.get("/app/api/memory-inbox/pending").json()["suggestions"]] == [
            pending["id"],
        ]

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
        assert resp.json() == {"user": 1, "project": 1, "doc": 0, "pending_count": 2}

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

    def test_doc_rows_in_counts_pending_and_resolved(self, env):
        doc_id = _doc(env, "Runbook")
        a = _user_suggestion(env, "A")
        _backdate(env, env.models.MemorySuggestion, a["id"], created_at=_naive_utc(hours=1))
        dm = _doc_suggestion(env, doc_id, "memory", "Deploys on Fridays")
        done = _doc_suggestion(
            env, doc_id, "instructions", "Be brief.", conversation_id=env.project_chat_id,
            base_instructions_version=0,
        )
        _run(env.doc_note_store.dismiss_suggestion(env.alice, done["id"]))
        client = _client(env)

        resp = client.get("/app/api/memory-inbox/counts")
        assert resp.status_code == 200
        assert resp.json() == {"user": 1, "project": 0, "doc": 1, "pending_count": 2}

        body = client.get("/app/api/memory-inbox/pending").json()
        assert body["pending_count"] == 2
        assert [(s["id"], s["kind"]) for s in body["suggestions"]] == [
            (a["id"], "user"), (dm["id"], "doc"),
        ]
        doc_row = body["suggestions"][1]
        assert set(doc_row) == DOC_ROW_KEYS
        assert doc_row["note_kind"] == "memory"
        assert doc_row["doc_id"] == doc_id
        assert doc_row["doc_title"] == "Runbook"
        assert doc_row["content"] == "Deploys on Fridays"
        assert doc_row["replaces_content"] is None
        assert doc_row["current_instructions"] is None
        assert doc_row["instructions_moved"] is False
        assert doc_row["conversation_title"] == "Coffee chat"

        body = client.get("/app/api/memory-inbox/resolved", params={"limit": 5}).json()
        assert [(s["id"], s["kind"], s["note_kind"], s["status"]) for s in body["suggestions"]] == [
            (done["id"], "doc", "instructions", "dismissed"),
        ]
        assert body["suggestions"][0]["project_name"] == "Launch"
        assert body["next_cursor"] is None

        assert _client(env, "bob").get("/app/api/memory-inbox/counts").json() == {
            "user": 0, "project": 0, "doc": 0, "pending_count": 0,
        }

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

    def test_doc_only_user_is_reminded_with_counts_merged_across_tables(self, env):
        doc_id = _doc(env, "Runbook")
        # alice: a fresh personal and project suggestion, and an old doc one
        # that makes her reminder due.
        _user_suggestion(env, "A")
        _project_suggestion(env, "P")
        d = _doc_suggestion(env, doc_id, "memory", "Fact")
        alice_oldest = _naive(slack_notifier.DEFAULT_INTERVAL_MINUTES + 60)
        _backdate(env, env.models.DocNoteSuggestion, d["id"], created_at=alice_oldest)
        # bob: nothing but a doc note suggestion.
        b = _doc_suggestion(env, doc_id, "memory", "Bob's", conversation_id=env.bob_chat_id, who=env.bob)
        bob_oldest = _naive(slack_notifier.DEFAULT_INTERVAL_MINUTES + 5)
        _backdate(env, env.models.DocNoteSuggestion, b["id"], created_at=bob_oldest)

        with patch(
            "db.action_request_store.list_open_request_summaries", AsyncMock(return_value=[]),
        ):
            rows = {r["user_id"]: r for r in _run(slack_notifier._collect_pending())}
        assert set(rows) == {env.alice, env.bob}
        assert rows[env.alice]["open_count"] == 0
        assert rows[env.alice]["suggestion_count"] == 3
        assert abs((rows[env.alice]["oldest_created_at"] - alice_oldest).total_seconds()) < 1
        assert rows[env.bob]["suggestion_count"] == 1
        assert abs((rows[env.bob]["oldest_created_at"] - bob_oldest).total_seconds()) < 1

        slack_ids = {env.alice: "UALICE", env.bob: "UBOB"}

        async def get_user(user_id):
            return {
                "id": user_id,
                "service_credentials": {"slack": {"oauth_blob": {
                    "access_token": "xoxp-x", "user_id": slack_ids[user_id],
                }}},
            }

        send = AsyncMock()
        slack_notifier._last_notified.clear()
        try:
            with patch(
                "db.action_request_store.list_open_request_summaries",
                AsyncMock(return_value=[]),
            ), patch("db.user_store.get_user_by_id", AsyncMock(side_effect=get_user)), \
                    patch("auth.config.load_slack_bot_token", lambda: "xoxb-test"), \
                    patch.object(slack_notifier, "_send_reminder", send):
                _run(slack_notifier._poll_once())
        finally:
            slack_notifier._last_notified.clear()
        assert sorted(call.args for call in send.await_args_list) == sorted([
            ("xoxb-test", "UALICE", 0, 3),
            ("xoxb-test", "UBOB", 0, 1),
        ])

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
