"""Quest Docs storage: the doc path resolver, the doc_reads.json sidecar, and
the on-disk layer in chat/docs/files.py.

Covers (spec sections 4.1 and 4.3):

1. ``ChatStorage.get_doc_dir`` -- the ONLY doc id-to-path resolver -- with
   the same canonical-id and containment checks as the conversation and
   project resolvers (tests/test_storage_path_resolvers.py).
2. The per-conversation ``doc_reads.json`` read-before-edit sidecar.
3. Body init/read/write: atomic replace, CRLF normalization, size cap,
   revision snapshots (naming, same-second sequence, 30-day pruning that
   always keeps the newest), and symlink/special-file refusal.
4. Assets: magic-byte sniffing, caps, name sanitizing and collision
   suffixes, the safe ``asset_path`` lookup.
5. Zip download contents and symlink refusal; the per-doc lock.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
import zipfile

import pytest

import chat.storage as storage_mod
from chat.docs import constants
from chat.docs import files


# Other suites reload chat.storage, so bind through the module at call time.
def _cs():
    return storage_mod.ChatStorage


def _invalid():
    return storage_mod.InvalidStorageIdError


BAD_IDS = ["", ".", "..", "a/b", "../x", "x/..", "/abs", "/", "a\\b/c"]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF87 = b"GIF87a" + b"\x00" * 32
GIF89 = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'


@pytest.fixture()
def docs_root(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    monkeypatch.setattr(storage_mod, "DOCS_DIR", root, raising=True)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats", raising=True)
    return root


@pytest.fixture()
def doc(docs_root):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, "# Title\n\nbody\n")
    return doc_id


def _age(path, days):
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# get_doc_dir
# ---------------------------------------------------------------------------


class TestGetDocDir:
    def test_uuid_resolves_to_plain_join(self, docs_root):
        doc_id = str(uuid.uuid4())
        assert _cs().get_doc_dir(doc_id) == docs_root / doc_id

    def test_missing_root_is_fine(self, docs_root):
        assert not docs_root.exists()
        assert _cs().get_doc_dir("d1") == docs_root / "d1"

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_rejects_non_canonical(self, docs_root, bad):
        with pytest.raises(_invalid()):
            _cs().get_doc_dir(bad)

    def test_non_string_rejected(self, docs_root):
        for value in (None, 12, b"doc"):
            with pytest.raises(_invalid()):
                _cs().get_doc_dir(value)  # type: ignore[arg-type]

    def test_symlink_escaping_docs_dir_rejected(self, docs_root, tmp_path):
        docs_root.mkdir()
        outside = tmp_path / "host-secret"
        outside.mkdir()
        (docs_root / "evil").symlink_to(outside, target_is_directory=True)
        with pytest.raises(_invalid()):
            _cs().get_doc_dir("evil")
        with pytest.raises(_invalid()):
            files.read_body("evil")

    def test_invalid_id_is_a_value_error(self):
        assert issubclass(_invalid(), ValueError)

    def test_files_layer_uses_the_resolver(self, docs_root):
        for bad in ("..", "a/b", ""):
            with pytest.raises(_invalid()):
                files.doc_paths(bad)
        paths = files.doc_paths("d1")
        assert paths.root == docs_root / "d1"
        assert paths.body == docs_root / "d1" / "doc.md"
        assert paths.assets == docs_root / "d1" / "assets"
        assert paths.revisions == docs_root / "d1" / "revisions"


# ---------------------------------------------------------------------------
# doc_reads.json sidecar
# ---------------------------------------------------------------------------


class TestDocReadsSidecar:
    def test_empty_when_missing(self, docs_root, tmp_path):
        (tmp_path / "chats" / "c1").mkdir(parents=True)
        assert _cs().get_doc_read_ids("c1") == []

    def test_add_dedups_and_preserves_order(self, docs_root, tmp_path):
        conv_dir = tmp_path / "chats" / "c1"
        conv_dir.mkdir(parents=True)
        _cs().add_doc_read_ids("c1", ["a", "b", "a"])
        _cs().add_doc_read_ids("c1", ["b", "c"])
        assert _cs().get_doc_read_ids("c1") == ["a", "b", "c"]
        on_disk = json.loads((conv_dir / "doc_reads.json").read_text())
        assert on_disk == {"doc_ids": ["a", "b", "c"]}

    def test_noop_add_skips_write(self, docs_root, tmp_path):
        conv_dir = tmp_path / "chats" / "c1"
        conv_dir.mkdir(parents=True)
        _cs().add_doc_read_ids("c1", [])
        assert not (conv_dir / "doc_reads.json").exists()
        _cs().add_doc_read_ids("c1", ["a"])
        mtime = (conv_dir / "doc_reads.json").stat().st_mtime_ns
        _age(conv_dir / "doc_reads.json", 1)
        aged = (conv_dir / "doc_reads.json").stat().st_mtime_ns
        assert aged != mtime
        _cs().add_doc_read_ids("c1", ["a"])
        assert (conv_dir / "doc_reads.json").stat().st_mtime_ns == aged

    def test_corrupt_file_reads_empty(self, docs_root, tmp_path):
        conv_dir = tmp_path / "chats" / "c1"
        conv_dir.mkdir(parents=True)
        (conv_dir / "doc_reads.json").write_text("{not json")
        assert _cs().get_doc_read_ids("c1") == []

    def test_bad_conversation_id_rejected(self, docs_root):
        with pytest.raises(_invalid()):
            _cs().get_doc_read_ids("../x")
        with pytest.raises(_invalid()):
            _cs().add_doc_read_ids("../x", ["a"])


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------


class TestBody:
    def test_init_creates_layout_and_returns_size(self, docs_root):
        doc_id = "d1"
        size = files.init_doc(doc_id, "héllo\n")
        root = docs_root / doc_id
        assert (root / "doc.md").is_file()
        assert (root / "assets").is_dir()
        assert (root / "revisions").is_dir()
        assert size == len("héllo\n".encode("utf-8"))
        assert files.read_body(doc_id) == "héllo\n"
        # first write never snapshots
        assert files.list_revisions(doc_id) == []

    def test_round_trip_and_crlf_normalization(self, doc):
        size = files.write_body(doc, "a\r\nb\r\n\r\nc")
        assert files.read_body(doc) == "a\nb\n\nc"
        assert size == len(b"a\nb\n\nc")
        # content otherwise verbatim (no trailing newline added)
        files.write_body(doc, "x")
        assert files.read_body(doc) == "x"

    def test_init_normalizes_crlf(self, docs_root):
        files.init_doc("d1", "one\r\ntwo\r\n")
        assert (docs_root / "d1" / "doc.md").read_bytes() == b"one\ntwo\n"

    def test_atomic_write_leaves_no_tmp(self, doc, docs_root):
        root = docs_root / doc
        # a stale temp file from a crashed write is replaced, not appended to
        (root / "doc.md.tmp").write_text("stale")
        files.write_body(doc, "new body\n")
        assert not (root / "doc.md.tmp").exists()
        assert files.read_body(doc) == "new body\n"

    def test_planted_tmp_symlink_is_not_written_through(self, doc, docs_root, tmp_path):
        root = docs_root / doc
        victim = tmp_path / "victim.txt"
        victim.write_text("untouched")
        (root / "doc.md.tmp").symlink_to(victim)
        files.write_body(doc, "safe\n")
        assert victim.read_text() == "untouched"
        assert files.read_body(doc) == "safe\n"
        assert not os.path.lexists(root / "doc.md.tmp")

    def test_size_cap(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 10)
        files.write_body(doc, "0123456789")  # exactly at the cap
        with pytest.raises(files.DocFileError):
            files.write_body(doc, "0123456789A")
        # bytes, not characters: 5 two-byte chars = 10 bytes is fine, 6 is not
        files.write_body(doc, "é" * 5)
        with pytest.raises(files.DocFileError):
            files.write_body(doc, "é" * 6)
        with pytest.raises(files.DocFileError):
            files.init_doc("other", "x" * 11)
        assert not (files.doc_paths("other").root).exists()

    def test_failed_write_leaves_body_untouched(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_CONTENT_SIZE", 10)
        before = files.read_body(doc)
        revs = files.list_revisions(doc)
        with pytest.raises(files.DocFileError):
            files.write_body(doc, "x" * 100)
        assert files.read_body(doc) == before
        assert files.list_revisions(doc) == revs

    def test_write_requires_existing_doc_dir(self, docs_root):
        with pytest.raises(files.DocFileError):
            files.write_body("never-created", "body")
        assert not (docs_root / "never-created").exists()

    def test_read_missing_body(self, docs_root):
        with pytest.raises(files.DocFileError):
            files.read_body("nope")

    def test_read_refuses_symlinked_body(self, doc, docs_root, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("host secret")
        body = docs_root / doc / "doc.md"
        body.unlink()
        body.symlink_to(secret)
        with pytest.raises(files.DocFileError):
            files.read_body(doc)
        # and a snapshot never copies through it
        with pytest.raises(files.DocFileError):
            files.write_body(doc, "x")
        assert secret.read_text() == "host secret"

    def test_read_refuses_fifo_without_blocking(self, doc, docs_root):
        body = docs_root / doc / "doc.md"
        body.unlink()
        os.mkfifo(body)
        with pytest.raises(files.DocFileError):
            files.read_body(doc)

    def test_undecodable_bytes_replaced(self, doc, docs_root):
        (docs_root / doc / "doc.md").write_bytes(b"ok \xff\xfe end")
        assert files.read_body(doc) == "ok \ufffd\ufffd end"


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


class TestRevisions:
    def test_snapshot_naming_and_content(self, doc):
        files.write_body(doc, "v2\n")
        revs = files.list_revisions(doc)
        assert len(revs) == 1
        assert files._REVISION_RE.match(revs[0].name)
        assert revs[0].name.endswith("-1.md")
        assert revs[0].read_text() == "# Title\n\nbody\n"
        files.write_body(doc, "v3\n")
        revs = files.list_revisions(doc)
        assert [r.read_text() for r in revs] == ["# Title\n\nbody\n", "v2\n"]

    def test_same_second_sequence(self, doc, monkeypatch):
        from datetime import datetime, timezone

        # Frozen at the real current second so pruning (mtime-based) keeps
        # every snapshot regardless of the machine's clock.
        fixed = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = fixed.strftime("%Y%m%dT%H%M%SZ")

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        monkeypatch.setattr(files, "datetime", _Frozen)
        for i in range(12):
            files.write_body(doc, f"v{i}\n")
        names = [p.name for p in files.list_revisions(doc)]
        assert names == [f"{stamp}-{n}.md" for n in range(1, 13)]
        # numeric (not lexicographic) ordering: -10 sorts after -9
        assert names[-1] == f"{stamp}-12.md"
        assert files.list_revisions(doc)[-1].read_text() == "v10\n"

    def test_snapshot_false_skips_snapshot(self, doc):
        files.write_body(doc, "v2\n", snapshot=False)
        assert files.list_revisions(doc) == []

    def test_prune_deletes_old_keeps_newer(self, doc):
        files.write_body(doc, "v2\n")
        files.write_body(doc, "v3\n")
        old, newer = files.list_revisions(doc)
        _age(old, 40)
        files.write_body(doc, "v4\n")  # prune runs on write
        remaining = files.list_revisions(doc)
        assert old not in remaining
        assert newer in remaining
        assert len(remaining) == 2  # newer + the snapshot just taken

    def test_newest_kept_even_if_old(self, doc):
        files.write_body(doc, "v2\n")
        (only,) = files.list_revisions(doc)
        _age(only, 400)
        files.write_body(doc, "v3\n", snapshot=False)
        assert files.list_revisions(doc) == [only]

    def test_all_old_keeps_only_newest(self, doc):
        for i in range(3):
            files.write_body(doc, f"v{i}\n")
        revs = files.list_revisions(doc)
        for r in revs:
            _age(r, 40)
        files.write_body(doc, "final\n", snapshot=False)
        assert files.list_revisions(doc) == [revs[-1]]

    def test_recent_revisions_survive(self, doc):
        for i in range(3):
            files.write_body(doc, f"v{i}\n")
        revs = files.list_revisions(doc)
        _age(revs[0], 29)
        files.write_body(doc, "again\n")
        assert revs[0] in files.list_revisions(doc)

    def test_unrelated_files_ignored(self, doc, docs_root):
        rev_dir = docs_root / doc / "revisions"
        (rev_dir / "notes.txt").write_text("x")
        _age(rev_dir / "notes.txt", 90)
        files.write_body(doc, "v2\n")
        assert (rev_dir / "notes.txt").exists()
        assert all(p.name != "notes.txt" for p in files.list_revisions(doc))


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class TestSniff:
    @pytest.mark.parametrize(
        "data,expected",
        [(PNG, "png"), (JPEG, "jpg"), (GIF87, "gif"), (GIF89, "gif"), (WEBP, "webp")],
    )
    def test_supported(self, data, expected):
        assert files.sniff_image_type(data) == expected

    @pytest.mark.parametrize(
        "data",
        [SVG, b"hello world", b"", b"\x89PN", b"RIFF\x00\x00\x00\x00WAVEfmt ", b"BM" + b"\x00" * 30],
    )
    def test_rejected(self, data):
        assert files.sniff_image_type(data) is None


class TestSanitize:
    @pytest.mark.parametrize(
        "hint,expected",
        [
            ("chart.png", "chart"),
            ("Chart.PNG", "Chart"),
            ("dir/sub/plot.jpeg", "plot"),
            ("C:\\Users\\me\\pic.gif", "pic"),
            ("../../etc/passwd", "passwd"),
            ("my chart (final)!.png", "my-chart-final"),
            ("..hidden.png", "hidden"),
            ("---x---.webp", "x"),
            ("", "image"),
            ("...", "image"),
            ("???.png", "image"),
            ("report.v2.png", "report.v2"),
            ("ünïcode.png", "n-code"),
        ],
    )
    def test_cases(self, hint, expected):
        assert files.sanitize_asset_name(hint) == expected

    def test_max_length(self):
        out = files.sanitize_asset_name("a" * 300 + ".png")
        assert out == "a" * 64

    def test_non_string(self):
        assert files.sanitize_asset_name(None) == "image"  # type: ignore[arg-type]


class TestAddAsset:
    def test_extension_from_sniffed_type(self, doc):
        info = files.add_asset(doc, "photo.png", JPEG)  # lies about its type
        assert info.name == "photo.jpg"
        assert info.size == len(JPEG)
        assert info.asset_count == 1
        assert info.total_bytes == len(JPEG)
        assert files.read_asset(doc, "photo.jpg") == JPEG

    def test_collision_suffixes(self, doc):
        names = [files.add_asset(doc, "chart.png", PNG).name for _ in range(3)]
        assert names == ["chart.png", "chart-2.png", "chart-3.png"]
        # a different type with the same stem does not collide
        assert files.add_asset(doc, "chart.png", GIF89).name == "chart.gif"
        assert files.asset_stats(doc) == (4, 3 * len(PNG) + len(GIF89))
        assert [a["name"] for a in files.list_assets(doc)] == [
            "chart-2.png", "chart-3.png", "chart.gif", "chart.png",
        ]

    def test_rejects_non_raster(self, doc):
        with pytest.raises(files.DocFileError, match="png, jpg, gif, webp"):
            files.add_asset(doc, "x.svg", SVG)
        with pytest.raises(files.DocFileError):
            files.add_asset(doc, "x.png", b"not an image")
        assert files.list_assets(doc) == []

    def test_size_cap(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_IMAGE_SIZE", len(PNG))
        files.add_asset(doc, "ok.png", PNG)
        with pytest.raises(files.DocFileError):
            files.add_asset(doc, "big.png", PNG + b"\x00")

    def test_count_cap(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_ASSETS", 2)
        files.add_asset(doc, "a.png", PNG)
        files.add_asset(doc, "b.png", PNG)
        with pytest.raises(files.DocFileError):
            files.add_asset(doc, "c.png", PNG)
        assert files.asset_stats(doc)[0] == 2

    def test_total_bytes_cap(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_MAX_ASSETS_TOTAL_BYTES", 2 * len(PNG) + 5)
        files.add_asset(doc, "a.png", PNG)
        files.add_asset(doc, "b.png", PNG)
        with pytest.raises(files.DocFileError):
            files.add_asset(doc, "c.png", PNG)

    def test_no_temp_files_left(self, doc, docs_root):
        files.add_asset(doc, "a.png", PNG)
        leftovers = [p.name for p in (docs_root / doc).iterdir() if p.name.startswith(".")]
        assert leftovers == []

    def test_asset_requires_existing_doc(self, docs_root):
        with pytest.raises(files.DocFileError):
            files.add_asset("never-created", "a.png", PNG)
        assert not (docs_root / "never-created").exists()

    def test_does_not_snapshot(self, doc):
        files.add_asset(doc, "a.png", PNG)
        assert files.list_revisions(doc) == []

    def test_list_and_stats_skip_symlinks_and_special(self, doc, docs_root, tmp_path):
        files.add_asset(doc, "a.png", PNG)
        assets = docs_root / doc / "assets"
        outside = tmp_path / "big.bin"
        outside.write_bytes(b"x" * 1000)
        (assets / "link.png").symlink_to(outside)
        (assets / "subdir.png").mkdir()
        os.mkfifo(assets / "pipe.png")
        assert files.list_assets(doc) == [{"name": "a.png", "size": len(PNG)}]
        assert files.asset_stats(doc) == (1, len(PNG))


class TestAssetPath:
    def test_existing(self, doc, docs_root):
        files.add_asset(doc, "a.png", PNG)
        assert files.asset_path(doc, "a.png") == docs_root / doc / "assets" / "a.png"

    @pytest.mark.parametrize(
        "name",
        ["", ".", "..", "../doc.md", "a/b.png", "/etc/passwd", ".hidden.png",
         "doc.md", "x.svg", "a\\b.png", "x.png\x00"],
    )
    def test_rejects_bad_names(self, doc, name):
        with pytest.raises(files.DocFileError):
            files.asset_path(doc, name)

    def test_missing(self, doc):
        with pytest.raises(files.DocFileError):
            files.asset_path(doc, "nope.png")

    def test_symlink_and_special_rejected(self, doc, docs_root, tmp_path):
        assets = docs_root / doc / "assets"
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG)
        (assets / "link.png").symlink_to(outside)
        os.mkfifo(assets / "pipe.png")
        (assets / "dir.png").mkdir()
        for name in ("link.png", "pipe.png", "dir.png"):
            with pytest.raises(files.DocFileError):
                files.asset_path(doc, name)
            with pytest.raises(files.DocFileError):
                files.read_asset(doc, name)

    def test_symlinked_assets_dir_rejected(self, doc, docs_root, tmp_path):
        assets = docs_root / doc / "assets"
        assets.rmdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "a.png").write_bytes(PNG)
        assets.symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(files.DocFileError):
            files.asset_path(doc, "a.png")
        with pytest.raises(files.DocFileError):
            files.add_asset(doc, "b.png", PNG)
        assert not (elsewhere / "b.png").exists()


# ---------------------------------------------------------------------------
# Zip, delete, lock
# ---------------------------------------------------------------------------


class TestZip:
    def test_contents(self, doc):
        files.add_asset(doc, "a.png", PNG)
        files.add_asset(doc, "b.gif", GIF89)
        files.write_body(doc, "![a](assets/a.png)\n")  # creates a revision too
        zip_path = files.build_zip(doc, "My / Doc")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                assert sorted(zf.namelist()) == ["assets/a.png", "assets/b.gif", "doc.md"]
                assert zf.read("doc.md") == b"![a](assets/a.png)\n"
                assert zf.read("assets/a.png") == PNG
        finally:
            zip_path.unlink(missing_ok=True)

    def test_symlink_refused_and_partial_removed(self, doc, docs_root, tmp_path, monkeypatch):
        made = []
        real_mkstemp = files.tempfile.mkstemp

        def spy(*a, **kw):
            fd, name = real_mkstemp(*a, dir=tmp_path, **kw)
            made.append(name)
            return fd, name

        monkeypatch.setattr(files.tempfile, "mkstemp", spy)
        secret = tmp_path / "secret.png"
        secret.write_bytes(PNG)
        (docs_root / doc / "assets" / "leak.png").symlink_to(secret)
        with pytest.raises(files.DocFileError, match="symbolic links"):
            files.build_zip(doc, "t")
        assert made and not os.path.exists(made[0])

    def test_special_file_refused(self, doc, docs_root):
        os.mkfifo(docs_root / doc / "assets" / "pipe.png")
        with pytest.raises(files.DocFileError, match="special files"):
            files.build_zip(doc, "t")

    def test_symlinked_body_refused(self, doc, docs_root, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("x")
        body = docs_root / doc / "doc.md"
        body.unlink()
        body.symlink_to(secret)
        with pytest.raises(files.DocFileError):
            files.build_zip(doc, "t")


class TestDelete:
    def test_delete_and_missing_ok(self, doc, docs_root):
        files.add_asset(doc, "a.png", PNG)
        files.delete_doc_dir(doc)
        assert not (docs_root / doc).exists()
        files.delete_doc_dir(doc)  # idempotent

    def test_delete_symlink_does_not_follow(self, docs_root):
        files.init_doc("real", "keep me\n")
        (docs_root / "alias").symlink_to(docs_root / "real", target_is_directory=True)
        files.delete_doc_dir("alias")
        assert not os.path.lexists(docs_root / "alias")
        assert files.read_body("real") == "keep me\n"

    def test_bad_id_rejected(self, docs_root):
        with pytest.raises(_invalid()):
            files.delete_doc_dir("..")


class TestLock:
    def test_lock_is_per_doc(self):
        a1 = files._get_lock("doc-a")
        assert files._get_lock("doc-a") is a1
        assert files._get_lock("doc-b") is not a1

    def test_lock_blocks_same_doc_not_other(self):
        acquired_other = threading.Event()
        acquired_same = threading.Event()

        def take(doc_id, event):
            with files.doc_lock(doc_id):
                event.set()

        with files.doc_lock("lock-a"):
            t_other = threading.Thread(target=take, args=("lock-b", acquired_other))
            t_same = threading.Thread(target=take, args=("lock-a", acquired_same))
            t_other.start()
            t_same.start()
            assert acquired_other.wait(2)
            assert not acquired_same.wait(0.2)
        assert acquired_same.wait(2)
        t_other.join()
        t_same.join()

    def test_write_waits_for_lock(self, doc):
        done = threading.Event()

        def writer():
            files.write_body(doc, "from thread\n")
            done.set()

        with files.doc_lock(doc):
            t = threading.Thread(target=writer)
            t.start()
            assert not done.wait(0.2)
            assert files.read_body(doc) == "# Title\n\nbody\n"
        assert done.wait(2)
        t.join()
        assert files.read_body(doc) == "from thread\n"
