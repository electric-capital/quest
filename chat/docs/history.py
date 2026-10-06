"""Quest Docs history: revision list/read/diff, restore, copy (Phase 3).

History is for EDITORS only: every entry point requires the UI write verdict
``free`` (``access.can_edit``: the owner or a write-share recipient). A
read-only viewer (read share, everyone-read share) gets ``forbidden``
(:data:`HISTORY_FORBIDDEN_MESSAGE`), because earlier versions can hold text
the owner removed before sharing, which a read recipient could otherwise
read and permanently copy. The routes check it right after visibility
(gate -> 404 -> 403 -> the rest); every function here checks it again first
thing, and re-checks it against a FRESH row after its file reads and before
anything is returned or created (:func:`_recheck_history_access`), so a
share revoked or downgraded while the request ran never leaks a version.
Restore's write re-checks under the per-doc write lock itself
(``replace_body_from_ui``).

A doc's versions are its current body (``doc.md`` + ``doc.meta.json``) and
the snapshots chat/docs/files.py takes before every body write
(``revisions/<YYYYMMDDTHHMMSSZ>-<n>.md`` + their ``.json`` sidecars, pruned
by age and count). This module reads them for the History view and turns
each into a **Version**::

    {"id":           "<ts>-<n>" (None for the current body),
     "written_at":   ISO UTC "...Z": when that body was written,
     "replaced_at":  ISO UTC "...Z" from the id's timestamp (None for current),
     "size":         bytes of that body,
     "source":       raw writer source -- the owner only, else None,
     "source_kind":  "conversation" | "action_request" | "ui" | "unknown",
     "source_label": who wrote it, phrased for THIS viewer,
     "conversation": {"id", "title", "project_id"} | None (the owner only)}

``source_label`` per viewer (:func:`_version_dict`): ``ui:<viewer id>``, or
the legacy plain ``ui`` seen by the owner, is "you"; ``ui:<other id>`` is
that user's name (else email; "a deleted user"); the legacy ``ui`` seen by
anyone else is "the owner"; a conversation is its title for the owner ("a
deleted conversation" when gone) and "a conversation" for anyone else;
``action_request:<id>`` (an approved ``write_doc`` card) is attributed to
the card's proposer (``action_request_store.get_action_request_owners``):
for the owner, "action request #<id>" when the owner proposed it (or the
card is gone) and "<proposer's name or email> (approved change)" when a
share recipient did ("a deleted user (approved change)" once that user is
gone); for anyone else, "you (approved change)" when the viewer proposed it
and "an approved change" otherwise; anything else is "unknown". Non-owners
never receive raw sources, conversation ids/titles or action request ids.

Editors' names ARE shown to every viewer, non-owners included (a
write-share recipient sees which other recipient restored or edited a
version): a deliberate decision, since names and emails of every user on
the install are already discoverable through ``/users/search``. Lookups are
batched: per response at most one ``action_request_store
.get_action_request_owners``, then one ``user_store.get_users_by_ids``
(UI writers and, for the owner, card proposers) and, for the owner, one
``conversation_store.get_conversations_meta`` plus one thread hop for all
conversation titles.

Metadata is best-effort: a missing or malformed record (or one describing a
body of another size, the same rule as ``files._snapshot_meta``) yields an
unknown source, ``written_at`` = the file's mtime and the real size.

Revision ids are untrusted URL input: they must fullmatch
``[0-9]{8}T[0-9]{6}Z-[0-9]+`` (ASCII digits) before any path is built, must
name a snapshot ``files.list_revisions`` currently lists, and are opened
``O_NOFOLLOW`` + ``O_NONBLOCK`` and re-checked ``S_ISREG`` on the
descriptor. Every failure is the one :class:`RevisionNotFound`. The listing
applies the same pattern, so it never shows an id that would 404 (planted
names with non-ASCII digits or a trailing newline are skipped). Listing and
reading run under the doc's file lock (``files.doc_lock``), so the current
body and the snapshots describe one moment.

Writes:

- :func:`restore_revision` writes the snapshot's body back through
  ``ui_writes.replace_body_from_ui`` (access + token re-checked under the
  per-doc write lock, the current body snapshotted first, source
  ``ui:<user_id>``); an identical body writes nothing.
- :func:`copy_revision` creates a NEW private user doc owned by the caller
  from a snapshot, with the assets the copied body references. The build
  runs shielded from cancellation (``service._shielded``): a client that
  disconnects mid-copy still gets a complete doc (announced by
  ``doc_list_changed``), never a half-built one.

Callers pass a ``doc`` the user may see in the UI and its ``access`` (the
routes get both from ``routes._get_doc_for_ui``, which also applies the
``public_projects`` gates; the fresh re-check repeats both: the access
matrix -- shares -- and the viewer's / owner's ``public_projects`` gate via
``ui_writes.public_doc_frozen_for``). Sync file IO runs in
``asyncio.to_thread``; :class:`DocFileError` and ``OSError`` from it
propagate unchanged for the route to map. Doc content is never logged.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from chat.docs import constants
from chat.docs import files as doc_files
from chat.docs import service
from chat.docs import ui_writes
from chat.docs.access import DocAccess
from chat.docs.constants import doc_not_found_message
from db import action_request_store, conversation_store, doc_store, user_store

logger = logging.getLogger(__name__)

__all__ = [
    "HISTORY_FORBIDDEN_MESSAGE",
    "REVISION_NOT_FOUND_MESSAGE",
    "SOURCE_KINDS",
    "RevisionNotFound",
    "copy_revision",
    "list_versions",
    "read_version",
    "referenced_asset_names",
    "require_history_access",
    "restore_revision",
]

SOURCE_KINDS = ("conversation", "action_request", "ui", "unknown")

REVISION_NOT_FOUND_MESSAGE = "Revision not found."
HISTORY_FORBIDDEN_MESSAGE = "History is available to people who can edit this doc."

# fullmatch only; [0-9] rather than \d, which also accepts non-ASCII digits.
_REVISION_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+")
_REVISION_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# A user id in ``ui:<id>`` / a card id in ``action_request:<id>``; longer
# runs are not ids SQLite could bind.
_INT_ID_RE = re.compile(r"[0-9]{1,18}")

# Default copy titles: how often to recompute the free "(copy N)" title
# after losing a race to a concurrent create of the same title.
_COPY_TITLE_ATTEMPTS = 5


class RevisionNotFound(service.DocRequestError):
    """``revision_not_found``: a malformed id, or no such snapshot (never
    existed, pruned, or not a regular file). One message for all of them."""

    def __init__(self) -> None:
        super().__init__("revision_not_found", REVISION_NOT_FOUND_MESSAGE)


def require_history_access(access: DocAccess) -> None:
    """``forbidden`` (:data:`HISTORY_FORBIDDEN_MESSAGE`) unless ``access``
    is the UI write verdict ``free`` (owner or write share)."""
    if access.write != "free":
        raise service.DocRequestError("forbidden", HISTORY_FORBIDDEN_MESSAGE)


async def _recheck_history_access(user: dict, doc_id: str) -> dict:
    """The doc's FRESH row, after re-running visibility and
    :func:`require_history_access` for ``user``.

    Called after a function's file reads and before it returns content or
    creates anything, so a share revoked (-> the not-found DocError) or
    downgraded to read (-> ``forbidden``) while the request ran wins.
    """
    fresh, access = await service.get_visible_doc(ui_writes.ui_caller(user), doc_id)
    if await ui_writes.public_doc_frozen_for(user, fresh):
        raise service.DocError(doc_not_found_message(doc_id))
    require_history_access(access)
    return fresh


# ---------------------------------------------------------------------------
# Sync layer (asyncio.to_thread only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawVersion:
    """A version before per-viewer labelling."""

    id: Optional[str]
    written_at: str
    replaced_at: Optional[str]
    size: int
    source: Optional[str]


def _iso_from_timestamp(ts: float) -> str:
    return doc_files._iso_z(datetime.fromtimestamp(ts, timezone.utc))


def _normalized_written_at(value) -> Optional[str]:
    """A metadata ``written_at`` re-rendered as ISO UTC ``...Z``; None when
    it is not a parseable timestamp (naive values are taken as UTC)."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return doc_files._iso_z(moment)


def _body_record(meta: Optional[dict], st: os.stat_result) -> tuple[str, Optional[str]]:
    """``(written_at, source)`` of a body file with stat ``st``.

    From ``meta`` (``doc.meta.json`` or a revision sidecar) when it is
    well-formed and describes a body of this size; otherwise the file's
    mtime and an unknown (None) source -- the rule ``files._snapshot_meta``
    applies when it copies the metadata into a sidecar.
    """
    if meta is not None and meta.get("size") == st.st_size:
        written_at = _normalized_written_at(meta.get("written_at"))
        if written_at is not None:
            return written_at, meta.get("source")
    return _iso_from_timestamp(st.st_mtime), None


def _replaced_at(rev_id: str) -> Optional[str]:
    """The snapshot time encoded in a revision id, as ISO UTC ``...Z``
    (None for an impossible date such as month 13)."""
    stamp = rev_id.split("-", 1)[0]
    try:
        moment = datetime.strptime(stamp, _REVISION_STAMP_FORMAT)
    except ValueError:
        return None
    return doc_files._iso_z(moment.replace(tzinfo=timezone.utc))


def _body_stat(doc_id: str) -> os.stat_result:
    """``lstat`` of ``doc.md``, which must be a regular file.

    :class:`DocFileError` for the cases ``files.read_body`` refuses (missing,
    a symlink, a FIFO or other special file), which the routes map to 400
    ``invalid_doc_files`` -- also before a restore, whose own body read
    inside ``ui_writes`` would surface them as a storage error.
    """
    paths = doc_files.doc_paths(doc_id)
    try:
        st = os.lstat(paths.body)
    except FileNotFoundError:
        raise doc_files.DocFileError("Doc body is missing.") from None
    if not stat.S_ISREG(st.st_mode):
        raise doc_files.DocFileError(f"Not a regular file: {doc_files.BODY_NAME}")
    return st


def _current_raw(doc_id: str) -> _RawVersion:
    """The current body's version: ``doc.meta.json`` or the fallback."""
    st = _body_stat(doc_id)
    written_at, source = _body_record(doc_files.read_doc_meta(doc_id), st)
    return _RawVersion(
        id=None, written_at=written_at, replaced_at=None, size=st.st_size, source=source,
    )


def _listed_revision_id(path: Path) -> Optional[str]:
    """The id of a ``files.list_revisions`` entry, or None when it is not
    one :func:`_revision_path` would accept (``files._REVISION_RE`` uses
    ``\\d`` and ``$``, so it also lists names with non-ASCII digits or a
    trailing newline)."""
    name = path.name
    if not name.endswith(".md"):
        return None
    rev_id = name[: -len(".md")]
    return rev_id if _REVISION_ID_RE.fullmatch(rev_id) else None


def _revision_raw(path: Path, rev_id: str, st: os.stat_result) -> _RawVersion:
    written_at, source = _body_record(doc_files.read_revision_meta(path), st)
    return _RawVersion(
        id=rev_id,
        written_at=written_at,
        replaced_at=_replaced_at(rev_id),
        size=st.st_size,
        source=source,
    )


def _list_versions_sync(doc_id: str) -> tuple[_RawVersion, list[_RawVersion]]:
    """``(current, revisions newest first)``.

    Under the doc's file lock, so the current body and the snapshot list
    describe the same moment (a write snapshots and replaces under it).
    Entries whose name is not a valid revision id, and anything that is not
    a regular file, are skipped.
    """
    with doc_files.doc_lock(doc_id):
        current = _current_raw(doc_id)
        revisions = []
        for path in reversed(doc_files.list_revisions(doc_id)):
            rev_id = _listed_revision_id(path)
            if rev_id is None:
                continue
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                revisions.append(_revision_raw(path, rev_id, st))
    return current, revisions


def _revision_path(doc_id: str, rev_id) -> Path:
    """The snapshot file named by ``rev_id``, or :class:`RevisionNotFound`.

    The id is checked against the pattern before any path is built, and the
    path must be one :func:`files.list_revisions` lists right now.
    """
    if not isinstance(rev_id, str) or not _REVISION_ID_RE.fullmatch(rev_id):
        raise RevisionNotFound()
    name = f"{rev_id}.md"
    for path in doc_files.list_revisions(doc_id):
        if path.name == name:
            return path
    raise RevisionNotFound()


def _read_regular_with_stat(path: Path) -> tuple[bytes, os.stat_result]:
    """``files._read_regular_bytes`` that also returns the descriptor's stat.

    Same guards: ``O_NOFOLLOW`` (a symlink leaf fails with ELOOP),
    ``O_NONBLOCK`` (a planted FIFO never blocks), ``S_ISREG`` on the open
    descriptor. Any failure is :class:`RevisionNotFound`.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ELOOP):
            logger.warning("[docs] could not open a revision snapshot", exc_info=True)
        raise RevisionNotFound() from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RevisionNotFound()
        with os.fdopen(fd, "rb") as f:
            fd = -1  # ownership passed to the file object
            return f.read(), st
    finally:
        if fd >= 0:
            os.close(fd)


_CURRENT_MODES = ("skip", "check", "read")


def _read_revision_sync(
    doc_id: str, rev_id, *, current: str = "skip",
) -> tuple[_RawVersion, str, Optional[str]]:
    """``(version, body text, current body text | None)`` of one snapshot
    (text decoded as UTF-8 with undecodable bytes replaced, like
    ``files.read_body``), under the doc's file lock.

    ``current``: ``"skip"`` leaves the current body alone; ``"check"``
    requires it to be a regular file (:func:`_body_stat`, before a
    restore); ``"read"`` also returns it (for the diff -- read in the same
    critical section as the snapshot). A missing revision wins over a broken
    current body.
    """
    if current not in _CURRENT_MODES:
        raise ValueError(f"current must be one of {_CURRENT_MODES}")
    with doc_files.doc_lock(doc_id):
        path = _revision_path(doc_id, rev_id)
        data, st = _read_regular_with_stat(path)
        raw = _revision_raw(path, rev_id, st)
        current_text = None
        if current == "check":
            _body_stat(doc_id)
        elif current == "read":
            current_text = doc_files.read_body(doc_id)
    return raw, data.decode("utf-8", errors="replace"), current_text


def referenced_asset_names(body: str) -> list[str]:
    """Sorted, deduped asset names the body references as ``assets/<name>``.

    Only names ``files._validate_asset_name`` accepts (one path segment, a
    raster image extension, no leading dot). Whether the file exists is not
    checked here.
    """
    # The one reference matcher (ui_writes.referenced_asset_names), so the
    # same decoded spellings the asset-delete in-use rule and the read-only
    # image filter accept are copied too.
    names = []
    for name in ui_writes.referenced_asset_names(body):
        try:
            doc_files._validate_asset_name(name)
        except doc_files.DocFileError:
            continue
        names.append(name)
    return sorted(names)


def _copy_assets_sync(source_doc_id: str, target_doc_id: str, body: str) -> int:
    """Copy the assets ``body`` references (:func:`referenced_asset_names`)
    from one doc into another's ``assets/``, keeping each name byte for
    byte. Returns how many were copied.

    Skipped (not an error): a name the source does not hold as a regular,
    non-symlink file directly in ``assets/`` (``files.asset_path`` +
    the no-follow regular-file read), a file that is not a sniffable raster
    image or is over ``DOC_MAX_IMAGE_SIZE``, and anything past
    ``DOC_MAX_ASSETS`` / ``DOC_MAX_ASSETS_TOTAL_BYTES``. Targets are created
    exclusively without following links (``files._write_new_file``).

    Holds the TARGET doc's file lock throughout (the source is only read:
    its assets are hard-linked into place whole, never written in place), so
    the caller's cleanup (``files.delete_doc_dir``, which takes the same
    lock) cannot remove the directory while this thread is still writing
    into it. An ``OSError`` writing the target propagates and the caller
    removes the whole directory.
    """
    target_assets = doc_files.doc_paths(target_doc_id).assets
    count = total = 0
    names = referenced_asset_names(body)
    if not names:
        return 0
    with doc_files.doc_lock(target_doc_id):
        for name in names:
            if count >= constants.DOC_MAX_ASSETS:
                break
            try:
                path = doc_files.asset_path(source_doc_id, name)
                if os.lstat(path).st_size > constants.DOC_MAX_IMAGE_SIZE:
                    continue
                data = doc_files._read_regular_bytes(
                    path, missing_message=f"Image not found: {name}",
                )
            except (doc_files.DocFileError, OSError):
                continue
            size = len(data)
            if size > constants.DOC_MAX_IMAGE_SIZE or doc_files.sniff_image_type(data) is None:
                continue
            if total + size > constants.DOC_MAX_ASSETS_TOTAL_BYTES:
                continue
            doc_files._write_new_file(target_assets / name, data)
            count += 1
            total += size
    return count


# ---------------------------------------------------------------------------
# Per-viewer labelling
# ---------------------------------------------------------------------------


def _classify(source) -> tuple[str, Optional[str]]:
    """``(source_kind, reference)`` of a raw write source.

    ``ui`` -> ``("ui", None)`` (legacy: the owner); ``ui:<1-18 digits>`` ->
    ``("ui", "<digits>")``; ``conversation:<id>`` -> ``("conversation",
    "<id>")``; ``action_request:<id>`` / bare ``action_request`` ->
    ``("action_request", "<id>" | None)``; anything else ``("unknown", None)``.
    """
    if not isinstance(source, str):
        return "unknown", None
    if source == "ui":
        return "ui", None
    if source == "action_request":
        return "action_request", None
    prefix, sep, rest = source.partition(":")
    if sep and rest:
        if prefix == "ui" and _INT_ID_RE.fullmatch(rest):
            return "ui", rest
        if prefix == "conversation":
            return "conversation", rest
        if prefix == "action_request":
            return "action_request", rest
    return "unknown", None


def _user_label(found: Optional[dict]) -> str:
    if not found:
        return "a deleted user"
    name = (found.get("name") or "").strip()
    return name or (found.get("email") or "").strip() or "a deleted user"


def _resolve_titles(metas: dict[str, dict]) -> dict[str, str]:
    """Sidebar titles for conversation rows (may read a legacy chat file)."""
    from chat.storage import ChatStorage

    titles = {}
    for conversation_id, meta in metas.items():
        try:
            titles[conversation_id] = ChatStorage._resolve_list_title(conversation_id, meta)
        except (OSError, ValueError):
            titles[conversation_id] = "New Chat"
    return titles


async def _conversation_refs(owner_id: int, conversation_ids: set[str]) -> dict[str, dict]:
    """``{id: {"id", "title", "project_id"}}`` for the owner's conversations
    among ``conversation_ids``: one batched ``get_conversations_meta`` and
    one thread hop for every title (missing or someone else's -> absent,
    shown as "a deleted conversation")."""
    metas = await conversation_store.get_conversations_meta(owner_id, conversation_ids)
    if not metas:
        return {}
    titles = await asyncio.to_thread(_resolve_titles, metas)
    return {
        conversation_id: {
            "id": conversation_id,
            "title": titles[conversation_id],
            "project_id": meta.get("project_id"),
        }
        for conversation_id, meta in metas.items()
    }


def _card_id(ref: Optional[str]) -> Optional[int]:
    """The integer id of an ``action_request:<id>`` reference, else None."""
    return int(ref) if ref and _INT_ID_RE.fullmatch(ref) else None


def _action_request_label(
    ref: Optional[str],
    *,
    viewer_id: int,
    is_owner: bool,
    users: dict[int, dict],
    card_owners: dict[int, int],
) -> str:
    """``source_label`` of an approved card's write (see the module doc)."""
    card_id = _card_id(ref)
    proposer = card_owners.get(card_id) if card_id is not None else None
    if not is_owner:
        if proposer is not None and proposer == viewer_id:
            return "you (approved change)"
        return "an approved change"
    if proposer is not None and proposer != viewer_id:
        return f"{_user_label(users.get(proposer))} (approved change)"
    return f"action request #{ref}" if ref else "an action request"


def _version_dict(
    raw: _RawVersion,
    kind: str,
    ref: Optional[str],
    *,
    viewer_id: int,
    is_owner: bool,
    users: dict[int, dict],
    conversations: dict[str, dict],
    card_owners: dict[int, int],
) -> dict:
    conversation = None
    if kind == "ui":
        if ref is None:
            label = "you" if is_owner else "the owner"
        elif int(ref) == viewer_id:
            label = "you"
        else:
            label = _user_label(users.get(int(ref)))
    elif kind == "conversation":
        if is_owner:
            found = conversations.get(ref)
            conversation = dict(found) if found else None
            label = found["title"] if found else "a deleted conversation"
        else:
            label = "a conversation"
    elif kind == "action_request":
        label = _action_request_label(
            ref, viewer_id=viewer_id, is_owner=is_owner,
            users=users, card_owners=card_owners,
        )
    else:
        label = "unknown"
    return {
        "id": raw.id,
        "written_at": raw.written_at,
        "replaced_at": raw.replaced_at,
        "size": raw.size,
        "source": raw.source if is_owner else None,
        "source_kind": kind,
        "source_label": label,
        "conversation": conversation,
    }


async def _versions_for_viewer(user: dict, doc: dict, raws: list[_RawVersion]) -> list[dict]:
    """Label ``raws`` for ``user``, with at most one lookup of each kind:
    card proposers (when some version came from an approved card), users
    (UI writers other than the viewer and, for the owner, proposers other
    than the owner), and -- for the owner only -- conversations."""
    viewer_id = user["id"]
    is_owner = doc["owner_id"] == viewer_id
    classified = [(raw, *_classify(raw.source)) for raw in raws]

    card_ids = {
        card_id for _raw, kind, ref in classified
        if kind == "action_request" and (card_id := _card_id(ref)) is not None
    }
    card_owners = (
        await action_request_store.get_action_request_owners(card_ids)
        if card_ids else {}
    )

    user_ids = {
        int(ref) for _raw, kind, ref in classified
        if kind == "ui" and ref is not None and int(ref) != viewer_id
    }
    if is_owner:
        user_ids |= {
            proposer for proposer in card_owners.values() if proposer != viewer_id
        }
    users = await user_store.get_users_by_ids(user_ids) if user_ids else {}

    conversations: dict[str, dict] = {}
    if is_owner:
        conversation_ids = {
            ref for _raw, kind, ref in classified if kind == "conversation"
        }
        if conversation_ids:
            conversations = await _conversation_refs(viewer_id, conversation_ids)

    return [
        _version_dict(
            raw, kind, ref,
            viewer_id=viewer_id, is_owner=is_owner,
            users=users, conversations=conversations, card_owners=card_owners,
        )
        for raw, kind, ref in classified
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_versions(user: dict, doc: dict, access: DocAccess) -> dict:
    """``{"current": Version, "revisions": [Version, ...]}``.

    Revisions newest first (timestamp, then sequence). The current body's
    record comes from ``doc.meta.json`` (fallback: ``doc.md``'s mtime, an
    unknown source, its real size). Editors only (owner or write share).

    Raises:
        DocDisabled: gate closed.
        DocRequestError: ``forbidden`` for a read-only viewer (also when the
            share was downgraded while the request ran).
        DocError: the doc became invisible while the request ran.
        DocFileError: ``doc.md`` missing, a symlink, or not a regular file.
    """
    service.require_enabled(ui_writes.ui_caller(user))
    require_history_access(access)
    current, revisions = await asyncio.to_thread(_list_versions_sync, doc["id"])
    fresh = await _recheck_history_access(user, doc["id"])
    labelled = await _versions_for_viewer(user, fresh, [current, *revisions])
    return {"current": labelled[0], "revisions": labelled[1:]}


async def read_version(
    user: dict, doc: dict, access: DocAccess, rev_id, *, diff_current: bool = False,
) -> dict:
    """One snapshot for an editor (owner or write share): its Version plus
    ``content``.

    With ``diff_current``, also ``diff`` =
    ``build_bounded_content_diff(old=<snapshot>, new=<current body>)`` --
    "changes since this version" (computed in a thread: up to two 1 MB
    bodies).

    The snapshot (and, for the diff, the current body) are read under the
    doc's file lock, so the diff compares two bodies of the same moment.

    Raises:
        DocDisabled: gate closed.
        DocRequestError: ``forbidden`` for a read-only viewer (also when the
            share was downgraded while the request ran).
        RevisionNotFound: malformed or missing id.
        DocError: the doc became invisible while the request ran.
        DocFileError: the current body is unreadable (diff only).
    """
    service.require_enabled(ui_writes.ui_caller(user))
    require_history_access(access)
    raw, content, current = await asyncio.to_thread(
        _read_revision_sync, doc["id"], rev_id,
        current="read" if diff_current else "skip",
    )
    fresh = await _recheck_history_access(user, doc["id"])
    (version,) = await _versions_for_viewer(user, fresh, [raw])
    version["content"] = content
    if diff_current:
        from chat.action_request_types._skill_content_edit import (
            build_bounded_content_diff,
        )

        version["diff"] = await asyncio.to_thread(
            build_bounded_content_diff, content, current,
        )
    return version


async def restore_revision(
    user: dict,
    doc: dict,
    access: DocAccess,
    rev_id,
    *,
    expected_updated_at,
) -> tuple[dict, str, bool]:
    """Write a snapshot's body back as the doc's current body.

    Checked in order: :func:`require_history_access` (``access.write ==
    "free"``: owner or write share, else ``forbidden``), the token (a non-empty
    string, else ``invalid_request``), the snapshot (else
    :class:`RevisionNotFound`), and -- in the same locked read -- that
    ``doc.md`` is a regular file (else :class:`DocFileError`, which the
    route maps to 400 ``invalid_doc_files`` like the list and read routes;
    ``replace_body_from_ui`` would surface a broken body as a generic
    storage error). The write itself is
    ``ui_writes.replace_body_from_ui``: access and ``expected_updated_at``
    re-checked against a fresh row under the per-doc write lock, the current
    body snapshotted into ``revisions/``, the old one written with source
    ``ui:<user_id>``, DB bump + realtime events. A snapshot identical to the
    current body writes nothing (``changed=False``).

    Returns:
        ``(doc, body, changed)`` as ``replace_body_from_ui``.

    Raises:
        DocDisabled; DocRequestError (``forbidden`` / ``invalid_request`` /
        ``revision_not_found`` / ``content_too_large``); DocFileError
        (broken ``doc.md``); doc_store.StaleDocError (carries the current
        row); DocError (the doc vanished or its storage failed).
    """
    service.require_enabled(ui_writes.ui_caller(user))
    require_history_access(access)
    if not isinstance(expected_updated_at, str) or not expected_updated_at:
        raise service.DocRequestError("invalid_request", "expected_updated_at is required.")
    _raw, content, _current = await asyncio.to_thread(
        _read_revision_sync, doc["id"], rev_id, current="check",
    )
    return await ui_writes.replace_body_from_ui(
        user, doc["id"], content, expected_updated_at=expected_updated_at,
    )


def _copy_title_candidate(base: str, n: int) -> str:
    """``"<base> (copy)"`` (n=1) or ``"<base> (copy <n>)"``, the base cut so
    the whole title stays within ``DOC_TITLE_MAX_LEN``."""
    suffix = " (copy)" if n == 1 else f" (copy {n})"
    stem = base[: max(0, constants.DOC_TITLE_MAX_LEN - len(suffix))].rstrip()
    return f"{stem}{suffix}"


async def _taken_user_doc_titles(user_id: int) -> set[str]:
    """Casefolded titles of the user's own user docs (the namespace a new
    private user doc's title must be unique in)."""
    docs = await doc_store.list_accessible_docs(
        user_id, include_user_docs=True, include_project_docs=False, owned_only=True,
    )
    return {
        (d.get("title") or "").strip().casefold()
        for d in docs
        if d.get("owner_id") == user_id and d.get("project_id") is None
    }


async def _insert_copy_row(
    user: dict,
    source_doc: dict,
    new_id: str,
    *,
    size: int,
    write_source: str,
    explicit_title: Optional[str],
) -> dict:
    """Insert the copy's row; the store's title check is the authority.

    An explicit title is tried once (``DuplicateDocTitleError`` propagates).
    The default title is the first free ``(copy)`` / ``(copy N)`` against
    the caller's current user-doc titles, recomputed if a concurrent create
    takes it first.
    """
    user_id = user["id"]

    async def _insert(title: str) -> dict:
        return await doc_store.create_doc(
            user_id,
            title,
            source_doc.get("description") or "",
            mode="private",
            project_id=None,
            content_size=size,
            last_write_source=write_source,
            doc_id=new_id,
        )

    if explicit_title is not None:
        return await _insert(explicit_title)
    for attempt in range(_COPY_TITLE_ATTEMPTS):
        taken = await _taken_user_doc_titles(user_id)
        n = 1
        while _copy_title_candidate(source_doc["title"], n).casefold() in taken:
            n += 1
        try:
            return await _insert(_copy_title_candidate(source_doc["title"], n))
        except doc_store.DuplicateDocTitleError:
            if attempt == _COPY_TITLE_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


async def copy_revision(
    user: dict, doc: dict, access: DocAccess, rev_id, *, title=None,
) -> dict:
    """Copy a snapshot into a NEW private user doc owned by ``user``.

    Editors only (owner or write share; a read-only viewer gets
    ``forbidden``, re-checked against a fresh row after the snapshot is
    read and before anything is created), from any doc they can see -- a
    project doc or a public-project doc included: the copy is always a
    private user doc of the caller (a recipient cannot write into the
    owner's project; public -> private is taint-safe).

    - Title: ``title`` when given (validated: 400 ``invalid_title``; taken
      in the caller's user docs: 409 ``duplicate_title``); else
      ``"<title> (copy)"``, then ``"(copy 2)"``, ... (the base cut to keep
      the title within ``DOC_TITLE_MAX_LEN``). The description is copied.
    - Body: the snapshot's body, written with source ``ui:<user_id>``
      (``doc.meta.json`` and ``last_write_source``); no revisions.
    - Assets: every asset the copied body references (``assets/<name>``,
      :func:`referenced_asset_names`) that the source still holds as a
      regular file is copied under the SAME name (:func:`_copy_assets_sync`:
      symlinks never followed, non-images and over-cap files skipped);
      unreferenced and missing ones are skipped.

    Same order and cleanup as ``service.create_doc_from_ui``: lay out the
    directory under a fresh id (body + assets), insert the row (then record
    the asset count), and on any failure remove the row and the directory.
    That build is shielded from cancellation (``service._shielded``): a
    cancelled request still completes the copy. Publishes
    ``doc_list_changed`` to the caller (the only one who can see the new
    doc).

    Returns:
        The store's doc dict of the new doc (with ``shares: []``).

    Raises:
        DocDisabled; DocRequestError (``forbidden`` / ``invalid_title`` /
        ``revision_not_found`` / ``content_too_large`` / ``duplicate_title``
        / ``invalid_request``); DocError (the source became invisible while
        the request ran, or a storage failure creating the copy).
    """
    service.require_enabled(ui_writes.ui_caller(user))
    require_history_access(access)
    explicit_title = None
    if title is not None:
        explicit_title, _ = service.validate_doc_metadata(title, None)
    _raw, content, _current = await asyncio.to_thread(
        _read_revision_sync, doc["id"], rev_id,
    )
    doc = await _recheck_history_access(user, doc["id"])
    size_bytes = len(content.encode("utf-8"))
    if size_bytes > constants.DOC_MAX_CONTENT_SIZE:
        raise service.DocRequestError(
            "content_too_large",
            f"This version is {size_bytes} bytes, over the "
            f"{constants.DOC_MAX_CONTENT_SIZE}-byte limit.",
        )
    if explicit_title is not None and (
        explicit_title.casefold() in await _taken_user_doc_titles(user["id"])
    ):
        # Fail fast, before any file is written (the insert re-checks).
        raise service.DocRequestError(
            "duplicate_title", f"A doc titled '{explicit_title}' already exists.",
        )

    # Everything above only reads; from here on files and a row are
    # created, so the build runs to completion (or to its own cleanup) even
    # if the request is cancelled -- see _build_copy.
    return await service._shielded(_build_copy(
        user, doc, content, explicit_title=explicit_title,
    ))


async def _build_copy(
    user: dict, source_doc: dict, content: str, *, explicit_title: Optional[str],
) -> dict:
    """Lay out, insert and announce the copy (see :func:`copy_revision`).

    Run shielded: a cancel arriving mid-build cannot leave a half-built doc
    (directory without row, row without assets) behind. On failure the
    cleanup always deletes the row first (a no-op when the insert never
    landed -- also covers an insert that committed but raised afterwards),
    then the directory (``files.delete_doc_dir`` takes the doc's file lock,
    so it waits for a still-running ``_copy_assets_sync``).
    """
    write_source = ui_writes.ui_write_source(user)
    new_id = str(uuid.uuid4())
    try:
        try:
            size = await asyncio.to_thread(
                doc_files.init_doc, new_id, content, write_source=write_source,
            )
            asset_count = await asyncio.to_thread(
                _copy_assets_sync, source_doc["id"], new_id, content,
            )
        except (doc_files.DocFileError, OSError) as exc:
            logger.warning("[docs] laying out a revision copy failed", exc_info=True)
            message = str(exc) if isinstance(exc, doc_files.DocFileError) else (
                f"Doc storage error: {exc.strerror or exc.__class__.__name__}."
            )
            raise service.DocError(message) from None
        new_doc = await _insert_copy_row(
            user, source_doc, new_id,
            size=size, write_source=write_source, explicit_title=explicit_title,
        )
        if asset_count:
            new_doc = await doc_store.update_after_write(
                new_id,
                content_size=size,
                asset_count=asset_count,
                last_write_source=write_source,
            )
            if new_doc is None:
                raise service.DocError(doc_not_found_message(new_id))
    except BaseException as exc:
        try:
            await doc_store.delete_doc(new_id)
        except Exception:
            logger.warning("[docs] cleanup of row %s failed", new_id, exc_info=True)
        try:
            await asyncio.to_thread(doc_files.delete_doc_dir, new_id)
        except Exception:
            logger.warning("[docs] cleanup of %s failed", new_id, exc_info=True)
        if isinstance(exc, doc_store.DuplicateDocTitleError):
            raise service.DocRequestError("duplicate_title", str(exc)) from None
        if isinstance(exc, doc_store.DocValidationError):
            raise service.DocRequestError("invalid_request", str(exc)) from None
        raise

    service._events().publish_doc_list_changed(user["id"])
    return new_doc
