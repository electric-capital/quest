"""Quest Docs model-facing tools: handlers, dispatch, registry, allowlists.

Covers:

1. The seven TOOL_CALL_REGISTRY entries (requires_service, mutating flags,
   prompt-safe descriptions) and their place in the public / script
   allowlists and the requires_service prompt gating.
2. ``ToolContext.run_kind`` derivation and the dispatch keyword params.
3. End-to-end ``_dispatch_tool_call(tool_call ...)`` runs against the
   isolated ``docs_env`` (tests/test_docs_service.py): JSON result shapes,
   argument coercion, ``approval_required`` / ``docs_disabled`` payloads.
4. Public-project dispatch reaches the doc tools (allowlisted) and never
   sees private docs; inference API runs refuse the four writes at
   dispatch but read; sub-agent / user-subagent / Slack verdicts.
5. The sandbox script bridge exposes exactly the three reads.
6. The ``docs`` feature gate registration.
"""

import asyncio
import inspect
import json
import uuid

import pytest

from chat.docs.access import (
    DENY_PUBLIC_DOC_FROM_PRIVATE,
    DENY_SCRIPT,
    DENY_SLACK_NEEDS_APPROVAL,
    DENY_SUB_AGENT,
    DENY_USER_SUBAGENT,
)
from chat.docs.constants import DOCS_SERVICE_KEY, doc_not_found_message, docs_disabled_message
from tests.test_docs_service import (  # noqa: F401  (docs_env is a fixture)
    PNG,
    body,
    docs_env,
    seed_doc,
    workspace_dir,
    make_caller,
)

DOC_TOOLS = (
    "list_docs", "search_docs", "read_doc", "create_doc",
    "edit_doc", "append_to_doc", "add_doc_image",
)
READ_TOOLS = {"list_docs", "search_docs", "read_doc"}
WRITE_TOOLS = {"create_doc", "edit_doc", "append_to_doc", "add_doc_image"}

# Substrings tests/test_public_projects.py::test_no_internal_tool_docs
# forbids in the public prompt, where these descriptions are rendered.
_PUBLIC_PROMPT_FORBIDDEN = (
    "system:", "create_action_request(", "load_skills", "wait_for_handles",
    "memory_search", "authed_post", "agent_task(", "load_gmail_attachment",
    "- **authed_get**",
)


def _run(coro):
    return asyncio.run(coro)


def _dispatch(env, tool, arguments, *, who="alice", cid="new", **kwargs):
    from chat.gemini_api.tool_dispatch import _dispatch_tool_call

    if cid == "new":
        cid = str(uuid.uuid4())
    if cid is not None:
        (env.dirs["chats"] / cid).mkdir(parents=True, exist_ok=True)
    result, extra = _run(_dispatch_tool_call(
        app=None, provider=None, user=env.users[who],
        conversation_id=cid, timezone="UTC",
        tool_name="tool_call",
        args={"tool_name": tool, "arguments": dict(arguments)},
        **kwargs,
    ))
    assert extra == []
    return json.loads(result)


# ---------------------------------------------------------------------------
# Registry, allowlists, prompt gating
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_seven_entries_with_handlers(self):
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        for name in DOC_TOOLS:
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["name"] == name
            assert spec["requires_service"] == DOCS_SERVICE_KEY == "docs"
            assert name in TOOL_CALL_HANDLERS

    def test_mutating_classification(self):
        from chat.llm.tool_schemas import mutating_tool_call_tools

        assert set(DOC_TOOLS) & mutating_tool_call_tools() == WRITE_TOOLS

    def test_parameters(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        def props(name):
            return TOOL_CALL_REGISTRY[name]["parameters"]["properties"]

        def required(name):
            return TOOL_CALL_REGISTRY[name]["parameters"]["required"]

        assert props("list_docs")["scope"]["enum"] == ["user", "project", "all"]
        assert (props("list_docs")["limit"]["minimum"], props("list_docs")["limit"]["maximum"]) == (1, 200)
        assert props("search_docs")["limit"]["maximum"] == 50
        assert required("search_docs") == ["query"]
        assert required("read_doc") == ["doc_id"]
        assert required("create_doc") == ["title", "content"]
        assert props("create_doc")["target"]["enum"] == ["user", "project"]
        assert required("edit_doc") == ["doc_id", "old_string", "new_string"]
        assert props("edit_doc")["replace_all"]["type"] == "boolean"
        assert required("append_to_doc") == ["doc_id", "content"]
        assert props("append_to_doc")["ensure_blank_line"]["default"] is True
        assert required("add_doc_image") == ["doc_id", "workspace_path"]
        assert props("add_doc_image")["placement"]["enum"] == ["append", "none"]

    def test_descriptions_are_public_prompt_safe(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        for name in DOC_TOOLS:
            text = json.dumps(TOOL_CALL_REGISTRY[name])
            for term in _PUBLIC_PROMPT_FORBIDDEN:
                assert term not in text, (name, term)

    def test_write_descriptions_explain_the_handoff(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        for name in ("edit_doc", "append_to_doc", "add_doc_image"):
            desc = TOOL_CALL_REGISTRY[name]["description"]
            assert "approval_required" in desc and "write_doc" in desc
            assert "suggested_request" in desc

    def test_public_allowlist_has_all_seven(self):
        from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST

        assert set(DOC_TOOLS) <= PUBLIC_TOOL_CALL_ALLOWLIST

    def test_script_allowlist_has_only_the_reads(self):
        from chat.gemini_api.script_tool_call import SCRIPT_TOOL_CALL_ALLOWLIST

        assert set(DOC_TOOLS) & SCRIPT_TOOL_CALL_ALLOWLIST == READ_TOOLS

    def test_prompt_hides_doc_tools_without_the_docs_key(self):
        from chat.gemini_api.system_prompt import _build_dynamic_tools_section

        hidden = _build_dynamic_tools_section(connected_services={})
        shown = _build_dynamic_tools_section(connected_services={"docs": True})
        for name in DOC_TOOLS:
            assert f"**{name}**" not in hidden
            assert f"**{name}**" in shown


# ---------------------------------------------------------------------------
# ToolContext.run_kind + dispatch params
# ---------------------------------------------------------------------------


class TestRunKind:
    @pytest.mark.parametrize("flags, expected", [
        ({}, "top_level"),
        ({"is_slack": True}, "slack"),
        ({"is_user_subagent": True}, "user_subagent"),
        ({"is_inference_api": True}, "inference_api"),
        ({"is_sub_agent": True}, "sub_agent"),
        ({"is_script": True}, "script"),
        # first match wins
        ({"is_script": True, "is_sub_agent": True}, "script"),
        ({"is_sub_agent": True, "is_inference_api": True}, "sub_agent"),
        ({"is_inference_api": True, "is_user_subagent": True}, "inference_api"),
        ({"is_user_subagent": True, "is_slack": True}, "user_subagent"),
        ({"is_public": True}, "top_level"),
    ])
    def test_derivation(self, flags, expected):
        from chat.gemini_api.tool_dispatch import ToolContext

        ctx = ToolContext(
            app=None, provider=None, user={"id": 1}, conversation_id="c",
            timezone="UTC", **flags,
        )
        assert ctx.run_kind == expected

    def test_dispatch_signatures_carry_the_flags(self):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call, _dispatch_tool_call_inner

        for fn in (_dispatch_tool_call, _dispatch_tool_call_inner):
            params = inspect.signature(fn).parameters
            for name in ("is_user_subagent", "is_slack", "is_script"):
                assert params[name].default is False

    def test_call_sites_pass_the_flags(self):
        import chat.gemini_api.conversation as conversation
        import chat.gemini_api.script_tool_call as script_tool_call

        source = inspect.getsource(conversation)
        assert "is_user_subagent=ctx.is_user_subagent" in source
        assert "is_slack=ctx.is_slack_origin" in source
        assert "is_script=True" in inspect.getsource(script_tool_call)


# ---------------------------------------------------------------------------
# End-to-end through dispatch
# ---------------------------------------------------------------------------


class TestToolsEndToEnd:
    def test_create_read_edit_append_list_search(self, docs_env):
        cid = str(uuid.uuid4())
        created = _dispatch(docs_env, "create_doc", {
            "title": "Journal", "content": "# Journal\nfirst entry\n",
            "description": "daily", "intent_message": "Create journal",
        }, cid=cid)
        assert created["mode"] == "private" and created["scope"] == "user"
        doc_id = created["id"]

        edited = _dispatch(docs_env, "edit_doc", {
            "doc_id": doc_id, "old_string": "first", "new_string": "1st",
            "replace_all": "true",
        }, cid=cid)
        assert edited["replaced"] == 1 and edited["total_lines"] == 2

        appended = _dispatch(docs_env, "append_to_doc", {
            "doc_id": doc_id, "content": "second", "ensure_blank_line": "false",
        }, cid=cid)
        assert appended == {
            "appended_lines": 1, "total_lines": 3, "updated_at": appended["updated_at"],
        }
        assert body(doc_id) == "# Journal\n1st entry\nsecond\n"
        row = _run(docs_env.doc_store.get_doc(doc_id))
        assert row["last_write_source"] == f"conversation:{cid}"

        read = _dispatch(docs_env, "read_doc", {"doc_id": doc_id, "start_line": "2"})
        assert read["content"] == "1st entry\nsecond\n" and read["start_line"] == 2

        listed = _dispatch(docs_env, "list_docs", {"limit": "5"})
        assert listed["count"] == 1 and listed["docs"][0]["id"] == doc_id

        found = _dispatch(docs_env, "search_docs", {"query": "SECOND"})
        assert found["results"][0]["matches"][0]["line"] == 3

    def test_add_doc_image_through_dispatch(self, docs_env):
        doc = seed_doc(docs_env, content="x\n")
        caller = make_caller(docs_env)
        (workspace_dir(docs_env, caller) / "plot.png").write_bytes(PNG)
        result = _dispatch(docs_env, "add_doc_image", {
            "doc_id": doc["id"], "workspace_path": "plot.png", "alt": "Plot",
        }, cid=caller.conversation_id)
        assert result["asset"] == "plot.png" and result["appended"] is True
        assert body(doc["id"]) == "x\n\n![Plot](assets/plot.png)\n"

    def test_error_shapes(self, docs_env):
        missing = str(uuid.uuid4())
        assert _dispatch(docs_env, "read_doc", {"doc_id": missing}) == {
            "error": doc_not_found_message(missing),
        }
        assert _dispatch(docs_env, "list_docs", {"limit": "many"}) == {
            "error": "limit must be an integer.",
        }
        doc = seed_doc(docs_env)
        unread = _dispatch(docs_env, "edit_doc", {
            "doc_id": doc["id"], "old_string": "one", "new_string": "1",
        })
        assert set(unread) == {"error"} and "has not been read" in unread["error"]

    def test_approval_required_payload(self, docs_env):
        doc = seed_doc(docs_env, content="alpha\n", shares=[("bob", "read")])
        cid = str(uuid.uuid4())
        _dispatch(docs_env, "read_doc", {"doc_id": doc["id"]}, cid=cid)
        result = _dispatch(docs_env, "edit_doc", {
            "doc_id": doc["id"], "old_string": "alpha", "new_string": "beta",
            "intent_message": "Edit",
        }, cid=cid)
        assert result == {
            "error": "approval_required",
            "message": (
                'This doc is shared; propose the change with '
                'create_action_request(request_type="write_doc", ...)'
            ),
            "suggested_request": {
                "request_type": "write_doc",
                "params": {
                    "operation": "edit", "doc_id": doc["id"],
                    "old_string": "alpha", "new_string": "beta", "replace_all": False,
                },
            },
        }
        appended = _dispatch(docs_env, "append_to_doc", {
            "doc_id": doc["id"], "content": "more", "ensure_blank_line": "false",
        })
        assert appended["error"] == "approval_required"
        assert appended["suggested_request"]["params"] == {
            "operation": "append", "doc_id": doc["id"],
            "content": "more", "ensure_blank_line": False,
        }
        assert body(doc["id"]) == "alpha\n"

    def test_gate_closed_every_tool_disabled_without_db(self, docs_env, monkeypatch):
        doc = seed_doc(docs_env)
        docs_env.fg.set_feature_enabled(docs_env.fg.FEATURE_DOCS, False)

        async def _boom(*_a, **_k):
            raise AssertionError("DB touched while the gate is closed")

        for name in ("get_doc", "list_accessible_docs", "create_doc", "update_after_write"):
            monkeypatch.setattr(docs_env.doc_store, name, _boom)
        arguments = {
            "list_docs": {"limit": "not-a-number"},
            "search_docs": {"query": "x"},
            "read_doc": {"doc_id": doc["id"]},
            "create_doc": {"title": "T", "content": "x"},
            "edit_doc": {"doc_id": doc["id"], "old_string": "a", "new_string": "b"},
            "append_to_doc": {"doc_id": doc["id"], "content": "x"},
            "add_doc_image": {"doc_id": doc["id"], "workspace_path": "a.png"},
        }
        for tool in DOC_TOOLS:
            assert _dispatch(docs_env, tool, arguments[tool]) == {
                "error": "docs_disabled", "message": docs_disabled_message(),
            }, tool


# ---------------------------------------------------------------------------
# Tiers: public, inference, sub-agents, Slack, scripts
# ---------------------------------------------------------------------------


class TestTiers:
    def test_public_dispatch_reaches_doc_tools(self, docs_env):
        private = seed_doc(docs_env, title="Secret")
        project = docs_env.public_project
        created = _dispatch(docs_env, "create_doc", {"title": "Findings", "content": "web\n"},
                            project_id=project, is_public=True)
        assert created["mode"] == "public"
        listed = _dispatch(docs_env, "list_docs", {}, project_id=project, is_public=True)
        assert [d["id"] for d in listed["docs"]] == [created["id"]]
        hidden = _dispatch(docs_env, "read_doc", {"doc_id": private["id"]},
                           project_id=project, is_public=True)
        assert hidden == {"error": doc_not_found_message(private["id"])}
        appended = _dispatch(docs_env, "append_to_doc",
                             {"doc_id": created["id"], "content": "more"},
                             project_id=project, is_public=True)
        assert "error" not in appended

    def test_private_dispatch_cannot_write_public_doc(self, docs_env):
        doc = seed_doc(docs_env, mode="public")
        result = _dispatch(docs_env, "append_to_doc", {"doc_id": doc["id"], "content": "x"})
        assert result == {"error": DENY_PUBLIC_DOC_FROM_PRIVATE}

    @pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
    def test_inference_api_refuses_writes_at_dispatch(self, docs_env, tool, monkeypatch):
        async def _boom(*_a, **_k):
            raise AssertionError("handler reached")

        from chat.gemini_api import tool_dispatch
        monkeypatch.setitem(tool_dispatch.TOOL_CALL_HANDLERS, tool, _boom)
        result = _dispatch(docs_env, tool, {"doc_id": "x"}, is_inference_api=True)
        assert "not available in inference API runs" in result["error"]

    def test_inference_api_reads(self, docs_env):
        doc = seed_doc(docs_env, content="hello\n")
        read = _dispatch(docs_env, "read_doc", {"doc_id": doc["id"]}, is_inference_api=True)
        assert read["content"] == "hello\n" and read["writable"] == "denied"
        listed = _dispatch(docs_env, "list_docs", {}, is_inference_api=True)
        assert listed["count"] == 1

    @pytest.mark.parametrize("flags, reason", [
        ({"is_sub_agent": True}, DENY_SUB_AGENT),
        ({"is_user_subagent": True}, DENY_USER_SUBAGENT),
    ])
    def test_read_only_dispatch_kinds(self, docs_env, flags, reason):
        doc = seed_doc(docs_env, content="t\n")
        assert _dispatch(docs_env, "read_doc", {"doc_id": doc["id"]}, **flags)["content"] == "t\n"
        for tool, args in (
            ("append_to_doc", {"doc_id": doc["id"], "content": "x"}),
            ("create_doc", {"title": "T", "content": "x"}),
        ):
            assert _dispatch(docs_env, tool, args, **flags) == {"error": reason}
        assert body(doc["id"]) == "t\n"

    def test_slack_dispatch(self, docs_env):
        own = seed_doc(docs_env, title="Own", content="a\n")
        shared = seed_doc(docs_env, title="Shared", content="a\n", shares=[("bob", "read")])
        ok = _dispatch(docs_env, "append_to_doc", {"doc_id": own["id"], "content": "b"},
                       is_slack=True)
        assert "error" not in ok
        refused = _dispatch(docs_env, "append_to_doc", {"doc_id": shared["id"], "content": "b"},
                            is_slack=True)
        assert refused == {"error": DENY_SLACK_NEEDS_APPROVAL}

    def test_script_bridge(self, docs_env):
        from chat.gemini_api.script_tool_call import (
            ScriptToolCallRequest,
            script_tool_call_endpoint,
        )

        user_doc = seed_doc(docs_env, title="Mine", content="script readable\n")
        project_doc = seed_doc(docs_env, title="Proj", project_id=docs_env.private_project)
        alice = docs_env.users["alice"]

        def call(tool, arguments):
            return _run(script_tool_call_endpoint(
                ScriptToolCallRequest(tool_name=tool, arguments=arguments),
                user=alice, lease=None,
            ))

        read = call("read_doc", {"doc_id": user_doc["id"]})
        payload = json.loads(read.body)
        assert payload["content"] == "script readable\n"
        assert payload["writable"] == "denied" and payload["write_note"] == DENY_SCRIPT
        hidden = json.loads(call("read_doc", {"doc_id": project_doc["id"]}).body)
        assert hidden == {"error": doc_not_found_message(project_doc["id"])}
        listed = json.loads(call("list_docs", {}).body)
        assert [d["id"] for d in listed["docs"]] == [user_doc["id"]]
        assert json.loads(call("search_docs", {"query": "readable"}).body)["results"]
        for tool in sorted(WRITE_TOOLS):
            assert call(tool, {}).status_code == 400

    def test_script_dispatch_denies_writes(self, docs_env):
        doc = seed_doc(docs_env)
        result = _dispatch(docs_env, "append_to_doc", {"doc_id": doc["id"], "content": "x"},
                           cid=None, is_script=True)
        assert result == {"error": DENY_SCRIPT}


# ---------------------------------------------------------------------------
# Feature gate registration
# ---------------------------------------------------------------------------


class TestFeatureGate:
    def test_registered_off_by_default_and_per_user(self, tmp_path, monkeypatch):
        import config.feature_gates as fg

        monkeypatch.setattr(fg, "FEATURE_GATES_FILE", tmp_path / "feature_gates.json")
        assert fg.FEATURE_DOCS == "docs"
        assert fg.FEATURE_DOCS in fg.KNOWN_FEATURES
        assert fg.FEATURE_DOCS in fg.PER_USER_ACCESS_FEATURES
        assert fg.FEATURE_LABELS[fg.FEATURE_DOCS]["label"] == "Quest Docs"
        assert not fg.docs_enabled_for("user@example.com")

        fg.set_feature_enabled(fg.FEATURE_DOCS, True)
        assert fg.docs_enabled_for("anyone@example.com")
        fg.set_feature_allowed_users(fg.FEATURE_DOCS, ["User@Example.com"])
        assert fg.docs_enabled_for("user@example.com")
        assert not fg.docs_enabled_for("other@example.com")
