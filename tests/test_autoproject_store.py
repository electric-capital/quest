"""AutoProjects store (db/autoproject_store.py) against an isolated SQLite file.

Covers:

1. Pure helpers: title normalizing and the default title cut from a first
   prompt, ``cut_text``, ``active_run_summary``.
2. AutoProject CRUD: create (defaults, validation, title_is_default),
   update (a set title clears title_is_default, no-op edits move nothing),
   ``set_title_from_run`` (only while the title is the default one),
   archive / unarchive, list order and ``active_run``, delete returning
   every conversation id to sweep.
3. The unarchived-AutoProject cap (archived ones do not count, unarchive
   is refused at the cap, other users are unaffected).
4. Runs: ``allocate_run`` (seq allocation, one active run, archived
   refused, refusals consume no seq, gaps after a delete are never
   refilled, one run per conversation, the conversation must belong to the
   AutoProject, prompt caps), status transitions, ``complete_run`` and
   ``save_run_entry`` with their caps, ``brief_conflict_version``, run
   delete rules, the startup-sweep helpers, the paging and context reads,
   the frozen run context.
5. The brief: versions with their sources, the stale token, pruning at
   ``AUTOPROJECT_BRIEF_MAX_VERSIONS``, restore as a new version, the brief
   conflict of a run (entry saved, retry with the conflict version).
6. Event, unread and read seqs.
7. Owner scoping: every owner-scoped call made by a second user on the
   first user's AutoProject behaves exactly like one on a missing id and
   changes nothing.
8. Cascades from an AutoProject, a run conversation and a user delete, and
   the account-delete helpers.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from chat.autoprojects.constants import (
    AUTOPROJECT_BRIEF_MAX_CHARS,
    AUTOPROJECT_BRIEF_MAX_VERSIONS,
    AUTOPROJECT_CONTEXT_MAX_CHARS,
    AUTOPROJECT_CONVERSATION_ORIGIN,
    AUTOPROJECT_PROMPT_MAX_CHARS,
    AUTOPROJECT_RUN_NOTES_MAX_CHARS,
    AUTOPROJECT_RUN_RESULT_MAX_CHARS,
    AUTOPROJECT_RUN_SUMMARY_MAX_CHARS,
    AUTOPROJECT_TITLE_MAX_CHARS,
    MAX_ACTIVE_AUTOPROJECTS_PER_USER,
    autoproject_not_found_message,
)


def _run(coro):
    return asyncio.run(coro)


class _Env:
    """The store, the models and two seeded users (alice, bob)."""

    def __init__(self, isolated_db, store, alice: int, bob: int):
        self.db = isolated_db
        self.store = store
        self.models = isolated_db.models
        self.alice = alice
        self.bob = bob

    def sql(self, query: str, **params):
        with self.db.engine.connect() as conn:
            return conn.execute(text(query), params).fetchall()

    async def conv(self, user_id: int, autoproject_id=None, *, origin=None) -> str:
        """Insert a conversation row (a run conversation when
        ``autoproject_id`` is given) and return its id."""
        now = datetime.now(timezone.utc)
        cid = str(uuid.uuid4())
        async with self.db.AsyncSessionLocal() as session:
            session.add(self.models.Conversation(
                id=cid, user_id=user_id, created_at=now, last_message_at=now,
                autoproject_id=autoproject_id,
                origin=origin or (AUTOPROJECT_CONVERSATION_ORIGIN if autoproject_id else None),
            ))
            await session.commit()
        return cid

    async def new_ap(self, user_id=None, title="Research plan", **kw) -> dict:
        return await self.store.create_autoproject(user_id or self.alice, title=title, **kw)

    async def start(self, ap_id: str, prompt="Do the thing", user_id=None) -> dict:
        """A run conversation plus its allocated run."""
        cid = await self.conv(user_id or self.alice, ap_id)
        return await self.store.allocate_run(ap_id, conversation_id=cid, prompt=prompt)

    async def finished(self, ap_id: str, prompt="Do the thing", user_id=None, **entry) -> dict:
        run = await self.start(ap_id, prompt, user_id=user_id)
        return await self.store.complete_run(
            run["id"],
            summary=entry.get("summary", f"summary of {prompt[:40]}"),
            result=entry.get("result", "the result"),
            notes=entry.get("notes", "the notes"),
        )


@pytest.fixture()
def env(isolated_db):
    import db.autoproject_store as store

    async def _seed():
        async with isolated_db.AsyncSessionLocal() as session:
            users = [
                isolated_db.models.User(
                    email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}", name=name,
                )
                for name in ("alice", "bob")
            ]
            session.add_all(users)
            await session.commit()
            return [u.id for u in users]

    alice, bob = _run(_seed())
    return _Env(isolated_db, store, alice, bob)


# ---------------------------------------------------------------------------
# 1. Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("Plan", "Plan"),
    ("  Plan \n the   trip\t", "Plan the trip"),
    ("x" * AUTOPROJECT_TITLE_MAX_CHARS, "x" * AUTOPROJECT_TITLE_MAX_CHARS),
])
def test_normalize_title_accepts(env, raw, expected):
    assert env.store.normalize_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   \n\t", "x" * (AUTOPROJECT_TITLE_MAX_CHARS + 1), None, 42, ["a"],
])
def test_normalize_title_refuses(env, raw):
    with pytest.raises(env.store.AutoProjectValidationError) as exc:
        env.store.normalize_title(raw)
    assert exc.value.code == "invalid_title"


@pytest.mark.parametrize("prompt, expected", [
    ("Find flights to Lisbon\nand hotels", "Find flights to Lisbon"),
    ("\n\n   \n  Research   the\tmarket  \nmore", "Research the market"),
    ("", "Untitled AutoProject"),
    ("  \n \t\n", "Untitled AutoProject"),
    (None, "Untitled AutoProject"),
    ("w" * AUTOPROJECT_TITLE_MAX_CHARS, "w" * AUTOPROJECT_TITLE_MAX_CHARS),
    ("w" * 500, "w" * (AUTOPROJECT_TITLE_MAX_CHARS - 1) + "…"),
])
def test_title_from_prompt(env, prompt, expected):
    title = env.store.title_from_prompt(prompt)
    assert title == expected
    assert len(title) <= AUTOPROJECT_TITLE_MAX_CHARS
    assert env.store.normalize_title(title) == title  # always a valid title


@pytest.mark.parametrize("value, cap, expected", [
    ("short", 10, "short"),
    ("exactly10!", 10, "exactly10!"),
    ("eleven chars", 10, "eleven ch…"),
    ("trailing   spaces here", 11, "trailing…"),
    ("abc", 1, "…"),
    ("abc", 0, "…"),
])
def test_cut_text(env, value, cap, expected):
    assert env.store.cut_text(value, cap) == expected


def test_active_run_summary_shapes(env):
    store = env.store
    assert store.active_run_summary(None) is None
    run = {"id": "r", "seq": 3, "status": "waiting", "prompt": "p", "notes": "n"}
    assert store.active_run_summary(run) == {"id": "r", "seq": 3, "status": "waiting"}
    row = env.models.AutoProjectRun(id="r2", seq=4, status="running")
    assert store.active_run_summary(row) == {"id": "r2", "seq": 4, "status": "running"}


# ---------------------------------------------------------------------------
# 2. AutoProject CRUD
# ---------------------------------------------------------------------------


def test_create_defaults_and_get(env):
    store = env.store

    async def go():
        ap = await env.new_ap(title="  My \n plan ", title_is_default=True,
                              model=" gemini-x ", thinking_effort=" high ")
        assert ap["title"] == "My plan"
        assert ap["title_is_default"] is True
        assert ap["user_id"] == env.alice
        assert (ap["brief"], ap["brief_version"], ap["brief_updated_at"]) == ("", 0, None)
        assert (ap["model"], ap["thinking_effort"]) == ("gemini-x", "high")
        assert ap["archived"] is False and ap["unread"] is False
        assert (ap["next_run_seq"], ap["event_seq"], ap["unread_seq"], ap["read_seq"]) == (
            1, 0, 0, 0,
        )
        assert ap["created_at"] and ap["last_activity_at"]
        assert await store.get_autoproject(env.alice, ap["id"]) == ap
        assert await store.get_autoproject_by_id(ap["id"]) == ap
        plain = await env.new_ap(title="Named")
        assert plain["title_is_default"] is False
        given = await env.new_ap(autoproject_id="0f0e6f43-5c2c-4e8e-9a4e-0d4c4f7d2a11")
        assert given["id"] == "0f0e6f43-5c2c-4e8e-9a4e-0d4c4f7d2a11"

    _run(go())


@pytest.mark.parametrize("kwargs, code", [
    ({"title": ""}, "invalid_title"),
    ({"title": "x" * (AUTOPROJECT_TITLE_MAX_CHARS + 1)}, "invalid_title"),
    ({"model": ""}, "invalid_model"),
    ({"model": "m" * 101}, "invalid_model"),
    ({"model": 7}, "invalid_model"),
    ({"thinking_effort": "  "}, "invalid_thinking_effort"),
    ({"thinking_effort": "e" * 21}, "invalid_thinking_effort"),
])
def test_create_refuses_invalid_fields(env, kwargs, code):
    args = {"title": "ok", **kwargs}
    with pytest.raises(env.store.AutoProjectValidationError) as exc:
        _run(env.store.create_autoproject(env.alice, **args))
    assert exc.value.code == code
    assert env.sql("SELECT COUNT(*) FROM autoprojects") == [(0,)]


def test_update_title_clears_default_and_noop_moves_nothing(env):
    store = env.store

    async def go():
        ap = await env.new_ap(title="Cut from prompt", title_is_default=True)
        # Same title still counts as the user naming it.
        named = await store.update_autoproject(env.alice, ap["id"], title="Cut from prompt")
        assert named["title_is_default"] is False
        assert named["event_seq"] == ap["event_seq"] + 1

        # Nothing changed: no bump.
        same = await store.update_autoproject(env.alice, ap["id"], title="Cut from prompt")
        assert same["event_seq"] == named["event_seq"]
        assert same["updated_at"] == named["updated_at"]
        untouched = await store.update_autoproject(env.alice, ap["id"])
        assert untouched["event_seq"] == named["event_seq"]

        edited = await store.update_autoproject(
            env.alice, ap["id"], model="m1", thinking_effort="low",
        )
        assert (edited["model"], edited["thinking_effort"]) == ("m1", "low")
        assert edited["event_seq"] == named["event_seq"] + 1
        assert edited["title"] == "Cut from prompt"  # UNSET left alone
        cleared = await store.update_autoproject(
            env.alice, ap["id"], model=None, thinking_effort=None,
        )
        assert (cleared["model"], cleared["thinking_effort"]) == (None, None)

        with pytest.raises(store.AutoProjectValidationError):
            await store.update_autoproject(env.alice, ap["id"], title="   ")

    _run(go())


def test_set_title_from_run_only_while_default(env):
    store = env.store

    async def go():
        ap = await env.new_ap(title="from prompt", title_is_default=True)
        assert await store.set_title_from_run(ap["id"], "  Model   name ") is True
        after = await store.get_autoproject(env.alice, ap["id"])
        assert after["title"] == "Model name" and after["title_is_default"] is False
        assert after["event_seq"] == ap["event_seq"] + 1
        # Only one model-side rename ever lands.
        assert await store.set_title_from_run(ap["id"], "Second") is False

        user_named = await env.new_ap(title="from prompt", title_is_default=True)
        await store.update_autoproject(env.alice, user_named["id"], title="Mine")
        assert await store.set_title_from_run(user_named["id"], "Model") is False
        assert (await store.get_autoproject(env.alice, user_named["id"]))["title"] == "Mine"

        with pytest.raises(store.AutoProjectValidationError):
            await store.set_title_from_run(ap["id"], "")
        assert await store.set_title_from_run(str(uuid.uuid4()), "x") is False

    _run(go())


def test_archive_unarchive_and_list(env):
    store = env.store

    async def go():
        a = await env.new_ap(title="A")
        b = await env.new_ap(title="B")
        c = await env.new_ap(title="C")
        await env.new_ap(user_id=env.bob, title="Bob's")
        # A run write moves last_activity_at: A becomes the newest.
        await env.finished(a["id"])
        listed = await store.list_autoprojects(env.alice)
        assert [r["title"] for r in listed] == ["A", "C", "B"]
        assert all(r["active_run"] is None for r in listed)

        archived = await store.set_autoproject_archived(env.alice, b["id"], True)
        assert archived["archived"] is True
        assert archived["last_activity_at"] == b["last_activity_at"]
        again = await store.set_autoproject_archived(env.alice, b["id"], True)
        assert again["event_seq"] == archived["event_seq"]  # no-op
        assert [r["title"] for r in await store.list_autoprojects(env.alice)] == ["A", "C"]
        assert [r["title"] for r in await store.list_autoprojects(env.alice, archived=True)] == [
            "B",
        ]
        back = await store.set_autoproject_archived(env.alice, b["id"], False)
        assert back["archived"] is False and back["last_activity_at"] == b["last_activity_at"]

        # An active run blocks archiving; the list shows it.
        run = await env.start(c["id"])
        with pytest.raises(store.AutoProjectRunActiveError) as exc:
            await store.set_autoproject_archived(env.alice, c["id"], True)
        assert exc.value.code == "autoproject_run_active"
        assert exc.value.active_run == {"id": run["id"], "seq": 1, "status": "running"}
        assert (await store.get_autoproject(env.alice, c["id"]))["archived"] is False
        rows = {r["title"]: r for r in await store.list_autoprojects(env.alice)}
        assert rows["C"]["active_run"] == {"id": run["id"], "seq": 1, "status": "running"}
        await store.set_run_status(run["id"], "waiting")
        rows = {r["title"]: r for r in await store.list_autoprojects(env.alice)}
        assert rows["C"]["active_run"]["status"] == "waiting"

    _run(go())


def test_cap_counts_unarchived_only(env):
    store = env.store

    async def go():
        ids = []
        for i in range(MAX_ACTIVE_AUTOPROJECTS_PER_USER):
            ids.append((await env.new_ap(title=f"AP {i}"))["id"])
        with pytest.raises(store.AutoProjectCapReachedError) as exc:
            await env.new_ap(title="one too many")
        assert exc.value.code == "autoprojects_cap_reached"
        assert exc.value.details == {"cap": MAX_ACTIVE_AUTOPROJECTS_PER_USER}
        # The refused create left nothing behind; bob is unaffected.
        assert len(await store.list_autoprojects(env.alice)) == MAX_ACTIVE_AUTOPROJECTS_PER_USER
        await env.new_ap(user_id=env.bob, title="Bob's first")

        # An archived AutoProject frees a slot ...
        await store.set_autoproject_archived(env.alice, ids[0], True)
        await env.new_ap(title="fits again")
        # ... and cannot come back while the cap is full.
        with pytest.raises(store.AutoProjectCapReachedError):
            await store.set_autoproject_archived(env.alice, ids[0], False)
        assert (await store.get_autoproject(env.alice, ids[0]))["archived"] is True
        # Deleting one makes room for the unarchive.
        await store.delete_autoproject(env.alice, ids[1])
        assert (await store.set_autoproject_archived(env.alice, ids[0], False))["archived"] is False

    _run(go())


# ---------------------------------------------------------------------------
# 4. Runs
# ---------------------------------------------------------------------------


def test_allocate_run_seqs_one_active_and_gaps(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        r1 = await env.start(ap["id"], "first prompt")
        assert (r1["seq"], r1["status"], r1["prompt"]) == (1, "running", "first prompt")
        assert r1["summary"] is None and r1["finished_at"] is None
        assert r1["context_brief_version"] is None and r1["brief_conflict_version"] is None

        # One active run: a second start is refused and consumes no seq,
        # whether the active run is running or waiting.
        for status in ("running", "waiting"):
            if status == "waiting":
                await store.set_run_status(r1["id"], "waiting")
            cid = await env.conv(env.alice, ap["id"])
            with pytest.raises(store.AutoProjectRunActiveError) as exc:
                await store.allocate_run(ap["id"], conversation_id=cid, prompt="second")
            assert exc.value.active_run == {"id": r1["id"], "seq": 1, "status": status}
        assert (await store.get_autoproject(env.alice, ap["id"]))["next_run_seq"] == 2

        await store.complete_run(r1["id"], summary="s", result="r", notes="n")
        r2 = await env.finished(ap["id"])
        r3 = await env.finished(ap["id"])
        assert (r2["seq"], r3["seq"]) == (2, 3)

        # Delete run 2: its number is never handed out again.
        await store.delete_run(env.alice, ap["id"], r2["id"])
        r4 = await env.start(ap["id"])
        assert r4["seq"] == 4
        assert await store.get_run_by_seq(ap["id"], 2) is None
        assert (await store.get_run_by_seq(ap["id"], 4))["id"] == r4["id"]
        assert await store.count_runs(ap["id"]) == 3
        assert await store.count_runs(ap["id"], before_seq=4) == 2
        assert (await store.get_active_run(ap["id"]))["id"] == r4["id"]
        assert (await store.get_run_by_conversation(r4["conversation_id"]))["id"] == r4["id"]
        assert await store.get_run_by_conversation(str(uuid.uuid4())) is None

    _run(go())


def test_allocate_run_refusals(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        other = await env.new_ap(title="Other")

        # Missing AutoProject.
        cid = await env.conv(env.alice, ap["id"])
        with pytest.raises(store.AutoProjectNotFoundError):
            await store.allocate_run(str(uuid.uuid4()), conversation_id=cid, prompt="p")
        # A conversation of another AutoProject, or a plain chat.
        foreign_cid = await env.conv(env.alice, other["id"])
        plain_cid = await env.conv(env.alice)
        for bad in (foreign_cid, plain_cid, str(uuid.uuid4())):
            with pytest.raises(ValueError, match="not a run conversation"):
                await store.allocate_run(ap["id"], conversation_id=bad, prompt="p")

        # One run per conversation, even after the first finished.
        run = await store.allocate_run(ap["id"], conversation_id=cid, prompt="p")
        await store.complete_run(run["id"], summary="s", result="r", notes="n")
        with pytest.raises(store.AutoProjectRunExistsError) as exc:
            await store.allocate_run(ap["id"], conversation_id=cid, prompt="again")
        assert exc.value.code == "autoproject_run_single_message"

        # Archived.
        await store.set_autoproject_archived(env.alice, ap["id"], True)
        with pytest.raises(store.AutoProjectArchivedError) as exc:
            await env.start(ap["id"])
        assert exc.value.code == "autoproject_archived"

        # None of the refusals consumed a seq.
        await store.set_autoproject_archived(env.alice, ap["id"], False)
        assert (await env.start(ap["id"]))["seq"] == 2

    _run(go())


@pytest.mark.parametrize("prompt", [
    "", "   \n\t ", None, 12, "p" * (AUTOPROJECT_PROMPT_MAX_CHARS + 1),
])
def test_allocate_run_prompt_refused(env, prompt):
    store = env.store

    async def go():
        ap = await env.new_ap()
        cid = await env.conv(env.alice, ap["id"])
        with pytest.raises(store.AutoProjectValidationError) as exc:
            await store.allocate_run(ap["id"], conversation_id=cid, prompt=prompt)
        assert exc.value.code == "invalid_prompt"
        if isinstance(prompt, str) and prompt.strip():
            assert exc.value.details == {
                "cap": AUTOPROJECT_PROMPT_MAX_CHARS, "actual": len(prompt),
            }
        assert await store.count_runs(ap["id"]) == 0

    _run(go())


def test_allocate_run_prompt_stored_verbatim_up_to_cap(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        raw = "  indented\n\n  prompt  \n"
        assert (await env.start(ap["id"], raw))["prompt"] == raw
        await store.set_run_status((await store.get_active_run(ap["id"]))["id"], "stopped")
        at_cap = "p" * AUTOPROJECT_PROMPT_MAX_CHARS
        assert (await env.start(ap["id"], at_cap))["prompt"] == at_cap

    _run(go())


def test_allocate_run_bumps_counters(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        await env.start(ap["id"])
        after = await store.get_autoproject(env.alice, ap["id"])
        assert after["next_run_seq"] == 2
        assert after["event_seq"] == ap["event_seq"] + 1
        assert after["last_activity_at"] >= ap["last_activity_at"]

    _run(go())


# (from, to, lands)
_TRANSITIONS = [
    ("running", "waiting", True),
    ("waiting", "running", True),
    ("running", "failed", True),
    ("waiting", "stopped", True),
    ("running", "stopped", True),
    ("completed", "running", False),
    ("completed", "failed", False),
    ("failed", "running", False),
    ("stopped", "waiting", False),
]


@pytest.mark.parametrize("start, target, lands", _TRANSITIONS)
def test_run_status_transitions(env, start, target, lands):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.start(ap["id"])
        if start == "completed":
            await store.complete_run(run["id"], summary="s", result="r", notes="n")
        elif start != "running":
            await store.set_run_status(run["id"], start)
        before = await store.get_autoproject(env.alice, ap["id"])
        changed = await store.set_run_status(run["id"], target, error="boom")
        after = await store.get_autoproject(env.alice, ap["id"])
        if not lands:
            assert changed is None
            assert (await store.get_run(run["id"]))["status"] == start
            assert after["event_seq"] == before["event_seq"]
            return
        assert changed["status"] == target
        assert after["event_seq"] == before["event_seq"] + 1
        if target in ("failed", "stopped"):
            assert changed["finished_at"]
        else:
            assert changed["finished_at"] is None
        # The error is written for a failure only.
        assert changed["error"] == ("boom" if target == "failed" else None)

    _run(go())


def test_set_run_status_guards(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.start(ap["id"])
        for bad in ("completed", "done", ""):
            with pytest.raises(ValueError):
                await store.set_run_status(run["id"], bad)
        # expected_from narrows the allowed starting statuses.
        assert await store.set_run_status(
            run["id"], "stopped", expected_from={"waiting"},
        ) is None
        assert (await store.set_run_status(
            run["id"], "stopped", expected_from={"running"},
        ))["status"] == "stopped"
        # ... and can reopen a finished run when a caller asks for it.
        assert (await store.set_run_status(
            run["id"], "running", expected_from={"stopped"},
        ))["status"] == "running"
        assert await store.set_run_status(str(uuid.uuid4()), "failed") is None

    _run(go())


_ENTRY_CAPS = [
    ("summary", AUTOPROJECT_RUN_SUMMARY_MAX_CHARS),
    ("result", AUTOPROJECT_RUN_RESULT_MAX_CHARS),
    ("notes", AUTOPROJECT_RUN_NOTES_MAX_CHARS),
]


@pytest.mark.parametrize("writer", ["complete_run", "save_run_entry"])
@pytest.mark.parametrize("field, cap", _ENTRY_CAPS)
def test_entry_caps(env, writer, field, cap):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.start(ap["id"])
        write = getattr(store, writer)
        entry = {"summary": "s", "result": "r", "notes": "n"}
        with pytest.raises(store.AutoProjectValidationError) as exc:
            await write(run["id"], **{**entry, field: "x" * (cap + 1)})
        assert exc.value.code == f"{field}_too_long"
        assert exc.value.details == {"cap": cap, "actual": cap + 1}
        # A refused entry writes nothing.
        unchanged = await store.get_run(run["id"])
        assert unchanged["status"] == "running" and unchanged[field] is None
        # Whitespace around the text does not count against the cap.
        saved = await write(run["id"], **{**entry, field: "  " + "x" * cap + "\n"})
        assert saved[field] == "x" * cap

    _run(go())


@pytest.mark.parametrize("field", ["summary", "result", "notes"])
def test_entry_refuses_non_strings(env, field):
    with pytest.raises(env.store.AutoProjectValidationError) as exc:
        env.store.validate_run_entry(**{"summary": "s", "result": "r", "notes": "n", field: 5})
    assert exc.value.code == f"invalid_{field}"
    assert env.store.validate_run_entry(None, None, None) == ("", "", "")


def test_complete_run(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.start(ap["id"])
        await store.set_run_status(run["id"], "waiting")  # completes from waiting too
        done = await store.complete_run(
            run["id"], summary="  one line ", result="\nanswer\n", notes=" notes ",
        )
        assert done["status"] == "completed" and done["finished_at"]
        assert (done["summary"], done["result"], done["notes"]) == ("one line", "answer", "notes")
        assert done["handoff_missing"] is False and done["error"] is None

        with pytest.raises(store.AutoProjectRunFinishedError) as exc:
            await store.complete_run(run["id"], summary="x", result="x", notes="x")
        assert exc.value.code == "autoproject_run_finished" and exc.value.status == "completed"
        assert (await store.get_run(run["id"]))["summary"] == "one line"
        with pytest.raises(store.AutoProjectRunNotFoundError):
            await store.complete_run(str(uuid.uuid4()), summary="x", result="x", notes="x")

        missing = await env.start(ap["id"])
        filled = await store.complete_run(
            missing["id"], summary="final", result="final text", notes="", handoff_missing=True,
        )
        assert filled["handoff_missing"] is True and filled["notes"] == ""

        stopped = await env.start(ap["id"])
        await store.set_run_status(stopped["id"], "stopped")
        with pytest.raises(store.AutoProjectRunFinishedError) as exc:
            await store.complete_run(stopped["id"], summary="x", result="x", notes="x")
        assert exc.value.status == "stopped"

    _run(go())


def test_save_run_entry_keeps_run_open(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.start(ap["id"])
        before = await store.get_autoproject(env.alice, ap["id"])
        pending = await store.save_run_entry(
            run["id"], summary=" s ", result="r", notes="n", brief_conflict_version=3,
        )
        assert pending["status"] == "running" and pending["finished_at"] is None
        assert (pending["summary"], pending["result"], pending["notes"]) == ("s", "r", "n")
        assert pending["brief_conflict_version"] == 3
        # A pending entry changes nothing the AutoProject page shows.
        after = await store.get_autoproject(env.alice, ap["id"])
        assert after["event_seq"] == before["event_seq"]
        # Saving again without a version keeps the recorded one.
        again = await store.save_run_entry(run["id"], summary="s2", result="r", notes="n")
        assert again["summary"] == "s2" and again["brief_conflict_version"] == 3
        # complete_run finalizes over the pending entry.
        done = await store.complete_run(run["id"], summary="s3", result="r3", notes="n3")
        assert (done["status"], done["summary"], done["brief_conflict_version"]) == (
            "completed", "s3", 3,
        )
        with pytest.raises(store.AutoProjectRunFinishedError):
            await store.save_run_entry(run["id"], summary="s", result="r", notes="n")
        with pytest.raises(store.AutoProjectRunNotFoundError):
            await store.save_run_entry(str(uuid.uuid4()), summary="s", result="r", notes="n")

    _run(go())


def test_brief_conflict_flow(env):
    """A run started on brief v1; the user edits it to v2 during the run;
    the run's write is stale, its entry is saved with the conflict version,
    and a retry against that version lands as the run's brief."""
    store = env.store
    from chat.autoprojects.runs import brief_write_base_version

    async def go():
        ap = await env.new_ap()
        await store.set_brief(ap["id"], "v1 text", expected_brief_version=0, source="ui")
        run = await env.start(ap["id"])
        await store.freeze_run_context(run["id"], context="ctx", brief_version=1)
        await store.set_brief(ap["id"], "user edit", expected_brief_version=1, source="ui")

        run = await store.get_run(run["id"])
        base = brief_write_base_version(run, None)
        assert base == 1
        with pytest.raises(store.StaleBriefError) as exc:
            await store.set_brief(
                ap["id"], "run brief", expected_brief_version=base, source=f"run:{run['id']}",
            )
        current = exc.value.current
        assert exc.value.code == "stale_brief"
        assert (current["brief"], current["brief_version"]) == ("user edit", 2)
        assert current["brief_updated_at"]
        await store.save_run_entry(
            run["id"], summary="s", result="r", notes="n",
            brief_conflict_version=current["brief_version"],
        )
        run = await store.get_run(run["id"])
        assert run["status"] == "running" and run["brief_conflict_version"] == 2

        # The model may only use the version the conflict handed it.
        assert brief_write_base_version(run, 2) == 2
        assert brief_write_base_version(run, 5) == 1
        assert brief_write_base_version(run, None) == 1
        merged = await store.set_brief(
            ap["id"], "user edit + run brief",
            expected_brief_version=brief_write_base_version(run, 2), source=f"run:{run['id']}",
        )
        assert (merged["brief"], merged["brief_version"]) == ("user edit + run brief", 3)
        assert await store.get_brief_version_source(ap["id"], 3) == f"run:{run['id']}"
        await store.complete_run(run["id"], summary="s", result="r", notes="n")

    _run(go())


@pytest.mark.parametrize("run, expected_arg, base", [
    ({"context_brief_version": 4, "brief_conflict_version": None}, None, 4),
    ({"context_brief_version": 4, "brief_conflict_version": None}, 5, 4),
    ({"context_brief_version": 4, "brief_conflict_version": None}, 4, 4),
    ({"context_brief_version": 4, "brief_conflict_version": 6}, 6, 6),
    ({"context_brief_version": 4, "brief_conflict_version": 6}, 7, 4),
    ({"context_brief_version": None, "brief_conflict_version": None}, 3, None),
])
def test_brief_write_base_version_accepts_only_the_conflict_version(run, expected_arg, base):
    from chat.autoprojects.runs import brief_write_base_version

    assert brief_write_base_version(run, expected_arg) == base


def test_delete_run_rules(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        other = await env.new_ap(title="Other")
        done = await env.finished(ap["id"])
        other_run = await env.finished(other["id"])
        active = await env.start(ap["id"])
        before = await store.get_autoproject(env.alice, ap["id"])

        with pytest.raises(store.AutoProjectRunActiveError) as exc:
            await store.delete_run(env.alice, ap["id"], active["id"])
        assert exc.value.code == "autoproject_run_active"
        # A run of another AutoProject reads as not found there.
        with pytest.raises(store.AutoProjectRunNotFoundError):
            await store.delete_run(env.alice, ap["id"], other_run["id"])
        with pytest.raises(store.AutoProjectRunNotFoundError):
            await store.delete_run(env.alice, ap["id"], str(uuid.uuid4()))
        with pytest.raises(store.AutoProjectNotFoundError):
            await store.delete_run(env.bob, ap["id"], done["id"])
        assert (await store.get_autoproject(env.alice, ap["id"]))["event_seq"] == before["event_seq"]

        cid = await store.delete_run(env.alice, ap["id"], done["id"])
        assert cid == done["conversation_id"]
        assert await store.get_run(done["id"]) is None
        assert env.sql("SELECT id FROM conversations WHERE id = :c", c=cid) == []
        after = await store.get_autoproject(env.alice, ap["id"])
        assert after["event_seq"] == before["event_seq"] + 1
        assert after["next_run_seq"] == before["next_run_seq"]
        assert await store.get_run(other_run["id"]) is not None

    _run(go())


def test_run_listings(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        runs = [await env.finished(ap["id"], f"prompt {i} " + "x" * 300) for i in range(1, 8)]
        await store.delete_run(env.alice, ap["id"], runs[2]["id"])  # seq 3

        page = await store.list_runs_page(ap["id"], limit=3)
        assert [r["seq"] for r in page["runs"]] == [7, 6, 5]
        assert page["next_before_seq"] == 5
        page = await store.list_runs_page(ap["id"], before_seq=5, limit=3)
        assert [r["seq"] for r in page["runs"]] == [4, 2, 1]
        assert page["next_before_seq"] is None
        assert "result" in page["runs"][0]
        assert (await store.list_runs_page(ap["id"], limit=0))["runs"][0]["seq"] == 7
        assert [r["seq"] for r in (await store.list_runs_page(ap["id"], limit=10_000))["runs"]] == [
            7, 6, 5, 4, 2, 1,
        ]

        ctx = await store.list_context_runs(ap["id"], before_seq=6, limit=3)
        assert [r["seq"] for r in ctx] == [5, 4, 2]
        assert all("result" not in r for r in ctx)
        assert ctx[0]["notes"] == "the notes"

        index = await store.list_run_index(ap["id"], offset=1, limit=2, prompt_chars=10)
        assert [r["seq"] for r in index] == [6, 5]
        assert index[0]["prompt_head"] == "prompt 6 x"
        assert set(index[0]) == {
            "id", "seq", "status", "summary", "handoff_missing", "started_at",
            "finished_at", "prompt_head",
        }

        seqs = await store.get_run_seqs(
            ap["id"], [runs[0]["id"], runs[2]["id"], "", None, "nope"],
        )
        assert seqs == {runs[0]["id"]: 1}
        assert await store.get_run_seqs(ap["id"], []) == {}

    _run(go())


def test_startup_sweep_helpers(env):
    store = env.store

    async def go():
        a = await env.new_ap(title="A")
        b = await env.new_ap(title="B")
        c = await env.new_ap(user_id=env.bob, title="C")
        running = await env.start(a["id"])
        waiting = await env.start(b["id"])
        await store.set_run_status(waiting["id"], "waiting")
        bob_running = await env.start(c["id"], user_id=env.bob)
        done = await env.finished((await env.new_ap(title="D"))["id"])

        assert [r["id"] for r in await store.list_runs_with_status("waiting")] == [waiting["id"]]
        assert {r["id"] for r in await store.list_runs_with_status("running")} == {
            running["id"], bob_running["id"],
        }
        assert [r["id"] for r in await store.list_runs_with_status("completed")] == [done["id"]]

        before = await store.get_autoproject(env.alice, a["id"])
        failed = await store.fail_interrupted_runs()
        assert {r["id"] for r in failed} == {running["id"], bob_running["id"]}
        assert all(
            r["status"] == "failed" and r["error"] == "interrupted by a restart"
            and r["finished_at"] for r in failed
        )
        assert (await store.get_run(waiting["id"]))["status"] == "waiting"
        assert (await store.get_autoproject(env.alice, a["id"]))["event_seq"] == (
            before["event_seq"] + 1
        )
        assert await store.fail_interrupted_runs() == []

    _run(go())


def test_freeze_run_context(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        other = await env.finished(ap["id"])
        run = await env.start(ap["id"])
        assert await store.get_run_context(run["id"]) is None
        assert await store.freeze_run_context(run["id"], context="first", brief_version=2) == (
            "first", 2,
        )
        # First writer wins, whatever later callers pass.
        assert await store.freeze_run_context(run["id"], context="second", brief_version=5) == (
            "first", 2,
        )
        assert await store.get_run_context(run["id"]) == ("first", 2)
        frozen = await store.get_run(run["id"])
        assert frozen["context_brief_version"] == 2 and "context" not in frozen
        # Freezing does not care about the run's status.
        assert await store.freeze_run_context(other["id"], context="", brief_version=0) == ("", 0)

        with pytest.raises(store.AutoProjectRunNotFoundError):
            await store.freeze_run_context(str(uuid.uuid4()), context="x", brief_version=0)
        bad = [
            {"context": "x" * (AUTOPROJECT_CONTEXT_MAX_CHARS + 1), "brief_version": 0},
            {"context": None, "brief_version": 0},
            {"context": "x", "brief_version": -1},
            {"context": "x", "brief_version": True},
            {"context": "x", "brief_version": "1"},
        ]
        for kwargs in bad:
            with pytest.raises(ValueError):
                await store.freeze_run_context(run["id"], **kwargs)
        ok = "x" * AUTOPROJECT_CONTEXT_MAX_CHARS
        await store.set_run_status(run["id"], "stopped")
        fresh = await env.start(ap["id"])
        assert (await store.freeze_run_context(fresh["id"], context=ok, brief_version=0))[0] == ok
        assert await store.get_run_context(str(uuid.uuid4())) is None

    _run(go())


# ---------------------------------------------------------------------------
# 5. Brief
# ---------------------------------------------------------------------------


def test_set_brief_versions_and_sources(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.start(ap["id"])
        s1 = await store.set_brief(ap["id"], "  first\n\nbrief  ", expected_brief_version=0,
                                   source="ui")
        assert (s1["brief"], s1["brief_version"]) == ("first\n\nbrief", 1)
        assert s1["brief_updated_at"]
        s2 = await store.set_brief(ap["id"], "second", expected_brief_version=1,
                                   source=f"run:{run['id']}")
        s3 = await store.restore_brief_version(ap["id"], 1, expected_brief_version=2)
        assert (s3["brief"], s3["brief_version"]) == ("first\n\nbrief", 3)
        s4 = await store.set_brief(ap["id"], "", expected_brief_version=3, source="ui")
        assert (s4["brief"], s4["brief_version"]) == ("", 4)  # empty allowed
        assert s2["brief_version"] == 2

        versions = await store.list_brief_versions(ap["id"], excerpt_chars=8)
        assert [(v["version"], v["source"], v["excerpt"]) for v in versions] == [
            (4, "ui", ""),
            (3, "restore:1", "first b…"),
            (2, f"run:{run['id']}", "second"),
            (1, "ui", "first b…"),
        ]
        assert all(v["created_at"] for v in versions)
        assert (await store.get_brief_version(ap["id"], 3))["content"] == "first\n\nbrief"
        assert (await store.get_brief_version(ap["id"], 2))["source"] == f"run:{run['id']}"
        assert await store.get_brief_version(ap["id"], 0) is None
        assert await store.get_brief_version_source(ap["id"], 0) is None
        assert await store.get_brief_version_source(ap["id"], 3) == "restore:1"

        after = await store.get_autoproject(env.alice, ap["id"])
        assert (after["brief"], after["brief_version"]) == ("", 4)

    _run(go())


def test_set_brief_bumps_event_seq_not_activity(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        await store.set_brief(ap["id"], "x", expected_brief_version=0, source="ui")
        after = await store.get_autoproject(env.alice, ap["id"])
        assert after["event_seq"] == ap["event_seq"] + 1
        assert after["last_activity_at"] == ap["last_activity_at"]

    _run(go())


@pytest.mark.parametrize("kwargs, error, code", [
    ({"content": "x" * (AUTOPROJECT_BRIEF_MAX_CHARS + 1)}, "validation", "brief_too_long"),
    ({"content": 5}, "validation", "invalid_brief"),
    ({"source": "user"}, "value", None),
    ({"source": "run:"}, "value", None),
    ({"source": "run:../x"}, "value", None),
    ({"source": "restore:abc"}, "value", None),
    ({"source": "restore:1234567890"}, "value", None),
    ({"expected_brief_version": -1}, "value", None),
    ({"expected_brief_version": True}, "value", None),
    ({"expected_brief_version": "0"}, "value", None),
    ({"expected_brief_version": None}, "value", None),
])
def test_set_brief_refusals(env, kwargs, error, code):
    store = env.store

    async def go():
        ap = await env.new_ap()
        args = {"content": "ok", "expected_brief_version": 0, "source": "ui", **kwargs}
        content = args.pop("content")
        if error == "validation":
            with pytest.raises(store.AutoProjectValidationError) as exc:
                await store.set_brief(ap["id"], content, **args)
            assert exc.value.code == code
        else:
            with pytest.raises(ValueError):
                await store.set_brief(ap["id"], content, **args)
        after = await store.get_autoproject(env.alice, ap["id"])
        assert (after["brief"], after["brief_version"]) == ("", 0)
        assert await store.list_brief_versions(ap["id"]) == []

    _run(go())


def test_set_brief_at_cap_and_stale_token(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        full = "b" * AUTOPROJECT_BRIEF_MAX_CHARS
        assert (await store.set_brief(ap["id"], full, expected_brief_version=0,
                                      source="ui"))["brief"] == full
        for stale in (0, 2, 99):
            with pytest.raises(store.StaleBriefError) as exc:
                await store.set_brief(ap["id"], "new", expected_brief_version=stale, source="ui")
            assert exc.value.current["brief"] == full
            assert exc.value.current["brief_version"] == 1
            assert exc.value.details == {"current": exc.value.current}
        assert len(await store.list_brief_versions(ap["id"])) == 1
        with pytest.raises(store.AutoProjectNotFoundError):
            await store.set_brief(str(uuid.uuid4()), "x", expected_brief_version=0, source="ui")

    _run(go())


def test_brief_versions_pruned_at_max(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        other = await env.new_ap(title="Other")
        await store.set_brief(other["id"], "kept", expected_brief_version=0, source="ui")
        total = AUTOPROJECT_BRIEF_MAX_VERSIONS + 5
        for v in range(total):
            await store.set_brief(ap["id"], f"text {v + 1}", expected_brief_version=v, source="ui")
        versions = await store.list_brief_versions(ap["id"])
        assert len(versions) == AUTOPROJECT_BRIEF_MAX_VERSIONS
        assert versions[0]["version"] == total
        assert versions[-1]["version"] == total - AUTOPROJECT_BRIEF_MAX_VERSIONS + 1
        assert await store.get_brief_version(ap["id"], 5) is None
        # Another AutoProject's versions are not pruned with these.
        assert [v["version"] for v in await store.list_brief_versions(other["id"])] == [1]

        # A pruned version cannot be restored; the oldest kept one can.
        with pytest.raises(store.BriefVersionNotFoundError) as exc:
            await store.restore_brief_version(ap["id"], 1, expected_brief_version=total)
        assert exc.value.code == "brief_version_not_found"
        oldest = total - AUTOPROJECT_BRIEF_MAX_VERSIONS + 1
        restored = await store.restore_brief_version(ap["id"], oldest, expected_brief_version=total)
        assert restored["brief"] == f"text {oldest}"
        versions = await store.list_brief_versions(ap["id"])
        assert len(versions) == AUTOPROJECT_BRIEF_MAX_VERSIONS
        assert versions[0]["source"] == f"restore:{oldest}"
        # The restore pruned the version it restored from: it was the oldest.
        assert await store.get_brief_version(ap["id"], oldest) is None

    _run(go())


def test_restore_refusals(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        await store.set_brief(ap["id"], "one", expected_brief_version=0, source="ui")
        await store.set_brief(ap["id"], "two", expected_brief_version=1, source="ui")
        with pytest.raises(store.StaleBriefError) as exc:
            await store.restore_brief_version(ap["id"], 1, expected_brief_version=1)
        assert exc.value.current["brief"] == "two"
        with pytest.raises(store.BriefVersionNotFoundError):
            await store.restore_brief_version(ap["id"], 7, expected_brief_version=2)
        with pytest.raises(store.AutoProjectNotFoundError):
            await store.restore_brief_version(str(uuid.uuid4()), 1, expected_brief_version=0)
        with pytest.raises(ValueError):
            await store.restore_brief_version(ap["id"], 1, expected_brief_version=-1)
        assert (await store.get_autoproject(env.alice, ap["id"]))["brief_version"] == 2

    _run(go())


# ---------------------------------------------------------------------------
# 6. Event, unread and read seqs
# ---------------------------------------------------------------------------


def test_unread_and_read_seqs(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        assert await store.mark_autoproject_unread(ap["id"]) is False  # event_seq 0
        await env.finished(ap["id"])  # event_seq 2
        assert await store.mark_autoproject_unread(ap["id"]) is True
        state = await store.get_autoproject(env.alice, ap["id"])
        assert state["unread_seq"] == state["event_seq"] == 2 and state["unread"] is True
        assert await store.mark_autoproject_unread(ap["id"]) is False

        # A read beyond event_seq is clamped; a stale read never lowers.
        assert await store.mark_autoproject_read(env.alice, ap["id"], up_to_seq=999) is True
        state = await store.get_autoproject(env.alice, ap["id"])
        assert state["read_seq"] == 2 and state["unread"] is False
        assert await store.mark_autoproject_read(env.alice, ap["id"], up_to_seq=1) is False
        assert (await store.get_autoproject(env.alice, ap["id"]))["read_seq"] == 2

        assert await store.bump_event_seq(ap["id"]) == 3
        assert await store.bump_event_seq(str(uuid.uuid4())) is None
        assert await store.mark_autoproject_unread(ap["id"]) is True
        listed = (await store.list_autoprojects(env.alice))[0]
        assert listed["unread"] is True
        assert await store.mark_autoproject_read(env.alice, ap["id"], up_to_seq=3) is True
        assert (await store.list_autoprojects(env.alice))[0]["unread"] is False

        with pytest.raises(store.AutoProjectNotFoundError):
            await store.mark_autoproject_read(env.alice, str(uuid.uuid4()), up_to_seq=1)

    _run(go())


# ---------------------------------------------------------------------------
# 7. Owner scoping
# ---------------------------------------------------------------------------


def _owner_scoped_calls(store, user_id, ap_id, run_id):
    return {
        "update_autoproject": lambda: store.update_autoproject(user_id, ap_id, title="Hijack"),
        "archive": lambda: store.set_autoproject_archived(user_id, ap_id, True),
        "unarchive": lambda: store.set_autoproject_archived(user_id, ap_id, False),
        "delete_autoproject": lambda: store.delete_autoproject(user_id, ap_id),
        "delete_run": lambda: store.delete_run(user_id, ap_id, run_id),
        "mark_read": lambda: store.mark_autoproject_read(user_id, ap_id, up_to_seq=99),
    }


@pytest.mark.parametrize("call", [
    "update_autoproject", "archive", "unarchive", "delete_autoproject", "delete_run", "mark_read",
])
def test_foreign_autoproject_behaves_like_a_missing_one(env, call):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.finished(ap["id"])
        await store.mark_autoproject_unread(ap["id"])
        if call == "unarchive":
            await store.set_autoproject_archived(env.alice, ap["id"], True)
        before = await store.get_autoproject(env.alice, ap["id"])
        runs_before = await store.list_runs_page(ap["id"])

        with pytest.raises(store.AutoProjectNotFoundError) as foreign:
            await _owner_scoped_calls(store, env.bob, ap["id"], run["id"])[call]()
        # The one not-found text, exactly what a missing id gets.
        assert str(foreign.value) == autoproject_not_found_message(ap["id"])
        assert foreign.value.code == "autoproject_not_found"
        assert foreign.value.details == {}
        with pytest.raises(store.AutoProjectNotFoundError) as missing:
            await _owner_scoped_calls(store, env.alice, "missing-id", run["id"])[call]()
        assert str(missing.value) == autoproject_not_found_message("missing-id")

        # Nothing of alice's moved.
        assert await store.get_autoproject(env.alice, ap["id"]) == before
        assert await store.list_runs_page(ap["id"]) == runs_before

    _run(go())


def test_foreign_reads_return_nothing(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        assert await store.get_autoproject(env.bob, ap["id"]) is None
        assert await store.get_autoproject(env.bob, str(uuid.uuid4())) is None
        assert await store.list_autoprojects(env.bob) == []
        assert await store.list_autoprojects(env.bob, archived=True) == []
        assert await store.list_autoproject_ids_for_user(env.bob) == []

    _run(go())


def test_missing_and_foreign_errors_are_identical(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        with pytest.raises(store.AutoProjectNotFoundError) as foreign:
            await store.update_autoproject(env.bob, ap["id"], title="x")
        await store.delete_autoproject(env.alice, ap["id"])
        with pytest.raises(store.AutoProjectNotFoundError) as missing:
            await store.update_autoproject(env.alice, ap["id"], title="x")
        assert (str(foreign.value), foreign.value.code, foreign.value.details) == (
            str(missing.value), missing.value.code, missing.value.details,
        )

    _run(go())


def test_concurrent_writers_cannot_both_pass_a_check(env):
    """Each check-then-write runs in the transaction that holds the write
    lock: of two concurrent starts, brief writes on one token, or creates
    at the cap, exactly one lands."""
    store = env.store

    async def go():
        ap = await env.new_ap()
        convs = [await env.conv(env.alice, ap["id"]) for _ in range(2)]
        results = await asyncio.gather(
            *(store.allocate_run(ap["id"], conversation_id=c, prompt="p") for c in convs),
            return_exceptions=True,
        )
        started = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, store.AutoProjectRunActiveError)]
        assert (len(started), len(refused)) == (1, 1)
        assert started[0]["seq"] == 1 and await store.count_runs(ap["id"]) == 1
        assert (await store.get_autoproject(env.alice, ap["id"]))["next_run_seq"] == 2

        results = await asyncio.gather(
            *(store.set_brief(ap["id"], text, expected_brief_version=0, source="ui")
              for text in ("one", "two")),
            return_exceptions=True,
        )
        assert sorted(type(r).__name__ for r in results) == ["StaleBriefError", "dict"]
        assert [v["version"] for v in await store.list_brief_versions(ap["id"])] == [1]

        for i in range(MAX_ACTIVE_AUTOPROJECTS_PER_USER - 2):
            await env.new_ap(title=f"filler {i}")
        results = await asyncio.gather(
            *(env.new_ap(title=f"race {i}") for i in range(2)), return_exceptions=True,
        )
        assert sorted(type(r).__name__ for r in results) == [
            "AutoProjectCapReachedError", "dict",
        ]
        assert len(await store.list_autoprojects(env.alice)) == MAX_ACTIVE_AUTOPROJECTS_PER_USER

    _run(go())


# ---------------------------------------------------------------------------
# 8. Cascades and account-delete helpers
# ---------------------------------------------------------------------------


def test_delete_autoproject_returns_every_conversation_and_cascades(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        keep = await env.new_ap(title="Keep")
        done = await env.finished(ap["id"])
        await store.set_brief(ap["id"], "b", expected_brief_version=0, source="ui")
        abandoned = await env.conv(env.alice, ap["id"])  # created, never sent
        plain = await env.conv(env.alice)
        kept_run = await env.finished(keep["id"])

        listed = await store.list_autoproject_conversation_ids(ap["id"])
        assert listed == sorted([done["conversation_id"], abandoned])
        assert await store.delete_autoproject(env.alice, ap["id"]) == listed

        assert await store.get_autoproject(env.alice, ap["id"]) is None
        for table in ("autoproject_runs", "autoproject_brief_versions"):
            assert env.sql(
                f"SELECT COUNT(*) FROM {table} WHERE autoproject_id = :a", a=ap["id"],
            ) == [(0,)]
        remaining = {r[0] for r in env.sql("SELECT id FROM conversations")}
        assert remaining == {plain, kept_run["conversation_id"]}
        with pytest.raises(store.AutoProjectNotFoundError):
            await store.delete_autoproject(env.alice, ap["id"])
        assert await store.list_autoproject_conversation_ids(ap["id"]) == []

    _run(go())


def test_conversation_delete_cascades_to_its_run(env):
    store = env.store

    async def go():
        ap = await env.new_ap()
        run = await env.finished(ap["id"])
        with env.db.engine.begin() as conn:
            conn.execute(text("DELETE FROM conversations WHERE id = :c"),
                         {"c": run["conversation_id"]})
        assert await store.get_run(run["id"]) is None
        assert await store.get_autoproject(env.alice, ap["id"]) is not None

    _run(go())


def test_user_delete_cascades_and_account_helpers(env):
    store = env.store

    async def go():
        a = await env.new_ap(title="A")
        b = await env.new_ap(title="B")
        await store.set_autoproject_archived(env.alice, b["id"], True)
        run = await env.finished(a["id"])
        await store.set_brief(a["id"], "b", expected_brief_version=0, source="ui")
        bob_ap = await env.new_ap(user_id=env.bob, title="Bob")
        bob_run = await env.finished(bob_ap["id"], user_id=env.bob)

        assert set(await store.list_autoproject_ids_for_user(env.alice)) == {a["id"], b["id"]}
        assert await store.list_autoproject_ids_for_user(env.bob) == [bob_ap["id"]]

        with env.db.engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": env.alice})
        assert await store.list_autoproject_ids_for_user(env.alice) == []
        assert await store.get_run(run["id"]) is None
        assert env.sql(
            "SELECT COUNT(*) FROM autoproject_brief_versions WHERE autoproject_id = :a",
            a=a["id"],
        ) == [(0,)]
        assert env.sql(
            "SELECT COUNT(*) FROM conversations WHERE id = :c", c=run["conversation_id"],
        ) == [(0,)]
        assert await store.get_run(bob_run["id"]) is not None

    _run(go())
