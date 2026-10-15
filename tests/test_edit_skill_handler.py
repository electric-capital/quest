"""Tests for the edit_skill action_request handler.

Covers ``EditSkillHandler.validate_params`` (required skill_id, system-id
rejection, empty-payload rejection, per-field validators, the
old_string/new_string content search-and-replace pairing rules, the legacy
``content`` param rejection, the in-call share/non-shared-visibility
contradiction, and the project_autoload bool type-check),
``render_preview`` shape (incl. the skill_content_diff field), and
``execute`` for both user and project targets (content search-and-replace
resolution incl. the stale/ambiguous-match TOCTOU close, the read-gate,
and the legacy full-content path; share add/remove resolution, the
visibility-leaves-shared roster auto-clear, ownership/project-access
enforcement, the name-collision execute-time guard, and the project
auto-load toggle) against an isolated DB.
"""

import asyncio
import uuid

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.project_store as project_store_mod
    import db.skill_store as skill_store_mod
    import db.user_store as user_store_mod

    return skill_store_mod, project_store_mod, user_store_mod, isolated_db.models


def _make_user(models_mod, name="Skill Tester"):
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"esk-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name=name,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _user_email(models_mod, uid):
    from db.engine import AsyncSessionLocal

    async def _go():
        async with AsyncSessionLocal() as db:
            u = await db.get(models_mod.User, uid)
            return u.email

    return _run(_go())


def _handler():
    from chat.action_request_types.edit_skill import EditSkillHandler
    return EditSkillHandler()


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unknown parameter for edit_skill"):
        _handler().validate_params({"skill_id": "s", "name": "n", "bogus": 1})


def test_validate_rejects_missing_skill_id():
    with pytest.raises(ValueError, match="skill_id"):
        _handler().validate_params({"name": "n"})


def test_validate_rejects_empty_skill_id():
    with pytest.raises(ValueError, match="non-empty"):
        _handler().validate_params({"skill_id": "   ", "name": "n"})


def test_validate_rejects_system_skill_id():
    with pytest.raises(ValueError, match="system:.* are not editable"):
        _handler().validate_params({"skill_id": "system:memory", "name": "n"})


def test_validate_rejects_empty_payload():
    with pytest.raises(ValueError, match="No editable fields provided"):
        _handler().validate_params({"skill_id": "s"})


def test_validate_happy_path_scalar():
    out = _handler().validate_params({"skill_id": "  s ", "name": "  N "})
    assert out["skill_id"] == "s"
    assert out["name"] == "N"


def test_validate_rejects_empty_name_when_present():
    with pytest.raises(ValueError, match="empty"):
        _handler().validate_params({"skill_id": "s", "name": "   "})


def test_validate_rejects_legacy_content_param():
    # Full-content replacement was replaced by old_string/new_string; the
    # rejection message steers the model to the new interface.
    with pytest.raises(ValueError, match="no longer supported"):
        _handler().validate_params({"skill_id": "s", "content": "new body"})


def test_validate_accepts_content_search_replace():
    out = _handler().validate_params({
        "skill_id": "s", "old_string": "old", "new_string": "new",
        "replace_all": True,
    })
    assert out["old_string"] == "old"
    assert out["new_string"] == "new"
    assert out["replace_all"] is True


def test_validate_accepts_empty_new_string_deletion():
    out = _handler().validate_params(
        {"skill_id": "s", "old_string": "gone", "new_string": ""}
    )
    assert out["new_string"] == ""


def test_validate_rejects_unpaired_old_new_string():
    with pytest.raises(ValueError, match="together"):
        _handler().validate_params({"skill_id": "s", "old_string": "old"})
    with pytest.raises(ValueError, match="together"):
        _handler().validate_params({"skill_id": "s", "new_string": "new"})


def test_validate_rejects_empty_old_string():
    with pytest.raises(ValueError, match="old_string must be a non-empty"):
        _handler().validate_params(
            {"skill_id": "s", "old_string": "", "new_string": "new"}
        )


def test_validate_rejects_identical_old_new_string():
    with pytest.raises(ValueError, match="identical"):
        _handler().validate_params(
            {"skill_id": "s", "old_string": "same", "new_string": "same"}
        )


def test_validate_rejects_oversize_new_string():
    from db.skill_store import MAX_SKILL_CONTENT_SIZE
    too_big = "x" * (MAX_SKILL_CONTENT_SIZE + 1)
    with pytest.raises(ValueError, match="maximum skill content size"):
        _handler().validate_params(
            {"skill_id": "s", "old_string": "o", "new_string": too_big}
        )


def test_validate_rejects_replace_all_without_old_string():
    with pytest.raises(ValueError, match="only valid alongside"):
        _handler().validate_params(
            {"skill_id": "s", "name": "N", "replace_all": True}
        )


def test_validate_rejects_non_bool_replace_all():
    with pytest.raises(ValueError, match="replace_all must be a boolean"):
        _handler().validate_params({
            "skill_id": "s", "old_string": "o", "new_string": "n",
            "replace_all": "yes",
        })


def test_validate_rejects_bad_visibility():
    with pytest.raises(ValueError, match="Invalid visibility"):
        _handler().validate_params({"skill_id": "s", "visibility": "secret"})


def test_validate_rejects_project_visibility():
    with pytest.raises(ValueError, match="Invalid visibility"):
        _handler().validate_params({"skill_id": "s", "visibility": "project"})


def test_validate_normalizes_share_emails():
    out = _handler().validate_params({
        "skill_id": "s", "visibility": "shared",
        "add_share_emails": [" a@b.co "], "remove_share_emails": ["c@d.co"],
    })
    assert out["add_share_emails"] == ["a@b.co"]
    assert out["remove_share_emails"] == ["c@d.co"]


def test_validate_rejects_non_list_share_emails():
    with pytest.raises(ValueError, match="add_share_emails must be a list"):
        _handler().validate_params({"skill_id": "s", "add_share_emails": "a@b.co"})


def test_validate_rejects_share_change_with_nonshared_visibility():
    # D3a: in-call contradiction -- setting visibility away from shared while
    # also adding/removing shares.
    with pytest.raises(ValueError, match="only valid when the skill is shared"):
        _handler().validate_params({
            "skill_id": "s", "visibility": "private",
            "add_share_emails": ["a@b.co"],
        })


def test_validate_accepts_project_autoload_bool():
    out = _handler().validate_params({"skill_id": "s", "project_autoload": True})
    assert out["project_autoload"] is True


def test_validate_rejects_non_bool_project_autoload():
    with pytest.raises(ValueError, match="project_autoload must be a boolean"):
        _handler().validate_params({"skill_id": "s", "project_autoload": "yes"})


def test_validate_project_autoload_alone_is_not_empty_payload():
    # D8: skill_id + project_autoload alone is a valid (non-empty) payload.
    out = _handler().validate_params({"skill_id": "s", "project_autoload": False})
    assert out["project_autoload"] is False


# ---------------------------------------------------------------------------
# render_preview
# ---------------------------------------------------------------------------


def test_render_preview_uses_injected_name_and_lists_fields():
    handler = _handler()

    async def _go():
        return await handler.render_preview({
            "skill_id": "abcdef12-3456",
            "current_skill_name": "My Skill",
            "name": "New Name",
            "visibility": "shared",
            "add_share_emails": ["a@b.co"],
            "project_autoload": True,
        })

    out = _run(_go())
    keys = [f["key"] for f in out]
    assert out[0] == {"key": "Skill", "value": "My Skill"}
    assert "Name" in keys
    assert "Visibility" in keys
    assert "Add shares" in keys
    assert "Auto-load" in keys
    autoload_row = next(f for f in out if f["key"] == "Auto-load")
    assert autoload_row["value"] == "Enable"


def test_render_preview_falls_back_to_short_id():
    handler = _handler()

    async def _go():
        return await handler.render_preview({"skill_id": "abcdef1234567890", "name": "N"})

    out = _run(_go())
    assert out[0]["value"] == "Skill #abcdef12"


def test_render_preview_content_diff_field():
    # With the precard-injected content_diff, the Content field is the
    # structured skill_content_diff type the FE diff component renders.
    handler = _handler()
    diff = {
        "added": 1,
        "removed": 1,
        "lines": [
            {"type": "del", "old_line": 1, "new_line": None, "text": "old"},
            {"type": "add", "old_line": None, "new_line": 1, "text": "new"},
        ],
    }

    async def _go():
        return await handler.render_preview({
            "skill_id": "s",
            "old_string": "old",
            "new_string": "new",
            "content_diff": diff,
        })

    out = _run(_go())
    content_row = next(f for f in out if f["key"] == "Content")
    assert content_row["type"] == "skill_content_diff"
    assert content_row["diff"] == diff
    assert content_row["value"] == "+1 / -1 line(s)"


def test_render_preview_content_edit_fallback_without_diff():
    # No injected diff (defensive): fall back to raw Replace / With rows.
    handler = _handler()

    async def _go():
        return await handler.render_preview(
            {"skill_id": "s", "old_string": "old", "new_string": "new"}
        )

    out = _run(_go())
    keys = [f["key"] for f in out]
    assert "Replace" in keys
    assert "With" in keys


def test_render_preview_legacy_content_field():
    # Legacy rows proposed before the search-and-replace interface still
    # render their full content.
    handler = _handler()

    async def _go():
        return await handler.render_preview(
            {"skill_id": "s", "content": "whole body"}
        )

    out = _run(_go())
    content_row = next(f for f in out if f["key"] == "Content")
    assert content_row["value"] == "whole body"
    assert "type" not in content_row


# ---------------------------------------------------------------------------
# execute -- user skill
# ---------------------------------------------------------------------------


def _make_user_skill(skill_store, uid, name="My Skill", visibility="private", content="c"):
    return _run(skill_store.create_skill(
        creator_id=uid, name=name, content=content, visibility=visibility,
    ))


def test_execute_user_updates_scalar_fields(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid, content="line one\nline two")
    handler = _handler()

    async def _go():
        return await handler.execute(
            {
                "skill_id": skill["id"],
                "name": "Renamed",
                "old_string": "line two",
                "new_string": "line 2",
            },
            {"id": uid},
        )

    result = _run(_go())
    assert result == {"success": True, "skill_id": skill["id"], "target": "user"}
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["name"] == "Renamed"
    assert persisted["content"] == "line one\nline 2"


def test_execute_content_edit_replace_all(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid, content="foo bar foo")
    handler = _handler()

    _run(handler.execute(
        {
            "skill_id": skill["id"],
            "old_string": "foo",
            "new_string": "baz",
            "replace_all": True,
        },
        {"id": uid},
    ))
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["content"] == "baz bar baz"


def test_execute_content_edit_stale_old_string_fails(_isolated_db):
    # TOCTOU close: the skill changed while the card sat open, so the
    # stale old_string no longer matches and the approve fails cleanly.
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid, content="current text")
    handler = _handler()

    async def _go():
        return await handler.execute(
            {
                "skill_id": skill["id"],
                "old_string": "text that is gone",
                "new_string": "new",
            },
            {"id": uid},
        )

    with pytest.raises(RuntimeError, match="old_string not found"):
        _run(_go())
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["content"] == "current text"


def test_execute_content_edit_ambiguous_match_fails(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid, content="dup and dup")
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "old_string": "dup", "new_string": "x"},
            {"id": uid},
        )

    with pytest.raises(RuntimeError, match="appears 2 times"):
        _run(_go())


def test_execute_content_edit_read_gate(_isolated_db, monkeypatch):
    # With a conversation_id, the execute-time read gate rejects content
    # edits for skills the conversation never loaded or read.
    from chat.storage import ChatStorage

    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid, content="c")
    handler = _handler()

    monkeypatch.setattr(
        ChatStorage, "get_skill_read_ids", staticmethod(lambda cid: [])
    )

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "old_string": "c", "new_string": "d"},
            {"id": uid},
            conversation_id="conv-1",
        )

    with pytest.raises(RuntimeError, match="not been loaded or read"):
        _run(_go())

    # Once the read is recorded, the same edit goes through.
    monkeypatch.setattr(
        ChatStorage,
        "get_skill_read_ids",
        staticmethod(lambda cid: [skill["id"]]),
    )
    _run(_go())
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["content"] == "d"


def test_execute_legacy_full_content_row(_isolated_db):
    # Rows proposed before the search-and-replace interface carry a full
    # ``content`` param; execute still honors them.
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid)
    handler = _handler()

    _run(handler.execute(
        {"skill_id": skill["id"], "content": "new body"}, {"id": uid},
    ))
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["content"] == "new body"


def test_execute_user_visibility_change(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid)
    handler = _handler()

    _run(handler.execute(
        {"skill_id": skill["id"], "visibility": "public"}, {"id": uid},
    ))
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["visibility"] == "public"


def test_execute_user_share_add_remove_resolution(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    owner = _make_user(models_mod, name="Owner")
    friend = _make_user(models_mod, name="Friend")
    gone = _make_user(models_mod, name="Gone")
    friend_email = _user_email(models_mod, friend)
    gone_email = _user_email(models_mod, gone)

    skill = _make_user_skill(skill_store, owner, visibility="shared")
    # Pre-seed a share for `gone` so a remove can be observed.
    _run(skill_store.add_skill_shares(skill["id"], [gone]))
    handler = _handler()

    async def _go():
        return await handler.execute(
            {
                "skill_id": skill["id"],
                "add_share_emails": [friend_email, "nobody@x.co"],
                "remove_share_emails": [gone_email],
            },
            {"id": owner},
        )

    _run(_go())
    shares = _run(skill_store.list_skill_shares(skill["id"]))
    user_ids = {s["user_id"] for s in shares}
    assert friend in user_ids
    assert gone not in user_ids


def test_execute_user_visibility_leaves_shared_clears_roster(_isolated_db):
    # D3a: moving visibility away from shared wipes the entire roster.
    skill_store, _ps, _us, models_mod = _isolated_db
    owner = _make_user(models_mod, name="Owner")
    friend = _make_user(models_mod, name="Friend")

    skill = _make_user_skill(skill_store, owner, visibility="shared")
    _run(skill_store.add_skill_shares(skill["id"], [friend]))
    handler = _handler()

    _run(handler.execute(
        {"skill_id": skill["id"], "visibility": "private"}, {"id": owner},
    ))
    shares = _run(skill_store.list_skill_shares(skill["id"]))
    assert shares == []


def test_execute_user_ownership_gate(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    owner = _make_user(models_mod, name="Owner")
    intruder = _make_user(models_mod, name="Intruder")
    skill = _make_user_skill(skill_store, owner)
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "Hijacked"}, {"id": intruder},
        )

    with pytest.raises(RuntimeError, match="not its owner"):
        _run(_go())


def test_execute_user_share_only_ownership_gate(_isolated_db):
    # The share-only branch does an explicit ownership pre-check because
    # update_skill (the scalar gate) is not called.
    skill_store, _ps, _us, models_mod = _isolated_db
    owner = _make_user(models_mod, name="Owner")
    intruder = _make_user(models_mod, name="Intruder")
    skill = _make_user_skill(skill_store, owner, visibility="shared")
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "add_share_emails": ["x@y.co"]},
            {"id": intruder},
        )

    with pytest.raises(RuntimeError, match="not its owner"):
        _run(_go())


def test_execute_user_name_collision_guard(_isolated_db):
    # Execute-time guard: renaming to a name the creator already uses. User
    # skills enforce (creator_id, name) uniqueness via the DB unique index, so
    # update_skill raises an IntegrityError at commit; the request stays open
    # and the model sees the failure. (The pre-card check in skill_precard.py is
    # the primary, friendlier guard; this asserts the TOCTOU close still fails
    # rather than silently overwriting.)
    from sqlalchemy.exc import IntegrityError

    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    _run(skill_store.create_skill(creator_id=uid, name="Taken", content="c"))
    skill = _make_user_skill(skill_store, uid, name="Original")
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "Taken"}, {"id": uid},
        )

    with pytest.raises(IntegrityError):
        _run(_go())
    # The collision must not have overwritten the name.
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["name"] == "Original"


def test_execute_user_project_autoload_rejected(_isolated_db):
    # D8: project_autoload is invalid on a user skill.
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    skill = _make_user_skill(skill_store, uid)
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "project_autoload": True}, {"id": uid},
        )

    with pytest.raises(RuntimeError, match="only available for project skills"):
        _run(_go())


def test_execute_skill_not_found(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": "nonexistent", "name": "N"}, {"id": uid},
        )

    with pytest.raises(RuntimeError, match="Skill not found"):
        _run(_go())


# ---------------------------------------------------------------------------
# execute -- project skill
# ---------------------------------------------------------------------------


def _make_project_skill(skill_store, uid, project_id, name="Proj Skill", content="c"):
    return _run(skill_store.create_skill(
        creator_id=uid, name=name, content=content, project_id=project_id,
    ))


def test_execute_project_updates_scalar_fields(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "Renamed", "description": "d"},
            {"id": uid},
            project_id=project["id"],
        )

    result = _run(_go())
    assert result == {"success": True, "skill_id": skill["id"], "target": "project"}
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["name"] == "Renamed"
    assert persisted["description"] == "d"


def test_execute_project_content_edit(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(
        skill_store, uid, project["id"], content="alpha beta"
    )
    handler = _handler()

    _run(handler.execute(
        {"skill_id": skill["id"], "old_string": "beta", "new_string": "gamma"},
        {"id": uid},
        project_id=project["id"],
    ))
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["content"] == "alpha gamma"


def test_execute_project_autoload_enable_and_disable(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    # Enable (toggle-only: no scalar fields -> update_project_skill skipped).
    _run(handler.execute(
        {"skill_id": skill["id"], "project_autoload": True},
        {"id": uid},
        project_id=project["id"],
    ))
    assert skill["id"] in _run(skill_store.list_project_autoloaded_skill_ids(project["id"]))

    # Disable.
    _run(handler.execute(
        {"skill_id": skill["id"], "project_autoload": False},
        {"id": uid},
        project_id=project["id"],
    ))
    assert skill["id"] not in _run(skill_store.list_project_autoloaded_skill_ids(project["id"]))


def test_execute_project_combined_field_edit_and_autoload(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    _run(handler.execute(
        {"skill_id": skill["id"], "name": "Both", "project_autoload": True},
        {"id": uid},
        project_id=project["id"],
    ))
    persisted = _run(skill_store.get_skill(skill["id"]))
    assert persisted["name"] == "Both"
    assert skill["id"] in _run(skill_store.list_project_autoloaded_skill_ids(project["id"]))


def test_execute_project_rejects_visibility(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "visibility": "private"},
            {"id": uid},
            project_id=project["id"],
        )

    with pytest.raises(RuntimeError, match="not allowed for project skills"):
        _run(_go())


def test_execute_project_rejects_share_emails(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "add_share_emails": ["a@b.co"]},
            {"id": uid},
            project_id=project["id"],
        )

    with pytest.raises(RuntimeError, match="not allowed for project skills"):
        _run(_go())


def test_execute_project_access_gate_no_project_id(_isolated_db):
    # The turn is not in the skill's project -> access denied.
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "X"},
            {"id": uid},
            project_id=None,
        )

    with pytest.raises(RuntimeError, match="do not have access"):
        _run(_go())


def test_execute_project_mismatch_rejected(_isolated_db):
    # Turn's project_id differs from the skill's project_id.
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    other_project = _run(project_store.create_project(uid, "Other"))
    skill = _make_project_skill(skill_store, uid, project["id"])
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "X"},
            {"id": uid},
            project_id=other_project["id"],
        )

    with pytest.raises(RuntimeError, match="do not have access"):
        _run(_go())


def test_execute_project_unauthorized_user_rejected(_isolated_db):
    # A user who does not own the project cannot edit its skills, even when
    # supplying the matching project_id.
    skill_store, project_store, _us, models_mod = _isolated_db
    owner = _make_user(models_mod, name="Owner")
    intruder = _make_user(models_mod, name="Intruder")
    project = _run(project_store.create_project(owner, "P"))
    skill = _make_project_skill(skill_store, owner, project["id"])
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "X"},
            {"id": intruder},
            project_id=project["id"],
        )

    with pytest.raises(RuntimeError, match="do not have access"):
        _run(_go())


def test_execute_project_name_collision_guard(_isolated_db):
    # Renaming a project skill to a name already used in the same project.
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    _make_project_skill(skill_store, uid, project["id"], name="Taken")
    skill = _make_project_skill(skill_store, uid, project["id"], name="Original")
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"skill_id": skill["id"], "name": "Taken"},
            {"id": uid},
            project_id=project["id"],
        )

    with pytest.raises(RuntimeError, match="already exists in this project"):
        _run(_go())


def test_handler_metadata():
    from db.models import ActionRequestType
    handler = _handler()
    assert handler.type_name == ActionRequestType.EDIT_SKILL
    assert handler.display_name == "Edit Skill"
    assert handler.approve_label == "Save"
