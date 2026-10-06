"""Quest Docs HTTP routes (chat/docs/routes.py) and the project-delete sweep.

Runs the router in a bare FastAPI app with the auth dependency overridden
(tests/test_project_skill_autoload_access.py pattern), on the isolated
``docs_env`` from tests/test_docs_service.py (tmp SQLite with foreign keys
on, tmp DOCS_DIR/CHATS_DIR/PROJECTS_DIR, docs gate open, realtime events
captured). Users: alice (owns the three projects), bob, carol.

Covers: the gate (every route 403 ``docs_disabled``), listing (user vs
project docs, order, keyset paging, shares), create, get (hidden == missing
byte for byte, the ``assets`` list), rename (409 ``stale_update`` shape),
the refused mode switch, user docs always private whatever the
``public_projects`` gate says (closed in the fixture by default), assets,
downloads (md + zip, symlink refusal), delete, and the project delete
route removing the project's doc directories.
"""

import io
import os
import uuid
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.docs import files
from chat.docs.constants import (
    doc_not_found_message,
    docs_disabled_message,
    user_doc_mode_private_message,
)
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    _run,
    body,
    docs_env,
    event_types,
    make_legacy_public,
    seed_doc,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 48

USER_DOC_PRIVATE = {
    "error": "user_doc_mode_private",
    "message": (
        "User docs are always private. Create the doc inside a public "
        "project to make it public."
    ),
}
PROJECT_MODE_INHERITED = {
    "error": "project_doc_mode_inherited",
    "message": "Project docs take their project's mode; it cannot be switched.",
}

NO_SWITCH_ACCESS = {"can_rename": True, "can_switch_mode": False, "can_delete": True}
# The owner's full access block (Phase 3 sharing / editing flags).
OWNER_ACCESS = {
    **NO_SWITCH_ACCESS, "can_edit": True, "can_share": True,
    "can_delete_assets": True, "write": "free",
}
# The owner-view row fields Phase 3 added (no recipient names to resolve).
OWNER_ROW_EXTRAS = {
    "shared_with_me": False, "permission": None, "owner": None,
    "last_write_user": None,
}


def client(env, who="alice"):
    from chat.docs import routes

    app = FastAPI()
    app.include_router(routes.router)
    user = env.users[who]

    async def _current():
        return user

    app.dependency_overrides[routes.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def detail(resp):
    return resp.json()["detail"]


def doc_dirs(env):
    return sorted(p.name for p in env.dirs["docs"].iterdir())


def open_public_projects(env):
    env.fg.set_feature_enabled(env.fg.FEATURE_PUBLIC_PROJECTS, True)


def uid(env, who):
    return env.users[who]["id"]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class TestGate:
    def test_every_route_403s_when_closed(self, docs_env):
        doc = seed_doc(docs_env)
        files.add_asset(doc["id"], "chart.png", PNG)
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_DOCS, False)
        c = client(docs_env)
        d = doc["id"]
        calls = [
            ("get", "/app/api/docs", None),
            ("post", "/app/api/docs", {"title": "New"}),
            ("get", f"/app/api/docs/{d}", None),
            ("put", f"/app/api/docs/{d}", {"title": "Renamed"}),
            ("put", f"/app/api/docs/{d}/mode", {"mode": "public"}),
            ("get", f"/app/api/docs/{d}/assets/chart.png", None),
            ("get", f"/app/api/docs/{d}/download?format=md", None),
            ("delete", f"/app/api/docs/{d}", None),
        ]
        for method, url, payload in calls:
            kwargs = {"json": payload} if payload is not None else {}
            resp = getattr(c, method)(url, **kwargs)
            assert resp.status_code == 403, (method, url)
            assert detail(resp) == {
                "error": "docs_disabled", "message": docs_disabled_message(),
            }
        # Nothing changed.
        row = _run(docs_env.doc_store.get_doc(d))
        assert row["title"] == "Notes" and row["mode"] == "private"
        assert doc_dirs(docs_env) == [d]
        assert docs_env.published == []

    def test_gate_per_user(self, docs_env):
        fg = docs_env.fg
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, [docs_env.users["bob"]["email"]])
        assert client(docs_env, "alice").get("/app/api/docs").status_code == 403
        assert client(docs_env, "bob").get("/app/api/docs").status_code == 200


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestList:
    def test_user_docs_vs_project_docs(self, docs_env):
        user_doc = seed_doc(docs_env, "Mine")
        proj_doc = seed_doc(docs_env, "Proj", project_id=docs_env.private_project)
        c = client(docs_env)

        resp = c.get("/app/api/docs")
        assert resp.status_code == 200
        data = resp.json()
        assert [r["id"] for r in data["docs"]] == [user_doc["id"]]
        assert data["has_more"] is False and data["next_cursor"] is None

        data = c.get(f"/app/api/docs?project_id={docs_env.private_project}").json()
        assert [r["id"] for r in data["docs"]] == [proj_doc["id"]]
        row = data["docs"][0]
        assert row["scope"] == "project"
        assert row["access"]["can_switch_mode"] is False

        assert c.get(
            f"/app/api/docs?project_id={docs_env.other_project}"
        ).json()["docs"] == []

    def test_owner_row_shape(self, docs_env):
        doc = seed_doc(docs_env, "Mine")
        row = client(docs_env).get("/app/api/docs").json()["docs"][0]
        assert row == {
            **{k: v for k, v in doc.items()},
            **OWNER_ROW_EXTRAS,
            "scope": "user",
            "shared": False,
            "shares": [],
            "access": OWNER_ACCESS,
        }
        assert "content" not in row

    def test_newest_updated_first(self, docs_env):
        a = seed_doc(docs_env, "A")
        b = seed_doc(docs_env, "B")
        c_doc = seed_doc(docs_env, "C")
        # Touch A: it moves to the top.
        _run(docs_env.doc_store.update_after_write(
            a["id"], content_size=1, last_write_source="ui",
        ))
        ids = [r["id"] for r in client(docs_env).get("/app/api/docs").json()["docs"]]
        assert ids == [a["id"], c_doc["id"], b["id"]]

    def test_keyset_paging(self, docs_env):
        for i in range(5):
            seed_doc(docs_env, f"Doc {i}")
        c = client(docs_env)
        full = [r["id"] for r in c.get("/app/api/docs").json()["docs"]]
        assert len(full) == 5

        seen = []
        cursor = None
        pages = 0
        while True:
            url = "/app/api/docs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            data = c.get(url).json()
            pages += 1
            seen.extend(r["id"] for r in data["docs"])
            if not data["has_more"]:
                assert data["next_cursor"] is None
                break
            assert len(data["docs"]) == 2
            last = data["docs"][-1]
            assert data["next_cursor"] == f"{last['updated_at']}|{last['id']}"
            cursor = data["next_cursor"]
        assert pages == 3
        assert seen == full

    def test_exact_page_has_no_more(self, docs_env):
        seed_doc(docs_env, "One")
        seed_doc(docs_env, "Two")
        data = client(docs_env).get("/app/api/docs?limit=2").json()
        assert len(data["docs"]) == 2
        assert data["has_more"] is False and data["next_cursor"] is None

    @pytest.mark.parametrize("cursor", ["garbage", "not-a-date|abc", "2026-10-05T00:00:00|"])
    def test_invalid_cursor(self, docs_env, cursor):
        resp = client(docs_env).get("/app/api/docs", params={"limit": 2, "cursor": cursor})
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_cursor"

    def test_limit_bounds(self, docs_env):
        c = client(docs_env)
        assert c.get("/app/api/docs?limit=0").status_code == 422
        assert c.get("/app/api/docs?limit=201").status_code == 422

    def test_shared_with_me_and_unshared_absent(self, docs_env):
        shared = seed_doc(docs_env, "Bob shared", owner="bob", shares=[("alice", "read")])
        everyone = seed_doc(docs_env, "Bob everyone", owner="bob", shares=[(None, "write")])
        seed_doc(docs_env, "Bob secret", owner="bob")
        seed_doc(docs_env, "Carol shared with bob", owner="carol", shares=[("bob", "write")])

        c = client(docs_env)
        # Phase 3: the default list is the caller's OWN user docs; docs
        # shared with them are the separate shared=true stream.
        assert c.get("/app/api/docs").json()["docs"] == []
        rows = {r["id"]: r for r in c.get("/app/api/docs?shared=true").json()["docs"]}
        assert set(rows) == {shared["id"], everyone["id"]}
        for row in rows.values():
            assert row["shared"] is True
            assert "shares" not in row  # the roster is owner-only
            assert row["access"]["can_rename"] is False
            assert row["access"]["can_delete"] is False
            assert row["access"]["can_switch_mode"] is False
        assert rows[shared["id"]]["access"]["write"] == "denied"
        assert rows[everyone["id"]]["access"]["write"] == "free"

    def test_project_must_be_visible(self, docs_env):
        seed_doc(docs_env, "Proj", project_id=docs_env.private_project)
        resp = client(docs_env, "bob").get(
            f"/app/api/docs?project_id={docs_env.private_project}"
        )
        assert resp.status_code == 404
        assert detail(resp)["error"] == "project_not_found"
        resp = client(docs_env).get(f"/app/api/docs?project_id={uuid.uuid4()}")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "project_not_found"

    def test_gated_off_public_project_404s(self, docs_env):
        doc = seed_doc(
            docs_env, "Open doc", mode="public", project_id=docs_env.public_project,
        )
        url = f"/app/api/docs?project_id={docs_env.public_project}"
        c = client(docs_env)
        assert c.get(url).status_code == 404
        open_public_projects(docs_env)
        assert [r["id"] for r in c.get(url).json()["docs"]] == [doc["id"]]


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_user_doc_defaults(self, docs_env):
        resp = client(docs_env).post(
            "/app/api/docs", json={"title": "  Plan  ", "description": "why"},
        )
        assert resp.status_code == 201
        row = resp.json()
        assert row["title"] == "Plan"
        assert row["description"] == "why"
        assert row["mode"] == "private"
        assert row["scope"] == "user"
        assert row["project_id"] is None
        assert row["content_size"] == 0
        # Phase 3: UI writes record who wrote them.
        assert row["last_write_source"] == f"ui:{uid(docs_env, 'alice')}"
        assert row["last_write_user"]["id"] == uid(docs_env, "alice")
        assert row["last_write_user"]["email"] == "alice@example.com"
        assert row["owner_id"] == uid(docs_env, "alice")
        assert row["shares"] == [] and row["shared"] is False
        assert row["access"] == OWNER_ACCESS
        # An empty body file exists.
        assert body(row["id"]) == ""
        assert (docs_env.dirs["docs"] / row["id"] / "doc.md").read_bytes() == b""
        alice = uid(docs_env, "alice")
        assert sorted(event_types(docs_env)) == sorted([
            (alice, "doc_list_changed"), (alice, "doc_changed"),
        ])

    @pytest.mark.parametrize("public_projects", [False, True])
    def test_user_doc_public_mode_400(self, docs_env, public_projects):
        if public_projects:
            open_public_projects(docs_env)
        resp = client(docs_env).post(
            "/app/api/docs", json={"title": "Open", "mode": "public"},
        )
        assert resp.status_code == 400
        assert detail(resp) == USER_DOC_PRIVATE
        assert detail(resp)["message"] == user_doc_mode_private_message()
        assert doc_dirs(docs_env) == []
        assert docs_env.published == []

    def test_project_mode_forced(self, docs_env):
        c = client(docs_env)
        row = c.post("/app/api/docs", json={
            "title": "Proj", "project_id": docs_env.private_project,
        }).json()
        assert row["mode"] == "private"
        assert row["scope"] == "project"
        assert row["project_id"] == docs_env.private_project
        assert row["access"]["can_switch_mode"] is False

        open_public_projects(docs_env)
        row = c.post("/app/api/docs", json={
            "title": "Open proj", "project_id": docs_env.public_project,
        }).json()
        assert row["mode"] == "public"
        # A matching explicit mode is fine.
        row = c.post("/app/api/docs", json={
            "title": "Open proj 2", "project_id": docs_env.public_project,
            "mode": "public",
        }).json()
        assert row["mode"] == "public"

    def test_disagreeing_mode_400(self, docs_env):
        resp = client(docs_env).post("/app/api/docs", json={
            "title": "Leak", "project_id": docs_env.private_project, "mode": "public",
        })
        assert resp.status_code == 400
        assert detail(resp)["error"] == "project_doc_mode_inherited"
        assert doc_dirs(docs_env) == []
        assert docs_env.published == []

    def test_duplicate_title_409(self, docs_env):
        seed_doc(docs_env, "Plan")
        before = doc_dirs(docs_env)
        resp = client(docs_env).post("/app/api/docs", json={"title": "PLAN"})
        assert resp.status_code == 409
        assert detail(resp)["error"] == "duplicate_title"
        # The directory laid out for the failed create was removed again.
        assert doc_dirs(docs_env) == before
        # Same title in a project is a different scope.
        resp = client(docs_env).post("/app/api/docs", json={
            "title": "Plan", "project_id": docs_env.private_project,
        })
        assert resp.status_code == 201

    @pytest.mark.parametrize("payload, code", [
        ({"title": ""}, "invalid_title"),
        ({"title": "   "}, "invalid_title"),
        ({"title": "x" * 201}, "invalid_title"),
        ({"title": "ok", "description": "d" * 501}, "invalid_description"),
        ({"title": "ok", "mode": "secret"}, "invalid_mode"),
    ])
    def test_validation_400(self, docs_env, payload, code):
        resp = client(docs_env).post("/app/api/docs", json=payload)
        assert resp.status_code == 400
        assert detail(resp)["error"] == code
        assert doc_dirs(docs_env) == []

    def test_non_owned_project_404(self, docs_env):
        resp = client(docs_env, "bob").post("/app/api/docs", json={
            "title": "Sneaky", "project_id": docs_env.private_project,
        })
        assert resp.status_code == 404
        assert detail(resp)["error"] == "project_not_found"
        assert doc_dirs(docs_env) == []

    def test_gated_off_public_project_404(self, docs_env):
        resp = client(docs_env).post("/app/api/docs", json={
            "title": "Open", "project_id": docs_env.public_project,
        })
        assert resp.status_code == 404
        assert detail(resp)["error"] == "project_not_found"


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class TestGet:
    def test_owner_gets_content_and_shares(self, docs_env):
        doc = seed_doc(docs_env, "Notes", "# Hi\nthere\n", shares=[("bob", "read")])
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 200
        row = resp.json()
        assert row["content"] == "# Hi\nthere\n"
        assert [s["user_id"] for s in row["shares"]] == [uid(docs_env, "bob")]
        assert row["shared"] is True
        assert row["access"]["write"] == "free"

    def test_recipient_gets_no_roster(self, docs_env):
        doc = seed_doc(docs_env, "Notes", "x\n", shares=[("bob", "read"), ("carol", "write")])
        row = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").json()
        assert row["content"] == "x\n"
        assert "shares" not in row
        assert row["shared"] is True
        assert row["access"] == {
            "can_rename": False, "can_switch_mode": False,
            "can_delete": False, "can_edit": False, "can_share": False,
            "can_delete_assets": False, "write": "denied",
        }
        row = client(docs_env, "carol").get(f"/app/api/docs/{doc['id']}").json()
        assert row["access"]["write"] == "free"
        assert row["access"]["can_edit"] is True
        assert "shares" not in row

    def test_last_write_conversation_resolved_for_the_footer(self, docs_env):
        """The viewer's "Last written by" line needs the writing conversation's
        title and project without fetching the whole chat history."""
        from datetime import datetime
        from db import conversation_store

        doc = seed_doc(docs_env, "Notes", "x\n", shares=[("bob", "read")])
        conv_id = str(uuid.uuid4())
        _run(conversation_store.create_conversation(
            uid(docs_env, "alice"), conv_id, datetime.utcnow(),
            project_id=docs_env.private_project, custom_name="Metrics chat",
        ))
        _run(docs_env.doc_store.update_after_write(
            doc["id"], content_size=2, last_write_source=f"conversation:{conv_id}",
        ))
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_conversation"] == {
            "id": conv_id, "title": "Metrics chat", "project_id": docs_env.private_project,
        }
        # Blanked for a share recipient along with last_write_source.
        row = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_source"] is None
        assert row["last_write_conversation"] is None

    def test_last_write_conversation_null_when_gone_or_not_a_conversation(self, docs_env):
        doc = seed_doc(docs_env, "Notes", "x\n")
        _run(docs_env.doc_store.update_after_write(
            doc["id"], content_size=2, last_write_source="conversation:does-not-exist",
        ))
        assert client(docs_env).get(f"/app/api/docs/{doc['id']}").json()["last_write_conversation"] is None
        _run(docs_env.doc_store.update_after_write(
            doc["id"], content_size=2, last_write_source="action_request:7",
        ))
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["last_write_source"] == "action_request:7"
        assert row["last_write_conversation"] is None

    def test_project_doc_visible_in_ui(self, docs_env):
        doc = seed_doc(docs_env, "Proj", "p\n", project_id=docs_env.private_project)
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["content"] == "p\n"
        assert row["scope"] == "project"

    def test_hidden_equals_missing(self, docs_env):
        doc = seed_doc(docs_env, "Secret", "classified\n")
        bob = client(docs_env, "bob")
        hidden = bob.get(f"/app/api/docs/{doc['id']}")
        assert hidden.status_code == 404
        assert detail(hidden) == {
            "error": "doc_not_found", "message": doc_not_found_message(doc["id"]),
        }
        assert b"classified" not in hidden.content
        # Same id once it really does not exist: byte-identical response.
        _run(docs_env.doc_store.delete_doc(doc["id"]))
        missing = bob.get(f"/app/api/docs/{doc['id']}")
        assert missing.status_code == hidden.status_code
        assert missing.content == hidden.content

    def test_unsafe_body_400(self, docs_env):
        doc = seed_doc(docs_env, "Notes")
        body_path = docs_env.dirs["docs"] / doc["id"] / "doc.md"
        outside = docs_env.tmp / "outside.md"
        outside.write_text("outside\n")
        body_path.unlink()
        os.symlink(outside, body_path)
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_doc_files"
        assert b"outside" not in resp.content


class TestDetailAssets:
    def test_fresh_doc_has_none(self, docs_env):
        doc = seed_doc(docs_env, "Plain")
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert row["assets"] == []
        # The created-from-UI path too (empty assets/ directory).
        created = client(docs_env).post("/app/api/docs", json={"title": "New"}).json()
        assert client(docs_env).get(f"/app/api/docs/{created['id']}").json()["assets"] == []

    def test_regular_images_sorted_with_size_and_mime(self, docs_env):
        # bob holds a WRITE share: editors see every image. Read-only
        # viewers see only referenced ones (tests/test_docs_sharing.py).
        doc = seed_doc(docs_env, "Pics", shares=[("bob", "write")])
        files.add_asset(doc["id"], "zeta.png", PNG)
        files.add_asset(doc["id"], "alpha.gif", GIF)
        assets_dir = docs_env.dirs["docs"] / doc["id"] / "assets"
        (assets_dir / "upper.JPG").write_bytes(JPEG)
        # Skipped: a symlink, a non-image extension, a hidden file, a
        # directory with an image name, an SVG.
        outside = docs_env.tmp / "outside.png"
        outside.write_bytes(PNG)
        os.symlink(outside, assets_dir / "link.png")
        (assets_dir / "notes.txt").write_text("not an image")
        (assets_dir / ".hidden.png").write_bytes(PNG)
        (assets_dir / "folder.png").mkdir()
        (assets_dir / "drawing.svg").write_text("<svg/>")

        c = client(docs_env)
        expected = [
            {"name": "alpha.gif", "size": len(GIF), "mime": "image/gif"},
            {"name": "upper.JPG", "size": len(JPEG), "mime": "image/jpeg"},
            {"name": "zeta.png", "size": len(PNG), "mime": "image/png"},
        ]
        assert c.get(f"/app/api/docs/{doc['id']}").json()["assets"] == expected
        # Every listed entry is one the asset route serves, with that MIME.
        for asset in expected:
            resp = c.get(f"/app/api/docs/{doc['id']}/assets/{asset['name']}")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == asset["mime"]
            assert len(resp.content) == asset["size"]
        # A write-share recipient sees the same images.
        bob_row = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}").json()
        assert bob_row["assets"] == expected
        # The list route does not carry the asset list.
        assert all("assets" not in r for r in c.get("/app/api/docs").json()["docs"])

    def test_names_add_asset_never_writes_are_skipped(self, docs_env):
        doc = seed_doc(docs_env, "Pics")
        files.add_asset(doc["id"], "chart.png", PNG)
        assets_dir = docs_env.dirs["docs"] / doc["id"] / "assets"
        # Image extensions the asset route would even serve, but names
        # outside [A-Za-z0-9._-]: planted, so not listed.
        for odd in ("my chart.png", "naïve.png", "semi;colon.png", "new\nline.png"):
            (assets_dir / odd).write_bytes(PNG)
        row = client(docs_env).get(f"/app/api/docs/{doc['id']}").json()
        assert [a["name"] for a in row["assets"]] == ["chart.png"]

    def test_unsafe_assets_dir_400(self, docs_env):
        doc = seed_doc(docs_env, "Pics")
        assets_dir = docs_env.dirs["docs"] / doc["id"] / "assets"
        elsewhere = docs_env.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.png").write_bytes(PNG)
        assets_dir.rmdir()
        os.symlink(elsewhere, assets_dir)
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_doc_files"
        assert b"secret.png" not in resp.content


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


class TestRename:
    def test_owner_renames(self, docs_env):
        doc = seed_doc(docs_env, "Old")
        resp = client(docs_env).put(f"/app/api/docs/{doc['id']}", json={
            "title": "New", "description": "desc",
            "expected_updated_at": doc["updated_at"],
        })
        assert resp.status_code == 200
        row = resp.json()
        assert row["title"] == "New" and row["description"] == "desc"
        assert row["updated_at"] != doc["updated_at"]
        assert row["shares"] == []
        alice = uid(docs_env, "alice")
        assert sorted(event_types(docs_env)) == sorted([
            (alice, "doc_list_changed"), (alice, "doc_changed"),
        ])
        assert docs_env.published[1][1]["updated_at"] == row["updated_at"]

    def test_noop_keeps_token_and_publishes_nothing(self, docs_env):
        doc = seed_doc(docs_env, "Same")
        row = client(docs_env).put(f"/app/api/docs/{doc['id']}", json={
            "title": "Same", "expected_updated_at": doc["updated_at"],
        }).json()
        assert row["updated_at"] == doc["updated_at"]
        assert docs_env.published == []

    def test_stale_409_flat_with_current(self, docs_env):
        doc = seed_doc(docs_env, "Old", shares=[("bob", "read")])
        c = client(docs_env)
        first = c.put(f"/app/api/docs/{doc['id']}", json={"title": "First"}).json()
        resp = c.put(f"/app/api/docs/{doc['id']}", json={
            "title": "Second", "expected_updated_at": doc["updated_at"],
        })
        assert resp.status_code == 409
        data = resp.json()
        assert "detail" not in data
        assert data["error"] == "stale_update"
        assert data["message"]
        current = data["current"]
        assert current["title"] == "First"
        assert current["updated_at"] == first["updated_at"]
        assert current["access"]["can_rename"] is True
        assert [s["user_id"] for s in current["shares"]] == [uid(docs_env, "bob")]
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["title"] == "First"

    def test_non_owner_403_hidden_404(self, docs_env):
        doc = seed_doc(docs_env, "Shared", shares=[("bob", "write")])
        resp = client(docs_env, "bob").put(
            f"/app/api/docs/{doc['id']}", json={"title": "Bob's now"},
        )
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        resp = client(docs_env, "carol").put(
            f"/app/api/docs/{doc['id']}", json={"title": "Carol's now"},
        )
        assert resp.status_code == 404
        assert detail(resp)["error"] == "doc_not_found"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["title"] == "Shared"

    def test_duplicate_409(self, docs_env):
        seed_doc(docs_env, "Taken")
        doc = seed_doc(docs_env, "Mine")
        resp = client(docs_env).put(f"/app/api/docs/{doc['id']}", json={"title": "taken"})
        assert resp.status_code == 409
        assert detail(resp)["error"] == "duplicate_title"

    @pytest.mark.parametrize("payload, code", [
        ({"title": " "}, "invalid_title"),
        ({"description": "d" * 501}, "invalid_description"),
    ])
    def test_validation_400(self, docs_env, payload, code):
        doc = seed_doc(docs_env, "Mine")
        resp = client(docs_env).put(f"/app/api/docs/{doc['id']}", json=payload)
        assert resp.status_code == 400
        assert detail(resp)["error"] == code


# ---------------------------------------------------------------------------
# Mode switch: refused for every doc in v1
# ---------------------------------------------------------------------------


class TestMode:
    @pytest.mark.parametrize("public_projects", [False, True])
    @pytest.mark.parametrize(
        "payload", [{"mode": "public"}, {"mode": "private"}, {"mode": "secret"}, {}],
    )
    def test_user_doc_400(self, docs_env, payload, public_projects):
        if public_projects:
            open_public_projects(docs_env)
        doc = seed_doc(docs_env, "Mine")
        resp = client(docs_env).put(f"/app/api/docs/{doc['id']}/mode", json=payload)
        assert resp.status_code == 400
        assert detail(resp) == USER_DOC_PRIVATE
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["mode"] == "private"
        assert row["updated_at"] == doc["updated_at"]
        assert docs_env.published == []

    @pytest.mark.parametrize("payload", [{"mode": "public"}, {"mode": "private"}, {}])
    def test_project_doc_inherited(self, docs_env, payload):
        open_public_projects(docs_env)
        c = client(docs_env)
        for project_id, mode in (
            (docs_env.private_project, "private"), (docs_env.public_project, "public"),
        ):
            doc = seed_doc(docs_env, f"Proj {mode}", mode=mode, project_id=project_id)
            resp = c.put(f"/app/api/docs/{doc['id']}/mode", json=payload)
            assert resp.status_code == 400
            assert detail(resp) == PROJECT_MODE_INHERITED
            assert _run(docs_env.doc_store.get_doc(doc["id"]))["mode"] == mode
        assert docs_env.published == []

    def test_non_owner_403(self, docs_env):
        doc = seed_doc(docs_env, "Shared", shares=[("bob", "write")])
        resp = client(docs_env, "bob").put(
            f"/app/api/docs/{doc['id']}/mode", json={"mode": "public"},
        )
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        assert _run(docs_env.doc_store.get_doc(doc["id"]))["mode"] == "private"

    def test_order(self, docs_env):
        """404 doc_not_found, then 403 forbidden, then the scope's 400."""
        shared = seed_doc(docs_env, "Shared", shares=[("bob", "write")])
        proj = seed_doc(
            docs_env, "Proj", project_id=docs_env.private_project, shares=[("bob", "read")],
        )
        for doc in (shared, proj):
            resp = client(docs_env, "carol").put(
                f"/app/api/docs/{doc['id']}/mode", json={"mode": "public"},
            )
            assert resp.status_code == 404
            assert detail(resp)["error"] == "doc_not_found"
            resp = client(docs_env, "bob").put(
                f"/app/api/docs/{doc['id']}/mode", json={"mode": "public"},
            )
            assert resp.status_code == 403
            assert detail(resp)["error"] == "forbidden"
        c = client(docs_env)
        assert detail(c.put(f"/app/api/docs/{shared['id']}/mode", json={})) == USER_DOC_PRIVATE
        assert detail(c.put(f"/app/api/docs/{proj['id']}/mode", json={})) == PROJECT_MODE_INHERITED


# ---------------------------------------------------------------------------
# User docs are always private, whatever the public_projects gate says
# (closed in docs_env); only public-project docs are public
# ---------------------------------------------------------------------------


class TestUserDocsAlwaysPrivate:
    @pytest.mark.parametrize("public_projects", [False, True])
    def test_create_is_private(self, docs_env, public_projects):
        if public_projects:
            open_public_projects(docs_env)
        c = client(docs_env)
        for title, payload in (("Plan", {}), ("Plan 2", {"mode": "private"})):
            resp = c.post("/app/api/docs", json={"title": title, **payload})
            assert resp.status_code == 201
            row = resp.json()
            assert (row["mode"], row["scope"]) == ("private", "user")
            assert row["access"]["can_switch_mode"] is False

    def test_create_public_400_for_everyone(self, docs_env):
        fg = docs_env.fg
        fg.set_feature_enabled(fg.FEATURE_PUBLIC_PROJECTS, True)
        fg.set_feature_allowed_users(
            fg.FEATURE_PUBLIC_PROJECTS, [docs_env.users["bob"]["email"]],
        )
        payload = {"title": "Open", "mode": "public"}
        for who in ("alice", "bob"):  # gate closed / open for the user
            resp = client(docs_env, who).post("/app/api/docs", json=payload)
            assert resp.status_code == 400
            assert detail(resp) == USER_DOC_PRIVATE
        assert doc_dirs(docs_env) == []
        assert docs_env.published == []
        assert client(docs_env, "bob").get("/app/api/docs").json()["docs"] == []

    def test_create_project_docs_unaffected(self, docs_env):
        c = client(docs_env)
        resp = c.post("/app/api/docs", json={
            "title": "Proj", "project_id": docs_env.private_project,
        })
        assert resp.status_code == 201
        assert resp.json()["mode"] == "private"
        # A disagreeing mode is still the project rule's error.
        resp = c.post("/app/api/docs", json={
            "title": "Leak", "project_id": docs_env.private_project, "mode": "public",
        })
        assert resp.status_code == 400
        assert detail(resp)["error"] == "project_doc_mode_inherited"

    def test_public_doc_lives_in_a_public_project(self, docs_env):
        """The only way to a public doc: create it in a public project,
        which the public_projects gate must show the user."""
        c = client(docs_env)
        payload = {"title": "Open", "project_id": docs_env.public_project}
        resp = c.post("/app/api/docs", json=payload)
        assert resp.status_code == 404
        assert detail(resp)["error"] == "project_not_found"
        open_public_projects(docs_env)
        resp = c.post("/app/api/docs", json=payload)
        assert resp.status_code == 201
        row = resp.json()
        assert (row["mode"], row["scope"]) == ("public", "project")
        assert row["access"]["can_switch_mode"] is False

    def test_can_switch_mode_always_false(self, docs_env):
        open_public_projects(docs_env)
        private = seed_doc(docs_env, "Private")
        legacy = make_legacy_public(docs_env, seed_doc(docs_env, "Legacy")["id"])
        proj = seed_doc(docs_env, "Proj", project_id=docs_env.private_project)
        pub = seed_doc(docs_env, "Pub", mode="public", project_id=docs_env.public_project)
        c = client(docs_env)
        rows = c.get("/app/api/docs").json()["docs"]
        for project_id in (docs_env.private_project, docs_env.public_project):
            rows += c.get(f"/app/api/docs?project_id={project_id}").json()["docs"]
        assert {r["id"] for r in rows} == {d["id"] for d in (private, legacy, proj, pub)}
        for row in rows:
            assert row["access"]["can_switch_mode"] is False, row["title"]
            detail_row = c.get(f"/app/api/docs/{row['id']}").json()
            assert detail_row["access"]["can_switch_mode"] is False, row["title"]

    def test_legacy_public_user_doc_cannot_switch(self, docs_env):
        legacy = make_legacy_public(docs_env, seed_doc(docs_env, "Legacy")["id"])
        resp = client(docs_env).put(
            f"/app/api/docs/{legacy['id']}/mode", json={"mode": "private"},
        )
        assert resp.status_code == 400
        assert detail(resp) == USER_DOC_PRIVATE
        assert _run(docs_env.doc_store.get_doc(legacy["id"]))["updated_at"] == legacy["updated_at"]


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class TestAssets:
    def test_serves_png_inline(self, docs_env):
        doc = seed_doc(docs_env, "Pics")
        name = files.add_asset(doc["id"], "chart.png", PNG).name
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/assets/{name}")
        assert resp.status_code == 200
        assert resp.content == PNG
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["content-disposition"].startswith("inline")
        assert resp.headers["cache-control"] == "private"
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_share_recipient_can_fetch(self, docs_env):
        # A read share fetches the images the current body references.
        doc = seed_doc(docs_env, "Pics", "![c](assets/chart.png)\n",
                       shares=[("bob", "read")])
        name = files.add_asset(doc["id"], "chart.png", PNG).name
        resp = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}/assets/{name}")
        assert resp.status_code == 200
        assert resp.content == PNG

    def test_hidden_doc_404(self, docs_env):
        doc = seed_doc(docs_env, "Pics")
        name = files.add_asset(doc["id"], "chart.png", PNG).name
        resp = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}/assets/{name}")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "doc_not_found"

    @pytest.mark.parametrize("name", [
        "missing.png", "doc.md", ".hidden.png", "..%2Fdoc.md",
        "..%5Cdoc.png", "assets", "chart.svg",
    ])
    def test_missing_and_traversal_404(self, docs_env, name):
        doc = seed_doc(docs_env, "Pics", "TOP SECRET BODY\n")
        files.add_asset(doc["id"], "chart.png", PNG)
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/assets/{name}")
        assert resp.status_code == 404
        assert b"TOP SECRET BODY" not in resp.content

    @pytest.mark.parametrize("name", [
        "..", "../doc.md", "../../outside.png", "/etc/passwd", "sub/chart.png",
        "..\\doc.png", "chart.png\x00.png",
    ])
    def test_handler_refuses_raw_traversal_names(self, docs_env, name):
        # The HTTP client normalizes dot segments and the router never
        # passes "/" into {name}; call the handler with the raw name.
        from fastapi import HTTPException
        from chat.docs.routes import get_ui_doc_asset

        doc = seed_doc(docs_env, "Pics")
        files.add_asset(doc["id"], "chart.png", PNG)
        (docs_env.tmp / "outside.png").write_bytes(PNG)
        with pytest.raises(HTTPException) as exc:
            _run(get_ui_doc_asset(doc["id"], name, user=docs_env.users["alice"]))
        assert exc.value.status_code == 404
        assert exc.value.detail["error"] == "asset_not_found"

    def test_symlinked_asset_404(self, docs_env):
        doc = seed_doc(docs_env, "Pics")
        outside = docs_env.tmp / "outside.png"
        outside.write_bytes(PNG)
        os.symlink(outside, docs_env.dirs["docs"] / doc["id"] / "assets" / "link.png")
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/assets/link.png")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "asset_not_found"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


class TestDownload:
    def test_markdown(self, docs_env):
        doc = seed_doc(docs_env, "Quarterly Plan", "# Plan\nüber\n")
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/download?format=md")
        assert resp.status_code == 200
        assert resp.content == (docs_env.dirs["docs"] / doc["id"] / "doc.md").read_bytes()
        assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="Quarterly Plan.md"')
        assert "filename*=UTF-8''Quarterly%20Plan.md" in disposition

    def test_default_format_is_md(self, docs_env):
        doc = seed_doc(docs_env, "Notes", "x\n")
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/download")
        assert resp.status_code == 200
        assert resp.content == b"x\n"

    def test_unsafe_title_is_sanitized(self, docs_env):
        # Quote, slash and colon replaced; U+2028 (a line separator) folded
        # to a space; the non-ASCII letter survives only in filename*.
        doc = seed_doc(docs_env, 'Q3 "plan"/notes: é x​')
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/download?format=md")
        disposition = resp.headers["content-disposition"]
        assert disposition == (
            'attachment; filename="Q3 -plan-notes- _ x.md"; '
            "filename*=UTF-8''Q3%20-plan-notes-%20%C3%A9%20x.md"
        )

    def test_zip(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env, "Pics", "![chart](assets/chart.png)\n")
        name = files.add_asset(doc["id"], "chart.png", PNG).name
        built = []
        real_build_zip = files.build_zip

        def spy(doc_id, title, *rest):
            path = real_build_zip(doc_id, title, *rest)
            built.append(path)
            return path

        monkeypatch.setattr(files, "build_zip", spy)
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/download?format=zip")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert resp.headers["content-disposition"].startswith('attachment; filename="Pics.zip"')
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert sorted(zf.namelist()) == ["assets/chart.png", "doc.md"]
            assert zf.read("doc.md") == b"![chart](assets/chart.png)\n"
            assert zf.read(f"assets/{name}") == PNG
        # The temp archive is removed after the response.
        assert len(built) == 1 and not built[0].exists()

    def test_zip_refuses_symlink(self, docs_env):
        doc = seed_doc(docs_env, "Pics")
        files.add_asset(doc["id"], "chart.png", PNG)
        outside = docs_env.tmp / "secret.png"
        outside.write_bytes(PNG + b"SECRET")
        os.symlink(outside, docs_env.dirs["docs"] / doc["id"] / "assets" / "link.png")
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/download?format=zip")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_doc_files"
        assert b"SECRET" not in resp.content

    def test_invalid_format(self, docs_env):
        doc = seed_doc(docs_env, "Notes")
        resp = client(docs_env).get(f"/app/api/docs/{doc['id']}/download?format=pdf")
        assert resp.status_code == 400
        assert detail(resp)["error"] == "invalid_format"

    def test_hidden_doc_404(self, docs_env):
        doc = seed_doc(docs_env, "Secret", "classified\n")
        resp = client(docs_env, "bob").get(f"/app/api/docs/{doc['id']}/download?format=md")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "doc_not_found"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_owner_deletes(self, docs_env):
        doc = seed_doc(docs_env, "Gone", shares=[("bob", "write"), (None, "read")])
        files.add_asset(doc["id"], "chart.png", PNG)
        resp = client(docs_env).delete(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}
        assert _run(docs_env.doc_store.get_doc(doc["id"])) is None
        assert _run(docs_env.doc_store.list_shares(doc["id"])) == []
        assert not (docs_env.dirs["docs"] / doc["id"]).exists()
        # Phase 3: the owner and the (captured) share recipients; nobody is
        # connected in the test, so the everyone row adds no one.
        assert event_types(docs_env) == [
            (uid(docs_env, "alice"), "doc_list_changed"),
            (uid(docs_env, "bob"), "doc_list_changed"),
        ]
        # Gone for everyone.
        assert client(docs_env).get(f"/app/api/docs/{doc['id']}").status_code == 404

    def test_non_owner_403(self, docs_env):
        doc = seed_doc(docs_env, "Shared", shares=[("bob", "write")])
        resp = client(docs_env, "bob").delete(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 403
        assert detail(resp)["error"] == "forbidden"
        assert _run(docs_env.doc_store.get_doc(doc["id"])) is not None
        assert (docs_env.dirs["docs"] / doc["id"]).is_dir()
        assert docs_env.published == []

    def test_hidden_404(self, docs_env):
        doc = seed_doc(docs_env, "Secret")
        resp = client(docs_env, "bob").delete(f"/app/api/docs/{doc['id']}")
        assert resp.status_code == 404
        assert detail(resp)["error"] == "doc_not_found"
        assert _run(docs_env.doc_store.get_doc(doc["id"])) is not None


# ---------------------------------------------------------------------------
# Project delete sweeps the project's doc directories
# ---------------------------------------------------------------------------


class TestProjectDelete:
    def test_project_delete_removes_doc_dirs(self, docs_env):
        from chat.project_routes import delete_user_project

        one = seed_doc(docs_env, "One", project_id=docs_env.private_project,
                       shares=[("bob", "read")])
        two = seed_doc(docs_env, "Two", project_id=docs_env.private_project)
        files.add_asset(two["id"], "chart.png", PNG)
        other = seed_doc(docs_env, "Other", project_id=docs_env.other_project)
        mine = seed_doc(docs_env, "Mine")

        alice = docs_env.users["alice"]
        result = _run(delete_user_project(docs_env.private_project, user=alice))
        assert result == {"success": True}

        for gone in (one, two):
            assert _run(docs_env.doc_store.get_doc(gone["id"])) is None
            assert not (docs_env.dirs["docs"] / gone["id"]).exists()
        assert _run(docs_env.doc_store.list_shares(one["id"])) == []
        for kept in (other, mine):
            assert _run(docs_env.doc_store.get_doc(kept["id"])) is not None
            assert (docs_env.dirs["docs"] / kept["id"]).is_dir()
        # Phase 3: the owner and the deleted docs' share recipients.
        assert event_types(docs_env) == [
            (alice["id"], "doc_list_changed"), (uid(docs_env, "bob"), "doc_list_changed"),
        ]

    def test_project_without_docs_publishes_nothing(self, docs_env):
        from chat.project_routes import delete_user_project

        alice = docs_env.users["alice"]
        _run(delete_user_project(docs_env.other_project, user=alice))
        assert docs_env.published == []
