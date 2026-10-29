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
   nothing; a change landing mid-read gives counts that match the block
   and a record never newer than it; a doc without notes never carries a block nor writes a
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
   offset, missing doc), ``notes_seen`` (None outside recording runs),
   ``record_notes_delivery`` (storage failures swallowed, caller bugs
   raised), ``notes_counts`` and ``notes_enabled``.

The write side (``DocNotesUnseen`` in the service's write resolve, the
``doc_notes_unseen`` tool result, the ``write_doc`` pre-card and the
approve-time pin):

9. The refusal: ``append_to_doc``, ``add_doc_image`` and ``edit_doc`` (after
   an instructions change) on a noted doc return exactly ``{"error":
   "doc_notes_unseen", "message", "doc_notes"}`` with the block, write
   nothing and record the delivered versions; the identical retry lands; a
   memories-only doc refuses a conversation without a record; the service
   raises ``DocNotesUnseen`` with the block and its text copy; the seen
   record is read once per write (not again under the lock) and never by
   approve-time execute; a refusal records the freshly loaded state, not
   the row; a recording kind without a conversation is never refused; a
   current instructions record skips the notes load.
10. Re-arming: an instructions change after a read refuses the next write
    once; memory changes and cleared instructions never refuse (the next
    read re-attaches), also when cleared between the row lookup and the
    notes load.
11. A doc without notes is never refused by the three tools nor the
    pre-card, writes no record and loads no notes.
12. The creator exception on writes, re-armed by a later instructions change.
13. Order: denied, then unread, then the notes, then ``approval_required``
    (whose ``suggested_request`` never carries a server-only pin).
14. Run kinds: a Slack run is refused once on its own doc; the read-only
    run kinds get their deny text before the step, recording nothing.
15. The ``write_doc`` pre-card: the refusal text (message, blank line,
    ``render_notes_text()``, blank line, the block's ``note``), through ``propose`` and the
    ``create_action_request`` arm (no card), for each operation; the retry
    pins the live ``instructions_version``; model-supplied
    ``instructions_version`` dropped and ``expected_instructions_version``
    refused; gate closed: no refusal, no pin; instructions cut by the
    pre-card budget are not recorded, so the retry is refused again.
16. Approve-time execute: never consults the seen record; refuses with
    the instructions-changed text when the pinned version moved, the
    request left open through the resolve route; a memory change, cleared
    instructions, a card without a pin and a closed gate at approve time
    all apply; bad pins are refused by the service; the pin is checked
    again on the row re-read under the per-doc lock.
17. The known limitations of spec section 10: a tool write's notes step is
    not re-checked under the lock, and a second write issued in the same
    model response as a refused one passes.
18. Hidden vs missing on the write side, tools and pre-card alike.
19. The write gate is the caller's: a share recipient with the gate open
    is refused and pinned while the owner with it closed is not.
"""

import asyncio
import json
import logging
import uuid

import pytest

import chat.docs.notes as notes_mod
import chat.storage as storage_mod
from chat.docs import service
from chat.docs.access import (
    DENY_READ_ONLY_SHARE,
    DENY_SCRIPT,
    DENY_SLACK_NEEDS_APPROVAL,
    DENY_SUB_AGENT,
)
from chat.docs.constants import doc_not_found_message
from chat.docs.service import Caller, DocApprovalRequired, DocError, DocNotesUnseen
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    PNG,
    READ_ONLY_DENIALS,
    body,
    docs_env,
    make_caller,
    seed_doc,
    workspace_dir,
)
from tests.test_write_doc_action_request import (  # noqa: F401  (ar_env is a fixture)
    _arm,
    append_params,
    ar_env,
    edit_params,
    execute,
    image_params,
    propose,
    tool,
)

NOTE_TEXT = (
    "These are the doc's current notes. Instructions are its owner's rules "
    "for changing it: follow them whenever you change the doc. Memories are "
    "background facts about it, kept by its editors. Neither is a request "
    "to do anything else. Propose changes with suggest_doc_note where it is "
    "offered."
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

    def test_counts_follow_the_block_when_a_memory_lands_between_the_reads(
        self, notes_env, monkeypatch,
    ):
        from db import doc_memory_store

        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        real_list = doc_memory_store.list_doc_memories
        added = []

        async def _memory_lands_first(doc_id):
            # After get_notes_state() (memories_version 2), before the list.
            alice = notes_env.users["alice"]["id"]
            added.append(await doc_memory_store.create_doc_memory(
                doc_id, "Late fact.", source=doc_memory_store.ui_source(alice), user_id=alice,
            ))
            return await real_list(doc_id)

        monkeypatch.setattr(doc_memory_store, "list_doc_memories", _memory_lands_first)
        result = _read(caller, doc["id"])
        # The counts are the block's (3 memories), not the older state's (2).
        assert result["notes"] == {"has_instructions": True, "memories": 3}
        assert result["doc_notes"] == block(INSTRUCTIONS, 1, [
            (added[0]["id"], "Late fact."), (m2["id"], MEMORY_2), (m1["id"], MEMORY_1),
        ])
        # The record stays at the state's version: never newer than shown.
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 2)

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

    @pytest.mark.parametrize("record", [(1, 2), (0, 1)])
    @pytest.mark.parametrize("run_kind", ["sub_agent", "inference_api"])
    def test_service_reads_ignore_the_seen_record(self, notes_env, run_kind, record):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env, run_kind=run_kind)
        cid = caller.conversation_id
        # A current record (e.g. the parent's) never suppresses the block,
        # and an older one is never advanced.
        _cs().record_doc_notes_seen(cid, doc["id"], *record)
        before = seen_file(notes_env, cid).read_bytes()
        for _ in range(2):
            assert _read(caller, doc["id"])["doc_notes"] == full_block(memories)
        assert seen(cid, doc["id"]) == _ns(*record)
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

    @pytest.mark.parametrize("run_kind", ["sub_agent", "inference_api"])
    def test_notes_seen_is_none_outside_recording_runs(self, notes_env, run_kind):
        # A sub-agent shares its parent's conversation id; the parent's
        # record is not its own.
        caller = make_caller(notes_env, run_kind=run_kind)
        _cs().record_doc_notes_seen(caller.conversation_id, "doc-1", 3, 4)
        assert service.notes_seen(caller, "doc-1") is None

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

    def test_record_notes_delivery_raises_on_bad_arguments(self, notes_env):
        # A caller bug, never swallowed (only storage failures are).
        caller = make_caller(notes_env)
        with pytest.raises(ValueError, match="instructions_version"):
            service.record_notes_delivery(caller, "doc-1", -1, 4)
        assert seen_files(notes_env) == []

    def test_record_notes_delivery_swallows_a_bad_conversation_id(self, notes_env, caplog):
        caller = Caller(user=notes_env.users["alice"], conversation_id="../x",
                        project_id=None, is_public=False, run_kind="top_level")
        with caplog.at_level(logging.WARNING, logger="chat.docs.service"):
            service.record_notes_delivery(caller, "doc-1", 3, 4)
        assert "could not record doc notes delivery" in caplog.text

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


# ---------------------------------------------------------------------------
# Write side: shared literals and helpers
# ---------------------------------------------------------------------------


UNSEEN_PLAN = (
    "Doc 'Plan' has notes you have not seen; they are below. Adjust the "
    "change if they require it, then repeat the call: it will not be "
    "refused for these notes again. Check any other change to this doc "
    "sent in the same response against them too."
)
INSTRUCTIONS_CHANGED = (
    "The doc's instructions changed after this change was proposed. Use "
    "Revise to ask the agent to propose it again under the current "
    "instructions."
)
NOT_READ_PLAN = (
    "Doc 'Plan' has not been read in this conversation. Read it with "
    "read_doc before editing."
)
APPROVAL_REQUIRED = (
    'This doc is shared; propose the change with '
    'create_action_request(request_type="write_doc", ...)'
)
# render_notes_text() of full_block(): the pre-card's copy of the notes.
FULL_TEXT = (
    "Instructions (follow them whenever you change this doc):\n"
    "Newest entry first.\n"
    "Never change the Risks section.\n"
    "\n"
    "Memories (newest first):\n"
    "- Q3 figures come from the finance table.\n"
    "- The board reads this on Fridays."
)
# The pre-card refusal: the message, the text copy, then the block's note.
PRECARD_REFUSAL = UNSEEN_PLAN + "\n\n" + FULL_TEXT + "\n\n" + NOTE_TEXT
ORIGINAL = "line one\nline two\n"
CARD_KEYS = [
    "operation", "doc_id", "content", "ensure_blank_line", "current_title",
    "doc_mode", "doc_scope", "share_summary", "require_approval", "content_diff",
]


def unseen_payload(notes_block, message=UNSEEN_PLAN):
    return {"error": "doc_notes_unseen", "message": message, "doc_notes": notes_block}


def _append(caller, doc_id, content):
    return tool("append_to_doc", caller, {"doc_id": doc_id, "content": content})


def _assert_landed(result, *keys):
    """A write tool's success payload: no error, the given keys first."""
    assert "error" not in result, result
    assert list(result)[: len(keys)] == list(keys)


def _assets(env, doc_id):
    return sorted(p.name for p in (env.dirs["docs"] / doc_id / "assets").iterdir())


@pytest.fixture()
def notes_ar_env(request):
    """``ar_env`` (action-request stores on the docs DB), notes gate open."""
    env = request.getfixturevalue("ar_env")
    env.fg.set_feature_enabled(env.fg.FEATURE_DOC_NOTES, True)
    return env


class _Req:
    app = None


def _resolve(caller, request_id):
    from chat.action_request_routes import ResolveRequestBody, resolve_user_action_request

    return _run(resolve_user_action_request(
        request_id, ResolveRequestBody(action="execute"), _Req(), caller.user,
    ))


# ---------------------------------------------------------------------------
# 9. The write refusal through the write tools
# ---------------------------------------------------------------------------


class TestWriteRefusal:
    def test_copy_constants(self):
        assert notes_mod.DOC_NOTES_UNSEEN_MESSAGE.format(title="Plan") == UNSEEN_PLAN
        assert notes_mod.DOC_INSTRUCTIONS_CHANGED_MESSAGE == INSTRUCTIONS_CHANGED

    def test_append_refused_once_then_lands(self, notes_env):
        doc, memories = noted_doc(notes_env)
        cid = str(uuid.uuid4())
        args = {"doc_id": doc["id"], "content": "line three"}

        raw = _raw_dispatch(notes_env, "append_to_doc", args, cid=cid)
        assert list(json.loads(raw)) == ["error", "message", "doc_notes"]
        assert raw == json.dumps({
            "error": "doc_notes_unseen",
            "message": UNSEEN_PLAN,
            "doc_notes": full_block(memories),
        })
        assert body(doc["id"]) == ORIGINAL
        assert seen(cid, doc["id"]) == _ns(1, 2)
        assert json.loads(seen_file(notes_env, cid).read_text()) == {
            "docs": {doc["id"]: {"instructions": 1, "memories": 2}},
        }

        retry = json.loads(_raw_dispatch(notes_env, "append_to_doc", args, cid=cid))
        assert retry == {
            "appended_lines": 1, "total_lines": 4, "updated_at": retry["updated_at"],
        }
        assert body(doc["id"]) == ORIGINAL + "\nline three\n"
        assert seen(cid, doc["id"]) == _ns(1, 2)

    def test_add_doc_image_refused_once_then_lands(self, notes_env):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        (workspace_dir(notes_env, caller) / "plot.png").write_bytes(PNG)
        args = {"doc_id": doc["id"], "workspace_path": "plot.png", "alt": "Plot"}

        refused = tool("add_doc_image", caller, args)
        assert json.dumps(refused) == json.dumps(unseen_payload(full_block(memories)))
        assert body(doc["id"]) == ORIGINAL
        assert _assets(notes_env, doc["id"]) == []
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 2)

        result = tool("add_doc_image", caller, args)
        assert result == {
            "asset": "plot.png", "markdown": "![Plot](assets/plot.png)",
            "appended": True, "asset_count": 1,
            "updated_at": result["updated_at"], "total_lines": 4,
        }
        assert body(doc["id"]) == ORIGINAL + "\n![Plot](assets/plot.png)\n"
        assert _assets(notes_env, doc["id"]) == ["plot.png"]

    def test_edit_doc_refused_after_an_instructions_change(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        _read(caller, doc["id"])
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        args = {"doc_id": doc["id"], "old_string": "line two", "new_string": "line 2"}

        refused = tool("edit_doc", caller, args)
        assert json.dumps(refused) == json.dumps(unseen_payload(block(
            "Figures in USD thousands.", 2, [(m2["id"], MEMORY_2), (m1["id"], MEMORY_1)],
        )))
        assert body(doc["id"]) == ORIGINAL
        assert seen(caller.conversation_id, doc["id"]) == _ns(2, 2)

        result = tool("edit_doc", caller, args)
        assert result == {"replaced": 1, "total_lines": 2, "updated_at": result["updated_at"]}
        assert body(doc["id"]) == "line one\nline 2\n"

    def test_memories_only_doc_refuses_a_conversation_without_a_record(self, notes_env):
        doc = seed_doc(notes_env, title="Plan")
        memory = add_memory(notes_env, doc["id"], MEMORY_1)
        caller = make_caller(notes_env)
        assert _append(caller, doc["id"], "x") == unseen_payload(
            block("", 0, [(memory["id"], MEMORY_1)]),
        )
        assert seen(caller.conversation_id, doc["id"]) == _ns(0, 1)
        _assert_landed(_append(caller, doc["id"], "x"), "appended_lines")

    def test_service_raises_doc_notes_unseen(self, notes_env):
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        with pytest.raises(DocNotesUnseen) as exc:
            _run(service.append_to_doc(caller, doc["id"], "x"))
        assert isinstance(exc.value, DocError)
        assert str(exc.value) == UNSEEN_PLAN
        assert exc.value.notes_block == full_block(memories)
        assert exc.value.notes_text == FULL_TEXT
        assert "DocNotesUnseen" in service.__all__

    def test_record_read_once_per_write_outside_the_lock(self, notes_env, monkeypatch):
        """The step reads the seen record once, in the first pass: the
        re-resolve under the per-doc lock does not repeat it (spec section
        10), and approve-time execute never reads it."""
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        _read(caller, doc["id"])
        calls = []
        real = service.notes_seen

        def _counting(caller_, doc_id):
            calls.append(doc_id)
            return real(caller_, doc_id)

        monkeypatch.setattr(service, "notes_seen", _counting)
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        assert calls == [doc["id"]]

        calls.clear()
        _run(service.apply_write_operation(
            caller, doc["id"], "append",
            {"content": "b", "expected_instructions_version": 1},
            write_source="ui", bypass_approval=True,
        ))
        assert calls == []
        assert body(doc["id"]) == ORIGINAL + "\na\n\nb\n"

    def test_refusal_records_the_loaded_state_not_the_row(self, notes_env, monkeypatch):
        """A memory landing after the row lookup: the refusal carries and
        records the freshly loaded state (memories version 3), never the
        row's older versions."""
        from db import doc_memory_store

        doc, (m1, m2) = noted_doc(notes_env)  # row: (1, 2)
        caller = make_caller(notes_env)
        real_state = service.doc_store.get_notes_state
        added = []

        async def _memory_lands_first(doc_id):
            alice = notes_env.users["alice"]["id"]
            added.append(await doc_memory_store.create_doc_memory(
                doc_id, "Late fact.", source=doc_memory_store.ui_source(alice), user_id=alice,
            ))
            return await real_state(doc_id)

        monkeypatch.setattr(service.doc_store, "get_notes_state", _memory_lands_first)
        assert _append(caller, doc["id"], "x") == unseen_payload(block(INSTRUCTIONS, 1, [
            (added[0]["id"], "Late fact."), (m2["id"], MEMORY_2), (m1["id"], MEMORY_1),
        ]))
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 3)

    def test_recording_kind_without_a_conversation_is_never_refused(self, notes_env, monkeypatch):
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env, cid=None)
        _trap_notes_reads(monkeypatch)
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        assert body(doc["id"]) == ORIGINAL + "\na\n"
        assert seen_files(notes_env) == []

    def test_current_instructions_skip_the_notes_load(self, notes_env, monkeypatch):
        """The row decides first: with the instructions seen, a memory
        change needs no notes query before the write lands."""
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        _read(caller, doc["id"])  # records (1, 2)
        add_memory(notes_env, doc["id"], "Third fact.")  # row: (1, 3)

        async def _no_load(*_a, **_k):
            raise AssertionError("notes loaded for a write that cannot be refused")

        monkeypatch.setattr(service, "load_notes", _no_load)
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 2)


# ---------------------------------------------------------------------------
# 10. Re-arming: instructions changes refuse, memory changes never do
# ---------------------------------------------------------------------------


class TestWriteRearm:
    def test_instructions_change_refuses_the_next_write_once(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        _read(caller, doc["id"])
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")

        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        assert _append(caller, doc["id"], "b") == unseen_payload(block(
            "Figures in USD thousands.", 2, [(m2["id"], MEMORY_2), (m1["id"], MEMORY_1)],
        ))
        assert seen(cid, doc["id"]) == _ns(2, 2)
        _assert_landed(_append(caller, doc["id"], "b"), "appended_lines")
        assert body(doc["id"]) == ORIGINAL + "\na\n\nb\n"
        # The refusal delivered the notes: the next read has nothing new.
        assert "doc_notes" not in _read(caller, doc["id"])

    def test_memory_changes_never_refuse_a_write(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        _read(caller, doc["id"])
        (workspace_dir(notes_env, caller) / "plot.png").write_bytes(PNG)

        m3 = add_memory(notes_env, doc["id"], "Third fact.")
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        edit_memory(notes_env, doc["id"], m1["id"], "The board reads it on Mondays.")
        _assert_landed(tool("edit_doc", caller, {
            "doc_id": doc["id"], "old_string": "line two", "new_string": "line 2",
        }), "replaced")
        delete_memory(doc["id"], m3["id"])
        _assert_landed(tool("add_doc_image", caller, {
            "doc_id": doc["id"], "workspace_path": "plot.png",
        }), "asset")
        assert seen(cid, doc["id"]) == _ns(1, 2)
        assert body(doc["id"]) == "line one\nline 2\n\na\n\n![plot](assets/plot.png)\n"

        # The next read re-attaches the current memories.
        result = _read(caller, doc["id"])
        assert result["doc_notes"] == block(INSTRUCTIONS, 1, [
            (m2["id"], MEMORY_2), (m1["id"], "The board reads it on Mondays."),
        ])
        assert seen(cid, doc["id"]) == _ns(1, 5)

    def test_cleared_instructions_never_refuse(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        _read(caller, doc["id"])

        set_instructions(notes_env, doc["id"], "")  # memories only, version 2
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        delete_memory(doc["id"], m1["id"])
        delete_memory(doc["id"], m2["id"])  # no notes left
        _assert_landed(_append(caller, doc["id"], "b"), "appended_lines")
        assert seen(cid, doc["id"]) == _ns(1, 2)
        assert body(doc["id"]) == ORIGINAL + "\na\n\nb\n"

        # A conversation without a record: the emptied doc has no notes.
        other = make_caller(notes_env)
        _assert_landed(_append(other, doc["id"], "c"), "appended_lines")
        assert not seen_file(notes_env, other.conversation_id).exists()

    def test_instructions_cleared_between_the_row_and_the_load_never_refuse(
        self, notes_env, monkeypatch,
    ):
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        _read(caller, doc["id"])  # records (1, 2)
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        real_state = service.doc_store.get_notes_state

        async def _cleared_meanwhile(doc_id):
            # The owner clears the rule after the row lookup refused the
            # write, before the notes load: the fresh state decides.
            state = await real_state(doc_id)
            await notes_env.doc_store.update_doc_instructions(
                doc_id, "", expected_version=state["instructions_version"],
                user_id=notes_env.users["alice"]["id"],
            )
            return await real_state(doc_id)

        monkeypatch.setattr(service.doc_store, "get_notes_state", _cleared_meanwhile)
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        assert seen(cid, doc["id"]) == _ns(1, 2)
        assert body(doc["id"]) == ORIGINAL + "\na\n"


# ---------------------------------------------------------------------------
# 11. A doc without notes is never refused
# ---------------------------------------------------------------------------


class TestNoNotesNeverRefused:
    def test_tools_and_precard(self, notes_env, monkeypatch):
        from db import doc_memory_store

        def _no_load(*_a, **_k):
            raise AssertionError("notes loaded for a doc without notes")

        async def _ano_load(*_a, **_k):
            _no_load()

        # The row decides: no notes query for a doc without notes.
        monkeypatch.setattr(service.doc_store, "get_notes_state", _ano_load)
        monkeypatch.setattr(doc_memory_store, "list_doc_memories", _ano_load)

        doc = seed_doc(notes_env, title="Plain")
        caller = make_caller(notes_env)
        (workspace_dir(notes_env, caller) / "plot.png").write_bytes(PNG)
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        _read(caller, doc["id"])
        _assert_landed(tool("edit_doc", caller, {
            "doc_id": doc["id"], "old_string": "line two", "new_string": "line 2",
        }), "replaced")
        _assert_landed(tool("add_doc_image", caller, {
            "doc_id": doc["id"], "workspace_path": "plot.png",
        }), "asset")

        shared = seed_doc(notes_env, title="Shared", shares=[("bob", "read")])
        _read(caller, shared["id"])
        for params in (
            append_params(shared["id"]),
            image_params(shared["id"], path="plot.png"),
            edit_params(shared["id"], old="line one", new="LINE ONE"),
        ):
            assert propose(caller, params)["instructions_version"] == 0
        assert seen_files(notes_env) == []


# ---------------------------------------------------------------------------
# 12. The creator exception on writes
# ---------------------------------------------------------------------------


class TestCreatorWrites:
    def test_creator_writes_until_instructions_appear(self, notes_env):
        caller = make_caller(notes_env)
        cid = caller.conversation_id
        doc_id = _run(service.create_doc(caller, "Journal", "first\n"))["id"]

        _assert_landed(_append(caller, doc_id, "a"), "appended_lines")
        # The owner adds a memory: the creator's writes still pass.
        memory = add_memory(notes_env, doc_id, "Started in October.")
        _assert_landed(_append(caller, doc_id, "b"), "appended_lines")
        assert seen(cid, doc_id) == _ns(0, 0)

        # The owner sets instructions: the creator is refused once.
        set_instructions(notes_env, doc_id, "One entry per day.")
        assert _append(caller, doc_id, "c") == unseen_payload(
            block("One entry per day.", 1, [(memory["id"], "Started in October.")]),
            message=(
                "Doc 'Journal' has notes you have not seen; they are below. "
                "Adjust the change if they require it, then repeat the call: "
                "it will not be refused for these notes again. Check any other "
                "change to this doc sent in the same response against them too."
            ),
        )
        assert seen(cid, doc_id) == _ns(1, 1)
        _assert_landed(_append(caller, doc_id, "c"), "appended_lines")
        assert body(doc_id) == "first\n\na\n\nb\n\nc\n"


# ---------------------------------------------------------------------------
# 13. Order: denied -> unread -> notes -> approval_required
# ---------------------------------------------------------------------------


class TestRefusalOrder:
    def test_notes_refusal_comes_before_approval_required(self, notes_env):
        doc, memories = noted_doc(notes_env, shares=[("bob", "read")])
        caller = make_caller(notes_env)
        args = {"doc_id": doc["id"], "content": "line three"}

        refused = tool("append_to_doc", caller, args)
        assert json.dumps(refused) == json.dumps(unseen_payload(full_block(memories)))
        retry = tool("append_to_doc", caller, args)
        assert json.dumps(retry) == json.dumps({
            "error": "approval_required",
            "message": APPROVAL_REQUIRED,
            "suggested_request": {"request_type": "write_doc", "params": {
                "operation": "append", "doc_id": doc["id"],
                "content": "line three", "ensure_blank_line": True,
            }},
        })
        assert body(doc["id"]) == ORIGINAL

    def test_suggested_request_never_carries_a_pin(self, notes_env):
        import hashlib

        doc, _memories = noted_doc(notes_env, shares=[("bob", "read")])
        caller = make_caller(notes_env)
        _read(caller, doc["id"])
        (workspace_dir(notes_env, caller) / "plot.png").write_bytes(PNG)
        with pytest.raises(DocApprovalRequired) as exc:
            _run(service.apply_write_operation(
                caller, doc["id"], "add_image",
                {
                    "workspace_path": "plot.png",
                    "expected_sha256": hashlib.sha256(PNG).hexdigest(),
                    "expected_instructions_version": 1,
                },
                write_source=None,
            ))
        assert exc.value.suggested_request == {"request_type": "write_doc", "params": {
            "operation": "add_image", "doc_id": doc["id"],
            "workspace_path": "plot.png", "alt": "", "placement": "append",
        }}

    def test_denied_comes_first(self, notes_env, monkeypatch):
        doc, _memories = noted_doc(notes_env, shares=[("bob", "read")])
        bob = make_caller(notes_env, "bob")
        (workspace_dir(notes_env, bob) / "plot.png").write_bytes(PNG)
        _trap_notes_reads(monkeypatch)
        for name, extra in (
            ("append_to_doc", {"content": "x"}),
            ("edit_doc", {"old_string": "line one", "new_string": "x"}),
            ("add_doc_image", {"workspace_path": "plot.png"}),
        ):
            assert tool(name, bob, {"doc_id": doc["id"], **extra}) == {
                "error": DENY_READ_ONLY_SHARE,
            }
        with pytest.raises(ValueError) as exc:
            propose(bob, append_params(doc["id"]))
        assert str(exc.value) == DENY_READ_ONLY_SHARE
        assert seen_files(notes_env) == []
        assert body(doc["id"]) == ORIGINAL

    def test_unread_comes_first(self, notes_env, monkeypatch):
        doc, _memories = noted_doc(notes_env)
        shared, _memories = noted_doc(notes_env, title="Shared", shares=[("bob", "read")])
        caller = make_caller(notes_env)
        _trap_notes_reads(monkeypatch)
        assert tool("edit_doc", caller, {
            "doc_id": doc["id"], "old_string": "line two", "new_string": "line 2",
        }) == {"error": NOT_READ_PLAN}
        with pytest.raises(ValueError) as exc:
            propose(caller, edit_params(shared["id"], old="line one", new="x"))
        assert str(exc.value) == (
            "Doc 'Shared' has not been read in this conversation. Read it with "
            "read_doc before editing."
        )
        assert seen_files(notes_env) == []


# ---------------------------------------------------------------------------
# 14. Run kinds
# ---------------------------------------------------------------------------


class TestWriteRunKinds:
    def test_slack_run_refused_once_on_its_own_doc(self, notes_env):
        doc, memories = noted_doc(notes_env)
        cid = str(uuid.uuid4())
        args = {"doc_id": doc["id"], "content": "from slack"}
        raw = _raw_dispatch(notes_env, "append_to_doc", args, cid=cid, is_slack=True)
        assert raw == json.dumps(unseen_payload(full_block(memories)))
        assert seen(cid, doc["id"]) == _ns(1, 2)
        retry = _dispatch(notes_env, "append_to_doc", args, cid=cid, is_slack=True)
        _assert_landed(retry, "appended_lines")
        assert body(doc["id"]) == ORIGINAL + "\nfrom slack\n"

        # The service with a Slack caller, on a second doc.
        other, other_memories = noted_doc(notes_env, title="Other")
        slack = make_caller(notes_env, run_kind="slack")
        with pytest.raises(DocNotesUnseen) as exc:
            _run(service.append_to_doc(slack, other["id"], "x"))
        assert exc.value.notes_block == full_block(other_memories)
        assert seen(slack.conversation_id, other["id"]) == _ns(1, 2)
        _run(service.append_to_doc(slack, other["id"], "x"))

    def test_slack_run_on_a_shared_doc_is_denied_first(self, notes_env, monkeypatch):
        doc, _memories = noted_doc(notes_env, shares=[("bob", "read")])
        slack = make_caller(notes_env, run_kind="slack")
        _trap_notes_reads(monkeypatch)
        assert _append(slack, doc["id"], "x") == {"error": DENY_SLACK_NEEDS_APPROVAL}
        assert seen_files(notes_env) == []

    @pytest.mark.parametrize("run_kind", sorted(READ_ONLY_DENIALS))
    def test_read_only_runs_are_denied_before_the_step(self, notes_env, monkeypatch, run_kind):
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env, run_kind=run_kind)
        _trap_notes_reads(monkeypatch)
        for call in (
            service.edit_doc(caller, doc["id"], "line one", "x"),
            service.append_to_doc(caller, doc["id"], "x"),
            service.add_doc_image(caller, doc["id"], "plot.png"),
        ):
            with pytest.raises(DocError) as exc:
                _run(call)
            assert type(exc.value) is DocError
            assert str(exc.value) == READ_ONLY_DENIALS[run_kind]
        assert seen_files(notes_env) == []
        assert body(doc["id"]) == ORIGINAL

    def test_sub_agent_dispatch_is_denied_and_never_credits_the_parent(self, notes_env):
        doc, _memories = noted_doc(notes_env)
        parent = str(uuid.uuid4())
        result = _dispatch(
            notes_env, "append_to_doc", {"doc_id": doc["id"], "content": "x"},
            cid=parent, is_sub_agent=True,
        )
        assert result == {"error": DENY_SUB_AGENT}
        assert seen_files(notes_env) == []


# ---------------------------------------------------------------------------
# 15. The write_doc pre-card
# ---------------------------------------------------------------------------


class TestPrecardRefusal:
    @pytest.mark.parametrize("make_params", [append_params, image_params])
    def test_refused_once_then_pinned(self, notes_env, make_params):
        doc, _memories = noted_doc(notes_env, shares=[("bob", "read")])
        caller = make_caller(notes_env)
        (workspace_dir(notes_env, caller) / "c.png").write_bytes(PNG)

        with pytest.raises(ValueError) as exc:
            propose(caller, make_params(doc["id"]))
        assert str(exc.value) == PRECARD_REFUSAL
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 2)
        assert body(doc["id"]) == ORIGINAL

        params = propose(caller, make_params(doc["id"]))
        assert list(params)[-1] == "instructions_version"
        assert params["instructions_version"] == 1
        if make_params is append_params:
            assert list(params) == [*CARD_KEYS, "instructions_version"]

    def test_edit_refused_after_an_instructions_change(self, notes_env):
        doc, (m1, m2) = noted_doc(notes_env, shares=[("bob", "read")])
        caller = make_caller(notes_env)
        _read(caller, doc["id"])
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")

        with pytest.raises(ValueError) as exc:
            propose(caller, edit_params(doc["id"], old="line two", new="line 2"))
        assert str(exc.value) == (
            UNSEEN_PLAN + "\n\n"
            "Instructions (follow them whenever you change this doc):\n"
            "Figures in USD thousands.\n"
            "\n"
            "Memories (newest first):\n"
            "- Q3 figures come from the finance table.\n"
            "- The board reads this on Fridays.\n"
            "\n" + NOTE_TEXT
        )
        assert seen(caller.conversation_id, doc["id"]) == _ns(2, 2)
        params = propose(caller, edit_params(doc["id"], old="line two", new="line 2"))
        assert params["instructions_version"] == 2

    def test_arm_refuses_without_a_card(self, notes_ar_env):
        from chat.gemini_api.turn_tools import SuspendForActionRequest

        env = notes_ar_env
        doc, _memories = noted_doc(env, shares=[("bob", "read")])
        caller = make_caller(env)
        user_id = caller.user["id"]

        _ctx, events, result = _arm(caller, append_params(doc["id"]))
        assert result == json.dumps({"error": f"Invalid parameters: {PRECARD_REFUSAL}"})
        assert _run(env.action_request_store.list_action_requests(user_id)) == []
        assert events == []
        assert seen(caller.conversation_id, doc["id"]) == _ns(1, 2)

        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, append_params(doc["id"]))
        row = _run(env.action_request_store.get_action_request(
            user_id, suspended.value.request_id,
        ))
        assert row["status"] == "open"
        assert row["params"]["instructions_version"] == 1
        assert body(doc["id"]) == ORIGINAL

    def test_model_supplied_pins(self, notes_env):
        doc, _memories = noted_doc(notes_env, shares=[("bob", "read")])
        caller = make_caller(notes_env)
        _read(caller, doc["id"])
        # A model copy of the card's key is dropped and replaced.
        params = propose(caller, append_params(doc["id"], instructions_version=99))
        assert params["instructions_version"] == 1
        # The service's own pin is never accepted from the model.
        with pytest.raises(ValueError) as exc:
            propose(caller, append_params(doc["id"], expected_instructions_version=1))
        assert str(exc.value).startswith(
            "Unknown parameter for write_doc: ['expected_instructions_version']. "
        )

    def test_gate_closed_no_refusal_and_no_pin(self, closed_env, monkeypatch):
        doc, _memories = noted_doc(closed_env, shares=[("bob", "read")])
        caller = make_caller(closed_env)
        _trap_notes_reads(monkeypatch)
        params = propose(caller, append_params(doc["id"], instructions_version=1))
        assert list(params) == CARD_KEYS
        assert seen_files(closed_env) == []

    @pytest.mark.parametrize("read_first, recorded", [(False, (0, 2)), (True, (1, 2))])
    def test_cut_instructions_are_not_recorded(
        self, notes_env, monkeypatch, read_first, recorded,
    ):
        doc, _memories = noted_doc(notes_env, shares=[("bob", "read")])
        caller = make_caller(notes_env)
        if read_first:
            _read(caller, doc["id"])  # records (1, 2)
        long_rules = "abcdefghij" * 40
        set_instructions(notes_env, doc["id"], long_rules)  # version 2
        monkeypatch.setattr(service.doc_notes, "DOC_NOTES_PRECARD_MAX_CHARS", 402)
        expected = (
            UNSEEN_PLAN + "\n\n"
            "Instructions (follow them whenever you change this doc):\n"
            + "abcdefghij" * 18 + "\n"
            "[Instructions cut here: 220 more characters. list_doc_notes "
            "returns them in full.]\n"
            "\n"
            "Memories (newest first):\n"
            "(2 more memories not shown; list_doc_notes pages them.)\n"
            "\n" + NOTE_TEXT
        )
        for _ in range(2):
            with pytest.raises(ValueError) as exc:
                propose(caller, append_params(doc["id"]))
            assert str(exc.value) == expected
            # The memories version is recorded, the cut instructions are not.
            assert seen(caller.conversation_id, doc["id"]) == _ns(*recorded)

        # The tools carry the whole block, so their refusal records them.
        refused = _append(caller, doc["id"], "x")
        assert refused["error"] == "doc_notes_unseen"
        assert refused["doc_notes"]["instructions"] == long_rules
        assert seen(caller.conversation_id, doc["id"]) == _ns(2, 2)
        assert propose(caller, append_params(doc["id"]))["instructions_version"] == 2


# ---------------------------------------------------------------------------
# 16. Approve-time execute
# ---------------------------------------------------------------------------


def _card(env, caller, operation):
    """A proposed (pre-carded) write_doc card on a shared noted doc whose
    notes the caller has seen. Returns ``(doc, memories, params, body)``,
    ``body`` being the doc body once the card applies."""
    doc, memories = noted_doc(env, shares=[("bob", "read")])
    _read(caller, doc["id"])
    if operation == "edit":
        params = propose(caller, edit_params(doc["id"], old="line two", new="line 2"))
        return doc, memories, params, "line one\nline 2\n"
    if operation == "add_image":
        (workspace_dir(env, caller) / "c.png").write_bytes(PNG)
        params = propose(caller, image_params(doc["id"], alt="Chart"))
        return doc, memories, params, ORIGINAL + "\n![Chart](assets/c.png)\n"
    params = propose(caller, append_params(doc["id"], content="approved"))
    return doc, memories, params, ORIGINAL + "\napproved\n"


class TestExecute:
    def test_seen_record_is_not_consulted(self, notes_env):
        caller = make_caller(notes_env)
        doc, _memories, params, applied = _card(notes_env, caller, "append")
        assert params["instructions_version"] == 1
        # As compaction would.
        assert _cs().clear_doc_notes_seen(caller.conversation_id) is True
        result = execute(params, caller, request_id=51)
        assert result["appended_lines"] == 1
        assert body(doc["id"]) == applied
        assert not seen_file(notes_env, caller.conversation_id).exists()

    @pytest.mark.parametrize("operation", ["append", "edit", "add_image"])
    def test_instructions_changed_refuses(self, notes_env, operation):
        caller = make_caller(notes_env)
        doc, _memories, params, applied = _card(notes_env, caller, operation)
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        row = _run(notes_env.doc_store.get_doc(doc["id"]))

        with pytest.raises(RuntimeError) as exc:
            execute(params, caller)
        assert str(exc.value) == INSTRUCTIONS_CHANGED
        assert body(doc["id"]) == ORIGINAL
        assert _assets(notes_env, doc["id"]) == []
        assert _run(notes_env.doc_store.get_doc(doc["id"]))["updated_at"] == row["updated_at"]

        # Read the new rules, propose again (validate_params drops the old
        # card's server keys): that card applies.
        _read(caller, doc["id"])
        again = propose(caller, params)
        assert again["instructions_version"] == 2
        execute(again, caller, request_id=52)
        assert body(doc["id"]) == applied

    def test_resolve_route_keeps_the_request_open(self, notes_ar_env):
        from fastapi import HTTPException

        from chat.gemini_api.turn_tools import SuspendForActionRequest

        env = notes_ar_env
        doc, _memories = noted_doc(env, shares=[("bob", "read")])
        caller = make_caller(env)
        _read(caller, doc["id"])
        with pytest.raises(SuspendForActionRequest) as suspended:
            _arm(caller, append_params(doc["id"]))
        request_id = suspended.value.request_id
        set_instructions(env, doc["id"], "Figures in USD thousands.")

        with pytest.raises(HTTPException) as exc:
            _resolve(caller, request_id)
        assert exc.value.status_code == 500
        assert exc.value.detail == {
            "error": "execution_failed",
            "message": f"Failed to execute request: {INSTRUCTIONS_CHANGED}",
        }
        row = _run(env.action_request_store.get_action_request(caller.user["id"], request_id))
        assert row["status"] == "open"
        assert body(doc["id"]) == ORIGINAL

    def test_pin_rechecked_under_the_lock(self, notes_env, monkeypatch):
        """An instructions change landing between the first pass and the
        re-resolve under the per-doc lock still refuses an approved card."""
        caller = make_caller(notes_env)
        doc, _memories, params, _applied = _card(notes_env, caller, "append")
        real_get = service.doc_store.get_doc
        lookups = []

        async def _change_before_the_locked_lookup(doc_id, **kwargs):
            lookups.append(doc_id)
            if len(lookups) == 2:  # the re-resolve under the lock
                state = await notes_env.doc_store.get_notes_state(doc_id)
                await notes_env.doc_store.update_doc_instructions(
                    doc_id, "Figures in USD thousands.",
                    expected_version=state["instructions_version"],
                    user_id=notes_env.users["alice"]["id"],
                )
            return await real_get(doc_id, **kwargs)

        monkeypatch.setattr(service.doc_store, "get_doc", _change_before_the_locked_lookup)
        with pytest.raises(RuntimeError) as exc:
            execute(params, caller)
        assert str(exc.value) == INSTRUCTIONS_CHANGED
        assert len(lookups) == 2
        assert body(doc["id"]) == ORIGINAL

    def test_memory_changes_while_open_never_refuse(self, notes_env):
        caller = make_caller(notes_env)
        doc, (m1, _m2), params, applied = _card(notes_env, caller, "append")
        add_memory(notes_env, doc["id"], "Third fact.")
        edit_memory(notes_env, doc["id"], m1["id"], "The board reads it on Mondays.")
        execute(params, caller, request_id=53)
        assert body(doc["id"]) == applied

    def test_cleared_instructions_never_refuse(self, notes_env):
        caller = make_caller(notes_env)
        doc, _memories, params, applied = _card(notes_env, caller, "append")
        set_instructions(notes_env, doc["id"], "")
        assert _run(notes_env.doc_store.get_doc(doc["id"]))["instructions_version"] == 2
        execute(params, caller, request_id=54)
        assert body(doc["id"]) == applied

    def test_card_without_a_pin_applies(self, closed_env):
        doc, _memories = noted_doc(closed_env, shares=[("bob", "read")])
        caller = make_caller(closed_env)
        params = propose(caller, append_params(doc["id"], content="approved"))
        assert "instructions_version" not in params
        # The gate opens and the instructions change while the card is open.
        closed_env.fg.set_feature_enabled(closed_env.fg.FEATURE_DOC_NOTES, True)
        set_instructions(closed_env, doc["id"], "Figures in USD thousands.")
        execute(params, caller, request_id=55)
        assert body(doc["id"]) == ORIGINAL + "\napproved\n"

    @pytest.mark.parametrize("forged", ["1", True, 1.0, None])
    def test_a_pin_that_is_not_an_int_is_ignored(self, notes_env, forged):
        caller = make_caller(notes_env)
        doc, _memories, params, applied = _card(notes_env, caller, "append")
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        execute({**params, "instructions_version": forged}, caller, request_id=56)
        assert body(doc["id"]) == applied

    def test_gate_closed_at_approve_applies(self, notes_env):
        caller = make_caller(notes_env)
        doc, _memories, params, applied = _card(notes_env, caller, "append")
        assert params["instructions_version"] == 1
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        notes_env.fg.set_feature_enabled(notes_env.fg.FEATURE_DOC_NOTES, False)
        execute(params, caller, request_id=57)
        assert body(doc["id"]) == applied

    @pytest.mark.parametrize("pin", [-1, True, "1", 1.5])
    def test_service_refuses_a_bad_pin(self, notes_env, pin):
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        with pytest.raises(DocError) as exc:
            _run(service.apply_write_operation(
                caller, doc["id"], "append",
                {"content": "x", "expected_instructions_version": pin},
                write_source=None, bypass_approval=True,
            ))
        assert str(exc.value) == "expected_instructions_version must be a non-negative integer."
        assert body(doc["id"]) == ORIGINAL

    def test_service_pin_without_a_card(self, notes_env):
        """The pin is the service's: a bypass write with a current pin
        lands, a moved one refuses, and neither records anything."""
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)

        def write(pin):
            return _run(service.apply_write_operation(
                caller, doc["id"], "append",
                {"content": "x", "expected_instructions_version": pin},
                write_source="ui", bypass_approval=True,
            ))

        write(1)
        with pytest.raises(DocError) as exc:
            write(0)
        assert str(exc.value) == INSTRUCTIONS_CHANGED
        assert body(doc["id"]) == ORIGINAL + "\nx\n"
        assert seen_files(notes_env) == []


# ---------------------------------------------------------------------------
# 17. Known limitation: parallel calls
# ---------------------------------------------------------------------------


class TestParallelCallsLimitation:
    def test_tool_write_not_rechecked_under_the_lock(self, notes_env, monkeypatch):
        """Spec 00011 section 10, a known limitation: a tool write's notes
        step reads the versions before the per-doc lock, so an instructions
        change landing between the check and the write applies from the
        next write on."""
        doc, _memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        _read(caller, doc["id"])  # records (1, 2)
        real_get = service.doc_store.get_doc
        lookups = []

        async def _change_before_the_locked_lookup(doc_id, **kwargs):
            lookups.append(doc_id)
            if len(lookups) == 2:
                state = await notes_env.doc_store.get_notes_state(doc_id)
                await notes_env.doc_store.update_doc_instructions(
                    doc_id, "Figures in USD thousands.",
                    expected_version=state["instructions_version"],
                    user_id=notes_env.users["alice"]["id"],
                )
            return await real_get(doc_id, **kwargs)

        monkeypatch.setattr(service.doc_store, "get_doc", _change_before_the_locked_lookup)
        _assert_landed(_append(caller, doc["id"], "a"), "appended_lines")
        monkeypatch.setattr(service.doc_store, "get_doc", real_get)
        assert _append(caller, doc["id"], "b")["error"] == "doc_notes_unseen"

    def test_second_write_in_the_same_response_passes(self, notes_env):
        """Spec 00011 section 10, a known limitation: a second write issued
        in the same model response as a refused one is not refused, because
        the first refusal already recorded the delivery, although the model
        drafted the second write before it saw the notes. Tracking delivery
        per model step was judged out of proportion."""
        doc, memories = noted_doc(notes_env)
        caller = make_caller(notes_env)
        first = _append(caller, doc["id"], "first")
        assert first == unseen_payload(full_block(memories))
        second = _append(caller, doc["id"], "second")
        assert second == {
            "appended_lines": 1, "total_lines": 4, "updated_at": second["updated_at"],
        }
        assert body(doc["id"]) == ORIGINAL + "\nsecond\n"


# ---------------------------------------------------------------------------
# 18. Hidden vs missing on the write side
# ---------------------------------------------------------------------------


class TestHiddenWriteParity:
    def test_hidden_noted_doc_writes_like_a_missing_id(self, notes_env):
        hidden, _memories = noted_doc(notes_env)
        missing = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        (notes_env.dirs["chats"] / cid / "workspace").mkdir(parents=True)
        (notes_env.dirs["chats"] / cid / "workspace" / "plot.png").write_bytes(PNG)
        for name, extra in (
            ("append_to_doc", {"content": "x"}),
            ("edit_doc", {"old_string": "line one", "new_string": "x"}),
            ("add_doc_image", {"workspace_path": "plot.png"}),
        ):
            raw_hidden = _raw_dispatch(
                notes_env, name, {"doc_id": hidden["id"], **extra}, who="bob", cid=cid,
            )
            raw_missing = _raw_dispatch(
                notes_env, name, {"doc_id": missing, **extra}, who="bob", cid=cid,
            )
            assert raw_hidden == json.dumps({"error": doc_not_found_message(hidden["id"])})
            assert raw_hidden.replace(hidden["id"], "<id>") == raw_missing.replace(missing, "<id>")

        bob = make_caller(notes_env, "bob")
        texts = []
        for doc_id in (hidden["id"], missing):
            with pytest.raises(ValueError) as exc:
                propose(bob, append_params(doc_id))
            texts.append(str(exc.value).replace(doc_id, "<id>"))
        assert texts[0] == texts[1] == doc_not_found_message("<id>")
        assert seen_files(notes_env) == []
        assert body(hidden["id"]) == ORIGINAL


# ---------------------------------------------------------------------------
# 19. The write gate is the caller's
# ---------------------------------------------------------------------------


class TestWriteGatePerCaller:
    def test_only_the_caller_with_the_gate_is_refused_and_pinned(self, notes_env):
        fg = notes_env.fg
        fg.set_feature_allowed_users(fg.FEATURE_DOC_NOTES, ["bob@example.com"])
        doc, memories = noted_doc(notes_env, shares=[("bob", "write")])

        bob = make_caller(notes_env, "bob")
        assert _append(bob, doc["id"], "from bob") == unseen_payload(full_block(memories))
        assert seen(bob.conversation_id, doc["id"]) == _ns(1, 2)
        assert _append(bob, doc["id"], "from bob")["error"] == "approval_required"
        bob_card = propose(bob, append_params(doc["id"], content="from bob"))
        assert bob_card["instructions_version"] == 1

        # The owner, with the gate closed for her: no refusal, no pin.
        alice = make_caller(notes_env)
        alice_card = propose(alice, append_params(doc["id"], content="from alice"))
        assert list(alice_card) == CARD_KEYS
        assert not seen_file(notes_env, alice.conversation_id).exists()

        # The owner changes the rules while both cards are open: the
        # recipient's card (the gate open for bob) refuses, hers applies.
        set_instructions(notes_env, doc["id"], "Figures in USD thousands.")
        with pytest.raises(RuntimeError) as exc:
            execute(bob_card, bob)
        assert str(exc.value) == INSTRUCTIONS_CHANGED
        execute(alice_card, alice, request_id=58)
        assert body(doc["id"]) == ORIGINAL + "\nfrom alice\n"
