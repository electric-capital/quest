"""Quest Tables HTTP routes (chat/tables/routes.py, phase A15).

Runs the router in a bare FastAPI app with the auth dependency overridden
(tests/test_docs_routes.py pattern) on the isolated ``env`` of
tests/test_table_service.py (tmp SQLite with foreign keys on, tmp
TABLES_DIR, tables gate open, realtime events captured). Users: alice (owns
the two projects), bob, carol.

Covers: auth, the gate (403 before any DB work), 404 parity on every
per-table route, create / list / get, keyset paging, project and shared
streams, metadata edits (``stale_update``, ``duplicate_title``), owner-only
refusals, schema changes (``stale_schema``, ``rows_violate``, op_index),
rows paging and filter/sort parsing, access flags, ``last_write_user``,
delete (204, directory gone, events, waits for the table lock).
"""

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from chat.tables import service
from chat.tables.constants import table_not_found_message, tables_disabled_message
from tests.test_table_service import (  # noqa: F401  (env is a fixture)
    COLUMNS,
    _run,
    env,
    fetch,
    make_table,
    table_dir,
    ui,
    uid,
)


def _app(env, who="alice"):
    from chat.tables import routes

    app = FastAPI()
    app.include_router(routes.router)
    if who is not None:
        user = env.users[who]

        async def _current():
            return user

        app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return app


def client(env, who="alice"):
    return TestClient(_app(env, who))


def detail(resp):
    return resp.json()["detail"]


def insert(env, table, rows, who="alice"):
    return _run(service.apply_row_write(ui(env, who), table["id"], "insert", {"rows": rows}))


def not_found_body(table_id):
    return {"detail": {"error": "table_not_found", "message": table_not_found_message(table_id)}}


# ---------------------------------------------------------------------------
# Auth and gate
# ---------------------------------------------------------------------------


class TestAuthAndGate:
    def test_anonymous_401(self, env):
        resp = client(env, None).get("/app/api/tables")
        assert resp.status_code == 401

    def test_every_route_403s_before_db_work(self, env, monkeypatch):
        table = make_table(env)
        env.fg.set_feature_enabled(env.fg.FEATURE_TABLES, False)

        async def boom(*_a, **_k):
            raise AssertionError("DB touched with the gate closed")

        from db import project_store
        monkeypatch.setattr(env.store, "get_table", boom)
        monkeypatch.setattr(env.store, "list_accessible_tables", boom)
        monkeypatch.setattr(env.store, "list_tables_shared_with", boom)
        monkeypatch.setattr(project_store, "get_project", boom)
        c = client(env)
        t = table["id"]
        calls = [
            ("get", "/app/api/tables", None),
            ("get", f"/app/api/tables?project_id={env.private_project}", None),
            ("get", "/app/api/tables?shared=true", None),
            ("post", "/app/api/tables", {"title": "New", "columns": COLUMNS,
                                         "project_id": env.private_project}),
            ("get", f"/app/api/tables/{t}", None),
            ("put", f"/app/api/tables/{t}", {"title": "Renamed"}),
            ("put", f"/app/api/tables/{t}/schema",
             {"operations": [], "expected_schema_version": 1}),
            ("get", f"/app/api/tables/{t}/rows", None),
            ("delete", f"/app/api/tables/{t}", None),
        ]
        for method, url, payload in calls:
            kwargs = {"json": payload} if payload is not None else {}
            resp = c.request(method.upper(), url, **kwargs)
            assert resp.status_code == 403, (method, url)
            assert detail(resp) == {
                "error": "tables_disabled", "message": tables_disabled_message(),
            }
        assert env.published == []
        assert table_dir(env, table).exists()


# ---------------------------------------------------------------------------
# 404 parity
# ---------------------------------------------------------------------------


class TestNotFoundParity:
    ROUTES = [
        ("GET", "/app/api/tables/{id}", None),
        ("PUT", "/app/api/tables/{id}", {"title": "X"}),
        ("PUT", "/app/api/tables/{id}/schema",
         {"operations": [{"op": "rename_table", "title": "X"}], "expected_schema_version": 1}),
        ("GET", "/app/api/tables/{id}/rows", None),
        ("DELETE", "/app/api/tables/{id}", None),
    ]

    @pytest.mark.parametrize("method,url,payload", ROUTES)
    def test_hidden_equals_missing_equals_malformed(self, env, method, url, payload):
        table = make_table(env, "Secret")
        bob = client(env, "bob")
        kwargs = {"json": payload} if payload is not None else {}

        hidden = bob.request(method, url.format(id=table["id"]), **kwargs)
        assert hidden.status_code == 404
        assert hidden.json() == not_found_body(table["id"])
        assert b"Secret" not in hidden.content

        random_id = str(uuid.uuid4())
        missing = bob.request(method, url.format(id=random_id), **kwargs)
        assert missing.status_code == 404
        assert missing.content == hidden.content.replace(
            table["id"].encode(), random_id.encode(),
        )

        malformed = bob.request(method, url.format(id="not-a-uuid"), **kwargs)
        assert malformed.status_code == 404
        assert malformed.json() == not_found_body("not-a-uuid")

        blank = bob.request(method, url.format(id="%20"), **kwargs)
        assert blank.status_code == 404
        assert blank.json() == not_found_body(" ")

        # The same id once it really is gone: byte-identical.
        _run(env.store.delete_table(table["id"]))
        gone = bob.request(method, url.format(id=table["id"]), **kwargs)
        assert gone.status_code == 404
        assert gone.content == hidden.content
        assert env.published == []


# ---------------------------------------------------------------------------
# Create, list, get
# ---------------------------------------------------------------------------


OWNER_ACCESS = {
    "can_read": True, "rows": "free", "schema": "approval", "can_edit": True,
    "can_restore": True, "can_rename": True, "can_delete": True, "can_share": True,
    "can_edit_schema": True, "can_require_approval": True,
    "can_download_history": True, "deny_reason": None, "schema_deny_reason": None,
}


class TestCreateListGet:
    def test_round_trip(self, env):
        c = client(env)
        resp = c.post("/app/api/tables", json={
            "title": "Inventory", "description": "Stock", "columns": COLUMNS,
        })
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["title"] == "Inventory"
        assert created["description"] == "Stock"
        assert created["mode"] == "private" and created["scope"] == "user"
        assert created["column_count"] == 3
        assert [c_["name"] for c_ in created["schema"]] == ["Name", "Price", "SKU"]
        assert all(c_["key"].startswith("c_") for c_ in created["schema"])
        assert created["last_write_source"] == f"ui:{uid(env, 'alice')}"
        assert created["last_write_user"] == {
            "id": uid(env, "alice"), "name": "", "email": "alice@example.com",
        }
        assert created["access"] == OWNER_ACCESS
        assert created["shares"] == [] and created["owner"] is None
        assert created["shared"] is False and created["shared_with_me"] is False
        assert table_dir(env, created).is_dir()

        listed = c.get("/app/api/tables").json()
        assert listed["has_more"] is False and listed["next_cursor"] is None
        [row] = listed["tables"]
        assert "schema" not in row
        assert row == {k: v for k, v in created.items() if k != "schema"}

        got = c.get(f"/app/api/tables/{created['id']}").json()
        assert got["schema"] == created["schema"]
        assert got["last_write_conversation"] is None
        assert {k: v for k, v in got.items() if k != "last_write_conversation"} == created

    def test_create_errors(self, env):
        c = client(env)
        make_table(env, "Taken")
        resp = c.post("/app/api/tables", json={"title": "taken", "columns": COLUMNS})
        assert resp.status_code == 409 and detail(resp)["error"] == "duplicate_title"

        resp = c.post("/app/api/tables", json={"title": "X", "columns": []})
        assert resp.status_code == 400 and detail(resp)["error"] == "no_columns"

        resp = c.post("/app/api/tables", json={
            "title": "X", "columns": [{"name": "A", "type": "nope"}],
        })
        assert resp.status_code == 400
        assert detail(resp)["column"] == "A"

        resp = c.post("/app/api/tables", json={"title": "  ", "columns": COLUMNS})
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_request"

        resp = c.post("/app/api/tables", json={"title": 5, "columns": COLUMNS})
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_request"

        for bad in (0, False, [], {}):
            resp = c.post("/app/api/tables", json={
                "title": "X", "columns": COLUMNS, "project_id": bad,
            })
            assert resp.status_code == 400, bad
            assert detail(resp)["error"] == "invalid_request"
        # "" means no project: a user table.
        resp = c.post("/app/api/tables", json={
            "title": "Blank project", "columns": COLUMNS, "project_id": "",
        })
        assert resp.status_code == 201 and resp.json()["scope"] == "user"

        resp = client(env, "bob").post("/app/api/tables", json={
            "title": "X", "columns": COLUMNS, "project_id": env.private_project,
        })
        assert resp.status_code == 404 and detail(resp)["error"] == "project_not_found"

    def test_table_limit_400(self, env, monkeypatch):
        async def full(*_a, **_k):
            raise env.store.TableLimitError("Too many tables.")

        monkeypatch.setattr(env.store, "create_table", full)
        resp = client(env).post("/app/api/tables", json={"title": "X", "columns": COLUMNS})
        assert resp.status_code == 400 and detail(resp)["error"] == "table_limit"
        assert list(env.dirs["tables"].iterdir()) == []

    def test_paging_three_pages(self, env):
        for i in range(5):
            make_table(env, f"T{i}")
        c = client(env)
        full = [r["id"] for r in c.get("/app/api/tables").json()["tables"]]
        assert len(full) == 5
        seen, cursor, pages = [], None, 0
        while True:
            url = "/app/api/tables?limit=2" + (f"&cursor={cursor}" if cursor else "")
            data = c.get(url).json()
            pages += 1
            seen += [r["id"] for r in data["tables"]]
            if not data["has_more"]:
                assert data["next_cursor"] is None
                break
            cursor = data["next_cursor"]
            # Stable: the same cursor gives the same page.
            assert c.get(url).json() == data
        assert pages == 3
        assert seen == full and len(set(seen)) == 5

    @pytest.mark.parametrize("cursor", ["garbage", "notadate|x", "2026-01-01T00:00:00|"])
    def test_invalid_cursor(self, env, cursor):
        resp = client(env).get(f"/app/api/tables?limit=2&cursor={cursor}")
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_cursor"

    def test_limit_bounds(self, env):
        c = client(env)
        assert c.get("/app/api/tables?limit=0").status_code == 422
        assert c.get("/app/api/tables?limit=201").status_code == 422

    def test_project_stream(self, env):
        user_t = make_table(env, "Mine")
        proj = client(env).post("/app/api/tables", json={
            "title": "Proj", "columns": COLUMNS, "project_id": env.public_project,
        }).json()
        assert proj["mode"] == "public" and proj["scope"] == "project"
        c = client(env)
        assert [r["id"] for r in c.get("/app/api/tables").json()["tables"]] == [user_t["id"]]
        data = c.get(f"/app/api/tables?project_id={env.public_project}").json()
        assert [r["id"] for r in data["tables"]] == [proj["id"]]
        resp = client(env, "bob").get(f"/app/api/tables?project_id={env.public_project}")
        assert resp.status_code == 404 and detail(resp)["error"] == "project_not_found"

    def test_shared_stream(self, env):
        read_t = make_table(env, "R", shares=[("bob", "read")])
        write_t = make_table(env, "W", shares=[("bob", "write")])
        every_t = make_table(env, "E", shares=[(None, "read")])
        make_table(env, "Private")
        bob = client(env, "bob")
        assert bob.get("/app/api/tables").json()["tables"] == []
        rows = bob.get("/app/api/tables?shared=true").json()["tables"]
        assert {r["id"]: r["permission"] for r in rows} == {
            read_t["id"]: "read", write_t["id"]: "write", every_t["id"]: "read",
        }
        for row in rows:
            assert row["shared_with_me"] is True
            assert row["shares"] is None and row["last_write_source"] is None
            assert row["owner"] == {"id": uid(env, "alice"), "name": "",
                                    "email": "alice@example.com"}
        # Two pages of the shared stream, no duplicates.
        first = bob.get("/app/api/tables?shared=true&limit=2").json()
        assert len(first["tables"]) == 2 and first["has_more"] is True
        second = bob.get(
            f"/app/api/tables?shared=true&limit=2&cursor={first['next_cursor']}"
        ).json()
        assert second["has_more"] is False and second["next_cursor"] is None
        paged = [r["id"] for r in first["tables"] + second["tables"]]
        assert paged == [r["id"] for r in rows]
        # Alice's own shared stream is empty (she owns them).
        assert client(env).get("/app/api/tables?shared=true").json()["tables"] == []
        resp = bob.get(f"/app/api/tables?shared=true&project_id={env.private_project}")
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Access flags and last_write_user
# ---------------------------------------------------------------------------


class TestAccessFlags:
    def test_owner_read_and_write_share(self, env):
        table = make_table(env, shares=[("bob", "read"), ("carol", "write")])
        url = f"/app/api/tables/{table['id']}"
        owner = client(env).get(url).json()
        assert owner["access"] == OWNER_ACCESS
        assert [s["user"]["email"] for s in owner["shares"]] == [
            "bob@example.com", "carol@example.com",
        ]

        reader = client(env, "bob").get(url).json()["access"]
        assert reader["rows"] == "denied" and reader["schema"] == "denied"
        assert reader["can_read"] is True
        assert not any(reader[k] for k in (
            "can_edit", "can_restore", "can_rename", "can_delete", "can_share",
            "can_edit_schema", "can_require_approval", "can_download_history",
        ))
        assert reader["deny_reason"]

        writer = client(env, "carol").get(url).json()["access"]
        assert writer["rows"] == "free" and writer["schema"] == "denied"
        assert writer["can_edit"] and writer["can_restore"] and writer["can_download_history"]
        assert not any(writer[k] for k in (
            "can_rename", "can_delete", "can_share", "can_edit_schema",
            "can_require_approval",
        ))
        assert writer["deny_reason"] is None and writer["schema_deny_reason"]

    def test_last_write_user_ui_writer_and_card(self, env):
        from db import action_request_store

        table = make_table(env, shares=[("carol", "write")])
        insert(env, table, [{"Name": "a"}], who="carol")
        row = client(env).get(f"/app/api/tables/{table['id']}").json()
        assert row["last_write_source"] == f"ui:{uid(env, 'carol')}"
        assert row["last_write_user"]["email"] == "carol@example.com"
        # Carol sees neither the source nor the writer.
        theirs = client(env, "carol").get(f"/app/api/tables/{table['id']}").json()
        assert theirs["last_write_source"] is None and theirs["last_write_user"] is None

        # An approved write_table_rows card proposed by carol.
        req = _run(action_request_store.create_action_request(
            uid(env, "carol"), str(uuid.uuid4()), "write_table_rows", {}, "r",
        ))
        _run(env.store.update_after_write(
            table["id"], row_count=1, last_write_source=f"action_request:{req['id']}",
        ))
        row = client(env).get(f"/app/api/tables/{table['id']}").json()
        assert row["last_write_user"]["email"] == "carol@example.com"

        # Alice's own card keeps the plain source wording.
        own = _run(action_request_store.create_action_request(
            uid(env, "alice"), str(uuid.uuid4()), "write_table_rows", {}, "r",
        ))
        _run(env.store.update_after_write(
            table["id"], row_count=1, last_write_source=f"action_request:{own['id']}",
        ))
        assert client(env).get(f"/app/api/tables/{table['id']}").json()[
            "last_write_user"] is None


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_owner_updates(self, env):
        table = make_table(env)
        resp = client(env).put(f"/app/api/tables/{table['id']}", json={
            "title": "Renamed", "description": "D", "require_approval": True,
            "expected_updated_at": table["updated_at"],
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["title"] == "Renamed" and data["description"] == "D"
        assert data["require_approval"] is True
        assert "schema" in data
        assert fetch(env, table)["title"] == "Renamed"
        assert any(ev["type"] == "table_changed" for _u, ev in env.published)

    def test_stale_update_flat(self, env):
        table = make_table(env, shares=[("bob", "read")])
        c = client(env)
        first = c.put(f"/app/api/tables/{table['id']}", json={"title": "First"}).json()
        resp = c.put(f"/app/api/tables/{table['id']}", json={
            "title": "Second", "expected_updated_at": table["updated_at"],
        })
        assert resp.status_code == 409
        data = resp.json()
        assert "detail" not in data
        assert data["error"] == "stale_update"
        assert data["message"] == service.StaleUpdate({}).message
        assert data["current"]["title"] == "First"
        assert data["current"]["updated_at"] == first["updated_at"]
        assert data["current"]["access"]["can_rename"] is True
        assert [s["user_id"] for s in data["current"]["shares"]] == [uid(env, "bob")]
        assert fetch(env, table)["title"] == "First"

    def test_duplicate_title(self, env):
        make_table(env, "Taken")
        table = make_table(env, "Other")
        resp = client(env).put(f"/app/api/tables/{table['id']}", json={"title": "TAKEN"})
        assert resp.status_code == 409 and detail(resp)["error"] == "duplicate_title"

    def test_non_owner_ill_typed_body_422(self, env):
        # FastAPI validates the typed body before the handler's own checks.
        table = make_table(env, shares=[("bob", "read")])
        resp = client(env, "bob").put(f"/app/api/tables/{table['id']}",
                                      json={"require_approval": "yes"})
        assert resp.status_code == 422

    def test_strict_bool(self, env):
        table = make_table(env)
        resp = client(env).put(f"/app/api/tables/{table['id']}",
                               json={"require_approval": "true"})
        assert resp.status_code == 422
        assert fetch(env, table)["require_approval"] is False

    def test_invalid_title_400(self, env):
        table = make_table(env)
        resp = client(env).put(f"/app/api/tables/{table['id']}", json={"title": "x" * 201})
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_request"


class TestOwnerOnly:
    @pytest.mark.parametrize("who", ["bob", "carol"])
    def test_non_owner_403(self, env, who):
        table = make_table(env, shares=[("bob", "read"), ("carol", "write")])
        c = client(env, who)
        t = table["id"]
        for method, url, payload in [
            ("PUT", f"/app/api/tables/{t}", {"title": "X"}),
            # Even an invalid body: forbidden is decided first.
            ("PUT", f"/app/api/tables/{t}/schema", {"operations": "garbage"}),
            ("PUT", f"/app/api/tables/{t}/schema", None),
            ("DELETE", f"/app/api/tables/{t}", None),
        ]:
            kwargs = {"json": payload} if payload is not None else {}
            resp = c.request(method, url, **kwargs)
            assert resp.status_code == 403, (method, url)
            assert detail(resp)["error"] == "forbidden"
        assert fetch(env, table)["title"] == table["title"]
        assert table_dir(env, table).exists()
        assert env.published == []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def url(self, table):
        return f"/app/api/tables/{table['id']}/schema"

    def test_apply(self, env):
        table = make_table(env)
        resp = client(env).put(self.url(table), json={
            "operations": [
                {"op": "add_column", "column": {"name": "Status", "type": "text"}},
                {"op": "rename_column", "name": "Price", "new_name": "Cost"},
            ],
            "expected_schema_version": 1,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["schema_version"] == 2
        assert [c_["name"] for c_ in data["schema"]] == ["Name", "Cost", "SKU", "Status"]
        assert data["column_count"] == 4
        assert isinstance(data["change_id"], int) and data["change_id"] >= 2
        assert data["last_write_source"] == f"ui:{uid(env, 'alice')}"

    def test_stale_schema(self, env):
        table = make_table(env)
        c = client(env)
        ok = c.put(self.url(table), json={
            "operations": [{"op": "rename_column", "name": "Price", "new_name": "Cost"}],
            "expected_schema_version": 1,
        })
        assert ok.status_code == 200
        resp = c.put(self.url(table), json={
            "operations": [{"op": "rename_column", "name": "SKU", "new_name": "Code"}],
            "expected_schema_version": 1,
        })
        assert resp.status_code == 409
        d = detail(resp)
        assert d["error"] == "stale_schema" and d["schema_version"] == 2
        assert [c_["name"] for c_ in d["schema"]] == ["Name", "Cost", "SKU"]

    def test_rows_violate(self, env):
        table = make_table(env)
        inserted = insert(env, table, [{"Name": "a"}, {"Name": "b", "Price": 1}, {"Name": "c"}])
        ids = inserted["inserted_ids"]
        resp = client(env).put(self.url(table), json={
            "operations": [{"op": "set_required", "name": "Price", "required": True}],
            "expected_schema_version": 1,
        })
        assert resp.status_code == 400
        d = detail(resp)
        assert d["error"] == "rows_violate"
        assert d["column"] == "Price" and d["check"] == "required"
        assert d["row_ids"] == [ids[0], ids[2]] and d["total"] == 2
        assert fetch(env, table)["schema_version"] == 1

    def test_operation_error_op_index(self, env):
        table = make_table(env)
        resp = client(env).put(self.url(table), json={
            "operations": [
                {"op": "rename_column", "name": "Price", "new_name": "Cost"},
                {"op": "drop_column", "name": "Nope"},
            ],
            "expected_schema_version": 1,
        })
        assert resp.status_code == 400
        d = detail(resp)
        assert d["op_index"] == 1 and d["op"] == "drop_column"
        assert fetch(env, table)["schema_version"] == 1

    def test_bad_version_and_duplicate_title(self, env):
        make_table(env, "Taken")
        table = make_table(env, "Other")
        c = client(env)
        resp = c.put(self.url(table), json={"operations": [], "expected_schema_version": "1"})
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_args"
        resp = c.put(self.url(table), json={
            "operations": [{"op": "rename_table", "title": "taken"}],
            "expected_schema_version": 1,
        })
        assert resp.status_code == 409 and detail(resp)["error"] == "duplicate_title"


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


class TestRows:
    def url(self, table, query=""):
        return f"/app/api/tables/{table['id']}/rows" + (f"?{query}" if query else "")

    def seed(self, env, n=5, **kw):
        table = make_table(env, **kw)
        insert(env, table, [{"Name": f"n{i}", "Price": i, "SKU": f"s{i}"} for i in range(n)])
        return table, {c_["name"]: c_["key"] for c_ in fetch(env, table)["schema"]}

    def test_paging_and_keys(self, env):
        table, keys = self.seed(env)
        c = client(env)
        data = c.get(self.url(table, "limit=2&offset=1")).json()
        assert data["total"] == 5 and data["limit"] == 2 and data["offset"] == 1
        assert data["truncated"] is True and data["schema_version"] == 1
        assert [r[keys["Name"]] for r in data["rows"]] == ["n1", "n2"]
        row = data["rows"][0]
        assert set(row) == {"_id", "_updated_at", *keys.values()}
        assert row[keys["Price"]] == "1"
        last = c.get(self.url(table, "limit=2&offset=4")).json()
        assert len(last["rows"]) == 1 and last["truncated"] is False

        default = c.get(self.url(table)).json()
        assert default["limit"] == 100 and len(default["rows"]) == 5

    def test_filter_sort(self, env):
        table, keys = self.seed(env)
        c = client(env)
        f = '{"column":"Price","op":"gte","value":3}'
        s = '[{"column":"Price","direction":"desc"}]'
        data = c.get(self.url(table), params={"filter": f, "sort": s}).json()
        assert [r[keys["Name"]] for r in data["rows"]] == ["n4", "n3"]
        assert data["total"] == 2

    def test_bad_params(self, env):
        table, _keys = self.seed(env)
        c = client(env)
        resp = c.get(self.url(table), params={"filter": "{nope"})
        assert resp.status_code == 400
        assert detail(resp) == {"error": "invalid_filter",
                                "message": "filter is not valid JSON.", "path": "filter"}
        resp = c.get(self.url(table), params={"sort": "["})
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_sort"
        assert detail(resp)["path"] == "sort"

        resp = c.get(self.url(table), params={"filter": '{"column":"Nope","op":"eq","value":1}'})
        assert resp.status_code == 400
        d = detail(resp)
        assert d["error"] == "unknown_column" and set(d) == {"error", "message", "path"}

        resp = c.get(self.url(table, "limit=201"))
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_limit"
        resp = c.get(self.url(table, "limit=abc"))
        assert resp.status_code == 400 and detail(resp)["path"] == "limit"
        for raw in ("１", "1_0", " 7", "1.5", "+3"):
            resp = c.get(self.url(table), params={"limit": raw})
            assert resp.status_code == 400, raw
            assert detail(resp)["error"] == "invalid_limit"
        resp = c.get(self.url(table, "offset=-1"))
        assert resp.status_code == 400 and detail(resp)["error"] == "invalid_offset"
        assert c.get(self.url(table, "limit=200")).status_code == 200

    def test_count_only(self, env):
        table, _keys = self.seed(env)
        data = client(env).get(self.url(table, "count_only=true")).json()
        assert data["rows"] == [] and data["total"] == 5 and data["truncated"] is False

    def test_count_only_with_filter(self, env):
        table, _keys = self.seed(env)
        data = client(env).get(self.url(table), params={
            "count_only": "true", "filter": '{"column":"Price","op":"lt","value":2}',
        }).json()
        assert data["rows"] == [] and data["total"] == 2

    def test_keys_stable_after_rename(self, env):
        table, keys = self.seed(env)
        c = client(env)
        resp = c.put(f"/app/api/tables/{table['id']}/schema", json={
            "operations": [{"op": "rename_column", "name": "Price", "new_name": "Cost"}],
            "expected_schema_version": 1,
        })
        assert resp.status_code == 200
        data = c.get(self.url(table, "limit=1")).json()
        assert data["schema_version"] == 2
        row = data["rows"][0]
        assert set(row) == {"_id", "_updated_at", *keys.values()}
        assert row[keys["Price"]] == "0"
        assert "Cost" not in row and "Price" not in row

    def test_readers_allowed(self, env):
        table, keys = self.seed(env, shares=[("bob", "read")])
        data = client(env, "bob").get(self.url(table)).json()
        assert data["total"] == 5 and keys["Name"] in data["rows"][0]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_owner_deletes(self, env):
        table = make_table(env, shares=[("bob", "read")])
        c = client(env)
        resp = c.delete(f"/app/api/tables/{table['id']}")
        assert resp.status_code == 204 and resp.content == b""
        assert fetch(env, table) is None
        assert not table_dir(env, table).exists()
        types = {(u, ev["type"]) for u, ev in env.published}
        for who in ("alice", "bob"):
            assert (uid(env, who), "table_changed") in types
            assert (uid(env, who), "table_list_changed") in types
        resp = c.get(f"/app/api/tables/{table['id']}")
        assert resp.status_code == 404
        assert resp.json() == not_found_body(table["id"])

    def test_delete_waits_for_table_lock(self, env):
        table = make_table(env)
        app = _app(env)

        async def scenario():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                lock = service._table_lock(table["id"])
                await lock.acquire()
                task = asyncio.create_task(ac.delete(f"/app/api/tables/{table['id']}"))
                for _ in range(20):
                    await asyncio.sleep(0.02)
                assert not task.done()
                assert table_dir(env, table).exists()
                assert await env.store.get_table(table["id"]) is not None
                lock.release()
                resp = await asyncio.wait_for(task, 10)
                return resp

        resp = asyncio.run(scenario())
        assert resp.status_code == 204
        assert fetch(env, table) is None
        assert not table_dir(env, table).exists()


def test_raise_table_error_statuses(caplog):
    import logging

    from chat.tables import routes, storage
    from chat.tables.revert import UnknownChangeError

    cases = [
        (service.TableDisabled(), 403),
        (service.TableForbidden("no"), 403),
        (service.TableDenied("no"), 403),
        (service.TableError("project_not_found", "x"), 404),
        (UnknownChangeError(3), 404),
        (service.TableError("share_not_found", "x"), 404),
        (service.TableError("user_not_found", "x"), 404),
        (service.TableError("duplicate_title", "x"), 409),
        (storage.TableStorageError("busy", "x"), 503),
        (storage.TableStorageError("corrupt_storage", "x"), 500),
        (storage.SchemaMismatchError("x"), 409),
        (service.TableError("table_limit", "x"), 400),
        (storage.ChangeTooLargeError(10, 5), 400),
    ]
    for exc, status in cases:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="chat.tables.routes"):
            with pytest.raises(HTTPException) as info:
                routes._raise_table_error(exc, "tid")
        assert info.value.status_code == status, exc
        assert info.value.detail["error"] == exc.code
        logged = [r.getMessage() for r in caplog.records if "[tables]" in r.getMessage()]
        if status == 500:
            assert logged == [f"[tables] storage error on tid: {exc.code}"]
        elif isinstance(exc, storage.TableStorageError):
            # A file-layer refusal (busy, schema_mismatch, ...) leaves a line.
            assert logged == [f"[tables] storage refusal on tid: {exc.code} -> {status} ({exc})"]
        else:
            assert logged == []
    with pytest.raises(HTTPException) as info:
        routes._raise_table_error(service.TableNotFound("other"), "tid")
    assert info.value.status_code == 404
    assert info.value.detail == {"error": "table_not_found",
                                 "message": table_not_found_message("tid")}
    with pytest.raises(KeyError):
        routes._raise_table_error(KeyError("x"), "tid")
