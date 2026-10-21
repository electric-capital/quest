"""Tests for inline service connect requests (chat/connect_requests.py).

Covers the tool spec, the row resolution and prompt paragraph, the
``request_service_connection`` dispatch arm (origin guards, the wait
handle + card it writes, the suspend sentinel), the resolve endpoint
(verify-and-accept vs decline), the resume gates and the card rewrite.
"""

import asyncio
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat import connect_requests


def _run(coro):
    return asyncio.run(coro)


GOOGLE = {
    "service": "google_services", "label": "Google Services",
    "description": "Gmail, Calendar, Drive, Docs, Sheets", "kind": "oauth",
    "connect_url": "/auth/google-services?popup=1", "connected": False,
    "needs_reauth": False, "available": True,
}
AIRTABLE = {
    "service": "airtable", "label": "Airtable", "kind": "api_key",
    "connected": False, "key_url": "/auth/airtable/save-token",
    "key_hint": None,
}
SLACK_CONNECTED = {
    "service": "slack", "label": "Slack", "kind": "oauth",
    "connect_url": "/auth/slack?popup=1", "connected": True,
    "needs_reauth": False, "available": True,
}
RAMP_UNAVAILABLE = {
    "service": "ramp", "label": "Ramp", "kind": "oauth",
    "connect_url": "/auth/ramp?popup=1", "connected": False,
    "available": False,
}
GITHUB_REAUTH = {
    "service": "github", "label": "GitHub", "kind": "oauth",
    "connect_url": "/auth/github?popup=1", "connected": True,
    "needs_reauth": True, "available": True,
}


@pytest.fixture()
def rows(monkeypatch):
    """Patch the connector rows the module reads; tests mutate the list."""
    current = [dict(GOOGLE), dict(AIRTABLE), dict(SLACK_CONNECTED),
               dict(RAMP_UNAVAILABLE), dict(GITHUB_REAUTH)]
    import chat.routes.user as user_routes
    monkeypatch.setattr(
        user_routes, "build_connector_rows", lambda user: [dict(r) for r in current],
    )
    return current


# ---------------------------------------------------------------------------
# Tool spec
# ---------------------------------------------------------------------------


def test_tool_spec_is_core_and_requires_service_and_reason():
    from chat.llm.tool_schemas import CORE_TOOL_CALL_TOOLS, TOOL_CALL_REGISTRY

    spec = TOOL_CALL_REGISTRY["request_service_connection"]
    assert spec["core"] is True
    assert "request_service_connection" in CORE_TOOL_CALL_TOOLS
    assert set(spec["parameters"]["required"]) == {"service", "reason"}
    assert "BLOCKS" in spec["description"]


# ---------------------------------------------------------------------------
# Row resolution + prompt paragraph
# ---------------------------------------------------------------------------


def test_connectable_rows_skip_connected_and_unavailable(rows):
    ids = [r["service"] for r in connect_requests.connectable_rows({})]
    assert ids == ["google_services", "airtable", "github"]


def test_find_connect_target_errors(rows):
    row, err = connect_requests.find_connect_target({}, "")
    assert row is None and err["error"] == "missing_service"
    assert err["connectable_services"] == ["google_services", "airtable", "github"]

    row, err = connect_requests.find_connect_target({}, "ramp")
    assert row is None and err["error"] == "unknown_service"

    row, err = connect_requests.find_connect_target({}, "nope")
    assert row is None and err["error"] == "unknown_service"

    row, err = connect_requests.find_connect_target({}, "slack")
    assert row is None and err["error"] == "already_connected"


def test_find_connect_target_accepts_connectable_and_reauth(rows):
    row, err = connect_requests.find_connect_target({}, "google_services")
    assert err is None and row["service"] == "google_services"
    row, err = connect_requests.find_connect_target({}, "github")
    assert err is None and row["service"] == "github"


def test_card_fields_carry_popup_url_only_for_oauth(rows):
    fields = connect_requests.card_fields(dict(GOOGLE), "  To search your inbox  ")
    assert fields["connect_url"] == "/auth/google-services?popup=1"
    assert fields["reason"] == "To search your inbox"
    assert fields["needs_reauth"] is False
    assert fields["kind"] == "oauth"

    fields = connect_requests.card_fields(dict(AIRTABLE), "x" * 500)
    assert "connect_url" not in fields
    assert len(fields["reason"]) == connect_requests.MAX_REASON_LEN
    assert "key_hint" not in fields and "key_url" not in fields

    fields = connect_requests.card_fields(dict(GITHUB_REAUTH), "r")
    assert fields["needs_reauth"] is True


def test_paragraph_lists_connectable_services_and_the_tool(rows):
    text = connect_requests.build_connectable_services_paragraph({})
    assert text.startswith("**Connectable services**")
    assert "Google Services (`google_services`: Gmail, Calendar" in text
    assert "Airtable (`airtable`)" in text
    assert "GitHub (`github`) -- connected but needs re-authorization" in text
    assert "Slack" not in text and "Ramp" not in text
    assert 'request_service_connection' in text


def test_paragraph_empty_when_nothing_connectable(rows):
    rows[:] = [dict(SLACK_CONNECTED), dict(RAMP_UNAVAILABLE)]
    assert connect_requests.build_connectable_services_paragraph({}) == ""


def test_paragraph_swallows_row_failures(monkeypatch):
    import chat.routes.user as user_routes

    def _boom(user):
        raise RuntimeError("no plugins")

    monkeypatch.setattr(user_routes, "build_connector_rows", _boom)
    assert connect_requests.build_connectable_services_paragraph({}) == ""


def test_system_prompt_renders_paragraph_only_at_top_level():
    from chat.gemini_api.system_prompt import (
        get_sub_agent_system_prompt,
        get_system_prompt,
    )

    para = "**Connectable services** PARA"
    top = get_system_prompt("k", connected_services={}, connectable_services=para)
    assert para in top
    assert "request_service_connection" in top

    slack = get_system_prompt("k", connected_services={}, is_slack=True)
    assert "`request_service_connection`" not in slack

    sub = get_sub_agent_system_prompt("agent", "k", connected_services={})
    assert "`request_service_connection`" not in sub


def test_skill_loader_not_connected_error_names_the_tool():
    from chat.system_skills.loader import load_system_skills

    results = load_system_skills(
        ["system:gmail"], base_url="http://x", api_key="k",
        connected_services={"google_services": False}, has_project=False,
    )
    assert results and "request_service_connection" in results[0]["error"]
    assert '"service": "google_services"' in results[0]["error"]


# ---------------------------------------------------------------------------
# Dispatch arm
# ---------------------------------------------------------------------------


@pytest.fixture()
def _user_id(isolated_db):
    from db.engine import AsyncSessionLocal

    async def _create():
        async with AsyncSessionLocal() as db:
            u = isolated_db.models.User(
                email=f"connect-{uuid.uuid4().hex}@example.com",
                api_key=f"k-{uuid.uuid4().hex}",
                name="Connect Tester",
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            return u.id

    return _run(_create())


def _make_ctx(user_id, events, messages, **overrides):
    from chat.gemini_api.run_context import RunContext
    from chat.gemini_api.usage import UsageAccumulator

    async def _on_event(event):
        events.append(event)

    fields = dict(
        app=None,
        user={"id": user_id, "email": "u@example.com"},
        conversation_id="conv-1",
        timezone="UTC",
        model="test-model",
        origin="web",
        project_id=None,
        routine_id=None,
        slack_context=None,
        provider=MagicMock(),
        provider_name="anthropic",
        chat=MagicMock(),
        is_slack_origin=False,
        is_user_subagent=False,
        is_inference_api=False,
        is_public=False,
        nested_subagents=False,
        user_subagents_enabled=False,
        user_subagents_gate_open=True,
        subagent_run=None,
        subagent_caller=None,
        custom_prompt="",
        resolved_project_guide="",
        resolved_skills_content="",
        on_event=_on_event,
        structured_messages=messages,
        usage_acc=UsageAccumulator(),
    )
    fields.update(overrides)
    return RunContext(**fields)


def _call(service, reason="To search your inbox", tool_id="tu-1"):
    from chat.gemini_api.turn_tools import (
        REQUEST_SERVICE_CONNECTION_KEY,
        ToolCall,
    )

    return ToolCall(
        name="tool_call",
        key=REQUEST_SERVICE_CONNECTION_KEY,
        tool_id=tool_id,
        raw_tool_id=tool_id,
        args={
            "tool_name": "request_service_connection",
            "arguments": {"service": service, "reason": reason},
        },
    )


def _dispatch(ctx, call):
    import db.tool_wait_handle_store as fresh_store
    from chat.gemini_api.turn_tools import (
        TurnState,
        _handle_request_service_connection,
    )

    with patch("chat.gemini_api.turn_tools.tool_wait_handle_store", fresh_store):
        return _run(_handle_request_service_connection(ctx, call, TurnState()))


@pytest.mark.parametrize("overrides, needle", [
    ({"is_slack_origin": True, "origin": "slack"}, "Slack-driven"),
    ({"is_user_subagent": True, "origin": "user_subagent"}, "no user watching"),
    ({"is_inference_api": True, "origin": "inference_api"}, "no user watching"),
    ({"routine_id": "r-1"}, "routine runs"),
])
def test_arm_refuses_run_kinds_without_a_card(rows, _user_id, overrides, needle):
    events, messages = [], []
    ctx = _make_ctx(_user_id, events, messages, **overrides)
    result = json.loads(_dispatch(ctx, _call("google_services")))
    assert needle in result["error"]
    assert events == [] and messages == []


def test_arm_returns_row_errors_without_a_card(rows, _user_id):
    events, messages = [], []
    ctx = _make_ctx(_user_id, events, messages)
    assert json.loads(_dispatch(ctx, _call("slack")))["error"] == "already_connected"
    assert json.loads(_dispatch(ctx, _call("ramp")))["error"] == "unknown_service"
    assert json.loads(_dispatch(ctx, _call("")))["error"] == "missing_service"
    assert events == [] and messages == []


def test_arm_opens_card_and_suspends(rows, _user_id):
    import db.tool_wait_handle_store as store
    from chat.gemini_api.turn_tools import SuspendForServiceConnection

    events, messages = [], []
    ctx = _make_ctx(_user_id, events, messages)
    with pytest.raises(SuspendForServiceConnection) as excinfo:
        _dispatch(ctx, _call("Google_Services", reason="To find the invoice"))

    exc = excinfo.value
    assert exc.service == "google_services" and exc.tool_id == "tu-1"
    handle = _run(store.get_handle_for_user(_user_id, exc.handle_id))
    assert handle["kind"] == "service_connection"
    assert handle["status"] == "pending"
    assert handle["tool_id"] == "tu-1"
    assert handle["payload"]["service"] == "google_services"
    assert handle["payload"]["connect_url"] == "/auth/google-services?popup=1"

    # Persist-before-emit: the structured message and the event agree.
    assert len(messages) == 1 and len(events) == 1
    msg = messages[0]
    assert msg["type"] == "connect_request"
    assert msg["wait_handle_id"] == exc.handle_id
    assert msg["status"] == "open"
    assert msg["label"] == "Google Services"
    assert msg["reason"] == "To find the invoice"
    assert "timestamp" in msg
    assert events[0]["type"] == "connect_request"
    assert events[0]["wait_handle_id"] == exc.handle_id


# ---------------------------------------------------------------------------
# Resolve endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def route_env(isolated_db, monkeypatch, rows):
    import chat.connect_request_routes as routes_mod
    import db.tool_wait_handle_store as store_mod
    from chat.storage import ChatStorage

    kicks: list = []
    events: list = []
    rewrites: list = []
    invalidated: list = []
    monkeypatch.setattr(
        routes_mod.wait_resume, "maybe_kick_resume",
        lambda app, user, cid: kicks.append(cid),
    )
    monkeypatch.setattr(
        routes_mod.bus, "publish_to_user",
        lambda user_id, event: events.append(event),
    )
    monkeypatch.setattr(
        ChatStorage, "update_connect_request_message",
        staticmethod(lambda **kwargs: rewrites.append(kwargs)),
    )
    import chat.gemini_api as gemini_api
    monkeypatch.setattr(
        gemini_api, "invalidate_user_sessions",
        lambda user_id: invalidated.append(user_id),
    )
    return {
        "routes": routes_mod, "store": store_mod, "rows": rows,
        "kicks": kicks, "events": events, "rewrites": rewrites,
        "invalidated": invalidated,
    }


def _client(env, user):
    routes_mod = env["routes"]
    app = FastAPI()
    app.include_router(routes_mod.router)

    async def _current():
        return user

    app.dependency_overrides[routes_mod.get_current_user_cookie_or_apikey_checked] = _current
    return TestClient(app)


def _open_handle(env, user_id, service="google_services", kind="service_connection"):
    return _run(env["store"].create_handle(
        user_id=user_id, conversation_id="conv-1", kind=kind,
        tool_id=f"tu-{uuid.uuid4().hex[:6]}",
        payload={"service": service, "label": "Google Services", "kind": "oauth"},
    ))


def test_decline_resolves_rejected_and_kicks(route_env, _user_id):
    user = {"id": _user_id, "email": "u@example.com"}
    handle = _open_handle(route_env, _user_id)
    res = _client(route_env, user).post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "decline"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resolved"] is True and body["status"] == "declined"
    row = _run(route_env["store"].get_handle_for_user(_user_id, handle["id"]))
    assert row["status"] == "rejected"
    assert row["response"]["status"] == "declined"
    assert "do not offer" in row["response"]["note"]
    assert route_env["kicks"] == ["conv-1"]
    assert route_env["events"][0]["type"] == "wait_handle_resolved"
    assert route_env["events"][0]["handle_id"] == handle["id"]
    assert route_env["events"][0]["kind"] == "service_connection"
    assert route_env["rewrites"] == [{
        "conversation_id": "conv-1", "wait_handle_id": handle["id"],
        "status": "declined",
    }]
    assert route_env["invalidated"] == []


def test_check_leaves_handle_pending_while_unconnected(route_env, _user_id):
    user = {"id": _user_id, "email": "u@example.com"}
    handle = _open_handle(route_env, _user_id)
    res = _client(route_env, user).post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "check"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "resolved": False, "status": "not_connected", "service": "google_services",
    }
    row = _run(route_env["store"].get_handle_for_user(_user_id, handle["id"]))
    assert row["status"] == "pending"
    assert route_env["kicks"] == [] and route_env["events"] == []


def test_check_accepts_once_connected(route_env, _user_id):
    user = {"id": _user_id, "email": "u@example.com"}
    handle = _open_handle(route_env, _user_id)
    # The credential landed (via the OAuth callback) after the card opened.
    for row in route_env["rows"]:
        if row["service"] == "google_services":
            row["connected"] = True
    res = _client(route_env, user).post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "check"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resolved"] is True and body["status"] == "connected"
    row = _run(route_env["store"].get_handle_for_user(_user_id, handle["id"]))
    assert row["status"] == "accepted"
    assert row["response"]["status"] == "connected"
    assert row["response"]["service"] == "google_services"
    assert route_env["kicks"] == ["conv-1"]
    assert route_env["invalidated"] == [_user_id]
    assert route_env["rewrites"][0]["status"] == "connected"


def test_check_treats_needs_reauth_as_unconnected(route_env, _user_id):
    user = {"id": _user_id, "email": "u@example.com"}
    handle = _open_handle(route_env, _user_id, service="github")
    res = _client(route_env, user).post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "check"},
    )
    assert res.json()["resolved"] is False
    for row in route_env["rows"]:
        if row["service"] == "github":
            row["needs_reauth"] = False
    res = _client(route_env, user).post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "check"},
    )
    assert res.json()["status"] == "connected"


def test_resolve_404_for_other_kinds_and_users(route_env, _user_id, isolated_db):
    user = {"id": _user_id, "email": "u@example.com"}
    other = _open_handle(route_env, _user_id, kind="action_request")
    res = _client(route_env, user).post(
        f"/app/api/connect-requests/{other['id']}/resolve", json={"action": "decline"},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "connect_request_not_found"

    stranger = {"id": _user_id + 1000, "email": "s@example.com"}
    mine = _open_handle(route_env, _user_id)
    res = _client(route_env, stranger).post(
        f"/app/api/connect-requests/{mine['id']}/resolve", json={"action": "decline"},
    )
    assert res.status_code == 404
    assert _run(route_env["store"].get_handle_for_user(_user_id, mine["id"]))["status"] == "pending"


def test_resolve_400_when_already_resolved(route_env, _user_id):
    user = {"id": _user_id, "email": "u@example.com"}
    handle = _open_handle(route_env, _user_id)
    client = _client(route_env, user)
    assert client.post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "decline"},
    ).status_code == 200
    res = client.post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "check"},
    )
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "already_resolved"
    assert res.json()["detail"]["status"] == "rejected"

    res = client.post(
        f"/app/api/connect-requests/{handle['id']}/resolve", json={"action": "approve"},
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Resume gates and card rewrite
# ---------------------------------------------------------------------------


def test_resume_gates_recognise_the_new_kind_and_tool(monkeypatch):
    from chat.wait_handles import resume

    assert "service_connection" in resume._ONE_ROW_PER_TOOL_USE_KINDS
    monkeypatch.setattr(
        resume, "_pending_tool_uses",
        lambda cid: [("tu-1", "tool_call", {
            "tool_name": "request_service_connection", "arguments": {},
        })],
    )
    assert resume._conversation_has_dangling_wait("conv-1") is True
    monkeypatch.setattr(
        resume, "_pending_tool_uses",
        lambda cid: [("tu-1", "tool_call", {"tool_name": "get_current_time"})],
    )
    assert resume._conversation_has_dangling_wait("conv-1") is False


def test_flush_types_and_tail_rewrite_cover_the_card():
    from chat._flush_helper import FLUSH_EVENT_TYPES
    from chat.routes.conversations import _REWRITTEN_IN_PLACE_TYPES

    assert "connect_request" in FLUSH_EVENT_TYPES
    assert "connect_request" in _REWRITTEN_IN_PLACE_TYPES


def test_update_connect_request_message_rewrites_status(tmp_path, monkeypatch):
    from chat.storage import ChatStorage

    chat_file = tmp_path / "chat_history.json"
    chat_file.write_text(json.dumps({"messages": [
        {"type": "text", "content": "hi"},
        {"type": "connect_request", "wait_handle_id": "wh-1", "status": "open"},
        {"type": "connect_request", "wait_handle_id": "wh-2", "status": "open"},
    ]}))
    monkeypatch.setattr(
        ChatStorage, "_get_chat_history_file",
        staticmethod(lambda cid: chat_file),
    )
    ChatStorage.update_connect_request_message(
        conversation_id="c", wait_handle_id="wh-2", status="declined",
    )
    messages = json.loads(chat_file.read_text())["messages"]
    assert messages[1]["status"] == "open"
    assert messages[2]["status"] == "declined"

    # Missing entry: silent no-op.
    ChatStorage.update_connect_request_message(
        conversation_id="c", wait_handle_id="nope", status="connected",
    )
    assert json.loads(chat_file.read_text())["messages"][2]["status"] == "declined"
