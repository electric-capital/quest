"""Doc notes HTTP routes (chat/docs/note_routes.py), phase A6 of spec 00011
(docs/architecture/doc-notes.md).

Runs the router in a bare FastAPI app with the auth dependency overridden
(tests/test_docs_routes.py pattern) on the isolated ``docs_env`` of
tests/test_docs_service.py, with the ``doc_notes`` gate open (``env``).
Users: alice (owns every doc), bob, carol, plus dave added here (a
stranger, or a viewer through an everyone share). Expected payloads are
literal.

Covers: auth (401 without a session); the gate (every route 403
``doc_notes_disabled`` with ``doc_notes`` closed and with ``docs`` closed,
before the doc lookup, nothing written; the per-user list); 404 parity
(a stranger and a revoked recipient get the missing doc's body byte for
byte, nothing written); the role matrix of every route (owner, write
share, read share, everyone-read and everyone-write shares, stranger);
the GET payload (all memories newest first, no budget); the instructions
save (clear, identical-text no-op, ``stale_instructions`` with
``current``, every ``invalid_instructions`` shape); the memory writes
(``invalid_memory``, ``duplicate_memory`` with ``memory_id``,
``memories_cap_reached``, ``memory_not_found``, identical-text no-op);
the source labels and editor identities per viewer, dead references
included, and no raw ``source`` / ``created_by`` / ``updated_by`` in any
response; ``doc_notes_changed`` after every landed write, alone, to the
doc's audience, never on a refusal or a no-op; the body's ``updated_at``
and ``last_write_source`` never moved.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_MEMORY_MAX_CHARS,
    doc_not_found_message,
    doc_notes_disabled_message,
)
from db import doc_memory_store
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    docs_env,
    seed_doc,
)

STALE_MESSAGE = (
    "The instructions changed since you started editing. Review the current "
    "text and save again."
)
FORBIDDEN_INSTRUCTIONS = {
    "error": "forbidden",
    "message": "Only the doc's owner can change its instructions.",
}
FORBIDDEN_READ_ONLY = {
    "error": "forbidden",
    "message": "You have read-only access to this doc.",
}
MEMORY_NOT_FOUND = {"error": "memory_not_found", "message": "Doc memory not found."}
NOT_A_STRING = {
    "error": "invalid_instructions", "message": "Doc instructions must be a string.",
}
BAD_VERSION = {
    "error": "invalid_instructions",
    "message": "expected_instructions_version must be a non-negative integer.",
}
DUPLICATE_MESSAGE = "This doc already has a memory with exactly this text."
CAP_REACHED = {
    "error": "memories_cap_reached",
    "message": (
        f"This doc already has the maximum of {DOC_MAX_MEMORIES} memories. "
        "Delete or edit one instead."
    ),
}
LABEL_YOU = "Added by you"
LABEL_PANEL = "Added in the Notes panel"
LABEL_SUGGESTION = "Suggested in a conversation"
RAW_KEYS = {"source", "created_by", "updated_by"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(request):
    """``docs_env`` with the ``doc_notes`` gate open for everyone and a
    fourth user, dave."""
    e = request.getfixturevalue("docs_env")
    e.fg.set_feature_enabled(e.fg.FEATURE_DOC_NOTES, True)
    e.users["dave"] = {"id": add_user(e, "dave@example.com"), "email": "dave@example.com"}
    return e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def add_user(env, address):
    from db.models import User

    async def _add():
        async with env.doc_store.AsyncSessionLocal() as db:
            user = User(email=address, api_key=f"k-{uuid.uuid4().hex}")
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user.id

    return _run(_add())


def _sql(env, statement, **params):
    async def _go():
        async with env.doc_store.AsyncSessionLocal() as db:
            await db.execute(text(statement), params)
            await db.commit()

    _run(_go())


def set_names(env, **names):
    for who, name in names.items():
        _sql(env, "UPDATE users SET name = :n WHERE id = :i", n=name, i=uid(env, who))


def client(env, who="alice"):
    from chat.docs import note_routes

    app = FastAPI()
    app.include_router(note_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[note_routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def detail(resp):
    return resp.json()["detail"]


def uid(env, who="alice"):
    return env.users[who]["id"]


def new_doc(env, shares=()):
    return seed_doc(env, title=f"Doc {uuid.uuid4().hex[:8]}", shares=shares)["id"]


def state(env, doc_id):
    return _run(env.doc_store.get_notes_state(doc_id))


def memories(doc_id):
    return _run(doc_memory_store.list_doc_memories(doc_id))


def add_memory(env, doc_id, content, who="alice"):
    return _run(doc_memory_store.create_doc_memory(
        doc_id, content, source=doc_memory_store.ui_source(uid(env, who)),
        user_id=uid(env, who),
    ))


def set_instructions(env, doc_id, content):
    current = state(env, doc_id)
    return _run(env.doc_store.update_doc_instructions(
        doc_id, content, expected_version=current["instructions_version"],
        user_id=uid(env),
    ))


def notes_changed(doc_id, instructions_version, memories_version):
    return {
        "type": "doc_notes_changed",
        "doc_id": doc_id,
        "instructions_version": instructions_version,
        "memories_version": memories_version,
    }


def audience_events(env, doc_id, instructions_version, memories_version, who=None):
    event = notes_changed(doc_id, instructions_version, memories_version)
    return [(uid(env, name), event) for name in (who or ("alice", "bob", "carol"))]


def raw_keys(value):
    """Every key of a JSON value that is a raw memory field."""
    if isinstance(value, dict):
        found = RAW_KEYS & set(value)
        for item in value.values():
            found |= raw_keys(item)
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found |= raw_keys(item)
        return found
    return set()


def snapshot(env, doc_id):
    """Everything a refused or hidden call must leave as it was."""
    doc = _run(env.doc_store.get_doc(doc_id, with_shares=False))
    return (
        state(env, doc_id), memories(doc_id),
        doc["updated_at"], doc["last_write_source"],
    )


def calls(doc_id, memory_id):
    """One request per route: (method, url, json body or None)."""
    base = f"/app/api/docs/{doc_id}"
    return [
        ("get", f"{base}/notes", None),
        ("put", f"{base}/instructions",
         {"instructions": "New rule.", "expected_instructions_version": 0}),
        ("post", f"{base}/memories", {"content": "Another fact."}),
        ("put", f"{base}/memories/{memory_id}", {"content": "Changed fact."}),
        ("delete", f"{base}/memories/{memory_id}", None),
    ]


def send(c, method, url, payload):
    if payload is None:
        return getattr(c, method)(url)
    return getattr(c, method)(url, json=payload)


def payload(env, doc_id, *, instructions="", instructions_version=0,
            instructions_updated_at=None, instructions_updated_by=None,
            memories_list=(), memories_version=0, can_edit=True, is_owner=True):
    return {
        "doc_id": doc_id,
        "instructions": instructions,
        "instructions_version": instructions_version,
        "instructions_updated_at": instructions_updated_at,
        "instructions_updated_by": instructions_updated_by,
        "memories": list(memories_list),
        "memories_version": memories_version,
        "can_edit": can_edit,
        "is_owner": is_owner,
    }


def public(memory, label):
    return {
        "id": memory["id"],
        "content": memory["content"],
        "source_label": label,
        "created_at": memory["created_at"],
        "updated_at": memory["updated_at"],
    }


# ---------------------------------------------------------------------------
# Auth and gate
# ---------------------------------------------------------------------------


class TestAuth:
    def test_every_route_401s_without_a_session(self, env):
        from chat.docs import note_routes

        doc = new_doc(env)
        memory = add_memory(env, doc, "A fact.")
        app = FastAPI()
        app.include_router(note_routes.router)
        c = TestClient(app)
        before = snapshot(env, doc)
        for method, url, body in calls(doc, memory["id"]):
            resp = send(c, method, url, body)
            assert resp.status_code == 401, (method, url)
            assert detail(resp)["error"] == "not_authenticated"
        assert snapshot(env, doc) == before
        assert env.published == []


class TestGate:
    @pytest.mark.parametrize("closed", ["doc_notes", "docs"])
    def test_every_route_403s_when_closed(self, env, closed):
        doc = new_doc(env, shares=[("bob", "write")])
        memory = add_memory(env, doc, "A fact.")
        before = snapshot(env, doc)
        env.fg.set_feature_enabled(closed, False)
        missing = str(uuid.uuid4())
        for who in ("alice", "bob", "carol"):
            c = client(env, who)
            # The gate comes before the doc lookup: a missing id 403s too.
            for doc_id in (doc, missing):
                for method, url, body in calls(doc_id, memory["id"]):
                    resp = send(c, method, url, body)
                    assert resp.status_code == 403, (who, method, url)
                    assert resp.json() == {"detail": {
                        "error": "doc_notes_disabled",
                        "message": doc_notes_disabled_message(),
                    }}
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_gate_per_user(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "write")])
        memory = add_memory(env, doc, "A fact.")
        fg = env.fg
        fg.set_feature_allowed_users(fg.FEATURE_DOC_NOTES, [env.users["bob"]["email"]])
        for who in ("alice", "carol"):
            resp = client(env, who).get(f"/app/api/docs/{doc}/notes")
            assert resp.status_code == 403
            assert detail(resp)["error"] == "doc_notes_disabled"
            resp = client(env, who).delete(f"/app/api/docs/{doc}/memories/{memory['id']}")
            assert resp.status_code == 403
        assert memories(doc) == [memory]
        bob = client(env, "bob")
        assert bob.get(f"/app/api/docs/{doc}/notes").status_code == 200
        resp = bob.post(f"/app/api/docs/{doc}/memories", json={"content": "Bob's fact."})
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 404 parity
# ---------------------------------------------------------------------------


class TestNotFoundParity:
    def _assert_hidden_like_missing(self, env, who, doc, memory_id):
        c = client(env, who)
        before = snapshot(env, doc)
        hidden = [send(c, *call) for call in calls(doc, memory_id)]
        for resp in hidden:
            assert resp.status_code == 404
            assert resp.json() == {"detail": {
                "error": "doc_not_found", "message": doc_not_found_message(doc),
            }}
        assert snapshot(env, doc) == before
        assert env.published == []
        _run(env.doc_store.delete_doc(doc))
        missing = [send(c, *call) for call in calls(doc, memory_id)]
        for hidden_resp, missing_resp in zip(hidden, missing):
            assert missing_resp.status_code == hidden_resp.status_code
            assert missing_resp.content == hidden_resp.content

    def test_stranger(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])
        memory = add_memory(env, doc, "A fact.")
        set_instructions(env, doc, "A rule.")
        self._assert_hidden_like_missing(env, "dave", doc, memory["id"])

    @pytest.mark.parametrize("permission", ["read", "write"])
    def test_revoked_recipient(self, env, permission):
        doc = new_doc(env, shares=[("bob", permission)])
        memory = add_memory(env, doc, "A fact.")
        assert client(env, "bob").get(f"/app/api/docs/{doc}/notes").status_code == 200
        [share] = _run(env.doc_store.list_shares(doc))
        assert _run(env.doc_store.remove_share(doc, share["id"]))
        self._assert_hidden_like_missing(env, "bob", doc, memory["id"])

    def test_missing_literal(self, env):
        missing = str(uuid.uuid4())
        resp = client(env).get(f"/app/api/docs/{missing}/notes")
        assert resp.status_code == 404
        assert resp.json() == {"detail": {
            "error": "doc_not_found", "message": doc_not_found_message(missing),
        }}


# ---------------------------------------------------------------------------
# Role matrix
# ---------------------------------------------------------------------------


# role -> (user, doc key). Doc "shared": bob write, carol read (dave is a
# stranger). Doc "everyone_read" / "everyone_write": one everyone share.
ROLES = {
    "owner": ("alice", "shared"),
    "write_share": ("bob", "shared"),
    "read_share": ("carol", "shared"),
    "everyone_read": ("dave", "everyone_read"),
    "everyone_write": ("dave", "everyone_write"),
    "stranger": ("dave", "shared"),
}
EDITORS = {"owner", "write_share", "everyone_write"}


def role_doc(env, role):
    who, kind = ROLES[role]
    shares = {
        "shared": [("bob", "write"), ("carol", "read")],
        "everyone_read": [(None, "read")],
        "everyone_write": [(None, "write")],
    }[kind]
    doc = new_doc(env, shares=shares)
    memory = add_memory(env, doc, "A fact.")
    env.published.clear()
    return who, doc, memory


class TestRoleMatrix:
    @pytest.mark.parametrize("role", list(ROLES))
    def test_get(self, env, role):
        who, doc, memory = role_doc(env, role)
        resp = client(env, who).get(f"/app/api/docs/{doc}/notes")
        if role == "stranger":
            assert resp.status_code == 404
            return
        assert resp.status_code == 200
        is_owner = role == "owner"
        assert resp.json() == payload(
            env, doc,
            memories_list=[public(memory, LABEL_YOU if is_owner else LABEL_PANEL)],
            memories_version=1, can_edit=role in EDITORS, is_owner=is_owner,
        )

    @pytest.mark.parametrize("role", list(ROLES))
    def test_put_instructions(self, env, role):
        who, doc, _memory = role_doc(env, role)
        before = snapshot(env, doc)
        resp = client(env, who).put(
            f"/app/api/docs/{doc}/instructions",
            json={"instructions": "A rule.", "expected_instructions_version": 0},
        )
        if role == "owner":
            assert resp.status_code == 200
            assert resp.json()["instructions"] == "A rule."
            assert state(env, doc)["instructions"] == "A rule."
            return
        if role == "stranger":
            assert resp.status_code == 404
        else:
            assert resp.status_code == 403
            assert detail(resp) == FORBIDDEN_INSTRUCTIONS
        assert snapshot(env, doc) == before
        assert env.published == []

    @pytest.mark.parametrize("role", list(ROLES))
    def test_post_memory(self, env, role):
        who, doc, _memory = role_doc(env, role)
        before = snapshot(env, doc)
        resp = client(env, who).post(
            f"/app/api/docs/{doc}/memories", json={"content": "New fact."},
        )
        if role in EDITORS:
            assert resp.status_code == 201
            assert resp.json()["content"] == "New fact."
            assert [m["content"] for m in memories(doc)] == ["New fact.", "A fact."]
            return
        if role == "stranger":
            assert resp.status_code == 404
        else:
            assert resp.status_code == 403
            assert detail(resp) == FORBIDDEN_READ_ONLY
        assert snapshot(env, doc) == before
        assert env.published == []

    @pytest.mark.parametrize("role", list(ROLES))
    def test_put_memory(self, env, role):
        who, doc, memory = role_doc(env, role)
        before = snapshot(env, doc)
        resp = client(env, who).put(
            f"/app/api/docs/{doc}/memories/{memory['id']}", json={"content": "Changed."},
        )
        if role in EDITORS:
            assert resp.status_code == 200
            assert resp.json()["content"] == "Changed."
            assert [m["content"] for m in memories(doc)] == ["Changed."]
            return
        if role == "stranger":
            assert resp.status_code == 404
        else:
            assert resp.status_code == 403
            assert detail(resp) == FORBIDDEN_READ_ONLY
        assert snapshot(env, doc) == before
        assert env.published == []

    @pytest.mark.parametrize("role", list(ROLES))
    def test_delete_memory(self, env, role):
        who, doc, memory = role_doc(env, role)
        before = snapshot(env, doc)
        resp = client(env, who).delete(f"/app/api/docs/{doc}/memories/{memory['id']}")
        if role in EDITORS:
            assert resp.status_code == 200
            assert resp.json() == {"success": True}
            assert memories(doc) == []
            return
        if role == "stranger":
            assert resp.status_code == 404
        else:
            assert resp.status_code == 403
            assert detail(resp) == FORBIDDEN_READ_ONLY
        assert snapshot(env, doc) == before
        assert env.published == []


# ---------------------------------------------------------------------------
# GET payload
# ---------------------------------------------------------------------------


class TestGetNotes:
    def test_empty(self, env):
        doc = new_doc(env)
        resp = client(env).get(f"/app/api/docs/{doc}/notes")
        assert resp.status_code == 200
        assert resp.json() == payload(env, doc)

    def test_owner_payload(self, env):
        set_names(env, alice="Alice A")
        doc = new_doc(env, shares=[("bob", "write")])
        first = add_memory(env, doc, "First fact.")
        second = add_memory(env, doc, "Second fact.", who="bob")
        saved = set_instructions(env, doc, "  Keep it short.\nCite sources.  ")
        resp = client(env).get(f"/app/api/docs/{doc}/notes")
        assert resp.status_code == 200
        assert resp.json() == payload(
            env, doc,
            instructions="Keep it short.\nCite sources.",
            instructions_version=1,
            instructions_updated_at=saved["instructions_updated_at"],
            instructions_updated_by={
                "id": uid(env), "name": "Alice A", "email": "alice@example.com",
            },
            memories_list=[
                public(second, "Added by bob@example.com"), public(first, LABEL_YOU),
            ],
            memories_version=2,
        )

    def test_all_memories_newest_first_no_budget(self, env):
        doc = new_doc(env)
        for i in range(DOC_MAX_MEMORIES):
            add_memory(env, doc, f"{i:02d}" + "x" * (DOC_MEMORY_MAX_CHARS - 2))
        resp = client(env).get(f"/app/api/docs/{doc}/notes")
        assert resp.status_code == 200
        got = resp.json()["memories"]
        assert len(got) == DOC_MAX_MEMORIES
        assert all(len(m["content"]) == DOC_MEMORY_MAX_CHARS for m in got)
        assert [m["id"] for m in got] == [m["id"] for m in memories(doc)]
        stamps = [m["created_at"] for m in got]
        assert stamps == sorted(stamps, reverse=True)
        assert got[0]["content"].startswith(f"{DOC_MAX_MEMORIES - 1:02d}")
        assert got[-1]["content"].startswith("00")


# ---------------------------------------------------------------------------
# Labels and identities
# ---------------------------------------------------------------------------


class TestIdentities:
    def _doc(self, env):
        set_names(env, alice="Alice A", bob="Bob B")
        doc = new_doc(env, shares=[("bob", "write"), ("dave", "write"), ("carol", "read")])
        by_alice = add_memory(env, doc, "Alice's fact.")
        by_bob = add_memory(env, doc, "Bob's fact.", who="bob")
        by_dave = add_memory(env, doc, "Dave's fact.", who="dave")
        # Dead references: a suggestion id that matches no suggestion, and a
        # ui source whose user does not exist (written behind the store).
        by_suggestion = _run(doc_memory_store.create_doc_memory(
            doc, "Suggested fact.",
            source=doc_memory_store.suggestion_source(str(uuid.uuid4())),
            user_id=uid(env),
        ))
        ghost = add_memory(env, doc, "Ghost's fact.")
        _sql(env, "UPDATE doc_memories SET source = 'ui:999999' WHERE id = :i", i=ghost["id"])
        set_instructions(env, doc, "A rule.")
        mems = {m["id"]: m for m in memories(doc)}
        return doc, [
            mems[ghost["id"]], mems[by_suggestion["id"]], mems[by_dave["id"]],
            mems[by_bob["id"]], mems[by_alice["id"]],
        ]

    def test_owner_sees_names(self, env):
        doc, (ghost, suggested, dave, bob, alice) = self._doc(env)
        body = client(env).get(f"/app/api/docs/{doc}/notes").json()
        assert body["instructions_updated_by"] == {
            "id": uid(env), "name": "Alice A", "email": "alice@example.com",
        }
        assert body["memories"] == [
            public(ghost, LABEL_PANEL),
            public(suggested, LABEL_SUGGESTION),
            public(dave, "Added by dave@example.com"),
            public(bob, "Added by Bob B"),
            public(alice, LABEL_YOU),
        ]
        assert raw_keys(body) == set()

    @pytest.mark.parametrize("who", ["bob", "carol", "dave", "everyone"])
    def test_non_owners_see_neutral_labels(self, env, who):
        doc, (ghost, suggested, dave, bob, alice) = self._doc(env)
        if who == "everyone":
            # A viewer only through an everyone share.
            _sql(env, "DELETE FROM doc_shares WHERE doc_id = :d AND user_id = :u",
                 d=doc, u=uid(env, "dave"))
            _run(env.doc_store.add_share(doc, None, "read"))
            who = "dave"
        resp = client(env, who).get(f"/app/api/docs/{doc}/notes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["instructions_updated_by"] is None
        assert body["is_owner"] is False
        assert body["memories"] == [
            public(ghost, LABEL_PANEL),
            public(suggested, LABEL_SUGGESTION),
            public(dave, LABEL_PANEL),
            public(bob, LABEL_PANEL),
            public(alice, LABEL_PANEL),
        ]
        assert raw_keys(body) == set()
        for leak in (b"@example.com", b"Alice A", b"Bob B"):
            assert leak not in resp.content

    def test_write_responses_label_for_the_caller(self, env):
        set_names(env, bob="Bob B")
        doc = new_doc(env, shares=[("bob", "write")])
        resp = client(env, "bob").post(f"/app/api/docs/{doc}/memories", json={"content": "B."})
        assert resp.status_code == 201
        assert resp.json()["source_label"] == LABEL_PANEL
        memory_id = resp.json()["id"]
        resp = client(env).put(f"/app/api/docs/{doc}/memories/{memory_id}", json={"content": "C."})
        # The source keeps the creator; the owner sees bob's name.
        assert resp.json()["source_label"] == "Added by Bob B"
        resp = client(env).post(f"/app/api/docs/{doc}/memories", json={"content": "A."})
        assert resp.json()["source_label"] == LABEL_YOU

    def test_no_route_sends_raw_fields(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        memory = add_memory(env, doc, "A fact.")
        for who in ("alice", "bob"):
            c = client(env, who)
            responses = [
                c.get(f"/app/api/docs/{doc}/notes"),
                c.post(f"/app/api/docs/{doc}/memories", json={"content": f"By {who}."}),
                c.put(f"/app/api/docs/{doc}/memories/{memory['id']}",
                      json={"content": f"Edited by {who}."}),
                c.post(f"/app/api/docs/{doc}/memories", json={"content": f"By {who}."}),
                c.delete(f"/app/api/docs/{doc}/memories/{memory['id']}"),
            ]
            if who == "alice":
                responses.append(c.put(
                    f"/app/api/docs/{doc}/instructions",
                    json={"instructions": "A rule.", "expected_instructions_version": 0},
                ))
            for resp in responses:
                assert raw_keys(resp.json()) == set(), resp.json()
            memory = add_memory(env, doc, f"Again {who}.")


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


class TestInstructions:
    def put(self, env, doc, body, who="alice"):
        return client(env, who).put(f"/app/api/docs/{doc}/instructions", json=body)

    def test_save_returns_payload_and_publishes(self, env):
        set_names(env, alice="Alice A")
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])
        memory = add_memory(env, doc, "A fact.")
        env.published.clear()
        resp = self.put(env, doc, {"instructions": " A rule.\n", "expected_instructions_version": 0})
        assert resp.status_code == 200
        now = state(env, doc)
        assert resp.json() == payload(
            env, doc, instructions="A rule.", instructions_version=1,
            instructions_updated_at=now["instructions_updated_at"],
            instructions_updated_by={
                "id": uid(env), "name": "Alice A", "email": "alice@example.com",
            },
            memories_list=[public(memory, LABEL_YOU)], memories_version=1,
        )
        assert now["instructions_updated_by"] == uid(env)
        assert env.published == audience_events(env, doc, 1, 1)

    def test_empty_string_clears(self, env):
        doc = new_doc(env)
        set_instructions(env, doc, "A rule.")
        env.published.clear()
        resp = self.put(env, doc, {"instructions": "", "expected_instructions_version": 1})
        assert resp.status_code == 200
        assert resp.json()["instructions"] == ""
        assert resp.json()["instructions_version"] == 2
        assert state(env, doc)["has_instructions"] is False
        assert env.published == audience_events(env, doc, 2, 0, who=("alice",))

    @pytest.mark.parametrize("same", ["A rule.", "  A rule.\n\n"])
    def test_identical_text_is_a_no_op(self, env, same):
        doc = new_doc(env, shares=[("bob", "write")])
        set_instructions(env, doc, "A rule.")
        before = snapshot(env, doc)
        env.published.clear()
        resp = self.put(env, doc, {"instructions": same, "expected_instructions_version": 1})
        assert resp.status_code == 200
        assert resp.json()["instructions_version"] == 1
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_stale(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        set_instructions(env, doc, "Rule A.")
        before = snapshot(env, doc)
        env.published.clear()
        resp = self.put(env, doc, {"instructions": "Rule B.", "expected_instructions_version": 0})
        assert resp.status_code == 409
        assert resp.json() == {
            "error": "stale_instructions",
            "message": STALE_MESSAGE,
            "current": {"instructions": "Rule A.", "instructions_version": 1},
        }
        assert snapshot(env, doc) == before
        assert env.published == []
        # A save from the current version lands.
        resp = self.put(env, doc, {"instructions": "Rule B.", "expected_instructions_version": 1})
        assert resp.status_code == 200
        assert resp.json()["instructions"] == "Rule B."
        assert env.published == audience_events(env, doc, 2, 0, who=("alice", "bob"))

    @pytest.mark.parametrize("body, error", [
        ({"instructions": "x" * (DOC_INSTRUCTIONS_MAX_CHARS + 1),
          "expected_instructions_version": 0},
         {"error": "invalid_instructions",
          "message": f"Doc instructions exceed the maximum length of "
                     f"{DOC_INSTRUCTIONS_MAX_CHARS} characters."}),
        ({"instructions": "a\x00b", "expected_instructions_version": 0},
         {"error": "invalid_instructions",
          "message": "Doc instructions cannot contain NUL characters."}),
        ({"instructions": 5, "expected_instructions_version": 0}, NOT_A_STRING),
        ({"instructions": True, "expected_instructions_version": 0}, NOT_A_STRING),
        ({"instructions": ["a"], "expected_instructions_version": 0}, NOT_A_STRING),
        ({"instructions": None, "expected_instructions_version": 0}, NOT_A_STRING),
        ({"expected_instructions_version": 0}, NOT_A_STRING),
        ({}, NOT_A_STRING),
        (None, NOT_A_STRING),
        ({"instructions": "A rule."}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": None}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": True}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": False}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": -1}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": "0"}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": 0.0}, BAD_VERSION),
        ({"instructions": "A rule.", "expected_instructions_version": 2**63}, BAD_VERSION),
    ])
    def test_invalid(self, env, body, error):
        doc = new_doc(env)
        before = snapshot(env, doc)
        c = client(env)
        url = f"/app/api/docs/{doc}/instructions"
        resp = c.put(url) if body is None else c.put(url, json=body)
        assert resp.status_code == 400
        assert detail(resp) == error
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_cap_counts_after_stripping(self, env):
        doc = new_doc(env)
        text_at_cap = "y" * DOC_INSTRUCTIONS_MAX_CHARS
        resp = self.put(env, doc, {
            "instructions": f"  {text_at_cap}\n", "expected_instructions_version": 0,
        })
        assert resp.status_code == 200
        assert resp.json()["instructions"] == text_at_cap


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


class TestMemories:
    def test_post_returns_memory_and_publishes(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])
        resp = client(env, "bob").post(
            f"/app/api/docs/{doc}/memories", json={"content": "  A fact.\nTwo lines.  "},
        )
        assert resp.status_code == 201
        [stored] = memories(doc)
        assert stored["source"] == f"ui:{uid(env, 'bob')}"
        assert stored["created_by"] == stored["updated_by"] == uid(env, "bob")
        assert resp.json() == public(stored, LABEL_PANEL)
        assert stored["content"] == "A fact.\nTwo lines."
        assert env.published == audience_events(env, doc, 0, 1)

    def test_put_returns_memory_and_publishes(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])
        memory = add_memory(env, doc, "Old fact.")
        env.published.clear()
        resp = client(env, "bob").put(
            f"/app/api/docs/{doc}/memories/{memory['id']}", json={"content": "New fact."},
        )
        assert resp.status_code == 200
        [stored] = memories(doc)
        assert stored["content"] == "New fact."
        assert stored["source"] == memory["source"]
        assert stored["updated_by"] == uid(env, "bob")
        assert resp.json() == public(stored, LABEL_PANEL)
        assert env.published == audience_events(env, doc, 0, 2)

    @pytest.mark.parametrize("same", ["A fact.", "\n A fact. "])
    def test_put_identical_text_is_a_no_op(self, env, same):
        doc = new_doc(env, shares=[("bob", "write")])
        memory = add_memory(env, doc, "A fact.")
        before = snapshot(env, doc)
        env.published.clear()
        resp = client(env).put(
            f"/app/api/docs/{doc}/memories/{memory['id']}", json={"content": same},
        )
        assert resp.status_code == 200
        assert resp.json() == public(memory, LABEL_YOU)
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_delete_publishes(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])
        keep = add_memory(env, doc, "Keep.")
        gone = add_memory(env, doc, "Gone.")
        env.published.clear()
        resp = client(env, "bob").delete(f"/app/api/docs/{doc}/memories/{gone['id']}")
        assert resp.status_code == 200
        assert resp.json() == {"success": True}
        assert memories(doc) == [keep]
        assert state(env, doc)["memory_count"] == 1
        assert env.published == audience_events(env, doc, 0, 3)

    @pytest.mark.parametrize("method", ["post", "put"])
    @pytest.mark.parametrize("body, message", [
        ({"content": ""}, "Memory content cannot be empty."),
        ({"content": " \n\t "}, "Memory content cannot be empty."),
        ({"content": "x" * (DOC_MEMORY_MAX_CHARS + 1)},
         f"Memory content exceeds the maximum length of {DOC_MEMORY_MAX_CHARS} characters."),
        ({"content": "a\x00b"}, "Memory content cannot contain NUL characters."),
        ({"content": 5}, "Memory content must be a string."),
        ({"content": ["a"]}, "Memory content must be a string."),
        ({"content": None}, "Memory content must be a string."),
        ({}, "Memory content must be a string."),
        (None, "Memory content must be a string."),
    ])
    def test_invalid(self, env, method, body, message):
        doc = new_doc(env)
        memory = add_memory(env, doc, "A fact.")
        before = snapshot(env, doc)
        env.published.clear()
        c = client(env)
        url = (
            f"/app/api/docs/{doc}/memories" if method == "post"
            else f"/app/api/docs/{doc}/memories/{memory['id']}"
        )
        resp = getattr(c, method)(url) if body is None else getattr(c, method)(url, json=body)
        assert resp.status_code == 400
        assert detail(resp) == {"error": "invalid_memory", "message": message}
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_content_at_cap(self, env):
        doc = new_doc(env)
        resp = client(env).post(
            f"/app/api/docs/{doc}/memories",
            json={"content": " " + "z" * DOC_MEMORY_MAX_CHARS + " "},
        )
        assert resp.status_code == 201
        assert resp.json()["content"] == "z" * DOC_MEMORY_MAX_CHARS

    def test_duplicate_on_post(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        existing = add_memory(env, doc, "A fact.")
        before = snapshot(env, doc)
        env.published.clear()
        resp = client(env, "bob").post(
            f"/app/api/docs/{doc}/memories", json={"content": " A fact. "},
        )
        assert resp.status_code == 409
        assert detail(resp) == {
            "error": "duplicate_memory", "message": DUPLICATE_MESSAGE,
            "memory_id": existing["id"],
        }
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_duplicate_on_put(self, env):
        doc = new_doc(env)
        first = add_memory(env, doc, "First.")
        second = add_memory(env, doc, "Second.")
        before = snapshot(env, doc)
        env.published.clear()
        resp = client(env).put(
            f"/app/api/docs/{doc}/memories/{second['id']}", json={"content": "First."},
        )
        assert resp.status_code == 409
        assert detail(resp) == {
            "error": "duplicate_memory", "message": DUPLICATE_MESSAGE,
            "memory_id": first["id"],
        }
        assert snapshot(env, doc) == before
        assert env.published == []

    def test_cap_reached(self, env):
        doc = new_doc(env)
        for i in range(DOC_MAX_MEMORIES):
            add_memory(env, doc, f"Fact {i}.")
        before = snapshot(env, doc)
        env.published.clear()
        resp = client(env).post(f"/app/api/docs/{doc}/memories", json={"content": "One more."})
        assert resp.status_code == 409
        assert detail(resp) == CAP_REACHED
        assert snapshot(env, doc) == before
        assert env.published == []
        # An edit still works at the cap.
        last = memories(doc)[0]
        resp = client(env).put(
            f"/app/api/docs/{doc}/memories/{last['id']}", json={"content": "Edited."},
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("method", ["put", "delete"])
    def test_memory_not_found(self, env, method):
        doc = new_doc(env)
        add_memory(env, doc, "A fact.")
        other_doc = new_doc(env)
        foreign = add_memory(env, other_doc, "Another doc's fact.")
        before, other_before = snapshot(env, doc), snapshot(env, other_doc)
        env.published.clear()
        c = client(env)
        for memory_id in (str(uuid.uuid4()), "nope", foreign["id"]):
            url = f"/app/api/docs/{doc}/memories/{memory_id}"
            resp = (
                c.put(url, json={"content": "Changed."}) if method == "put"
                else c.delete(url)
            )
            assert resp.status_code == 404, memory_id
            assert detail(resp) == MEMORY_NOT_FOUND
        assert snapshot(env, doc) == before
        assert snapshot(env, other_doc) == other_before
        assert env.published == []


# ---------------------------------------------------------------------------
# Events and the body's concurrency token
# ---------------------------------------------------------------------------


class TestEventsAndBody:
    def test_each_write_one_event_body_untouched(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])
        _sql(
            env,
            "UPDATE docs SET updated_at = '2026-01-02 03:04:05.000000', "
            "last_write_source = :s WHERE id = :d",
            s=f"ui:{uid(env, 'bob')}", d=doc,
        )
        body_before = _run(env.doc_store.get_doc(doc, with_shares=False))
        alice, bob = client(env), client(env, "bob")
        base = f"/app/api/docs/{doc}"
        steps = [
            (lambda: alice.put(f"{base}/instructions",
                               json={"instructions": "R.", "expected_instructions_version": 0}),
             (1, 0)),
            (lambda: bob.post(f"{base}/memories", json={"content": "M."}), (1, 1)),
        ]
        for request, (iv, mv) in steps:
            env.published.clear()
            assert request().status_code in (200, 201)
            assert env.published == audience_events(env, doc, iv, mv)
        memory_id = memories(doc)[0]["id"]
        for request, (iv, mv) in [
            (lambda: bob.put(f"{base}/memories/{memory_id}", json={"content": "N."}), (1, 2)),
            (lambda: alice.delete(f"{base}/memories/{memory_id}"), (1, 3)),
            (lambda: alice.put(f"{base}/instructions",
                               json={"instructions": "", "expected_instructions_version": 1}),
             (2, 3)),
        ]:
            env.published.clear()
            assert request().status_code == 200
            assert env.published == audience_events(env, doc, iv, mv)
        env.published.clear()
        assert alice.get(f"{base}/notes").status_code == 200
        assert env.published == []
        body_after = _run(env.doc_store.get_doc(doc, with_shares=False))
        assert body_after["updated_at"] == body_before["updated_at"]
        assert body_after["last_write_source"] == f"ui:{uid(env, 'bob')}"
        assert {k: v for k, v in body_after.items()
                if k not in ("has_instructions", "memory_count",
                             "instructions_version", "memories_version")} == {
            k: v for k, v in body_before.items()
            if k not in ("has_instructions", "memory_count",
                         "instructions_version", "memories_version")}

    def test_everyone_share_reaches_connected_users(self, env, monkeypatch):
        from chat.realtime import bus

        doc = new_doc(env, shares=[(None, "write")])
        bob, carol = uid(env, "bob"), uid(env, "carol")
        monkeypatch.setattr(bus, "connected_user_ids", lambda: [bob, carol])
        resp = client(env, "dave").post(f"/app/api/docs/{doc}/memories", json={"content": "M."})
        assert resp.status_code == 201
        event = notes_changed(doc, 0, 1)
        assert env.published == [(uid(env), event), (bob, event), (carol, event)]
