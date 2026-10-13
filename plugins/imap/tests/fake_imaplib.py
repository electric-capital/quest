"""A fake ``imaplib`` for the IMAP plugin's tests (reusable by tool tests).

It emulates the parts of ``imaplib.IMAP4`` / ``imaplib.IMAP4_SSL`` that
plugins/imap/upstream.py uses, with imaplib's real return shapes:
``(typ, data)`` pairs, FETCH data as ``[(b'1 (UID 5 BODY[] {12}',
b'<literal>'), b')', ...]`` (several literals per message possible;
``BINARY[...]`` literals use the literal8 marker ``~{n}``), LIST lines
(literal tuples followed by imaplib's empty trailer line for awkward
names), STATUS, SEARCH, EXAMINE with UIDVALIDITY reached through
``response('UIDVALIDITY')`` (popped from ``untagged_responses`` exactly
like imaplib), CAPABILITY returning every collected untagged CAPABILITY
(the login ``[CAPABILITY ...]`` response code AND the reply, as imaplib
does), user names checked the way a server parses them (an unquoted name
with specials is a BAD command), ``debug`` initialised from
``imaplib.Debug``, and imaplib's exceptions (``imaplib.IMAP4.error`` /
``.abort``).

Model
-----
``FakeImapNetwork`` holds ``FakeImapServer``s by host. A server holds
accounts (``FakeMailbox``: password + folders); a folder holds a
UIDVALIDITY and ``{uid: FakeMessage(raw bytes, flags, internaldate)}``.
Every command any connection receives is appended to
``server.commands`` as a tuple (``("LOGIN", username)`` -- the password
is never recorded -- ``("EXAMINE", '"INBOX"')``, ``("UID FETCH", "5",
"(UID BODY.PEEK[])")``, ``("UID SEARCH", ...)``, ...), so a test can
assert PEEK-only access. A non-PEEK ``BODY[...]`` / ``BINARY[...]`` /
``RFC822`` / ``RFC822.TEXT`` fetch DOES set ``\\Seen`` (as a real server
would), so tests can prove the plugin never sends one.

Scripted failures (attributes on ``FakeImapServer``)
---------------------------------------------------
- ``connect_error``: an exception raised by the constructor (e.g.
  ``ConnectionRefusedError(111, "Connection refused")`` or an
  ``ssl.SSLCertVerificationError``).
- ``advertise_starttls`` (default True): when False, a plain
  connection's CAPABILITY lacks STARTTLS.
- ``starttls_error``: exception raised by ``starttls()``.
- ``auth_fail``: force every LOGIN / AUTHENTICATE to fail.
- ``hang_on``: a command name (``"LOGIN"``, ``"SEARCH"``, ``"FETCH"``,
  ``"LIST"``, ``"EXAMINE"``) that blocks until the socket is shut down
  from outside (then raises ``imaplib.IMAP4.abort('socket error: EOF')``)
  -- the timeout path. ``"CONNECT"`` blocks inside the constructor (like
  a stuck TCP connect / TLS handshake) until the socket the fake
  registered through ``upstream.register_connect_socket`` is shut down,
  then raises ``OSError``; ``server.connect_exited`` is set when the
  constructor returns or raises.
- ``omit_uidvalidity_code``: EXAMINE sends no ``[UIDVALIDITY]`` code
  (the transport falls back to STATUS).
- ``unsolicited_fetch``: every UID FETCH reply starts with an unsolicited
  ``* n FETCH (FLAGS (...))`` for another message (no UID).
- ``uid_last``: FETCH responses put the ``UID`` item last.
- ``FakeFolder.raw_name``: a server-side name that is not the canonical
  modified UTF-7 encoding of ``name``; mailbox arguments are compared
  with the raw names byte for byte, like a real server.
- ``string_disposition``: emit BODYSTRUCTURE dispositions as a raw
  header string (as pymap does) instead of the RFC 3501 list form.
- folder missing: just don't create it (EXAMINE / STATUS answer NO).
- UIDVALIDITY change: ``mailbox.bump_uidvalidity("INBOX")``.

Fixtures (registered through plugins/imap/tests/conftest.py)
----------------------------------------------------------
- ``imap_admin_hosts``: an ``AdminHosts`` object;
  ``.set("a\\nb:143 starttls")`` sets the admin card's ``allowed_hosts``
  entries from legacy-style lines (one per line), ``.clear()`` removes the card. Patches ``config.service_credentials.read_service_credentials``
  for service ``imap`` only (others delegate to the real store).
- ``fake_imap``: a ``FakeImapNetwork`` with one server
  ``imap.example.com`` (``network.server``), account
  ``alice@example.com`` / ``s3cret-pass`` (``network.mailbox``) with an
  empty INBOX, admin card allowing ``imap.example.com`` (= the
  canonical ``DEFAULT_SERVER`` line ``imap.example.com:993 ssl``), and the
  plugin transport (``upstream.IMAP4_CLASS`` / ``IMAP4_SSL_CLASS``) routed
  to the network. Helpers: ``network.row(...)`` builds a stored connection
  row (``server`` / ``username`` / ``password`` values; ``values=`` stores
  arbitrary values instead, e.g. a legacy host/port/security row) and ``network.user(rows)`` a user dict in the db/user_store.py
  shape (``service_credentials`` + ``service_connections``), for tool
  tests resolving connections through config.user_connections.

Example (tool test)::

    def test_search(fake_imap, imap_plugin):
        uid = fake_imap.mailbox.add_message("INBOX", make_message(subject="Hi"))
        user = fake_imap.user([fake_imap.row(is_default=True)])
        ...  # call the tool handler with ctx.user = user
        assert not any("BODY[" in " ".join(c) for c in fake_imap.server.commands)
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Optional

import pytest

from plugins.imap import upstream
from plugins.imap.protocol import encode_folder_name

DEFAULT_HOST = "imap.example.com"
DEFAULT_SERVER = "imap.example.com:993 ssl"  # canonical admin line
DEFAULT_USERNAME = "alice@example.com"
DEFAULT_PASSWORD = "s3cret-pass"

BASE_CAPABILITIES = ("IMAP4REV1", "LITERAL+", "SASL-IR", "AUTH=PLAIN", "ID", "IDLE")
POST_LOGIN_CAPABILITIES = ("IMAP4REV1", "LITERAL+", "ID", "IDLE", "SPECIAL-USE", "UIDPLUS", "MOVE")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def make_message(
    *,
    subject: str = "Hello",
    sender: str = "Bob <bob@example.org>",
    to: str = "alice@example.com",
    body: str = "Hello there.\n",
    html: Optional[str] = None,
    date_header: Optional[str] = None,
    attachments: tuple = (),
) -> bytes:
    """Build a raw RFC 5322 message. ``attachments`` items are
    ``(filename, mime, bytes)``."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = date_header or "Mon, 05 Oct 2026 10:00:00 +0000"
    msg["Message-ID"] = email.utils.make_msgid(domain="example.org")
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, mime, data in attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes(policy=email.policy.SMTP)


@dataclass
class FakeMessage:
    raw: bytes
    flags: set = field(default_factory=set)
    internaldate: datetime = field(
        default_factory=lambda: datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
    )


@dataclass
class FakeFolder:
    name: str
    uidvalidity: int = 1
    messages: dict = field(default_factory=dict)  # uid -> FakeMessage
    next_uid: int = 1
    attributes: tuple = ("\\HasNoChildren",)
    delimiter: Optional[str] = "/"
    raw_name: Optional[str] = None  # non-canonical server-side name

    @property
    def wire_name(self) -> str:
        if self.name == "INBOX":
            return "INBOX"
        return self.raw_name or encode_folder_name(self.name)


class FakeMailbox:
    """One account's folders (keyed by decoded name)."""

    def __init__(self, password: str):
        self.password = password
        self.folders: dict[str, FakeFolder] = {}

    def add_folder(self, name: str, *, uidvalidity: int = 1, special_use: Optional[str] = None,
                   delimiter: Optional[str] = "/", raw_name: Optional[str] = None) -> FakeFolder:
        attrs = ("\\HasNoChildren",) + ((special_use,) if special_use else ())
        folder = FakeFolder(name=name, uidvalidity=uidvalidity, attributes=attrs, delimiter=delimiter,
                            raw_name=raw_name)
        self.folders[name] = folder
        return folder

    def add_message(self, folder: str, raw: bytes, *, flags=(), uid: Optional[int] = None,
                    internaldate: Optional[datetime] = None) -> int:
        f = self.folders[folder]
        if uid is None:
            uid = f.next_uid
        f.next_uid = max(f.next_uid, uid + 1)
        msg = FakeMessage(raw=raw, flags=set(flags))
        if internaldate is not None:
            msg.internaldate = internaldate
        f.messages[uid] = msg
        return uid

    def bump_uidvalidity(self, folder: str) -> int:
        self.folders[folder].uidvalidity += 1
        return self.folders[folder].uidvalidity


class FakeImapServer:
    def __init__(self, host: str):
        self.host = host
        self.accounts: dict[str, FakeMailbox] = {}
        self.commands: list[tuple] = []
        self.connections: list["FakeIMAP4"] = []
        # scripted failures
        self.connect_error: Optional[BaseException] = None
        self.advertise_starttls = True
        self.starttls_error: Optional[BaseException] = None
        self.auth_fail = False
        self.hang_on: Optional[str] = None
        self.string_disposition = False
        self.omit_uidvalidity_code = False
        self.unsolicited_fetch = False
        self.uid_last = False
        self.connect_exited = threading.Event()

    def add_account(self, username: str, password: str, *, inbox: bool = True) -> FakeMailbox:
        mailbox = FakeMailbox(password)
        if inbox:
            mailbox.add_folder("INBOX")
        self.accounts[username] = mailbox
        return mailbox

    @property
    def mailbox(self) -> FakeMailbox:
        return next(iter(self.accounts.values()))

    def record(self, *cmd) -> None:
        self.commands.append(tuple(cmd))

    def command_text(self) -> str:
        """Every recorded command, one per line (for substring asserts)."""
        return "\n".join(" ".join(str(p) for p in c) for c in self.commands)


class FakeImapNetwork:
    def __init__(self) -> None:
        self.servers: dict[str, FakeImapServer] = {}

    def add_server(self, host: str) -> FakeImapServer:
        server = FakeImapServer(host)
        self.servers[host] = server
        return server

    @property
    def server(self) -> FakeImapServer:
        return self.servers[DEFAULT_HOST]

    @property
    def mailbox(self) -> FakeMailbox:
        return self.server.accounts[DEFAULT_USERNAME]

    def lookup(self, host: str) -> FakeImapServer:
        server = self.servers.get(host)
        if server is None:
            import socket
            raise socket.gaierror(-2, "Name or service not known")
        return server

    def classes(self):
        network = self

        class _IMAP4(FakeIMAP4):
            def __init__(self, host="", port=143, timeout=None):
                super().__init__(network, host, port, timeout=timeout, implicit_tls=False)

        class _IMAP4_SSL(FakeIMAP4):
            def __init__(self, host="", port=993, *, ssl_context=None, timeout=None):
                super().__init__(network, host, port, timeout=timeout, implicit_tls=True,
                                 ssl_context=ssl_context)

        return _IMAP4, _IMAP4_SSL

    # -- row helpers for tool tests -----------------------------------

    def row(self, *, row_id: int = 1, username: str = DEFAULT_USERNAME,
            password: str = DEFAULT_PASSWORD, server: str = DEFAULT_SERVER,
            is_default: bool = True, account: Optional[str] = None,
            description: Optional[str] = None, values: Optional[dict] = None) -> dict:
        from plugins.imap.settings import account_for, parse_server_line
        if values is None:
            values = {"server": server, "username": username, "password": password}
            host = parse_server_line(server).host
        else:
            host = values.get("host") or DEFAULT_HOST
        return {
            "id": row_id, "service": "imap",
            "account": account or account_for(username, host),
            "description": description, "is_default": is_default, "secret": None,
            "oauth_blob": {"values": dict(values), "capabilities": list(POST_LOGIN_CAPABILITIES)},
            "created_at": f"2026-10-0{min(row_id, 9)}T00:00:00", "updated_at": None,
        }

    def user(self, rows: list[dict], *, email_addr: str = "u@example.com", user_id: int = 1) -> dict:
        user: dict = {"id": user_id, "email": email_addr}
        if rows:
            default = next((r for r in rows if r.get("is_default")), rows[0])
            user["service_credentials"] = {"imap": default}
            user["service_connections"] = {"imap": list(rows)}
        return user


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.shutdown_calls = 0

    def shutdown(self, how) -> None:
        self.shutdown_calls += 1
        self.closed.set()

    def close(self) -> None:
        self.closed.set()


def _q(value: Optional[str]) -> str:
    if value is None:
        return "NIL"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(arg: str) -> str:
    arg = arg.strip()
    if len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"':
        return re.sub(r'\\(.)', r'\1', arg[1:-1])
    return arg


def _split_args(text: str) -> list[str]:
    """Split a command argument string into tokens (quoted strings kept)."""
    tokens: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == " ":
            i += 1
        elif c == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            tokens.append(text[i:j + 1])
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] != " ":
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _imap_date(text: str) -> date:
    day, mon, year = text.split("-")
    return date(int(year), _MONTHS.index(mon.capitalize()) + 1, int(day))


def _split_raw(raw: bytes) -> tuple[bytes, bytes]:
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = raw.find(sep)
        if idx >= 0:
            return raw[: idx + len(sep)], raw[idx + len(sep):]
    return raw, b""


class FakeIMAP4:
    """Fake connection; see the module docstring."""

    error = imaplib.IMAP4.error
    abort = imaplib.IMAP4.abort
    readonly = imaplib.IMAP4.readonly

    def __init__(self, network: FakeImapNetwork, host: str, port: int, *, timeout=None,
                 implicit_tls: bool, ssl_context=None):
        server = network.lookup(host)
        self.server = server
        self.host = host
        self.port = port
        self.timeout = timeout
        self.implicit_tls = implicit_tls
        self.ssl_context = ssl_context
        self.starttls_context = None
        self.tls = implicit_tls
        self.sock = _FakeSocket()
        self.literal: Optional[bytes] = None
        self.untagged_responses: dict = {}
        self.state = "NONAUTH"
        self.mailbox: Optional[FakeMailbox] = None
        self.selected: Optional[FakeFolder] = None
        self.logged_out = False
        self._cmd_log: dict = {}
        self.debug = imaplib.Debug  # as imaplib.IMAP4.__init__ does
        self._login_caps: Optional[bytes] = None
        server.record("CONNECT", host, port, "TLS" if implicit_tls else "PLAIN")
        try:
            # Like upstream.QuestIMAP4: register the socket before blocking.
            upstream.register_connect_socket(self.sock)
            if server.hang_on == "CONNECT":
                if not self.sock.closed.wait(10):
                    raise TimeoutError("timed out")
                raise OSError(9, "Bad file descriptor")
            if server.connect_error is not None:
                raise server.connect_error
        finally:
            server.connect_exited.set()
        server.connections.append(self)

    # -- capability --------------------------------------------------

    @property
    def capabilities(self) -> tuple:
        caps = list(BASE_CAPABILITIES)
        if not self.tls and self.server.advertise_starttls:
            caps.append("STARTTLS")
        if not self.tls:
            caps.append("LOGINDISABLED")
        return tuple(caps)

    def capability(self):
        self.server.record("CAPABILITY")
        caps = POST_LOGIN_CAPABILITIES if self.state != "NONAUTH" else self.capabilities
        # imaplib returns every untagged CAPABILITY collected so far: the
        # login response code first, then this reply.
        collected = self.untagged_responses.pop("CAPABILITY", [])
        return "OK", [*collected, " ".join(caps).encode()]

    def _maybe_hang(self, name: str) -> None:
        if self.server.hang_on == name:
            self.sock.closed.wait(10)
            raise self.abort("socket error: EOF")

    def starttls(self, ssl_context=None):
        self.server.record("STARTTLS")
        if "STARTTLS" not in self.capabilities:
            raise self.abort("TLS not supported by server")
        if self.server.starttls_error is not None:
            raise self.server.starttls_error
        self.starttls_context = ssl_context
        self.tls = True
        return "OK", [b"Begin TLS negotiation now"]

    # -- auth --------------------------------------------------------

    def _auth(self, username: str, password: str):
        mailbox = self.server.accounts.get(username)
        if self.server.auth_fail or mailbox is None or mailbox.password != password:
            raise self.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.mailbox = mailbox
        self.state = "AUTH"
        code = " ".join(POST_LOGIN_CAPABILITIES)
        self.untagged_responses.setdefault("CAPABILITY", []).append(code.encode())
        return "OK", [f"[CAPABILITY {code}] Logged in".encode()]

    def login(self, user, password):
        # imaplib sends ``user`` verbatim (it quotes only the password); a
        # server parses an unquoted name as an atom.
        if user.startswith('"'):
            user = _unquote(user)
        elif not user or re.search(r'[\s(){%*"\\\]\x00-\x1f]', user):
            self.server.record("LOGIN", user)
            raise self.error(b"LOGIN command error: BAD [b'Invalid user name argument']")
        self.server.record("LOGIN", user)
        self._maybe_hang("LOGIN")
        if not self.tls:
            raise self.error(b"[PRIVACYREQUIRED] Plaintext authentication disallowed")
        # Real imaplib encodes command arguments as ASCII.
        str(user).encode("ascii")
        str(password).encode("ascii")
        self._cmd_log[0] = (b"LOGIN " + user.encode() + b" " + password.encode(), 0.0)
        return self._auth(user, password)

    def authenticate(self, mechanism, authobject):
        self.server.record("AUTHENTICATE", mechanism)
        if mechanism.upper() != "PLAIN":
            raise self.error(b"Unsupported mechanism")
        payload = authobject(b"")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        _authzid, user, password = payload.split(b"\0", 2)
        return self._auth(user.decode("utf-8"), password.decode("utf-8"))

    def logout(self):
        self.server.record("LOGOUT")
        self.logged_out = True
        self.state = "LOGOUT"
        return "BYE", [b"Logging out"]

    def shutdown(self):
        self.sock.shutdown(2)

    def response(self, code):
        return code, self.untagged_responses.pop(code.upper(), [None])

    # -- mailbox commands ----------------------------------------------

    def _require_auth(self, name):
        if self.state == "NONAUTH":
            raise self.error(f"command {name} illegal in state NONAUTH")

    def _folder(self, quoted: str) -> Optional[FakeFolder]:
        """Raw (wire) name comparison, INBOX case-insensitive."""
        raw = _unquote(quoted)
        if not self.mailbox:
            return None
        if raw.upper() == "INBOX":
            return self.mailbox.folders.get("INBOX")
        for folder in self.mailbox.folders.values():
            if folder.wire_name == raw:
                return folder
        return None

    def list(self, directory='""', pattern="*"):
        self._require_auth("LIST")
        self.server.record("LIST", directory, pattern)
        self._maybe_hang("LIST")
        data: list = []
        for folder in self.mailbox.folders.values():
            raw = folder.wire_name
            attrs = " ".join(folder.attributes)
            delim = _q(folder.delimiter)
            if '"' in raw or "\\" in raw:
                data.append((f"({attrs}) {delim} {{{len(raw)}}}".encode(), raw.encode()))
                data.append(b"")  # imaplib's trailer line after the literal
            else:
                data.append(f"({attrs}) {delim} {_q(raw)}".encode())
        return "OK", data

    def status(self, mailbox, names):
        self._require_auth("STATUS")
        self.server.record("STATUS", mailbox, names)
        folder = self._folder(mailbox)
        if folder is None:
            return "NO", [b"[NONEXISTENT] Mailbox doesn't exist"]
        unseen = sum(1 for m in folder.messages.values() if "\\Seen" not in m.flags)
        return "OK", [
            f'{mailbox} (MESSAGES {len(folder.messages)} UNSEEN {unseen} '
            f'UIDVALIDITY {folder.uidvalidity})'.encode()
        ]

    def select(self, mailbox="INBOX", readonly=False):
        self._require_auth("SELECT")
        self.untagged_responses = {}
        name = "EXAMINE" if readonly else "SELECT"
        self.server.record(name, mailbox)
        self._maybe_hang(name)
        folder = self._folder(mailbox)
        if folder is None:
            self.state = "AUTH"
            return "NO", [b"[NONEXISTENT] Mailbox doesn't exist"]
        self.selected = folder
        self.state = "SELECTED"
        self.untagged_responses["EXISTS"] = [str(len(folder.messages)).encode()]
        if not self.server.omit_uidvalidity_code:
            self.untagged_responses["UIDVALIDITY"] = [str(folder.uidvalidity).encode()]
        if readonly:
            self.untagged_responses["READ-ONLY"] = [b""]
        return "OK", [str(len(folder.messages)).encode()]

    def uid(self, command, *args):
        command = command.upper()
        literal, self.literal = self.literal, None
        self.server.record(f"UID {command}", *args, *(("<literal>",) if literal is not None else ()))
        if self.state != "SELECTED":
            raise self.error(f"command UID illegal in state {self.state}")
        self._maybe_hang(command)
        if command == "SEARCH":
            return self._search(list(args), literal)
        if command == "FETCH":
            return self._fetch(args[0], args[1])
        raise self.error(f"UID command error: BAD [b'Unknown command {command}']")

    # -- SEARCH --------------------------------------------------------

    def _search(self, args: list, literal: Optional[bytes]):
        tokens = _split_args(" ".join(str(a) for a in args))
        values: list = [_unquote(t) if t.startswith('"') else t for t in tokens]
        if literal is not None:
            values.append(literal.decode("utf-8"))
        if values[:1] and str(values[0]).upper() == "CHARSET":
            values = values[2:]
        matches = []
        for uid, msg in sorted(self.selected.messages.items()):
            if self._match(values, msg):
                matches.append(uid)
        return "OK", [" ".join(str(u) for u in matches).encode()]

    def _match(self, values: list, msg: FakeMessage) -> bool:
        parsed = email.message_from_bytes(msg.raw, policy=email.policy.default)
        i = 0
        while i < len(values):
            key = str(values[i]).upper()
            i += 1
            if key == "ALL":
                continue
            if key in ("SEEN", "UNSEEN", "FLAGGED", "UNFLAGGED", "ANSWERED", "UNANSWERED"):
                flag = "\\" + key.removeprefix("UN").capitalize()
                has = flag in msg.flags
                if has == key.startswith("UN"):
                    return False
                continue
            if i >= len(values):
                raise self.error(f"SEARCH command error: BAD [b'Missing argument for {key}']")
            value = str(values[i])
            i += 1
            if key in ("FROM", "TO", "CC", "SUBJECT"):
                header = str(parsed.get(key, "") or "")
                if value.lower() not in header.lower():
                    return False
            elif key in ("TEXT", "BODY"):
                text = msg.raw.decode("utf-8", "replace")
                try:
                    part = parsed.get_body(preferencelist=("plain", "html"))
                    if part is not None:
                        text += part.get_content()
                except Exception:
                    pass
                if value.lower() not in text.lower():
                    return False
            elif key in ("SINCE", "BEFORE", "ON"):
                when = _imap_date(value)
                day = msg.internaldate.date()
                if key == "SINCE" and day < when:
                    return False
                if key == "BEFORE" and day >= when:
                    return False
                if key == "ON" and day != when:
                    return False
            elif key in ("LARGER", "SMALLER"):
                size = len(msg.raw)
                if key == "LARGER" and not size > int(value):
                    return False
                if key == "SMALLER" and not size < int(value):
                    return False
            else:
                raise self.error(f"SEARCH command error: BAD [b'Unknown search key {key}']")
        return True

    # -- FETCH ---------------------------------------------------------

    def _parse_uids(self, spec: str) -> list[int]:
        uids: list[int] = []
        for piece in spec.split(","):
            if ":" in piece:
                lo, hi = piece.split(":")
                top = max(self.selected.messages or [0])
                lo_i = int(lo)
                hi_i = top if hi == "*" else int(hi)
                uids.extend(u for u in sorted(self.selected.messages) if lo_i <= u <= hi_i)
            else:
                uids.append(int(piece))
        return uids

    def _fetch(self, uid_spec: str, items_text: str):
        items = _split_items(items_text)
        data: list = []
        ordered = sorted(self.selected.messages)
        if self.server.unsolicited_fetch and ordered:
            # e.g. another client changed flags: no UID item.
            data.append(f"{len(ordered)} (FLAGS (\\Seen))".encode())
        uid_last = self.server.uid_last
        for uid in self._parse_uids(uid_spec):
            msg = self.selected.messages.get(uid)
            if msg is None:
                continue
            seq = ordered.index(uid) + 1
            sets_seen = any(_item_sets_seen(i) for i in items)
            if sets_seen:
                msg.flags.add("\\Seen")
            text = f"{seq} (" if uid_last else f"{seq} (UID {uid}"
            for item in items:
                upper = item.upper()
                if upper == "UID":
                    continue
                if upper == "FLAGS":
                    text += f" FLAGS ({' '.join(sorted(msg.flags))})"
                elif upper == "RFC822.SIZE":
                    text += f" RFC822.SIZE {len(msg.raw)}"
                elif upper == "INTERNALDATE":
                    d = msg.internaldate
                    text += (f' INTERNALDATE "{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year} '
                             f'{d:%H:%M:%S} +0000"')
                elif upper == "ENVELOPE":
                    text += " ENVELOPE " + _envelope(msg.raw)
                elif upper in ("BODYSTRUCTURE", "BODY"):
                    parsed = email.message_from_bytes(msg.raw)
                    text += f" {upper} " + _bodystructure(parsed, self.server.string_disposition)
                else:
                    key, payload = self._section(item, msg.raw)
                    marker = "~" if key.startswith("BINARY") else ""  # RFC 3516 literal8
                    text += f" {key} {marker}{{{len(payload)}}}"
                    data.append((text.encode(), payload))
                    text = ""
            if uid_last:
                text += f" UID {uid}"
            data.append((text + ")").encode())
        return "OK", data

    def _section(self, item: str, raw: bytes) -> tuple[str, bytes]:
        upper = item.upper()
        m = re.match(r"^(BODY|BODY\.PEEK|BINARY|BINARY\.PEEK)\[(.*)\](?:<(\d+)(?:\.(\d+))?>)?$", upper)
        if upper in ("RFC822", "RFC822.TEXT", "RFC822.HEADER"):
            header, body = _split_raw(raw)
            payload = {"RFC822": raw, "RFC822.TEXT": body, "RFC822.HEADER": header}[upper]
            return upper, payload
        if not m:
            raise self.error(f"FETCH command error: BAD [b'Unknown item {item}']")
        kind = m.group(1).replace(".PEEK", "")
        section = m.group(2)
        payload = _section_bytes(raw, section)
        key = f"{kind}[{section}]"
        if m.group(3) is not None:
            start = int(m.group(3))
            length = int(m.group(4)) if m.group(4) else len(payload)
            payload = payload[start:start + length]
            key += f"<{start}>"
        return key, payload


def _item_sets_seen(item: str) -> bool:
    upper = item.upper()
    return (upper.startswith("BODY[") or upper.startswith("BINARY[")
            or upper in ("RFC822", "RFC822.TEXT"))


def _split_items(text: str) -> list[str]:
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    items: list[str] = []
    buf = ""
    depth = 0
    for c in text:
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        if c == " " and depth == 0:
            if buf:
                items.append(buf)
            buf = ""
        else:
            buf += c
    if buf:
        items.append(buf)
    return items


def _section_bytes(raw: bytes, section: str) -> bytes:
    header, body = _split_raw(raw)
    if section == "":
        return raw
    if section == "HEADER":
        return header
    if section == "TEXT":
        return body
    if section.startswith("HEADER.FIELDS"):
        names = re.findall(r"[A-Z0-9-]+", section[len("HEADER.FIELDS"):])
        parsed = email.message_from_bytes(raw)
        out = ""
        for name in names:
            for value in parsed.get_all(name, []) or []:
                out += f"{name.title()}: {value}\r\n"
        return (out + "\r\n").encode("utf-8", "surrogateescape")
    if re.fullmatch(r"\d+(?:\.\d+)*", section):
        part = email.message_from_bytes(raw)
        for idx in section.split("."):
            if not part.is_multipart():
                if idx == "1":
                    continue
                return b""
            children = part.get_payload()
            n = int(idx)
            if n < 1 or n > len(children):
                return b""
            part = children[n - 1]
        if part.is_multipart():
            return part.as_bytes()
        payload = part.get_payload(decode=False)
        if isinstance(payload, list):
            return b""
        return payload.encode("utf-8", "surrogateescape")
    return b""


def _addresses(value: Optional[str]) -> str:
    if not value:
        return "NIL"
    out = []
    for name, addr in email.utils.getaddresses([value]):
        mailbox, _, host = addr.partition("@")
        out.append(f"({_q(name or None)} NIL {_q(mailbox)} {_q(host or None)})")
    return "(" + "".join(out) + ")" if out else "NIL"


def _envelope(raw: bytes) -> str:
    msg = email.message_from_bytes(raw)
    sender = msg.get("From")
    fields = [
        _q(msg.get("Date")), _q(msg.get("Subject")),
        _addresses(sender), _addresses(msg.get("Sender") or sender),
        _addresses(msg.get("Reply-To") or sender), _addresses(msg.get("To")),
        _addresses(msg.get("Cc")), _addresses(msg.get("Bcc")),
        _q(msg.get("In-Reply-To")), _q(msg.get("Message-ID")),
    ]
    return "(" + " ".join(fields) + ")"


def _params(params: list[tuple[str, str]]) -> str:
    if not params:
        return "NIL"
    return "(" + " ".join(f"{_q(k.upper())} {_q(v)}" for k, v in params) + ")"


def _bodystructure(part, string_disposition: bool = False) -> str:
    if part.is_multipart():
        children = "".join(_bodystructure(c, string_disposition) for c in part.get_payload())
        boundary = part.get_boundary()
        params = _params([("boundary", boundary)] if boundary else [])
        return f"({children} {_q(part.get_content_subtype().upper())} {params} NIL NIL NIL)"
    maintype = part.get_content_maintype().upper()
    subtype = part.get_content_subtype().upper()
    ctype_params = [(k, v) for k, v in (part.get_params() or [])[1:]]
    payload = part.get_payload(decode=False)
    if isinstance(payload, list):  # message/rfc822
        inner = payload[0]
        body = inner.as_bytes()
        lines = body.count(b"\n")  # no backslash inside f-strings (Python 3.11)
        return (f"({_q(maintype)} {_q(subtype)} {_params(ctype_params)} NIL NIL "
                f"{_q((part.get('Content-Transfer-Encoding') or '7bit').upper())} {len(body)} "
                f"{_envelope(body)} {_bodystructure(inner, string_disposition)} "
                f"{lines} NIL NIL NIL NIL)")
    raw_payload = (payload or "").encode("utf-8", "surrogateescape")
    encoding = (part.get("Content-Transfer-Encoding") or "7bit").upper()
    disp_header = part.get("Content-Disposition")
    if disp_header is None:
        disposition = "NIL"
    elif string_disposition:
        disposition = _q(str(disp_header))
    else:
        kind = part.get_content_disposition() or "attachment"
        dparams = [(k, v) for k, v in (part.get_params(header="content-disposition") or [])[1:]]
        disposition = f"({_q(kind.upper())} {_params(dparams)})"
    fields = (f"{_q(maintype)} {_q(subtype)} {_params(ctype_params)} NIL NIL "
              f"{_q(encoding)} {len(raw_payload)}")
    if maintype == "TEXT":
        text_lines = raw_payload.count(b"\n")
        fields += f" {text_lines}"
    return f"({fields} NIL {disposition} NIL NIL)"


# -- fixtures ---------------------------------------------------------


class AdminHosts:
    """Controls the IMAP admin card seen by the plugin (fresh per read)."""

    def __init__(self) -> None:
        self.config: Optional[dict] = None
        self.reads = 0

    def set(self, text: str) -> None:
        """Set the card from legacy-style lines (the test shorthand): each
        line becomes a canonical entry, an unparseable line a raw
        ``{"host": line}`` entry so the card stays invalid."""
        from plugins.imap.settings import parse_server_line

        entries = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entries.append(parse_server_line(line).entry())
            except ValueError:
                entries.append({"host": line.strip(), "port": "", "security": ""})
        self.config = {"allowed_hosts": entries}

    def clear(self) -> None:
        self.config = None


@pytest.fixture()
def imap_admin_hosts(monkeypatch):
    from config import service_credentials

    admin = AdminHosts()
    real = service_credentials.read_service_credentials

    def _read(service):
        if service == "imap":
            admin.reads += 1
            return dict(admin.config) if admin.config is not None else None
        return real(service)

    monkeypatch.setattr(service_credentials, "read_service_credentials", _read)
    return admin


@pytest.fixture()
def fake_imap(monkeypatch, imap_admin_hosts):
    network = FakeImapNetwork()
    server = network.add_server(DEFAULT_HOST)
    server.add_account(DEFAULT_USERNAME, DEFAULT_PASSWORD)
    imap4, imap4_ssl = network.classes()
    monkeypatch.setattr(upstream, "IMAP4_CLASS", imap4)
    monkeypatch.setattr(upstream, "IMAP4_SSL_CLASS", imap4_ssl)
    imap_admin_hosts.set(DEFAULT_HOST)
    network.admin = imap_admin_hosts
    return network
