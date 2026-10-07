"""Non-regular workspace files must never block host-side reads.

The script sandbox mounts the conversation workspace read-write and its
seccomp profile only denies symlink creation, so a script can plant a FIFO
(``os.mkfifo``) at any workspace path. A blocking ``open()`` on a FIFO with
no writer never returns; the file preview / Save-to-Drive routes call the
sync ``get_file_content`` helper straight from the async handler, so one
such request would freeze the whole event loop. ``create_folder_zip`` hands
``os.walk``'s ``files`` entries to ``ZipFile.write`` and would hang the same
way. Both helpers must reject anything that is not a regular file.
"""

import os
import threading
from pathlib import Path

import pytest

from chat.file_storage import create_folder_zip, get_file_content


def _call_with_timeout(fn, timeout=3.0):
    """Run ``fn`` on a worker thread; fail the test instead of hanging."""
    outcome = {}

    def runner():
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"{fn.__name__ if hasattr(fn, '__name__') else fn} blocked for {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_get_file_content_rejects_fifo(workspace_root: Path):
    os.mkfifo(workspace_root / "pipe.txt")

    with pytest.raises(ValueError, match="Not a file"):
        _call_with_timeout(lambda: get_file_content(workspace_root, "pipe.txt"))


def test_get_file_content_rejects_fifo_swapped_in_after_type_check(
    workspace_root: Path, monkeypatch
):
    """The descriptor-level S_ISREG check closes the check-then-open race."""
    target = workspace_root / "notes.md"
    target.write_text("hello")
    fifo = workspace_root / "fifo.tmp"
    os.mkfifo(fifo)

    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):
        # Replace the regular file with the FIFO right before the open.
        if Path(path) == target:
            os.replace(fifo, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(ValueError, match="Not a file"):
        _call_with_timeout(lambda: get_file_content(workspace_root, "notes.md"))


def test_get_file_content_still_reads_regular_files(workspace_root: Path):
    (workspace_root / "notes.md").write_text("# hi\n", encoding="utf-8")

    content, name, size = get_file_content(workspace_root, "notes.md")

    assert content == "# hi\n"
    assert name == "notes.md"
    assert size == 5


def test_create_folder_zip_rejects_fifo_descendant(workspace_root: Path):
    sub = workspace_root / "sub"
    sub.mkdir()
    (sub / "ok.txt").write_text("fine")
    os.mkfifo(sub / "pipe.md")

    with pytest.raises(ValueError, match="special files"):
        _call_with_timeout(lambda: create_folder_zip(workspace_root, "sub"))
