"""Prompt tiers for the ``tool_call`` tools (chat/llm/tool_schemas.py).

Every registry tool is either core -- documented in full in every prompt's
Dynamic Tools section -- or skill-documented: named on the enumeration line
of the system skill that documents it, with the full parameter reference
appended to that skill's loaded content. The public-project prompt has no
skill tools and documents everything it offers in full.
"""

import pytest

from chat.gemini_api.system_prompt import (
    _build_dynamic_tools_section,
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
)
from chat.llm.tool_schemas import (
    CORE_TOOL_CALL_TOOLS,
    PROJECT_HIDDEN_WORKSPACE_TOOLS,
    PROJECT_ONLY_TOOL_CALL_TOOLS,
    PUBLIC_ONLY_TOOL_CALL_TOOLS,
    PUBLIC_TOOL_CALL_ALLOWLIST,
    TOOL_CALL_REGISTRY,
    format_tool_call_doc,
    render_tool_reference,
    tools_for_skill,
)
from chat.system_skills import (
    CATALOG,
    build_system_skills_enumeration,
    load_system_skills,
)
from tests.prompt_support import core_bullet, named_under_skill, skill_tools, tool_offered

GMAIL_TOOLS = {
    "get_gmail_messages", "get_gmail_message_urls", "create_gmail_draft",
    "send_gmail_to_self", "modify_gmail_labels", "archive_gmail_message",
    "list_gmail_labels", "list_gmail_quest_labels",
}


class TestRegistryTiers:
    def test_every_tool_has_exactly_one_tier(self):
        for name, spec in TOOL_CALL_REGISTRY.items():
            tiers = [
                bool(spec.get("core")), bool(spec.get("skill")),
                bool(spec.get("public_only")),
            ]
            assert tiers.count(True) == 1, name
            assert (name in CORE_TOOL_CALL_TOOLS) is bool(spec.get("core")), name
            assert (name in PUBLIC_ONLY_TOOL_CALL_TOOLS) is bool(spec.get("public_only")), name

    def test_public_only_roster(self):
        # The browser tools, and nothing else: offered by the public prompt
        # alone (gated on the browser feature), never by a regular prompt.
        assert PUBLIC_ONLY_TOOL_CALL_TOOLS
        assert all(name.startswith("browser_") for name in PUBLIC_ONLY_TOOL_CALL_TOOLS)
        assert PUBLIC_ONLY_TOOL_CALL_TOOLS <= PUBLIC_TOOL_CALL_ALLOWLIST

    def test_every_documenting_skill_exists(self):
        for name, spec in TOOL_CALL_REGISTRY.items():
            if spec.get("skill"):
                assert spec["skill"] in CATALOG, (name, spec["skill"])

    def test_core_roster(self):
        assert "authed_get" in CORE_TOOL_CALL_TOOLS
        assert "memory_search" in CORE_TOOL_CALL_TOOLS
        for name in (
            "authed_post", "wait_for_handles", "project_db_query", "list_docs",
            *GMAIL_TOOLS,
        ):
            assert name not in CORE_TOOL_CALL_TOOLS, name

    def test_tools_for_skill(self):
        assert set(tools_for_skill("system:gmail")) == GMAIL_TOOLS
        assert tools_for_skill("system:gcp") == ["authed_post"]
        assert tools_for_skill("system:calendar") == []


class TestDynamicToolsSection:
    def test_core_only_by_default(self):
        section = _build_dynamic_tools_section(connected_services=None)
        for name, spec in TOOL_CALL_REGISTRY.items():
            assert (core_bullet(name) in section) is bool(spec.get("core")), name
        assert "System Skill" in section

    def test_full_mode_documents_every_tool(self):
        section = _build_dynamic_tools_section(connected_services=None, core_only=False)
        for name in TOOL_CALL_REGISTRY:
            assert core_bullet(name) in section, name
        assert "System Skill" not in section

    def test_exclude_still_applies(self):
        section = _build_dynamic_tools_section(exclude={"authed_get"}, connected_services={})
        assert core_bullet("authed_get") not in section
        assert core_bullet("memory_search") in section


class TestEnumeration:
    def test_lines_name_the_skills_tools(self):
        block = build_system_skills_enumeration(connected_services=None, has_project=True)
        assert set(skill_tools(block, "system:gmail")) == GMAIL_TOOLS
        assert skill_tools(block, "system:project_db") == ["project_db_query"]
        assert skill_tools(block, "system:calendar") == []
        assert "Tool reference" in block

    def test_exclude_tools_drops_names(self):
        block = build_system_skills_enumeration(
            connected_services=None, exclude_tools={"wait_for_handles"},
        )
        assert skill_tools(block, "system:action_requests") == []
        assert "wait_for_handles" not in block

    def test_skill_gate_hides_its_tools(self):
        block = build_system_skills_enumeration(connected_services={})
        assert skill_tools(block, "system:gmail") is None
        assert "get_gmail_messages" not in block


class TestSkillContent:
    def test_reference_rendered_from_registry(self):
        reference = render_tool_reference("system:gmail")
        assert reference.startswith("## Tool reference")
        for name in GMAIL_TOOLS:
            assert format_tool_call_doc(name, TOOL_CALL_REGISTRY[name]) in reference
        assert render_tool_reference("system:calendar") == ""

    def test_loaded_skill_ends_with_the_reference(self):
        [gmail] = load_system_skills(["system:gmail"], None, "http://x", "k")
        content = gmail["content"]
        assert content.rstrip().endswith(render_tool_reference("system:gmail").rstrip())
        assert content.index("## Tool reference") > 0
        [calendar] = load_system_skills(["system:calendar"], None, "http://x", "k")
        assert "## Tool reference" not in calendar["content"]


class TestPrompts:
    @pytest.mark.parametrize("has_project", [False, True])
    def test_top_level_offers_every_tool_in_its_tier(self, has_project):
        prompt = get_system_prompt("k", connected_services=None, has_project=has_project)
        hidden = PROJECT_HIDDEN_WORKSPACE_TOOLS if has_project else PROJECT_ONLY_TOOL_CALL_TOOLS
        for name, spec in TOOL_CALL_REGISTRY.items():
            if name in hidden:
                assert not tool_offered(prompt, name), name
            elif spec.get("core"):
                assert core_bullet(name) in prompt, name
            elif spec.get("public_only"):
                assert not tool_offered(prompt, name), name
            else:
                assert core_bullet(name) not in prompt, name
                assert named_under_skill(prompt, spec["skill"], name), name

    def test_sub_agent_names_skill_tools_minus_its_excludes(self):
        prompt = get_sub_agent_system_prompt("helper", "k", connected_services=None)
        assert not tool_offered(prompt, "wait_for_handles")
        assert named_under_skill(prompt, "system:gmail", "get_gmail_messages")
        assert core_bullet("get_gmail_messages") not in prompt

    def test_public_prompt_inlines_every_offered_tool(self):
        prompt = get_public_project_system_prompt(
            docs_enabled=True, tables_enabled=True, browser_enabled=True,
        )
        for name in PUBLIC_TOOL_CALL_ALLOWLIST - PROJECT_HIDDEN_WORKSPACE_TOOLS:
            if name not in TOOL_CALL_REGISTRY:
                continue  # a plugin tool, present only when its plugin is loaded
            assert core_bullet(name) in prompt, name
        assert "load_skills" not in prompt

    def test_fully_connected_prompt_stays_compact(self):
        # The point of the tiers: the always-on prompt no longer grows with
        # every documented tool (it was ~112K chars with everything connected).
        prompt = get_system_prompt("k", connected_services=None, has_project=True)
        assert len(prompt) < 70_000
