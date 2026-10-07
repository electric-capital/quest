"""``chat.file_storage.copy_entry`` -- copying between workspace roots.

Devplan 00009 sections 6.3 / 10.8: both ends validated against their own
root, symlink / special-file leaves refused, symlinks and special files
inside a directory skipped, dot-entries skipped unless named or
``include_hidden``, ``destination_exists`` without ``overwrite`` (nothing
written), directory merge with ``overwrite``, self/subtree destinations
refused, and ``move`` deleting the source only after a complete copy.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

import chat.file_storage as fs
from chat.file_storage import CopyEntryError, copy_entry, is_scratch_source


@pytest.fixture()
def spaces(tmp_path):
    src = tmp_path / "conv"
    dst = tmp_path / "proj"
    src.mkdir()
    dst.mkdir()
    return src, dst


def _copy(src_root, src_rel, dst_root, dst_rel=None, **kw):
    kw.setdefault("overwrite", False)
    kw.setdefault("include_hidden", False)
    kw.setdefault("move", False)
    return copy_entry(src_root, src_rel, dst_root, dst_rel or src_rel, **kw)


def _code(excinfo) -> str:
    return excinfo.value.code


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text()
        for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
    }


class TestFile:
    def test_copies_file_and_creates_parents(self, spaces):
        src, dst = spaces
        (src / "notes.md").write_text("hello")
        result = _copy(src, "notes.md", dst, "reports/q3/notes.md")
        assert result == {
            "type": "file", "path": "/reports/q3/notes.md",
            "files_copied": 1, "skipped": 0, "moved": False,
        }
        assert (dst / "reports/q3/notes.md").read_text() == "hello"
        assert (src / "notes.md").read_text() == "hello"

    def test_default_style_leading_slash_is_root_relative(self, spaces):
        src, dst = spaces
        (src / "a.txt").write_text("a")
        result = _copy(src, "/a.txt", dst, "/a.txt")
        assert result["path"] == "/a.txt"
        assert (dst / "a.txt").read_text() == "a"

    def test_preserves_mtime(self, spaces):
        src, dst = spaces
        f = src / "old.txt"
        f.write_text("x")
        os.utime(f, (1_000_000, 1_000_000))
        _copy(src, "old.txt", dst)
        assert (dst / "old.txt").stat().st_mtime == 1_000_000

    def test_existing_without_overwrite_writes_nothing(self, spaces):
        src, dst = spaces
        (src / "a.txt").write_text("new")
        (dst / "a.txt").write_text("old")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "a.txt", dst)
        assert _code(e) == "destination_exists"
        assert (dst / "a.txt").read_text() == "old"

    def test_overwrite_replaces_file(self, spaces):
        src, dst = spaces
        (src / "a.txt").write_text("new")
        (dst / "a.txt").write_text("old")
        _copy(src, "a.txt", dst, overwrite=True)
        assert (dst / "a.txt").read_text() == "new"
        # No temp files left beside the destination.
        assert sorted(p.name for p in dst.iterdir()) == ["a.txt"]

    def test_type_mismatch_is_invalid_destination_even_with_overwrite(self, spaces):
        src, dst = spaces
        (src / "a").write_text("file")
        (dst / "a").mkdir()
        (src / "d").mkdir()
        (src / "d" / "x.txt").write_text("x")
        (dst / "d").write_text("file in the way")
        for rel in ("a", "d"):
            with pytest.raises(CopyEntryError) as e:
                _copy(src, rel, dst, overwrite=True)
            assert _code(e) == "invalid_destination"
        assert (dst / "d").read_text() == "file in the way"

    def test_parent_that_is_a_file_is_invalid_destination(self, spaces):
        src, dst = spaces
        (src / "a.txt").write_text("a")
        (dst / "reports").write_text("not a dir")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "a.txt", dst, "reports/a.txt")
        assert _code(e) == "invalid_destination"

    def test_missing_source(self, spaces):
        src, dst = spaces
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "nope.txt", dst)
        assert _code(e) == "not_found"

    def test_explicit_hidden_file_is_copied(self, spaces):
        src, dst = spaces
        (src / ".env.example").write_text("K=V")
        _copy(src, ".env.example", dst)
        assert (dst / ".env.example").read_text() == "K=V"


class TestDirectory:
    def _make(self, src):
        (src / "d" / "sub").mkdir(parents=True)
        (src / "d" / "a.txt").write_text("a")
        (src / "d" / "sub" / "b.txt").write_text("b")
        (src / "d" / ".hidden").write_text("h")
        (src / "d" / "sub" / ".cache").mkdir()
        (src / "d" / "sub" / ".cache" / "c.txt").write_text("c")

    def test_copies_tree_skipping_hidden(self, spaces):
        src, dst = spaces
        self._make(src)
        result = _copy(src, "d", dst)
        assert result["type"] == "folder"
        assert result["files_copied"] == 2
        assert result["skipped"] == 2
        assert _tree(dst) == {"d/a.txt": "a", "d/sub/b.txt": "b"}

    def test_include_hidden_copies_dot_entries(self, spaces):
        src, dst = spaces
        self._make(src)
        result = _copy(src, "d", dst, include_hidden=True)
        assert result["skipped"] == 0
        assert _tree(dst) == {
            "d/.hidden": "h", "d/a.txt": "a", "d/sub/.cache/c.txt": "c",
            "d/sub/b.txt": "b",
        }

    def test_explicit_hidden_dir_copies_its_visible_contents(self, spaces):
        src, dst = spaces
        (src / ".cfg" / ".inner").mkdir(parents=True)
        (src / ".cfg" / "x.txt").write_text("x")
        (src / ".cfg" / ".inner" / "y.txt").write_text("y")
        _copy(src, ".cfg", dst)
        assert _tree(dst) == {".cfg/x.txt": "x"}

    def test_existing_dir_without_overwrite_writes_nothing(self, spaces):
        src, dst = spaces
        self._make(src)
        (dst / "d").mkdir()
        (dst / "d" / "keep.txt").write_text("keep")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "d", dst)
        assert _code(e) == "destination_exists"
        assert _tree(dst) == {"d/keep.txt": "keep"}

    def test_overwrite_merges_and_keeps_unrelated_files(self, spaces):
        src, dst = spaces
        self._make(src)
        (dst / "d" / "sub").mkdir(parents=True)
        (dst / "d" / "keep.txt").write_text("keep")
        (dst / "d" / "a.txt").write_text("old a")
        _copy(src, "d", dst, overwrite=True)
        assert _tree(dst) == {
            "d/a.txt": "a", "d/keep.txt": "keep", "d/sub/b.txt": "b",
        }

    def test_merge_conflict_deep_in_tree_writes_nothing(self, spaces):
        src, dst = spaces
        self._make(src)
        (dst / "d").mkdir()
        (dst / "d" / "a.txt").write_text("old a")
        (dst / "d" / "sub").write_text("file where a dir goes")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "d", dst, overwrite=True)
        assert _code(e) == "invalid_destination"
        assert (dst / "d" / "a.txt").read_text() == "old a"

    def test_symlink_inside_dir_is_skipped(self, spaces, tmp_path):
        src, dst = spaces
        outside = tmp_path / "outside.txt"
        outside.write_text("host secret")
        (src / "d").mkdir()
        (src / "d" / "ok.txt").write_text("ok")
        (src / "d" / "link.txt").symlink_to(outside)
        (src / "d" / "dirlink").symlink_to(tmp_path, target_is_directory=True)
        result = _copy(src, "d", dst)
        assert result["skipped"] == 2
        assert _tree(dst) == {"d/ok.txt": "ok"}
        assert not os.path.lexists(dst / "d" / "link.txt")
        assert not os.path.lexists(dst / "d" / "dirlink")

    def test_fifo_and_socket_inside_dir_are_skipped(self, spaces):
        src, dst = spaces
        (src / "d").mkdir()
        (src / "d" / "ok.txt").write_text("ok")
        os.mkfifo(src / "d" / "pipe")
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(src / "d" / "sock"))
            result = _copy(src, "d", dst)
        finally:
            sock.close()
        assert result["skipped"] == 2
        assert sorted(p.name for p in (dst / "d").iterdir()) == ["ok.txt"]

    def test_destination_inside_source_refused(self, spaces):
        src, _dst = spaces
        self._make(src)
        for dest in ("d", "d/sub/copy", "/d/"):
            with pytest.raises(CopyEntryError) as e:
                copy_entry(src, "d", src, dest, overwrite=True,
                           include_hidden=False, move=False)
            assert _code(e) == "invalid_destination", dest

    def test_copy_beside_itself_in_the_same_root_is_fine(self, spaces):
        src, _dst = spaces
        self._make(src)
        copy_entry(src, "d", src, "d-copy", overwrite=False,
                   include_hidden=False, move=False)
        assert (src / "d-copy" / "sub" / "b.txt").read_text() == "b"


class TestRefusals:
    def test_symlink_leaf_refused(self, spaces, tmp_path):
        src, dst = spaces
        (tmp_path / "target.txt").write_text("t")
        (src / "link.txt").symlink_to(src / ".." / "target.txt")
        (src / "inside.txt").write_text("i")
        (src / "inner-link").symlink_to(src / "inside.txt")
        for rel in ("link.txt", "inner-link"):
            with pytest.raises(CopyEntryError) as e:
                _copy(src, rel, dst)
            assert _code(e) in ("not_a_regular_file", "invalid_path"), rel
        assert list(dst.iterdir()) == []

    def test_symlinked_parent_component_refused(self, spaces):
        src, dst = spaces
        (src / "real").mkdir()
        (src / "real" / "f.txt").write_text("f")
        (src / "alias").symlink_to(src / "real", target_is_directory=True)
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "alias/f.txt", dst)
        assert _code(e) == "not_a_regular_file"

    def test_symlink_in_destination_path_refused(self, spaces, tmp_path):
        src, dst = spaces
        (src / "a.txt").write_text("a")
        elsewhere = dst / "real"
        elsewhere.mkdir()
        (dst / "alias").symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "a.txt", dst, "alias/a.txt")
        assert _code(e) == "invalid_destination"
        # A leaf link escaping the root fails containment; one pointing
        # inside is still refused rather than written through or replaced.
        (dst / "leaf").symlink_to(tmp_path / "x.txt")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "a.txt", dst, "leaf", overwrite=True)
        assert _code(e) == "invalid_path"
        assert not (tmp_path / "x.txt").exists()
        (dst / "inner.txt").write_text("inner")
        (dst / "leaf2").symlink_to(dst / "inner.txt")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "a.txt", dst, "leaf2", overwrite=True)
        assert _code(e) == "invalid_destination"
        assert (dst / "inner.txt").read_text() == "inner"
        assert (dst / "leaf2").is_symlink()

    def test_fifo_leaf_refused_without_blocking(self, spaces):
        src, dst = spaces
        os.mkfifo(src / "pipe")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "pipe", dst)
        assert _code(e) == "not_a_regular_file"
        assert list(dst.iterdir()) == []

    @pytest.mark.parametrize("rel", ["../x", "a/../../x", "..", "sub/.."])
    def test_dotdot_rejected_on_source(self, spaces, rel):
        src, dst = spaces
        with pytest.raises(CopyEntryError) as e:
            _copy(src, rel, dst, "ok.txt")
        assert _code(e) == "invalid_path"

    @pytest.mark.parametrize("rel", ["../x", "a/../../x", ".."])
    def test_dotdot_rejected_on_destination(self, spaces, rel):
        src, dst = spaces
        (src / "a.txt").write_text("a")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, "a.txt", dst, rel)
        assert _code(e) == "invalid_path"
        assert list(dst.iterdir()) == []

    def test_absolute_paths_stay_inside_their_root(self, spaces, tmp_path):
        src, dst = spaces
        (tmp_path / "outside.txt").write_text("outside")
        with pytest.raises(CopyEntryError) as e:
            _copy(src, str(tmp_path / "outside.txt"), dst, "x.txt")
        assert _code(e) == "not_found"
        (src / "a.txt").write_text("a")
        result = _copy(src, "a.txt", dst, str(tmp_path / "landed.txt"))
        assert not (tmp_path / "landed.txt").exists()
        assert (dst / result["path"].lstrip("/")).read_text() == "a"

    @pytest.mark.parametrize("rel", ["", "/", ".", "//"])
    def test_root_is_not_a_source(self, spaces, rel):
        src, dst = spaces
        with pytest.raises(CopyEntryError) as e:
            _copy(src, rel, dst, "x")
        assert _code(e) == "invalid_path"

    def test_root_is_not_a_destination(self, spaces):
        src, dst = spaces
        (src / "d").mkdir()
        with pytest.raises(CopyEntryError) as e:
            copy_entry(src, "d", dst, "/", overwrite=True,
                       include_hidden=False, move=False)
        assert _code(e) == "invalid_destination"


class TestMove:
    def test_move_file_removes_source(self, spaces):
        src, dst = spaces
        (src / "a.txt").write_text("a")
        result = _copy(src, "a.txt", dst, move=True)
        assert result["moved"] is True
        assert not (src / "a.txt").exists()
        assert (dst / "a.txt").read_text() == "a"

    def test_move_dir_keeps_skipped_entries_in_source(self, spaces):
        src, dst = spaces
        (src / "d" / "sub").mkdir(parents=True)
        (src / "d" / "a.txt").write_text("a")
        (src / "d" / "sub" / "b.txt").write_text("b")
        (src / "d" / ".keep").write_text("hidden")
        _copy(src, "d", dst, move=True)
        assert _tree(dst) == {"d/a.txt": "a", "d/sub/b.txt": "b"}
        # Only what was delivered is gone; the skipped dot-file (and the
        # directory holding it) stay.
        assert _tree(src) == {"d/.keep": "hidden"}
        assert not (src / "d" / "sub").exists()

    def test_move_dir_fully_copied_removes_it(self, spaces):
        src, dst = spaces
        (src / "d" / "sub").mkdir(parents=True)
        (src / "d" / "sub" / "b.txt").write_text("b")
        _copy(src, "d", dst, move=True)
        assert not (src / "d").exists()

    def test_failure_mid_copy_keeps_source_and_cleans_destination(
        self, spaces, monkeypatch,
    ):
        src, dst = spaces
        (src / "d").mkdir()
        for name in ("a.txt", "b.txt", "c.txt"):
            (src / "d" / name).write_text(name)

        real = fs._copy_regular_file
        calls = {"n": 0}

        def flaky(s, d):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            real(s, d)

        monkeypatch.setattr(fs, "_copy_regular_file", flaky)
        with pytest.raises(OSError):
            _copy(src, "d", dst, move=True)
        assert sorted(p.name for p in (src / "d").iterdir()) == [
            "a.txt", "b.txt", "c.txt",
        ]
        assert not (dst / "d").exists()

    def test_failure_on_overwrite_keeps_source(self, spaces, monkeypatch):
        src, dst = spaces
        (src / "a.txt").write_text("new")
        (dst / "a.txt").write_text("old")

        def boom(s, d):
            raise OSError("disk full")

        monkeypatch.setattr(fs, "_copy_regular_file", boom)
        with pytest.raises(OSError):
            _copy(src, "a.txt", dst, overwrite=True, move=True)
        assert (src / "a.txt").read_text() == "new"
        assert (dst / "a.txt").read_text() == "old"


@pytest.mark.parametrize("rel,expected", [
    (".responses/x.json", True),
    ("/.responses", True),
    (".subagent_responses/alice/r.md", True),
    ("pasted/abc.png", True),
    ("pasted", True),
    ("./pasted/abc.png", True),
    ("reports/pasted/x.png", False),
    ("pasted-notes.md", False),
    (".temp/x", False),
    ("notes.md", False),
    ("", False),
])
def test_is_scratch_source(rel, expected):
    assert is_scratch_source(rel) is expected
