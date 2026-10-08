"""``GET .../files/download-sanitized`` on both workspace route sets.

The route reads the workspace file with ``read_file_bytes`` (the same
non-blocking regular-file contract as the preview route), rewrites it with
``chat.image_sanitizer`` in a thread and serves the copy as an attachment;
nothing is written to the workspace. Format is picked by magic bytes, so
a renamed non-image is 400 ``unsanitizable_image``.
"""

from __future__ import annotations

import io
import os
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import chat.file_routes as file_routes
import chat.storage as storage_mod
from chat.file_storage import read_file_bytes

SECRET = b"api_key=sk-live-0123456789"


def _png_with_secret() -> bytes:
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", SECRET.decode())
    buf = io.BytesIO()
    Image.new("RGB", (3, 2), (10, 200, 30)).save(buf, "PNG", pnginfo=info)
    return buf.getvalue() + SECRET


@pytest.fixture()
def env(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    projects = tmp_path / "projects"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(storage_mod, "PROJECTS_DIR", projects, raising=True)

    convs = {
        "c1": {"id": "c1", "user_id": 1, "project_id": None},
        "theirs": {"id": "theirs", "user_id": 2, "project_id": None},
    }
    projs = {"p1": {"id": "p1", "user_id": 1}, "p2": {"id": "p2", "user_id": 2}}

    async def get_meta(user_id, conversation_id):
        row = convs.get(conversation_id)
        return row if row and row["user_id"] == user_id else None

    async def get_project(user_id, project_id):
        row = projs.get(project_id)
        return row if row and row["user_id"] == user_id else None

    monkeypatch.setattr("db.conversation_store.get_conversation_meta", get_meta, raising=True)
    monkeypatch.setattr("db.project_store.get_project", get_project, raising=True)
    monkeypatch.setattr(file_routes.bus, "publish_to_user", lambda *a, **kw: None)

    from chat.auth import get_current_user_cookie_or_apikey_checked

    app = FastAPI()
    app.include_router(file_routes.router)

    async def user_a():
        return {"id": 1, "email": "a@example.test"}

    app.dependency_overrides[get_current_user_cookie_or_apikey_checked] = user_a

    conv_root = chats / "c1" / "workspace"
    proj_root = projects / "p1" / "workspace" / "workspace"
    conv_root.mkdir(parents=True)
    proj_root.mkdir(parents=True)
    for root in (conv_root, proj_root):
        (root / "chart.png").write_bytes(_png_with_secret())
        (root / "notes.png").write_bytes(b"# not an image, whatever the name says")
        (root / "sub").mkdir()
    return {"client": TestClient(app), "conv_root": conv_root, "proj_root": proj_root}


@pytest.mark.parametrize("prefix", ["/app/api/conversations/c1", "/app/api/projects/p1"])
class TestSanitizedDownload:
    def test_serves_a_stripped_attachment(self, env, prefix):
        r = env["client"].get(f"{prefix}/files/download-sanitized", params={"path": "chart.png"})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
        assert r.headers["content-disposition"].startswith('attachment; filename="chart.png"')
        assert SECRET not in r.content
        with Image.open(io.BytesIO(r.content)) as img:
            assert img.size == (3, 2)
            assert img.getpixel((0, 0)) == (10, 200, 30)
            assert img.text["Comment"] == "Generated with Quest"
        # Nothing written: the workspace still holds the original only.
        root = env["conv_root"] if "conversations" in prefix else env["proj_root"]
        assert sorted(p.name for p in root.iterdir()) == ["chart.png", "notes.png", "sub"]
        assert SECRET in (root / "chart.png").read_bytes()

    def test_non_image_bytes_are_refused(self, env, prefix):
        r = env["client"].get(f"{prefix}/files/download-sanitized", params={"path": "notes.png"})
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "unsanitizable_image"

    def test_missing_and_invalid_paths(self, env, prefix):
        c = env["client"]
        r = c.get(f"{prefix}/files/download-sanitized", params={"path": "nope.png"})
        assert r.status_code == 404
        r = c.get(f"{prefix}/files/download-sanitized", params={"path": "sub"})
        assert r.status_code == 400
        r = c.get(f"{prefix}/files/download-sanitized", params={"path": "../../x.png"})
        assert r.status_code == 400


@pytest.mark.parametrize("url", [
    "/app/api/conversations/theirs/files/download-sanitized",
    "/app/api/conversations/missing/files/download-sanitized",
    "/app/api/projects/p2/files/download-sanitized",
    "/app/api/projects/missing/files/download-sanitized",
])
def test_ownership_404(env, url):
    r = env["client"].get(url, params={"path": "chart.png"})
    assert r.status_code == 404, r.text


def test_read_file_bytes_rejects_fifo(tmp_path):
    """Same event-loop guard as the preview route: a planted FIFO must not
    block the read."""
    root = tmp_path / "ws"
    root.mkdir()
    os.mkfifo(root / "pipe.png")
    outcome = {}

    def run():
        try:
            read_file_bytes(root, "pipe.png")
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(3.0)
    assert not t.is_alive(), "read_file_bytes blocked on a FIFO"
    assert isinstance(outcome.get("error"), ValueError)
