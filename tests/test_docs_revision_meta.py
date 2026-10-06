"""Quest Docs body metadata (``doc.meta.json``) and revision sidecars.

``<doc>/doc.meta.json`` = ``{"written_at", "source", "size"}`` of the CURRENT
``doc.md``: written by ``init_doc`` and every body write (``write_source`` =
the writer of the new body), never by asset-only writes. A snapshot
``revisions/<ts>-<n>.md`` gets a ``<ts>-<n>.json`` sidecar that is a copy of
the replaced body's ``doc.meta.json`` (fallback for legacy/malformed/stale
metadata: ``doc.md``'s mtime, null source, the real size). Pruning (date and
count rules) removes both halves together, an orphan sidecar goes too, an
undeletable one never aborts the write, a missing one is tolerated, and
``read_revision_meta`` returns None for anything missing or malformed.
"""

from __future__ import annotations

import errno
import json
import os
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import chat.storage as storage_mod
from chat.docs import constants
from chat.docs import files
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    PNG,
    _run,
    docs_env,
    make_caller,
    seed_doc,
    svc,
    workspace_dir,
)

INITIAL = "# Title\n\nbody\n"


@pytest.fixture()
def docs_root(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    monkeypatch.setattr(storage_mod, "DOCS_DIR", root, raising=True)
    monkeypatch.setattr(storage_mod, "CHATS_DIR", tmp_path / "chats", raising=True)
    return root


@pytest.fixture()
def clock(monkeypatch):
    """A settable ``files.datetime.now`` (starts an hour ago, whole second).

    Pruning compares real file mtimes against ``now - 30 days``, so a clock
    within the hour keeps every snapshot inside the retention window.
    """
    state = SimpleNamespace(
        now=datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1),
    )

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return state.now

    monkeypatch.setattr(files, "datetime", _Frozen)
    return state


@pytest.fixture()
def doc(docs_root):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, INITIAL, write_source="s0")
    return doc_id


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _age(path, days):
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def _meta_path(doc_id):
    return files.doc_paths(doc_id).meta


def _rev_dir(doc_id):
    return files.doc_paths(doc_id).revisions


def _sidecar(rev):
    return rev.with_suffix(".json")


def _load(rev):
    return json.loads(_sidecar(rev).read_text())


def _stems(doc_id, suffix):
    return sorted(
        name[: -len(suffix)] for name in os.listdir(_rev_dir(doc_id))
        if name.endswith(suffix)
    )


# ---------------------------------------------------------------------------
# doc.meta.json
# ---------------------------------------------------------------------------


class TestDocMeta:
    def test_init_writes_meta(self, docs_root, clock):
        size = files.init_doc("d1", "héllo\n", write_source="conversation:c1")
        meta_file = docs_root / "d1" / "doc.meta.json"
        assert json.loads(meta_file.read_text()) == {
            "written_at": iso(clock.now),
            "source": "conversation:c1",
            "size": size,
        }
        assert files.read_doc_meta("d1") == json.loads(meta_file.read_text())
        assert files.read_body("d1") == "héllo\n"
        assert files.list_revisions("d1") == []

    def test_init_without_source(self, docs_root):
        files.init_doc("d1", "")
        assert files.read_doc_meta("d1")["source"] is None
        assert files.read_doc_meta("d1")["size"] == 0

    def test_write_describes_the_new_body(self, doc, clock):
        clock.now += timedelta(minutes=5)
        files.write_body(doc, "v2\n", write_source="b")
        assert files.read_doc_meta(doc) == {
            "written_at": iso(clock.now), "source": "b", "size": 3,
        }
        clock.now += timedelta(minutes=5)
        files.modify_body(doc, lambda cur: cur + "v3\n", write_source="c")
        assert files.read_doc_meta(doc) == {
            "written_at": iso(clock.now), "source": "c", "size": 6,
        }

    def test_identical_write_keeps_meta(self, doc, clock):
        before = _meta_path(doc).read_bytes()
        clock.now += timedelta(minutes=5)
        files.write_body(doc, INITIAL, write_source="other")
        files.modify_body(doc, lambda cur: cur, write_source="other")
        assert _meta_path(doc).read_bytes() == before
        assert os.listdir(_rev_dir(doc)) == []

    def test_asset_only_write_leaves_meta(self, doc):
        before = _meta_path(doc).read_bytes()
        files.add_asset(doc, "chart.png", PNG)
        assert _meta_path(doc).read_bytes() == before

    def test_not_an_asset_nor_downloaded(self, doc):
        files.add_asset(doc, "chart.png", PNG)
        assert [a["name"] for a in files.list_assets(doc)] == ["chart.png"]
        assert files.read_body(doc) == INITIAL
        zip_path = files.build_zip(doc, "T")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                assert sorted(zf.namelist()) == ["assets/chart.png", "doc.md"]
        finally:
            zip_path.unlink()

    def test_failed_meta_write_drops_the_stale_file(self, doc, monkeypatch):
        real = files._write_new_file

        def failing(path, data):
            if path.name == "doc.meta.json.tmp":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data)

        monkeypatch.setattr(files, "_write_new_file", failing)
        files.write_body(doc, "v2\n", write_source="b")
        assert files.read_body(doc) == "v2\n"
        # The old file (describing INITIAL) is gone, not left to misattribute.
        assert not os.path.lexists(_meta_path(doc))
        assert not os.path.lexists(_meta_path(doc).with_name("doc.meta.json.tmp"))
        # Restore only this patch (undo() would also revert DOCS_DIR).
        monkeypatch.setattr(files, "_write_new_file", real)
        files.write_body(doc, "v3\n", write_source="c")
        newest = files.list_revisions(doc)[-1]
        assert files.read_revision_meta(newest)["source"] is None  # fallback

    def test_symlinked_meta_not_followed(self, doc, tmp_path):
        outside = tmp_path / "outside.json"
        planted = json.dumps({"written_at": "x", "source": "evil", "size": len(INITIAL)})
        outside.write_text(planted)
        _meta_path(doc).unlink()
        os.symlink(outside, _meta_path(doc))
        assert files.read_doc_meta(doc) is None
        files.write_body(doc, "v2\n", write_source="b")
        (rev,) = files.list_revisions(doc)
        assert files.read_revision_meta(rev)["source"] is None
        # Replaced as a directory entry, never written through.
        assert outside.read_text() == planted
        assert not _meta_path(doc).is_symlink()
        assert files.read_doc_meta(doc)["source"] == "b"


# ---------------------------------------------------------------------------
# Revision sidecars
# ---------------------------------------------------------------------------


class TestSidecar:
    def test_copies_the_replaced_body_meta(self, docs_root, clock):
        files.init_doc("d1", INITIAL, write_source="conversation:a")
        body_meta = files.read_doc_meta("d1")
        clock.now += timedelta(minutes=5)
        files.write_body("d1", "v2\n", write_source="conversation:b")
        (rev,) = files.list_revisions("d1")
        sidecar = _load(rev)
        assert sidecar == body_meta == {
            "written_at": iso(clock.now - timedelta(minutes=5)),
            "source": "conversation:a",
            "size": len(INITIAL.encode()),
        }
        assert files.read_revision_meta(rev) == sidecar
        # written_at is when that body was written, before the snapshot.
        snapshot_at = datetime.strptime(
            rev.name.split("-")[0], "%Y%m%dT%H%M%SZ",
        ).replace(tzinfo=timezone.utc)
        assert parse_iso(sidecar["written_at"]) < snapshot_at
        assert sidecar["size"] == rev.stat().st_size

    def test_legacy_doc_without_meta_falls_back_to_mtime(self, doc):
        _meta_path(doc).unlink()
        body_path = files.doc_paths(doc).body
        os.utime(body_path, (1_700_000_000, 1_700_000_000))
        files.write_body(doc, "v2\n", write_source="b")
        (rev,) = files.list_revisions(doc)
        assert _load(rev) == {
            "written_at": "2023-11-14T22:13:20.000Z",
            "source": None,
            "size": len(INITIAL.encode()),
        }
        # From now on the doc has metadata again.
        assert files.read_doc_meta(doc)["source"] == "b"

    @pytest.mark.parametrize("raw", [
        b"garbage",
        b"[]",
        b'{"written_at": "2026-10-06T00:00:00.000Z", "source": null, "size": -1}',
        b'{"written_at": "2026-10-06T00:00:00.000Z", "size": 15}',
    ])
    def test_malformed_meta_falls_back(self, doc, raw):
        _meta_path(doc).write_bytes(raw)
        files.write_body(doc, "v2\n", write_source="b")
        (rev,) = files.list_revisions(doc)
        meta = _load(rev)
        assert meta["source"] is None
        assert meta["size"] == len(INITIAL.encode())

    def test_meta_of_another_size_is_stale(self, doc):
        # doc.md changed behind the module's back: its meta describes
        # another body, so the snapshot does not trust it.
        files.doc_paths(doc).body.write_text("edited by hand, much longer\n")
        files.write_body(doc, "v2\n", write_source="b")
        (rev,) = files.list_revisions(doc)
        assert _load(rev)["source"] is None
        assert _load(rev)["size"] == len("edited by hand, much longer\n")

    def test_identical_body_writes_nothing(self, doc):
        files.write_body(doc, INITIAL, write_source="ui")
        files.modify_body(doc, lambda cur: cur, write_source="ui")
        assert os.listdir(_rev_dir(doc)) == []

    def test_snapshot_false_writes_no_revision(self, doc):
        files.write_body(doc, "v2\n", snapshot=False, write_source="b")
        files.modify_body(doc, lambda cur: cur + "x", snapshot=False, write_source="c")
        assert os.listdir(_rev_dir(doc)) == []
        assert files.read_doc_meta(doc)["source"] == "c"

    def test_same_second_snapshots_get_paired_names(self, docs_root, clock):
        files.init_doc("d1", INITIAL, write_source="s0")
        stamp = clock.now.strftime("%Y%m%dT%H%M%SZ")
        for i in range(1, 4):
            files.write_body("d1", f"v{i}\n", write_source=f"s{i}")
        assert sorted(os.listdir(_rev_dir("d1"))) == sorted(
            f"{stamp}-{n}.{ext}" for n in (1, 2, 3) for ext in ("md", "json")
        )
        revs = files.list_revisions("d1")
        assert [files.read_revision_meta(r)["source"] for r in revs] == ["s0", "s1", "s2"]
        assert [files.read_revision_meta(r)["size"] for r in revs] == [
            r.stat().st_size for r in revs
        ]

    def test_allocation_skips_an_orphan_sidecar_name(self, docs_root, clock):
        files.init_doc("d1", INITIAL, write_source="s0")
        stamp = clock.now.strftime("%Y%m%dT%H%M%SZ")
        rev_dir = _rev_dir("d1")
        (rev_dir / f"{stamp}-1.json").write_text("stale orphan")
        files.write_body("d1", "v2\n", write_source="s1")
        # The snapshot took -2 (never a .md beside the stale -1.json), and
        # the orphan was pruned after the write.
        assert sorted(os.listdir(rev_dir)) == [f"{stamp}-2.json", f"{stamp}-2.md"]
        (rev,) = files.list_revisions("d1")
        assert files.read_revision_meta(rev)["source"] == "s0"

    def test_sidecar_failure_does_not_block_the_write(self, doc, monkeypatch):
        real = files._write_new_file

        def failing(path, data):
            if path.parent.name == "revisions" and path.suffix == ".json":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data)

        monkeypatch.setattr(files, "_write_new_file", failing)
        files.write_body(doc, "v2\n", write_source="b")
        assert files.read_body(doc) == "v2\n"
        assert files.read_doc_meta(doc)["source"] == "b"
        (rev,) = files.list_revisions(doc)
        assert rev.read_text() == INITIAL
        assert not os.path.lexists(_sidecar(rev))
        assert files.read_revision_meta(rev) is None


# ---------------------------------------------------------------------------
# Pruning in lockstep
# ---------------------------------------------------------------------------


class TestSidecarPrune:
    def test_date_rule_prunes_the_pair(self, doc):
        files.write_body(doc, "v2\n", write_source="s1")
        files.write_body(doc, "v3\n", write_source="s2")
        old, newer = files.list_revisions(doc)
        _age(old, 40)  # the .md's mtime decides; the sidecar's is irrelevant
        files.write_body(doc, "v4\n", write_source="s3")
        assert not os.path.lexists(old)
        assert not os.path.lexists(_sidecar(old))
        assert _load(newer)["source"] == "s1"
        assert _stems(doc, ".md") == _stems(doc, ".json")
        assert len(files.list_revisions(doc)) == 2

    def test_newest_pair_kept_even_if_old(self, doc):
        files.write_body(doc, "v2\n", write_source="s1")
        (only,) = files.list_revisions(doc)
        _age(only, 400)
        files.write_body(doc, "v3\n", snapshot=False)
        assert files.list_revisions(doc) == [only]
        assert files.read_revision_meta(only)["source"] == "s0"

    def test_count_rule_prunes_pairs(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_REVISION_MAX_COUNT", 5)
        for i in range(1, 12):
            files.write_body(doc, f"v{i}\n", write_source=f"s{i}")
        revs = files.list_revisions(doc)
        assert len(revs) == 5
        assert _stems(doc, ".md") == _stems(doc, ".json")
        # The newest five snapshots survive, each with its own sidecar
        # naming the writer of the body it holds.
        assert [files.read_revision_meta(r)["source"] for r in revs] == [
            f"s{i}" for i in range(6, 11)
        ]
        assert revs[-1].read_text() == "v10\n"

    def test_orphan_sidecars_pruned(self, doc):
        files.write_body(doc, "v2\n", write_source="s1")
        (rev,) = files.list_revisions(doc)
        rev.unlink()  # e.g. a prune interrupted between the two unlinks
        other = _rev_dir(doc) / "20200101T000000Z-1.json"
        other.write_text("{}")
        files.write_body(doc, "v3\n", write_source="s2")
        assert not os.path.lexists(_sidecar(rev))
        assert not os.path.lexists(other)
        (new,) = files.list_revisions(doc)
        assert files.read_revision_meta(new)["source"] == "s1"

    def test_orphan_pruned_without_a_snapshot(self, doc):
        files.write_body(doc, "v2\n")
        (rev,) = files.list_revisions(doc)
        rev.unlink()
        files.write_body(doc, "v3\n", snapshot=False)
        assert os.listdir(_rev_dir(doc)) == []

    def test_orphan_symlink_removed_not_followed(self, doc, tmp_path):
        target = tmp_path / "outside.json"
        target.write_text('{"keep": true}')
        link = _rev_dir(doc) / "20200101T000000Z-1.json"
        os.symlink(target, link)
        files.write_body(doc, "v2\n")
        assert not os.path.lexists(link)
        assert target.read_text() == '{"keep": true}'

    def test_undeletable_sidecar_does_not_abort_the_write(self, doc, monkeypatch, caplog):
        files.write_body(doc, "v2\n", write_source="s1")
        files.write_body(doc, "v3\n", write_source="s2")
        old, _newer = files.list_revisions(doc)
        _age(old, 40)
        real_unlink = Path.unlink

        def unlink(self, *args, **kwargs):
            if self.parent.name == "revisions" and self.suffix == ".json":
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        files.write_body(doc, "v4\n", write_source="s3")
        assert files.read_body(doc) == "v4\n"
        assert not os.path.lexists(old)
        assert os.path.lexists(_sidecar(old))  # left behind, logged
        assert "could not remove revision metadata" in caplog.text
        monkeypatch.setattr(Path, "unlink", real_unlink)
        files.write_body(doc, "v5\n", write_source="s4")  # now swept as an orphan
        assert not os.path.lexists(_sidecar(old))

    def test_unrelated_entries_untouched(self, doc):
        rev_dir = _rev_dir(doc)
        keep = ["notes.json", "20200101T000000Z-1.txt", "x-1.json", "20200101T000000Z.json"]
        for name in keep:
            (rev_dir / name).write_text("x")
            _age(rev_dir / name, 90)
        (rev_dir / "20200101T000000Z-9.json").mkdir()  # a directory at a sidecar name
        files.write_body(doc, "v2\n")
        files.write_body(doc, "v3\n")
        for name in keep:
            assert (rev_dir / name).is_file()
        assert (rev_dir / "20200101T000000Z-9.json").is_dir()
        assert len(files.list_revisions(doc)) == 2


# ---------------------------------------------------------------------------
# read_revision_meta
# ---------------------------------------------------------------------------


class TestReadRevisionMeta:
    def test_missing_sidecar_tolerated(self, doc):
        files.write_body(doc, "v2\n", write_source="s1")
        files.write_body(doc, "v3\n", write_source="s2")
        first, second = files.list_revisions(doc)
        _sidecar(first).unlink()
        assert files.list_revisions(doc) == [first, second]
        assert files.read_revision_meta(first) is None
        assert files.read_revision_meta(second)["source"] == "s1"
        # Pruning a sidecar-less snapshot works, and counting ignores sidecars.
        _age(first, 40)
        files.write_body(doc, "v4\n", write_source="s3")
        revs = files.list_revisions(doc)
        assert first not in revs and len(revs) == 2
        assert _stems(doc, ".md") == _stems(doc, ".json")

    @pytest.mark.parametrize("raw", [
        b"not json",
        b"\xff\xfe",
        b"[1, 2]",
        b'"text"',
        b'{"written_at": 1, "source": null, "size": 3}',
        b'{"written_at": "2026-10-06T00:00:00.000Z", "size": 3}',
        b'{"written_at": "2026-10-06T00:00:00.000Z", "source": 5, "size": 3}',
        b'{"written_at": "2026-10-06T00:00:00.000Z", "source": null, "size": "3"}',
        b'{"written_at": "2026-10-06T00:00:00.000Z", "source": null, "size": true}',
        b'{"written_at": "2026-10-06T00:00:00.000Z", "source": null, "size": -1}',
    ])
    def test_malformed_returns_none(self, doc, raw):
        files.write_body(doc, "v2\n")
        (rev,) = files.list_revisions(doc)
        _sidecar(rev).write_bytes(raw)
        assert files.read_revision_meta(rev) is None

    def test_symlinked_sidecar_not_followed(self, doc, tmp_path):
        files.write_body(doc, "v2\n")
        (rev,) = files.list_revisions(doc)
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"written_at": "x", "source": "evil", "size": 1}))
        _sidecar(rev).unlink()
        os.symlink(outside, _sidecar(rev))
        assert files.read_revision_meta(rev) is None

    def test_fifo_sidecar_does_not_block(self, doc):
        files.write_body(doc, "v2\n")
        (rev,) = files.list_revisions(doc)
        _sidecar(rev).unlink()
        os.mkfifo(_sidecar(rev))
        assert files.read_revision_meta(rev) is None

    def test_non_snapshot_path_returns_none(self, doc):
        files.write_body(doc, "v2\n")
        (rev,) = files.list_revisions(doc)
        assert files.read_revision_meta(files.doc_paths(doc).body) is None
        assert files.read_revision_meta(_sidecar(rev)) is None


# ---------------------------------------------------------------------------
# The service: attribution follows the body, not the row
# ---------------------------------------------------------------------------


def _sources(doc_id):
    return [files.read_revision_meta(r)["source"] for r in files.list_revisions(doc_id)]


class TestServiceSource:
    def test_asset_only_write_does_not_take_over_attribution(self, docs_env):
        """A appends, B adds an image without touching the body, A appends
        again: the second snapshot holds A's body and says so, although
        the row's last_write_source named B in between."""
        doc = seed_doc(docs_env, content="start\n")
        a = make_caller(docs_env)
        b = make_caller(docs_env)
        _run(svc().append_to_doc(a, doc["id"], "from a"))
        (workspace_dir(docs_env, b) / "chart.png").write_bytes(PNG)
        _run(svc().add_doc_image(b, doc["id"], "chart.png", placement="none"))
        row = _run(docs_env.doc_store.get_doc(doc["id"]))
        assert row["last_write_source"] == f"conversation:{b.conversation_id}"
        assert files.read_doc_meta(doc["id"])["source"] == f"conversation:{a.conversation_id}"
        _run(svc().append_to_doc(a, doc["id"], "again from a"))
        assert _sources(doc["id"]) == [None, f"conversation:{a.conversation_id}"]
        second = files.list_revisions(doc["id"])[-1]
        assert second.read_text() == "start\n\nfrom a\n"

    def test_append_and_edit_record_the_writer_of_each_body(self, docs_env):
        doc = seed_doc(docs_env, content="line one\n")  # seeded: no source
        a = make_caller(docs_env)
        b = make_caller(docs_env)
        _run(svc().append_to_doc(a, doc["id"], "from a"))
        _run(svc().append_to_doc(b, doc["id"], "from b"))
        _run(svc().read_doc(b, doc["id"]))
        _run(svc().edit_doc(b, doc["id"], "from a", "FROM A"))
        assert _sources(doc["id"]) == [
            None,
            f"conversation:{a.conversation_id}",
            f"conversation:{b.conversation_id}",
        ]
        assert files.read_doc_meta(doc["id"])["source"] == f"conversation:{b.conversation_id}"

    def test_explicit_write_source(self, docs_env):
        doc = seed_doc(docs_env, content="x\n")
        caller = make_caller(docs_env)
        _run(svc().append_to_doc(caller, doc["id"], "one", write_source="action_request:7"))
        assert files.read_doc_meta(doc["id"])["source"] == "action_request:7"
        _run(svc().append_to_doc(caller, doc["id"], "two"))
        assert _sources(doc["id"]) == [None, "action_request:7"]

    def test_creates_write_the_meta(self, docs_env):
        caller = make_caller(docs_env)
        created = _run(svc().create_doc(caller, "From chat", "hello\n"))
        assert files.read_doc_meta(created["id"]) == {
            "written_at": files.read_doc_meta(created["id"])["written_at"],
            "source": f"conversation:{caller.conversation_id}",
            "size": len(b"hello\n"),
        }
        ui = _run(svc().create_doc_from_ui(docs_env.users["alice"], "From UI"))
        assert files.read_doc_meta(ui["id"])["source"] == "ui"
        assert files.read_doc_meta(ui["id"])["size"] == 0

    def test_ui_created_doc_then_image_append(self, docs_env):
        doc = _run(svc().create_doc_from_ui(docs_env.users["alice"], "Report"))
        caller = make_caller(docs_env)
        _run(svc().append_to_doc(caller, doc["id"], "# Report"))
        (workspace_dir(docs_env, caller) / "chart.png").write_bytes(PNG)
        _run(svc().add_doc_image(caller, doc["id"], "chart.png", alt="Chart"))
        # Creating took no snapshot; the append replaced the "ui" body, the
        # image append (a body change) replaced the conversation's.
        assert _sources(doc["id"]) == ["ui", f"conversation:{caller.conversation_id}"]
