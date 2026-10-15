"""Tests for the read-only skill inspection tools (devplan 00120).

Covers ``_handle_list_my_skills`` and ``_handle_get_skill``: categorization,
the ``exclude_*`` filters, inline autoload metadata across the user / project /
routine tiers, omission of skill bodies in the listing, and the security gating
on ``get_skill`` (access gate on the body, owner-only share roster). Also a
schema-wiring sanity check that both tools land in ``BASE_TOOLS`` and convert to
both provider formats.

All DB calls are monkeypatched -- no database is touched.
"""

import asyncio
import json
from unittest.mock import patch

from chat.gemini_api.tool_handlers import (
    _handle_list_my_skills,
    _handle_get_skill,
)
from chat.llm.tool_schemas import (
    BASE_TOOLS,
    to_gemini_declarations,
    to_anthropic_tools,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_async(value):
    """Return an async function that ignores its args and resolves to value."""
    async def _inner(*args, **kwargs):
        return value
    return _inner


# A mixed fixture: own, shared, and public skills. user_id under test is 1.
_OWN = {
    "id": "skill-own",
    "creator_id": 1,
    "creator_name": "Me",
    "creator_email": "me@example.com",
    "name": "My Skill",
    "description": "mine",
    "content": "own body",
    "visibility": "private",
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-02T00:00:00",
}
_SHARED = {
    "id": "skill-shared",
    "creator_id": 2,
    "creator_name": "Alice",
    "creator_email": "alice@example.com",
    "name": "Alice Skill",
    "description": "shared with me",
    "content": "shared body",
    "visibility": "shared",
    "created_at": None,
    "updated_at": None,
}
_PUBLIC = {
    "id": "skill-public",
    "creator_id": 3,
    "creator_name": "Bob",
    "creator_email": "bob@example.com",
    "name": "Bob Skill",
    "description": "public",
    "content": "public body",
    "visibility": "public",
    "created_at": None,
    "updated_at": None,
}
# A project-scoped skill: no creator, visibility 'project'.
_PROJECT = {
    "id": "skill-project",
    "creator_id": None,
    "creator_name": None,
    "creator_email": None,
    "name": "Project Skill",
    "description": "scoped to a project",
    "content": "project body",
    "visibility": "project",
    "created_at": None,
    "updated_at": None,
}


def _patch_autoload(user_ids=None, project_ids=None, routines=None):
    """Patch the autoload membership functions used by the resolver.

    routines: list of (routine_dict, [skill_id, ...]) tuples.
    """
    user_ids = user_ids or []
    project_ids = project_ids or []
    routines = routines or []

    async def fake_user(_uid):
        return list(user_ids)

    async def fake_project(_pid):
        return list(project_ids)

    routine_dicts = [r for r, _ in routines]
    routine_map = {r["id"]: ids for r, ids in routines}

    async def fake_list_routines(_pid):
        return routine_dicts

    async def fake_routine_ids(rid):
        return list(routine_map.get(rid, []))

    return [
        patch("db.skill_store.list_user_autoloaded_skill_ids", new=fake_user),
        patch("db.skill_store.list_project_autoloaded_skill_ids", new=fake_project),
        patch("db.skill_store.list_routine_autoloaded_skill_ids", new=fake_routine_ids),
        patch("db.routine_store.list_routines", new=fake_list_routines),
    ]


# ---------------------------------------------------------------------------
# list_my_skills
# ---------------------------------------------------------------------------

def test_list_my_skills_categorizes_all_three():
    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1))
        finally:
            for p in patches:
                p.stop()
    payload = json.loads(result)
    assert payload["skill_count"] == 3
    by_id = {s["id"]: s for s in payload["skills"]}
    assert by_id["skill-own"]["category"] == "own"
    assert by_id["skill-shared"]["category"] == "shared"
    assert by_id["skill-public"]["category"] == "public"
    # No content body in the listing.
    assert all("content" not in s for s in payload["skills"])
    # Creator info is surfaced.
    assert by_id["skill-shared"]["creator_name"] == "Alice"


def test_list_my_skills_exclude_own():
    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1, exclude_own=True))
        finally:
            for p in patches:
                p.stop()
    cats = {s["category"] for s in json.loads(result)["skills"]}
    assert cats == {"shared", "public"}


def test_list_my_skills_exclude_shared():
    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1, exclude_shared=True))
        finally:
            for p in patches:
                p.stop()
    cats = {s["category"] for s in json.loads(result)["skills"]}
    assert cats == {"own", "public"}


def test_list_my_skills_exclude_public():
    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1, exclude_public=True))
        finally:
            for p in patches:
                p.stop()
    cats = {s["category"] for s in json.loads(result)["skills"]}
    assert cats == {"own", "shared"}


def test_list_my_skills_inline_autoload_tiers(isolated_db):
    # isolated_db: list_project_skills(project_id) is not patched and reads
    # the (empty) store; without it the call reaches the real engine.
    # own -> user tier + a routine; public -> project tier.
    routines = [({"id": "r1", "name": "Daily digest"}, ["skill-own"])]
    patches = _patch_autoload(
        user_ids=["skill-own"],
        project_ids=["skill-public"],
        routines=routines,
    )
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1, project_id="proj-1"))
        finally:
            for p in patches:
                p.stop()
    by_id = {s["id"]: s for s in json.loads(result)["skills"]}
    assert by_id["skill-own"]["autoload"] == {
        "user": True, "project": False, "routines": ["Daily digest"],
    }
    assert by_id["skill-public"]["autoload"] == {
        "user": False, "project": True, "routines": [],
    }
    assert by_id["skill-shared"]["autoload"] == {
        "user": False, "project": False, "routines": [],
    }


def test_list_my_skills_error_path():
    async def boom(*a, **k):
        raise RuntimeError("db down")
    with patch("db.skill_store.list_accessible_skills", new=boom):
        result = _run(_handle_list_my_skills(user_id=1))
    assert "error" in json.loads(result)


def test_list_my_skills_includes_project_skills():
    # With a project_id, project-scoped skills join the listing under the
    # "project" category, with no creator info and no content body.
    patches = _patch_autoload(project_ids=["skill-project"])
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])), \
         patch("db.skill_store.list_project_skills",
               new=_fake_async([_PROJECT])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1, project_id="proj-1"))
        finally:
            for p in patches:
                p.stop()
    payload = json.loads(result)
    assert payload["skill_count"] == 4
    by_id = {s["id"]: s for s in payload["skills"]}
    assert by_id["skill-project"]["category"] == "project"
    assert by_id["skill-project"]["visibility"] == "project"
    assert by_id["skill-project"]["creator_name"] is None
    # No content body in the listing.
    assert all("content" not in s for s in payload["skills"])
    # Project-tier autoload metadata is resolved correctly.
    assert by_id["skill-project"]["autoload"] == {
        "user": False, "project": True, "routines": [],
    }


def test_list_my_skills_no_project_skills_without_project():
    # Without a project_id, list_project_skills must not be consulted and no
    # project skills appear.
    called = {"project": False}

    async def fake_project_skills(_pid):
        called["project"] = True
        return [_PROJECT]

    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])), \
         patch("db.skill_store.list_project_skills", new=fake_project_skills):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1))
        finally:
            for p in patches:
                p.stop()
    cats = {s["category"] for s in json.loads(result)["skills"]}
    assert "project" not in cats
    assert called["project"] is False


def test_list_my_skills_exclude_project():
    # exclude_project drops project-scoped skills (and skips the query).
    called = {"project": False}

    async def fake_project_skills(_pid):
        called["project"] = True
        return [_PROJECT]

    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _SHARED, _PUBLIC])), \
         patch("db.skill_store.list_project_skills", new=fake_project_skills):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(
                user_id=1, project_id="proj-1", exclude_project=True))
        finally:
            for p in patches:
                p.stop()
    cats = {s["category"] for s in json.loads(result)["skills"]}
    assert cats == {"own", "shared", "public"}
    assert called["project"] is False


def test_list_my_skills_dedups_project_skill_also_returned_elsewhere():
    # If the same skill id surfaces in both lists, it appears once.
    patches = _patch_autoload()
    with patch("db.skill_store.list_accessible_skills",
               new=_fake_async([_OWN, _PROJECT])), \
         patch("db.skill_store.list_project_skills",
               new=_fake_async([_PROJECT])):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_list_my_skills(user_id=1, project_id="proj-1"))
        finally:
            for p in patches:
                p.stop()
    ids = [s["id"] for s in json.loads(result)["skills"]]
    assert ids.count("skill-project") == 1


# ---------------------------------------------------------------------------
# get_skill
# ---------------------------------------------------------------------------

def test_get_skill_owner_gets_content_and_shares():
    shares = [
        {"user_id": 2, "email": "alice@example.com", "name": "Alice",
         "created_at": "2026-01-01T00:00:00"},
    ]
    patches = _patch_autoload(user_ids=["skill-own"])
    with patch("db.skill_store.user_can_access_skill", new=_fake_async(True)), \
         patch("db.skill_store.get_skill", new=_fake_async(_OWN)), \
         patch("db.skill_store.list_skill_shares", new=_fake_async(shares)):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_get_skill(user_id=1, skill_id="skill-own"))
        finally:
            for p in patches:
                p.stop()
    skill = json.loads(result)["skill"]
    assert skill["content"] == "own body"
    assert skill["is_owner"] is True
    assert skill["shares"] == [
        {"user_id": 2, "email": "alice@example.com", "name": "Alice"},
    ]
    assert skill["autoload"]["user"] is True


def test_get_skill_non_owner_with_access_no_shares():
    called = {"shares": False}

    async def fake_shares(_sid):
        called["shares"] = True
        return []

    patches = _patch_autoload()
    with patch("db.skill_store.user_can_access_skill", new=_fake_async(True)), \
         patch("db.skill_store.get_skill", new=_fake_async(_PUBLIC)), \
         patch("db.skill_store.list_skill_shares", new=fake_shares):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_get_skill(user_id=1, skill_id="skill-public"))
        finally:
            for p in patches:
                p.stop()
    skill = json.loads(result)["skill"]
    assert skill["content"] == "public body"
    assert skill["is_owner"] is False
    assert skill["shares"] is None
    # list_skill_shares must NOT be called for non-owners.
    assert called["shares"] is False


def test_get_skill_no_access_returns_not_found_and_skips_body():
    called = {"get": False, "shares": False}

    async def fake_get(_sid):
        called["get"] = True
        return _OWN

    async def fake_shares(_sid):
        called["shares"] = True
        return []

    with patch("db.skill_store.user_can_access_skill", new=_fake_async(False)), \
         patch("db.skill_store.get_skill", new=fake_get), \
         patch("db.skill_store.list_skill_shares", new=fake_shares):
        result = _run(_handle_get_skill(user_id=99, skill_id="skill-own"))
    payload = json.loads(result)
    assert payload == {"error": "Skill not found or not accessible."}
    # Body/shares never fetched once the access gate fails.
    assert called["get"] is False
    assert called["shares"] is False


def test_get_skill_project_scoped_accessible_via_project():
    # user_can_access_skill returns False (no creator/public/shared match),
    # but the skill belongs to the conversation's project, so access is
    # granted via get_project_skill.
    called = {"project_lookup": False, "shares": False}

    async def fake_project_lookup(_pid, _sid):
        called["project_lookup"] = True
        return _PROJECT

    async def fake_shares(_sid):
        called["shares"] = True
        return []

    patches = _patch_autoload(project_ids=["skill-project"])
    with patch("db.skill_store.user_can_access_skill", new=_fake_async(False)), \
         patch("db.skill_store.get_project_skill", new=fake_project_lookup), \
         patch("db.skill_store.get_skill", new=_fake_async(_PROJECT)), \
         patch("db.skill_store.list_skill_shares", new=fake_shares):
        for p in patches:
            p.start()
        try:
            result = _run(_handle_get_skill(
                user_id=1, skill_id="skill-project", project_id="proj-1"))
        finally:
            for p in patches:
                p.stop()
    skill = json.loads(result)["skill"]
    assert skill["content"] == "project body"
    assert skill["visibility"] == "project"
    assert skill["is_owner"] is False
    # Project skills have no creator and no per-user share roster.
    assert skill["shares"] is None
    assert called["shares"] is False
    assert called["project_lookup"] is True
    assert skill["autoload"]["project"] is True


def test_get_skill_project_scoped_wrong_project_denied():
    # A project skill from another project is not accessible.
    called = {"get": False}

    async def fake_get(_sid):
        called["get"] = True
        return _PROJECT

    with patch("db.skill_store.user_can_access_skill", new=_fake_async(False)), \
         patch("db.skill_store.get_project_skill", new=_fake_async(None)), \
         patch("db.skill_store.get_skill", new=fake_get), \
         patch("db.skill_store.list_skill_shares", new=_fake_async([])):
        result = _run(_handle_get_skill(
            user_id=1, skill_id="skill-project", project_id="other-proj"))
    payload = json.loads(result)
    assert payload == {"error": "Skill not found or not accessible."}
    assert called["get"] is False


def test_get_skill_empty_id():
    result = _run(_handle_get_skill(user_id=1, skill_id=""))
    assert "error" in json.loads(result)


# ---------------------------------------------------------------------------
# schema wiring sanity
# ---------------------------------------------------------------------------

def test_new_tools_declared_in_base_tools_and_converters():
    names = {t["name"] for t in BASE_TOOLS}
    assert "list_my_skills" in names
    assert "get_skill" in names

    gemini = to_gemini_declarations(BASE_TOOLS)
    anthropic = to_anthropic_tools(BASE_TOOLS)

    def _names(decls, key):
        out = set()
        for d in decls:
            out.add(d.get(key) if isinstance(d, dict) else getattr(d, key, None))
        return out

    # to_anthropic_tools yields dicts with "name".
    anthropic_names = {t["name"] for t in anthropic}
    assert {"list_my_skills", "get_skill"} <= anthropic_names

    # to_gemini_declarations output items expose a name (dict or object).
    gemini_names = set()
    for d in gemini:
        if isinstance(d, dict):
            gemini_names.add(d.get("name"))
        else:
            gemini_names.add(getattr(d, "name", None))
    assert {"list_my_skills", "get_skill"} <= gemini_names

    spec = next(t for t in BASE_TOOLS if t["name"] == "list_my_skills")
    props = spec["parameters"]["properties"]
    assert {"exclude_own", "exclude_shared", "exclude_public",
            "exclude_project"} <= set(props)
    assert all(props[k]["type"] == "boolean"
               for k in ("exclude_own", "exclude_shared", "exclude_public",
                         "exclude_project"))

    get_spec = next(t for t in BASE_TOOLS if t["name"] == "get_skill")
    assert get_spec["parameters"]["required"] == ["skill_id"]
