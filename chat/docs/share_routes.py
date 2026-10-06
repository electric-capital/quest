"""Quest Docs sharing routes: the owner manages a doc's share roster.

Two routes under ``/app/api`` (same auth dependency and ``docs`` gate as
chat/docs/routes.py, gate checked first):

- ``POST /docs/{id}/shares`` ``{"user_email"?: str, "everyone"?: bool,
  "permission": "read" | "write"}`` grants one recipient -- a Quest user
  by email (an exact match wins, else ASCII case-insensitive:
  ``user_store.find_user_by_email_ci``) or everyone on the install. Upsert:
  one row per recipient and at most one everyone row (``doc_store.add_share``),
  so re-sharing with a new permission updates the existing row.
- ``DELETE /docs/{id}/shares/{share_id}`` removes one row.

Both return 200 with the OWNER's row (``routes._ui_row``: ``shares`` with
each recipient's ``user {id, name, email}``, null for the everyone row).

Checks, in order: 403 ``docs_disabled``; 404 ``doc_not_found`` for a
missing or hidden doc (``routes._get_doc_for_ui``: the identical body
either way); 403 ``forbidden`` for a visible doc the caller does not own
(recipients -- read or write -- never manage shares); then POST: 400
``invalid_request`` unless exactly one target is given (``user_email``
non-empty XOR ``everyone: true``), 400 ``invalid_permission``, 404
``user_not_found`` for an email no Quest user has (also when the recipient
is deleted mid-request), 409 ``ambiguous_user`` when several case-variant
accounts match and none exactly, 400 ``cannot_share_with_owner`` for the
owner's own email; DELETE: 404 ``share_not_found`` for a malformed id or a
share that is not this doc's. A doc of a public project is frozen (404)
while the viewer's or the owner's ``public_projects`` gate is closed.

What a share grants is decided by the one access rule
(chat/docs/access.py), unchanged: in the UI a read share views and a write
share also edits / restores (no rename, delete, sharing or asset deletion);
from the recipient's conversations a shared USER doc is readable (read
share) or writable through a ``write_doc`` approval card (write share on a
private doc), while a shared PROJECT doc stays hidden there (project docs
are reachable only from their project's conversations, and projects are
not shared). The first share on a private doc also turns the owner's own
conversation writes into approval cards.

Share changes do not bump ``updated_at`` (they are not content changes),
so an editor's optimistic-concurrency token survives them. They run under
the per-doc write lock (``service._write_lock``): a model write that queued
behind a share change re-resolves its verdict with the new roster, and the
doc delete reads the roster it notifies under the same lock. Each change
publishes ``doc_changed {doc_id, updated_at: null}`` (open viewers
re-fetch: access changed) and ``doc_list_changed`` to the owner and the
affected recipient, or to every connected user for the everyone row
(``events.publish_share_changed``);
re-sharing with the same permission changes nothing and publishes nothing.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import events as doc_events
from chat.docs import routes as doc_routes
from chat.docs import service as doc_service
from chat.docs.constants import DOC_SHARE_PERMISSIONS
from db import doc_store, user_store

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)

_SHARE_ID_RE = re.compile(r"[0-9]{1,18}")


class AddDocShareRequest(BaseModel):
    # Typed loosely so every malformed body gets this API's own 400 codes
    # instead of a FastAPI 422.
    user_email: Optional[Any] = None
    everyone: Optional[Any] = None
    permission: Optional[Any] = None


def _parse_target(body: AddDocShareRequest) -> Optional[str]:
    """The recipient email, or None for everyone; 400 ``invalid_request``
    unless exactly one target is given."""
    email = body.user_email
    everyone = body.everyone
    if email is not None and not isinstance(email, str):
        raise doc_routes._http_error(400, "invalid_request", "user_email must be a string.")
    if everyone is not None and not isinstance(everyone, bool):
        raise doc_routes._http_error(400, "invalid_request", "everyone must be a boolean.")
    email = (email or "").strip()
    if bool(email) == (everyone is True):
        raise doc_routes._http_error(
            400, "invalid_request",
            'Give exactly one recipient: "user_email" or "everyone": true.',
        )
    return email or None


def _parse_permission(permission: Any) -> str:
    if not isinstance(permission, str) or permission not in DOC_SHARE_PERMISSIONS:
        raise doc_routes._http_error(
            400, "invalid_permission",
            f"permission must be one of: {', '.join(DOC_SHARE_PERMISSIONS)}.",
        )
    return permission


def _share_not_found() -> Exception:
    return doc_routes._http_error(404, "share_not_found", "Share not found.")


def _user_not_found() -> Exception:
    return doc_routes._http_error(404, "user_not_found", "No Quest user has that email.")


@router.post("/docs/{doc_id}/shares")
async def add_doc_share(
    doc_id: str,
    body: Optional[AddDocShareRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Share a doc with one Quest user (by email) or with everyone, or
    change that recipient's permission (owner only). Returns the owner's
    row. Errors in the module docstring."""
    doc_routes._require_docs_enabled(user)
    doc, _access = await doc_routes._get_doc_for_ui(user, doc_id)
    doc_routes._require_owner(user, doc, "manage its sharing")
    body = body or AddDocShareRequest()
    email = _parse_target(body)
    permission = _parse_permission(body.permission)

    recipient_id: Optional[int] = None
    if email is not None:
        try:
            recipient = await user_store.find_user_by_email_ci(email)
        except user_store.AmbiguousUserEmailError:
            raise doc_routes._http_error(
                409, "ambiguous_user",
                "Several Quest accounts match that email; type it exactly.",
            )
        if recipient is None:
            raise _user_not_found()
        if recipient["id"] == doc["owner_id"]:
            raise doc_routes._http_error(
                400, "cannot_share_with_owner",
                "You own this doc; share it with someone else.",
            )
        recipient_id = recipient["id"]

    async def _locked() -> dict:
        async with doc_service._write_lock(doc["id"]):
            previous = next(
                (
                    share for share in await doc_store.list_shares(doc["id"])
                    if share["user_id"] == recipient_id
                ),
                None,
            )
            try:
                await doc_store.add_share(doc["id"], recipient_id, permission)
            except doc_store.ShareTargetGoneError:
                # A foreign key refused the row: the doc or the recipient
                # was deleted after the checks above. Name the one gone.
                if await doc_store.get_doc(doc["id"], with_shares=False) is None:
                    raise doc_routes._doc_not_found(doc_id)
                raise _user_not_found()
            except doc_store.DocValidationError:
                # Every input was validated above, so the doc is gone.
                raise doc_routes._doc_not_found(doc_id)
            current = await doc_store.get_doc(doc["id"], with_shares=True)
            if current is None:
                raise doc_routes._doc_not_found(doc_id)
            if previous is None or previous["permission"] != permission:
                doc_events.publish_share_changed(
                    current["id"], current["owner_id"], recipient_id,
                )
            return current

    current = await doc_service._shielded(_locked())
    return await doc_routes._ui_row(user, current)


@router.delete("/docs/{doc_id}/shares/{share_id}")
async def remove_doc_share(
    doc_id: str,
    share_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Remove one share row (owner only); the recipient loses access at
    once (a ``write_doc`` card they proposed is refused at approve time).
    Returns the owner's row. 404 ``share_not_found`` for a malformed id or
    a share of another doc."""
    doc_routes._require_docs_enabled(user)
    doc, _access = await doc_routes._get_doc_for_ui(user, doc_id)
    doc_routes._require_owner(user, doc, "manage its sharing")
    if not _SHARE_ID_RE.fullmatch(share_id):
        raise _share_not_found()
    wanted = int(share_id)

    async def _locked() -> dict:
        async with doc_service._write_lock(doc["id"]):
            target = next(
                (
                    share for share in await doc_store.list_shares(doc["id"])
                    if share["id"] == wanted
                ),
                None,
            )
            if target is None or not await doc_store.remove_share(doc["id"], wanted):
                raise _share_not_found()
            current = await doc_store.get_doc(doc["id"], with_shares=True)
            if current is None:
                raise doc_routes._doc_not_found(doc_id)
            doc_events.publish_share_changed(
                current["id"], current["owner_id"], target["user_id"],
            )
            return current

    current = await doc_service._shielded(_locked())
    return await doc_routes._ui_row(user, current)
