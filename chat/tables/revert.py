"""Restore a Quest Table to a point in its change log by forward replay
(see docs/architecture/quest-tables.md and spec section 4.5).

There is one revert operation: :func:`restore_to` rebuilds the table as it
was right after a chosen change, replaying the log forward from change #1
into a fresh file and swapping that file in. There is no single-change
undo and no pruning; the ``before`` images stay in the log for the
History detail view only.

Replay path
-----------

Every change id ``Q`` has a **replay path**, the entries whose ``after``
images, applied in order to an empty file, give the table as it was right
after ``Q``::

    path(first) = [first]
    path(Q)     = path(target) + [Q]     when Q is a restore entry
    path(Q)     = path(prev(Q)) + [Q]    otherwise (prev = the next lower id)

So a restore entry inside a replayed range means "state equals its
target", and reversing a restore is restoring to the entry just before it.
Each entry's predecessor on its path is a single id, so a path is a walk
down a linked list (:func:`replay_path`, iterative, linear). An entry is
**in effect** when the path to the newest entry contains it
(:func:`in_effect_ids`); nothing is stored for this.

Applying one entry
------------------

- change #1 (``create_table``): the DDL of
  :func:`chat.tables.storage.create_table_db` for its columns.
- ``rows``: every ``_id`` of ``row_ids`` is deleted, then each row image
  of ``after`` is inserted verbatim (original ``_id``, timestamps and
  stored cells, no re-canonicalization). Deleting first means values
  swapped between rows in one change never trip a unique index.
- ``schema`` (images spelled out in
  :func:`chat.tables.storage.apply_schema_change`): drop the columns gone
  from ``after["ddl_columns"]`` (their unique index first), append the new
  ones in that order, check the physical order equals it, drop the unique
  indexes gone from ``after["unique_indexes"]`` and those in
  ``after["created_indexes"]``, apply ``after["backfill"]`` to every row,
  replay ``before["rewrites"]`` forward in order, then create the
  ``created_indexes`` in order and check the index order equals
  ``unique_indexes``, so ``sqlite_master`` and ``PRAGMA index_list`` match
  the live history.
- ``restore``: nothing (the path already led through its target).

Replay only reproduces states the table passed through, so nothing is
re-validated and no restore is refused for conflicts or size; each step
reads one entry's images, so memory is bounded by ``TABLE_CHANGE_MAX_BYTES``.
Images that do not replay (missing keys, a cell that does not hold what a
step expects, a constraint broken mid-replay) are ``corrupt_storage``.

Restore entry
-------------

``kind`` / ``op`` "restore", ``summary`` "Restored table to change #N",
``schema_before`` the caller's ``schema_version``, ``schema_after`` one
more (a restore always bumps the version so open views reload),
``row_ids`` ``[]``, ``before`` ``{}``, ``after`` ``{"restore_to": N}``.
"""

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any, NamedTuple

from chat.tables.schema import Column, TableSchemaError, load_schema
from chat.tables.storage import (
    CHANGE_COLUMNS,
    RESERVED_COLUMNS,
    TABLE_DB_NAME,
    TableStorageError,
    _busy_as_refusal,
    _check_schema_matches,
    _check_source,
    _count,
    _create_table_ddl,
    _ddl_columns,
    _check_list_args,
    _ident,
    _insert_image_row,
    _is_busy,
    _json,
    _list_changes,
    _meta_values,
    _multi_sort_key,
    _open_db_file,
    _resolve_now,
    _unique_indexes,
    append_change,
    connect,
    sql_type,
    unique_index_name,
    write_meta,
)

logger = logging.getLogger(__name__)

_RESERVED = frozenset(RESERVED_COLUMNS)

# SQLite side files of a database file.
_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")

# Prefix of the temporary file a restore builds next to table.db.
TEMP_PREFIX = TABLE_DB_NAME + ".restore-"


# --- Errors ------------------------------------------------------------------


class RevertRefusal(TableStorageError):
    """A refused restore. ``change_id`` is the change the caller named."""

    def __init__(self, code: str, message: str, *, change_id: int):
        super().__init__(code, message)
        self.change_id = change_id


class UnknownChangeError(RevertRefusal):
    """``unknown_change``: the log has no change with this id (404)."""

    def __init__(self, change_id: int):
        super().__init__(
            "unknown_change", f"There is no change #{change_id}.", change_id=change_id
        )


# --- Results -----------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of :func:`restore_to`: ``change_id`` the new restore entry,
    ``restored_to`` the target change, the new ``schema_version``, the
    ``columns`` and ``title`` at the target and the ``row_count``."""

    change_id: int
    restored_to: int
    schema_version: int
    columns: list[Column]
    title: str
    row_count: int


class ChangeRef(NamedTuple):
    """The light record of one change the replay path needs: its ``id``,
    ``kind`` and, for a restore entry, ``restore_to`` (else None)."""

    id: int
    kind: str
    restore_to: int | None


# --- Replay path (pure) ------------------------------------------------------


def _corrupt(detail: str) -> TableStorageError:
    return TableStorageError(
        "corrupt_storage",
        f"The change log cannot be replayed ({detail}); nothing was changed.",
    )


def _predecessors(entries: Sequence[ChangeRef]) -> dict[int, int | None]:
    """``{id: the previous id on its replay path}`` (None for the first
    entry). Ids must ascend; a restore target must be an earlier entry."""
    prev: dict[int, int | None] = {}
    last: int | None = None
    for entry in entries:
        if last is not None and entry.id <= last:
            raise ValueError("Change refs must be in ascending id order.")
        if entry.kind == "restore":
            target = entry.restore_to
            if (
                not isinstance(target, int)
                or isinstance(target, bool)
                or target not in prev
            ):
                raise _corrupt(f"restore entry #{entry.id} has no earlier target")
            prev[entry.id] = target
        else:
            prev[entry.id] = last
        last = entry.id
    return prev


def replay_path(entries: Sequence[ChangeRef], change_id: int | None = None) -> list[int]:
    """The ids whose ``after`` images rebuild the table as it was right
    after ``change_id`` (default: the newest entry), oldest first (see the
    module docstring). ``entries`` are every change of the log in
    ascending id order (:func:`read_change_refs`); ``[]`` for an empty
    log. An id not in ``entries`` is :class:`UnknownChangeError`."""
    if not entries:
        if change_id is None:
            return []
        raise UnknownChangeError(change_id)
    prev = _predecessors(entries)
    target = entries[-1].id if change_id is None else change_id
    if target not in prev:
        raise UnknownChangeError(target)
    path: list[int] = []
    node: int | None = target
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()
    return path


def in_effect_ids_from(entries: Sequence[ChangeRef]) -> set[int]:
    """The ids on the replay path to the newest of ``entries`` (all of
    them, ascending): the entries in effect. Entries not in the set were
    rewound by a restore."""
    return set(replay_path(entries))


# --- Reads -------------------------------------------------------------------


def _read_refs(conn: sqlite3.Connection) -> list[ChangeRef]:
    try:
        rows = conn.execute(
            "SELECT id, kind, CASE WHEN kind = 'restore' "
            "THEN json_extract(after, '$.restore_to') END FROM changes ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise
        raise _corrupt("a restore entry is not valid JSON") from exc
    return [ChangeRef(*row) for row in rows]


def read_change_refs(table_dir: Path) -> list[ChangeRef]:
    """Every change's :class:`ChangeRef`, ascending (one small row per
    entry, no images)."""
    with connect(table_dir) as conn, _busy_as_refusal():
        return _read_refs(conn)


def in_effect_ids(table_dir: Path) -> set[int]:
    """The ids of the changes in effect (on the replay path to the newest
    entry). The path can reach any older id, so a page of the log alone
    cannot decide it; a list route uses :func:`list_changes_in_effect`,
    which reads the page and the refs together."""
    return in_effect_ids_from(read_change_refs(table_dir))


def list_changes_in_effect(
    table_dir: Path, *, limit: int = 50, before_id: int | None = None
) -> list[dict]:
    """:func:`chat.tables.storage.list_changes` with an ``in_effect`` flag
    on each entry. The page and every :class:`ChangeRef` are read in one
    read transaction, so the flags describe exactly the listed log."""
    limit = _check_list_args(limit, before_id)
    with connect(table_dir) as conn, _busy_as_refusal():
        conn.execute("BEGIN")
        try:
            page = _list_changes(conn, limit=limit, before_id=before_id)
            effect = in_effect_ids_from(_read_refs(conn))
        finally:
            conn.execute("ROLLBACK")
    for entry in page:
        entry["in_effect"] = entry["id"] in effect
    return page


# --- Replay ------------------------------------------------------------------


class _State:
    """The replayed table's schema as of the last applied entry."""

    def __init__(self) -> None:
        self.columns: list[Column] = []
        self.title = ""


def _load_columns(raw: Any) -> list[Column]:
    try:
        return load_schema(raw)
    except (TableSchemaError, TypeError, AttributeError, KeyError) as exc:
        raise _corrupt("a recorded column list is invalid") from exc


def _apply_create(conn: sqlite3.Connection, state: _State, after: Any) -> None:
    if not isinstance(after, Mapping) or after.get("op") != "create_table":
        raise _corrupt("the first change does not create the table")
    columns = _load_columns(after["columns"])
    _create_table_ddl(conn, columns)
    state.columns, state.title = columns, after["title"]


def _apply_rows(
    conn: sqlite3.Connection, state: _State, change_id: int, row_ids: Any, after: Any
) -> None:
    if not isinstance(row_ids, list) or not isinstance(after, list):
        raise _corrupt(f"change #{change_id} has malformed images")
    ids = set(row_ids)
    expected = {"_id", "_created_at", "_updated_at", *(c.key for c in state.columns)}
    for image in after:
        if not isinstance(image, Mapping) or set(image) != expected:
            raise _corrupt(f"a row image of change #{change_id} does not match the columns")
        if image["_id"] not in ids:
            raise _corrupt(f"change #{change_id} writes a row it does not list")
    conn.execute(
        "DELETE FROM rows WHERE _id IN (SELECT value FROM json_each(?))",
        (_json(sorted(ids)),),
    )
    for image in after:
        _insert_image_row(conn, state.columns, image)


def _replay_rewrite(
    conn: sqlite3.Connection, change_id: int, step: Any, by_key: Mapping[str, Column]
) -> None:
    """Apply one ``rewrites`` step forward, exactly as
    :func:`chat.tables.storage.apply_schema_change` executed it."""
    kind = step["kind"]
    key = step["key"]
    column = by_key.get(key)
    if column is None:
        raise _corrupt(f"change #{change_id} rewrites a column it does not keep")
    col = _ident(key)
    if kind == "wrap":
        for row_id in step["row_ids"]:
            row = conn.execute(f"SELECT {col} FROM rows WHERE _id = ?", (row_id,)).fetchone()
            if row is None or row[0] is None:
                raise _corrupt(f"change #{change_id} wraps a missing cell")
            conn.execute(
                f"UPDATE rows SET {col} = ? WHERE _id = ?", (_json([row[0]]), row_id)
            )
    elif kind == "enum_rename":
        ids = _json(sorted(set(step["row_ids"])))
        held = conn.execute(
            f"SELECT COUNT(*) FROM rows WHERE {col} = ? "
            "AND _id IN (SELECT value FROM json_each(?))",
            (step["old"], ids),
        ).fetchone()[0]
        if held != len(set(step["row_ids"])):
            raise _corrupt(f"change #{change_id} renames an option the rows do not hold")
        conn.execute(
            f"UPDATE rows SET {col} = ? WHERE _id IN (SELECT value FROM json_each(?))",
            (step["new"], ids),
        )
    elif kind == "enum_rename_multi":
        order = _multi_sort_key(column.options or ())
        old, new = step["old"], step["new"]
        for id_text, recorded in step["cells"].items():
            row_id = int(id_text)
            row = conn.execute(f"SELECT {col} FROM rows WHERE _id = ?", (row_id,)).fetchone()
            if row is None or row[0] != recorded:
                raise _corrupt(f"change #{change_id} renames a cell that differs")
            values = [new if v == old else v for v in json.loads(recorded)]
            conn.execute(
                f"UPDATE rows SET {col} = ? WHERE _id = ?",
                (_json(sorted(set(values), key=order)), row_id),
            )
    else:
        raise _corrupt(f"change #{change_id} has an unknown rewrite {kind!r}")


def _apply_schema(
    conn: sqlite3.Connection, state: _State, change_id: int, before: Any, after: Any
) -> None:
    columns = _load_columns(after["columns"])
    by_key = {c.key: c for c in columns}
    target = list(after["ddl_columns"])
    current = _ddl_columns(conn)
    for key in current:
        if key not in _RESERVED and key not in target:
            conn.execute(f'DROP INDEX IF EXISTS "{unique_index_name(key)}"')
            conn.execute(f"ALTER TABLE rows DROP COLUMN {_ident(key)}")
    for key in target:
        if key not in current:
            column = by_key.get(key)
            if column is None:
                raise _corrupt(f"change #{change_id} adds a column it does not describe")
            conn.execute(f"ALTER TABLE rows ADD COLUMN {_ident(key)} {sql_type(column)}")
    if _ddl_columns(conn) != target or set(target) - _RESERVED != set(by_key):
        raise _corrupt(f"change #{change_id} does not reproduce its column order")
    wanted = list(after["unique_indexes"])
    created = list(after["created_indexes"])
    by_index = {unique_index_name(c.key): c for c in columns if c.unique}
    if sorted(wanted) != sorted(by_index) or not set(created) <= set(wanted):
        raise _corrupt(f"change #{change_id} lists unique indexes its columns lack")
    for name in _unique_indexes(conn):
        if name not in wanted or name in created:
            conn.execute(f'DROP INDEX "{name}"')
    for key, fill in after["backfill"].items():
        if key not in by_key:
            raise _corrupt(f"change #{change_id} backfills a column it does not keep")
        conn.execute(f"UPDATE rows SET {_ident(key)} = ?", (fill["value"],))
    for step in before["rewrites"]:
        _replay_rewrite(conn, change_id, step, by_key)
    for name in created:
        conn.execute(
            f'CREATE UNIQUE INDEX "{name}" ON rows({_ident(by_index[name].key)})'
        )
    if _unique_indexes(conn) != wanted:
        raise _corrupt(f"change #{change_id} does not reproduce its index order")
    state.columns, state.title = columns, after["title"]


def _replay(
    live: sqlite3.Connection, new: sqlite3.Connection, path: Sequence[int]
) -> _State:
    """Apply every entry of ``path`` to the empty ``new`` database, reading
    one entry's images at a time from ``live``."""
    state = _State()
    for position, change_id in enumerate(path):
        kind, row_ids, before, after = live.execute(
            "SELECT kind, row_ids, CASE WHEN kind = 'schema' THEN before END, "
            "CASE WHEN kind != 'restore' THEN after END FROM changes WHERE id = ?",
            (change_id,),
        ).fetchone()
        if position == 0:
            if kind != "schema":
                raise _corrupt("the first change does not create the table")
            _apply_create(new, state, json.loads(after))
        elif kind == "rows":
            _apply_rows(new, state, change_id, json.loads(row_ids), json.loads(after))
        elif kind == "schema":
            _apply_schema(new, state, change_id, json.loads(before), json.loads(after))
        elif kind != "restore":
            raise _corrupt(f"change #{change_id} has unknown kind {kind!r}")
    return state


def _sequence(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (name,)).fetchone()
    return row[0] if row is not None else 0


def _build(
    live: sqlite3.Connection,
    new: sqlite3.Connection,
    path: Sequence[int],
    *,
    change_id: int,
    schema_version: int,
    now: datetime,
    source: str,
    actor_user_id: int | None,
) -> tuple[_State, int, int]:
    """Fill ``new``: replay, copy the log, fix the sequences, append the
    restore entry, write ``meta``. Returns ``(state, new entry id, row
    count)``."""
    state = _replay(live, new, path)
    cols = ", ".join(CHANGE_COLUMNS)
    marks = ", ".join("?" for _ in CHANGE_COLUMNS)
    for row in live.execute(f"SELECT {cols} FROM changes ORDER BY id"):
        new.execute(f"INSERT INTO changes ({cols}) VALUES ({marks})", row)
    logged_max = live.execute(
        "SELECT MAX(j.value) FROM changes, json_each(changes.row_ids) AS j"
    ).fetchone()[0]
    rows_seq = max(
        _sequence(live, "rows"),
        logged_max or 0,
        new.execute("SELECT coalesce(MAX(_id), 0) FROM rows").fetchone()[0],
    )
    new.execute("DELETE FROM sqlite_sequence WHERE name IN ('rows', 'changes')")
    if rows_seq:
        new.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('rows', ?)", (rows_seq,))
    new.execute(
        "INSERT INTO sqlite_sequence (name, seq) VALUES ('changes', ?)",
        (_sequence(live, "changes"),),
    )
    new_id = append_change(
        new,
        now=now,
        source=source,
        actor_user_id=actor_user_id,
        kind="restore",
        op="restore",
        summary=f"Restored table to change #{change_id}",
        schema_before=schema_version,
        schema_after=schema_version + 1,
        row_ids=[],
        before={},
        after={"restore_to": change_id},
    )
    write_meta(
        new,
        schema=state.columns,
        schema_version=schema_version + 1,
        title=state.title,
        table_id=_meta_values(live)["table_id"],
    )
    return state, new_id, _count(new)


# --- Files -------------------------------------------------------------------


def _side_files(path: Path) -> list[Path]:
    return [path.with_name(path.name + s) for s in _SIDE_SUFFIXES]


def _remove_file(path: Path) -> None:
    for item in (path, *_side_files(path)):
        with suppress(OSError):
            item.unlink(missing_ok=True)


def _fsync_path(path: Path, *, directory: bool = False) -> None:
    flags = os.O_RDONLY | (os.O_DIRECTORY if directory else os.O_NOFOLLOW)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_temp(table_dir: Path) -> Path:
    for _ in range(8):
        temp = table_dir / (TEMP_PREFIX + secrets.token_hex(8))
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return temp
    raise TableStorageError("busy", "The table is busy; try again.")


def _remove_stale_temps(table_dir: Path) -> None:
    """Remove ``table.db.restore-*`` files (and their side files) a crashed
    restore left behind. Only a caller holding the table lock may run it,
    so no restore is building one right now."""
    with os.scandir(table_dir) as entries:
        names = [e.name for e in entries if e.name.startswith(TEMP_PREFIX)]
    for name in names:
        with suppress(OSError):
            os.unlink(table_dir / name)


def _replace_file(src: Path, dst: Path) -> None:
    """The atomic swap (a separate function so tests can fail it)."""
    os.replace(src, dst)


def _busy() -> TableStorageError:
    return TableStorageError("busy", "The table is busy; try again.")


def _check_change_id(change_id: object) -> int:
    if not isinstance(change_id, int) or isinstance(change_id, bool):
        raise ValueError(f"change_id must be an integer, got {change_id!r}")
    return change_id


# --- Restore -----------------------------------------------------------------


def restore_to(
    table_dir: Path,
    schema: Sequence[Column],
    change_id: int,
    *,
    schema_version: int,
    source: str,
    actor_user_id: int | None,
    now: datetime | None = None,
) -> RestoreResult:
    """Rebuild the table as it was right after change ``change_id`` and
    append one ``restore`` entry (module docstring). Restoring to the
    newest entry is allowed: it rebuilds the same state and still records
    a restore entry.

    ``schema`` / ``schema_version`` are what the caller loaded from
    quest.db. Updating quest.db from the result (schema, ``schema_version``,
    title, ``row_count``, ``last_write_source = "revert:<new entry id>"``)
    is the caller's job.

    Steps: open the live file, remove temp files a crashed restore left
    (:data:`TEMP_PREFIX`), and hold ``BEGIN IMMEDIATE`` on the live file
    (no writer can change the log while it is read); check the schema
    (:class:`SchemaMismatchError`) and ``change_id``
    (:class:`UnknownChangeError` when < 1 or not in the log); create
    ``table.db.restore-<random>`` in the table directory (``O_CREAT |
    O_EXCL | O_NOFOLLOW``) and build the new database in it through
    :func:`chat.tables.storage._open_db_file`'s guards, in rollback-journal
    mode and one transaction: replay the path, copy every ``changes`` row
    unchanged, set the ``rows`` sequence past every ``_id`` the log names
    and the live sequence (ids are never reused) and the ``changes``
    sequence to the live one, append the restore entry, write ``meta``
    (``table_id`` from the live file). Then switch the new file to WAL (so
    its header says WAL), close it, check it left no side file, fsync it.

    The swap: end the live transaction, ``PRAGMA wal_checkpoint(TRUNCATE)``
    (not completed: ``busy``), check the log and ``rows`` sequence did not
    move (best effort only: a writer that commits after this check and
    before the rename is lost), close the live connection, which as the last connection deletes
    ``-wal`` and ``-shm``; any side file still there means another
    connection is open, and the restore is refused (``busy``). Only then
    ``os.replace`` the temp file over ``table.db`` and fsync the
    directory. A stale ``-wal`` next to the new file would be replayed
    into it (its frames carry no link to the database file), which is why
    the side files must be gone first.

    Requirement for the caller (the A8 service): the per-table lock is
    the only real guarantee. Every writer **and** reader of ``table.db``
    must hold it for its whole call, and the restore holds it throughout;
    storage functions open and close one connection per call, so none
    outlives a call. Without it, a write committed between the re-check
    and the rename is silently lost, and a connection opened between the
    side-file check and the rename could leave a WAL the new file would
    inherit.

    Refusals (the live file untouched, the temp file and its side files
    removed): :class:`SchemaMismatchError`, :class:`UnknownChangeError`,
    ``corrupt_storage`` (images that do not replay), ``busy``. Any other
    failure before the rename also removes the temp file. After the
    rename the table is restored, so a failing directory fsync is only
    logged and the result returned (raising would leave quest.db stale)."""
    change_id = _check_change_id(change_id)
    _check_source(source)
    now = _resolve_now(now)
    table_dir = Path(table_dir)
    db = table_dir / TABLE_DB_NAME
    temp: Path | None = None
    try:
        with connect(table_dir) as live:
            _remove_stale_temps(table_dir)
            with _busy_as_refusal():
                live.execute("BEGIN IMMEDIATE")
            try:
                _check_schema_matches(live, schema, schema_version)
                with _busy_as_refusal():
                    refs = _read_refs(live)
                if change_id < 1:
                    raise UnknownChangeError(change_id)
                path = replay_path(refs, change_id)
                newest = refs[-1].id
                rows_seq = _sequence(live, "rows")
                temp = _create_temp(table_dir)
                new = _open_db_file(temp, wal=False)
                try:
                    new.execute("PRAGMA journal_mode=DELETE")
                    new.execute("BEGIN IMMEDIATE")
                    try:
                        state, new_id, count = _build(
                            live,
                            new,
                            path,
                            change_id=change_id,
                            schema_version=schema_version,
                            now=now,
                            source=source,
                            actor_user_id=actor_user_id,
                        )
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        AttributeError,
                        IndexError,
                        sqlite3.IntegrityError,
                    ) as exc:
                        raise _corrupt("an image does not replay") from exc
                    except sqlite3.OperationalError as exc:
                        if _is_busy(exc):
                            raise _busy() from exc
                        raise _corrupt("an image does not replay") from exc
                    new.execute("COMMIT")
                    new.execute("PRAGMA journal_mode=WAL")
                finally:
                    new.close()
                if any(os.path.lexists(p) for p in _side_files(temp)):
                    raise _busy()
                _fsync_path(temp)
            finally:
                if live.in_transaction:
                    live.execute("ROLLBACK")
            with _busy_as_refusal():
                busy, _, _ = live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                moved = (
                    live.execute("SELECT MAX(id) FROM changes").fetchone()[0] != newest
                    or _sequence(live, "rows") != rows_seq
                )
            if busy or moved:
                raise _busy()
        if any(os.path.lexists(side) for side in _side_files(db)):
            raise _busy()
        _replace_file(temp, db)
        temp = None
    finally:
        if temp is not None:
            _remove_file(temp)
    try:
        _fsync_path(table_dir, directory=True)
    except OSError:
        logger.warning("Table restore: fsync of %s failed after the swap", table_dir,
                       exc_info=True)
    return RestoreResult(
        change_id=new_id,
        restored_to=change_id,
        schema_version=schema_version + 1,
        columns=list(state.columns),
        title=state.title,
        row_count=count,
    )
