"""Doc notes stores (db/doc_store.py instructions, db/doc_memory_store.py)
against an isolated SQLite file.

Covers phase A1 of spec 00011 (docs/architecture/doc-notes.md):

1. Instructions: save (strip, newlines kept, version bump, editor and
   time recorded), clear, the character cap, the stale token, identical
   text as a no-op, concurrent saves with one token, the session helper.
2. Memories: create / get / list newest first / update / delete, the
   character cap, empty and non-string text, exact duplicates on create
   and update, the per-doc cap (also under concurrency), missing docs,
   memory ids of another doc, sources, timestamps, the session helpers.
3. Each version is bumped only by its own kind of change, exactly once
   per landed write, never by a refusal or a no-op.
4. ``docs.memory_count`` equals the real row count, also after
   concurrent creates.
5. ``docs.updated_at`` and ``last_write_source`` are untouched by notes.
6. The instructions are absent from ``update_doc_metadata()``, from every
   doc dict and from the candidate queries' SQL (``length()`` only), and
   the notes keys are absent from UI rows and tool list rows.
7. ``get_notes_state()``.
8. Cascades: ``delete_doc()`` (with and without foreign keys), a raw
   doc delete, a memory delete, a user delete.

Same isolation pattern as tests/test_docs_store.py, with both stores'
``AsyncSessionLocal`` pointed at the temp database.
"""

import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_MEMORY_MAX_CHARS,
)


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_doc_notes_store_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.doc_memory_store as mem_mod
    import db.doc_store as doc_store_mod
    import db.models as models_mod

    models_mod.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(doc_store_mod, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(mem_mod, "AsyncSessionLocal", session_local)

    async def _seed():
        async with session_local() as db:
            users = [
                models_mod.User(email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for name in ("alice", "bob", "carol")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            project = models_mod.Project(user_id=users[0].id, name="Research")
            db.add(project)
            now = datetime.now(timezone.utc)
            conversation = models_mod.Conversation(
                id=str(uuid.uuid4()), user_id=users[0].id,
                created_at=now, last_message_at=now,
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(project)
            return [u.id for u in users], project.id, conversation.id

    (alice, bob, carol), project_id, conversation_id = _run(_seed())
    yield {
        "store": doc_store_mod,
        "mem": mem_mod,
        "models": models_mod,
        "session": session_local,
        "sync_engine": sync_engine,
        "async_engine": async_engine,
        "db_path": db_path,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "p1": project_id,
        "conv": conversation_id,
    }
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(env, title="Board update", owner=None, **kw):
    kw.setdefault("mode", "private")
    return _run(env["store"].create_doc(owner or env["alice"], title, **kw))


def _state(env, doc_id):
    return _run(env["store"].get_notes_state(doc_id))


def _save(env, doc_id, text_, expected, user=None):
    return _run(env["store"].update_doc_instructions(
        doc_id, text_, expected_version=expected, user_id=user or env["alice"],
    ))


def _add(env, doc_id, content, user=None):
    user = user or env["alice"]
    return _run(env["mem"].create_doc_memory(
        doc_id, content, source=env["mem"].ui_source(user), user_id=user,
    ))


def _scalar(env, sql, **params):
    with env["sync_engine"].connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _memory_rows(env, doc_id):
    return _scalar(env, "SELECT count(*) FROM doc_memories WHERE doc_id = :d", d=doc_id)


def _suggestion_rows(env, doc_id):
    return _scalar(
        env, "SELECT count(*) FROM doc_note_suggestions WHERE doc_id = :d", d=doc_id,
    )


def _versions(env, doc_id):
    state = _state(env, doc_id)
    return state["instructions_version"], state["memories_version"], state["memory_count"]


def _add_suggestion(env, doc_id, *, kind="memory", content="Proposed", **kw):
    async def _insert():
        async with env["session"]() as db:
            row = env["models"].DocNoteSuggestion(
                user_id=env["alice"], doc_id=doc_id, conversation_id=env["conv"],
                kind=kind, content=content, **kw,
            )
            db.add(row)
            await db.commit()
            return row.id

    return _run(_insert())


# ---------------------------------------------------------------------------
# 1. Instructions
# ---------------------------------------------------------------------------


class TestInstructions:
    def test_save_strips_bumps_and_records(self, env):
        doc = _doc(env)
        state = _save(env, doc["id"], "  Newest entry first.  \n", 0)
        assert state["doc_id"] == doc["id"]
        assert state["instructions"] == "Newest entry first."
        assert state["instructions_version"] == 1
        assert state["instructions_updated_by"] == env["alice"]
        datetime.fromisoformat(state["instructions_updated_at"])
        assert state["memories_version"] == 0
        assert state["memory_count"] == 0
        assert _state(env, doc["id"]) == state

    def test_multiline_text_kept(self, env):
        doc = _doc(env)
        body = "Newest entry first.\n\n  - never touch Risks\nUSD thousands"
        state = _save(env, doc["id"], f"\n\t{body}\n\n", 0)
        assert state["instructions"] == body

    def test_second_save_needs_the_new_token(self, env):
        doc = _doc(env)
        _save(env, doc["id"], "One", 0)
        state = _save(env, doc["id"], "Two", 1, user=env["bob"])
        assert state["instructions"] == "Two"
        assert state["instructions_version"] == 2
        assert state["instructions_updated_by"] == env["bob"]

    @pytest.mark.parametrize("empty", ["", "   \n\t  "])
    def test_clear(self, env, empty):
        doc = _doc(env)
        first = _save(env, doc["id"], "Rule", 0)
        time.sleep(0.002)
        state = _save(env, doc["id"], empty, 1)
        assert state["instructions"] == ""
        assert state["instructions_version"] == 2
        assert state["instructions_updated_at"] > first["instructions_updated_at"]
        assert _run(env["store"].get_doc(doc["id"]))["has_instructions"] is False

    def test_cap_counts_characters_after_strip(self, env):
        doc = _doc(env)
        exact = "a" * DOC_INSTRUCTIONS_MAX_CHARS
        assert _save(env, doc["id"], f"  {exact}\n", 0)["instructions"] == exact
        with pytest.raises(env["store"].InvalidInstructionsError):
            _save(env, doc["id"], exact + "b", 1)
        assert _state(env, doc["id"])["instructions"] == exact
        assert _state(env, doc["id"])["instructions_version"] == 1

    def test_cap_counts_characters_not_bytes(self, env):
        doc = _doc(env)
        wide = "€" * DOC_INSTRUCTIONS_MAX_CHARS  # 3 bytes each in UTF-8
        assert len(wide.encode("utf-8")) > DOC_INSTRUCTIONS_MAX_CHARS
        assert _save(env, doc["id"], wide, 0)["instructions"] == wide
        with pytest.raises(env["store"].InvalidInstructionsError):
            _save(env, doc["id"], wide + "€", 1)

    @pytest.mark.parametrize("bad", [None, 5, b"rule", ["rule"]])
    def test_non_str_refused(self, env, bad):
        doc = _doc(env)
        with pytest.raises(env["store"].InvalidInstructionsError) as info:
            _save(env, doc["id"], bad, 0)
        assert info.value.code == "invalid_instructions"
        # Mapped like every other doc validation error.
        assert isinstance(info.value, env["store"].DocValidationError)
        assert _versions(env, doc["id"]) == (0, 0, 0)

    @pytest.mark.parametrize("bad", [None, "0", True, 0.0])
    def test_bad_expected_version_refused(self, env, bad):
        doc = _doc(env)
        with pytest.raises(env["store"].InvalidInstructionsError):
            _save(env, doc["id"], "Rule", bad)
        assert _state(env, doc["id"])["instructions"] == ""

    def test_stale_token(self, env):
        doc = _doc(env)
        _save(env, doc["id"], "Live rule", 0)
        with pytest.raises(env["store"].StaleInstructionsError) as info:
            _save(env, doc["id"], "Late rule", 0, user=env["bob"])
        assert info.value.code == "stale_instructions"
        current = info.value.current
        assert current == _state(env, doc["id"])
        assert current["instructions"] == "Live rule"
        assert current["instructions_version"] == 1
        assert current["instructions_updated_by"] == env["alice"]

    def test_stale_token_checked_before_no_op(self, env):
        doc = _doc(env)
        _save(env, doc["id"], "Rule", 0)
        with pytest.raises(env["store"].StaleInstructionsError):
            _save(env, doc["id"], "Rule", 0)

    def test_missing_doc(self, env):
        assert _save(env, "no-such-doc", "Rule", 0) is None
        assert _state(env, "no-such-doc") is None

    def test_identical_text_is_a_no_op(self, env):
        doc = _doc(env)
        saved = _save(env, doc["id"], "Rule\nTwo", 0)
        time.sleep(0.002)
        again = _save(env, doc["id"], "  Rule\nTwo\n", 1, user=env["bob"])
        assert again == saved
        assert _state(env, doc["id"]) == saved

    def test_concurrent_saves_with_one_token(self, env):
        doc = _doc(env)

        async def _both():
            return await asyncio.gather(
                env["store"].update_doc_instructions(
                    doc["id"], "From Alice", expected_version=0, user_id=env["alice"],
                ),
                env["store"].update_doc_instructions(
                    doc["id"], "From Bob", expected_version=0, user_id=env["bob"],
                ),
                return_exceptions=True,
            )

        results = _run(_both())
        landed = [r for r in results if isinstance(r, dict)]
        stale = [r for r in results if isinstance(r, env["store"].StaleInstructionsError)]
        assert len(landed) == 1 and len(stale) == 1, results
        state = _state(env, doc["id"])
        assert state["instructions_version"] == 1
        assert state["instructions"] == landed[0]["instructions"]
        assert stale[0].current["instructions"] == landed[0]["instructions"]

    def test_session_helper_leaves_the_commit_to_the_caller(self, env):
        doc = _doc(env)
        helper = env["store"].write_instructions_in_session

        async def _write(commit):
            async with env["session"]() as db:
                state = await helper(
                    db, doc["id"], "  Raw input  ", expected_version=0,
                    user_id=env["bob"], now=datetime.now(timezone.utc),
                )
                if commit:
                    await db.commit()
                else:
                    await db.rollback()
                return state

        # The helper normalizes the text itself.
        assert _run(_write(commit=False))["instructions"] == "Raw input"
        assert _state(env, doc["id"])["instructions_version"] == 0
        assert _run(_write(commit=True))["instructions_version"] == 1
        assert _state(env, doc["id"])["instructions"] == "Raw input"


# ---------------------------------------------------------------------------
# 2. Memories
# ---------------------------------------------------------------------------


MEMORY_KEYS = {
    "id", "doc_id", "content", "source", "created_by", "updated_by",
    "created_at", "updated_at",
}


class TestMemories:
    def test_create_shape(self, env):
        doc = _doc(env)
        memory = _add(env, doc["id"], "  The Q3 numbers come from finance.\n")
        assert set(memory) == MEMORY_KEYS
        uuid.UUID(memory["id"])
        assert memory["doc_id"] == doc["id"]
        assert memory["content"] == "The Q3 numbers come from finance."
        assert memory["source"] == f"ui:{env['alice']}"
        assert memory["created_by"] == env["alice"]
        assert memory["updated_by"] == env["alice"]
        assert memory["created_at"] == memory["updated_at"]
        datetime.fromisoformat(memory["created_at"])
        assert _run(env["mem"].get_doc_memory(doc["id"], memory["id"])) == memory

    def test_newlines_kept(self, env):
        doc = _doc(env)
        assert _add(env, doc["id"], "Line one\n  line two\n")["content"] == "Line one\n  line two"

    def test_list_newest_first(self, env):
        doc = _doc(env)
        other = _doc(env, title="Other")
        ids = [_add(env, doc["id"], f"Fact {i}")["id"] for i in range(3)]
        _add(env, other["id"], "Elsewhere")
        listed = _run(env["mem"].list_doc_memories(doc["id"]))
        assert [m["id"] for m in listed] == list(reversed(ids))
        assert all(set(m) == MEMORY_KEYS for m in listed)
        assert _run(env["mem"].list_doc_memories("no-such-doc")) == []

    def test_update(self, env):
        doc = _doc(env)
        memory = _add(env, doc["id"], "Old fact")
        time.sleep(0.002)
        updated = _run(env["mem"].update_doc_memory(
            doc["id"], memory["id"], "  New fact\n", user_id=env["bob"],
        ))
        assert updated["content"] == "New fact"
        assert updated["updated_by"] == env["bob"]
        assert updated["updated_at"] > memory["updated_at"]
        for key in ("id", "doc_id", "source", "created_by", "created_at"):
            assert updated[key] == memory[key]
        assert _run(env["mem"].get_doc_memory(doc["id"], memory["id"])) == updated

    def test_update_identical_text_is_a_no_op(self, env):
        doc = _doc(env)
        memory = _add(env, doc["id"], "Fact")
        before = _versions(env, doc["id"])
        same = _run(env["mem"].update_doc_memory(
            doc["id"], memory["id"], " Fact ", user_id=env["bob"],
        ))
        assert same == memory
        assert _run(env["mem"].get_doc_memory(doc["id"], memory["id"])) == memory
        assert _versions(env, doc["id"]) == before

    def test_delete(self, env):
        doc = _doc(env)
        keep = _add(env, doc["id"], "Keep")
        gone = _add(env, doc["id"], "Gone")
        assert _run(env["mem"].delete_doc_memory(doc["id"], gone["id"])) is True
        assert _run(env["mem"].get_doc_memory(doc["id"], gone["id"])) is None
        assert [m["id"] for m in _run(env["mem"].list_doc_memories(doc["id"]))] == [keep["id"]]
        assert _run(env["mem"].delete_doc_memory(doc["id"], gone["id"])) is False

    def test_missing_memory(self, env):
        doc = _doc(env)
        assert _run(env["mem"].get_doc_memory(doc["id"], "nope")) is None
        assert _run(env["mem"].update_doc_memory(
            doc["id"], "nope", "Fact", user_id=env["alice"],
        )) is None
        assert _run(env["mem"].delete_doc_memory(doc["id"], "nope")) is False
        assert _versions(env, doc["id"]) == (0, 0, 0)

    def test_missing_doc(self, env):
        assert _add(env, "no-such-doc", "Fact") is None
        assert _run(env["mem"].update_doc_memory(
            "no-such-doc", "nope", "Fact", user_id=env["alice"],
        )) is None
        assert _run(env["mem"].delete_doc_memory("no-such-doc", "nope")) is False
        assert _scalar(env, "SELECT count(*) FROM doc_memories") == 0

    def test_memory_of_another_doc_is_unreachable(self, env):
        doc = _doc(env)
        other = _doc(env, title="Other")
        memory = _add(env, doc["id"], "Fact")
        doc_before, other_before = _versions(env, doc["id"]), _versions(env, other["id"])
        assert _run(env["mem"].get_doc_memory(other["id"], memory["id"])) is None
        assert _run(env["mem"].update_doc_memory(
            other["id"], memory["id"], "Hijack", user_id=env["bob"],
        )) is None
        assert _run(env["mem"].delete_doc_memory(other["id"], memory["id"])) is False
        assert _run(env["mem"].get_doc_memory(doc["id"], memory["id"])) == memory
        assert _versions(env, doc["id"]) == doc_before
        assert _versions(env, other["id"]) == other_before

    def test_content_cap_counts_characters_after_strip(self, env):
        doc = _doc(env)
        exact = "m" * DOC_MEMORY_MAX_CHARS
        memory = _add(env, doc["id"], f"\n {exact} \n")
        assert memory["content"] == exact
        with pytest.raises(env["mem"].InvalidMemoryError) as info:
            _add(env, doc["id"], exact + "m")
        assert info.value.code == "invalid_memory"
        with pytest.raises(env["mem"].InvalidMemoryError):
            _run(env["mem"].update_doc_memory(
                doc["id"], memory["id"], "x" * (DOC_MEMORY_MAX_CHARS + 1), user_id=env["alice"],
            ))
        assert _memory_rows(env, doc["id"]) == 1

    def test_content_cap_counts_characters_not_bytes(self, env):
        doc = _doc(env)
        wide = "é" * DOC_MEMORY_MAX_CHARS  # 2 bytes each in UTF-8
        assert _add(env, doc["id"], wide)["content"] == wide
        with pytest.raises(env["mem"].InvalidMemoryError):
            _add(env, doc["id"], "è" * (DOC_MEMORY_MAX_CHARS + 1))

    @pytest.mark.parametrize("bad", ["", "   \n\t ", None, 7, b"fact", ["fact"]])
    def test_invalid_content_refused(self, env, bad):
        doc = _doc(env)
        memory = _add(env, doc["id"], "Fact")
        before = _versions(env, doc["id"])
        with pytest.raises(env["mem"].InvalidMemoryError):
            _add(env, doc["id"], bad)
        with pytest.raises(env["mem"].InvalidMemoryError):
            _run(env["mem"].update_doc_memory(doc["id"], memory["id"], bad, user_id=env["alice"]))
        assert _versions(env, doc["id"]) == before
        assert _run(env["mem"].get_doc_memory(doc["id"], memory["id"])) == memory

    def test_duplicate_on_create(self, env):
        doc = _doc(env)
        first = _add(env, doc["id"], "The board reads this on Fridays.")
        before = _versions(env, doc["id"])
        with pytest.raises(env["mem"].DuplicateMemoryError) as info:
            _add(env, doc["id"], "  The board reads this on Fridays.\n", user=env["bob"])
        assert info.value.code == "duplicate_memory"
        assert info.value.memory_id == first["id"]
        assert _versions(env, doc["id"]) == before
        assert _memory_rows(env, doc["id"]) == 1

    def test_duplicate_is_exact_text(self, env):
        doc = _doc(env)
        _add(env, doc["id"], "Fact")
        assert _add(env, doc["id"], "fact")["content"] == "fact"
        assert _add(env, doc["id"], "Fact.")["content"] == "Fact."

    def test_duplicate_on_update_into_another_memory(self, env):
        doc = _doc(env)
        one = _add(env, doc["id"], "One")
        two = _add(env, doc["id"], "Two")
        before = _versions(env, doc["id"])
        with pytest.raises(env["mem"].DuplicateMemoryError) as info:
            _run(env["mem"].update_doc_memory(doc["id"], two["id"], " One ", user_id=env["bob"]))
        assert info.value.memory_id == one["id"]
        assert _run(env["mem"].get_doc_memory(doc["id"], two["id"])) == two
        assert _versions(env, doc["id"]) == before

    def test_same_text_on_another_doc_allowed(self, env):
        doc = _doc(env)
        other = _doc(env, title="Other")
        _add(env, doc["id"], "Shared fact")
        assert _add(env, other["id"], "Shared fact")["doc_id"] == other["id"]

    def test_cap(self, env):
        doc = _doc(env)

        async def _fill():
            for i in range(DOC_MAX_MEMORIES):
                await env["mem"].create_doc_memory(
                    doc["id"], f"Fact {i}", source=env["mem"].ui_source(env["alice"]),
                    user_id=env["alice"],
                )

        _run(_fill())
        assert _versions(env, doc["id"]) == (0, DOC_MAX_MEMORIES, DOC_MAX_MEMORIES)
        with pytest.raises(env["mem"].MemoriesCapReachedError) as info:
            _add(env, doc["id"], "One too many")
        assert info.value.code == "memories_cap_reached"
        # Duplicate is checked before the cap.
        with pytest.raises(env["mem"].DuplicateMemoryError):
            _add(env, doc["id"], "Fact 3")
        assert _versions(env, doc["id"]) == (0, DOC_MAX_MEMORIES, DOC_MAX_MEMORIES)
        assert _memory_rows(env, doc["id"]) == DOC_MAX_MEMORIES
        # Editing at the cap still works; deleting frees a slot.
        newest = _run(env["mem"].list_doc_memories(doc["id"]))[0]
        assert _run(env["mem"].update_doc_memory(
            doc["id"], newest["id"], "Edited", user_id=env["alice"],
        ))["content"] == "Edited"
        assert _run(env["mem"].delete_doc_memory(doc["id"], newest["id"]))
        assert _add(env, doc["id"], "Fits again")["content"] == "Fits again"
        assert _versions(env, doc["id"]) == (0, DOC_MAX_MEMORIES + 3, DOC_MAX_MEMORIES)

    def test_concurrent_creates_never_pass_the_cap(self, env):
        doc = _doc(env)

        async def _race():
            for i in range(DOC_MAX_MEMORIES - 2):
                await env["mem"].create_doc_memory(
                    doc["id"], f"Fact {i}", source=env["mem"].ui_source(env["alice"]),
                    user_id=env["alice"],
                )
            return await asyncio.gather(
                *(
                    env["mem"].create_doc_memory(
                        doc["id"], f"Racer {i}", source=env["mem"].ui_source(env["bob"]),
                        user_id=env["bob"],
                    )
                    for i in range(5)
                ),
                return_exceptions=True,
            )

        results = _run(_race())
        landed = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, env["mem"].MemoriesCapReachedError)]
        assert len(landed) == 2 and len(refused) == 3, results
        assert _memory_rows(env, doc["id"]) == DOC_MAX_MEMORIES
        assert _versions(env, doc["id"]) == (0, DOC_MAX_MEMORIES, DOC_MAX_MEMORIES)

    def test_sources(self, env):
        mem = env["mem"]
        suggestion_id = str(uuid.uuid4())
        assert mem.ui_source(42) == "ui:42"
        assert mem.suggestion_source(suggestion_id) == f"suggestion:{suggestion_id}"
        assert mem.validate_source("ui:7") == "ui:7"
        assert mem.validate_source(f"suggestion:{suggestion_id}") == f"suggestion:{suggestion_id}"
        for bad in (
            "ui", "ui:", "ui:0", "ui:abc", "ui:7 ", "suggestion:", "suggestion:abc",
            f"suggestion:{suggestion_id.upper()}", "conversation:abc", "", None, 7,
        ):
            with pytest.raises(ValueError):
                mem.validate_source(bad)
        for bad in (True, "7", None, 0):
            with pytest.raises(ValueError):
                mem.ui_source(bad)
        with pytest.raises(ValueError):
            mem.suggestion_source("not-a-uuid")

    def test_source_stored_and_bad_source_refused(self, env):
        doc = _doc(env)
        suggestion_id = str(uuid.uuid4())
        memory = _run(env["mem"].create_doc_memory(
            doc["id"], "From a suggestion",
            source=env["mem"].suggestion_source(suggestion_id), user_id=env["bob"],
        ))
        assert memory["source"] == f"suggestion:{suggestion_id}"
        with pytest.raises(ValueError):
            _run(env["mem"].create_doc_memory(
                doc["id"], "Bad source", source="ui", user_id=env["bob"],
            ))
        assert _memory_rows(env, doc["id"]) == 1

    def test_session_helpers_replace_or_insert_in_one_transaction(self, env):
        """The suggestion-accept pattern: overwrite the replaced memory,
        inserting when it is gone -- one bump per landed change."""
        doc = _doc(env)
        target = _add(env, doc["id"], "Old fact")
        mem = env["mem"]

        async def _accept(memory_id, content):
            async with env["session"]() as db:
                now = datetime.now(timezone.utc)
                result = await mem.overwrite_memory_in_session(
                    db, doc["id"], memory_id, content, user_id=env["bob"], now=now,
                )
                if result is None:
                    result = await mem.insert_memory_in_session(
                        db, doc["id"], content,
                        source=mem.suggestion_source(str(uuid.uuid4())),
                        user_id=env["bob"], now=now,
                    )
                await db.commit()
                return result

        replaced = _run(_accept(target["id"], " Replaced fact "))
        assert replaced["id"] == target["id"]
        assert replaced["content"] == "Replaced fact"
        assert replaced["source"] == target["source"]
        assert _versions(env, doc["id"]) == (0, 2, 1)

        inserted = _run(_accept("gone-memory-id", "Inserted fact"))
        assert inserted["id"] != target["id"]
        assert inserted["created_at"] == inserted["updated_at"]
        assert _versions(env, doc["id"]) == (0, 3, 2)

        # An unchanged overwrite inside a committing transaction bumps nothing.
        assert _run(_accept(target["id"], "Replaced fact")) == replaced
        assert _versions(env, doc["id"]) == (0, 3, 2)

    def test_session_helper_refusal_rolls_back_with_the_caller(self, env):
        doc = _doc(env)
        _add(env, doc["id"], "Fact")
        mem = env["mem"]

        async def _attempt():
            async with env["session"]() as db:
                try:
                    await mem.insert_memory_in_session(
                        db, doc["id"], "Fact", source=mem.ui_source(env["bob"]),
                        user_id=env["bob"], now=datetime.now(timezone.utc),
                    )
                except mem.DuplicateMemoryError:
                    await db.rollback()
                    return "refused"

        assert _run(_attempt()) == "refused"
        assert _versions(env, doc["id"]) == (0, 1, 1)


# ---------------------------------------------------------------------------
# 3. Versions: each bumped only by its own kind of change
# ---------------------------------------------------------------------------


class TestVersions:
    def test_instructions_change_leaves_memories_version(self, env):
        doc = _doc(env)
        _add(env, doc["id"], "Fact")
        assert _versions(env, doc["id"]) == (0, 1, 1)
        _save(env, doc["id"], "Rule", 0)
        assert _versions(env, doc["id"]) == (1, 1, 1)
        _save(env, doc["id"], "", 1)
        assert _versions(env, doc["id"]) == (2, 1, 1)

    def test_memory_changes_leave_instructions_version(self, env):
        doc = _doc(env)
        saved = _save(env, doc["id"], "Rule", 0)
        memory = _add(env, doc["id"], "Fact", user=env["bob"])
        assert _versions(env, doc["id"]) == (1, 1, 1)
        _run(env["mem"].update_doc_memory(doc["id"], memory["id"], "Fact 2", user_id=env["bob"]))
        assert _versions(env, doc["id"]) == (1, 2, 1)
        _run(env["mem"].delete_doc_memory(doc["id"], memory["id"]))
        assert _versions(env, doc["id"]) == (1, 3, 0)
        state = _state(env, doc["id"])
        assert state["instructions"] == "Rule"
        assert state["instructions_updated_at"] == saved["instructions_updated_at"]
        assert state["instructions_updated_by"] == env["alice"]

    def test_refused_and_no_op_writes_bump_nothing(self, env):
        doc = _doc(env)
        _save(env, doc["id"], "Rule", 0)
        one = _add(env, doc["id"], "One")
        _add(env, doc["id"], "Two")
        before = _state(env, doc["id"])
        store, mem = env["store"], env["mem"]

        attempts = [
            (lambda: _save(env, doc["id"], "x" * (DOC_INSTRUCTIONS_MAX_CHARS + 1), 1),
             store.InvalidInstructionsError),
            (lambda: _save(env, doc["id"], "Other rule", 0), store.StaleInstructionsError),
            (lambda: _add(env, doc["id"], "   "), mem.InvalidMemoryError),
            (lambda: _add(env, doc["id"], "Two"), mem.DuplicateMemoryError),
            (lambda: _run(mem.update_doc_memory(
                doc["id"], one["id"], "Two", user_id=env["alice"])), mem.DuplicateMemoryError),
        ]
        for attempt, error in attempts:
            with pytest.raises(error):
                attempt()
        assert _save(env, doc["id"], "Rule", 1)["instructions_version"] == 1
        assert _run(mem.update_doc_memory(doc["id"], one["id"], "One", user_id=env["bob"])) == one
        assert _run(mem.update_doc_memory(doc["id"], "nope", "X", user_id=env["bob"])) is None
        assert _run(mem.delete_doc_memory(doc["id"], "nope")) is False
        assert _state(env, doc["id"]) == before


# ---------------------------------------------------------------------------
# 4. memory_count stays in sync
# ---------------------------------------------------------------------------


class TestMemoryCount:
    def test_count_tracks_rows(self, env):
        doc = _doc(env)
        memories = [_add(env, doc["id"], f"Fact {i}") for i in range(3)]
        assert _state(env, doc["id"])["memory_count"] == 3 == _memory_rows(env, doc["id"])
        assert _run(env["store"].get_doc(doc["id"]))["memory_count"] == 3
        _run(env["mem"].delete_doc_memory(doc["id"], memories[1]["id"]))
        assert _state(env, doc["id"])["memory_count"] == 2 == _memory_rows(env, doc["id"])

    def test_count_is_resynced_from_the_rows(self, env):
        doc = _doc(env)
        _add(env, doc["id"], "Fact")
        with env["sync_engine"].begin() as conn:
            conn.execute(text("UPDATE docs SET memory_count = 99 WHERE id = :d"), {"d": doc["id"]})
        _add(env, doc["id"], "Fact 2")
        assert _state(env, doc["id"])["memory_count"] == 2

    def test_concurrent_creates(self, env):
        doc = _doc(env)

        async def _many():
            return await asyncio.gather(*(
                env["mem"].create_doc_memory(
                    doc["id"], f"Concurrent {i}", source=env["mem"].ui_source(env["alice"]),
                    user_id=env["alice"],
                )
                for i in range(8)
            ))

        results = _run(_many())
        assert len({m["id"] for m in results}) == 8
        assert _versions(env, doc["id"]) == (0, 8, 8)
        assert _memory_rows(env, doc["id"]) == 8


# ---------------------------------------------------------------------------
# 5. The body's token and write source are never touched
# ---------------------------------------------------------------------------


class TestBodyTokenUntouched:
    def test_notes_writes_leave_the_doc_row_alone(self, env):
        doc = _doc(env, content_size=12, last_write_source="conversation:c1")
        fixed = ("title", "description", "content_size", "asset_count",
                 "last_write_source", "updated_at", "created_at", "require_approval")

        def _check():
            row = _run(env["store"].get_doc(doc["id"]))
            assert {k: row[k] for k in fixed} == {k: doc[k] for k in fixed}

        _save(env, doc["id"], "Rule", 0)
        _check()
        _save(env, doc["id"], "", 1)
        _check()
        memory = _add(env, doc["id"], "Fact")
        _check()
        _run(env["mem"].update_doc_memory(doc["id"], memory["id"], "Fact 2", user_id=env["bob"]))
        _check()
        _run(env["mem"].delete_doc_memory(doc["id"], memory["id"]))
        _check()


# ---------------------------------------------------------------------------
# 6. Out of update_doc_metadata(), doc dicts, the candidate SQL and UI rows
# ---------------------------------------------------------------------------


def _noted_doc(env, **kw):
    doc = _doc(env, **kw)
    _save(env, doc["id"], "Rule", 0)
    _add(env, doc["id"], "Fact 1")
    _add(env, doc["id"], "Fact 2")
    return doc


NOTED = {"has_instructions": True, "memory_count": 2,
         "instructions_version": 1, "memories_version": 2}


class TestOutOfMetadataAndListRows:
    def test_update_doc_metadata_takes_no_instructions(self, env):
        doc = _doc(env)
        with pytest.raises(TypeError):
            _run(env["store"].update_doc_metadata(doc["id"], instructions="Rule"))
        assert _state(env, doc["id"])["instructions"] == ""

    def test_metadata_and_body_writes_leave_notes_alone(self, env):
        doc = _noted_doc(env)
        before = _state(env, doc["id"])
        renamed = _run(env["store"].update_doc_metadata(
            doc["id"], title="Renamed", description="Why", require_approval=True,
        ))
        assert {k: renamed[k] for k in NOTED} == NOTED
        written = _run(env["store"].update_after_write(
            doc["id"], content_size=5, asset_count=1, last_write_source="ui",
        ))
        assert {k: written[k] for k in NOTED} == NOTED
        assert _state(env, doc["id"]) == before

    def test_doc_dicts_carry_counts_never_text(self, env):
        store = env["store"]
        doc = _noted_doc(env, project_id=env["p1"])
        _run(store.add_share(doc["id"], env["bob"], "read"))
        dicts = [
            _run(store.get_doc(doc["id"])),
            _run(store.get_doc(doc["id"], with_shares=False)),
            *_run(store.list_accessible_docs(
                env["alice"], include_project_docs=True, project_id=env["p1"],
            )),
            *_run(store.list_docs_shared_with(env["bob"])),
            *_run(store.list_docs_for_project(env["p1"])),
        ]
        assert len(dicts) == 5
        for row in dicts:
            assert {k: row[k] for k in store.NOTES_STATE_KEYS} == NOTED
            assert "instructions" not in row
        _save(env, doc["id"], "", 1)
        assert _run(store.get_doc(doc["id"]))["has_instructions"] is False

    def test_candidate_queries_select_length_not_text(self, env):
        store = env["store"]
        doc = _noted_doc(env, project_id=env["p1"])
        _run(store.add_share(doc["id"], env["bob"], "write"))
        statements = []

        def _capture(_conn, _cursor, statement, *_args):
            statements.append(statement)

        engine = env["async_engine"].sync_engine
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            _run(store.get_doc(doc["id"]))
            _run(store.list_accessible_docs(
                env["alice"], include_project_docs=True, project_id=env["p1"],
            ))
            _run(store.list_accessible_docs(env["alice"], include_owned_project_docs=True))
            _run(store.list_docs_shared_with(env["bob"]))
            _run(store.list_docs_for_project(env["p1"]))
            _run(store.update_doc_metadata(doc["id"], title="Renamed"))
            doc_selects = [
                s for s in statements
                if s.lstrip().upper().startswith("SELECT") and "FROM docs" in s
            ]
            statements.clear()
            _run(store.get_notes_state(doc["id"]))
            notes_selects = list(statements)
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        bare = re.compile(r"docs\.instructions\b")
        for statement in doc_selects:
            assert not bare.search(statement.replace("length(docs.instructions)", "")), statement
        # Every Doc entity load (they all carry the cached count) selects
        # the has_instructions expression.
        entity_loads = [s for s in doc_selects if "docs.memory_count" in s]
        assert len(entity_loads) >= 6
        for statement in entity_loads:
            assert "length(docs.instructions) >" in statement, statement
        # get_notes_state is the one read that loads the text.
        assert any(bare.search(s) for s in notes_selects)

    def test_ui_rows_leave_notes_out(self, env):
        from chat.docs.routes import _doc_row, _ui_access

        doc_id = _noted_doc(env)["id"]
        _run(env["store"].add_share(doc_id, env["bob"], "write"))
        doc = _run(env["store"].get_doc(doc_id))
        for user_id in (env["alice"], env["bob"]):
            user = {"id": user_id, "email": f"{user_id}@example.com"}
            row = _doc_row(user, doc, _ui_access(user, doc))
            assert row["title"] == doc["title"]
            for key in (*env["store"].NOTES_STATE_KEYS, "instructions"):
                assert key not in row

    def test_tool_list_rows_leave_notes_out(self, env):
        from chat.docs.routes import _ui_access
        from chat.docs.service import _list_row

        doc = _run(env["store"].get_doc(_noted_doc(env)["id"]))
        user = {"id": env["alice"], "email": "alice@example.com"}
        row = _list_row(doc, _ui_access(user, doc))
        assert row["title"] == doc["title"]
        for key in (*env["store"].NOTES_STATE_KEYS, "instructions"):
            assert key not in row


# ---------------------------------------------------------------------------
# 7. get_notes_state()
# ---------------------------------------------------------------------------


class TestNotesState:
    def test_fresh_doc(self, env):
        doc = _doc(env)
        assert _state(env, doc["id"]) == {
            "doc_id": doc["id"],
            "instructions": "",
            "instructions_version": 0,
            "instructions_updated_at": None,
            "instructions_updated_by": None,
            "memories_version": 0,
            "memory_count": 0,
        }

    def test_after_writes(self, env):
        doc = _doc(env)
        saved = _save(env, doc["id"], "Rule one\nRule two", 0, user=env["carol"])
        _add(env, doc["id"], "Fact")
        state = _state(env, doc["id"])
        assert state["instructions"] == "Rule one\nRule two"
        assert state["instructions_version"] == 1
        assert state["instructions_updated_at"] == saved["instructions_updated_at"]
        assert state["instructions_updated_by"] == env["carol"]
        assert state["memories_version"] == 1
        assert state["memory_count"] == 1

    def test_missing_doc(self, env):
        assert _state(env, "no-such-doc") is None


# ---------------------------------------------------------------------------
# 8. Cascades
# ---------------------------------------------------------------------------


def _two_noted_docs(env):
    """Doc A with two memories and two suggestions; doc B with one of each."""
    a = _doc(env, title="A")
    b = _doc(env, title="B")
    _save(env, a["id"], "Rule A", 0)
    m1 = _add(env, a["id"], "A fact 1")
    m2 = _add(env, a["id"], "A fact 2")
    _add_suggestion(env, a["id"], replaces_memory_id=m1["id"], memory_id=m2["id"])
    _add_suggestion(env, a["id"], kind="instructions", content="Rule A2",
                    base_instructions_version=1)
    mb = _add(env, b["id"], "B fact")
    _add_suggestion(env, b["id"], replaces_memory_id=mb["id"])
    return a, b


class TestCascades:
    def test_delete_doc_removes_its_notes_only(self, env):
        a, b = _two_noted_docs(env)
        b_state = _state(env, b["id"])
        assert _run(env["store"].delete_doc(a["id"])) is True
        assert _memory_rows(env, a["id"]) == 0
        assert _suggestion_rows(env, a["id"]) == 0
        assert _state(env, a["id"]) is None
        assert _memory_rows(env, b["id"]) == 1
        assert _suggestion_rows(env, b["id"]) == 1
        assert _state(env, b["id"]) == b_state

    def test_delete_doc_without_foreign_keys(self, env, monkeypatch):
        a, b = _two_noted_docs(env)
        plain_engine = create_async_engine(f"sqlite+aiosqlite:///{env['db_path']}")
        plain_session = async_sessionmaker(plain_engine, expire_on_commit=False)
        monkeypatch.setattr(env["store"], "AsyncSessionLocal", plain_session)

        async def _fk_state():
            async with plain_session() as db:
                return (await db.execute(text("PRAGMA foreign_keys"))).scalar()

        try:
            assert _run(_fk_state()) == 0
            assert _run(env["store"].delete_doc(a["id"])) is True
        finally:
            _run(plain_engine.dispose())
        assert _memory_rows(env, a["id"]) == 0
        assert _suggestion_rows(env, a["id"]) == 0
        assert _memory_rows(env, b["id"]) == 1
        assert _suggestion_rows(env, b["id"]) == 1

    def test_raw_doc_delete_cascades(self, env):
        a, b = _two_noted_docs(env)
        with env["sync_engine"].begin() as conn:
            conn.execute(text("DELETE FROM docs WHERE id = :d"), {"d": a["id"]})
        assert _memory_rows(env, a["id"]) == 0
        assert _suggestion_rows(env, a["id"]) == 0
        assert _memory_rows(env, b["id"]) == 1
        assert _suggestion_rows(env, b["id"]) == 1

    def test_memory_delete_nulls_suggestion_references(self, env):
        doc = _doc(env)
        m1 = _add(env, doc["id"], "Fact 1")
        m2 = _add(env, doc["id"], "Fact 2")
        sid = _add_suggestion(env, doc["id"], replaces_memory_id=m1["id"], memory_id=m2["id"])
        _run(env["mem"].delete_doc_memory(doc["id"], m1["id"]))
        _run(env["mem"].delete_doc_memory(doc["id"], m2["id"]))
        with env["sync_engine"].connect() as conn:
            row = conn.execute(
                text("SELECT replaces_memory_id, memory_id FROM doc_note_suggestions "
                     "WHERE id = :s"), {"s": sid},
            ).one()
        assert tuple(row) == (None, None)

    def test_user_delete_nulls_authorship(self, env):
        doc = _doc(env)
        _save(env, doc["id"], "Rule", 0, user=env["bob"])
        by_bob = _add(env, doc["id"], "Bob's fact", user=env["bob"])
        by_alice = _add(env, doc["id"], "Alice's fact")
        _run(env["mem"].update_doc_memory(
            doc["id"], by_alice["id"], "Alice's fact, edited by Bob", user_id=env["bob"],
        ))
        before = _versions(env, doc["id"])
        with env["sync_engine"].begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": env["bob"]})
        state = _state(env, doc["id"])
        assert state["instructions"] == "Rule"
        assert state["instructions_updated_by"] is None
        assert _versions(env, doc["id"]) == before
        bob_memory = _run(env["mem"].get_doc_memory(doc["id"], by_bob["id"]))
        assert (bob_memory["created_by"], bob_memory["updated_by"]) == (None, None)
        alice_memory = _run(env["mem"].get_doc_memory(doc["id"], by_alice["id"]))
        assert (alice_memory["created_by"], alice_memory["updated_by"]) == (env["alice"], None)
