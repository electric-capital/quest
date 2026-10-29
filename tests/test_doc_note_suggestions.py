"""Doc note suggestions service (chat/docs/note_suggestions.py), phase A4 of
spec 00011 (docs/architecture/doc-notes.md), with
``service.publish_notes_changed()`` and the suggestion events of the doc,
project and account delete paths.

Runs against the isolated ``docs_env`` of tests/test_docs_service.py with
the ``doc_notes`` gate open (``env``; its connections turn foreign keys on,
so every suggesting conversation gets a real ``conversations`` row), and
the ``account_env`` of tests/test_docs_prompting.py for the account delete.
Expected payloads are literal.

Covers:

1. Registration of both kinds: the result, the stored row (the base
   instructions version) and the ``doc_note_suggestions_changed`` event
   with ``pending_count`` / ``total_pending``.
2. Every guard and cap: ``already_saved`` (memory text, instructions
   text), ``already_suggested`` (any conversation of the user; never
   another user's row, a resolved row or the other kind), the conversation
   cap, the per-doc cap counted per user, an instructions replacement never
   refused by the caps it frees, the character caps per kind, empty
   instructions, ``memories_cap_reached`` (not for a replacement),
   ``memory_not_found``, every ``invalid_request`` shape, and the per-user
   lock against parallel calls in one model response.
3. Check order: argument shape before visibility; a hidden doc reads like
   a missing one, byte for byte with ``read_doc``; the denied verdict
   before instructions ownership; ownership before the seen record; the
   seen record before the guards; the two gates; programming errors.
4. Taint: every deny cause but the two Slack ones (and ``ui_read_only``,
   unreachable from a conversation) refuses with its ``deny_reason``, both
   kinds, pinned by a coverage test; the two Slack causes, the top-level
   ``approval`` verdicts and a public conversation on its own public doc
   are admitted; a write share's memory suggestion is admitted and its
   instructions suggestion refused with ``instructions_owner_only``.
5. Unseen notes: ``DocNotesUnseen`` with the block and the suggest
   message, the delivery recorded, the identical retry registers; a
   memories-only change refuses too; a doc without notes and no record
   passes and records nothing.
6. Supersede across two conversations, publishing to both, and within one.
7. ``review_rows``: ``doc_title``, ``current_instructions``,
   ``instructions_moved`` false then true, ``replaces_content``, and the
   blanked fields once the doc is invisible (share revoked) or gone.
8. Accept and dismiss: both kinds with and without an override, the
   memory row and its source, replace in place and the replaced-memory-gone
   fallback, the cap and ``duplicate_memory`` at accept,
   ``stale_instructions`` with ``current`` and success with the current
   version, ``forbidden`` after a downgrade or revoke and for a non-owner's
   instructions row, ``suggestion_not_found`` / ``suggestion_resolved``,
   invalid overrides and versions, review with the gate closed, and
   ``HTTP_STATUS``.
9. Accept events: ``doc_note_suggestions_changed`` to the reviewer, then
   ``doc_notes_changed`` with the new versions to the doc's audience
   (owner, direct recipients, connected users for an everyone share),
   none for a no-op accept, never a ``doc_changed``;
   ``service.publish_notes_changed()`` is best-effort.
10. The seen advance: only for unedited text (an override equal after
    normalisation counts as unedited) and a record current before the
    accept; no record on a doc without notes counts as current; storage
    failures swallowed.
11. Delete paths: ``delete_doc_from_ui``, ``delete_user_project`` and
    ``delete_account`` publish ``doc_note_suggestions_changed`` (the
    affected conversation's new count) to every user with pending rows on
    the deleted docs, after the doc events; the deleted account's own user
    gets none; nothing extra without pending rows; the helpers are
    best-effort; the account delete captures before the project cascade.
12. Review follow-ups: instructions saved between the seen check and the
    state the base version comes from refuse with the new notes; the
    registration lock is per user across conversations; the replaced
    memory is checked before the guards; a long memory id is cut in the
    refusal; notes emptied before the fresh load do not refuse; resolved
    rows get no notes-derived review fields; a failed publish never fails
    a landed accept or dismiss.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

import chat.storage as storage_mod
from chat.artifact_access import DENY_CAUSES
from chat.docs import note_suggestions as ns
from chat.docs import notes as notes_mod
from chat.docs import service
from chat.docs.access import (
    DENY_INFERENCE_API,
    DENY_OUTSIDE_PROJECT,
    DENY_PUBLIC_DOC_FROM_PRIVATE,
    DENY_READ_ONLY_SHARE,
    DENY_SCRIPT,
    DENY_SUB_AGENT,
    DENY_USER_SUBAGENT,
)
from chat.docs.constants import (
    DOC_INSTRUCTIONS_MAX_CHARS,
    DOC_MAX_MEMORIES,
    DOC_MEMORY_MAX_CHARS,
    DOC_NOTE_MAX_PENDING_PER_CONVERSATION,
    DOC_NOTE_MAX_PENDING_PER_DOC,
    doc_not_found_message,
    doc_notes_disabled_message,
    docs_disabled_message,
)
from chat.docs.note_suggestions import NoteSuggestionError
from chat.docs.service import Caller, DocDisabled, DocError, DocNotesUnseen
from db import doc_memory_store
from db import doc_note_suggestion_store as store
from tests.test_docs_prompting import (  # noqa: F401  (account_env is a fixture)
    _seed_doc as seed_account_doc,
    account_env,
)
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    docs_env,
    give_doc_source,
    seed_doc,
)

NOTE_TEXT = (
    "These are the doc's current notes. Instructions are its owner's rules "
    "for changing it: follow them whenever you change the doc. Memories are "
    "background facts about it, kept by its editors. Propose changes with "
    "suggest_doc_note where it is offered."
)
SUGGESTED = (
    "Registered for the user's review in the doc note suggestions panel; "
    "nothing is saved until they accept it. Keep going."
)
SUPERSEDED = (
    "Registered for the user's review in the doc note suggestions panel, in "
    "place of the earlier instructions suggestion for this doc that was "
    "still waiting for their review; nothing is saved until they accept it. "
    "Keep going."
)
ALREADY_SAVED_MEMORY = "This doc already has a memory with exactly this text; nothing to suggest."
ALREADY_SAVED_INSTRUCTIONS = (
    "The doc's instructions already read exactly like this; nothing to suggest."
)
ALREADY_SUGGESTED = (
    "A suggestion with exactly this text is already waiting for the user's "
    "review; do not suggest it again."
)
OWNER_ONLY = (
    "Only the doc's owner can change its instructions, and this conversation's "
    "user is not the owner. Put the proposed rule in your reply so the user "
    "can pass it to the owner."
)
ADMITTED_CAUSES = {"slack_needs_approval", "slack_approval_required"}


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


def conv(env, who="alice", *, project=None, public=False, run_kind="top_level"):
    """A caller in a new conversation of ``who`` (row + directory). A
    script keeps the project and mode but has no conversation."""
    from db import conversation_store

    cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(
        uid(env, who), cid, datetime.now(timezone.utc), project_id=project,
    ))
    (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    return Caller(
        user=env.users[who],
        conversation_id=None if run_kind == "script" else cid,
        project_id=project, is_public=public, run_kind=run_kind,
    )


def new_doc(env, owner="alice", *, project=None, mode="private", shares=()):
    return seed_doc(
        env, title=f"Doc {uuid.uuid4().hex[:8]}", owner=owner, mode=mode,
        project_id=project, shares=shares,
    )


def suggest(caller, doc_id, kind="memory", content="A fact.", **kw):
    return _run(ns.register_suggestion(caller, doc_id, kind, content, **kw))


def refused(caller, doc_id, kind="memory", content="A fact.", **kw) -> NoteSuggestionError:
    with pytest.raises(NoteSuggestionError) as info:
        suggest(caller, doc_id, kind, content, **kw)
    return info.value


def accept(env, sid, who="alice", **kw):
    return _run(ns.accept_suggestion(env.users[who], sid, **kw))


def accept_refused(env, sid, who="alice", **kw) -> NoteSuggestionError:
    with pytest.raises(NoteSuggestionError) as info:
        accept(env, sid, who, **kw)
    return info.value


def dismiss(env, sid, who="alice"):
    return _run(ns.dismiss_suggestion(env.users[who], sid))


def dismiss_refused(env, sid, who="alice") -> NoteSuggestionError:
    with pytest.raises(NoteSuggestionError) as info:
        dismiss(env, sid, who)
    return info.value


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


def memories(doc_id):
    return _run(doc_memory_store.list_doc_memories(doc_id))


def state(env, doc_id):
    return _run(env.doc_store.get_notes_state(doc_id))


def mark_seen(env, caller, doc_id):
    current = state(env, doc_id)
    _cs().record_doc_notes_seen(
        caller.conversation_id, doc_id,
        current["instructions_version"], current["memories_version"],
    )


def seen(caller, doc_id):
    return _cs().get_doc_notes_seen(caller.conversation_id, doc_id)


def ns_seen(instructions, memories_version):
    return notes_mod.NotesSeen(instructions=instructions, memories=memories_version)


def get_row(env, sid, who="alice"):
    return _run(store.get_suggestion(uid(env, who), sid))


def pending(env, caller, who="alice"):
    return _run(store.list_pending(uid(env, who), caller.conversation_id))


def all_rows(env):
    async def go():
        from sqlalchemy import func, select
        from db.models import DocNoteSuggestion

        async with env.doc_store.AsyncSessionLocal() as db:
            return (await db.execute(
                select(func.count()).select_from(DocNoteSuggestion)
            )).scalar_one()
    return _run(go())


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


def share_id(env, doc_id, who):
    [share] = [
        s for s in _run(env.doc_store.list_shares(doc_id))
        if s["user_id"] == uid(env, who)
    ]
    return share["id"]


# ---------------------------------------------------------------------------
# 1. Registration
# ---------------------------------------------------------------------------


class TestRegister:
    def test_memory(self, env):
        doc = new_doc(env)
        caller = conv(env)
        result = suggest(
            caller, doc["id"], "memory", "  The board reads this on Fridays.\n",
            reasoning="Said twice in this conversation.",
        )
        [row] = pending(env, caller)
        assert result == {
            "status": "suggested",
            "suggestion_id": row["id"],
            "pending_count": 1,
            "message": SUGGESTED,
        }
        assert {k: v for k, v in row.items() if k not in ("id", "created_at")} == {
            "user_id": uid(env),
            "doc_id": doc["id"],
            "conversation_id": caller.conversation_id,
            "kind": "memory",
            "content": "The board reads this on Fridays.",
            "reasoning": "Said twice in this conversation.",
            "base_instructions_version": None,
            "replaces_memory_id": None,
            "status": "pending",
            "memory_id": None,
            "resolved_at": None,
        }
        assert env.published == [(uid(env), changed(caller.conversation_id, 1, 1))]
        # Nothing reaches the notes before a person accepts.
        assert memories(doc["id"]) == []

    def test_instructions(self, env):
        doc = new_doc(env)
        set_instructions(env, doc["id"], "Newest entry first.")
        caller = conv(env)
        mark_seen(env, caller, doc["id"])
        result = suggest(
            caller, doc["id"], "instructions", "Newest entry first.\nFigures in USD thousands.",
        )
        assert result == {
            "status": "suggested",
            "suggestion_id": result["suggestion_id"],
            "pending_count": 1,
            "message": SUGGESTED,
        }
        row = get_row(env, result["suggestion_id"])
        assert (row["kind"], row["content"], row["base_instructions_version"],
                row["replaces_memory_id"], row["status"]) == (
            "instructions", "Newest entry first.\nFigures in USD thousands.", 1, None, "pending",
        )
        assert env.published == [(uid(env), changed(caller.conversation_id, 1, 1))]
        assert state(env, doc["id"])["instructions"] == "Newest entry first."

    def test_total_pending_spans_conversations_and_replace_is_stored(self, env):
        doc = new_doc(env)
        memory = add_memory(env, doc["id"], "Old fact.")
        first, second = conv(env), conv(env)
        mark_seen(env, first, doc["id"])
        mark_seen(env, second, doc["id"])
        suggest(first, doc["id"], "memory", "Fact one.")
        env.published.clear()
        result = suggest(
            second, doc["id"], "memory", "New fact.", replaces_memory_id=f" {memory['id']} ",
        )
        assert result["pending_count"] == 1
        assert get_row(env, result["suggestion_id"])["replaces_memory_id"] == memory["id"]
        assert env.published == [(uid(env), changed(second.conversation_id, 1, 2))]

    def test_parallel_calls_cannot_both_pass_a_guard(self, env):
        doc = new_doc(env)
        caller = conv(env)

        async def both():
            return await asyncio.gather(
                ns.register_suggestion(caller, doc["id"], "memory", "Same fact."),
                ns.register_suggestion(caller, doc["id"], "memory", "Same fact."),
            )

        results = _run(both())
        assert sorted(r["status"] for r in results) == ["already_suggested", "suggested"]
        assert len(pending(env, caller)) == 1


# ---------------------------------------------------------------------------
# 2. Guards and caps
# ---------------------------------------------------------------------------


class TestGuards:
    def test_already_saved_memory(self, env):
        doc = new_doc(env)
        saved = add_memory(env, doc["id"], "Figures in EUR.")
        other = add_memory(env, doc["id"], "Another fact.")
        caller = conv(env)
        mark_seen(env, caller, doc["id"])
        env.published.clear()
        expected = {
            "status": "already_saved",
            "memory_id": saved["id"],
            "message": ALREADY_SAVED_MEMORY,
        }
        assert suggest(caller, doc["id"], "memory", "  Figures in EUR.\n") == expected
        # Also as the replacement of another memory.
        assert suggest(
            caller, doc["id"], "memory", "Figures in EUR.", replaces_memory_id=other["id"],
        ) == expected
        assert all_rows(env) == 0 and env.published == []

    def test_already_saved_instructions(self, env):
        doc = new_doc(env)
        set_instructions(env, doc["id"], "Newest entry first.")
        caller = conv(env)
        mark_seen(env, caller, doc["id"])
        assert suggest(caller, doc["id"], "instructions", " Newest entry first. ") == {
            "status": "already_saved",
            "message": ALREADY_SAVED_INSTRUCTIONS,
        }
        assert all_rows(env) == 0 and env.published == []

    def test_already_suggested(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        first, second, bob = conv(env), conv(env), conv(env, "bob")
        original = suggest(first, doc["id"], "memory", "Same fact.")
        env.published.clear()
        # Pending from another conversation of the same user.
        assert suggest(second, doc["id"], "memory", " Same fact. ") == {
            "status": "already_suggested",
            "suggestion_id": original["suggestion_id"],
            "pending_count": 0,
            "message": ALREADY_SUGGESTED,
        }
        assert env.published == []
        # Not another user's row, not the other kind ...
        assert suggest(bob, doc["id"], "memory", "Same fact.")["status"] == "suggested"
        assert suggest(first, doc["id"], "instructions", "Same fact.")["status"] == "suggested"
        # ... and not a resolved row.
        old = suggest(first, doc["id"], "memory", "Old fact.")["suggestion_id"]
        dismiss(env, old)
        assert suggest(second, doc["id"], "memory", "Old fact.")["status"] == "suggested"

    def test_conversation_cap(self, env):
        caller = conv(env)
        per_doc = DOC_NOTE_MAX_PENDING_PER_DOC
        docs = [new_doc(env)["id"] for _ in range(-(-DOC_NOTE_MAX_PENDING_PER_CONVERSATION // per_doc) + 1)]
        for n in range(DOC_NOTE_MAX_PENDING_PER_CONVERSATION):
            assert suggest(caller, docs[n // per_doc], "memory", f"Fact {n}.")["status"] == "suggested"
        err = refused(caller, docs[-1], "memory", "One more.")
        assert (err.code, str(err)) == (
            "conversation_pending_cap_reached",
            f"This conversation already has {DOC_NOTE_MAX_PENDING_PER_CONVERSATION} doc "
            "note suggestions waiting for the user's review. Do not suggest more "
            "until they have reviewed them.",
        )
        # Another conversation of the user is not capped by it.
        assert suggest(conv(env), docs[-1], "memory", "One more.")["status"] == "suggested"

    def test_doc_cap_counts_only_this_users_rows(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        bob = conv(env, "bob")
        for i in range(3):
            suggest(bob, doc, "memory", f"Bob fact {i}.")
        a1, a2 = conv(env), conv(env)
        for i in range(DOC_NOTE_MAX_PENDING_PER_DOC):
            assert suggest(a1 if i % 2 else a2, doc, "memory", f"Alice fact {i}.")["status"] == "suggested"
        err = refused(a1, doc, "memory", "One more.")
        assert (err.code, str(err)) == (
            "doc_pending_cap_reached",
            f"The user already has {DOC_NOTE_MAX_PENDING_PER_DOC} note suggestions for "
            "this doc waiting for their review. Do not suggest more for it until "
            "they have reviewed them.",
        )
        # Bob's own count is his, and another doc is not capped.
        assert suggest(bob, doc, "memory", "Bob fact 3.")["status"] == "suggested"
        assert suggest(a1, new_doc(env)["id"], "memory", "Elsewhere.")["status"] == "suggested"

    def test_instructions_replacement_never_refused_by_the_caps_it_frees(self, env):
        caller = conv(env)
        doc_a, doc_b, doc_c = (new_doc(env)["id"] for _ in range(3))
        first = suggest(caller, doc_a, "instructions", "Rule one.")
        for i in range(DOC_NOTE_MAX_PENDING_PER_DOC - 1):
            suggest(caller, doc_a, "memory", f"A fact {i}.")
        for i in range(DOC_NOTE_MAX_PENDING_PER_CONVERSATION - DOC_NOTE_MAX_PENDING_PER_DOC):
            suggest(caller, doc_b, "memory", f"B fact {i}.")
        # Both caps are full: a new memory is refused ...
        assert refused(caller, doc_c, "memory", "C fact.").code == "conversation_pending_cap_reached"
        assert refused(conv(env), doc_a, "memory", "A extra.").code == "doc_pending_cap_reached"
        # ... but replacing the pending instructions raises neither count.
        replaced = suggest(caller, doc_a, "instructions", "Rule two.")
        assert (replaced["status"], replaced["pending_count"], replaced["message"]) == (
            "suggested", DOC_NOTE_MAX_PENDING_PER_CONVERSATION, SUPERSEDED,
        )
        assert get_row(env, first["suggestion_id"])["status"] == "dismissed"
        # From another conversation, the per-doc count frees the slot too.
        other = conv(env)
        assert suggest(other, doc_a, "instructions", "Rule three.")["status"] == "suggested"
        assert refused(other, doc_a, "memory", "A extra.").code == "doc_pending_cap_reached"

    def test_character_caps_and_empty_text(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        # The caps count characters, not bytes.
        assert suggest(caller, doc, "memory", "é" * DOC_MEMORY_MAX_CHARS)["status"] == "suggested"
        assert suggest(
            caller, doc, "instructions", "é" * DOC_INSTRUCTIONS_MAX_CHARS,
        )["status"] == "suggested"
        cases = [
            ("memory", "é" * (DOC_MEMORY_MAX_CHARS + 1), "invalid_memory",
             f"Memory content exceeds the maximum length of {DOC_MEMORY_MAX_CHARS} characters."),
            ("memory", "  \n ", "invalid_memory", "Memory content cannot be empty."),
            ("memory", "a\x00b", "invalid_memory", "Memory content cannot contain NUL characters."),
            ("instructions", "é" * (DOC_INSTRUCTIONS_MAX_CHARS + 1), "invalid_instructions",
             f"Doc instructions exceed the maximum length of {DOC_INSTRUCTIONS_MAX_CHARS} characters."),
            ("instructions", "  \n ", "invalid_instructions",
             "An instructions suggestion is the complete new instructions text, so "
             "it cannot be empty. Only the user can clear the instructions, in the "
             "doc's Notes panel."),
        ]
        for kind, content, code, message in cases:
            err = refused(caller, doc, kind, content)
            assert (err.code, str(err)) == (code, message), kind
        assert len(pending(env, caller)) == 2

    def test_memories_cap(self, env):
        doc = new_doc(env)["id"]
        existing = [add_memory(env, doc, f"Fact {i}.") for i in range(DOC_MAX_MEMORIES)]
        caller = conv(env)
        mark_seen(env, caller, doc)
        err = refused(caller, doc, "memory", "One more.")
        assert (err.code, str(err)) == (
            "memories_cap_reached",
            f"This doc already has the maximum of {DOC_MAX_MEMORIES} memories. Suggest "
            "replacing an outdated one with replaces_memory_id instead.",
        )
        result = suggest(caller, doc, "memory", "One more.", replaces_memory_id=existing[0]["id"])
        assert result["status"] == "suggested"
        assert get_row(env, result["suggestion_id"])["replaces_memory_id"] == existing[0]["id"]

    def test_memory_not_found(self, env):
        doc = new_doc(env)["id"]
        elsewhere = add_memory(env, new_doc(env)["id"], "Another doc's fact.")
        caller = conv(env)
        for bad in (elsewhere["id"], str(uuid.uuid4())):
            err = refused(caller, doc, "memory", "New fact.", replaces_memory_id=bad)
            assert (err.code, str(err)) == (
                "memory_not_found",
                f"Memory '{bad}' is not a memory of this doc. Call list_doc_notes for "
                "the ids of its memories.",
            )
        assert all_rows(env) == 0

    def test_target_gone_before_the_insert_reads_like_a_missing_doc(self, env, monkeypatch):
        doc = new_doc(env)["id"]
        caller = conv(env)

        async def gone(*_a, **_kw):
            raise store.SuggestionTargetGoneError("The doc or the conversation was deleted.")

        monkeypatch.setattr(store, "create_suggestion", gone)
        env.published.clear()
        with pytest.raises(DocError) as info:
            suggest(caller, doc, "memory", "A fact.")
        assert type(info.value) is DocError and str(info.value) == doc_not_found_message(doc)
        assert env.published == []

    def test_invalid_request(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        cases = [
            ("note", "x", {}, 'kind must be "instructions" or "memory".'),
            (None, "x", {}, 'kind must be "instructions" or "memory".'),
            (["memory"], "x", {}, 'kind must be "instructions" or "memory".'),
            ("memory", 5, {}, "content must be a string."),
            ("memory", None, {}, "content must be a string."),
            ("memory", "x", {"reasoning": 3}, "reasoning must be a string."),
            ("memory", "x", {"replaces_memory_id": 7},
             "replaces_memory_id must be the id of one of the doc's memories, a string."),
            ("instructions", "x", {"replaces_memory_id": "some-id"},
             "replaces_memory_id applies to memory suggestions only; an instructions "
             "suggestion carries the complete new text of the instructions."),
        ]
        for kind, content, kw, message in cases:
            err = refused(caller, doc, kind, content, **kw)
            assert (err.code, str(err)) == ("invalid_request", message), (kind, content, kw)
        assert all_rows(env) == 0 and env.published == []
        # A blank replaces_memory_id counts as none.
        result = suggest(caller, doc, "memory", "x", replaces_memory_id="  ")
        assert get_row(env, result["suggestion_id"])["replaces_memory_id"] is None


# ---------------------------------------------------------------------------
# 3. Check order, gates, programming errors
# ---------------------------------------------------------------------------


class TestOrder:
    def test_argument_shape_before_visibility(self, env):
        hidden = new_doc(env, owner="bob")
        assert refused(conv(env), hidden["id"], "note", "x").code == "invalid_request"

    def test_hidden_reads_like_missing(self, env):
        hidden = new_doc(env, owner="bob")
        caller = conv(env)
        for doc_id in (hidden["id"], str(uuid.uuid4())):
            with pytest.raises(DocError) as got:
                suggest(caller, doc_id, "memory", "A fact.")
            with pytest.raises(DocError) as read:
                _run(service.read_doc(caller, doc_id))
            assert type(got.value) is DocError
            assert str(got.value) == str(read.value) == doc_not_found_message(doc_id)
        assert all_rows(env) == 0

    def test_denied_before_owner_only(self, env):
        doc = new_doc(env, shares=[("bob", "read")])
        with pytest.raises(DocError) as info:
            suggest(conv(env, "bob"), doc["id"], "instructions", "A rule.")
        assert type(info.value) is DocError and str(info.value) == DENY_READ_ONLY_SHARE

    def test_owner_only_before_the_seen_record(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        set_instructions(env, doc["id"], "A rule.")
        bob = conv(env, "bob")
        err = refused(bob, doc["id"], "instructions", "Another rule.")
        assert (err.code, str(err)) == ("instructions_owner_only", OWNER_ONLY)
        assert seen(bob, doc["id"]) is None
        # The memory kind gets as far as the seen record.
        with pytest.raises(DocNotesUnseen):
            suggest(bob, doc["id"], "memory", "A fact.")
        assert seen(bob, doc["id"]) == ns_seen(1, 0)

    def test_seen_record_before_the_guards(self, env):
        doc = new_doc(env)
        saved = add_memory(env, doc["id"], "Known fact.")
        caller = conv(env)
        with pytest.raises(DocNotesUnseen):
            suggest(caller, doc["id"], "memory", "Known fact.")
        assert suggest(caller, doc["id"], "memory", "Known fact.") == {
            "status": "already_saved",
            "memory_id": saved["id"],
            "message": ALREADY_SAVED_MEMORY,
        }

    def test_gates(self, env):
        doc = new_doc(env)
        caller = conv(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_DOC_NOTES, False)
        err = refused(caller, doc["id"])
        assert (err.code, str(err)) == ("doc_notes_disabled", doc_notes_disabled_message())
        # The docs gate comes first.
        env.fg.set_feature_enabled(env.fg.FEATURE_DOC_NOTES, True)
        env.fg.set_feature_enabled(env.fg.FEATURE_DOCS, False)
        with pytest.raises(DocDisabled) as info:
            suggest(caller, doc["id"])
        assert str(info.value) == docs_disabled_message()
        assert all_rows(env) == 0

    def test_programming_errors(self, env):
        doc = new_doc(env)
        alice = env.users["alice"]
        callers = [
            Caller(user=alice, conversation_id=None, project_id=None,
                   is_public=False, run_kind="ui"),
            Caller(user=alice, conversation_id=str(uuid.uuid4()), project_id=None,
                   is_public=False, run_kind="ui"),
            Caller(user=alice, conversation_id=None, project_id=None,
                   is_public=False, run_kind="top_level"),
            Caller(user=alice, conversation_id=None, project_id=None,
                   is_public=False, run_kind="slack"),
        ]
        for caller in callers:
            with pytest.raises(ValueError):
                suggest(caller, doc["id"])


# ---------------------------------------------------------------------------
# 4. Taint and the admitted verdicts
# ---------------------------------------------------------------------------


def _public_from_private(env):
    # A private project conversation reading a Project Access source's
    # public doc.
    give_doc_source(env, env.private_project, env.public_project)
    doc = new_doc(env, project=env.public_project, mode="public")
    return conv(env, project=env.private_project), doc["id"], DENY_PUBLIC_DOC_FROM_PRIVATE


def _outside_project(env):
    # The owner's standalone conversation on one of their project docs.
    doc = new_doc(env, project=env.private_project)
    return conv(env), doc["id"], DENY_OUTSIDE_PROJECT


def _read_only_share(env):
    doc = new_doc(env, shares=[("bob", "read")])
    return conv(env, "bob"), doc["id"], DENY_READ_ONLY_SHARE


def _run_kind(run_kind, text):
    def build(env):
        return conv(env, run_kind=run_kind), new_doc(env)["id"], text
    return build


REFUSALS = {
    "public_from_private": _public_from_private,
    "outside_project": _outside_project,
    "read_only_share": _read_only_share,
    "sub_agent": _run_kind("sub_agent", DENY_SUB_AGENT),
    "inference_api": _run_kind("inference_api", DENY_INFERENCE_API),
    "user_subagent": _run_kind("user_subagent", DENY_USER_SUBAGENT),
    "script": _run_kind("script", DENY_SCRIPT),
}


def test_refusal_cases_cover_every_other_deny_cause():
    # ui_read_only: a UI caller never suggests (register raises ValueError).
    assert set(REFUSALS) == set(DENY_CAUSES) - ADMITTED_CAUSES - {"ui_read_only"}


@pytest.mark.parametrize("kind", ["memory", "instructions"])
@pytest.mark.parametrize("cause", sorted(REFUSALS))
def test_denied_verdicts_refuse(env, cause, kind):
    caller, doc_id, text = REFUSALS[cause](env)
    _doc, access = _run(service.get_visible_doc(caller, doc_id))
    assert (access.write, access.deny_cause, access.deny_reason) == ("denied", cause, text)
    with pytest.raises(DocError) as info:
        suggest(caller, doc_id, kind, "Some rule.")
    assert type(info.value) is DocError and str(info.value) == text
    assert all_rows(env) == 0 and env.published == []


class TestAdmitted:
    def _verdict(self, caller, doc_id):
        _doc, access = _run(service.get_visible_doc(caller, doc_id))
        return access.write, access.deny_cause

    def test_slack_needs_approval(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        bob, alice = conv(env, "bob", run_kind="slack"), conv(env, run_kind="slack")
        for caller in (bob, alice):
            assert self._verdict(caller, doc) == ("denied", "slack_needs_approval")
        assert suggest(bob, doc, "memory", "Bob's fact.")["status"] == "suggested"
        assert suggest(alice, doc, "instructions", "Alice's rule.")["status"] == "suggested"

    def test_slack_approval_required(self, env):
        doc = new_doc(env)["id"]
        _run(env.doc_store.update_doc_metadata(doc, require_approval=True))
        caller = conv(env, run_kind="slack")
        assert self._verdict(caller, doc) == ("denied", "slack_approval_required")
        assert suggest(caller, doc, "memory", "A fact.")["status"] == "suggested"
        assert suggest(caller, doc, "instructions", "A rule.")["status"] == "suggested"

    def test_top_level_approval_verdicts(self, env):
        shared = new_doc(env, shares=[("bob", "read")])["id"]
        switched = new_doc(env)["id"]
        _run(env.doc_store.update_doc_metadata(switched, require_approval=True))
        caller = conv(env)
        for doc in (shared, switched):
            assert self._verdict(caller, doc) == ("approval", None)
            assert suggest(caller, doc, "memory", "A fact.")["status"] == "suggested"
            assert suggest(caller, doc, "instructions", "A rule.")["status"] == "suggested"

    def test_public_conversation_on_its_own_public_doc(self, env):
        doc = new_doc(env, project=env.public_project, mode="public")["id"]
        caller = conv(env, project=env.public_project, public=True)
        assert self._verdict(caller, doc) == ("free", None)
        assert suggest(caller, doc, "memory", "A public fact.")["status"] == "suggested"

    def test_write_share(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        bob = conv(env, "bob")
        assert self._verdict(bob, doc) == ("approval", None)
        assert suggest(bob, doc, "memory", "Bob's fact.")["status"] == "suggested"
        err = refused(bob, doc, "instructions", "Bob's rule.")
        assert (err.code, str(err)) == ("instructions_owner_only", OWNER_ONLY)
        assert [r["kind"] for r in pending(env, bob, "bob")] == ["memory"]


# ---------------------------------------------------------------------------
# 5. Unseen notes
# ---------------------------------------------------------------------------


class TestUnseenNotes:
    def test_refusal_records_and_the_retry_registers(self, env):
        doc = new_doc(env)
        set_instructions(env, doc["id"], "Newest entry first.")
        memory = add_memory(env, doc["id"], "The board reads this on Fridays.")
        caller = conv(env)
        env.published.clear()
        with pytest.raises(DocNotesUnseen) as info:
            suggest(caller, doc["id"], "memory", "Q3 figures come from the finance table.")
        err = info.value
        assert str(err) == (
            f"Doc '{doc['title']}' has notes you have not seen; they are below. "
            "If they do not already say it, suggest the change again (for "
            "instructions, the current text with your change applied): it "
            "will not be refused for these notes again."
        )
        assert err.notes_block == {
            "instructions": "Newest entry first.",
            "instructions_version": 1,
            "memories": [{"id": memory["id"], "content": "The board reads this on Fridays."}],
            "memories_omitted": 0,
            "note": NOTE_TEXT,
        }
        assert err.notes_text == notes_mod.render_notes_text(err.notes_block).text
        assert seen(caller, doc["id"]) == ns_seen(1, 1)
        assert all_rows(env) == 0 and env.published == []
        result = suggest(caller, doc["id"], "memory", "Q3 figures come from the finance table.")
        assert result["status"] == "suggested"

    def test_a_memories_only_change_refuses_too(self, env):
        doc = new_doc(env)["id"]
        add_memory(env, doc, "Fact one.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        add_memory(env, doc, "Fact two.")
        with pytest.raises(DocNotesUnseen):
            suggest(caller, doc, "memory", "Fact three.")
        assert seen(caller, doc) == ns_seen(0, 2)
        assert suggest(caller, doc, "memory", "Fact three.")["status"] == "suggested"

    def test_no_notes_and_no_record_passes(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        assert suggest(caller, doc, "memory", "First fact.")["status"] == "suggested"
        assert suggest(caller, doc, "instructions", "First rule.")["status"] == "suggested"
        assert seen(caller, doc) is None


# ---------------------------------------------------------------------------
# 6. Supersede
# ---------------------------------------------------------------------------


class TestSupersede:
    def test_across_two_conversations_publishes_to_both(self, env):
        doc = new_doc(env)["id"]
        c1, c2 = conv(env), conv(env)
        first = suggest(c1, doc, "instructions", "Rule A.")
        kept = suggest(c1, doc, "memory", "A fact.")
        env.published.clear()
        second = suggest(c2, doc, "instructions", "Rule B.")
        assert second == {
            "status": "suggested",
            "suggestion_id": second["suggestion_id"],
            "pending_count": 1,
            "message": SUPERSEDED,
        }
        old = get_row(env, first["suggestion_id"])
        assert old["status"] == "dismissed" and old["resolved_at"] is not None
        # Memory suggestions are never superseded.
        assert get_row(env, kept["suggestion_id"])["status"] == "pending"
        alice = uid(env)
        assert env.published == [
            (alice, changed(c2.conversation_id, 1, 2)),
            (alice, changed(c1.conversation_id, 1, 2)),
        ]
        # Within one conversation: one event.
        env.published.clear()
        third = suggest(c2, doc, "instructions", "Rule C.")
        assert third["message"] == SUPERSEDED and third["pending_count"] == 1
        assert env.published == [(alice, changed(c2.conversation_id, 1, 2))]
        assert [r["content"] for r in pending(env, c2)] == ["Rule C."]


# ---------------------------------------------------------------------------
# 7. review_rows
# ---------------------------------------------------------------------------


class TestReviewRows:
    def test_rows(self, env):
        doc = new_doc(env)
        doc_id = doc["id"]
        set_instructions(env, doc_id, "Rule A.")
        memory = add_memory(env, doc_id, "Old fact.")
        caller = conv(env)
        mark_seen(env, caller, doc_id)
        instr = suggest(caller, doc_id, "instructions", "Rule A.\nRule B.")["suggestion_id"]
        replace = suggest(
            caller, doc_id, "memory", "New fact.", replaces_memory_id=memory["id"],
        )["suggestion_id"]
        plain = suggest(caller, doc_id, "memory", "Another fact.")["suggestion_id"]
        alice = uid(env)

        def review():
            rows = pending(env, caller)
            out = _run(ns.review_rows(alice, rows))
            assert [r["id"] for r in out] == [r["id"] for r in rows]
            by_id = {r["id"]: r for r in rows}
            return by_id, {r["id"]: r for r in out}

        rows, out = review()
        assert out == {
            instr: {**rows[instr], "doc_title": doc["title"],
                    "current_instructions": "Rule A.", "instructions_moved": False,
                    "replaces_content": None},
            replace: {**rows[replace], "doc_title": doc["title"],
                      "replaces_content": "Old fact."},
            plain: {**rows[plain], "doc_title": doc["title"], "replaces_content": None},
        }
        # The field moved and the replaced memory changed.
        set_instructions(env, doc_id, "Rule A, revised.")
        _run(doc_memory_store.update_doc_memory(
            doc_id, memory["id"], "Old fact, edited.", user_id=alice,
        ))
        _rows, out = review()
        assert (out[instr]["current_instructions"], out[instr]["instructions_moved"]) == (
            "Rule A, revised.", True,
        )
        assert out[replace]["replaces_content"] == "Old fact, edited."
        # The replaced memory is gone.
        _run(doc_memory_store.delete_doc_memory(doc_id, memory["id"]))
        _rows, out = review()
        assert out[replace]["replaces_content"] is None

    def test_a_doc_no_longer_visible_blanks_the_doc_fields(self, env):
        doc = new_doc(env, shares=[("bob", "write")])
        doc_id = doc["id"]
        memory = add_memory(env, doc_id, "Shared fact.")
        bob = conv(env, "bob")
        mark_seen(env, bob, doc_id)
        suggest(bob, doc_id, "memory", "Bob's fact.", replaces_memory_id=memory["id"])
        # An instructions row of Bob's, inserted directly (the service
        # refuses it), written against version 0.
        _run(store.create_suggestion(
            uid(env, "bob"), doc_id, bob.conversation_id, "instructions", "Bob's rule.",
            base_instructions_version=0,
        ))
        set_instructions(env, doc_id, "The owner's rule.")
        rows = pending(env, bob, "bob")

        def extras(out):
            return [
                {k: r[k] for k in ("content", "doc_title", "current_instructions",
                                   "instructions_moved", "replaces_content") if k in r}
                for r in out
            ]

        visible = _run(ns.review_rows(uid(env, "bob"), rows))
        assert extras(visible) == [
            {"content": "Bob's fact.", "doc_title": doc["title"], "replaces_content": "Shared fact."},
            {"content": "Bob's rule.", "doc_title": doc["title"],
             "current_instructions": "The owner's rule.", "instructions_moved": True,
             "replaces_content": None},
        ]
        blanked = [
            {"content": "Bob's fact.", "doc_title": "", "replaces_content": None},
            {"content": "Bob's rule.", "doc_title": "", "current_instructions": None,
             "instructions_moved": False, "replaces_content": None},
        ]
        _run(env.doc_store.remove_share(doc_id, share_id(env, doc_id, "bob")))
        assert extras(_run(ns.review_rows(uid(env, "bob"), rows))) == blanked
        # The suggestions stay listed after the revoke.
        assert len(pending(env, bob, "bob")) == 2
        # A doc that is gone reads the same.
        _run(env.doc_store.delete_doc(doc_id))
        assert extras(_run(ns.review_rows(uid(env, "bob"), rows))) == blanked


# ---------------------------------------------------------------------------
# 8. Accept and dismiss
# ---------------------------------------------------------------------------


class TestAccept:
    def test_memory_unedited(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        sid = suggest(caller, doc, "memory", "A fact.")["suggestion_id"]
        env.published.clear()
        result = accept(env, sid)
        [memory] = memories(doc)
        assert {k: v for k, v in memory.items() if k not in ("id", "created_at", "updated_at")} == {
            "doc_id": doc,
            "content": "A fact.",
            "source": f"suggestion:{sid}",
            "created_by": uid(env),
            "updated_by": uid(env),
        }
        row = get_row(env, sid)
        assert (row["status"], row["memory_id"]) == ("accepted", memory["id"])
        assert result == {"suggestion": row, "memory": memory, "notes": state(env, doc)}
        assert env.published == [
            (uid(env), changed(caller.conversation_id, 0, 0)),
            (uid(env), notes_changed(doc, 0, 1)),
        ]

    def test_memory_override(self, env):
        doc = new_doc(env)["id"]
        sid = suggest(conv(env), doc, "memory", "A fact.")["suggestion_id"]
        result = accept(env, sid, content="  An edited fact. ")
        assert [m["content"] for m in memories(doc)] == ["An edited fact."]
        assert result["memory"]["source"] == f"suggestion:{sid}"
        # The row keeps the proposed text.
        assert result["suggestion"]["content"] == "A fact."

    def test_instructions_unedited_override_and_clear(self, env):
        doc = new_doc(env)["id"]
        set_instructions(env, doc, "Rule A.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        sid = suggest(caller, doc, "instructions", "Rule A.\nRule B.")["suggestion_id"]
        env.published.clear()
        result = accept(env, sid)
        assert result["memory"] is None
        assert (result["notes"]["instructions"], result["notes"]["instructions_version"],
                result["notes"]["instructions_updated_by"]) == ("Rule A.\nRule B.", 2, uid(env))
        assert result["notes"] == state(env, doc)
        assert env.published == [
            (uid(env), changed(caller.conversation_id, 0, 0)),
            (uid(env), notes_changed(doc, 2, 0)),
        ]
        sid = suggest(caller, doc, "instructions", "Rule C.")["suggestion_id"]
        assert accept(env, sid, content="Rule C, edited.")["notes"]["instructions"] == "Rule C, edited."
        mark_seen(env, caller, doc)
        sid = suggest(caller, doc, "instructions", "Rule D.")["suggestion_id"]
        # A reviewer's empty override clears the field.
        result = accept(env, sid, content="")
        assert (result["notes"]["instructions"], result["notes"]["instructions_version"]) == ("", 4)

    def test_replace_in_place(self, env):
        doc = new_doc(env)["id"]
        memory = add_memory(env, doc, "Old fact.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        sid = suggest(
            caller, doc, "memory", "New fact.", replaces_memory_id=memory["id"],
        )["suggestion_id"]
        result = accept(env, sid)
        assert [(m["id"], m["content"], m["source"], m["created_at"]) for m in memories(doc)] == [
            (memory["id"], "New fact.", memory["source"], memory["created_at"]),
        ]
        assert result["memory"]["id"] == result["suggestion"]["memory_id"] == memory["id"]
        assert (result["notes"]["memory_count"], result["notes"]["memories_version"]) == (1, 2)

    def test_replaced_memory_gone_falls_back_to_insert(self, env):
        doc = new_doc(env)["id"]
        memory = add_memory(env, doc, "Old fact.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        sid = suggest(
            caller, doc, "memory", "New fact.", replaces_memory_id=memory["id"],
        )["suggestion_id"]
        _run(doc_memory_store.delete_doc_memory(doc, memory["id"]))
        result = accept(env, sid)
        [inserted] = memories(doc)
        assert inserted["id"] != memory["id"]
        assert (inserted["content"], inserted["source"]) == ("New fact.", f"suggestion:{sid}")
        assert result["suggestion"]["memory_id"] == inserted["id"]

    def test_cap_at_accept(self, env):
        doc = new_doc(env)["id"]
        for i in range(DOC_MAX_MEMORIES - 1):
            add_memory(env, doc, f"Fact {i}.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        sid = suggest(caller, doc, "memory", "Last slot.")["suggestion_id"]
        add_memory(env, doc, "Filler.")
        env.published.clear()
        err = accept_refused(env, sid)
        assert (err.code, str(err), err.current, err.memory_id) == (
            "memories_cap_reached",
            f"This doc already has the maximum of {DOC_MAX_MEMORIES} memories. "
            "Delete or edit one instead.",
            None, None,
        )
        assert get_row(env, sid)["status"] == "pending"
        assert len(memories(doc)) == DOC_MAX_MEMORIES and env.published == []

    def test_duplicate_memory(self, env):
        doc = new_doc(env)["id"]
        sid = suggest(conv(env), doc, "memory", "Same fact.")["suggestion_id"]
        duplicate = add_memory(env, doc, "Same fact.")
        err = accept_refused(env, sid)
        assert (err.code, err.memory_id, str(err)) == (
            "duplicate_memory", duplicate["id"],
            "This doc already has a memory with exactly this text.",
        )
        assert get_row(env, sid)["status"] == "pending"
        # A different override lands.
        assert accept(env, sid, content="Same fact, reworded.")["memory"]["content"] == (
            "Same fact, reworded."
        )

    def test_stale_instructions(self, env):
        doc = new_doc(env)["id"]
        set_instructions(env, doc, "Rule A.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        sid = suggest(caller, doc, "instructions", "Rule A.\nRule B.")["suggestion_id"]
        set_instructions(env, doc, "Rule A, revised.")
        env.published.clear()
        stale = (
            "stale_instructions",
            {"instructions": "Rule A, revised.", "instructions_version": 2},
            "The doc's instructions changed since this suggestion was shown. Review "
            "it against the current text and accept again.",
        )
        # The default expected version is the one the proposal was written
        # against; an explicit stale one is refused the same way.
        for kw in ({}, {"expected_instructions_version": 1}):
            err = accept_refused(env, sid, **kw)
            assert (err.code, err.current, str(err)) == stale
        assert get_row(env, sid)["status"] == "pending"
        assert state(env, doc)["instructions"] == "Rule A, revised." and env.published == []
        result = accept(env, sid, expected_instructions_version=2)
        assert (result["notes"]["instructions"], result["notes"]["instructions_version"]) == (
            "Rule A.\nRule B.", 3,
        )

    def test_forbidden_after_downgrade_and_revoke_dismiss_still_works(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        bob = conv(env, "bob")
        sid = suggest(bob, doc, "memory", "Bob's fact.")["suggestion_id"]
        forbidden = (
            "forbidden",
            "You can no longer edit this doc, so you cannot change its memories. "
            "Dismiss the suggestion instead.",
        )
        _run(env.doc_store.add_share(doc, uid(env, "bob"), "read"))
        err = accept_refused(env, sid, "bob")
        assert (err.code, str(err)) == forbidden
        _run(env.doc_store.remove_share(doc, share_id(env, doc, "bob")))
        err = accept_refused(env, sid, "bob")
        assert (err.code, str(err)) == forbidden
        assert get_row(env, sid, "bob")["status"] == "pending"
        env.published.clear()
        assert dismiss(env, sid, "bob")["status"] == "dismissed"
        assert env.published == [(uid(env, "bob"), changed(bob.conversation_id, 0, 0))]
        assert memories(doc) == []

    def test_forbidden_for_a_non_owners_instructions_row(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        bob = conv(env, "bob")
        row, _ = _run(store.create_suggestion(
            uid(env, "bob"), doc, bob.conversation_id, "instructions", "Bob's rule.",
            base_instructions_version=0,
        ))
        err = accept_refused(env, row["id"], "bob")
        assert (err.code, str(err)) == (
            "forbidden", "Only the doc's owner can change its instructions.",
        )
        assert state(env, doc)["instructions"] == ""
        assert get_row(env, row["id"], "bob")["status"] == "pending"

    def test_not_found_and_resolved(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        mine = suggest(conv(env), doc, "memory", "Alice's fact.")["suggestion_id"]
        bobs = suggest(conv(env, "bob"), doc, "memory", "Bob's fact.")["suggestion_id"]
        not_found = ("suggestion_not_found", "Suggestion not found.")
        resolved = ("suggestion_resolved", "This suggestion was already accepted or dismissed.")
        for sid in (bobs, str(uuid.uuid4())):
            for err in (accept_refused(env, sid), dismiss_refused(env, sid)):
                assert (err.code, str(err)) == not_found
        accept(env, mine)
        for err in (accept_refused(env, mine), dismiss_refused(env, mine)):
            assert (err.code, str(err)) == resolved
        dismiss(env, bobs, "bob")
        for err in (accept_refused(env, bobs, "bob"), dismiss_refused(env, bobs, "bob")):
            assert (err.code, str(err)) == resolved

    def test_invalid_overrides_and_versions(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        memory_sid = suggest(caller, doc, "memory", "A fact.")["suggestion_id"]
        instr_sid = suggest(caller, doc, "instructions", "A rule.")["suggestion_id"]
        env.published.clear()
        for content in (" ", "x" * (DOC_MEMORY_MAX_CHARS + 1), 5):
            assert accept_refused(env, memory_sid, content=content).code == "invalid_memory"
        err = accept_refused(env, instr_sid, content="x" * (DOC_INSTRUCTIONS_MAX_CHARS + 1))
        assert err.code == "invalid_instructions"
        for bad in (-1, True, "1", 1.0, 2**63):
            err = accept_refused(env, instr_sid, expected_instructions_version=bad)
            assert (err.code, str(err)) == (
                "invalid_instructions",
                "expected_instructions_version must be a non-negative integer.",
            )
        assert [get_row(env, s)["status"] for s in (memory_sid, instr_sid)] == ["pending"] * 2
        assert env.published == [] and memories(doc) == []
        # A memory suggestion ignores the instructions version.
        assert accept(env, memory_sid, expected_instructions_version="n/a")["memory"]["content"] == (
            "A fact."
        )

    def test_review_works_with_the_gate_closed(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        first = suggest(caller, doc, "memory", "Fact one.")["suggestion_id"]
        second = suggest(caller, doc, "memory", "Fact two.")["suggestion_id"]
        env.fg.set_feature_enabled(env.fg.FEATURE_DOC_NOTES, False)
        assert accept(env, first)["suggestion"]["status"] == "accepted"
        assert dismiss(env, second)["status"] == "dismissed"
        rows = _run(store.list_resolved_for_user(uid(env), 10))
        assert _run(ns.review_rows(uid(env), rows))[0]["doc_title"].startswith("Doc ")


def test_http_status_covers_every_review_code():
    assert ns.HTTP_STATUS == {
        "suggestion_not_found": 404,
        "suggestion_resolved": 409,
        "forbidden": 403,
        "stale_instructions": 409,
        "invalid_memory": 400,
        "invalid_instructions": 400,
        "memories_cap_reached": 409,
        "duplicate_memory": 409,
    }


# ---------------------------------------------------------------------------
# 9. Accept events
# ---------------------------------------------------------------------------


class TestAcceptEvents:
    def test_doc_audience(self, env):
        doc = new_doc(env, shares=[("bob", "write"), ("carol", "read")])["id"]
        caller = conv(env)
        sid = suggest(caller, doc, "memory", "A fact.")["suggestion_id"]
        env.published.clear()
        accept(env, sid)
        alice, bob, carol = uid(env), uid(env, "bob"), uid(env, "carol")
        assert env.published == [
            (alice, changed(caller.conversation_id, 0, 0)),
            (alice, notes_changed(doc, 0, 1)),
            (bob, notes_changed(doc, 0, 1)),
            (carol, notes_changed(doc, 0, 1)),
        ]

    def test_everyone_share_reaches_connected_users(self, env, monkeypatch):
        from chat.realtime import bus

        doc = new_doc(env, shares=[(None, "write")])["id"]
        bob = conv(env, "bob")
        sid = suggest(bob, doc, "memory", "A fact.")["suggestion_id"]
        alice, bob_id, carol = uid(env), uid(env, "bob"), uid(env, "carol")
        monkeypatch.setattr(bus, "connected_user_ids", lambda: [bob_id, carol, 909])
        env.published.clear()
        accept(env, sid, "bob")
        assert env.published == [
            (bob_id, changed(bob.conversation_id, 0, 0)),
            (alice, notes_changed(doc, 0, 1)),
            (bob_id, notes_changed(doc, 0, 1)),
            (carol, notes_changed(doc, 0, 1)),
            (909, notes_changed(doc, 0, 1)),
        ]

    def test_no_doc_notes_changed_when_nothing_changed(self, env):
        doc = new_doc(env)["id"]
        memory = add_memory(env, doc, "Old fact.")
        set_instructions(env, doc, "Rule A.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        memory_sid = suggest(
            caller, doc, "memory", "New fact.", replaces_memory_id=memory["id"],
        )["suggestion_id"]
        instr_sid = suggest(caller, doc, "instructions", "Rule B.")["suggestion_id"]
        # A person made both changes before the review.
        _run(doc_memory_store.update_doc_memory(doc, memory["id"], "New fact.", user_id=uid(env)))
        set_instructions(env, doc, "Rule B.")
        before = state(env, doc)
        env.published.clear()
        assert accept(env, memory_sid)["notes"] == before
        assert accept(env, instr_sid, expected_instructions_version=2)["notes"] == before
        assert env.published == [
            (uid(env), changed(caller.conversation_id, 1, 1)),
            (uid(env), changed(caller.conversation_id, 0, 0)),
        ]

    def test_publish_notes_changed_is_best_effort(self, env, monkeypatch):
        doc = new_doc(env, shares=[("bob", "read")])["id"]
        after = {"instructions_version": 3, "memories_version": 4, "instructions": "secret"}
        _run(service.publish_notes_changed(doc, after))
        assert env.published == [
            (uid(env), notes_changed(doc, 3, 4)),
            (uid(env, "bob"), notes_changed(doc, 3, 4)),
        ]
        env.published.clear()
        # A doc gone meanwhile publishes nothing; a failure is swallowed.
        _run(service.publish_notes_changed(str(uuid.uuid4()), after))

        async def boom(*_a, **_kw):
            raise RuntimeError("database is down")

        monkeypatch.setattr(env.doc_store, "get_doc", boom)
        _run(service.publish_notes_changed(doc, after))
        assert env.published == []


# ---------------------------------------------------------------------------
# 10. The seen advance
# ---------------------------------------------------------------------------


class TestSeenAdvance:
    def _setup(self, env):
        doc = new_doc(env)["id"]
        set_instructions(env, doc, "Rule A.")
        caller = conv(env)
        mark_seen(env, caller, doc)
        return doc, caller

    def test_unedited_accept_advances_a_current_record(self, env):
        doc, caller = self._setup(env)
        sid = suggest(caller, doc, "instructions", "Rule A.\nRule B.")["suggestion_id"]
        accept(env, sid)
        assert seen(caller, doc) == ns_seen(2, 0)
        # An override equal to the proposal after normalisation is unedited.
        sid = suggest(caller, doc, "memory", "A fact.")["suggestion_id"]
        accept(env, sid, content="  A fact.\n")
        assert seen(caller, doc) == ns_seen(2, 1)

    def test_edited_accept_does_not_advance(self, env):
        doc, caller = self._setup(env)
        sid = suggest(caller, doc, "instructions", "Rule A.\nRule B.")["suggestion_id"]
        accept(env, sid, content="Rule A.\nRule B, edited.")
        assert state(env, doc)["instructions_version"] == 2
        assert seen(caller, doc) == ns_seen(1, 0)
        # The same for a memory.
        mark_seen(env, caller, doc)
        sid = suggest(caller, doc, "memory", "A fact.")["suggestion_id"]
        accept(env, sid, content="A different fact.")
        assert state(env, doc)["memories_version"] == 1
        assert seen(caller, doc) == ns_seen(2, 0)

    def test_a_record_behind_before_the_accept_is_not_advanced(self, env):
        doc, caller = self._setup(env)
        sid = suggest(caller, doc, "instructions", "Rule A.\nRule B.")["suggestion_id"]
        set_instructions(env, doc, "Rule A, revised.")
        accept(env, sid, expected_instructions_version=2)
        assert state(env, doc)["instructions_version"] == 3
        assert seen(caller, doc) == ns_seen(1, 0)

    def test_no_record_on_a_doc_without_notes_counts_as_current(self, env):
        doc = new_doc(env)["id"]
        caller = conv(env)
        sid = suggest(caller, doc, "memory", "First fact.")["suggestion_id"]
        assert seen(caller, doc) is None
        accept(env, sid)
        assert seen(caller, doc) == ns_seen(0, 1)

    def test_storage_failure_is_swallowed(self, env, monkeypatch):
        doc, caller = self._setup(env)
        sid = suggest(caller, doc, "instructions", "Rule B.")["suggestion_id"]

        def boom(*_a, **_kw):
            raise OSError("disk on fire")

        monkeypatch.setattr(_cs(), "get_doc_notes_seen", boom)
        assert accept(env, sid)["notes"]["instructions"] == "Rule B."


# ---------------------------------------------------------------------------
# 11. Delete paths
# ---------------------------------------------------------------------------


class TestDeletePaths:
    def test_delete_doc_from_ui(self, env):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        other = new_doc(env)["id"]
        c1, c2, bob = conv(env), conv(env), conv(env, "bob")
        suggest(c1, doc, "memory", "Alice's fact.")
        suggest(c1, other, "memory", "Another doc's fact.")
        suggest(c2, doc, "instructions", "Alice's rule.")
        suggest(bob, doc, "memory", "Bob's fact.")
        alice, bob_id = uid(env), uid(env, "bob")
        env.published.clear()
        _run(service.delete_doc_from_ui(env.users["alice"], doc))
        assert [(u, ev["type"]) for u, ev in env.published[:4]] == [
            (alice, "doc_changed"), (bob_id, "doc_changed"),
            (alice, "doc_list_changed"), (bob_id, "doc_list_changed"),
        ]
        counts = {c1.conversation_id: 1, c2.conversation_id: 0}
        assert env.published[4:] == [
            *((alice, changed(cid, counts[cid], 1)) for cid in sorted(counts)),
            (bob_id, changed(bob.conversation_id, 0, 0)),
        ]
        # Nothing extra without pending rows.
        quiet = new_doc(env)["id"]
        env.published.clear()
        _run(service.delete_doc_from_ui(env.users["alice"], quiet))
        assert [(u, ev["type"]) for u, ev in env.published] == [
            (alice, "doc_changed"), (alice, "doc_list_changed"),
        ]

    def test_delete_user_project(self, env):
        from chat.project_routes import delete_user_project

        project_doc = new_doc(env, project=env.private_project)["id"]
        user_doc = new_doc(env)["id"]
        in_project = conv(env, project=env.private_project)
        standalone = conv(env)
        suggest(in_project, project_doc, "memory", "Project fact.")
        suggest(standalone, user_doc, "memory", "User fact.")
        alice = uid(env)
        env.published.clear()
        _run(delete_user_project(env.private_project, user=env.users["alice"]))
        assert [(u, ev["type"]) for u, ev in env.published] == [
            (alice, "doc_changed"), (alice, "doc_list_changed"),
            (alice, "doc_note_suggestions_changed"),
        ]
        assert env.published[-1] == (alice, changed(in_project.conversation_id, 0, 1))
        # Nothing extra without pending rows.
        new_doc(env, project=env.other_project)
        env.published.clear()
        _run(delete_user_project(env.other_project, user=env.users["alice"]))
        assert [(u, ev["type"]) for u, ev in env.published] == [
            (alice, "doc_changed"), (alice, "doc_list_changed"),
        ]

    def test_capture_failure_gives_no_pairs(self, env, monkeypatch):
        async def boom(_doc_ids):
            raise RuntimeError("database is down")

        monkeypatch.setattr(store, "list_pending_pairs_for_docs", boom)
        assert _run(ns.capture_pending_pairs(["any"])) == []

    def test_publish_for_pairs_is_best_effort_and_excludes(self, env, monkeypatch):
        doc = new_doc(env, shares=[("bob", "write")])["id"]
        mine, bob = conv(env), conv(env, "bob")
        suggest(mine, doc, "memory", "Alice's fact.")
        suggest(bob, doc, "memory", "Bob's fact.")
        real = ns.publish_changed

        async def flaky(user_id, conversation_id):
            if conversation_id == "broken":
                raise RuntimeError("bus is down")
            return await real(user_id, conversation_id)

        monkeypatch.setattr(ns, "publish_changed", flaky)
        env.published.clear()
        alice, bob_id = uid(env), uid(env, "bob")
        _run(ns.publish_changed_for_pairs(
            [(alice, "broken"), (alice, mine.conversation_id), (bob_id, bob.conversation_id)],
            exclude_user_id=bob_id,
        ))
        assert env.published == [(alice, changed(mine.conversation_id, 1, 1))]


def test_delete_account_publishes_to_other_users_only(request, monkeypatch):
    from chat.realtime import bus
    from db import conversation_store

    env = request.getfixturevalue("account_env")
    published: list = []
    monkeypatch.setattr(bus, "publish_to_user", lambda u, ev: published.append((u, ev)))
    monkeypatch.setattr(bus, "connected_user_ids", lambda: [])
    alice, bob = env["users"]["alice"]["id"], env["users"]["bob"]["id"]
    shared = seed_account_doc(env, "alice", "Shared")
    bobs_own = seed_account_doc(env, "bob", "Bob's own")
    _run(env["doc_store"].add_share(shared, bob, "write"))

    def new_conv(user_id):
        cid = str(uuid.uuid4())
        _run(conversation_store.create_conversation(user_id, cid, datetime.now(timezone.utc)))
        return cid

    alice_cid, bob_cid, bob_other = new_conv(alice), new_conv(bob), new_conv(bob)
    for user_id, doc_id, cid, text in (
        (alice, shared, alice_cid, "Alice's fact."),
        (bob, shared, bob_cid, "Bob's fact."),
        (bob, bobs_own, bob_other, "Bob's own fact."),
    ):
        _run(store.create_suggestion(user_id, doc_id, cid, "memory", text))

    import chat.routes.user as user_routes

    response = _run(user_routes.delete_account(request=None, user=env["users"]["alice"]))
    assert response.status_code == 200
    # Bob's row went with Alice's doc; Alice (deleted) hears nothing.
    assert published == [(bob, changed(bob_cid, 0, 1))]
    assert _run(store.count_pending_for_user(bob)) == 1


def test_delete_account_captures_before_the_project_cascade(request, monkeypatch):
    """The capture runs before any row delete: pending rows on the deleted
    user's PROJECT docs, which go with the project delete before the user
    row, are captured too. (Only the owner's conversations reach a project
    doc, and their pairs are skipped; this row is inserted through the
    store to pin the order.)"""
    from chat.realtime import bus
    from db import conversation_store

    env = request.getfixturevalue("account_env")
    published: list = []
    monkeypatch.setattr(bus, "publish_to_user", lambda u, ev: published.append((u, ev)))
    monkeypatch.setattr(bus, "connected_user_ids", lambda: [])
    alice, bob = env["users"]["alice"]["id"], env["users"]["bob"]["id"]
    project = _run(env["project_store"].create_project(alice, "Research"))
    project_doc = seed_account_doc(env, "alice", "Log", project_id=project["id"])
    bob_cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(bob, bob_cid, datetime.now(timezone.utc)))
    _run(store.create_suggestion(bob, project_doc, bob_cid, "memory", "A fact."))

    import chat.routes.user as user_routes

    response = _run(user_routes.delete_account(request=None, user=env["users"]["alice"]))
    assert response.status_code == 200
    assert published == [(bob, changed(bob_cid, 0, 0))]


# ---------------------------------------------------------------------------
# 12. Review follow-ups
# ---------------------------------------------------------------------------


class TestReviewFollowUps:
    def test_instructions_saved_after_the_seen_check_reach_the_model_first(
        self, env, monkeypatch,
    ):
        """The owner saves the instructions between check 3 (on the row) and
        the state the base version comes from: the suggestion is refused with
        the new notes instead of claiming a base the model never saw."""
        doc = new_doc(env)
        set_instructions(env, doc["id"], "Rule A.")
        caller = conv(env)
        mark_seen(env, caller, doc["id"])
        real = env.doc_store.get_notes_state
        saved: list[int] = []

        async def owner_saves_first(doc_id):
            if not saved:
                current = await real(doc_id)
                await env.doc_store.update_doc_instructions(
                    doc_id, "Rule A.\nOwner's new rule.",
                    expected_version=current["instructions_version"],
                    user_id=uid(env),
                )
                saved.append(1)
            return await real(doc_id)

        monkeypatch.setattr(env.doc_store, "get_notes_state", owner_saves_first)
        with pytest.raises(DocNotesUnseen) as info:
            suggest(caller, doc["id"], "instructions", "Rule B.")
        assert info.value.notes_block["instructions"] == "Rule A.\nOwner's new rule."
        assert info.value.notes_block["instructions_version"] == 2
        assert seen(caller, doc["id"]).instructions == 2
        assert pending(env, caller) == []

        result = suggest(caller, doc["id"], "instructions", "Rule B.")
        assert result["status"] == "suggested"
        [row] = pending(env, caller)
        assert row["base_instructions_version"] == 2

    def test_the_lock_is_per_user_across_conversations(self, env):
        doc = new_doc(env)
        one, two = conv(env), conv(env)

        async def both():
            return await asyncio.gather(
                ns.register_suggestion(one, doc["id"], "memory", "Same fact."),
                ns.register_suggestion(two, doc["id"], "memory", "Same fact."),
            )

        statuses = sorted(r["status"] for r in _run(both()))
        assert statuses == ["already_suggested", "suggested"]

    def test_the_replaced_memory_is_checked_before_the_guards(self, env):
        doc = new_doc(env)
        add_memory(env, doc["id"], "A fact.")
        other = new_doc(env)
        foreign = add_memory(env, other["id"], "Elsewhere.")
        caller = conv(env)
        mark_seen(env, caller, doc["id"])
        error = refused(
            caller, doc["id"], "memory", "A fact.", replaces_memory_id=foreign["id"],
        )
        assert error.code == "memory_not_found"

    def test_a_long_memory_id_is_cut_in_the_refusal(self, env):
        doc = new_doc(env)
        caller = conv(env)
        error = refused(caller, doc["id"], "memory", "A fact.", replaces_memory_id="x" * 500)
        assert error.code == "memory_not_found"
        assert "x" * 100 + "..." in str(error)
        assert "x" * 101 not in str(error)

    def test_notes_emptied_before_the_fresh_load_do_not_refuse(self, env, monkeypatch):
        """No record and a noted row, but the notes are gone by the fresh
        load: nothing to deliver, so the suggestion registers."""
        doc = new_doc(env)
        memory = add_memory(env, doc["id"], "Old fact.")
        row = _run(env.doc_store.get_doc(doc["id"], with_shares=True))
        assert row["memory_count"] == 1
        caller = conv(env)
        real = service.load_notes

        async def emptied_first(doc_id, **kw):
            await doc_memory_store.delete_doc_memory(doc_id, memory["id"])
            return await real(doc_id, **kw)

        monkeypatch.setattr(service, "load_notes", emptied_first)
        assert suggest(caller, doc["id"], "memory", "New fact.")["status"] == "suggested"
        assert seen(caller, doc["id"]) is None

    def test_resolved_rows_get_no_notes_derived_fields(self, env):
        doc = new_doc(env)
        memory = add_memory(env, doc["id"], "Old fact.")
        caller = conv(env)
        mark_seen(env, caller, doc["id"])
        suggest(caller, doc["id"], "instructions", "Rule.")
        suggest(caller, doc["id"], "memory", "New fact.", replaces_memory_id=memory["id"])
        instructions_row, memory_row = pending(env, caller)
        accept(env, instructions_row["id"])
        dismiss(env, memory_row["id"])
        rows = _run(ns.review_rows(uid(env), [
            get_row(env, instructions_row["id"]), get_row(env, memory_row["id"]),
        ]))
        assert [r["doc_title"] for r in rows] == [doc["title"], doc["title"]]
        assert rows[0]["current_instructions"] is None
        assert rows[0]["instructions_moved"] is False
        assert rows[1]["replaces_content"] is None

    def test_a_failed_publish_never_fails_a_landed_accept_or_dismiss(self, env, monkeypatch):
        doc = new_doc(env)
        caller = conv(env)
        suggest(caller, doc["id"], "memory", "One.")
        suggest(caller, doc["id"], "memory", "Two.")
        one, two = pending(env, caller)

        async def broken(user_id, conversation_id):
            raise RuntimeError("database gone")

        monkeypatch.setattr(ns, "publish_changed", broken)
        result = accept(env, one["id"])
        assert result["suggestion"]["status"] == "accepted"
        assert [m["content"] for m in memories(doc["id"])] == ["One."]
        assert dismiss(env, two["id"])["status"] == "dismissed"
