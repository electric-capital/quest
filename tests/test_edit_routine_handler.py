"""Tests for the edit_routine action_request handler and list_routines tool.

Covers ``EditRoutineHandler.validate_params`` (required routine_id,
empty-payload rejection, per-field validators, the model registry check,
the schedule spec validation, and the mutually-exclusive pairs),
``render_preview`` shape (injected current name / skill names),
``routine_precard_check`` (project scoping, routine existence, name
collision, skill access, token + prompt-diff capture), and ``execute`` (field updates
with the optimistic-concurrency TOCTOU close, schedule create / replace /
clear, and skill auto-load toggles) against an isolated DB. Also the
``list_routines`` read tool handler output shape and its schema wiring.
"""

import asyncio
import json
import uuid

import pytest

from chat.action_request_types.edit_routine import EditRoutineHandler


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(isolated_db):
    import db.project_store as project_store_mod
    import db.routine_store as routine_store_mod
    import db.schedule_store as schedule_store_mod
    import db.skill_store as skill_store_mod

    return {
        "routine_store": routine_store_mod,
        "schedule_store": schedule_store_mod,
        "skill_store": skill_store_mod,
        "project_store": project_store_mod,
        "models": isolated_db.models,
    }


def _make_user(models_mod, name="Routine Tester"):
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"ert-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name=name,
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _setup(stores):
    """Create a user, a project, and one routine. Returns (user, project, routine)."""
    user_id = _make_user(stores["models"])
    user = {"id": user_id, "email": "x@example.com"}
    project = _run(stores["project_store"].create_project(user_id, f"P-{uuid.uuid4().hex[:6]}"))
    routine = _run(stores["routine_store"].create_routine(
        user_id=user_id,
        project_id=project["id"],
        name="Daily Digest",
        prompt="Summarize the day.",
    ))
    return user, project, routine


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------

def test_validate_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unknown parameter"):
        EditRoutineHandler().validate_params({"routine_id": "r", "guide_id": "g"})


def test_validate_rejects_missing_routine_id():
    with pytest.raises(ValueError, match="routine_id"):
        EditRoutineHandler().validate_params({"name": "x"})


def test_validate_rejects_empty_payload():
    with pytest.raises(ValueError, match="No editable fields"):
        EditRoutineHandler().validate_params({"routine_id": "r"})


def test_validate_happy_path_scalars():
    out = EditRoutineHandler().validate_params(
        {"routine_id": " r1 ", "name": " New Name ", "prompt": "Do things."}
    )
    assert out == {"routine_id": "r1", "name": "New Name", "prompt": "Do things."}


def test_validate_rejects_empty_prompt():
    with pytest.raises(ValueError, match="prompt cannot be empty"):
        EditRoutineHandler().validate_params({"routine_id": "r", "prompt": "  "})


def test_validate_rejects_oversize_name():
    with pytest.raises(ValueError, match="maximum length"):
        EditRoutineHandler().validate_params({"routine_id": "r", "name": "x" * 101})


def test_validate_rejects_unknown_model():
    with pytest.raises(ValueError, match="Unknown model"):
        EditRoutineHandler().validate_params({"routine_id": "r", "model": "made-up"})


def test_validate_rejects_deprecated_model():
    from chat.llm.config import MODEL_REGISTRY
    deprecated = next(m for m, e in MODEL_REGISTRY.items() if e.get("deprecated"))
    with pytest.raises(ValueError, match="deprecated"):
        EditRoutineHandler().validate_params({"routine_id": "r", "model": deprecated})


def test_validate_accepts_registry_model():
    from chat.llm.config import MODEL_REGISTRY
    model = next(m for m, e in MODEL_REGISTRY.items() if not e.get("deprecated"))
    out = EditRoutineHandler().validate_params({"routine_id": "r", "model": model})
    assert out["model"] == model


def test_validate_rejects_model_with_clear_model():
    from chat.llm.config import MODEL_REGISTRY
    model = next(m for m, e in MODEL_REGISTRY.items() if not e.get("deprecated"))
    with pytest.raises(ValueError, match="mutually exclusive"):
        EditRoutineHandler().validate_params(
            {"routine_id": "r", "model": model, "clear_model": True}
        )


def test_validate_clear_model_false_is_not_an_edit():
    with pytest.raises(ValueError, match="No editable fields"):
        EditRoutineHandler().validate_params({"routine_id": "r", "clear_model": False})


def test_validate_schedule_daily():
    out = EditRoutineHandler().validate_params({
        "routine_id": "r",
        "schedule": {
            "schedule_type": "daily",
            "daily_time_local": "09:30",
            "timezone": "America/New_York",
        },
    })
    assert out["schedule"] == {
        "schedule_type": "daily",
        "daily_time_local": "09:30",
        "timezone": "America/New_York",
        "is_enabled": True,
    }


def test_validate_schedule_rejects_bad_timezone():
    with pytest.raises(ValueError, match="Invalid timezone"):
        EditRoutineHandler().validate_params({
            "routine_id": "r",
            "schedule": {
                "schedule_type": "daily",
                "daily_time_local": "09:30",
                "timezone": "Mars/Olympus_Mons",
            },
        })


def test_validate_schedule_rejects_bad_time():
    with pytest.raises(ValueError, match="Invalid time format"):
        EditRoutineHandler().validate_params({
            "routine_id": "r",
            "schedule": {
                "schedule_type": "daily",
                "daily_time_local": "9:30",
                "timezone": "UTC",
            },
        })


def test_validate_schedule_hourly_and_interval_bounds():
    handler = EditRoutineHandler()
    out = handler.validate_params({
        "routine_id": "r",
        "schedule": {"schedule_type": "hourly", "hourly_minute": 59, "is_enabled": False},
    })
    assert out["schedule"]["hourly_minute"] == 59
    assert out["schedule"]["is_enabled"] is False
    with pytest.raises(ValueError, match="hourly_minute"):
        handler.validate_params({
            "routine_id": "r",
            "schedule": {"schedule_type": "hourly", "hourly_minute": 60},
        })
    with pytest.raises(ValueError, match="interval_minutes"):
        handler.validate_params({
            "routine_id": "r",
            "schedule": {"schedule_type": "every_n_minutes", "interval_minutes": 0},
        })


def test_validate_schedule_rejects_unknown_schedule_key():
    with pytest.raises(ValueError, match="Unknown schedule key"):
        EditRoutineHandler().validate_params({
            "routine_id": "r",
            "schedule": {"schedule_type": "hourly", "hourly_minute": 5, "cron": "* *"},
        })


def test_validate_rejects_schedule_with_clear_schedule():
    with pytest.raises(ValueError, match="mutually exclusive"):
        EditRoutineHandler().validate_params({
            "routine_id": "r",
            "clear_schedule": True,
            "schedule": {"schedule_type": "hourly", "hourly_minute": 5},
        })


def test_validate_rejects_skill_id_overlap():
    with pytest.raises(ValueError, match="both add_skill_ids and remove_skill_ids"):
        EditRoutineHandler().validate_params({
            "routine_id": "r",
            "add_skill_ids": ["a", "b"],
            "remove_skill_ids": ["b"],
        })


def test_validate_rejects_empty_skill_id_list():
    with pytest.raises(ValueError, match="empty list"):
        EditRoutineHandler().validate_params({"routine_id": "r", "add_skill_ids": []})


# ---------------------------------------------------------------------------
# render_preview
# ---------------------------------------------------------------------------

def test_render_preview_lists_changed_fields():
    params = {
        "routine_id": "abcdef12-3456",
        "current_routine_name": "Daily Digest",
        "name": "Weekly Digest",
        "model": "claude-opus-4-8",
        "clear_schedule": True,
        "add_skill_ids": ["s1"],
        "skill_names": {"s1": "Report Style"},
    }
    fields = _run(EditRoutineHandler().render_preview(params))
    by_key = {}
    for f in fields:
        by_key.setdefault(f["key"], []).append(f["value"])
    assert by_key["Routine"] == ["Daily Digest"]
    assert by_key["Name"] == ["Weekly Digest"]
    assert by_key["Model"] == ["claude-opus-4-8"]
    assert by_key["Schedule"] == ["Remove schedule"]
    assert by_key["Auto-load skills"] == ["Report Style"]


def test_render_preview_falls_back_to_short_id():
    fields = _run(EditRoutineHandler().render_preview(
        {"routine_id": "abcdef12-3456", "prompt": "New prompt"}
    ))
    assert fields[0] == {"key": "Routine", "value": "Routine #abcdef12"}
    assert {"key": "Prompt", "value": "New prompt"} in fields


def test_render_preview_describes_daily_schedule():
    fields = _run(EditRoutineHandler().render_preview({
        "routine_id": "r",
        "schedule": {
            "schedule_type": "daily",
            "daily_time_local": "07:15",
            "timezone": "UTC",
            "is_enabled": False,
        },
    }))
    values = [f["value"] for f in fields if f["key"] == "Schedule"]
    assert values == ["Daily at 07:15 (UTC) (disabled)"]


# ---------------------------------------------------------------------------
# precard check
# ---------------------------------------------------------------------------

def test_precard_rejects_no_project(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    with pytest.raises(ValueError, match="project conversations"):
        _run(routine_precard_check(
            "edit_routine",
            {"routine_id": "r", "name": "x"}, {"id": 1}, None,
        ))


def test_precard_rejects_routine_in_other_project(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    other = _run(stores["project_store"].create_project(user["id"], "Other"))
    with pytest.raises(ValueError, match="Routine not found"):
        _run(routine_precard_check(
            "edit_routine",
            {"routine_id": routine["id"], "name": "x"}, user, other["id"],
        ))


def test_precard_rejects_name_collision(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    _run(stores["routine_store"].create_routine(
        user_id=user["id"], project_id=project["id"],
        name="Taken", prompt="p",
    ))
    with pytest.raises(ValueError, match="already exists"):
        _run(routine_precard_check(
            "edit_routine",
            {"routine_id": routine["id"], "name": "Taken"}, user, project["id"],
        ))


def test_precard_rejects_inaccessible_add_skill(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    with pytest.raises(ValueError, match="not accessible"):
        _run(routine_precard_check(
            "edit_routine",
            {"routine_id": routine["id"], "add_skill_ids": ["nope"]},
            user, project["id"],
        ))


def test_precard_injects_enrichment(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    skill = _run(stores["skill_store"].create_skill(
        creator_id=user["id"], name="Report Style", content="body",
    ))
    params = {"routine_id": routine["id"], "add_skill_ids": [skill["id"]]}
    _run(routine_precard_check("edit_routine", params, user, project["id"]))
    assert params["current_routine_name"] == "Daily Digest"
    assert params["expected_updated_at"] == routine["updated_at"]
    assert params["skill_names"] == {skill["id"]: "Report Style"}


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_execute_updates_fields(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    from chat.llm.config import MODEL_REGISTRY
    model = next(m for m, e in MODEL_REGISTRY.items() if not e.get("deprecated"))

    result = _run(EditRoutineHandler().execute(
        {
            "routine_id": routine["id"],
            "name": "Weekly Digest",
            "prompt": "Summarize the week.",
            "model": model,
            "expected_updated_at": routine["updated_at"],
        },
        user,
        project_id=project["id"],
    ))
    assert result["success"] is True

    updated = _run(stores["routine_store"].get_routine(user["id"], routine["id"]))
    assert updated["name"] == "Weekly Digest"
    assert updated["prompt"] == "Summarize the week."
    assert updated["model"] == model


def test_execute_clear_model(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    _run(stores["routine_store"].update_routine(
        user_id=user["id"], routine_id=routine["id"], model="claude-opus-4-8",
    ))
    _run(EditRoutineHandler().execute(
        {"routine_id": routine["id"], "clear_model": True},
        user, project_id=project["id"],
    ))
    updated = _run(stores["routine_store"].get_routine(user["id"], routine["id"]))
    assert updated["model"] is None


def test_execute_rejects_wrong_project(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    other = _run(stores["project_store"].create_project(user["id"], "Other"))
    with pytest.raises(RuntimeError, match="does not belong"):
        _run(EditRoutineHandler().execute(
            {"routine_id": routine["id"], "name": "x"},
            user, project_id=other["id"],
        ))


def test_execute_stale_token_fails_cleanly(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    # A concurrent edit lands after the card was proposed.
    _run(stores["routine_store"].update_routine(
        user_id=user["id"], routine_id=routine["id"], name="Renamed Meanwhile",
    ))
    with pytest.raises(RuntimeError, match="modified after the request"):
        _run(EditRoutineHandler().execute(
            {
                "routine_id": routine["id"],
                "name": "My Edit",
                "expected_updated_at": routine["updated_at"],
            },
            user, project_id=project["id"],
        ))
    current = _run(stores["routine_store"].get_routine(user["id"], routine["id"]))
    assert current["name"] == "Renamed Meanwhile"


def test_execute_creates_replaces_and_clears_schedule(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    handler = EditRoutineHandler()

    # Create (disabled) -- daily.
    _run(handler.execute(
        {
            "routine_id": routine["id"],
            "schedule": {
                "schedule_type": "daily",
                "daily_time_local": "09:30",
                "timezone": "UTC",
                "is_enabled": False,
            },
        },
        user, project_id=project["id"],
    ))
    sched = _run(stores["schedule_store"].get_schedule_for_routine(routine["id"]))
    assert sched["schedule_type"] == "daily"
    assert sched["daily_time_local"] == "09:30"
    assert sched["daily_time_utc"] == "09:30"  # UTC tz -> same wall time
    assert sched["is_enabled"] is False

    # Replace with hourly -- daily fields cleared, re-enabled.
    _run(handler.execute(
        {
            "routine_id": routine["id"],
            "schedule": {"schedule_type": "hourly", "hourly_minute": 15, "is_enabled": True},
        },
        user, project_id=project["id"],
    ))
    sched = _run(stores["schedule_store"].get_schedule_for_routine(routine["id"]))
    assert sched["schedule_type"] == "hourly"
    assert sched["hourly_minute"] == 15
    assert sched["daily_time_local"] is None
    assert sched["is_enabled"] is True

    # Clear.
    _run(handler.execute(
        {"routine_id": routine["id"], "clear_schedule": True},
        user, project_id=project["id"],
    ))
    assert _run(stores["schedule_store"].get_schedule_for_routine(routine["id"])) is None


def test_execute_toggles_skill_autoloads(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    skill = _run(stores["skill_store"].create_skill(
        creator_id=user["id"], name="Report Style", content="body",
    ))
    handler = EditRoutineHandler()

    _run(handler.execute(
        {"routine_id": routine["id"], "add_skill_ids": [skill["id"]]},
        user, project_id=project["id"],
    ))
    ids = _run(stores["skill_store"].list_routine_autoloaded_skill_ids(routine["id"]))
    assert ids == [skill["id"]]

    _run(handler.execute(
        {"routine_id": routine["id"], "remove_skill_ids": [skill["id"]]},
        user, project_id=project["id"],
    ))
    ids = _run(stores["skill_store"].list_routine_autoloaded_skill_ids(routine["id"]))
    assert ids == []


def test_execute_inaccessible_skill_blocks_all_changes(_isolated_db):
    stores = _isolated_db
    user, project, routine = _setup(stores)
    with pytest.raises(RuntimeError, match="not accessible"):
        _run(EditRoutineHandler().execute(
            {
                "routine_id": routine["id"],
                "name": "Should Not Apply",
                "add_skill_ids": ["nope"],
            },
            user, project_id=project["id"],
        ))
    current = _run(stores["routine_store"].get_routine(user["id"], routine["id"]))
    assert current["name"] == "Daily Digest"


# ---------------------------------------------------------------------------
# list_routines tool handler
# ---------------------------------------------------------------------------

def test_list_routines_requires_project():
    from chat.gemini_api.tool_handlers import _handle_list_routines
    result = json.loads(_run(_handle_list_routines(1, project_id=None)))
    assert "project conversations" in result["error"]


def test_list_routines_output_shape(_isolated_db):
    from chat.gemini_api.tool_handlers import _handle_list_routines
    stores = _isolated_db
    user, project, routine = _setup(stores)
    skill = _run(stores["skill_store"].create_skill(
        creator_id=user["id"], name="Report Style", content="body",
    ))
    _run(stores["skill_store"].set_routine_skill_autoload(routine["id"], skill["id"], True))
    _run(stores["schedule_store"].create_schedule(
        user_id=user["id"], routine_id=routine["id"],
        schedule_type="hourly", hourly_minute=5,
    ))

    result = json.loads(_run(_handle_list_routines(user["id"], project_id=project["id"])))
    assert result["routine_count"] == 1
    row = result["routines"][0]
    assert row["id"] == routine["id"]
    assert row["name"] == "Daily Digest"
    assert row["prompt"] == "Summarize the day."
    assert row["schedule"]["schedule_type"] == "hourly"
    assert row["autoloaded_skills"] == [{"id": skill["id"], "name": "Report Style"}]


def test_list_routines_schema_wiring():
    from chat.llm.tool_schemas import (
        BASE_TOOLS,
        to_gemini_declarations,
        to_anthropic_tools,
    )
    names = [t["name"] for t in BASE_TOOLS]
    assert "list_routines" in names
    # Both provider conversions accept the new spec.
    assert any(t["name"] == "list_routines" for t in to_anthropic_tools(BASE_TOOLS))
    gemini_names = set()
    for d in to_gemini_declarations(BASE_TOOLS):
        gemini_names.add(d.get("name") if isinstance(d, dict) else getattr(d, "name", None))
    assert "list_routines" in gemini_names


# ---------------------------------------------------------------------------
# prompt diff preview (precard injection + render_preview field)
# ---------------------------------------------------------------------------

def test_precard_injects_prompt_diff(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    params = {"routine_id": routine["id"], "prompt": "Summarize the week."}
    _run(routine_precard_check("edit_routine", params, user, project["id"]))
    diff = params["content_diff"]
    assert diff["removed"] == 1 and diff["added"] == 1
    texts = {(l["type"], l["text"]) for l in diff["lines"]}
    assert ("del", "Summarize the day.") in texts
    assert ("add", "Summarize the week.") in texts


def test_precard_skips_diff_for_identical_prompt(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    params = {"routine_id": routine["id"], "prompt": "Summarize the day."}
    _run(routine_precard_check("edit_routine", params, user, project["id"]))
    assert "content_diff" not in params


def test_render_preview_prompt_diff_field():
    diff = {
        "added": 1,
        "removed": 1,
        "lines": [
            {"type": "del", "old_line": 1, "new_line": None, "text": "old"},
            {"type": "add", "old_line": None, "new_line": 1, "text": "new"},
        ],
    }
    fields = _run(EditRoutineHandler().render_preview({
        "routine_id": "r",
        "prompt": "new",
        "content_diff": diff,
    }))
    prompt_fields = [f for f in fields if f["key"] == "Prompt"]
    assert prompt_fields == [{
        "key": "Prompt",
        "value": "+1 / -1 line(s)",
        "type": "skill_content_diff",
        "diff": diff,
    }]


# ---------------------------------------------------------------------------
# create_routine handler
# ---------------------------------------------------------------------------

def test_create_validate_requires_name_and_prompt():
    from chat.action_request_types.create_routine import CreateRoutineHandler
    with pytest.raises(ValueError, match="name"):
        CreateRoutineHandler().validate_params({"prompt": "p"})
    with pytest.raises(ValueError, match="prompt"):
        CreateRoutineHandler().validate_params({"name": "n"})
    with pytest.raises(ValueError, match="Unknown parameter"):
        CreateRoutineHandler().validate_params(
            {"name": "n", "prompt": "p", "guide_id": "g"}
        )


def test_create_validate_happy_path():
    from chat.action_request_types.create_routine import CreateRoutineHandler
    from chat.llm.config import MODEL_REGISTRY
    model = next(m for m, e in MODEL_REGISTRY.items() if not e.get("deprecated"))
    out = CreateRoutineHandler().validate_params({
        "name": " Digest ",
        "prompt": "Summarize.",
        "model": model,
        "schedule": {"schedule_type": "hourly", "hourly_minute": 5},
        "skill_ids": ["s1"],
    })
    assert out["name"] == "Digest"
    assert out["model"] == model
    assert out["schedule"]["hourly_minute"] == 5
    assert out["skill_ids"] == ["s1"]


def test_create_validate_rejects_deprecated_model():
    from chat.action_request_types.create_routine import CreateRoutineHandler
    from chat.llm.config import MODEL_REGISTRY
    deprecated = next(m for m, e in MODEL_REGISTRY.items() if e.get("deprecated"))
    with pytest.raises(ValueError, match="deprecated"):
        CreateRoutineHandler().validate_params(
            {"name": "n", "prompt": "p", "model": deprecated}
        )


def test_create_render_preview():
    from chat.action_request_types.create_routine import CreateRoutineHandler
    fields = _run(CreateRoutineHandler().render_preview({
        "name": "Digest",
        "prompt": "Summarize.",
        "schedule": {"schedule_type": "hourly", "hourly_minute": 5, "is_enabled": True},
        "skill_ids": ["s1"],
        "skill_names": {"s1": "Report Style"},
    }))
    by_key = {f["key"]: f["value"] for f in fields}
    assert by_key["Name"] == "Digest"
    assert by_key["Prompt"] == "Summarize."
    assert by_key["Schedule"] == "Hourly at minute 5"
    assert by_key["Auto-load skills"] == "Report Style"


def test_create_precard_rejects_no_project(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    with pytest.raises(ValueError, match="project conversations"):
        _run(routine_precard_check(
            "create_routine", {"name": "n", "prompt": "p"}, {"id": 1}, None,
        ))


def test_create_precard_rejects_name_collision(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    with pytest.raises(ValueError, match="already exists"):
        _run(routine_precard_check(
            "create_routine",
            {"name": "Daily Digest", "prompt": "p"}, user, project["id"],
        ))


def test_create_precard_rejects_public_project(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user_id = _make_user(stores["models"])
    user = {"id": user_id, "email": "x@example.com"}
    project = _run(stores["project_store"].create_project(user_id, "Pub", public=True))
    with pytest.raises(ValueError, match="managed from the UI"):
        _run(routine_precard_check(
            "create_routine", {"name": "n", "prompt": "p"}, user, project["id"],
        ))


def test_create_precard_injects_skill_names(_isolated_db):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    skill = _run(stores["skill_store"].create_skill(
        creator_id=user["id"], name="Report Style", content="body",
    ))
    params = {"name": "Fresh", "prompt": "p", "skill_ids": [skill["id"]]}
    _run(routine_precard_check("create_routine", params, user, project["id"]))
    assert params["skill_names"] == {skill["id"]: "Report Style"}


def test_create_execute_full(_isolated_db):
    from chat.action_request_types.create_routine import CreateRoutineHandler
    stores = _isolated_db
    user, project, routine = _setup(stores)
    skill = _run(stores["skill_store"].create_skill(
        creator_id=user["id"], name="Report Style", content="body",
    ))
    from chat.llm.config import MODEL_REGISTRY
    model = next(m for m, e in MODEL_REGISTRY.items() if not e.get("deprecated"))

    result = _run(CreateRoutineHandler().execute(
        {
            "name": "Weekly Digest",
            "prompt": "Summarize the week.",
            "model": model,
            "schedule": {
                "schedule_type": "daily",
                "daily_time_local": "08:00",
                "timezone": "UTC",
                "is_enabled": True,
            },
            "skill_ids": [skill["id"]],
        },
        user, project_id=project["id"],
    ))
    assert result["success"] is True
    created = _run(stores["routine_store"].get_routine(user["id"], result["routine_id"]))
    assert created["name"] == "Weekly Digest"
    assert created["model"] == model
    assert created["guide_id"] is None
    sched = _run(stores["schedule_store"].get_schedule_for_routine(result["routine_id"]))
    assert sched["schedule_type"] == "daily"
    assert sched["daily_time_utc"] == "08:00"
    assert sched["is_enabled"] is True
    ids = _run(stores["skill_store"].list_routine_autoloaded_skill_ids(result["routine_id"]))
    assert ids == [skill["id"]]


def test_create_execute_rejects_public_project(_isolated_db):
    from chat.action_request_types.create_routine import CreateRoutineHandler
    stores = _isolated_db
    user_id = _make_user(stores["models"])
    user = {"id": user_id, "email": "x@example.com"}
    project = _run(stores["project_store"].create_project(user_id, "Pub", public=True))
    with pytest.raises(RuntimeError, match="managed from the UI"):
        _run(CreateRoutineHandler().execute(
            {"name": "n", "prompt": "p"}, user, project_id=project["id"],
        ))


def test_create_execute_name_collision_toctou(_isolated_db):
    from chat.action_request_types.create_routine import CreateRoutineHandler
    stores = _isolated_db
    user, project, routine = _setup(stores)
    # Collision created after the (passing) precard, caught at execute.
    with pytest.raises(RuntimeError, match="already exists"):
        _run(CreateRoutineHandler().execute(
            {"name": "Daily Digest", "prompt": "p"},
            user, project_id=project["id"],
        ))


def test_create_execute_inaccessible_skill_blocks_creation(_isolated_db):
    from chat.action_request_types.create_routine import CreateRoutineHandler
    stores = _isolated_db
    user, project, routine = _setup(stores)
    with pytest.raises(RuntimeError, match="not accessible"):
        _run(CreateRoutineHandler().execute(
            {"name": "Fresh", "prompt": "p", "skill_ids": ["nope"]},
            user, project_id=project["id"],
        ))
    names = [r["name"] for r in _run(stores["routine_store"].list_routines(project["id"]))]
    assert "Fresh" not in names


# ---------------------------------------------------------------------------
# create_routine default-model injection
# ---------------------------------------------------------------------------

def _patch_available_models(monkeypatch, models):
    import chat.llm.config as llm_config
    monkeypatch.setattr(llm_config, "get_available_models", lambda: list(models))


def test_pick_default_routine_model_preference(monkeypatch):
    from chat.action_request_types._routine_validation import (
        pick_default_routine_model,
    )
    _patch_available_models(
        monkeypatch, ["gemini-3.7-flash", "claude-sonnet-5", "claude-opus-4-8"]
    )
    assert pick_default_routine_model() == "claude-sonnet-5"
    _patch_available_models(monkeypatch, ["claude-opus-4-8", "gemini-3.7-flash"])
    assert pick_default_routine_model() == "gemini-3.7-flash"
    _patch_available_models(monkeypatch, ["claude-opus-4-8"])
    assert pick_default_routine_model() is None


def test_create_precard_injects_default_model(_isolated_db, monkeypatch):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    _patch_available_models(monkeypatch, ["claude-sonnet-5", "gemini-3.7-flash"])
    params = {"name": "Fresh", "prompt": "p"}
    _run(routine_precard_check("create_routine", params, user, project["id"]))
    assert params["model"] == "claude-sonnet-5"
    assert params["model_defaulted"] is True


def test_create_precard_keeps_explicit_model(_isolated_db, monkeypatch):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    _patch_available_models(monkeypatch, ["claude-sonnet-5"])
    params = {"name": "Fresh", "prompt": "p", "model": "claude-opus-4-8"}
    _run(routine_precard_check("create_routine", params, user, project["id"]))
    assert params["model"] == "claude-opus-4-8"
    assert "model_defaulted" not in params


def test_create_precard_no_default_when_unavailable(_isolated_db, monkeypatch):
    from chat.action_request_types.routine_precard import routine_precard_check
    stores = _isolated_db
    user, project, routine = _setup(stores)
    _patch_available_models(monkeypatch, [])
    params = {"name": "Fresh", "prompt": "p"}
    _run(routine_precard_check("create_routine", params, user, project["id"]))
    assert "model" not in params
    assert "model_defaulted" not in params


def test_create_render_preview_marks_defaulted_model():
    from chat.action_request_types.create_routine import CreateRoutineHandler
    fields = _run(CreateRoutineHandler().render_preview({
        "name": "Digest",
        "prompt": "Summarize.",
        "model": "claude-sonnet-5",
        "model_defaulted": True,
    }))
    by_key = {f["key"]: f["value"] for f in fields}
    assert by_key["Model"] == "claude-sonnet-5 (default)"


def test_create_render_preview_explicit_model_unmarked():
    from chat.action_request_types.create_routine import CreateRoutineHandler
    fields = _run(CreateRoutineHandler().render_preview({
        "name": "Digest",
        "prompt": "Summarize.",
        "model": "claude-opus-4-8",
    }))
    by_key = {f["key"]: f["value"] for f in fields}
    assert by_key["Model"] == "claude-opus-4-8"
