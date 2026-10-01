"""Tests for the table-driven tool dispatch in chat/gemini_api/tool_dispatch.py.

The old per-tool ``elif`` ladder was replaced by two name-keyed handler
tables; these tests pin the rosters (parity with TOOL_CALL_REGISTRY), the
plugin registration guards, the public-project rejects, and the
requires_service prompt gating.
"""

import asyncio
import json

import pytest

from chat.gemini_api.tool_dispatch import (
    DIRECT_TOOL_HANDLERS,
    TOOL_CALL_HANDLERS,
    _dispatch_tool_call,
    register_dynamic_tool,
    register_tool_call_handler,
    unregister_dynamic_tool,
)
from chat.llm.tool_schemas import (
    ACTION_REQUEST_TYPE_ENUM,
    PUBLIC_TOOL_CALL_ALLOWLIST,
    TOOL_CALL_REGISTRY,
    register_action_request_type,
    register_tool_call_tool,
)


def _run(coro):
    return asyncio.run(coro)


_USER = {"id": 1, "email": "t@example.com"}


def _dispatch(tool_name, args, **kwargs):
    return _run(_dispatch_tool_call(
        app=None, provider=None, user=_USER,
        conversation_id="c1", timezone="UTC",
        tool_name=tool_name, args=args, **kwargs,
    ))


# ---------------------------------------------------------------------------
# Table parity
# ---------------------------------------------------------------------------


class TestTableParity:
    def test_tool_call_table_matches_registry(self):
        """Every dynamic tool in TOOL_CALL_REGISTRY has a dispatch handler
        and vice-versa -- the tables can never drift silently."""
        assert set(TOOL_CALL_HANDLERS) == set(TOOL_CALL_REGISTRY)

    def test_direct_table_roster(self):
        assert set(DIRECT_TOOL_HANDLERS) == {
            "get_current_time",
            "list_workspace_files",
            "get_workspace_file",
            "load_gmail_attachment",
            "write_workspace_file",
            "memory_search",
            "memory_list",
            "list_skills",
            "search_skills",
            "load_skills",
            "list_my_skills",
            "get_skill",
            "list_routines",
            "run_script",
            "run_python",
            "project_db_query",
        }

    def test_public_allowlist_is_subset_of_tool_call_table(self, slack_plugin):
        # send_slack_dm_to_self is served by the Slack plugin (the one
        # migrated tool in the allowlist).
        assert PUBLIC_TOOL_CALL_ALLOWLIST <= set(TOOL_CALL_HANDLERS)


# ---------------------------------------------------------------------------
# Registration guards
# ---------------------------------------------------------------------------


async def _dummy_handler(ctx, args):
    return json.dumps({"ok": True})


class TestRegistration:
    def test_handler_requires_spec_first(self):
        with pytest.raises(ValueError, match="no TOOL_CALL_REGISTRY spec"):
            register_tool_call_handler("tp_nospec", _dummy_handler)

    def test_duplicate_spec_rejected(self):
        with pytest.raises(ValueError, match="already registered"):
            register_tool_call_tool({
                "name": "get_current_time",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            })

    def test_spec_requires_name_and_parameters(self):
        with pytest.raises(ValueError, match="name"):
            register_tool_call_tool({"description": "d", "parameters": {}})
        with pytest.raises(ValueError, match="parameters"):
            register_tool_call_tool({"name": "tp_x", "description": "d"})

    def test_register_then_dispatch_round_trip(self):
        register_tool_call_tool({
            "name": "tp_round_trip",
            "description": "d",
            "parameters": {"type": "object", "properties": {}},
        })
        register_tool_call_handler("tp_round_trip", _dummy_handler)
        try:
            with pytest.raises(ValueError, match="already registered"):
                register_tool_call_handler("tp_round_trip", _dummy_handler)
            result, extra = _dispatch(
                "tool_call",
                {"tool_name": "tp_round_trip", "arguments": {}},
            )
            assert json.loads(result) == {"ok": True}
            assert extra == []
        finally:
            TOOL_CALL_REGISTRY.pop("tp_round_trip", None)
            TOOL_CALL_HANDLERS.pop("tp_round_trip", None)

    def test_register_dynamic_tool_registers_both_tables(self):
        from chat.llm.tool_schemas import PLUGIN_TOOL_NAMES
        register_dynamic_tool(
            {"name": "tp_pair", "description": "d",
             "parameters": {"type": "object", "properties": {}}},
            _dummy_handler,
        )
        try:
            assert "tp_pair" in TOOL_CALL_REGISTRY
            assert TOOL_CALL_HANDLERS["tp_pair"] is _dummy_handler
            assert "tp_pair" in PLUGIN_TOOL_NAMES
            result, _ = _dispatch(
                "tool_call", {"tool_name": "tp_pair", "arguments": {}},
            )
            assert json.loads(result) == {"ok": True}
        finally:
            unregister_dynamic_tool("tp_pair")
        assert "tp_pair" not in TOOL_CALL_REGISTRY
        assert "tp_pair" not in TOOL_CALL_HANDLERS
        assert "tp_pair" not in PLUGIN_TOOL_NAMES

    def test_register_dynamic_tool_rolls_back_spec_when_handler_fails(self):
        # Fault injection: a handler-side refusal (here a pre-existing
        # handler entry with no spec) must not leave an orphan spec behind.
        from chat.llm.tool_schemas import PLUGIN_TOOL_NAMES
        TOOL_CALL_HANDLERS["tp_orphan"] = _dummy_handler
        try:
            with pytest.raises(ValueError, match="already registered"):
                register_dynamic_tool(
                    {"name": "tp_orphan", "description": "d",
                     "parameters": {"type": "object", "properties": {}}},
                    _dummy_handler,
                )
            assert "tp_orphan" not in TOOL_CALL_REGISTRY
            assert "tp_orphan" not in PLUGIN_TOOL_NAMES
            # The pre-existing handler entry was not ours; it stays.
            assert TOOL_CALL_HANDLERS["tp_orphan"] is _dummy_handler
        finally:
            TOOL_CALL_HANDLERS.pop("tp_orphan", None)

    def test_register_dynamic_tool_rejects_non_callable_handler_untouched(self):
        with pytest.raises(ValueError, match="not callable"):
            register_dynamic_tool(
                {"name": "tp_nocall", "description": "d",
                 "parameters": {"type": "object", "properties": {}}},
                "not-a-callable",
            )
        assert "tp_nocall" not in TOOL_CALL_REGISTRY
        assert "tp_nocall" not in TOOL_CALL_HANDLERS

    def test_register_dynamic_tool_duplicate_core_name_untouched(self):
        with pytest.raises(ValueError, match="already registered"):
            register_dynamic_tool(
                {"name": "get_current_time", "description": "d",
                 "parameters": {"type": "object", "properties": {}}},
                _dummy_handler,
            )
        # The core pair is intact (the failed spec add never reached the
        # handler step, and the rollback only removes a spec it added).
        assert TOOL_CALL_REGISTRY["get_current_time"]["name"] == "get_current_time"
        assert "get_current_time" in TOOL_CALL_HANDLERS

    def test_action_request_type_duplicate_rejected(self):
        with pytest.raises(ValueError, match="already registered"):
            register_action_request_type("create_memory")

    def test_action_request_type_subagent_return_reserved(self):
        assert "subagent_return" not in ACTION_REQUEST_TYPE_ENUM
        with pytest.raises(ValueError, match="reserved"):
            register_action_request_type("subagent_return")

    def test_action_request_type_registers_new_string(self):
        register_action_request_type("tp_new_type")
        try:
            assert "tp_new_type" in ACTION_REQUEST_TYPE_ENUM
        finally:
            ACTION_REQUEST_TYPE_ENUM.remove("tp_new_type")


# ---------------------------------------------------------------------------
# Dispatch behavior parity with the old ladder
# ---------------------------------------------------------------------------


class TestDispatchBehavior:
    def test_missing_inner_tool_name(self):
        result, _ = _dispatch("tool_call", {"arguments": {}})
        assert "requires a 'tool_name'" in result

    def test_unknown_inner_tool(self):
        result, _ = _dispatch(
            "tool_call", {"tool_name": "definitely_not_a_tool", "arguments": {}},
        )
        assert "Unknown tool" in result

    def test_non_dict_arguments_rejected(self):
        result, _ = _dispatch(
            "tool_call", {"tool_name": "get_current_time", "arguments": "nope"},
        )
        assert "must be a JSON object" in result

    def test_get_current_time_via_tool_call(self):
        result, _ = _dispatch(
            "tool_call", {"tool_name": "get_current_time", "arguments": {}},
        )
        assert "utc" in result.lower()

    def test_get_current_time_direct(self):
        result, _ = _dispatch("get_current_time", {})
        assert "utc" in result.lower()

    def test_wait_for_handles_stub_for_sub_agents(self):
        result, _ = _dispatch(
            "tool_call", {"tool_name": "wait_for_handles", "arguments": {}},
        )
        assert "only available to the top-level agent" in result

    def test_handler_exception_becomes_structured_error(self):
        register_tool_call_tool({
            "name": "tp_boom",
            "description": "d",
            "parameters": {"type": "object", "properties": {}},
        })

        async def _boom(ctx, args):
            raise RuntimeError("kapow")

        register_tool_call_handler("tp_boom", _boom)
        try:
            result, _ = _dispatch(
                "tool_call", {"tool_name": "tp_boom", "arguments": {}},
            )
            assert json.loads(result) == {
                "error": "tool_call 'tp_boom' failed: kapow",
            }
        finally:
            TOOL_CALL_REGISTRY.pop("tp_boom", None)
            TOOL_CALL_HANDLERS.pop("tp_boom", None)


# ---------------------------------------------------------------------------
# Public-project enforcement
# ---------------------------------------------------------------------------


class TestPublicEnforcement:
    def test_non_allowlisted_dynamic_tool_rejected(self):
        result, _ = _dispatch(
            "tool_call", {"tool_name": "memory_search", "arguments": {"query": "x"}},
            is_public=True,
        )
        assert "not available in public-project" in result

    def test_non_allowlisted_direct_tool_rejected(self):
        result, _ = _dispatch("memory_search", {"query": "x"}, is_public=True)
        assert "not available in public-project" in result

    def test_allowlisted_tool_passes_gate(self):
        result, _ = _dispatch(
            "tool_call", {"tool_name": "get_current_time", "arguments": {}},
            is_public=True,
        )
        assert "utc" in result.lower()


# ---------------------------------------------------------------------------
# requires_service prompt gating
# ---------------------------------------------------------------------------


class TestRequiresServiceGating:
    # A plugin tool's requires_service gating is pinned end-to-end in
    # tests/test_plugin_loader.py (via example_ping) and each plugin's
    # own suite.

    def test_ungated_tools_unaffected(self):
        from chat.gemini_api.system_prompt import _build_dynamic_tools_section
        section = _build_dynamic_tools_section(connected_services={})
        assert "get_current_time" in section
        assert "authed_get" in section
