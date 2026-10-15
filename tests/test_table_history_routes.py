"""Quest Tables history and download routes (chat/tables/history_routes.py,
chat/tables/download_routes.py, phase A16).

Same harness as tests/test_table_routes.py: the routers in a bare FastAPI
app with the auth dependency overridden, on the isolated ``env`` of
tests/test_table_service.py (tables gate open, events captured).

Covers: the gate, 404 parity and the reader 403 on every history route;
the change list (shape, paging, ``source_label`` per source kind, actor);
change detail for rows / schema / restore changes, truncation and the
``unknown_change`` body; restore round trip with ``in_effect`` flags and
undo; CSV contents per column type; SQLite copies with and without the
``changes`` table; temp directory cleanup.
"""

import csv
import io
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.tables import service
from chat.tables.constants import (
    TABLE_CHANGE_DETAIL_MAX_ROWS,
    table_not_found_message,
    tables_disabled_message,
)
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    _run,
    env,
    fetch,
    make_table,
    ui,
    uid,
)

HISTORY_FORBIDDEN = {
    "error": "forbidden",
    "message": "History is available to people who can edit this table.",
}


def _app(env, who="alice"):
    from chat.tables import download_routes, history_routes, routes

    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(history_routes.router)
    app.include_router(download_routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return app


def client(env, who="alice"):
    return TestClient(_app(env, who))


def detail(resp):
    return resp.json()["detail"]


def insert(env, table, rows, who="alice", **kwargs):
    return _run(service.apply_row_write(
        ui(env, who), table["id"], "insert", {"rows": rows}, **kwargs,
    ))


def not_found_body(table_id):
    return {"detail": {"error": "table_not_found", "message": table_not_found_message(table_id)}}


def schema_change(env, table, ops, who="alice"):
    current = fetch(env, table)
    return _run(service.apply_schema_change(
        ui(env, who), table["id"], ops,
        expected_schema_version=current["schema_version"],
    ))


def make_conversation(env, who, title):
    from db import conversation_store

    cid = str(uuid.uuid4())
    _run(conversation_store.create_conversation(
        uid(env, who), cid, datetime.now(timezone.utc), custom_name=title,
    ))
    return cid


def set_name(env, who, name):
    from db import user_store

    async def _go():
        from sqlalchemy import update

        from db.models import User
        async with user_store.AsyncSessionLocal() as db:
            await db.execute(update(User).where(User.id == uid(env, who)).values(name=name))
            await db.commit()

    _run(_go())


def changes_list(env, table, who="alice", **params):
    resp = client(env, who).get(f"/app/api/tables/{table['id']}/changes", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


HISTORY_ROUTES = [
    ("GET", "/app/api/tables/{id}/changes", None),
    ("GET", "/app/api/tables/{id}/changes/1", None),
    ("POST", "/app/api/tables/{id}/restore", {"change_id": 1}),
]
ALL_ROUTES = HISTORY_ROUTES + [
    ("GET", "/app/api/tables/{id}/download", None),
    ("GET", "/app/api/tables/{id}/download?include_history=maybe", None),
]


# ---------------------------------------------------------------------------
# Gate, 404 parity, readers
# ---------------------------------------------------------------------------


class TestAccess:
    @pytest.mark.parametrize("method,url,payload", ALL_ROUTES)
    def test_gate_first(self, env, method, url, payload, monkeypatch):
        table = make_table(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        monkeypatch.setattr(env.store, "get_table", boom)
        kwargs = {"json": payload} if payload is not None else {}
        resp = client(env).request(method, url.format(id=table["id"]), **kwargs)
        assert resp.status_code == 403
        assert detail(resp) == {"error": "tables_disabled", "message": tables_disabled_message()}

    @pytest.mark.parametrize("method,url,payload", ALL_ROUTES)
    def test_hidden_equals_missing(self, env, method, url, payload):
        table = make_table(env, "Secret")
        kwargs = {"json": payload} if payload is not None else {}
        bob = client(env, "bob")
        hidden = bob.request(method, url.format(id=table["id"]), **kwargs)
        assert hidden.status_code == 404
        assert hidden.json() == not_found_body(table["id"])
        random_id = str(uuid.uuid4())
        missing = bob.request(method, url.format(id=random_id), **kwargs)
        assert missing.status_code == 404
        assert missing.content == hidden.content.replace(
            table["id"].encode(), random_id.encode(),
        )
        malformed = bob.request(method, url.format(id="nope"), **kwargs)
        assert malformed.json() == not_found_body("nope")

    @pytest.mark.parametrize("method,url,payload", HISTORY_ROUTES)
    @pytest.mark.parametrize("share", [("bob", "read"), (None, "read")])
    def test_readers_get_403(self, env, method, url, payload, share):
        table = make_table(env, shares=[share])
        before = fetch(env, table)
        kwargs = {"json": payload} if payload is not None else {}
        resp = client(env, "bob").request(method, url.format(id=table["id"]), **kwargs)
        assert resp.status_code == 403
        assert detail(resp) == HISTORY_FORBIDDEN
        assert fetch(env, table)["schema_version"] == before["schema_version"]

    def test_reader_403_before_body_validation(self, env):
        table = make_table(env, shares=[("bob", "read")])
        c = client(env, "bob")
        resp = c.post(f"/app/api/tables/{table['id']}/restore", json={"change_id": "x"})
        assert resp.status_code == 403
        resp = c.get(f"/app/api/tables/{table['id']}/changes?limit=abc")
        assert resp.status_code == 403
        resp = c.get(f"/app/api/tables/{table['id']}/changes/zzz")
        assert resp.status_code == 403

    def test_write_share_recipient_is_editor(self, env):
        table = make_table(env, shares=[("bob", "write")])
        body = changes_list(env, table, "bob")
        assert [e["id"] for e in body["changes"]] == [1]


# ---------------------------------------------------------------------------
# Change list
# ---------------------------------------------------------------------------


class TestChangeList:
    def test_shape(self, env):
        table = make_table(env)
        insert(env, table, [{"Name": "A"}, {"Name": "B"}])
        body = changes_list(env, table)
        assert body["has_more"] is False and body["next_before_id"] is None
        newest, first = body["changes"]
        alice = uid(env, "alice")
        assert set(newest) == {
            "id", "created_at", "source", "source_label", "conversation", "actor",
            "kind", "op", "summary", "row_count", "in_effect", "restore_to",
            "schema_before", "schema_after", "size_bytes",
        }
        assert newest["id"] == 2 and newest["kind"] == "rows" and newest["op"] == "insert"
        assert newest["row_count"] == 2 and newest["in_effect"] is True
        assert newest["restore_to"] is None
        assert newest["source"] == f"ui:{alice}"
        assert newest["source_label"] == "you"
        assert newest["actor"] == {"user_id": alice, "name": "alice@example.com"}
        assert newest["conversation"] is None
        assert newest["schema_before"] == newest["schema_after"] == 1
        assert newest["size_bytes"] > 0
        assert first["id"] == 1 and first["summary"] == "Created table"
        assert first["kind"] == "schema" and first["row_count"] == 0

    def test_paging(self, env):
        table = make_table(env)
        for i in range(5):
            insert(env, table, [{"Name": f"r{i}"}])
        page = changes_list(env, table, limit=2)
        assert [e["id"] for e in page["changes"]] == [6, 5]
        assert page["has_more"] is True and page["next_before_id"] == 5
        page = changes_list(env, table, limit=2, before_id=5)
        assert [e["id"] for e in page["changes"]] == [4, 3]
        page = changes_list(env, table, limit=2, before_id=3)
        assert [e["id"] for e in page["changes"]] == [2, 1]
        assert page["has_more"] is False and page["next_before_id"] is None
        page = changes_list(env, table, limit=200)
        assert len(page["changes"]) == 6 and page["has_more"] is False

    @pytest.mark.parametrize("entries,has_more", [(4, True), (3, False)])
    def test_has_more_at_the_store_clamp(self, env, monkeypatch, entries, has_more):
        from chat.tables import history_routes, storage

        monkeypatch.setattr(history_routes, "CHANGES_MAX_LIMIT", 3)
        monkeypatch.setattr(storage, "_LIST_CHANGES_MAX", 3)
        table = make_table(env)
        for i in range(entries - 1):
            insert(env, table, [{"Name": f"r{i}"}])
        page = changes_list(env, table, limit=3)
        assert [e["id"] for e in page["changes"]] == [entries, entries - 1, entries - 2]
        assert page["has_more"] is has_more
        assert page["next_before_id"] == (2 if has_more else None)

    @pytest.mark.parametrize("params,code", [
        ({"limit": "0"}, "invalid_request"),
        ({"limit": "201"}, "invalid_request"),
        ({"limit": "abc"}, "invalid_request"),
        ({"before_id": "0"}, "invalid_cursor"),
        ({"before_id": "-3"}, "invalid_cursor"),
        ({"before_id": "x"}, "invalid_cursor"),
    ])
    def test_bad_params(self, env, params, code):
        table = make_table(env)
        resp = client(env).get(f"/app/api/tables/{table['id']}/changes", params=params)
        assert resp.status_code == 400
        assert detail(resp)["error"] == code

    def test_source_labels(self, env):
        alice, bob = uid(env, "alice"), uid(env, "bob")
        set_name(env, "bob", "Bob Builder")
        table = make_table(env, shares=[("bob", "write")])
        alice_convo = make_conversation(env, "alice", "Planning chat")
        bob_convo = make_conversation(env, "bob", "Bob's chat")
        dead = str(uuid.uuid4())
        writes = [
            f"conversation:{alice_convo}",   # 2
            f"conversation:{bob_convo}",     # 3
            f"conversation:{dead}",          # 4
            "action_request:42",             # 5
            f"ui:{bob}",                     # 6
        ]
        for i, source in enumerate(writes):
            insert(env, table, [{"Name": f"r{i}"}], write_source=source,
                   actor_user_id=bob if source == f"ui:{bob}" else alice)
        _run(service.apply_restore(ui(env), table["id"], 3, actor_user_id=alice))  # 7

        by_id = {e["id"]: e for e in changes_list(env, table)["changes"]}
        assert by_id[2]["source_label"] == "Planning chat"
        assert by_id[2]["conversation"]["id"] == alice_convo
        assert by_id[2]["conversation"]["title"] == "Planning chat"
        assert by_id[3]["source_label"] == "a conversation"  # bob's, alice can't open
        assert by_id[3]["conversation"] is None
        assert by_id[3]["source"] is None  # not even for the owner
        assert by_id[4]["source"] is None  # a dead conversation
        assert by_id[5]["source"] == "action_request:42"
        assert by_id[4]["source_label"] == "a conversation"
        assert by_id[5]["source_label"] == "Request #42"
        assert by_id[6]["source_label"] == "Bob Builder"
        assert by_id[6]["actor"] == {"user_id": bob, "name": "Bob Builder"}
        assert by_id[7]["source_label"] == "Restored to #3"
        assert by_id[7]["restore_to"] == 3 and by_id[7]["kind"] == "restore"
        assert by_id[1]["source_label"] == "you"
        assert by_id[2]["source"] == f"conversation:{alice_convo}"

        # The write-share recipient: the owner's conversation is opaque, its
        # own conversation is titled, its own UI write is "you", raw
        # sources are withheld.
        bob_view = {e["id"]: e for e in changes_list(env, table, "bob")["changes"]}
        assert bob_view[2]["source_label"] == "a conversation"
        assert bob_view[2]["conversation"] is None
        assert bob_view[3]["source_label"] == "Bob's chat"
        assert bob_view[3]["conversation"]["id"] == bob_convo
        assert bob_view[6]["source_label"] == "you"
        assert bob_view[5]["source_label"] == "Request #42"
        assert bob_view[1]["source_label"] == "alice@example.com"
        assert all(e["source"] is None for e in bob_view.values())

    def test_deleted_user_label(self, env, monkeypatch):
        table = make_table(env)
        carol = uid(env, "carol")
        insert(env, table, [{"Name": "x"}], write_source=f"ui:{carol}",
               actor_user_id=carol)
        from db import user_store

        async def _gone(_ids):
            return {}

        monkeypatch.setattr(user_store, "get_users_by_ids", _gone)
        entry = changes_list(env, table)["changes"][0]
        assert entry["source_label"] == "a user"
        assert entry["actor"] == {"user_id": carol, "name": "a user"}


# ---------------------------------------------------------------------------
# Change detail
# ---------------------------------------------------------------------------


ENUM_COLUMNS = [
    {"name": "Title", "type": "text"},
    {"name": "Count", "type": "number"},
    {"name": "Link", "type": "url"},
    {"name": "Done", "type": "boolean"},
    {"name": "Status", "type": "enum", "options": ["Open", "Done"]},
    {"name": "Tags", "type": "enum", "options": ["a", "b", "c"], "multiple": True},
]


def make_typed_table(env, title="Typed", shares=()):
    table = _run(service.create_table(ui(env), title=title, columns=ENUM_COLUMNS))
    for who, permission in shares:
        _run(env.store.add_share(table["id"], None if who is None else uid(env, who), permission))
    env.published.clear()
    return fetch(env, table)


def keys_by_name(table):
    return {c["name"]: c["key"] for c in table["schema"]}


class TestChangeDetail:
    def test_rows_change(self, env):
        table = make_typed_table(env)
        insert(env, table, [{"Title": "One", "Done": True, "Tags": ["b", "a"], "Count": 3}])
        _run(service.apply_row_write(
            ui(env), table["id"], "update",
            {"updates": [{"_id": 1, "Title": "Uno", "Done": False}]},
        ))
        k = keys_by_name(table)
        resp = client(env).get(f"/app/api/tables/{table['id']}/changes/3")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == 3 and body["op"] == "update" and body["in_effect"] is True
        assert [c["name"] for c in body["columns"]] == [c["name"] for c in ENUM_COLUMNS]
        assert all(c["current"] for c in body["columns"])
        tags = next(c for c in body["columns"] if c["name"] == "Tags")
        assert tags["multiple"] is True and tags["options"] == ["a", "b", "c"]
        assert len(body["before"]) == 1 and len(body["after"]) == 1
        before, after = body["before"][0], body["after"][0]
        assert before["_id"] == 1
        assert before[k["Title"]] == "One" and after[k["Title"]] == "Uno"
        assert before[k["Done"]] is True and after[k["Done"]] is False
        assert after[k["Tags"]] == ["a", "b"]
        assert body["truncated"] == {"before": False, "after": False}
        assert body["before_total"] == 1 and body["after_total"] == 1
        assert body["row_ids"] == [1] and body["row_ids_total"] == 1
        assert body["schema_diff"] is None and body["operations"] is None

    def test_schema_change(self, env):
        table = make_table(env)
        insert(env, table, [{"Name": "A", "SKU": "s1"}])
        schema_change(env, table, [
            {"op": "add_column", "column": {"name": "Status", "type": "text"}},
            {"op": "rename_column", "name": "Price", "new_name": "Cost"},
            {"op": "drop_column", "name": "SKU"},
        ])
        resp = client(env).get(f"/app/api/tables/{table['id']}/changes/3")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "schema"
        names = [c["name"] for c in body["columns"]]
        assert names == ["Name", "Cost", "Status", "SKU"]
        sku = next(c for c in body["columns"] if c["name"] == "SKU")
        assert sku["current"] is False
        statuses = {e["key"]: e["status"] for e in body["schema_diff"]["columns"]}
        assert sorted(statuses.values()) == ["added", "dropped", "renamed", "unchanged"]
        assert [op["op"] for op in body["operations"]] == [
            "add_column", "rename_column", "drop_column",
        ]
        assert body["before"]["dropped_values"] == {sku["key"]: {"1": "s1"}}
        assert "ddl_columns" not in body["before"] and "rewrites" not in body["before"]
        assert set(body["after"]) == {"columns", "title", "operations", "backfill"}
        assert body["before_total"] is None

    def test_backfill_values_are_logical(self, env):
        table = make_table(env)
        insert(env, table, [{"Name": "A"}])
        schema_change(env, table, [
            {"op": "add_column", "column": {"name": "Flag", "type": "boolean",
                                            "required": True, "default": True}},
            {"op": "add_column", "column": {"name": "Tags", "type": "enum",
                                            "options": ["a", "b"], "multiple": True,
                                            "required": True, "default": ["b"]}},
        ])
        body = client(env).get(f"/app/api/tables/{table['id']}/changes/3").json()
        keys = {c["name"]: c["key"] for c in body["columns"]}
        assert body["after"]["backfill"] == {
            keys["Flag"]: {"value": True}, keys["Tags"]: {"value": ["b"]},
        }

    def test_undecodable_multi_enum_cell_stays_raw(self):
        from chat.tables.history_routes import _logical
        from chat.tables.schema import Column

        tags = Column(key="c_t", name="Tags", type="enum", options=("a",), multiple=True)
        assert _logical(tags, '["a"]') == ["a"]
        assert _logical(tags, '"a"') == '"a"'
        assert _logical(tags, "not json") == "not json"
        assert _logical(tags, "[1]") == "[1]"

    def test_create_change(self, env):
        table = make_table(env)
        body = client(env).get(f"/app/api/tables/{table['id']}/changes/1").json()
        assert body["after"]["op"] == "create_table"
        assert body["before"] == {}
        assert all(e["status"] == "added" for e in body["schema_diff"]["columns"])
        assert body["operations"] is None

    def test_restore_change_and_dropped_key_label(self, env):
        table = make_table(env)
        insert(env, table, [{"Name": "A", "SKU": "s1"}])
        schema_change(env, table, [{"op": "drop_column", "name": "SKU"}])
        _run(service.apply_restore(ui(env), table["id"], 2))
        restore = client(env).get(f"/app/api/tables/{table['id']}/changes/4").json()
        assert restore["kind"] == "restore"
        assert restore["before"] == {} and restore["after"] == {"restore_to": 2}
        assert restore["restore_to"] == 2

        # Drop SKU again: the insert's images hold a key absent from the
        # current schema, labelled by key.
        schema_change(env, table, [{"op": "drop_column", "name": "SKU"}])
        sku_key = next(c["key"] for c in table["schema"] if c["name"] == "SKU")
        body = client(env).get(f"/app/api/tables/{table['id']}/changes/2").json()
        sku = next(c for c in body["columns"] if c["key"] == sku_key)
        assert sku == {"key": sku_key, "name": sku_key, "type": None, "multiple": False,
                       "options": None, "precision": None, "current": False}
        assert body["after"][0][sku_key] == "s1"
        assert body["in_effect"] is True

    def test_truncation(self, env):
        table = make_table(env)
        total = TABLE_CHANGE_DETAIL_MAX_ROWS + 7
        insert(env, table, [{"Name": f"r{i}"} for i in range(total)])
        body = client(env).get(f"/app/api/tables/{table['id']}/changes/2").json()
        assert len(body["after"]) == TABLE_CHANGE_DETAIL_MAX_ROWS
        assert body["truncated"] == {"before": False, "after": True}
        assert body["after_total"] == total and body["before"] == []
        assert body["row_count"] == total and body["row_ids_total"] == total

    def test_truncation_by_bytes(self, env, monkeypatch):
        from chat.tables import history_routes

        monkeypatch.setattr(history_routes, "TABLE_CHANGE_DETAIL_MAX_BYTES", 600)
        table = make_table(env)
        insert(env, table, [{"Name": "x" * 200} for _ in range(5)])
        body = client(env).get(f"/app/api/tables/{table['id']}/changes/2").json()
        assert 0 < len(body["after"]) < 5
        assert body["truncated"]["after"] is True

    @pytest.mark.parametrize("cid", ["99", "abc", "0", "-1", "1.5"])
    def test_unknown_change(self, env, cid):
        table = make_table(env)
        resp = client(env).get(f"/app/api/tables/{table['id']}/changes/{cid}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": {
            "error": "unknown_change", "message": f"There is no change #{cid}.",
        }}


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_round_trip_and_undo(self, env):
        table = make_table(env, shares=[("bob", "write")])
        insert(env, table, [{"Name": "A"}])                       # 2
        insert(env, table, [{"Name": "B"}, {"Name": "C"}])        # 3
        schema_change(env, table, [
            {"op": "add_column", "column": {"name": "Status", "type": "text"}},
        ])                                                        # 4
        assert fetch(env, table)["row_count"] == 3
        version = fetch(env, table)["schema_version"]
        env.published.clear()

        c = client(env, "bob")
        resp = c.post(f"/app/api/tables/{table['id']}/restore", json={"change_id": 2})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["row_count"] == 1 and body["restored_to"] == 2
        assert body["schema_version"] == version + 1
        assert body["change"]["id"] == 5 and body["change"]["kind"] == "restore"
        assert body["change"]["source_label"] == "Restored to #2"
        assert body["change"]["actor"]["user_id"] == uid(env, "bob")
        assert body["change"]["in_effect"] is True
        assert body["table"]["row_count"] == 1
        assert [col["name"] for col in body["table"]["schema"]] == ["Name", "Price", "SKU"]
        assert fetch(env, table)["schema_version"] == version + 1
        assert env.published  # the service published the write

        flags = {e["id"]: e["in_effect"] for e in changes_list(env, table)["changes"]}
        assert flags == {1: True, 2: True, 3: False, 4: False, 5: True}

        # Undo: restore to the entry just before the restore.
        resp = c.post(f"/app/api/tables/{table['id']}/restore", json={"change_id": 4})
        assert resp.status_code == 200, resp.text
        assert resp.json()["row_count"] == 3
        assert [col["name"] for col in resp.json()["table"]["schema"]] == [
            "Name", "Price", "SKU", "Status",
        ]
        flags = {e["id"]: e["in_effect"] for e in changes_list(env, table)["changes"]}
        assert flags == {1: True, 2: True, 3: True, 4: True, 5: False, 6: True}

    def test_entry_unreadable_after_restore_is_null(self, env, monkeypatch):
        table = make_table(env)
        insert(env, table, [{"Name": "A"}])

        async def revoked(*_a, **_k):
            raise service.TableForbidden(service.HISTORY_FORBIDDEN_MESSAGE)

        monkeypatch.setattr(service, "list_changes", revoked)
        resp = client(env).post(f"/app/api/tables/{table['id']}/restore", json={"change_id": 1})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["change"] is None
        assert body["row_count"] == 0 and body["restored_to"] == 1

    def test_denied_in_service_is_forbidden(self, env, monkeypatch):
        table = make_table(env, shares=[("bob", "write")])

        async def denied(*_a, **_k):
            raise service.TableDenied("Restoring needs edit access to this table.")

        monkeypatch.setattr(service, "apply_restore", denied)
        resp = client(env, "bob").post(
            f"/app/api/tables/{table['id']}/restore", json={"change_id": 1},
        )
        assert resp.status_code == 403
        assert detail(resp) == HISTORY_FORBIDDEN

    @pytest.mark.parametrize("payload", [{}, {"change_id": "2"}, {"change_id": True},
                                         {"change_id": 1.5}, {"change_id": None}])
    def test_bad_change_id(self, env, payload):
        table = make_table(env)
        resp = client(env).post(f"/app/api/tables/{table['id']}/restore", json=payload)
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_request"

    @pytest.mark.parametrize("change_id", [99, 0])
    def test_unknown_change(self, env, change_id):
        table = make_table(env)
        resp = client(env).post(
            f"/app/api/tables/{table['id']}/restore", json={"change_id": change_id},
        )
        assert resp.status_code == 404
        assert detail(resp) == {"error": "unknown_change",
                                "message": f"There is no change #{change_id}."}
        assert fetch(env, table)["schema_version"] == 1


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


class TestDownload:
    def test_csv_every_type(self, env):
        table = make_typed_table(env, title="Q3: report/final", shares=[("bob", "read")])
        insert(env, table, [
            {"Title": 'a, "quoted"\nline', "Count": 12.5, "Link": "https://x.test/a?b=1",
             "Done": True, "Status": "Open", "Tags": ["c", "a"]},
            {"Title": "=SUM(A1)", "Count": -3, "Done": False, "Tags": ["b"]},
            {"Title": None},
        ])
        resp = client(env, "bob").get(f"/app/api/tables/{table['id']}/download?format=csv")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"
        cd = resp.headers["content-disposition"]
        assert cd.startswith('attachment; filename="Q3- report-final.csv"')
        assert "filename*=UTF-8''Q3-%20report-final.csv" in cd
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8"), newline="")))
        assert rows[0] == ["Title", "Count", "Link", "Done", "Status", "Tags"]
        assert rows[1] == ['a, "quoted"\nline', "12.5", "https://x.test/a?b=1",
                           "true", "Open", "a; c"]
        assert rows[2] == ["'=SUM(A1)", "-3", "", "false", "", "b"]
        assert rows[3] == ["", "", "", "", "", ""]

    def test_default_format_is_csv(self, env):
        table = make_table(env)
        resp = client(env).get(f"/app/api/tables/{table['id']}/download")
        assert resp.status_code == 200
        assert resp.content.decode().splitlines()[0] == "Name,Price,SKU"

    def _tables(self, content, tmp_path):
        path = tmp_path / f"dl-{uuid.uuid4().hex}.sqlite"
        path.write_bytes(content)
        conn = sqlite3.connect(path)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            rows = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        finally:
            conn.close()
        return names, rows

    def test_sqlite_without_history_by_default(self, env, tmp_path):
        table = make_table(env, shares=[("bob", "read")])
        insert(env, table, [{"Name": "A"}, {"Name": "B"}])
        resp = client(env, "bob").get(f"/app/api/tables/{table['id']}/download?format=sqlite")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/vnd.sqlite3"
        assert 'filename="Inventory.sqlite"' in resp.headers["content-disposition"]
        names, rows = self._tables(resp.content, tmp_path)
        assert "rows" in names and "meta" in names and "changes" not in names
        assert rows == 2

    @pytest.mark.parametrize("who,share", [("alice", None), ("bob", ("bob", "write"))])
    def test_sqlite_with_history_for_editors(self, env, tmp_path, who, share):
        table = make_table(env, shares=[share] if share else [])
        insert(env, table, [{"Name": "A"}])
        resp = client(env, who).get(
            f"/app/api/tables/{table['id']}/download?format=sqlite&include_history=true",
        )
        assert resp.status_code == 200, resp.text
        names, _rows = self._tables(resp.content, tmp_path)
        assert "changes" in names

    def test_history_forbidden_for_reader(self, env):
        table = make_table(env, shares=[("bob", "read")])
        resp = client(env, "bob").get(
            f"/app/api/tables/{table['id']}/download?format=sqlite&include_history=true",
        )
        assert resp.status_code == 403
        assert detail(resp) == HISTORY_FORBIDDEN

    @pytest.mark.parametrize("raw,ok", [
        ("1", True), ("TRUE", True), ("0", False), ("false", False), ("", False),
        ("maybe", None), ("yes", None),
    ])
    def test_include_history_parsing(self, env, tmp_path, raw, ok):
        table = make_table(env)
        resp = client(env).get(
            f"/app/api/tables/{table['id']}/download?format=sqlite&include_history={raw}",
        )
        if ok is None:
            assert resp.status_code == 400
            assert detail(resp)["error"] == "invalid_request"
            return
        assert resp.status_code == 200
        names, _rows = self._tables(resp.content, tmp_path)
        assert ("changes" in names) is ok

    def test_bad_requests(self, env):
        table = make_table(env)
        c = client(env)
        resp = c.get(f"/app/api/tables/{table['id']}/download?format=xlsx")
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_format"
        resp = c.get(f"/app/api/tables/{table['id']}/download?format=csv&include_history=true")
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_request"

    def _pin_mkdtemp(self, monkeypatch, tmp_path):
        import tempfile

        made = []

        def fake_mkdtemp(prefix=None, **_kw):
            assert prefix == "quest-table-dl-"
            path = tmp_path / f"{prefix}{len(made)}"
            path.mkdir()
            made.append(path)
            return str(path)

        monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
        return made

    @pytest.mark.parametrize("fmt", ["csv", "sqlite"])
    def test_temp_dir_removed_after_response(self, env, monkeypatch, tmp_path, fmt):
        made = self._pin_mkdtemp(monkeypatch, tmp_path)
        table = make_table(env)
        resp = client(env).get(f"/app/api/tables/{table['id']}/download?format={fmt}")
        assert resp.status_code == 200
        assert len(made) == 1 and not made[0].exists()

    def test_temp_dir_removed_on_export_error(self, env, monkeypatch, tmp_path):
        made = self._pin_mkdtemp(monkeypatch, tmp_path)
        table = make_table(env)

        async def broken(*_a, dest_dir, **_k):
            (dest_dir / "partial").write_text("x")
            raise service.TableError("busy", "The table is busy.")

        monkeypatch.setattr(service, "export_table", broken)
        resp = client(env).get(f"/app/api/tables/{table['id']}/download?format=csv")
        assert resp.status_code == 503
        assert detail(resp)["error"] == "busy"
        assert len(made) == 1 and not made[0].exists()

    def test_temp_dir_removed_on_unexpected_error(self, env, monkeypatch, tmp_path):
        made = self._pin_mkdtemp(monkeypatch, tmp_path)
        table = make_table(env)

        async def broken(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(service, "export_table", broken)
        c = TestClient(_app(env), raise_server_exceptions=False)
        resp = c.get(f"/app/api/tables/{table['id']}/download?format=csv")
        assert resp.status_code == 500
        assert len(made) == 1 and not made[0].exists()
