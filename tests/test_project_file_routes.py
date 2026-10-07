"""Project workspace file routes, copy routes and ``file_list_changed`` scope.

Devplan 00009 sections 7 / 8:

* ``/projects/{pid}/files...`` mirror the conversation file routes over the
  shared project workspace (``projects/{pid}/workspace/workspace``), 404
  unless the caller owns the project;
* the conversation routes of a PROJECT conversation operate on that
  conversation's own workspace (``chats/{cid}/workspace``);
* ``/conversations/{cid}/files/copy-to-project`` / ``copy-from-project``
  copy or move entries between the two spaces;
* every publisher sets ``scope`` explicitly: conversation routes
  ``"conversation"``, project routes ``"project"``, copy routes both.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import chat.file_routes as file_routes
import chat.storage as storage_mod
from chat.realtime import events as realtime_events


@pytest.fixture()
def env(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)

    convs = {
        "inproj": {"id": "inproj", "user_id": 1, "project_id": "p1"},
        "solo": {"id": "solo", "user_id": 1, "project_id": None},
        "theirs": {"id": "theirs", "user_id": 2, "project_id": "p2"},
    }
    projs = {
        "p1": {"id": "p1", "user_id": 1},
        "p2": {"id": "p2", "user_id": 2},
        "fresh": {"id": "fresh", "user_id": 1},
    }

    async def get_meta(user_id, conversation_id):
        row = convs.get(conversation_id)
        return row if row and row["user_id"] == user_id else None

    async def get_project(user_id, project_id):
        row = projs.get(project_id)
        return row if row and row["user_id"] == user_id else None

    monkeypatch.setattr("db.conversation_store.get_conversation_meta", get_meta, raising=True)
    monkeypatch.setattr("db.project_store.get_project", get_project, raising=True)

    events: list[tuple[int, dict]] = []
    monkeypatch.setattr(
        file_routes.bus, "publish_to_user",
        lambda user_id, event: events.append((user_id, event)),
    )

    from chat.auth import get_current_user_cookie_or_apikey_checked

    app = FastAPI()
    app.include_router(file_routes.router)

    async def user_a():
        return {"id": 1, "email": "a@example.test"}

    app.dependency_overrides[get_current_user_cookie_or_apikey_checked] = user_a

    conv_root = chats / "inproj" / "workspace"
    proj_root = projects / "p1" / "workspace" / "workspace"
    conv_root.mkdir(parents=True)
    proj_root.mkdir(parents=True)
    (proj_root / "shared.md").write_text("# shared")
    (conv_root / "mine.md").write_text("# mine")

    return {
        "client": TestClient(app),
        "conv_root": conv_root,
        "proj_root": proj_root,
        "projects": projects,
        "chats": chats,
        "events": events,
    }


def _scopes(events):
    return [
        (e["scope"], e["conversation_id"], e["project_id"])
        for _uid, e in events if e["type"] == "file_list_changed"
    ]


# ---------------------------------------------------------------------------
# Project routes
# ---------------------------------------------------------------------------


class TestProjectRoutes:
    def test_list_hits_the_project_root(self, env):
        r = env["client"].get("/app/api/projects/p1/files")
        assert r.status_code == 200, r.text
        assert [f["name"] for f in r.json()["files"]] == ["shared.md"]

    def test_list_on_never_written_project_is_empty(self, env):
        r = env["client"].get("/app/api/projects/fresh/files")
        assert r.status_code == 200, r.text
        assert r.json()["files"] == []
        assert (env["projects"] / "fresh" / "workspace" / "workspace").is_dir()

    def test_upload_content_download_info_delete(self, env):
        c, proj_root = env["client"], env["proj_root"]
        r = c.post(
            "/app/api/projects/p1/files/upload",
            params={"path": "reports"},
            files={"files": ("q3.md", b"# q3", "text/markdown")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["errors"] == []
        assert (proj_root / "reports" / "q3.md").read_bytes() == b"# q3"
        assert not (env["conv_root"] / "reports").exists()

        r = c.get("/app/api/projects/p1/files/content", params={"path": "reports/q3.md"})
        assert r.status_code == 200 and r.json()["content"] == "# q3"

        r = c.get("/app/api/projects/p1/files/download", params={"path": "shared.md"})
        assert r.status_code == 200 and r.content == b"# shared"

        r = c.get("/app/api/projects/p1/files/info", params={"path": "reports"})
        assert r.json() == {"name": "reports", "type": "folder", "fileCount": 1}

        r = c.get("/app/api/projects/p1/files/download-folder", params={"path": "reports"})
        assert r.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert "reports/q3.md" in names

        r = c.request("DELETE", "/app/api/projects/p1/files", params={"path": "reports"})
        assert r.status_code == 200 and r.json()["deletedCount"] == 1
        assert not (proj_root / "reports").exists()

    def test_create_folder(self, env):
        r = env["client"].post(
            "/app/api/projects/p1/files/create-folder", json={"path": "", "name": "new"},
        )
        assert r.status_code == 200, r.text
        assert (env["proj_root"] / "new").is_dir()

    def test_path_errors_map_like_conversation_routes(self, env):
        c = env["client"]
        r = c.get("/app/api/projects/p1/files/content", params={"path": "../x"})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_path"
        r = c.get("/app/api/projects/p1/files/content", params={"path": "missing.md"})
        assert r.status_code == 404 and r.json()["detail"]["error"] == "not_found"

    def test_save_to_drive_reads_the_project_root(self, env, monkeypatch):
        sent = {}

        class _Resp:
            is_success = True

            def json(self):
                return {"id": "doc1", "name": "Shared"}

        async def fake_request(client, user, method, url, content=None, headers=None):
            sent["body"] = content
            return _Resp()

        monkeypatch.setattr(file_routes, "make_authenticated_request", fake_request)
        r = env["client"].post(
            "/app/api/projects/p1/files/save-to-drive",
            json={"path": "shared.md", "title": "Shared"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["url"].endswith("/document/d/doc1/edit")
        assert b"# shared" in sent["body"]

    @pytest.mark.parametrize("pid", ["p2", "missing", "..", "p1%2F..%2Fp2"])
    def test_ownership_404(self, env, pid):
        c = env["client"]
        calls = [
            ("GET", f"/app/api/projects/{pid}/files", {}),
            ("GET", f"/app/api/projects/{pid}/files/content", {"params": {"path": "shared.md"}}),
            ("GET", f"/app/api/projects/{pid}/files/download", {"params": {"path": "shared.md"}}),
            ("GET", f"/app/api/projects/{pid}/files/info", {"params": {"path": "shared.md"}}),
            ("DELETE", f"/app/api/projects/{pid}/files", {"params": {"path": "shared.md"}}),
            ("POST", f"/app/api/projects/{pid}/files/create-folder",
             {"json": {"path": "", "name": "x"}}),
            ("POST", f"/app/api/projects/{pid}/files/upload",
             {"files": {"files": ("x.txt", b"x", "text/plain")}}),
        ]
        for method, url, kw in calls:
            r = c.request(method, url, **kw)
            assert r.status_code == 404, (method, url, r.text)
        assert not (env["projects"] / "p2").exists()
        assert (env["proj_root"] / "shared.md").exists()


# ---------------------------------------------------------------------------
# Conversation routes on a project conversation
# ---------------------------------------------------------------------------


class TestProjectConversationRoutes:
    def test_conversation_routes_use_the_conversation_root(self, env):
        c, conv_root, proj_root = env["client"], env["conv_root"], env["proj_root"]
        r = c.get("/app/api/conversations/inproj/files")
        assert [f["name"] for f in r.json()["files"]] == ["mine.md"]
        r = c.post(
            "/app/api/conversations/inproj/files/create-folder",
            json={"path": "", "name": "out"},
        )
        assert r.status_code == 200
        assert (conv_root / "out").is_dir() and not (proj_root / "out").exists()
        r = c.get("/app/api/conversations/inproj/files/content", params={"path": "shared.md"})
        assert r.status_code == 404  # strict addressing: no fallback to the project space


# ---------------------------------------------------------------------------
# Copy routes
# ---------------------------------------------------------------------------


class TestCopyRoutes:
    def test_copy_to_project(self, env):
        c = env["client"]
        r = c.post(
            "/app/api/conversations/inproj/files/copy-to-project",
            json={"path": "mine.md", "dest": "reports/mine.md"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "/reports/mine.md"
        assert (env["proj_root"] / "reports" / "mine.md").read_text() == "# mine"
        assert (env["conv_root"] / "mine.md").exists()

    def test_copy_from_project_defaults_dest_to_path(self, env):
        r = env["client"].post(
            "/app/api/conversations/inproj/files/copy-from-project",
            json={"path": "/shared.md", "dest": ""},
        )
        assert r.status_code == 200, r.text
        assert (env["conv_root"] / "shared.md").read_text() == "# shared"
        assert (env["proj_root"] / "shared.md").exists()

    def test_conflict_then_overwrite(self, env):
        c = env["client"]
        (env["proj_root"] / "mine.md").write_text("old")
        url = "/app/api/conversations/inproj/files/copy-to-project"
        r = c.post(url, json={"path": "mine.md"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "destination_exists"
        assert (env["proj_root"] / "mine.md").read_text() == "old"
        r = c.post(url, json={"path": "mine.md", "overwrite": True})
        assert r.status_code == 200, r.text
        assert (env["proj_root"] / "mine.md").read_text() == "# mine"

    def test_move_removes_source(self, env):
        c = env["client"]
        r = c.post(
            "/app/api/conversations/inproj/files/copy-from-project",
            json={"path": "shared.md", "move": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["moved"] is True
        assert not (env["proj_root"] / "shared.md").exists()
        assert (env["conv_root"] / "shared.md").exists()

    def test_standalone_conversation_400(self, env):
        (env["chats"] / "solo" / "workspace").mkdir(parents=True)
        (env["chats"] / "solo" / "workspace" / "a.txt").write_text("a")
        for direction in ("copy-to-project", "copy-from-project"):
            r = env["client"].post(
                f"/app/api/conversations/solo/files/{direction}", json={"path": "a.txt"},
            )
            assert r.status_code == 400
            assert r.json()["detail"]["error"] == "not_a_project_conversation"

    def test_other_users_conversation_404(self, env):
        r = env["client"].post(
            "/app/api/conversations/theirs/files/copy-from-project", json={"path": "x"},
        )
        assert r.status_code == 404

    @pytest.mark.parametrize("path", [
        ".responses/r.json", "pasted/p.png", ".subagent_responses/bob/r.md", "pasted",
    ])
    def test_scratch_sources_refused_to_project(self, env, path):
        src = env["conv_root"] / path
        src.parent.mkdir(parents=True, exist_ok=True)
        if not src.suffix:
            src.mkdir()
        else:
            src.write_text("scratch")
        r = env["client"].post(
            "/app/api/conversations/inproj/files/copy-to-project", json={"path": path},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "forbidden_source"
        assert sorted(p.name for p in env["proj_root"].iterdir()) == ["shared.md"]

    def test_scratch_names_allowed_from_project(self, env):
        (env["proj_root"] / "pasted").mkdir()
        (env["proj_root"] / "pasted" / "logo.png").write_bytes(b"png")
        r = env["client"].post(
            "/app/api/conversations/inproj/files/copy-from-project",
            json={"path": "pasted/logo.png"},
        )
        assert r.status_code == 200, r.text
        assert (env["conv_root"] / "pasted" / "logo.png").read_bytes() == b"png"

    @pytest.mark.parametrize("body,status,code", [
        ({"path": "missing.md"}, 404, "not_found"),
        ({"path": "../x"}, 400, "invalid_path"),
        ({"path": "mine.md", "dest": "../../escape.md"}, 400, "invalid_path"),
        ({"path": ""}, 400, "invalid_path"),
    ])
    def test_error_mapping(self, env, body, status, code):
        r = env["client"].post(
            "/app/api/conversations/inproj/files/copy-to-project", json=body,
        )
        assert r.status_code == status, r.text
        assert r.json()["detail"]["error"] == code

    def test_fifo_source_400(self, env):
        import os
        os.mkfifo(env["conv_root"] / "pipe")
        r = env["client"].post(
            "/app/api/conversations/inproj/files/copy-to-project", json={"path": "pipe"},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "not_a_regular_file"


# ---------------------------------------------------------------------------
# file_list_changed scope per publisher
# ---------------------------------------------------------------------------


class TestFileListChangedScope:
    def test_conversation_route_publishes_conversation_scope(self, env):
        env["client"].post(
            "/app/api/conversations/inproj/files/upload",
            files={"files": ("a.txt", b"a", "text/plain")},
        )
        env["client"].request(
            "DELETE", "/app/api/conversations/inproj/files", params={"path": "a.txt"},
        )
        assert _scopes(env["events"]) == [
            ("conversation", "inproj", "p1"),
            ("conversation", "inproj", "p1"),
        ]

    def test_composer_attachment_publishes_conversation_scope(self, env):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        r = env["client"].post(
            "/app/api/conversations/inproj/composer-attachments",
            files={"files": ("shot.png", png, "image/png")},
        )
        assert r.status_code == 200, r.text
        assert _scopes(env["events"]) == [("conversation", "inproj", "p1")]

    def test_project_route_publishes_project_scope(self, env):
        c = env["client"]
        c.post(
            "/app/api/projects/p1/files/upload",
            files={"files": ("a.txt", b"a", "text/plain")},
        )
        c.post("/app/api/projects/p1/files/create-folder", json={"path": "", "name": "d"})
        c.request("DELETE", "/app/api/projects/p1/files", params={"path": "a.txt"})
        assert _scopes(env["events"]) == [("project", None, "p1")] * 3
        assert all(uid == 1 for uid, _ in env["events"])

    def test_copy_route_publishes_both_scopes(self, env):
        r = env["client"].post(
            "/app/api/conversations/inproj/files/copy-to-project", json={"path": "mine.md"},
        )
        assert r.status_code == 200, r.text
        assert sorted(_scopes(env["events"])) == [
            ("conversation", "inproj", "p1"),
            ("project", "inproj", "p1"),
        ]

    def test_failed_copy_publishes_nothing(self, env):
        env["client"].post(
            "/app/api/conversations/inproj/files/copy-to-project", json={"path": "nope"},
        )
        assert env["events"] == []

    def test_reads_publish_nothing(self, env):
        c = env["client"]
        c.get("/app/api/projects/p1/files")
        c.get("/app/api/conversations/inproj/files")
        assert env["events"] == []


class TestMakeFileListChanged:
    @pytest.mark.parametrize("scope", ["conversation", "project"])
    def test_valid_scopes(self, scope):
        ev = realtime_events.make_file_list_changed(
            conversation_id="c", project_id="p", scope=scope,
        )
        assert ev == {
            "type": "file_list_changed", "conversation_id": "c",
            "project_id": "p", "scope": scope,
        }

    def test_scope_not_derived_from_project_id(self):
        ev = realtime_events.make_file_list_changed(
            conversation_id="c", project_id="p", scope="conversation",
        )
        assert ev["scope"] == "conversation"

    @pytest.mark.parametrize("scope", ["", "Project", "workspace", None])
    def test_rejects_bad_scope(self, scope):
        with pytest.raises(ValueError):
            realtime_events.make_file_list_changed(
                conversation_id="c", project_id="p", scope=scope,
            )
