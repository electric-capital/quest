"""IMAP transport (plugins/imap/upstream.py) over the fake imaplib."""

import asyncio
import imaplib
import logging
import re
import ssl
import time

import pytest

from plugins.imap import upstream
from plugins.imap.tests import fake_imaplib
from plugins.imap.tests.fake_imaplib import (
    DEFAULT_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    make_message,
)
from plugins.imap.upstream import ImapError, ImapSettings, run_imap


def _values(**overrides):
    values = {
        "host": DEFAULT_HOST, "port": "", "security": "ssl",
        "username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD,
    }
    values.update(overrides)
    return values


def _settings(**overrides) -> ImapSettings:
    return upstream.settings_from_values(_values(**overrides))


def _run(settings, work, **kw):
    return asyncio.run(run_imap(settings, work, **kw))


def _verify(**overrides):
    return asyncio.run(upstream.verify_imap_connection(_values(**overrides)))


def _commands(fake_imap):
    return [c[0] for c in fake_imap.server.commands]


# -- verify ---------------------------------------------------------------


def test_verify_success_records_capabilities(fake_imap):
    result = _verify()
    assert result.error is None
    assert result.account == "alice@example.com"
    assert result.extras["capabilities"] == list(fake_imaplib.POST_LOGIN_CAPABILITIES)
    assert result.extras["verified_at"]
    assert _commands(fake_imap) == ["CONNECT", "LOGIN", "CAPABILITY", "LOGOUT"]


def test_verify_account_derivation(fake_imap):
    fake_imap.server.add_account("Bob", "pw-bob")
    result = _verify(username="Bob", password="pw-bob")
    assert result.account == "bob@imap.example.com"
    fake_imap.server.add_account("Carol@Example.COM", "pw-carol")
    assert _verify(username="Carol@Example.COM", password="pw-carol").account == "carol@example.com"


@pytest.mark.parametrize("username", ['bad user', 'quote"user', "tab\tuser"])
def test_verify_rejects_unusable_account_before_connecting(fake_imap, username):
    result = _verify(username=username)
    assert result.account is None
    assert "cannot be used as an account name" in result.error
    assert fake_imap.server.commands == []


def test_verify_wrong_password(fake_imap, caplog):
    caplog.set_level(logging.DEBUG)
    result = _verify(password="wrong-password-123")
    assert result.account is None
    assert result.error.startswith("Login failed:")
    assert "AUTHENTICATIONFAILED" in result.error
    assert "wrong-password-123" not in result.error
    assert "wrong-password-123" not in caplog.text
    assert _commands(fake_imap)[-1] == "LOGOUT"


def test_verify_allow_list(fake_imap):
    assert _verify(host="other.example.com").error == "Server not allowed"
    assert _verify(port="1993").error == "Port 1993 is not allowed for imap.example.com"
    assert _verify(port="nope").error == "Port must be a number between 1 and 65535."
    assert fake_imap.server.commands == []


def test_verify_connect_error(fake_imap):
    fake_imap.server.connect_error = ConnectionRefusedError(111, "Connection refused")
    result = _verify()
    assert result.error == "Could not connect to imap.example.com:993: Connection refused"


def test_verify_tls_error(fake_imap):
    fake_imap.server.connect_error = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    result = _verify()
    assert result.error.startswith("Could not connect to imap.example.com:993: ")
    assert result.error.count("Could not connect") == 1
    assert "CERTIFICATE_VERIFY_FAILED" in result.error


# -- TLS ------------------------------------------------------------------


def _spy_contexts(monkeypatch):
    made = []
    real = ssl.create_default_context

    def _spy(*args, **kwargs):
        ctx = real(*args, **kwargs)
        made.append(ctx)
        return ctx

    monkeypatch.setattr(upstream.ssl, "create_default_context", _spy)
    return made


def test_ssl_uses_default_context(fake_imap, monkeypatch):
    made = _spy_contexts(monkeypatch)
    _run(_settings(), lambda s: None)
    conn = fake_imap.server.connections[0]
    assert conn.implicit_tls and conn.port == 993
    assert conn.ssl_context is made[0]
    assert conn.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert conn.ssl_context.check_hostname is True
    assert conn.ssl_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert conn.timeout == upstream.SOCKET_TIMEOUT


def test_starttls_uses_default_context(fake_imap, monkeypatch):
    made = _spy_contexts(monkeypatch)
    _run(_settings(security="starttls"), lambda s: None)
    conn = fake_imap.server.connections[0]
    assert not conn.implicit_tls and conn.port == 143
    assert conn.starttls_context is made[0]
    assert conn.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert conn.starttls_context.check_hostname is True
    assert conn.starttls_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert _commands(fake_imap)[:3] == ["CONNECT", "STARTTLS", "LOGIN"]


def test_tls_context_floor_and_verification():
    ctx = upstream.make_tls_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_starttls_not_advertised_is_refused(fake_imap):
    fake_imap.server.advertise_starttls = False
    with pytest.raises(ImapError) as exc:
        _run(_settings(security="starttls"), lambda s: None)
    assert exc.value.code == "imap_connect_failed"
    assert "STARTTLS" in exc.value.message
    assert "LOGIN" not in _commands(fake_imap)
    assert fake_imap.server.connections[0].sock.shutdown_calls == 1


def test_starttls_failure(fake_imap):
    fake_imap.server.starttls_error = ssl.SSLError(1, "wrong version number")
    with pytest.raises(ImapError) as exc:
        _run(_settings(security="starttls"), lambda s: None)
    assert exc.value.code == "imap_connect_failed"
    assert "wrong version number" in exc.value.message
    assert "LOGIN" not in _commands(fake_imap)


def test_connect_dns_failure(fake_imap):
    fake_imap.admin.set("imap.example.com\nnowhere.example.com")
    with pytest.raises(ImapError) as exc:
        _run(_settings(host="nowhere.example.com"), lambda s: None)
    assert exc.value.code == "imap_connect_failed"
    assert re.search(r"Could not connect to nowhere\.example\.com:993\b",
                     exc.value.message)


# -- allow-list re-check ------------------------------------------------------


def test_allow_list_rechecked_on_every_connect(fake_imap):
    settings = _settings()
    assert _run(settings, lambda s: "ok") == "ok"
    reads = fake_imap.admin.reads
    fake_imap.admin.set("other.example.com")  # the admin removes the host
    with pytest.raises(ImapError) as exc:
        _run(settings, lambda s: "ok")
    assert exc.value.code == "imap_host_not_allowed"
    assert fake_imap.admin.reads > reads
    assert _commands(fake_imap).count("CONNECT") == 1
    fake_imap.admin.clear()
    with pytest.raises(ImapError) as exc:
        _run(settings, lambda s: "ok")
    assert exc.value.code == "imap_host_not_allowed"
    fake_imap.admin.set("imap.example.com")
    assert _run(settings, lambda s: "ok") == "ok"


def test_allow_list_port_check(fake_imap):
    fake_imap.admin.set("imap.example.com:143")
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: None)
    assert exc.value.code == "imap_host_not_allowed"
    assert exc.value.to_dict()["port"] == 993
    assert _run(_settings(security="starttls"), lambda s: "ok") == "ok"


# -- auth -------------------------------------------------------------------


def test_auth_failure_and_password_hygiene(fake_imap, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "Sup3r-Secret-PW"
    settings = _settings(password=secret)
    assert secret not in repr(settings) and secret not in str(settings)
    with pytest.raises(ImapError) as exc:
        _run(settings, lambda s: None)
    assert exc.value.code == "imap_auth_failed"
    for text in (str(exc.value), repr(exc.value), repr(exc.value.to_dict()), caplog.text,
                 fake_imap.server.command_text()):
        assert secret not in text
    # imaplib's sent-command ring buffer (holds LOGIN) is cleared.
    assert fake_imap.server.connections[0]._cmd_log == {}


def test_server_echoing_password_is_scrubbed(fake_imap, monkeypatch):
    secret = "echoed-secret"

    def _echo(self, user, password):
        raise imaplib.IMAP4.error(f"LOGIN {user} {password} rejected".encode())

    monkeypatch.setattr(fake_imaplib.FakeIMAP4, "login", _echo)
    with pytest.raises(ImapError) as exc:
        _run(_settings(password=secret), lambda s: None)
    assert secret not in exc.value.message
    assert "***" in exc.value.message


def test_non_ascii_password_uses_authenticate_plain(fake_imap):
    fake_imap.server.add_account("dora@example.com", "pässwörd")
    assert _run(_settings(username="dora@example.com", password="pässwörd"), lambda s: "in") == "in"
    assert "AUTHENTICATE" in _commands(fake_imap)
    assert "LOGIN" not in _commands(fake_imap)


def test_non_ascii_password_without_auth_plain(fake_imap, monkeypatch):
    monkeypatch.setattr(fake_imaplib, "BASE_CAPABILITIES", ("IMAP4REV1",))
    with pytest.raises(ImapError) as exc:
        _run(_settings(password="pässwörd"), lambda s: None)
    assert exc.value.code == "imap_auth_failed"
    assert "non-ASCII" in exc.value.message
    assert "pässwörd" not in exc.value.message


@pytest.mark.parametrize("password", ["pw\r\nA2 DELETE INBOX", "pw\0x"])
def test_crlf_in_credentials_refused(fake_imap, password):
    with pytest.raises(ImapError) as exc:
        _run(_settings(password=password), lambda s: None)
    assert exc.value.code == "imap_auth_failed"
    assert "LOGIN" not in _commands(fake_imap)


# -- session ----------------------------------------------------------------


def test_list_status_examine(fake_imap):
    fake_imap.mailbox.add_folder("Entwürfe", special_use="\\Drafts")
    fake_imap.mailbox.add_message("INBOX", make_message())
    fake_imap.mailbox.add_message("INBOX", make_message(), flags={"\\Seen"})

    def work(s):
        return s.list_folders(), s.list_folders(limit=1), s.status("INBOX"), s.examine("Entwürfe")

    folders, limited, status, uidvalidity = _run(_settings(), work)
    assert [f["name"] for f in folders] == ["INBOX", "Entwürfe"]
    assert folders[1]["raw_name"] == "Entw&APw-rfe"
    assert folders[1]["special_use"] == "\\Drafts"
    assert len(limited) == 1
    assert status == {"messages": 2, "unseen": 1, "uidvalidity": 1}
    assert uidvalidity == 1
    assert ("EXAMINE", '"Entw&APw-rfe"') in fake_imap.server.commands
    assert "SELECT" not in _commands(fake_imap)


def test_folder_not_found(fake_imap):
    for work in (lambda s: s.examine("Nope"), lambda s: s.status("Nope")):
        with pytest.raises(ImapError) as exc:
            _run(_settings(), work)
        assert exc.value.code == "imap_folder_not_found"
        assert exc.value.to_dict()["folder"] == "Nope"
    assert _commands(fake_imap).count("LOGOUT") == 2


def test_folder_name_injection_refused(fake_imap):
    with pytest.raises(ValueError):
        _run(_settings(), lambda s: s.examine('INBOX"\r\nA9 DELETE "INBOX'))
    assert "DELETE" not in fake_imap.server.command_text()


def test_uidvalidity_change(fake_imap):
    settings = _settings()
    first = _run(settings, lambda s: s.check_uidvalidity("INBOX", None))
    assert _run(settings, lambda s: s.check_uidvalidity("INBOX", first)) == first
    fake_imap.mailbox.bump_uidvalidity("INBOX")
    with pytest.raises(ImapError) as exc:
        _run(settings, lambda s: s.check_uidvalidity("INBOX", first))
    assert exc.value.code == "imap_uidvalidity_changed"
    assert exc.value.to_dict()["expected"] == first
    assert exc.value.to_dict()["current"] == first + 1


def test_search(fake_imap):
    mb = fake_imap.mailbox
    u1 = mb.add_message("INBOX", make_message(subject='Say "hi"', sender="Bob <bob@example.org>"))
    u2 = mb.add_message("INBOX", make_message(subject="Grüße aus Köln", sender="Jörg <j@example.de>"),
                        flags={"\\Seen"})
    u3 = mb.add_message("INBOX", make_message(subject="Köln Grüße again", body="Grüße",
                                                 sender="Eve <eve@example.net>"))

    def work(s):
        s.examine("INBOX")
        return (
            s.uid_search([]),
            s.uid_search(["UNSEEN"]),
            s.uid_search([("SUBJECT", 'say "hi"')]),
            s.uid_search([("SUBJECT", "grüße"), ("SUBJECT", "köln")]),
            s.uid_search([("SUBJECT", "grüße"), "UNSEEN"]),
            s.uid_search([("FROM", "bob"), ("SINCE", "01-Oct-2026"), ("LARGER", 10)]),
        )

    all_, unseen, quoted, both, unseen_umlaut, combo = _run(_settings(), work)
    assert all_ == [u1, u2, u3]
    assert unseen == [u1, u3]
    assert quoted == [u1]
    assert both == [u2, u3]
    assert unseen_umlaut == [u3]
    assert combo == [u1]
    text = fake_imap.server.command_text()
    assert 'UID SEARCH SUBJECT "say \\"hi\\""' in text
    assert "UID SEARCH CHARSET UTF-8 UNSEEN SUBJECT <literal>" in text


@pytest.mark.parametrize("terms", [
    [("SUBJECT", "x\r\nA1 LOGOUT")],
    [("FROM", "a\nb")],
    ["UNSEEN\r\n"],
    ["UNSEEN\n"],
    ["bad atom"],
    [("SUBJECT",)],
    [("SINCE", True)],
])
def test_search_rejects_unsafe_terms(fake_imap, terms):
    with pytest.raises(ValueError):
        _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_search(terms)))
    assert "UID SEARCH" not in fake_imap.server.command_text()


def test_protocol_error_mapping(fake_imap):
    fake_imap.mailbox.add_message("INBOX", make_message())
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_search(["BOGUS"])))
    assert exc.value.code == "imap_protocol_error"
    assert _commands(fake_imap)[-1] == "LOGOUT"


@pytest.mark.parametrize("items", [
    "BODY[]", "(UID BODY[TEXT])", "body[1]", "BODY[HEADER]<0.100>", "RFC822", "RFC822.TEXT",
    "(FLAGS RFC822)", "BINARY[1]", "FLAGS\n", "UID\r\nA1 STORE 1 +FLAGS (\\Deleted)", "",
])
def test_uid_fetch_refuses_items_that_set_seen(fake_imap, items):
    uid = fake_imap.mailbox.add_message("INBOX", make_message())
    with pytest.raises(ValueError):
        _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_fetch([uid], items)))
    assert "UID FETCH" not in fake_imap.server.command_text()
    assert fake_imap.mailbox.folders["INBOX"].messages[uid].flags == set()


def test_uid_fetch_peek_never_sets_seen(fake_imap):
    raw = make_message(subject="Invoice", attachments=(("a.pdf", "application/pdf", b"%PDF-1.4"),))
    uid = fake_imap.mailbox.add_message("INBOX", raw)
    items = ("UID FLAGS RFC822.SIZE RFC822.HEADER INTERNALDATE ENVELOPE BODYSTRUCTURE BODY "
             "BODY.PEEK[] BODY.PEEK[HEADER] BODY.PEEK[2]<0.4> BINARY.PEEK[1]")

    def work(s):
        s.examine("INBOX")
        return s.uid_fetch([uid, uid, 999], items)

    got = _run(_settings(), work)
    assert list(got) == [uid]
    msg = got[uid]
    assert msg["BODY[]"] == raw
    assert msg["BODY[HEADER]"].startswith(b"Subject: Invoice")
    assert "BODY[2]<0>" in msg
    assert msg["RFC822.SIZE"] == len(raw)
    assert msg["FLAGS"] == []
    assert fake_imap.mailbox.folders["INBOX"].messages[uid].flags == set()
    fetches = [c for c in fake_imap.server.commands if c[0] == "UID FETCH"]
    assert fetches == [("UID FETCH", f"{uid},999", f"({items})")]


def test_fake_sets_seen_on_non_peek_fetch(fake_imap):
    """Guard for the guard: the fake really would mark a message read."""
    uid = fake_imap.mailbox.add_message("INBOX", make_message())
    imap4, _ = fake_imap.classes()
    conn = imap4(DEFAULT_HOST, 143)
    conn.starttls()
    conn.login(DEFAULT_USERNAME, DEFAULT_PASSWORD)
    conn.select('"INBOX"', readonly=True)
    conn.uid("FETCH", str(uid), "(BODY[])")
    assert "\\Seen" in fake_imap.mailbox.folders["INBOX"].messages[uid].flags


@pytest.mark.parametrize("uids", [[0], [-1], ["5"], [True]])
def test_uid_fetch_rejects_bad_uids(fake_imap, uids):
    with pytest.raises(ValueError):
        _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_fetch(uids, "FLAGS")))


# -- timeout ------------------------------------------------------------------


@pytest.mark.parametrize("hang_on", ["LOGIN", "FETCH"])
def test_timeout_shuts_socket_and_unblocks_thread(fake_imap, hang_on):
    uid = fake_imap.mailbox.add_message("INBOX", make_message())
    fake_imap.server.hang_on = hang_on
    started = time.monotonic()
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_fetch([uid], "FLAGS")),
             timeout=0.3)
    assert exc.value.code == "imap_timeout"
    assert time.monotonic() - started < 5
    conn = fake_imap.server.connections[0]
    assert conn.sock.shutdown_calls >= 1
    # Shut down from outside, so the hung worker thread unblocks at once.
    assert conn.sock.closed.is_set()


def test_socket_timeout_during_work_maps_to_imap_timeout(fake_imap):
    def work(s):
        raise TimeoutError("timed out")

    with pytest.raises(ImapError) as exc:
        _run(_settings(), work)
    assert exc.value.code == "imap_timeout"


def test_work_value_error_propagates_and_logs_out(fake_imap):
    def work(s):
        raise ValueError("bug")

    with pytest.raises(ValueError):
        _run(_settings(), work)
    assert _commands(fake_imap)[-1] == "LOGOUT"


# -- settings objects -----------------------------------------------------------


def test_settings_from_row_and_errors(fake_imap):
    row = fake_imap.row(port="1993", security="starttls", account="alias@example.com")
    s = upstream.settings_from_row(row)
    assert (s.host, s.port, s.security, s.account) == (DEFAULT_HOST, 1993, "starttls", "alias@example.com")
    for values in ({}, _values(security="plain"), _values(port="x"), _values(password="")):
        with pytest.raises(ImapError) as exc:
            upstream.settings_from_values(values)
        assert exc.value.code == "imap_not_configured"


def test_imap_error_to_dict():
    err = ImapError("imap_uidvalidity_changed", "changed", expected=1, current=2)
    assert err.to_dict() == {
        "error": "imap_uidvalidity_changed", "message": "changed", "expected": 1, "current": 2,
    }


# -- review fixes: hostile servers, bounds, connect cancellation ----------------


def test_hostile_fetch_response_is_protocol_error_not_value_error(fake_imap, monkeypatch):
    uid = fake_imap.mailbox.add_message("INBOX", make_message())

    def hostile(self, uid_spec, items_text):
        return "OK", [b"1 (UID 1 BODYSTRUCTURE " + b"(" * 5000 + b")" * 5000 + b")"]

    monkeypatch.setattr(fake_imaplib.FakeIMAP4, "_fetch", hostile)
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_fetch([uid], "BODYSTRUCTURE")))
    assert exc.value.code == "imap_protocol_error"
    assert "parse" in exc.value.message


def test_hostile_status_and_huge_uid_tokens(fake_imap, monkeypatch):
    def status(self, mailbox, names):
        return "OK", [b'"INBOX" (MESSAGES ' + b"9" * 5000 + b" UNSEEN \xc2\xb2 UIDVALIDITY 3)"]

    def uid(self, command, *args):
        return "OK", [b"1 2 " + b"9" * 5000 + " ²".encode()]

    monkeypatch.setattr(fake_imaplib.FakeIMAP4, "status", status)
    got = _run(_settings(), lambda s: s.status("INBOX"))
    assert got == {"messages": 0, "unseen": 0, "uidvalidity": 3}
    monkeypatch.setattr(fake_imaplib.FakeIMAP4, "uid", uid)
    assert _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_search([]))[1]) == [1, 2]


def test_binary_literal8_through_fake(fake_imap):
    raw = make_message(attachments=(("a.bin", "application/octet-stream", b"\x00\x01binary"),))
    uid = fake_imap.mailbox.add_message("INBOX", raw)
    got = _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_fetch([uid], "BINARY.PEEK[1]"))[1])
    assert isinstance(got[uid]["BINARY[1]"], bytes) and got[uid]["BINARY[1]"].startswith(b"Hello")
    # The fake sends the literal8 marker, as imaplib hands it over.
    conn = fake_imap.server.connections[0]
    conn.state, conn.selected = "SELECTED", fake_imap.mailbox.folders["INBOX"]
    _typ, data = conn.uid("FETCH", str(uid), "(UID BINARY.PEEK[1])")
    assert data[0][0].endswith(b"BINARY[1] ~{%d}" % len(data[0][1]))


def test_unsolicited_fetch_and_uid_last_ordering(fake_imap):
    mb = fake_imap.mailbox
    u1 = mb.add_message("INBOX", make_message(subject="one"))
    u2 = mb.add_message("INBOX", make_message(subject="two"))
    fake_imap.server.unsolicited_fetch = True
    fake_imap.server.uid_last = True
    got = _run(_settings(), lambda s: (s.examine("INBOX"),
                                         s.uid_fetch([u1, u2], "(UID FLAGS BODY.PEEK[HEADER])"))[1])
    assert list(got) == [u1, u2]
    assert got[u2]["BODY[HEADER]"].startswith(b"Subject: two")
    assert got[u1]["FLAGS"] == []


def test_capabilities_use_newest_reply_and_dedupe(fake_imap, monkeypatch):
    # The fake already returns the login response code AND the reply.
    caps = _run(_settings(), lambda s: s.capabilities)
    assert caps == fake_imaplib.POST_LOGIN_CAPABILITIES
    monkeypatch.setattr(
        fake_imaplib.FakeIMAP4, "capability",
        lambda self: ("OK", [b"IMAP4REV1 OLD", b"IMAP4rev1 IDLE IDLE MOVE idle", None]),
    )
    assert _run(_settings(), lambda s: s.capabilities) == ("IMAP4REV1", "IDLE", "MOVE")


def test_username_is_quoted(fake_imap):
    odd = 'we(ird) %user* "x"'
    fake_imap.server.add_account(odd, "pw-odd")
    assert _run(_settings(username=odd, password="pw-odd"), lambda s: "in") == "in"
    assert ("LOGIN", odd) in fake_imap.server.commands
    # The fake really rejects an unquoted name with specials (guard for the guard).
    imap4, _ = fake_imap.classes()
    conn = imap4(DEFAULT_HOST, 143)
    conn.starttls()
    with pytest.raises(imaplib.IMAP4.error):
        conn.login(odd, "pw-odd")


def test_not_or_refused_with_literal_terms(fake_imap):
    fake_imap.mailbox.add_message("INBOX", make_message(subject="Grüße"))
    for terms in (["NOT", ("SUBJECT", "grüße")], ["OR", ("FROM", "jörg"), ("SUBJECT", "köln")]):
        with pytest.raises(ValueError):
            _run(_settings(), lambda s: (s.examine("INBOX"), s.uid_search(terms)))
    assert "UID SEARCH" not in fake_imap.server.command_text()


def test_examine_falls_back_to_status_for_uidvalidity(fake_imap):
    fake_imap.mailbox.folders["INBOX"].uidvalidity = 42
    fake_imap.server.omit_uidvalidity_code = True
    assert _run(_settings(), lambda s: s.examine("INBOX")) == 42
    assert ("STATUS", '"INBOX"', "(UIDVALIDITY)") in fake_imap.server.commands


def test_non_canonical_raw_folder_names(fake_imap):
    fake_imap.mailbox.add_folder("éé", raw_name="&AOk-&AOk-", uidvalidity=9)

    def work(s):
        listed = [f for f in s.list_folders() if f["name"] == "éé"]
        return listed, s.examine("&AOk-&AOk-"), s.status("éé", raw_name="&AOk-&AOk-")

    listed, uidvalidity, status = _run(_settings(), work)
    assert listed[0]["raw_name"] == "&AOk-&AOk-"
    assert uidvalidity == 9 and status["uidvalidity"] == 9
    # The decoded name encodes canonically, which this server does not know.
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: s.examine("éé"))
    assert exc.value.code == "imap_folder_not_found"


def test_list_literal_name_with_trailer(fake_imap):
    fake_imap.mailbox.add_folder('Say "hi"')
    folders = _run(_settings(), lambda s: s.list_folders())
    assert [f["name"] for f in folders] == ["INBOX", 'Say "hi"']


def test_debug_level_never_reaches_transport(fake_imap, monkeypatch):
    monkeypatch.setattr(imaplib, "Debug", 5)
    _run(_settings(), lambda s: None)
    assert fake_imap.server.connections[0].debug == 0
    real = upstream.QuestIMAP4.__new__(upstream.QuestIMAP4)
    real.debug = 5  # what imaplib.IMAP4.__init__ does with imaplib.Debug
    assert real.debug == 0


def test_real_transport_classes_are_the_quest_subclasses():
    assert issubclass(upstream.QuestIMAP4, imaplib.IMAP4)
    assert issubclass(upstream.QuestIMAP4_SSL, imaplib.IMAP4_SSL)
    assert upstream.QuestIMAP4_SSL.debug is upstream.QuestIMAP4.debug


def _bare(cls, data: bytes):
    import io

    conn = cls.__new__(cls)
    conn.file = io.BytesIO(data)
    return conn


def test_long_response_line_beyond_imaplib_maxline():
    line = b"* SEARCH " + b" ".join(b"%d" % i for i in range(1, 200_001)) + b"\r\n"
    assert len(line) > imaplib._MAXLINE
    conn = _bare(upstream.QuestIMAP4, line)
    assert conn.readline() == line
    assert conn._quest_broken is False


def test_response_line_and_literal_caps(monkeypatch):
    monkeypatch.setattr(upstream, "MAX_LINE_BYTES", 100)
    conn = _bare(upstream.QuestIMAP4_SSL, b"x" * 200 + b"\r\n")
    with pytest.raises(imaplib.IMAP4.error):
        conn.readline()
    assert conn._quest_broken is True
    monkeypatch.setattr(upstream, "MAX_LITERAL_BYTES", 10)
    conn = _bare(upstream.QuestIMAP4, b"0123456789abc")
    assert conn.read(10) == b"0123456789"
    with pytest.raises(imaplib.IMAP4.error) as exc:
        conn.read(11)
    assert "literal" in str(exc.value)
    assert conn._quest_broken is True


def test_literal_cap_stays_above_save_limit():
    from plugins.imap import tools

    assert upstream.MAX_LITERAL_BYTES > tools.MAX_SAVE_MESSAGE_BYTES


def test_broken_connection_is_hard_closed_not_logged_out(fake_imap):
    def work(s):
        s._conn._quest_broken = True
        raise imaplib.IMAP4.error("command: FETCH => literal too large")

    with pytest.raises(ImapError) as exc:
        _run(_settings(), work)
    assert exc.value.code == "imap_protocol_error"
    assert "LOGOUT" not in _commands(fake_imap)
    assert fake_imap.server.connections[0].sock.closed.is_set()


def test_abort_is_hard_closed_not_logged_out(fake_imap):
    def work(s):
        raise imaplib.IMAP4.abort("socket error: EOF")

    with pytest.raises(ImapError) as exc:
        _run(_settings(), work)
    assert exc.value.code == "imap_connect_failed"
    assert "LOGOUT" not in _commands(fake_imap)


def test_timeout_does_not_send_logout(fake_imap):
    fake_imap.server.hang_on = "LIST"
    with pytest.raises(ImapError):
        _run(_settings(), lambda s: s.list_folders(), timeout=0.3)
    assert "LOGOUT" not in _commands(fake_imap)


def test_timeout_during_connect_unblocks_worker(fake_imap):
    fake_imap.server.hang_on = "CONNECT"
    started = time.monotonic()
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: None, timeout=0.3)
    assert exc.value.code == "imap_timeout"
    # The stuck constructor's socket was shut down from outside: the worker
    # thread leaves the connect phase at once instead of after 10 s.
    assert fake_imap.server.connect_exited.wait(3)
    assert time.monotonic() - started < 3
    assert "LOGIN" not in _commands(fake_imap)


def test_open_socket_registers_and_cancel_interrupts_connect(monkeypatch):
    """The real connect helper: a cancel shuts the socket a blocked connect waits on."""
    import threading

    class BlockingSocket:
        def __init__(self, *a):
            self.closed = threading.Event()

        def settimeout(self, t):
            pass

        def connect(self, address):
            self.closed.wait(5)
            raise OSError(9, "Bad file descriptor")

        def shutdown(self, how):
            self.closed.set()

        def close(self):
            self.closed.set()

    monkeypatch.setattr(upstream.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("192.0.2.1", 993))])
    monkeypatch.setattr(upstream.socket, "socket", BlockingSocket)
    state = upstream._CallState()
    outcome = {}

    def worker():
        upstream._connect_local.state = state
        started = time.monotonic()
        try:
            upstream._open_socket("imap.example.com", 993, 20)
        except OSError as exc:
            outcome["error"] = exc
        outcome["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.1)
    state.cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert isinstance(outcome["error"], OSError) and outcome["elapsed"] < 2
    # Registering after the cancel closes the socket and refuses at once.
    late = BlockingSocket()
    with pytest.raises(OSError):
        state.add_socket(late)
    assert late.closed.is_set()


def test_sessions_run_on_dedicated_bounded_pool(fake_imap):
    import threading

    names = _run(_settings(), lambda s: threading.current_thread().name)
    assert names.startswith("imap")
    assert upstream._EXECUTOR._max_workers == upstream.MAX_WORKERS


def test_connect_error_text_has_single_prefix(fake_imap):
    fake_imap.server.connect_error = ConnectionRefusedError(111, "Connection refused")
    with pytest.raises(ImapError) as exc:
        _run(_settings(), lambda s: None)
    assert exc.value.message == "Could not connect to imap.example.com:993: Connection refused"
    err = ssl.SSLCertVerificationError(1, "x")
    err.verify_message = "self-signed certificate"
    assert upstream._exc_text(err) == "certificate verify failed: self-signed certificate"
