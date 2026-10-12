"""IMAP plugin settings: the admin host allow-list and per-row helpers.

Stdlib-only (the admin card is read lazily through
``config.service_credentials``), so the manifest hooks stay importable
without the app stack.

Admin card (service ``imap``): one ``allowed_hosts`` textarea, one entry
per line, ``host`` or ``host:port``. Hostnames are lower-cased; IPv4
literals are accepted as written, IPv6 literals only in brackets
(``[::1]`` / ``[::1]:993``). A line without a port allows both default
ports (993 for implicit TLS, 143 for STARTTLS). No scheme, path or
userinfo. Every connect re-reads the card (:func:`load_allowed_hosts`),
so removing a host takes effect on the next tool call.

Per-user connection rows (``form`` kind) store their form values under
``oauth_blob["values"]``: ``host``, ``port`` (blank = the security
default), ``security`` (``ssl`` / ``starttls``), ``username``,
``password``.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

SERVICE_ID = "imap"
DEFAULT_PORTS = (993, 143)  # a line without a port allows both
SECURITY_CHOICES = ("ssl", "starttls")
DEFAULT_PORT_FOR_SECURITY = {"ssl": 993, "starttls": 143}
ROW_WARNING_HOST_REMOVED = "Server no longer allowed"

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IPV4_LIKE_RE = re.compile(r"^[0-9.]+$")


@dataclass(frozen=True)
class AllowedHost:
    """One allow-list entry; ``port`` None allows every DEFAULT_PORTS port."""

    host: str
    port: Optional[int]

    def line(self) -> str:
        """The canonical admin-card line for this entry."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port is None else f"{host}:{self.port}"


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


def _parse_line(line: str) -> AllowedHost:
    if any(ch.isspace() for ch in line):
        raise ValueError("whitespace inside the entry")
    if "://" in line:
        raise ValueError("no scheme (write just the host name)")
    if "/" in line or "\\" in line:
        raise ValueError("no path (write just the host name)")
    if "@" in line:
        raise ValueError("no user name (write just the host name)")
    if line.startswith("["):
        end = line.find("]")
        if end < 0:
            raise ValueError("unclosed '[' in IPv6 address")
        inner, rest = line[1:end], line[end + 1:]
        try:
            host = str(ipaddress.IPv6Address(inner))
        except ValueError:
            raise ValueError(f"invalid IPv6 address {inner!r}") from None
        if "%" in host:
            raise ValueError("IPv6 zone ids are not supported")
        if not rest:
            return AllowedHost(host, None)
        if not rest.startswith(":"):
            raise ValueError("unexpected text after ']'")
        return AllowedHost(host, _parse_port(rest[1:]))
    if line.count(":") > 1:
        raise ValueError("IPv6 addresses must be written in brackets, e.g. [::1]:993")
    if ":" in line:
        host_text, port_text = line.split(":", 1)
        return AllowedHost(_normalize_hostname(host_text), _parse_port(port_text))
    return AllowedHost(_normalize_hostname(line), None)


def parse_allowed_hosts(text: str) -> list[AllowedHost]:
    """Parse the admin ``allowed_hosts`` text.

    Blank lines and surrounding whitespace are ignored, duplicates
    collapsed (first occurrence keeps its position). Raises
    ``ValueError("Line N: ...")`` on the first bad line.
    """
    if text is None:
        return []
    if not isinstance(text, str):
        raise ValueError("allowed hosts must be text")
    entries: list[AllowedHost] = []
    seen: set[AllowedHost] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            entry = _parse_line(line)
        except ValueError as exc:
            raise ValueError(f"Line {number}: {exc}") from None
        if entry not in seen:
            seen.add(entry)
            entries.append(entry)
    return entries


def validate_imap_admin(values: dict) -> dict:
    """``credential_validate`` hook: normalise ``allowed_hosts`` to one
    canonical entry per line, or raise ValueError (shown on the card)."""
    entries = parse_allowed_hosts(values.get("allowed_hosts") or "")
    if not entries:
        raise ValueError("Allowed IMAP servers: list at least one server.")
    return {**values, "allowed_hosts": "\n".join(e.line() for e in entries)}


def imap_is_configured(config: dict) -> bool:
    """At least one valid host line (False on any parse error)."""
    try:
        return bool(parse_allowed_hosts((config or {}).get("allowed_hosts") or ""))
    except (ValueError, AttributeError):
        return False


def load_allowed_hosts() -> list[AllowedHost]:
    """FRESH read of the admin card; [] when unset or unparseable."""
    from config import service_credentials

    config = service_credentials.read_service_credentials(SERVICE_ID)
    if not isinstance(config, dict):
        return []
    try:
        return parse_allowed_hosts(config.get("allowed_hosts") or "")
    except ValueError as exc:
        logger.warning("IMAP admin allow-list is invalid: %s", exc)
        return []


def normalize_host(host: str) -> str:
    """Canonical comparison form of a row's host value."""
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


def allowed_ports(host: str, allowed: Optional[Iterable[AllowedHost]] = None) -> set[int]:
    """Ports the allow-list permits for ``host``; empty = host not allowed."""
    if allowed is None:
        allowed = load_allowed_hosts()
    wanted = normalize_host(host)
    ports: set[int] = set()
    for entry in allowed:
        if entry.host == wanted:
            if entry.port is None:
                ports.update(DEFAULT_PORTS)
            else:
                ports.add(entry.port)
    return ports


def is_allowed(host: str, port: int, allowed: Optional[Iterable[AllowedHost]] = None) -> bool:
    return port in allowed_ports(host, allowed)


def imap_field_options() -> dict[str, list[str]]:
    """``field_options`` hook: the host select's choices, admin order."""
    hosts: list[str] = []
    for entry in load_allowed_hosts():
        if entry.host not in hosts:
            hosts.append(entry.host)
    return {"host": hosts}


def row_values(row: dict) -> dict:
    """The stored form values of a connection row ({} when absent)."""
    blob = (row or {}).get("oauth_blob")
    if not isinstance(blob, dict):
        return {}
    values = blob.get("values")
    return values if isinstance(values, dict) else {}


def effective_port(values: dict) -> int:
    """The port a connection uses: the explicit ``port`` value, else the
    default for its ``security`` (993 ssl / 143 starttls). Raises
    ValueError on a malformed port or unknown security."""
    raw = values.get("port")
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if text:
        try:
            return _parse_port(text)
        except ValueError:
            raise ValueError("Port must be a number between 1 and 65535.") from None
    security = values.get("security")
    if security not in DEFAULT_PORT_FOR_SECURITY:
        raise ValueError("Security must be one of: ssl, starttls.")
    return DEFAULT_PORT_FOR_SECURITY[security]


def imap_connected(row: dict) -> bool:
    """``connected`` hook: the row holds a host, username and password."""
    values = row_values(row)
    return all(
        isinstance(values.get(k), str) and values.get(k).strip()
        for k in ("host", "username", "password")
    )


def imap_row_warning(row: dict) -> Optional[str]:
    """``row_warning`` hook: flag rows whose server left the allow-list."""
    values = row_values(row)
    host = values.get("host")
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        port = effective_port(values)
    except ValueError:
        return ROW_WARNING_HOST_REMOVED
    if not is_allowed(host, port):
        return ROW_WARNING_HOST_REMOVED
    return None


def account_for(username: str, host: str) -> str:
    """The connection's account: the username when it is an address, else
    ``username@host``; lower-cased."""
    user = (username or "").strip()
    if "@" in user:
        return user.lower()
    return f"{user}@{normalize_host(host)}".lower()
