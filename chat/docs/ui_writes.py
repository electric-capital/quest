"""Human (UI) writes to a doc: whole-body replace, image upload, asset delete.

The HTTP routes of the editor (``PUT /docs/{id}/content``,
``POST /docs/{id}/assets``, ``DELETE /docs/{id}/assets/{name}`` in
chat/docs/edit_routes.py) and of History (restore) write through the
functions here. These are UI actions by a person, so they never go through
the model tools' approval card: the UI verdict of the access rule
(``resolve_doc_access(run_kind="ui")``) is ``free`` for the owner and for a
write-share recipient, and ``denied`` otherwise. Deleting an asset is
owner-only on top of that (a route-level ownership rule, like rename).

Same pipeline as chat/docs/service.py: feature gate -> visible doc ->
access -> per-doc asyncio write lock
(shielded) -> file op in a thread -> DB bump + realtime events
(``service._finish_write``). A body replace snapshots the replaced body into
``revisions/`` and records the writer in ``doc.meta.json``; asset-only
writes (image upload / delete) take no snapshot and leave ``doc.meta.json``
alone (assets are not part of a body version), bump ``updated_at`` and
``asset_count``, and keep the row's ``last_write_source`` (it names the
writer of the current BODY, which an image change does not touch).

A body write records ``ui:<user_id>`` (:func:`ui_write_source`) as its
source. The UI may write into a public (project) doc: content enters a
public doc only from public conversations or from a person in the UI, so
the taint invariant holds.

Invariant 5 of the spec (no MODEL-facing full replace) holds because these
functions are reachable only from the ``/app/api`` routes on the main
port, which no model-driven channel can address: the ``curl_proxy_*``
tools dispatch ``/api/*`` paths only (``route_dispatch.validate_url``);
restricted script sandboxes have no egress except a bridge to the sandbox
tool API port and hold only a short-lived ``qsb_`` token that authenticates
nothing on the main port; public-project sandboxes get no credential at
all. (The routes accept a session cookie or the user's API key, so the
protection is the unreachability, not the auth scheme.)
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import unquote

from chat.docs import constants
from chat.docs import files as doc_files
from chat.docs import service
from chat.docs.access import DENY_UI_READ_ONLY
from db import doc_store

logger = logging.getLogger(__name__)

__all__ = [
    "add_asset_from_ui",
    "asset_referenced",
    "delete_asset_from_ui",
    "ALT_MAX_CHARS",
    "image_markdown",
    "referenced_asset_names",
    "replace_body_from_ui",
    "ui_caller",
    "ui_write_source",
]

# MIME type per sniffed extension (files.sniff_image_type); the same values
# chat/file_routes.py ``_INLINE_IMAGE_MIMES`` serves the assets with.
_ASSET_MIMES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Characters escaped in an image's alt text: the bracket pair and the
# backslash (as service._image_markdown), plus the backtick and angle
# brackets, which open code spans / autolinks / raw HTML that take
# precedence over the link brackets and could swallow the ``](...)``, and
# the pipe, which would split a GFM table cell the image is placed in.
_ALT_ESCAPE_RE = re.compile(r"([\\\[\]`<>|])")
# Alt text longer than this (after cleaning) is truncated.
ALT_MAX_CHARS = 300

# A CommonMark backslash escape: backslash + one ASCII punctuation char.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")

# ``assets/<name>`` with the maximal run of name characters as the name; a
# lookahead so overlapping candidates (``assets/assets/x.png``) are all seen.
_ASSET_REF_RE = re.compile(r"(?=assets/([A-Za-z0-9._-]+))")


def ui_write_source(user: dict) -> str:
    """``ui:<user_id>`` -- who wrote a body/asset from the UI.

    Recorded in ``docs.last_write_source`` and in ``doc.meta.json`` (so the
    revision sidecars carry it into History). Legacy rows may hold a plain
    ``"ui"``, which means the doc's owner.
    """
    return f"ui:{user['id']}"


def ui_caller(user: dict) -> service.Caller:
    return service.Caller(
        user=user, conversation_id=None, project_id=None,
        is_public=False, run_kind="ui",
    )


async def _get_ui_doc(caller: service.Caller, doc_id: str):
    """``service.get_visible_doc`` for a UI caller; a hidden doc raises the
    one not-found text. Called again under the write lock so a share
    revoked while a request queued is honoured."""
    return await service.get_visible_doc(caller, doc_id)


def _check_content(content) -> str:
    """CRLF-normalized ``content`` within the size cap, or DocRequestError."""
    if not isinstance(content, str):
        raise service.DocRequestError("invalid_request", "content must be a string.")
    normalized = content.replace("\r\n", "\n")
    try:
        size = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        raise service.DocRequestError(
            "invalid_request", "content is not valid UTF-8 text.",
        ) from None
    if size > constants.DOC_MAX_CONTENT_SIZE:
        raise service.DocRequestError(
            "content_too_large",
            f"Doc content is {size} bytes, over the "
            f"{constants.DOC_MAX_CONTENT_SIZE}-byte limit.",
        )
    return normalized


async def _body_files(fn, *args, **kwargs):
    """``service._files`` for the ``doc.md`` reads and writes here, except
    that a refused body (missing, a symlink or special file -- a
    ``DocFileError``) becomes DocRequestError ``invalid_doc_files`` (400,
    as History's restore pre-check reports it) instead of a generic
    storage DocError (500)."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except doc_files.DocFileError as exc:
        raise service.DocRequestError("invalid_doc_files", str(exc)) from None
    except OSError as exc:
        logger.warning("[docs] file operation failed", exc_info=True)
        raise service.DocError(
            f"Doc storage error: {exc.strerror or exc.__class__.__name__}."
        ) from None


def _require_ui_write(access) -> None:
    if access.write != "free":
        raise service.DocRequestError("forbidden", access.deny_reason or DENY_UI_READ_ONLY)


def _require_owner(user: dict, doc: dict, action: str) -> None:
    if doc["owner_id"] != user["id"]:
        raise service.DocRequestError("forbidden", f"Only the doc's owner can {action}.")


async def replace_body_from_ui(
    user: dict,
    doc_id: str,
    content,
    *,
    expected_updated_at,
    write_source: Optional[str] = None,
) -> tuple[dict, str, bool]:
    """Replace a doc's whole body from the UI (editor save, History restore).

    Checked before taking the lock: the gate, the content (string, UTF-8,
    under ``DOC_MAX_CONTENT_SIZE`` after CRLF -> LF), the token (required),
    visibility (:func:`_get_ui_doc`) and
    the UI write verdict. Then, under the per-doc asyncio write lock and
    shielded from cancellation, everything that matters is re-checked
    against a FRESH row (TOCTOU: a share revoked or another write landing
    while this request queued): visibility, write verdict, and
    ``expected_updated_at == row.updated_at``. A body identical to the
    current one writes nothing (no snapshot, no ``updated_at`` bump, no
    event) and returns ``changed=False``. Otherwise ``files.write_body``
    snapshots the replaced body into ``revisions/`` and writes the new one
    with ``write_source`` (default ``ui:<user_id>``), and
    ``service._finish_write`` bumps the row and publishes the events.

    Returns:
        ``(doc, body, changed)``: the store dict (with shares) after the
        write, the body as stored, and whether anything was written.

    Raises:
        DocDisabled: gate closed.
        DocError: ``doc_not_found_message`` (missing or hidden), or a
            storage failure.
        DocRequestError: ``invalid_request`` / ``content_too_large`` /
            ``forbidden`` (no write access) / ``invalid_doc_files``
            (``doc.md`` missing, a symlink or a special file).
        doc_store.StaleDocError: token mismatch (carries the current row).
    """
    caller = ui_caller(user)
    service.require_enabled(caller)
    normalized = _check_content(content)
    if not isinstance(expected_updated_at, str) or not expected_updated_at:
        raise service.DocRequestError(
            "invalid_request", "expected_updated_at is required.",
        )
    doc, access = await _get_ui_doc(caller, doc_id)
    _require_ui_write(access)
    source = write_source or ui_write_source(user)

    async def _locked() -> tuple[dict, str, bool]:
        async with service._write_lock(doc["id"]):
            fresh, fresh_access = await _get_ui_doc(caller, doc["id"])
            _require_ui_write(fresh_access)
            if fresh["updated_at"] != expected_updated_at:
                raise doc_store.StaleDocError(current=fresh)
            current_body = await _body_files(doc_files.read_body, fresh["id"])
            if current_body == normalized:
                return fresh, current_body, False
            size = await _body_files(
                doc_files.write_body, fresh["id"], normalized, write_source=source,
            )
            updated = await service._finish_write(
                fresh["id"], content_size=size, write_source=source,
            )
            return updated, normalized, True

    return await service._shielded(_locked())


# ---------------------------------------------------------------------------
# Assets: editor image upload, owner asset delete
# ---------------------------------------------------------------------------


def _clean_alt(alt) -> str:
    """Alt text on one line: control/format characters -> space, runs of
    whitespace collapsed, trimmed, at most :data:`ALT_MAX_CHARS` long."""
    if not isinstance(alt, str):
        return ""
    text = "".join(
        " " if unicodedata.category(ch).startswith("C") else ch for ch in alt
    )
    return " ".join(text.split())[:ALT_MAX_CHARS].rstrip()


def _filename_stem(filename) -> str:
    """The uploaded file's base name without its extension."""
    if not isinstance(filename, str):
        return ""
    return PurePosixPath(filename.replace("\\", "/")).stem


def image_markdown(alt, filename, asset_name: str) -> str:
    """``![alt](assets/<asset_name>)`` for an uploaded image.

    ``alt`` defaults to the uploaded file's stem, then to the stored
    asset's stem, and is cut to :data:`ALT_MAX_CHARS`. Like
    ``service._image_markdown`` the text is kept on one line with ``\\``,
    ``[`` and ``]`` backslash-escaped; backticks, angle brackets and ``|``
    are escaped too, so the alt can never open a code span, autolink or
    HTML tag that swallows the link target, nor split a table cell.
    ``asset_name`` is always a sanitized ``[A-Za-z0-9._-]`` name from
    ``files.add_asset``.
    """
    text = (
        _clean_alt(alt)
        or _clean_alt(_filename_stem(filename))
        or asset_name.rsplit(".", 1)[0]
    )
    escaped = _ALT_ESCAPE_RE.sub(r"\\\1", text)
    return f"![{escaped}](assets/{asset_name})"


def _check_upload(data) -> str:
    """The sniffed extension of an uploaded image, or DocRequestError.

    ``data`` is at most ``DOC_MAX_IMAGE_SIZE + 1`` bytes (the route never
    reads more), so a longer value means the upload is over the cap.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise service.DocRequestError("invalid_request", "file is required.")
    ext = doc_files.sniff_image_type(data)
    if ext is None:
        raise service.DocRequestError(
            "invalid_image", "Not a supported raster image (png, jpg, gif, webp).",
        )
    cap = constants.DOC_MAX_IMAGE_SIZE
    if len(data) > cap:
        raise service.DocRequestError(
            "image_too_large", f"The image is over the {cap}-byte per-image limit.",
        )
    return ext


async def add_asset_from_ui(
    user: dict,
    doc_id: str,
    filename,
    data: bytes,
    alt=None,
) -> dict:
    """Store an uploaded raster image in the doc's ``assets/`` (editor upload).

    Needs the UI write verdict (owner or write share). Validated like
    ``add_doc_image``: magic-byte sniff (png/jpg/gif/webp; SVG and anything
    else refused), ``DOC_MAX_IMAGE_SIZE``, then -- under the per-doc write
    lock, shielded -- ``DOC_MAX_ASSETS`` / ``DOC_MAX_ASSETS_TOTAL_BYTES``
    against the files on disk. The stored name is
    ``files.sanitize_asset_name(filename)`` plus the sniffed extension,
    ``-2``, ``-3``... on collision. The body is NOT changed (the editor
    inserts the returned ``markdown`` itself and saves it with the next
    content PUT): no revision snapshot, ``doc.meta.json`` untouched. The row
    gets an asset-only bump (``asset_count`` and ``updated_at``;
    ``content_size`` and ``last_write_source`` -- the body's writer -- kept
    as read from the row under the lock) and the usual realtime events.

    Returns:
        ``{"asset": {"name", "size", "mime"}, "markdown", "asset_count",
        "updated_at", "previous_updated_at"}`` -- ``previous_updated_at`` is
        the row's token read under the lock just before this write, so an
        editor holding exactly that token may adopt ``updated_at``.

    Raises:
        DocDisabled; DocError (missing/hidden, storage failure);
        DocRequestError ``forbidden`` / ``invalid_request`` /
        ``invalid_image`` / ``image_too_large`` / ``asset_limit``.
    """
    caller = ui_caller(user)
    service.require_enabled(caller)
    doc, access = await _get_ui_doc(caller, doc_id)
    _require_ui_write(access)
    ext = _check_upload(data)
    data = bytes(data)
    name_hint = filename if isinstance(filename, str) else ""

    async def _locked() -> tuple[str, dict, doc_files.AssetInfo]:
        async with service._write_lock(doc["id"]):
            fresh, fresh_access = await _get_ui_doc(caller, doc["id"])
            _require_ui_write(fresh_access)
            count, total = await service._files(doc_files.asset_stats, fresh["id"])
            if count >= constants.DOC_MAX_ASSETS:
                raise service.DocRequestError(
                    "asset_limit",
                    f"This doc already has {count} images, the maximum is "
                    f"{constants.DOC_MAX_ASSETS}.",
                )
            if total + len(data) > constants.DOC_MAX_ASSETS_TOTAL_BYTES:
                raise service.DocRequestError(
                    "asset_limit",
                    f"Adding this image would bring the doc's images to "
                    f"{total + len(data)} bytes, over the "
                    f"{constants.DOC_MAX_ASSETS_TOTAL_BYTES}-byte limit.",
                )
            # Other service writers hold this lock too, so the caps checked
            # above still hold; add_asset re-checks them as a backstop.
            info = await service._files(doc_files.add_asset, fresh["id"], name_hint, data)
            updated = await service._finish_write(
                fresh["id"],
                content_size=fresh["content_size"],
                asset_count=info.asset_count,
                # Asset-only: the body (and so its writer) is unchanged.
                write_source=fresh["last_write_source"],
            )
            return fresh["updated_at"], updated, info

    previous, updated, info = await service._shielded(_locked())
    return {
        "asset": {"name": info.name, "size": info.size, "mime": _ASSET_MIMES[ext]},
        "markdown": image_markdown(alt, filename, info.name),
        "asset_count": info.asset_count,
        "updated_at": updated["updated_at"],
        "previous_updated_at": previous,
    }


def _reference_variants(body: str) -> tuple[str, ...]:
    """``body`` as written plus decoded copies a renderer could resolve to
    the same URL: markdown backslash escapes removed, then HTML character
    references and percent-escapes decoded in both orders (markdown
    resolves entities before the URL is percent-decoded; the reverse order
    is checked too, erring on "in use")."""
    unescaped = _MD_ESCAPE_RE.sub(r"\1", body)
    return (
        body,
        unquote(html.unescape(unescaped)),
        html.unescape(unquote(unescaped)),
    )


def referenced_asset_names(body: str) -> frozenset[str]:
    """Every name ``body`` references as ``assets/<name>``, in one pass.

    A name is the maximal run of name characters (``[A-Za-z0-9._-]``) after
    ``assets/``, so ``assets/chart.png.png`` references ``chart.png.png``
    and NOT ``chart.png`` (while ``![x](assets/chart.png)``,
    ``"assets/chart.png"`` and ``assets/chart.png?v=2`` reference
    ``chart.png``). Escaped spellings count too, scanned in every decoded
    copy of :func:`_reference_variants`: ``assets/ch%61rt.png``,
    ``assets%2Fchart.png``, ``assets/chart&#46;png``, ``assets/chart\\.png``.

    Names are NOT validated (callers that build paths from them validate,
    e.g. History's copy). Costs three full-body decodes plus a scan, so
    callers compute the set once per body and test membership; CPU-bound on
    a large body -- run it in a thread from async code.
    """
    names: set[str] = set()
    for text in _reference_variants(body):
        names.update(match.group(1) for match in _ASSET_REF_RE.finditer(text))
    return frozenset(names)


def asset_referenced(body: str, name: str) -> bool:
    """Whether ``body`` references ``assets/<name>``
    (``name in referenced_asset_names(body)``; see there for the rules).

    One call costs a whole :func:`referenced_asset_names` pass: to test
    several names against one body, compute the set once instead.
    """
    return name in referenced_asset_names(body)


def _asset_not_found(name) -> service.DocRequestError:
    return service.DocRequestError("asset_not_found", f"Image not found: {name}")


async def delete_asset_from_ui(user: dict, doc_id: str, name) -> dict:
    """Delete one of the doc's images (owner only).

    Checked in order: gate, visible doc (else the not-found DocError),
    ownership (``forbidden``), a valid asset name (``asset_not_found``).
    Then under the per-doc write lock, shielded: re-check visibility and
    ownership, require the asset to exist as a regular file
    (``asset_not_found``), refuse with ``asset_in_use`` while the CURRENT
    body references ``assets/<name>`` (:func:`asset_referenced`; older
    revisions may still reference it -- restoring one shows a broken
    image), then ``files.delete_asset`` (never follows a symlink, touches
    nothing outside ``assets/``). Asset-only write: no revision snapshot,
    ``doc.meta.json`` untouched, the row's ``asset_count`` / ``updated_at``
    bumped (``last_write_source`` kept: the body's writer), realtime events
    sent. The in-use check (one :func:`referenced_asset_names` pass over
    the body) runs in a thread.

    Returns:
        ``{"deleted": True, "asset_count", "updated_at",
        "previous_updated_at"}`` (``previous_updated_at`` as in
        :func:`add_asset_from_ui`).

    Raises:
        DocDisabled; DocError (missing/hidden, storage failure);
        DocRequestError ``forbidden`` / ``asset_not_found`` /
        ``asset_in_use`` / ``invalid_doc_files`` (unreadable ``doc.md``).
    """
    caller = ui_caller(user)
    service.require_enabled(caller)
    doc, _access = await _get_ui_doc(caller, doc_id)
    _require_owner(user, doc, "delete its images")
    try:
        doc_files._validate_asset_name(name)
    except doc_files.DocFileError:
        raise _asset_not_found(name) from None

    async def _locked() -> tuple[str, dict, int]:
        async with service._write_lock(doc["id"]):
            fresh, _fresh_access = await _get_ui_doc(caller, doc["id"])
            _require_owner(user, fresh, "delete its images")
            try:
                await asyncio.to_thread(doc_files.asset_path, fresh["id"], name)
            except (doc_files.DocFileError, OSError):
                raise _asset_not_found(name) from None
            body = await _body_files(doc_files.read_body, fresh["id"])
            if await asyncio.to_thread(asset_referenced, body, name):
                raise service.DocRequestError(
                    "asset_in_use",
                    f"The doc still shows {name}; remove it from the text "
                    "before deleting the image.",
                )
            try:
                count = await asyncio.to_thread(doc_files.delete_asset, fresh["id"], name)
            except doc_files.DocFileError:
                raise _asset_not_found(name) from None
            except OSError as exc:
                logger.warning("[docs] asset delete failed", exc_info=True)
                raise service.DocError(
                    f"Doc storage error: {exc.strerror or exc.__class__.__name__}."
                ) from None
            updated = await service._finish_write(
                fresh["id"],
                content_size=fresh["content_size"],
                asset_count=count,
                write_source=fresh["last_write_source"],
            )
            return fresh["updated_at"], updated, count

    previous, updated, count = await service._shielded(_locked())
    return {
        "deleted": True,
        "asset_count": count,
        "updated_at": updated["updated_at"],
        "previous_updated_at": previous,
    }
