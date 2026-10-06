"""Quest Docs service layer (chat/docs/service.py) against an isolated
SQLite file, DOCS_DIR and CHATS_DIR, with the ``docs`` feature gate open.

Covers:

1. create / read / edit / append / add_image happy paths: body on disk,
   read sidecar, ``last_write_source``, revision snapshots, realtime events.
2. read-before-edit, title collision, size caps, image validation.
3. The approval gate: ``approval_required`` payloads for a shared private
   doc (owner and write-share recipient), read shares denied,
   ``apply_write_operation(bypass_approval=True)`` and
   ``preview_write_operation``.
4. Taint + invisibility: private conversations never write (or create)
   public docs; public conversations never see private docs (the hidden
   error is byte-equal to a random missing id) and never create user docs
   (always private); a leftover public user doc is private everywhere;
   project docs are hidden from standalone and other-project conversations.
5. Read-only run kinds and Slack.
6. Gate closed: DocDisabled before any DB work.
7. list_docs / search_docs (snippets, title-only hits, truncated scan).

The ``docs_env`` fixture and the helpers below are imported by
tests/test_docs_tools.py (and mirrored by the write_doc action-request
tests).
"""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.docs import constants
from chat.docs.access import (
    APPROVAL_WRITE_NOTE,
    DENY_INFERENCE_API,
    DENY_PUBLIC_DOC_FROM_PRIVATE,
    DENY_READ_ONLY_SHARE,
    DENY_SCRIPT,
    DENY_SLACK_NEEDS_APPROVAL,
    DENY_SUB_AGENT,
    DENY_USER_SUBAGENT,
)
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from chat.docs.service import (
    APPROVAL_REQUIRED_MESSAGE,
    Caller,
    DocApprovalRequired,
    DocDisabled,
    DocError,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32

READ_ONLY_DENIALS = {
    "sub_agent": DENY_SUB_AGENT,
    "inference_api": DENY_INFERENCE_API,
    "user_subagent": DENY_USER_SUBAGENT,
    "script": DENY_SCRIPT,
}


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def docs_env(tmp_path, monkeypatch):
    """Isolated DB + data dirs, docs gate open for everyone, events captured.

    Users: alice (owner of everything), bob, carol. Projects (all alice's):
    ``private_project``, ``other_project`` (private) and ``public_project``.
    """
    import config.feature_gates as fg

    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
    fg.set_feature_enabled(fg.FEATURE_DOCS, True)

    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.conversation_store as conversation_store
    import db.doc_store as doc_store
    import db.models as models
    import db.project_store as project_store
    import db.user_store as user_store
    import db.action_request_store as action_request_store
    import chat.storage as storage_mod

    models.Base.metadata.create_all(sync_engine)
    # user_store / action_request_store: the UI rows resolve owner / share
    # recipient / last writer (incl. a recipient's approved card) through
    # get_users_by_ids / get_action_request_owners (never the real data dir).
    for mod in (doc_store, project_store, conversation_store, user_store,
                action_request_store):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    dirs = {name: tmp_path / name for name in ("chats", "projects", "docs")}
    for path in dirs.values():
        path.mkdir()
    monkeypatch.setattr(storage_mod, "CHATS_DIR", dirs["chats"])
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", dirs["projects"])
    monkeypatch.setattr(storage_mod, "DOCS_DIR", dirs["docs"])

    published: list[tuple[int, dict]] = []
    from chat.realtime import bus
    monkeypatch.setattr(
        bus, "publish_to_user", lambda user_id, ev: published.append((user_id, ev)),
    )
    # Nobody is connected unless a test says so (an "everyone" share fans
    # out to bus.connected_user_ids(); tests/test_docs_sharing.py overrides).
    monkeypatch.setattr(bus, "connected_user_ids", lambda: [])

    async def _seed():
        async with session_local() as db:
            users = [
                models.User(email=f"{name}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for name in ("alice", "bob", "carol")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            return {u.email.split("@")[0]: {"id": u.id, "email": u.email} for u in users}

    users = _run(_seed())
    alice = users["alice"]["id"]
    private_project = _run(project_store.create_project(alice, "Research"))["id"]
    other_project = _run(project_store.create_project(alice, "Other"))["id"]
    public_project = _run(project_store.create_project(alice, "Open", public=True))["id"]

    yield SimpleNamespace(
        tmp=tmp_path,
        dirs=dirs,
        users=users,
        private_project=private_project,
        other_project=other_project,
        public_project=public_project,
        published=published,
        doc_store=doc_store,
        fg=fg,
    )
    _run(async_engine.dispose())
    sync_engine.dispose()


def make_caller(env, who="alice", *, project=None, public=False,
                run_kind="top_level", cid="new"):
    """A caller with a fresh conversation directory (``cid="new"``)."""
    if cid == "new":
        cid = str(uuid.uuid4())
    if cid is not None:
        (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    if run_kind == "script":
        cid, project, public = None, None, False
    return Caller(
        user=env.users[who], conversation_id=cid, project_id=project,
        is_public=public, run_kind=run_kind,
    )


def seed_doc(env, title="Notes", content="line one\nline two\n", *, owner="alice",
             mode="private", project_id=None, shares=()):
    """Create a doc row + directory directly (bypassing the service)."""
    from chat.docs import files

    doc_id = str(uuid.uuid4())
    size = files.init_doc(doc_id, content)
    doc = _run(env.doc_store.create_doc(
        env.users[owner]["id"], title, mode=mode, project_id=project_id,
        content_size=size, doc_id=doc_id,
    ))
    for who, permission in shares:
        user_id = None if who is None else env.users[who]["id"]
        _run(env.doc_store.add_share(doc_id, user_id, permission))
    return doc


def make_legacy_public(env, doc_id):
    """Set a USER doc's stored mode to ``public`` behind the store's back:
    a row from before migration e1b7c4d9a2f6 (the store refuses to create
    a public user doc, and nothing can switch a mode any more)."""
    from db.models import Doc

    async def _flip():
        async with env.doc_store.AsyncSessionLocal() as db:
            row = await db.get(Doc, doc_id)
            assert row.project_id is None
            row.mode = "public"
            await db.commit()

    _run(_flip())
    doc = _run(env.doc_store.get_doc(doc_id))
    assert doc["mode"] == "public"
    return doc


def body(doc_id):
    from chat.docs import files
    return files.read_body(doc_id)


def workspace_dir(env, caller):
    # Mirrors tool_handlers._get_workspace_dir(): get_workspace_path() + "workspace".
    if caller.project_id:
        path = env.dirs["projects"] / caller.project_id / "workspace" / "workspace"
    else:
        path = env.dirs["chats"] / caller.conversation_id / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def event_types(env):
    return [(uid, ev["type"]) for uid, ev in env.published]


def svc():
    from chat.docs import service
    return service


# ---------------------------------------------------------------------------
# create_doc
# ---------------------------------------------------------------------------


class TestCreate:
    def test_happy_path_user_doc(self, docs_env):
        caller = make_caller(docs_env)
        result = _run(svc().create_doc(
            caller, "Plan", "# Plan\r\nfirst\n", description="why",
        ))
        assert set(result) == {
            "id", "title", "mode", "scope", "project_id", "content_size", "updated_at",
        }
        assert result["mode"] == "private"
        assert result["scope"] == "user"
        assert result["project_id"] is None
        assert body(result["id"]) == "# Plan\nfirst\n"
        assert result["content_size"] == len("# Plan\nfirst\n")

        row = _run(docs_env.doc_store.get_doc(result["id"]))
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"
        assert row["description"] == "why"
        from chat.storage import ChatStorage
        assert ChatStorage.get_doc_read_ids(caller.conversation_id) == [result["id"]]
        alice = docs_env.users["alice"]["id"]
        assert event_types(docs_env) == [
            (alice, "doc_list_changed"), (alice, "doc_changed"),
        ]
        assert docs_env.published[1][1]["doc_id"] == result["id"]

    def test_public_conversation_creates_public_project_docs_only(self, docs_env):
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        project_doc = _run(svc().create_doc(caller, "Open proj", "x", target="project"))
        assert project_doc["mode"] == "public"
        assert project_doc["scope"] == "project"
        assert project_doc["project_id"] == docs_env.public_project

    @pytest.mark.parametrize("target", ["user", None, ""])
    def test_public_conversation_cannot_create_user_docs(self, docs_env, target):
        # User docs are always private, and a public conversation never
        # creates a private doc: target="user" (the default) is refused.
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        kwargs = {} if target is None else {"target": target}
        with pytest.raises(DocError) as exc:
            _run(svc().create_doc(caller, "Open notes", "x", **kwargs))
        assert str(exc.value) == (
            "User docs are always private and cannot be created from a public "
            'conversation; use create_doc(target="project") to create a doc in '
            "this project."
        )
        assert str(exc.value) == constants.user_doc_in_public_conversation_message()
        assert list(docs_env.dirs["docs"].iterdir()) == []
        assert docs_env.published == []

    def test_private_conversation_user_docs_are_private(self, docs_env):
        for caller in (make_caller(docs_env),
                       make_caller(docs_env, project=docs_env.private_project)):
            doc = _run(svc().create_doc(caller, f"Mine {caller.project_id}", "x"))
            assert (doc["mode"], doc["scope"]) == ("private", "user")

    def test_private_conversation_creates_private_docs_only(self, docs_env):
        caller = make_caller(docs_env, project=docs_env.private_project)
        doc = _run(svc().create_doc(caller, "Proj doc", "x", target="project"))
        assert doc["mode"] == "private"
        assert doc["scope"] == "project"

    def test_taint_private_caller_in_public_project_cannot_create(self, docs_env):
        # Defensive: a private conversation can never mint a public doc,
        # even if handed a public project's id.
        caller = make_caller(docs_env, project=docs_env.public_project, public=False)
        with pytest.raises(DocError, match="does not match"):
            _run(svc().create_doc(caller, "Leak", "secret", target="project"))
        assert list(docs_env.dirs["docs"].iterdir()) == []

    def test_project_target_requires_project_conversation(self, docs_env):
        caller = make_caller(docs_env)
        with pytest.raises(DocError) as exc:
            _run(svc().create_doc(caller, "X", "y", target="project"))
        assert str(exc.value) == (
            'create_doc(target="project") is only available inside a project conversation'
        )

    def test_project_must_be_owned(self, docs_env):
        caller = make_caller(docs_env, who="bob", project=docs_env.private_project)
        with pytest.raises(DocError, match="Project not found"):
            _run(svc().create_doc(caller, "X", "y", target="project"))

    def test_title_collision_case_insensitive_cleans_up(self, docs_env):
        caller = make_caller(docs_env)
        _run(svc().create_doc(caller, "Plan", "a"))
        with pytest.raises(DocError) as exc:
            _run(svc().create_doc(caller, "  plan ", "b"))
        assert str(exc.value).startswith("A doc titled 'plan' already exists")
        # Only the first doc's directory remains.
        assert len(list(docs_env.dirs["docs"].iterdir())) == 1

    def test_content_size_cap(self, docs_env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 10)
        caller = make_caller(docs_env)
        with pytest.raises(DocError, match="over the 10-byte limit"):
            _run(svc().create_doc(caller, "Big", "x" * 11))
        assert list(docs_env.dirs["docs"].iterdir()) == []

    def test_bad_title_and_target(self, docs_env):
        caller = make_caller(docs_env)
        with pytest.raises(DocError, match="title cannot be empty"):
            _run(svc().create_doc(caller, "  ", "x"))
        with pytest.raises(DocError, match="target"):
            _run(svc().create_doc(caller, "T", "x", target="team"))
        with pytest.raises(DocError, match="maximum length"):
            _run(svc().create_doc(caller, "t" * 201, "x"))
        assert list(docs_env.dirs["docs"].iterdir()) == []

    @pytest.mark.parametrize("run_kind", sorted(READ_ONLY_DENIALS))
    def test_read_only_run_kinds_cannot_create(self, docs_env, run_kind):
        caller = make_caller(docs_env, run_kind=run_kind)
        with pytest.raises(DocError) as exc:
            _run(svc().create_doc(caller, "T", "x"))
        assert str(exc.value) == READ_ONLY_DENIALS[run_kind]

    def test_slack_can_create(self, docs_env):
        caller = make_caller(docs_env, run_kind="slack")
        assert _run(svc().create_doc(caller, "From Slack", "x"))["mode"] == "private"


# ---------------------------------------------------------------------------
# read_doc
# ---------------------------------------------------------------------------


class TestRead:
    def test_full_read_records_sidecar(self, docs_env):
        doc = seed_doc(docs_env, content="a\nb\nc\n")
        caller = make_caller(docs_env)
        result = _run(svc().read_doc(caller, doc["id"]))
        assert result["content"] == "a\nb\nc\n"
        assert result["total_lines"] == 3
        assert (result["start_line"], result["end_line"]) == (1, 3)
        assert result["truncated"] is False and "note" not in result
        assert result["shared"] is False and "shares" not in result
        assert result["writable"] == "free" and result["write_note"] is None
        assert result["scope"] == "user" and result["mode"] == "private"
        from chat.storage import ChatStorage
        assert ChatStorage.get_doc_read_ids(caller.conversation_id) == [doc["id"]]

    def test_line_range(self, docs_env):
        doc = seed_doc(docs_env, content="1\n2\n3\n4\n5")
        caller = make_caller(docs_env)
        result = _run(svc().read_doc(caller, doc["id"], start_line=2, end_line=3))
        assert result["content"] == "2\n3\n"
        assert (result["start_line"], result["end_line"]) == (2, 3)
        tail = _run(svc().read_doc(caller, doc["id"], start_line=4, end_line=99))
        assert tail["content"] == "4\n5" and tail["end_line"] == 5
        with pytest.raises(DocError, match="past the end"):
            _run(svc().read_doc(caller, doc["id"], start_line=6))
        with pytest.raises(DocError, match="greater than or equal"):
            _run(svc().read_doc(caller, doc["id"], start_line=3, end_line=2))
        with pytest.raises(DocError, match="positive integer"):
            _run(svc().read_doc(caller, doc["id"], start_line=0))

    def test_truncated_read_pages_at_line_boundary(self, docs_env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_READ_MAX_CHARS", 10)
        doc = seed_doc(docs_env, content="aaaa\nbbbb\ncccc\n")
        caller = make_caller(docs_env)
        result = _run(svc().read_doc(caller, doc["id"]))
        assert result["content"] == "aaaa\nbbbb\n"
        assert result["truncated"] is True
        assert result["end_line"] == 2
        assert result["note"].startswith(
            "Content truncated at 10 characters; page with start_line/end_line"
        )
        rest = _run(svc().read_doc(caller, doc["id"], start_line=3))
        assert rest["content"] == "cccc\n" and rest["truncated"] is False

    def test_empty_doc(self, docs_env):
        doc = seed_doc(docs_env, content="")
        result = _run(svc().read_doc(make_caller(docs_env), doc["id"]))
        assert result["content"] == "" and result["total_lines"] == 0
        assert (result["start_line"], result["end_line"]) == (0, 0)

    def test_missing_doc(self, docs_env):
        missing = str(uuid.uuid4())
        with pytest.raises(DocError) as exc:
            _run(svc().read_doc(make_caller(docs_env), missing))
        assert str(exc.value) == doc_not_found_message(missing)


# ---------------------------------------------------------------------------
# edit_doc
# ---------------------------------------------------------------------------


class TestEdit:
    def test_requires_read(self, docs_env):
        doc = seed_doc(docs_env, title="Log")
        caller = make_caller(docs_env)
        with pytest.raises(DocError) as exc:
            _run(svc().edit_doc(caller, doc["id"], "one", "1", write_source="w"))
        assert str(exc.value) == (
            "Doc 'Log' has not been read in this conversation. "
            "Read it with read_doc before editing."
        )
        assert body(doc["id"]) == "line one\nline two\n"

    def test_happy_path(self, docs_env):
        from chat.docs import files

        doc = seed_doc(docs_env)
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        docs_env.published.clear()
        result = _run(svc().edit_doc(
            caller, doc["id"], "one", "ONE", write_source=f"conversation:{caller.conversation_id}",
        ))
        assert result["replaced"] == 1 and result["total_lines"] == 2
        assert body(doc["id"]) == "line ONE\nline two\n"
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"
        assert row["content_size"] == len("line ONE\nline two\n")
        assert row["updated_at"] == result["updated_at"]
        assert row["updated_at"] >= doc["updated_at"]
        # The previous body was snapshotted.
        revisions = files.list_revisions(doc["id"])
        assert len(revisions) == 1
        assert revisions[0].read_text() == "line one\nline two\n"
        alice = docs_env.users["alice"]["id"]
        assert event_types(docs_env) == [(alice, "doc_changed"), (alice, "doc_list_changed")]

    def test_doc_noun_errors(self, docs_env):
        doc = seed_doc(docs_env, content="dup dup\n")
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        with pytest.raises(DocError) as exc:
            _run(svc().edit_doc(caller, doc["id"], "zzz", "y"))
        assert str(exc.value) == (
            "old_string not found in the doc content. The doc may have changed "
            "-- re-read it with read_doc and retry with the exact current text."
        )
        with pytest.raises(DocError, match="appears 2 times in the doc content"):
            _run(svc().edit_doc(caller, doc["id"], "dup", "x"))
        with pytest.raises(DocError, match="identical"):
            _run(svc().edit_doc(caller, doc["id"], "dup", "dup"))
        with pytest.raises(DocError, match="must not be empty"):
            _run(svc().edit_doc(caller, doc["id"], "", "x"))
        with pytest.raises(DocError, match="Docs must have non-empty content"):
            _run(svc().edit_doc(caller, doc["id"], "dup dup\n", "", replace_all=False))
        result = _run(svc().edit_doc(caller, doc["id"], "dup", "x", replace_all=True))
        assert result["replaced"] == 2
        assert body(doc["id"]) == "x x\n"

    def test_edit_size_cap(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, content="small\n")
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 10)
        with pytest.raises(DocError, match="maximum size of 10 bytes"):
            _run(svc().edit_doc(caller, doc["id"], "small", "much bigger"))
        assert body(doc["id"]) == "small\n"

    def test_created_doc_counts_as_read(self, docs_env):
        caller = make_caller(docs_env)
        doc = _run(svc().create_doc(caller, "Fresh", "hello world\n"))
        _run(svc().edit_doc(caller, doc["id"], "world", "there"))
        assert body(doc["id"]) == "hello there\n"

    def test_default_write_source_is_the_conversation(self, docs_env):
        doc = seed_doc(docs_env)
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        _run(svc().edit_doc(caller, doc["id"], "one", "1"))
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"


# ---------------------------------------------------------------------------
# append_to_doc
# ---------------------------------------------------------------------------


class TestAppend:
    @pytest.mark.parametrize("existing, ensure, expected", [
        ("", True, "new\n"),
        ("a", True, "a\n\nnew\n"),
        ("a\n", True, "a\n\nnew\n"),
        ("a\n\n", True, "a\n\nnew\n"),
        ("a", False, "a\nnew\n"),
        ("a\n", False, "a\nnew\n"),
    ])
    def test_separator(self, docs_env, existing, ensure, expected):
        doc = seed_doc(docs_env, content=existing)
        result = _run(svc().append_to_doc(
            make_caller(docs_env), doc["id"], "new", ensure_blank_line=ensure,
            write_source="x",
        ))
        assert body(doc["id"]) == expected
        assert result["appended_lines"] == 1
        assert result["total_lines"] == expected.count("\n")

    def test_no_read_needed_and_metadata(self, docs_env):
        doc = seed_doc(docs_env, content="# Log\n")
        caller = make_caller(docs_env)
        docs_env.published.clear()
        _run(svc().append_to_doc(caller, doc["id"], "## 2026-10-05\r\nentry", write_source="routine-x"))
        assert body(doc["id"]) == "# Log\n\n## 2026-10-05\nentry\n"
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == "routine-x"
        assert row["content_size"] == len(body(doc["id"]).encode())
        assert {t for _u, t in event_types(docs_env)} == {"doc_changed", "doc_list_changed"}

    def test_size_cap_and_empty(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, content="12345")
        caller = make_caller(docs_env)
        with pytest.raises(DocError, match="must not be empty"):
            _run(svc().append_to_doc(caller, doc["id"], "  "))
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 8)
        with pytest.raises(DocError, match="over the 8-byte limit"):
            _run(svc().append_to_doc(caller, doc["id"], "6789"))
        assert body(doc["id"]) == "12345"

    def test_concurrent_appends_lose_nothing(self, docs_env):
        doc = seed_doc(docs_env, content="start\n")
        caller = make_caller(docs_env)

        async def many():
            await asyncio.gather(*(
                svc().append_to_doc(caller, doc["id"], f"entry {i}") for i in range(20)
            ))

        _run(many())
        text = body(doc["id"])
        for i in range(20):
            assert f"entry {i}\n" in text
        on_disk = (docs_env.dirs["docs"] / doc["id"] / "doc.md").stat().st_size
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["content_size"] == on_disk


# ---------------------------------------------------------------------------
# add_doc_image
# ---------------------------------------------------------------------------


class TestAddImage:
    def test_append_placement(self, docs_env):
        from chat.docs import files

        doc = seed_doc(docs_env, content="# Report\n")
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "My Chart.png").write_bytes(PNG)
        result = _run(svc().add_doc_image(caller, doc["id"], "My Chart.png", alt="Revenue"))
        assert result["asset"] == "My-Chart.png"
        assert result["markdown"] == "![Revenue](assets/My-Chart.png)"
        assert result["appended"] is True and result["asset_count"] == 1
        assert body(doc["id"]) == "# Report\n\n![Revenue](assets/My-Chart.png)\n"
        assert result["total_lines"] == 3
        assert files.read_asset(doc["id"], "My-Chart.png") == PNG
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["asset_count"] == 1
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"

    def test_none_placement_and_collision(self, docs_env):
        doc = seed_doc(docs_env, content="x\n")
        caller = make_caller(docs_env, project=docs_env.private_project)
        ws = workspace_dir(docs_env, caller)
        (ws / "pic.gif").write_bytes(GIF)
        first = _run(svc().add_doc_image(caller, doc["id"], "pic.gif", placement="none"))
        second = _run(svc().add_doc_image(caller, doc["id"], "pic.gif", placement="none"))
        assert first["asset"] == "pic.gif" and second["asset"] == "pic-2.gif"
        assert second["markdown"] == "![pic-2](assets/pic-2.gif)"
        assert first["appended"] is False and "total_lines" not in first
        assert second["asset_count"] == 2
        assert body(doc["id"]) == "x\n"
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["asset_count"] == 2 and row["content_size"] == 2

    def test_rejects_non_images_and_oversize(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env)
        caller = make_caller(docs_env)
        ws = workspace_dir(docs_env, caller)
        (ws / "fake.png").write_text("<svg></svg>")
        with pytest.raises(DocError, match="Not a supported raster image"):
            _run(svc().add_doc_image(caller, doc["id"], "fake.png"))
        (ws / "big.png").write_bytes(PNG + b"\x00" * 100)
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", 50)
        with pytest.raises(DocError, match="over the 50-byte per-image limit"):
            _run(svc().add_doc_image(caller, doc["id"], "big.png"))
        with pytest.raises(DocError, match="not found"):
            _run(svc().add_doc_image(caller, doc["id"], "nope.png"))
        with pytest.raises(DocError, match="traversal"):
            _run(svc().add_doc_image(caller, doc["id"], "../x.png"))
        assert body(doc["id"]) == "line one\nline two\n"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["asset_count"] == 0

    def test_symlink_escaping_workspace_refused(self, docs_env):
        doc = seed_doc(docs_env)
        caller = make_caller(docs_env)
        outside = docs_env.tmp / "secret.png"
        outside.write_bytes(PNG)
        os.symlink(outside, workspace_dir(docs_env, caller) / "link.png")
        with pytest.raises(DocError, match="outside"):
            _run(svc().add_doc_image(caller, doc["id"], "link.png"))

    def test_bad_placement(self, docs_env):
        doc = seed_doc(docs_env)
        with pytest.raises(DocError, match="placement"):
            _run(svc().add_doc_image(make_caller(docs_env), doc["id"], "a.png", placement="top"))


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


class TestApproval:
    def _shared(self, docs_env, permission="read"):
        return seed_doc(docs_env, content="alpha beta\n", shares=[("bob", permission)])

    def test_owner_gets_approval_required_payloads(self, docs_env):
        doc = self._shared(docs_env)
        caller = make_caller(docs_env)
        ws = workspace_dir(docs_env, caller)
        (ws / "c.png").write_bytes(PNG)
        _run(svc().read_doc(caller, doc["id"]))

        with pytest.raises(DocApprovalRequired) as exc:
            _run(svc().edit_doc(caller, doc["id"], "alpha", "ALPHA"))
        assert str(exc.value) == APPROVAL_REQUIRED_MESSAGE
        assert exc.value.suggested_request == {
            "request_type": "write_doc",
            "params": {
                "operation": "edit", "doc_id": doc["id"],
                "old_string": "alpha", "new_string": "ALPHA", "replace_all": False,
            },
        }
        with pytest.raises(DocApprovalRequired) as exc:
            _run(svc().append_to_doc(caller, doc["id"], "more"))
        assert exc.value.suggested_request["params"] == {
            "operation": "append", "doc_id": doc["id"],
            "content": "more", "ensure_blank_line": True,
        }
        with pytest.raises(DocApprovalRequired) as exc:
            _run(svc().add_doc_image(caller, doc["id"], "c.png"))
        assert exc.value.suggested_request["params"] == {
            "operation": "add_image", "doc_id": doc["id"],
            "workspace_path": "c.png", "alt": "", "placement": "append",
        }
        # Nothing was written, no asset stored.
        assert body(doc["id"]) == "alpha beta\n"
        assert list((docs_env.dirs["docs"] / doc["id"] / "assets").iterdir()) == []

    def test_approval_path_dry_runs_first(self, docs_env):
        doc = self._shared(docs_env)
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        # A stale old_string fails now, not after a card is proposed.
        with pytest.raises(DocError) as exc:
            _run(svc().edit_doc(caller, doc["id"], "gamma", "x"))
        assert not isinstance(exc.value, DocApprovalRequired)

    def test_unread_edit_fails_before_approval(self, docs_env):
        doc = self._shared(docs_env)
        with pytest.raises(DocError, match="has not been read") as exc:
            _run(svc().edit_doc(make_caller(docs_env), doc["id"], "alpha", "x"))
        assert not isinstance(exc.value, DocApprovalRequired)

    def test_write_share_recipient_needs_approval(self, docs_env):
        doc = self._shared(docs_env, permission="write")
        bob = make_caller(docs_env, who="bob")
        with pytest.raises(DocApprovalRequired):
            _run(svc().append_to_doc(bob, doc["id"], "from bob"))
        listed = _run(svc().list_docs(bob))
        assert [(d["id"], d["writable"], d["write_note"]) for d in listed] == [
            (doc["id"], "approval", APPROVAL_WRITE_NOTE),
        ]

    def test_read_share_recipient_denied(self, docs_env):
        doc = self._shared(docs_env, permission="read")
        bob = make_caller(docs_env, who="bob")
        assert _run(svc().read_doc(bob, doc["id"]))["content"] == "alpha beta\n"
        with pytest.raises(DocError) as exc:
            _run(svc().append_to_doc(bob, doc["id"], "x"))
        assert str(exc.value) == DENY_READ_ONLY_SHARE
        assert not isinstance(exc.value, DocApprovalRequired)

    def test_bypass_approval_applies(self, docs_env):
        doc = self._shared(docs_env)
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        result = _run(svc().apply_write_operation(
            caller, doc["id"], "edit",
            {"old_string": "alpha", "new_string": "ALPHA", "replace_all": False},
            write_source="action_request:r1", bypass_approval=True,
        ))
        assert result["replaced"] == 1
        assert body(doc["id"]) == "ALPHA beta\n"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["last_write_source"] == "action_request:r1"

    def test_bypass_still_refuses_denied_hidden_and_unread(self, docs_env):
        doc = self._shared(docs_env)
        caller = make_caller(docs_env)
        # unread
        with pytest.raises(DocError, match="has not been read"):
            _run(svc().apply_write_operation(
                caller, doc["id"], "edit", {"old_string": "alpha", "new_string": "x"},
                write_source="a", bypass_approval=True,
            ))
        # denied: bob's write share was downgraded to read meanwhile
        _run(docs_env.doc_store.add_share(doc["id"], docs_env.users["bob"]["id"], "write"))
        bob = make_caller(docs_env, who="bob")
        with pytest.raises(DocApprovalRequired):
            _run(svc().append_to_doc(bob, doc["id"], "x"))
        _run(docs_env.doc_store.add_share(doc["id"], docs_env.users["bob"]["id"], "read"))
        with pytest.raises(DocError) as exc:
            _run(svc().apply_write_operation(
                bob, doc["id"], "append", {"content": "x"},
                write_source="a", bypass_approval=True,
            ))
        assert str(exc.value) == DENY_READ_ONLY_SHARE
        # hidden: carol has no relationship
        carol = make_caller(docs_env, who="carol")
        with pytest.raises(DocError) as exc:
            _run(svc().apply_write_operation(
                carol, doc["id"], "append", {"content": "x"},
                write_source="a", bypass_approval=True,
            ))
        assert str(exc.value) == doc_not_found_message(doc["id"])
        assert body(doc["id"]) == "alpha beta\n"

    def test_unknown_params_ignored_by_apply(self, docs_env):
        doc = self._shared(docs_env)
        result = _run(svc().apply_write_operation(
            make_caller(docs_env), doc["id"], "append",
            {"content": "tail", "content_diff": {"lines": []}, "current_title": "x"},
            write_source="a", bypass_approval=True,
        ))
        assert result["appended_lines"] == 1

    def test_bad_operation(self, docs_env):
        doc = self._shared(docs_env)
        with pytest.raises(DocError, match="operation must be one of"):
            _run(svc().apply_write_operation(
                make_caller(docs_env), doc["id"], "replace", {}, write_source="a",
            ))


class TestPreview:
    def test_edit_preview_does_not_write(self, docs_env):
        doc = seed_doc(docs_env, content="alpha beta\n", shares=[("bob", "read")])
        caller = make_caller(docs_env)
        _run(svc().read_doc(caller, doc["id"]))
        preview = _run(svc().preview_write_operation(
            caller, doc["id"], "edit", {"old_string": "beta", "new_string": "BETA"},
        ))
        assert preview["access"].write == "approval"
        assert preview["doc"]["id"] == doc["id"]
        assert preview["current_body"] == "alpha beta\n"
        assert preview["new_body"] == "alpha BETA\n"
        assert preview["replacements"] == 1
        assert body(doc["id"]) == "alpha beta\n"

    def test_free_verdict_does_not_raise(self, docs_env):
        doc = seed_doc(docs_env, content="a\n")
        preview = _run(svc().preview_write_operation(
            make_caller(docs_env), doc["id"], "append", {"content": "b"},
        ))
        assert preview["access"].write == "free"
        assert preview["new_body"] == "a\n\nb\n" and preview["replacements"] == 0

    def test_add_image_preview(self, docs_env):
        doc = seed_doc(docs_env, content="a\n", shares=[(None, "read")])
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "Q3 chart.png").write_bytes(PNG)
        preview = _run(svc().preview_write_operation(
            caller, doc["id"], "add_image", {"workspace_path": "Q3 chart.png"},
        ))
        assert preview["asset_name_preview"] == "Q3-chart.png"
        assert preview["image_bytes_size"] == len(PNG)
        assert preview["markdown"] == "![Q3-chart](assets/Q3-chart.png)"
        assert preview["new_body"] == "a\n\n![Q3-chart](assets/Q3-chart.png)\n"
        none = _run(svc().preview_write_operation(
            caller, doc["id"], "add_image",
            {"workspace_path": "Q3 chart.png", "placement": "none"},
        ))
        assert none["new_body"] == none["current_body"] == "a\n"
        assert list((docs_env.dirs["docs"] / doc["id"] / "assets").iterdir()) == []

    def test_preview_errors_match_apply(self, docs_env):
        doc = seed_doc(docs_env, mode="public", project_id=docs_env.public_project)
        # Defensive shape: a private caller inside the public project.
        caller = make_caller(docs_env, project=docs_env.public_project)
        with pytest.raises(DocError) as exc:
            _run(svc().preview_write_operation(
                caller, doc["id"], "append", {"content": "x"},
            ))
        assert str(exc.value) == DENY_PUBLIC_DOC_FROM_PRIVATE


# ---------------------------------------------------------------------------
# Taint + invisibility
# ---------------------------------------------------------------------------


class TestTaintAndVisibility:
    def test_private_caller_never_writes_public_doc(self, docs_env):
        doc = seed_doc(
            docs_env, mode="public", content="pub\n", project_id=docs_env.public_project,
        )
        # Public docs are public-project docs; a private caller can only
        # reach one with the (defensive) private-caller-in-that-project shape.
        caller = make_caller(docs_env, project=docs_env.public_project)
        (workspace_dir(docs_env, caller) / "i.png").write_bytes(PNG)
        assert _run(svc().read_doc(caller, doc["id"]))["write_note"] == DENY_PUBLIC_DOC_FROM_PRIVATE
        for call in (
            svc().edit_doc(caller, doc["id"], "pub", "leak"),
            svc().append_to_doc(caller, doc["id"], "leak"),
            svc().add_doc_image(caller, doc["id"], "i.png"),
            svc().add_doc_image(caller, doc["id"], "i.png", placement="none"),
        ):
            with pytest.raises(DocError) as exc:
                _run(call)
            assert str(exc.value) == DENY_PUBLIC_DOC_FROM_PRIVATE
        assert body(doc["id"]) == "pub\n"
        assert list((docs_env.dirs["docs"] / doc["id"] / "assets").iterdir()) == []

    def test_public_caller_cannot_see_private_doc(self, docs_env):
        private = seed_doc(docs_env, title="Secret")
        public_doc = seed_doc(
            docs_env, title="Open", mode="public", content="open secret\n",
            project_id=docs_env.public_project,
        )
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        missing = str(uuid.uuid4())
        for fn in (
            lambda d: svc().read_doc(caller, d),
            lambda d: svc().append_to_doc(caller, d, "x"),
            lambda d: svc().edit_doc(caller, d, "a", "b"),
            lambda d: svc().get_visible_doc(caller, d),
        ):
            with pytest.raises(DocError) as hidden:
                _run(fn(private["id"]))
            with pytest.raises(DocError) as absent:
                _run(fn(missing))
            assert str(hidden.value) == doc_not_found_message(private["id"])
            assert str(absent.value) == doc_not_found_message(missing)
            assert str(hidden.value).replace(private["id"], "<id>") == \
                str(absent.value).replace(missing, "<id>")
        assert [d["id"] for d in _run(svc().list_docs(caller))] == [public_doc["id"]]
        found = _run(svc().search_docs(caller, "secret"))
        assert [r["id"] for r in found["results"]] == [public_doc["id"]]
        # The public conversation writes the public doc freely.
        _run(svc().append_to_doc(caller, public_doc["id"], "more"))
        assert body(public_doc["id"]).endswith("more\n")

    def test_legacy_public_user_doc_never_reaches_a_public_conversation(self, docs_env):
        """A user doc whose stored mode still says public (pre-migration)
        is a private doc: list/search/read from a public conversation never
        surface it, for the owner or a write-share recipient, whatever the
        scope."""
        legacy = make_legacy_public(docs_env, seed_doc(
            docs_env, title="Legacy open", content="legacy needle\n",
            shares=[("bob", "write"), (None, "read")],
        )["id"])
        project_doc = seed_doc(
            docs_env, title="Project open", content="project needle\n",
            mode="public", project_id=docs_env.public_project,
        )
        for who in ("alice", "bob", "carol"):
            caller = make_caller(
                docs_env, who=who, project=docs_env.public_project, public=True,
            )
            visible = [project_doc["id"]] if who == "alice" else []
            for scope in ("all", "user", "project"):
                listed = [d["id"] for d in _run(svc().list_docs(caller, scope=scope))]
                assert listed == (visible if scope != "user" else []), (who, scope)
                found = _run(svc().search_docs(caller, "needle", scope=scope))
                assert [r["id"] for r in found["results"]] == (
                    visible if scope != "user" else []
                ), (who, scope)
                assert legacy["id"] not in listed
            for fn in (
                lambda d: svc().read_doc(caller, d),
                lambda d: svc().append_to_doc(caller, d, "x"),
            ):
                with pytest.raises(DocError) as exc:
                    _run(fn(legacy["id"]))
                assert str(exc.value) == doc_not_found_message(legacy["id"])
        assert body(legacy["id"]) == "legacy needle\n"

    def test_legacy_public_user_doc_is_private_in_private_conversations(self, docs_env):
        unshared = make_legacy_public(
            docs_env, seed_doc(docs_env, title="Mine", content="a\n")["id"],
        )
        shared = make_legacy_public(docs_env, seed_doc(
            docs_env, title="Ours", content="a\n", shares=[("bob", "read")],
        )["id"])
        alice = make_caller(docs_env)
        # Owner, unshared: free (not "read-only, public doc").
        assert _run(svc().read_doc(alice, unshared["id"]))["writable"] == "free"
        _run(svc().append_to_doc(alice, unshared["id"], "b"))
        assert body(unshared["id"]) == "a\n\nb\n"
        # Owner, shared: approval; read recipient: read-only share.
        with pytest.raises(DocApprovalRequired):
            _run(svc().append_to_doc(alice, shared["id"], "b"))
        bob = make_caller(docs_env, who="bob")
        with pytest.raises(DocError) as exc:
            _run(svc().append_to_doc(bob, shared["id"], "b"))
        assert str(exc.value) == DENY_READ_ONLY_SHARE

    def test_project_docs_hidden_outside_their_project(self, docs_env):
        doc = seed_doc(docs_env, title="Proj", project_id=docs_env.private_project)
        standalone = make_caller(docs_env)
        other = make_caller(docs_env, project=docs_env.other_project)
        inside = make_caller(docs_env, project=docs_env.private_project)
        for caller in (standalone, other):
            with pytest.raises(DocError) as exc:
                _run(svc().read_doc(caller, doc["id"]))
            assert str(exc.value) == doc_not_found_message(doc["id"])
            assert _run(svc().list_docs(caller)) == []
        assert [d["id"] for d in _run(svc().list_docs(inside))] == [doc["id"]]
        assert _run(svc().list_docs(inside, scope="user")) == []
        assert _run(svc().read_doc(inside, doc["id"]))["scope"] == "project"


# ---------------------------------------------------------------------------
# Run kinds
# ---------------------------------------------------------------------------


class TestRunKinds:
    @pytest.mark.parametrize("run_kind", sorted(READ_ONLY_DENIALS))
    def test_read_only_run_kinds(self, docs_env, run_kind):
        doc = seed_doc(docs_env, content="text\n")
        caller = make_caller(docs_env, run_kind=run_kind)
        result = _run(svc().read_doc(caller, doc["id"]))
        assert result["content"] == "text\n"
        assert result["writable"] == "denied"
        assert result["write_note"] == READ_ONLY_DENIALS[run_kind]
        for call in (
            svc().edit_doc(caller, doc["id"], "text", "x"),
            svc().append_to_doc(caller, doc["id"], "x"),
            svc().add_doc_image(caller, doc["id"], "a.png"),
        ):
            with pytest.raises(DocError) as exc:
                _run(call)
            assert str(exc.value) == READ_ONLY_DENIALS[run_kind]
        assert body(doc["id"]) == "text\n"

    def test_script_never_sees_project_docs(self, docs_env):
        seed_doc(docs_env, title="Proj", project_id=docs_env.private_project)
        user_doc = seed_doc(docs_env, title="Mine")
        caller = make_caller(docs_env, run_kind="script")
        assert [d["id"] for d in _run(svc().list_docs(caller))] == [user_doc["id"]]

    def test_slack_owner_unshared_free(self, docs_env):
        doc = seed_doc(docs_env, content="s\n")
        caller = make_caller(docs_env, run_kind="slack")
        _run(svc().append_to_doc(caller, doc["id"], "t"))
        assert body(doc["id"]) == "s\n\nt\n"

    def test_slack_shared_denied(self, docs_env):
        doc = seed_doc(docs_env, content="s\n", shares=[("bob", "read")])
        caller = make_caller(docs_env, run_kind="slack")
        with pytest.raises(DocError) as exc:
            _run(svc().append_to_doc(caller, doc["id"], "t"))
        assert str(exc.value) == DENY_SLACK_NEEDS_APPROVAL
        assert not isinstance(exc.value, DocApprovalRequired)


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_closed_gate_refuses_before_db(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env)
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_DOCS, False)

        async def _boom(*_a, **_k):
            raise AssertionError("DB touched while the gate is closed")

        for name in ("get_doc", "list_accessible_docs", "create_doc", "update_after_write"):
            monkeypatch.setattr(docs_env.doc_store, name, _boom)
        caller = make_caller(docs_env)
        calls = [
            svc().list_docs(caller),
            svc().search_docs(caller, "x"),
            svc().read_doc(caller, doc["id"]),
            svc().get_visible_doc(caller, doc["id"]),
            svc().create_doc(caller, "T", "x"),
            svc().edit_doc(caller, doc["id"], "a", "b"),
            svc().append_to_doc(caller, doc["id"], "x"),
            svc().add_doc_image(caller, doc["id"], "a.png"),
            svc().apply_write_operation(
                caller, doc["id"], "append", {"content": "x"},
                write_source="a", bypass_approval=True,
            ),
            svc().preview_write_operation(caller, doc["id"], "append", {"content": "x"}),
        ]
        for call in calls:
            with pytest.raises(DocDisabled) as exc:
                _run(call)
            assert str(exc.value) == docs_disabled_message()

    def test_per_user_allow_list(self, docs_env):
        fg = docs_env.fg
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, ["Alice@example.com"])
        assert _run(svc().list_docs(make_caller(docs_env))) == []
        with pytest.raises(DocDisabled):
            _run(svc().list_docs(make_caller(docs_env, who="bob")))


# ---------------------------------------------------------------------------
# list_docs / search_docs
# ---------------------------------------------------------------------------


class TestListAndSearch:
    def test_list_rows_order_and_limit(self, docs_env):
        first = seed_doc(docs_env, title="First")
        second = seed_doc(docs_env, title="Second", shares=[("bob", "read")])
        caller = make_caller(docs_env)
        rows = _run(svc().list_docs(caller))
        assert [r["id"] for r in rows] == [second["id"], first["id"]]
        assert set(rows[0]) == {
            "id", "title", "description", "mode", "scope", "project_id",
            "content_size", "asset_count", "updated_at", "shared", "writable",
            "write_note",
        }
        # The share roster itself is never exposed; only a flag.
        assert rows[0]["shared"] is True and rows[1]["shared"] is False
        assert rows[0]["writable"] == "approval" and rows[1]["writable"] == "free"
        assert [r["id"] for r in _run(svc().list_docs(caller, limit=1))] == [second["id"]]
        with pytest.raises(DocError, match="scope"):
            _run(svc().list_docs(caller, scope="team"))

    def test_hidden_rows_do_not_eat_the_limit(self, docs_env):
        visible = seed_doc(
            docs_env, title="Public one", mode="public", project_id=docs_env.public_project,
        )
        for i in range(3):
            seed_doc(docs_env, title=f"Private {i}")  # newer, hidden from public
        legacy = seed_doc(docs_env, title="Legacy")  # newest, hidden too
        make_legacy_public(docs_env, legacy["id"])
        caller = make_caller(docs_env, project=docs_env.public_project, public=True)
        assert [r["id"] for r in _run(svc().list_docs(caller, limit=1))] == [visible["id"]]

    def test_search_snippets_and_title_hits(self, docs_env):
        body_doc = seed_doc(
            docs_env, title="Weekly",
            content="intro\nthe Quarterly numbers\nmore\nQUARTERLY again\n",
        )
        title_doc = seed_doc(docs_env, title="Quarterly plan", content="nothing here\n")
        seed_doc(docs_env, title="Unrelated", content="zzz\n")
        caller = make_caller(docs_env)
        result = _run(svc().search_docs(caller, "quarterly"))
        assert result["truncated"] is False
        by_id = {r["id"]: r for r in result["results"]}
        assert set(by_id) == {body_doc["id"], title_doc["id"]}
        assert by_id[title_doc["id"]]["matches"] == []
        matches = by_id[body_doc["id"]]["matches"]
        assert [m["line"] for m in matches] == [2]  # one window covers both hits
        assert "Quarterly numbers" in matches[0]["snippet"]
        # Search is not a read.
        from chat.storage import ChatStorage
        assert ChatStorage.get_doc_read_ids(caller.conversation_id) == []

    def test_search_snippet_window_and_cap(self, docs_env, monkeypatch):
        monkeypatch.setattr(constants, "DOC_SEARCH_SNIPPET_CHARS", 20)
        monkeypatch.setattr(constants, "DOC_SEARCH_SNIPPETS_PER_DOC", 2)
        text = "".join(f"line {i} needle {'x' * 40}\n" for i in range(1, 6))
        doc = seed_doc(docs_env, content=text)
        result = _run(svc().search_docs(make_caller(docs_env), "NEEDLE"))
        matches = result["results"][0]["matches"]
        assert [m["line"] for m in matches] == [1, 2]
        assert all(len(m["snippet"]) <= 20 and "needle" in m["snippet"] for m in matches)
        assert result["results"][0]["id"] == doc["id"]

    def test_search_truncated_scan(self, docs_env, monkeypatch):
        old = seed_doc(docs_env, title="Old", content="needle " + "x" * 100)
        new = seed_doc(docs_env, title="New", content="needle " + "y" * 100)
        monkeypatch.setattr(constants, "DOC_SEARCH_MAX_SCAN_BYTES", 150)
        result = _run(svc().search_docs(make_caller(docs_env), "needle"))
        assert result["truncated"] is True
        assert [r["id"] for r in result["results"]] == [new["id"]]
        # Metadata still matches past the budget.
        result = _run(svc().search_docs(make_caller(docs_env), "old"))
        assert [r["id"] for r in result["results"]] == [old["id"]]
        assert result["results"][0]["matches"] == []

    def test_search_limit_and_validation(self, docs_env):
        for i in range(3):
            seed_doc(docs_env, title=f"Doc {i}", content="common\n")
        caller = make_caller(docs_env)
        assert len(_run(svc().search_docs(caller, "common", limit=2))["results"]) == 2
        with pytest.raises(DocError, match="query"):
            _run(svc().search_docs(caller, "   "))

    def test_results_are_json_serializable(self, docs_env):
        doc = seed_doc(docs_env)
        caller = make_caller(docs_env)
        json.dumps(_run(svc().list_docs(caller)))
        json.dumps(_run(svc().search_docs(caller, "line")))
        json.dumps(_run(svc().read_doc(caller, doc["id"])))
