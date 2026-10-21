"""Service connect requests: the model offers the user an inline Connect card.

When a user asks for something that needs a service they have not
connected yet (an email search while Google Services is unconnected), the
top-level model calls the ``request_service_connection`` tool instead of
telling the user to visit Settings. The tool opens a ``connect_request``
card in the conversation and blocks on a ``service_connection`` wait
handle until the user connects the service (verified server-side by the
resolve endpoint in ``chat/connect_request_routes.py``) or declines.

The rows come from ``build_connector_rows`` in ``chat/routes/user.py`` --
the same rows the Settings > Data Connections picker renders -- so the
prompt paragraph, the tool and the card can never disagree with Settings
about which services an admin has configured.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Tool name (rides on ``tool_call``), durable chat-history message type and
# wait-handle kind. The three names are deliberately distinct: the tool is
# what the model calls, the message is what the FE renders, the kind is
# what the resume bucket matches on.
TOOL_NAME = "request_service_connection"
MESSAGE_TYPE = "connect_request"
HANDLE_KIND = "service_connection"

# Card copy from the model is user-facing; clip rather than reject.
MAX_REASON_LEN = 200

# Connection kinds whose add flow the inline card can run itself (an OAuth
# popup). The other kinds (api_key / form) need the Settings add flow, so
# the card sends the user there and re-checks when they come back.
POPUP_KINDS = frozenset({"oauth"})


def _row_available(row: dict) -> bool:
    return row.get("available", True) is not False


def row_is_connected(row: dict) -> bool:
    """A row counts as connected only when nothing is left to do: a
    connected oauth row that still needs re-authorization is not."""
    return bool(row.get("connected")) and not bool(row.get("needs_reauth"))


def _row_state(row: dict) -> str:
    """``connected`` / ``needs_reauth`` / ``connectable`` for an available row."""
    if row_is_connected(row):
        return "connected"
    if row.get("connected") and row.get("needs_reauth"):
        return "needs_reauth"
    return "connectable"


def connector_rows(user: dict) -> list[dict]:
    """Every available connector row for *user* (Settings' row list)."""
    from chat.routes.user import build_connector_rows

    return [row for row in build_connector_rows(user) if _row_available(row)]


def find_row(user: dict, service: str) -> dict | None:
    """The available row whose ``service`` id is *service*, or ``None``."""
    for row in connector_rows(user):
        if row.get("service") == service:
            return row
    return None


def connectable_rows(user: dict) -> list[dict]:
    """Available rows the model may open a connect card for: not connected,
    or connected but needing re-authorization."""
    return [
        row for row in connector_rows(user)
        if _row_state(row) != "connected"
    ]


def find_connect_target(user: dict, service: str) -> tuple[dict | None, dict | None]:
    """Resolve the tool's ``service`` argument to a connectable row.

    Returns ``(row, None)`` on success or ``(None, error)`` where *error*
    is the structured tool result to return to the model.
    """
    connectable = connectable_rows(user)
    ids = [row["service"] for row in connectable]
    if not service:
        return None, {
            "error": "missing_service",
            "message": "Pass the id of the service to connect.",
            "connectable_services": ids,
        }
    row = find_row(user, service)
    if row is None:
        return None, {
            "error": "unknown_service",
            "message": (
                f"{service!r} is not a service this user can connect: the "
                "admin has not configured it, or the id is wrong. Only the "
                "services listed under connectable_services can be "
                "connected here; for anything else, tell the user it is "
                "not available on this Quest instance."
            ),
            "connectable_services": ids,
        }
    if _row_state(row) == "connected":
        return None, {
            "error": "already_connected",
            "message": (
                f"{row.get('label', service)} is already connected. Load its "
                "system skill and use it; if its calls fail with an "
                "authorization error, this tool can be used after the "
                "connection reports that it needs re-authorization."
            ),
            "service": service,
        }
    return row, None


def card_fields(row: dict, reason: str) -> dict:
    """The service facts the card and the wait-handle payload carry.

    Never includes a credential or key hint: only identity, kind, and (for
    the oauth kind) the popup URL the Settings picker would open.
    """
    kind = row.get("kind") or "oauth"
    fields = {
        "service": row["service"],
        "label": row.get("label") or row["service"],
        "description": row.get("description") or "",
        "kind": kind,
        "needs_reauth": bool(row.get("connected") and row.get("needs_reauth")),
        "reason": (reason or "").strip()[:MAX_REASON_LEN],
    }
    if kind in POPUP_KINDS and row.get("connect_url"):
        fields["connect_url"] = row["connect_url"]
    return fields


def _service_line(row: dict) -> str:
    label = row.get("label") or row["service"]
    line = f"{label} (`{row['service']}`"
    if row.get("description"):
        line += f": {row['description']}"
    line += ")"
    if _row_state(row) == "needs_reauth":
        line += " -- connected but needs re-authorization"
    return line


def build_connectable_services_paragraph(user: dict) -> str:
    """The "**Connectable services**" prompt paragraph, or ``""``.

    Lists the admin-configured services this user has not connected (or
    must re-authorize) and tells the model to offer the inline Connect
    card through ``request_service_connection`` instead of pointing at
    Settings. Empty when nothing is connectable, so a fully-connected
    user's prompt is unchanged.
    """
    try:
        rows = connectable_rows(user)
    except Exception:
        logger.exception("Connectable services paragraph failed; omitting it")
        return ""
    if not rows:
        return ""
    lines = "; ".join(_service_line(row) for row in rows)
    return (
        "**Connectable services** (configured by the admin, not yet "
        f"connected by this user): {lines}. When the user's request needs "
        "one of these, do not say it is unavailable and do not send them "
        "to Settings: call `tool_call(tool_name=\"request_service_connection\", "
        "arguments={\"service\": \"<id>\", \"reason\": \"...\"})`. It shows an "
        "inline Connect card and blocks until the user connects "
        "(`status: \"connected\"` -- the service's tools and system skill "
        "are available from then on, so load the skill and carry on with "
        "the original request) or declines (`status: \"declined\"` -- "
        "continue without the service and do not ask again in this "
        "conversation unless the user brings it up)."
    )
