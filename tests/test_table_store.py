"""Quest Tables metadata store (db/table_store.py) against an isolated SQLite file.

Covers (spec section 4.2, phase A1):

1. create_table validation, the shape, the per-owner cap, user tables
   always private, case-insensitive title uniqueness per
   ``(owner_id, project_id, mode)``.
2. get_table with/without shares.
3. list_accessible_tables / list_tables_shared_with: candidates, scope
   flags, mode filter, ``(updated_at DESC, id DESC)`` order and keyset paging.
4. update_table_metadata (stale token, rename collision, bump only on
   change), update_after_write, update_schema (version bump, stale check).
5. delete_table + shares, the share upsert / single-everyone-row rules.
6. id sweeps and FK cascades on user / project / table / recipient delete.
7. The migration's revision chain.

Same isolation pattern as tests/test_docs_store.py (``PRAGMA
foreign_keys=ON`` on every connection like db/engine.py).
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

from chat.tables.constants import TABLE_DESCRIPTION_MAX_LEN, TABLE_TITLE_MAX_LEN

SCHEMA = [{"key": "c1", "name": "Name", "type": "text"}]


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_table_store_test_")
    db_path = os.path.join(tmpdir, "quest.db")
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models_mod
    import db.table_store as store_mod

    models_mod.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(store_mod, "AsyncSessionLocal", session_local)

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
            pub = models_mod.Project(user_id=users[0].id, name="Open", public=True)
            db.add_all([p1, p2, pub])
            await db.commit()
            for p in (p1, p2, pub):
                await db.refresh(p)
            return [u.id for u in users], p1.id, p2.id, pub.id

    (alice, bob, carol), p1, p2, pub = _run(_seed())
    yield {
        "store": store_mod,
        "models": models_mod,
        "session": session_local,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "p1": p1,
        "p2": p2,
        "pub": pub,
    }
    _run(async_engine.dispose())
    sync_engine.dispose()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _create(env, owner=None, title="Contacts", **kw):
    kw.setdefault("mode", "private")
    kw.setdefault("schema", SCHEMA)
    return _run(env["store"].create_table(owner or env["alice"], title, **kw))


async def _set_updated_at(env, table_id, ts: datetime):
    async with env["session"]() as db:
        table = await db.get(env["models"].DataTable, table_id)
        table.updated_at = ts
        await db.commit()


async def _share_rows(env, table_id=None):
    async with env["session"]() as db:
        stmt = select(env["models"].DataTableShare)
        if table_id is not None:
            stmt = stmt.where(env["models"].DataTableShare.table_id == table_id)
        return list((await db.execute(stmt)).scalars().all())


async def _sql(env, statement, **params):
    async with env["session"]() as db:
        await db.execute(text(statement), params)
        await db.commit()


def _slow_session(monkeypatch, delay=0.2):
    """Sleep after every AsyncSession.get / execute so two concurrent store
    calls interleave between their read and their write."""
    from sqlalchemy.ext.asyncio import AsyncSession

    orig_get, orig_execute = AsyncSession.get, AsyncSession.execute

    async def slow_get(self, *args, **kwargs):
        result = await orig_get(self, *args, **kwargs)
        await asyncio.sleep(delay)
        return result

    async def slow_execute(self, *args, **kwargs):
        result = await orig_execute(self, *args, **kwargs)
        await asyncio.sleep(delay)
        return result

    monkeypatch.setattr(AsyncSession, "get", slow_get)
    monkeypatch.setattr(AsyncSession, "execute", slow_execute)


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


class TestCreate:
    def test_shape(self, env):
        table = _create(
            env, title="  Leads  ", description="  why  ",
            last_write_source="conversation:c1",
        )
        assert set(table) == {
            "id", "owner_id", "project_id", "title", "description", "mode",
            "schema", "schema_version", "row_count", "last_write_source",
            "require_approval", "created_at", "updated_at", "shares",
        }
        assert table["title"] == "Leads"
        assert table["description"] == "why"
        assert table["owner_id"] == env["alice"]
        assert table["project_id"] is None
        assert table["mode"] == "private"
        assert table["schema"] == SCHEMA
        assert table["schema_version"] == 1
        assert table["row_count"] == 0
        assert table["require_approval"] is False
        assert table["last_write_source"] == "conversation:c1"
        assert table["shares"] == []
        assert table["created_at"] == table["updated_at"]
        datetime.fromisoformat(table["updated_at"])
        uuid.UUID(table["id"])

    def test_pregenerated_id_and_project_mode(self, env):
        table_id = str(uuid.uuid4())
        table = _create(env, table_id=table_id, project_id=env["pub"], mode="public")
        assert table["id"] == table_id
        assert table["project_id"] == env["pub"]
        assert table["mode"] == "public"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"title": ""},
            {"title": "   "},
            {"title": "x" * (TABLE_TITLE_MAX_LEN + 1)},
            {"title": None},
            {"description": "x" * (TABLE_DESCRIPTION_MAX_LEN + 1)},
            {"mode": "secret"},
            {"mode": "public"},  # a user table is always private
            {"schema": {"c1": "text"}},
            {"schema": ["c1"]},
        ],
    )
    def test_validation(self, env, kwargs):
        kwargs.setdefault("title", "Fine")
        title = kwargs.pop("title")
        with pytest.raises(env["store"].TableValidationError):
            _create(env, title=title, **kwargs)

    def test_limits_inclusive(self, env):
        table = _create(
            env, title="x" * TABLE_TITLE_MAX_LEN,
            description="d" * TABLE_DESCRIPTION_MAX_LEN,
        )
        assert len(table["title"]) == TABLE_TITLE_MAX_LEN

    def test_per_owner_cap(self, env, monkeypatch):
        store = env["store"]
        monkeypatch.setattr(store, "TABLE_MAX_PER_OWNER", 3)
        _create(env, title="A")
        _create(env, title="B", project_id=env["p1"])  # project tables count too
        _create(env, title="C", project_id=env["pub"], mode="public")
        with pytest.raises(store.TableLimitError, match="TABLE_MAX_PER_OWNER") as exc:
            _create(env, title="D")
        assert "3" in str(exc.value)
        _create(env, owner=env["bob"], title="D")  # per owner, not global
        # Freeing a slot lets the owner create again.
        ids = _run(store.list_table_ids_for_user(env["alice"]))
        _run(store.delete_table(ids[0]))
        _create(env, title="D")

    def test_duplicate_title_case_insensitive(self, env):
        store = env["store"]
        _create(env, title="Roadmap")
        for dup in ("Roadmap", "roadmap", "  ROADMAP "):
            with pytest.raises(store.DuplicateTableTitleError):
                _create(env, title=dup)
        _create(env, title="Über Plan")
        with pytest.raises(store.DuplicateTableTitleError):
            _create(env, title="über plan")

    def test_title_scope(self, env):
        store = env["store"]
        _create(env, title="Roadmap")                          # alice user table
        _create(env, title="Roadmap", project_id=env["p1"])    # a project: fine
        _create(env, title="roadmap", project_id=env["p2"])    # another project: fine
        _create(env, owner=env["bob"], title="Roadmap")        # another owner: fine
        _create(env, title="Roadmap", project_id=env["pub"], mode="public")
        with pytest.raises(store.DuplicateTableTitleError):
            _create(env, title="ROADMAP", project_id=env["p1"])
        with pytest.raises(store.DuplicateTableTitleError):
            _create(env, title="ROADMAP", project_id=env["pub"], mode="public")

    def test_title_scope_per_mode(self, env):
        # Same (owner, project), other mode: a separate namespace, so a public
        # create never learns a private title (a legacy-shaped row via SQL).
        store = env["store"]
        private = _create(env, title="Secret", project_id=env["pub"], mode="public")
        _run(_sql(env, "UPDATE data_tables SET mode = 'private' WHERE id = :id",
                  id=private["id"]))
        public = _create(env, title="Secret", project_id=env["pub"], mode="public")
        assert public["mode"] == "public"
        with pytest.raises(store.DuplicateTableTitleError):
            _create(env, title="secret", project_id=env["pub"], mode="public")


class TestGet:
    def test_get_with_and_without_shares(self, env):
        store = env["store"]
        table = _create(env)
        _run(store.add_share(table["id"], env["bob"], "read"))
        full = _run(store.get_table(table["id"]))
        assert [s["user_id"] for s in full["shares"]] == [env["bob"]]
        assert full["schema"] == SCHEMA
        bare = _run(store.get_table(table["id"], with_shares=False))
        assert "shares" not in bare
        assert bare["id"] == table["id"]

    def test_missing(self, env):
        assert _run(env["store"].get_table("nope")) is None
        assert _run(env["store"].get_table("nope", with_shares=False)) is None


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


class TestListing:
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

        rows = {t["id"]: t for t in _run(store.list_accessible_tables(env["alice"]))}
        assert set(rows) == {mine["id"], bobs_shared["id"], carols_everyone["id"]}
        assert bobs_private["id"] not in rows and carols_other["id"] not in rows
        assert rows[mine["id"]]["shares"] == []
        assert rows[bobs_shared["id"]]["shares"][0]["permission"] == "write"

        owned = _run(store.list_accessible_tables(env["alice"], owned_only=True))
        assert [t["id"] for t in owned] == [mine["id"]]

        shared = {t["id"] for t in _run(store.list_tables_shared_with(env["alice"]))}
        assert shared == {bobs_shared["id"], carols_everyone["id"]}
        # The everyone share reaches bob too; carol's own table is not "shared with" her.
        assert {t["id"] for t in _run(store.list_tables_shared_with(env["bob"]))} == {
            carols_everyone["id"], carols_other["id"],
        }
        assert carols_everyone["id"] not in {
            t["id"] for t in _run(store.list_tables_shared_with(env["carol"]))
        }

    def test_scope_flags_and_mode(self, env):
        store = env["store"]
        user_t = _create(env, title="User")
        p1_t = _create(env, title="P1", project_id=env["p1"])
        pub_t = _create(env, title="Pub", project_id=env["pub"], mode="public")

        def ids(**kw):
            return {t["id"] for t in _run(store.list_accessible_tables(env["alice"], **kw))}

        assert ids() == {user_t["id"]}
        assert ids(project_id=env["p1"]) == {user_t["id"]}  # flag off
        assert ids(project_id=env["p1"], include_project_tables=True) == {
            user_t["id"], p1_t["id"],
        }
        assert ids(
            project_id=env["p1"], include_project_tables=True, include_user_tables=False,
        ) == {p1_t["id"]}
        assert ids(include_project_tables=True, include_user_tables=False) == set()
        assert ids(project_id=env["pub"], include_project_tables=True, mode="public") == {
            pub_t["id"],
        }
        with pytest.raises(store.TableValidationError):
            ids(mode="secret")

    def test_source_project_ids(self, env):
        # A private project's linked sources: only their PUBLIC tables.
        store = env["store"]
        p1_t = _create(env, title="P1", project_id=env["p1"])
        pub_t = _create(env, title="Pub", project_id=env["pub"], mode="public")
        legacy = _create(env, title="Pub private", project_id=env["pub"], mode="public")
        _run(_sql(env, "UPDATE data_tables SET mode = 'private' WHERE id = :id",
                  id=legacy["id"]))

        def ids(**kw):
            return {t["id"] for t in _run(store.list_accessible_tables(env["alice"], **kw))}

        assert ids(include_user_tables=False, source_project_ids=[env["pub"]]) == {
            pub_t["id"],
        }
        assert ids(
            project_id=env["p1"], include_project_tables=True,
            include_user_tables=False, source_project_ids={env["pub"]},
        ) == {p1_t["id"], pub_t["id"]}
        assert ids(include_user_tables=False, source_project_ids=[env["p2"]]) == set()

    def test_order_and_keyset_paging(self, env):
        store = env["store"]
        tables = [_create(env, title=f"T {i}") for i in range(7)]
        base = datetime(2026, 1, 1, 12, 0, 0)
        # Three share one timestamp to exercise the id tiebreak.
        stamps = [base.replace(hour=h) for h in (10, 11, 11, 11, 13, 14, 15)]
        for t, ts in zip(tables, stamps):
            _run(_set_updated_at(env, t["id"], ts))

        full = _run(store.list_accessible_tables(env["alice"]))
        keys = [(datetime.fromisoformat(t["updated_at"]), t["id"]) for t in full]
        assert keys == sorted(keys, reverse=True)
        assert len(full) == 7

        seen, before = [], None
        while page := _run(store.list_accessible_tables(env["alice"], limit=2, before=before)):
            assert len(page) <= 2
            seen.extend(t["id"] for t in page)
            before = (datetime.fromisoformat(page[-1]["updated_at"]), page[-1]["id"])
        assert seen == [t["id"] for t in full]  # no gaps, no duplicates

    def test_paging_real_timestamps(self, env):
        # Cursor round trip through the stored microsecond ISO strings.
        store = env["store"]
        tables = [_create(env, title=f"R {i}") for i in range(5)]
        for t in tables:
            _run(store.update_after_write(t["id"], row_count=1, last_write_source="ui:1"))
        full = _run(store.list_accessible_tables(env["alice"]))
        assert len({t["updated_at"] for t in full}) == 5
        assert any(datetime.fromisoformat(t["updated_at"]).microsecond for t in full)
        seen, before = [], None
        while page := _run(store.list_accessible_tables(env["alice"], limit=2, before=before)):
            seen.extend(t["id"] for t in page)
            before = (datetime.fromisoformat(page[-1]["updated_at"]), page[-1]["id"])
        assert seen == [t["id"] for t in full]

    def test_shared_with_paging(self, env):
        store = env["store"]
        tables = [_create(env, owner=env["bob"], title=f"B {i}") for i in range(5)]
        for t in tables:
            _run(store.add_share(t["id"], env["alice"], "read"))
            _run(_set_updated_at(env, t["id"], datetime(2026, 1, 1)))  # all equal
        full = _run(store.list_tables_shared_with(env["alice"]))
        assert [t["id"] for t in full] == sorted((t["id"] for t in tables), reverse=True)
        seen, before = [], None
        while page := _run(store.list_tables_shared_with(env["alice"], limit=2, before=before)):
            seen.extend(t["id"] for t in page)
            before = (datetime.fromisoformat(page[-1]["updated_at"]), page[-1]["id"])
        assert seen == [t["id"] for t in full]


# ---------------------------------------------------------------------------
# updates
# ---------------------------------------------------------------------------


class TestUpdateMetadata:
    def test_rename_bumps_updated_at(self, env):
        store = env["store"]
        table = _create(env, title="Old")
        _run(_set_updated_at(env, table["id"], datetime(2026, 1, 1)))
        token = _run(store.get_table(table["id"]))["updated_at"]
        updated = _run(store.update_table_metadata(
            table["id"], title="New", description="desc", require_approval=True,
            expected_updated_at=token,
        ))
        assert updated["title"] == "New"
        assert updated["description"] == "desc"
        assert updated["require_approval"] is True
        assert updated["updated_at"] != token
        assert updated["schema_version"] == 1  # metadata edits leave the schema alone
        assert "shares" in updated

    def test_noop_does_not_bump(self, env):
        store = env["store"]
        table = _create(env, title="Same", description="d")
        _run(_set_updated_at(env, table["id"], datetime(2026, 1, 1)))
        token = _run(store.get_table(table["id"]))["updated_at"]
        same = _run(store.update_table_metadata(
            table["id"], title=" Same ", description="d", require_approval=False,
        ))
        assert same["updated_at"] == token

    def test_stale_token(self, env):
        store = env["store"]
        table = _create(env, title="Old")
        with pytest.raises(store.StaleTableError) as exc:
            _run(store.update_table_metadata(
                table["id"], title="New", expected_updated_at="2000-01-01T00:00:00",
            ))
        assert exc.value.current["title"] == "Old"
        assert "shares" in exc.value.current
        assert _run(store.get_table(table["id"]))["title"] == "Old"

    def test_stale_update_same_token(self, env):
        store = env["store"]
        table = _create(env, title="Old")
        _run(_set_updated_at(env, table["id"], datetime(2026, 1, 1)))
        token = _run(store.get_table(table["id"]))["updated_at"]
        first = _run(store.update_table_metadata(
            table["id"], title="First", expected_updated_at=token,
        ))
        assert first["title"] == "First"
        with pytest.raises(store.StaleTableError) as exc:
            _run(store.update_table_metadata(
                table["id"], title="Second", expected_updated_at=token,
            ))
        assert exc.value.current["title"] == "First"
        assert exc.value.current["updated_at"] == first["updated_at"]

    def test_concurrent_metadata_updates_one_wins(self, env, monkeypatch):
        store = env["store"]
        table = _create(env, title="Old")
        _run(_set_updated_at(env, table["id"], datetime(2026, 1, 1)))
        token = _run(store.get_table(table["id"]))["updated_at"]
        _slow_session(monkeypatch)

        async def _both():
            return await asyncio.gather(
                store.update_table_metadata(table["id"], title="A", expected_updated_at=token),
                store.update_table_metadata(table["id"], title="B", expected_updated_at=token),
                return_exceptions=True,
            )

        results = _run(_both())
        wins = [r for r in results if isinstance(r, dict)]
        stale = [r for r in results if isinstance(r, store.StaleTableError)]
        assert len(wins) == 1 and len(stale) == 1, results
        assert _run(store.get_table(table["id"]))["title"] == wins[0]["title"]

    def test_rename_collision(self, env):
        store = env["store"]
        _create(env, title="Taken")
        other = _create(env, title="Other")
        with pytest.raises(store.DuplicateTableTitleError):
            _run(store.update_table_metadata(other["id"], title="TAKEN"))
        # Renaming to a case variant of its own title is fine.
        assert _run(store.update_table_metadata(other["id"], title="OTHER"))["title"] == "OTHER"
        # A same-titled table in another scope does not block.
        p1_t = _create(env, title="Proj", project_id=env["p1"])
        assert _run(store.update_table_metadata(p1_t["id"], title="Taken"))["title"] == "Taken"

    def test_validation_and_missing(self, env):
        store = env["store"]
        table = _create(env)
        with pytest.raises(store.TableValidationError):
            _run(store.update_table_metadata(table["id"], title=""))
        with pytest.raises(store.TableValidationError):
            _run(store.update_table_metadata(table["id"], require_approval="yes"))
        assert _run(store.update_table_metadata("nope", title="X")) is None


class TestWriteSetters:
    def test_update_after_write(self, env):
        store = env["store"]
        table = _create(env)
        _run(_set_updated_at(env, table["id"], datetime(2026, 1, 1)))
        token = _run(store.get_table(table["id"]))["updated_at"]
        updated = _run(store.update_after_write(
            table["id"], row_count=42, last_write_source="ui:7",
        ))
        assert updated["row_count"] == 42
        assert updated["last_write_source"] == "ui:7"
        assert updated["updated_at"] != token
        assert updated["schema_version"] == 1
        assert updated["schema"] == SCHEMA
        with pytest.raises(store.TableValidationError):
            _run(store.update_after_write(table["id"], row_count=-1, last_write_source=None))
        with pytest.raises(store.TableValidationError):
            _run(store.update_after_write(table["id"], row_count=True, last_write_source=None))
        assert _run(store.update_after_write("nope", row_count=1, last_write_source=None)) is None

    def test_update_schema(self, env):
        store = env["store"]
        table = _create(env)
        _run(_set_updated_at(env, table["id"], datetime(2026, 1, 1)))
        token = _run(store.get_table(table["id"]))["updated_at"]
        new_schema = SCHEMA + [{"key": "c2", "name": "Email", "type": "url"}]
        updated = _run(store.update_schema(
            table["id"], schema=new_schema, expected_schema_version=1,
            last_write_source="action_request:ar1",
        ))
        assert updated["schema"] == new_schema
        assert updated["schema_version"] == 2
        assert updated["row_count"] == 0  # untouched without row_count
        assert updated["last_write_source"] == "action_request:ar1"
        assert updated["updated_at"] != token

        reverted = _run(store.update_schema(
            table["id"], schema=SCHEMA, expected_schema_version=2,
            last_write_source="revert:5", row_count=3,
        ))
        assert reverted["schema_version"] == 3
        assert reverted["row_count"] == 3
        assert reverted["schema"] == SCHEMA

    def test_concurrent_update_schema_one_wins(self, env, monkeypatch):
        store = env["store"]
        table = _create(env)
        _slow_session(monkeypatch)

        async def _both():
            return await asyncio.gather(*(
                store.update_schema(
                    table["id"], schema=SCHEMA + [{"key": k, "name": k, "type": "text"}],
                    expected_schema_version=1, last_write_source=f"ui:{k}",
                )
                for k in ("c2", "c3")
            ), return_exceptions=True)

        results = _run(_both())
        wins = [r for r in results if isinstance(r, dict)]
        stale = [r for r in results if isinstance(r, store.StaleTableSchemaError)]
        assert len(wins) == 1 and len(stale) == 1, results
        final = _run(store.get_table(table["id"]))
        assert final["schema_version"] == 2
        assert final["schema"] == wins[0]["schema"]

    def test_update_schema_stale(self, env):
        store = env["store"]
        table = _create(env)
        with pytest.raises(store.StaleTableSchemaError) as exc:
            _run(store.update_schema(
                table["id"], schema=[], expected_schema_version=2,
                last_write_source="ui:1",
            ))
        assert exc.value.current["schema_version"] == 1
        assert not isinstance(exc.value, store.StaleTableError)
        after = _run(store.get_table(table["id"]))
        assert after["schema"] == SCHEMA and after["schema_version"] == 1
        with pytest.raises(store.TableValidationError):
            _run(store.update_schema(
                table["id"], schema="nope", expected_schema_version=1,
                last_write_source=None,
            ))
        assert _run(store.update_schema(
            "nope", schema=[], expected_schema_version=1, last_write_source=None,
        )) is None


# ---------------------------------------------------------------------------
# delete + shares
# ---------------------------------------------------------------------------


class TestDeleteAndShares:
    def test_delete_removes_shares(self, env):
        store = env["store"]
        table = _create(env)
        _run(store.add_share(table["id"], env["bob"], "read"))
        _run(store.add_share(table["id"], None, "read"))
        assert _run(store.delete_table(table["id"])) is True
        assert _run(store.get_table(table["id"])) is None
        assert _run(_share_rows(env, table["id"])) == []
        assert _run(store.delete_table(table["id"])) is False

    def test_single_everyone_row(self, env):
        store = env["store"]
        table = _create(env)
        first = _run(store.add_share(table["id"], None, "read"))
        second = _run(store.add_share(table["id"], None, "write"))
        assert second["id"] == first["id"]
        assert second["permission"] == "write"
        rows = _run(_share_rows(env, table["id"]))
        assert len(rows) == 1 and rows[0].user_id is None

    def test_everyone_index_backstop(self, env):
        # The partial unique index refuses a second everyone row even when
        # the store's pre-check is bypassed.
        from sqlalchemy.exc import IntegrityError

        table = _create(env)
        _run(env["store"].add_share(table["id"], None, "read"))
        with pytest.raises(IntegrityError):
            _run(_sql(
                env,
                "INSERT INTO data_table_shares (table_id, user_id, permission, created_at) "
                "VALUES (:t, NULL, 'write', '2026-01-01')",
                t=table["id"],
            ))

    def test_per_user_upsert(self, env):
        store = env["store"]
        table = _create(env)
        before = _run(store.get_table(table["id"]))["updated_at"]
        a = _run(store.add_share(table["id"], env["bob"], "read"))
        b = _run(store.add_share(table["id"], env["bob"], "write"))
        assert a["id"] == b["id"] and b["permission"] == "write"
        _run(store.add_share(table["id"], env["carol"], "read"))
        shares = _run(store.list_shares(table["id"]))
        assert [(s["user_id"], s["permission"]) for s in shares] == [
            (env["bob"], "write"), (env["carol"], "read"),
        ]
        assert set(shares[0]) == {"id", "user_id", "permission", "created_at"}
        # Access changes are not content changes.
        assert _run(store.get_table(table["id"]))["updated_at"] == before

    def test_share_validation(self, env):
        store = env["store"]
        table = _create(env)
        with pytest.raises(store.TableValidationError):
            _run(store.add_share(table["id"], env["bob"], "admin"))
        with pytest.raises(store.TableValidationError):
            _run(store.add_share(table["id"], env["alice"], "read"))  # the owner
        with pytest.raises(store.TableValidationError):
            _run(store.add_share("nope", env["bob"], "read"))
        with pytest.raises(store.TableValidationError):
            _run(store.add_share(table["id"], True, "read"))

    def test_add_share_integrity_fallback(self, env, monkeypatch):
        # A competing add_share lands between the pre-check SELECT and the
        # INSERT: the unique index refuses ours and the store updates the
        # winner's row instead of failing.
        from sqlalchemy.ext.asyncio import AsyncSession

        store = env["store"]
        table = _create(env)
        original = AsyncSession.execute
        state = {"armed": True}

        async def racing_execute(self, statement, *args, **kwargs):
            result = await original(self, statement, *args, **kwargs)
            if state["armed"] and "data_table_shares" in str(statement):
                state["armed"] = False
                result = result.freeze()  # buffer before the competing write
                await _sql(
                    env,
                    "INSERT INTO data_table_shares (table_id, user_id, permission, created_at) "
                    "VALUES (:t, :u, 'read', '2026-01-01')",
                    t=table["id"], u=env["bob"],
                )
                return result()
            return result

        monkeypatch.setattr(AsyncSession, "execute", racing_execute)
        share = _run(store.add_share(table["id"], env["bob"], "write"))
        monkeypatch.setattr(AsyncSession, "execute", original)
        assert state["armed"] is False
        rows = _run(_share_rows(env, table["id"]))
        assert len(rows) == 1
        assert share["id"] == rows[0].id
        assert rows[0].permission == "write" and share["permission"] == "write"

    def test_share_target_gone(self, env):
        store = env["store"]
        table = _create(env)
        with pytest.raises(store.ShareTargetGoneError):
            _run(store.add_share(table["id"], 999_999, "read"))

    def test_remove_share(self, env):
        store = env["store"]
        table = _create(env)
        other = _create(env, title="Other")
        share = _run(store.add_share(table["id"], env["bob"], "read"))
        assert _run(store.remove_share(other["id"], share["id"])) is False
        assert _run(store.remove_share(table["id"], share["id"])) is True
        assert _run(store.remove_share(table["id"], share["id"])) is False
        assert _run(store.list_shares(table["id"])) == []


# ---------------------------------------------------------------------------
# id sweeps + FK cascades
# ---------------------------------------------------------------------------


class TestSweepsAndCascades:
    def test_list_table_ids(self, env):
        store = env["store"]
        user_t = _create(env, title="U")
        p1_t = _create(env, title="P", project_id=env["p1"])
        bob_t = _create(env, owner=env["bob"], title="B")
        assert _run(store.list_table_ids_for_project(env["p1"])) == [p1_t["id"]]
        assert _run(store.list_table_ids_for_project(env["p2"])) == []
        assert set(_run(store.list_table_ids_for_user(env["alice"]))) == {
            user_t["id"], p1_t["id"],
        }
        assert _run(store.list_table_ids_for_user(env["bob"])) == [bob_t["id"]]

    def test_project_delete_cascades(self, env):
        store = env["store"]
        p_t = _create(env, title="P", project_id=env["p1"])
        user_t = _create(env, title="U")
        _run(store.add_share(p_t["id"], env["bob"], "read"))
        _run(_sql(env, "DELETE FROM projects WHERE id = :id", id=env["p1"]))
        assert _run(store.get_table(p_t["id"])) is None
        assert _run(store.get_table(user_t["id"])) is not None
        assert _run(_share_rows(env, p_t["id"])) == []

    def test_owner_delete_cascades(self, env):
        store = env["store"]
        bob_t = _create(env, owner=env["bob"], title="B")
        _run(store.add_share(bob_t["id"], env["carol"], "read"))
        _run(_sql(env, "DELETE FROM users WHERE id = :id", id=env["bob"]))
        assert _run(store.get_table(bob_t["id"])) is None
        assert _run(_share_rows(env, bob_t["id"])) == []

    def test_recipient_delete_removes_share(self, env):
        store = env["store"]
        alice_t = _create(env, title="A")
        _run(store.add_share(alice_t["id"], env["bob"], "write"))
        _run(store.add_share(alice_t["id"], None, "read"))
        _run(_sql(env, "DELETE FROM users WHERE id = :id", id=env["bob"]))
        remaining = _run(store.get_table(alice_t["id"]))
        # bob's share vanished with him; the everyone row survives
        assert [s["user_id"] for s in remaining["shares"]] == [None]

    def test_table_delete_cascades_shares_in_sql(self, env):
        # The FK cascade alone (no store helper) removes the shares.
        store = env["store"]
        table = _create(env)
        _run(store.add_share(table["id"], env["bob"], "read"))
        _run(_sql(env, "DELETE FROM data_tables WHERE id = :id", id=table["id"]))
        assert _run(_share_rows(env, table["id"])) == []


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_revision_chain():
    import importlib.util

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from config.paths import PROJECT_ROOT

    rev = "e5c97b362821"
    path = PROJECT_ROOT / "alembic" / "versions" / f"{rev}_create_data_tables_and_shares.py"
    spec = importlib.util.spec_from_file_location(f"migration_{rev}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == rev
    assert module.down_revision == "c7e2a9b4d1f8"  # add memory_suggestions
    script = ScriptDirectory.from_config(Config(str(PROJECT_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert rev in {r.revision for r in script.walk_revisions(base="base", head=heads[0])}
