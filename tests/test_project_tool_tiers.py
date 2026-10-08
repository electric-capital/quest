"""Tier membership and allowlists for the project-only dynamic tools.

The project database tool and the six project-workspace file / copy tools
(``PROJECT_ONLY_TOOL_CALL_TOOLS``) are offered only to conversations that
belong to a project: top-level, routine, sub-agent (incl. nested) and
public-project conversations. Cross-user subagent and inference-API runs
are standalone and never see them. The model is offered a dynamic tool
exactly when the prompt's Dynamic Tools section lists it (``tool_call`` has
no name enum), so the prompt builders' output is the source of truth here.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST
from chat.gemini_api.system_prompt import (
    get_inference_api_system_prompt,
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
    get_user_subagent_system_prompt,
)
from chat.llm.tool_schemas import (
    BASE_TOOLS,
    INFERENCE_API_TOOLS,
    PUBLIC_ROUTINE_TOOLS,
    ROUTINE_TOP_LEVEL_TOOLS,
    SLACK_TOP_LEVEL_TOOLS,
    SUB_AGENT_TOOLS_NESTED,
    TOP_LEVEL_TOOLS,
    USER_SUBAGENT_TOOLS,
    PROJECT_ONLY_TOOL_CALL_TOOLS,
    PUBLIC_TOOL_CALL_ALLOWLIST,
    TOOL_CALL_REGISTRY,
    _GMAIL_ATTACHMENTS_SCHEMA,
    _RETURN_TO_CALLER,
    mutating_tool_call_tools,
)

PROJECT_FILE_TOOLS = (
    "list_project_files",
    "get_project_file",
    "write_project_file",
    "edit_project_file",
    "copy_file_to_project",
    "copy_project_file",
)
ALL_PROJECT_ONLY = (*PROJECT_FILE_TOOLS, "project_db_query")

COPY_SENTENCE = (
    "In a project conversation, project files must be copied into this "
    "conversation's workspace first (`copy_project_file`)."
)


def _listed(prompt: str, name: str) -> bool:
    """Whether the Dynamic Tools section of *prompt* offers tool *name*."""
    return f"- **{name}** --" in prompt


def _assert_offered(prompt: str, offered: bool) -> None:
    for name in ALL_PROJECT_ONLY:
        assert _listed(prompt, name) is offered, name


# ---------------------------------------------------------------------------
# Registry specs
# ---------------------------------------------------------------------------

class TestRegistrySpecs:
    def test_project_only_set(self):
        assert PROJECT_ONLY_TOOL_CALL_TOOLS == frozenset(ALL_PROJECT_ONLY)
        assert PROJECT_ONLY_TOOL_CALL_TOOLS <= set(TOOL_CALL_REGISTRY)

    @pytest.mark.parametrize("name,props,required", [
        ("list_project_files", set(), []),
        ("get_project_file", {"path"}, ["path"]),
        ("write_project_file", {"path", "content"}, ["path", "content"]),
        (
            "edit_project_file",
            {"path", "old_string", "new_string", "replace_all"},
            ["path", "old_string", "new_string"],
        ),
        (
            "copy_file_to_project",
            {"path", "dest", "overwrite", "include_hidden"},
            ["path"],
        ),
        (
            "copy_project_file",
            {"path", "dest", "overwrite", "include_hidden"},
            ["path"],
        ),
    ])
    def test_spec_shape(self, name, props, required):
        spec = TOOL_CALL_REGISTRY[name]
        assert spec["name"] == name
        params = spec["parameters"]
        assert set(params["properties"]) - {"intent_message"} == props
        assert params["required"] == required
        # Not connector tools and not inference-API "mutating" tools.
        assert "requires_service" not in spec
        assert not spec.get("mutating")
        desc = spec["description"]
        assert "Only available in project conversations" in desc
        assert "project workspace" in desc
        if name.startswith("copy_"):
            assert "destination_exists" in desc
            assert "not_a_regular_file" in desc
            assert "include_hidden" in desc
            assert "'skipped'" in desc
            for p in ("overwrite", "include_hidden"):
                assert params["properties"][p]["type"] == "boolean"

    def test_edit_project_file_requires_prior_read(self):
        desc = TOOL_CALL_REGISTRY["edit_project_file"]["description"]
        assert "get_project_file" in desc
        assert "earlier in this conversation" in desc
        assert "another conversation of the project does not count" in desc

    def test_write_project_file_points_at_deliverables(self):
        desc = TOOL_CALL_REGISTRY["write_project_file"]["description"]
        assert "finished deliverables" in desc
        assert "1MB" in desc
        assert "copy_file_to_project" in desc

    def test_copy_dest_default_and_direction(self):
        to_proj = TOOL_CALL_REGISTRY["copy_file_to_project"]["parameters"]["properties"]
        from_proj = TOOL_CALL_REGISTRY["copy_project_file"]["parameters"]["properties"]
        assert "relative to this conversation's workspace" in to_proj["path"]["description"]
        assert "relative to the project workspace" in to_proj["dest"]["description"]
        assert "relative to the project workspace" in from_proj["path"]["description"]
        assert "relative to this conversation's workspace" in from_proj["dest"]["description"]
        for props in (to_proj, from_proj):
            assert "Defaults to the same relative path" in props["dest"]["description"]

    def test_edit_mirrors_edit_workspace_file(self):
        ws = TOOL_CALL_REGISTRY["edit_workspace_file"]["parameters"]
        pj = TOOL_CALL_REGISTRY["edit_project_file"]["parameters"]
        assert set(ws["properties"]) == set(pj["properties"])
        assert ws["required"] == pj["required"]

    def test_copy_to_project_mentions_scratch_refusal(self):
        desc = TOOL_CALL_REGISTRY["copy_file_to_project"]["description"]
        for scratch in (".responses/", ".subagent_responses/", "pasted/"):
            assert scratch in desc


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

class TestAllowlists:
    def test_public_allowlist_includes_project_tools(self):
        assert PROJECT_ONLY_TOOL_CALL_TOOLS <= PUBLIC_TOOL_CALL_ALLOWLIST

    def test_script_bridge_excludes_project_tools(self):
        assert not (PROJECT_ONLY_TOOL_CALL_TOOLS & SCRIPT_TOOL_CALL_ALLOWLIST)

    def test_not_mutating(self):
        assert not (set(PROJECT_FILE_TOOLS) & mutating_tool_call_tools())


# ---------------------------------------------------------------------------
# Prompt tiers
# ---------------------------------------------------------------------------

class TestTopLevelPrompt:
    @pytest.mark.parametrize("is_routine", [False, True])
    @pytest.mark.parametrize("has_project", [False, True])
    def test_offered_only_with_project(self, has_project, is_routine):
        p = get_system_prompt(
            "key", has_project=has_project, is_routine=is_routine,
        )
        _assert_offered(p, has_project)

    def test_slack_conversation_has_none(self):
        _assert_offered(get_system_prompt("key", is_slack=True), False)


class TestSubAgentPrompt:
    @pytest.mark.parametrize("can_nest", [False, True])
    @pytest.mark.parametrize("has_project", [False, True])
    def test_offered_only_with_project(self, has_project, can_nest):
        p = get_sub_agent_system_prompt(
            "Researcher", "key", has_project=has_project, can_nest=can_nest,
        )
        _assert_offered(p, has_project)


class TestPublicPrompt:
    @pytest.mark.parametrize("is_routine", [False, True])
    def test_offered(self, is_routine):
        p = get_public_project_system_prompt(
            user_email="ada@example.com", is_routine=is_routine,
        )
        _assert_offered(p, True)


class TestStandaloneRunKinds:
    def test_user_subagent_prompt_has_none(self):
        p = get_user_subagent_system_prompt(
            "key", target_user_email="t@example.com",
            caller_email="c@example.com",
        )
        _assert_offered(p, False)

    def test_inference_api_prompt_has_none(self):
        p = get_inference_api_system_prompt("", user_email="u@example.com")
        _assert_offered(p, False)


# ---------------------------------------------------------------------------
# Sub-agent wiring: _run_sub_agent derives has_project from project_id for
# 1st- and 2nd-level agents alike.
# ---------------------------------------------------------------------------

def _make_provider():
    provider = MagicMock()
    provider.create_session.return_value = MagicMock()

    async def _empty_stream(session, message):
        if False:
            yield None  # pragma: no cover

    provider.send_message_stream.side_effect = _empty_stream
    usage = MagicMock()
    usage.input_tokens = usage.output_tokens = usage.cached_tokens = 0
    usage.cache_creation_tokens = usage.cache_read_tokens = 0
    provider.get_usage.return_value = usage
    return provider


@pytest.mark.parametrize("level", [1, 2])
@pytest.mark.parametrize("project_id", [None, "proj-1"])
def test_run_sub_agent_passes_project(level, project_id):
    from chat.gemini_api.sub_agent import _run_sub_agent

    provider = _make_provider()
    user = {
        "id": 1, "email": "t@example.com", "name": "T",
        "api_key": "k", "settings": {},
    }
    with patch(
        "chat.gemini_api.sub_agent.record_api_call", new=AsyncMock()
    ), patch(
        "api.instructions.get_user_connected_services", return_value={}
    ), patch(
        "chat.gemini_api.sub_agent.compute_new_input_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent.compute_total_context_tokens", return_value=0
    ):
        asyncio.run(_run_sub_agent(
            app=MagicMock(), provider=provider, user=user,
            conversation_id="conv-1", timezone="UTC",
            model="claude-haiku-4.5", agent_name="A", prompt="go",
            project_id=project_id, nested_enabled=True, level=level,
        ))
    prompt = provider.create_session.call_args.kwargs["system_prompt"]
    _assert_offered(prompt, project_id is not None)


# ---------------------------------------------------------------------------
# Conversation-space path params carry the copy-first sentence
# ---------------------------------------------------------------------------

def _base_tool(name):
    return next(t for t in BASE_TOOLS if t["name"] == name)


def test_copy_sentence_on_conversation_path_params():
    run_script_path = _base_tool("run_script")["parameters"]["properties"]["path"]
    assert COPY_SENTENCE in run_script_path["description"]
    files = _RETURN_TO_CALLER["parameters"]["properties"]["files"]
    assert COPY_SENTENCE in files["description"]
    doc_image = TOOL_CALL_REGISTRY["add_doc_image"]["parameters"]["properties"]
    assert COPY_SENTENCE in doc_image["workspace_path"]["description"]
    gmail_ws = _GMAIL_ATTACHMENTS_SCHEMA["items"]["properties"]["workspace_path"]
    assert COPY_SENTENCE in gmail_ws["description"]


def _description_strings(node):
    """Yield every ``description`` string nested anywhere in *node*."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            elif key == "provider_descriptions" and isinstance(value, dict):
                yield from (v for v in value.values() if isinstance(v, str))
            else:
                yield from _description_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _description_strings(item)


def _core_specs():
    from chat.llm.tool_schemas import PLUGIN_TOOL_NAMES

    specs = [
        spec for name, spec in TOOL_CALL_REGISTRY.items()
        if name not in PLUGIN_TOOL_NAMES
    ]
    for tier in (
        BASE_TOOLS, TOP_LEVEL_TOOLS, SLACK_TOP_LEVEL_TOOLS,
        ROUTINE_TOP_LEVEL_TOOLS, SUB_AGENT_TOOLS_NESTED, USER_SUBAGENT_TOOLS,
        INFERENCE_API_TOOLS, PUBLIC_ROUTINE_TOOLS,
    ):
        specs.extend(tier)
    return specs


@pytest.mark.parametrize("stale", [
    "conversation/project workspace",
    "the conversation workspace directory",
    "Retrieve a file from the conversation workspace",
    "in the conversation workspace",
    "to the conversation workspace",
])
def test_no_stale_workspace_wording_in_core_descriptions(stale):
    hits = [
        d for d in _description_strings(_core_specs()) if stale in d
    ]
    assert not hits, hits
