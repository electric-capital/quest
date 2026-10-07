"""Workspace file routes must not run filesystem walks on the event loop.

Security finding #279201: the file-info and delete routes counted a folder's
descendants with ``Path.rglob`` (and the zip download walked and compressed
a whole subtree) synchronously inside ``async`` handlers. A sandbox script
can plant an arbitrarily large or deep tree in its workspace, so one request
against that tree stalled every other request and WebSocket on the server.
The sync helpers now run via ``asyncio.to_thread`` so the loop keeps
serving while the walk proceeds.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import chat.file_routes as file_routes

BLOCK_SECONDS = 0.4
HEARTBEAT_SECONDS = 0.01


async def _run_with_heartbeat(coro):
    """Run ``coro`` while counting how many times the loop got a turn."""
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            ticks += 1

    hb = asyncio.ensure_future(heartbeat())
    try:
        result = await coro
    finally:
        hb.cancel()
    return result, ticks


def _slow(return_value):
    def helper(*_args, **_kwargs):
        time.sleep(BLOCK_SECONDS)
        return return_value
    return helper


@pytest.fixture
def owned_workspace(tmp_path: Path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()

    async def resolve(_user_id, _conversation_id):
        return {"project_id": None}, root

    monkeypatch.setattr(file_routes, "resolve_owned_workspace", resolve)
    monkeypatch.setattr(
        file_routes, "_publish_file_list_changed", lambda *a, **k: None,
    )
    return root


USER = {"id": 1}
CASES = [
    (
        "list_workspace_files",
        {"currentPath": "", "files": [], "canGoUp": False},
        lambda: file_routes.list_files("conv", "", USER),
    ),
    (
        "get_file_content",
        ("hello", "notes.md", 5),
        lambda: file_routes.read_file_content("conv", "notes.md", USER),
    ),
    (
        "create_folder_zip",
        (Path("/nonexistent/archive.zip"), "sub"),
        lambda: file_routes.download_folder_as_zip("conv", "sub", USER),
    ),
    (
        "count_workspace_item_files",
        {"name": "sub", "type": "folder", "count": 3},
        lambda: file_routes.file_info("conv", "sub", USER),
    ),
    (
        "delete_workspace_item",
        {"name": "sub", "type": "folder", "deleted_count": 3},
        lambda: file_routes.delete_file("conv", "sub", USER),
    ),
]


@pytest.mark.parametrize("helper_name,helper_result,call", CASES,
                         ids=[c[0] for c in CASES])
def test_route_keeps_event_loop_responsive(
    owned_workspace, monkeypatch, helper_name, helper_result, call,
):
    monkeypatch.setattr(file_routes, helper_name, _slow(helper_result))

    result, ticks = asyncio.run(_run_with_heartbeat(call()))

    assert result is not None
    # A synchronous call would starve the heartbeat for the whole
    # BLOCK_SECONDS; off-thread it keeps ticking roughly every 10ms.
    expected = BLOCK_SECONDS / HEARTBEAT_SECONDS
    assert ticks > expected / 4, (
        f"{helper_name} blocked the event loop: {ticks} heartbeat ticks "
        f"during a {BLOCK_SECONDS}s call (expected ~{expected:.0f})"
    )


def test_route_still_maps_helper_errors(owned_workspace, monkeypatch):
    """Offloading must not swallow the ValueError/FileNotFoundError mapping."""
    from fastapi import HTTPException

    def boom(*_a, **_k):
        raise FileNotFoundError("Not found: sub")

    monkeypatch.setattr(file_routes, "count_workspace_item_files", boom)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(file_routes.file_info("conv", "sub", USER))
    assert exc_info.value.status_code == 404
