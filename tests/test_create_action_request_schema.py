"""Tests asserting the create_action_request tool schema stays generic.

The per-type catalog (e.g. `send_slack_message`, `upload_to_drive`)
lives in each backend's `system:<name>` skill content, NOT in the
always-present create_action_request tool schema. This file guards
against regressions that would re-inline per-service details into the
tool description.
"""

from chat.llm.tool_schemas import TOP_LEVEL_TOOLS
from chat.system_skills import CATALOG


PER_SERVICE_TYPE_NAMES = [
    "send_slack_message",
    "send_slack_dm",
    "send_telegram_message",
    "send_twitter_dm",
    "github_trigger_workflow",
    "github_comment_on_issue",
    "github_set_issue_state",
    "create_calendar_invite",
    "edit_calendar_event",
    "upload_to_drive",
    "create_drive_folder",
    "create_skill",
    "edit_skill",
]


def _find_create_action_request_spec() -> dict:
    for spec in TOP_LEVEL_TOOLS:
        if spec["name"] == "create_action_request":
            return spec
    raise AssertionError("create_action_request tool not found in TOP_LEVEL_TOOLS")


def test_tool_description_is_generic():
    spec = _find_create_action_request_spec()
    desc = spec["description"]
    for name in PER_SERVICE_TYPE_NAMES:
        assert name not in desc, (
            f"create_action_request description must not list per-type "
            f"name {name!r} -- document it in the backend's system skill "
            f"instead. Description was: {desc!r}"
        )


def test_request_type_param_description_is_generic():
    spec = _find_create_action_request_spec()
    rtype_desc = spec["parameters"]["properties"]["request_type"]["description"]
    for name in PER_SERVICE_TYPE_NAMES:
        assert name not in rtype_desc, (
            f"request_type param description leaks per-type name {name!r}"
        )


def test_params_description_is_generic():
    spec = _find_create_action_request_spec()
    params_desc = spec["parameters"]["properties"]["params"]["description"]
    for name in PER_SERVICE_TYPE_NAMES:
        assert name not in params_desc, (
            f"params description leaks per-type name {name!r}"
        )


def test_description_points_to_backend_skills():
    spec = _find_create_action_request_spec()
    desc = spec["description"]
    assert "system:" in desc, (
        "create_action_request description should direct the agent to "
        "load the relevant `system:<backend>` skill"
    )


def test_description_calls_out_top_level_only_restriction():
    # The tool is not available inside sub-agents. The description must
    # say so explicitly so a model running as a sub-agent (which can
    # still see this tool documented in loaded skills / examples) does
    # not hallucinate a call to it.
    spec = _find_create_action_request_spec()
    desc = spec["description"]
    assert "top-level" in desc.lower(), (
        "create_action_request description should mention the "
        "top-level-only restriction"
    )
    assert "sub-agent" in desc.lower(), (
        "create_action_request description should mention sub-agents "
        "(so the model knows the tool is not available there)"
    )


def test_enum_still_lists_all_action_request_types(
    example_plugin, slack_plugin, twitter_plugin, telegram_plugin, github_plugin,
):
    # The enum is still the source of truth for valid request_type values;
    # it must not be trimmed even though the prose listing moved out.
    # Plugin-registered types (example_echo here; each real plugin's types
    # in its own suite) join the enum alongside the core types.
    spec = _find_create_action_request_spec()
    enum = set(spec["parameters"]["properties"]["request_type"]["enum"])
    for name in PER_SERVICE_TYPE_NAMES + ["example_echo"]:
        assert name in enum, f"enum missing {name}"


# ---------------------------------------------------------------------------
# Backend skills now carry the per-type catalog
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"
API_KEY = "test-key"


def _skill_content(skill_id: str) -> str:
    skill = CATALOG[skill_id]
    return skill.content_builder(BASE_URL, API_KEY)


def test_system_slack_documents_slack_sends(slack_plugin):
    content = _skill_content("system:slack")
    assert "send_slack_message" in content
    assert "send_slack_dm" in content


def test_system_telegram_documents_send(telegram_plugin):
    content = _skill_content("system:telegram")
    assert "send_telegram_message" in content


def test_system_calendar_documents_invite():
    content = _skill_content("system:calendar")
    assert "create_calendar_invite" in content
    assert "edit_calendar_event" in content
    assert "expected_updated" in content


def test_system_twitter_documents_dm(twitter_plugin):
    content = _skill_content("system:twitter")
    assert "send_twitter_dm" in content


def test_system_drive_documents_upload():
    content = _skill_content("system:drive")
    assert "upload_to_drive" in content


def test_system_drive_documents_create_folder():
    content = _skill_content("system:drive")
    assert "create_drive_folder" in content


def test_system_skill_management_documents_create_and_edit():
    content = _skill_content("system:skill_management")
    assert "create_skill" in content
    assert "edit_skill" in content
    # The read inspection tools are co-located in the same skill.
    assert "list_my_skills" in content
    assert "get_skill" in content
