"""Project skill auto-load must respect skill access (finding #279220).

Covers the PUT /projects/{id}/skills/{skill_id}/autoload route guard (only
project-local skills or skills the caller can currently access may be
enabled; disabling never needs access) and the conversation-time resolver
``get_project_autoloaded_skills`` re-checking access against the project
owner so a stale autoload row stops resolving after a share is revoked or
the skill goes private.
"""

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(isolated_db):
    import chat.project_skill_routes as routes_mod
    import db.project_store as project_store_mod
    import db.skill_store as skill_store_mod

    return skill_store_mod, project_store_mod, isolated_db.models, routes_mod


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


def _client_as(routes_mod, user):
    app = FastAPI()
    app.include_router(routes_mod.router)

    async def _current():
        return user

    app.dependency_overrides[routes_mod.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def _autoload_url(project_id, skill_id):
    return f"/app/api/projects/{project_id}/skills/{skill_id}/autoload"


def test_route_rejects_foreign_private_skill(env):
    skill_store, project_store, models_mod, routes_mod = env
    owner = _make_user(models_mod, "owner")
    attacker = _make_user(models_mod, "attacker")

    project = _run(project_store.create_project(attacker["id"], "p", public=False))
    secret = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Secret", content="SECRET BODY", visibility="private",
    ))

    resp = _client_as(routes_mod, attacker).put(
        _autoload_url(project["id"], secret["id"]), json={"enabled": True},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"
    assert _run(skill_store.list_project_autoloaded_skill_ids(project["id"])) == []


def test_route_rejects_unknown_and_other_projects_local_skill(env):
    skill_store, project_store, models_mod, routes_mod = env
    owner = _make_user(models_mod, "owner")
    attacker = _make_user(models_mod, "attacker")

    owner_project = _run(project_store.create_project(owner["id"], "op", public=False))
    owner_local = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Local", content="local body", project_id=owner_project["id"],
    ))
    attacker_project = _run(project_store.create_project(attacker["id"], "ap", public=False))

    client = _client_as(routes_mod, attacker)
    resp = client.put(_autoload_url(attacker_project["id"], owner_local["id"]), json={"enabled": True})
    assert resp.status_code == 404
    resp = client.put(_autoload_url(attacker_project["id"], str(uuid.uuid4())), json={"enabled": True})
    assert resp.status_code == 404
    assert _run(skill_store.list_project_autoloaded_skill_ids(attacker_project["id"])) == []


def test_route_allows_project_local_own_public_and_shared_skills(env):
    skill_store, project_store, models_mod, routes_mod = env
    owner = _make_user(models_mod, "owner")
    other = _make_user(models_mod, "other")

    project = _run(project_store.create_project(owner["id"], "p", public=False))
    local = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Local", content="local", project_id=project["id"],
    ))
    own = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Own", content="own", visibility="private",
    ))
    public = _run(skill_store.create_skill(
        creator_id=other["id"], name="Public", content="public", visibility="public",
    ))
    shared = _run(skill_store.create_skill(
        creator_id=other["id"], name="Shared", content="shared", visibility="shared",
    ))
    _run(skill_store.add_skill_shares(shared["id"], [owner["id"]]))

    client = _client_as(routes_mod, owner)
    for skill in (local, own, public, shared):
        resp = client.put(_autoload_url(project["id"], skill["id"]), json={"enabled": True})
        assert resp.status_code == 200, (skill["name"], resp.text)

    ids = set(_run(skill_store.list_project_autoloaded_skill_ids(project["id"])))
    assert ids == {local["id"], own["id"], public["id"], shared["id"]}

    resolved = {s["id"]: s for s in _run(skill_store.get_project_autoloaded_skills(project["id"]))}
    assert set(resolved) == ids
    assert resolved[shared["id"]]["content"] == "shared"


def test_route_disable_never_needs_access(env):
    """Disabling a stale row must work even after access was revoked."""
    skill_store, project_store, models_mod, routes_mod = env
    owner = _make_user(models_mod, "owner")
    member = _make_user(models_mod, "member")

    project = _run(project_store.create_project(member["id"], "p", public=False))
    shared = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Shared", content="v1", visibility="shared",
    ))
    _run(skill_store.add_skill_shares(shared["id"], [member["id"]]))
    _run(skill_store.set_project_skill_autoload(project["id"], shared["id"], True))
    _run(skill_store.remove_skill_share(shared["id"], member["id"]))

    resp = _client_as(routes_mod, member).put(
        _autoload_url(project["id"], shared["id"]), json={"enabled": False},
    )
    assert resp.status_code == 200
    assert _run(skill_store.list_project_autoloaded_skill_ids(project["id"])) == []


def test_resolver_drops_revoked_and_private_skills(env):
    """A stale autoload row must not keep leaking the current skill body."""
    skill_store, project_store, models_mod, _routes_mod = env
    owner = _make_user(models_mod, "owner")
    member = _make_user(models_mod, "member")

    project = _run(project_store.create_project(member["id"], "p", public=False))
    shared = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Shared", content="v1", visibility="shared",
    ))
    _run(skill_store.add_skill_shares(shared["id"], [member["id"]]))
    _run(skill_store.set_project_skill_autoload(project["id"], shared["id"], True))
    assert [s["id"] for s in _run(skill_store.get_project_autoloaded_skills(project["id"]))] == [shared["id"]]

    # Share revoked: row stays, resolution stops.
    _run(skill_store.remove_skill_share(shared["id"], member["id"]))
    assert _run(skill_store.get_project_autoloaded_skills(project["id"])) == []

    # Re-share, then the creator flips the skill private with new content.
    _run(skill_store.add_skill_shares(shared["id"], [member["id"]]))
    assert len(_run(skill_store.get_project_autoloaded_skills(project["id"]))) == 1
    _run(skill_store.update_skill(
        creator_id=owner["id"], skill_id=shared["id"], content="NEW SECRET", visibility="private",
    ))
    assert _run(skill_store.get_project_autoloaded_skills(project["id"])) == []

    # Public skill made private also drops out.
    public = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Public", content="pub", visibility="public",
    ))
    _run(skill_store.set_project_skill_autoload(project["id"], public["id"], True))
    assert [s["id"] for s in _run(skill_store.get_project_autoloaded_skills(project["id"]))] == [public["id"]]
    _run(skill_store.update_skill(creator_id=owner["id"], skill_id=public["id"], visibility="private"))
    assert _run(skill_store.get_project_autoloaded_skills(project["id"])) == []


def test_resolver_ignores_other_projects_local_skill(env):
    skill_store, project_store, models_mod, _routes_mod = env
    owner = _make_user(models_mod, "owner")
    attacker = _make_user(models_mod, "attacker")

    owner_project = _run(project_store.create_project(owner["id"], "op", public=False))
    owner_local = _run(skill_store.create_skill(
        creator_id=owner["id"], name="Local", content="local body", project_id=owner_project["id"],
    ))
    attacker_project = _run(project_store.create_project(attacker["id"], "ap", public=False))
    # Simulate a row written before the route guard existed.
    _run(skill_store.set_project_skill_autoload(attacker_project["id"], owner_local["id"], True))

    assert _run(skill_store.get_project_autoloaded_skills(attacker_project["id"])) == []
    # The owning project still resolves its own local skill.
    _run(skill_store.set_project_skill_autoload(owner_project["id"], owner_local["id"], True))
    assert [s["id"] for s in _run(skill_store.get_project_autoloaded_skills(owner_project["id"]))] == [owner_local["id"]]
