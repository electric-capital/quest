"""Connection resolution for per-user plugin connections.

A user may hold one or more connections (``user_service_credentials``
rows) per service. The user dict carries them two ways:

- ``user["service_credentials"][service]`` -- the default row, for every
  service (the compat shim unconverted plugins keep reading);
- ``user["service_connections"][service]`` -- every row, default first.

:func:`resolve_connection` is the ONLY place that maps a tool call's
optional ``connection`` argument (an account address) to a row; plugins,
``authed_get`` and the script bridge all call it. Only plugins whose
``UserConnectionSpec`` sets ``multi_connection=True`` accept a named
connection that is not their sole row's account.

Like the rest of ``config/``, this module is stdlib-only at import time;
the plugin registry is imported lazily inside the functions that need it.
"""

from __future__ import annotations


class ConnectionNotFound(LookupError):
    """A named connection matches none of the user's rows for the service.

    ``available`` lists the user's account addresses, default first, so
    the caller can return them to the model in a structured error.
    """

    def __init__(self, service: str, requested: str, available: list[str]):
        self.service = service
        self.requested = requested
        self.available = list(available)
        listed = ", ".join(self.available) if self.available else "none"
        super().__init__(
            f"No {service} connection named {requested!r} (available: {listed})"
        )


class ConnectionNotSupported(ValueError):
    """A connection was named for a single-connection service."""

    def __init__(self, service: str, requested: str):
        self.service = service
        self.requested = requested
        super().__init__(
            f"The {service} service does not take a connection argument "
            f"(got {requested!r})"
        )


def is_multi_connection(service: str) -> bool:
    """Whether ``service`` is a loaded plugin with ``multi_connection=True``."""
    from config.plugins import get_user_connection_spec

    spec = get_user_connection_spec(service)
    return bool(spec is not None and spec.multi_connection)


def _service_rows(user: dict, service: str) -> list[dict]:
    """The user's rows for ``service``, default first.

    Reads ``service_connections`` and falls back to the single default row
    in ``service_credentials`` (user dicts built before the connections
    list existed, or hand-built in tests). A list that somehow lacks the
    default-first order is reordered so index 0 is always the default.
    """
    rows = (user.get("service_connections") or {}).get(service)
    if rows is None:
        row = (user.get("service_credentials") or {}).get(service)
        return [row] if row else []
    rows = [r for r in rows if r]
    default_idx = next(
        (i for i, r in enumerate(rows) if r.get("is_default")), None,
    )
    if default_idx:  # not None and not already first
        rows.insert(0, rows.pop(default_idx))
    return rows


def _default_row(user: dict, service: str, rows: list[dict]) -> dict | None:
    """The default row: ``service_credentials`` first, else the list head."""
    row = (user.get("service_credentials") or {}).get(service)
    if row:
        return row
    return rows[0] if rows else None


def _norm(account: str | None) -> str:
    return (account or "").strip().lower()


def resolve_connection(
    user: dict, service: str, connection: str | None = None,
) -> dict | None:
    """Return the row dict to use, or None when the user has no connection.

    ``connection`` None (or blank) -> the default row
    (``service_credentials[service]``). A named connection -> the row whose
    account equals it case-insensitively after stripping; raises
    :class:`ConnectionNotFound` listing the available accounts when none
    does. For a service whose spec is not ``multi_connection``, a named
    connection is accepted only when it equals the sole row's account;
    anything else raises :class:`ConnectionNotSupported`.
    """
    rows = _service_rows(user, service)
    requested = (connection or "").strip()
    if not requested:
        return _default_row(user, service, rows)
    if not rows:
        # Named but nothing to resolve against: the caller's "not connected"
        # handling applies, same as the default path.
        return None
    wanted = _norm(requested)
    multi = is_multi_connection(service)
    if not multi:
        sole = rows[0]
        if len(rows) == 1 and _norm(sole.get("account")) == wanted:
            return sole
        raise ConnectionNotSupported(service, requested)
    for row in rows:
        if _norm(row.get("account")) == wanted:
            return row
    raise ConnectionNotFound(
        service, requested, [row.get("account") or "" for row in rows],
    )


def connection_label(row: dict) -> str:
    """``"work@example.com"`` or ``"work@example.com (default)"``."""
    account = row.get("account") or ""
    if not account:
        # A legacy row (pre-multi-connection) has no account address.
        return "default" if row.get("is_default") else ""
    return f"{account} (default)" if row.get("is_default") else account


def connection_summary(user: dict, service: str) -> list[dict]:
    """The user's connections for ``service`` as prompt / UI summaries.

    Each entry is ``{"account", "description", "is_default", "label"}``,
    default first. Row ids and credentials are never included.
    """
    summary = []
    for i, row in enumerate(_service_rows(user, service)):
        # A row dict without the flag (pre-multi-connection shape) is the
        # sole, default row when it heads the list.
        is_default = bool(row.get("is_default", i == 0))
        account = row.get("account") or ""
        summary.append({
            "account": account,
            "description": row.get("description"),
            "is_default": is_default,
            "label": connection_label({"account": account, "is_default": is_default}),
        })
    return summary
