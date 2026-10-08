"""Tier membership and allowlists for the project-only dynamic tools.

The project database tool and the five scheme-qualified file tools
(``PROJECT_ONLY_TOOL_CALL_TOOLS``: list_files / read_file / write_file /
edit_file / copy_file over ``chat://`` and ``proj://``) are offered only to
conversations that belong to a project: top-level, routine, sub-agent (incl.
nested) and public-project conversations, whose prompts in turn hide the
four ``*_workspace_file`` tools (``PROJECT_HIDDEN_WORKSPACE_TOOLS``).
Standalone conversations -- and cross-user subagent and inference-API runs,
which are always standalone -- keep the four workspace tools and never see
the five. The model is offered a dynamic tool
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
    PROJECT_HIDDEN_WORKSPACE_TOOLS,
    PROJECT_ONLY_TOOL_CALL_TOOLS,
    PUBLIC_TOOL_CALL_ALLOWLIST,
    TOOL_CALL_REGISTRY,
    _GMAIL_ATTACHMENTS_SCHEMA,
    _RETURN_TO_CALLER,
    mutating_tool_call_tools,
)

FILE_TOOLS = ("list_files", "read_file", "write_file", "edit_file", "copy_file")
ALL_PROJECT_ONLY = (*FILE_TOOLS, "project_db_query")
WORKSPACE_TOOLS = (
    "list_workspace_files",
    "get_workspace_file",
    "write_workspace_file",
    "edit_workspace_file",
)

from chat.workspace_hints import PROJECT_COPY_FIRST_SENTENCE as COPY_SENTENCE


def _listed(prompt: str, name: str) -> bool:
    """Whether the Dynamic Tools section of *prompt* offers tool *name*."""
    return f"- **{name}** --" in prompt


def _assert_offered(prompt: str, project: bool) -> None:
    """Project presentation: the five scheme-qualified file tools and
    project_db_query, and none of the four *_workspace_file tools.
    Standalone presentation: exactly the reverse."""
    for name in ALL_PROJECT_ONLY:
        assert _listed(prompt, name) is project, name
    for name in WORKSPACE_TOOLS:
        assert _listed(prompt, name) is (not project), name


# ---------------------------------------------------------------------------
# Registry specs
# ---------------------------------------------------------------------------

class TestRegistrySpecs:
    def test_project_only_set(self):
        assert PROJECT_ONLY_TOOL_CALL_TOOLS == frozenset(ALL_PROJECT_ONLY)
        assert PROJECT_ONLY_TOOL_CALL_TOOLS <= set(TOOL_CALL_REGISTRY)

    def test_hidden_workspace_set(self):
        assert PROJECT_HIDDEN_WORKSPACE_TOOLS == frozenset(WORKSPACE_TOOLS)
        assert PROJECT_HIDDEN_WORKSPACE_TOOLS <= set(TOOL_CALL_REGISTRY)
        assert not (PROJECT_HIDDEN_WORKSPACE_TOOLS & PROJECT_ONLY_TOOL_CALL_TOOLS)

    @pytest.mark.parametrize("name,props,required", [
        ("list_files", {"path"}, ["path"]),
        ("read_file", {"path"}, ["path"]),
        ("write_file", {"path", "content"}, ["path", "content"]),
        (
            "edit_file",
            {"path", "old_string", "new_string", "replace_all"},
            ["path", "old_string", "new_string"],
        ),
        (
            "copy_file",
            {"src", "dest", "overwrite", "include_hidden"},
            ["src", "dest"],
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
        assert "chat://" in desc and "proj://" in desc
        for pname in props & {"path", "src", "dest"}:
            pdesc = params["properties"][pname]["description"]
            assert "chat://" in pdesc or "proj://" in pdesc, (name, pname)
        if name == "copy_file":
            for code in ("destination_exists", "not_a_regular_file", "invalid_destination"):
                assert code in desc
            assert "'skipped'" in desc
            for p in ("overwrite", "include_hidden"):
                assert params["properties"][p]["type"] == "boolean"

    def test_scheme_is_mandatory_in_descriptions(self):
        props = TOOL_CALL_REGISTRY["list_files"]["parameters"]["properties"]
        assert "scheme is required" in props["path"]["description"]

    def test_edit_file_requires_prior_read(self):
        desc = TOOL_CALL_REGISTRY["edit_file"]["description"]
        assert "read_file" in desc
        assert "earlier in this conversation" in desc
        assert "another conversation of the project does not count" in desc

    def test_write_file_points_at_deliverables(self):
        desc = TOOL_CALL_REGISTRY["write_file"]["description"]
        assert "finished deliverables" in desc
        assert "1MB" in desc
        assert "copy_file" in desc

    def test_edit_mirrors_edit_workspace_file(self):
        ws = TOOL_CALL_REGISTRY["edit_workspace_file"]["parameters"]
        new = TOOL_CALL_REGISTRY["edit_file"]["parameters"]
        assert set(ws["properties"]) == set(new["properties"])
        assert ws["required"] == new["required"]

    def test_copy_mentions_scratch_refusal_toward_proj(self):
        desc = TOOL_CALL_REGISTRY["copy_file"]["description"]
        for scratch in (".responses/", ".subagent_responses/", "pasted/"):
            assert scratch in desc
        assert (
            "This conversation's '.responses/', '.subagent_responses/' and "
            "'pasted/' (chat://) cannot be copied to proj://."
        ) in desc

    def test_copy_invalid_destination_wording(self):
        desc = TOOL_CALL_REGISTRY["copy_file"]["description"]
        assert (
            "A destination equal to the source, inside it, or containing it "
            "is refused (invalid_destination)."
        ) in desc

    def test_list_files_result_paths_are_space_relative(self):
        desc = TOOL_CALL_REGISTRY["list_files"]["description"]
        assert "{file_count, files}" in desc
        assert "relative to the space root" in desc
        assert "'proj://reports/q3.md'" in desc

    def test_read_file_office_wording(self):
        desc = TOOL_CALL_REGISTRY["read_file"]["description"]
        assert "Do not use it for Office documents" in desc
        assert "refused" not in desc

    def test_edit_file_cross_family_read(self):
        desc = TOOL_CALL_REGISTRY["edit_file"]["description"]
        assert "get_workspace_file / write_workspace_file read counts too" in desc

    def test_copy_dest_has_no_default(self):
        dest = TOOL_CALL_REGISTRY["copy_file"]["parameters"]["properties"]["dest"]
        assert "Required" in dest["description"]
        assert "Defaults" not in dest["description"]


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

class TestAllowlists:
    def test_public_allowlist_includes_project_tools(self):
        assert PROJECT_ONLY_TOOL_CALL_TOOLS <= PUBLIC_TOOL_CALL_ALLOWLIST

    def test_public_allowlist_keeps_workspace_tools_dispatchable(self):
        # Hidden from the public prompt, but replayed calls still dispatch.
        assert PROJECT_HIDDEN_WORKSPACE_TOOLS <= PUBLIC_TOOL_CALL_ALLOWLIST

    def test_script_bridge_excludes_file_tools(self):
        assert not (PROJECT_ONLY_TOOL_CALL_TOOLS & SCRIPT_TOOL_CALL_ALLOWLIST)
        assert not (PROJECT_HIDDEN_WORKSPACE_TOOLS & SCRIPT_TOOL_CALL_ALLOWLIST)

    def test_not_mutating(self):
        assert not (set(FILE_TOOLS) & mutating_tool_call_tools())


# ---------------------------------------------------------------------------
# Prompt tiers
# ---------------------------------------------------------------------------

class TestTopLevelPrompt:
    @pytest.mark.parametrize("is_routine", [False, True])
    @pytest.mark.parametrize("has_project", [False, True])
    def test_file_tools_follow_project(self, has_project, is_routine):
        p = get_system_prompt(
            "key", has_project=has_project, is_routine=is_routine,
        )
        _assert_offered(p, has_project)

    def test_slack_conversation_is_standalone(self):
        _assert_offered(get_system_prompt("key", is_slack=True), False)

    def test_project_rules_and_examples_name_only_offered_tools(self):
        p = get_system_prompt("key", has_project=True)
        for name in WORKSPACE_TOOLS:
            assert f'tool_name="{name}"' not in p, name
        assert 'tool_name="list_files", arguments={"path": "chat://"}' in p
        assert 'tool_name="copy_file"' in p


class TestSubAgentPrompt:
    @pytest.mark.parametrize("can_nest", [False, True])
    @pytest.mark.parametrize("has_project", [False, True])
    def test_file_tools_follow_project(self, has_project, can_nest):
        p = get_sub_agent_system_prompt(
            "Researcher", "key", has_project=has_project, can_nest=can_nest,
        )
        _assert_offered(p, has_project)

    def test_project_rules_name_only_offered_tools(self):
        p = get_sub_agent_system_prompt("Researcher", "key", has_project=True)
        rules = p[p.index("**Important rules:**"):]
        for name in WORKSPACE_TOOLS:
            assert f"`{name}`" not in rules, name
        assert "`read_file` (`chat://<file>`)" in rules


class TestPublicPrompt:
    @pytest.mark.parametrize("is_routine", [False, True])
    def test_offered(self, is_routine):
        p = get_public_project_system_prompt(
            user_email="ada@example.com", is_routine=is_routine,
        )
        _assert_offered(p, True)
        for name in WORKSPACE_TOOLS:
            assert f'tool_name="{name}"' not in p, name


class TestStandaloneRunKinds:
    def test_user_subagent_prompt_is_standalone(self):
        p = get_user_subagent_system_prompt(
            "key", target_user_email="t@example.com",
            caller_email="c@example.com",
        )
        _assert_offered(p, False)

    def test_inference_api_prompt_has_no_project_tools(self):
        # Inference runs also drop mutating tools, but none of the file
        # tools is mutating: the workspace tools stay, the new five never.
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


# ---------------------------------------------------------------------------
# Public-project rejection lists what the public prompt offers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("project_id", ["proj-1", None])
def test_public_rejection_lists_offered_tools(project_id):
    import json

    from chat.gemini_api.tool_dispatch import _dispatch_tool_call

    result, _parts = asyncio.run(_dispatch_tool_call(
        app=None, provider=None, user={"id": 1, "email": "t@example.com"},
        conversation_id="conv-1", timezone="UTC", tool_name="tool_call",
        args={"tool_name": "memory_search", "arguments": {}},
        project_id=project_id, is_public=True,
    ))
    error = json.loads(result)["error"]
    listed = error.split("Available dynamic tools: ", 1)[1]
    for name in FILE_TOOLS:
        assert name in listed, name
    for name in WORKSPACE_TOOLS:
        assert (name in listed) is (project_id is None), name
