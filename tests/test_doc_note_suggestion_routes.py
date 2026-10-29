"""Doc note suggestion review routes (chat/docs/note_suggestion_routes.py)
and the ``pending_doc_note_suggestions`` key of ``GET /conversations/{id}``,
phase A6 of spec 00011 (docs/architecture/doc-notes.md, Review).

Runs against the isolated ``docs_env`` of tests/test_docs_service.py with
the ``doc_notes`` gate open (``env``; foreign keys are on, so every row
gets a real ``conversations`` row), through a bare FastAPI app holding the
router with the auth dependency overridden. Suggestion rows are made
directly with ``doc_note_suggestion_store.create_suggestion()``, so the
tests never depend on the registration checks of phase A4. Expected
payloads are literal.

Covers:

1. Auth: every route answers 401 without credentials.
2. No gate: with ``doc_notes`` closed, and with ``docs`` closed, both lists,
   accept and dismiss still work.
3. ``GET /docs/{id}/note-suggestions``: the caller's own pending rows for
   the doc, oldest first (never another user's, a resolved one or another
   doc's), with the review fields and ``note_kind``, never ``kind``; 404
   ``doc_not_found`` byte-identical for a missing id, a doc never shared,
   a revoked recipient with a pending row and a deleted doc, and equal to
   ``GET /docs/{id}``'s body.
4. ``GET /conversations/{id}/doc-note-suggestions``: oldest first with
   ``pending_count``, only that conversation's pending rows; 404 for a
   missing and a foreign conversation; a revoked recipient's rows stay
   listed with the doc's fields blanked; ``instructions_moved`` false, then
   true after the owner saves the field.
5. Accept: a memory by the owner and by a write-share recipient (label,
   counters, the events the service sent and no more, no editor id or raw
   source anywhere), the content override, the labels of a replaced Notes
   panel memory per viewer, instructions with the default version and the
   ``""`` override; every refusal (404, 409 resolved, 403 forbidden after a
   revoke, a downgrade and for a non-owner's instructions row, the FLAT 409
   ``stale_instructions`` then a successful re-accept, 400
   ``invalid_memory`` / ``invalid_instructions``, 409
   ``memories_cap_reached``, 409 ``duplicate_memory`` with ``memory_id``)
   leaves the row pending.
6. Dismiss: the owner, a read-share and a revoked recipient; 404 / 409.
7. ``GET /conversations/{id}`` carries ``pending_doc_note_suggestions``:
   0 without rows, that conversation's pending count otherwise, also with
   the gate closed.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_MEMORY_MAX_CHARS,
    doc_not_found_message,
)
from db import doc_memory_store
from db import doc_note_suggestion_store as store
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    docs_env,
    seed_doc,
)

SUGGESTED_LABEL = "Suggested in a conversation"
NOT_FOUND = {"detail": {"error": "suggestion_not_found", "message": "Suggestion not found."}}
RESOLVED = {
    "detail": {
        "error": "suggestion_resolved",
        "message": "This suggestion was already accepted or dismissed.",
    },
}
FORBIDDEN_MEMORY = {
    "detail": {
        "error": "forbidden",
        "message": (
            "You can no longer edit this doc, so you cannot change its memories. "
            "Dismiss the suggestion instead."
        ),
    },
}
FORBIDDEN_INSTRUCTIONS = {
    "detail": {
        "error": "forbidden",
        "message": "Only the doc's owner can change its instructions.",
    },
}
STALE_MESSAGE = (
    "The doc's instructions changed since this suggestion was shown. Review "
    "it against the current text and accept again."
)
# Keys that must never leave the server in a review response.
PRIVATE_KEYS = {"created_by", "updated_by", "source", "instructions_updated_by", "kind"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(request):
    """``docs_env`` with the ``doc_notes`` gate open for everyone."""
    e = request.getfixturevalue("docs_env")
    e.fg.set_feature_enabled(e.fg.FEATURE_DOC_NOTES, True)
    return e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def uid(env, who="alice"):
    return env.users[who]["id"]


def client(env, who="alice"):
    """The router on a bare app, signed in as ``who`` (None: no override,
    so the real cookie / API-key dependency runs)."""
    from chat.docs import note_suggestion_routes as routes

    app = FastAPI()
    app.include_router(routes.router)
    if who is not None:
        user = env.users[who]

        async def _current():
            return user

        app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def conversation(env, who="alice"):
    """A new conversation of ``who`` (row + directory); its id."""
    from db import conversation_store

    cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(uid(env, who), cid, datetime.now(timezone.utc)))
    (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    return cid


def new_doc(env, owner="alice", *, shares=()):
    return seed_doc(env, title=f"Doc {uuid.uuid4().hex[:8]}", owner=owner, shares=shares)


def state(env, doc_id):
    return _run(env.doc_store.get_notes_state(doc_id))


def make_row(env, doc_id, cid, kind="memory", content="A fact.", who="alice", *,
             base=None, replaces=None, reasoning=None):
    """A pending suggestion written straight to the store; an instructions
    row is written against the field's current version unless ``base``."""
    if kind == "instructions" and base is None:
        base = state(env, doc_id)["instructions_version"]
    row, _superseded = _run(store.create_suggestion(
        uid(env, who), doc_id, cid, kind, content,
        reasoning=reasoning, base_instructions_version=base, replaces_memory_id=replaces,
    ))
    return row


def get_row(env, sid, who="alice"):
    return _run(store.get_suggestion(uid(env, who), sid))


def set_instructions(env, doc_id, text, who="alice"):
    return _run(env.doc_store.update_doc_instructions(
        doc_id, text, expected_version=state(env, doc_id)["instructions_version"],
        user_id=uid(env, who),
    ))


def add_memory(env, doc_id, text, who="alice"):
    return _run(doc_memory_store.create_doc_memory(
        doc_id, text, source=doc_memory_store.ui_source(uid(env, who)),
        user_id=uid(env, who),
    ))


def memories(doc_id):
    return _run(doc_memory_store.list_doc_memories(doc_id))


def share_id(env, doc_id, who):
    [share] = [
        s for s in _run(env.doc_store.list_shares(doc_id))
        if s["user_id"] == uid(env, who)
    ]
    return share["id"]


def revoke(env, doc_id, who):
    _run(env.doc_store.remove_share(doc_id, share_id(env, doc_id, who)))


def listed(row, doc_title, *, replaces_content=None, current_instructions=None,
           instructions_moved=False):
    """A pending store row as the review lists send it."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "doc_id": row["doc_id"],
        "conversation_id": row["conversation_id"],
        "note_kind": row["kind"],
        "content": row["content"],
        "reasoning": row["reasoning"],
        "base_instructions_version": row["base_instructions_version"],
        "replaces_memory_id": row["replaces_memory_id"],
        "status": "pending",
        "memory_id": None,
        "created_at": row["created_at"],
        "resolved_at": None,
        "doc_title": doc_title,
        "replaces_content": replaces_content,
        "current_instructions": current_instructions,
        "instructions_moved": instructions_moved,
    }


def resolved(env, sid, who="alice"):
    """A resolved row as accept / dismiss send it (no review fields)."""
    row = get_row(env, sid, who)
    assert row["status"] in ("accepted", "dismissed") and row["resolved_at"]
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "doc_id": row["doc_id"],
        "conversation_id": row["conversation_id"],
        "note_kind": row["kind"],
        "content": row["content"],
        "reasoning": row["reasoning"],
        "base_instructions_version": row["base_instructions_version"],
        "replaces_memory_id": row["replaces_memory_id"],
        "status": row["status"],
        "memory_id": row["memory_id"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


def all_keys(value):
    """Every dict key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in all_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in all_keys(v)}
    return set()


def doc_list(env, doc_id, who="alice"):
    return client(env, who).get(f"/app/api/docs/{doc_id}/note-suggestions")


def conv_list(env, cid, who="alice"):
    return client(env, who).get(f"/app/api/conversations/{cid}/doc-note-suggestions")


def accept(env, sid, who="alice", body=None):
    c = client(env, who)
    url = f"/app/api/doc-note-suggestions/{sid}/accept"
    return c.post(url) if body is None else c.post(url, json=body)


def dismiss(env, sid, who="alice"):
    return client(env, who).post(f"/app/api/doc-note-suggestions/{sid}/dismiss")


def changed(cid, pending_count, total_pending):
    return {
        "type": "doc_note_suggestions_changed",
        "conversation_id": cid,
        "pending_count": pending_count,
        "total_pending": total_pending,
    }


def notes_changed(doc_id, instructions_version, memories_version):
    return {
        "type": "doc_notes_changed",
        "doc_id": doc_id,
        "instructions_version": instructions_version,
        "memories_version": memories_version,
    }


# ---------------------------------------------------------------------------
# 1. Auth
# ---------------------------------------------------------------------------


ROUTES = [
    ("get", "/app/api/docs/{id}/note-suggestions"),
    ("get", "/app/api/conversations/{id}/doc-note-suggestions"),
    ("post", "/app/api/doc-note-suggestions/{id}/accept"),
    ("post", "/app/api/doc-note-suggestions/{id}/dismiss"),
]


@pytest.mark.parametrize("method, path", ROUTES)
def test_every_route_needs_credentials(env, method, path):
    doc = new_doc(env)
    cid = conversation(env)
    sid = make_row(env, doc["id"], cid)["id"]
    target = {"docs": doc["id"], "conversations": cid}.get(path.split("/")[3], sid)
    resp = getattr(client(env, None), method)(path.format(id=target))
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "not_authenticated"
    assert get_row(env, sid)["status"] == "pending"


# ---------------------------------------------------------------------------
# 2. No gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate", ["FEATURE_DOC_NOTES", "FEATURE_DOCS"])
def test_every_route_works_with_a_gate_closed(env, gate):
    doc = new_doc(env)
    cid = conversation(env)
    first = make_row(env, doc["id"], cid, content="Fact one.")
    second = make_row(env, doc["id"], cid, content="Fact two.")
    env.fg.set_feature_enabled(getattr(env.fg, gate), False)
    assert not env.fg.doc_notes_enabled_for(env.users["alice"]["email"])

    expected = {
        "suggestions": [listed(first, doc["title"]), listed(second, doc["title"])],
        "pending_count": 2,
    }
    resp = doc_list(env, doc["id"])
    assert (resp.status_code, resp.json()) == (200, expected)
    resp = conv_list(env, cid)
    assert (resp.status_code, resp.json()) == (200, expected)

    resp = accept(env, first["id"])
    assert resp.status_code == 200
    assert resp.json()["memory"]["content"] == "Fact one."
    resp = dismiss(env, second["id"])
    assert (resp.status_code, resp.json()) == (200, {"suggestion": resolved(env, second["id"])})

    empty = {"suggestions": [], "pending_count": 0}
    assert doc_list(env, doc["id"]).json() == empty
    assert conv_list(env, cid).json() == empty
    assert [m["content"] for m in memories(doc["id"])] == ["Fact one."]


def test_hidden_equals_missing_with_the_docs_gate_closed(env):
    """The ungated doc route still answers a hidden doc like a missing one."""
    doc = new_doc(env, owner="bob")
    env.fg.set_feature_enabled(env.fg.FEATURE_DOCS, False)
    hidden = doc_list(env, doc["id"])
    missing_id = str(uuid.uuid4())
    missing = doc_list(env, missing_id)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == {
        "detail": {"error": "doc_not_found", "message": doc_not_found_message(doc["id"])},
    }
    assert missing.json() == {
        "detail": {"error": "doc_not_found", "message": doc_not_found_message(missing_id)},
    }


# ---------------------------------------------------------------------------
# 3. GET /docs/{id}/note-suggestions
# ---------------------------------------------------------------------------


class TestDocList:
    def test_own_pending_rows_of_this_doc_oldest_first(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        other = new_doc(env)
        doc_id = doc["id"]
        set_instructions(env, doc_id, "Rule A.")
        memory = add_memory(env, doc_id, "Old fact.")
        first_cid, second_cid = conversation(env), conversation(env)
        bob_cid = conversation(env, "bob")

        replacing = make_row(env, doc_id, first_cid, content="New fact.",
                             replaces=memory["id"], reasoning="Said twice.")
        make_row(env, other["id"], first_cid, content="Another doc's fact.")
        bobs = make_row(env, doc_id, bob_cid, content="Bob's fact.", who="bob")
        instructions = make_row(env, doc_id, second_cid, "instructions", "Rule A.\nRule B.")
        gone = make_row(env, doc_id, first_cid, content="Dismissed fact.")
        _run(store.dismiss_suggestion(uid(env), gone["id"]))
        plain = make_row(env, doc_id, first_cid, content="Plain fact.")

        resp = doc_list(env, doc_id)
        assert resp.status_code == 200
        assert resp.json() == {
            "suggestions": [
                listed(replacing, doc["title"], replaces_content="Old fact."),
                listed(instructions, doc["title"], current_instructions="Rule A."),
                listed(plain, doc["title"]),
            ],
            "pending_count": 3,
        }
        assert all("kind" not in row for row in resp.json()["suggestions"])
        assert replacing["reasoning"] == "Said twice."
        # Bob sees only his own row on the doc.
        assert doc_list(env, doc_id, "bob").json() == {
            "suggestions": [listed(bobs, doc["title"])], "pending_count": 1,
        }
        # Carol, who cannot see the doc, gets the not-found answer; the
        # other doc lists only its own row.
        assert doc_list(env, doc_id, "carol").status_code == 404
        assert [r["content"] for r in doc_list(env, other["id"]).json()["suggestions"]] == [
            "Another doc's fact.",
        ]

    def test_hidden_or_missing_doc_is_the_same_404(self, env):
        from chat.docs import routes as doc_routes

        doc = new_doc(env, shares=[("bob", "write")])
        doc_id = doc["id"]
        bob_cid = conversation(env, "bob")
        row = make_row(env, doc_id, bob_cid, content="Bob's fact.", who="bob")
        revoke(env, doc_id, "bob")

        expected = {"detail": {"error": "doc_not_found", "message": doc_not_found_message(doc_id)}}
        hidden = doc_list(env, doc_id, "bob")
        never_shared = doc_list(env, doc_id, "carol")
        assert (hidden.status_code, hidden.json()) == (404, expected)
        assert hidden.content == never_shared.content
        assert hidden.content == json.dumps(expected, separators=(",", ":")).encode()

        # GET /docs/{id} gives a hidden doc the very same body.
        app = FastAPI()
        app.include_router(doc_routes.router)

        async def _bob():
            return env.users["bob"]

        app.dependency_overrides[doc_routes.get_current_user_cookie_or_apikey_checked] = _bob
        docs_route = TestClient(app).get(f"/app/api/docs/{doc_id}")
        assert (docs_route.status_code, docs_route.content) == (404, hidden.content)

        # The row stays pending, listed blank in the conversation.
        assert get_row(env, row["id"], "bob")["status"] == "pending"
        assert conv_list(env, bob_cid, "bob").json()["suggestions"][0]["doc_title"] == ""

        # A deleted doc and a random id read the same.
        _run(env.doc_store.delete_doc(doc_id))
        assert doc_list(env, doc_id).content == hidden.content
        missing = str(uuid.uuid4())
        resp = doc_list(env, missing)
        assert (resp.status_code, resp.json()) == (
            404, {"detail": {"error": "doc_not_found", "message": doc_not_found_message(missing)}},
        )


# ---------------------------------------------------------------------------
# 4. GET /conversations/{id}/doc-note-suggestions
# ---------------------------------------------------------------------------


class TestDocListReadShare:
    def test_a_read_share_recipient_lists_their_own_rows(self, env):
        doc = new_doc(env, shares=[("bob", "read")])
        row = make_row(env, doc["id"], conversation(env, "bob"), content="Bob's.", who="bob")
        make_row(env, doc["id"], conversation(env), content="Alice's.")
        resp = doc_list(env, doc["id"], "bob")
        assert (resp.status_code, resp.json()) == (
            200, {"suggestions": [listed(row, doc["title"])], "pending_count": 1},
        )


class TestConversationList:
    def test_pending_rows_of_the_conversation_oldest_first(self, env):
        first_doc, second_doc = new_doc(env), new_doc(env)
        cid, other_cid = conversation(env), conversation(env)
        one = make_row(env, first_doc["id"], cid, content="Fact one.")
        make_row(env, first_doc["id"], other_cid, content="Elsewhere.")
        two = make_row(env, second_doc["id"], cid, "instructions", "Rule.")
        done = make_row(env, second_doc["id"], cid, content="Done.")
        _run(store.dismiss_suggestion(uid(env), done["id"]))
        three = make_row(env, first_doc["id"], cid, content="Fact three.")

        resp = conv_list(env, cid)
        assert resp.status_code == 200
        assert resp.json() == {
            "suggestions": [
                listed(one, first_doc["title"]),
                listed(two, second_doc["title"], current_instructions=""),
                listed(three, first_doc["title"]),
            ],
            "pending_count": 3,
        }
        assert conv_list(env, conversation(env)).json() == {"suggestions": [], "pending_count": 0}

    def test_missing_or_foreign_conversation_is_404(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        bob_cid = conversation(env, "bob")
        make_row(env, doc["id"], bob_cid, who="bob")
        for cid in (bob_cid, str(uuid.uuid4())):
            resp = conv_list(env, cid)
            assert (resp.status_code, resp.json()) == (404, {
                "detail": {
                    "error": "conversation_not_found",
                    "message": f"Conversation {cid} not found",
                },
            })

    def test_a_revoked_recipients_rows_stay_listed_blank(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        doc_id = doc["id"]
        set_instructions(env, doc_id, "The owner's rule.")
        memory = add_memory(env, doc_id, "Shared fact.")
        bob_cid = conversation(env, "bob")
        replacing = make_row(env, doc_id, bob_cid, content="Bob's fact.", who="bob",
                             replaces=memory["id"])
        # An instructions row of Bob's, written against version 0 (the
        # service would refuse it: Bob is not the owner).
        instructions = make_row(env, doc_id, bob_cid, "instructions", "Bob's rule.",
                                who="bob", base=0)
        assert conv_list(env, bob_cid, "bob").json() == {
            "suggestions": [
                listed(replacing, doc["title"], replaces_content="Shared fact."),
                listed(instructions, doc["title"],
                       current_instructions="The owner's rule.", instructions_moved=True),
            ],
            "pending_count": 2,
        }
        revoke(env, doc_id, "bob")
        assert conv_list(env, bob_cid, "bob").json() == {
            "suggestions": [listed(replacing, ""), listed(instructions, "")],
            "pending_count": 2,
        }

    def test_instructions_moved_after_the_owner_saves(self, env):
        doc = new_doc(env)
        doc_id = doc["id"]
        set_instructions(env, doc_id, "Rule A.")
        cid = conversation(env)
        row = make_row(env, doc_id, cid, "instructions", "Rule A.\nRule B.")
        assert row["base_instructions_version"] == 1
        assert conv_list(env, cid).json()["suggestions"] == [
            listed(row, doc["title"], current_instructions="Rule A."),
        ]
        set_instructions(env, doc_id, "Rule A, revised.")
        assert conv_list(env, cid).json()["suggestions"] == [
            listed(row, doc["title"], current_instructions="Rule A, revised.",
                   instructions_moved=True),
        ]


# ---------------------------------------------------------------------------
# 5. Accept
# ---------------------------------------------------------------------------


class TestAccept:
    def test_memory_by_the_owner(self, env):
        doc_id = new_doc(env)["id"]
        cid = conversation(env)
        sid = make_row(env, doc_id, cid)["id"]
        env.published.clear()
        resp = accept(env, sid)
        assert resp.status_code == 200
        [memory] = memories(doc_id)
        assert memory["source"] == f"suggestion:{sid}"
        assert resp.json() == {
            "suggestion": resolved(env, sid),
            "memory": {
                "id": memory["id"],
                "content": "A fact.",
                "source_label": SUGGESTED_LABEL,
                "created_at": memory["created_at"],
                "updated_at": memory["updated_at"],
            },
            "notes": {
                "doc_id": doc_id,
                "instructions_version": 0,
                "memories_version": 1,
                "has_instructions": False,
                "memory_count": 1,
            },
        }
        assert resp.json()["suggestion"]["memory_id"] == memory["id"]
        assert not all_keys(resp.json()) & PRIVATE_KEYS
        # The service's events, once each; the route adds none.
        assert env.published == [
            (uid(env), changed(cid, 0, 0)),
            (uid(env), notes_changed(doc_id, 0, 1)),
        ]

    def test_memory_by_a_write_share_recipient(self, env):
        doc_id = new_doc(env, shares=[("bob", "write")])["id"]
        sid = make_row(env, doc_id, conversation(env, "bob"), content="Bob's fact.", who="bob")["id"]
        resp = accept(env, sid, "bob")
        assert resp.status_code == 200
        [memory] = memories(doc_id)
        body = resp.json()
        assert body["suggestion"] == resolved(env, sid, "bob")
        assert body["memory"] == {
            "id": memory["id"],
            "content": "Bob's fact.",
            "source_label": SUGGESTED_LABEL,
            "created_at": memory["created_at"],
            "updated_at": memory["updated_at"],
        }
        assert body["notes"] == {
            "doc_id": doc_id, "instructions_version": 0, "memories_version": 1,
            "has_instructions": False, "memory_count": 1,
        }
        assert memory["created_by"] == uid(env, "bob")
        assert not all_keys(body) & PRIVATE_KEYS

    def test_content_override(self, env):
        doc_id = new_doc(env)["id"]
        sid = make_row(env, doc_id, conversation(env))["id"]
        resp = accept(env, sid, body={"content": "  An edited fact. "})
        assert resp.status_code == 200
        assert resp.json()["memory"]["content"] == "An edited fact."
        assert [m["content"] for m in memories(doc_id)] == ["An edited fact."]
        # The row keeps the proposed text.
        assert resp.json()["suggestion"]["content"] == "A fact."

    def test_a_replaced_panel_memory_keeps_its_label_per_viewer(self, env):
        doc_id = new_doc(env, shares=[("bob", "write")])["id"]
        by_bob = add_memory(env, doc_id, "Bob's panel fact.", who="bob")
        by_alice = add_memory(env, doc_id, "Alice's panel fact.")
        alice_cid, bob_cid = conversation(env), conversation(env, "bob")
        labels = []
        for who, cid, memory in (
            ("alice", alice_cid, by_bob),
            ("alice", alice_cid, by_alice),
            ("bob", bob_cid, by_alice),
        ):
            sid = make_row(env, doc_id, cid, content=f"Rewritten by {who}: {memory['id']}",
                           who=who, replaces=memory["id"])["id"]
            resp = accept(env, sid, who)
            assert resp.status_code == 200
            assert resp.json()["memory"]["id"] == memory["id"]
            labels.append(resp.json()["memory"]["source_label"])
        # The owner learns who wrote it (bob has no name: his email); bob
        # learns nothing.
        assert labels == [
            "Added by bob@example.com", "Added by you", "Added in the Notes panel",
        ]

    def test_instructions_default_and_clearing_override(self, env):
        doc_id = new_doc(env)["id"]
        set_instructions(env, doc_id, "Rule A.")
        cid = conversation(env)
        sid = make_row(env, doc_id, cid, "instructions", "Rule A.\nRule B.")["id"]
        resp = accept(env, sid)
        assert (resp.status_code, resp.json()) == (200, {
            "suggestion": resolved(env, sid),
            "memory": None,
            "notes": {
                "doc_id": doc_id, "instructions_version": 2, "memories_version": 0,
                "has_instructions": True, "memory_count": 0,
            },
        })
        assert state(env, doc_id)["instructions"] == "Rule A.\nRule B."
        assert not all_keys(resp.json()) & PRIVATE_KEYS
        # A reviewer's empty override clears the field.
        sid = make_row(env, doc_id, cid, "instructions", "Rule C.")["id"]
        resp = accept(env, sid, body={"content": ""})
        assert resp.status_code == 200
        assert resp.json()["notes"] == {
            "doc_id": doc_id, "instructions_version": 3, "memories_version": 0,
            "has_instructions": False, "memory_count": 0,
        }
        assert state(env, doc_id)["instructions"] == ""

    def test_not_found(self, env):
        doc_id = new_doc(env, shares=[("bob", "write")])["id"]
        bobs = make_row(env, doc_id, conversation(env, "bob"), who="bob")["id"]
        for sid in (bobs, str(uuid.uuid4())):
            resp = accept(env, sid)
            assert (resp.status_code, resp.json()) == (404, NOT_FOUND)
        assert get_row(env, bobs, "bob")["status"] == "pending"

    def test_resolved(self, env):
        doc_id = new_doc(env)["id"]
        cid = conversation(env)
        accepted = make_row(env, doc_id, cid, content="Fact one.")["id"]
        dismissed = make_row(env, doc_id, cid, content="Fact two.")["id"]
        assert accept(env, accepted).status_code == 200
        assert dismiss(env, dismissed).status_code == 200
        for sid in (accepted, dismissed):
            resp = accept(env, sid)
            assert (resp.status_code, resp.json()) == (409, RESOLVED)
        assert [m["content"] for m in memories(doc_id)] == ["Fact one."]

    def test_forbidden_after_a_downgrade_and_a_revoke(self, env):
        doc_id = new_doc(env, shares=[("bob", "write")])["id"]
        sid = make_row(env, doc_id, conversation(env, "bob"), content="Bob's fact.", who="bob")["id"]
        _run(env.doc_store.add_share(doc_id, uid(env, "bob"), "read"))
        resp = accept(env, sid, "bob")
        assert (resp.status_code, resp.json()) == (403, FORBIDDEN_MEMORY)
        revoke(env, doc_id, "bob")
        resp = accept(env, sid, "bob")
        assert (resp.status_code, resp.json()) == (403, FORBIDDEN_MEMORY)
        assert get_row(env, sid, "bob")["status"] == "pending"
        assert memories(doc_id) == []

    def test_forbidden_for_a_non_owners_instructions_row(self, env):
        doc_id = new_doc(env, shares=[("bob", "write")])["id"]
        sid = make_row(env, doc_id, conversation(env, "bob"), "instructions", "Bob's rule.",
                       who="bob")["id"]
        resp = accept(env, sid, "bob")
        assert (resp.status_code, resp.json()) == (403, FORBIDDEN_INSTRUCTIONS)
        assert get_row(env, sid, "bob")["status"] == "pending"
        assert state(env, doc_id)["instructions"] == ""

    def test_stale_instructions_then_accept_again(self, env):
        doc_id = new_doc(env)["id"]
        set_instructions(env, doc_id, "Rule A.")
        sid = make_row(env, doc_id, conversation(env), "instructions", "Rule A.\nRule B.")["id"]
        set_instructions(env, doc_id, "Rule A, revised.")
        env.published.clear()
        stale = {
            "error": "stale_instructions",
            "message": STALE_MESSAGE,
            "current": {"instructions": "Rule A, revised.", "instructions_version": 2},
        }
        # The default (the version the proposal was written against) and an
        # explicit old version are refused alike, with the FLAT body.
        for body in (None, {"expected_instructions_version": 1}):
            resp = accept(env, sid, body=body)
            assert (resp.status_code, resp.json()) == (409, stale)
        assert get_row(env, sid)["status"] == "pending"
        assert state(env, doc_id)["instructions"] == "Rule A, revised."
        assert env.published == []
        resp = accept(env, sid, body={"expected_instructions_version": 2})
        assert resp.status_code == 200
        assert resp.json()["notes"]["instructions_version"] == 3
        assert state(env, doc_id)["instructions"] == "Rule A.\nRule B."

    def test_invalid_memory(self, env):
        doc_id = new_doc(env)["id"]
        sid = make_row(env, doc_id, conversation(env))["id"]
        for content, message in (
            ("", "Memory content cannot be empty."),
            ("x" * (DOC_MEMORY_MAX_CHARS + 1),
             f"Memory content exceeds the maximum length of {DOC_MEMORY_MAX_CHARS} characters."),
            (5, "Memory content must be a string."),
        ):
            resp = accept(env, sid, body={"content": content})
            assert (resp.status_code, resp.json()) == (400, {
                "detail": {"error": "invalid_memory", "message": message},
            })
        assert get_row(env, sid)["status"] == "pending"
        assert memories(doc_id) == []

    def test_invalid_instructions(self, env):
        doc_id = new_doc(env)["id"]
        sid = make_row(env, doc_id, conversation(env), "instructions", "A rule.")["id"]
        resp = accept(env, sid, body={"content": "x" * (DOC_INSTRUCTIONS_MAX_CHARS + 1)})
        assert (resp.status_code, resp.json()) == (400, {
            "detail": {
                "error": "invalid_instructions",
                "message": (
                    "Doc instructions exceed the maximum length of "
                    f"{DOC_INSTRUCTIONS_MAX_CHARS} characters."
                ),
            },
        })
        for version in ("3", -1, True):
            resp = accept(env, sid, body={"expected_instructions_version": version})
            assert (resp.status_code, resp.json()) == (400, {
                "detail": {
                    "error": "invalid_instructions",
                    "message": "expected_instructions_version must be a non-negative integer.",
                },
            })
        assert get_row(env, sid)["status"] == "pending"
        assert state(env, doc_id)["instructions"] == ""

    def test_memories_cap_reached(self, env):
        doc_id = new_doc(env)["id"]
        sid = make_row(env, doc_id, conversation(env), content="One too many.")["id"]
        for i in range(DOC_MAX_MEMORIES):
            add_memory(env, doc_id, f"Fact {i}.")
        resp = accept(env, sid)
        assert (resp.status_code, resp.json()) == (409, {
            "detail": {
                "error": "memories_cap_reached",
                "message": (
                    f"This doc already has the maximum of {DOC_MAX_MEMORIES} memories. "
                    "Delete or edit one instead."
                ),
            },
        })
        assert get_row(env, sid)["status"] == "pending"
        assert len(memories(doc_id)) == DOC_MAX_MEMORIES

    def test_duplicate_memory(self, env):
        doc_id = new_doc(env)["id"]
        sid = make_row(env, doc_id, conversation(env), content="Same fact.")["id"]
        duplicate = add_memory(env, doc_id, "Same fact.")
        resp = accept(env, sid)
        assert (resp.status_code, resp.json()) == (409, {
            "detail": {
                "error": "duplicate_memory",
                "message": "This doc already has a memory with exactly this text.",
                "memory_id": duplicate["id"],
            },
        })
        assert get_row(env, sid)["status"] == "pending"
        # A different override lands.
        resp = accept(env, sid, body={"content": "Same fact, reworded."})
        assert (resp.status_code, resp.json()["memory"]["content"]) == (200, "Same fact, reworded.")


# ---------------------------------------------------------------------------
# 6. Dismiss
# ---------------------------------------------------------------------------


class TestAcceptEdges:
    def test_null_content_is_no_override(self, env):
        doc = new_doc(env)
        row = make_row(env, doc["id"], conversation(env), content="As proposed.")
        resp = accept(env, row["id"], body={"content": None})
        assert resp.status_code == 200
        assert resp.json()["memory"]["content"] == "As proposed."

    def test_non_string_instructions_override(self, env):
        doc = new_doc(env)
        row = make_row(env, doc["id"], conversation(env), kind="instructions", content="Rule.")
        resp = accept(env, row["id"], body={"content": 5})
        assert (resp.status_code, resp.json()) == (400, {
            "detail": {
                "error": "invalid_instructions",
                "message": "Doc instructions must be a string.",
            },
        })
        assert get_row(env, row["id"])["status"] == "pending"

    def test_a_memory_accept_ignores_the_instructions_version(self, env):
        doc = new_doc(env)
        row = make_row(env, doc["id"], conversation(env), content="A fact.")
        resp = accept(env, row["id"], body={"expected_instructions_version": "junk"})
        assert resp.status_code == 200
        assert resp.json()["memory"]["content"] == "A fact."

    def test_a_failed_label_lookup_never_fails_a_landed_accept(self, env, monkeypatch):
        from chat.docs import note_suggestion_routes as routes

        def _boom(*_args, **_kwargs):
            raise RuntimeError("lookup failed")

        doc = new_doc(env)
        row = make_row(env, doc["id"], conversation(env), content="A fact.")
        # Runs only in the route's post-accept labelling, after the commit.
        monkeypatch.setattr(routes.note_payloads, "label_user_ids", _boom)
        resp = accept(env, row["id"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory"]["content"] == "A fact."
        assert body["memory"]["source_label"] == SUGGESTED_LABEL
        assert get_row(env, row["id"])["status"] == "accepted"


class TestDismiss:
    def test_by_the_owner(self, env):
        doc_id = new_doc(env)["id"]
        cid = conversation(env)
        sid = make_row(env, doc_id, cid, "instructions", "A rule.")["id"]
        env.published.clear()
        resp = dismiss(env, sid)
        assert (resp.status_code, resp.json()) == (200, {"suggestion": resolved(env, sid)})
        assert resp.json()["suggestion"]["status"] == "dismissed"
        assert not all_keys(resp.json()) & PRIVATE_KEYS
        assert state(env, doc_id)["instructions"] == ""
        assert env.published == [(uid(env), changed(cid, 0, 0))]

    @pytest.mark.parametrize("permission", ["read", "revoked"])
    def test_needs_no_edit_access(self, env, permission):
        doc_id = new_doc(env, shares=[("bob", "read" if permission == "read" else "write")])["id"]
        sid = make_row(env, doc_id, conversation(env, "bob"), content="Bob's fact.", who="bob")["id"]
        if permission == "revoked":
            revoke(env, doc_id, "bob")
        resp = dismiss(env, sid, "bob")
        assert (resp.status_code, resp.json()) == (200, {"suggestion": resolved(env, sid, "bob")})
        assert memories(doc_id) == []

    def test_not_found_and_resolved(self, env):
        doc_id = new_doc(env, shares=[("bob", "write")])["id"]
        cid = conversation(env)
        bobs = make_row(env, doc_id, conversation(env, "bob"), who="bob")["id"]
        for sid in (bobs, str(uuid.uuid4())):
            resp = dismiss(env, sid)
            assert (resp.status_code, resp.json()) == (404, NOT_FOUND)
        assert get_row(env, bobs, "bob")["status"] == "pending"
        accepted = make_row(env, doc_id, cid, content="Fact one.")["id"]
        dismissed = make_row(env, doc_id, cid, content="Fact two.")["id"]
        assert accept(env, accepted).status_code == 200
        assert dismiss(env, dismissed).status_code == 200
        for sid in (accepted, dismissed):
            resp = dismiss(env, sid)
            assert (resp.status_code, resp.json()) == (409, RESOLVED)


# ---------------------------------------------------------------------------
# 7. GET /conversations/{id}
# ---------------------------------------------------------------------------


@pytest.fixture()
def conv_route(env, monkeypatch):
    """``GET /conversations/{id}`` (chat/routes/conversations.py) on a bare
    app, signed in as alice: the real handler, with the stores it reads
    beyond ``docs_env``'s pointed at the same isolated database. Returns a
    ``get(conversation_id)`` callable."""
    import chat.routes as chat_routes
    from db import (
        memory_suggestion_store,
        project_memory_suggestion_store,
        tool_wait_handle_store,
    )

    session_local = env.doc_store.AsyncSessionLocal
    for mod in (memory_suggestion_store, project_memory_suggestion_store,
                tool_wait_handle_store):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    app = FastAPI()
    app.include_router(chat_routes.router)
    user = env.users["alice"]

    async def _current():
        return user

    app.dependency_overrides[
        chat_routes.conversations.get_current_user_cookie_or_apikey_checked
    ] = _current
    test_client = TestClient(app)

    def get(conversation_id):
        resp = test_client.get(f"/app/api/conversations/{conversation_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    return get


def chat_conversation(env, who="alice"):
    """A conversation made the way the app makes one (row + chat_history.json)."""
    from chat.storage import ChatStorage

    cid, _title = _run(ChatStorage.create_conversation(uid(env, who)))
    return cid


class TestGetConversation:
    def test_pending_doc_note_suggestions(self, env, conv_route):
        doc = new_doc(env, shares=[("bob", "write")])
        cid, other_cid = chat_conversation(env), chat_conversation(env)
        body = conv_route(cid)
        assert body["pending_doc_note_suggestions"] == 0
        # Beside its twins.
        assert (body["pending_memory_suggestions"],
                body["pending_project_memory_suggestions"]) == (0, 0)

        rows = [make_row(env, doc["id"], cid, content=f"Fact {i}.") for i in range(3)]
        make_row(env, doc["id"], cid, "instructions", "A rule.")
        make_row(env, doc["id"], other_cid, content="Elsewhere.")
        make_row(env, doc["id"], chat_conversation(env, "bob"), content="Bob's.", who="bob")
        _run(store.dismiss_suggestion(uid(env), rows[0]["id"]))
        assert conv_route(cid)["pending_doc_note_suggestions"] == 3
        assert conv_route(other_cid)["pending_doc_note_suggestions"] == 1

        # Never gated.
        env.fg.set_feature_enabled(env.fg.FEATURE_DOC_NOTES, False)
        assert conv_route(cid)["pending_doc_note_suggestions"] == 3
