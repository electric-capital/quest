"""Quest Tables service layer (chat/tables/service.py) and realtime events
(chat/tables/events.py) against an isolated SQLite file and TABLES_DIR,
with the ``tables`` feature gate open.

Covers (phase A8):

1. Gate closed: TableDisabled before any DB work; hidden vs missing
   byte-equal TableNotFound.
2. The per-table lock: concurrent writes produce ordered changes, reads
   take the lock too, cancellation shielding.
3. Verdicts: approval (nothing written), bypass_approval, denied.
4. Schema change (version bump, stale token), restore round trip (quest.db
   matches table.db meta), create failure cleanup, delete.
5. Event audiences; project-delete and account-delete directory sweeps.
"""

import asyncio
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from chat.tables import service, storage
from chat.tables.access import DENY_READ_ONLY_SHARE, DENY_SUB_AGENT
from chat.tables.constants import table_not_found_message, tables_disabled_message
from chat.tables.schema import load_schema
from chat.tables.service import (
    Caller,
    StaleSchema,
    TableApprovalRequired,
    TableDenied,
    TableDisabled,
    TableForbidden,
    TableNotFound,
    caller_for_ui,
)

COLUMNS = [
    {"name": "Name", "type": "text", "required": True},
    {"name": "Price", "type": "number"},
    {"name": "SKU", "type": "text", "unique": True},
]


def _run(coro):
    return asyncio.run(coro)


def _fk_on(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import config.feature_gates as fg

    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
    fg.set_feature_enabled(fg.FEATURE_TABLES, True)

    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(sync_engine, "connect", _fk_on)
    event.listen(async_engine.sync_engine, "connect", _fk_on)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import chat.storage as storage_mod
    import db.action_request_store as action_request_store
    import db.conversation_store as conversation_store
    import db.doc_store as doc_store
    import db.guide_store as guide_store
    import db.memory_store as memory_store
    import db.models as models
    import db.project_store as project_store
    import db.routine_store as routine_store
    import db.table_store as table_store
    import db.user_store as user_store

    models.Base.metadata.create_all(sync_engine)
    for mod in (
        action_request_store, conversation_store, doc_store, guide_store,
        memory_store, project_store, routine_store, table_store, user_store,
    ):
        monkeypatch.setattr(mod, "AsyncSessionLocal", session_local)

    dirs = {name: tmp_path / name for name in ("chats", "projects", "docs", "tables")}
    for path in dirs.values():
        path.mkdir()
    monkeypatch.setattr(storage_mod, "CHATS_DIR", dirs["chats"])
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", dirs["projects"])
    monkeypatch.setattr(storage_mod, "DOCS_DIR", dirs["docs"])
    monkeypatch.setattr(storage_mod, "TABLES_DIR", dirs["tables"])

    published: list[tuple[int, dict]] = []
    connected: list[int] = []
    from chat.realtime import bus
    monkeypatch.setattr(
        bus, "publish_to_user", lambda user_id, ev: published.append((user_id, ev)),
    )
    monkeypatch.setattr(bus, "connected_user_ids", lambda: list(connected))

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
    public_project = _run(project_store.create_project(alice, "Open", public=True))["id"]

    yield SimpleNamespace(
        tmp=tmp_path, dirs=dirs, users=users, published=published,
        connected=connected, store=table_store, fg=fg,
        private_project=private_project, public_project=public_project,
    )
    _run(async_engine.dispose())
    sync_engine.dispose()


def convo(env, who="alice", *, project=None, public=False, run_kind="top_level"):
    return Caller(
        user=env.users[who], conversation_id=str(uuid.uuid4()), project_id=project,
        is_public=public, run_kind=run_kind,
    )


def ui(env, who="alice"):
    return caller_for_ui(env.users[who])


def make_table(env, title="Inventory", *, owner="alice", project_id=None, shares=()):
    table = _run(service.create_table(
        ui(env, owner), title=title, columns=COLUMNS, project_id=project_id,
    ))
    for who, permission in shares:
        user_id = None if who is None else env.users[who]["id"]
        _run(env.store.add_share(table["id"], user_id, permission))
    env.published.clear()
    return table


def table_dir(env, table):
    return env.dirs["tables"] / table["id"]


def changes(env, table):
    return list(reversed(storage.list_changes(table_dir(env, table))))


def fetch(env, table):
    return _run(env.store.get_table(table["id"]))


def events(env):
    return [(u, ev["type"], ev.get("table_id"), ev.get("updated_at") is None)
            for u, ev in env.published]


def uid(env, who):
    return env.users[who]["id"]


# ---------------------------------------------------------------------------
# Gate and not-found parity
# ---------------------------------------------------------------------------


class TestGateAndVisibility:
    def test_gate_closed_before_any_db_work(self, env, monkeypatch):
        table = make_table(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(env.store, "get_table", boom)
        monkeypatch.setattr(env.store, "list_accessible_tables", boom)
        caller = convo(env)
        calls = [
            service.get_visible_table(caller, table["id"]),
            service.list_visible_tables(caller),
            service.query_rows(caller, table["id"]),
            service.sample_rows(caller, table["id"]),
            service.apply_row_write(caller, table["id"], "insert", {"rows": [{"Name": "x"}]}),
            service.apply_schema_change(caller, table["id"], [], expected_schema_version=1),
            service.apply_restore(ui(env), table["id"], 1),
            service.create_table(ui(env), title="T", columns=COLUMNS),
            service.delete_table(ui(env), table["id"]),
        ]
        for coro in calls:
            with pytest.raises(TableDisabled) as exc:
                _run(coro)
            assert exc.value.code == "tables_disabled"
            assert str(exc.value) == tables_disabled_message()

    def test_hidden_and_missing_byte_equal(self, env, monkeypatch):
        table = make_table(env)
        calls = {"get": 0}
        real_get = env.store.get_table

        async def counting(*a, **k):
            calls["get"] += 1
            return await real_get(*a, **k)

        monkeypatch.setattr(env.store, "get_table", counting)
        bob = convo(env, "bob")
        with pytest.raises(TableNotFound) as hidden:
            _run(service.get_visible_table(bob, table["id"]))
        hidden_calls = calls["get"]
        missing_id = str(uuid.uuid4())
        with pytest.raises(TableNotFound) as missing:
            _run(service.get_visible_table(bob, missing_id))
        assert calls["get"] - hidden_calls == hidden_calls == 1
        assert str(hidden.value) == table_not_found_message(table["id"])
        assert str(hidden.value).replace(table["id"], "X") == \
            str(missing.value).replace(missing_id, "X")
        assert hidden.value.code == missing.value.code == "table_not_found"
        # Public conversation: a private table is hidden from its owner too.
        with pytest.raises(TableNotFound):
            _run(service.query_rows(
                convo(env, public=True, project=env.public_project), table["id"],
            ))

    def test_list_visible_tables(self, env):
        mine = make_table(env, "Mine")
        shared = make_table(env, "Bobs", owner="bob", shares=[("alice", "read")])
        project = make_table(env, "Proj", project_id=env.private_project)
        public = make_table(env, "Pub", project_id=env.public_project)
        make_table(env, "Hidden", owner="bob")

        ids = [t["id"] for t, _a in _run(service.list_visible_tables(convo(env)))]
        assert set(ids) == {mine["id"], shared["id"]}
        ids = [t["id"] for t, _a in _run(service.list_visible_tables(
            convo(env, project=env.private_project)))]
        assert set(ids) == {mine["id"], shared["id"], project["id"]}
        ids = [t["id"] for t, _a in _run(service.list_visible_tables(
            convo(env, project=env.public_project, public=True)))]
        assert ids == [public["id"]]
        # Newest-updated first.
        _run(service.apply_row_write(convo(env), mine["id"], "insert", {"rows": [{"Name": "a"}]}))
        ids = [t["id"] for t, _a in _run(service.list_visible_tables(convo(env), limit=1))]
        assert ids == [mine["id"]]


# ---------------------------------------------------------------------------
# Lock, ordering, shielding
# ---------------------------------------------------------------------------


class TestLock:
    def test_concurrent_inserts_are_ordered(self, env):
        table = make_table(env)
        caller = convo(env)

        async def go():
            return await asyncio.gather(
                service.apply_row_write(caller, table["id"], "insert",
                                        {"rows": [{"Name": "a", "SKU": "1"}]}),
                service.apply_row_write(caller, table["id"], "insert",
                                        {"rows": [{"Name": "b"}, {"Name": "c"}]}),
            )

        first, second = _run(go())
        assert [first["change_id"], second["change_id"]] == [2, 3]
        assert second["row_count"] == 3
        log = changes(env, table)
        assert [c["id"] for c in log] == [1, 2, 3]
        assert [c["op"] for c in log[1:]] == ["insert", "insert"]
        row = fetch(env, table)
        assert row["row_count"] == 3 == storage.row_count(table_dir(env, table))
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"

    def test_reads_take_the_lock(self, env):
        table = make_table(env)

        async def go():
            lock = service._table_lock(table["id"])
            await lock.acquire()
            task = asyncio.ensure_future(service.query_rows(convo(env), table["id"]))
            await asyncio.sleep(0.1)
            assert not task.done()
            lock.release()
            _t, _a, result = await asyncio.wait_for(task, 5)
            return result

        assert _run(go()).total == 0

    def test_read_waits_for_restore(self, env, monkeypatch):
        table = make_table(env)
        _run(service.apply_row_write(convo(env), table["id"], "insert", {"rows": [{"Name": "a"}]}))
        entered, release = threading.Event(), threading.Event()
        order: list[str] = []
        real_restore = service.revert.restore_to

        def slow_restore(*a, **k):
            entered.set()
            assert release.wait(5)
            result = real_restore(*a, **k)
            order.append("restore")
            return result

        real_query = service.filters.run_query

        def recording_query(*a, **k):
            order.append("query")
            return real_query(*a, **k)

        monkeypatch.setattr(service.revert, "restore_to", slow_restore)
        monkeypatch.setattr(service.filters, "run_query", recording_query)

        async def go():
            restore = asyncio.ensure_future(service.apply_restore(ui(env), table["id"], 1))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            query = asyncio.ensure_future(service.query_rows(ui(env), table["id"]))
            await asyncio.sleep(0.1)
            assert not query.done()
            release.set()
            await restore
            return await query

        _t, _a, result = _run(go())
        assert order == ["restore", "query"]
        assert result.total == 0

    def test_cancelled_write_lands_fully(self, env, monkeypatch):
        table = make_table(env)
        caller = convo(env)
        entered, release = threading.Event(), threading.Event()
        real_insert = storage.insert_rows

        def slow_insert(*a, **k):
            entered.set()
            assert release.wait(5)
            return real_insert(*a, **k)

        monkeypatch.setattr(storage, "insert_rows", slow_insert)

        async def go():
            task = asyncio.ensure_future(service.apply_row_write(
                caller, table["id"], "insert", {"rows": [{"Name": "a"}, {"Name": "b"}]},
            ))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(500):
                if env.published:
                    break
                await asyncio.sleep(0.01)

        _run(go())
        row = fetch(env, table)
        assert row["row_count"] == 2 == storage.row_count(table_dir(env, table))
        assert row["last_write_source"] == f"conversation:{caller.conversation_id}"
        assert [c["id"] for c in changes(env, table)] == [1, 2]
        assert [t for _u, t, _i, _n in events(env)] == ["table_changed", "table_list_changed"]


    def test_second_write_waits_for_first_bump(self, env, monkeypatch):
        """The second writer enters storage only after the first one's
        quest.db bump: row_count lands in write order."""
        table = make_table(env)
        caller = convo(env)
        entered, release = threading.Event(), threading.Event()
        calls: list[str] = []
        real_insert = storage.insert_rows
        real_bump = env.store.update_after_write

        def gated_insert(*a, **k):
            calls.append("insert")
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
            return real_insert(*a, **k)

        async def recording_bump(*a, **k):
            calls.append(f"bump:{k['row_count']}")
            return await real_bump(*a, **k)

        monkeypatch.setattr(storage, "insert_rows", gated_insert)
        monkeypatch.setattr(env.store, "update_after_write", recording_bump)

        async def go():
            first = asyncio.ensure_future(service.apply_row_write(
                caller, table["id"], "insert", {"rows": [{"Name": "a"}]}))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            second = asyncio.ensure_future(service.apply_row_write(
                caller, table["id"], "insert", {"rows": [{"Name": "b"}, {"Name": "c"}]}))
            await asyncio.sleep(0.1)
            assert calls == ["insert"] and not second.done()
            release.set()
            return await first, await second

        first, second = _run(go())
        assert calls == ["insert", "bump:1", "insert", "bump:3"]
        assert (first["change_id"], second["change_id"]) == (2, 3)
        assert fetch(env, table)["row_count"] == 3

    def test_access_re_resolved_under_the_lock(self, env):
        """A share revoked while the write queued: the write refuses with
        the not-found text and writes nothing."""
        table = make_table(env, shares=[("bob", "write")])
        share_id = fetch(env, table)["shares"][0]["id"]

        async def go():
            lock = service._table_lock(table["id"])
            await lock.acquire()
            task = asyncio.ensure_future(service.apply_row_write(
                convo(env, "bob"), table["id"], "insert", {"rows": [{"Name": "a"}]},
                bypass_approval=True,
            ))
            await asyncio.sleep(0.05)
            assert not task.done()
            assert await env.store.remove_share(table["id"], share_id)
            lock.release()
            with pytest.raises(TableNotFound) as exc:
                await task
            return exc.value

        exc = _run(go())
        assert str(exc) == table_not_found_message(table["id"])
        assert [c["id"] for c in changes(env, table)] == [1]
        assert env.published == []

    def test_cancelled_restore_lands_fully(self, env, monkeypatch):
        table = make_table(env)
        _run(service.apply_row_write(convo(env), table["id"], "insert", {"rows": [{"Name": "a"}]}))
        env.published.clear()
        entered, release = threading.Event(), threading.Event()
        real_restore = service.revert.restore_to

        def slow_restore(*a, **k):
            entered.set()
            assert release.wait(5)
            return real_restore(*a, **k)

        monkeypatch.setattr(service.revert, "restore_to", slow_restore)

        async def go():
            task = asyncio.ensure_future(service.apply_restore(ui(env), table["id"], 1))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(500):
                if env.published:
                    break
                await asyncio.sleep(0.01)

        _run(go())
        row = fetch(env, table)
        meta = storage.read_meta(table_dir(env, table))
        assert row["schema_version"] == meta.schema_version == 2
        assert row["row_count"] == 0 == storage.row_count(table_dir(env, table))
        assert row["last_write_source"] == "revert:1"
        assert [t for _u, t, _i, _n in events(env)] == ["table_changed", "table_list_changed"]


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


class TestVerdicts:
    def test_approval_writes_nothing(self, env):
        table = make_table(env, shares=[("bob", "write")])
        caller = convo(env)
        with pytest.raises(TableApprovalRequired) as exc:
            _run(service.apply_row_write(caller, table["id"], "insert",
                                         {"rows": [{"Name": "a"}]}))
        assert exc.value.code == "approval_required"
        assert exc.value.access.rows == "approval"
        assert exc.value.suggested_request == {
            "request_type": "write_table_rows",
            "params": {"table_id": table["id"], "operation": "insert",
                       "rows": [{"Name": "a"}]},
        }
        assert [c["id"] for c in changes(env, table)] == [1]
        assert fetch(env, table)["row_count"] == 0
        assert env.published == []

    def test_bypass_approval_writes(self, env):
        table = make_table(env, shares=[("bob", "write")])
        result = _run(service.apply_row_write(
            convo(env, "bob"), table["id"], "insert", {"rows": [{"Name": "a"}]},
            write_source="action_request:42", actor_user_id=uid(env, "alice"),
            bypass_approval=True,
        ))
        assert result["inserted"] == 1 and result["change_id"] == 2
        entry = changes(env, table)[-1]
        assert entry["source"] == "action_request:42"
        assert entry["actor_user_id"] == uid(env, "alice")
        assert fetch(env, table)["last_write_source"] == "action_request:42"

    def test_denied(self, env):
        table = make_table(env, shares=[("bob", "read")])
        for caller, reason in (
            (convo(env, "bob"), DENY_READ_ONLY_SHARE),
            (convo(env, run_kind="sub_agent"), DENY_SUB_AGENT),
        ):
            with pytest.raises(TableDenied) as exc:
                _run(service.apply_row_write(
                    caller, table["id"], "insert", {"rows": [{"Name": "a"}]},
                    bypass_approval=True,
                ))
            assert exc.value.code == "denied" and str(exc.value) == reason
        assert fetch(env, table)["row_count"] == 0

    def test_update_upsert_delete(self, env):
        table = make_table(env)
        c = convo(env)
        ins = _run(service.apply_row_write(c, table["id"], "insert", {"rows": [
            {"Name": "a", "Price": 1, "SKU": "x"}, {"Name": "b", "Price": 5, "SKU": "y"},
        ]}))
        a, b = ins["inserted_ids"]
        up = _run(service.apply_row_write(c, table["id"], "update",
                                          {"updates": [{"_id": a, "Price": 2}]}))
        assert up["updated"] == 1
        ups = _run(service.apply_row_write(c, table["id"], "upsert", {
            "key_column": "SKU", "rows": [{"SKU": "y", "Name": "B"}, {"SKU": "z", "Name": "c"}],
        }))
        assert (ups["inserted"], ups["updated"]) == (1, 1)
        dele = _run(service.apply_row_write(c, table["id"], "delete", {
            "filter": {"column": "Price", "op": "gte", "value": 2},
        }))
        assert dele["deleted"] == 2 and sorted(dele["row_ids"]) == sorted([a, b])
        none = _run(service.apply_row_write(c, table["id"], "delete", {
            "filter": {"column": "Name", "op": "eq", "value": "nobody"},
        }))
        assert none["change_id"] is None and none["deleted"] == 0
        with pytest.raises(service.TableError) as exc:
            _run(service.apply_row_write(c, table["id"], "delete",
                                         {"row_ids": [1], "filter": {}}))
        assert exc.value.code == "invalid_args"
        assert fetch(env, table)["row_count"] == 1
        _t, _a, rows = _run(service.sample_rows(c, table["id"]))
        assert [r["Name"] for r in rows] == ["c"]
        assert set(rows[0]) == {"_id", "_updated_at", "Name", "Price", "SKU"}


# ---------------------------------------------------------------------------
# Schema change, restore, create, delete
# ---------------------------------------------------------------------------


class TestSchemaAndRestore:
    def test_schema_change_bumps_version(self, env):
        table = make_table(env)
        result = _run(service.apply_schema_change(
            ui(env), table["id"],
            [{"op": "add_column", "column": {"name": "Notes", "type": "text"}},
             {"op": "rename_table", "title": "Stock"}],
            expected_schema_version=1,
        ))
        assert result["schema_version"] == 2 and result["change_id"] == 2
        row = fetch(env, table)
        meta = storage.read_meta(table_dir(env, table))
        assert row["schema_version"] == meta.schema_version == 2
        assert [c["name"] for c in row["schema"]] == ["Name", "Price", "SKU", "Notes"]
        assert load_schema(row["schema"]) == meta.schema
        assert row["title"] == "Stock"
        assert row["last_write_source"] == f"ui:{uid(env, 'alice')}"
        assert events(env) == [
            (uid(env, "alice"), "table_changed", table["id"], False),
            (uid(env, "alice"), "table_list_changed", None, True),
        ]
        assert env.published[0][1]["schema_version"] == 2

        with pytest.raises(StaleSchema) as exc:
            _run(service.apply_schema_change(
                ui(env), table["id"], [{"op": "drop_column", "name": "Notes"}],
                expected_schema_version=1,
            ))
        assert exc.value.code == "stale_schema"
        assert exc.value.current["schema_version"] == 2

    def test_schema_change_denied_for_non_owner_and_public(self, env):
        table = make_table(env, shares=[("bob", "write")])
        with pytest.raises(TableDenied):
            _run(service.apply_schema_change(
                ui(env, "bob"), table["id"], [{"op": "rename_table", "title": "X"}],
                expected_schema_version=1,
            ))
        public = make_table(env, "Pub", project_id=env.public_project)
        with pytest.raises(TableDenied):
            _run(service.apply_schema_change(
                convo(env, project=env.public_project, public=True), public["id"],
                [{"op": "rename_table", "title": "X"}], expected_schema_version=1,
            ))
        assert fetch(env, table)["schema_version"] == 1

    def test_restore_round_trip(self, env):
        table = make_table(env, shares=[("bob", "write")])
        bob_ui = ui(env, "bob")
        _run(service.apply_row_write(bob_ui, table["id"], "insert",
                                     {"rows": [{"Name": "a"}, {"Name": "b"}]}))
        _run(service.apply_schema_change(
            ui(env), table["id"],
            [{"op": "add_column", "column": {"name": "Notes", "type": "text"}}],
            expected_schema_version=1,
        ))
        env.published.clear()
        result = _run(service.apply_restore(bob_ui, table["id"], 2))
        assert result["restored_to"] == 2 and result["change_id"] == 4
        row = fetch(env, table)
        meta = storage.read_meta(table_dir(env, table))
        assert row["schema_version"] == meta.schema_version == 3
        assert load_schema(row["schema"]) == meta.schema
        assert [c["name"] for c in row["schema"]] == ["Name", "Price", "SKU"]
        assert row["row_count"] == 2 == storage.row_count(table_dir(env, table))
        assert row["last_write_source"] == "revert:2"
        assert changes(env, table)[-1]["actor_user_id"] == uid(env, "bob")
        assert {u for u, t, _i, _n in events(env) if t == "table_changed"} == {
            uid(env, "alice"), uid(env, "bob"),
        }
        # Restore to the table's creation: zero rows.
        _run(service.apply_restore(ui(env), table["id"], 1))
        assert fetch(env, table)["row_count"] == 0

    def test_restore_denied_for_read_share_and_unknown_change(self, env):
        table = make_table(env, shares=[("bob", "read")])
        with pytest.raises(TableDenied):
            _run(service.apply_restore(ui(env, "bob"), table["id"], 1))
        with pytest.raises(storage.TableStorageError) as exc:
            _run(service.apply_restore(ui(env), table["id"], 99))
        assert exc.value.code == "unknown_change"
        assert fetch(env, table)["schema_version"] == 1


    def test_rename_rolled_back_when_the_file_change_fails(self, env, monkeypatch):
        table = make_table(env)

        def broken(*_a, **_k):
            raise storage.TableStorageError("busy", "boom")

        monkeypatch.setattr(storage, "apply_schema_change", broken)
        with pytest.raises(storage.TableStorageError):
            _run(service.apply_schema_change(
                ui(env), table["id"], [{"op": "rename_table", "title": "Stock"}],
                expected_schema_version=1,
            ))
        row = fetch(env, table)
        assert row["title"] == "Inventory" and row["schema_version"] == 1
        assert env.published == []

    def test_lost_schema_bump_is_healed(self, env, monkeypatch):
        """table.db committed a schema change but quest.db's bump failed:
        the next call refuses with schema_mismatch after recording
        table.db's schema, and a retry works."""
        table = make_table(env)
        _run(service.apply_row_write(convo(env), table["id"], "insert", {"rows": [{"Name": "a"}]}))
        real_update = env.store.update_schema

        async def failing(*_a, **_k):
            raise RuntimeError("quest.db unavailable")

        monkeypatch.setattr(env.store, "update_schema", failing)
        with pytest.raises(RuntimeError):
            _run(service.apply_schema_change(
                ui(env), table["id"],
                [{"op": "add_column", "column": {"name": "Notes", "type": "text"}}],
                expected_schema_version=1,
            ))
        assert fetch(env, table)["schema_version"] == 1
        assert storage.read_meta(table_dir(env, table)).schema_version == 2
        monkeypatch.setattr(env.store, "update_schema", real_update)
        env.published.clear()

        with pytest.raises(storage.SchemaMismatchError):
            _run(service.query_rows(convo(env), table["id"]))
        row = fetch(env, table)
        meta = storage.read_meta(table_dir(env, table))
        assert row["schema_version"] == meta.schema_version == 2
        assert load_schema(row["schema"]) == meta.schema
        assert row["row_count"] == 1
        assert row["last_write_source"] == f"ui:{uid(env, 'alice')}"
        assert [t for _u, t, _i, _n in events(env)] == ["table_changed", "table_list_changed"]
        _t, _a, result = _run(service.query_rows(convo(env), table["id"]))
        assert result.total == 1 and result.rows[0]["Notes"] is None
        # A plain mismatch with nothing to heal leaves quest.db alone.
        with pytest.raises(storage.SchemaMismatchError):
            _run(service._file_call(
                {**row, "schema_version": 7}, storage.insert_rows,
                table_dir(env, table), load_schema(row["schema"]),
                rows=[{"Name": "z"}], schema_version=7,
                source="ui:1", actor_user_id=None,
            ))
        assert fetch(env, table)["schema_version"] == 2


class TestCreateDelete:
    def test_create(self, env):
        table = _run(service.create_table(
            ui(env), title="  Inv ", columns=COLUMNS, project_id=env.public_project,
        ))
        assert table["title"] == "Inv" and table["mode"] == "public"
        assert table["last_write_source"] == f"ui:{uid(env, 'alice')}"
        meta = storage.read_meta(table_dir(env, table))
        assert meta.table_id == table["id"] and load_schema(table["schema"]) == meta.schema
        log = changes(env, table)
        assert [(c["id"], c["summary"]) for c in log] == [(1, "Created table")]
        assert events(env) == [(uid(env, "alice"), "table_list_changed", None, True)]
        # A private conversation never creates a public table.
        with pytest.raises(service.TableError) as exc:
            _run(service.create_table(
                convo(env), title="X", columns=COLUMNS, project_id=env.public_project,
                write_source="action_request:1",
            ))
        assert exc.value.code == "mode_mismatch"
        with pytest.raises(service.TableError) as exc:
            _run(service.create_table(ui(env), title="Inv", columns=COLUMNS,
                                      project_id=env.public_project))
        assert exc.value.code == "duplicate_title"
        assert len(list(env.dirs["tables"].iterdir())) == 1

    def test_create_storage_failure_leaves_nothing(self, env, monkeypatch):
        def broken(table_dir, **_k):
            table_dir.mkdir()
            (table_dir / "table.db").write_bytes(b"partial")
            raise storage.TableStorageError("busy", "boom")

        monkeypatch.setattr(storage, "create_table_db", broken)
        with pytest.raises(storage.TableStorageError):
            _run(service.create_table(ui(env), title="T", columns=COLUMNS))
        assert list(env.dirs["tables"].iterdir()) == []
        assert _run(env.store.list_table_ids_for_user(uid(env, "alice"))) == []
        assert env.published == []

    def test_create_store_failure_removes_directory(self, env, monkeypatch):
        async def refuse(*_a, **_k):
            raise env.store.TableLimitError("full")

        monkeypatch.setattr(env.store, "create_table", refuse)
        with pytest.raises(service.TableError) as exc:
            _run(service.create_table(ui(env), title="T", columns=COLUMNS))
        assert exc.value.code == "table_limit"
        assert list(env.dirs["tables"].iterdir()) == []

    def test_delete(self, env):
        table = make_table(env, shares=[("bob", "write"), ("carol", "read")])
        with pytest.raises(TableForbidden):
            _run(service.delete_table(ui(env, "bob"), table["id"]))
        with pytest.raises(TableNotFound):
            _run(service.delete_table(ui(env, "bob"), str(uuid.uuid4())))
        assert table_dir(env, table).is_dir()

        _run(service.delete_table(ui(env), table["id"]))
        assert fetch(env, table) is None
        assert not table_dir(env, table).exists()
        people = [uid(env, w) for w in ("alice", "bob", "carol")]
        assert events(env) == (
            [(u, "table_changed", table["id"], True) for u in people]
            + [(u, "table_list_changed", None, True) for u in people]
        )
        assert env.published[0][1]["schema_version"] is None


# ---------------------------------------------------------------------------
# Audiences and lifecycle sweeps
# ---------------------------------------------------------------------------


class TestAudiencesAndSweeps:
    def _write(self, env, table, caller):
        env.published.clear()
        _run(service.apply_row_write(caller, table["id"], "insert",
                                     {"rows": [{"Name": "x"}]}, bypass_approval=True))
        return [u for u, t, _i, _n in events(env) if t == "table_changed"]

    def test_audiences(self, env):
        alice, bob, carol = (uid(env, w) for w in ("alice", "bob", "carol"))
        unshared = make_table(env, "U")
        assert self._write(env, unshared, convo(env)) == [alice]
        shared = make_table(env, "S", shares=[("bob", "read")])
        assert self._write(env, shared, convo(env)) == [alice, bob]
        everyone = make_table(env, "E", shares=[(None, "read")])
        env.connected[:] = [carol, alice, 999]
        assert self._write(env, everyone, convo(env)) == [alice, carol, 999]
        ev = env.published[0][1]
        assert set(ev) == {"type", "table_id", "updated_at", "schema_version"}
        assert ev["updated_at"] == fetch(env, everyone)["updated_at"]

    def test_project_delete_sweeps_table_dirs(self, env):
        from chat.project_routes import delete_user_project

        one = make_table(env, "One", project_id=env.private_project,
                         shares=[("bob", "read")])
        two = make_table(env, "Two", project_id=env.private_project)
        mine = make_table(env, "Mine")
        alice = env.users["alice"]
        assert _run(delete_user_project(env.private_project, user=alice)) == {"success": True}
        for gone in (one, two):
            assert fetch(env, gone) is None
            assert not table_dir(env, gone).exists()
        assert fetch(env, mine) is not None and table_dir(env, mine).is_dir()
        seen = events(env)
        assert sorted(seen[:3]) == sorted([
            (alice["id"], "table_changed", one["id"], True),
            (uid(env, "bob"), "table_changed", one["id"], True),
            (alice["id"], "table_changed", two["id"], True),
        ])
        assert seen[3:] == [
            (alice["id"], "table_list_changed", None, True),
            (uid(env, "bob"), "table_list_changed", None, True),
        ]

    def test_account_delete_sweeps_table_dirs(self, env):
        import chat.routes.user as user_routes

        user_table = make_table(env, "Mine")
        project_table = make_table(env, "Proj", project_id=env.private_project)
        bobs = make_table(env, "Bobs", owner="bob")
        response = _run(user_routes.delete_account(request=None, user=env.users["alice"]))
        assert response.status_code == 200
        assert not table_dir(env, user_table).exists()
        assert not table_dir(env, project_table).exists()
        assert table_dir(env, bobs).is_dir()
