"""Quest Docs metadata store (db/doc_store.py) against an isolated SQLite file.

Covers (spec section 4.2):

1. create_doc validation and case-insensitive title uniqueness per
   ``(owner_id, project_id)``, the NULL-project (user doc) case included.
2. get_doc with/without shares; the dict shape.
3. list_accessible_docs: candidates (owner, per-user share, everyone share),
   scope flags, ``(updated_at DESC, id DESC)`` order and keyset paging.
4. update_doc_metadata (stale token, duplicate title, bump only on change),
   set_doc_mode, update_after_write.
5. delete_doc + shares, the share upsert / single-everyone-row rules.
6. list_doc_ids_for_project / _for_user and FK cascades on project/user delete.

Same isolation pattern as tests/test_project_archive.py, plus
``PRAGMA foreign_keys=ON`` on every connection like db/engine.py.
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.docs.constants import DOC_DESCRIPTION_MAX_LEN, DOC_TITLE_MAX_LEN


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_docs_store_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.doc_store as doc_store_mod

    models_mod.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(doc_store_mod, "AsyncSessionLocal", session_local)

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
            p1 = models_mod.Project(user_id=users[0].id, name="Research")
            p2 = models_mod.Project(user_id=users[0].id, name="Other")
            db.add_all([p1, p2])
            await db.commit()
            await db.refresh(p1)
            await db.refresh(p2)
            return [u.id for u in users], p1.id, p2.id

    (alice, bob, carol), p1, p2 = _run(_seed())
    yield {
        "store": doc_store_mod,
        "models": models_mod,
        "session": session_local,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "p1": p1,
        "p2": p2,
    }
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _create(env, owner=None, title="Notes", **kw):
    kw.setdefault("mode", "private")
    return _run(env["store"].create_doc(owner or env["alice"], title, **kw))


async def _set_updated_at(env, doc_id, ts: datetime):
    async with env["session"]() as db:
        doc = await db.get(env["models"].Doc, doc_id)
        doc.updated_at = ts
        await db.commit()


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


class TestCreate:
    def test_shape(self, env):
        doc = _create(
            env, title="  Plan  ", description="  why  ", content_size=12,
            last_write_source="conversation:c1",
        )
        assert set(doc) == {
            "id", "owner_id", "project_id", "title", "description", "mode",
            "content_size", "asset_count", "last_write_source", "created_at",
            "updated_at", "shares",
        }
        assert doc["title"] == "Plan"
        assert doc["description"] == "why"
        assert doc["owner_id"] == env["alice"]
        assert doc["project_id"] is None
        assert doc["mode"] == "private"
        assert doc["content_size"] == 12
        assert doc["asset_count"] == 0
        assert doc["last_write_source"] == "conversation:c1"
        assert doc["shares"] == []
        assert doc["created_at"] == doc["updated_at"]
        datetime.fromisoformat(doc["updated_at"])  # parseable token
        uuid.UUID(doc["id"])

    def test_pregenerated_id(self, env):
        doc_id = str(uuid.uuid4())
        assert _create(env, doc_id=doc_id)["id"] == doc_id

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"title": ""},
            {"title": "   "},
            {"title": "x" * (DOC_TITLE_MAX_LEN + 1)},
            {"title": None},
            {"description": "x" * (DOC_DESCRIPTION_MAX_LEN + 1)},
            {"mode": "secret"},
            {"content_size": -1},
        ],
    )
    def test_validation(self, env, kwargs):
        kwargs.setdefault("title", "Fine")
        title = kwargs.pop("title")
        with pytest.raises(env["store"].DocValidationError):
            _create(env, title=title, **kwargs)

    def test_limits_inclusive(self, env):
        doc = _create(
            env, title="x" * DOC_TITLE_MAX_LEN,
            description="d" * DOC_DESCRIPTION_MAX_LEN,
        )
        assert len(doc["title"]) == DOC_TITLE_MAX_LEN

    def test_duplicate_title_case_insensitive(self, env):
        store = env["store"]
        _create(env, title="Roadmap")
        for dup in ("Roadmap", "roadmap", "  ROADMAP "):
            with pytest.raises(store.DuplicateDocTitleError):
                _create(env, title=dup)
        assert issubclass(store.DuplicateDocTitleError, ValueError)

    def test_duplicate_title_unicode_case(self, env):
        _create(env, title="Über Plan")
        with pytest.raises(env["store"].DuplicateDocTitleError):
            _create(env, title="über plan")

    def test_title_scope(self, env):
        _create(env, title="Roadmap")                         # alice user doc
        _create(env, title="Roadmap", project_id=env["p1"])   # same title in a project: fine
        _create(env, title="roadmap", project_id=env["p2"])   # another project: fine
        _create(env, owner=env["bob"], title="Roadmap")       # another owner: fine
        with pytest.raises(env["store"].DuplicateDocTitleError):
            _create(env, title="ROADMAP", project_id=env["p1"])


class TestGet:
    def test_get_with_and_without_shares(self, env):
        store = env["store"]
        doc = _create(env)
        _run(store.add_share(doc["id"], env["bob"], "read"))
        full = _run(store.get_doc(doc["id"]))
        assert [s["user_id"] for s in full["shares"]] == [env["bob"]]
        bare = _run(store.get_doc(doc["id"], with_shares=False))
        assert "shares" not in bare
        assert bare["id"] == doc["id"]

    def test_missing(self, env):
        assert _run(env["store"].get_doc("nope")) is None


# ---------------------------------------------------------------------------
# list_accessible_docs
# ---------------------------------------------------------------------------


class TestListAccessible:
    def test_candidates(self, env):
        store = env["store"]
        mine = _create(env, title="Mine")
        bobs_private = _create(env, owner=env["bob"], title="Bob private")
        bobs_shared = _create(env, owner=env["bob"], title="Bob shared")
        carols_everyone = _create(env, owner=env["carol"], title="Carol all")
        carols_other = _create(env, owner=env["carol"], title="Carol to bob")
        _run(store.add_share(bobs_shared["id"], env["alice"], "write"))
        _run(store.add_share(carols_everyone["id"], None, "read"))
        _run(store.add_share(carols_other["id"], env["bob"], "read"))

        ids = {d["id"] for d in _run(store.list_accessible_docs(env["alice"]))}
        assert ids == {mine["id"], bobs_shared["id"], carols_everyone["id"]}
        assert bobs_private["id"] not in ids and carols_other["id"] not in ids

        rows = {d["id"]: d for d in _run(store.list_accessible_docs(env["alice"]))}
        assert rows[mine["id"]]["shares"] == []
        assert rows[bobs_shared["id"]]["shares"][0]["permission"] == "write"
        assert rows[carols_everyone["id"]]["shares"][0]["user_id"] is None

    def test_scope_flags(self, env):
        store = env["store"]
        user_doc = _create(env, title="User doc")
        p1_doc = _create(env, title="P1 doc", project_id=env["p1"])
        p2_doc = _create(env, title="P2 doc", project_id=env["p2"])

        def ids(**kw):
            return {d["id"] for d in _run(store.list_accessible_docs(env["alice"], **kw))}

        assert ids() == {user_doc["id"]}
        assert ids(project_id=env["p1"]) == {user_doc["id"]}  # flag off
        assert ids(project_id=env["p1"], include_project_docs=True) == {
            user_doc["id"], p1_doc["id"],
        }
        assert ids(
            project_id=env["p1"], include_project_docs=True, include_user_docs=False,
        ) == {p1_doc["id"]}
        assert ids(include_project_docs=True, include_user_docs=False) == set()
        assert ids(include_user_docs=False) == set()
        assert p2_doc["id"] not in ids(project_id=env["p1"], include_project_docs=True)

    def test_order_and_keyset_paging(self, env):
        store = env["store"]
        docs = [_create(env, title=f"Doc {i}") for i in range(5)]
        base = datetime(2026, 1, 1, 12, 0, 0)
        # Two docs share a timestamp to exercise the id tiebreak.
        stamps = [base.replace(hour=h) for h in (10, 11, 11, 13, 14)]
        for d, ts in zip(docs, stamps):
            _run(_set_updated_at(env, d["id"], ts))

        full = _run(store.list_accessible_docs(env["alice"]))
        keys = [(datetime.fromisoformat(d["updated_at"]), d["id"]) for d in full]
        assert keys == sorted(keys, reverse=True)
        assert len(full) == 5

        seen = []
        before = None
        while True:
            page = _run(store.list_accessible_docs(env["alice"], limit=2, before=before))
            if not page:
                break
            seen.extend(d["id"] for d in page)
            last = page[-1]
            before = (datetime.fromisoformat(last["updated_at"]), last["id"])
        assert seen == [d["id"] for d in full]

    def test_limit(self, env):
        for i in range(3):
            _create(env, title=f"Doc {i}")
        assert len(_run(env["store"].list_accessible_docs(env["alice"], limit=2))) == 2


# ---------------------------------------------------------------------------
# updates
# ---------------------------------------------------------------------------


class TestUpdateMetadata:
    def test_rename_bumps_updated_at(self, env):
        store = env["store"]
        doc = _create(env, title="Old")
        _run(_set_updated_at(env, doc["id"], datetime(2026, 1, 1)))
        token = _run(store.get_doc(doc["id"]))["updated_at"]
        updated = _run(store.update_doc_metadata(
            doc["id"], title="New", description="desc", expected_updated_at=token,
        ))
        assert updated["title"] == "New"
        assert updated["description"] == "desc"
        assert updated["updated_at"] != token
        assert "shares" in updated

    def test_noop_does_not_bump(self, env):
        store = env["store"]
        doc = _create(env, title="Same")
        again = _run(store.update_doc_metadata(doc["id"], title=" Same "))
        assert again["updated_at"] == doc["updated_at"]

    def test_stale_token(self, env):
        store = env["store"]
        doc = _create(env, title="Old")
        _run(store.add_share(doc["id"], env["bob"], "read"))
        with pytest.raises(store.StaleDocError) as exc:
            _run(store.update_doc_metadata(
                doc["id"], title="New", expected_updated_at="2000-01-01T00:00:00",
            ))
        current = exc.value.current
        assert current["id"] == doc["id"]
        assert current["title"] == "Old"  # nothing changed
        assert current["updated_at"] == doc["updated_at"]
        assert [s["user_id"] for s in current["shares"]] == [env["bob"]]
        assert issubclass(store.StaleDocError, ValueError)

    def test_rename_duplicate(self, env):
        store = env["store"]
        _create(env, title="Taken")
        doc = _create(env, title="Mine")
        with pytest.raises(store.DuplicateDocTitleError):
            _run(store.update_doc_metadata(doc["id"], title="TAKEN"))
        # case-only rename of itself is fine
        assert _run(store.update_doc_metadata(doc["id"], title="MINE"))["title"] == "MINE"

    def test_validation_and_missing(self, env):
        store = env["store"]
        doc = _create(env)
        with pytest.raises(store.DocValidationError):
            _run(store.update_doc_metadata(doc["id"], title="  "))
        assert _run(store.update_doc_metadata("nope", title="X")) is None


class TestModeAndWrites:
    def test_set_doc_mode(self, env):
        store = env["store"]
        doc = _create(env)
        _run(_set_updated_at(env, doc["id"], datetime(2026, 1, 1)))
        before = _run(store.get_doc(doc["id"]))["updated_at"]
        out = _run(store.set_doc_mode(doc["id"], "public"))
        assert out["mode"] == "public"
        assert out["updated_at"] != before
        with pytest.raises(store.DocValidationError):
            _run(store.set_doc_mode(doc["id"], "internal"))
        assert _run(store.set_doc_mode("nope", "public")) is None

    def test_update_after_write(self, env):
        store = env["store"]
        doc = _create(env, content_size=5)
        _run(_set_updated_at(env, doc["id"], datetime(2026, 1, 1)))
        out = _run(store.update_after_write(
            doc["id"], content_size=99, last_write_source="ui",
        ))
        assert out["content_size"] == 99
        assert out["asset_count"] == 0  # unchanged when None
        assert out["last_write_source"] == "ui"
        assert out["updated_at"] > "2026-01-01T00:00:00"
        out = _run(store.update_after_write(
            doc["id"], content_size=100, asset_count=3,
            last_write_source="action_request:7",
        ))
        assert out["asset_count"] == 3
        assert out["last_write_source"] == "action_request:7"
        with pytest.raises(store.DocValidationError):
            _run(store.update_after_write(doc["id"], content_size=-1, last_write_source="ui"))
        assert _run(store.update_after_write("nope", content_size=1, last_write_source="ui")) is None


# ---------------------------------------------------------------------------
# delete + shares
# ---------------------------------------------------------------------------


async def _share_rows(env, doc_id=None):
    async with env["session"]() as db:
        stmt = select(env["models"].DocShare)
        if doc_id is not None:
            stmt = stmt.where(env["models"].DocShare.doc_id == doc_id)
        return list((await db.execute(stmt)).scalars().all())


class TestDeleteAndShares:
    def test_delete_removes_shares(self, env):
        store = env["store"]
        doc = _create(env)
        _run(store.add_share(doc["id"], env["bob"], "read"))
        _run(store.add_share(doc["id"], None, "read"))
        assert _run(store.delete_doc(doc["id"])) is True
        assert _run(store.get_doc(doc["id"])) is None
        assert _run(_share_rows(env, doc["id"])) == []
        assert _run(store.delete_doc(doc["id"])) is False

    def test_single_everyone_row(self, env):
        store = env["store"]
        doc = _create(env)
        first = _run(store.add_share(doc["id"], None, "read"))
        second = _run(store.add_share(doc["id"], None, "write"))
        assert second["id"] == first["id"]
        assert second["permission"] == "write"
        rows = _run(_share_rows(env, doc["id"]))
        assert len(rows) == 1 and rows[0].user_id is None

    def test_per_user_upsert(self, env):
        store = env["store"]
        doc = _create(env)
        a = _run(store.add_share(doc["id"], env["bob"], "read"))
        b = _run(store.add_share(doc["id"], env["bob"], "write"))
        assert a["id"] == b["id"] and b["permission"] == "write"
        _run(store.add_share(doc["id"], env["carol"], "read"))
        shares = _run(store.list_shares(doc["id"]))
        assert [(s["user_id"], s["permission"]) for s in shares] == [
            (env["bob"], "write"), (env["carol"], "read"),
        ]
        assert set(shares[0]) == {"id", "user_id", "permission", "created_at"}

    def test_share_validation(self, env):
        store = env["store"]
        doc = _create(env)
        with pytest.raises(store.DocValidationError):
            _run(store.add_share(doc["id"], env["bob"], "admin"))
        with pytest.raises(store.DocValidationError):
            _run(store.add_share(doc["id"], env["alice"], "read"))  # the owner
        with pytest.raises(store.DocValidationError):
            _run(store.add_share("nope", env["bob"], "read"))

    def test_remove_share(self, env):
        store = env["store"]
        doc = _create(env)
        other = _create(env, title="Other")
        share = _run(store.add_share(doc["id"], env["bob"], "read"))
        # the share id must belong to the given doc
        assert _run(store.remove_share(other["id"], share["id"])) is False
        assert _run(store.remove_share(doc["id"], share["id"])) is True
        assert _run(store.remove_share(doc["id"], share["id"])) is False
        assert _run(store.list_shares(doc["id"])) == []


# ---------------------------------------------------------------------------
# id sweeps + FK cascades
# ---------------------------------------------------------------------------


class TestSweepsAndCascades:
    def test_list_doc_ids(self, env):
        store = env["store"]
        user_doc = _create(env, title="U")
        p1_doc = _create(env, title="P", project_id=env["p1"])
        bob_doc = _create(env, owner=env["bob"], title="B")
        assert _run(store.list_doc_ids_for_project(env["p1"])) == [p1_doc["id"]]
        assert _run(store.list_doc_ids_for_project(env["p2"])) == []
        assert set(_run(store.list_doc_ids_for_user(env["alice"]))) == {
            user_doc["id"], p1_doc["id"],
        }
        assert _run(store.list_doc_ids_for_user(env["bob"])) == [bob_doc["id"]]

    def test_project_delete_cascades(self, env):
        store = env["store"]
        p_doc = _create(env, title="P", project_id=env["p1"])
        user_doc = _create(env, title="U")
        _run(store.add_share(p_doc["id"], env["bob"], "read"))

        async def _drop_project():
            async with env["session"]() as db:
                await db.execute(text("DELETE FROM projects WHERE id = :id"), {"id": env["p1"]})
                await db.commit()

        _run(_drop_project())
        assert _run(store.get_doc(p_doc["id"])) is None
        assert _run(store.get_doc(user_doc["id"])) is not None
        assert _run(_share_rows(env, p_doc["id"])) == []

    def test_user_delete_cascades(self, env):
        store = env["store"]
        bob_doc = _create(env, owner=env["bob"], title="B")
        alice_doc = _create(env, title="A")
        _run(store.add_share(alice_doc["id"], env["bob"], "write"))
        _run(store.add_share(alice_doc["id"], None, "read"))

        async def _drop_bob():
            async with env["session"]() as db:
                await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": env["bob"]})
                await db.commit()

        _run(_drop_bob())
        assert _run(store.get_doc(bob_doc["id"])) is None
        remaining = _run(store.get_doc(alice_doc["id"]))
        # bob's share vanished with him; the everyone row survives
        assert [s["user_id"] for s in remaining["shares"]] == [None]
