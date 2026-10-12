"""IMAP transport (plugins/imap/upstream.py) over the fake imaplib."""

import asyncio
import imaplib
import logging
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
    assert result.error.startswith("Could not connect:")
    assert "Connection refused" in result.error


def test_verify_tls_error(fake_imap):
    fake_imap.server.connect_error = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    result = _verify()
    assert result.error.startswith("Could not connect:")
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
    assert conn.timeout == upstream.SOCKET_TIMEOUT


def test_starttls_uses_default_context(fake_imap, monkeypatch):
    made = _spy_contexts(monkeypatch)
    _run(_settings(security="starttls"), lambda s: None)
    conn = fake_imap.server.connections[0]
    assert not conn.implicit_tls and conn.port == 143
    assert conn.starttls_context is made[0]
    assert conn.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert _commands(fake_imap)[:3] == ["CONNECT", "STARTTLS", "LOGIN"]


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
    assert "nowhere.example.com:993" in exc.value.message


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
