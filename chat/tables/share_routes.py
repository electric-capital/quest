"""Quest Tables sharing routes: the owner manages a table's share roster
(spec section 7, phase A16; a copy of chat/docs/share_routes.py).

Two routes under ``/app/api`` (same auth dependency and ``tables`` gate as
chat/tables/routes.py, gate checked first):

- ``POST /tables/{id}/shares`` ``{"user_email"?: str, "everyone"?: bool,
  "permission": "read" | "write"}`` grants one recipient -- a Quest user
  by email (an exact match wins, else ASCII case-insensitive:
  ``user_store.find_user_by_email_ci``) or everyone on the install. Upsert:
  one row per recipient and at most one everyone row
  (``table_store.add_share``), so re-sharing with a new permission updates
  the existing row and a second everyone share is no conflict.
- ``DELETE /tables/{id}/shares/{share_id}`` removes one row.

Both return 200 with the OWNER's list row (``routes._ui_row``: ``shares``
with each recipient's ``user {id, name, email}``, null for the everyone
row). There is no GET: the roster is on the owner's table detail.

Checks, in order: 403 ``tables_disabled``; 404 ``table_not_found`` for a
missing or hidden table (one body); 403 ``forbidden`` for a visible table
the caller does not own (read and write recipients never manage shares);
then POST: 400 ``invalid_request`` unless exactly one target is given
(``user_email`` non-empty XOR ``everyone: true``), 400
``invalid_permission``, 404 ``user_not_found`` for an email no Quest user
has (also when the recipient is deleted mid-request), 409
``ambiguous_user`` when several case-variant accounts match and none
exactly, 400 ``cannot_share_with_owner`` for the owner's own email;
DELETE: 404 ``share_not_found`` for a malformed id or a share that is not
this table's.

The writes go through ``service.add_share`` / ``service.remove_share``:
under the table lock (a queued read or write re-resolves with the new
roster), no ``updated_at`` bump (not a content change, so an editor's
token survives), and ``table_changed {updated_at: null}`` +
``table_list_changed`` to the old and new audiences together (a removed
recipient's viewer closes). Re-sharing with the same permission changes
nothing and publishes nothing.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.tables import service
from chat.tables.constants import TABLE_SHARE_PERMISSIONS
from chat.tables.routes import (
    _get_table_for_ui,
    _http_error,
    _raise_table_error,
    _require_owner,
    _require_tables_enabled,
    _ui_row,
)
from db import user_store

router = APIRouter(
    prefix="/app/api",
    tags=["tables"],
)

_SHARE_ID_RE = re.compile(r"[0-9]{1,18}")


class AddTableShareRequest(BaseModel):
    # Typed loosely so every malformed body gets this API's own 400 codes
    # instead of a FastAPI 422.
    user_email: Optional[Any] = None
    everyone: Optional[Any] = None
    permission: Optional[Any] = None


def _parse_target(body: AddTableShareRequest) -> Optional[str]:
    """The recipient email, or None for everyone; 400 ``invalid_request``
    unless exactly one target is given."""
    email = body.user_email
    everyone = body.everyone
    if email is not None and not isinstance(email, str):
        raise _http_error(400, "invalid_request", "user_email must be a string.")
    if everyone is not None and not isinstance(everyone, bool):
        raise _http_error(400, "invalid_request", "everyone must be a boolean.")
    email = (email or "").strip()
    if bool(email) == (everyone is True):
        raise _http_error(
            400, "invalid_request",
            'Give exactly one recipient: "user_email" or "everyone": true.',
        )
    return email or None


def _parse_permission(permission: Any) -> str:
    if not isinstance(permission, str) or permission not in TABLE_SHARE_PERMISSIONS:
        raise _http_error(
            400, "invalid_permission",
            f"permission must be one of: {', '.join(TABLE_SHARE_PERMISSIONS)}.",
        )
    return permission


def _user_not_found() -> Exception:
    return _http_error(404, "user_not_found", "No Quest user has that email.")


@router.post("/tables/{table_id}/shares")
async def add_table_share(
    table_id: str,
    body: Optional[AddTableShareRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Share a table with one Quest user (by email) or with everyone, or
    change that recipient's permission (owner only). Returns the owner's
    row. Errors in the module docstring."""
    _require_tables_enabled(user)
    table, _access = await _get_table_for_ui(user, table_id)
    _require_owner(user, table, "manage its sharing")
    body = body or AddTableShareRequest()
    email = _parse_target(body)
    permission = _parse_permission(body.permission)

    recipient_id: Optional[int] = None
    if email is not None:
        try:
            recipient = await user_store.find_user_by_email_ci(email)
        except user_store.AmbiguousUserEmailError:
            raise _http_error(
                409, "ambiguous_user",
                "Several Quest accounts match that email; type it exactly.",
            )
        if recipient is None:
            raise _user_not_found()
        if recipient["id"] == table["owner_id"]:
            raise _http_error(
                400, "cannot_share_with_owner",
                "You own this table; share it with someone else.",
            )
        recipient_id = recipient["id"]

    try:
        current, _changed = await service.add_share(
            service.caller_for_ui(user), table["id"],
            user_id=recipient_id, permission=permission,
        )
    except service.TableError as exc:
        if exc.code == "user_not_found":
            raise _user_not_found()
        _raise_table_error(exc, table_id)
    return await _ui_row(user, current)


@router.delete("/tables/{table_id}/shares/{share_id}")
async def remove_table_share(
    table_id: str,
    share_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Remove one share row (owner only); the recipient loses access at
    once. Returns the owner's row. 404 ``share_not_found`` for a malformed
    id or a share of another table."""
    _require_tables_enabled(user)
    table, _access = await _get_table_for_ui(user, table_id)
    _require_owner(user, table, "manage its sharing")
    if not _SHARE_ID_RE.fullmatch(share_id):
        raise _http_error(404, "share_not_found", "Share not found.")
    try:
        current = await service.remove_share(
            service.caller_for_ui(user), table["id"], int(share_id),
        )
    except service.TableError as exc:
        _raise_table_error(exc, table_id)
    return await _ui_row(user, current)
