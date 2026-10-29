"""The per-conversation doc notes seen record, ``doc_notes_seen.json``.

Covers ``ChatStorage.get_doc_notes_seen`` / ``record_doc_notes_seen`` /
``clear_doc_notes_seen`` (issue #187, spec sections 3 and 4.2):

1. Round trip and the exact on-disk shape; docs are independent; a missing
   file or conversation directory reads as no record.
2. The per-field maximum rule, the ``(0, 0)`` creator record, and the
   skipped write when the stored entry would not change.
3. Tolerant reading: a corrupt file (not JSON, invalid UTF-8, wrong top
   level, a directory at the path) reads as no record for every doc, a
   malformed entry as no record for that doc only; the next record rewrites
   the file with the valid entries plus the new one.
4. A failed write (missing conversation directory, ``OSError`` from the
   atomic write) is logged at warning and swallowed; the directory is never
   created.
5. Clearing removes only this file, is a no-op when missing, and logs and
   swallows an ``OSError``.
6. Argument validation (``ValueError``) and bad conversation ids
   (``InvalidStorageIdError``) from all three methods; the path resolves
   through ``ChatStorage._get_conversation_dir`` (test_compaction.py fakes it).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from pathlib import Path

import pytest

import chat.docs.notes as notes_mod
import chat.storage as storage_mod


# Other suites reload chat.storage, so bind through the module at call time.
def _cs():
    return storage_mod.ChatStorage


def _invalid():
    return storage_mod.InvalidStorageIdError


def _seen(instructions, memories):
    return notes_mod.NotesSeen(instructions=instructions, memories=memories)


BAD_IDS = ["", ".", "..", "a/b", "../x", "x/..", "/abs", "/", "a\\b/c"]

FILE_NAME = "doc_notes_seen.json"


@pytest.fixture()
def chats_root(tmp_path, monkeypatch):
    root = tmp_path / "chats"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", root, raising=True)
    return root


@pytest.fixture()
def conv(chats_root):
    conv_dir = chats_root / "c1"
    conv_dir.mkdir(parents=True)
    return conv_dir


@pytest.fixture()
def warnings_log(caplog, monkeypatch):
    # chat/logging_config.py turns propagation off for the "chat" logger.
    monkeypatch.setattr(logging.getLogger("chat"), "propagate", True)
    caplog.set_level(logging.WARNING, logger="chat.storage")
    return caplog


def _storage_warnings(caplog):
    return [
        r for r in caplog.records
        if r.name == "chat.storage" and r.levelno == logging.WARNING
    ]


def _on_disk(conv_dir):
    return json.loads((conv_dir / FILE_NAME).read_text())


def _age(path, days):
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_missing_file_reads_none(self, conv):
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert not (conv / FILE_NAME).exists()

    def test_missing_conversation_dir_reads_none(self, chats_root):
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert not (chats_root / "c1").exists()

    def test_round_trip_and_on_disk_shape(self, conv):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(3, 7)
        assert _on_disk(conv) == {"docs": {"d1": {"instructions": 3, "memories": 7}}}

    def test_docs_are_independent(self, conv):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        _cs().record_doc_notes_seen("c1", "d2", 1, 0)
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(3, 7)
        assert _cs().get_doc_notes_seen("c1", "d2") == _seen(1, 0)
        assert _cs().get_doc_notes_seen("c1", "d3") is None
        assert _on_disk(conv) == {
            "docs": {
                "d1": {"instructions": 3, "memories": 7},
                "d2": {"instructions": 1, "memories": 0},
            }
        }

    def test_conversations_are_independent(self, conv, chats_root):
        (chats_root / "c2").mkdir()
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        assert _cs().get_doc_notes_seen("c2", "d1") is None

    def test_no_temp_files_left(self, conv):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        _cs().record_doc_notes_seen("c1", "d1", 4, 7)
        assert sorted(p.name for p in conv.iterdir()) == [FILE_NAME]

    def test_path_resolves_through_private_conversation_dir(self, tmp_path, monkeypatch):
        # tests/test_compaction.py fakes this exact attribute.
        target = tmp_path / "faked"
        target.mkdir()
        monkeypatch.setattr(
            _cs(), "_get_conversation_dir",
            staticmethod(lambda conversation_id: target),
        )
        _cs().record_doc_notes_seen("any", "d1", 1, 2)
        assert (target / FILE_NAME).is_file()
        assert _cs().get_doc_notes_seen("any", "d1") == _seen(1, 2)
        _cs().clear_doc_notes_seen("any")
        assert not (target / FILE_NAME).exists()


# ---------------------------------------------------------------------------
# Max rule and skipped writes
# ---------------------------------------------------------------------------


class TestRecordRule:
    def test_per_field_maximum(self, conv):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        _cs().record_doc_notes_seen("c1", "d1", 2, 9)
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(3, 9)
        _cs().record_doc_notes_seen("c1", "d1", 5, 1)
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(5, 9)
        assert _on_disk(conv) == {"docs": {"d1": {"instructions": 5, "memories": 9}}}

    def test_creator_zero_record_is_a_record(self, conv):
        _cs().record_doc_notes_seen("c1", "new-doc", 0, 0)
        seen = _cs().get_doc_notes_seen("c1", "new-doc")
        assert seen is not None
        assert seen == _seen(0, 0)
        assert _on_disk(conv) == {
            "docs": {"new-doc": {"instructions": 0, "memories": 0}}
        }

    def test_noop_record_skips_write(self, conv):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        path = conv / FILE_NAME
        mtime = path.stat().st_mtime_ns
        _age(path, 1)
        aged = path.stat().st_mtime_ns
        assert aged != mtime
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)  # same
        _cs().record_doc_notes_seen("c1", "d1", 2, 5)  # older on both fields
        assert path.stat().st_mtime_ns == aged
        _cs().record_doc_notes_seen("c1", "d1", 3, 8)  # newer memories
        assert path.stat().st_mtime_ns != aged
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(3, 8)

    def test_record_after_clear_starts_fresh(self, conv):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        _cs().clear_doc_notes_seen("c1")
        _cs().record_doc_notes_seen("c1", "d1", 1, 1)
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(1, 1)


# ---------------------------------------------------------------------------
# Corrupt files and malformed entries
# ---------------------------------------------------------------------------


VALID_D1 = {"instructions": 3, "memories": 7}

CORRUPT_CONTENTS = {
    "not-json": b"{not json",
    "empty-file": b"",
    "invalid-utf8": (
        b'{"docs": {"d1": {"instructions": 3, "memories": 7}, '
        b'"d\xff": {"instructions": 1, "memories": 1}}}'
    ),
    "json-list": b'[{"docs": {"d1": {"instructions": 3, "memories": 7}}}]',
    "json-string": b'"docs"',
    "empty-object": b"{}",
    "docs-list": b'{"docs": [{"d1": {"instructions": 3, "memories": 7}}]}',
    "docs-null": b'{"docs": null}',
    "deeply-nested": b"[" * 100_000 + b"]" * 100_000,
}


class TestCorruptFile:
    @pytest.mark.parametrize("content", CORRUPT_CONTENTS.values(), ids=CORRUPT_CONTENTS.keys())
    def test_reads_none_then_record_replaces(self, conv, warnings_log, content):
        (conv / FILE_NAME).write_bytes(content)
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert _cs().get_doc_notes_seen("c1", "d2") is None
        assert _storage_warnings(warnings_log)

        _cs().record_doc_notes_seen("c1", "d2", 1, 2)
        assert _on_disk(conv) == {"docs": {"d2": {"instructions": 1, "memories": 2}}}
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert _cs().get_doc_notes_seen("c1", "d2") == _seen(1, 2)

    def test_directory_at_path(self, conv, warnings_log):
        (conv / FILE_NAME).mkdir()
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert _storage_warnings(warnings_log)
        warnings_log.clear()

        # The atomic replace cannot land on a directory: logged and swallowed.
        _cs().record_doc_notes_seen("c1", "d1", 1, 2)
        assert _storage_warnings(warnings_log)
        assert (conv / FILE_NAME).is_dir()
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert [p.name for p in conv.iterdir()] == [FILE_NAME]  # no temp file left
        warnings_log.clear()

        # Clearing cannot unlink a directory either: logged and swallowed.
        _cs().clear_doc_notes_seen("c1")
        assert _storage_warnings(warnings_log)
        assert (conv / FILE_NAME).is_dir()


MALFORMED_ENTRIES = {
    "string": "x",
    "list": [3, 7],
    "null": None,
    "instructions-missing": {"memories": 7},
    "memories-missing": {"instructions": 3},
    "string-number": {"instructions": "3", "memories": 7},
    "float": {"instructions": 3, "memories": 7.0},
    "true": {"instructions": True, "memories": 7},
    "false": {"instructions": 3, "memories": False},
    "negative": {"instructions": 3, "memories": -1},
    "null-field": {"instructions": None, "memories": 7},
}


class TestMalformedEntries:
    @pytest.mark.parametrize("entry", MALFORMED_ENTRIES.values(), ids=MALFORMED_ENTRIES.keys())
    def test_only_that_doc_reads_none(self, conv, warnings_log, entry):
        (conv / FILE_NAME).write_text(json.dumps({"docs": {"bad": entry, "d1": VALID_D1}}))
        assert _cs().get_doc_notes_seen("c1", "bad") is None
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(3, 7)
        assert _storage_warnings(warnings_log)

        _cs().record_doc_notes_seen("c1", "other", 1, 1)
        assert _on_disk(conv) == {
            "docs": {"d1": VALID_D1, "other": {"instructions": 1, "memories": 1}}
        }

    def test_malformed_entry_does_not_feed_the_maximum(self, conv):
        (conv / FILE_NAME).write_text(
            json.dumps({"docs": {"d1": {"instructions": 99, "memories": "9"}}})
        )
        _cs().record_doc_notes_seen("c1", "d1", 1, 2)
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(1, 2)
        assert _on_disk(conv) == {"docs": {"d1": {"instructions": 1, "memories": 2}}}

    def test_empty_doc_id_key_ignored(self, conv):
        (conv / FILE_NAME).write_text(json.dumps({"docs": {"": VALID_D1, "d1": VALID_D1}}))
        assert _cs().get_doc_notes_seen("c1", "") is None
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(3, 7)


# ---------------------------------------------------------------------------
# Failed writes
# ---------------------------------------------------------------------------


class TestFailedWrite:
    def test_missing_conversation_dir_logged_not_created(self, chats_root, warnings_log):
        _cs().record_doc_notes_seen("c1", "d1", 1, 2)  # no exception
        assert not (chats_root / "c1").exists()
        warnings = _storage_warnings(warnings_log)
        assert len(warnings) == 1
        assert "conversation=c1" in warnings[0].getMessage()
        assert warnings[0].exc_info is not None
        assert _cs().get_doc_notes_seen("c1", "d1") is None

    def test_os_error_from_atomic_write_logged(self, conv, warnings_log, monkeypatch):
        _cs().record_doc_notes_seen("c1", "d1", 1, 2)
        before = (conv / FILE_NAME).read_bytes()

        def fail(path, data):
            raise OSError(errno.ENOSPC, "No space left on device", str(path))

        monkeypatch.setattr(storage_mod, "write_json_atomic", fail)
        _cs().record_doc_notes_seen("c1", "d1", 5, 6)  # no exception
        warnings = _storage_warnings(warnings_log)
        assert len(warnings) == 1
        assert "conversation=c1" in warnings[0].getMessage()
        assert isinstance(warnings[0].exc_info[1], OSError)
        assert (conv / FILE_NAME).read_bytes() == before
        assert _cs().get_doc_notes_seen("c1", "d1") == _seen(1, 2)


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


class TestClear:
    def test_removes_only_this_file(self, conv):
        _cs().add_doc_read_ids("c1", ["d1"])
        (conv / "chat_history.json").write_text('{"messages": []}')
        (conv / "sdk_history.json").write_text("[]")
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)

        _cs().clear_doc_notes_seen("c1")
        assert not (conv / FILE_NAME).exists()
        assert _cs().get_doc_notes_seen("c1", "d1") is None
        assert _cs().get_doc_read_ids("c1") == ["d1"]
        assert (conv / "chat_history.json").read_text() == '{"messages": []}'
        assert (conv / "sdk_history.json").read_text() == "[]"

    def test_missing_file_is_noop(self, conv, warnings_log):
        _cs().clear_doc_notes_seen("c1")
        assert list(conv.iterdir()) == []
        assert not _storage_warnings(warnings_log)

    def test_missing_conversation_dir_is_noop(self, chats_root, warnings_log):
        _cs().clear_doc_notes_seen("c1")
        assert not (chats_root / "c1").exists()
        assert not _storage_warnings(warnings_log)

    def test_os_error_logged_and_swallowed(self, conv, warnings_log, monkeypatch):
        _cs().record_doc_notes_seen("c1", "d1", 3, 7)
        real_unlink = Path.unlink

        def unlink(self, *args, **kwargs):
            if self.name == FILE_NAME:
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        _cs().clear_doc_notes_seen("c1")  # no exception
        warnings = _storage_warnings(warnings_log)
        assert len(warnings) == 1
        assert "conversation=c1" in warnings[0].getMessage()
        assert isinstance(warnings[0].exc_info[1], PermissionError)
        assert (conv / FILE_NAME).exists()


# ---------------------------------------------------------------------------
# Argument and id validation
# ---------------------------------------------------------------------------


BAD_VERSIONS = [-1, True, False, 1.0, "1", None]


class TestArguments:
    @pytest.mark.parametrize("bad", BAD_VERSIONS, ids=repr)
    def test_bad_instructions_version(self, conv, bad):
        with pytest.raises(ValueError, match="instructions_version"):
            _cs().record_doc_notes_seen("c1", "d1", bad, 0)
        assert not (conv / FILE_NAME).exists()

    @pytest.mark.parametrize("bad", BAD_VERSIONS, ids=repr)
    def test_bad_memories_version(self, conv, bad):
        with pytest.raises(ValueError, match="memories_version"):
            _cs().record_doc_notes_seen("c1", "d1", 0, bad)
        assert not (conv / FILE_NAME).exists()

    @pytest.mark.parametrize("bad", ["", None, 5, b"d1"], ids=repr)
    def test_bad_doc_id(self, conv, bad):
        with pytest.raises(ValueError, match="doc_id"):
            _cs().record_doc_notes_seen("c1", bad, 1, 1)
        assert not (conv / FILE_NAME).exists()

    def test_bad_arguments_are_not_storage_id_errors(self, conv):
        with pytest.raises(ValueError) as exc_info:
            _cs().record_doc_notes_seen("c1", "d1", -1, 0)
        assert not isinstance(exc_info.value, _invalid())


class TestBadConversationIds:
    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_get_rejects(self, chats_root, bad):
        with pytest.raises(_invalid()):
            _cs().get_doc_notes_seen(bad, "d1")

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_record_rejects(self, chats_root, bad):
        with pytest.raises(_invalid()):
            _cs().record_doc_notes_seen(bad, "d1", 1, 1)

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_clear_rejects(self, chats_root, bad):
        with pytest.raises(_invalid()):
            _cs().clear_doc_notes_seen(bad)
