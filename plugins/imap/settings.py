"""IMAP plugin settings: the admin server list and per-row helpers.

Stdlib-only (the admin card is read lazily through
``config.service_credentials``), so the manifest hooks stay importable
without the app stack.

Admin card (service ``imap``): one ``allowed_hosts`` textarea ("IMAP
servers"), one server configuration per line::

    host[:port][ ssl|starttls]

- ``host``: a DNS name (lower-cased, trailing dot dropped, punycode for
  non-ASCII), an IPv4 literal, or an IPv6 literal in brackets
  (``[::1]``). No scheme, path or userinfo.
- defaults: no port and no mode = ``993 ssl``; ``:143`` without a mode =
  ``starttls``; any other port without a mode = ``ssl``; ``ssl`` without
  a port = 993; ``starttls`` without a port = 143.
- the mode is ``ssl`` (implicit TLS) or ``starttls``, case-insensitive,
  separated by whitespace; there is no plaintext mode.
- a host may appear on several lines with different port/mode; two lines
  naming the same configuration (after the defaults are applied) are an
  error.

Saving the card normalises every line to its canonical form
``host:port mode`` (``imap.example.com:993 ssl``, ``[::1]:143 starttls``).
Every connect re-reads the card (:func:`load_allowed_servers`), so
removing a line takes effect on the next tool call.

Per-user connection rows (``form`` kind) store their form values under
``oauth_blob["values"]``: ``server`` (one of the canonical lines, chosen
from a select), ``username``, ``password``. A row's ``server`` is
resolved against the CURRENT admin list on every connect
(:func:`resolve_server`); a row whose server is gone, or a legacy row
from before the server select (``host`` / ``port`` / ``security`` and no
``server``), fails closed with ``imap_host_not_allowed``.

Known limitation (generic layer, ``auth/connections.py``, deliberately
unchanged in this phase): the re-entry form sends secrets only when
typed, so a PUT that changes ``server`` with a blank password keeps the
stored password and will offer it to the new (admin-allowed) server.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

SERVICE_ID = "imap"
SECURITY_CHOICES = ("ssl", "starttls")
DEFAULT_PORT_FOR_SECURITY = {"ssl": 993, "starttls": 143}
STARTTLS_DEFAULT_PORT = 143  # a line with this port and no mode is starttls
ROW_WARNING_HOST_REMOVED = "Server no longer allowed"

# DNS labels; "_" is accepted because internal hosts use it (it is not
# valid in public DNS names but resolvers handle it).
_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_IPV4_LIKE_RE = re.compile(r"^[0-9.]+$")


@dataclass(frozen=True)
class ServerConfig:
    """One admin-allowed server configuration (all defaults applied)."""

    host: str
    port: int
    security: str

    def line(self) -> str:
        """The canonical form: ``host:port mode`` (IPv6 bracketed)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port} {self.security}"


def _parse_port(text: str) -> int:
    if not text.isdigit() or len(text) > 5:
        raise ValueError(f"invalid port {text!r}")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is out of range (1-65535)")
    return port


def _normalize_hostname(text: str) -> str:
    """Validate a DNS hostname or IPv4 literal; returns it lower-cased."""
    host = text.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise ValueError("empty host")
    if not host.isascii():
        raise ValueError("non-ASCII host name (use the punycode xn-- form)")
    if _IPV4_LIKE_RE.match(host):
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            raise ValueError(f"invalid IPv4 address {text!r}") from None
    if len(host) > 253:
        raise ValueError("host name is too long")
    for label in host.split("."):
        if not _LABEL_RE.match(label):
            raise ValueError(f"invalid host name {text!r}")
    return host


def _parse_host_port(text: str) -> tuple[str, Optional[int]]:
    """``host`` / ``host:port`` / ``[v6]`` / ``[v6]:port``."""
    if "://" in text:
        raise ValueError("no scheme (write just the host name)")
    if "/" in text or "\\" in text:
        raise ValueError("no path (write just the host name)")
    if "@" in text:
        raise ValueError("no user name (write just the host name)")
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("unclosed '[' in IPv6 address")
        inner, rest = text[1:end], text[end + 1:]
        try:
            host = str(ipaddress.IPv6Address(inner))
        except ValueError:
            raise ValueError(f"invalid IPv6 address {inner!r}") from None
        if "%" in host:
            raise ValueError("IPv6 zone ids are not supported")
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise ValueError("unexpected text after ']'")
        return host, _parse_port(rest[1:])
    if text.count(":") > 1:
        raise ValueError("IPv6 addresses must be written in brackets, e.g. [::1]:993")
    if ":" in text:
        host_text, port_text = text.split(":", 1)
        return _normalize_hostname(host_text), _parse_port(port_text)
    return _normalize_hostname(text), None


def parse_server_line(line: str) -> ServerConfig:
    """Parse one ``host[:port][ ssl|starttls]`` line (defaults applied).

    Raises ValueError with a short reason on a bad line.
    """
    if not isinstance(line, str):
        raise ValueError("server must be text")
    words = line.split()
    if not words:
        raise ValueError("empty line")
    if len(words) > 2:
        raise ValueError("expected host[:port] followed by an optional ssl or starttls")
    host, port = _parse_host_port(words[0])
    security: Optional[str] = None
    if len(words) == 2:
        security = words[1].lower()
        if security not in SECURITY_CHOICES:
            raise ValueError(
                f"unknown security mode {words[1]!r} (use ssl or starttls; "
                "plaintext IMAP is not supported)"
            )
    if security is None:
        security = "starttls" if port == STARTTLS_DEFAULT_PORT else "ssl"
    if port is None:
        port = DEFAULT_PORT_FOR_SECURITY[security]
    return ServerConfig(host, port, security)


def parse_allowed_servers(text: Any) -> list[ServerConfig]:
    """Parse the admin ``allowed_hosts`` text, in admin order.

    Blank lines and surrounding whitespace are ignored. Raises
    ``ValueError("Line N: ...")`` on the first bad line, including a line
    that names the same configuration as an earlier one.
    """
    if text is None:
        return []
    if not isinstance(text, str):
        raise ValueError("the server list must be text")
    entries: list[ServerConfig] = []
    first_line: dict[ServerConfig, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = parse_server_line(raw)
        except ValueError as exc:
            raise ValueError(f"Line {number}: {exc}") from None
        if entry in first_line:
            raise ValueError(
                f"Line {number}: duplicate of line {first_line[entry]} ({entry.line()})"
            )
        first_line[entry] = number
        entries.append(entry)
    return entries


def validate_imap_admin(values: dict) -> dict:
    """``credential_validate`` hook: normalise ``allowed_hosts`` to one
    canonical ``host:port mode`` line per server, or raise ValueError
    (shown on the card)."""
    entries = parse_allowed_servers(values.get("allowed_hosts") or "")
    if not entries:
        raise ValueError("IMAP servers: list at least one server.")
    return {**values, "allowed_hosts": "\n".join(e.line() for e in entries)}


def imap_is_configured(config: dict) -> bool:
    """At least one valid server line (False on any parse error)."""
    try:
        return bool(parse_allowed_servers((config or {}).get("allowed_hosts") or ""))
    except (ValueError, AttributeError):
        return False


def load_allowed_servers() -> list[ServerConfig]:
    """FRESH read of the admin card; [] when unset or unparseable."""
    from config import service_credentials

    config = service_credentials.read_service_credentials(SERVICE_ID)
    if not isinstance(config, dict):
        return []
    try:
        return parse_allowed_servers(config.get("allowed_hosts") or "")
    except ValueError as exc:
        logger.warning("IMAP admin server list is invalid: %s", exc)
        return []


def resolve_server(
    value: Any, allowed: Optional[Iterable[ServerConfig]] = None,
) -> Optional[ServerConfig]:
    """The admin configuration a stored ``server`` value names, or None
    when it is malformed or not (any longer) on the admin list. Reads the
    CURRENT admin card unless ``allowed`` is given."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        wanted = parse_server_line(value)
    except ValueError:
        return None
    if allowed is None:
        allowed = load_allowed_servers()
    return wanted if wanted in set(allowed) else None


def imap_field_options() -> dict[str, list[str]]:
    """``field_options`` hook: the server select's choices, the canonical
    admin lines in admin order."""
    return {"server": [entry.line() for entry in load_allowed_servers()]}


def normalize_host(host: str) -> str:
    """Canonical comparison form of a host value."""
    text = (host or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text.endswith("."):
        text = text[:-1]
    if ":" in text:
        try:
            return str(ipaddress.IPv6Address(text))
        except ValueError:
            return text
    return text


def row_values(row: dict) -> dict:
    """The stored form values of a connection row ({} when absent)."""
    blob = (row or {}).get("oauth_blob")
    if not isinstance(blob, dict):
        return {}
    values = blob.get("values")
    return values if isinstance(values, dict) else {}


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_legacy_row(row: dict) -> bool:
    """A row from before the server select: form values without a
    ``server`` (it held ``host`` / ``port`` / ``security``). Never
    migrated; it fails closed as a server no longer allowed."""
    values = row_values(row)
    return bool(values) and not _filled(values.get("server"))


def imap_connected(row: dict) -> bool:
    """``connected`` hook: the row holds a server, username and password."""
    values = row_values(row)
    return all(_filled(values.get(k)) for k in ("server", "username", "password"))


def imap_row_warning(row: dict) -> Optional[str]:
    """``row_warning`` hook: flag rows whose server is not on the current
    admin list (removed, malformed, or a legacy row without ``server``)."""
    values = row_values(row)
    if not values:
        return None
    if resolve_server(values.get("server")) is None:
        return ROW_WARNING_HOST_REMOVED
    return None


def account_for(username: str, host: str) -> str:
    """The connection's account: the username when it is an address, else
    ``username@host``; lower-cased."""
    user = (username or "").strip()
    if "@" in user:
        return user.lower()
    return f"{user}@{normalize_host(host)}".lower()
