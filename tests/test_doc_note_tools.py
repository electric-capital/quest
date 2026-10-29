"""Doc notes model tools ``list_doc_notes`` and ``suggest_doc_note``, phase
A5 of spec 00011 (docs/architecture/doc-notes.md): the registry specs, the
handlers (chat/gemini_api/tool_handlers/docs.py), the dispatch wiring and
backstop (chat/gemini_api/tool_dispatch.py), ``service.list_doc_notes()``
and the script bridge (chat/gemini_api/script_tool_call.py).

Driven through ``_dispatch_tool_call(tool_call ...)`` (the real dispatch
path) and, for scripts, ``script_tool_call_endpoint``, against the isolated
``docs_env`` of tests/test_docs_service.py with the ``doc_notes`` gate open
(``env``; its connections turn foreign keys on, so every suggesting
conversation gets a real ``conversations`` row). Expected payloads are
literal; hidden-doc bodies are compared as raw JSON strings.

Covers:

1. Registry: both specs with ``requires_service == "doc_notes"``, core
   tier, not mutating (``mutating_tool_call_tools()`` holds neither), their
   parameters, prompt-safe descriptions within budget, both in
   ``PUBLIC_TOOL_CALL_ALLOWLIST``, only ``list_doc_notes`` in
   ``SCRIPT_TOOL_CALL_ALLOWLIST``, handler/registry parity; the
   ``is_routine`` dispatch flag (top-level run kind, default False).
2. Gate closed (``doc_notes`` off, and ``docs`` off): both tools give the
   exact ``doc_notes_disabled`` body for every run kind (top-level,
   routine, Slack, sub-agent, cross-user subagent, inference API, script,
   public), before argument checks, the backstop and visibility, writing
   no seen record and no suggestion row.
3. Hidden vs missing: for a missing id, another user's unshared private
   doc, a private doc from a public conversation and a doc of another
   project, both tools' result strings are byte-identical to read_doc's,
   and nothing is recorded or registered.
4. The ``list_doc_notes`` block: the result shape, the block equal to
   read_doc's ``doc_notes`` for the same state, paging past
   ``DOC_NOTES_DELIVERY_MAX_CHARS`` reaching every memory exactly once, an
   offset past the end, offsets refused (before the doc lookup) or
   coerced.
5. Recording: page 0 records both versions (read_doc then carries no
   block, a write is not refused); a later page advances only the
   instructions of an existing entry (the memories entry is kept, read_doc
   still delivers) and records nothing without one (the first write is
   still refused with the whole notes); Slack, cross-user subagent and
   routine runs record; sub-agent (on its parent's conversation),
   inference-API and script runs never do.
6. ``can_suggest``: owner, write share, read share, the two Slack deny
   causes, a private conversation on a Project Access public doc, a public
   conversation on its own public doc, and every run kind that may not
   suggest.
7. ``suggest_doc_note`` through dispatch: the registered result and row,
   ``already_saved``, the ``NoteSuggestionError`` codes, a deny reason as a
   plain error, ``doc_notes_unseen`` with the block and the identical
   retry, the run-kind backstop (``suggest_not_available``, before
   argument checks and visibility, nothing registered), Slack and public
   conversations admitted.
8. The script bridge: ``list_doc_notes`` returns the full block with
   ``can_suggest: false`` and records nothing; ``suggest_doc_note`` is not
   allowlisted (400).
9. The conversation loop: ``run_conversation_turn`` dispatches with
   ``is_routine`` true for a routine run and false otherwise.
"""

import asyncio
import inspect
import json
import re
import uuid
from datetime import datetime, timezone

import pytest

import chat.storage as storage_mod
from chat.docs import notes as notes_mod
from chat.docs.access import DENY_READ_ONLY_SHARE
from chat.docs.constants import (
    DOC_NOTES_DELIVERY_MAX_CHARS,
    DOC_NOTES_SERVICE_KEY,
    doc_not_found_message,
)
from db import doc_memory_store
from db import doc_note_suggestion_store as store
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    body,
    docs_env,
    give_doc_source,
    seed_doc,
)

NOTES_TOOLS = ("list_doc_notes", "suggest_doc_note")

DISABLED = {
    "error": "doc_notes_disabled",
    "message": (
        "Quest Doc notes are not enabled for this user. An admin can enable "
        "them in Settings > Features (they require Quest Docs)."
    ),
}
NOT_AVAILABLE = {
    "error": "suggest_not_available",
    "message": (
        "suggest_doc_note is only available to the top-level agent of a "
        "user's own conversation, not to sub-agents, cross-user subagents, "
        "inference API runs, sandbox scripts or routine runs. Include the "
        "proposed note in your response instead."
    ),
}
NOTE_TEXT = (
    "Follow the instructions whenever you change this doc; they were written "
    "or approved by its owner. Memories are background facts about it, kept "
    "by its editors. Propose changes with suggest_doc_note."
)
SUGGESTED = (
    "Registered for the user's review in the doc note suggestions panel; "
    "nothing is saved until they accept it. Keep going."
)
ALREADY_SAVED_MEMORY = "This doc already has a memory with exactly this text; nothing to suggest."
OWNER_ONLY = (
    "Only the doc's owner can change its instructions, and this conversation's "
    "user is not the owner. Put the proposed rule in your reply so the user "
    "can pass it to the owner."
)
SUGGEST_UNSEEN = (
    "Doc '{title}' has notes you have not seen. Read them below, then suggest "
    "the change again if it is still needed."
)
INSTRUCTIONS = "Newest entry first.\nNever change the Risks section."
MEMORY_1 = "The board reads this on Fridays."
MEMORY_2 = "Q3 figures come from the finance table."

# Dispatch flags per run kind. A script has no conversation of its own;
# a public conversation belongs to the public project.
RUNS = {
    "top_level": {},
    "routine": {"is_routine": True},
    "slack": {"is_slack": True},
    "sub_agent": {"is_sub_agent": True},
    "user_subagent": {"is_user_subagent": True},
    "inference_api": {"is_inference_api": True},
    "script": {"is_script": True},
    "public": {"is_public": True},
}
NO_SUGGEST_RUNS = ("sub_agent", "user_subagent", "inference_api", "script", "routine")


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


def _cs():
    # Other suites reload chat.storage; bind at call time.
    return storage_mod.ChatStorage


def uid(env, who="alice"):
    return env.users[who]["id"]


def conv(env, who="alice", *, project=None):
    """A new conversation of ``who``: its row and its directory."""
    from db import conversation_store

    cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(
        uid(env, who), cid, datetime.now(timezone.utc), project_id=project,
    ))
    (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    return cid


def call_raw(env, tool, arguments, *, cid, who="alice", run="top_level", project=None):
    """Dispatch ``tool`` through ``tool_call``; the raw result string."""
    from chat.gemini_api.tool_dispatch import _dispatch_tool_call

    flags = dict(RUNS[run])
    if run == "script":
        cid = None
    if run == "public" and project is None:
        project = env.public_project
    result, extra = _run(_dispatch_tool_call(
        app=None, provider=None, user=env.users[who],
        conversation_id=cid, timezone="UTC",
        tool_name="tool_call",
        args={"tool_name": tool, "arguments": dict(arguments)},
        project_id=project, **flags,
    ))
    assert extra == []
    return result


def call(env, tool, arguments, **kw):
    return json.loads(call_raw(env, tool, arguments, **kw))


def new_doc(env, owner="alice", *, project=None, mode="private", shares=()):
    return seed_doc(
        env, title=f"Doc {uuid.uuid4().hex[:8]}", owner=owner, mode=mode,
        project_id=project, shares=shares,
    )


def set_instructions(env, doc_id, text, who="alice"):
    current = state(env, doc_id)
    return _run(env.doc_store.update_doc_instructions(
        doc_id, text, expected_version=current["instructions_version"],
        user_id=uid(env, who),
    ))


def add_memory(env, doc_id, text, who="alice"):
    return _run(doc_memory_store.create_doc_memory(
        doc_id, text, source=doc_memory_store.ui_source(uid(env, who)),
        user_id=uid(env, who),
    ))


def noted_doc(env, *, memories=(MEMORY_1, MEMORY_2), **kw):
    """A doc with ``INSTRUCTIONS`` and ``memories`` (added oldest first)."""
    doc = new_doc(env, **kw)
    set_instructions(env, doc["id"], INSTRUCTIONS)
    for text in memories:
        add_memory(env, doc["id"], text)
    return doc


def state(env, doc_id):
    return _run(env.doc_store.get_notes_state(doc_id))


def memories(doc_id):
    return _run(doc_memory_store.list_doc_memories(doc_id))


def seen(cid, doc_id):
    return _cs().get_doc_notes_seen(cid, doc_id)


def ns_seen(instructions, memories_version):
    return notes_mod.NotesSeen(instructions=instructions, memories=memories_version)


def mark_seen(env, cid, doc_id):
    current = state(env, doc_id)
    _cs().record_doc_notes_seen(
        cid, doc_id, current["instructions_version"], current["memories_version"],
    )


def seen_file(env, cid):
    return env.dirs["chats"] / cid / "doc_notes_seen.json"


def all_rows(env):
    async def go():
        from sqlalchemy import func, select
        from db.models import DocNoteSuggestion

        async with env.doc_store.AsyncSessionLocal() as db:
            return (await db.execute(
                select(func.count()).select_from(DocNoteSuggestion)
            )).scalar_one()
    return _run(go())


def pending(env, cid, who="alice"):
    return _run(store.list_pending(uid(env, who), cid))


def full_versions(env, doc_id):
    current = state(env, doc_id)
    return ns_seen(current["instructions_version"], current["memories_version"])


# ---------------------------------------------------------------------------
# 1. Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_specs(self):
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import (
            CORE_TOOL_CALL_TOOLS,
            TOOL_CALL_REGISTRY,
            mutating_tool_call_tools,
        )

        for name in NOTES_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["name"] == name
            assert spec["requires_service"] == DOC_NOTES_SERVICE_KEY == "doc_notes"
            assert spec["core"] is True and "skill" not in spec
            assert "mutating" not in spec
            assert name in CORE_TOOL_CALL_TOOLS
            assert name in TOOL_CALL_HANDLERS
        assert not set(NOTES_TOOLS) & mutating_tool_call_tools()
        assert set(TOOL_CALL_HANDLERS) == set(TOOL_CALL_REGISTRY)

    def test_parameters(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        listing = TOOL_CALL_REGISTRY["list_doc_notes"]["parameters"]
        assert list(listing["properties"]) == ["doc_id", "memory_offset", "intent_message"]
        assert listing["required"] == ["doc_id"]
        offset = listing["properties"]["memory_offset"]
        assert (offset["type"], offset["minimum"], offset["default"]) == ("integer", 0, 0)

        suggest = TOOL_CALL_REGISTRY["suggest_doc_note"]["parameters"]
        assert list(suggest["properties"]) == [
            "doc_id", "kind", "content", "reasoning", "replaces_memory_id", "intent_message",
        ]
        assert suggest["required"] == ["doc_id", "kind", "content"]
        assert suggest["properties"]["kind"]["enum"] == ["instructions", "memory"]
        content = suggest["properties"]["content"]["description"]
        assert "max 1,000 characters" in content and "max 4,000 characters" in content

    def test_descriptions_are_prompt_safe_and_compact(self):
        # Core tools are inlined in every gate-open prompt, the public
        # prompt included: no internal-only tool or skill may be named
        # (tests/test_public_projects.py::test_no_internal_tool_docs).
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY, format_tool_call_doc

        rendered = "\n".join(
            format_tool_call_doc(name, TOOL_CALL_REGISTRY[name]) for name in NOTES_TOOLS
        )
        for term in (
            "authed_get", "authed_post", "load_skills", "system:", "agent_task(",
            "load_gmail_attachment", "wait_for_handles", "suggest_memory",
            "create_action_request(",
        ):
            assert term not in rendered, term
        assert not re.search(r"(?<!project_)memory_search", rendered)
        assert len(rendered) < 4_000

    def test_allowlists(self):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST

        assert set(NOTES_TOOLS) <= PUBLIC_TOOL_CALL_ALLOWLIST
        assert "list_doc_notes" in SCRIPT_TOOL_CALL_ALLOWLIST
        assert "suggest_doc_note" not in SCRIPT_TOOL_CALL_ALLOWLIST

    def test_is_routine_flag(self):
        from chat.gemini_api.tool_dispatch import (
            ToolContext,
            _dispatch_tool_call,
            _dispatch_tool_call_inner,
        )

        for fn in (_dispatch_tool_call, _dispatch_tool_call_inner):
            assert inspect.signature(fn).parameters["is_routine"].default is False
        plain = ToolContext(
            app=None, provider=None, user={"id": 1}, conversation_id="c", timezone="UTC",
        )
        assert plain.is_routine is False
        routine = ToolContext(
            app=None, provider=None, user={"id": 1}, conversation_id="c",
            timezone="UTC", is_routine=True,
        )
        # A routine run stays top-level (it records deliveries).
        assert routine.run_kind == "top_level"


# ---------------------------------------------------------------------------
# 2. Gate closed
# ---------------------------------------------------------------------------


def _close(env, which):
    if which == "doc_notes":
        env.fg.set_feature_enabled(env.fg.FEATURE_DOC_NOTES, False)
    else:
        # doc_notes stays on, but it requires docs.
        env.fg.set_feature_enabled(env.fg.FEATURE_DOCS, False)


class TestGateClosed:
    @pytest.mark.parametrize("which", ["doc_notes", "docs"])
    @pytest.mark.parametrize("run", list(RUNS))
    @pytest.mark.parametrize("tool", NOTES_TOOLS)
    def test_disabled_body_for_every_run_kind(self, env, which, run, tool):
        project = env.public_project if run == "public" else None
        doc = noted_doc(env, project=project, mode="public" if project else "private")
        cid = conv(env, project=project)
        _close(env, which)
        arguments = {"doc_id": doc["id"], "kind": "memory", "content": "A fact."}
        raw = call_raw(env, tool, arguments, cid=cid, run=run)
        assert raw == json.dumps(DISABLED)
        assert not seen_file(env, cid).exists()
        assert all_rows(env) == 0

    @pytest.mark.parametrize("which", ["doc_notes", "docs"])
    def test_gate_before_arguments_backstop_and_visibility(self, env, which):
        cid = conv(env)
        _close(env, which)
        missing = str(uuid.uuid4())
        assert call(env, "list_doc_notes", {"doc_id": missing, "memory_offset": "x"},
                    cid=cid) == DISABLED
        assert call(env, "suggest_doc_note", {"doc_id": missing, "kind": "bogus"},
                    cid=cid) == DISABLED
        for run in NO_SUGGEST_RUNS:
            assert call(env, "suggest_doc_note", {"doc_id": missing}, cid=cid, run=run) == DISABLED

    def test_gate_is_the_callers(self, env):
        # Open for bob only: bob reads the notes of alice's shared doc,
        # alice is refused on her own doc.
        doc = noted_doc(env, shares=[("bob", "read")])
        env.fg.set_feature_allowed_users(env.fg.FEATURE_DOC_NOTES, ["bob@example.com"])
        assert call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=conv(env)) == DISABLED
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=conv(env, "bob"), who="bob")
        assert listed["doc_notes"]["instructions"] == INSTRUCTIONS


# ---------------------------------------------------------------------------
# 3. Hidden vs missing
# ---------------------------------------------------------------------------


class TestHiddenParity:
    def _cases(self, env):
        """``(doc_id, who, run, project)`` for a missing id and three hidden
        noted docs."""
        bobs = noted_doc(env, owner="bob")
        alices_user_doc = noted_doc(env)
        other_project_doc = noted_doc(env, project=env.other_project)
        return [
            (str(uuid.uuid4()), "alice", "top_level", None),
            (bobs["id"], "alice", "top_level", None),
            (alices_user_doc["id"], "alice", "public", env.public_project),
            (other_project_doc["id"], "alice", "top_level", env.private_project),
        ]

    def test_byte_identical_to_read_doc(self, env):
        for doc_id, who, run, project in self._cases(env):
            cid = conv(env, who, project=project)
            kw = {"cid": cid, "who": who, "run": run, "project": project}
            read = call_raw(env, "read_doc", {"doc_id": doc_id}, **kw)
            assert read == json.dumps({"error": doc_not_found_message(doc_id)})
            assert call_raw(env, "list_doc_notes", {"doc_id": doc_id}, **kw) == read
            assert call_raw(env, "list_doc_notes", {"doc_id": doc_id, "memory_offset": 1},
                            **kw) == read
            for kind in ("memory", "instructions"):
                assert call_raw(env, "suggest_doc_note", {
                    "doc_id": doc_id, "kind": kind, "content": "A fact.",
                }, **kw) == read
            assert not seen_file(env, cid).exists()
        assert all_rows(env) == 0


# ---------------------------------------------------------------------------
# 4. The list_doc_notes block
# ---------------------------------------------------------------------------


class TestListBlock:
    def test_result_and_block_equal_read_docs(self, env):
        doc = noted_doc(env)
        read = call(env, "read_doc", {"doc_id": doc["id"]}, cid=conv(env))
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"], "intent_message": "Notes"},
                      cid=conv(env))
        block = {
            "instructions": INSTRUCTIONS,
            "instructions_version": 1,
            "memories": [{"id": m["id"], "content": m["content"]} for m in memories(doc["id"])],
            "memories_omitted": 0,
            "note": NOTE_TEXT,
        }
        assert read["doc_notes"] == block
        assert {m["content"] for m in block["memories"]} == {MEMORY_1, MEMORY_2}
        assert list(listed) == ["id", "title", "memory_offset", "doc_notes", "can_suggest"]
        assert listed == {
            "id": doc["id"],
            "title": doc["title"],
            "memory_offset": 0,
            "doc_notes": block,
            "can_suggest": True,
        }

    def test_doc_without_notes(self, env):
        doc = new_doc(env)
        cid = conv(env)
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=cid)
        assert listed["doc_notes"] == {
            "instructions": "",
            "instructions_version": 0,
            "memories": [],
            "memories_omitted": 0,
            "note": NOTE_TEXT,
        }
        assert seen(cid, doc["id"]) == ns_seen(0, 0)

    def test_paging_reaches_every_memory_once(self, env):
        texts = [f"Memory {i:02d}: " + "x" * 880 for i in range(20)]
        doc = noted_doc(env, memories=texts)
        stored = memories(doc["id"])
        cid = conv(env)
        offset, collected, pages = 0, [], 0
        while True:
            listed = call(env, "list_doc_notes", {"doc_id": doc["id"], "memory_offset": offset},
                          cid=cid)
            block = listed["doc_notes"]
            assert listed["memory_offset"] == offset
            assert block["instructions"] == INSTRUCTIONS
            assert len(json.dumps(block, ensure_ascii=False)) <= DOC_NOTES_DELIVERY_MAX_CHARS
            assert block["memories"]
            collected += block["memories"]
            pages += 1
            offset += len(block["memories"])
            assert block["memories_omitted"] == len(stored) - offset
            if not block["memories_omitted"]:
                break
        assert pages > 1
        assert collected == [{"id": m["id"], "content": m["content"]} for m in stored]

        for past in (len(stored), len(stored) + 5):
            listed = call(env, "list_doc_notes", {"doc_id": doc["id"], "memory_offset": past},
                          cid=cid)
            assert listed["doc_notes"]["memories"] == []
            assert listed["doc_notes"]["memories_omitted"] == 0
            assert listed["doc_notes"]["instructions"] == INSTRUCTIONS

    @pytest.mark.parametrize("offset, error", [
        (-1, "memory_offset must be a non-negative integer."),
        ("-1", "memory_offset must be a non-negative integer."),
        ("x", "memory_offset must be an integer."),
        (True, "memory_offset must be an integer."),
        (1.5, "memory_offset must be an integer."),
        ([1], "memory_offset must be an integer."),
    ])
    def test_bad_offsets_refused_before_the_lookup(self, env, offset, error):
        doc = noted_doc(env)
        cid = conv(env)
        for doc_id in (doc["id"], str(uuid.uuid4())):
            assert call(env, "list_doc_notes", {"doc_id": doc_id, "memory_offset": offset},
                        cid=cid) == {"error": error}
        assert not seen_file(env, cid).exists()

    @pytest.mark.parametrize("offset, expected", [("2", 2), (1.0, 1), (None, 0), ("", 0)])
    def test_offsets_coerced(self, env, offset, expected):
        doc = noted_doc(env, memories=("A.", "B.", "C."))
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"], "memory_offset": offset},
                      cid=conv(env))
        assert listed["memory_offset"] == expected
        newest_first = [m["content"] for m in memories(doc["id"])]
        assert [m["content"] for m in listed["doc_notes"]["memories"]] == newest_first[expected:]

    def test_bad_doc_ids(self, env):
        cid = conv(env)
        for doc_id in (None, "", "  ", 7):
            assert call(env, "list_doc_notes", {"doc_id": doc_id}, cid=cid) == {
                "error": "doc_id must be a non-empty string.",
            }


# ---------------------------------------------------------------------------
# 5. Recording
# ---------------------------------------------------------------------------


class TestRecording:
    def test_page_zero_records_both_versions(self, env):
        doc = noted_doc(env)
        cid = conv(env)
        call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=cid)
        assert seen(cid, doc["id"]) == full_versions(env, doc["id"]) == ns_seen(1, 2)
        read = call(env, "read_doc", {"doc_id": doc["id"]}, cid=cid)
        assert "doc_notes" not in read
        assert read["notes"] == {"has_instructions": True, "memories": 2}
        appended = call(env, "append_to_doc", {"doc_id": doc["id"], "content": "more"}, cid=cid)
        assert "error" not in appended
        assert body(doc["id"]).endswith("more\n")

    def test_later_page_without_an_entry_records_nothing(self, env):
        # Memories only: a record of any kind would let a write pass, so a
        # later page shown first must leave the first write refused with
        # the whole notes (the newest memories were never shown).
        doc = new_doc(env)
        for text in ("A.", "B.", "C."):
            add_memory(env, doc["id"], text)
        writer = conv(env)
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"], "memory_offset": 2},
                      cid=writer)
        assert [m["content"] for m in listed["doc_notes"]["memories"]] == ["A."]
        assert seen(writer, doc["id"]) is None
        refused = call(env, "append_to_doc", {"doc_id": doc["id"], "content": "more"}, cid=writer)
        assert refused["error"] == "doc_notes_unseen"
        assert [m["content"] for m in refused["doc_notes"]["memories"]] == ["C.", "B.", "A."]
        retried = call(env, "append_to_doc", {"doc_id": doc["id"], "content": "more"}, cid=writer)
        assert "error" not in retried
        # With instructions too: nothing recorded, read_doc delivers all.
        noted = noted_doc(env, memories=("A.", "B.", "C."))
        reader = conv(env)
        call(env, "list_doc_notes", {"doc_id": noted["id"], "memory_offset": 1}, cid=reader)
        assert seen(reader, noted["id"]) is None
        read = call(env, "read_doc", {"doc_id": noted["id"]}, cid=reader)
        assert [m["content"] for m in read["doc_notes"]["memories"]] == [
            m["content"] for m in memories(noted["id"])
        ]
        assert seen(reader, noted["id"]) == full_versions(env, noted["id"])

    def test_later_page_keeps_an_existing_memories_entry(self, env):
        doc = noted_doc(env)
        cid = conv(env)
        mark_seen(env, cid, doc["id"])
        add_memory(env, doc["id"], "A newer fact.")
        set_instructions(env, doc["id"], INSTRUCTIONS + "\nUse USD.")
        call(env, "list_doc_notes", {"doc_id": doc["id"], "memory_offset": 1}, cid=cid)
        # Instructions advanced to what was shown; memories kept at 2 (the
        # newest memory was on page 0, not shown).
        assert seen(cid, doc["id"]) == ns_seen(2, 2)
        read = call(env, "read_doc", {"doc_id": doc["id"]}, cid=cid)
        assert read["doc_notes"]["memories"][0]["content"] == "A newer fact."

    @pytest.mark.parametrize("run", ["slack", "user_subagent", "routine"])
    def test_recording_runs(self, env, run):
        doc = noted_doc(env)
        cid = conv(env)
        call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=cid, run=run)
        assert seen(cid, doc["id"]) == ns_seen(1, 2)

    def test_sub_agent_never_advances_its_parents_record(self, env):
        doc = noted_doc(env)
        parent = conv(env)
        _cs().record_doc_notes_seen(parent, doc["id"], 0, 0)
        before = seen_file(env, parent).read_bytes()
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=parent, run="sub_agent")
        assert listed["doc_notes"]["instructions"] == INSTRUCTIONS
        assert seen_file(env, parent).read_bytes() == before

    @pytest.mark.parametrize("run", ["inference_api", "script"])
    def test_runs_that_never_record(self, env, run):
        doc = noted_doc(env)
        cid = conv(env)
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=cid, run=run)
        assert listed["doc_notes"]["memories_omitted"] == 0
        assert len(listed["doc_notes"]["memories"]) == 2
        assert not seen_file(env, cid).exists()


# ---------------------------------------------------------------------------
# 6. can_suggest
# ---------------------------------------------------------------------------


def _can_suggest(env, doc_id, *, who="alice", run="top_level", project=None):
    cid = conv(env, who, project=project)
    listed = call(env, "list_doc_notes", {"doc_id": doc_id},
                  cid=cid, who=who, run=run, project=project)
    return listed["can_suggest"]


class TestCanSuggest:
    def test_owner_and_shares(self, env):
        own = noted_doc(env)
        assert _can_suggest(env, own["id"]) is True
        write_share = noted_doc(env, shares=[("bob", "write")])
        assert _can_suggest(env, write_share["id"], who="bob") is True
        read_share = noted_doc(env, shares=[("bob", "read")])
        assert _can_suggest(env, read_share["id"], who="bob") is False

    def test_slack_deny_causes_admit(self, env):
        shared = noted_doc(env, shares=[("bob", "write")])
        # Approval verdicts become denials in Slack (slack_needs_approval).
        assert _can_suggest(env, shared["id"], run="slack") is True
        switched = noted_doc(env)
        _run(env.doc_store.update_doc_metadata(switched["id"], require_approval=True))
        # The owner's switch in Slack: slack_approval_required.
        assert _can_suggest(env, switched["id"], run="slack") is True
        # A read share stays refused in Slack.
        read_share = noted_doc(env, shares=[("bob", "read")])
        assert _can_suggest(env, read_share["id"], who="bob", run="slack") is False

    def test_taint(self, env):
        give_doc_source(env, env.private_project, env.public_project)
        public_doc = noted_doc(env, project=env.public_project, mode="public")
        # A private conversation reads a Project Access source's public
        # doc but may not suggest for it.
        assert _can_suggest(env, public_doc["id"], project=env.private_project) is False
        # The public project's own conversation may.
        assert _can_suggest(env, public_doc["id"], run="public") is True

    @pytest.mark.parametrize("run", NO_SUGGEST_RUNS)
    def test_run_kinds_that_may_not_suggest(self, env, run):
        own = noted_doc(env)
        assert _can_suggest(env, own["id"], run=run) is False


# ---------------------------------------------------------------------------
# 7. suggest_doc_note through dispatch
# ---------------------------------------------------------------------------


class TestSuggest:
    def test_registers(self, env):
        doc = new_doc(env)
        cid = conv(env)
        result = call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": MEMORY_1,
            "reasoning": "Said twice.", "intent_message": "Suggest doc memory",
        }, cid=cid)
        [row] = pending(env, cid)
        assert result == {
            "status": "suggested",
            "suggestion_id": row["id"],
            "pending_count": 1,
            "message": SUGGESTED,
        }
        assert (row["doc_id"], row["conversation_id"], row["kind"], row["content"],
                row["reasoning"], row["status"]) == (
            doc["id"], cid, "memory", MEMORY_1, "Said twice.", "pending",
        )
        # Nothing reaches the notes before a person accepts.
        assert memories(doc["id"]) == []

    def test_instructions_and_replacement(self, env):
        doc = noted_doc(env, memories=(MEMORY_1,))
        cid = conv(env)
        listed = call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=cid)
        [memory] = listed["doc_notes"]["memories"]
        replaced = call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": "The board reads it on Mondays.",
            "replaces_memory_id": memory["id"],
        }, cid=cid)
        assert replaced["status"] == "suggested"
        instructions = call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "instructions", "content": INSTRUCTIONS + "\nUse USD.",
        }, cid=cid)
        assert instructions["status"] == "suggested" and instructions["pending_count"] == 2
        rows = {r["kind"]: r for r in pending(env, cid)}
        assert rows["memory"]["replaces_memory_id"] == memory["id"]
        assert rows["instructions"]["base_instructions_version"] == 1

    def test_already_saved(self, env):
        doc = noted_doc(env, memories=(MEMORY_1,))
        cid = conv(env)
        call(env, "list_doc_notes", {"doc_id": doc["id"]}, cid=cid)
        [memory] = memories(doc["id"])
        assert call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": MEMORY_1,
        }, cid=cid) == {
            "status": "already_saved",
            "memory_id": memory["id"],
            "message": ALREADY_SAVED_MEMORY,
        }
        assert all_rows(env) == 0

    def test_coded_refusals(self, env):
        shared = new_doc(env, shares=[("bob", "write")])
        bob_cid = conv(env, "bob")
        assert call(env, "suggest_doc_note", {
            "doc_id": shared["id"], "kind": "instructions", "content": "Use USD.",
        }, cid=bob_cid, who="bob") == {"error": "instructions_owner_only", "message": OWNER_ONLY}

        own = new_doc(env)
        cid = conv(env)
        assert call(env, "suggest_doc_note", {
            "doc_id": own["id"], "kind": "bogus", "content": "x",
        }, cid=cid) == {
            "error": "invalid_request",
            "message": 'kind must be "instructions" or "memory".',
        }
        assert call(env, "suggest_doc_note", {
            "doc_id": own["id"], "kind": "memory", "content": "x", "replaces_memory_id": "nope",
        }, cid=cid) == {
            "error": "memory_not_found",
            "message": (
                "Memory 'nope' is not a memory of this doc. Call list_doc_notes "
                "for the ids of its memories."
            ),
        }
        assert all_rows(env) == 0

    def test_deny_reason_is_a_plain_error(self, env):
        doc = new_doc(env, shares=[("bob", "read")])
        result = call_raw(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": "A fact.",
        }, cid=conv(env, "bob"), who="bob")
        assert result == json.dumps({"error": DENY_READ_ONLY_SHARE})
        assert all_rows(env) == 0

    def test_unseen_notes_then_the_identical_retry(self, env):
        doc = noted_doc(env)
        expected_block = call(env, "list_doc_notes", {"doc_id": doc["id"]},
                              cid=conv(env))["doc_notes"]
        cid = conv(env)
        arguments = {"doc_id": doc["id"], "kind": "memory", "content": "A new fact."}
        refused = call(env, "suggest_doc_note", arguments, cid=cid)
        assert list(refused) == ["error", "message", "doc_notes"]
        assert refused == {
            "error": "doc_notes_unseen",
            "message": SUGGEST_UNSEEN.format(title=doc["title"]),
            "doc_notes": expected_block,
        }
        assert seen(cid, doc["id"]) == ns_seen(1, 2)
        assert all_rows(env) == 0
        retried = call(env, "suggest_doc_note", arguments, cid=cid)
        assert retried["status"] == "suggested"

    @pytest.mark.parametrize("run", NO_SUGGEST_RUNS)
    def test_backstop(self, env, run):
        doc = new_doc(env)
        cid = conv(env)
        for arguments in (
            {"doc_id": doc["id"], "kind": "memory", "content": "A fact."},
            # Before argument checks and visibility.
            {"doc_id": str(uuid.uuid4()), "kind": "bogus"},
        ):
            raw = call_raw(env, "suggest_doc_note", arguments, cid=cid, run=run)
            assert raw == json.dumps(NOT_AVAILABLE)
        assert all_rows(env) == 0
        assert not seen_file(env, cid).exists()

    def test_no_conversation_is_refused_by_the_backstop(self, env):
        doc = new_doc(env)
        assert call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": "A fact.",
        }, cid=None) == NOT_AVAILABLE

    def test_slack_may_suggest(self, env):
        own = new_doc(env)
        cid = conv(env)
        assert call(env, "suggest_doc_note", {
            "doc_id": own["id"], "kind": "memory", "content": "A fact.",
        }, cid=cid, run="slack")["status"] == "suggested"
        # A Slack deny cause admits the suggestion.
        shared = new_doc(env, shares=[("bob", "write")])
        assert call(env, "suggest_doc_note", {
            "doc_id": shared["id"], "kind": "memory", "content": "Another fact.",
        }, cid=cid, run="slack")["status"] == "suggested"
        assert len(pending(env, cid)) == 2

    def test_public_conversation_may_suggest_on_its_own_public_doc(self, env):
        doc = new_doc(env, project=env.public_project, mode="public")
        cid = conv(env, project=env.public_project)
        result = call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": "Sources are cited inline.",
        }, cid=cid, run="public")
        assert result["status"] == "suggested"
        [row] = pending(env, cid)
        assert row["doc_id"] == doc["id"]


# ---------------------------------------------------------------------------
# 8. The script bridge
# ---------------------------------------------------------------------------


class TestScriptBridge:
    def _call(self, env, tool, arguments, lease):
        from chat.gemini_api.script_tool_call import (
            ScriptToolCallRequest,
            script_tool_call_endpoint,
        )

        return _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name=tool, arguments=arguments),
            user=env.users["alice"], lease=lease,
        ))

    def _lease(self, env, cid, project=None):
        from chat.sandbox_tokens import SandboxLease

        return SandboxLease(
            user_id=uid(env), conversation_id=cid, expires_at=1e12, project_id=project,
        )

    def test_list_doc_notes_full_block_nothing_recorded(self, env):
        doc = noted_doc(env, project=env.private_project)
        expected_block = call(env, "read_doc", {"doc_id": doc["id"]},
                              cid=conv(env, project=env.private_project))["doc_notes"]
        cid = conv(env, project=env.private_project)
        lease = self._lease(env, cid, env.private_project)
        response = self._call(env, "list_doc_notes", {"doc_id": doc["id"]}, lease)
        assert response.status_code == 200
        assert json.loads(response.body) == {
            "id": doc["id"],
            "title": doc["title"],
            "memory_offset": 0,
            "doc_notes": expected_block,
            "can_suggest": False,
        }
        # read_doc gives a script the counts only; list_doc_notes the block.
        read = json.loads(self._call(env, "read_doc", {"doc_id": doc["id"]}, lease).body)
        assert "doc_notes" not in read and read["notes"]["memories"] == 2
        assert not seen_file(env, cid).exists()
        # From another project's container the doc is hidden, like read_doc.
        elsewhere = self._lease(env, cid, env.other_project)
        hidden = self._call(env, "list_doc_notes", {"doc_id": doc["id"]}, elsewhere)
        assert json.loads(hidden.body) == {"error": doc_not_found_message(doc["id"])}

    def test_suggest_doc_note_not_allowlisted(self, env):
        doc = new_doc(env)
        cid = conv(env)
        response = self._call(env, "suggest_doc_note", {
            "doc_id": doc["id"], "kind": "memory", "content": "A fact.",
        }, self._lease(env, cid))
        assert response.status_code == 400
        assert "not available from scripts" in json.loads(response.body)["error"]
        assert all_rows(env) == 0


# ---------------------------------------------------------------------------
# 9. The conversation loop passes is_routine to dispatch
# ---------------------------------------------------------------------------


class TestLoopWiring:
    """run_conversation_turn hands ``is_routine=bool(routine_id)`` to
    ``_dispatch_tool_call``, so the suggest_doc_note backstop and
    ``can_suggest`` see routine runs (their run kind stays top_level).
    The collaborators are stubbed as in tests/test_stream_error_surfacing.py."""

    def _dispatch_kwargs(self, monkeypatch, *, routine_id):
        from unittest.mock import AsyncMock, MagicMock

        import api.instructions as instructions
        import db.conversation_store as conv_store
        import db.skill_store as skill_store
        from chat.gemini_api import conversation as conv_mod
        from chat.llm.base import StreamEvent, UsageStats

        monkeypatch.setattr(conv_mod, "load_server_config", lambda: {"gemini": {"model": "fake"}})
        monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
        monkeypatch.setattr(conv_mod, "get_system_prompt", lambda *a, **kw: "system prompt")
        monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
        monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
        monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: object())
        monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())
        monkeypatch.setattr(conv_store, "update_conversation_model", AsyncMock())
        monkeypatch.setattr(skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]))
        monkeypatch.setattr(skill_store, "get_routine_autoloaded_skills", AsyncMock(return_value=[]))
        monkeypatch.setattr(instructions, "get_user_connected_services", lambda _u: {})

        async def _fake_history(_cid):
            return {}

        monkeypatch.setattr(conv_mod, "_read_chat_history", _fake_history)
        monkeypatch.setattr(
            conv_mod.ChatStorage, "set_system_prompt", staticmethod(lambda *a, **kw: None),
        )

        provider = MagicMock()
        provider.get_pending_tool_use_args = MagicMock(return_value=[])
        provider.get_usage = MagicMock(return_value=UsageStats())
        provider.repair_session_history = MagicMock(return_value=0)
        turns: list = []

        async def _stream(_chat, _message):
            turns.append(_message)
            if len(turns) == 1:
                yield StreamEvent(
                    type="tool_call", tool_name="tool_call", tool_id="call_1",
                    tool_args={"tool_name": "suggest_doc_note", "arguments": {
                        "doc_id": "d1", "kind": "memory", "content": "A fact.",
                    }},
                )
            else:
                yield StreamEvent(type="text", text="done")

        provider.send_message_stream = _stream
        monkeypatch.setattr(conv_mod, "get_provider_instance", lambda _n, _i=None: provider)
        dispatched = AsyncMock(return_value=(json.dumps(NOT_AVAILABLE), []))
        monkeypatch.setattr(conv_mod, "_dispatch_tool_call", dispatched)

        async def _on_event(_event):
            return None

        _run(conv_mod.run_conversation_turn(
            app=None,
            user={"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"},
            message="hello", conversation_id="conv-routine-wiring", timezone="UTC",
            model="fake", on_event=_on_event, messages_out=[], routine_id=routine_id,
        ))
        assert dispatched.await_count == 1
        return dispatched.await_args.kwargs

    def test_routine_run_dispatches_with_is_routine(self, monkeypatch):
        assert self._dispatch_kwargs(monkeypatch, routine_id="r1")["is_routine"] is True

    def test_ordinary_run_dispatches_without_it(self, monkeypatch):
        assert self._dispatch_kwargs(monkeypatch, routine_id=None)["is_routine"] is False
