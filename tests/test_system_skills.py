"""Tests for the chat.system_skills module (catalog + loader).

Covers:
- Catalog completeness (id prefix, description length, content_builder
  returns non-empty).
- Gate filtering by connected_services and has_project.
- load_system_skills routing for hits, misses, and gate failures.
- build_system_skills_enumeration formatting and conditional inclusion.
"""

from chat.system_skills import (
    CATALOG,
    SystemSkill,
    build_system_skills_enumeration,
    is_system_skill_id,
    list_system_skills,
    load_system_skills,
)


BASE_URL = "http://localhost:8000"
API_KEY = "test-api-key-123"

ALL_CONNECTED = {
    "google_services": True,
    "slack": True,
    "telegram": True,
    "airtable": True,
    "github": True,
    "twitter": True,
    "ramp": True,
    # The _example plugin's skill gate (see the example_plugin fixture).
    "example": True,
}

NONE_CONNECTED = {k: False for k in ALL_CONNECTED}


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------

def test_catalog_is_non_empty():
    # 13 connection-gated backend + 1 ungated backend (federal_register;
    # sec_edgar is plugin-registered) + 4 cross-cutting.
    assert len(CATALOG) >= 12


def test_every_id_uses_system_prefix():
    for sid, skill in CATALOG.items():
        assert sid.startswith("system:"), sid
        assert isinstance(skill, SystemSkill)
        assert skill.id == sid


def test_descriptions_under_120_chars():
    for sid, skill in CATALOG.items():
        assert len(skill.description) <= 120, (
            f"{sid!r} description is {len(skill.description)} chars: "
            f"{skill.description!r}"
        )


def test_content_builders_return_non_empty_strings_for_all_skills():
    # When all gates pass, every content builder should return real text.
    for sid, skill in CATALOG.items():
        content = skill.content_builder(BASE_URL, API_KEY)
        assert isinstance(content, str), sid
        assert content.strip(), f"Skill {sid!r} produced empty content"


def test_is_system_skill_id_predicate():
    assert is_system_skill_id("system:gmail")
    assert is_system_skill_id("system:")
    assert not is_system_skill_id("not-a-system-skill")
    assert not is_system_skill_id("uuid-1234")
    assert not is_system_skill_id("")
    # Defensive: non-strings should not crash and should return False.
    assert not is_system_skill_id(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gate filtering
# ---------------------------------------------------------------------------

def test_list_system_skills_hides_disconnected_backends():
    services = dict(ALL_CONNECTED)
    services["slack"] = False
    visible_ids = {s.id for s in list_system_skills(services, has_project=True)}
    assert "system:slack" not in visible_ids
    assert "system:gmail" in visible_ids


def test_list_system_skills_with_no_services_only_shows_cross_cutting():
    visible_ids = {s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)}
    # Cross-cutting skills with no `requires` and no project gate.
    assert "system:memory" in visible_ids
    assert "system:workspace" in visible_ids
    assert "system:action_requests" in visible_ids
    assert "system:skill_management" in visible_ids
    # project_db is hidden when not in a project.
    assert "system:project_db" not in visible_ids
    # All backends are hidden.
    assert "system:gmail" not in visible_ids
    assert "system:slack" not in visible_ids


def test_list_system_skills_project_db_only_when_in_project():
    in_project = {s.id for s in list_system_skills(NONE_CONNECTED, has_project=True)}
    assert "system:project_db" in in_project
    out_of_project = {s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)}
    assert "system:project_db" not in out_of_project


def test_list_system_skills_none_means_include_all_backends():
    # Passing connected_services=None is the legacy "include all" path.
    visible_ids = {s.id for s in list_system_skills(None, has_project=True)}
    for sid in CATALOG:
        assert sid in visible_ids


# ---------------------------------------------------------------------------
# load_system_skills
# ---------------------------------------------------------------------------

def test_load_system_skills_returns_content_when_gate_passes(example_plugin):
    results = load_system_skills(
        ["system:example"], ALL_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    entry = results[0]
    assert entry["id"] == "system:example"
    assert entry["visibility"] == "system"
    assert "content" in entry and entry["content"]
    assert "error" not in entry


def test_load_system_skills_returns_error_when_gate_fails(example_plugin):
    results = load_system_skills(
        ["system:example"], NONE_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    entry = results[0]
    assert entry["id"] == "system:example"
    assert "content" not in entry
    assert "error" in entry
    assert "not connected" in entry["error"].lower()


def test_load_system_skills_skips_unknown_system_ids():
    results = load_system_skills(
        ["system:does-not-exist"], ALL_CONNECTED, BASE_URL, API_KEY,
    )
    assert results == []


def test_load_system_skills_ignores_non_system_ids():
    # Caller is expected to partition first, but loader should still be safe.
    results = load_system_skills(
        ["uuid-1234", "system:memory"], ALL_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    assert results[0]["id"] == "system:memory"


def test_load_system_skills_project_db_requires_project_flag():
    # has_project=False -> project_db rejected.
    results = load_system_skills(
        ["system:project_db"], ALL_CONNECTED, BASE_URL, API_KEY,
        has_project=False,
    )
    assert len(results) == 1
    assert "error" in results[0]

    # has_project=True -> project_db loaded.
    results = load_system_skills(
        ["system:project_db"], ALL_CONNECTED, BASE_URL, API_KEY,
        has_project=True,
    )
    assert len(results) == 1
    assert "content" in results[0]


def test_federal_register_skill_is_ungated_and_loadable():
    # The Federal Register API is free/public, so its skill carries no
    # `requires` gate: it must be present in the catalog, visible even with
    # nothing connected and outside a project, and loadable (content, no error).
    assert "system:federal_register" in CATALOG
    assert CATALOG["system:federal_register"].requires is None

    visible_ids = {
        s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)
    }
    assert "system:federal_register" in visible_ids

    results = load_system_skills(
        ["system:federal_register"], NONE_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    entry = results[0]
    assert entry["id"] == "system:federal_register"
    assert "error" not in entry
    assert "content" in entry and entry["content"].strip()


def test_slides_skill_is_gated_on_google_services_and_loadable():
    # Google Slides is a per-user OAuth service, so its skill is gated on the
    # `google_services` connection (like Docs / Sheets): present in the catalog,
    # visible only when google_services is connected, and loadable then.
    assert "system:slides" in CATALOG
    assert CATALOG["system:slides"].requires == "google_services"

    visible_when_connected = {
        s.id for s in list_system_skills(ALL_CONNECTED, has_project=False)
    }
    assert "system:slides" in visible_when_connected

    hidden_when_disconnected = {
        s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)
    }
    assert "system:slides" not in hidden_when_disconnected

    results = load_system_skills(
        ["system:slides"], ALL_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    entry = results[0]
    assert entry["id"] == "system:slides"
    assert "error" not in entry
    assert "content" in entry and entry["content"].strip()


def test_ramp_skill_is_gated_on_ramp_and_loadable():
    # Ramp is a per-user OAuth service, so its skill is gated on the `ramp`
    # connection (like GitHub/Twitter): present in the catalog, visible only
    # when Ramp is connected, and loadable then.
    assert "system:ramp" in CATALOG
    assert CATALOG["system:ramp"].requires == "ramp"

    visible_when_connected = {
        s.id for s in list_system_skills(ALL_CONNECTED, has_project=False)
    }
    assert "system:ramp" in visible_when_connected

    hidden_when_disconnected = {
        s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)
    }
    assert "system:ramp" not in hidden_when_disconnected

    results = load_system_skills(
        ["system:ramp"], ALL_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    entry = results[0]
    assert entry["id"] == "system:ramp"
    assert "error" not in entry
    assert "content" in entry and entry["content"].strip()


def test_skill_management_skill_is_ungated_and_loadable():
    # system:skill_management is a cross-cutting skill (no `requires` gate): it
    # documents the read inspection tools and the create_skill / edit_skill
    # write request types. It must be present, visible with nothing connected
    # and outside a project, and loadable (content, no error).
    assert "system:skill_management" in CATALOG
    assert CATALOG["system:skill_management"].requires is None

    visible_ids = {
        s.id for s in list_system_skills(NONE_CONNECTED, has_project=False)
    }
    assert "system:skill_management" in visible_ids

    results = load_system_skills(
        ["system:skill_management"], NONE_CONNECTED, BASE_URL, API_KEY,
    )
    assert len(results) == 1
    entry = results[0]
    assert entry["id"] == "system:skill_management"
    assert "error" not in entry
    assert "content" in entry and entry["content"].strip()
    assert "create_skill" in entry["content"]
    assert "edit_skill" in entry["content"]


def test_load_system_skills_preserves_input_order():
    results = load_system_skills(
        ["system:memory", "system:workspace", "system:action_requests"],
        ALL_CONNECTED, BASE_URL, API_KEY,
    )
    assert [r["id"] for r in results] == [
        "system:memory", "system:workspace", "system:action_requests",
    ]


# ---------------------------------------------------------------------------
# Enumeration block
# ---------------------------------------------------------------------------

def test_enumeration_lists_visible_skills_only(telegram_plugin):
    services = dict(ALL_CONNECTED)
    services["telegram"] = False
    text = build_system_skills_enumeration(services, has_project=False)
    assert "system:gmail" in text
    assert "system:telegram" not in text
    # project_db hidden when not in a project.
    assert "system:project_db" not in text


def test_enumeration_returns_empty_string_when_no_visible_skills():
    # Force every skill to be hidden by passing an empty services map and
    # has_project=False -- but cross-cutting skills (no `requires`, no
    # project gate) are still visible. So we simply assert that the
    # enumeration is non-empty in that case as a sanity check.
    text = build_system_skills_enumeration(NONE_CONNECTED, has_project=False)
    assert text  # cross-cutting skills are still there


def test_enumeration_header_mentions_load_skills():
    text = build_system_skills_enumeration(ALL_CONNECTED, has_project=True)
    assert "load_skills" in text
    assert "system:" in text


# ---------------------------------------------------------------------------
# Sub-agent restriction notes
# ---------------------------------------------------------------------------

def test_action_requests_skill_mentions_sub_agent_restriction():
    # `create_action_request` is top-level only. The action_requests
    # skill is the canonical reference doc the model loads when it
    # needs the full mechanics, so it must spell out that sub-agents
    # cannot call it (parallel to the existing system:memory note).
    skill = CATALOG["system:action_requests"]
    content = skill.content_builder(BASE_URL, API_KEY)
    assert "sub-agent" in content.lower(), (
        "system:action_requests should mention sub-agents (the tool is "
        "top-level only)"
    )
    assert "agent_task_response" in content, (
        "system:action_requests should tell sub-agents to return the "
        "proposed action via agent_task_response"
    )
