"""Doc notes delivery through the Quest Docs service and the doc tools
(chat/docs/service.py, chat/gemini_api/tool_handlers/docs.py), phase A3 of
spec 00011 (docs/architecture/doc-notes.md).

Runs against the isolated ``docs_env`` of tests/test_docs_service.py, with
the ``doc_notes`` gate open for everyone (``notes_env``) or closed
(``closed_env``). Expected payloads are literal.

Covers:

1. Read delivery in recording runs: the first ``read_doc`` of a noted doc
   carries the ``doc_notes`` block and the ``notes`` counts (last, after a
   truncation ``note``) and records both versions; the next read does not
   carry it; a memory added, edited or deleted and an instructions change
   re-attach the current state, also when cleared or deleted down to
   nothing; a change landing mid-read gives counts and a record that match
   the block; a doc without notes never carries a block nor writes a
   record; top-level, Slack and cross-user subagent reads record; a failed
   notes load records nothing.
2. The creator exception: ``create_doc`` records ``(0, 0)``, so the
   creator gets the notes only once someone adds some; with the gate
   closed it records nothing.
3. Runs that never record: sub-agent and inference-API reads carry the
   block on every read of a noted doc whatever the seen record says, never
   advance it (the sub-agent's parent keeps its ``doc_reads.json`` credit),
   and get no block for a doc without notes; a recording run kind without
   a conversation reads the same way; scripts get the counts only.
4. ``list_docs`` / ``search_docs``: every row ends with the counts, never
   any notes content; hidden docs and their counts never appear; nothing
   is recorded.
5. Hidden vs missing: a hidden noted doc reads exactly like a missing id
   and records nothing.
6. The gate is the caller's, never the doc owner's.
7. Invariant 6: with the gate closed, each of the seven doc tools returns
   byte-identical JSON to its pre-notes payload on a noted doc, touches no
   notes store or seen record, and its write lands; with the gate open the
   same read differs.
8. The shared helpers: ``load_notes`` (state read before the memories,
   offset, missing doc), ``notes_seen``, ``record_notes_delivery``,
   ``notes_counts`` and ``notes_enabled``.
"""

import asyncio
import json
import logging
import uuid

import pytest

import chat.docs.notes as notes_mod
import chat.storage as storage_mod
from chat.docs import service
from chat.docs.access import DENY_SCRIPT, DENY_SUB_AGENT
from chat.docs.constants import doc_not_found_message
from chat.docs.service import Caller, DocError
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    PNG,
    body,
    docs_env,
    make_caller,
    seed_doc,
)

NOTE_TEXT = (
    "Follow the instructions whenever you change this doc; they were written "
    "or approved by its owner. Memories are background facts about it, kept "
    "by its editors. Propose changes with suggest_doc_note."
)
INSTRUCTIONS = "Newest entry first.\nNever change the Risks section."
MEMORY_1 = "The board reads this on Fridays."
MEMORY_2 = "Q3 figures come from the finance table."

READ_KEYS = [
    "id", "title", "mode", "scope", "total_lines", "content", "shared",
    "writable", "write_note", "start_line", "end_line", "truncated",
]
LIST_KEYS = [
    "id", "title", "description", "mode", "scope", "project_id",
    "content_size", "asset_count", "updated_at", "shared", "writable",
    "write_note",
]
SEARCH_KEYS = ["id", "title", "mode", "scope", "matches"]
NO_NOTES = {"has_instructions": False, "memories": 0}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def closed_env(request):
    """``docs_env`` as is: the ``doc_notes`` gate closed (its default)."""
    return request.getfixturevalue("docs_env")


@pytest.fixture()
def notes_env(closed_env):
    """``docs_env`` with the ``doc_notes`` gate open for everyone."""
    closed_env.fg.set_feature_enabled(closed_env.fg.FEATURE_DOC_NOTES, True)
    return closed_env


# --- Helpers ---


def _cs():
    # Other suites reload chat.storage; bind at call time.
    return storage_mod.ChatStorage


def _ns(instructions, memories):
    return notes_mod.NotesSeen(instructions=instructions, memories=memories)


def set_instructions(env, doc_id, text):
    """Save the instructions from the current version, as alice."""
    state = _run(env.doc_store.get_notes_state(doc_id))
    return _run(env.doc_store.update_doc_instructions(
        doc_id, text,
        expected_version=state["instructions_version"],
        user_id=env.users["alice"]["id"],
    ))


def add_memory(env, doc_id, text):
    from db import doc_memory_store

    alice = env.users["alice"]["id"]
    return _run(doc_memory_store.create_doc_memory(
        doc_id, text, source=doc_memory_store.ui_source(alice), user_id=alice,
    ))


def edit_memory(env, doc_id, memory_id, text):
    from db import doc_memory_store

    return _run(doc_memory_store.update_doc_memory(
        doc_id, memory_id, text, user_id=env.users["alice"]["id"],
    ))


def delete_memory(doc_id, memory_id):
    from db import doc_memory_store

    assert _run(doc_memory_store.delete_doc_memory(doc_id, memory_id))


def seen(cid, doc_id):
    return _cs().get_doc_notes_seen(cid, doc_id)


def seen_file(env, cid):
    return env.dirs["chats"] / cid / "doc_notes_seen.json"


def seen_files(env):
    return sorted(env.dirs["chats"].rglob("doc_notes_seen.json"))


def noted_doc(env, title="Plan", content="line one\nline two\n", **kwargs):
    """A doc with instructions (version 1) and two memories (version 2).

    Returns ``(doc, [memory_1, memory_2])``, memories in creation order.
    """
    doc = seed_doc(env, title=title, content=content, **kwargs)
    set_instructions(env, doc["id"], INSTRUCTIONS)
    memories = [add_memory(env, doc["id"], MEMORY_1), add_memory(env, doc["id"], MEMORY_2)]
    return doc, memories


def block(instructions, instructions_version, memories, memories_omitted=0):
    """A literal notes block; ``memories`` are ``(id, content)`` pairs, newest first."""
    return {
        "instructions": instructions,
        "instructions_version": instructions_version,
        "memories": [{"id": mid, "content": text} for mid, text in memories],
        "memories_omitted": memories_omitted,
        "note": NOTE_TEXT,
    }


def full_block(memories):
    """The block of :func:`noted_doc` as created."""
    m1, m2 = memories
    return block(INSTRUCTIONS, 1, [(m2["id"], MEMORY_2), (m1["id"], MEMORY_1)])


def read_payload(doc, **extra):
    """The read_doc result of a ``noted_doc`` body read whole by its owner."""
    payload = {
        "id": doc["id"],
        "title": doc["title"],
        "mode": "private",
        "scope": "user",
        "total_lines": 2,
        "content": "line one\nline two\n",
        "shared": False,
        "writable": "free",
        "write_note": None,
        "start_line": 1,
        "end_line": 2,
        "truncated": False,
    }
    payload.update(extra)
    return payload


def _raw_dispatch(env, tool, arguments, *, who="alice", cid="new", **kwargs):
    """The JSON string a ``tool_call`` dispatch returns (tests/test_docs_tools.py)."""
    from chat.gemini_api.tool_dispatch import _dispatch_tool_call

    if cid == "new":
        cid = str(uuid.uuid4())
    if cid is not None:
        (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    result, extra = _run(_dispatch_tool_call(
        app=None, provider=None, user=env.users[who],
        conversation_id=cid, timezone="UTC",
        tool_name="tool_call",
        args={"tool_name": tool, "arguments": dict(arguments)},
        **kwargs,
    ))
    assert extra == []
    return result


def _dispatch(env, tool, arguments, **kwargs):
    return json.loads(_raw_dispatch(env, tool, arguments, **kwargs))


def _read(caller, doc_id, **kwargs):
    return _run(service.read_doc(caller, doc_id, **kwargs))


# ---------------------------------------------------------------------------
# 1. Read delivery in recording runs
# ---------------------------------------------------------------------------


class TestRecordingReads:
    def test_first_read_attaches_and_records_second_does_not(self, notes_env):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id

        first = _read(caller, doc["id"])
        expected = read_payload(
            doc,
            notes={"has_instructions": True, "memories": 2},
            doc_notes=full_block(memories),
        )
        assert json.dumps(first) == json.dumps(expected)
        assert seen(cid, doc["id"]) == _ns(1, 2)
        assert json.loads(seen_file(notes_env, cid).read_text()) == {
            "docs": {doc["id"]: {"instructions": 1, "memories": 2}},
        }

        second = _read(caller, doc["id"])
        assert json.dumps(second) == json.dumps(
            read_payload(doc, notes={"has_instructions": True, "memories": 2})
        )
        assert seen(cid, doc["id"]) == _ns(1, 2)
        # The read sidecar is fed as before.
        assert _cs().get_doc_read_ids(cid) == [doc["id"]]

    def test_notes_come_after_the_truncation_note(self, notes_env, monkeypatch):
        from chat.docs import constants

        doc, memories = noted_doc(notes_env, content="aaaa\nbbbb\ncccc\n")
        monkeypatch.setattr(constants, "DOC_READ_MAX_CHARS", 6)
        result = _read(make_caller(notes_env), doc["id"])
        assert list(result) == [*READ_KEYS, "note", "notes", "doc_notes"]
        assert result["note"] == (
            "Content truncated at 5 characters; page with start_line/end_line "
            "(returned lines 1-1 of 3)."
        )
        assert result["doc_notes"] == full_block(memories)

    def test_memory_changes_reattach(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        _read(caller, doc["id"])

        m3 = add_memory(notes_env, doc["id"], "Third fact.")
        result = _read(caller, doc["id"])
        assert result["notes"] == {"has_instructions": True, "memories": 3}
        assert result["doc_notes"] == block(INSTRUCTIONS, 1, [
            (m3["id"], "Third fact."), (m2["id"], MEMORY_2), (m1["id"], MEMORY_1),
        ])
        assert seen(cid, doc["id"]) == _ns(1, 3)
        assert "doc_notes" not in _read(caller, doc["id"])

        edit_memory(notes_env, doc["id"], m1["id"], "The board reads it on Mondays.")
        result = _read(caller, doc["id"])
        assert result["doc_notes"] == block(INSTRUCTIONS, 1, [
            (m3["id"], "Third fact."), (m2["id"], MEMORY_2),
            (m1["id"], "The board reads it on Mondays."),
        ])
        assert seen(cid, doc["id"]) == _ns(1, 4)
        assert "doc_notes" not in _read(caller, doc["id"])

        delete_memory(doc["id"], m3["id"])
        result = _read(caller, doc["id"])
        assert result["notes"] == {"has_instructions": True, "memories": 2}
        assert result["doc_notes"] == block(INSTRUCTIONS, 1, [
            (m2["id"], MEMORY_2), (m1["id"], "The board reads it on Mondays."),
        ])
        assert seen(cid, doc["id"]) == _ns(1, 5)
        assert "doc_notes" not in _read(caller, doc["id"])

    def test_instructions_change_reattaches(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        _read(caller, doc["id"])

        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        result = _read(caller, doc["id"])
        assert result["notes"] == {"has_instructions": True, "memories": 2}
        assert result["doc_notes"] == block(
            "Figures in USD thousands.", 2, [(m2["id"], MEMORY_2), (m1["id"], MEMORY_1)],
        )
        assert seen(caller.conversation_id, doc["id"]) == _ns(2, 2)
        assert "doc_notes" not in _read(caller, doc["id"])

    def test_cleared_notes_reattach_the_current_empty_state(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        _read(caller, doc["id"])

        set_instructions(notes_env, doc["id"], "")
        result = _read(caller, doc["id"])
        assert result["notes"] == {"has_instructions": False, "memories": 2}
        assert result["doc_notes"] == block("", 2, [(m2["id"], MEMORY_2), (m1["id"], MEMORY_1)])
        assert seen(cid, doc["id"]) == _ns(2, 2)

        delete_memory(doc["id"], m1["id"])
        delete_memory(doc["id"], m2["id"])
        result = _read(caller, doc["id"])
        assert list(result) == [*READ_KEYS, "notes", "doc_notes"]
        assert result["notes"] == NO_NOTES
        assert result["doc_notes"] == block("", 2, [])
        assert seen(cid, doc["id"]) == _ns(2, 4)

        # Nothing left and nothing new: no block.
        assert json.dumps(_read(caller, doc["id"])) == json.dumps(
            read_payload(doc, notes=NO_NOTES)
        )

    def test_counts_and_record_follow_the_block_when_notes_change_mid_read(
        self, notes_env, monkeypatch,
    ):
        from db import doc_memory_store

        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        real_state = service.doc_store.get_notes_state
        added = []

        async def _state_after_a_memory_lands(doc_id):
            # A memory saved after the visibility lookup, before the load.
            alice = notes_env.users["alice"]["id"]
            added.append(await doc_memory_store.create_doc_memory(
                doc_id, "Late fact.", source=doc_memory_store.ui_source(alice), user_id=alice,
            ))
            return await real_state(doc_id)

        monkeypatch.setattr(service.doc_store, "get_notes_state", _state_after_a_memory_lands)
        result = _read(caller, doc["id"])
        assert result["notes"] == {"has_instructions": True, "memories": 3}
        assert result["doc_notes"] == block(INSTRUCTIONS, 1, [
            (added[0]["id"], "Late fact."), (m2["id"], MEMORY_2), (m1["id"], MEMORY_1),
        ])
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 3)

    def test_doc_without_notes_never_attaches_nor_records(self, notes_env):
        doc = seed_doc(notes_env)
        caller = make_caller(notes_env)
        for _ in range(2):
            result = _read(caller, doc["id"])
            assert json.dumps(result) == json.dumps(read_payload(doc, notes=NO_NOTES))
        assert not seen_file(notes_env, caller.conversation_id).exists()

    @pytest.mark.parametrize("flags", [{}, {"is_slack": True}, {"is_user_subagent": True}])
    def test_recording_run_kinds_record_through_dispatch(self, notes_env, flags):
        doc, memories = noted_doc(notes_env)
        cid = str(uuid.uuid4())
        first = _dispatch(notes_env, "read_doc", {"doc_id": doc["id"]}, cid=cid, **flags)
        assert first["doc_notes"] == full_block(memories)
        assert first["notes"] == {"has_instructions": True, "memories": 2}
        assert seen(cid, doc["id"]) == _ns(1, 2)
        second = _dispatch(notes_env, "read_doc", {"doc_id": doc["id"]}, cid=cid, **flags)
        assert list(second) == [*READ_KEYS, "notes"]

    @pytest.mark.parametrize("run_kind", ["slack", "user_subagent"])
    def test_recording_run_kinds_record_through_the_service(self, notes_env, run_kind):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env, run_kind=run_kind)
        assert _read(caller, doc["id"])["doc_notes"] == full_block(memories)
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 2)
        assert "doc_notes" not in _read(caller, doc["id"])

    @pytest.mark.parametrize("target", ["state", "memories"])
    def test_failed_notes_load_records_nothing(self, notes_env, monkeypatch, target):
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)

        if target == "state":
            async def _gone(_doc_id):
                return None  # deleted between the visibility check and the load

            monkeypatch.setattr(service.doc_store, "get_notes_state", _gone)
            with pytest.raises(DocError) as exc:
                _read(caller, doc["id"])
            assert str(exc.value) == doc_not_found_message(doc["id"])
        else:
            async def _boom(_doc_id):
                raise RuntimeError("database is locked")

            monkeypatch.setattr(service.doc_memory_store, "list_doc_memories", _boom)
            with pytest.raises(RuntimeError):
                _read(caller, doc["id"])
        assert not seen_file(notes_env, caller.conversation_id).exists()
        assert _cs().get_doc_read_ids(caller.conversation_id) == []


# ---------------------------------------------------------------------------
# 2. The creator exception
# ---------------------------------------------------------------------------


class TestCreatorException:
    @pytest.mark.parametrize("run_kind", ["top_level", "slack"])
    def test_create_records_zero_versions(self, notes_env, run_kind):
        caller = make_caller(notes_env, run_kind=run_kind)
        cid = caller.conversation_id
        created = _run(service.create_doc(caller, "Journal", "first\n"))
        assert list(created) == [
            "id", "title", "mode", "scope", "project_id", "content_size", "updated_at",
        ]
        doc_id = created["id"]
        assert seen(cid, doc_id) == _ns(0, 0)

        result = _read(caller, doc_id)
        assert list(result) == [*READ_KEYS, "notes"]
        assert result["notes"] == NO_NOTES

        memory = add_memory(notes_env, doc_id, "Started in October.")
        result = _read(caller, doc_id)
        assert result["doc_notes"] == block("", 0, [(memory["id"], "Started in October.")])
        assert seen(cid, doc_id) == _ns(0, 1)

        set_instructions(notes_env, doc_id, "One entry per day.")
        result = _read(caller, doc_id)
        assert result["doc_notes"] == block(
            "One entry per day.", 1, [(memory["id"], "Started in October.")],
        )
        assert seen(cid, doc_id) == _ns(1, 1)

    def test_create_through_dispatch_records(self, notes_env):
        cid = str(uuid.uuid4())
        created = _dispatch(notes_env, "create_doc", {"title": "J", "content": "x"}, cid=cid)
        assert seen(cid, created["id"]) == _ns(0, 0)

    def test_create_with_the_gate_closed_records_nothing(self, closed_env):
        caller = make_caller(closed_env)
        _run(service.create_doc(caller, "Journal", "first\n"))
        assert not seen_file(closed_env, caller.conversation_id).exists()


# ---------------------------------------------------------------------------
# 3. Runs that never record
# ---------------------------------------------------------------------------


class TestUnrecordedReads:
    def test_sub_agent_dispatch_attaches_every_read_and_never_credits_the_parent(
        self, notes_env,
    ):
        doc, memories = noted_doc(notes_env)
        parent = str(uuid.uuid4())
        for _ in range(2):
            result = _dispatch(
                notes_env, "read_doc", {"doc_id": doc["id"]}, cid=parent, is_sub_agent=True,
            )
            assert json.dumps(result) == json.dumps(read_payload(
                doc,
                writable="denied",
                write_note=DENY_SUB_AGENT,
                notes={"has_instructions": True, "memories": 2},
                doc_notes=full_block(memories),
            ))
        assert not seen_file(notes_env, parent).exists()
        # doc_reads.json still credits the parent, as before notes.
        assert _cs().get_doc_read_ids(parent) == [doc["id"]]
        # The parent's own first read still gets the notes.
        parent_read = _dispatch(notes_env, "read_doc", {"doc_id": doc["id"]}, cid=parent)
        assert parent_read["doc_notes"] == full_block(memories)
        assert seen(parent, doc["id"]) == _ns(1, 2)

    def test_inference_api_dispatch_attaches_every_read(self, notes_env):
        doc, memories = noted_doc(notes_env)
        cid = str(uuid.uuid4())
        for _ in range(2):
            result = _dispatch(
                notes_env, "read_doc", {"doc_id": doc["id"]}, cid=cid, is_inference_api=True,
            )
            assert result["doc_notes"] == full_block(memories)
            assert result["notes"] == {"has_instructions": True, "memories": 2}
        assert seen_files(notes_env) == []

    @pytest.mark.parametrize("run_kind", ["sub_agent", "inference_api"])
    def test_service_reads_ignore_the_seen_record(self, notes_env, run_kind):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env, run_kind=run_kind)
        cid = caller.conversation_id
        # A current record (e.g. the parent's) never suppresses the block.
        _cs().record_doc_notes_seen(cid, doc["id"], 1, 2)
        before = seen_file(notes_env, cid).read_bytes()
        for _ in range(2):
            assert _read(caller, doc["id"])["doc_notes"] == full_block(memories)
        assert seen_file(notes_env, cid).read_bytes() == before

    @pytest.mark.parametrize("flags", [{"is_sub_agent": True}, {"is_inference_api": True}])
    def test_doc_without_notes_gets_no_block(self, notes_env, flags):
        doc = seed_doc(notes_env)
        result = _dispatch(notes_env, "read_doc", {"doc_id": doc["id"]}, **flags)
        assert list(result) == [*READ_KEYS, "notes"]
        assert result["notes"] == NO_NOTES
        assert seen_files(notes_env) == []

    def test_recording_kind_without_a_conversation_attaches_every_read(self, notes_env):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env, cid=None)
        for _ in range(2):
            assert _read(caller, doc["id"])["doc_notes"] == full_block(memories)
        plain = seed_doc(notes_env, title="Plain")
        assert "doc_notes" not in _read(caller, plain["id"])
        assert seen_files(notes_env) == []

    def test_script_reads_get_counts_only(self, notes_env):
        doc, _memories = noted_doc(notes_env)
        expected = json.dumps(read_payload(
            doc, writable="denied", write_note=DENY_SCRIPT,
            notes={"has_instructions": True, "memories": 2},
        ))
        caller = make_caller(notes_env, run_kind="script")
        assert json.dumps(_read(caller, doc["id"])) == expected
        assert _raw_dispatch(
            notes_env, "read_doc", {"doc_id": doc["id"]}, cid=None, is_script=True,
        ) == expected
        assert seen_files(notes_env) == []

    def test_script_bridge_gets_counts_only(self, notes_env):
        from chat.gemini_api.script_tool_call import (
            ScriptToolCallRequest,
            script_tool_call_endpoint,
        )

        doc, _memories = noted_doc(notes_env)

        def call(tool, arguments):
            response = _run(script_tool_call_endpoint(
                ScriptToolCallRequest(tool_name=tool, arguments=arguments),
                user=notes_env.users["alice"], lease=None,
            ))
            return json.loads(response.body)

        read = call("read_doc", {"doc_id": doc["id"]})
        assert list(read) == [*READ_KEYS, "notes"]
        assert read["notes"] == {"has_instructions": True, "memories": 2}
        listed = call("list_docs", {})
        assert listed["docs"][0]["notes"] == {"has_instructions": True, "memories": 2}
        assert seen_files(notes_env) == []


# ---------------------------------------------------------------------------
# 4. list_docs / search_docs counts
# ---------------------------------------------------------------------------


class TestListAndSearch:
    def _two_docs(self, env):
        noted, _memories = noted_doc(env, title="Noted", content="alpha body\n")
        plain = seed_doc(env, title="Plain", content="alpha plain\n")
        return noted, plain

    def _assert_no_content(self, payload):
        text = json.dumps(payload)
        for secret in ("Newest entry first", "Risks", MEMORY_1, MEMORY_2):
            assert secret not in text

    def test_list_rows_end_with_counts(self, notes_env):
        noted, plain = self._two_docs(notes_env)
        caller = make_caller(notes_env)
        rows = _run(service.list_docs(caller))
        by_id = {row["id"]: row for row in rows}
        assert set(by_id) == {noted["id"], plain["id"]}
        for row in rows:
            assert list(row) == [*LIST_KEYS, "notes"]
        assert by_id[noted["id"]]["notes"] == {"has_instructions": True, "memories": 2}
        assert by_id[plain["id"]]["notes"] == NO_NOTES
        self._assert_no_content(rows)

        listed = _dispatch(notes_env, "list_docs", {})
        assert {d["id"]: d["notes"] for d in listed["docs"]} == {
            noted["id"]: {"has_instructions": True, "memories": 2},
            plain["id"]: NO_NOTES,
        }
        self._assert_no_content(listed)
        assert seen_files(notes_env) == []

    def test_search_rows_end_with_counts(self, notes_env):
        noted, plain = self._two_docs(notes_env)
        found = _run(service.search_docs(make_caller(notes_env), "alpha"))
        by_id = {row["id"]: row for row in found["results"]}
        assert set(by_id) == {noted["id"], plain["id"]}
        for row in found["results"]:
            assert list(row) == [*SEARCH_KEYS, "notes"]
        assert by_id[noted["id"]] == {
            "id": noted["id"], "title": "Noted", "mode": "private", "scope": "user",
            "matches": [{"line": 1, "snippet": "alpha body"}],
            "notes": {"has_instructions": True, "memories": 2},
        }
        assert by_id[plain["id"]]["notes"] == NO_NOTES
        self._assert_no_content(found)
        # Notes are never searched.
        assert _run(service.search_docs(make_caller(notes_env), "Fridays")) == {
            "results": [], "truncated": False,
        }
        assert seen_files(notes_env) == []

    def test_hidden_docs_and_their_counts_never_appear(self, notes_env):
        hidden, _memories = noted_doc(notes_env, title="Hidden", content="alpha one\n")
        shared, _memories = noted_doc(
            notes_env, title="Shared", content="alpha two\n", shares=[("bob", "read")],
        )
        bob = make_caller(notes_env, "bob")
        rows = _run(service.list_docs(bob))
        assert [(row["id"], row["notes"]) for row in rows] == [
            (shared["id"], {"has_instructions": True, "memories": 2}),
        ]
        found = _run(service.search_docs(bob, "alpha"))
        assert [(row["id"], row["notes"]) for row in found["results"]] == [
            (shared["id"], {"has_instructions": True, "memories": 2}),
        ]
        assert hidden["id"] not in json.dumps([rows, found])

        public = make_caller(notes_env, project=notes_env.public_project, public=True)
        assert _run(service.list_docs(public)) == []
        assert _run(service.search_docs(public, "alpha"))["results"] == []


# ---------------------------------------------------------------------------
# 5. Hidden vs missing
# ---------------------------------------------------------------------------


class TestHiddenParity:
    def test_hidden_noted_doc_reads_like_a_missing_id(self, notes_env):
        hidden, _memories = noted_doc(notes_env)
        missing = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        raw_hidden = _raw_dispatch(notes_env, "read_doc", {"doc_id": hidden["id"]},
                                   who="bob", cid=cid)
        raw_missing = _raw_dispatch(notes_env, "read_doc", {"doc_id": missing},
                                    who="bob", cid=cid)
        assert raw_hidden == json.dumps({"error": doc_not_found_message(hidden["id"])})
        assert raw_missing == json.dumps({"error": doc_not_found_message(missing)})
        assert raw_hidden.replace(hidden["id"], "<id>") == raw_missing.replace(missing, "<id>")
        assert seen_files(notes_env) == []

        with pytest.raises(DocError) as exc:
            _read(make_caller(notes_env, "bob"), hidden["id"])
        assert str(exc.value) == doc_not_found_message(hidden["id"])
        assert seen_files(notes_env) == []


# ---------------------------------------------------------------------------
# 6. The gate is the caller's
# ---------------------------------------------------------------------------


class TestGatePerCaller:
    def test_only_the_caller_with_the_gate_gets_notes(self, notes_env):
        fg = notes_env.fg
        fg.set_feature_allowed_users(fg.FEATURE_DOC_NOTES, ["bob@example.com"])
        doc, memories = noted_doc(notes_env, shares=[("bob", "read")])

        bob = make_caller(notes_env, "bob")
        result = _read(bob, doc["id"])
        assert result["doc_notes"] == full_block(memories)
        assert result["notes"] == {"has_instructions": True, "memories": 2}
        assert seen(bob.conversation_id, doc["id"]) == _ns(1, 2)
        assert _run(service.list_docs(bob))[0]["notes"] == {
            "has_instructions": True, "memories": 2,
        }

        # The owner, with the gate closed for her, sees the doc as before.
        alice = make_caller(notes_env)
        result = _read(alice, doc["id"])
        assert list(result) == READ_KEYS
        assert not seen_file(notes_env, alice.conversation_id).exists()
        assert list(_run(service.list_docs(alice))[0]) == LIST_KEYS


# ---------------------------------------------------------------------------
# 7. Invariant 6: gate closed, every doc tool unchanged
# ---------------------------------------------------------------------------


DOC_TOOLS = (
    "list_docs", "search_docs", "read_doc", "create_doc",
    "edit_doc", "append_to_doc", "add_doc_image",
)


def _trap_notes_reads(monkeypatch):
    """Make every notes store read and seen-record access fail loudly."""
    from db import doc_memory_store

    def _boom(*_a, **_k):
        raise AssertionError("doc notes touched while the gate is closed")

    async def _aboom(*_a, **_k):
        _boom()

    monkeypatch.setattr(service.doc_store, "get_notes_state", _aboom)
    monkeypatch.setattr(doc_memory_store, "list_doc_memories", _aboom)
    monkeypatch.setattr(_cs(), "get_doc_notes_seen", staticmethod(_boom))
    monkeypatch.setattr(_cs(), "record_doc_notes_seen", staticmethod(_boom))


class TestGateClosedUnchanged:
    @pytest.mark.parametrize("tool", DOC_TOOLS)
    def test_byte_identical_and_no_record(self, closed_env, monkeypatch, tool):
        env = closed_env
        doc, _memories = noted_doc(env)
        doc_id = doc["id"]
        cid = str(uuid.uuid4())
        (env.dirs["chats"] / cid).mkdir()
        if tool == "edit_doc":
            # edit_doc's read-before-edit prerequisite, gate closed too.
            _raw_dispatch(env, "read_doc", {"doc_id": doc_id}, cid=cid)
        if tool == "add_doc_image":
            workspace = env.dirs["chats"] / cid / "workspace"
            workspace.mkdir()
            (workspace / "plot.png").write_bytes(PNG)
        _trap_notes_reads(monkeypatch)
        row = _run(env.doc_store.get_doc(doc_id))

        arguments = {
            "list_docs": {},
            "search_docs": {"query": "line two"},
            "read_doc": {"doc_id": doc_id},
            "create_doc": {"title": "Fresh", "content": "x\n"},
            "edit_doc": {"doc_id": doc_id, "old_string": "line two", "new_string": "line 2"},
            "append_to_doc": {"doc_id": doc_id, "content": "line three"},
            "add_doc_image": {"doc_id": doc_id, "workspace_path": "plot.png", "alt": "Plot"},
        }[tool]
        raw = _raw_dispatch(env, tool, arguments, cid=cid)
        got = json.loads(raw)

        if tool == "list_docs":
            expected = {"docs": [{
                "id": doc_id, "title": "Plan", "description": "", "mode": "private",
                "scope": "user", "project_id": None,
                "content_size": row["content_size"], "asset_count": 0,
                "updated_at": row["updated_at"], "shared": False,
                "writable": "free", "write_note": None,
            }], "count": 1}
        elif tool == "search_docs":
            expected = {"results": [{
                "id": doc_id, "title": "Plan", "mode": "private", "scope": "user",
                "matches": [{"line": 2, "snippet": "line one\nline two"}],
            }], "truncated": False}
        elif tool == "read_doc":
            expected = read_payload(doc)
        elif tool == "create_doc":
            expected = {
                "id": got["id"], "title": "Fresh", "mode": "private", "scope": "user",
                "project_id": None, "content_size": 2, "updated_at": got["updated_at"],
            }
            assert body(got["id"]) == "x\n"
        elif tool == "edit_doc":
            expected = {"replaced": 1, "total_lines": 2, "updated_at": got["updated_at"]}
            assert body(doc_id) == "line one\nline 2\n"
        elif tool == "append_to_doc":
            expected = {"appended_lines": 1, "total_lines": 4, "updated_at": got["updated_at"]}
            assert body(doc_id) == "line one\nline two\n\nline three\n"
        else:
            expected = {
                "asset": "plot.png", "markdown": "![Plot](assets/plot.png)",
                "appended": True, "asset_count": 1,
                "updated_at": got["updated_at"], "total_lines": 4,
            }
            assert body(doc_id) == "line one\nline two\n\n![Plot](assets/plot.png)\n"
        assert raw == json.dumps(expected)
        if tool in ("edit_doc", "append_to_doc", "add_doc_image"):
            assert got["updated_at"] != row["updated_at"]
        assert seen_files(env) == []

    def test_gate_open_read_differs(self, notes_env):
        doc, memories = noted_doc(notes_env)
        raw = _raw_dispatch(notes_env, "read_doc", {"doc_id": doc["id"]})
        assert raw != json.dumps(read_payload(doc))
        assert raw == json.dumps(read_payload(
            doc,
            notes={"has_instructions": True, "memories": 2},
            doc_notes=full_block(memories),
        ))
        listed = _dispatch(notes_env, "list_docs", {})
        assert list(listed["docs"][0]) == [*LIST_KEYS, "notes"]


# ---------------------------------------------------------------------------
# 8. The shared helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_load_notes_reads_the_state_before_the_memories(self, notes_env, monkeypatch):
        from db import doc_memory_store

        doc, memories = noted_doc(notes_env)
        calls = []
        real_state = service.doc_store.get_notes_state
        real_memories = doc_memory_store.list_doc_memories

        async def _state(doc_id):
            calls.append("state")
            return await real_state(doc_id)

        async def _memories(doc_id):
            calls.append("memories")
            return await real_memories(doc_id)

        monkeypatch.setattr(service.doc_store, "get_notes_state", _state)
        monkeypatch.setattr(doc_memory_store, "list_doc_memories", _memories)
        state, notes_block = _run(service.load_notes(doc["id"]))
        assert calls == ["state", "memories"]
        assert notes_block == full_block(memories)
        assert {k: state[k] for k in (
            "doc_id", "instructions", "has_instructions", "instructions_version",
            "memories_version", "memory_count",
        )} == {
            "doc_id": doc["id"], "instructions": INSTRUCTIONS, "has_instructions": True,
            "instructions_version": 1, "memories_version": 2, "memory_count": 2,
        }

    def test_load_notes_offset(self, notes_env):
        doc, (m1, _m2) = noted_doc(notes_env)
        _state, notes_block = _run(service.load_notes(doc["id"], memory_offset=1))
        assert notes_block == block(INSTRUCTIONS, 1, [(m1["id"], MEMORY_1)])
        _state, notes_block = _run(service.load_notes(doc["id"], memory_offset=5))
        assert notes_block == block(INSTRUCTIONS, 1, [])

    @pytest.mark.parametrize("offset", [-1, True, "1", 1.0, None])
    def test_load_notes_refuses_a_bad_offset_before_any_read(
        self, notes_env, monkeypatch, offset,
    ):
        doc, _memories = noted_doc(notes_env)
        _trap_notes_reads(monkeypatch)
        with pytest.raises(DocError) as exc:
            _run(service.load_notes(doc["id"], memory_offset=offset))
        assert str(exc.value) == "memory_offset must be a non-negative integer."

    def test_load_notes_missing_doc(self, notes_env):
        missing = str(uuid.uuid4())
        with pytest.raises(DocError) as exc:
            _run(service.load_notes(missing))
        assert str(exc.value) == doc_not_found_message(missing)

    def test_notes_seen(self, notes_env, monkeypatch):
        caller = make_caller(notes_env)
        assert service.notes_seen(caller, "doc-1") is None
        _cs().record_doc_notes_seen(caller.conversation_id, "doc-1", 3, 4)
        assert service.notes_seen(caller, "doc-1") == _ns(3, 4)

        # No conversation: None without touching storage.
        _trap_notes_reads(monkeypatch)
        assert service.notes_seen(make_caller(notes_env, cid=None), "doc-1") is None
        assert service.notes_seen(make_caller(notes_env, run_kind="script"), "doc-1") is None

    def test_notes_seen_fails_closed_on_a_bad_conversation_id(self, notes_env, caplog):
        caller = Caller(user=notes_env.users["alice"], conversation_id="../x",
                        project_id=None, is_public=False, run_kind="top_level")
        with caplog.at_level(logging.WARNING, logger="chat.docs.service"):
            assert service.notes_seen(caller, "doc-1") is None
        assert "could not read the doc notes seen record" in caplog.text

    @pytest.mark.parametrize("run_kind", ["top_level", "slack", "user_subagent"])
    def test_record_notes_delivery_records_in_recording_runs(self, notes_env, run_kind):
        caller = make_caller(notes_env, run_kind=run_kind)
        service.record_notes_delivery(caller, "doc-1", 3, 4)
        assert seen(caller.conversation_id, "doc-1") == _ns(3, 4)

    @pytest.mark.parametrize("run_kind", ["sub_agent", "inference_api", "script", "ui"])
    def test_record_notes_delivery_is_a_no_op_elsewhere(self, notes_env, monkeypatch, run_kind):
        caller = make_caller(notes_env, run_kind=run_kind)
        _trap_notes_reads(monkeypatch)
        service.record_notes_delivery(caller, "doc-1", 3, 4)
        service.record_notes_delivery(make_caller(notes_env, cid=None), "doc-1", 3, 4)
        assert seen_files(notes_env) == []

    def test_record_notes_delivery_swallows_os_errors(self, notes_env, monkeypatch, caplog):
        caller = make_caller(notes_env)

        def _fail(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(_cs(), "record_doc_notes_seen", staticmethod(_fail))
        with caplog.at_level(logging.WARNING, logger="chat.docs.service"):
            service.record_notes_delivery(caller, "doc-1", 3, 4)
        assert "could not record doc notes delivery" in caplog.text
        assert seen_files(notes_env) == []

    def test_record_notes_delivery_swallows_bad_arguments(self, notes_env, caplog):
        caller = make_caller(notes_env)
        with caplog.at_level(logging.WARNING, logger="chat.docs.service"):
            service.record_notes_delivery(caller, "doc-1", -1, 4)
        assert "could not record doc notes delivery" in caplog.text
        assert seen_files(notes_env) == []

    def test_record_notes_delivery_refuses_an_unknown_run_kind(self, notes_env):
        caller = Caller(user=notes_env.users["alice"], conversation_id=str(uuid.uuid4()),
                        project_id=None, is_public=False, run_kind="bogus")
        with pytest.raises(ValueError, match="Unknown run kind"):
            service.record_notes_delivery(caller, "doc-1", 3, 4)

    def test_notes_counts(self, notes_env):
        doc, _memories = noted_doc(notes_env)
        row = _run(notes_env.doc_store.get_doc(doc["id"]))
        state = _run(notes_env.doc_store.get_notes_state(doc["id"]))
        for source in (row, state):
            assert service.notes_counts(source) == {"has_instructions": True, "memories": 2}
        plain = _run(notes_env.doc_store.get_doc(seed_doc(notes_env, title="Plain")["id"]))
        assert service.notes_counts(plain) == NO_NOTES

    def test_notes_enabled_is_per_caller(self, notes_env):
        fg = notes_env.fg
        alice, bob = make_caller(notes_env), make_caller(notes_env, "bob")
        assert service.notes_enabled(alice) and service.notes_enabled(bob)
        fg.set_feature_allowed_users(fg.FEATURE_DOC_NOTES, ["bob@example.com"])
        assert not service.notes_enabled(alice)
        assert service.notes_enabled(bob)
        # doc_notes requires docs for the same user.
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, ["alice@example.com"])
        assert not service.notes_enabled(bob)
        fg.set_feature_enabled(fg.FEATURE_DOC_NOTES, False)
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, None)
        assert not service.notes_enabled(alice) and not service.notes_enabled(bob)
