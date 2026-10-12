"""IMAP transport: one verified-TLS session per tool call.

``run_imap(settings, work)`` re-checks the admin server list against the
CURRENT admin card, then in a worker thread (stdlib ``imaplib`` is
blocking) connects, logs in, runs ``work(ImapSession)`` and logs out.
The whole call is bounded by ``CALL_TIMEOUT``; on expiry the socket is
shut down from the event loop's side so the worker thread unblocks, and
``ImapError("imap_timeout")`` is raised. Sessions run on a dedicated,
bounded thread pool (``MAX_WORKERS``) so stuck servers cannot exhaust the
event loop's default executor. The connect phase is cancellable too: the
transport subclasses (:class:`QuestIMAP4` / :class:`QuestIMAP4_SSL`)
register every socket they open (TCP connect, implicit-TLS and STARTTLS
handshakes) with the call before blocking on it.

Security properties (issue #84 spec 10.2 / 10.6):

- TLS always (TLS 1.2 minimum), verified with
  ``ssl.create_default_context()`` (see ``make_tls_context``): implicit
  TLS (``IMAP4_SSL``) for an ``ssl`` server line, ``STARTTLS`` for a
  ``starttls`` one (refused when the server does not advertise it). There
  is no plaintext mode and no verification opt-out.
- The connection's ``server`` value must name a configuration on the
  CURRENT admin list, checked when the settings are built and again
  right before every connect (``check_host_allowed``).
- 20 s socket timeout per operation.
- Bounded reads: a server literal above ``MAX_LITERAL_BYTES`` and a
  response line above ``MAX_LINE_BYTES`` are refused
  (``imap_protocol_error``); imaplib's 1 MB line limit is raised so a
  ``* SEARCH`` reply of a very large folder still parses.
- ``imaplib.Debug`` is ignored (the subclasses pin ``debug`` to 0), so
  a global debug level can never print the LOGIN line.
- Read-only: folders are opened with ``EXAMINE`` and ``uid_fetch``
  refuses every FETCH item that could set ``\\Seen``.
- The password never appears in logs, reprs or error payloads; command
  arguments are quoted and CR/LF/NUL are refused.

``ImapError.code`` values: ``imap_auth_failed``, ``imap_host_not_allowed``,
``imap_connect_failed``, ``imap_folder_not_found``,
``imap_uidvalidity_changed``, ``imap_protocol_error``, ``imap_timeout``,
``imap_not_configured``.

Tests swap the transport classes (``IMAP4_CLASS`` / ``IMAP4_SSL_CLASS``)
for the fake in ``plugins/imap/tests/fake_imaplib.py``.
"""

from __future__ import annotations

import asyncio
import contextvars
import imaplib
import logging
import re
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from config.plugin_types import VerifyResult

from plugins.imap import protocol
from plugins.imap.settings import (
    DEFAULT_PORT_FOR_SECURITY,
    ServerConfig,
    account_for,
    load_allowed_servers,
    resolve_server,
    row_values,
)

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT = 20.0  # per socket operation
CALL_TIMEOUT = 60.0  # whole tool call
# verify runs under the generic route's 20 s limit; stay below it so the
# plugin's own timeout (which also shuts the socket) fires first.
VERIFY_TIMEOUT = 18.0

# Read bounds (see the module docstring). MAX_LITERAL_BYTES must stay above
# the largest message imap_save_attachment downloads (tools.py,
# MAX_SAVE_MESSAGE_BYTES, about 71 MB).
MAX_LITERAL_BYTES = 80 * 1024 * 1024
MAX_LINE_BYTES = 32 * 1024 * 1024
MAX_WORKERS = 8

T = TypeVar("T")

_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="imap")

# The _CallState of the session being opened on this worker thread: the
# transport subclasses register their sockets with it so a timeout can shut
# them down even before the constructor returns.
_connect_local = threading.local()


class ImapError(Exception):
    """A structured IMAP failure; ``to_dict()`` is the tool result shape."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}

    def __repr__(self) -> str:
        return f"ImapError({self.code!r}, {self.message!r})"


@dataclass(frozen=True)
class ImapSettings:
    """A resolved connection: host / port / security come from the admin
    server configuration the row's ``server`` value names."""

    host: str
    port: int
    security: str
    username: str
    password: str = field(repr=False)
    account: str = ""

    @property
    def server(self) -> ServerConfig:
        return ServerConfig(self.host, self.port, self.security)


def _server_not_allowed(value) -> ImapError:
    if isinstance(value, str) and value.strip():
        shown = value.strip()[:200]
        return ImapError(
            "imap_host_not_allowed",
            f"The IMAP server {shown} is no longer allowed by the administrator.",
            server=shown,
        )
    return ImapError(
        "imap_host_not_allowed",
        "This connection's IMAP server is not on the administrator's server list; "
        "the user must re-add the mailbox in Settings > Data Connections.",
    )


def _settings(values: dict, account: Optional[str]) -> ImapSettings:
    """Resolve ``values["server"]`` against the CURRENT admin list."""
    raw_server = values.get("server")
    config = resolve_server(raw_server)
    if config is None:
        raise _server_not_allowed(raw_server)
    username = (values.get("username") or "").strip()
    password = values.get("password") or ""
    if not username or not password:
        raise ImapError("imap_not_configured", "The connection has no username or password.")
    return ImapSettings(
        host=config.host, port=config.port, security=config.security, username=username,
        password=password, account=account or account_for(username, config.host),
    )


def settings_from_values(values: dict) -> ImapSettings:
    """Settings from raw form values (verify)."""
    return _settings(values or {}, None)


def settings_from_row(row: dict) -> ImapSettings:
    """Settings from a stored connection row (a legacy row without
    ``server`` raises ``imap_host_not_allowed``)."""
    return _settings(row_values(row), (row or {}).get("account") or None)


# -- helpers ------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value if v is not None)
    return str(value)


def _exc_text(exc: BaseException) -> str:
    """The human part of an exception: the certificate verification
    message, an OSError's ``strerror``, else the single argument."""
    verify = getattr(exc, "verify_message", None)
    if isinstance(exc, ssl.SSLCertVerificationError) and verify:
        return f"certificate verify failed: {verify}"
    if isinstance(exc, ssl.SSLError) and getattr(exc, "reason", None) and not exc.strerror:
        return str(exc.reason)
    if isinstance(exc, OSError) and exc.strerror:
        return _text(exc.strerror)
    if exc.args:
        return _text(exc.args[0] if len(exc.args) == 1 else list(exc.args))
    return type(exc).__name__


def _scrub(message: str, settings: ImapSettings) -> str:
    """Remove the password from a server/OS message, defensively."""
    pw = settings.password
    if pw and len(pw) >= 3 and pw in message:
        message = message.replace(pw, "***")
    return message[:500]


def _is_safe_credential(value: str) -> bool:
    return not any(c in value for c in ("\r", "\n", "\0"))


# -- session ------------------------------------------------------------

_ITEM_CHARS_RE = re.compile(r"[A-Za-z0-9.\[\]<>() \-]*")
_UNSAFE_ITEM_RES = (
    re.compile(r"\bBODY\["),  # BODY[...] without .PEEK
    re.compile(r"\bBINARY\["),  # BINARY[...] without .PEEK
    re.compile(r"\bRFC822(?!\.SIZE\b|\.HEADER\b)"),  # RFC822 / RFC822.TEXT
)
_SEARCH_ATOM_RE = re.compile(r"[A-Z][A-Z0-9-]*")


def check_fetch_items(items: str) -> str:
    """Validate a FETCH item list; ValueError for anything that could set
    ``\\Seen`` (``BODY[`` / ``BINARY[`` without ``.PEEK``, bare ``RFC822``,
    ``RFC822.TEXT``) or contains unexpected characters."""
    if not isinstance(items, str) or not items.strip():
        raise ValueError("fetch items must be a non-empty string")
    if not _ITEM_CHARS_RE.fullmatch(items):
        raise ValueError("fetch items contain unexpected characters")
    upper = items.upper()
    for pattern in _UNSAFE_ITEM_RES:
        if pattern.search(upper):
            raise ValueError(
                f"fetch item list {items!r} could mark messages as read; use BODY.PEEK[...]"
            )
    text = items.strip()
    if not text.startswith("("):
        text = f"({text})"
    return text


def _parsed(fn: Callable[..., T], *args: Any) -> T:
    """Run a protocol parser; any failure on hostile server data becomes
    ``imap_protocol_error`` (never a ValueError, which the tools would
    report as an invalid argument)."""
    try:
        return fn(*args)
    except (ValueError, RecursionError, IndexError, TypeError) as exc:
        raise ImapError(
            "imap_protocol_error", f"Could not parse the server response: {str(exc)[:200]}",
        ) from None


class ImapSession:
    """A logged-in IMAP connection, used synchronously inside ``work``.

    Folder arguments are decoded names (as ``list_folders`` reports in
    ``name``); a name the server stores non-canonically in modified UTF-7
    can also be passed as its listed ``raw_name`` (tried verbatim when the
    canonical encoding is not found, see ``protocol.raw_name_candidate``).
    """

    def __init__(self, conn: Any, capabilities: tuple[str, ...]):
        self._conn = conn
        self.capabilities = capabilities
        self.selected: Optional[str] = None

    def __repr__(self) -> str:
        return "ImapSession()"

    def _check(self, typ: str, data: Any, what: str) -> None:
        if typ != "OK":
            raise ImapError("imap_protocol_error", f"{what} failed: {_text(data)[:300]}")

    def list_folders(self, limit: int = 200) -> list[dict]:
        typ, data = self._conn.list('""', "*")
        self._check(typ, data, "LIST")
        folders: list[dict] = []
        for line in data or ():
            if line is None:
                continue
            parsed = _parsed(protocol.parse_list_line, line)
            if parsed is None:
                continue
            folders.append(parsed)
            if len(folders) >= limit:
                break
        return folders

    @staticmethod
    def _folder_args(folder: str, raw_name: Optional[str] = None) -> list[str]:
        """Quoted mailbox arguments to try, in order."""
        if raw_name is not None:
            return [protocol.quote_raw_folder(raw_name)]
        attempts = [protocol.quote_folder(folder)]
        raw = protocol.raw_name_candidate(folder)
        if raw is not None:
            attempts.append(protocol.quote_raw_folder(raw))
        return attempts

    def _status_values(self, mailbox_arg: str, items: str) -> tuple[str, Any, dict]:
        typ, data = self._conn.status(mailbox_arg, items)
        values = _parsed(protocol.parse_status_response, data) if typ == "OK" else {}
        return typ, data, values

    def status(self, folder: str, *, raw_name: Optional[str] = None) -> dict:
        typ, data, values = "NO", None, {}
        for arg in self._folder_args(folder, raw_name):
            typ, data, values = self._status_values(arg, "(MESSAGES UNSEEN UIDVALIDITY)")
            if typ == "OK":
                break
        if typ != "OK":
            raise ImapError(
                "imap_folder_not_found", f"Folder {folder!r} not found: {_text(data)[:300]}",
                folder=folder,
            )
        return {
            "messages": values.get("MESSAGES", 0),
            "unseen": values.get("UNSEEN", 0),
            "uidvalidity": values.get("UIDVALIDITY"),
        }

    def examine(self, folder: str, *, raw_name: Optional[str] = None) -> int:
        """Open ``folder`` read-only; returns its UIDVALIDITY (from the
        ``[UIDVALIDITY]`` response code, else a ``STATUS`` fallback)."""
        typ, data, used = "NO", None, None
        for arg in self._folder_args(folder, raw_name):
            typ, data = self._conn.select(arg, readonly=True)
            if typ == "OK":
                used = arg
                break
        if typ != "OK":
            self.selected = None
            raise ImapError(
                "imap_folder_not_found", f"Folder {folder!r} not found: {_text(data)[:300]}",
                folder=folder,
            )
        self.selected = folder
        _code, values = self._conn.response("UIDVALIDITY")
        for value in values or ():
            text = _text(value).strip()
            if protocol.is_number_atom(text):
                return int(text)
        # Some servers omit the response code; STATUS reports it too.
        typ, _data, status = self._status_values(used, "(UIDVALIDITY)")
        if typ == "OK" and isinstance(status.get("UIDVALIDITY"), int):
            return status["UIDVALIDITY"]
        raise ImapError("imap_protocol_error", "The server did not report UIDVALIDITY.")

    def check_uidvalidity(self, folder: str, expected: Optional[int]) -> int:
        current = self.examine(folder)
        if expected is not None and int(expected) != current:
            raise ImapError(
                "imap_uidvalidity_changed",
                f"UIDVALIDITY of {folder!r} changed (UIDs are no longer valid); "
                "re-run imap_search_messages.",
                folder=folder, expected=int(expected), current=current,
            )
        return current

    def _search_once(self, args: list[str], literal: Optional[bytes]) -> set[int]:
        if literal is not None:
            self._conn.literal = literal
        try:
            typ, data = self._conn.uid("SEARCH", *args)
        finally:
            self._conn.literal = None
        self._check(typ, data, "SEARCH")
        uids: set[int] = set()
        for line in data or ():
            for token in _text(line).split():
                if protocol.is_number_atom(token):
                    uids.add(int(token))
        return uids

    def uid_search(self, terms: list) -> list[int]:
        """``UID SEARCH`` over ``terms``: atoms (``"UNSEEN"``) or ``(KEY,
        value)`` pairs. ASCII values are quoted; a non-ASCII value is sent
        as a UTF-8 literal with ``CHARSET UTF-8`` (imaplib allows one
        literal per command, so each further non-ASCII value runs its own
        search and the UID sets are intersected). Ascending UIDs."""
        ascii_args: list[str] = []
        literal_terms: list[tuple[str, bytes]] = []
        prefix_ops = False
        for term in terms or ():
            if isinstance(term, str):
                atom = term.upper()
                if not _SEARCH_ATOM_RE.fullmatch(atom):
                    raise ValueError(f"invalid search atom {term!r}")
                prefix_ops = prefix_ops or atom in ("NOT", "OR")
                ascii_args.append(atom)
                continue
            if not (isinstance(term, (tuple, list)) and len(term) == 2):
                raise ValueError(f"invalid search term {term!r}")
            key, value = term
            key = str(key).upper()
            if not _SEARCH_ATOM_RE.fullmatch(key):
                raise ValueError(f"invalid search key {key!r}")
            if isinstance(value, bool):
                raise ValueError(f"invalid search value for {key}")
            if isinstance(value, int):
                if value < 0:
                    raise ValueError(f"invalid search value for {key}")
                ascii_args.extend([key, str(value)])
                continue
            protocol.check_safe_text(value, f"search value for {key}")
            if value.isascii():
                ascii_args.extend([key, protocol.quote_imap_string(value)])
            else:
                literal_terms.append((key, value.encode("utf-8")))
        if not literal_terms:
            return sorted(self._search_once(ascii_args or ["ALL"], None))
        if prefix_ops:
            # Literal terms are moved to the end of the command (and split
            # across commands), which would re-bind NOT / OR operands.
            raise ValueError("NOT / OR cannot be combined with non-ASCII search values")
        result: Optional[set[int]] = None
        for key, data in literal_terms:
            uids = self._search_once(["CHARSET", "UTF-8", *ascii_args, key], data)
            result = uids if result is None else (result & uids)
            if not result:
                break
        return sorted(result or ())

    def uid_fetch(self, uids: list[int], items: str) -> dict[int, dict[str, Any]]:
        """``UID FETCH`` parsed per UID. Refuses (ValueError) any item that
        could set ``\\Seen``; keys normalise ``BODY.PEEK[X]`` to ``BODY[X]``."""
        text = check_fetch_items(items)
        clean: list[int] = []
        for uid in uids or ():
            if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
                raise ValueError(f"invalid UID {uid!r}")
            if uid not in clean:
                clean.append(uid)
        if not clean:
            return {}
        typ, data = self._conn.uid("FETCH", ",".join(str(u) for u in clean), text)
        self._check(typ, data, "FETCH")
        parsed = _parsed(protocol.parse_fetch_response, data)
        return {uid: parsed[uid] for uid in clean if uid in parsed}


# -- connection lifecycle -----------------------------------------------


class _CallState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: Any = None
        self.socks: list = []
        self.cancelled = False

    def set_conn(self, conn: Any) -> bool:
        with self.lock:
            self.conn = conn
            return not self.cancelled

    def add_socket(self, sock: Any) -> None:
        """Register a socket a connect step is about to block on; closes it
        and raises when the call was already cancelled."""
        with self.lock:
            cancelled = self.cancelled
            if not cancelled:
                self.socks.append(sock)
        if cancelled:
            _close_socket(sock)
            raise OSError("the IMAP call was cancelled")

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            conn = self.conn
            socks = list(self.socks)
        for sock in socks:
            _close_socket(sock)
        if conn is not None:
            _hard_close(conn)


def _close_socket(sock: Any) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        sock.close()
    except (OSError, ValueError):
        pass


def _hard_close(conn: Any) -> None:
    sock = getattr(conn, "sock", None)
    if sock is not None:
        _close_socket(sock)


def register_connect_socket(sock: Any) -> None:
    """Called by the transport classes for every socket they block on
    while connecting (also by the test fake)."""
    state = getattr(_connect_local, "state", None)
    if state is not None:
        state.add_socket(sock)


def _open_socket(host: str, port: int, timeout: Optional[float]) -> socket.socket:
    """``socket.create_connection`` with each socket registered before
    its (blocking) connect, so a call timeout can interrupt it."""
    error: Optional[OSError] = None
    for family, socktype, proto, _canon, address in socket.getaddrinfo(
        host or None, port, 0, socket.SOCK_STREAM,
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            register_connect_socket(sock)
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect(address)
            return sock
        except OSError as exc:
            error = exc
            _close_socket(sock)
            state = getattr(_connect_local, "state", None)
            if state is not None and state.cancelled:
                break
    raise error or OSError(f"no addresses found for {host}")


class _QuestTransport:
    """Mixin over imaplib's classes: cancellable connect, bounded reads,
    debug output pinned off."""

    # imaplib.IMAP4.__init__ assigns ``self.debug = imaplib.Debug``; a
    # global debug level >= 4 would print the LOGIN command line.
    debug = property(lambda self: 0, lambda self, value: None)
    _quest_broken = False

    def _create_socket(self, timeout):
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        return _open_socket(self.host, self.port, timeout)

    def read(self, size):
        if size > MAX_LITERAL_BYTES:
            self._quest_broken = True
            raise imaplib.IMAP4.error(
                f"the server sent a {size:,} byte literal, above the "
                f"{MAX_LITERAL_BYTES // (1024 * 1024)} MB limit"
            )
        return super().read(size)

    def readline(self):
        line = self.file.readline(MAX_LINE_BYTES + 1)
        if len(line) > MAX_LINE_BYTES:
            self._quest_broken = True
            raise imaplib.IMAP4.error(
                f"the server sent a response line above {MAX_LINE_BYTES // (1024 * 1024)} MB"
            )
        return line

    def starttls(self, ssl_context=None):
        """imaplib's STARTTLS with the handshake made cancellable (the
        wrapped socket is registered before the handshake blocks)."""
        name = "STARTTLS"
        if self._tls_established:
            raise self.abort("TLS session already established")
        if name not in self.capabilities:
            raise self.abort("TLS not supported by server")
        if ssl_context is None:
            ssl_context = make_tls_context()
        typ, dat = self._simple_command(name)
        if typ != "OK":
            raise self.error("Couldn't establish TLS session")
        sock = ssl_context.wrap_socket(
            self.sock, server_hostname=self.host, do_handshake_on_connect=False,
        )
        self.sock = sock
        register_connect_socket(sock)
        sock.do_handshake()
        self.file = sock.makefile("rb")
        self._tls_established = True
        self._get_capabilities()
        return self._untagged_response(typ, dat, name)


class QuestIMAP4(_QuestTransport, imaplib.IMAP4):
    """Plain-TCP transport (always upgraded with STARTTLS by ``_connect``)."""


class QuestIMAP4_SSL(_QuestTransport, imaplib.IMAP4_SSL):
    """Implicit-TLS transport; the handshake runs after registration."""

    def _create_socket(self, timeout):
        raw = _QuestTransport._create_socket(self, timeout)
        try:
            sock = self.ssl_context.wrap_socket(
                raw, server_hostname=self.host, do_handshake_on_connect=False,
            )
        except BaseException:
            _close_socket(raw)
            raise
        try:
            register_connect_socket(sock)
            sock.do_handshake()
        except BaseException:
            _close_socket(sock)
            raise
        return sock


# Transport classes; tests replace these with fakes.
IMAP4_CLASS = QuestIMAP4
IMAP4_SSL_CLASS = QuestIMAP4_SSL


def _connect_failed(settings: ImapSettings, exc: BaseException, what: str = "Could not connect to") -> ImapError:
    return ImapError(
        "imap_connect_failed",
        f"{what} {settings.host}:{settings.port}: {_scrub(_exc_text(exc), settings)}",
    )


def make_tls_context() -> ssl.SSLContext:
    """The TLS context for every IMAP connection the plugin opens.

    ``ssl.create_default_context()`` supplies the system CA store,
    certificate verification and hostname checking (never relaxed here);
    the protocol floor is pinned to TLS 1.2 explicitly rather than left to
    the OpenSSL build's default.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _connect(settings: ImapSettings, state: Optional[_CallState] = None) -> Any:
    _connect_local.state = state
    try:
        return _connect_inner(settings)
    finally:
        _connect_local.state = None


def _connect_inner(settings: ImapSettings) -> Any:
    context = make_tls_context()
    try:
        if settings.security == "ssl":
            conn = IMAP4_SSL_CLASS(
                settings.host, settings.port, ssl_context=context, timeout=SOCKET_TIMEOUT,
            )
            _quiet(conn)
            return conn
        conn = IMAP4_CLASS(settings.host, settings.port, timeout=SOCKET_TIMEOUT)
        _quiet(conn)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise _connect_failed(settings, exc) from None
    try:
        caps = tuple(str(c).upper() for c in getattr(conn, "capabilities", ()) or ())
        if "STARTTLS" not in caps:
            raise ImapError(
                "imap_connect_failed",
                f"{settings.host}:{settings.port} does not offer STARTTLS; "
                "plaintext IMAP is not supported.",
            )
        conn.starttls(ssl_context=context)
    except ImapError:
        _hard_close(conn)
        raise
    except (OSError, imaplib.IMAP4.error) as exc:
        _hard_close(conn)
        raise _connect_failed(settings, exc, "STARTTLS failed with") from None
    return conn


def _quiet(conn: Any) -> None:
    """Debug output off (a no-op on the Quest transport classes, which pin
    it; covers replacement classes)."""
    try:
        conn.debug = 0
    except AttributeError:
        pass


def _login(conn: Any, settings: ImapSettings) -> None:
    username, password = settings.username, settings.password
    if not (_is_safe_credential(username) and _is_safe_credential(password)):
        raise ImapError(
            "imap_auth_failed", "Username or password contains line breaks or NUL characters.",
        )
    caps = tuple(str(c).upper() for c in getattr(conn, "capabilities", ()) or ())
    needs_sasl = not (username.isascii() and password.isascii()) or "LOGINDISABLED" in caps
    try:
        if needs_sasl:
            if "AUTH=PLAIN" not in caps:
                raise ImapError(
                    "imap_auth_failed",
                    "The username or password contains non-ASCII characters and the "
                    "server does not offer AUTHENTICATE PLAIN."
                    if "LOGINDISABLED" not in caps else
                    "The server disables LOGIN and does not offer AUTHENTICATE PLAIN.",
                )
            payload = b"\0" + username.encode("utf-8") + b"\0" + password.encode("utf-8")
            typ, data = conn.authenticate("PLAIN", lambda _challenge: payload)
        else:
            # imaplib quotes only the password; quote the user name too so
            # names with ( ) % * \ { ] " or spaces are sent intact.
            typ, data = conn.login(protocol.quote_imap_string(username), password)
    except ImapError:
        raise
    except imaplib.IMAP4.abort as exc:
        raise ImapError(
            "imap_connect_failed", f"Connection lost during login: {_scrub(_exc_text(exc), settings)}",
        ) from None
    except imaplib.IMAP4.error as exc:
        raise ImapError(
            "imap_auth_failed", f"Login failed: {_scrub(_exc_text(exc), settings)}",
        ) from None
    finally:
        # imaplib keeps a ring buffer of sent commands (incl. LOGIN).
        log = getattr(conn, "_cmd_log", None)
        if hasattr(log, "clear"):
            log.clear()
    if typ != "OK":
        raise ImapError("imap_auth_failed", f"Login failed: {_scrub(_text(data), settings)}")


def _dedupe(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)


def _capabilities(conn: Any) -> tuple[str, ...]:
    """The post-login CAPABILITY list. imaplib returns every untagged
    CAPABILITY it collected (a ``[CAPABILITY ...]`` login response code and
    the reply itself), so only the newest entry counts; de-duplicated."""
    try:
        typ, data = conn.capability()
        if typ == "OK":
            entries = [d for d in (data or ()) if d]
            if entries:
                caps = _dedupe(_text(entries[-1]).upper().split())
                if caps:
                    return caps
    except (OSError, imaplib.IMAP4.error):
        pass
    return _dedupe(str(c).upper() for c in getattr(conn, "capabilities", ()) or ())


def _logout(conn: Any) -> None:
    try:
        conn.logout()
    except Exception:
        _hard_close(conn)


def _run_session(settings: ImapSettings, work: Callable[[ImapSession], T], state: _CallState) -> T:
    if state.cancelled:  # queued behind busy workers until the call expired
        raise ImapError("imap_timeout", "The IMAP call timed out.")
    try:
        conn = _connect(settings, state)
    except ImapError:
        if state.cancelled:
            raise ImapError("imap_timeout", "The IMAP call timed out.") from None
        raise
    if not state.set_conn(conn):
        _hard_close(conn)
        raise ImapError("imap_timeout", "The IMAP call timed out.")
    graceful = False  # LOGOUT only while the connection is in sync
    try:
        _login(conn, settings)
        session = ImapSession(conn, _capabilities(conn))
        result = work(session)
        graceful = True
        return result
    except ImapError as exc:
        graceful = (
            exc.code not in ("imap_connect_failed", "imap_timeout")
            and not getattr(conn, "_quest_broken", False)
        )
        raise
    except (socket.timeout, TimeoutError):
        raise ImapError(
            "imap_timeout", f"The server did not answer within {SOCKET_TIMEOUT:.0f} seconds.",
        ) from None
    except imaplib.IMAP4.abort as exc:
        if state.cancelled:
            raise ImapError("imap_timeout", "The IMAP call timed out.") from None
        raise ImapError(
            "imap_connect_failed", f"Connection lost: {_scrub(_exc_text(exc), settings)}",
        ) from None
    except imaplib.IMAP4.error as exc:
        graceful = not getattr(conn, "_quest_broken", False)
        raise ImapError(
            "imap_protocol_error", f"IMAP error: {_scrub(_exc_text(exc), settings)}",
        ) from None
    except OSError as exc:
        if state.cancelled:
            raise ImapError("imap_timeout", "The IMAP call timed out.") from None
        raise ImapError(
            "imap_connect_failed", f"Connection lost: {_scrub(_exc_text(exc), settings)}",
        ) from None
    except BaseException:
        # e.g. a bug in ``work``: still in sync as far as we know.
        graceful = True
        raise
    finally:
        if graceful and not state.cancelled:
            _logout(conn)
        else:
            # After a timeout / abort LOGOUT would only wait out another
            # socket timeout; just drop the connection.
            _hard_close(conn)


def check_host_allowed(settings: ImapSettings) -> None:
    """Re-check (host, port, security) against the CURRENT admin card."""
    if settings.server not in set(load_allowed_servers()):
        raise _server_not_allowed(settings.server.line())


async def run_imap(
    settings: ImapSettings,
    work: Callable[[ImapSession], T],
    *,
    timeout: float = CALL_TIMEOUT,
) -> T:
    """Run ``work`` in one fresh IMAP session (see module docstring)."""
    check_host_allowed(settings)
    state = _CallState()
    try:
        # Cancelling the to_thread future cannot stop the worker thread;
        # state.cancel() shuts its socket so it unblocks promptly, and its
        # late result is discarded.
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        return await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, context.run, _run_session, settings, work, state),
            timeout,
        )
    except asyncio.TimeoutError:
        state.cancel()
        logger.info(
            "[imap] call to %s:%s timed out after %ss (account=%s)",
            settings.host, settings.port, timeout, settings.account,
        )
        raise ImapError(
            "imap_timeout", f"The IMAP call did not finish within {timeout:.0f} seconds.",
        ) from None
    except asyncio.CancelledError:
        state.cancel()
        raise
    except ImapError as exc:
        logger.info(
            "[imap] %s on %s:%s (account=%s)", exc.code, settings.host, settings.port,
            settings.account,
        )
        raise


# -- verify (form connection hook) ---------------------------------------


def _verify_message(exc: ImapError) -> str:
    if exc.code == "imap_auth_failed":
        return exc.message if exc.message.startswith("Login failed") else f"Login failed: {exc.message}"
    if exc.code == "imap_connect_failed":
        return exc.message
    if exc.code == "imap_timeout":
        return "The server did not answer in time."
    if exc.code == "imap_host_not_allowed":
        return exc.message
    return exc.message


async def verify_imap_connection(values: dict) -> VerifyResult:
    """``verify`` hook: server resolved against the CURRENT admin list,
    live LOGIN + CAPABILITY, account."""
    values = values or {}
    config = resolve_server(values.get("server"))
    if config is None:
        return VerifyResult(error="Server not allowed")
    username = (values.get("username") or "").strip()
    try:
        from db.user_service_credential_store import normalize_account

        account = normalize_account(account_for(username, config.host))
    except ValueError as exc:
        return VerifyResult(
            error=f"This username cannot be used as an account name ({exc}).",
        )
    try:
        settings = settings_from_values(values)
    except ImapError as exc:
        return VerifyResult(error=exc.message)
    try:
        caps = await run_imap(settings, lambda s: list(s.capabilities), timeout=VERIFY_TIMEOUT)
    except ImapError as exc:
        return VerifyResult(error=_verify_message(exc))
    return VerifyResult(
        account=account,
        extras={
            "capabilities": caps,
            "verified_at": datetime.now(timezone.utc).isoformat(),
        },
    )


__all__ = [
    "CALL_TIMEOUT",
    "DEFAULT_PORT_FOR_SECURITY",
    "ImapError",
    "ImapSession",
    "ImapSettings",
    "QuestIMAP4",
    "QuestIMAP4_SSL",
    "SOCKET_TIMEOUT",
    "check_fetch_items",
    "check_host_allowed",
    "run_imap",
    "settings_from_row",
    "settings_from_values",
    "verify_imap_connection",
]
