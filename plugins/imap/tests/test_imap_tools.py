"""The five imap_* tools over the fake IMAP server (issue #84 phase 3).

Handlers are called directly with a SimpleNamespace ctx (the shape of
chat.gemini_api.tool_dispatch.ToolContext they read) and, at least once,
through the real dynamic tool dispatch path (``_dispatch_tool_call`` with
``tool_call``). Connection rows come from ``fake_imap.row`` /
``fake_imap.user`` and resolve through config.user_connections.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chat.gemini_api import tool_handlers
from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS, _dispatch_tool_call
from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

from plugins.imap import tools as tools_mod
from plugins.imap.render import list_attachments, parse_message
from plugins.imap.tests.fake_imaplib import FakeIMAP4, make_message

TOOL_NAMES = (
    "imap_list_folders",
    "imap_search_messages",
    "imap_get_messages",
    "imap_get_message_urls",
    "imap_save_attachment",
)
LONG_URL = "https://tracking.example.net/click?campaign=october-2026&recipient=alice&sig=0123456789abcdef"
PDF_BYTES = b"%PDF-1.4\n" + bytes(range(256)) * 4


def _run(coro):
    return asyncio.run(coro)


def _ctx(user, conversation_id="conv-1", project_id=None):
    return SimpleNamespace(user=user, conversation_id=conversation_id, project_id=project_id)


def _call(handler, ctx, args):
    out = _run(handler(ctx, args))
    try:
        return json.loads(out)
    except ValueError:
        return out


def _default_user(fake_imap):
    return fake_imap.user([fake_imap.row(is_default=True)])


@pytest.fixture()
def url_cache(tmp_path, monkeypatch):
    """Route the per-conversation URL cache into tmp_path."""
    from api.gmail import helpers

    monkeypatch.setattr(
        helpers, "_get_url_cache_path", lambda cid: tmp_path / "urls" / f"{cid}.sqlite",
    )
    return tmp_path / "urls"


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()

    async def _dir(conversation_id, project_id=None):
        return root

    with patch.object(tool_handlers, "conversation_workspace_dir", _dir), \
            patch.object(tool_handlers, "_publish_file_list_changed") as published:
        yield root, published


@pytest.fixture()
def two_accounts(fake_imap):
    """alice@example.com on imap.example.com (default) + bob@other.org on mail.other.org."""
    other = fake_imap.add_server("mail.other.org")
    mailbox = other.add_account("bob@other.org", "bob-pass")
    fake_imap.admin.set("imap.example.com\nmail.other.org")
    user = fake_imap.user([
        fake_imap.row(row_id=1, is_default=True),
        fake_imap.row(row_id=2, is_default=False, server="mail.other.org:993 ssl",
                      username="bob@other.org", password="bob-pass"),
    ])
    return SimpleNamespace(user=user, server=other, mailbox=mailbox)


# ---------------------------------------------------------------------------
# Registration and specs
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_all_tools_registered_gated_and_declare_connection(self, imap_plugin):
        assert tuple(t.spec["name"] for t in tools_mod.ALL_TOOLS) == TOOL_NAMES
        for name in TOOL_NAMES:
            spec = TOOL_CALL_REGISTRY[name]
            assert spec["requires_service"] == "imap"
            props = spec["parameters"]["properties"]
            assert props["connection"]["type"] == "string"
            assert "connection" not in spec["parameters"].get("required", [])
            assert props["intent_message"]["type"] == "string"
            assert name in TOOL_CALL_HANDLERS
        assert not any(t.mutating for t in tools_mod.ALL_TOOLS)

    def test_script_allowlist_is_the_four_read_tools(self, imap_plugin):
        assert imap_plugin.script_tool_allowlist == frozenset(TOOL_NAMES[:4])
        assert tools_mod.SCRIPT_TOOL_NAMES == frozenset(TOOL_NAMES[:4])

    def test_loader_accepts_final_manifest(self):
        from pathlib import Path

        from config import plugins as plugins_mod

        plugin_py = Path(tools_mod.__file__).resolve().parent / "plugin.py"
        plugin = plugins_mod._import_plugin_module(plugin_py).get_plugin()
        plugins_mod.validate_plugin(plugin, set())
        assert [s.id for s in plugin.system_skills] == ["system:imap"]
        assert plugin.system_skills[0].requires == "imap"

    def test_unregistered_without_plugin(self):
        for name in TOOL_NAMES:
            assert name not in TOOL_CALL_REGISTRY


# ---------------------------------------------------------------------------
# imap_list_folders
# ---------------------------------------------------------------------------


class TestListFolders:
    def test_lists_folders_with_counts_and_special_use(self, fake_imap):
        mb = fake_imap.mailbox
        mb.add_folder("Sent", special_use="\\Sent", uidvalidity=7)
        mb.add_folder("Archive/2026")
        mb.add_folder("Entwürfe", special_use="\\Drafts")
        mb.add_message("INBOX", make_message(subject="a"))
        mb.add_message("INBOX", make_message(subject="b"), flags=("\\Seen",))
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        assert out["account"] == "alice@example.com"
        by_name = {f["name"]: f for f in out["folders"]}
        assert set(by_name) == {"INBOX", "Sent", "Archive/2026", "Entwürfe"}
        assert by_name["INBOX"]["messages"] == 2 and by_name["INBOX"]["unseen"] == 1
        assert by_name["Sent"]["special_use"] == "\\Sent"
        assert by_name["Sent"]["uidvalidity"] == 7
        assert by_name["Entwürfe"]["special_use"] == "\\Drafts"
        assert by_name["INBOX"]["delimiter"] == "/"
        assert out["count"] == 4 and "truncated" not in out

    def test_noselect_and_failing_status_still_listed(self, fake_imap, monkeypatch):
        mb = fake_imap.mailbox
        parent = mb.add_folder("Projects")
        parent.attributes = ("\\Noselect", "\\HasChildren")
        mb.add_folder("Broken")
        real_status = FakeIMAP4.status

        def status(self, mailbox, names):
            if "Broken" in mailbox:
                return "NO", [b"[SERVERBUG] status failed"]
            return real_status(self, mailbox, names)

        monkeypatch.setattr(FakeIMAP4, "status", status)
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        by_name = {f["name"]: f for f in out["folders"]}
        assert by_name["Projects"]["selectable"] is False
        assert by_name["Projects"]["messages"] is None
        assert by_name["Broken"]["messages"] is None and "status_error" in by_name["Broken"]
        assert by_name["INBOX"]["messages"] == 0
        # no STATUS sent for the \Noselect container
        assert not any(c[0] == "STATUS" and "Projects" in c[1] for c in fake_imap.server.commands)

    def test_capped_at_200(self, fake_imap):
        for i in range(205):
            fake_imap.mailbox.add_folder(f"F{i:03d}")
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        assert out["count"] == 200 and out["truncated"] is True


# ---------------------------------------------------------------------------
# imap_search_messages
# ---------------------------------------------------------------------------


def _seed_inbox(fake_imap):
    from datetime import datetime, timezone

    mb = fake_imap.mailbox
    uids = []
    for i, (sender, subject, day) in enumerate([
        ("ACME Billing <billing@acme.com>", "Invoice 1", 1),
        ("Carol <carol@example.org>", "Lunch?", 3),
        ("ACME Billing <billing@acme.com>", "Invoice 2", 5),
        ("Dave <dave@example.org>", "Report", 7),
    ]):
        attachments = (("report.pdf", "application/pdf", PDF_BYTES),) if subject == "Report" else ()
        uids.append(mb.add_message(
            "INBOX", make_message(subject=subject, sender=sender, attachments=attachments),
            flags=("\\Seen",) if i == 0 else (),
            internaldate=datetime(2026, 10, day, 9, tzinfo=timezone.utc),
        ))
    return uids


class TestSearch:
    def test_all_newest_first_with_rows(self, fake_imap):
        uids = _seed_inbox(fake_imap)
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert out["folder"] == "INBOX" and out["uidvalidity"] == 1
        assert out["total_matches"] == 4 and out["account"] == "alice@example.com"
        assert [m["uid"] for m in out["messages"]] == sorted(uids, reverse=True)
        top = out["messages"][0]
        assert top["subject"] == "Report" and top["has_attachments"] is True
        assert top["from"] == "Dave <dave@example.org>"
        assert top["to"] == "alice@example.com"
        assert isinstance(top["size"], int) and top["flags"] == []
        assert out["messages"][-1]["flags"] == ["\\Seen"]
        assert out["messages"][-1]["has_attachments"] is False

    def test_criteria_and_limit(self, fake_imap):
        _seed_inbox(fake_imap)
        user = _default_user(fake_imap)
        out = _call(tools_mod._handle_search_messages, _ctx(user), {"from": "acme", "limit": 1})
        assert out["total_matches"] == 2 and out["returned"] == 1
        assert out["messages"][0]["subject"] == "Invoice 2" and "note" in out
        out = _call(tools_mod._handle_search_messages, _ctx(user),
                    {"since": "2026-10-03", "before": "2026-10-07"})
        assert [m["subject"] for m in out["messages"]] == ["Invoice 2", "Lunch?"]
        out = _call(tools_mod._handle_search_messages, _ctx(user), {"unseen": True, "subject": "invoice"})
        assert [m["subject"] for m in out["messages"]] == ["Invoice 2"]
        text = fake_imap.server.command_text()
        assert 'SINCE "03-Oct-2026"' in text and 'BEFORE "07-Oct-2026"' in text
        assert 'FROM "acme"' in text

    def test_non_ascii_text_criterion(self, fake_imap):
        fake_imap.mailbox.add_message("INBOX", make_message(subject="Grüße aus Köln"))
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)),
                    {"subject": "Köln"})
        assert out["total_matches"] == 1

    def test_empty_folder(self, fake_imap):
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert out["total_matches"] == 0 and out["messages"] == []

    def test_folder_not_found(self, fake_imap):
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "Nope"})
        assert out["error"] == "imap_folder_not_found"

    @pytest.mark.parametrize("args, fragment", [
        ({"limit": 0}, "limit"),
        ({"limit": 51}, "limit"),
        ({"limit": "lots"}, "limit"),
        ({"limit": True}, "limit"),
        ({"since": "2026/10/05"}, "YYYY-MM-DD"),
        ({"since": "2026-02-30"}, "calendar date"),
        ({"since": "2026-10-05", "before": "2026-10-05"}, "before"),
        ({"from": "x\r\nA001 LOGOUT"}, "line breaks"),
        ({"subject": 5}, "string"),
        ({"unseen": "yes"}, "unseen"),
        ({"folder": ["INBOX"]}, "folder"),
        ({"text": "x" * 501}, "at most"),
    ])
    def test_argument_validation(self, fake_imap, args, fragment):
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), args)
        assert out["error"] == "invalid_arguments"
        assert fragment in out["message"]
        assert fake_imap.server.commands == []  # rejected before connecting

    def test_imap_date(self):
        from datetime import date

        assert tools_mod.imap_date(date(2026, 1, 9)) == "09-Jan-2026"
        assert tools_mod.imap_date(date(2026, 12, 31)) == "31-Dec-2026"


# ---------------------------------------------------------------------------
# imap_get_messages
# ---------------------------------------------------------------------------


class TestGetMessages:
    def test_single_message_markdown(self, fake_imap, url_cache):
        uid = fake_imap.mailbox.add_message("INBOX", make_message(subject="Quarterly", body="Numbers inside.\n"))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "inbox", "uids": [uid]})
        assert isinstance(out, str)
        assert "Subject: Quarterly" in out and "Numbers inside." in out
        assert "Folder: INBOX" in out and f"UID: {uid}" in out and "UIDVALIDITY: 1" in out
        assert "Account: alice@example.com" in out

    def test_batch_with_missing_uid(self, fake_imap, url_cache):
        a = fake_imap.mailbox.add_message("INBOX", make_message(subject="First"))
        b = fake_imap.mailbox.add_message("INBOX", make_message(subject="Second"))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [b, 999, a]})
        assert out.startswith("# IMAP messages batch")
        assert "Fetched 2 message(s). 1 error(s)." in out
        assert "imap_message_not_found" in out and "999" in out
        assert out.index("Subject: Second") < out.index("Subject: First")

    def test_single_missing_uid_is_json_error(self, fake_imap):
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [42]})
        assert out["error"] == "imap_message_not_found" and out["uid"] == 42

    def test_uidvalidity_change(self, fake_imap):
        uid = fake_imap.mailbox.add_message("INBOX", make_message())
        user = _default_user(fake_imap)
        found = _call(tools_mod._handle_search_messages, _ctx(user), {})
        old = found["uidvalidity"]
        fake_imap.mailbox.bump_uidvalidity("INBOX")
        out = _call(tools_mod._handle_get_messages, _ctx(user),
                    {"folder": "INBOX", "uids": [uid], "uidvalidity": old})
        assert out["error"] == "imap_uidvalidity_changed"
        assert out["expected"] == old and out["current"] == old + 1
        assert "imap_search_messages" in out["message"]
        # no message bytes fetched after the mismatch
        assert "BODY.PEEK[]" not in fake_imap.server.command_text()
        # matching uidvalidity works
        ok = _call(tools_mod._handle_get_messages, _ctx(user),
                   {"folder": "INBOX", "uids": [uid], "uidvalidity": old + 1})
        assert isinstance(ok, str) and "Subject: Hello" in ok

    def test_folder_not_found(self, fake_imap):
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "Gone", "uids": [1]})
        assert out["error"] == "imap_folder_not_found"

    @pytest.mark.parametrize("args, fragment", [
        ({"uids": [1]}, "folder is required"),
        ({"folder": "INBOX"}, "uids is required"),
        ({"folder": "INBOX", "uids": []}, "uids is required"),
        ({"folder": "INBOX", "uids": [0]}, "between 1"),
        ({"folder": "INBOX", "uids": [True]}, "integer"),
        ({"folder": "INBOX", "uids": [1.5]}, "integer"),
        ({"folder": "INBOX", "uids": list(range(1, 22))}, "At most 20"),
        ({"folder": "INBOX", "uids": [1], "uidvalidity": "abc"}, "uidvalidity"),
        ({"folder": "INBOX", "uids": [1], "include_html": "maybe"}, "include_html"),
    ])
    def test_argument_validation(self, fake_imap, args, fragment):
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)), args)
        assert out["error"] == "invalid_arguments" and fragment in out["message"]
        assert fake_imap.server.commands == []

    def test_uids_dedupe_and_string_forms(self, fake_imap, url_cache):
        uid = fake_imap.mailbox.add_message("INBOX", make_message(subject="Once"))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid, str(uid)]})
        assert isinstance(out, str) and out.count("Subject: Once") == 1

    def test_html_fallback_urls_and_lookup(self, fake_imap, url_cache):
        html = f'<p>Hi <b>Alice</b>, <a href="{LONG_URL}">click here</a> to confirm.</p>'
        uid = fake_imap.mailbox.add_message("INBOX", make_message(body="\n", html=html))
        user = _default_user(fake_imap)
        out = _call(tools_mod._handle_get_messages, _ctx(user), {"folder": "INBOX", "uids": [uid]})
        assert "(#1#)" in out and LONG_URL not in out
        assert "**Alice**" in out and "<p>" not in out
        assert "Replaced URL count: 1" in out

        # latest rendering (no uidvalidity)
        res = _call(tools_mod._handle_get_message_urls, _ctx(user), {"folder": "INBOX", "uid": uid})
        assert res["urls"] == {"1": LONG_URL}
        # explicit uidvalidity + url_ids
        res = _call(tools_mod._handle_get_message_urls, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "uidvalidity": 1, "url_ids": [1, 7]})
        assert res["urls"] == {"1": LONG_URL} and "7" in res["errors"]
        assert res["uidvalidity"] == 1
        # a different uidvalidity is a cache miss
        res = _call(tools_mod._handle_get_message_urls, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "uidvalidity": 2})
        assert res["error"] == "url_mapping_not_found"
        # the lookup never talks to the server
        before = len(fake_imap.server.commands)
        _call(tools_mod._handle_get_message_urls, _ctx(user), {"folder": "INBOX", "uid": uid})
        assert len(fake_imap.server.commands) == before

    def test_include_urls_and_include_html(self, fake_imap, url_cache):
        html = f'<p><a href="{LONG_URL}">link</a></p>'
        uid = fake_imap.mailbox.add_message("INBOX", make_message(body="\n", html=html))
        user = _default_user(fake_imap)
        out = _call(tools_mod._handle_get_messages, _ctx(user),
                    {"folder": "INBOX", "uids": [uid], "include_urls": True})
        assert LONG_URL in out and "(#1#)" not in out
        out = _call(tools_mod._handle_get_messages, _ctx(user),
                    {"folder": "INBOX", "uids": [uid], "include_html": True})
        assert "## Body (HTML)" in out and "<a href=" in out

    def test_attachment_list_matches_render_enumeration(self, fake_imap, url_cache):
        raw = make_message(subject="Files", attachments=(
            ("report.pdf", "application/pdf", PDF_BYTES),
            ("data.csv", "text/csv", b"a,b\n1,2\n"),
        ))
        uid = fake_imap.mailbox.add_message("INBOX", raw)
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid]})
        for att in list_attachments(parse_message(raw)):
            assert f"attachment_index: `{att.index}`" in out
            assert att.filename in out

    def test_huge_message_headers_only(self, fake_imap, monkeypatch, url_cache):
        monkeypatch.setattr(tools_mod, "MAX_RENDER_MESSAGE_BYTES", 2000)
        big = fake_imap.mailbox.add_message("INBOX", make_message(
            subject="Huge", attachments=(("big.pdf", "application/pdf", PDF_BYTES * 4),)))
        small = fake_imap.mailbox.add_message("INBOX", make_message(subject="Small"))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [big, small]})
        assert "Subject: Huge" in out and "rendering limit" in out
        assert "big.pdf" in out and "filename" in out
        assert "Subject: Small" in out
        fetches = [c for c in fake_imap.server.commands if c[0] == "UID FETCH"]
        assert not any(c[1] == str(big) and "BODY.PEEK[]" in c[2] for c in fetches)
        assert any(c[1] == str(big) and "BODY.PEEK[HEADER]" in c[2] for c in fetches)

    def test_batch_byte_budget(self, fake_imap, monkeypatch, url_cache):
        a = fake_imap.mailbox.add_message("INBOX", make_message(subject="A", body="x" * 3000))
        b = fake_imap.mailbox.add_message("INBOX", make_message(subject="B", body="y" * 3000))
        monkeypatch.setattr(tools_mod, "MAX_BATCH_BYTES", 4000)
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [a, b]})
        assert "Subject: A" in out and "imap_batch_too_large" in out


# ---------------------------------------------------------------------------
# imap_get_message_urls
# ---------------------------------------------------------------------------


class TestGetMessageUrls:
    def test_requires_conversation(self, fake_imap):
        out = _call(tools_mod._handle_get_message_urls,
                    _ctx(_default_user(fake_imap), conversation_id=None),
                    {"folder": "INBOX", "uid": 1})
        assert out["error"] == "no_conversation_context"

    def test_not_cached(self, fake_imap, url_cache):
        out = _call(tools_mod._handle_get_message_urls, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": 3})
        assert out["error"] == "url_mapping_not_found"
        assert fake_imap.server.commands == []

    @pytest.mark.parametrize("args", [
        {"folder": "INBOX"},
        {"uid": 1},
        {"folder": "INBOX", "uid": 1, "url_ids": ["one"]},
        {"folder": "INBOX", "uid": 1, "uidvalidity": 0},
    ])
    def test_argument_validation(self, fake_imap, args):
        out = _call(tools_mod._handle_get_message_urls, _ctx(_default_user(fake_imap)), args)
        assert out["error"] == "invalid_arguments"

    def test_mappings_are_per_account(self, imap_plugin, fake_imap, two_accounts, url_cache):
        html = f'<p><a href="{LONG_URL}">x</a></p>'
        uid = fake_imap.mailbox.add_message("INBOX", make_message(body="\n", html=html))
        ctx = _ctx(two_accounts.user)
        _call(tools_mod._handle_get_messages, ctx, {"folder": "INBOX", "uids": [uid]})
        res = _call(tools_mod._handle_get_message_urls, ctx, {"folder": "INBOX", "uid": uid})
        assert res["urls"] == {"1": LONG_URL}
        other = _call(tools_mod._handle_get_message_urls, ctx,
                      {"folder": "INBOX", "uid": uid, "connection": "bob@other.org"})
        assert other["error"] == "url_mapping_not_found"


# ---------------------------------------------------------------------------
# imap_save_attachment
# ---------------------------------------------------------------------------


class TestSaveAttachment:
    def _seed(self, fake_imap):
        return fake_imap.mailbox.add_message("INBOX", make_message(subject="Files", attachments=(
            ("report.pdf", "application/pdf", PDF_BYTES),
            ("notes.txt", "text/plain", b"hello notes\n"),
        )))

    def test_save_by_index_and_by_filename(self, fake_imap, workspace):
        root, published = workspace
        uid = self._seed(fake_imap)
        user = _default_user(fake_imap)
        out = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1, "uidvalidity": 1})
        assert out["status"] == "success"
        assert out["path"] == "imap-attachments/alice@example.com/report.pdf"
        assert (root / out["path"]).read_bytes() == PDF_BYTES
        assert out["size_bytes"] == len(PDF_BYTES) and out["content_type"] == "application/pdf"
        assert out["attachment_index"] == 1 and out["uidvalidity"] == 1
        published.assert_called_once_with(1, "conversation", "conv-1", None)

        out = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "filename": "NOTES.TXT"})
        assert out["path"] == "imap-attachments/alice@example.com/notes.txt"
        assert (root / out["path"]).read_bytes() == b"hello notes\n"

    def test_collision_gets_suffix(self, fake_imap, workspace):
        root, _ = workspace
        uid = self._seed(fake_imap)
        user = _default_user(fake_imap)
        args = {"folder": "INBOX", "uid": uid, "attachment_index": 1}
        first = _call(tools_mod._handle_save_attachment, _ctx(user), args)
        second = _call(tools_mod._handle_save_attachment, _ctx(user), args)
        third = _call(tools_mod._handle_save_attachment, _ctx(user), args)
        assert first["filename"] == "report.pdf"
        assert second["filename"] == "report-2.pdf"
        assert third["filename"] == "report-3.pdf"
        assert (root / first["path"]).read_bytes() == PDF_BYTES

    @pytest.mark.parametrize("save_as, expected", [
        ("../../evil.sh", "_.._evil.sh"),
        ("/etc/passwd", "_etc_passwd"),
        ("..", "attachment-1.pdf"),
        ("sub/dir/name.pdf", "sub_dir_name.pdf"),
        ("..\\..\\win.pdf", "_.._win.pdf"),
        (".hidden", "hidden"),
        ("bad\x07name\x1b.pdf", "bad_name_.pdf"),
        ("bidi\u202ename.pdf", "bidi_name.pdf"),
        ("renamed.pdf", "renamed.pdf"),
    ])
    def test_save_as_is_sanitised_and_contained(self, fake_imap, workspace, save_as, expected):
        root, _ = workspace
        uid = self._seed(fake_imap)
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1, "save_as": save_as})
        assert out["status"] == "success", out
        assert out["filename"] == expected
        written = (root / out["path"]).resolve()
        assert written.parent == (root / "imap-attachments" / "alice@example.com").resolve()
        assert written.read_bytes() == PDF_BYTES

    def test_unnamed_attachment_and_long_name(self, fake_imap, workspace):
        assert tools_mod.safe_filename("", "attachment-2.bin") == "attachment-2.bin"
        long = tools_mod.safe_filename("a" * 400 + ".pdf", "x")
        assert len(long.encode("utf-8")) <= 200 and long.endswith(".pdf")

    def test_symlinked_destination_refused(self, fake_imap, workspace, tmp_path):
        root, _ = workspace
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "imap-attachments").symlink_to(outside)
        uid = self._seed(fake_imap)
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1})
        assert out["error"] == "invalid_destination"
        assert list(outside.iterdir()) == []

    def test_attachment_not_found_lists_available(self, fake_imap, workspace):
        uid = self._seed(fake_imap)
        user = _default_user(fake_imap)
        out = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 9})
        assert out["error"] == "imap_attachment_not_found"
        assert [a["filename"] for a in out["available"]] == ["report.pdf", "notes.txt"]
        out = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "filename": "missing.doc"})
        assert out["error"] == "imap_attachment_not_found"

    def test_message_not_found(self, fake_imap, workspace):
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": 77, "attachment_index": 1})
        assert out["error"] == "imap_message_not_found"

    def test_attachment_cap(self, fake_imap, workspace, monkeypatch):
        root, published = workspace
        uid = self._seed(fake_imap)
        monkeypatch.setattr(tools_mod, "MAX_ATTACHMENT_BYTES", 100)
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1})
        assert out["error"] == "imap_attachment_too_large"
        assert not (root / "imap-attachments").exists()
        published.assert_not_called()

    def test_oversized_message_refused_before_download(self, fake_imap, workspace, monkeypatch):
        uid = self._seed(fake_imap)
        monkeypatch.setattr(tools_mod, "MAX_SAVE_MESSAGE_BYTES", 500)
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1})
        assert out["error"] == "imap_attachment_too_large"
        assert "BODY.PEEK[]" not in fake_imap.server.command_text()

    def test_uidvalidity_change(self, fake_imap, workspace):
        uid = self._seed(fake_imap)
        fake_imap.mailbox.bump_uidvalidity("INBOX")
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1, "uidvalidity": 1})
        assert out["error"] == "imap_uidvalidity_changed"

    @pytest.mark.parametrize("args, fragment", [
        ({"folder": "INBOX", "uid": 1}, "exactly one"),
        ({"folder": "INBOX", "uid": 1, "attachment_index": 1, "filename": "a"}, "exactly one"),
        ({"folder": "INBOX", "attachment_index": 1}, "uid is required"),
        ({"uid": 1, "attachment_index": 1}, "folder is required"),
        ({"folder": "INBOX", "uid": 1, "attachment_index": 0}, "attachment_index"),
        ({"folder": "INBOX", "uid": 1, "attachment_index": 1, "save_as": "a\nb"}, "line breaks"),
    ])
    def test_argument_validation(self, fake_imap, workspace, args, fragment):
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)), args)
        assert out["error"] == "invalid_arguments" and fragment in out["message"]

    def test_requires_conversation(self, fake_imap):
        out = _call(tools_mod._handle_save_attachment,
                    _ctx(_default_user(fake_imap), conversation_id=None),
                    {"folder": "INBOX", "uid": 1, "attachment_index": 1})
        assert out["error"] == "no_conversation_context"


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class TestConnections:
    def test_named_connection_uses_that_account(self, imap_plugin, fake_imap, two_accounts):
        two_accounts.mailbox.add_message("INBOX", make_message(subject="Bob's mail"))
        fake_imap.mailbox.add_message("INBOX", make_message(subject="Alice's mail"))
        ctx = _ctx(two_accounts.user)
        out = _call(tools_mod._handle_search_messages, ctx, {"connection": "Bob@Other.org"})
        assert out["account"] == "bob@other.org"
        assert [m["subject"] for m in out["messages"]] == ["Bob's mail"]
        assert ("LOGIN", "bob@other.org") in two_accounts.server.commands
        default = _call(tools_mod._handle_search_messages, ctx, {})
        assert default["account"] == "alice@example.com"
        assert [m["subject"] for m in default["messages"]] == ["Alice's mail"]

    def test_connection_not_found_lists_available(self, imap_plugin, fake_imap, two_accounts):
        for handler, args in [
            (tools_mod._handle_list_folders, {}),
            (tools_mod._handle_search_messages, {}),
            (tools_mod._handle_get_messages, {"folder": "INBOX", "uids": [1]}),
            (tools_mod._handle_get_message_urls, {"folder": "INBOX", "uid": 1}),
            (tools_mod._handle_save_attachment, {"folder": "INBOX", "uid": 1, "attachment_index": 1}),
        ]:
            out = _call(handler, _ctx(two_accounts.user), {**args, "connection": "nobody@x.org"})
            assert out["error"] == "connection_not_found", handler
            assert out["service"] == "imap" and out["requested"] == "nobody@x.org"
            assert out["available"] == ["alice@example.com", "bob@other.org"]
        assert fake_imap.server.commands == [] and two_accounts.server.commands == []

    def test_connection_must_be_string(self, imap_plugin, fake_imap):
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {"connection": 3})
        assert out["error"] == "invalid_arguments"

    def test_not_connected(self, fake_imap):
        for user in ({"id": 1, "email": "u@example.com"},
                     fake_imap.user([{**fake_imap.row(), "oauth_blob": {}}])):
            out = _call(tools_mod._handle_list_folders, _ctx(user), {})
            assert out["error"] == "imap_not_connected"
            assert "Settings > Data Connections" in out["message"]

    def test_host_removed_from_admin_list(self, fake_imap):
        fake_imap.admin.set("mail.elsewhere.org")
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        assert out["error"] == "imap_host_not_allowed"
        assert fake_imap.server.commands == []  # never connected
        fake_imap.admin.clear()
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert out["error"] == "imap_host_not_allowed"

    def test_server_line_removed_while_host_stays(self, fake_imap):
        # Only the 993/ssl configuration is the row's; a starttls line for
        # the same host does not keep it alive.
        fake_imap.admin.set("imap.example.com:143")
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        assert out["error"] == "imap_host_not_allowed"
        assert out["server"] == "imap.example.com:993 ssl"
        assert fake_imap.server.commands == []

    def test_tools_resolve_server_starttls(self, fake_imap):
        fake_imap.admin.set("imap.example.com:143")
        user = fake_imap.user([fake_imap.row(server="imap.example.com:143 starttls")])
        out = _call(tools_mod._handle_list_folders, _ctx(user), {})
        assert "error" not in out
        conn = fake_imap.server.connections[0]
        assert not conn.implicit_tls and conn.port == 143
        assert [c[0] for c in fake_imap.server.commands][:3] == ["CONNECT", "STARTTLS", "LOGIN"]

    def test_legacy_row_without_server_fails_closed(self, fake_imap):
        legacy = fake_imap.row(values={
            "host": "imap.example.com", "port": "", "security": "ssl",
            "username": "alice@example.com", "password": "s3cret-pass",
        })
        out = _call(tools_mod._handle_list_folders, _ctx(fake_imap.user([legacy])), {})
        assert out["error"] == "imap_host_not_allowed"
        assert "s3cret-pass" not in json.dumps(out)
        assert fake_imap.server.commands == []  # never connected

    def test_bare_username_account_uses_resolved_host(self, fake_imap):
        fake_imap.server.add_account("bob", "pw-bob")
        row = fake_imap.row(username="bob", password="pw-bob")
        assert row["account"] == "bob@imap.example.com"
        out = _call(tools_mod._handle_list_folders, _ctx(fake_imap.user([row])), {})
        assert "error" not in out
        assert out["account"] == "bob@imap.example.com"
        assert [c for c in fake_imap.server.commands if c[0] == "LOGIN"] == [("LOGIN", "bob")]

    def test_auth_failure_is_structured(self, fake_imap):
        fake_imap.server.auth_fail = True
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        assert out["error"] == "imap_auth_failed"
        assert "s3cret-pass" not in json.dumps(out)


# ---------------------------------------------------------------------------
# Real dispatch path
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_tool_call_dispatch(self, imap_plugin, fake_imap, two_accounts):
        fake_imap.mailbox.add_folder("Sent", special_use="\\Sent")
        result_text, extra = _run(_dispatch_tool_call(
            app=None, provider=None, user=two_accounts.user,
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "imap_list_folders", "arguments": {}},
        ))
        payload = json.loads(result_text)
        assert payload["account"] == "alice@example.com"
        assert {f["name"] for f in payload["folders"]} == {"INBOX", "Sent"}
        assert extra == []

        result_text, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user=two_accounts.user,
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "imap_search_messages",
                  "arguments": {"connection": "bob@other.org"}},
        ))
        assert json.loads(result_text)["account"] == "bob@other.org"

    def test_hidden_from_prompt_until_connected(self, imap_plugin):
        # Plugin tools are skill-documented: a prompt names them on the
        # system:imap enumeration line, gated with the skill.
        from chat.system_skills import build_system_skills_enumeration
        from tests.prompt_support import named_under_skill

        hidden = build_system_skills_enumeration(connected_services={"imap": False})
        shown = build_system_skills_enumeration(connected_services={"imap": True})
        for name in TOOL_NAMES:
            assert name not in hidden
            assert named_under_skill(shown, "system:imap", name), name


# ---------------------------------------------------------------------------
# system:imap skill
# ---------------------------------------------------------------------------


class TestSkill:
    def test_skill_lists_user_accounts(self, imap_plugin, fake_imap, two_accounts):
        from chat.system_skills import load_system_skills

        [res] = load_system_skills(
            ["system:imap"], {"imap": True}, "http://b", "k", user=two_accounts.user,
        )
        content = res["content"]
        assert "- `alice@example.com` (default)" in content
        assert "- `bob@other.org`" in content
        assert '"connection": "bob@other.org"' in content
        assert "{{" not in content
        for name in TOOL_NAMES:
            assert name in content

    def test_skill_without_connections(self, imap_plugin):
        from chat.system_skills import load_system_skills

        [res] = load_system_skills(["system:imap"], None, "http://b", "k")
        assert "No IMAP mailbox is connected yet" in res["content"]
        assert '"connection": "work@example.com"' in res["content"]

    def test_load_skills_tool_threads_user(self, imap_plugin, fake_imap, two_accounts):
        from chat.gemini_api import tool_dispatch as td

        ctx = SimpleNamespace(user={**two_accounts.user, "api_key": "k"},
                              conversation_id=None, project_id=None)
        with patch.object(td, "_get_connected_services", return_value={"imap": True}):
            out = _run(td._tool_load_skills(ctx, {"skill_ids": ["system:imap"]}))
        assert "bob@other.org" in out

    def test_skill_content_covers_spec_topics(self, imap_plugin):
        from plugins.imap.manifest import imap_skill_content

        text = imap_skill_content("b", "k", connections=[])
        for phrase in (
            "case-insensitive", "YYYY-MM-DD", "NEVER marks a message as read",
            "UIDVALIDITY", "`limit`", "no write operations", "untrusted data",
            "Connected accounts", "imap_uidvalidity_changed", "imap_host_not_allowed",
        ):
            assert phrase in text, phrase

    def test_skill_hidden_until_connected(self, imap_plugin):
        from chat.system_skills.loader import list_system_skills

        assert "system:imap" not in [s.id for s in list_system_skills({"imap": False})]
        assert "system:imap" in [s.id for s in list_system_skills({"imap": True})]


# ---------------------------------------------------------------------------
# Review fixes: raw folder names, shared attachment rule, hostile data
# ---------------------------------------------------------------------------


_CALENDAR_MESSAGE = (
    b"Subject: Invite\r\nFrom: a@example.com\r\nTo: alice@example.com\r\n"
    b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
    b"--XX\r\nContent-Type: text/plain\r\n\r\nPart one.\r\n"
    b"--XX\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    b"--XX\r\nContent-Type: text/plain\r\n\r\nPart two.\r\n"
    b"--XX\r\nContent-Type: application/pdf\r\n"
    b"Content-Disposition: attachment; filename=\"q*`3`.pdf\"\r\n\r\n%PDF\r\n"
    b"--XX--\r\n"
)


class TestReviewFixes:
    def test_raw_name_listed_and_accepted(self, fake_imap, url_cache):
        mb = fake_imap.mailbox
        mb.add_folder("éé", raw_name="&AOk-&AOk-")
        mb.add_folder("Entwürfe")  # canonical: no raw_name
        uid = mb.add_message("éé", make_message(subject="odd folder"))
        user = _default_user(fake_imap)
        out = _call(tools_mod._handle_list_folders, _ctx(user), {})
        by_name = {f["name"]: f for f in out["folders"]}
        assert by_name["éé"]["raw_name"] == "&AOk-&AOk-"
        assert by_name["éé"]["messages"] == 1  # STATUS used the raw name
        assert "raw_name" not in by_name["Entwürfe"] and "raw_name" not in by_name["INBOX"]
        found = _call(tools_mod._handle_search_messages, _ctx(user), {"folder": "&AOk-&AOk-"})
        assert [m["uid"] for m in found["messages"]] == [uid]
        doc = _call(tools_mod._handle_get_messages, _ctx(user),
                    {"folder": "&AOk-&AOk-", "uids": [uid]})
        assert "Subject: odd folder" in doc

    def test_render_and_save_agree_on_attachment_index(self, fake_imap, workspace, url_cache):
        root, _ = workspace
        uid = fake_imap.mailbox.add_message("INBOX", _CALENDAR_MESSAGE)
        user = _default_user(fake_imap)
        doc = _call(tools_mod._handle_get_messages, _ctx(user), {"folder": "INBOX", "uids": [uid]})
        assert "Part one." in doc and "Part two." in doc
        assert "`text/calendar`, ~" in doc and "attachment_index: `1`" in doc
        assert "**q\\*\\`3\\`.pdf** -- `application/pdf`" in doc and "attachment_index: `2`" in doc
        cal = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1})
        assert cal["content_type"] == "text/calendar"
        assert (root / cal["path"]).read_bytes().startswith(b"BEGIN:VCALENDAR")
        assert cal["size_bytes"] == len((root / cal["path"]).read_bytes())  # exact size
        # The escaped spelling shown in the listing also works as filename.
        pdf = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "filename": "q\\*\\`3\\`.pdf"})
        assert pdf["attachment_index"] == 2 and pdf["content_type"] == "application/pdf"
        # has_attachments uses the same rule from BODYSTRUCTURE.
        found = _call(tools_mod._handle_search_messages, _ctx(user), {})
        assert found["messages"][0]["has_attachments"] is True

    def test_has_attachments_for_calendar_only_message(self, fake_imap):
        raw = (b"Subject: cal\r\nContent-Type: multipart/alternative; boundary=A\r\n\r\n"
               b"--A\r\nContent-Type: text/plain\r\n\r\ninvite\r\n"
               b"--A\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\n--A--\r\n")
        fake_imap.mailbox.add_message("INBOX", raw)
        found = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert found["messages"][0]["has_attachments"] is True

    def test_huge_message_listing_uses_shared_rule_and_safe_names(self, fake_imap, monkeypatch,
                                                                 url_cache):
        monkeypatch.setattr(tools_mod, "MAX_RENDER_MESSAGE_BYTES", 100)
        uid = fake_imap.mailbox.add_message("INBOX", _CALENDAR_MESSAGE)
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid]})
        assert "rendering limit" in out
        section = out.split("## Attachments", 1)[1]
        assert "`text/calendar`" in section
        assert "**q\\*\\`3\\`.pdf**" in section
        assert "`text/plain`" not in section

    def test_complex_message_save_refused_and_render_headers_only(self, fake_imap, workspace,
                                                                  url_cache):
        parts = b"".join(b"--XX\r\nContent-Type: text/plain\r\n\r\nx\r\n" for _ in range(3000))
        raw = (b"Subject: Bomb\r\nContent-Type: multipart/mixed; boundary=XX\r\n\r\n"
               + parts + b"--XX--\r\n")
        uid = fake_imap.mailbox.add_message("INBOX", raw)
        user = _default_user(fake_imap)
        doc = _call(tools_mod._handle_get_messages, _ctx(user), {"folder": "INBOX", "uids": [uid]})
        assert "Subject: Bomb" in doc and "Not rendered" in doc
        out = _call(tools_mod._handle_save_attachment, _ctx(user),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 1})
        assert out["error"] == "imap_message_too_complex"

    def test_available_sizes_marked_approximate(self, fake_imap, workspace):
        uid = fake_imap.mailbox.add_message("INBOX", make_message(attachments=(
            ("report.pdf", "application/pdf", PDF_BYTES),)))
        out = _call(tools_mod._handle_save_attachment, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uid": uid, "attachment_index": 5})
        assert out["available"] == [{"attachment_index": 1, "filename": "report.pdf",
                                     "mime": "application/pdf", "approx_size": len(PDF_BYTES)}]

    def test_hostile_server_response_is_protocol_error(self, fake_imap, monkeypatch):
        fake_imap.mailbox.add_message("INBOX", make_message())

        def hostile(self, uid_spec, items_text):
            return "OK", [b"1 (UID 1 ENVELOPE " + b"(" * 3000 + b")" * 3000 + b")"]

        monkeypatch.setattr(FakeIMAP4, "_fetch", hostile)
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert out["error"] == "imap_protocol_error"
