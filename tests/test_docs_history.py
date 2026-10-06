"""Quest Docs history (chat/docs/history.py + chat/docs/history_routes.py).

Runs the base docs router plus the history router in a bare FastAPI app with
the auth dependency overridden, on the isolated ``docs_env`` from
tests/test_docs_service.py (tmp SQLite, tmp DOCS_DIR/CHATS_DIR, docs gate
open, realtime events captured), with db/user_store.py pointed at the same
database. Users: alice (owns the docs and the three projects; named "Alice
Owner"), bob ("Bob Builder"), carol (no name). Shares are created with
``doc_store.add_share`` directly.

Covers:

1. Listing: current + revisions newest first, ``replaced_at`` from the id,
   ``written_at``/``size``/``source`` from the sidecars, every
   ``source_label`` rule for the owner and for a non-owner (who never gets
   raw sources, conversation ids/titles or action request ids), one batched
   user lookup, deduped conversation lookups, missing/malformed/mismatched
   sidecars and a missing ``doc.meta.json``.
2. Reading one revision, ``?diff=current``, bad ``diff`` values, malformed /
   missing / pruned / symlinked revision ids (one ``revision_not_found``).
3. Access: gate, strangers (hidden == missing), read shares (no restore),
   public-project docs behind the ``public_projects`` gate.
4. Restore: snapshot + write with ``ui:<user_id>``, identical body = no
   write, stale token (flat 409), a write landing while the restore waits
   for the per-doc lock (TOCTOU), token validation, a write-share
   recipient's restore attributed by name in the owner's history.
5. Copy: private user doc of the caller (also from project / public-project
   docs and for share recipients), default ``(copy N)`` titles, explicit
   titles, referenced assets copied under the same names (symlinks, missing,
   unreferenced and non-image files skipped), cleanup on failure, the
   ``doc_list_changed`` event.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.docs import files
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    GIF,
    PNG,
    _run,
    body,
    docs_env,
    event_types,
    seed_doc,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 48
NOT_FOUND = {"error": "revision_not_found", "message": "Revision not found."}


# ---------------------------------------------------------------------------
# Fixture + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(docs_env, monkeypatch):
    """``docs_env`` plus db/user_store.py and db/action_request_store.py
    (``routes._ui_row`` resolves approved cards' proposers) on the same DB,
    and user names."""
    import db.action_request_store as action_request_store
    import db.user_store as user_store

    for mod in (user_store, action_request_store):
        monkeypatch.setattr(
            mod, "AsyncSessionLocal", docs_env.doc_store.AsyncSessionLocal,
        )
    set_name(docs_env, "alice", "Alice Owner")
    set_name(docs_env, "bob", "Bob Builder")
    return docs_env


def set_name(env, who, name):
    from db.models import User

    async def _set():
        async with env.doc_store.AsyncSessionLocal() as db:
            row = await db.get(User, env.users[who]["id"])
            row.name = name
            await db.commit()

    _run(_set())


def client(env, who="alice", **kwargs):
    from chat.docs import history_routes, routes

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(history_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app, **kwargs)


def uid(env, who):
    return env.users[who]["id"]


def detail(resp):
    return resp.json()["detail"]


def rev_ids(doc_id):
    """Revision ids, oldest first."""
    return [p.name[: -len(".md")] for p in files.list_revisions(doc_id)]


def rev_path(doc_id, rev_id):
    return files.doc_paths(doc_id).revisions / f"{rev_id}.md"


def meta_of(path):
    return json.loads(path.read_text())


def iso_mtime(path):
    return files._iso_z(datetime.fromtimestamp(os.lstat(path).st_mtime, timezone.utc))


def doc_dirs(env):
    return sorted(p.name for p in env.dirs["docs"].iterdir())


def make_conversation(env, title, *, project_id=None, owner="alice"):
    from db import conversation_store

    cid = str(uuid.uuid4())
    (env.dirs["chats"] / cid).mkdir()
    _run(conversation_store.create_conversation(
        uid(env, owner), cid, datetime.now(timezone.utc),
        project_id=project_id, custom_name=title,
    ))
    return cid


def write(env, doc_id, content, source):
    """A body write that also bumps the DB row (like the service does)."""
    size = files.write_body(doc_id, content, write_source=source)
    return _run(env.doc_store.update_after_write(
        doc_id, content_size=size, last_write_source=source,
    ))


def token(env, doc_id):
    return _run(env.doc_store.get_doc(doc_id))["updated_at"]


def restore(c, doc_id, rev_id, tok):
    return c.post(
        f"/app/api/docs/{doc_id}/revisions/{rev_id}/restore",
        json={"expected_updated_at": tok},
    )


def copy(c, doc_id, rev_id, payload=None):
    kwargs = {} if payload is None else {"json": payload}
    return c.post(f"/app/api/docs/{doc_id}/revisions/{rev_id}/copy", **kwargs)


def user_docs_of(env, who):
    docs = _run(env.doc_store.list_accessible_docs(uid(env, who)))
    return [d for d in docs if d["owner_id"] == uid(env, who)]


def make_card(env, who, doc_id="d"):
    """An approved write_doc action request row proposed by ``who``."""
    from db.models import ActionRequest

    async def _add():
        async with env.doc_store.AsyncSessionLocal() as db:
            row = ActionRequest(
                user_id=uid(env, who), conversation_id=str(uuid.uuid4()),
                request_type="write_doc", params={"doc_id": doc_id},
                reasoning="r", status="approved",
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row.id

    return _run(_add())


FORBIDDEN = {
    "error": "forbidden",
    "message": "History is available to people who can edit this doc.",
}


# ---------------------------------------------------------------------------
# Listing + labels
# ---------------------------------------------------------------------------


@pytest.fixture()
def labelled(env):
    """A doc whose snapshots cover every source kind.

    Bodies b0..b9; body i was written by ``sources[i]`` (b0 by seed_doc's
    init_doc, unknown writer). Snapshot i holds body i; the current body is
    b9.
    """
    cid = make_conversation(env, "Planning chat", project_id=env.private_project)
    deleted_user = uid(env, "carol") + 1000
    sources = [
        None,
        "ui",
        f"conversation:{cid}",
        "action_request:ar-secret-77",
        f"ui:{uid(env, 'bob')}",
        f"ui:{uid(env, 'alice')}",
        f"ui:{uid(env, 'carol')}",
        "conversation:gone-conversation-id",
        f"ui:{deleted_user}",
        "garbage-source",
    ]
    doc = seed_doc(env, "Labels", "b0\n", shares=[("bob", "write")])
    for i, source in enumerate(sources[1:], start=1):
        files.write_body(doc["id"], f"b{i}\n" * i, write_source=source)
    return {"doc": doc, "cid": cid, "sources": sources, "deleted_user": deleted_user}


OWNER_LABELS = [
    ("unknown", "unknown"),
    ("ui", "you"),
    ("conversation", "Planning chat"),
    ("action_request", "action request #ar-secret-77"),
    ("ui", "Bob Builder"),
    ("ui", "you"),
    ("ui", "carol@example.com"),
    ("conversation", "a deleted conversation"),
    ("ui", "a deleted user"),
    ("unknown", "unknown"),
]

RECIPIENT_LABELS = [
    ("unknown", "unknown"),
    ("ui", "the owner"),
    ("conversation", "a conversation"),
    ("action_request", "an approved change"),
    ("ui", "you"),
    ("ui", "Alice Owner"),
    ("ui", "carol@example.com"),
    ("conversation", "a conversation"),
    ("ui", "a deleted user"),
    ("unknown", "unknown"),
]


def _chronological(listing):
    """Versions oldest first: the revisions reversed, then current."""
    return [*reversed(listing["revisions"]), listing["current"]]


class TestList:
    def test_shape_order_and_metadata(self, env, labelled):
        doc_id = labelled["doc"]["id"]
        resp = client(env).get(f"/app/api/docs/{doc_id}/revisions")
        assert resp.status_code == 200
        listing = resp.json()
        ids = rev_ids(doc_id)
        assert len(ids) == 9
        assert [v["id"] for v in listing["revisions"]] == list(reversed(ids))

        for version in listing["revisions"]:
            path = rev_path(doc_id, version["id"])
            sidecar = meta_of(path.with_suffix(".json"))
            stamp = version["id"].split("-")[0]
            assert version["replaced_at"] == (
                f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:"
                f"{stamp[11:13]}:{stamp[13:15]}.000Z"
            )
            assert version["written_at"] == sidecar["written_at"]
            assert version["size"] == path.stat().st_size == sidecar["size"]
            assert set(version) == {
                "id", "written_at", "replaced_at", "size", "source",
                "source_kind", "source_label", "conversation",
            }

        current = listing["current"]
        meta = meta_of(files.doc_paths(doc_id).meta)
        assert current["id"] is None and current["replaced_at"] is None
        assert current["written_at"] == meta["written_at"]
        assert current["size"] == len("b9\n" * 9) == meta["size"]

    def test_owner_labels(self, env, labelled):
        doc_id = labelled["doc"]["id"]
        listing = client(env).get(f"/app/api/docs/{doc_id}/revisions").json()
        versions = _chronological(listing)
        assert [(v["source_kind"], v["source_label"]) for v in versions] == OWNER_LABELS
        assert [v["source"] for v in versions] == labelled["sources"]
        conversations = [v["conversation"] for v in versions]
        assert conversations[2] == {
            "id": labelled["cid"],
            "title": "Planning chat",
            "project_id": env.private_project,
        }
        assert conversations[:2] + conversations[3:] == [None] * 9

    def test_recipient_labels_leak_nothing(self, env, labelled):
        doc_id = labelled["doc"]["id"]
        resp = client(env, "bob").get(f"/app/api/docs/{doc_id}/revisions")
        assert resp.status_code == 200
        versions = _chronological(resp.json())
        assert [(v["source_kind"], v["source_label"]) for v in versions] == RECIPIENT_LABELS
        assert all(v["source"] is None and v["conversation"] is None for v in versions)
        text = resp.text
        for secret in (
            labelled["cid"], "Planning chat", "ar-secret-77",
            "gone-conversation-id", "garbage-source", str(labelled["deleted_user"]),
        ):
            assert secret not in text

    def test_user_and_conversation_lookups_batched(self, env, monkeypatch):
        from chat.docs import history

        cid = make_conversation(env, "Chat")
        other_cid = make_conversation(env, "Other chat")
        doc = seed_doc(env, "Batch", "v0\n")
        sources = [
            f"conversation:{cid}", f"ui:{uid(env, 'bob')}", f"conversation:{cid}",
            f"ui:{uid(env, 'carol')}", f"ui:{uid(env, 'bob')}",
            f"conversation:{other_cid}", "conversation:missing", f"conversation:{cid}",
        ]
        for i, source in enumerate(sources, start=1):
            files.write_body(doc["id"], f"v{i}\n", write_source=source)

        user_calls, batch_calls, single_calls = [], [], []
        real_users = history.user_store.get_users_by_ids
        real_batch = history.conversation_store.get_conversations_meta

        async def users_spy(ids):
            user_calls.append(set(ids))
            return await real_users(ids)

        async def batch_spy(user_id, ids):
            batch_calls.append((user_id, set(ids)))
            return await real_batch(user_id, ids)

        async def single_spy(*args):
            single_calls.append(args)
            raise AssertionError("per-id conversation lookup")

        monkeypatch.setattr(history.user_store, "get_users_by_ids", users_spy)
        monkeypatch.setattr(history.conversation_store, "get_conversations_meta", batch_spy)
        monkeypatch.setattr(history.conversation_store, "get_conversation_meta", single_spy)

        listing = client(env).get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert user_calls == [{uid(env, "bob"), uid(env, "carol")}]
        assert batch_calls == [(uid(env, "alice"), {cid, other_cid, "missing"})]
        assert single_calls == []
        labels = [v["source_label"] for v in _chronological(listing)]
        assert labels[1:] == [
            "Chat", "Bob Builder", "Chat", "carol@example.com", "Bob Builder",
            "Other chat", "a deleted conversation", "Chat",
        ]

        # A recipient never triggers conversation lookups; its own writes
        # need no user lookup.
        user_calls.clear()
        batch_calls.clear()
        _run(env.doc_store.add_share(doc["id"], uid(env, "bob"), "write"))
        client(env, "bob").get(f"/app/api/docs/{doc['id']}/revisions")
        assert user_calls == [{uid(env, "carol")}]
        assert batch_calls == [] and single_calls == []

    def test_approved_card_attribution(self, env, monkeypatch):
        """``action_request:<id>`` versions are attributed to the card's
        proposer, with one owners lookup and one users lookup per response."""
        from chat.docs import history

        doc = seed_doc(
            env, "Cards", "v0\n", shares=[("bob", "write"), ("carol", "write")],
        )
        alice_card = make_card(env, "alice", doc["id"])
        bob_card = make_card(env, "bob", doc["id"])
        carol_card = make_card(env, "carol", doc["id"])
        gone_card = carol_card + 1000  # no such row (e.g. deleted)
        cards = [alice_card, bob_card, carol_card, gone_card, bob_card]
        for i, card in enumerate(cards, start=1):
            files.write_body(doc["id"], f"v{i}\n", write_source=f"action_request:{card}")

        owner_calls, user_calls = [], []
        real_owners = history.action_request_store.get_action_request_owners
        real_users = history.user_store.get_users_by_ids

        async def owners_spy(ids):
            owner_calls.append(set(ids))
            return await real_owners(ids)

        async def users_spy(ids):
            user_calls.append(set(ids))
            return await real_users(ids)

        monkeypatch.setattr(
            history.action_request_store, "get_action_request_owners", owners_spy,
        )
        monkeypatch.setattr(history.user_store, "get_users_by_ids", users_spy)

        def labels(who):
            owner_calls.clear()
            user_calls.clear()
            resp = client(env, who).get(f"/app/api/docs/{doc['id']}/revisions")
            assert resp.status_code == 200
            versions = _chronological(resp.json())[1:]
            assert all(v["source_kind"] == "action_request" for v in versions)
            assert owner_calls == [{alice_card, bob_card, carol_card, gone_card}]
            return resp, [v["source_label"] for v in versions]

        _resp, owner_labels = labels("alice")
        assert owner_labels == [
            f"action request #{alice_card}",
            "Bob Builder (approved change)",
            "carol@example.com (approved change)",
            f"action request #{gone_card}",
            "Bob Builder (approved change)",
        ]
        assert user_calls == [{uid(env, "bob"), uid(env, "carol")}]

        resp, bob_labels = labels("bob")
        assert bob_labels == [
            "an approved change",
            "you (approved change)",
            "an approved change",
            "an approved change",
            "you (approved change)",
        ]
        assert user_calls == []  # a non-owner needs no proposer names
        assert "action request #" not in resp.text
        assert all(v["source"] is None for v in _chronological(resp.json()))

        _resp, carol_labels = labels("carol")
        assert carol_labels[2] == "you (approved change)"
        assert carol_labels.count("an approved change") == 4

        # A single revision read uses the same rules.
        bob_rev = rev_ids(doc["id"])[2]  # v2, written by bob's card
        got = client(env).get(f"/app/api/docs/{doc['id']}/revisions/{bob_rev}").json()
        assert got["source_label"] == "Bob Builder (approved change)"

    def test_card_proposer_gone_or_no_cards(self, env, monkeypatch):
        from chat.docs import history

        doc = seed_doc(env, "Cards", "v0\n", shares=[("bob", "write")])
        files.write_body(doc["id"], "v1\n", write_source="action_request:41")
        files.write_body(doc["id"], "v2\n", write_source="ui")
        deleted_user = uid(env, "carol") + 1000

        async def owners(ids):
            return {41: deleted_user}

        monkeypatch.setattr(history.action_request_store, "get_action_request_owners", owners)
        listing = client(env).get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert listing["revisions"][0]["source_label"] == "a deleted user (approved change)"

        # No card-written version: no owners lookup at all.
        calls = []

        async def never(ids):
            calls.append(ids)
            return {}

        monkeypatch.setattr(history.action_request_store, "get_action_request_owners", never)
        plain = seed_doc(env, "Plain", "v0\n")
        files.write_body(plain["id"], "v1\n", write_source="action_request:not-a-number")
        files.write_body(plain["id"], "v2\n", write_source="ui")
        listing = client(env).get(f"/app/api/docs/{plain['id']}/revisions").json()
        assert calls == []
        assert listing["revisions"][0]["source_label"] == "action request #not-a-number"

    def test_200_conversations_bounded_queries(self, env, monkeypatch):
        """200 routine runs each wrote one version: one conversations query
        and one title thread hop for the whole listing."""
        from sqlalchemy import event

        from chat.docs import history
        from db.models import Conversation

        doc = seed_doc(env, "Routine log", "v0\n")
        cids = [str(uuid.uuid4()) for _ in range(200)]

        async def _insert():
            now = datetime.now(timezone.utc)
            async with env.doc_store.AsyncSessionLocal() as db:
                db.add_all([
                    Conversation(
                        id=c, user_id=uid(env, "alice"), created_at=now,
                        last_message_at=now, custom_name=f"Run {i}",
                    )
                    for i, c in enumerate(cids)
                ])
                await db.commit()

        _run(_insert())
        for i, c in enumerate(cids, start=1):
            files.write_body(doc["id"], f"v{i}\n", write_source=f"conversation:{c}")
        files.write_body(doc["id"], "final\n", write_source="ui")
        assert len(rev_ids(doc["id"])) == 200  # DOC_REVISION_MAX_COUNT

        statements, title_hops = [], []
        engine = env.doc_store.AsyncSessionLocal.kw["bind"].sync_engine

        def _count(_conn, _cursor, statement, *_args):
            statements.append(statement)

        real_titles = history._resolve_titles

        def titles_spy(metas):
            title_hops.append(len(metas))
            return real_titles(metas)

        monkeypatch.setattr(history, "_resolve_titles", titles_spy)
        event.listen(engine, "before_cursor_execute", _count)
        try:
            resp = client(env).get(f"/app/api/docs/{doc['id']}/revisions")
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert resp.status_code == 200
        conversation_queries = [
            s for s in statements if "FROM conversations" in s
        ]
        assert len(conversation_queries) == 1
        assert len(statements) <= 5  # doc lookup + shares + conversations
        # 201 snapshots were taken; the count cap pruned v0's, so all 200
        # remaining ones name a distinct conversation.
        assert title_hops == [200]
        labels = [v["source_label"] for v in resp.json()["revisions"]]
        assert labels == [f"Run {i}" for i in reversed(range(200))]

    def test_get_conversations_meta(self, env):
        from sqlalchemy import event

        from db import conversation_store

        mine = make_conversation(env, "Mine", project_id=env.private_project)
        theirs = make_conversation(env, "Theirs", owner="bob")
        ids = [mine, theirs, "missing", mine, None, 7] + [
            str(uuid.uuid4()) for _ in range(600)
        ]
        statements = []
        engine = env.doc_store.AsyncSessionLocal.kw["bind"].sync_engine

        def _count(_conn, _cursor, statement, *_args):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            found = _run(conversation_store.get_conversations_meta(uid(env, "alice"), ids))
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert set(found) == {mine}
        single = _run(conversation_store.get_conversation_meta(uid(env, "alice"), mine))
        assert found[mine] == single
        assert found[mine]["custom_name"] == "Mine"
        assert found[mine]["project_id"] == env.private_project
        # 603 distinct string ids -> two chunks of <= 500.
        assert len([s for s in statements if "FROM conversations" in s]) == 2
        assert _run(conversation_store.get_conversations_meta(uid(env, "alice"), [])) == {}

    def test_no_revisions_and_no_user_lookup(self, env, monkeypatch):
        from chat.docs import history

        calls = []

        async def spy(ids):
            calls.append(ids)
            return {}

        monkeypatch.setattr(history.user_store, "get_users_by_ids", spy)
        doc = seed_doc(env, "Fresh", "only\n")
        listing = client(env).get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert listing["revisions"] == []
        assert listing["current"]["size"] == len("only\n")
        assert listing["current"]["source_kind"] == "unknown"
        assert calls == []

    def test_missing_malformed_and_mismatched_sidecars(self, env):
        doc = seed_doc(env, "Sidecars", "v0\n")
        for i in range(1, 5):
            files.write_body(doc["id"], f"v{i}\n" * (i + 1), write_source=f"ui:{uid(env, 'bob')}")
        first, second, third, fourth = rev_ids(doc["id"])
        rev_path(doc["id"], first).with_suffix(".json").unlink()
        rev_path(doc["id"], second).with_suffix(".json").write_text("{not json")
        mismatched = meta_of(rev_path(doc["id"], third).with_suffix(".json"))
        mismatched["size"] += 1
        rev_path(doc["id"], third).with_suffix(".json").write_text(json.dumps(mismatched))
        bad_time = meta_of(rev_path(doc["id"], fourth).with_suffix(".json"))
        bad_time["written_at"] = "yesterday-ish"
        rev_path(doc["id"], fourth).with_suffix(".json").write_text(json.dumps(bad_time))

        listing = client(env).get(f"/app/api/docs/{doc['id']}/revisions").json()
        by_id = {v["id"]: v for v in listing["revisions"]}
        for rev_id in (first, second, third, fourth):
            version = by_id[rev_id]
            path = rev_path(doc["id"], rev_id)
            assert version["source_kind"] == "unknown"
            assert version["source_label"] == "unknown"
            assert version["source"] is None
            assert version["written_at"] == iso_mtime(path)
            assert version["size"] == path.stat().st_size

    def test_current_without_doc_meta(self, env):
        doc = seed_doc(env, "Legacy", "old body\n")
        files.doc_paths(doc["id"]).meta.unlink()
        current = client(env).get(f"/app/api/docs/{doc['id']}/revisions").json()["current"]
        assert current["source"] is None and current["source_kind"] == "unknown"
        assert current["written_at"] == iso_mtime(files.doc_paths(doc["id"]).body)
        assert current["size"] == len("old body\n")

    def test_symlinked_revision_not_listed(self, env):
        doc = seed_doc(env, "Links", "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (only,) = rev_ids(doc["id"])
        path = rev_path(doc["id"], only)
        outside = env.tmp / "outside.md"
        outside.write_text("outside secret\n")
        path.unlink()
        os.symlink(outside, path)
        c = client(env)
        assert c.get(f"/app/api/docs/{doc['id']}/revisions").json()["revisions"] == []
        resp = c.get(f"/app/api/docs/{doc['id']}/revisions/{only}")
        assert resp.status_code == 404 and detail(resp) == NOT_FOUND
        assert "outside secret" not in resp.text

    def test_unservable_planted_names_not_listed(self, env):
        """files.list_revisions matches with ``\\d`` and ``$``, so it lists a
        name with non-ASCII digits or a trailing newline; History must not
        show an id its own read route would 404."""
        doc = seed_doc(env, "Planted", "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (real,) = rev_ids(doc["id"])
        revisions = files.doc_paths(doc["id"]).revisions
        planted = ["٢٠٢٦١٠٠٦T120000Z-1.md", "20991231T235959Z-1.md\n"]
        for name in planted:
            (revisions / name).write_text("planted\n")
        assert set(planted) <= {p.name for p in files.list_revisions(doc["id"])}

        c = client(env)
        listing = c.get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert [v["id"] for v in listing["revisions"]] == [real]
        assert "planted" not in json.dumps(listing)
        for version in listing["revisions"]:
            assert c.get(f"/app/api/docs/{doc['id']}/revisions/{version['id']}").status_code == 200

    def test_symlinked_revisions_dir_lists_nothing(self, env):
        doc = seed_doc(env, "Linked dir", "v0\n")
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        revisions = files.doc_paths(doc["id"]).revisions
        elsewhere = env.tmp / "elsewhere"
        revisions.rename(elsewhere)
        os.symlink(elsewhere, revisions)
        c = client(env)
        assert c.get(f"/app/api/docs/{doc['id']}/revisions").json()["revisions"] == []
        base = f"/app/api/docs/{doc['id']}/revisions/{rev}"
        for resp in (
            c.get(base),
            c.post(f"{base}/restore", json={"expected_updated_at": token(env, doc["id"])}),
            c.post(f"{base}/copy"),
        ):
            assert resp.status_code == 404
            assert detail(resp) == NOT_FOUND
        assert body(doc["id"]) == "v1\n"

    def test_fifo_revision_skipped_without_blocking(self, env):
        from chat.docs import history

        doc = seed_doc(env, "Fifo", "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        path = rev_path(doc["id"], rev)
        path.unlink()
        os.mkfifo(path)
        c = client(env)
        assert c.get(f"/app/api/docs/{doc['id']}/revisions").json()["revisions"] == []
        resp = c.get(f"/app/api/docs/{doc['id']}/revisions/{rev}")
        assert resp.status_code == 404 and detail(resp) == NOT_FOUND

        # The open itself never blocks on a FIFO (O_NONBLOCK) and refuses it.
        outcome = []

        def _probe():
            try:
                history._read_regular_with_stat(path)
                outcome.append("read")
            except history.RevisionNotFound:
                outcome.append("refused")

        probe = threading.Thread(target=_probe, daemon=True)
        probe.start()
        probe.join(5)
        assert outcome == ["refused"]

    def test_broken_body_is_invalid_doc_files(self, env):
        doc = seed_doc(env, "Broken", "x\n")
        write(env, doc["id"], "y\n", "ui")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        body_path = files.doc_paths(doc["id"]).body
        outside = env.tmp / "outside.md"
        outside.write_text("outside\n")
        c = client(env)
        base = f"/app/api/docs/{doc['id']}/revisions"
        for breakage in ("missing", "symlink"):
            body_path.unlink(missing_ok=True)
            if breakage == "symlink":
                os.symlink(outside, body_path)
            for resp in (
                c.get(base),
                c.get(f"{base}/{rev}?diff=current"),
                c.post(f"{base}/{rev}/restore", json={"expected_updated_at": tok}),
            ):
                assert resp.status_code == 400, (breakage, resp.text)
                assert detail(resp)["error"] == "invalid_doc_files"
            # Reading the revision alone does not need the current body.
            assert c.get(f"{base}/{rev}").status_code == 200
            # A missing revision still wins.
            resp = c.post(f"{base}/20000101T000000Z-1/restore", json={"expected_updated_at": tok})
            assert resp.status_code == 404
        assert outside.read_text() == "outside\n"
        assert token(env, doc["id"]) == tok


# ---------------------------------------------------------------------------
# One revision + diff
# ---------------------------------------------------------------------------


class TestRead:
    def test_read_matches_list_entry(self, env, labelled):
        doc_id = labelled["doc"]["id"]
        c = client(env)
        listing = c.get(f"/app/api/docs/{doc_id}/revisions").json()
        for i, version in enumerate(reversed(listing["revisions"])):
            resp = c.get(f"/app/api/docs/{doc_id}/revisions/{version['id']}")
            assert resp.status_code == 200
            got = resp.json()
            assert got.pop("content") == ("b0\n" if i == 0 else f"b{i}\n" * i)
            assert "diff" not in got
            assert got == version

    def test_recipient_read_has_no_source(self, env, labelled):
        doc_id = labelled["doc"]["id"]
        conv_rev = rev_ids(doc_id)[2]
        resp = client(env, "bob").get(f"/app/api/docs/{doc_id}/revisions/{conv_rev}")
        assert resp.status_code == 200
        got = resp.json()
        assert got["source"] is None and got["conversation"] is None
        assert got["source_label"] == "a conversation"
        assert labelled["cid"] not in resp.text

    def test_diff_against_current(self, env):
        from chat.action_request_types._skill_content_edit import (
            build_bounded_content_diff,
        )

        doc = seed_doc(env, "Diff", "alpha\nbeta\ngamma\n")
        files.write_body(doc["id"], "alpha\nBETA\ngamma\ndelta\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        resp = client(env).get(f"/app/api/docs/{doc['id']}/revisions/{rev}?diff=current")
        assert resp.status_code == 200
        got = resp.json()
        assert got["content"] == "alpha\nbeta\ngamma\n"
        assert got["diff"] == build_bounded_content_diff(
            "alpha\nbeta\ngamma\n", "alpha\nBETA\ngamma\ndelta\n",
        )
        assert got["diff"]["added"] == 2 and got["diff"]["removed"] == 1

    @pytest.mark.parametrize("value", ["previous", "", "CURRENT"])
    def test_bad_diff_value(self, env, value):
        doc = seed_doc(env, "Diff", "a\n")
        files.write_body(doc["id"], "b\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        resp = client(env).get(f"/app/api/docs/{doc['id']}/revisions/{rev}?diff={value}")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    @pytest.mark.parametrize("rev_id", [
        "nope",
        "20261006T120000Z",
        "20261006T120000Z-1.md",
        "20261006T120000Z-1.json",
        "20261006t120000z-1",
        "20000101T000000Z-1",  # well-formed, never existed
        "٢٠٢٦١٠٠٦T120000Z-1",  # non-ASCII digits
    ])
    def test_malformed_and_missing_ids_one_body(self, env, rev_id):
        doc = seed_doc(env, "Ids", "a\n")
        files.write_body(doc["id"], "b\n", write_source="ui")
        c = client(env)
        base = f"/app/api/docs/{doc['id']}/revisions/{rev_id}"
        for resp in (
            c.get(base),
            c.get(f"{base}?diff=current"),
            c.post(f"{base}/restore", json={"expected_updated_at": token(env, doc["id"])}),
            c.post(f"{base}/copy"),
        ):
            assert resp.status_code == 404, resp.text
            assert detail(resp) == NOT_FOUND
        assert body(doc["id"]) == "b\n"
        assert len(doc_dirs(env)) == 1

    @pytest.mark.parametrize("rev_id", [
        "../doc", "../../docs", "20261006T120000Z-1/../../x", "/etc/passwd",
        "20261006T120000Z-1\n", None, 5,
    ])
    def test_traversal_ids_refused_before_any_path(self, env, rev_id, monkeypatch):
        from chat.docs import history
        from chat.docs.routes import _ui_access

        doc = seed_doc(env, "Ids", "a\n")
        alice = env.users["alice"]
        access = _ui_access(alice, doc)
        tok = token(env, doc["id"])

        def _no_path(*_args, **_kwargs):
            raise AssertionError("a path was built for a malformed revision id")

        monkeypatch.setattr(history.doc_files, "list_revisions", _no_path)
        monkeypatch.setattr(history.doc_files, "doc_paths", _no_path)

        with pytest.raises(history.RevisionNotFound) as info:
            history._revision_path(doc["id"], rev_id)
        assert info.value.code == "revision_not_found"
        assert str(info.value) == NOT_FOUND["message"]
        for call in (
            history.read_version(alice, doc, access, rev_id),
            history.read_version(alice, doc, access, rev_id, diff_current=True),
            history.restore_revision(alice, doc, access, rev_id, expected_updated_at=tok),
            history.copy_revision(alice, doc, access, rev_id),
        ):
            with pytest.raises(history.RevisionNotFound):
                _run(call)

    def test_pruned_revision_404_everywhere(self, env):
        doc = seed_doc(env, "Pruned", "v0\n")
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        path = rev_path(doc["id"], rev)
        path.unlink()
        path.with_suffix(".json").unlink()
        c = client(env)
        base = f"/app/api/docs/{doc['id']}/revisions/{rev}"
        for resp in (
            c.get(base),
            c.post(f"{base}/restore", json={"expected_updated_at": token(env, doc["id"])}),
            c.post(f"{base}/copy"),
        ):
            assert resp.status_code == 404
            assert detail(resp) == NOT_FOUND
        assert body(doc["id"]) == "v1\n"


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _all_routes(c, doc_id, rev_id, tok):
    base = f"/app/api/docs/{doc_id}/revisions"
    return [
        c.get(base),
        c.get(f"{base}/{rev_id}"),
        c.get(f"{base}/{rev_id}?diff=current"),
        c.post(f"{base}/{rev_id}/restore", json={"expected_updated_at": tok}),
        c.post(f"{base}/{rev_id}/copy"),
    ]


class TestAccess:
    def test_gate_closed(self, env):
        doc = seed_doc(env, "Gated", "a\n")
        files.write_body(doc["id"], "b\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        env.fg.set_feature_enabled(env.fg.FEATURE_DOCS, False)
        for resp in _all_routes(client(env), doc["id"], rev, tok):
            assert resp.status_code == 403
            assert detail(resp) == {
                "error": "docs_disabled", "message": docs_disabled_message(),
            }
        assert body(doc["id"]) == "b\n"
        assert len(doc_dirs(env)) == 1

    def test_stranger_hidden_equals_missing(self, env):
        doc = seed_doc(env, "Secret", "classified\n")
        files.write_body(doc["id"], "classified v2\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        expected = {"error": "doc_not_found", "message": doc_not_found_message(doc["id"])}
        hidden = _all_routes(client(env, "carol"), doc["id"], rev, tok)
        for resp in hidden:
            assert resp.status_code == 404
            assert detail(resp) == expected
            assert "classified" not in resp.text
        # The same requests once the doc really does not exist.
        _run(env.doc_store.delete_doc(doc["id"]))
        missing = _all_routes(client(env, "carol"), doc["id"], rev, tok)
        assert [r.content for r in missing] == [r.content for r in hidden]

    @pytest.mark.parametrize("who, shares", [
        ("bob", [("bob", "read")]),
        ("carol", [(None, "read")]),
        # A user row never downgrades an everyone grant, nor vice versa:
        # read + read is still read-only.
        ("bob", [("bob", "read"), (None, "read")]),
    ])
    def test_read_only_viewers_forbidden_everywhere(self, env, who, shares):
        """Earlier versions can hold text removed before sharing: a read
        recipient gets 403 on every history route, before any other check."""
        doc = seed_doc(env, "Shared", "secret draft\n", shares=shares)
        write(env, doc["id"], "public part\n", "ui")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        dirs = doc_dirs(env)
        c = client(env, who)
        responses = _all_routes(c, doc["id"], rev, tok) + [
            # 403 wins over a bad diff value, a malformed / missing id, a
            # missing token and an invalid title.
            c.get(f"/app/api/docs/{doc['id']}/revisions/{rev}?diff=bogus"),
            c.get(f"/app/api/docs/{doc['id']}/revisions/nope"),
            c.post(f"/app/api/docs/{doc['id']}/revisions/20000101T000000Z-1/restore"),
            copy(c, doc["id"], "20000101T000000Z-1", {"title": ""}),
        ]
        for resp in responses:
            assert resp.status_code == 403, resp.request.url
            assert detail(resp) == FORBIDDEN
            assert "secret draft" not in resp.text
        assert body(doc["id"]) == "public part\n"
        assert token(env, doc["id"]) == tok
        assert doc_dirs(env) == dirs
        # The doc itself stays readable for them.
        assert c.get(f"/app/api/docs/{doc['id']}").status_code == 200

    @pytest.mark.parametrize("who, shares", [
        ("bob", [("bob", "write")]),
        ("carol", [(None, "write")]),
        ("bob", [("bob", "read"), (None, "write")]),
    ])
    def test_write_share_editors_get_full_history(self, env, who, shares):
        doc = seed_doc(env, "Shared", "v0\n", shares=shares)
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        c = client(env, who)
        base = f"/app/api/docs/{doc['id']}/revisions"
        listing = c.get(base)
        assert listing.status_code == 200
        assert [v["id"] for v in listing.json()["revisions"]] == [rev]
        assert c.get(f"{base}/{rev}").json()["content"] == "v0\n"
        assert c.get(f"{base}/{rev}?diff=current").json()["diff"]["added"] == 1
        copied = copy(c, doc["id"], rev)
        assert copied.status_code == 201
        assert copied.json()["owner_id"] == uid(env, who)
        resp = restore(c, doc["id"], rev, token(env, doc["id"]))
        assert resp.status_code == 200 and resp.json()["changed"] is True
        assert body(doc["id"]) == "v0\n"

    def test_share_downgraded_to_read_is_forbidden(self, env):
        doc = seed_doc(env, "Downgrade", "v0\n", shares=[("bob", "write")])
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        c = client(env, "bob")
        assert c.get(f"/app/api/docs/{doc['id']}/revisions").status_code == 200
        _run(env.doc_store.add_share(doc["id"], uid(env, "bob"), "read"))
        for resp in _all_routes(c, doc["id"], rev, token(env, doc["id"])):
            assert resp.status_code == 403
            assert detail(resp) == FORBIDDEN

    @pytest.mark.parametrize("change", ["downgrade", "revoke"])
    def test_share_changed_while_reading_wins(self, env, monkeypatch, change):
        """The service re-checks a FRESH row after its file reads: a share
        downgraded (403) or revoked (404) mid-request leaks nothing and
        creates nothing."""
        from chat.docs import history

        doc = seed_doc(env, "Race", "secret draft\n", shares=[("bob", "write")])
        write(env, doc["id"], "cleaned up\n", "ui")
        (rev,) = rev_ids(doc["id"])
        share_id = _run(env.doc_store.get_doc(doc["id"]))["shares"][0]["id"]
        real_recheck = history._recheck_history_access
        rechecks = []

        async def change_then_recheck(user, doc_id):
            if change == "downgrade":
                await env.doc_store.add_share(doc_id, uid(env, "bob"), "read")
            else:
                await env.doc_store.remove_share(doc_id, share_id)
            rechecks.append(doc_id)
            return await real_recheck(user, doc_id)

        monkeypatch.setattr(history, "_recheck_history_access", change_then_recheck)
        if change == "downgrade":
            expected_status, expected = 403, FORBIDDEN
        else:
            expected_status = 404
            expected = {"error": "doc_not_found", "message": doc_not_found_message(doc["id"])}
        c = client(env, "bob")
        base = f"/app/api/docs/{doc['id']}/revisions"
        dirs = doc_dirs(env)
        for request in (
            lambda: c.get(base),
            lambda: c.get(f"{base}/{rev}"),
            lambda: c.get(f"{base}/{rev}?diff=current"),
            lambda: copy(c, doc["id"], rev),
        ):
            _run(env.doc_store.add_share(doc["id"], uid(env, "bob"), "write"))
            if change == "revoke":
                share_id = _run(env.doc_store.get_doc(doc["id"]))["shares"][0]["id"]
            resp = request()
            assert resp.status_code == expected_status, resp.text
            assert detail(resp) == expected
            assert "secret draft" not in resp.text
        assert len(rechecks) == 4
        assert doc_dirs(env) == dirs
        assert user_docs_of(env, "bob") == []

    def test_public_project_doc_behind_gate(self, env):
        doc = seed_doc(env, "Open", "v0\n", mode="public", project_id=env.public_project)
        write(env, doc["id"], "v1\n", f"ui:{uid(env, 'alice')}")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        expected = {"error": "doc_not_found", "message": doc_not_found_message(doc["id"])}
        for resp in _all_routes(client(env), doc["id"], rev, tok):
            assert resp.status_code == 404
            assert detail(resp) == expected
        assert body(doc["id"]) == "v1\n"

        env.fg.set_feature_enabled(env.fg.FEATURE_PUBLIC_PROJECTS, True)
        c = client(env)
        assert c.get(f"/app/api/docs/{doc['id']}/revisions").status_code == 200
        resp = copy(c, doc["id"], rev)
        assert resp.status_code == 201
        row = resp.json()
        assert row["project_id"] is None and row["mode"] == "private"
        assert body(row["id"]) == "v0\n"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_owner_restore(self, env):
        cid = make_conversation(env, "Writer chat")
        doc = seed_doc(env, "Restore", "original\n")
        write(env, doc["id"], "second\n", f"conversation:{cid}")
        write(env, doc["id"], "third\n", "action_request:9")
        first, second = rev_ids(doc["id"])
        c = client(env)
        tok = c.get(f"/app/api/docs/{doc['id']}").json()["updated_at"]
        env.published.clear()

        resp = restore(c, doc["id"], first, tok)
        assert resp.status_code == 200
        row = resp.json()
        assert row["id"] == doc["id"]
        assert row["content"] == "original\n" and row["changed"] is True
        assert row["updated_at"] != tok
        assert row["content_size"] == len("original\n")
        assert body(doc["id"]) == "original\n"

        # The replaced body (third) was snapshotted with its writer.
        ids = rev_ids(doc["id"])
        assert ids[:2] == [first, second] and len(ids) == 3
        assert files.doc_paths(doc["id"]).revisions.joinpath(f"{ids[2]}.md").read_text() == "third\n"
        assert meta_of(rev_path(doc["id"], ids[2]).with_suffix(".json"))["source"] == "action_request:9"
        alice_src = f"ui:{uid(env, 'alice')}"
        assert files.read_doc_meta(doc["id"])["source"] == alice_src
        assert _run(env.doc_store.get_doc(doc["id"]))["last_write_source"] == alice_src
        assert (uid(env, "alice"), "doc_changed") in event_types(env)

        listing = c.get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert listing["current"]["source_label"] == "you"
        assert listing["revisions"][0]["source_label"] == "action request #9"

    def test_identical_body_writes_nothing(self, env):
        doc = seed_doc(env, "Same", "A\n")
        write(env, doc["id"], "B\n", "ui")
        write(env, doc["id"], "A\n", "ui")
        first, _second = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        meta_before = files.read_doc_meta(doc["id"])
        env.published.clear()
        resp = restore(client(env), doc["id"], first, tok)
        assert resp.status_code == 200
        row = resp.json()
        assert row["changed"] is False and row["content"] == "A\n"
        assert row["updated_at"] == tok == token(env, doc["id"])
        assert len(rev_ids(doc["id"])) == 2
        assert files.read_doc_meta(doc["id"]) == meta_before
        assert env.published == []

    def test_stale_token_flat_409(self, env):
        doc = seed_doc(env, "Stale", "v0\n")
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        c = client(env)
        tok = c.get(f"/app/api/docs/{doc['id']}").json()["updated_at"]
        # Another writer lands after the client read its token.
        landed = write(env, doc["id"], "v2 from elsewhere\n", "conversation:other")
        resp = restore(c, doc["id"], rev, tok)
        assert resp.status_code == 409
        got = resp.json()
        assert "detail" not in got
        assert got["error"] == "stale_update" and got["message"]
        assert got["current"]["id"] == doc["id"]
        assert got["current"]["updated_at"] == landed["updated_at"]
        assert body(doc["id"]) == "v2 from elsewhere\n"

    def test_write_landing_while_restore_waits_for_lock(self, env, monkeypatch):
        """TOCTOU: the route's own checks pass, then a write lands before the
        restore holds the per-doc lock; the locked re-check must refuse."""
        from chat.docs import service

        doc = seed_doc(env, "Race", "v0\n")
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        real_lock = service._write_lock
        intruded = []

        class _IntrudingLock:
            def __init__(self, lock):
                self.lock = lock

            async def __aenter__(self):
                if not intruded:
                    intruded.append(True)
                    size = await asyncio.to_thread(
                        files.write_body, doc["id"], "intruder\n",
                        write_source="conversation:intruder",
                    )
                    await env.doc_store.update_after_write(
                        doc["id"], content_size=size,
                        last_write_source="conversation:intruder",
                    )
                await self.lock.acquire()

            async def __aexit__(self, *exc):
                self.lock.release()

        monkeypatch.setattr(
            service, "_write_lock", lambda doc_id: _IntrudingLock(real_lock(doc_id)),
        )
        resp = restore(client(env), doc["id"], rev, tok)
        assert intruded == [True]
        assert resp.status_code == 409
        assert resp.json()["error"] == "stale_update"
        assert body(doc["id"]) == "intruder\n"
        assert files.read_doc_meta(doc["id"])["source"] == "conversation:intruder"

    @pytest.mark.parametrize("payload", [None, {}, {"expected_updated_at": ""},
                                         {"expected_updated_at": 123},
                                         {"expected_updated_at": None}])
    def test_token_required(self, env, payload):
        doc = seed_doc(env, "Token", "v0\n")
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        kwargs = {} if payload is None else {"json": payload}
        resp = client(env).post(f"/app/api/docs/{doc['id']}/revisions/{rev}/restore", **kwargs)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"
        assert body(doc["id"]) == "v1\n"

    def test_write_share_restore_attributed_by_name(self, env):
        doc = seed_doc(
            env, "Team", "draft\n", shares=[("bob", "write"), ("carol", "write")],
        )
        write(env, doc["id"], "final\n", f"ui:{uid(env, 'alice')}")
        (rev,) = rev_ids(doc["id"])
        bob = client(env, "bob")
        tok = bob.get(f"/app/api/docs/{doc['id']}").json()["updated_at"]
        env.published.clear()

        resp = restore(bob, doc["id"], rev, tok)
        assert resp.status_code == 200
        row = resp.json()
        assert row["changed"] is True and row["content"] == "draft\n"
        assert row["last_write_source"] is None  # non-owner row
        bob_src = f"ui:{uid(env, 'bob')}"
        assert files.read_doc_meta(doc["id"])["source"] == bob_src
        assert _run(env.doc_store.get_doc(doc["id"]))["last_write_source"] == bob_src
        assert (uid(env, "alice"), "doc_changed") in event_types(env)

        owner_view = client(env).get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert owner_view["current"]["source"] == bob_src
        assert owner_view["current"]["source_label"] == "Bob Builder"
        assert owner_view["revisions"][0]["source_label"] == "you"  # alice's "final"

        bob_view = bob.get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert bob_view["current"]["source_label"] == "you"
        assert bob_view["revisions"][0]["source_label"] == "Alice Owner"

        # Another write-share editor sees the name, never the raw source.
        carol_view = client(env, "carol").get(f"/app/api/docs/{doc['id']}/revisions").json()
        assert carol_view["current"]["source_label"] == "Bob Builder"
        assert carol_view["current"]["source"] is None

    def test_restore_after_share_revoked_mid_request(self, env, monkeypatch):
        """The locked re-check also catches a write share revoked between the
        route's access check and the write."""
        from chat.docs import service

        doc = seed_doc(env, "Revoke", "v0\n", shares=[("bob", "write")])
        write(env, doc["id"], "v1\n", "ui")
        (rev,) = rev_ids(doc["id"])
        tok = token(env, doc["id"])
        share_id = _run(env.doc_store.get_doc(doc["id"]))["shares"][0]["id"]
        real_lock = service._write_lock

        class _RevokingLock:
            def __init__(self, lock):
                self.lock = lock

            async def __aenter__(self):
                await env.doc_store.remove_share(doc["id"], share_id)
                await self.lock.acquire()

            async def __aexit__(self, *exc):
                self.lock.release()

        monkeypatch.setattr(
            service, "_write_lock", lambda doc_id: _RevokingLock(real_lock(doc_id)),
        )
        resp = restore(client(env, "bob"), doc["id"], rev, tok)
        assert resp.status_code == 404
        assert detail(resp) == {
            "error": "doc_not_found", "message": doc_not_found_message(doc["id"]),
        }
        assert body(doc["id"]) == "v1\n"


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


class TestCopy:
    def test_default_titles_and_new_doc(self, env):
        doc = seed_doc(env, "Plan", "v0 body\n", project_id=env.private_project)
        _run(env.doc_store.update_doc_metadata(doc["id"], description="The plan"))
        write(env, doc["id"], "v1 body\n", "ui")
        (rev,) = rev_ids(doc["id"])
        c = client(env)
        env.published.clear()

        resp = copy(c, doc["id"], rev)
        assert resp.status_code == 201
        row = resp.json()
        assert row["title"] == "Plan (copy)"
        assert row["description"] == "The plan"
        assert row["owner_id"] == uid(env, "alice")
        assert row["project_id"] is None and row["scope"] == "user"
        assert row["mode"] == "private" and row["shared"] is False
        assert row["asset_count"] == 0
        alice_src = f"ui:{uid(env, 'alice')}"
        assert row["last_write_source"] == alice_src
        assert body(row["id"]) == "v0 body\n"
        assert row["content_size"] == len("v0 body\n")
        assert files.read_doc_meta(row["id"])["source"] == alice_src
        assert rev_ids(row["id"]) == []
        assert event_types(env) == [(uid(env, "alice"), "doc_list_changed")]
        # The source is untouched.
        assert body(doc["id"]) == "v1 body\n"
        assert rev_ids(doc["id"]) == [rev]

        assert copy(c, doc["id"], rev).json()["title"] == "Plan (copy 2)"
        assert copy(c, doc["id"], rev).json()["title"] == "Plan (copy 3)"

    def test_default_title_skips_case_insensitive_collision(self, env):
        doc = seed_doc(env, "Notes", "v0\n")
        seed_doc(env, "NOTES (COPY)", "other\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        assert copy(client(env), doc["id"], rev).json()["title"] == "Notes (copy 2)"

    def test_long_title_truncated(self, env):
        title = "T" * 195 + " tail"
        assert len(title) == 200
        doc = seed_doc(env, title, "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        c = client(env)
        first = copy(c, doc["id"], rev).json()["title"]
        assert len(first) <= 200 and first.endswith(" (copy)")
        second = copy(c, doc["id"], rev).json()["title"]
        assert len(second) <= 200 and second.endswith(" (copy 2)")

    def test_explicit_title(self, env):
        doc = seed_doc(env, "Plan", "v0\n")
        seed_doc(env, "Taken", "x\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        c = client(env)
        dirs = doc_dirs(env)

        resp = copy(c, doc["id"], rev, {"title": "  taken  "})
        assert resp.status_code == 409
        assert detail(resp)["error"] == "duplicate_title"
        for bad in ("", "   ", "x" * 201, 123):
            resp = copy(c, doc["id"], rev, {"title": bad})
            assert resp.status_code == 400, bad
            assert detail(resp)["error"] == "invalid_title"
        assert doc_dirs(env) == dirs

        resp = copy(c, doc["id"], rev, {"title": "  Fresh name  "})
        assert resp.status_code == 201
        assert resp.json()["title"] == "Fresh name"
        resp = copy(c, doc["id"], rev, {"title": None})
        assert resp.json()["title"] == "Plan (copy)"

    def test_explicit_title_race_cleans_up(self, env, monkeypatch):
        """A collision only the store sees (a concurrent create) is still a
        409 and leaves nothing behind."""
        from chat.docs import history

        doc = seed_doc(env, "Plan", "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        dirs = doc_dirs(env)

        async def nothing_taken(_user_id):
            return set()

        monkeypatch.setattr(history, "_taken_user_doc_titles", nothing_taken)
        resp = copy(client(env), doc["id"], rev, {"title": "Plan"})
        assert resp.status_code == 409
        assert detail(resp)["error"] == "duplicate_title"
        assert doc_dirs(env) == dirs

    def test_referenced_assets_copied_with_same_names(self, env, monkeypatch):
        doc = seed_doc(env, "Pics", "v0\n")
        assets = files.doc_paths(doc["id"]).assets
        assert files.add_asset(doc["id"], "used.png", PNG).name == "used.png"
        assert files.add_asset(doc["id"], "unused.png", PNG).name == "unused.png"
        assert files.add_asset(doc["id"], "gone.png", PNG).name == "gone.png"
        assert files.add_asset(doc["id"], "anim.gif", GIF).name == "anim.gif"
        # Names add_asset would not produce are still kept verbatim.
        (assets / "Photo.JPEG").write_bytes(JPEG)
        (assets / "fake.png").write_bytes(b"not an image at all")
        outside = env.tmp / "outside.png"
        outside.write_bytes(PNG)
        os.symlink(outside, assets / "link.png")

        revision_body = (
            "![a](assets/used.png)\n"
            "![b](assets/gone.png)\n"
            "![c](assets/link.png)\n"
            "![d](assets/fake.png)\n"
            "![e](assets/Photo.JPEG \"title\")\n"
            "see assets/anim.gif, also assets/../escape.png and assets/x.svg\n"
        )
        files.write_body(doc["id"], revision_body, write_source="ui")
        files.write_body(doc["id"], "current no images\n", write_source="ui")
        rev = rev_ids(doc["id"])[-1]
        (assets / "gone.png").unlink()

        opened = []
        real_open = os.open

        def open_spy(path, *args, **kwargs):
            if not isinstance(path, int):
                opened.append(os.path.realpath(os.fsdecode(path)))
                opened.append(os.path.abspath(os.fsdecode(path)))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_spy)
        resp = copy(client(env), doc["id"], rev)
        monkeypatch.setattr(os, "open", real_open)
        assert resp.status_code == 201
        # Neither the link nor its target was ever opened.
        assert str(outside) not in opened
        assert str(assets / "link.png") not in opened
        assert str(assets / "used.png") in opened  # the spy did see the reads
        row = resp.json()
        new_assets = files.doc_paths(row["id"]).assets
        assert sorted(os.listdir(new_assets)) == ["Photo.JPEG", "anim.gif", "used.png"]
        for name in ("Photo.JPEG", "anim.gif", "used.png"):
            target = new_assets / name
            assert not target.is_symlink()
            assert target.read_bytes() == (assets / name).read_bytes()
        assert row["asset_count"] == 3
        assert _run(env.doc_store.get_doc(row["id"]))["asset_count"] == 3
        assert body(row["id"]) == revision_body
        # The source keeps everything (and the symlink target was not read).
        assert sorted(os.listdir(assets)) == [
            "Photo.JPEG", "anim.gif", "fake.png", "link.png", "unused.png", "used.png",
        ]

    def test_referenced_asset_names(self):
        from chat.docs.history import referenced_asset_names

        text = (
            "![x](assets/a.png) assets/a.png assets/b.PNG. assets/xassets/c.webp "
            "assets/.hidden.png assets/d.txt assets/e.jpg-more myassets/f.gif"
        )
        assert referenced_asset_names(text) == ["a.png", "c.webp", "f.gif"]

    def test_write_share_recipient_copy_is_theirs(self, env):
        doc = seed_doc(env, "Alice notes", "v0\n", shares=[("bob", "write")])
        files.add_asset(doc["id"], "pic.png", PNG)
        write(env, doc["id"], "![p](assets/pic.png)\n", "ui")
        write(env, doc["id"], "v2\n", "ui")
        rev = rev_ids(doc["id"])[-1]
        env.published.clear()
        resp = copy(client(env, "bob"), doc["id"], rev)
        assert resp.status_code == 201
        row = resp.json()
        assert row["owner_id"] == uid(env, "bob")
        assert row["title"] == "Alice notes (copy)"
        assert row["access"]["can_delete"] is True
        assert row["last_write_source"] == f"ui:{uid(env, 'bob')}"
        assert files.read_doc_meta(row["id"])["source"] == f"ui:{uid(env, 'bob')}"
        assert os.listdir(files.doc_paths(row["id"]).assets) == ["pic.png"]
        assert event_types(env) == [(uid(env, "bob"), "doc_list_changed")]
        assert [d["id"] for d in user_docs_of(env, "bob")] == [row["id"]]
        # Alice's doc is unchanged and unshared with bob's copy.
        assert body(doc["id"]) == "v2\n"

    def test_recipient_default_title_ignores_owner_titles(self, env):
        doc = seed_doc(env, "Notes", "v0\n", shares=[("bob", "write")])
        # The owner's own copy, even shared with bob, is not in bob's namespace.
        seed_doc(env, "Notes (copy)", "alice's copy\n", shares=[("bob", "read")])
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        row = copy(client(env, "bob"), doc["id"], rev).json()
        assert row["title"] == "Notes (copy)"
        assert row["owner_id"] == uid(env, "bob")
        assert copy(client(env), doc["id"], rev).json()["title"] == "Notes (copy 2)"

    def test_copy_everyone_shared_doc(self, env):
        doc = seed_doc(env, "Handbook", "v0 rules\n", shares=[(None, "write")])
        write(env, doc["id"], "v1 rules\n", "ui")
        (rev,) = rev_ids(doc["id"])
        env.published.clear()
        resp = copy(client(env, "carol"), doc["id"], rev)
        assert resp.status_code == 201
        row = resp.json()
        assert row["owner_id"] == uid(env, "carol")
        assert row["title"] == "Handbook (copy)"
        assert row["shares"] == [] and row["shared"] is False
        assert body(row["id"]) == "v0 rules\n"
        assert event_types(env) == [(uid(env, "carol"), "doc_list_changed")]
        # The copy is carol's alone: bob cannot see it.
        hidden = client(env, "bob").get(f"/app/api/docs/{row['id']}")
        assert hidden.status_code == 404

    def test_insert_that_raises_after_commit_is_cleaned_up(self, env, monkeypatch):
        """The cleanup deletes the row unconditionally, so an insert that
        landed but raised on the way back leaves no orphan row."""
        doc = seed_doc(env, "Plan", "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        dirs = doc_dirs(env)
        before = [d["id"] for d in user_docs_of(env, "alice")]
        real_create = env.doc_store.create_doc
        created = []

        async def create_then_fail(*args, **kwargs):
            created.append(await real_create(*args, **kwargs))
            raise RuntimeError("connection lost after commit")

        monkeypatch.setattr(env.doc_store, "create_doc", create_then_fail)
        resp = copy(client(env, raise_server_exceptions=False), doc["id"], rev)
        assert resp.status_code == 500
        assert len(created) == 1
        assert _run(env.doc_store.get_doc(created[0]["id"])) is None
        assert [d["id"] for d in user_docs_of(env, "alice")] == before
        assert doc_dirs(env) == dirs

    def test_cancelled_copy_still_completes(self, env, monkeypatch):
        """The build is shielded: cancelling the request mid-copy neither
        leaves a half-built doc nor loses the copy."""
        from chat.docs import history
        from chat.docs.routes import _ui_access

        doc = seed_doc(env, "Pics", "v0\n")
        files.add_asset(doc["id"], "pic.png", PNG)
        files.write_body(doc["id"], "![p](assets/pic.png)\n", write_source="ui")
        files.write_body(doc["id"], "later\n", write_source="ui")
        rev = rev_ids(doc["id"])[-1]
        started, release = threading.Event(), threading.Event()
        real_copy = history._copy_assets_sync

        def slow_copy(*args):
            started.set()
            release.wait(5)
            return real_copy(*args)

        monkeypatch.setattr(history, "_copy_assets_sync", slow_copy)
        alice = env.users["alice"]
        env.published.clear()

        async def scenario():
            access = _ui_access(alice, doc)
            task = asyncio.ensure_future(history.copy_revision(alice, doc, access, rev))
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The build announces itself as its very last step.
            for _ in range(500):
                if env.published:
                    return await env.doc_store.list_accessible_docs(
                        alice["id"], owned_only=True,
                    )
                await asyncio.sleep(0.01)
            raise AssertionError("the shielded copy never finished")

        docs = _run(scenario())
        (copied,) = [d for d in docs if d["id"] != doc["id"]]
        assert copied["title"] == "Pics (copy)"
        assert copied["asset_count"] == 1
        assert os.listdir(files.doc_paths(copied["id"]).assets) == ["pic.png"]
        assert body(copied["id"]) == "![p](assets/pic.png)\n"
        assert event_types(env) == [(alice["id"], "doc_list_changed")]

    def test_failure_before_insert_cleans_up(self, env, monkeypatch):
        doc = seed_doc(env, "Plan", "v0\n")
        files.write_body(doc["id"], "v1\n", write_source="ui")
        (rev,) = rev_ids(doc["id"])
        dirs = doc_dirs(env)

        async def boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(env.doc_store, "create_doc", boom)
        env.published.clear()
        resp = copy(client(env, raise_server_exceptions=False), doc["id"], rev)
        assert resp.status_code == 500
        assert doc_dirs(env) == dirs
        assert env.published == []

    def test_failure_after_insert_removes_row_and_dir(self, env, monkeypatch):
        doc = seed_doc(env, "Plan", "v0\n")
        files.add_asset(doc["id"], "pic.png", PNG)
        files.write_body(doc["id"], "![p](assets/pic.png)\n", write_source="ui")
        files.write_body(doc["id"], "later\n", write_source="ui")
        rev = rev_ids(doc["id"])[-1]
        dirs = doc_dirs(env)
        before = [d["id"] for d in user_docs_of(env, "alice")]

        async def boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(env.doc_store, "update_after_write", boom)
        resp = copy(client(env, raise_server_exceptions=False), doc["id"], rev)
        assert resp.status_code == 500
        assert doc_dirs(env) == dirs
        assert [d["id"] for d in user_docs_of(env, "alice")] == before

    def test_storage_failure_is_doc_storage_error(self, env, monkeypatch):
        from chat.docs import history

        doc = seed_doc(env, "Plan", "v0\n")
        files.add_asset(doc["id"], "pic.png", PNG)
        files.write_body(doc["id"], "![p](assets/pic.png)\n", write_source="ui")
        files.write_body(doc["id"], "later\n", write_source="ui")
        rev = rev_ids(doc["id"])[-1]
        dirs = doc_dirs(env)

        real_write = history.doc_files._write_new_file
        failed = []

        def disk_full_for_assets(path, data):
            if path.parent.name == "assets":
                failed.append(path.name)
                raise OSError(28, "No space left on device")
            return real_write(path, data)

        monkeypatch.setattr(history.doc_files, "_write_new_file", disk_full_for_assets)
        resp = copy(client(env), doc["id"], rev)
        assert failed == ["pic.png"]
        assert resp.status_code == 500
        assert detail(resp)["error"] == "doc_storage_error"
        assert doc_dirs(env) == dirs
