"""Quest Tables service wrappers for the HTTP routes (phases A15/A16):
``update_metadata``, ``list_changes``, ``get_change``, ``export_table``,
``add_share`` and ``remove_share`` in chat/tables/service.py, and the CSV
writer in chat/tables/export.py.

Covers owner / editor / reader checks, hidden == missing, the gate, the
stale-update token, history paging with ``in_effect`` and ``row_count``,
CSV and SQLite downloads, share events to the old and new audiences, no
``updated_at`` bump on share changes, and the per-table lock.
"""

import asyncio
import csv
import io
import sqlite3
import threading

import pytest

from chat.tables import revert, service
from chat.tables.service import (
    HISTORY_FORBIDDEN_MESSAGE,
    StaleUpdate,
    TableDisabled,
    TableError,
    TableForbidden,
    TableNotFound,
)
from tests.test_table_service import (  # noqa: F401 - env is a fixture
    _run,
    convo,
    env,
    events,
    fetch,
    make_table,
    ui,
    uid,
)

ALL_TYPES = [
    {"name": "Name", "type": "text", "required": True},
    {"name": "Price", "type": "number"},
    {"name": "Link", "type": "url"},
    {"name": "Active", "type": "boolean"},
    {"name": "Status", "type": "enum", "options": ["Open", "Done"]},
    {"name": "Tags", "type": "enum", "options": ["a", "b", "c"], "multiple": True},
    {"name": "Due", "type": "date"},
]


def insert(env, table, rows, who="alice"):
    return _run(service.apply_row_write(ui(env, who), table["id"], "insert", {"rows": rows}))


def make_typed(env):
    table = _run(service.create_table(ui(env), title="Typed", columns=ALL_TYPES))
    env.published.clear()
    return table


def share(env, table, who, permission):
    user_id = None if who is None else uid(env, who)
    _run(env.store.add_share(table["id"], user_id, permission))
    env.published.clear()


# ---------------------------------------------------------------------------
# Gate and not-found parity
# ---------------------------------------------------------------------------


class TestGateAndVisibility:
    def test_gate_closed_before_any_store_call(self, env, monkeypatch, tmp_path):
        table = make_table(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("store touched with the gate closed")

        for name in ("get_table", "update_table_metadata", "add_share", "remove_share"):
            monkeypatch.setattr(env.store, name, boom)
        calls = [
            service.update_metadata(ui(env), table["id"], title="X"),
            service.list_changes(ui(env), table["id"]),
            service.get_change(ui(env), table["id"], 1),
            service.export_table(ui(env), table["id"], fmt="csv", dest_dir=tmp_path),
            service.add_share(ui(env), table["id"], user_id=uid(env, "bob"), permission="read"),
            service.remove_share(ui(env), table["id"], 1),
        ]
        for call in calls:
            with pytest.raises(TableDisabled):
                _run(call)

    @staticmethod
    def _six_calls(env, caller, table_id, dest):
        return [
            service.update_metadata(caller, table_id, title="X"),
            service.list_changes(caller, table_id),
            service.get_change(caller, table_id, 1),
            service.export_table(caller, table_id, fmt="csv", dest_dir=dest),
            service.add_share(caller, table_id, user_id=uid(env, "carol"), permission="read"),
            service.remove_share(caller, table_id, 1),
        ]

    def test_hidden_equals_missing(self, env, tmp_path):
        table = make_table(env)
        bob = ui(env, "bob")

        def outcomes():
            out = []
            for call in self._six_calls(env, bob, table["id"], tmp_path):
                with pytest.raises(TableNotFound) as exc:
                    _run(call)
                out.append((type(exc.value), exc.value.code, str(exc.value)))
            return out

        hidden = outcomes()
        _run(service.delete_table(ui(env), table["id"]))
        assert outcomes() == hidden
        assert hidden[0] == (TableNotFound, "table_not_found",
                             service.TableNotFound(table["id"]).message)

    def test_hidden_never_takes_a_lock(self, env, tmp_path):
        table = make_table(env)

        async def go():
            lock = service._table_lock(table["id"])
            await lock.acquire()
            try:
                for call in self._six_calls(env, ui(env, "bob"), table["id"], tmp_path):
                    with pytest.raises(TableNotFound):
                        await asyncio.wait_for(call, 2)
            finally:
                lock.release()

        _run(go())


# ---------------------------------------------------------------------------
# update_metadata
# ---------------------------------------------------------------------------


class TestUpdateMetadata:
    def test_happy_path_publishes(self, env):
        table = make_table(env, shares=[("bob", "read")])
        updated = _run(service.update_metadata(
            ui(env), table["id"], title="Stock", description="All items",
            require_approval=True, expected_updated_at=table["updated_at"],
        ))
        assert (updated["title"], updated["description"], updated["require_approval"]) == (
            "Stock", "All items", True,
        )
        assert [s["user_id"] for s in updated["shares"]] == [uid(env, "bob")]
        assert updated["updated_at"] != table["updated_at"]
        assert set(events(env)) == {
            (uid(env, "alice"), "table_changed", table["id"], False),
            (uid(env, "bob"), "table_changed", table["id"], False),
            (uid(env, "alice"), "table_list_changed", None, True),
            (uid(env, "bob"), "table_list_changed", None, True),
        }

    def test_no_change_no_event(self, env):
        table = make_table(env)
        same = _run(service.update_metadata(ui(env), table["id"], title=table["title"]))
        assert same["updated_at"] == table["updated_at"]
        assert env.published == []

    def test_owner_only(self, env):
        table = make_table(env, shares=[("bob", "write"), (None, "write")])
        for who in ("bob", "carol"):
            with pytest.raises(TableForbidden):
                _run(service.update_metadata(ui(env, who), table["id"], title="X"))
        assert fetch(env, table)["title"] == table["title"]

    def test_waits_for_the_lock(self, env):
        table = make_table(env)

        async def go():
            lock = service._table_lock(table["id"])
            await lock.acquire()
            task = asyncio.ensure_future(
                service.update_metadata(ui(env), table["id"], title="Later"),
            )
            await asyncio.sleep(0.1)
            assert not task.done()
            assert (await env.store.get_table(table["id"]))["title"] == table["title"]
            lock.release()
            return await asyncio.wait_for(task, 5)

        assert _run(go())["title"] == "Later"

    def test_stale_token_on_a_no_op_edit_is_refused(self, env):
        # Store behavior: the token is checked before the no-change test
        # (only the conditional UPDATE is skipped on a no-op), so a stale
        # token refuses even when nothing would change.
        table = make_table(env)
        moved = _run(service.update_metadata(ui(env), table["id"], description="new"))
        env.published.clear()
        with pytest.raises(StaleUpdate) as exc:
            _run(service.update_metadata(
                ui(env), table["id"], description="new",
                expected_updated_at=table["updated_at"],
            ))
        assert exc.value.current["updated_at"] == moved["updated_at"]
        same = _run(service.update_metadata(
            ui(env), table["id"], description="new", expected_updated_at=moved["updated_at"],
        ))
        assert same["updated_at"] == moved["updated_at"]
        assert env.published == []

    def test_stale_update_carries_current(self, env):
        table = make_table(env, shares=[("bob", "read")])
        _run(service.update_metadata(ui(env), table["id"], description="first"))
        env.published.clear()
        with pytest.raises(StaleUpdate) as exc:
            _run(service.update_metadata(
                ui(env), table["id"], title="X", expected_updated_at=table["updated_at"],
            ))
        assert exc.value.code == "stale_update"
        assert exc.value.current["description"] == "first"
        assert [s["user_id"] for s in exc.value.current["shares"]] == [uid(env, "bob")]
        assert env.published == []

    def test_duplicate_title_and_invalid(self, env):
        make_table(env, title="Other")
        table = make_table(env)
        with pytest.raises(TableError) as exc:
            _run(service.update_metadata(ui(env), table["id"], title="Other"))
        assert exc.value.code == "duplicate_title"
        with pytest.raises(TableError) as exc:
            _run(service.update_metadata(ui(env), table["id"], title="   "))
        assert exc.value.code == "invalid_request"
        with pytest.raises(TableError) as exc:
            _run(service.update_metadata(ui(env), table["id"], require_approval="yes"))
        assert exc.value.code == "invalid_request"


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    def test_list_changes_in_effect_row_count_and_paging(self, env):
        table = make_table(env)
        insert(env, table, [{"Name": "a"}, {"Name": "b"}])        # change 2
        insert(env, table, [{"Name": "c"}])                        # change 3
        _run(service.apply_restore(ui(env), table["id"], 2))       # change 4
        _t, access, page = _run(service.list_changes(ui(env), table["id"]))
        assert access.rows == "free"
        assert [(c["id"], c["in_effect"], c["row_count"]) for c in page] == [
            (4, True, 0), (3, False, 1), (2, True, 2), (1, True, 0),
        ]
        assert page[0]["restore_to"] == 2
        assert set(page[0]) == {
            "id", "created_at", "source", "actor_user_id", "kind", "op", "summary",
            "schema_before", "schema_after", "size_bytes", "restore_to", "row_count",
            "in_effect",
        }
        _t, _a, older = _run(service.list_changes(ui(env), table["id"], limit=2, before_id=3))
        assert [c["id"] for c in older] == [2, 1]
        with pytest.raises(TableError) as exc:
            _run(service.list_changes(ui(env), table["id"], before_id="3"))
        assert exc.value.code == "invalid_args"

    def test_editors_only(self, env):
        table = make_table(env, shares=[("bob", "write"), ("carol", "read")])
        _run(service.list_changes(ui(env, "bob"), table["id"]))
        _run(service.get_change(ui(env, "bob"), table["id"], 1))
        for call in (
            service.list_changes(ui(env, "carol"), table["id"]),
            service.get_change(ui(env, "carol"), table["id"], 1),
        ):
            with pytest.raises(TableForbidden) as exc:
                _run(call)
            assert str(exc.value) == HISTORY_FORBIDDEN_MESSAGE

    def test_non_ui_callers_refused(self, env, tmp_path):
        table = make_table(env)
        for call in (
            service.list_changes(convo(env), table["id"]),
            service.get_change(convo(env), table["id"], 1),
            service.export_table(convo(env), table["id"], fmt="sqlite",
                                 include_history=True, dest_dir=tmp_path),
        ):
            with pytest.raises(TableError) as exc:
                _run(call)
            assert exc.value.code == "invalid_args"

    def test_everyone_share_permission_decides(self, env):
        table = make_table(env, shares=[(None, "read")])
        with pytest.raises(TableForbidden):
            _run(service.list_changes(ui(env, "carol"), table["id"]))
        share(env, table, None, "write")
        _run(service.list_changes(ui(env, "carol"), table["id"]))

    def test_owner_stays_editor_with_require_approval(self, env):
        table = make_table(env)
        _run(service.update_metadata(ui(env), table["id"], require_approval=True))
        _t, access, page = _run(service.list_changes(ui(env), table["id"]))
        assert access.rows == "free" and page

    def test_get_change(self, env):
        table = make_table(env)
        insert(env, table, [{"Name": "a"}])
        _t, _a, change = _run(service.get_change(ui(env), table["id"], 2))
        assert change["id"] == 2 and change["row_ids"] == [1]
        assert change["before"] is not None and change["after"] is not None
        with pytest.raises(revert.UnknownChangeError) as exc:
            _run(service.get_change(ui(env), table["id"], 99))
        assert exc.value.code == "unknown_change"
        with pytest.raises(TableError) as exc:
            _run(service.get_change(ui(env), table["id"], "2"))
        assert exc.value.code == "invalid_args"

    def test_get_change_entry(self, env):
        table = make_table(env, shares=[("bob", "write"), ("carol", "read")])
        insert(env, table, [{"Name": "a"}, {"Name": "b"}])        # change 2
        insert(env, table, [{"Name": "c"}])                        # change 3
        _run(service.apply_restore(ui(env), table["id"], 2))       # change 4
        _t, access, change, entry = _run(
            service.get_change_entry(ui(env, "bob"), table["id"], 3),
        )
        assert access.rows == "free"
        assert change["id"] == entry["id"] == 3 and change["row_ids"] == [3]
        assert entry["in_effect"] is False and entry["row_count"] == 1
        _t, _a, _c, entry = _run(service.get_change_entry(ui(env), table["id"], 4))
        assert entry["in_effect"] is True and entry["restore_to"] == 2
        with pytest.raises(revert.UnknownChangeError):
            _run(service.get_change_entry(ui(env), table["id"], 99))
        with pytest.raises(TableError) as exc:
            _run(service.get_change_entry(ui(env), table["id"], True))
        assert exc.value.code == "invalid_args"
        with pytest.raises(TableForbidden):
            _run(service.get_change_entry(ui(env, "carol"), table["id"], 1))

    def test_share_revoked_while_queued(self, env):
        table = make_table(env, shares=[("bob", "write")])

        async def go():
            lock = service._table_lock(table["id"])
            await lock.acquire()
            task = asyncio.ensure_future(service.list_changes(ui(env, "bob"), table["id"]))
            await asyncio.sleep(0.05)
            await env.store.add_share(table["id"], uid(env, "bob"), "read")
            lock.release()
            with pytest.raises(TableForbidden):
                await asyncio.wait_for(task, 5)

        _run(go())


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_csv_every_type(self, env, tmp_path):
        table = make_typed(env)
        tricky = 'Say "hi", then\nbye'
        insert(env, table, [
            {"Name": tricky, "Price": "12.50", "Link": "https://example.com/a?b=1",
             "Active": True, "Status": "Done", "Tags": ["c", "a"], "Due": " 2026-10-16 "},
            {"Name": "plain", "Price": 0, "Active": False, "Tags": []},
            {"Name": "ünïcode"},
        ])
        result_table, path = _run(service.export_table(
            ui(env), table["id"], fmt="csv", dest_dir=tmp_path,
        ))
        assert result_table["id"] == table["id"]
        assert path == tmp_path / "table.csv"
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert raw.startswith(b"Name,Price,Link,Active,Status,Tags,Due\r\n")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")))
        assert rows == [
            ["Name", "Price", "Link", "Active", "Status", "Tags", "Due"],
            [tricky, "12.5", "https://example.com/a?b=1", "true", "Done", "a; c", "2026-10-16"],
            ["plain", "0", "", "false", "", "", ""],
            ["ünïcode", "", "", "", "", "", ""],
        ]

    def test_csv_formula_injection_defused(self, env, tmp_path):
        table = _run(service.create_table(ui(env), title="Risky", columns=[
            {"name": "=Name", "type": "text"},
            {"name": "Amount", "type": "number"},
            {"name": "Link", "type": "url"},
            {"name": "Flag", "type": "boolean"},
            {"name": "Pick", "type": "enum", "options": ["-x", "ok"]},
            {"name": "Many", "type": "enum", "options": ["@a", "b"], "multiple": True},
        ]))
        insert(env, table, [
            {"=Name": "=HYPERLINK(\"http://evil\")", "Amount": "-5", "Pick": "-x",
             "Many": ["@a", "b"], "Flag": True},
            {"=Name": "+1", "Pick": "ok", "Many": ["b", "@a"]},
            {"=Name": "@SUM(A1)"},
            {"=Name": "\tx"},
            {"=Name": "safe = fine"},
        ])
        _t, path = _run(service.export_table(ui(env), table["id"], fmt="csv", dest_dir=tmp_path))
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))
        assert rows == [
            ["'=Name", "Amount", "Link", "Flag", "Pick", "Many"],
            ["'=HYPERLINK(\"http://evil\")", "-5", "", "true", "'-x", "'@a; b"],
            ["'+1", "", "", "", "ok", "'@a; b"],
            ["'@SUM(A1)", "", "", "", "", ""],
            ["'\tx", "", "", "", "", ""],
            ["safe = fine", "", "", "", "", ""],
        ]

    def test_csv_holds_the_lock_only_for_the_snapshot(self, env, tmp_path, monkeypatch):
        from chat.tables import export

        table = make_table(env)
        insert(env, table, [{"Name": "a"}])
        started, release = threading.Event(), threading.Event()
        real = export.write_csv

        def slow_write(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(export, "write_csv", slow_write)

        async def go():
            task = asyncio.ensure_future(
                service.export_table(ui(env), table["id"], fmt="csv", dest_dir=tmp_path),
            )
            while not started.is_set():
                await asyncio.sleep(0.01)
            # write_csv is blocked; a row write must not wait for it.
            await asyncio.wait_for(
                service.apply_row_write(ui(env), table["id"], "insert",
                                        {"rows": [{"Name": "b"}]}),
                2,
            )
            assert not task.done()
            release.set()
            return await asyncio.wait_for(task, 5)

        _t, path = _run(go())
        names = [r[0] for r in csv.reader(io.StringIO(path.read_text(encoding="utf-8")))]
        assert names == ["Name", "a"]  # the snapshot predates the write

    def test_cancelled_export_waits_for_the_file_work(self, env, tmp_path, monkeypatch):
        from chat.tables import export

        table = make_table(env)
        started, release = threading.Event(), threading.Event()
        real = export.write_csv

        def slow_write(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(export, "write_csv", slow_write)

        async def go():
            task = asyncio.ensure_future(
                service.export_table(ui(env), table["id"], fmt="csv", dest_dir=tmp_path),
            )
            while not started.is_set():
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.1)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)

        _run(go())
        assert (tmp_path / "table.csv").exists()

    def test_csv_readers_allowed(self, env, tmp_path):
        table = make_table(env, shares=[("bob", "read")])
        _t, path = _run(service.export_table(
            ui(env, "bob"), table["id"], fmt="csv", dest_dir=tmp_path,
        ))
        assert path.read_text(encoding="utf-8").startswith("Name,Price,SKU")

    def test_sqlite_without_and_with_history(self, env, tmp_path):
        table = make_table(env, shares=[("bob", "write"), ("carol", "read")])
        insert(env, table, [{"Name": "a"}])

        def tables_in(path):
            conn = sqlite3.connect(path)
            try:
                return {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                conn.close()

        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        _t, plain = _run(service.export_table(
            ui(env, "carol"), table["id"], fmt="sqlite", dest_dir=plain_dir,
        ))
        assert plain == plain_dir / "table.sqlite"
        assert "changes" not in tables_in(plain) and "rows" in tables_in(plain)

        hist_dir = tmp_path / "hist"
        hist_dir.mkdir()
        _t, hist = _run(service.export_table(
            ui(env, "bob"), table["id"], fmt="sqlite", include_history=True,
            dest_dir=hist_dir,
        ))
        assert "changes" in tables_in(hist)

    def test_include_history_reader_forbidden(self, env, tmp_path):
        table = make_table(env, shares=[("carol", "read")])
        dest = tmp_path / "dl"
        dest.mkdir()
        with pytest.raises(TableForbidden):
            _run(service.export_table(
                ui(env, "carol"), table["id"], fmt="sqlite", include_history=True,
                dest_dir=dest,
            ))
        assert list(dest.iterdir()) == []

    def test_bad_args(self, env, tmp_path):
        table = make_table(env)
        for kwargs in (
            {"fmt": "xlsx"},
            {"fmt": "csv", "include_history": True},
        ):
            with pytest.raises(TableError) as exc:
                _run(service.export_table(ui(env), table["id"], dest_dir=tmp_path, **kwargs))
            assert exc.value.code == "invalid_args"
        (tmp_path / "file").write_text("x")
        (tmp_path / "link").symlink_to(tmp_path)
        for bad in (tmp_path / "missing", tmp_path / "file", tmp_path / "link"):
            with pytest.raises(ValueError):
                _run(service.export_table(ui(env), table["id"], fmt="csv", dest_dir=bad))


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------


class TestShares:
    def test_add_share_events_and_no_bump(self, env):
        table = make_table(env)
        updated, changed = _run(service.add_share(
            ui(env), table["id"], user_id=uid(env, "bob"), permission="read",
        ))
        assert changed is True
        assert [(s["user_id"], s["permission"]) for s in updated["shares"]] == [
            (uid(env, "bob"), "read"),
        ]
        assert updated["updated_at"] == table["updated_at"]
        assert set(events(env)) == {
            (uid(env, "alice"), "table_changed", table["id"], True),
            (uid(env, "bob"), "table_changed", table["id"], True),
            (uid(env, "alice"), "table_list_changed", None, True),
            (uid(env, "bob"), "table_list_changed", None, True),
        }

    def test_same_permission_is_unchanged(self, env):
        table = make_table(env, shares=[("bob", "read")])
        _t, changed = _run(service.add_share(
            ui(env), table["id"], user_id=uid(env, "bob"), permission="read",
        ))
        assert changed is False and env.published == []
        updated, changed = _run(service.add_share(
            ui(env), table["id"], user_id=uid(env, "bob"), permission="write",
        ))
        assert changed is True
        assert [s["permission"] for s in updated["shares"]] == ["write"]

    def test_everyone_share_upserts(self, env):
        table = make_table(env)
        env.connected[:] = [uid(env, "carol")]
        _t, changed = _run(service.add_share(ui(env), table["id"], user_id=None, permission="read"))
        assert changed
        assert (uid(env, "carol"), "table_changed", table["id"], True) in set(events(env))
        updated, changed = _run(service.add_share(
            ui(env), table["id"], user_id=None, permission="write",
        ))
        assert changed and [s["permission"] for s in updated["shares"]] == ["write"]

    def test_remove_share_tells_removed_recipient(self, env):
        table = make_table(env, shares=[("bob", "read"), ("carol", "write")])
        bob_share = next(s for s in fetch(env, table)["shares"] if s["user_id"] == uid(env, "bob"))
        updated = _run(service.remove_share(ui(env), table["id"], bob_share["id"]))
        assert [s["user_id"] for s in updated["shares"]] == [uid(env, "carol")]
        assert updated["updated_at"] == table["updated_at"]
        assert (uid(env, "bob"), "table_changed", table["id"], True) in set(events(env))
        assert {u for u, t, _i, _n in events(env) if t == "table_list_changed"} == {
            uid(env, "alice"), uid(env, "bob"), uid(env, "carol"),
        }
        with pytest.raises(TableNotFound):
            _run(service.list_changes(ui(env, "bob"), table["id"]))

    def test_remove_everyone_share_tells_connected_users(self, env):
        table = make_table(env, shares=[(None, "read")])
        env.connected[:] = [uid(env, "carol")]
        share_id = fetch(env, table)["shares"][0]["id"]
        _run(service.remove_share(ui(env), table["id"], share_id))
        assert (uid(env, "carol"), "table_changed", table["id"], True) in set(events(env))
        assert (uid(env, "carol"), "table_list_changed", None, True) in set(events(env))

    def test_add_share_table_gone(self, env, monkeypatch):
        table = make_table(env)
        real = env.store.add_share

        async def delete_then_add(table_id, user_id, permission):
            # An account delete removes the row without the table lock.
            await env.store.delete_table(table_id)
            return await real(table_id, user_id, permission)

        monkeypatch.setattr(env.store, "add_share", delete_then_add)
        with pytest.raises(TableNotFound):
            _run(service.add_share(
                ui(env), table["id"], user_id=uid(env, "bob"), permission="read",
            ))

    def test_remove_share_not_found(self, env):
        table = make_table(env, shares=[("bob", "read")])
        other = make_table(env, title="Other", shares=[("carol", "read")])
        carol_share = fetch(env, other)["shares"][0]["id"]
        for share_id in (carol_share, 9999, "1"):
            with pytest.raises(TableError) as exc:
                _run(service.remove_share(ui(env), table["id"], share_id))
            assert exc.value.code == "share_not_found"
        assert len(fetch(env, other)["shares"]) == 1
        assert env.published == []

    def test_owner_only(self, env):
        table = make_table(env, shares=[("bob", "write")])
        share_id = fetch(env, table)["shares"][0]["id"]
        with pytest.raises(TableForbidden):
            _run(service.add_share(
                ui(env, "bob"), table["id"], user_id=uid(env, "carol"), permission="read",
            ))
        with pytest.raises(TableForbidden):
            _run(service.remove_share(ui(env, "bob"), table["id"], share_id))

    def test_store_errors_mapped(self, env):
        table = make_table(env)
        for kwargs, code in (
            ({"user_id": uid(env, "alice"), "permission": "read"}, "cannot_share_with_owner"),
            ({"user_id": uid(env, "bob"), "permission": "admin"}, "invalid_request"),
            ({"user_id": 999999, "permission": "read"}, "user_not_found"),
        ):
            with pytest.raises(TableError) as exc:
                _run(service.add_share(ui(env), table["id"], **kwargs))
            assert exc.value.code == code
        assert fetch(env, table)["shares"] == []

    def test_share_change_waits_for_the_lock(self, env):
        table = make_table(env)

        async def go():
            lock = service._table_lock(table["id"])
            await lock.acquire()
            task = asyncio.ensure_future(service.add_share(
                ui(env), table["id"], user_id=uid(env, "bob"), permission="read",
            ))
            await asyncio.sleep(0.1)
            assert not task.done()
            assert (await env.store.get_table(table["id"]))["shares"] == []
            lock.release()
            _t, changed = await asyncio.wait_for(task, 5)
            return changed

        assert _run(go()) is True
