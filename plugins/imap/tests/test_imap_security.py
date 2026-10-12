"""Security properties of the imap_* tools (issue #84 spec 10.6 / 11.5).

- Read-only access: every FETCH the tools send uses PEEK items only, and
  messages stay un-\\Seen after searching, reading and saving attachments.
- Public projects: the tools are not in PUBLIC_TOOL_CALL_ALLOWLIST and a
  public conversation's dispatch refuses them.
- ``/api/imap`` is blocked in curl_proxy route dispatch.
- The script bridge exposes exactly the four read tools.
- Argument values cannot inject IMAP commands; the password never leaks
  into a tool result.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chat.gemini_api import tool_handlers
from chat.gemini_api.tool_dispatch import _dispatch_tool_call
from chat.llm.tool_schemas import PUBLIC_TOOL_CALL_ALLOWLIST

from plugins.imap import tools as tools_mod
from plugins.imap.tests.fake_imaplib import DEFAULT_PASSWORD, make_message

TOOL_NAMES = tuple(t.spec["name"] for t in tools_mod.ALL_TOOLS)
READ_TOOLS = (
    "imap_list_folders",
    "imap_search_messages",
    "imap_get_messages",
    "imap_get_message_urls",
)
LONG_URL = "https://tracking.example.net/click?campaign=october-2026&recipient=alice&sig=0123456789abcdef"

# A FETCH item that would set \Seen on a real server.
_SEEN_SETTING_ITEM = re.compile(r"\b(BODY|BINARY)\[|\bRFC822(?!\.SIZE\b|\.HEADER\b)", re.I)


def _run(coro):
    return asyncio.run(coro)


def _ctx(user, conversation_id="conv-1"):
    return SimpleNamespace(user=user, conversation_id=conversation_id, project_id=None)


def _call(handler, ctx, args):
    out = _run(handler(ctx, args))
    try:
        return json.loads(out)
    except ValueError:
        return out


@pytest.fixture()
def url_cache(tmp_path, monkeypatch):
    from api.gmail import helpers

    monkeypatch.setattr(helpers, "_get_url_cache_path", lambda cid: tmp_path / f"{cid}.sqlite")


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()

    async def _dir(conversation_id, project_id=None):
        return root

    with patch.object(tool_handlers, "conversation_workspace_dir", _dir), \
            patch.object(tool_handlers, "_publish_file_list_changed"):
        yield root


# ---------------------------------------------------------------------------
# PEEK only: nothing the tools do marks a message read
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_no_tool_sets_seen(self, fake_imap, url_cache, workspace):
        html = f'<p>Pay <a href="{LONG_URL}">here</a></p>'
        mb = fake_imap.mailbox
        uids = [
            mb.add_message("INBOX", make_message(subject="Plain")),
            mb.add_message("INBOX", make_message(body="\n", html=html)),
            mb.add_message("INBOX", make_message(subject="Files", attachments=(
                ("a.pdf", "application/pdf", b"%PDF-1.4 data"),))),
        ]
        user = fake_imap.user([fake_imap.row()])
        ctx = _ctx(user)
        assert _call(tools_mod._handle_list_folders, ctx, {})["count"] == 1
        assert _call(tools_mod._handle_search_messages, ctx, {"unseen": True})["total_matches"] == 3
        assert isinstance(_call(tools_mod._handle_get_messages, ctx,
                                {"folder": "INBOX", "uids": uids}), str)
        _call(tools_mod._handle_get_messages, ctx,
              {"folder": "INBOX", "uids": uids[:1], "include_html": True})
        assert _call(tools_mod._handle_save_attachment, ctx,
                     {"folder": "INBOX", "uid": uids[2], "attachment_index": 1})["status"] == "success"

        for uid in uids:
            assert "\\Seen" not in mb.folders["INBOX"].messages[uid].flags
        fetches = [c for c in fake_imap.server.commands if c[0] == "UID FETCH"]
        assert fetches, "expected some FETCH commands"
        for command in fetches:
            items = re.sub(r"\b(BODY|BINARY)\.PEEK\[", "", command[2], flags=re.I)
            assert not _SEEN_SETTING_ITEM.search(items), command
        # folders are opened read-only (EXAMINE), never SELECTed
        assert not any(c[0] == "SELECT" for c in fake_imap.server.commands)
        assert any(c[0] == "EXAMINE" for c in fake_imap.server.commands)

    def test_huge_message_path_is_peek_too(self, fake_imap, monkeypatch, url_cache):
        monkeypatch.setattr(tools_mod, "MAX_RENDER_MESSAGE_BYTES", 100)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(subject="Huge"))
        out = _call(tools_mod._handle_get_messages, _ctx(fake_imap.user([fake_imap.row()])),
                    {"folder": "INBOX", "uids": [uid]})
        assert "Subject: Huge" in out
        assert "\\Seen" not in fake_imap.mailbox.folders["INBOX"].messages[uid].flags

    def test_no_mutating_tools(self):
        assert not any(t.mutating for t in tools_mod.ALL_TOOLS)


# ---------------------------------------------------------------------------
# Public projects, proxy block, script bridge
# ---------------------------------------------------------------------------


class TestExposure:
    def test_not_in_public_allowlist(self, imap_plugin):
        for name in TOOL_NAMES:
            assert name not in PUBLIC_TOOL_CALL_ALLOWLIST, name

    def test_public_conversation_dispatch_refuses(self, imap_plugin, fake_imap):
        result_text, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user=fake_imap.user([fake_imap.row()]),
            conversation_id="c1", timezone="UTC", tool_name="tool_call",
            args={"tool_name": "imap_list_folders", "arguments": {}},
            is_public=True, project_id="p1",
        ))
        assert "not available in public-project" in json.loads(result_text)["error"]
        assert fake_imap.server.commands == []

    def test_api_imap_in_blocked_paths(self):
        from chat.route_dispatch import _BLOCKED_PROXY_PATHS

        assert "/api/imap" in _BLOCKED_PROXY_PATHS

    @pytest.mark.parametrize("tool_name, url", [
        ("curl_proxy_get", "http://localhost:8000/api/imap/folders"),
        ("curl_proxy_get", "http://localhost:8000/api/imap"),
        ("curl_proxy_post", "http://localhost:8000/api/imap/messages?folder=INBOX"),
    ])
    def test_curl_proxy_on_api_imap_is_blocked(self, tool_name, url):
        from chat.route_dispatch import execute_tool_call

        result, notices = _run(execute_tool_call(
            app=object(),  # the block fires before route resolution
            user={"id": 1, "email": "u@example.com"},
            tool_name=tool_name,
            tool_args={"url": url},
        ))
        assert "dedicated tool" in json.loads(result)["error"]
        assert notices == []

    def test_script_bridge_has_exactly_the_read_tools(self, imap_plugin):
        from chat.gemini_api import script_tool_call

        allowed = {n for n in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST if n.startswith("imap_")}
        assert allowed == set(READ_TOOLS)
        assert "imap_save_attachment" not in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST


# ---------------------------------------------------------------------------
# Injection and secrets
# ---------------------------------------------------------------------------


class TestInjection:
    @pytest.mark.parametrize("handler, args", [
        (tools_mod._handle_search_messages, {"folder": "INBOX\r\nA1 DELETE INBOX"}),
        (tools_mod._handle_search_messages, {"subject": "x\nA1 LOGOUT"}),
        (tools_mod._handle_search_messages, {"text": "nul\x00byte"}),
        (tools_mod._handle_get_messages, {"folder": "INBOX\nX", "uids": [1]}),
        (tools_mod._handle_save_attachment,
         {"folder": "INBOX", "uid": 1, "filename": "a\r\nb"}),
    ])
    def test_line_breaks_rejected_before_connecting(self, fake_imap, handler, args):
        out = _call(handler, _ctx(fake_imap.user([fake_imap.row()])), args)
        assert out["error"] == "invalid_arguments"
        assert fake_imap.server.commands == []

    def test_quotes_in_criteria_are_escaped(self, fake_imap):
        fake_imap.mailbox.add_message("INBOX", make_message(subject='Say "hi" \\ there'))
        out = _call(tools_mod._handle_search_messages, _ctx(fake_imap.user([fake_imap.row()])),
                    {"subject": '"hi" \\'})
        assert out["total_matches"] == 1
        searches = [c for c in fake_imap.server.commands if c[0] == "UID SEARCH"]
        assert any('"\\"hi\\" \\\\"' in " ".join(c[1:]) for c in searches)

    def test_folder_name_is_quoted(self, fake_imap):
        fake_imap.mailbox.add_folder('Odd "name"')
        out = _call(tools_mod._handle_search_messages, _ctx(fake_imap.user([fake_imap.row()])),
                    {"folder": 'Odd "name"'})
        assert out["total_matches"] == 0 and out["folder"] == 'Odd "name"'

    def test_password_never_in_results(self, fake_imap, url_cache):
        uid = fake_imap.mailbox.add_message("INBOX", make_message())
        user = fake_imap.user([fake_imap.row()])
        outputs = [
            _run(tools_mod._handle_list_folders(_ctx(user), {})),
            _run(tools_mod._handle_search_messages(_ctx(user), {})),
            _run(tools_mod._handle_get_messages(_ctx(user), {"folder": "INBOX", "uids": [uid]})),
        ]
        fake_imap.server.auth_fail = True
        outputs.append(_run(tools_mod._handle_list_folders(_ctx(user), {})))
        for out in outputs:
            assert DEFAULT_PASSWORD not in out

    def test_named_connection_cannot_reach_other_users_rows(self, imap_plugin, fake_imap):
        # The resolver only ever sees the calling user's rows.
        mine = fake_imap.user([fake_imap.row()])
        out = _call(tools_mod._handle_list_folders, _ctx(mine), {"connection": "someone@else.org"})
        assert out["error"] == "connection_not_found"
        assert out["available"] == ["alice@example.com"]
