"""Tests for edit_skill content search-and-replace plumbing.

Covers ``apply_content_edit`` (exact replacement, replace_all, the
not-found / ambiguous / empty-result / oversize rejections) and
``build_content_diff`` (line typing, 1-based numbering, added/removed
counts) in ``chat/action_request_types/_skill_content_edit.py``, the
``ChatStorage`` skill-reads sidecar, and the ``skill_precard_check``
content-edit arm (read-before-edit gate, same-turn match rejection, and
``content_diff`` preview injection) against an isolated DB.
"""

import asyncio
import uuid

import pytest

from chat.action_request_types._skill_content_edit import (
    apply_content_edit,
    build_content_diff,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# apply_content_edit
# ---------------------------------------------------------------------------


def test_apply_single_replacement():
    new_content, replacements = apply_content_edit("a b c", "b", "B")
    assert new_content == "a B c"
    assert replacements == 1


def test_apply_replace_all():
    new_content, replacements = apply_content_edit(
        "x y x", "x", "z", replace_all=True
    )
    assert new_content == "z y z"
    assert replacements == 2


def test_apply_first_only_when_unique_match_required():
    # A unique match replaces only that occurrence.
    new_content, replacements = apply_content_edit("one two", "two", "2")
    assert new_content == "one 2"
    assert replacements == 1


def test_apply_not_found_raises():
    with pytest.raises(ValueError, match="old_string not found"):
        apply_content_edit("abc", "zzz", "y")


def test_apply_ambiguous_without_replace_all_raises():
    with pytest.raises(ValueError, match="appears 2 times"):
        apply_content_edit("dup dup", "dup", "x")


def test_apply_empty_result_raises():
    with pytest.raises(ValueError, match="empty"):
        apply_content_edit("only text", "only text", "  ")


def test_apply_oversize_result_raises():
    from db.skill_store import MAX_SKILL_CONTENT_SIZE
    big = "x" * MAX_SKILL_CONTENT_SIZE
    with pytest.raises(ValueError, match="maximum size"):
        apply_content_edit("small", "small", big + "y")


# ---------------------------------------------------------------------------
# build_content_diff
# ---------------------------------------------------------------------------


def test_diff_replace_line():
    diff = build_content_diff("keep\nold\nkeep2", "keep\nnew\nkeep2")
    assert diff["added"] == 1
    assert diff["removed"] == 1
    types = [line["type"] for line in diff["lines"]]
    assert types == ["context", "del", "add", "context"]
    del_line = diff["lines"][1]
    add_line = diff["lines"][2]
    assert del_line == {
        "type": "del", "old_line": 2, "new_line": None, "text": "old",
    }
    assert add_line == {
        "type": "add", "old_line": None, "new_line": 2, "text": "new",
    }


def test_diff_pure_insertion_and_numbering():
    diff = build_content_diff("a\nb", "a\nx\nb")
    assert diff["added"] == 1
    assert diff["removed"] == 0
    add_line = next(l for l in diff["lines"] if l["type"] == "add")
    assert add_line["new_line"] == 2
    # Context lines carry both sides' numbers.
    tail = diff["lines"][-1]
    assert tail == {
        "type": "context", "old_line": 2, "new_line": 3, "text": "b",
    }


def test_diff_covers_whole_content():
    old = "\n".join(f"line {i}" for i in range(20))
    new = old.replace("line 10", "line ten")
    diff = build_content_diff(old, new)
    # All 19 unchanged lines appear as context (the FE collapses them).
    assert sum(1 for l in diff["lines"] if l["type"] == "context") == 19


# ---------------------------------------------------------------------------
# ChatStorage skill-reads sidecar
# ---------------------------------------------------------------------------


def test_skill_reads_sidecar(monkeypatch, tmp_path):
    from chat.storage import ChatStorage

    conv_dir = tmp_path / "conv"
    conv_dir.mkdir()
    monkeypatch.setattr(
        ChatStorage,
        "_get_conversation_dir",
        staticmethod(lambda conversation_id: conv_dir),
    )

    assert ChatStorage.get_skill_read_ids("c1") == []
    ChatStorage.add_skill_read_ids("c1", ["s1", "s2"])
    ChatStorage.add_skill_read_ids("c1", ["s2", "s3", "s3"])
    assert ChatStorage.get_skill_read_ids("c1") == ["s1", "s2", "s3"]

    # All-known IDs skip the rewrite (mtime unchanged).
    reads_file = conv_dir / "skill_reads.json"
    before = reads_file.stat().st_mtime_ns
    ChatStorage.add_skill_read_ids("c1", ["s1"])
    assert reads_file.stat().st_mtime_ns == before


# ---------------------------------------------------------------------------
# skill_precard_check -- content-edit arm
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.skill_store as skill_store_mod

    return skill_store_mod, isolated_db.models


def _make_user(models_mod):
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"sce-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Precard Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _patch_read_ids(monkeypatch, ids):
    from chat.storage import ChatStorage
    monkeypatch.setattr(
        ChatStorage, "get_skill_read_ids", staticmethod(lambda cid: ids)
    )


def test_precard_content_edit_read_gate(_isolated_db, monkeypatch):
    from chat.action_request_types.skill_precard import skill_precard_check

    skill_store, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _run(skill_store.create_skill(
        creator_id=uid, name="S", content="hello world",
    ))
    _patch_read_ids(monkeypatch, [])

    params = {
        "skill_id": skill["id"], "old_string": "world", "new_string": "there",
    }
    with pytest.raises(ValueError, match="not been loaded or read"):
        _run(skill_precard_check(
            "edit_skill", params, {"id": uid}, None, conversation_id="c1",
        ))


def test_precard_content_edit_injects_diff(_isolated_db, monkeypatch):
    from chat.action_request_types.skill_precard import skill_precard_check

    skill_store, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _run(skill_store.create_skill(
        creator_id=uid, name="S", content="hello world",
    ))
    _patch_read_ids(monkeypatch, [skill["id"]])

    params = {
        "skill_id": skill["id"], "old_string": "world", "new_string": "there",
    }
    _run(skill_precard_check(
        "edit_skill", params, {"id": uid}, None, conversation_id="c1",
    ))
    diff = params["content_diff"]
    assert diff["added"] == 1
    assert diff["removed"] == 1
    assert [l["type"] for l in diff["lines"]] == ["del", "add"]
    # The skill itself is untouched at proposal time.
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["content"] == "hello world"


def test_precard_content_edit_rejects_stale_match(_isolated_db, monkeypatch):
    from chat.action_request_types.skill_precard import skill_precard_check

    skill_store, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _run(skill_store.create_skill(
        creator_id=uid, name="S", content="hello world",
    ))
    _patch_read_ids(monkeypatch, [skill["id"]])

    params = {
        "skill_id": skill["id"], "old_string": "nope", "new_string": "there",
    }
    with pytest.raises(ValueError, match="old_string not found"):
        _run(skill_precard_check(
            "edit_skill", params, {"id": uid}, None, conversation_id="c1",
        ))


def test_precard_non_content_edit_skips_gate(_isolated_db, monkeypatch):
    # Metadata-only edits (name / description / visibility / shares) do not
    # require the skill to have been read.
    from chat.action_request_types.skill_precard import skill_precard_check

    skill_store, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _run(skill_store.create_skill(
        creator_id=uid, name="S", content="hello world",
    ))
    _patch_read_ids(monkeypatch, [])

    params = {"skill_id": skill["id"], "name": "Renamed"}
    _run(skill_precard_check(
        "edit_skill", params, {"id": uid}, None, conversation_id="c1",
    ))
    assert "content_diff" not in params
