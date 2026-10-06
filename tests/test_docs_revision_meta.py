"""Quest Docs revision metadata sidecars (chat/docs/files.py).

Every ``revisions/<ts>-<n>.md`` snapshot gets a ``<ts>-<n>.json`` sidecar
``{"written_at", "source", "size"}``. Pruning (date and count rules) removes
both halves together, an orphan sidecar goes too, a missing sidecar is
tolerated (listing and counting use the ``.md`` files only), and
``read_revision_meta`` returns None for anything missing or malformed. The
service passes the doc row's ``last_write_source`` -- the writer of the body
being replaced -- as the snapshot source.
"""

from __future__ import annotations

import errno
import json
import os
import time
import uuid
from datetime import datetime, timezone

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
def doc(docs_root):
    doc_id = str(uuid.uuid4())
    files.init_doc(doc_id, INITIAL)
    return doc_id


@pytest.fixture()
def frozen_now(monkeypatch):
    """Freeze files.datetime.now at the real current second (pruning is
    mtime-based, so every snapshot stays inside the retention window)."""
    fixed = datetime.now(timezone.utc).replace(microsecond=0)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(files, "datetime", _Frozen)
    return fixed


def _age(path, days):
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


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
# Writing the sidecar
# ---------------------------------------------------------------------------


class TestSidecarWrite:
    def test_written_with_source_and_size(self, doc):
        files.write_body(doc, "v2\n", snapshot_source="conversation:abc")
        (rev,) = files.list_revisions(doc)
        meta = _load(rev)
        assert set(meta) == {"written_at", "source", "size"}
        assert meta["source"] == "conversation:abc"
        assert meta["size"] == len(INITIAL.encode()) == rev.stat().st_size
        # ISO UTC with Z, the same second as the snapshot's file name.
        assert meta["written_at"].endswith("Z")
        written = datetime.fromisoformat(meta["written_at"].replace("Z", "+00:00"))
        assert written.tzinfo is not None
        assert written.strftime("%Y%m%dT%H%M%SZ") == rev.name.split("-")[0]
        assert abs((datetime.now(timezone.utc) - written).total_seconds()) < 60
        assert files.read_revision_meta(rev) == meta

    def test_size_is_bytes_of_the_snapshot(self, doc):
        files.write_body(doc, "héllo wörld\n")
        files.write_body(doc, "next\n", snapshot_source="ui")
        newest = files.list_revisions(doc)[-1]
        assert newest.read_text() == "héllo wörld\n"
        assert _load(newest)["size"] == len("héllo wörld\n".encode("utf-8"))

    def test_source_defaults_to_null(self, doc):
        files.write_body(doc, "v2\n")
        (rev,) = files.list_revisions(doc)
        assert _load(rev)["source"] is None
        assert files.read_revision_meta(rev)["source"] is None

    def test_modify_body_passes_source(self, doc):
        files.modify_body(doc, lambda cur: cur + "more\n", snapshot_source="ui")
        (rev,) = files.list_revisions(doc)
        assert rev.read_text() == INITIAL
        assert _load(rev) == {
            "written_at": _load(rev)["written_at"],
            "source": "ui",
            "size": len(INITIAL.encode()),
        }

    def test_identical_body_writes_nothing(self, doc):
        files.write_body(doc, INITIAL, snapshot_source="ui")
        files.modify_body(doc, lambda cur: cur, snapshot_source="ui")
        assert os.listdir(_rev_dir(doc)) == []

    def test_snapshot_false_writes_nothing(self, doc):
        files.write_body(doc, "v2\n", snapshot=False, snapshot_source="ui")
        files.modify_body(doc, lambda cur: cur + "x", snapshot=False, snapshot_source="ui")
        assert os.listdir(_rev_dir(doc)) == []

    def test_same_second_snapshots_get_paired_names(self, doc, frozen_now):
        stamp = frozen_now.strftime("%Y%m%dT%H%M%SZ")
        for i in range(3):
            files.write_body(doc, f"v{i}\n", snapshot_source=f"conversation:{i}")
        assert sorted(os.listdir(_rev_dir(doc))) == sorted(
            f"{stamp}-{n}.{ext}" for n in (1, 2, 3) for ext in ("md", "json")
        )
        revs = files.list_revisions(doc)
        assert [files.read_revision_meta(r)["source"] for r in revs] == [
            "conversation:0", "conversation:1", "conversation:2",
        ]
        assert [files.read_revision_meta(r)["size"] for r in revs] == [
            r.stat().st_size for r in revs
        ]

    def test_allocation_skips_an_orphan_sidecar_name(self, doc, frozen_now):
        stamp = frozen_now.strftime("%Y%m%dT%H%M%SZ")
        rev_dir = _rev_dir(doc)
        (rev_dir / f"{stamp}-1.json").write_text("stale orphan")
        files.write_body(doc, "v2\n", snapshot_source="ui")
        # The snapshot took -2 (never a .md beside the stale -1.json), and
        # the orphan was pruned after the write.
        assert sorted(os.listdir(rev_dir)) == [f"{stamp}-2.json", f"{stamp}-2.md"]
        (rev,) = files.list_revisions(doc)
        assert files.read_revision_meta(rev)["source"] == "ui"

    def test_sidecar_failure_does_not_block_the_write(self, doc, monkeypatch):
        real = files._write_new_file

        def failing(path, data):
            if path.suffix == ".json":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data)

        monkeypatch.setattr(files, "_write_new_file", failing)
        files.write_body(doc, "v2\n", snapshot_source="ui")
        assert files.read_body(doc) == "v2\n"
        (rev,) = files.list_revisions(doc)
        assert rev.read_text() == INITIAL
        assert not os.path.lexists(_sidecar(rev))
        assert files.read_revision_meta(rev) is None


# ---------------------------------------------------------------------------
# Pruning in lockstep
# ---------------------------------------------------------------------------


class TestSidecarPrune:
    def test_date_rule_prunes_the_pair(self, doc):
        files.write_body(doc, "v2\n", snapshot_source="a")
        files.write_body(doc, "v3\n", snapshot_source="b")
        old, newer = files.list_revisions(doc)
        _age(old, 40)  # the .md's mtime decides; the sidecar's is irrelevant
        files.write_body(doc, "v4\n", snapshot_source="c")
        assert not os.path.lexists(old)
        assert not os.path.lexists(_sidecar(old))
        assert _load(newer)["source"] == "b"
        assert _stems(doc, ".md") == _stems(doc, ".json")
        assert len(files.list_revisions(doc)) == 2

    def test_newest_pair_kept_even_if_old(self, doc):
        files.write_body(doc, "v2\n", snapshot_source="a")
        (only,) = files.list_revisions(doc)
        _age(only, 400)
        _age(_sidecar(only), 400)
        files.write_body(doc, "v3\n", snapshot=False)
        assert files.list_revisions(doc) == [only]
        assert files.read_revision_meta(only)["source"] == "a"

    def test_count_rule_prunes_pairs(self, doc, monkeypatch):
        monkeypatch.setattr(constants, "DOC_REVISION_MAX_COUNT", 5)
        for i in range(1, 12):
            files.write_body(doc, f"v{i}\n", snapshot_source=f"s{i - 1}")
        revs = files.list_revisions(doc)
        assert len(revs) == 5
        assert _stems(doc, ".md") == _stems(doc, ".json")
        # The newest five snapshots survive, each with its own sidecar.
        assert [files.read_revision_meta(r)["source"] for r in revs] == [
            f"s{i}" for i in range(6, 11)
        ]
        assert revs[-1].read_text() == "v10\n"

    def test_orphan_sidecars_pruned(self, doc):
        files.write_body(doc, "v2\n", snapshot_source="a")
        (rev,) = files.list_revisions(doc)
        rev.unlink()  # e.g. a prune interrupted between the two unlinks
        other = _rev_dir(doc) / "20200101T000000Z-1.json"
        other.write_text("{}")
        files.write_body(doc, "v3\n", snapshot_source="b")
        assert not os.path.lexists(_sidecar(rev))
        assert not os.path.lexists(other)
        (new,) = files.list_revisions(doc)
        assert files.read_revision_meta(new)["source"] == "b"

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
# Missing / malformed sidecars
# ---------------------------------------------------------------------------


class TestReadRevisionMeta:
    def test_missing_sidecar_tolerated(self, doc):
        files.write_body(doc, "v2\n", snapshot_source="a")
        files.write_body(doc, "v3\n", snapshot_source="b")
        first, second = files.list_revisions(doc)
        _sidecar(first).unlink()
        assert files.list_revisions(doc) == [first, second]
        assert files.read_revision_meta(first) is None
        assert files.read_revision_meta(second)["source"] == "b"
        # Pruning a sidecar-less snapshot works, and counting ignores sidecars.
        _age(first, 40)
        files.write_body(doc, "v4\n", snapshot_source="c")
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
# The service records who wrote the body being replaced
# ---------------------------------------------------------------------------


def _sources(doc_id):
    return [files.read_revision_meta(r)["source"] for r in files.list_revisions(doc_id)]


class TestServiceSource:
    def test_append_and_edit_record_the_previous_writer(self, docs_env):
        doc = seed_doc(docs_env, content="line one\n")  # no last_write_source
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
        sizes = [files.read_revision_meta(r)["size"] for r in files.list_revisions(doc["id"])]
        assert sizes == [r.stat().st_size for r in files.list_revisions(doc["id"])]

    def test_explicit_write_source_is_recorded_for_the_next_snapshot(self, docs_env):
        doc = seed_doc(docs_env, content="x\n")
        caller = make_caller(docs_env)
        _run(svc().append_to_doc(caller, doc["id"], "one", write_source="action_request:7"))
        _run(svc().append_to_doc(caller, doc["id"], "two"))
        assert _sources(doc["id"]) == [None, "action_request:7"]

    def test_ui_created_doc_then_image_append(self, docs_env):
        alice = docs_env.users["alice"]
        doc = _run(svc().create_doc_from_ui(alice, "Report"))
        caller = make_caller(docs_env)
        _run(svc().append_to_doc(caller, doc["id"], "# Report"))
        (workspace_dir(docs_env, caller) / "chart.png").write_bytes(PNG)
        _run(svc().add_doc_image(caller, doc["id"], "chart.png", alt="Chart"))
        # Creating the doc took no snapshot; the append replaced the "ui"
        # body, the image append replaced the conversation's.
        assert _sources(doc["id"]) == ["ui", f"conversation:{caller.conversation_id}"]
