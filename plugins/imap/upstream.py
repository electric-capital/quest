"""IMAP transport: one verified-TLS session per tool call.

``run_imap(settings, work)`` re-checks the admin host allow-list against
the CURRENT admin card, then in a worker thread (stdlib ``imaplib`` is
blocking) connects, logs in, runs ``work(ImapSession)`` and logs out.
The whole call is bounded by ``CALL_TIMEOUT``; on expiry the socket is
shut down from the event loop's side so the worker thread unblocks, and
``ImapError("imap_timeout")`` is raised.

Security properties (issue #84 spec 10.2 / 10.6):

- TLS always, verified with ``ssl.create_default_context()``: implicit
  TLS (``IMAP4_SSL``) for security ``ssl``, ``STARTTLS`` for security
  ``starttls`` (refused when the server does not advertise it). There is
  no plaintext mode and no verification opt-out.
- 20 s socket timeout per operation.
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
import imaplib
import logging
import re
import socket
import ssl
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

from config.plugin_types import VerifyResult

from plugins.imap import protocol
from plugins.imap.settings import (
    DEFAULT_PORT_FOR_SECURITY,
    SECURITY_CHOICES,
    account_for,
    allowed_ports,
    effective_port,
    load_allowed_hosts,
    normalize_host,
    row_values,
)

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT = 20.0  # per socket operation
CALL_TIMEOUT = 60.0  # whole tool call
# verify runs under the generic route's 20 s limit; stay below it so the
# plugin's own timeout (which also shuts the socket) fires first.
VERIFY_TIMEOUT = 18.0

# Transport classes; tests replace these with fakes.
IMAP4_CLASS = imaplib.IMAP4
IMAP4_SSL_CLASS = imaplib.IMAP4_SSL

T = TypeVar("T")


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
    host: str
    port: int
    security: str
    username: str
    password: str = field(repr=False)
    account: str = ""


def _settings(values: dict, account: Optional[str]) -> ImapSettings:
    host = normalize_host(values.get("host") or "")
    if not host:
        raise ImapError("imap_not_configured", "The connection has no IMAP server.")
    security = values.get("security")
    if security not in SECURITY_CHOICES:
        raise ImapError("imap_not_configured", "Security must be one of: ssl, starttls.")
    try:
        port = effective_port(values)
    except ValueError as exc:
        raise ImapError("imap_not_configured", str(exc)) from None
    username = (values.get("username") or "").strip()
    password = values.get("password") or ""
    if not username or not password:
        raise ImapError("imap_not_configured", "The connection has no username or password.")
    return ImapSettings(
        host=host, port=port, security=security, username=username,
        password=password, account=account or account_for(username, host),
    )


def settings_from_values(values: dict) -> ImapSettings:
    """Settings from raw form values (verify)."""
    return _settings(values or {}, None)


def settings_from_row(row: dict) -> ImapSettings:
    """Settings from a stored connection row."""
    return _settings(row_values(row), (row or {}).get("account") or None)


# -- helpers ------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value if v is not None)
    return str(value)


def _exc_text(exc: BaseException) -> str:
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


class ImapSession:
    """A logged-in IMAP connection, used synchronously inside ``work``."""

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
            parsed = protocol.parse_list_line(line)
            if parsed is None:
                continue
            folders.append(parsed)
            if len(folders) >= limit:
                break
        return folders

    def status(self, folder: str) -> dict:
        typ, data = self._conn.status(
            protocol.quote_folder(folder), "(MESSAGES UNSEEN UIDVALIDITY)"
        )
        if typ != "OK":
            raise ImapError(
                "imap_folder_not_found", f"Folder {folder!r} not found: {_text(data)[:300]}",
                folder=folder,
            )
        values = protocol.parse_status_response(data)
        return {
            "messages": values.get("MESSAGES", 0),
            "unseen": values.get("UNSEEN", 0),
            "uidvalidity": values.get("UIDVALIDITY"),
        }

    def examine(self, folder: str) -> int:
        """Open ``folder`` read-only; returns its UIDVALIDITY."""
        typ, data = self._conn.select(protocol.quote_folder(folder), readonly=True)
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
            if text.isdigit():
                return int(text)
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
                if token.isdigit():
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
        for term in terms or ():
            if isinstance(term, str):
                atom = term.upper()
                if not _SEARCH_ATOM_RE.fullmatch(atom):
                    raise ValueError(f"invalid search atom {term!r}")
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
        parsed = protocol.parse_fetch_response(data)
        return {uid: parsed[uid] for uid in clean if uid in parsed}


# -- connection lifecycle -----------------------------------------------


class _CallState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conn: Any = None
        self.cancelled = False

    def set_conn(self, conn: Any) -> bool:
        with self.lock:
            self.conn = conn
            return not self.cancelled

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            conn = self.conn
        if conn is not None:
            _hard_close(conn)


def _hard_close(conn: Any) -> None:
    sock = getattr(conn, "sock", None)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _connect(settings: ImapSettings) -> Any:
    context = ssl.create_default_context()
    try:
        if settings.security == "ssl":
            return IMAP4_SSL_CLASS(
                settings.host, settings.port, ssl_context=context, timeout=SOCKET_TIMEOUT,
            )
        conn = IMAP4_CLASS(settings.host, settings.port, timeout=SOCKET_TIMEOUT)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ImapError(
            "imap_connect_failed",
            f"Could not connect to {settings.host}:{settings.port}: {_scrub(_exc_text(exc), settings)}",
        ) from None
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
        raise ImapError(
            "imap_connect_failed",
            f"STARTTLS with {settings.host}:{settings.port} failed: "
            f"{_scrub(_exc_text(exc), settings)}",
        ) from None
    return conn


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
            typ, data = conn.login(username, password)
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


def _capabilities(conn: Any) -> tuple[str, ...]:
    try:
        typ, data = conn.capability()
        if typ == "OK":
            caps = tuple(_text(data).upper().split())
            if caps:
                return caps
    except (OSError, imaplib.IMAP4.error):
        pass
    return tuple(str(c).upper() for c in getattr(conn, "capabilities", ()) or ())


def _logout(conn: Any) -> None:
    try:
        conn.logout()
    except Exception:
        _hard_close(conn)


def _run_session(settings: ImapSettings, work: Callable[[ImapSession], T], state: _CallState) -> T:
    conn = _connect(settings)
    if not state.set_conn(conn):
        _hard_close(conn)
        raise ImapError("imap_timeout", "The IMAP call timed out.")
    try:
        _login(conn, settings)
        session = ImapSession(conn, _capabilities(conn))
        return work(session)
    except ImapError:
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
        raise ImapError(
            "imap_protocol_error", f"IMAP error: {_scrub(_exc_text(exc), settings)}",
        ) from None
    except OSError as exc:
        if state.cancelled:
            raise ImapError("imap_timeout", "The IMAP call timed out.") from None
        raise ImapError(
            "imap_connect_failed", f"Connection lost: {_scrub(_exc_text(exc), settings)}",
        ) from None
    finally:
        if not state.cancelled:
            _logout(conn)


def check_host_allowed(settings: ImapSettings) -> None:
    """Re-check (host, port) against the CURRENT admin card."""
    ports = allowed_ports(settings.host, load_allowed_hosts())
    if not ports:
        raise ImapError(
            "imap_host_not_allowed",
            f"The IMAP server {settings.host} is no longer allowed by the administrator.",
            host=settings.host,
        )
    if settings.port not in ports:
        raise ImapError(
            "imap_host_not_allowed",
            f"Port {settings.port} is not allowed for {settings.host}.",
            host=settings.host, port=settings.port,
        )


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
        return await asyncio.wait_for(
            asyncio.to_thread(_run_session, settings, work, state), timeout,
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
        return f"Could not connect: {exc.message}"
    if exc.code == "imap_timeout":
        return "The server did not answer in time."
    if exc.code == "imap_host_not_allowed":
        return exc.message
    return exc.message


async def verify_imap_connection(values: dict) -> VerifyResult:
    """``verify`` hook: allow-list check, live LOGIN + CAPABILITY, account."""
    values = values or {}
    host = normalize_host(values.get("host") or "")
    try:
        port = effective_port(values)
    except ValueError as exc:
        return VerifyResult(error=str(exc))
    ports = allowed_ports(host, load_allowed_hosts())
    if not ports:
        return VerifyResult(error="Server not allowed")
    if port not in ports:
        return VerifyResult(error=f"Port {port} is not allowed for {host}")
    username = (values.get("username") or "").strip()
    try:
        from db.user_service_credential_store import normalize_account

        account = normalize_account(account_for(username, host))
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
    "SOCKET_TIMEOUT",
    "check_fetch_items",
    "check_host_allowed",
    "run_imap",
    "settings_from_row",
    "settings_from_values",
    "verify_imap_connection",
]
