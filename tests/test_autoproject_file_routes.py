"""AutoProject workspace file routes and the run copy routes (chat/file_routes.py).

* ``/autoprojects/{id}/files...`` mirror the project file routes over the
  AutoProject workspace (``AUTOPROJECTS_DIR/<id>/workspace``), resolved through
  ``resolve_autoproject_workspace_for_route``: 403 ``autoprojects_disabled`` while
  the gate is closed, the same 404 body for a missing and a foreign
  AutoProject, archived AutoProjects still browsable; writes publish
  ``file_list_changed`` with scope ``autoproject`` and the AutoProject id.
* ``/conversations/{cid}/files/copy-to-autoproject`` / ``copy-from-autoproject``
  copy or move entries between a run's conversation workspace and its
  AutoProject's workspace: 400 ``not_an_autoproject_run_conversation`` for any other
  conversation, scratch sources refused into the AutoProject, path escapes and
  symlinks refused, events for both spaces.

Uses the ``env`` fixture of tests/test_autoproject_routes.py (real rows on
the isolated database, temp data dirs, events captured).
"""

from __future__ import annotations

import io
import os
import uuid
import zipfile

import pytest

import chat.file_routes as file_routes
from tests.test_autoproject_routes import (  # noqa: F401  (env is a fixture)
    DISABLED_BODY,
    _run,
    client,
    detail,
    env,
    finished_run,
    make_autoproject,
    not_found_body,
    start_run,
    uid,
)


def files_client(env, who="alice"):
    return client(env, who, file_routes.router)


def _scopes(env):
    return [
        (ev["scope"], ev["conversation_id"], ev["project_id"], ev.get("autoproject_id"))
        for _uid, ev in env.published if ev["type"] == "file_list_changed"
    ]


@pytest.fixture()
def ws(env):
    """Alice's AutoProject with a file in its workspace, and a finished run
    whose conversation workspace has a file of its own."""
    from chat.storage import ChatStorage

    autoproject = make_autoproject(env)
    root = ChatStorage.get_autoproject_workspace_root(autoproject["id"])
    root.mkdir(parents=True)
    (root / "shared.md").write_text("# shared")
    run = finished_run(env, autoproject)
    conv_root = ChatStorage.get_conversation_workspace_root(run["conversation_id"])
    conv_root.mkdir(parents=True, exist_ok=True)
    (conv_root / "mine.md").write_text("# mine")
    env.published.clear()
    return {"autoproject": autoproject, "root": root, "run": run, "conv_root": conv_root}


def _ap_calls(autoproject_id):
    base = f"/app/api/autoprojects/{autoproject_id}/files"
    return [
        ("GET", base, {}),
        ("GET", f"{base}/content", {"params": {"path": "shared.md"}}),
        ("GET", f"{base}/download", {"params": {"path": "shared.md"}}),
        ("GET", f"{base}/download-sanitized", {"params": {"path": "shared.md"}}),
        ("GET", f"{base}/info", {"params": {"path": "shared.md"}}),
        ("GET", f"{base}/download-folder", {"params": {"path": "folder"}}),
        ("POST", f"{base}/save-to-drive", {"json": {"path": "shared.md", "title": "S"}}),
        ("DELETE", base, {"params": {"path": "shared.md"}}),
        ("POST", f"{base}/create-folder", {"json": {"path": "", "name": "x"}}),
        ("POST", f"{base}/upload", {"files": {"files": ("x.txt", b"x", "text/plain")}}),
    ]


# ---------------------------------------------------------------------------
# AutoProject workspace routes
# ---------------------------------------------------------------------------


class TestAutoProjectFileRoutes:
    def test_list_and_never_written_autoproject(self, env, ws):
        c = files_client(env)
        r = c.get(f"/app/api/autoprojects/{ws['autoproject']['id']}/files")
        assert r.status_code == 200, r.text
        assert [f["name"] for f in r.json()["files"]] == ["shared.md"]

        fresh = make_autoproject(env, title="Fresh")
        r = c.get(f"/app/api/autoprojects/{fresh['id']}/files")
        assert r.status_code == 200 and r.json()["files"] == []
        assert (env.dirs["autoprojects"] / fresh["id"] / "workspace").is_dir()

    def test_upload_content_download_info_folder_delete(self, env, ws):
        c, root = files_client(env), ws["root"]
        base = f"/app/api/autoprojects/{ws['autoproject']['id']}/files"
        r = c.post(f"{base}/upload", params={"path": "reports"},
                   files={"files": ("q3.md", b"# q3", "text/markdown")})
        assert r.status_code == 200, r.text
        assert r.json()["errors"] == []
        assert (root / "reports" / "q3.md").read_bytes() == b"# q3"
        # Only the AutoProject space was written.
        assert not (ws["conv_root"] / "reports").exists()

        r = c.get(f"{base}/content", params={"path": "reports/q3.md"})
        assert r.status_code == 200 and r.json()["content"] == "# q3"
        r = c.get(f"{base}/download", params={"path": "shared.md"})
        assert r.status_code == 200 and r.content == b"# shared"
        r = c.get(f"{base}/info", params={"path": "reports"})
        assert r.json() == {"name": "reports", "type": "folder", "fileCount": 1}
        r = c.get(f"{base}/download-folder", params={"path": "reports"})
        assert "reports/q3.md" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        r = c.post(f"{base}/create-folder", json={"path": "", "name": "drafts"})
        assert r.status_code == 200 and (root / "drafts").is_dir()
        r = c.request("DELETE", base, params={"path": "reports"})
        assert r.status_code == 200 and r.json()["deletedCount"] == 1
        assert not (root / "reports").exists()

    def test_writes_publish_autoproject_scope_reads_nothing(self, env, ws):
        c = files_client(env)
        ap_id = ws["autoproject"]["id"]
        base = f"/app/api/autoprojects/{ap_id}/files"
        c.get(base)
        c.get(f"{base}/content", params={"path": "shared.md"})
        assert env.published == []
        c.post(f"{base}/upload", files={"files": ("a.txt", b"a", "text/plain")})
        c.post(f"{base}/create-folder", json={"path": "", "name": "d"})
        c.request("DELETE", base, params={"path": "a.txt"})
        assert _scopes(env) == [("autoproject", None, None, ap_id)] * 3
        assert all(user_id == uid(env) for user_id, _ev in env.published)

    def test_archived_autoproject_stays_browsable(self, env, ws):
        ap_id = ws["autoproject"]["id"]
        from chat.autoprojects import service

        _run(service.set_autoproject_archived(env.users["alice"], ap_id, True))
        c = files_client(env)
        r = c.get(f"/app/api/autoprojects/{ap_id}/files")
        assert r.status_code == 200 and [f["name"] for f in r.json()["files"]] == ["shared.md"]
        r = c.post(f"/app/api/autoprojects/{ap_id}/files/create-folder",
                   json={"path": "", "name": "later"})
        assert r.status_code == 200

    @pytest.mark.parametrize("path,status,code", [
        ("../x", 400, "invalid_path"),
        ("../../chats/x", 400, "invalid_path"),
        ("missing.md", 404, "not_found"),
    ])
    def test_path_errors(self, env, ws, path, status, code):
        r = files_client(env).get(
            f"/app/api/autoprojects/{ws['autoproject']['id']}/files/content",
            params={"path": path},
        )
        assert r.status_code == status, r.text
        assert detail(r)["error"] == code

    def test_symlink_leaf_is_not_followed(self, env, ws, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("outside")
        os.symlink(secret, ws["root"] / "link.txt")
        c = files_client(env)
        base = f"/app/api/autoprojects/{ws['autoproject']['id']}/files"
        for url in (f"{base}/content", f"{base}/download", f"{base}/info"):
            r = c.get(url, params={"path": "link.txt"})
            assert r.status_code == 400, (url, r.status_code)
            assert detail(r)["error"] == "invalid_path"
            assert b"outside" not in r.content

    def test_gate_closed_403_everywhere(self, env, ws):
        env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, False)
        c = files_client(env)
        for method, url, kw in _ap_calls(ws["autoproject"]["id"]):
            r = c.request(method, url, **kw)
            assert r.status_code == 403, (method, url, r.text)
            assert r.json() == DISABLED_BODY
        assert (ws["root"] / "shared.md").exists()
        assert env.published == []

    def test_foreign_and_missing_404_same_body(self, env, ws, monkeypatch):
        async def no_drive(*_a, **_kw):
            raise AssertionError("Drive must not be called for a foreign AutoProject")

        monkeypatch.setattr(file_routes, "make_authenticated_request", no_drive)
        (ws["root"] / "folder").mkdir()
        bob = files_client(env, "bob")
        ap_id = ws["autoproject"]["id"]
        missing_id = str(uuid.uuid4())
        for (method, url, kw), (_m, missing_url, _kw) in zip(
            _ap_calls(ap_id), _ap_calls(missing_id),
        ):
            foreign = bob.request(method, url, **kw)
            assert foreign.status_code == 404, (method, url, foreign.text)
            assert foreign.json() == not_found_body(ap_id)
            missing = bob.request(method, missing_url, **kw)
            assert missing.content == foreign.content.replace(
                ap_id.encode(), missing_id.encode(),
            )
        assert sorted(p.name for p in ws["root"].iterdir()) == ["folder", "shared.md"]
        assert not (env.dirs["autoprojects"] / missing_id).exists()
        assert env.published == []

    @pytest.mark.parametrize("bad_id", ["..", "x%2F..%2F..", "."])
    def test_non_canonical_ids_404(self, env, ws, bad_id):
        r = files_client(env).get(f"/app/api/autoprojects/{bad_id}/files")
        assert r.status_code == 404

    def test_save_to_drive_reads_the_autoproject_root(self, env, ws, monkeypatch):
        sent = {}

        class _Resp:
            is_success = True

            def json(self):
                return {"id": "doc1", "name": "Shared"}

        async def fake_request(client_, user, method, url, content=None, headers=None):
            sent["body"], sent["user"] = content, user
            return _Resp()

        monkeypatch.setattr(file_routes, "make_authenticated_request", fake_request)
        r = files_client(env).post(
            f"/app/api/autoprojects/{ws['autoproject']['id']}/files/save-to-drive",
            json={"path": "shared.md", "title": "Shared"},
        )
        assert r.status_code == 200, r.text
        assert b"# shared" in sent["body"]
        assert sent["user"]["id"] == uid(env)


# ---------------------------------------------------------------------------
# Copy between a run's conversation workspace and its AutoProject's workspace
# ---------------------------------------------------------------------------


class TestCopyRoutes:
    def _url(self, ws_or_cid, direction):
        cid = ws_or_cid if isinstance(ws_or_cid, str) else ws_or_cid["run"]["conversation_id"]
        return f"/app/api/conversations/{cid}/files/{direction}"

    def test_copy_to_autoproject(self, env, ws):
        r = files_client(env).post(
            self._url(ws, "copy-to-autoproject"), json={"path": "mine.md", "dest": "out/mine.md"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["path"] == "/out/mine.md"
        assert (ws["root"] / "out" / "mine.md").read_text() == "# mine"
        assert (ws["conv_root"] / "mine.md").exists()
        cid, ap_id = ws["run"]["conversation_id"], ws["autoproject"]["id"]
        # Both spaces announced; an AutoProject run is never in a project.
        assert sorted(_scopes(env)) == [
            ("autoproject", cid, None, ap_id),
            ("conversation", cid, None, None),
        ]

    def test_copy_from_autoproject_defaults_dest_to_path(self, env, ws):
        r = files_client(env).post(
            self._url(ws, "copy-from-autoproject"), json={"path": "/shared.md", "dest": ""},
        )
        assert r.status_code == 200, r.text
        assert (ws["conv_root"] / "shared.md").read_text() == "# shared"
        assert (ws["root"] / "shared.md").exists()

    def test_conflict_then_overwrite(self, env, ws):
        (ws["root"] / "mine.md").write_text("old")
        c, url = files_client(env), self._url(ws, "copy-to-autoproject")
        r = c.post(url, json={"path": "mine.md"})
        assert r.status_code == 409 and detail(r)["error"] == "destination_exists"
        assert (ws["root"] / "mine.md").read_text() == "old"
        r = c.post(url, json={"path": "mine.md", "overwrite": True})
        assert r.status_code == 200, r.text
        assert (ws["root"] / "mine.md").read_text() == "# mine"

    @pytest.mark.parametrize("direction,src_key,dst_key,name", [
        ("copy-from-autoproject", "root", "conv_root", "shared.md"),
        ("copy-to-autoproject", "conv_root", "root", "mine.md"),
    ])
    def test_move_removes_source(self, env, ws, direction, src_key, dst_key, name):
        r = files_client(env).post(self._url(ws, direction), json={"path": name, "move": True})
        assert r.status_code == 200, r.text
        assert r.json()["moved"] is True
        assert not (ws[src_key] / name).exists()
        assert (ws[dst_key] / name).exists()

    @pytest.mark.parametrize("path", [
        ".responses/r.json", "pasted/p.png", ".subagent_responses/bob/r.md", "pasted",
    ])
    def test_scratch_sources_refused_into_the_autoproject(self, env, ws, path):
        src = ws["conv_root"] / path
        src.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix:
            src.write_text("scratch")
        else:
            src.mkdir()
        r = files_client(env).post(self._url(ws, "copy-to-autoproject"), json={"path": path})
        assert r.status_code == 400
        assert detail(r)["error"] == "forbidden_source"
        assert "autoproject" in detail(r)["message"]
        assert sorted(p.name for p in ws["root"].iterdir()) == ["shared.md"]
        assert env.published == []

    def test_scratch_names_allowed_from_the_autoproject(self, env, ws):
        (ws["root"] / "pasted").mkdir()
        (ws["root"] / "pasted" / "logo.png").write_bytes(b"png")
        r = files_client(env).post(
            self._url(ws, "copy-from-autoproject"), json={"path": "pasted/logo.png"},
        )
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize("body,status,code", [
        ({"path": "missing.md"}, 404, "not_found"),
        ({"path": "../x"}, 400, "invalid_path"),
        ({"path": "mine.md", "dest": "../../escape.md"}, 400, "invalid_path"),
        ({"path": ""}, 400, "invalid_path"),
    ])
    def test_error_mapping(self, env, ws, body, status, code):
        r = files_client(env).post(self._url(ws, "copy-to-autoproject"), json=body)
        assert r.status_code == status, r.text
        assert detail(r)["error"] == code
        assert env.published == []

    def test_symlink_source_refused(self, env, ws, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("outside")
        os.symlink(secret, ws["conv_root"] / "link.txt")
        r = files_client(env).post(self._url(ws, "copy-to-autoproject"), json={"path": "link.txt"})
        assert r.status_code == 400, r.text
        assert detail(r)["error"] == "invalid_path"
        assert not (ws["root"] / "link.txt").exists()

    def test_not_a_run_conversation_400(self, env, ws):
        from chat.storage import ChatStorage
        from db import project_store

        standalone, _ = _run(ChatStorage.create_conversation(uid(env)))
        project = _run(project_store.create_project(uid(env), "Proj"))
        in_project, _ = _run(ChatStorage.create_project_conversation(uid(env), project["id"]))
        c = files_client(env)
        for cid in (standalone, in_project):
            for direction in ("copy-to-autoproject", "copy-from-autoproject"):
                r = c.post(self._url(cid, direction), json={"path": "a.txt"})
                assert r.status_code == 400, (cid, direction, r.text)
                assert detail(r)["error"] == "not_an_autoproject_run_conversation"
        # And the project copy routes refuse a run conversation.
        for direction in ("copy-to-project", "copy-from-project"):
            r = c.post(self._url(ws, direction), json={"path": "mine.md"})
            assert r.status_code == 400
            assert detail(r)["error"] == "not_a_project_conversation"

    def test_foreign_run_conversation_404(self, env, ws):
        for direction in ("copy-to-autoproject", "copy-from-autoproject"):
            r = files_client(env, "bob").post(self._url(ws, direction), json={"path": "mine.md"})
            assert r.status_code == 404
        assert not (ws["root"] / "mine.md").exists()

    def test_gate_closed_403(self, env, ws):
        env.fg.set_feature_enabled(env.fg.FEATURE_AUTOPROJECTS, False)
        for direction in ("copy-to-autoproject", "copy-from-autoproject"):
            r = files_client(env).post(self._url(ws, direction), json={"path": "mine.md"})
            assert r.status_code == 403
            assert r.json() == DISABLED_BODY
        assert not (ws["root"] / "mine.md").exists()

    def test_archived_autoproject_allowed(self, env, ws):
        from chat.autoprojects import service

        _run(service.set_autoproject_archived(env.users["alice"], ws["autoproject"]["id"], True))
        r = files_client(env).post(self._url(ws, "copy-to-autoproject"), json={"path": "mine.md"})
        assert r.status_code == 200, r.text

    def test_active_run_conversation_copies_too(self, env, ws):
        from chat.storage import ChatStorage

        run = start_run(env, ws["autoproject"])
        root = ChatStorage.get_conversation_workspace_root(run["conversation_id"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "live.md").write_text("live")
        r = files_client(env).post(
            self._url(run["conversation_id"], "copy-to-autoproject"), json={"path": "live.md"},
        )
        assert r.status_code == 200, r.text
        assert (ws["root"] / "live.md").read_text() == "live"


# ---------------------------------------------------------------------------
# The conversation routes of a run conversation use its own workspace
# ---------------------------------------------------------------------------


def test_run_conversation_routes_use_the_conversation_workspace(env, ws):
    c = files_client(env)
    cid = ws["run"]["conversation_id"]
    r = c.get(f"/app/api/conversations/{cid}/files")
    assert [f["name"] for f in r.json()["files"]] == ["mine.md"]
    # No fallback into the AutoProject space.
    r = c.get(f"/app/api/conversations/{cid}/files/content", params={"path": "shared.md"})
    assert r.status_code == 404
    c.post(f"/app/api/conversations/{cid}/files/upload",
           files={"files": ("a.txt", b"a", "text/plain")})
    assert (ws["conv_root"] / "a.txt").exists() and not (ws["root"] / "a.txt").exists()
    assert _scopes(env) == [("conversation", cid, None, None)]


class TestMakeFileListChangedAutoProjectScope:
    def test_payload(self):
        from chat.realtime import events as realtime_events

        assert realtime_events.make_file_list_changed(
            conversation_id=None, project_id=None, scope="autoproject", autoproject_id="a1",
        ) == {
            "type": "file_list_changed", "conversation_id": None, "project_id": None,
            "scope": "autoproject", "autoproject_id": "a1",
        }
        # Other scopes keep their shape (no autoproject_id key).
        assert "autoproject_id" not in realtime_events.make_file_list_changed(
            conversation_id="c", project_id=None, scope="conversation",
        )

    def test_autoproject_scope_requires_the_id(self):
        from chat.realtime import events as realtime_events

        with pytest.raises(ValueError):
            realtime_events.make_file_list_changed(
                conversation_id=None, project_id=None, scope="autoproject",
            )
