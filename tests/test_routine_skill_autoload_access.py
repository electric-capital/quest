"""Routine skill auto-loads resolve by the skill access rule.

``get_routine_autoloaded_skills`` applies ``user_can_access_skill`` against
the routine's owner: a row resolves when the owner created the skill, the
skill is public, or it is shared with the owner. Same behaviour as the
project tier (tests/test_project_skill_autoload_access.py).
"""

import asyncio
import uuid

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(isolated_db):
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod
    import db.skill_store as skill_store_mod

    return skill_store_mod, project_store_mod, routine_store_mod, isolated_db.models


def _make_user(models_mod, label):
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"{label}-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name=label,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return {"id": u.id, "email": u.email, "name": u.name}

    return _run(_create())


def _routine(project_store, routine_store, user):
    project = _run(project_store.create_project(user["id"], "p", public=False))
    routine = _run(routine_store.create_routine(
        user["id"], project["id"], name="r", prompt="do it",
    ))
    return project, routine


def test_resolver_follows_share_and_visibility_changes(env):
    """Resolution tracks the skill's current sharing and visibility."""
    skill_store, project_store, routine_store, models_mod = env
    owner = _make_user(models_mod, "owner")
    member = _make_user(models_mod, "member")
    project, routine = _routine(project_store, routine_store, member)

    def resolve():
        return _run(skill_store.get_routine_autoloaded_skills(
            routine["id"], member["id"], project["id"],
        ))

    shared = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Shared", content="v1", visibility="shared",
    ))
    _run(skill_store.add_skill_shares(shared["id"], [member["id"]]))
    _run(skill_store.set_routine_skill_autoload(routine["id"], shared["id"], True))
    assert [s["id"] for s in resolve()] == [shared["id"]]

    # Share removed: the row stays, the skill no longer resolves.
    _run(skill_store.remove_skill_share(shared["id"], member["id"]))
    assert resolve() == []

    # Re-share, then the creator flips the skill private with new content.
    _run(skill_store.add_skill_shares(shared["id"], [member["id"]]))
    assert len(resolve()) == 1
    _run(skill_store.update_skill(
        creator_id=owner["id"], skill_id=shared["id"], content="v2", visibility="private",
    ))
    assert resolve() == []

    # Public skill made private also drops out.
    public = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Public", content="pub", visibility="public",
    ))
    _run(skill_store.set_routine_skill_autoload(routine["id"], public["id"], True))
    assert [s["id"] for s in resolve()] == [public["id"]]
    _run(skill_store.update_skill(creator_id=owner["id"], skill_id=public["id"], visibility="private"))
    assert resolve() == []


def test_resolver_keeps_own_public_and_shared_skills(env):
    skill_store, project_store, routine_store, models_mod = env
    owner = _make_user(models_mod, "owner")
    other = _make_user(models_mod, "other")
    project, routine = _routine(project_store, routine_store, owner)

    own_private = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Own", content="own", visibility="private",
    ))
    public = _run(skill_store.create_skill(
        creator_id=other["id"], name="Public", content="public", visibility="public",
    ))
    shared = _run(skill_store.create_skill(
        creator_id=other["id"], name="Shared", content="shared", visibility="shared",
    ))
    _run(skill_store.add_skill_shares(shared["id"], [owner["id"]]))

    for skill in (own_private, public, shared):
        _run(skill_store.set_routine_skill_autoload(routine["id"], skill["id"], True))

    resolved = _run(skill_store.get_routine_autoloaded_skills(
        routine["id"], owner["id"], project["id"],
    ))
    assert {s["id"] for s in resolved} == {
        own_private["id"], public["id"], shared["id"],
    }
