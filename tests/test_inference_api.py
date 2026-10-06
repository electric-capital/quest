"""Tests for the one-shot inference API feature.

Covers the inference_api_keys store (token minting/hashing round-trip),
the bearer-token auth dependency, the final-response extraction helper,
and the INFERENCE_API_TOOLS tier composition. DB-touching tests run
against an isolated temp SQLite file (same pattern as
test_action_request_wait_handle.py).
"""

import asyncio
import os
import shutil
import tempfile
import uuid
from importlib import reload

import pytest
from fastapi import HTTPException


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _isolated_db(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="quest_inference_api_test_")
    db_path = os.path.join(tmpdir, "quest.db")

    from config import paths
    monkeypatch.setattr(paths, "DATABASE_PATH", db_path, raising=True)

    import db.engine as engine_mod
    reload(engine_mod)
    import db.models as models_mod
    reload(models_mod)
    import db.inference_api_key_store as store_mod
    reload(store_mod)
    import db.user_store as user_store_mod
    reload(user_store_mod)

    models_mod.Base.metadata.create_all(engine_mod.engine)

    yield store_mod, models_mod

    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture()
def _user_id(_isolated_db):
    _store, models_mod = _isolated_db
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = models_mod.User(
                email=f"inference-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Inference Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def test_generate_token_prefix_and_uniqueness():
    from db import inference_api_key_store as store

    t1 = store.generate_token()
    t2 = store.generate_token()
    assert t1.startswith(store.TOKEN_PREFIX)
    assert t1 != t2
    assert len(t1) > 30


def test_hash_token_is_sha256_hex():
    from db import inference_api_key_store as store

    digest = store.hash_token("qst_example")
    assert len(digest) == 64
    assert digest == store.hash_token("qst_example")
    assert digest != store.hash_token("qst_other")


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------


def test_create_list_delete_key_round_trip(_isolated_db, _user_id):
    store, _models = _isolated_db

    token = store.generate_token()
    created = _run(store.create_key(_user_id, "dashboard", token))
    assert created["name"] == "dashboard"
    assert created["token_hint"] == token[-4:]
    assert created["last_used_at"] is None
    # The raw token never appears in the stored projection.
    assert token not in str(created)

    keys = _run(store.list_keys(_user_id))
    assert [k["id"] for k in keys] == [created["id"]]

    # Lookup by raw token resolves the same row.
    found = _run(store.get_key_by_token(token))
    assert found is not None and found["id"] == created["id"]
    assert _run(store.get_key_by_token("qst_wrong")) is None

    # last_used_at is set by touch_last_used.
    _run(store.touch_last_used(created["id"]))
    keys = _run(store.list_keys(_user_id))
    assert keys[0]["last_used_at"] is not None

    # Delete is owner-scoped.
    assert _run(store.delete_key(_user_id + 999, created["id"])) is False
    assert _run(store.delete_key(_user_id, created["id"])) is True
    assert _run(store.list_keys(_user_id)) == []
    assert _run(store.get_key_by_token(token)) is None


def test_count_keys(_isolated_db, _user_id):
    store, _models = _isolated_db
    assert _run(store.count_keys(_user_id)) == 0
    for i in range(3):
        _run(store.create_key(_user_id, f"key-{i}", store.generate_token()))
    assert _run(store.count_keys(_user_id)) == 3


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


def test_auth_dependency_rejects_missing_and_bad_tokens(_isolated_db):
    from chat.inference_api import get_current_user_inference_token

    with pytest.raises(HTTPException) as exc:
        _run(get_current_user_inference_token(_FakeRequest({})))
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        _run(get_current_user_inference_token(
            _FakeRequest({"Authorization": "Bearer qst_nope"})
        ))
    assert exc.value.status_code == 401


def test_auth_dependency_rejects_users_api_key(_isolated_db, _user_id):
    """The per-user users.api_key must NOT authenticate inference calls."""
    from chat.inference_api import get_current_user_inference_token
    from db.user_store import get_user_by_id

    user = _run(get_user_by_id(_user_id))
    with pytest.raises(HTTPException) as exc:
        _run(get_current_user_inference_token(
            _FakeRequest({"Authorization": f"Bearer {user['api_key']}"})
        ))
    assert exc.value.status_code == 401


def test_auth_dependency_accepts_valid_token(_isolated_db, _user_id, monkeypatch):
    store, _models = _isolated_db
    import chat.inference_api as inference_api

    # Domain enforcement is config-dependent; the dependency must gate on
    # check_user_allowed, so pin it open here.
    monkeypatch.setattr(inference_api, "check_user_allowed", lambda email: True)

    token = store.generate_token()
    created = _run(store.create_key(_user_id, "svc", token))

    user = _run(inference_api.get_current_user_inference_token(
        _FakeRequest({"Authorization": f"Bearer {token}"})
    ))
    assert user["id"] == _user_id
    assert user["_inference_key_id"] == created["id"]

    # Successful auth bumps last_used_at.
    keys = _run(store.list_keys(_user_id))
    assert keys[0]["last_used_at"] is not None


def test_auth_dependency_enforces_domain(_isolated_db, _user_id, monkeypatch):
    store, _models = _isolated_db
    import chat.inference_api as inference_api

    monkeypatch.setattr(inference_api, "check_user_allowed", lambda email: False)

    token = store.generate_token()
    _run(store.create_key(_user_id, "svc", token))

    with pytest.raises(HTTPException) as exc:
        _run(inference_api.get_current_user_inference_token(
            _FakeRequest({"Authorization": f"Bearer {token}"})
        ))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Final-response extraction
# ---------------------------------------------------------------------------


def test_extract_final_response_picks_last_valid_call():
    from chat.inference_api import extract_final_response

    messages = [
        {"type": "text", "role": "assistant", "content": "thinking..."},
        {
            "type": "tool_use",
            "tool_name": "return_final_response",
            "tool_input": {"response": "   "},  # rejected empty attempt
        },
        {
            "type": "tool_use",
            "tool_name": "tool_call",
            "tool_input": {"tool_name": "get_current_time"},
        },
        {
            "type": "tool_use",
            "tool_name": "return_final_response",
            "tool_input": {"response": "# Final answer\n\nDone."},
        },
    ]
    assert extract_final_response(messages) == "# Final answer\n\nDone."


def test_extract_final_response_none_when_absent():
    from chat.inference_api import extract_final_response

    assert extract_final_response([]) is None
    assert extract_final_response([
        {"type": "text", "role": "assistant", "content": "no tool call"},
        {
            "type": "tool_use",
            "tool_name": "return_final_response",
            "tool_input": {"response": ""},
        },
    ]) is None


# ---------------------------------------------------------------------------
# Tool tier composition
# ---------------------------------------------------------------------------


def test_inference_api_tools_tier():
    from chat.llm.tool_schemas import BASE_TOOLS, INFERENCE_API_TOOLS

    names = {t["name"] for t in INFERENCE_API_TOOLS}
    assert "return_final_response" in names
    # No action requests, no sub-agent spawning, no cross-user return.
    assert "create_action_request" not in names
    assert "agent_task" not in names
    assert "agent_task_parallel" not in names
    assert "return_to_caller" not in names
    # The base tier is fully included.
    assert {t["name"] for t in BASE_TOOLS} <= names


# ---------------------------------------------------------------------------
# Owner api_key must not enter the run
# ---------------------------------------------------------------------------


def test_run_inference_scrubs_owner_api_key_from_the_run(monkeypatch):
    """An inference token is narrower than the owner's reusable users.api_key.

    Everything the model sees in a run (proxy preamble, skill docs) is
    deliverable to the token holder through return_final_response, so
    run_conversation_turn must be driven with a user dict whose api_key is
    blank while identity is preserved. (Sandbox containers are unaffected:
    their QUEST_API_KEY is an ephemeral per-run token minted from the user
    id, see tests/test_sandbox_tokens.py.)

    Storage and flushing are stubbed (no DB, no module reloads) so the test
    exercises only the run_inference chokepoint.
    """
    import chat.gemini_api as gemini_api
    import chat.inference_api as inference_api
    from chat.storage import ChatStorage

    owner = {
        "id": 42,
        "email": "owner@example.com",
        "name": "Owner",
        "api_key": "owner-wide-api-key-" + uuid.uuid4().hex,
        "settings": {},
    }

    async def _create_conversation(user_id, model=None):
        assert user_id == owner["id"]
        return "conv-inference-1"

    async def _append_message(*args, **kwargs):
        return None

    async def _noop_flush():
        return None

    monkeypatch.setattr(
        ChatStorage, "create_inference_api_conversation",
        staticmethod(_create_conversation),
    )
    monkeypatch.setattr(
        ChatStorage, "append_message", staticmethod(_append_message),
    )
    monkeypatch.setattr(
        inference_api, "make_flush_callback",
        lambda *a, **k: (_noop_flush, None),
    )

    seen: dict = {}

    async def _fake_turn(**kwargs):
        seen.update(kwargs)
        kwargs["messages_out"].append({
            "type": "tool_use",
            "tool_name": "return_final_response",
            "tool_input": {"response": "done"},
        })

    monkeypatch.setattr(gemini_api, "run_conversation_turn", _fake_turn)

    result = _run(inference_api.run_inference(None, owner, "hello", None))
    assert result == {"conversation_id": "conv-inference-1", "response": "done"}

    run_user = seen["user"]
    assert run_user["api_key"] == ""
    assert run_user["id"] == owner["id"]
    assert run_user["email"] == owner["email"]
    assert seen["origin"] == "inference_api"
    # The caller's dict is not mutated.
    assert owner["api_key"].startswith("owner-wide-api-key-")

    # And the prompt built from the scrubbed key carries no bearer.
    from chat.gemini_api.system_prompt import get_inference_api_system_prompt
    prompt = get_inference_api_system_prompt(
        run_user["api_key"], user_email=owner["email"],
    )
    assert owner["api_key"] not in prompt


# ---------------------------------------------------------------------------
# Inference runs must not change anything: mutating-tool classification
# ---------------------------------------------------------------------------

# Every core tool_call-routed tool, classified. A new registry entry must be
# added to exactly one of these so the inference-run gate is a deliberate
# decision, not an omission.
_CORE_READ_OR_WORKSPACE_TOOLS = {
    "get_current_time",
    "list_workspace_files", "get_workspace_file",
    "write_workspace_file", "edit_workspace_file",  # workspace-local only
    "memory_search", "memory_list",
    "wait_for_handles",
    "download_drive_file", "google_export_doc",  # external read -> workspace
    "google_export_sheet", "google_export_slides",
    "list_gmail_quest_labels", "get_gmail_messages", "list_gmail_labels",
    "get_gmail_message_urls",
    "set_conversation_name",  # the run's own row
    "project_db_query",
    "authed_get", "authed_post", "get_response_content",
    "telegram_get_me", "telegram_list_dialogs", "telegram_get_messages",
    "telegram_list_contacts",
    "list_docs", "search_docs", "read_doc",  # Quest Docs reads
}
_CORE_MUTATING_TOOLS = {
    "archive_gmail_message", "modify_gmail_labels",
    "create_gmail_draft", "send_gmail_to_self",
    # Quest Docs writes (no card when free; inference runs are read-only)
    "create_doc", "edit_doc", "append_to_doc", "add_doc_image",
}


def test_every_core_dynamic_tool_is_classified():
    from chat.llm.tool_schemas import (
        PLUGIN_TOOL_NAMES, TOOL_CALL_REGISTRY, mutating_tool_call_tools,
    )

    known = _CORE_READ_OR_WORKSPACE_TOOLS | _CORE_MUTATING_TOOLS
    assert not (_CORE_READ_OR_WORKSPACE_TOOLS & _CORE_MUTATING_TOOLS)
    core = set(TOOL_CALL_REGISTRY) - PLUGIN_TOOL_NAMES
    unclassified = core - known
    assert not unclassified, f"classify these dynamic tools: {sorted(unclassified)}"
    assert mutating_tool_call_tools() & core == _CORE_MUTATING_TOOLS


def test_plugin_self_send_tools_are_mutating(slack_plugin):
    from chat.llm.tool_schemas import mutating_tool_call_tools
    assert "send_slack_dm_to_self" in mutating_tool_call_tools()
    # Reads stay read.
    assert "list_slack_teams" not in mutating_tool_call_tools()


def test_mutating_proxy_paths_cover_the_write_routes():
    from chat.llm.tool_schemas import MUTATING_PROXY_PATHS
    assert {"/api/gmail-simple/drafts", "/api/gmail-simple/send-self"} <= MUTATING_PROXY_PATHS


class TestInferenceDispatchRejectsMutations:
    def _dispatch(self, tool_name, args, **kw):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call_inner
        return _run(_dispatch_tool_call_inner(
            app=None, provider=None,
            user={"id": 1, "email": "u@example.com", "api_key": ""},
            conversation_id="conv-1", timezone="UTC",
            tool_name=tool_name, args=args, is_inference_api=True, **kw,
        ))

    @pytest.mark.parametrize("name", sorted(_CORE_MUTATING_TOOLS))
    def test_mutating_dynamic_tool_rejected(self, name):
        import json
        result, extra = self._dispatch(
            "tool_call", {"tool_name": name, "arguments": {"message_id": "x"}},
        )
        parsed = json.loads(result)
        assert "not available in inference API runs" in parsed["error"]
        assert extra == []

    def test_plugin_mutating_tool_rejected(self, slack_plugin):
        import json
        result, _ = self._dispatch(
            "tool_call",
            {"tool_name": "send_slack_dm_to_self", "arguments": {"message": "hi"}},
        )
        assert "not available in inference API runs" in json.loads(result)["error"]

    def test_read_tool_passes_gate(self):
        import json
        result, _ = self._dispatch(
            "tool_call", {"tool_name": "get_current_time", "arguments": {}},
        )
        parsed = json.loads(result)
        assert "error" not in parsed
        assert "utc" in json.dumps(parsed).lower()

    def test_mutating_proxy_path_rejected(self):
        import json
        from chat.route_dispatch import execute_tool_call
        for path in ("/api/gmail-simple/drafts", "/api/gmail-simple/send-self", "/api/reset-api-key"):
            result, notices = _run(execute_tool_call(
                app=None, user={"id": 1}, tool_name="curl_proxy_post",
                tool_args={"url": f"http://localhost:8000{path}", "body": "{}"},
                block_mutating=True,
            ))
            assert "not available in inference API runs" in json.loads(result)["error"], path
            assert notices == []

    def test_normal_conversations_are_unaffected(self):
        """Without the flag the same call reaches the handler (and fails on
        the missing Gmail credentials rather than on the gate)."""
        import json
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call_inner
        result, _ = _run(_dispatch_tool_call_inner(
            app=None, provider=None,
            user={"id": 1, "email": "u@example.com", "api_key": "k"},
            conversation_id="conv-1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "list_gmail_quest_labels", "arguments": {}},
        ))
        assert "not available in inference API runs" not in result

    def test_sandbox_launch_carries_the_restriction(self):
        """run_script / run_python from an inference run mint a lease whose
        block_mutating_tools flag the sandbox tool API enforces."""
        import inspect
        from chat.gemini_api import tool_dispatch
        for wrapper in (tool_dispatch._tool_run_script, tool_dispatch._tool_run_python):
            assert "block_mutating_tools=ctx.is_inference_api" in inspect.getsource(wrapper)


def test_inference_prompt_hides_mutating_tools_and_states_the_boundary(slack_plugin):
    from chat.gemini_api.system_prompt import get_inference_api_system_prompt
    from chat.llm.tool_schemas import mutating_tool_call_tools

    prompt = get_inference_api_system_prompt(
        "", user_email="u@example.com",
        connected_services={"gmail": True, "slack": True, "telegram": True},
    )
    for name in mutating_tool_call_tools():
        assert f"**{name}**" not in prompt, name
    # Reads are still advertised.
    assert "get_gmail_messages" in prompt
    assert "READ-ONLY" in prompt
