"""Tests for the browser tool handlers (chat/gemini_api/tool_handlers/browser.py)
and their place in the public tool tier.

The session manager is replaced by a recording fake: these tests cover
the refusals (non-public conversation, closed gate), the relay of each
tool's arguments to its agent action, error relaying, the screenshot
written into the conversation workspace and attached inline, and the
dispatch path in both conversation modes.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

import config.feature_gates as fg
from chat.browser_sessions import BrowserError
from chat.gemini_api.tool_dispatch import ToolContext
from chat.gemini_api.tool_handlers import browser as handlers

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _run(coro):
    return asyncio.run(coro)


class FakeProvider:
    def __init__(self):
        self.uploads = []

    async def upload_file(self, file_path, mime_type, display_name="", model=""):
        self.uploads.append((file_path, mime_type))
        return {"type": "image", "path": file_path}

    def make_file_part(self, ref):
        return ("part", ref)


class FakeSessions:
    def __init__(self):
        self.calls = []
        self.stopped = []
        self.response = {"url": "https://x/", "title": "X", "snapshot": "- link [ref=e1]"}
        self.raise_error = None
        self.session = SimpleNamespace(calls=3)

    async def call(self, conversation_id, user_id, action, params):
        self.calls.append((conversation_id, user_id, action, params))
        if self.raise_error:
            raise self.raise_error
        return dict(self.response)

    async def stop(self, conversation_id, reason=""):
        self.stopped.append((conversation_id, reason))
        return True

    def get(self, conversation_id):
        return self.session


@pytest.fixture
def gates_file(tmp_path, monkeypatch):
    path = tmp_path / "feature_gates.json"
    monkeypatch.setattr(fg, "FEATURE_GATES_FILE", path)
    return path


@pytest.fixture
def sessions(monkeypatch):
    fake = FakeSessions()
    monkeypatch.setattr(handlers, "browser_sessions", fake)
    return fake


def _ctx(is_public=True, provider=None, conversation_id="conv-1"):
    return ToolContext(
        app=None, provider=provider or FakeProvider(),
        user={"id": 7, "email": "ada@example.com", "api_key": ""},
        conversation_id=conversation_id, timezone="UTC",
        project_id="proj-1", model="m", is_public=is_public,
    )


def _open_gate(email="ada@example.com"):
    fg.set_feature_enabled(fg.FEATURE_BROWSER, True)


class TestRefusals:
    def test_non_public_conversation(self, gates_file, sessions):
        _open_gate()
        result = json.loads(_run(handlers.handle_browser_navigate(_ctx(is_public=False), {"url": "https://x"})))
        assert result["error"] == "browser_unavailable"
        assert "public project" in result["message"]
        assert sessions.calls == []

    def test_gate_closed(self, gates_file, sessions):
        result = json.loads(_run(handlers.handle_browser_navigate(_ctx(), {"url": "https://x"})))
        assert result["error"] == "browser_disabled"
        assert "Settings > Features" in result["message"]
        assert sessions.calls == []

    def test_gate_closed_for_this_user(self, gates_file, sessions):
        fg.set_feature_enabled(fg.FEATURE_BROWSER, True)
        fg.set_feature_allowed_users(fg.FEATURE_BROWSER, ["other@example.com"])
        result = json.loads(_run(handlers.handle_browser_snapshot(_ctx(), {})))
        assert result["error"] == "browser_disabled"

    def test_close_is_refused_too(self, gates_file, sessions):
        result = json.loads(_run(handlers.handle_browser_close(_ctx(is_public=False), {})))
        assert result["error"] == "browser_unavailable"
        assert sessions.stopped == []


class TestRelay:
    @pytest.mark.parametrize("handler, args, action, params", [
        (handlers.handle_browser_navigate, {"url": "https://x"}, "navigate", {"url": "https://x"}),
        (handlers.handle_browser_snapshot, {}, "snapshot", {}),
        (handlers.handle_browser_click, {"ref": "e3", "double": True}, "click", {"ref": "e3", "double": True}),
        (handlers.handle_browser_click, {"ref": "e3"}, "click", {"ref": "e3", "double": False}),
        (handlers.handle_browser_type, {"ref": "e2", "text": "hi", "submit": True}, "type",
         {"ref": "e2", "text": "hi", "submit": True, "append": False}),
        (handlers.handle_browser_select, {"ref": "e2", "values": ["a"]}, "select", {"ref": "e2", "values": ["a"]}),
        (handlers.handle_browser_press, {"key": "Enter"}, "press", {"key": "Enter"}),
        (handlers.handle_browser_hover, {"ref": "e9"}, "hover", {"ref": "e9"}),
        (handlers.handle_browser_scroll, {}, "scroll", {"amount": 600}),
        (handlers.handle_browser_scroll, {"amount": -200, "ref": "e4"}, "scroll", {"amount": -200, "ref": "e4"}),
        (handlers.handle_browser_back, {}, "back", {}),
        (handlers.handle_browser_wait, {"text": "Done"}, "wait", {"text": "Done"}),
        (handlers.handle_browser_wait, {"seconds": 2}, "wait", {"seconds": 2}),
        (handlers.handle_browser_wait, {}, "wait", {}),
        (handlers.handle_browser_evaluate, {"expression": "1+1"}, "evaluate", {"expression": "1+1"}),
    ])
    def test_arguments_reach_the_agent(self, gates_file, sessions, handler, args, action, params):
        _open_gate()
        result = json.loads(_run(handler(_ctx(), args)))
        assert result == sessions.response
        assert sessions.calls == [("conv-1", 7, action, params)]

    def test_agent_error_is_relayed(self, gates_file, sessions):
        _open_gate()
        sessions.response = {"error": "TimeoutError: Page.goto: Timeout 30000ms exceeded."}
        result = json.loads(_run(handlers.handle_browser_navigate(_ctx(), {"url": "https://slow"})))
        assert result["error"].startswith("TimeoutError")

    def test_session_error_is_relayed(self, gates_file, sessions):
        _open_gate()
        sessions.raise_error = BrowserError("The server's browser capacity is in use right now.")
        result = json.loads(_run(handlers.handle_browser_navigate(_ctx(), {"url": "https://x"})))
        assert result["error"] == "browser_error"
        assert "capacity" in result["message"]

    def test_close_stops_the_session(self, gates_file, sessions):
        _open_gate()
        result = json.loads(_run(handlers.handle_browser_close(_ctx(), {})))
        assert result["closed"] is True
        assert sessions.stopped == [("conv-1", "browser_close")]


class TestScreenshot:
    @pytest.fixture
    def workspace(self, tmp_path, monkeypatch):
        root = tmp_path / "workspace"
        root.mkdir()

        async def fake_root(conversation_id):
            return root

        monkeypatch.setattr(handlers, "conversation_workspace_dir", fake_root)
        return root

    def test_writes_png_and_attaches_it(self, gates_file, sessions, workspace):
        _open_gate()
        sessions.response = {"url": "https://x/a", "title": "My Page: v2", "png_base64": PNG, "full_page": False}
        provider = FakeProvider()
        text, parts = _run(handlers.handle_browser_screenshot(_ctx(provider=provider), {}))
        result = json.loads(text)
        assert sessions.calls == [("conv-1", 7, "screenshot", {"full_page": False})]
        assert result["path"] == "screenshots/My-Page-v2-3.png"
        assert (workspace / result["path"]).read_bytes() == base64.b64decode(PNG)
        assert result["size_bytes"] == len(base64.b64decode(PNG))
        assert parts == [("part", {"type": "image", "path": str(workspace / result["path"])})]
        assert provider.uploads[0][1] == "image/png"
        assert "attached" in result["note"]

    def test_custom_path_and_full_page(self, gates_file, sessions, workspace):
        _open_gate()
        sessions.response = {"url": "https://x/", "title": "", "png_base64": PNG, "full_page": True}
        text, _ = _run(handlers.handle_browser_screenshot(_ctx(), {"path": "shots/home", "full_page": True}))
        result = json.loads(text)
        assert sessions.calls[0][3] == {"full_page": True}
        assert result["path"] == "shots/home.png" and result["full_page"] is True
        assert (workspace / "shots" / "home.png").exists()

    def test_path_traversal_rejected(self, gates_file, sessions, workspace):
        _open_gate()
        sessions.response = {"url": "https://x/", "title": "", "png_base64": PNG}
        text, parts = _run(handlers.handle_browser_screenshot(_ctx(), {"path": "../evil.png"}))
        assert "traversal" in json.loads(text)["error"]
        assert parts == []
        assert not list(workspace.parent.glob("evil.png"))

    def test_provider_without_inline_images(self, gates_file, sessions, workspace):
        _open_gate()
        sessions.response = {"url": "https://x/", "title": "T", "png_base64": PNG}

        class NoUpload(FakeProvider):
            async def upload_file(self, *a, **k):
                return None

        text, parts = _run(handlers.handle_browser_screenshot(_ctx(provider=NoUpload()), {}))
        assert parts == []
        assert "could not be attached" in json.loads(text)["note"]

    def test_agent_error_writes_nothing(self, gates_file, sessions, workspace):
        _open_gate()
        sessions.response = {"error": "TargetClosedError: page closed"}
        text, parts = _run(handlers.handle_browser_screenshot(_ctx(), {}))
        assert json.loads(text)["error"].startswith("TargetClosedError")
        assert parts == [] and list(workspace.iterdir()) == []


class TestDispatch:
    def _dispatch(self, is_public, args):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call_inner
        return _run(_dispatch_tool_call_inner(
            app=None, provider=FakeProvider(),
            user={"id": 7, "email": "ada@example.com", "api_key": ""},
            conversation_id="conv-1", timezone="UTC", project_id="proj-1",
            tool_name="tool_call", args=args, is_public=is_public,
        ))

    def test_public_dispatch_reaches_the_handler(self, gates_file, sessions):
        _open_gate()
        result, _ = self._dispatch(True, {"tool_name": "browser_navigate", "arguments": {"url": "https://x"}})
        assert json.loads(result) == sessions.response
        assert sessions.calls[0][2] == "navigate"

    def test_private_dispatch_is_refused_by_the_handler(self, gates_file, sessions):
        _open_gate()
        result, _ = self._dispatch(False, {"tool_name": "browser_navigate", "arguments": {"url": "https://x"}})
        assert json.loads(result)["error"] == "browser_unavailable"
        assert sessions.calls == []

    def test_browser_tools_in_public_allowlist_and_registry(self):
        from chat.llm.tool_schemas import (
            PUBLIC_ONLY_TOOL_CALL_TOOLS, PUBLIC_TOOL_CALL_ALLOWLIST, TOOL_CALL_REGISTRY,
        )
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        names = {n for n in TOOL_CALL_REGISTRY if n.startswith("browser_")}
        assert names == PUBLIC_ONLY_TOOL_CALL_TOOLS
        assert names <= PUBLIC_TOOL_CALL_ALLOWLIST
        assert names <= set(TOOL_CALL_HANDLERS)
        assert names == {
            "browser_navigate", "browser_snapshot", "browser_click", "browser_type",
            "browser_select", "browser_press", "browser_hover", "browser_scroll",
            "browser_back", "browser_wait", "browser_screenshot", "browser_evaluate",
            "browser_close",
        }
        for name in names:
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["requires_service"] == "browser"
            assert spec.get("public_only") is True
            assert "intent_message" in spec["parameters"]["properties"]


class TestFeatureGate:
    def test_registered_with_per_user_access(self):
        assert fg.FEATURE_BROWSER in fg.KNOWN_FEATURES
        assert fg.FEATURE_BROWSER in fg.PER_USER_ACCESS_FEATURES
        assert fg.FEATURE_LABELS[fg.FEATURE_BROWSER]["label"]

    def test_browser_enabled_for(self, gates_file):
        assert fg.browser_enabled_for("ada@example.com") is False
        fg.set_feature_enabled(fg.FEATURE_BROWSER, True)
        assert fg.browser_enabled_for("ada@example.com") is True
        fg.set_feature_allowed_users(fg.FEATURE_BROWSER, ["Other@example.com"])
        assert fg.browser_enabled_for("ada@example.com") is False
        assert fg.browser_enabled_for("other@example.com") is True

    def test_regular_prompts_never_carry_the_key(self):
        # The connected-services map sets the browser pseudo-key False for
        # every user: only the public prompt (browser_enabled) offers them.
        from api.instructions import PSEUDO_SERVICE_KEYS, get_user_connected_services
        assert "browser" in PSEUDO_SERVICE_KEYS
        services = get_user_connected_services({"email": "ada@example.com"})
        assert services["browser"] is False
