"""HTTP routes for Quest Docs (spec section 7, the v1 rows).

Everything is under ``/app/api``, authenticated by
``get_current_user_cookie_or_apikey_checked`` and refused with 403
``docs_disabled`` while the ``docs`` feature gate is closed for the user
(checked first in every handler, before any DB work).

Visibility goes through the one access rule
(``chat.docs.access.resolve_doc_access`` with ``run_kind="ui"``, via
``chat.docs.service.get_visible_doc``): a doc the user cannot see is a
404 ``doc_not_found`` with exactly the body a nonexistent id gets. Docs of
a public project are hidden the same way while the ``public_projects``
gate is closed for the viewer or for the doc's owner (frozen for everyone). Rename, mode switch, delete and share
management (chat/docs/share_routes.py) additionally require ownership (403
``forbidden`` for a visible non-owned doc) -- a route check, not part of the
matrix.

Every row returned to the UI is built by :func:`_ui_row` (one batched user
lookup per response via ``db.user_store.get_users_by_ids``): the store's
doc dict plus

- ``scope`` (``user`` / ``project``), ``shared`` (has any share row);
- ``access``: ``write`` (the UI verdict), ``can_edit`` (``write ==
  "free"``: owner or write share -- content edits, image upload, restore),
  ``can_rename`` / ``can_delete`` / ``can_share`` / ``can_delete_assets``
  (owner only), ``can_switch_mode`` (always false);
- ``shared_with_me`` (the caller is not the owner) and ``permission``
  (the caller's effective share, ``read`` / ``write``; null for the owner);
- ``owner {id, name, email}`` -- for non-owners only (null for the owner);
- ``shares`` -- owner only, each entry with ``user {id, name, email}``
  (null for the everyone row);
- ``last_write_user {id, name, email}`` -- owner only: the user of a
  ``ui:<user_id>`` source, or of an ``action_request:<id>`` source whose
  approved card belonged to someone other than the owner (a write-share
  recipient; a proposer whose account is gone keeps ``{id, name: null,
  email: null}``); null otherwise, or for a ``ui:<id>`` user who is gone.
  ``last_write_source`` stays raw for the owner and is null for non-owners.
  Image-only writes (upload / asset delete) keep ``last_write_source``: it
  names the writer of the current body.

``GET /docs`` serves three keyset-paged streams: the caller's own user
docs (default), one project's docs (``project_id``), and docs shared WITH
the caller (``shared=true``, user and project docs). A share recipient
sees shared docs here (read; a write share may also edit and restore), but
from the recipient's conversations only shared USER docs are reachable --
project docs stay hidden outside their project's conversations, and
projects are not shared (chat/docs/access.py, rule 2). The owner's first
share on a private doc turns the owner's own conversation writes into
approval cards.

Images: owners and write shares see every asset of a doc; a read-only
viewer (read or everyone-read share) sees only the images the CURRENT body
references -- in the detail's ``assets`` list, on the asset route (404
``asset_not_found`` otherwise) and in the zip download -- so an image the
owner removed from the text before sharing stays private.

All sync file IO runs in ``asyncio.to_thread``. Doc content is never
logged. Realtime events go to the doc's whole audience (owner, share
recipients, every connected user for an everyone share; chat/docs/events.py).

User docs are always private: POST with ``mode: "public"`` and no project
is 400 ``user_doc_mode_private``; the only public docs are the docs of a
public project, which take the project's mode. No doc's mode can be
switched in v1: ``PUT /docs/{id}/mode`` stays registered but refuses every
doc, and ``access.can_switch_mode`` is always false.

Errors are ``HTTPException(detail={"error": <code>, "message": ...})``
except the optimistic-concurrency conflict, which is a flat 409
``{"error": "stale_update", "message", "current": row}`` like routines.
"""

import asyncio
import logging
import os
import re
import unicodedata
from collections import OrderedDict
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.docs import events as doc_events
from chat.docs import files as doc_files
from chat.docs import service as doc_service
from chat.docs import ui_writes
from chat.docs.access import DocAccess, effective_share, resolve_doc_access
from chat.docs.constants import doc_not_found_message, docs_disabled_message
from chat.file_routes import _INLINE_IMAGE_MIMES
from chat.project_routes import _get_visible_project, _public_projects_enabled_for
from config.feature_gates import docs_enabled_for
from db import action_request_store, doc_store, user_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)

DOWNLOAD_FORMATS = ("md", "zip")

# DocRequestError codes that are not a plain 400.
_REQUEST_ERROR_STATUS = {
    "project_not_found": 404,
    "duplicate_title": 409,
}

# Characters never allowed in a download file name (path separators,
# Windows-reserved punctuation, quotes); control/format characters are
# dropped separately by Unicode category.
_FILENAME_UNSAFE_RE = re.compile(r'[/\\:*?"<>|]+')
_FILENAME_MAX_LEN = 120

# Every name files.add_asset can produce (sanitize_asset_name stem + a
# sniffed extension); the detail payload lists nothing else.
_LISTED_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


class CreateDocRequest(BaseModel):
    title: str
    description: Optional[str] = None
    mode: Optional[str] = None
    project_id: Optional[str] = None


class UpdateDocRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    expected_updated_at: Optional[str] = None


class SetDocModeRequest(BaseModel):
    mode: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"error": code, "message": message},
    )


def _require_docs_enabled(user: dict) -> None:
    """403 ``docs_disabled`` unless the docs gate is open for this user."""
    if not docs_enabled_for(user.get("email") or ""):
        raise _http_error(403, "docs_disabled", docs_disabled_message())


def _ui_caller(user: dict) -> doc_service.Caller:
    return doc_service.Caller(
        user=user, conversation_id=None, project_id=None,
        is_public=False, run_kind="ui",
    )


def _ui_access(user: dict, doc: dict) -> DocAccess:
    return resolve_doc_access(
        doc, user_id=user["id"], is_public=False, project_id=None, run_kind="ui",
    )


def _doc_not_found(doc_id: str) -> HTTPException:
    # The one body for a missing AND a hidden doc (invariant 2).
    return _http_error(404, "doc_not_found", doc_not_found_message(doc_id))


_UI_SOURCE_RE = re.compile(r"ui:([0-9]{1,18})")
_ACTION_REQUEST_SOURCE_RE = re.compile(r"action_request:([0-9]{1,18})")


def _source_id(pattern: re.Pattern, source) -> Optional[int]:
    if not isinstance(source, str):
        return None
    match = pattern.fullmatch(source)
    return int(match.group(1)) if match else None


def _ui_source_user_id(source) -> Optional[int]:
    """The user id of a ``ui:<user_id>`` write source, else None (legacy
    plain ``"ui"``, conversation / action-request sources, garbage)."""
    return _source_id(_UI_SOURCE_RE, source)


def _last_writer_id(doc: dict, card_owners: dict) -> Optional[int]:
    """Who wrote the doc's current state, for the owner's ``last_write_user``.

    ``ui:<user_id>`` -> that user. ``action_request:<id>`` -> the user whose
    approved card it was (``card_owners``: request id -> user id), but only
    when that is NOT the owner -- a share recipient's approved change, so
    the owner's footer can name them (the owner's own cards keep the
    "action request #id" wording). Anything else -> None.
    """
    source = doc.get("last_write_source")
    writer = _ui_source_user_id(source)
    if writer is not None:
        return writer
    request_id = _source_id(_ACTION_REQUEST_SOURCE_RE, source)
    if request_id is not None:
        proposer = card_owners.get(request_id)
        if proposer is not None and proposer != doc["owner_id"]:
            return proposer
    return None


def _row_user_ids(user: dict, doc: dict, card_owners: Optional[dict] = None) -> set[int]:
    """The user ids :func:`_doc_row` resolves for this viewer: the owner
    (non-owners), or the share recipients and the last writer (owner)."""
    if doc["owner_id"] != user["id"]:
        return {doc["owner_id"]}
    ids = {
        share["user_id"] for share in doc.get("shares") or []
        if share.get("user_id") is not None
    }
    writer = _last_writer_id(doc, card_owners or {})
    if writer is not None:
        ids.add(writer)
    return ids


def _user_ref(users: dict, user_id: int) -> dict:
    """``{id, name, email}`` of a user; a user missing from the lookup (a
    race with an account deletion -- the cascade removes their shares) keeps
    its id with null name/email."""
    found = users.get(user_id)
    if found is not None:
        return {"id": found["id"], "name": found.get("name"), "email": found.get("email")}
    return {"id": user_id, "name": None, "email": None}


def _doc_row(
    user: dict,
    doc: dict,
    access: DocAccess,
    users: Optional[dict] = None,
    card_owners: Optional[dict] = None,
) -> dict:
    """The UI row (shape in the module docstring) for ``user``.

    ``users`` is the ``get_users_by_ids`` result covering
    :func:`_row_user_ids`; unresolved ids keep their id with null
    name/email (``last_write_user`` becomes null). ``card_owners`` maps
    action-request ids to their users (``_last_writer_id``). New code calls
    :func:`_ui_row`, which does both lookups.

    ``access.can_switch_mode`` is always false (user docs are always
    private, project docs keep their project's mode); the key stays in the
    row shape for the frontend type.
    """
    users = users or {}
    is_owner = doc["owner_id"] == user["id"]
    shares = doc.get("shares") or []
    row = {key: value for key, value in doc.items() if key != "shares"}
    row["scope"] = "project" if doc.get("project_id") else "user"
    row["shared"] = bool(shares)
    row["shared_with_me"] = not is_owner
    row["permission"] = None if is_owner else effective_share(doc, user["id"])
    if is_owner:
        row["owner"] = None
        row["shares"] = [
            {
                **share,
                "user": (
                    None if share.get("user_id") is None
                    else _user_ref(users, share["user_id"])
                ),
            }
            for share in shares
        ]
        writer = _last_writer_id(doc, card_owners or {})
        if writer is None:
            row["last_write_user"] = None
        elif writer in users or _ui_source_user_id(doc.get("last_write_source")) is None:
            # Known user; or a recipient's approved card whose proposer is
            # gone -- keep the id with null name/email, like shares do.
            row["last_write_user"] = _user_ref(users, writer)
        else:
            # A ``ui:<id>`` writer who no longer exists: the raw source
            # already carries the id.
            row["last_write_user"] = None
    else:
        row["owner"] = _user_ref(users, doc["owner_id"])
        # The owner's conversation ids (and who else edits) are not the
        # recipient's business.
        row["last_write_source"] = None
        row["last_write_user"] = None
    row["access"] = {
        "can_rename": is_owner,
        "can_switch_mode": False,
        "can_delete": is_owner,
        "can_edit": access.write == "free",
        "can_share": is_owner,
        "can_delete_assets": is_owner,
        "write": access.write,
    }
    return row


async def _ui_rows(
    user: dict,
    pairs: list[tuple[dict, Optional[DocAccess]]],
    known_users: Optional[dict] = None,
) -> list[dict]:
    """:func:`_doc_row` for many docs with at most ONE ``get_users_by_ids``
    call (none when no row needs a name, or ``known_users`` -- a lookup the
    caller already made -- covers them all), preceded by at most one
    ``get_action_request_owners`` call when an owner row's last write was
    an approved card. ``access`` None = the UI verdict for ``user``."""
    request_ids = {
        request_id
        for doc, _access in pairs
        if doc["owner_id"] == user["id"]
        and (request_id := _source_id(
            _ACTION_REQUEST_SOURCE_RE, doc.get("last_write_source"),
        )) is not None
    }
    card_owners = (
        await action_request_store.get_action_request_owners(request_ids)
        if request_ids else {}
    )
    users = dict(known_users or {})
    ids: set[int] = set()
    for doc, _access in pairs:
        ids |= _row_user_ids(user, doc, card_owners)
    missing = ids - users.keys()
    if missing:
        users.update(await user_store.get_users_by_ids(missing))
    return [
        _doc_row(
            user, doc, access if access is not None else _ui_access(user, doc),
            users, card_owners,
        )
        for doc, access in pairs
    ]


async def _ui_row(user: dict, doc: dict, access: Optional[DocAccess] = None) -> dict:
    """The row every UI response carries for one doc (see :func:`_doc_row`).

    Async so it can enrich the row with user lookups (owner, share
    recipients, the last UI writer). ``access`` defaults to the UI verdict
    for ``user``. Every route returning a doc row goes through here (or
    :func:`_ui_rows` for a list).
    """
    return (await _ui_rows(user, [(doc, access)]))[0]


async def _stale_conflict(user: dict, current: dict) -> JSONResponse:
    """The flat 409 ``stale_update`` body (no ``detail`` wrapper, like
    PUT /routines/{id}) carrying the current row (via :func:`_ui_row`)."""
    return JSONResponse(
        status_code=409,
        content={
            "error": "stale_update",
            "message": "This doc was modified by someone else.",
            "current": await _ui_row(user, current),
        },
    )


def _asset_rows(assets: list[dict]) -> list[dict]:
    """``[{"name", "size", "mime"}]`` for the detail payload.

    ``assets`` comes from ``files.list_assets`` (regular, non-hidden files
    directly in ``assets/``, sorted by name). Left out: names outside
    ``[A-Za-z0-9._-]`` (``add_asset`` never writes one, so anything else
    was planted) and entries the asset route would 404 -- a name ``files``
    refuses or an extension outside ``_INLINE_IMAGE_MIMES``.
    """
    rows = []
    for asset in assets:
        name = asset["name"]
        # fullmatch: "$" alone would also accept a trailing newline.
        if not _LISTED_ASSET_NAME_RE.fullmatch(name):
            continue
        try:
            doc_files._validate_asset_name(name)
        except doc_files.DocFileError:
            continue
        mime = _INLINE_IMAGE_MIMES.get(os.path.splitext(name)[1].lower())
        if mime is None:
            continue
        rows.append({"name": name, "size": asset["size"], "mime": mime})
    return rows


def _sees_all_assets(access: DocAccess) -> bool:
    """Owners and write shares (UI verdict ``free``) see every asset,
    including ones only older revisions reference; read-only viewers see
    only the images the CURRENT body references (an image removed from the
    text before sharing stays private)."""
    return access.write == "free"


def _referenced_asset_rows(names: frozenset, rows: list[dict]) -> list[dict]:
    """The asset rows whose name is in ``names``
    (``ui_writes.referenced_asset_names`` of the body: the one reference
    matcher, escaped spellings included, computed once per body)."""
    return [row for row in rows if row["name"] in names]


def _referenced_asset_filter():
    """A ``files.build_zip`` ``asset_filter`` keeping only the images the
    archived body references: the name set is computed once per body
    (memoized on the body object build_zip passes for every asset), so the
    filter costs one :func:`ui_writes.referenced_asset_names` pass inside
    build_zip's file lock, not one per asset."""
    memo: dict = {}

    def _keep(body: str, name: str) -> bool:
        if memo.get("body") is not body:
            memo["body"] = body
            memo["names"] = ui_writes.referenced_asset_names(body)
        return name in memo["names"]

    return _keep


# The referenced-image set of a doc's body for the read-only asset route,
# keyed (doc_id, updated_at): every body write bumps updated_at, so a key
# never outlives its body (a viewer's page loads N images; one body pass
# serves them all). Event-loop access only; the set is computed in a thread.
_REFERENCED_NAMES_CACHE: "OrderedDict[tuple[str, str], frozenset]" = OrderedDict()
_REFERENCED_NAMES_CACHE_SIZE = 64


def _remember_referenced_names(doc: dict, names: frozenset) -> None:
    key = (doc["id"], doc["updated_at"])
    _REFERENCED_NAMES_CACHE[key] = names
    _REFERENCED_NAMES_CACHE.move_to_end(key)
    while len(_REFERENCED_NAMES_CACHE) > _REFERENCED_NAMES_CACHE_SIZE:
        _REFERENCED_NAMES_CACHE.popitem(last=False)


def _referenced_names_from_disk(doc_id: str) -> frozenset:
    return ui_writes.referenced_asset_names(doc_files.read_body(doc_id))


async def _referenced_names(doc: dict) -> frozenset:
    """The names ``doc``'s current body references, cached per
    ``(doc_id, updated_at)`` (raises DocFileError / OSError when the body
    cannot be read). A write landing between the row read and the body
    read can only make the cached set newer than its key, never staler
    than the key's body (the file is written before the row is bumped; the
    window where the file is new and the row old is the write lock's)."""
    key = (doc["id"], doc["updated_at"])
    names = _REFERENCED_NAMES_CACHE.get(key)
    if names is not None:
        _REFERENCED_NAMES_CACHE.move_to_end(key)
        return names
    names = await asyncio.to_thread(_referenced_names_from_disk, doc["id"])
    _remember_referenced_names(doc, names)
    return names


def _asset_not_found(name: str) -> HTTPException:
    # One body for a missing, unsafe, or (read-only viewer) unreferenced name.
    return _http_error(404, "asset_not_found", f"Image not found: {name}")


async def _get_doc_for_ui(user: dict, doc_id: str) -> tuple[dict, DocAccess]:
    """``(doc, access)`` for a doc the user may see, else 404 doc_not_found.

    Docs of a public project are hidden (same 404) while the
    ``public_projects`` gate is closed for the viewer OR for the doc's
    owner, matching the project routes' "hidden public projects 404 on
    every by-id endpoint" rule: a project hidden from its owner is frozen
    for its share recipients too (no reads, edits or restores the owner
    could not see). A project doc's mode mirrors its project's ``public``
    flag, so no project lookup is needed; the owner's email costs one user
    lookup, only for a public-project doc seen by a non-owner.
    """
    try:
        doc, access = await doc_service.get_visible_doc(_ui_caller(user), doc_id)
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocError:
        raise _doc_not_found(doc_id)
    if _is_public_project_doc(doc):
        if not _public_projects_enabled_for(user):
            raise _doc_not_found(doc_id)
        if doc["owner_id"] != user["id"]:
            owners = await user_store.get_users_by_ids([doc["owner_id"]])
            if not _public_projects_open(owners.get(doc["owner_id"]), {}):
                raise _doc_not_found(doc_id)
    return doc, access


def _is_public_project_doc(doc: dict) -> bool:
    """A doc of a public project (its mode mirrors ``projects.public``);
    hidden from the UI while the viewer's or the owner's
    ``public_projects`` gate is closed."""
    return doc.get("project_id") is not None and doc.get("mode") == "public"


def _public_projects_open(person: Optional[dict], cache: dict) -> bool:
    """Whether the ``public_projects`` gate is open for ``person`` (a
    ``get_users_by_ids`` entry; None -- a user that no longer exists --
    fails closed). ``cache`` memoizes per user id within one request."""
    if person is None:
        return False
    if person["id"] not in cache:
        cache[person["id"]] = _public_projects_enabled_for(person)
    return cache[person["id"]]


def _raise_request_error(exc: "doc_service.DocRequestError", doc_id: str):
    status = {
        "forbidden": 403,
        "duplicate_title": 409,
        "project_not_found": 404,
    }.get(exc.code, 400)
    raise _http_error(status, exc.code, str(exc))


def _require_owner(user: dict, doc: dict, action: str) -> None:
    if doc["owner_id"] != user["id"]:
        raise _http_error(403, "forbidden", f"Only the doc's owner can {action}.")


async def _require_visible_project(user: dict, project_id: str) -> dict:
    """The project, owned by the user and not hidden by the public-projects
    gate (the same check the project routes use), else 404."""
    project = await _get_visible_project(user, project_id)
    if project is None:
        raise _http_error(404, "project_not_found", "Project not found.")
    return project


async def _doc_files_call(fn, *args):
    """Run a sync chat/docs/files.py call in a thread, mapping its errors.

    ``DocFileError`` (missing body, symlink or special file in the doc
    directory) -> 400 ``invalid_doc_files``; any other OSError -> 500
    ``doc_storage_error`` without the path.
    """
    try:
        return await asyncio.to_thread(fn, *args)
    except doc_files.DocFileError as exc:
        raise _http_error(400, "invalid_doc_files", str(exc))
    except OSError as exc:
        logger.warning("[docs] file operation failed", exc_info=True)
        raise _http_error(
            500, "doc_storage_error",
            f"Doc storage error: {exc.strerror or exc.__class__.__name__}.",
        )


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a ``<updated_at_iso>|<doc_id>`` keyset cursor (400 on garbage)."""
    ts_part, sep, id_part = cursor.partition("|")
    if not sep or not id_part:
        raise _http_error(400, "invalid_cursor", "Malformed cursor.")
    try:
        ts = datetime.fromisoformat(ts_part)
    except ValueError:
        raise _http_error(400, "invalid_cursor", "Malformed cursor.")
    return ts, id_part


async def _visible_docs_page(
    user: dict,
    project_id: Optional[str],
    limit: Optional[int],
    before: Optional[tuple[datetime, str]],
    *,
    shared: bool = False,
) -> tuple[list[tuple[dict, DocAccess]], bool, dict]:
    """Visible docs of one stream, newest-updated first, plus ``has_more``
    and the owners looked up on the way (``{user_id: {id, email, name}}``,
    for :func:`_ui_rows`' ``known_users``).

    Streams: ``shared`` -> ``doc_store.list_docs_shared_with`` (docs shared
    with the user that they do not own, user and project docs); else
    ``project_id`` -> that project's docs; else the user's OWN user docs.
    Every row still passes the access rule, and public-project docs are
    dropped while the viewer's OR the owner's ``public_projects`` gate is
    closed (the ``_get_doc_for_ui`` rule; the viewer's half is pushed into
    SQL for the shared stream, the owner's needs the owners' emails --
    one lookup per store batch, reused for the rows). With ``limit`` it
    gathers ``limit + 1`` visible rows (paging the store past any hidden
    ones) so ``has_more`` is exact; without it, the whole list.
    """
    want = None if limit is None else limit + 1
    viewer_gate_open = _public_projects_enabled_for(user) if shared else None
    gate_cache: dict = {}
    owners: dict = {}
    out: list[tuple[dict, DocAccess]] = []
    while True:
        if shared:
            rows = await doc_store.list_docs_shared_with(
                user["id"], limit=want, before=before,
                include_public_project_docs=viewer_gate_open,
            )
        else:
            rows = await doc_store.list_accessible_docs(
                user["id"],
                project_id=project_id,
                include_user_docs=project_id is None,
                include_project_docs=project_id is not None,
                owned_only=project_id is None,
                limit=want,
                before=before,
            )
        # Other users' docs (the shared stream; the own/project streams hold
        # only the viewer's): their owners, in one lookup per batch.
        missing = {
            doc["owner_id"] for doc in rows if doc["owner_id"] != user["id"]
        } - owners.keys()
        if missing:
            owners.update(await user_store.get_users_by_ids(missing))
        for doc in rows:
            access = _ui_access(user, doc)
            if not access.visible:
                continue
            if _is_public_project_doc(doc):
                if not _public_projects_open(user, gate_cache):
                    continue
                if doc["owner_id"] != user["id"] and not _public_projects_open(
                    owners.get(doc["owner_id"]), gate_cache,
                ):
                    continue
            out.append((doc, access))
            if want is not None and len(out) >= want:
                break
        if want is None or len(out) >= want or len(rows) < want:
            break
        last = rows[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])
    has_more = limit is not None and len(out) > limit
    if has_more:
        out = out[:limit]
    return out, has_more, owners


def _download_filename(title: str, ext: str) -> str:
    """A file name derived from the doc title, safe for any file system."""
    name = unicodedata.normalize("NFC", title or "")
    name = "".join(
        ch for ch in name if not unicodedata.category(ch).startswith("C")
    )
    name = _FILENAME_UNSAFE_RE.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    name = name[:_FILENAME_MAX_LEN].rstrip(" .-") or "doc"
    return f"{name}.{ext}"


def _attachment_disposition(filename: str) -> str:
    """``attachment`` Content-Disposition: ASCII fallback + RFC 5987 UTF-8."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


# ---------------------------------------------------------------------------
# List + create
# ---------------------------------------------------------------------------


@router.get("/docs")
async def list_user_docs(
    project_id: Optional[str] = Query(None),
    shared: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List docs, newest-updated first. Three independent streams:

    - default (no ``project_id``, ``shared`` false): the user's OWN user
      docs (no project). User docs shared with the user are not here.
    - ``project_id``: that project's docs (the project must be the user's
      and visible, else 404 ``project_not_found``).
    - ``shared=true``: docs shared WITH the user (a direct or an everyone
      share) that they do not own, user and project docs alike; docs of a
      public project are left out while the user's OR the doc owner's
      ``public_projects`` gate is closed. Together with ``project_id`` ->
      400 ``invalid_request``.

    With no ``limit`` the whole stream is returned; ``limit`` (1..200) pages
    with an opaque keyset ``next_cursor`` (``<updated_at>|<id>``) the client
    echoes back as ``cursor`` (400 ``invalid_cursor`` on garbage); each
    stream pages on its own. ``has_more`` and ``next_cursor`` are always
    present.

    Returns:
        ``{"docs": [row, ...], "has_more": bool, "next_cursor": str | None}``.
    """
    _require_docs_enabled(user)
    project_id = project_id or None
    if shared and project_id is not None:
        raise _http_error(
            400, "invalid_request",
            "shared=true lists docs shared with you across all projects; "
            "it cannot be combined with project_id.",
        )
    if project_id is not None:
        await _require_visible_project(user, project_id)
    before = _parse_cursor(cursor) if cursor else None

    page, has_more, owners = await _visible_docs_page(
        user, project_id, limit, before, shared=shared,
    )
    rows = await _ui_rows(user, page, known_users=owners)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last['updated_at']}|{last['id']}"
    return {"docs": rows, "has_more": has_more, "next_cursor": next_cursor}


@router.post("/docs", status_code=201)
async def create_ui_doc(
    body: CreateDocRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Create an empty doc owned by the user.

    A user doc is always private: ``mode`` omitted or ``"private"`` gives
    a private doc, ``"public"`` is 400 ``user_doc_mode_private`` (a public
    doc is created inside a public project). A project doc takes its
    project's mode, and a supplied ``mode`` that disagrees is 400
    ``project_doc_mode_inherited``; a public project hidden by the
    ``public_projects`` gate is 404 ``project_not_found``. Errors also:
    409 ``duplicate_title``, 400 ``invalid_title`` / ``invalid_description``
    / ``invalid_mode``. Returns the row (201).
    """
    _require_docs_enabled(user)
    project_id = body.project_id or None
    if project_id is not None:
        await _require_visible_project(user, project_id)
    try:
        doc = await doc_service.create_doc_from_ui(
            user, body.title, body.description or "", body.mode, project_id,
        )
    except doc_service.DocRequestError as exc:
        raise _http_error(
            _REQUEST_ERROR_STATUS.get(exc.code, 400), exc.code, str(exc),
        )
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocError as exc:
        raise _http_error(500, "doc_storage_error", str(exc))
    return await _ui_row(user, doc)


# ---------------------------------------------------------------------------
# One doc
# ---------------------------------------------------------------------------


@router.get("/docs/{doc_id}")
async def get_ui_doc(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """The row plus ``content`` (the whole ``doc.md``) and ``assets``.

    ``assets`` is ``[{"name", "size", "mime"}]`` for the embedded images
    the asset route serves: regular files directly in ``assets/`` (symlinks,
    directories, special and hidden files skipped), sorted by name, ``mime``
    from the extension, entries without a raster image extension left out.
    At most ``DOC_MAX_ASSETS`` entries, so it rides on this payload rather
    than a separate endpoint; the list route does not carry it. A read-only
    viewer (UI verdict not ``free``: a read or everyone-read share) gets
    only the images ``content`` references (``ui_writes.referenced_asset_names``,
    escaped spellings included); owners and write shares get all of them.

    404 ``doc_not_found`` for a missing or hidden doc (identical bodies).
    """
    _require_docs_enabled(user)
    doc, access = await _get_doc_for_ui(user, doc_id)
    content = await _doc_files_call(doc_files.read_body, doc["id"])
    assets = await _doc_files_call(doc_files.list_assets, doc["id"])
    asset_rows = _asset_rows(assets)
    if not _sees_all_assets(access):
        # Read-only viewers: only the images this very body references (one
        # pass over the body; it also primes the asset route's cache).
        names = await asyncio.to_thread(ui_writes.referenced_asset_names, content)
        _remember_referenced_names(doc, names)
        asset_rows = _referenced_asset_rows(names, asset_rows)
    row = await _ui_row(user, doc, access)
    row["content"] = content
    row["assets"] = asset_rows
    row["last_write_conversation"] = await _last_write_conversation(user, row)
    return row


async def _last_write_conversation(user: dict, row: dict) -> Optional[dict]:
    """``{id, title, project_id}`` of the conversation named by the row's
    ``last_write_source``, for the viewer's "Last written by" footer.

    One metadata-row lookup instead of the viewer fetching the whole chat
    history for a title. None when the source is not a conversation (``ui``
    / ``action_request:<id>`` / blanked for non-owners) or the conversation
    no longer exists -- the viewer then shows "a deleted conversation".
    """
    source = row.get("last_write_source")
    if not isinstance(source, str) or not source.startswith("conversation:"):
        return None
    conversation_id = source[len("conversation:"):]
    if not conversation_id:
        return None
    from chat.storage import ChatStorage
    from db.conversation_store import get_conversation_meta

    meta = await get_conversation_meta(user["id"], conversation_id)
    if not meta:
        return None
    title = await asyncio.to_thread(ChatStorage._resolve_list_title, conversation_id, meta)
    return {
        "id": conversation_id,
        "title": title,
        "project_id": meta.get("project_id"),
    }


@router.put("/docs/{doc_id}")
async def update_ui_doc(
    doc_id: str,
    body: UpdateDocRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Rename and/or change the description (owner only).

    ``expected_updated_at`` is the optimistic-concurrency token: a mismatch
    is a flat 409 ``stale_update`` carrying the ``current`` row. 409
    ``duplicate_title``; 400 ``invalid_title`` / ``invalid_description``;
    403 ``forbidden`` for a visible doc the user does not own. A change
    publishes ``doc_list_changed`` + ``doc_changed`` to the doc's audience.
    """
    _require_docs_enabled(user)
    doc, _access = await _get_doc_for_ui(user, doc_id)
    _require_owner(user, doc, "rename it")
    try:
        title, description = doc_service.validate_doc_metadata(
            body.title, body.description,
        )
        updated = await doc_store.update_doc_metadata(
            doc["id"],
            title=title,
            description=description,
            expected_updated_at=body.expected_updated_at,
        )
    except doc_service.DocRequestError as exc:
        raise _http_error(400, exc.code, str(exc))
    except doc_store.StaleDocError as exc:
        return await _stale_conflict(user, exc.current)
    except doc_store.DuplicateDocTitleError as exc:
        raise _http_error(409, "duplicate_title", str(exc))
    except doc_store.DocValidationError as exc:
        raise _http_error(400, "invalid_request", str(exc))
    if updated is None:
        raise _doc_not_found(doc_id)
    if updated["updated_at"] != doc["updated_at"]:
        # Owner + share recipients (every connected user for an everyone
        # share): the title shows in their lists and viewers too.
        doc_events.publish_doc_list_changed_for(updated)
        doc_events.publish_doc_changed_for(updated)
    return await _ui_row(user, updated)


@router.put("/docs/{doc_id}/mode")
async def set_ui_doc_mode(
    doc_id: str,
    body: Optional[SetDocModeRequest] = None,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Not switchable in v1: refuses every doc, whatever the body says
    (the body is optional so a missing or malformed one still gets the
    404 / 403 / 400 below rather than a 422).

    Checked in order: 404 ``doc_not_found`` (missing, hidden, or in a
    public project hidden by the ``public_projects`` gate); 403
    ``forbidden`` for a non-owner; then 400 ``user_doc_mode_private`` for a
    user doc (always private) or 400 ``project_doc_mode_inherited`` for a
    project doc (its mode is the project's). Nothing changes and no event
    is sent. Kept registered so an old client gets a clear error.
    """
    _require_docs_enabled(user)
    doc, _access = await _get_doc_for_ui(user, doc_id)
    _require_owner(user, doc, "change its mode")
    _raise_request_error(doc_service.mode_switch_refusal(doc), doc_id)


@router.get("/docs/{doc_id}/assets/{name}")
async def get_ui_doc_asset(
    doc_id: str,
    name: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Serve one embedded image inline with its real MIME type.

    404 ``asset_not_found`` for anything that is not a regular,
    non-symlink file directly inside the doc's ``assets/`` with a raster
    image extension (traversal names included) -- and, for a read-only
    viewer, for an image the CURRENT body does not reference (same body:
    an image the owner removed from the text before sharing stays
    invisible). Assets are written only by the server (sniffed by magic
    bytes on the way in); the doc directory is never mounted into a
    sandbox.
    """
    _require_docs_enabled(user)
    doc, access = await _get_doc_for_ui(user, doc_id)
    if not _sees_all_assets(access):
        try:
            referenced = await _referenced_names(doc)
        except (doc_files.DocFileError, OSError):
            raise _asset_not_found(name)
        if name not in referenced:
            raise _asset_not_found(name)
    try:
        # read_asset re-validates the name and opens the leaf O_NOFOLLOW +
        # S_ISREG (FileResponse would follow a symlink at open time).
        data = await asyncio.to_thread(doc_files.read_asset, doc["id"], name)
    except (doc_files.DocFileError, OSError):
        raise _asset_not_found(name)
    media_type = _INLINE_IMAGE_MIMES.get(
        os.path.splitext(name)[1].lower(), "application/octet-stream",
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "private",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/docs/{doc_id}/download")
async def download_ui_doc(
    doc_id: str,
    download_format: str = Query("md", alias="format"),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Download a doc: ``?format=md`` (default) = ``doc.md`` as
    ``<title>.md``; ``?format=zip`` = ``doc.md`` + ``assets/`` as
    ``<title>.zip``. A read-only viewer's zip holds only the images the
    archived ``doc.md`` references (the filter runs inside ``build_zip``
    against the body it archives, under the doc lock).

    400 ``invalid_format`` for any other format; 400 ``invalid_doc_files``
    when the doc directory holds a symlink or special file (or the body is
    missing).
    """
    _require_docs_enabled(user)
    doc, access = await _get_doc_for_ui(user, doc_id)
    if download_format not in DOWNLOAD_FORMATS:
        raise _http_error(
            400, "invalid_format",
            f"format must be one of: {', '.join(DOWNLOAD_FORMATS)}.",
        )
    disposition = _attachment_disposition(
        _download_filename(doc["title"], download_format),
    )
    if download_format == "md":
        content = await _doc_files_call(doc_files.read_body, doc["id"])
        return Response(
            content=content.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": disposition},
        )
    zip_path = await _doc_files_call(
        doc_files.build_zip, doc["id"], doc["title"],
        None if _sees_all_assets(access) else _referenced_asset_filter(),
    )
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": disposition},
        background=BackgroundTask(os.unlink, zip_path),
    )


@router.delete("/docs/{doc_id}")
async def delete_ui_doc(
    doc_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete a doc (owner only): the row (shares go with it), then the
    directory (best-effort, logged), then ``doc_list_changed`` to the owner
    and every share recipient (captured before the delete). 403
    ``forbidden`` for a non-owner."""
    _require_docs_enabled(user)
    await _get_doc_for_ui(user, doc_id)
    try:
        await doc_service.delete_doc_from_ui(user, doc_id)
    except doc_service.DocRequestError as exc:
        _raise_request_error(exc, doc_id)
    except doc_service.DocDisabled:
        raise _http_error(403, "docs_disabled", docs_disabled_message())
    except doc_service.DocError:
        raise _doc_not_found(doc_id)
    return {"deleted": True}
