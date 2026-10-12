"""Hardening of the imap_* tools (security review of plugins/imap/tools.py).

Byte-capped workspace names, traversal through attachment / account names,
symlinks in the destination, the per-call output budget, header / search
field caps, understated RFC822.SIZE, the >25 MB listing's attachment_index,
parse / render failures contained per message, the URL-lookup account key,
cross-user connection isolation through the real dispatch with DB users,
and imap_get_messages through the script bridge.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from chat.gemini_api.tool_dispatch import _dispatch_tool_call

from plugins.imap import tools as tools_mod
from plugins.imap.manifest import _NO_CONNECTIONS
from plugins.imap.tests.fake_imaplib import FakeIMAP4, make_message
from plugins.imap.tests.test_imap_tools import (  # noqa: F401 (fixtures)
    LONG_URL,
    PDF_BYTES,
    _call,
    _ctx,
    _default_user,
    _run,
    url_cache,
    workspace,
)
from plugins.imap.upstream import ImapError, ImapSession


def _save(user, uid, **extra):
    return _call(tools_mod._handle_save_attachment, _ctx(user),
                 {"folder": "INBOX", "uid": uid, "attachment_index": 1, **extra})


def _raw_with_filename(filename_header: bytes) -> bytes:
    return (
        b"Subject: names\r\nFrom: a@example.com\r\nTo: alice@example.com\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=XX\r\n\r\n"
        b"--XX\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--XX\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=" + filename_header + b"\r\n\r\n%PDF-1.4 data\r\n"
        b"--XX--\r\n"
    )


# ---------------------------------------------------------------------------
# Workspace names (findings 1, 6, 9)
# ---------------------------------------------------------------------------


class TestNames:
    def test_long_names_capped_in_bytes(self):
        cjk = tools_mod.safe_filename("請求書" * 100 + ".pdf", "x")
        assert len(cjk.encode("utf-8")) <= 200 and cjk.endswith(".pdf")
        assert cjk.startswith("請求書")
        ascii_name = tools_mod.safe_filename("a" * 400 + ".pdf", "x")
        assert len(ascii_name.encode()) <= 200 and ascii_name.endswith(".pdf")
        # An absurd "extension" is not kept whole.
        weird = tools_mod.safe_filename("x." + "é" * 300, "fallback.bin")
        assert len(weird.encode("utf-8")) <= 200
        # Never a dangling partial UTF-8 sequence.
        weird.encode("utf-8").decode("utf-8")

    def test_long_cjk_attachment_name_saves(self, fake_imap, workspace):
        root, _ = workspace
        name = "請求書" * 100 + ".pdf"
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            attachments=((name, "application/pdf", PDF_BYTES),)))
        user = _default_user(fake_imap)
        first = _save(user, uid)
        second = _save(user, uid)
        assert first["status"] == "success", first
        assert len(first["filename"].encode("utf-8")) <= 200
        assert first["filename"].endswith(".pdf")
        assert second["filename"].endswith("-2.pdf")
        assert (root / first["path"]).read_bytes() == PDF_BYTES

    @pytest.mark.parametrize("header", [
        b'"../../x.pdf"',
        b'"/abs/path.pdf"',
        b'"..\\\\..\\\\win.pdf"',
        b'"nul\x00byte.pdf"',
        b'"...hidden.pdf"',
        b"*=utf-8''..%2F..%2Fencoded.pdf",
    ])
    def test_traversal_in_mime_filename_contained(self, fake_imap, workspace, header):
        root, _ = workspace
        uid = fake_imap.mailbox.add_message("INBOX", _raw_with_filename(header))
        out = _save(_default_user(fake_imap), uid)
        assert out["status"] == "success", out
        name = out["filename"]
        assert "/" not in name and "\\" not in name and "\x00" not in name
        assert not name.startswith(".")
        written = (root / out["path"]).resolve()
        assert written.parent == (root / "imap-attachments" / "alice@example.com").resolve()
        assert written.read_bytes().startswith(b"%PDF")

    @pytest.mark.parametrize("account", ["../evil", "a/b", "..", "/etc", "x\\..\\y"])
    def test_hostile_account_stays_one_hashed_component(self, fake_imap, workspace, account):
        root, _ = workspace
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            attachments=(("r.pdf", "application/pdf", PDF_BYTES),)))
        user = fake_imap.user([fake_imap.row(account=account)])
        out = _save(user, uid)
        assert out["status"] == "success", out
        written = (root / out["path"]).resolve()
        assert written.parent.parent == (root / "imap-attachments").resolve()
        assert re.search(r"-[0-9a-f]{8}$", written.parent.name)

    def test_distinct_accounts_never_share_a_folder(self):
        assert tools_mod.account_dir_name("a/b") != tools_mod.account_dir_name("a_b")
        assert tools_mod.account_dir_name("a_b") == "a_b"
        assert tools_mod.account_dir_name("alice@example.com") == "alice@example.com"
        long = tools_mod.account_dir_name("ü/" * 200)
        assert len(long.encode("utf-8")) <= 200

    def test_symlink_at_account_dir_refused(self, fake_imap, workspace, tmp_path):
        root, published = workspace
        outside = tmp_path / "outside-acct"
        outside.mkdir()
        (root / "imap-attachments").mkdir()
        (root / "imap-attachments" / "alice@example.com").symlink_to(outside)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            attachments=(("r.pdf", "application/pdf", PDF_BYTES),)))
        out = _save(_default_user(fake_imap), uid)
        assert out["error"] == "invalid_destination"
        assert list(outside.iterdir()) == []
        published.assert_not_called()

    def test_symlink_at_target_file_never_followed(self, fake_imap, workspace, tmp_path):
        root, _ = workspace
        victim = tmp_path / "victim.txt"
        victim.write_bytes(b"original")
        directory = root / "imap-attachments" / "alice@example.com"
        directory.mkdir(parents=True)
        (directory / "r.pdf").symlink_to(victim)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            attachments=(("r.pdf", "application/pdf", PDF_BYTES),)))
        out = _save(_default_user(fake_imap), uid)
        assert out["status"] == "success" and out["filename"] == "r-2.pdf"
        assert victim.read_bytes() == b"original"
        assert (directory / "r-2.pdf").read_bytes() == PDF_BYTES


# ---------------------------------------------------------------------------
# Parse / render failures (finding 2)
# ---------------------------------------------------------------------------


class TestFailuresContained:
    def test_garbled_search_row_keeps_the_others(self, fake_imap, monkeypatch):
        mb = fake_imap.mailbox
        mb.add_message("INBOX", make_message(subject="first"))
        mb.add_message("INBOX", make_message(subject="second"))
        real = tools_mod.protocol.decode_envelope
        calls = {"n": 0}

        def flaky(env):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RecursionError("deep")
            return real(env)

        monkeypatch.setattr(tools_mod.protocol, "decode_envelope", flaky)
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert out["returned"] == 2
        bad, good = out["messages"]
        assert bad["uid"] == 2 and "parse_error" in bad and "subject" not in bad
        assert isinstance(bad["size"], int)
        assert good["subject"] == "first"

    def test_stray_value_error_is_protocol_error(self, fake_imap, monkeypatch):
        def boom(self, terms):
            raise ValueError("raw parser garbage ‮")

        monkeypatch.setattr(ImapSession, "uid_search", boom)
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        assert out["error"] == "imap_protocol_error"
        assert "garbage" not in out["message"]

    def test_render_failure_is_a_batch_error(self, fake_imap, monkeypatch, url_cache):
        a = fake_imap.mailbox.add_message("INBOX", make_message(subject="Good"))
        b = fake_imap.mailbox.add_message("INBOX", make_message(subject="Bad"))
        real = tools_mod.render_message_markdown

        def flaky(raw, **kwargs):
            if kwargs["uid"] == b:
                raise IndexError("broken")
            return real(raw, **kwargs)

        monkeypatch.setattr(tools_mod, "render_message_markdown", flaky)
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [a, b]})
        assert "Subject: Good" in out
        assert "imap_render_failed" in out and "broken" not in out

    def test_status_error_text_sanitised(self, fake_imap, monkeypatch):
        def bad_status(self, folder, *, raw_name=None):
            raise ImapError("imap_protocol_error", "STATUS failed: ‮evil\nline\x1b[31m")

        monkeypatch.setattr(ImapSession, "status", bad_status)
        out = _call(tools_mod._handle_list_folders, _ctx(_default_user(fake_imap)), {})
        err = out["folders"][0]["status_error"]
        assert "‮" not in err and "\n" not in err and "\x1b" not in err
        assert "evil" in err


# ---------------------------------------------------------------------------
# Output budget and caps (finding 3)
# ---------------------------------------------------------------------------


class TestOutputBudget:
    def test_batch_stops_at_budget(self, fake_imap, monkeypatch, url_cache):
        monkeypatch.setattr(tools_mod, "MAX_OUTPUT_CHARS", 4000)
        mb = fake_imap.mailbox
        a = mb.add_message("INBOX", make_message(subject="A", body="x" * 2500 + "\n"))
        b = mb.add_message("INBOX", make_message(subject="B", body="y" * 2500 + "\n"))
        c = mb.add_message("INBOX", make_message(subject="C", body="short\n"))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [a, b, c]})
        assert "Subject: A" in out and "Subject: B" not in out
        assert "imap_output_limit" in out and f"fetch UID {b} in a separate call" in out
        assert "Subject: C" in out  # a later message that still fits is kept

    def test_single_oversized_message_body_cut(self, fake_imap, monkeypatch, url_cache):
        monkeypatch.setattr(tools_mod, "MAX_OUTPUT_CHARS", 3000)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            subject="Long", body="z" * 20000 + "\n",
            attachments=(("keep.pdf", "application/pdf", PDF_BYTES),)))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid]})
        assert isinstance(out, str) and len(out) <= 3000
        assert "Subject: Long" in out and "output limit" in out
        assert "keep.pdf" in out and "attachment_index: `1`" in out

    def test_header_line_capped(self, fake_imap, url_cache):
        to = ", ".join(f"user{i}@example.com" for i in range(2000))
        uid = fake_imap.mailbox.add_message("INBOX", make_message(subject="Many", to=to))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid]})
        to_line = next(line for line in out.splitlines() if line.startswith("To: "))
        assert len(to_line) <= 2001 and to_line.endswith("…")

    def test_search_fields_capped_and_single_line(self, fake_imap):
        fake_imap.mailbox.add_message("INBOX", make_message(subject="S" * 3000))
        out = _call(tools_mod._handle_search_messages, _ctx(_default_user(fake_imap)), {})
        row = out["messages"][0]
        assert len(row["subject"]) == 501 and row["subject"].endswith("…")
        for key in ("date", "from", "to", "subject"):
            assert "\n" not in row[key] and len(row[key]) <= 501


# ---------------------------------------------------------------------------
# Understated RFC822.SIZE (finding 4) and the large-message listing (finding 5)
# ---------------------------------------------------------------------------


def _lie_about_size(monkeypatch):
    real = FakeIMAP4._fetch
    pattern = re.compile(rb"RFC822\.SIZE \d+")

    def lying(self, uid_spec, items_text):
        typ, data = real(self, uid_spec, items_text)
        out = []
        for item in data:
            if isinstance(item, tuple):
                out.append((pattern.sub(b"RFC822.SIZE 10", item[0]), *item[1:]))
            elif isinstance(item, bytes):
                out.append(pattern.sub(b"RFC822.SIZE 10", item))
            else:
                out.append(item)
        return typ, out

    monkeypatch.setattr(FakeIMAP4, "_fetch", lying)


class TestLargeMessages:
    def test_understated_size_rendered_as_large(self, fake_imap, monkeypatch, url_cache):
        _lie_about_size(monkeypatch)
        monkeypatch.setattr(tools_mod, "MAX_RENDER_MESSAGE_BYTES", 2000)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            subject="Liar", attachments=(("big.pdf", "application/pdf", PDF_BYTES * 4),)))
        out = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid]})
        assert "Subject: Liar" in out and "rendering limit" in out
        assert "big.pdf" in out and "attachment_index: `1`" in out

    def test_understated_size_save_refused(self, fake_imap, monkeypatch, workspace):
        _lie_about_size(monkeypatch)
        monkeypatch.setattr(tools_mod, "MAX_SAVE_MESSAGE_BYTES", 500)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            attachments=(("big.pdf", "application/pdf", PDF_BYTES),)))
        out = _save(_default_user(fake_imap), uid)
        assert out["error"] == "imap_attachment_too_large"

    def test_large_listing_index_matches_save(self, fake_imap, monkeypatch, workspace, url_cache):
        root, _ = workspace
        monkeypatch.setattr(tools_mod, "MAX_RENDER_MESSAGE_BYTES", 100)
        raw = (
            b"Subject: Big\r\nFrom: a@example.com\r\nTo: alice@example.com\r\n"
            b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=XX\r\n\r\n"
            b"--XX\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--XX\r\nContent-Type: image/png\r\n\r\nPNGDATA\r\n"
            b"--XX\r\nContent-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; filename=\"b.pdf\"\r\n\r\n%PDF-1.4\r\n"
            b"--XX--\r\n"
        )
        uid = fake_imap.mailbox.add_message("INBOX", raw)
        user = _default_user(fake_imap)
        doc = _call(tools_mod._handle_get_messages, _ctx(user), {"folder": "INBOX", "uids": [uid]})
        assert "**(unnamed)** -- `image/png`" in doc and "attachment_index: `1`" in doc
        assert "**b.pdf** -- `application/pdf`" in doc and "attachment_index: `2`" in doc
        assert "only be saved by attachment_index" in doc
        unnamed = _save(user, uid, attachment_index=1)
        assert unnamed["content_type"] == "image/png"
        pdf = _save(user, uid, attachment_index=2)
        assert pdf["filename"] == "b.pdf"
        assert (root / pdf["path"]).read_bytes().startswith(b"%PDF")

    def test_large_listing_says_unsaveable_above_save_limit(self, fake_imap, monkeypatch,
                                                            url_cache):
        monkeypatch.setattr(tools_mod, "MAX_RENDER_MESSAGE_BYTES", 100)
        monkeypatch.setattr(tools_mod, "MAX_SAVE_MESSAGE_BYTES", 200)
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            attachments=(("big.pdf", "application/pdf", PDF_BYTES),)))
        doc = _call(tools_mod._handle_get_messages, _ctx(_default_user(fake_imap)),
                    {"folder": "INBOX", "uids": [uid]})
        assert "cannot be saved" in doc


# ---------------------------------------------------------------------------
# Nits: URL-lookup account key (7), manifest wording (10)
# ---------------------------------------------------------------------------


class TestNits:
    def test_url_lookup_uses_rendered_account(self, fake_imap, url_cache):
        row = fake_imap.row()
        row["account"] = None  # legacy unnamed row: rendering falls back to user@host
        user = fake_imap.user([row])
        html = f'<p><a href="{LONG_URL}">x</a></p>'
        uid = fake_imap.mailbox.add_message("INBOX", make_message(body="\n", html=html))
        doc = _call(tools_mod._handle_get_messages, _ctx(user), {"folder": "INBOX", "uids": [uid]})
        assert "(#1#)" in doc
        res = _call(tools_mod._handle_get_message_urls, _ctx(user), {"folder": "INBOX", "uid": uid})
        assert res["urls"] == {"1": LONG_URL}
        assert res["account"] == "alice@example.com"

    def test_no_connections_text_is_neutral(self):
        assert "imap_not_connected" not in _NO_CONNECTIONS


# ---------------------------------------------------------------------------
# Cross-user isolation through the real dispatch, with DB users (finding 6)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_users(tmp_path, monkeypatch, fake_imap):
    """An isolated DB with two users, each with their own IMAP connection."""
    db_path = tmp_path / "quest.db"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)

    import db.models as models
    import db.user_service_credential_store as cred_store
    import db.user_store as user_store

    models.Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(cred_store, "AsyncSessionLocal", session_local)
    monkeypatch.setattr(user_store, "AsyncSessionLocal", session_local)

    other = fake_imap.add_server("mail.other.org")
    other.add_account("bob@other.org", "bob-pass")
    fake_imap.admin.set("imap.example.com\nmail.other.org")

    ids = {}
    for name, row in (
        ("a", fake_imap.row()),
        ("b", fake_imap.row(host="mail.other.org", username="bob@other.org",
                            password="bob-pass")),
    ):
        user = _run(user_store.create_user(
            email=f"{name}-{uuid.uuid4().hex[:8]}@example.com", name=name,
            api_key=f"k-{uuid.uuid4().hex}",
        ))
        _run(cred_store.create_connection(
            user["id"], "imap", account=row["account"], oauth_blob=row["oauth_blob"],
        ))
        ids[name] = user["id"]
    yield {
        "a": _run(user_store.get_user_by_id(ids["a"])),
        "b": _run(user_store.get_user_by_id(ids["b"])),
        "other_server": other,
    }
    sync_engine.dispose()


class TestCrossUserIsolation:
    def test_user_cannot_name_another_users_account(self, imap_plugin, fake_imap, db_users):
        user_a = db_users["a"]
        assert [r["account"] for r in user_a["service_connections"]["imap"]] == [
            "alice@example.com"]
        for tool_name, arguments in (
            ("imap_search_messages", {}),
            ("imap_list_folders", {}),
            ("imap_get_messages", {"folder": "INBOX", "uids": [1]}),
        ):
            result_text, _ = _run(_dispatch_tool_call(
                app=None, provider=None, user=user_a, conversation_id="c1",
                timezone="UTC", tool_name="tool_call",
                args={"tool_name": tool_name,
                      "arguments": {**arguments, "connection": "bob@other.org"}},
            ))
            out = json.loads(result_text)
            assert out["error"] == "connection_not_found", tool_name
            assert out["available"] == ["alice@example.com"]
            assert "bob-pass" not in result_text
        assert db_users["other_server"].commands == []

        # B's own default works and reaches B's server only.
        result_text, _ = _run(_dispatch_tool_call(
            app=None, provider=None, user=db_users["b"], conversation_id="c2",
            timezone="UTC", tool_name="tool_call",
            args={"tool_name": "imap_search_messages", "arguments": {}},
        ))
        assert json.loads(result_text)["account"] == "bob@other.org"


# ---------------------------------------------------------------------------
# Script bridge (finding 6)
# ---------------------------------------------------------------------------


class TestScriptBridge:
    def test_get_messages_from_script_keeps_urls_and_writes_no_cache(
            self, imap_plugin, fake_imap, url_cache):
        from chat.gemini_api.script_tool_call import (
            ScriptToolCallRequest,
            script_tool_call_endpoint,
        )

        html = f'<p><a href="{LONG_URL}">x</a></p>'
        uid = fake_imap.mailbox.add_message("INBOX", make_message(
            subject="From a script", body="\n", html=html))
        response = _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="imap_get_messages",
                                  arguments={"folder": "INBOX", "uids": [uid]}),
            user=_default_user(fake_imap), lease=None,
        ))
        assert response.status_code == 200 and response.media_type == "text/plain"
        body = response.body.decode()
        assert "Subject: From a script" in body
        assert LONG_URL in body and "(#1#)" not in body
        assert not url_cache.exists()

        response = _run(script_tool_call_endpoint(
            ScriptToolCallRequest(tool_name="imap_save_attachment",
                                  arguments={"folder": "INBOX", "uid": uid,
                                             "attachment_index": 1}),
            user=_default_user(fake_imap), lease=None,
        ))
        assert response.status_code == 400
