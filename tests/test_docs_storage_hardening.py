"""Review-driven hardening tests for the Quest Docs storage layer.

Pins the fixes made after the storage review:

1. ``files.modify_body`` is a single critical section -- concurrent
   appends never lose a line and ``content_size`` matches the bytes on disk.
2. No revision snapshot when the new body is byte-identical.
3. ``DOC_REVISION_MAX_COUNT`` bounds snapshots inside the retention window
   (newest always kept).
4. ``init_doc`` refuses an existing directory (no inherited assets/revisions).
5. The partial unique index allows at most one "everyone" share row per doc
   even when the store's check-then-insert is bypassed.
6. ``list_accessible_docs(mode=...)`` filters in SQL so ``limit`` applies to
   the visible set.
7. Keyset paging splits an ``updated_at`` tie across pages correctly.
"""

import asyncio
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.docs import constants, files
from chat.docs.files import DocFileError


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def docs_dir(monkeypatch, tmp_path):
    import chat.storage as storage_mod
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr(storage_mod, "DOCS_DIR", root)
    return root


# ---------------------------------------------------------------------------
# files.py
# ---------------------------------------------------------------------------


def test_modify_body_serializes_concurrent_appends(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "start\n")
    n = 40
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            files.modify_body(doc_id, lambda cur, i=i: cur + f"line {i}\n")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    body = files.read_body(doc_id)
    lines = body.splitlines()
    assert lines[0] == "start"
    assert sorted(int(l.split()[1]) for l in lines[1:]) == list(range(n))
    assert len(body.encode("utf-8")) == os.path.getsize(files.doc_paths(doc_id).body)


def test_modify_body_returns_new_body_and_size_and_propagates_fn_errors(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "a\r\nb\n")
    new_body, size = files.modify_body(doc_id, lambda cur: cur.replace("b", "B"))
    assert new_body == "a\nB\n"
    assert size == len(new_body.encode("utf-8"))

    class Boom(ValueError):
        pass

    def bad(_cur: str) -> str:
        raise Boom("no match")

    with pytest.raises(Boom):
        files.modify_body(doc_id, bad)
    assert files.read_body(doc_id) == "a\nB\n"
    # The failed call took no snapshot; the successful one took exactly one.
    assert len(files.list_revisions(doc_id)) == 1


def test_identical_body_write_takes_no_snapshot(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "same\n")
    files.write_body(doc_id, "same\n")
    files.modify_body(doc_id, lambda cur: cur)
    assert files.list_revisions(doc_id) == []
    files.write_body(doc_id, "different\n")
    assert len(files.list_revisions(doc_id)) == 1


def test_revision_count_cap_prunes_oldest_keeps_newest(docs_dir, monkeypatch):
    monkeypatch.setattr(constants, "DOC_REVISION_MAX_COUNT", 5)
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "v0\n")
    for i in range(1, 12):
        files.write_body(doc_id, f"v{i}\n")
    revisions = files.list_revisions(doc_id)
    # Pruning runs before the new snapshot is counted in the next write, so
    # the directory holds at most the cap (+1 for the snapshot just taken).
    assert len(revisions) <= 6
    # The newest snapshot is the body just replaced (v10).
    assert revisions[-1].read_text() == "v10\n"
    # The oldest snapshots (v0, v1, ...) were pruned.
    assert all(r.read_text() not in ("v0\n", "v1\n", "v2\n") for r in revisions)


def test_init_doc_refuses_existing_directory(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "first\n")
    with pytest.raises(DocFileError):
        files.init_doc(doc_id, "second\n")
    assert files.read_body(doc_id) == "first\n"
    # A stray non-directory entry is refused too.
    other = str(uuid.uuid4())
    (docs_dir / other).write_text("not a dir")
    with pytest.raises(DocFileError):
        files.init_doc(other, "x\n")


def test_lone_surrogate_is_a_doc_file_error(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "ok\n")
    with pytest.raises(DocFileError):
        files.write_body(doc_id, "bad \ud800 char\n")
    assert files.read_body(doc_id) == "ok\n"


def test_asset_path_overlong_name_is_not_found(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "ok\n")
    with pytest.raises(DocFileError):
        files.asset_path(doc_id, "a" * 300 + ".png")


def test_lock_table_does_not_retain_unused_locks(docs_dir):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "ok\n")
    assert doc_id not in list(files._locks.keys())  # released after init
    lock = files._get_lock(doc_id)
    assert doc_id in list(files._locks.keys())
    del lock
    assert doc_id not in list(files._locks.keys())


# ---------------------------------------------------------------------------
# doc_store.py
# ---------------------------------------------------------------------------


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_docs_hardening_")
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
                models_mod.User(email=f"{n}@example.com", api_key=f"k-{uuid.uuid4().hex}")
                for n in ("alice", "bob")
            ]
            db.add_all(users)
            await db.commit()
            for u in users:
                await db.refresh(u)
            return [u.id for u in users]

    alice, bob = _run(_seed())
    yield {"store": doc_store_mod, "models": models_mod, "session": session_local,
           "alice": alice, "bob": bob}
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_partial_unique_index_allows_one_everyone_row(env):
    store, models = env["store"], env["models"]
    doc = _run(store.create_doc(env["alice"], "Shared", mode="private"))

    async def _insert_everyone_twice():
        async with env["session"]() as db:
            db.add(models.DocShare(doc_id=doc["id"], user_id=None, permission="read",
                                   created_at=datetime.now(timezone.utc)))
            await db.commit()
            db.add(models.DocShare(doc_id=doc["id"], user_id=None, permission="write",
                                   created_at=datetime.now(timezone.utc)))
            await db.commit()

    with pytest.raises(IntegrityError):
        _run(_insert_everyone_twice())
    shares = _run(store.list_shares(doc["id"]))
    assert [s["user_id"] for s in shares] == [None]
    # The store's upsert still works on top of the index.
    _run(store.add_share(doc["id"], None, "write"))
    shares = _run(store.list_shares(doc["id"]))
    assert len(shares) == 1 and shares[0]["permission"] == "write"


def test_list_accessible_docs_mode_filter_applies_before_limit(env):
    store = env["store"]
    alice = env["alice"]
    for i in range(5):
        _run(store.create_doc(alice, f"Private {i}", mode="private"))
    pub = _run(store.create_doc(alice, "Public one", mode="public"))

    async def _age_public():
        async with env["session"]() as db:
            row = await db.get(env["models"].Doc, pub["id"])
            row.updated_at = datetime(2000, 1, 1)
            await db.commit()

    _run(_age_public())
    rows = _run(store.list_accessible_docs(alice, mode="public", limit=3))
    assert [r["id"] for r in rows] == [pub["id"]]
    rows = _run(store.list_accessible_docs(alice, mode="private", limit=3))
    assert len(rows) == 3 and all(r["mode"] == "private" for r in rows)
    with pytest.raises(ValueError):
        _run(store.list_accessible_docs(alice, mode="secret"))


def test_keyset_paging_splits_an_updated_at_tie(env):
    store = env["store"]
    alice = env["alice"]
    stamps = [10, 11, 11, 13]
    ids = []
    for i, minute in enumerate(stamps):
        d = _run(store.create_doc(alice, f"Doc {i}", mode="private"))
        ids.append(d["id"])

    async def _stamp():
        async with env["session"]() as db:
            for doc_id, minute in zip(ids, stamps):
                row = await db.get(env["models"].Doc, doc_id)
                row.updated_at = datetime(2024, 1, 1, 0, minute)
            await db.commit()

    _run(_stamp())
    seen: list[str] = []
    before = None
    while True:
        page = _run(store.list_accessible_docs(alice, limit=2, before=before))
        if not page:
            break
        seen.extend(r["id"] for r in page)
        last = page[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])
        if len(page) < 2:
            break
    assert sorted(seen) == sorted(ids)
    assert len(seen) == len(ids)  # no row repeated or skipped across the tie
