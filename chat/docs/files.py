"""On-disk layer for Quest Docs.

Layout (spec section 4.1)::

    <DOCS_DIR>/<doc_id>/
      doc.md                       the current body (UTF-8, LF)
      assets/<name>.<ext>          raster images only: png, jpg, gif, webp
      revisions/<YYYYMMDDTHHMMSSZ>-<n>.md
                                   snapshot of doc.md taken before each write
      revisions/<YYYYMMDDTHHMMSSZ>-<n>.json
                                   its metadata sidecar: {"written_at",
                                   "source", "size"} (see _snapshot_body)

Rules every function here keeps:

- Paths come ONLY from ``ChatStorage.get_doc_dir(doc_id)`` (canonical id +
  containment check); nothing here joins ``DOCS_DIR`` with an id.
- Leaves are opened with ``O_NOFOLLOW`` and re-checked as regular files on
  the descriptor (``fstat`` + ``S_ISREG``), reads also ``O_NONBLOCK`` so a
  planted FIFO can never block (same guards as chat/file_storage.py).
- Writes are atomic (temp file + ``os.replace`` / ``os.link``) and every
  mutation runs under a per-doc ``threading.Lock`` (:func:`doc_lock`).

Everything is synchronous; async callers wrap calls in
``asyncio.to_thread``. Failures the caller should surface are raised as
:class:`DocFileError` (a ``ValueError``) with a model/user-facing message.
"""

import errno
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import weakref
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from chat import storage as _storage
from chat.docs import constants

logger = logging.getLogger(__name__)

BODY_NAME = "doc.md"
ASSETS_DIR_NAME = "assets"
REVISIONS_DIR_NAME = "revisions"

_BODY_TMP_NAME = BODY_NAME + ".tmp"
_REVISION_RE = re.compile(r"^(\d{8}T\d{6}Z)-(\d+)\.md$")
_REVISION_META_RE = re.compile(r"^(\d{8}T\d{6}Z)-(\d+)\.json$")
# Either half of a snapshot pair; name allocation skips both.
_REVISION_ENTRY_RE = re.compile(r"^(\d{8}T\d{6}Z)-(\d+)\.(?:md|json)$")
_ASSET_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ASSET_STEM_MAX_LEN = 64


class DocFileError(ValueError):
    """A doc file operation was refused or failed (caps, bad name, symlink,
    special file, missing body). The message is safe to show the model/user."""


@dataclass(frozen=True)
class DocPaths:
    """Resolved paths of one doc's directory."""

    root: Path
    body: Path
    assets: Path
    revisions: Path


@dataclass(frozen=True)
class AssetInfo:
    """Result of :func:`add_asset`: the stored name and the doc's new totals."""

    name: str
    size: int
    asset_count: int
    total_bytes: int


def doc_paths(doc_id: str) -> DocPaths:
    """Return the paths for ``doc_id`` (validated via ``ChatStorage.get_doc_dir``).

    Raises:
        chat.storage.InvalidStorageIdError: non-canonical / escaping id.
    """
    # Looked up through the module at call time: some test suites reload
    # chat.storage, and the resolver must see the live DOCS_DIR.
    root = _storage.ChatStorage.get_doc_dir(doc_id)
    return DocPaths(
        root=root,
        body=root / BODY_NAME,
        assets=root / ASSETS_DIR_NAME,
        revisions=root / REVISIONS_DIR_NAME,
    )


# ---------------------------------------------------------------------------
# Per-doc lock
# ---------------------------------------------------------------------------

# Weak values: a lock lives while someone holds or waits on it (they keep a
# strong reference), so the table does not grow with every doc ever touched.
_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_locks_guard = threading.Lock()


def _get_lock(doc_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(doc_id)
        if lock is None:
            lock = threading.Lock()
            _locks[doc_id] = lock
        return lock


@contextmanager
def doc_lock(doc_id: str) -> Iterator[None]:
    """Serialize writers of one doc within this process.

    One ``threading.Lock`` per doc id (created on first use), so writes to
    different docs never wait on each other. Not re-entrant: the public
    mutating functions below each take it once and use unlocked helpers
    internally.
    """
    lock = _get_lock(doc_id)
    with lock:
        yield


# ---------------------------------------------------------------------------
# Low-level safe I/O
# ---------------------------------------------------------------------------


def _no_follow_opener(path: str, flags: int) -> int:
    """``open()`` opener that refuses a symlink leaf (ELOOP) -- mirrors
    chat/file_storage.py ``_no_follow_opener``."""
    return os.open(path, flags | os.O_NOFOLLOW)


def _ensure_real_dir(path: Path) -> None:
    """Create ``path`` if missing, then require it to be a real directory
    (not a symlink to one)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        st = os.lstat(path)
    except OSError as exc:
        # A dangling symlink at the name raises FileExistsError from mkdir.
        raise DocFileError(
            f"Doc storage path is unusable: {path.name} ({exc.strerror})"
        ) from None
    if not stat.S_ISDIR(st.st_mode):
        raise DocFileError(f"Doc storage path is not a directory: {path.name}")


def _create_root_exclusive(root: Path) -> None:
    """Create a NEW doc root; an existing entry of any kind is an error.

    A fresh doc must never inherit another directory's ``assets/`` or
    ``revisions/`` (e.g. an orphan left by an earlier failed create).
    """
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(root)
    except FileExistsError:
        raise DocFileError("Doc storage already exists.") from None
    except OSError as exc:
        raise DocFileError(f"Cannot create doc storage: {exc.strerror}") from None


def _ensure_layout(paths: DocPaths, *, create_root: bool) -> None:
    """Make sure the doc root and its ``assets/`` / ``revisions/`` exist.

    Only :func:`init_doc` creates the root; every later write requires it,
    so a write racing a delete fails instead of resurrecting an orphan
    directory for a doc whose row is gone.
    """
    if create_root:
        _create_root_exclusive(paths.root)
    else:
        try:
            st = os.lstat(paths.root)
        except FileNotFoundError:
            raise DocFileError("Doc storage is missing.") from None
        if not stat.S_ISDIR(st.st_mode):
            raise DocFileError("Doc storage path is not a directory.")
    _ensure_real_dir(paths.assets)
    _ensure_real_dir(paths.revisions)


def _read_regular_bytes(path: Path, *, missing_message: str) -> bytes:
    """Read a regular, non-symlink file without ever blocking on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise DocFileError(missing_message) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DocFileError(f"Refusing to read a symbolic link: {path.name}") from None
        raise DocFileError(f"Cannot read {path.name}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DocFileError(f"Not a regular file: {path.name}")
        with os.fdopen(fd, "rb") as f:
            fd = -1  # ownership passed to the file object
            return f.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _write_new_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively (never through a symlink) and write ``data``.

    Unlinks a partial file on failure.
    """
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise


def _normalize_body(content) -> str:
    if not isinstance(content, str):
        raise DocFileError("Doc content must be a string.")
    return content.replace("\r\n", "\n")


def _encode_body(content: str) -> bytes:
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise DocFileError(
            "Doc content contains characters that cannot be encoded as UTF-8 "
            "(lone surrogates)."
        ) from None
    if len(data) > constants.DOC_MAX_CONTENT_SIZE:
        raise DocFileError(
            f"Doc content is {len(data)} bytes, over the "
            f"{constants.DOC_MAX_CONTENT_SIZE}-byte limit."
        )
    return data


def _atomic_write_body(paths: DocPaths, data: bytes) -> None:
    """Write ``doc.md.tmp`` then ``os.replace`` it onto ``doc.md``.

    A stale temp file (or a symlink planted at its name) is unlinked first;
    ``os.unlink`` never follows a link, and the create is exclusive +
    no-follow. ``os.replace`` swaps the directory entry, so a symlinked
    ``doc.md`` is replaced, never written through.
    """
    tmp = paths.root / _BODY_TMP_NAME
    tmp.unlink(missing_ok=True)
    _write_new_file(tmp, data)
    try:
        os.replace(tmp, paths.body)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Body
# ---------------------------------------------------------------------------


def init_doc(doc_id: str, content: str) -> int:
    """Lay out a new doc directory and write its first body.

    Creates ``<doc>/``, ``assets/`` and ``revisions/``, writes ``doc.md``
    atomically (CRLF normalized to LF, otherwise verbatim) and takes no
    revision snapshot.

    Returns:
        Byte size of the written ``doc.md``.

    Raises:
        DocFileError: content over ``DOC_MAX_CONTENT_SIZE`` or a bad layout.
    """
    data = _encode_body(_normalize_body(content))
    paths = doc_paths(doc_id)
    with doc_lock(doc_id):
        _ensure_layout(paths, create_root=True)
        _atomic_write_body(paths, data)
    return len(data)


def read_body(doc_id: str) -> str:
    """Return ``doc.md`` as text (UTF-8, undecodable bytes replaced).

    Raises:
        DocFileError: missing body, symlinked or non-regular ``doc.md``.
    """
    paths = doc_paths(doc_id)
    data = _read_regular_bytes(paths.body, missing_message="Doc body is missing.")
    return data.decode("utf-8", errors="replace")


def _next_revision_path(revisions_dir: Path, now: datetime) -> Path:
    # Counts .json names too, so a new snapshot never lands beside an
    # orphan sidecar of an older one (orphans are pruned after the write).
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    highest = 0
    for name in os.listdir(revisions_dir):
        m = _REVISION_ENTRY_RE.match(name)
        if m and m.group(1) == stamp:
            highest = max(highest, int(m.group(2)))
    return revisions_dir / f"{stamp}-{highest + 1}.md"


def _revision_meta_path(revision: Path) -> Path:
    """``revisions/<ts>-<n>.json`` for ``revisions/<ts>-<n>.md``."""
    return revision.with_suffix(".json")


def _write_revision_meta(
    revision: Path, *, now: datetime, source: Optional[str], size: int,
) -> None:
    """Write the sidecar of a just-written snapshot (exclusive create).

    Best-effort: a failure is logged and the snapshot stays without
    metadata (readers tolerate a missing sidecar), so the body write it
    precedes still goes ahead.
    """
    meta = {
        "written_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": None if source is None else str(source),
        "size": size,
    }
    try:
        _write_new_file(
            _revision_meta_path(revision),
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )
    except OSError:
        logger.warning(
            "[docs] could not write revision metadata for %s",
            revision.name, exc_info=True,
        )


def _snapshot_body(
    paths: DocPaths,
    data: Optional[bytes] = None,
    *,
    source: Optional[str] = None,
) -> Optional[Path]:
    """Copy the current ``doc.md`` into ``revisions/``; None when no body yet.

    ``data`` is the current body when the caller already read it. A
    symlinked or special ``doc.md`` raises (never snapshotted through).

    Beside the ``<ts>-<n>.md`` snapshot it writes ``<ts>-<n>.json``:
    ``{"written_at": <ISO UTC with Z, when the snapshot was taken>,
    "source": <source>, "size": <bytes of the snapshot>}``. ``source`` is
    the ``last_write_source`` of the body being snapshotted (None when
    unknown). The ``.md`` is written first, so a crash leaves at worst a
    snapshot without a sidecar, never a sidecar without its snapshot.
    """
    if data is None:
        if not os.path.lexists(paths.body):
            return None
        data = _read_regular_bytes(paths.body, missing_message="Doc body is missing.")
    now = datetime.now(timezone.utc)
    for _ in range(100):
        target = _next_revision_path(paths.revisions, now)
        try:
            _write_new_file(target, data)
        except FileExistsError:
            continue
        _write_revision_meta(target, now=now, source=source, size=len(data))
        return target
    raise DocFileError("Could not allocate a revision snapshot name.")


def _revision_sort_key(path: Path) -> tuple[str, int]:
    m = _REVISION_RE.match(path.name)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def _list_revision_files(revisions_dir: Path) -> list[Path]:
    if not revisions_dir.is_dir() or revisions_dir.is_symlink():
        return []
    found = []
    for name in os.listdir(revisions_dir):
        if not _REVISION_RE.match(name):
            continue
        path = revisions_dir / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            found.append(path)
    found.sort(key=_revision_sort_key)
    return found


def _unlink_revision_meta(path: Path) -> None:
    """Remove one sidecar entry (never follows a link; a directory planted
    at the name is left alone)."""
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            return
        path.unlink()
    except FileNotFoundError:
        pass


def _prune_orphan_revision_meta(revisions_dir: Path) -> None:
    """Delete every ``<ts>-<n>.json`` whose ``<ts>-<n>.md`` is gone."""
    if not revisions_dir.is_dir() or revisions_dir.is_symlink():
        return
    for name in os.listdir(revisions_dir):
        if not _REVISION_META_RE.match(name):
            continue
        meta = revisions_dir / name
        if not os.path.lexists(meta.with_suffix(".md")):
            _unlink_revision_meta(meta)


def _prune_revisions(revisions_dir: Path) -> None:
    """Delete snapshots older than the retention window, and the oldest
    ones beyond ``DOC_REVISION_MAX_COUNT``.

    The newest snapshot (by its timestamp/sequence name) is always kept,
    however old, so an idle doc keeps one restore point. The count cap
    bounds the disk a looping writer can consume inside the window (each
    snapshot is up to ``DOC_MAX_CONTENT_SIZE``).

    Both rules count and date the ``.md`` snapshots only; a snapshot's
    ``.json`` sidecar is deleted with it, and any sidecar left without its
    ``.md`` (an interrupted prune, a hand-deleted snapshot) is deleted too.
    """
    revisions = _list_revision_files(revisions_dir)
    if len(revisions) > 1:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=constants.DOC_REVISION_RETENTION_DAYS)
        ).timestamp()
        excess = max(0, len(revisions) - constants.DOC_REVISION_MAX_COUNT)
        for index, path in enumerate(revisions[:-1]):
            try:
                if not (index < excess or os.lstat(path).st_mtime < cutoff):
                    continue
                path.unlink()
            except FileNotFoundError:
                pass
            _unlink_revision_meta(_revision_meta_path(path))
    _prune_orphan_revision_meta(revisions_dir)


def write_body(
    doc_id: str,
    content: str,
    *,
    snapshot: bool = True,
    snapshot_source: Optional[str] = None,
) -> int:
    """Replace ``doc.md`` with ``content`` (CRLF -> LF), atomically.

    Under the doc lock: snapshot the current body into ``revisions/`` (when
    ``snapshot`` and a body exists), prune old revisions, then write
    ``doc.md.tmp`` and ``os.replace`` it onto ``doc.md``. The size cap is
    checked before anything is touched. ``snapshot_source`` is the
    ``last_write_source`` of the body being replaced, recorded in the
    snapshot's ``.json`` sidecar.

    Returns:
        Byte size of the new ``doc.md``.

    Raises:
        DocFileError: content over ``DOC_MAX_CONTENT_SIZE``, symlinked or
            special ``doc.md``, a missing doc directory (only
            :func:`init_doc` creates it), or a bad layout.
    """
    data = _encode_body(_normalize_body(content))
    paths = doc_paths(doc_id)
    with doc_lock(doc_id):
        _write_body_locked(paths, data, snapshot=snapshot, source=snapshot_source)
    return len(data)


def _write_body_locked(
    paths: DocPaths,
    data: bytes,
    *,
    snapshot: bool,
    source: Optional[str] = None,
) -> None:
    """Snapshot + prune + atomic write; caller holds the doc lock.

    The snapshot (and its sidecar) is skipped when the new body is
    byte-identical to the current one (nothing to restore to).
    """
    _ensure_layout(paths, create_root=False)
    if snapshot:
        current = _current_body_bytes(paths)
        if current is not None and current != data:
            _snapshot_body(paths, current, source=source)
    _prune_revisions(paths.revisions)
    _atomic_write_body(paths, data)


def _current_body_bytes(paths: DocPaths) -> Optional[bytes]:
    if not os.path.lexists(paths.body):
        return None
    return _read_regular_bytes(paths.body, missing_message="Doc body is missing.")


def modify_body(
    doc_id: str,
    fn: Callable[[str], str],
    *,
    snapshot: bool = True,
    snapshot_source: Optional[str] = None,
) -> tuple[str, int]:
    """Read-modify-write ``doc.md`` in ONE critical section.

    Under the doc lock: read the current body, call ``fn(current) ->
    new_body``, then snapshot/prune/write exactly like :func:`write_body`
    (``snapshot_source`` as there). This is the primitive every edit/append
    uses -- a separate ``read_body`` + ``write_body`` pair would let
    concurrent writers clobber each other (the lock is not re-entrant, so
    callers cannot wrap the pair themselves). ``fn`` runs with the lock
    held: keep it pure and quick (string work only). Any exception from
    ``fn`` propagates unchanged and leaves the body untouched.

    Returns:
        ``(new_body, byte_size)`` with ``new_body`` CRLF-normalized as written.

    Raises:
        DocFileError: as :func:`write_body`.
    """
    paths = doc_paths(doc_id)
    with doc_lock(doc_id):
        current_bytes = _read_regular_bytes(
            paths.body, missing_message="Doc body is missing."
        )
        current = current_bytes.decode("utf-8", errors="replace")
        new_body = _normalize_body(fn(current))
        data = _encode_body(new_body)
        _write_body_locked(paths, data, snapshot=snapshot, source=snapshot_source)
    return new_body, len(data)


def list_revisions(doc_id: str) -> list[Path]:
    """Revision snapshot files (the ``.md`` half of each pair), oldest first
    (timestamp, then sequence). A snapshot without a sidecar is listed."""
    return _list_revision_files(doc_paths(doc_id).revisions)


def read_revision_meta(path: Path) -> Optional[dict]:
    """The ``.json`` sidecar of a snapshot from :func:`list_revisions`.

    Returns the sidecar dict (``written_at`` str, ``source`` str or None,
    ``size`` int), or None when the sidecar is missing, unreadable (a
    symlink or special file is never followed or opened blocking), not
    JSON, or not that shape.
    """
    path = Path(path)
    if not _REVISION_RE.match(path.name):
        return None
    try:
        raw = _read_regular_bytes(
            _revision_meta_path(path), missing_message="Revision metadata is missing.",
        )
        meta = json.loads(raw.decode("utf-8"))
    except (DocFileError, OSError, ValueError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError.
        return None
    if (
        not isinstance(meta, dict)
        or not isinstance(meta.get("written_at"), str)
        or "source" not in meta
        or not (meta["source"] is None or isinstance(meta["source"], str))
        or not isinstance(meta.get("size"), int)
        or isinstance(meta.get("size"), bool)
    ):
        return None
    return meta


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def sniff_image_type(data: bytes) -> Optional[str]:
    """Return ``"png"``, ``"jpg"``, ``"gif"`` or ``"webp"`` from magic bytes.

    Anything else -- SVG (XML text, scriptable), BMP, text, truncated
    headers -- returns None. The extension of a stored asset always comes
    from here, never from the caller's file name.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def sanitize_asset_name(hint: str) -> str:
    """Turn a caller-supplied file name into a safe asset stem (no extension).

    Basename only (``/`` and ``\\`` separators), extension dropped, runs of
    characters outside ``[A-Za-z0-9._-]`` collapsed to one ``-``, leading
    and trailing dots/dashes stripped, at most 64 characters, ``"image"``
    when nothing is left.
    """
    name = hint if isinstance(hint, str) else ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _ext = os.path.splitext(name)
    stem = _ASSET_NAME_UNSAFE_RE.sub("-", stem).strip(".-")
    stem = stem[:_ASSET_STEM_MAX_LEN].rstrip(".-")
    return stem or "image"


def _iter_asset_entries(assets_dir: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Yield ``(name, lstat)`` for every directory entry of ``assets/``."""
    if not os.path.lexists(assets_dir):
        return
    st = os.lstat(assets_dir)
    if not stat.S_ISDIR(st.st_mode):
        raise DocFileError("Doc assets path is not a directory.")
    for name in sorted(os.listdir(assets_dir)):
        try:
            yield name, os.lstat(assets_dir / name)
        except FileNotFoundError:
            continue


def _regular_assets(assets_dir: Path) -> list[tuple[str, int]]:
    """``(name, size)`` of regular, non-hidden files in ``assets/``."""
    return [
        (name, st.st_size)
        for name, st in _iter_asset_entries(assets_dir)
        if stat.S_ISREG(st.st_mode) and not name.startswith(".")
    ]


def list_assets(doc_id: str) -> list[dict]:
    """``[{"name", "size"}]`` for the doc's assets, sorted by name.

    Regular files only: symlinks, directories and special files are skipped.
    """
    assets = _regular_assets(doc_paths(doc_id).assets)
    return [{"name": name, "size": size} for name, size in assets]


def asset_stats(doc_id: str) -> tuple[int, int]:
    """``(count, total_bytes)`` over the doc's regular asset files."""
    assets = _regular_assets(doc_paths(doc_id).assets)
    return len(assets), sum(size for _name, size in assets)


def add_asset(doc_id: str, name_hint: str, data: bytes) -> AssetInfo:
    """Store a raster image under ``assets/`` and return its final name.

    The type is sniffed from ``data`` (png/jpg/gif/webp only) and decides
    the extension; the stem comes from :func:`sanitize_asset_name` with a
    ``-2``, ``-3``... suffix on collision. The file is written to a temp
    name in the doc root and hard-linked into place (``os.link`` is atomic
    and fails if the name exists), so readers never see a partial image.

    Raises:
        DocFileError: unsupported type, over ``DOC_MAX_IMAGE_SIZE``, or the
            doc would exceed ``DOC_MAX_ASSETS`` / ``DOC_MAX_ASSETS_TOTAL_BYTES``.
    """
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    ext = sniff_image_type(data)
    if ext is None:
        raise DocFileError(
            "Not a supported raster image (png, jpg, gif, webp)."
        )
    size = len(data)
    if size > constants.DOC_MAX_IMAGE_SIZE:
        raise DocFileError(
            f"Image is {size} bytes, over the {constants.DOC_MAX_IMAGE_SIZE}-byte "
            f"per-image limit."
        )

    paths = doc_paths(doc_id)
    stem = sanitize_asset_name(name_hint)
    with doc_lock(doc_id):
        _ensure_layout(paths, create_root=False)
        existing = _regular_assets(paths.assets)
        count = len(existing)
        total = sum(s for _n, s in existing)
        if count >= constants.DOC_MAX_ASSETS:
            raise DocFileError(
                f"This doc already has {count} images, the maximum is "
                f"{constants.DOC_MAX_ASSETS}."
            )
        if total + size > constants.DOC_MAX_ASSETS_TOTAL_BYTES:
            raise DocFileError(
                f"Adding this image would bring the doc's images to "
                f"{total + size} bytes, over the "
                f"{constants.DOC_MAX_ASSETS_TOTAL_BYTES}-byte limit."
            )

        tmp = paths.root / f".asset-{uuid.uuid4().hex}.tmp"
        _write_new_file(tmp, data)
        try:
            n = 1
            while True:
                name = f"{stem}.{ext}" if n == 1 else f"{stem}-{n}.{ext}"
                try:
                    os.link(tmp, paths.assets / name)
                    break
                except FileExistsError:
                    n += 1
                    if n > 10_000:
                        raise DocFileError("Could not allocate an image name.")
        finally:
            tmp.unlink(missing_ok=True)

    return AssetInfo(
        name=name, size=size, asset_count=count + 1, total_bytes=total + size,
    )


def _validate_asset_name(name) -> str:
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or name.startswith(".")
        or "\\" in name
        or "\x00" in name
    ):
        raise DocFileError("Invalid image name.")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in constants.DOC_ASSET_EXTENSIONS:
        raise DocFileError("Invalid image name.")
    return name


def asset_path(doc_id: str, name: str) -> Path:
    """Return the path of an existing asset, refusing anything unsafe.

    ``name`` must be one canonical path segment with an image extension,
    and must name a regular, non-symlink file directly inside ``assets/``.

    Raises:
        DocFileError: bad name, missing, symlink, or not a regular file.
    """
    _validate_asset_name(name)
    paths = doc_paths(doc_id)
    try:
        dir_st = os.lstat(paths.assets)
        if not stat.S_ISDIR(dir_st.st_mode):
            raise DocFileError(f"Image not found: {name}")
        st = os.lstat(paths.assets / name)
    except OSError:
        # FileNotFoundError, ENAMETOOLONG, ... -- all "no such asset".
        raise DocFileError(f"Image not found: {name}") from None
    if not stat.S_ISREG(st.st_mode):
        raise DocFileError(f"Image not found: {name}")
    return paths.assets / name


def read_asset(doc_id: str, name: str) -> bytes:
    """Return an asset's bytes via :func:`asset_path` plus a no-follow,
    non-blocking, regular-file-checked open (closes the lstat/open race)."""
    path = asset_path(doc_id, name)
    return _read_regular_bytes(path, missing_message=f"Image not found: {name}")


# ---------------------------------------------------------------------------
# Download / delete
# ---------------------------------------------------------------------------


def build_zip(doc_id: str, title: str) -> Path:
    """Build a temp zip of ``doc.md`` + ``assets/*`` and return its path.

    Archive layout matches the doc directory (``doc.md``, ``assets/<name>``)
    so the body's relative ``assets/...`` image links keep working after
    extraction. Any symlink or non-regular entry under ``assets/`` refuses
    the whole archive (like ``create_folder_zip``); the partial zip is
    unlinked on any failure. ``title`` only seeds the temp file name -- the
    route chooses the download name. The caller deletes the returned file.

    Raises:
        DocFileError: missing/unsafe body, symlinks or special files.
    """
    paths = doc_paths(doc_id)
    prefix = f"quest-doc-{sanitize_asset_name(title or '')[:40]}-"
    with doc_lock(doc_id):
        fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".zip")
        os.close(fd)
        zip_path = Path(tmp_name)
        try:
            body = _read_regular_bytes(paths.body, missing_message="Doc body is missing.")
            entries = list(_iter_asset_entries(paths.assets))
            for name, st in entries:
                if stat.S_ISLNK(st.st_mode):
                    raise DocFileError(
                        "Cannot archive a doc containing symbolic links."
                    )
                if not stat.S_ISREG(st.st_mode):
                    raise DocFileError(
                        "Cannot archive a doc containing special files."
                    )
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(BODY_NAME, body)
                for name, _st in entries:
                    data = _read_regular_bytes(
                        paths.assets / name, missing_message=f"Image vanished: {name}",
                    )
                    zf.writestr(f"{ASSETS_DIR_NAME}/{name}", data)
        except BaseException:
            zip_path.unlink(missing_ok=True)
            raise
    return zip_path


def delete_doc_dir(doc_id: str) -> None:
    """Remove a doc's directory tree; a missing directory is fine.

    Call after the DB row is gone (collect ids first for project/account
    deletes). A symlink at the doc's path is unlinked, never followed.
    The per-doc lock object is kept (dropping it could let a waiter and a
    newcomer hold two different locks for the same id).
    """
    paths = doc_paths(doc_id)
    with doc_lock(doc_id):
        try:
            st = os.lstat(paths.root)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(paths.root)
        else:
            paths.root.unlink(missing_ok=True)
