"""Doc note suggestion store (db/doc_note_suggestion_store.py) against the
isolated database of ``docs_env`` (tests/test_docs_service.py), whose
connections turn foreign keys on.

Covers the store of phase A4 of spec 00011 (docs/architecture/doc-notes-review.md):

1. ``create_suggestion`` for both kinds: the row dict, stored values,
   content normalized per kind with character caps, and every ValueError
   refusal inserting nothing.
2. The supersede rule: a newer instructions suggestion dismisses the same
   user's older pending one on the doc, across conversations, and nothing
   else.
3. Lists and counts per conversation, per doc and per user, oldest first;
   ``get_suggestion`` and ``find_pending_with_content`` scoping; the
   resolved keyset page; pending summaries; pending pairs for docs.
4. ``accept_suggestion``: memory insert, override, replace in place, the
   replaced-memory-gone fallback, duplicate and cap refusals; instructions
   write, the stale token, override and clear; the claim taken first and
   undone on every refusal; concurrent accepts.
5. ``dismiss_suggestion``, scoping and resolved rows.
6. Cascades: doc delete, conversation delete, memory delete; the admin
   report rows.
7. ``create_suggestion`` takes the doc's write lock first (the
   replaced-memory check runs inside the transaction) and reports a doc or
   conversation gone as ``SuggestionTargetGoneError``; concurrent
   instructions creates leave one pending row; the claim precedes both
   notes reads and the instructions write; dismiss losing its claim;
   no-op accepts resolve the row without a notes change.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, update

from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_MEMORY_MAX_CHARS,
)
from db import doc_memory_store
from db import doc_note_suggestion_store as store
from db.doc_memory_store import (
    DuplicateMemoryError,
    InvalidMemoryError,
    MemoriesCapReachedError,
)
from db.doc_store import InvalidInstructionsError, StaleInstructionsError
from db.memory_suggestion_store import decode_resolved_cursor, encode_resolved_cursor
from db.models import Conversation, Doc, DocNoteSuggestion
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    docs_env,
    seed_doc,
)

ROW_KEYS = {
    "id", "user_id", "doc_id", "conversation_id", "kind", "content",
    "reasoning", "base_instructions_version", "replaces_memory_id", "status",
    "memory_id", "created_at", "resolved_at",
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(request):
    """``docs_env`` (tests/test_docs_service.py) as is."""
    return request.getfixturevalue("docs_env")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid(env, who):
    return env.users[who]["id"]


def _conv(env, who="alice"):
    from db import conversation_store

    cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(
        _uid(env, who), cid, datetime.now(timezone.utc), project_id=None,
    ))
    return cid


def _doc(env, owner="alice"):
    return seed_doc(env, title=f"Doc {uuid.uuid4().hex[:8]}", owner=owner)["id"]


def _mem_suggest(env, doc_id, cid, content, who="alice", **kw):
    row, superseded = _run(store.create_suggestion(
        _uid(env, who), doc_id, cid, "memory", content, **kw,
    ))
    assert superseded == []
    return row


def _instr_suggest(env, doc_id, cid, content, who="alice", base=0, **kw):
    return _run(store.create_suggestion(
        _uid(env, who), doc_id, cid, "instructions", content,
        base_instructions_version=base, **kw,
    ))


def _memory(env, doc_id, content, who="alice"):
    return _run(doc_memory_store.create_doc_memory(
        doc_id, content,
        source=doc_memory_store.ui_source(_uid(env, who)), user_id=_uid(env, who),
    ))


def _state(env, doc_id):
    return _run(env.doc_store.get_notes_state(doc_id))


def _get(env, row, who="alice"):
    return _run(store.get_suggestion(_uid(env, who), row["id"]))


def _ids(rows):
    return [r["id"] for r in rows]


def _session(env):
    return env.doc_store.AsyncSessionLocal()


def _row_count(env):
    async def go():
        async with _session(env) as db:
            return (await db.execute(
                select(func.count()).select_from(DocNoteSuggestion)
            )).scalar_one()
    return _run(go())


def _set_row(env, suggestion_id, **values):
    async def go():
        async with _session(env) as db:
            await db.execute(
                update(DocNoteSuggestion)
                .where(DocNoteSuggestion.id == suggestion_id)
                .values(**values)
            )
            await db.commit()
    _run(go())


def _assert_pending(env, row, who="alice"):
    fresh = _get(env, row, who)
    assert fresh["status"] == "pending"
    assert fresh["resolved_at"] is None
    assert fresh["memory_id"] is None
    return fresh


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_normalize_suggestion_content_dispatches_per_kind():
    assert store.normalize_suggestion_content("memory", "  a fact\n ") == "a fact"
    assert store.normalize_suggestion_content("instructions", "  ") == ""
    with pytest.raises(InvalidMemoryError):
        store.normalize_suggestion_content("memory", "  ")
    with pytest.raises(ValueError, match="kind"):
        store.normalize_suggestion_content("note", "x")


def test_create_memory_row_shape(env):
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(
        env, doc_id, cid, "  The Q3 numbers come\nfrom finance.  ",
        reasoning="  user   said\n so  ",
    )
    assert set(row) == ROW_KEYS
    assert row["user_id"] == _uid(env, "alice")
    assert row["doc_id"] == doc_id
    assert row["conversation_id"] == cid
    assert row["kind"] == "memory"
    assert row["content"] == "The Q3 numbers come\nfrom finance."
    assert row["reasoning"] == "user said so"
    assert row["base_instructions_version"] is None
    assert row["replaces_memory_id"] is None
    assert row["status"] == "pending"
    assert row["memory_id"] is None
    assert row["resolved_at"] is None
    created = datetime.fromisoformat(row["created_at"])
    assert created.tzinfo is None  # naive UTC as stored
    assert abs(datetime.now(timezone.utc).replace(tzinfo=None) - created) < timedelta(minutes=1)
    assert _get(env, row) == row


def test_create_instructions_row_shape(env):
    doc_id, cid = _doc(env), _conv(env)
    row, superseded = _instr_suggest(
        env, doc_id, cid, "  Newest entry first.\nUSD thousands.  ", base=3,
    )
    assert superseded == []
    assert set(row) == ROW_KEYS
    assert row["kind"] == "instructions"
    assert row["content"] == "Newest entry first.\nUSD thousands."
    assert row["base_instructions_version"] == 3
    assert row["reasoning"] is None
    assert row["status"] == "pending"
    # An empty text proposes clearing the field.
    cleared, _ = _instr_suggest(env, doc_id, cid, "   ", base=3)
    assert cleared["content"] == ""


def test_create_memory_with_replaces_memory_id(env):
    doc_id, cid = _doc(env), _conv(env)
    memory = _memory(env, doc_id, "old fact")
    row = _mem_suggest(env, doc_id, cid, "new fact", replaces_memory_id=f" {memory['id']} ")
    assert row["replaces_memory_id"] == memory["id"]
    # A blank id counts as none.
    assert _mem_suggest(env, doc_id, cid, "other", replaces_memory_id="  ")[
        "replaces_memory_id"] is None


def test_create_refuses_invalid_content_per_kind(env):
    doc_id, cid = _doc(env), _conv(env)
    # Caps count characters: a two-byte character at the cap passes.
    assert _mem_suggest(env, doc_id, cid, "é" * DOC_MEMORY_MAX_CHARS)["content"] == (
        "é" * DOC_MEMORY_MAX_CHARS
    )
    row, _ = _instr_suggest(env, doc_id, cid, "é" * DOC_INSTRUCTIONS_MAX_CHARS)
    assert len(row["content"]) == DOC_INSTRUCTIONS_MAX_CHARS
    before = _row_count(env)
    for bad in ("", "   \n ", "é" * (DOC_MEMORY_MAX_CHARS + 1), "a\x00b", None, 5):
        with pytest.raises(InvalidMemoryError):
            _run(store.create_suggestion(_uid(env, "alice"), doc_id, cid, "memory", bad))
    for bad in ("é" * (DOC_INSTRUCTIONS_MAX_CHARS + 1), "a\x00b", None, ["x"]):
        with pytest.raises(InvalidInstructionsError):
            _instr_suggest(env, doc_id, cid, bad)
    assert _row_count(env) == before
    # The instructions refusal superseded nothing.
    assert _get(env, row)["status"] == "pending"


def test_create_value_errors_insert_nothing(env):
    doc_id, other_doc, cid = _doc(env), _doc(env), _conv(env)
    other_memory = _memory(env, other_doc, "fact elsewhere")
    pending, _ = _instr_suggest(env, doc_id, cid, "Keep this pending.")
    alice = _uid(env, "alice")
    before = _row_count(env)
    calls = [
        dict(kind="note", content="x"),
        dict(kind="instructions", content="Rule."),
        dict(kind="instructions", content="Rule.", base_instructions_version=-1),
        dict(kind="instructions", content="Rule.", base_instructions_version=True),
        dict(kind="instructions", content="Rule.", base_instructions_version="0"),
        dict(kind="instructions", content="Rule.", base_instructions_version=0,
             replaces_memory_id=other_memory["id"]),
        dict(kind="memory", content="fact", base_instructions_version=0),
        dict(kind="memory", content="fact", replaces_memory_id=other_memory["id"]),
        dict(kind="memory", content="fact", replaces_memory_id=str(uuid.uuid4())),
        dict(kind="memory", content="fact", replaces_memory_id=7),
    ]
    for call in calls:
        kind = call.pop("kind")
        content = call.pop("content")
        with pytest.raises(ValueError) as exc_info:
            _run(store.create_suggestion(alice, doc_id, cid, kind, content, **call))
        assert not isinstance(
            exc_info.value, (InvalidMemoryError, InvalidInstructionsError)
        ), (kind, call)
    assert _row_count(env) == before
    assert _get(env, pending)["status"] == "pending"


# ---------------------------------------------------------------------------
# Supersede
# ---------------------------------------------------------------------------


def test_instructions_supersede_across_conversations_only_own_doc_kind(env):
    doc_id, other_doc = _doc(env), _doc(env)
    conv1, conv2 = _conv(env), _conv(env)
    bob_conv = _conv(env, "bob")
    first, _ = _instr_suggest(env, doc_id, conv1, "First text.")
    bob_row, _ = _instr_suggest(env, doc_id, bob_conv, "Bob's text.", who="bob")
    elsewhere, _ = _instr_suggest(env, other_doc, conv1, "Other doc text.")
    memory_row = _mem_suggest(env, doc_id, conv1, "a fact")

    second, superseded = _instr_suggest(env, doc_id, conv2, "Second text.", base=0)

    assert _ids(superseded) == [first["id"]]
    assert superseded[0]["status"] == "dismissed"
    assert superseded[0]["resolved_at"] == second["created_at"]
    assert superseded[0]["conversation_id"] == conv1
    assert _get(env, first) == superseded[0]
    assert second["status"] == "pending"
    # Another user's row, another doc's row and memory rows are untouched.
    assert _get(env, bob_row, "bob")["status"] == "pending"
    assert _get(env, elsewhere)["status"] == "pending"
    assert _get(env, memory_row)["status"] == "pending"
    assert _ids(_run(store.list_pending_for_doc(_uid(env, "alice"), doc_id))) == [
        memory_row["id"], second["id"],
    ]

    # A third supersedes only the second; the already dismissed first row
    # keeps the resolved_at the second create gave it.
    first_after_second = _get(env, first)
    third, superseded = _instr_suggest(env, doc_id, conv1, "Third text.")
    assert _ids(superseded) == [second["id"]]
    assert _get(env, first) == first_after_second
    assert _run(store.count_pending_for_doc(_uid(env, "alice"), doc_id)) == 2
    assert third["status"] == "pending"


def test_memory_suggestions_never_supersede(env):
    doc_id, cid = _doc(env), _conv(env)
    instr, _ = _instr_suggest(env, doc_id, cid, "Rule.")
    one = _mem_suggest(env, doc_id, cid, "fact one")
    two = _mem_suggest(env, doc_id, _conv(env), "fact two")
    for row in (instr, one, two):
        assert _get(env, row)["status"] == "pending"


# ---------------------------------------------------------------------------
# Lists, counts, lookups
# ---------------------------------------------------------------------------


def test_lists_and_counts_per_conversation_doc_and_user(env):
    alice, bob = _uid(env, "alice"), _uid(env, "bob")
    doc1, doc2 = _doc(env), _doc(env)
    conv1, conv2, bob_conv = _conv(env), _conv(env), _conv(env, "bob")
    a = _mem_suggest(env, doc1, conv1, "fact a")
    instr, _ = _instr_suggest(env, doc1, conv1, "Rule.")
    b = _mem_suggest(env, doc1, conv2, "fact b")
    c = _mem_suggest(env, doc2, conv2, "fact c")
    d = _mem_suggest(env, doc1, bob_conv, "fact d", who="bob")

    assert _ids(_run(store.list_pending(alice, conv1))) == [a["id"], instr["id"]]
    assert _run(store.count_pending(alice, conv1)) == 2
    assert _ids(_run(store.list_pending(alice, conv2))) == [b["id"], c["id"]]
    assert _run(store.list_pending(bob, conv1)) == []
    assert _run(store.count_pending(bob, conv1)) == 0
    assert _run(store.list_pending(alice, bob_conv)) == []

    assert _ids(_run(store.list_pending_for_doc(alice, doc1))) == [
        a["id"], instr["id"], b["id"],
    ]
    assert _run(store.count_pending_for_doc(alice, doc1)) == 3
    assert _ids(_run(store.list_pending_for_doc(bob, doc1))) == [d["id"]]
    assert _run(store.count_pending_for_doc(bob, doc2)) == 0

    assert _ids(_run(store.list_pending_for_user(alice))) == [
        a["id"], instr["id"], b["id"], c["id"],
    ]
    assert _run(store.count_pending_for_user(alice)) == 4
    assert _ids(_run(store.list_pending_for_user(bob))) == [d["id"]]
    assert _run(store.list_pending_for_user(_uid(env, "carol"))) == []

    # Resolved rows drop out everywhere.
    _run(store.dismiss_suggestion(alice, a["id"]))
    _run(store.accept_suggestion(alice, b["id"]))
    assert _ids(_run(store.list_pending(alice, conv1))) == [instr["id"]]
    assert _run(store.count_pending(alice, conv2)) == 1
    assert _ids(_run(store.list_pending_for_doc(alice, doc1))) == [instr["id"]]
    assert _run(store.count_pending_for_doc(alice, doc1)) == 1
    assert _ids(_run(store.list_pending_for_user(alice))) == [instr["id"], c["id"]]
    assert _run(store.count_pending_for_user(alice)) == 2


def test_pending_lists_break_created_at_ties_by_id(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    rows = [_mem_suggest(env, doc_id, cid, f"fact {i}") for i in range(4)]
    tie = datetime(2026, 10, 1, 12, 0, 0)
    for row in rows:
        _set_row(env, row["id"], created_at=tie)
    expected = sorted(_ids(rows))
    assert _ids(_run(store.list_pending(alice, cid))) == expected
    assert _ids(_run(store.list_pending_for_doc(alice, doc_id))) == expected
    assert _ids(_run(store.list_pending_for_user(alice))) == expected


def test_get_suggestion_scoping(env):
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    assert _run(store.get_suggestion(_uid(env, "bob"), row["id"])) is None
    assert _run(store.get_suggestion(_uid(env, "alice"), str(uuid.uuid4()))) is None
    _run(store.dismiss_suggestion(_uid(env, "alice"), row["id"]))
    assert _get(env, row)["status"] == "dismissed"


def test_find_pending_with_content(env):
    alice, bob = _uid(env, "alice"), _uid(env, "bob")
    doc_id, other_doc = _doc(env), _doc(env)
    conv1, conv2 = _conv(env), _conv(env)
    _mem_suggest(env, doc_id, conv1, "noise")
    row = _mem_suggest(env, doc_id, conv2, "the fact")
    instr, _ = _instr_suggest(env, doc_id, conv1, "The rule.")

    found = _run(store.find_pending_with_content(alice, doc_id, "memory", "the fact"))
    assert found == row and found["conversation_id"] == conv2
    assert _run(store.find_pending_with_content(
        alice, doc_id, "instructions", "The rule.")) == instr
    # Same text, another kind / doc / user, or not exact: no match.
    assert _run(store.find_pending_with_content(
        alice, doc_id, "instructions", "the fact")) is None
    assert _run(store.find_pending_with_content(
        alice, other_doc, "memory", "the fact")) is None
    assert _run(store.find_pending_with_content(
        bob, doc_id, "memory", "the fact")) is None
    assert _run(store.find_pending_with_content(
        alice, doc_id, "memory", "the fact ")) is None
    assert _run(store.find_pending_with_content(
        alice, doc_id, "memory", "The fact")) is None
    # Never a resolved row.
    _run(store.dismiss_suggestion(alice, row["id"]))
    assert _run(store.find_pending_with_content(
        alice, doc_id, "memory", "the fact")) is None
    _run(store.accept_suggestion(alice, instr["id"]))
    assert _run(store.find_pending_with_content(
        alice, doc_id, "instructions", "The rule.")) is None


def test_list_resolved_for_user_keyset_pages_with_ties(env):
    alice, bob = _uid(env, "alice"), _uid(env, "bob")
    doc_id, cid, bob_conv = _doc(env), _conv(env), _conv(env, "bob")
    rows = [_mem_suggest(env, doc_id, cid, f"fact {i}") for i in range(7)]
    for row in rows[:5]:
        _run(store.dismiss_suggestion(alice, row["id"]))
    for row in rows[5:]:
        _run(store.accept_suggestion(alice, row["id"]))
    pending = _mem_suggest(env, doc_id, cid, "still pending")
    bob_row = _mem_suggest(env, doc_id, bob_conv, "bob fact", who="bob")
    _run(store.dismiss_suggestion(bob, bob_row["id"]))

    t1, t2 = datetime(2026, 10, 1, 9, 0, 0), datetime(2026, 10, 2, 9, 0, 0)
    for row in rows[:3]:
        _set_row(env, row["id"], resolved_at=t1)
    for row in rows[3:]:
        _set_row(env, row["id"], resolved_at=t2)
    expected = (
        sorted(_ids(rows[3:]), reverse=True) + sorted(_ids(rows[:3]), reverse=True)
    )

    seen, cursor, pages = [], None, 0
    while True:
        page = _run(store.list_resolved_for_user(alice, 3, cursor))
        if not page:
            break
        pages += 1
        assert len(page) <= 3
        assert all(r["status"] in ("accepted", "dismissed") for r in page)
        seen.extend(_ids(page))
        cursor = decode_resolved_cursor(encode_resolved_cursor(page[-1]))
    assert pages == 3
    assert seen == expected
    assert pending["id"] not in seen and bob_row["id"] not in seen
    assert _ids(_run(store.list_resolved_for_user(bob, 10))) == [bob_row["id"]]


def test_list_pending_summaries(env):
    alice, bob, carol = (_uid(env, w) for w in ("alice", "bob", "carol"))
    doc_id = _doc(env)
    first = _mem_suggest(env, doc_id, _conv(env), "fact 1")
    _instr_suggest(env, doc_id, _conv(env), "Rule.")
    bob_row = _mem_suggest(env, doc_id, _conv(env, "bob"), "fact 2", who="bob")
    carol_row = _mem_suggest(env, doc_id, _conv(env, "carol"), "fact 3", who="carol")
    _run(store.dismiss_suggestion(carol, carol_row["id"]))

    summaries = _run(store.list_pending_summaries())
    assert summaries == [
        {"user_id": alice, "pending_count": 2,
         "oldest_created_at": datetime.fromisoformat(first["created_at"])},
        {"user_id": bob, "pending_count": 1,
         "oldest_created_at": datetime.fromisoformat(bob_row["created_at"])},
    ]


def test_list_pending_pairs_for_docs(env, monkeypatch):
    alice, bob, carol = (_uid(env, w) for w in ("alice", "bob", "carol"))
    doc1, doc2, doc3, doc4 = (_doc(env) for _ in range(4))
    conv1, conv2 = _conv(env), _conv(env)
    bob_conv, carol_conv = _conv(env, "bob"), _conv(env, "carol")
    _mem_suggest(env, doc1, conv1, "fact 1")
    _instr_suggest(env, doc1, conv1, "Rule.")
    _mem_suggest(env, doc1, bob_conv, "fact 2", who="bob")
    _mem_suggest(env, doc2, conv2, "fact 3")
    resolved = _mem_suggest(env, doc3, carol_conv, "fact 4", who="carol")
    _run(store.dismiss_suggestion(carol, resolved["id"]))
    _mem_suggest(env, doc4, conv2, "not asked for")

    expected = sorted([(alice, conv1), (alice, conv2), (bob, bob_conv)])
    assert _run(store.list_pending_pairs_for_docs([doc1, doc2, doc3])) == expected
    # Chunked IN lookups give the same answer; duplicates in the input are fine.
    monkeypatch.setattr(store, "_IN_CHUNK", 1)
    assert _run(store.list_pending_pairs_for_docs(
        [doc2, doc1, doc1, doc3, str(uuid.uuid4())])) == expected
    assert _run(store.list_pending_pairs_for_docs([doc3])) == []

    def _no_session():
        raise AssertionError("no query for empty input")

    monkeypatch.setattr(store, "AsyncSessionLocal", _no_session)
    assert _run(store.list_pending_pairs_for_docs([])) == []


# ---------------------------------------------------------------------------
# Accept: memory kind
# ---------------------------------------------------------------------------


def test_accept_memory_inserts(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "The board reads this on Fridays.")
    before = _state(env, doc_id)

    result = _run(store.accept_suggestion(alice, row["id"]))

    assert set(result) == {"suggestion", "memory", "notes_before", "notes_after"}
    memory = result["memory"]
    assert memory["content"] == "The board reads this on Fridays."
    assert memory["source"] == f"suggestion:{row['id']}"
    assert memory["created_by"] == alice and memory["updated_by"] == alice
    suggestion = result["suggestion"]
    assert suggestion["status"] == "accepted"
    assert suggestion["memory_id"] == memory["id"]
    assert suggestion["content"] == row["content"]
    # One ``now`` for every write of the accept.
    assert suggestion["resolved_at"] == memory["created_at"] == memory["updated_at"]
    assert result["notes_before"] == before
    after = result["notes_after"]
    assert after["memories_version"] == before["memories_version"] + 1
    assert after["memory_count"] == before["memory_count"] + 1
    assert after["instructions_version"] == before["instructions_version"]
    assert after == _state(env, doc_id)
    assert _run(doc_memory_store.list_doc_memories(doc_id)) == [memory]
    assert _get(env, row) == suggestion


def test_accept_memory_with_override_content(env):
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "proposed fact")
    result = _run(store.accept_suggestion(
        _uid(env, "alice"), row["id"], content="  edited\nfact  ",
    ))
    assert result["memory"]["content"] == "edited\nfact"
    # The row keeps the proposed text.
    assert result["suggestion"]["content"] == "proposed fact"


def test_accept_memory_replaces_in_place(env):
    alice, bob = _uid(env, "alice"), _uid(env, "bob")
    doc_id, cid = _doc(env), _conv(env)
    old = _memory(env, doc_id, "old fact", who="bob")
    _memory(env, doc_id, "unrelated fact")
    row = _mem_suggest(env, doc_id, cid, "new fact", replaces_memory_id=old["id"])
    before = _state(env, doc_id)

    result = _run(store.accept_suggestion(alice, row["id"]))

    memory = result["memory"]
    assert memory["id"] == old["id"]
    assert memory["content"] == "new fact"
    assert memory["source"] == old["source"]
    assert memory["created_by"] == bob and memory["created_at"] == old["created_at"]
    assert memory["updated_by"] == alice
    assert memory["updated_at"] == result["suggestion"]["resolved_at"]
    assert result["suggestion"]["memory_id"] == old["id"]
    after = result["notes_after"]
    assert after["memory_count"] == before["memory_count"] == 2
    assert after["memories_version"] == before["memories_version"] + 1
    assert _run(doc_memory_store.get_doc_memory(doc_id, old["id"]))["content"] == "new fact"


def test_accept_memory_replaced_memory_gone_inserts(env):
    alice = _uid(env, "alice")
    doc_id, other_doc, cid = _doc(env), _doc(env), _conv(env)
    old = _memory(env, doc_id, "old fact")
    row = _mem_suggest(env, doc_id, cid, "new fact", replaces_memory_id=old["id"])
    assert _run(doc_memory_store.delete_doc_memory(doc_id, old["id"]))
    # The foreign key nulled the reference.
    assert _get(env, row)["replaces_memory_id"] is None

    result = _run(store.accept_suggestion(alice, row["id"]))
    assert result["memory"]["id"] != old["id"]
    assert result["memory"]["source"] == f"suggestion:{row['id']}"
    assert result["notes_after"]["memory_count"] == 1

    # A reference left pointing at another doc's memory (written behind the
    # store's back) also inserts and leaves that memory alone.
    foreign = _memory(env, other_doc, "foreign fact")
    row2 = _mem_suggest(env, doc_id, cid, "second fact")
    _set_row(env, row2["id"], replaces_memory_id=foreign["id"])
    result = _run(store.accept_suggestion(alice, row2["id"]))
    assert result["memory"]["doc_id"] == doc_id
    assert result["memory"]["content"] == "second fact"
    assert _run(doc_memory_store.get_doc_memory(other_doc, foreign["id"]))[
        "content"] == "foreign fact"


def test_accept_memory_duplicate_refused_row_stays_pending(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    existing = _memory(env, doc_id, "known fact")
    other = _memory(env, doc_id, "other fact")
    row = _mem_suggest(env, doc_id, cid, "known fact")
    replacing = _mem_suggest(env, doc_id, cid, "fresh", replaces_memory_id=other["id"])
    before = _state(env, doc_id)

    with pytest.raises(DuplicateMemoryError) as exc_info:
        _run(store.accept_suggestion(alice, row["id"]))
    assert exc_info.value.memory_id == existing["id"]
    _assert_pending(env, row)

    # An override that duplicates another memory on a replace, too.
    with pytest.raises(DuplicateMemoryError):
        _run(store.accept_suggestion(alice, replacing["id"], content="known fact"))
    _assert_pending(env, replacing)

    assert _state(env, doc_id) == before
    assert {m["content"] for m in _run(doc_memory_store.list_doc_memories(doc_id))} == {
        "known fact", "other fact",
    }


def test_accept_memory_cap(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    first = _memory(env, doc_id, "fact 0")
    replacing = _mem_suggest(env, doc_id, cid, "fact 0, revised",
                             replaces_memory_id=first["id"])
    inserting = _mem_suggest(env, doc_id, cid, "one too many")
    for i in range(1, DOC_MAX_MEMORIES):
        _memory(env, doc_id, f"fact {i}")
    before = _state(env, doc_id)
    assert before["memory_count"] == DOC_MAX_MEMORIES

    with pytest.raises(MemoriesCapReachedError):
        _run(store.accept_suggestion(alice, inserting["id"]))
    _assert_pending(env, inserting)
    assert _state(env, doc_id) == before

    # Replacing at the cap is fine.
    result = _run(store.accept_suggestion(alice, replacing["id"]))
    assert result["memory"]["id"] == first["id"]
    assert result["notes_after"]["memory_count"] == DOC_MAX_MEMORIES
    assert result["notes_after"]["memories_version"] == before["memories_version"] + 1


def test_accept_memory_invalid_override_refused(env):
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    before = _state(env, doc_id)
    for bad in ("   ", "é" * (DOC_MEMORY_MAX_CHARS + 1)):
        with pytest.raises(InvalidMemoryError):
            _run(store.accept_suggestion(_uid(env, "alice"), row["id"], content=bad))
        _assert_pending(env, row)
    assert _state(env, doc_id) == before


# ---------------------------------------------------------------------------
# Accept: instructions kind
# ---------------------------------------------------------------------------


def test_accept_instructions_writes_field(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row, _ = _instr_suggest(env, doc_id, cid, "Newest entry first.", base=0)
    before = _state(env, doc_id)

    result = _run(store.accept_suggestion(alice, row["id"]))

    assert result["memory"] is None
    assert result["notes_before"] == before
    after = result["notes_after"]
    assert after["instructions"] == "Newest entry first."
    assert after["has_instructions"] is True
    assert after["instructions_version"] == before["instructions_version"] + 1
    assert after["instructions_updated_by"] == alice
    assert after["memories_version"] == before["memories_version"]
    assert after == _state(env, doc_id)
    suggestion = result["suggestion"]
    assert suggestion["status"] == "accepted"
    assert suggestion["memory_id"] is None
    assert suggestion["resolved_at"] == after["instructions_updated_at"]


def test_accept_instructions_stale_then_explicit_version(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row, _ = _instr_suggest(env, doc_id, cid, "Proposed rule.", base=0)
    moved = _run(env.doc_store.update_doc_instructions(
        doc_id, "Someone else's rule.", expected_version=0, user_id=alice,
    ))
    assert moved["instructions_version"] == 1

    with pytest.raises(StaleInstructionsError) as exc_info:
        _run(store.accept_suggestion(alice, row["id"]))
    assert exc_info.value.current["instructions"] == "Someone else's rule."
    assert exc_info.value.current["instructions_version"] == 1
    _assert_pending(env, row)
    assert _state(env, doc_id) == moved

    # An explicit stale token is refused too.
    with pytest.raises(StaleInstructionsError):
        _run(store.accept_suggestion(alice, row["id"], expected_instructions_version=0))
    _assert_pending(env, row)

    # The reviewer saw the moved field (version 1) and accepts against it.
    result = _run(store.accept_suggestion(
        alice, row["id"], expected_instructions_version=1,
    ))
    assert result["notes_before"] == moved
    assert result["notes_after"]["instructions"] == "Proposed rule."
    assert result["notes_after"]["instructions_version"] == 2


def test_accept_instructions_override_and_clear(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row, _ = _instr_suggest(env, doc_id, cid, "Proposed.", base=0)
    result = _run(store.accept_suggestion(alice, row["id"], content="  Edited\nrule.  "))
    assert result["notes_after"]["instructions"] == "Edited\nrule."
    assert result["suggestion"]["content"] == "Proposed."

    clear, _ = _instr_suggest(env, doc_id, cid, "Something.", base=1)
    result = _run(store.accept_suggestion(alice, clear["id"], content=""))
    assert result["notes_after"]["instructions"] == ""
    assert result["notes_after"]["has_instructions"] is False
    assert result["notes_after"]["instructions_version"] == 2


def test_accept_instructions_invalid_input_refused(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row, _ = _instr_suggest(env, doc_id, cid, "Rule.", base=0)
    before = _state(env, doc_id)
    with pytest.raises(InvalidInstructionsError):
        _run(store.accept_suggestion(
            alice, row["id"], content="é" * (DOC_INSTRUCTIONS_MAX_CHARS + 1),
        ))
    _assert_pending(env, row)
    for bad in (-1, True, "0"):
        with pytest.raises(InvalidInstructionsError):
            _run(store.accept_suggestion(
                alice, row["id"], expected_instructions_version=bad,
            ))
        _assert_pending(env, row)
    assert _state(env, doc_id) == before


# ---------------------------------------------------------------------------
# Claim, scoping, resolved rows, dismiss
# ---------------------------------------------------------------------------


def test_accept_claims_first_and_undoes_the_claim_on_failure(env, monkeypatch):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    before = _state(env, doc_id)
    seen = []

    async def failing_insert(db, doc_id, content, **kw):
        status = (await db.execute(
            select(DocNoteSuggestion.status).where(DocNoteSuggestion.id == row["id"])
        )).scalar_one()
        seen.append(status)
        raise RuntimeError("insert failed")

    monkeypatch.setattr(doc_memory_store, "insert_memory_in_session", failing_insert)
    with pytest.raises(RuntimeError):
        _run(store.accept_suggestion(alice, row["id"]))
    assert seen == ["accepted"]  # the claim landed before the notes write
    _assert_pending(env, row)
    assert _state(env, doc_id) == before


def test_accept_loses_the_claim_to_a_concurrent_resolve(env, monkeypatch):
    """The row is pending when loaded, but another tab dismisses it before
    the claim: the claim matches nothing and the accept changes nothing."""
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    before = _state(env, doc_id)
    real = env.doc_store.AsyncSessionLocal

    def racing_session():
        session = real()
        original_get = session.get

        async def get(*args, **kwargs):
            loaded = await original_get(*args, **kwargs)
            async with real() as other:  # the other tab
                await other.execute(
                    update(DocNoteSuggestion)
                    .where(DocNoteSuggestion.id == row["id"])
                    .values(status="dismissed",
                            resolved_at=datetime.now(timezone.utc))
                )
                await other.commit()
            return loaded

        session.get = get
        return session

    monkeypatch.setattr(store, "AsyncSessionLocal", racing_session)
    with pytest.raises(store.SuggestionAlreadyResolvedError) as exc_info:
        _run(store.accept_suggestion(alice, row["id"]))
    assert exc_info.value.status == "resolved"
    monkeypatch.setattr(store, "AsyncSessionLocal", real)
    fresh = _get(env, row)
    assert fresh["status"] == "dismissed" and fresh["memory_id"] is None
    assert _state(env, doc_id) == before
    assert _run(doc_memory_store.list_doc_memories(doc_id)) == []


def test_two_accepts_of_one_row(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    _run(store.accept_suggestion(alice, row["id"]))
    with pytest.raises(store.SuggestionAlreadyResolvedError) as exc_info:
        _run(store.accept_suggestion(alice, row["id"]))
    assert exc_info.value.status == "accepted"

    concurrent = _mem_suggest(env, doc_id, cid, "another fact")
    before = _state(env, doc_id)

    async def both():
        return await asyncio.gather(
            store.accept_suggestion(alice, concurrent["id"]),
            store.accept_suggestion(alice, concurrent["id"]),
            return_exceptions=True,
        )

    results = _run(both())
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, store.SuggestionAlreadyResolvedError)]
    assert len(wins) == 1 and len(losses) == 1, results
    after = _state(env, doc_id)
    assert after["memories_version"] == before["memories_version"] + 1
    assert after["memory_count"] == before["memory_count"] + 1
    assert [m["content"] for m in _run(doc_memory_store.list_doc_memories(doc_id))].count(
        "another fact") == 1


def test_accept_and_dismiss_scoping(env):
    alice, bob = _uid(env, "alice"), _uid(env, "bob")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    before = _state(env, doc_id)
    assert _run(store.accept_suggestion(bob, row["id"])) is None
    assert _run(store.dismiss_suggestion(bob, row["id"])) is None
    assert _run(store.accept_suggestion(alice, str(uuid.uuid4()))) is None
    assert _run(store.dismiss_suggestion(alice, str(uuid.uuid4()))) is None
    _assert_pending(env, row)
    assert _state(env, doc_id) == before


def test_resolved_rows_refuse_accept_and_dismiss(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    accepted = _mem_suggest(env, doc_id, cid, "fact 1")
    dismissed = _mem_suggest(env, doc_id, cid, "fact 2")
    _run(store.accept_suggestion(alice, accepted["id"]))
    _run(store.dismiss_suggestion(alice, dismissed["id"]))
    for row, status in ((accepted, "accepted"), (dismissed, "dismissed")):
        for call in (store.accept_suggestion, store.dismiss_suggestion):
            with pytest.raises(store.SuggestionAlreadyResolvedError) as exc_info:
                _run(call(alice, row["id"]))
            assert exc_info.value.status == status


def test_dismiss_sets_status_and_leaves_notes(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    memory_row = _mem_suggest(env, doc_id, cid, "fact")
    instr, _ = _instr_suggest(env, doc_id, cid, "Rule.")
    before = _state(env, doc_id)
    for row in (memory_row, instr):
        dismissed = _run(store.dismiss_suggestion(alice, row["id"]))
        assert set(dismissed) == ROW_KEYS
        assert dismissed["status"] == "dismissed"
        assert dismissed["resolved_at"] is not None
        assert dismissed["memory_id"] is None
        assert _get(env, row) == dismissed
    assert _state(env, doc_id) == before


# ---------------------------------------------------------------------------
# Cascades and the admin report
# ---------------------------------------------------------------------------


def test_doc_delete_removes_its_suggestions(env):
    alice = _uid(env, "alice")
    doc1, doc2, doc3, cid = _doc(env), _doc(env), _doc(env), _conv(env)
    gone = [_mem_suggest(env, doc1, cid, "fact"), _instr_suggest(env, doc1, cid, "R.")[0]]
    accepted = _mem_suggest(env, doc1, cid, "accepted fact")
    _run(store.accept_suggestion(alice, accepted["id"]))
    raw_gone = _mem_suggest(env, doc3, cid, "fact 3")
    kept = _mem_suggest(env, doc2, cid, "fact 2")

    assert _run(env.doc_store.delete_doc(doc1))
    for row in gone + [accepted]:
        assert _get(env, row) is None

    # A raw row delete cascades through the foreign key as well.
    async def raw_delete():
        async with _session(env) as db:
            await db.execute(delete(Doc).where(Doc.id == doc3))
            await db.commit()

    _run(raw_delete())
    assert _get(env, raw_gone) is None
    assert _get(env, kept)["status"] == "pending"
    assert _row_count(env) == 1


def test_conversation_delete_removes_its_suggestions(env):
    alice = _uid(env, "alice")
    doc_id, conv1, conv2 = _doc(env), _conv(env), _conv(env)
    accepted = _mem_suggest(env, doc_id, conv1, "accepted fact")
    memory = _run(store.accept_suggestion(alice, accepted["id"]))["memory"]
    pending = _mem_suggest(env, doc_id, conv1, "pending fact")
    kept = _mem_suggest(env, doc_id, conv2, "other conversation")

    async def delete_conversation():
        async with _session(env) as db:
            await db.execute(delete(Conversation).where(Conversation.id == conv1))
            await db.commit()

    _run(delete_conversation())
    assert _get(env, accepted) is None and _get(env, pending) is None
    assert _get(env, kept)["status"] == "pending"
    # Accepted memories stay; their source is now a dead reference.
    assert _run(doc_memory_store.get_doc_memory(doc_id, memory["id"])) == memory


def test_memory_delete_nulls_suggestion_references(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    target = _memory(env, doc_id, "to be replaced")
    replacing = _mem_suggest(env, doc_id, cid, "replacement", replaces_memory_id=target["id"])
    accepted = _mem_suggest(env, doc_id, cid, "accepted fact")
    created = _run(store.accept_suggestion(alice, accepted["id"]))["memory"]

    assert _run(doc_memory_store.delete_doc_memory(doc_id, target["id"]))
    assert _run(doc_memory_store.delete_doc_memory(doc_id, created["id"]))

    fresh = _get(env, replacing)
    assert fresh["replaces_memory_id"] is None and fresh["status"] == "pending"
    fresh = _get(env, accepted)
    assert fresh["memory_id"] is None and fresh["status"] == "accepted"


def test_list_all_suggestion_rows_shape(env):
    alice, bob = _uid(env, "alice"), _uid(env, "bob")
    doc_id = _doc(env)
    mem = _mem_suggest(env, doc_id, _conv(env), "fact")
    instr, _ = _instr_suggest(env, doc_id, _conv(env, "bob"), "Rule.", who="bob")
    _run(store.dismiss_suggestion(bob, instr["id"]))

    rows = _run(store.list_all_suggestion_rows())
    assert len(rows) == 2
    for row in rows:
        assert set(row) == {"user_id", "doc_id", "kind", "status", "created_at",
                            "resolved_at"}
        assert isinstance(row["created_at"], datetime)
        assert row["created_at"].tzinfo is None
    by_user = {row["user_id"]: row for row in rows}
    assert by_user[alice]["kind"] == "memory"
    assert by_user[alice]["status"] == "pending"
    assert by_user[alice]["resolved_at"] is None
    assert by_user[alice]["created_at"] == datetime.fromisoformat(mem["created_at"])
    assert by_user[bob]["kind"] == "instructions"
    assert by_user[bob]["status"] == "dismissed"
    assert isinstance(by_user[bob]["resolved_at"], datetime)
    assert all(row["doc_id"] == doc_id for row in rows)


# ---------------------------------------------------------------------------
# Review follow-ups: the doc lock, gone targets, claim order, no-op accepts
# ---------------------------------------------------------------------------


def test_create_on_a_missing_doc_or_conversation_raises_target_gone(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    for kind, kw in (("memory", {}), ("instructions", {"base_instructions_version": 0})):
        with pytest.raises(store.SuggestionTargetGoneError):
            _run(store.create_suggestion(
                alice, str(uuid.uuid4()), cid, kind, "text", **kw,
            ))
        with pytest.raises(store.SuggestionTargetGoneError):
            _run(store.create_suggestion(
                alice, doc_id, str(uuid.uuid4()), kind, "text", **kw,
            ))
    assert _row_count(env) == 0


def test_create_takes_the_doc_lock_before_the_replaced_memory_check(env, monkeypatch):
    """The replaced-memory check runs inside the write transaction, so a
    memory delete (which takes the same doc lock) cannot slip between the
    check and the insert."""
    doc_id, cid = _doc(env), _conv(env)
    memory = _memory(env, doc_id, "old")
    real = env.doc_store.AsyncSessionLocal
    statements: list[str] = []

    def watching_session():
        session = real()
        original_execute = session.execute

        async def execute(stmt, *args, **kwargs):
            text = str(stmt)
            if text.startswith("UPDATE docs"):
                statements.append("lock")
            elif "FROM doc_memories" in text:
                statements.append("memory check")
            return await original_execute(stmt, *args, **kwargs)

        session.execute = execute
        return session

    monkeypatch.setattr(store, "AsyncSessionLocal", watching_session)
    _mem_suggest(env, doc_id, cid, "new", replaces_memory_id=memory["id"])
    assert statements == ["lock", "memory check"]


def test_concurrent_instructions_creates_leave_one_pending(env):
    alice = _uid(env, "alice")
    doc_id = _doc(env)
    cids = [_conv(env) for _ in range(5)]

    async def all_at_once():
        return await asyncio.gather(*(
            store.create_suggestion(
                alice, doc_id, cid, "instructions", f"Rule {i}",
                base_instructions_version=0,
            )
            for i, cid in enumerate(cids)
        ))

    results = _run(all_at_once())
    pending = _run(store.list_pending_for_doc(alice, doc_id))
    assert len(pending) == 1
    created = {row["id"] for row, _sup in results}
    superseded = [s["id"] for _row, sup in results for s in sup]
    # Every row but the survivor was superseded exactly once, by a later create.
    assert sorted(superseded) == sorted(created - {pending[0]["id"]})


def test_accept_reads_notes_before_under_the_claim(env, monkeypatch):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    real = env.doc_store.load_notes_state_in_session
    seen: list[str] = []

    async def watching(db, doc_id):
        seen.append((await db.execute(
            select(DocNoteSuggestion.status).where(DocNoteSuggestion.id == row["id"])
        )).scalar_one())
        return await real(db, doc_id)

    monkeypatch.setattr(env.doc_store, "load_notes_state_in_session", watching)
    _run(store.accept_suggestion(alice, row["id"]))
    assert seen == ["accepted", "accepted"]  # notes_before and notes_after


def test_instructions_accept_claims_first_and_undoes_the_claim(env, monkeypatch):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row, _sup = _instr_suggest(env, doc_id, cid, "Rule")
    before = _state(env, doc_id)
    seen: list[str] = []

    async def failing_write(db, doc_id, text, **kw):
        seen.append((await db.execute(
            select(DocNoteSuggestion.status).where(DocNoteSuggestion.id == row["id"])
        )).scalar_one())
        raise RuntimeError("write failed")

    monkeypatch.setattr(env.doc_store, "write_instructions_in_session", failing_write)
    with pytest.raises(RuntimeError):
        _run(store.accept_suggestion(alice, row["id"]))
    assert seen == ["accepted"]
    fresh = _get(env, row)
    assert fresh["status"] == "pending" and fresh["resolved_at"] is None
    assert _state(env, doc_id) == before


def test_dismiss_loses_the_claim_to_a_concurrent_resolve(env, monkeypatch):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)
    row = _mem_suggest(env, doc_id, cid, "fact")
    real = env.doc_store.AsyncSessionLocal

    def racing_session():
        session = real()
        original_get = session.get

        async def get(*args, **kwargs):
            loaded = await original_get(*args, **kwargs)
            async with real() as other:  # another tab accepts first
                await other.execute(
                    update(DocNoteSuggestion)
                    .where(DocNoteSuggestion.id == row["id"])
                    .values(status="accepted",
                            resolved_at=datetime.now(timezone.utc))
                )
                await other.commit()
            return loaded

        session.get = get
        return session

    monkeypatch.setattr(store, "AsyncSessionLocal", racing_session)
    with pytest.raises(store.SuggestionAlreadyResolvedError) as exc_info:
        _run(store.dismiss_suggestion(alice, row["id"]))
    assert exc_info.value.status == "resolved"
    monkeypatch.setattr(store, "AsyncSessionLocal", real)
    assert _get(env, row)["status"] == "accepted"


def test_noop_accepts_resolve_the_row_without_a_notes_change(env):
    alice = _uid(env, "alice")
    doc_id, cid = _doc(env), _conv(env)

    # Instructions equal to the field at the expected version.
    _run(env.doc_store.update_doc_instructions(
        doc_id, "Rule", expected_version=0, user_id=alice,
    ))
    row, _sup = _instr_suggest(env, doc_id, cid, "Rule", base=1)
    result = _run(store.accept_suggestion(alice, row["id"]))
    assert result["suggestion"]["status"] == "accepted"
    assert result["notes_after"] == result["notes_before"]
    assert result["notes_after"]["instructions_version"] == 1

    # A replace in place with the memory's own text.
    memory = _memory(env, doc_id, "fact")
    replace = _mem_suggest(env, doc_id, cid, "fact", replaces_memory_id=memory["id"])
    result = _run(store.accept_suggestion(alice, replace["id"]))
    assert result["suggestion"]["status"] == "accepted"
    assert result["suggestion"]["memory_id"] == memory["id"]
    assert result["memory"]["id"] == memory["id"]
    assert result["notes_after"] == result["notes_before"]
