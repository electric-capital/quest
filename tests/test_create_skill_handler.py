"""Tests for the create_skill action_request handler.

Covers ``CreateSkillHandler.validate_params`` (happy path + each rejection),
``render_preview`` shape, and ``execute`` for both user and project targets
with an isolated DB.
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
                email=f"csk-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name=name,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _handler():
    from chat.action_request_types.create_skill import CreateSkillHandler
    return CreateSkillHandler()


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_happy_path_user_default():
    out = _handler().validate_params({"name": "  My Skill ", "content": "do X"})
    assert out["name"] == "My Skill"
    assert out["content"] == "do X"
    assert out["target"] == "user"
    assert out["visibility"] == "private"


def test_validate_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unknown parameter for create_skill"):
        _handler().validate_params({"name": "n", "content": "c", "bogus": 1})


def test_validate_rejects_missing_name():
    with pytest.raises(ValueError, match="name"):
        _handler().validate_params({"content": "c"})


def test_validate_rejects_missing_content():
    with pytest.raises(ValueError, match="content"):
        _handler().validate_params({"name": "n"})


def test_validate_rejects_empty_name():
    with pytest.raises(ValueError, match="empty"):
        _handler().validate_params({"name": "   ", "content": "c"})


def test_validate_rejects_oversize_content():
    from db.skill_store import MAX_SKILL_CONTENT_SIZE
    too_big = "x" * (MAX_SKILL_CONTENT_SIZE + 1)
    with pytest.raises(ValueError, match="maximum size"):
        _handler().validate_params({"name": "n", "content": too_big})


def test_validate_rejects_bad_visibility():
    with pytest.raises(ValueError, match="Invalid visibility"):
        _handler().validate_params({"name": "n", "content": "c", "visibility": "secret"})


def test_validate_rejects_project_visibility_for_user_target():
    with pytest.raises(ValueError, match="Invalid visibility"):
        _handler().validate_params({"name": "n", "content": "c", "visibility": "project"})


def test_validate_rejects_share_emails_without_shared():
    with pytest.raises(ValueError, match="only valid when visibility is 'shared'"):
        _handler().validate_params({
            "name": "n", "content": "c", "visibility": "private",
            "share_emails": ["a@b.co"],
        })


def test_validate_accepts_share_emails_when_shared():
    out = _handler().validate_params({
        "name": "n", "content": "c", "visibility": "shared",
        "share_emails": [" a@b.co "],
    })
    assert out["share_emails"] == ["a@b.co"]


def test_validate_rejects_bad_target():
    with pytest.raises(ValueError, match="Invalid target"):
        _handler().validate_params({"name": "n", "content": "c", "target": "global"})


def test_validate_rejects_visibility_for_project_target():
    with pytest.raises(ValueError, match="visibility is not allowed for project skills"):
        _handler().validate_params({
            "name": "n", "content": "c", "target": "project", "visibility": "private",
        })


def test_validate_rejects_share_emails_for_project_target():
    with pytest.raises(ValueError, match="share_emails is not allowed for project skills"):
        _handler().validate_params({
            "name": "n", "content": "c", "target": "project",
            "share_emails": ["a@b.co"],
        })


def test_render_preview_user_shape():
    handler = _handler()

    async def _go():
        return await handler.render_preview({
            "target": "user", "name": "N", "content": "C",
            "visibility": "shared", "description": "D",
            "share_emails": ["a@b.co"],
        })

    out = _run(_go())
    keys = [f["key"] for f in out]
    assert keys == ["Target", "Name", "Visibility", "Description", "Shared with", "Content"]
    assert out[0]["value"] == "User skill"


def test_render_preview_project_omits_visibility():
    handler = _handler()

    async def _go():
        return await handler.render_preview({
            "target": "project", "name": "N", "content": "C",
        })

    out = _run(_go())
    keys = [f["key"] for f in out]
    assert "Visibility" not in keys
    assert out[0]["value"] == "Project skill"


# ---------------------------------------------------------------------------
# execute -- user target
# ---------------------------------------------------------------------------


def test_execute_user_creates_skill(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "user", "name": "My Skill", "content": "do X",
             "visibility": "private"},
            {"id": uid},
        )

    result = _run(_go())
    assert result["success"] is True
    assert result["target"] == "user"
    persisted = _run(skill_store.get_skill(result["skill_id"]))
    assert persisted["name"] == "My Skill"
    assert persisted["creator_id"] == uid


def test_execute_user_shares_resolve(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    owner = _make_user(models_mod, name="Owner")
    friend_id = _make_user(models_mod, name="Friend")
    from db.engine import AsyncSessionLocal

    async def _friend_email():
        async with AsyncSessionLocal() as db:
            u = await db.get(models_mod.User, friend_id)
            return u.email

    friend_email = _run(_friend_email())
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "user", "name": "Shared Skill", "content": "c",
             "visibility": "shared", "share_emails": [friend_email, "nobody@x.co"]},
            {"id": owner},
        )

    result = _run(_go())
    assert result["shared_with"] == [friend_email]
    shares = _run(skill_store.list_skill_shares(result["skill_id"]))
    assert [s["user_id"] for s in shares] == [friend_id]


def test_execute_user_name_collision_raises(_isolated_db):
    skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    _run(skill_store.create_skill(creator_id=uid, name="Dup", content="c"))
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "user", "name": "Dup", "content": "c2"},
            {"id": uid},
        )

    with pytest.raises(RuntimeError, match="already exists for you"):
        _run(_go())


# ---------------------------------------------------------------------------
# execute -- project target
# ---------------------------------------------------------------------------


def test_execute_project_creates_skill(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "project", "name": "Proj Skill", "content": "c"},
            {"id": uid},
            project_id=project["id"],
        )

    result = _run(_go())
    assert result["target"] == "project"
    persisted = _run(skill_store.get_skill(result["skill_id"]))
    assert persisted["visibility"] == "project"
    assert persisted["creator_id"] is None


def test_execute_project_no_project_raises(_isolated_db):
    _skill_store, _ps, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "project", "name": "n", "content": "c"},
            {"id": uid},
            project_id=None,
        )

    with pytest.raises(RuntimeError, match="not in a project"):
        _run(_go())


def test_execute_project_unauthorized_raises(_isolated_db):
    _skill_store, project_store, _us, models_mod = _isolated_db
    owner = _make_user(models_mod)
    other = _make_user(models_mod)
    project = _run(project_store.create_project(owner, "P"))
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "project", "name": "n", "content": "c"},
            {"id": other},
            project_id=project["id"],
        )

    with pytest.raises(RuntimeError, match="not in a project you own"):
        _run(_go())


def test_execute_project_name_collision_raises(_isolated_db):
    skill_store, project_store, _us, models_mod = _isolated_db
    uid = _make_user(models_mod)
    project = _run(project_store.create_project(uid, "P"))
    _run(skill_store.create_skill(creator_id=uid, name="Dup", content="c",
                                  project_id=project["id"]))
    handler = _handler()

    async def _go():
        return await handler.execute(
            {"target": "project", "name": "Dup", "content": "c2"},
            {"id": uid},
            project_id=project["id"],
        )

    with pytest.raises(RuntimeError, match="already exists in this project"):
        _run(_go())


def test_handler_metadata():
    from db.models import ActionRequestType
    handler = _handler()
    assert handler.type_name == ActionRequestType.CREATE_SKILL
    assert handler.display_name == "Create Skill"
    assert handler.approve_label == "Create"
