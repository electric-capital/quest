"""Table file storage for Quest Tables: ``table.db`` DDL, row writes, the
change log and the reads (see docs/architecture/quest-tables.md).

Layout::

    <TABLES_DIR>/<table_id>/        from ChatStorage.get_table_dir(table_id) only
      table.db                      SQLite, WAL mode: rows + changes + meta

``table.db`` holds three tables:

- ``rows``: ``_id INTEGER PRIMARY KEY AUTOINCREMENT``, ``_created_at``,
  ``_updated_at`` (UTC ISO strings, see :func:`_utc_iso`), then one column
  per schema column named by its immutable key (``c_1a2b3c4d``): INTEGER
  for booleans, TEXT for everything else, never NOT NULL (``required`` is
  enforced in Python by :func:`chat.tables.schema.canonicalize`, so making
  a column required needs no table rebuild). One unique index per
  ``unique`` column, named by :func:`unique_index_name`. AUTOINCREMENT (a
  deviation from the spec's plain ``INTEGER PRIMARY KEY``) means a deleted
  row's ``_id`` is never handed out again: a restore replays rows under
  their original ``_id`` values, and reuse would make a change's ``row_ids``
  name two different rows.
- ``changes``: the change log (spec section 4.5), one entry per write in
  the same transaction as the write (spec invariant 6), kept in full from
  table creation (no pruning).
- ``meta``: ``schema`` (compact JSON of :func:`dump_schema`),
  ``schema_version``, ``table_id`` and ``title``, so a downloaded copy is
  self-describing. quest.db stays the source of truth.

Everything here is synchronous ``sqlite3`` with one connection per call;
the service calls these functions through ``asyncio.to_thread`` under its
per-table ``asyncio.Lock``. Every public function takes the table
directory, never a database path. Only :func:`create_table_db` creates
that directory (exclusively, so a new table never inherits an orphan
file); everything else requires it and refuses with ``storage_missing``.

Every connection comes from :func:`connect`, which refuses a symlinked
table directory, a ``table.db`` that is a symlink or not a regular file
(``O_NOFOLLOW`` + ``fstat`` + ``S_ISREG``, opened ``O_NONBLOCK`` so a
planted FIFO never blocks), a ``-wal`` / ``-shm`` / ``-journal`` side
file that is not a regular file, and a file swapped between the check and
the connect. It turns off SQLite's double-quoted string literals (so a
quoted identifier naming no column is an error, never a string), sets WAL
mode, the busy timeout and an authorizer that denies ``ATTACH`` (the
project database guard), as defense in depth even though no model SQL
ever reaches the file. A database locked past the busy timeout surfaces
as ``busy``, for reads and writes alike.

Writes run in one ``BEGIN IMMEDIATE`` transaction (:func:`_write_txn`):
schema match check, validation (every problem in every row collected),
caps, unique checks against the final state, the write and the change
entry. Any refusal rolls everything back, so nothing is written.
Every row write takes ``dry_run=True``: the same transaction runs to its
end (every check, the write and the change entry's size cap) and is then
rolled back, so it raises exactly what the real write would and otherwise
returns the :class:`RowWriteResult` the write would have, with
``change_id`` None and the change entry's row images in ``before_images``
/ ``after_images`` (the approval path and precards validate this way).
Values go through :func:`apply_default` (insert) or :func:`canonicalize`
(patches), then :func:`to_storage`; reads go through :func:`from_storage`.
Payload rows are keyed by column **name**; images, stored rows and
:func:`dump_rows` output are keyed by column **key**.

Row images (a ``rows`` change's ``before`` / ``after``) are lists of full
stored rows sorted by ``_id``: ``{"_id", "_created_at", "_updated_at",
<key>: <stored cell>}`` with every schema column present, cells in stored
form (booleans 0/1, multi-enums as their compact JSON string), so a rename
never breaks a restore and replay is a plain INSERT.

Schema changes (:func:`apply_schema_change`) execute a
:class:`chat.tables.schema_ops.SchemaChangePlan`'s effects in order in one
transaction: column DDL, unique indexes, backfills, enum rewrites, wraps
and ``rows_violate`` checks (:class:`RowsViolateError`), then ``meta`` and
one ``schema`` change entry. Their images (spelled out in that function's
docstring) are dicts, not row lists: the before and after columns and
title, the physical ``rows`` column order on both sides
(``ddl_columns``), every dropped column's pre-change values, the ordered
cell rewrites (wraps and option renames, compact) a restore replays
forward, each backfilled column's value and the normalized operations.
Change #1 (``create_table``) keeps its own image shape (see
:func:`create_table_db`).
:func:`check_schema_violations` is the same run, always rolled back,
returning counts of what it would touch.

The caps come from :mod:`chat.tables.constants` imported by name; this
module's globals are the patch point (tests monkeypatch
``chat.tables.storage.TABLE_MAX_ROWS`` and friends), read at call time.

The other modules of this package (queries, revert)
build their own transactions from the private helpers here:
:func:`connect`, :func:`_open_db_file`, :func:`_write_txn`,
:func:`_busy_as_refusal`, :func:`_ident`, :func:`sql_type`,
:func:`unique_index_name`, :func:`_create_table_ddl`, :func:`append_change`,
:func:`_list_changes`, :func:`_unique_indexes`,
:func:`_insert_image_row`, :func:`_select_rows` and
:func:`_check_schema_matches`. Nothing outside ``chat.tables`` uses them.
"""

from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import errno
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

from chat.tables.constants import (
    TABLE_CHANGE_MAX_BYTES,
    TABLE_DB_BUSY_TIMEOUT_S,
    TABLE_MAX_COLUMNS,
    TABLE_MAX_ROWS,
    TABLE_ROWS_VIOLATE_MAX_IDS,
    TABLE_WRITE_MAX_ROWS,
)
from chat.tables.schema import (
    RESERVED_COLUMN_NAMES,
    Column,
    TableSchemaError,
    TableValueError,
    apply_default,
    canonicalize,
    clip_repr,
    coerce_value,
    dump_schema,
    field_label,
    find_column,
    from_storage,
    is_valid_column_key,
    load_schema,
    to_storage,
)
from chat.tables.schema_ops import (
    AddColumnDDL,
    Backfill,
    CaptureDroppedValues,
    CreateUniqueIndex,
    DropColumnDDL,
    DropUniqueIndex,
    EnumRewrite,
    SchemaChangePlan,
    ViolationCheck,
    WrapMulti,
)

logger = logging.getLogger(__name__)

TABLE_DB_NAME = "table.db"

# SQLite side files next to table.db; each must be a regular file if present.
_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")

# A write's ``source`` (also data_tables.last_write_source) starts with one
# of these, followed by a non-empty id.
SOURCE_PREFIXES = ("conversation:", "action_request:", "ui:", "revert:")

# Server-maintained columns of ``rows``.
ROW_ID = "_id"
RESERVED_COLUMNS = RESERVED_COLUMN_NAMES

# Most changes list_changes returns per call.
_LIST_CHANGES_MAX = 200

_CHANGE_TOO_LARGE_HINT = "Split it into smaller writes."

_JSON_SEPARATORS = (",", ":")


# --- Errors ------------------------------------------------------------------


class TableStorageError(Exception):
    """A refusal from the table file layer. ``code`` is machine-readable,
    ``message`` is model/user-facing text (also ``str(err)``).

    Plain codes: ``unsafe_path``, ``storage_missing``, ``storage_exists``,
    ``corrupt_storage``, ``busy``, ``no_rows``, ``constraint_failed``,
    ``invalid_selector``, ``copy_exists``; the subclasses carry the rest.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidRowsError(TableStorageError):
    """Row validation failed. ``problems`` lists every problem as
    ``{"row": index, "column": name_or_None, "code": code, "reason": text}``
    ordered by row, then schema column order."""

    def __init__(self, problems: list[dict]):
        count = len(problems)
        super().__init__(
            "invalid_rows",
            f"The rows have {count} problem{'' if count == 1 else 's'}; "
            "nothing was written.",
        )
        self.problems = problems


class TooManyRowsError(TableStorageError):
    """A write touches more than ``TABLE_WRITE_MAX_ROWS`` rows."""

    def __init__(self, count: int, cap: int | None = None):
        cap = TABLE_WRITE_MAX_ROWS if cap is None else cap
        super().__init__(
            "too_many_rows",
            f"This write would touch {count} rows; at most {cap} rows can be "
            "written in one call. Split it into smaller calls or narrow the filter.",
        )
        self.count = count
        self.cap = cap


class TableFullError(TableStorageError):
    """The write would take the table past ``TABLE_MAX_ROWS`` rows."""

    def __init__(self, row_count: int, adding: int, cap: int | None = None):
        cap = TABLE_MAX_ROWS if cap is None else cap
        super().__init__(
            "table_full",
            f"The table holds {row_count} rows and can hold at most {cap}; "
            f"adding {adding} more would exceed the cap.",
        )
        self.row_count = row_count
        self.adding = adding
        self.cap = cap


class ChangeTooLargeError(TableStorageError):
    """The change's before/after images exceed ``TABLE_CHANGE_MAX_BYTES``.
    ``hint`` ends the message (default: split the write)."""

    def __init__(
        self, size_bytes: int, cap: int | None = None, *, hint: str | None = None
    ):
        cap = TABLE_CHANGE_MAX_BYTES if cap is None else cap
        hint = _CHANGE_TOO_LARGE_HINT if hint is None else hint
        super().__init__(
            "change_too_large",
            f"This write's change record would be {size_bytes} bytes, over the "
            f"{cap}-byte limit for one change. {hint}",
        )
        self.size_bytes = size_bytes
        self.cap = cap


class UpsertKeyError(TableStorageError):
    """The upsert ``key_column`` is unknown (``unknown_key_column``) or not
    unique (``key_not_unique``)."""


class SchemaMismatchError(TableStorageError):
    """The live table no longer matches the schema the caller passed.

    This means quest.db's schema and table.db disagree; it is distinct from
    the API-level stale-token refusal a caller raises before getting here."""

    def __init__(self, message: str):
        super().__init__("schema_mismatch", message)


# --- Results -----------------------------------------------------------------


@dataclass(frozen=True)
class RowWriteResult:
    """Outcome of one row write.

    ``row_ids``: every touched ``_id``, in input order for an insert (so a
    caller can map payload index to ``_id``), sorted for the other ops.
    ``inserted_ids`` / ``updated_ids``: the rows inserted / updated, in
    input order (empty where the op does neither). ``row_count``: rows in
    the table after the commit. ``change_id``: None when nothing was
    written (a where-delete that matched no row, or a dry run; a dry run's
    ids are the ones the write would assign or touch).

    ``before_images`` / ``after_images``: the change entry's ``before`` /
    ``after`` row images (stored form, keyed by column key, sorted by
    ``_id``; see the module docstring), filled on a dry run only (empty
    tuples after a real write): insert ``[]`` / the rows it would insert
    under the ids the dry run assigned; update the touched rows before /
    after; upsert the pre-existing matched rows only / every written row;
    delete the rows it would delete / ``[]``. The action-request previews
    and the ``rows_changed`` check read them.
    """

    op: str
    inserted: int
    updated: int
    deleted: int
    row_ids: list[int]
    inserted_ids: list[int]
    updated_ids: list[int]
    row_count: int
    change_id: int | None
    before_images: Sequence[dict] = field(default=())
    after_images: Sequence[dict] = field(default=())


@dataclass(frozen=True)
class TableMeta:
    """The ``meta`` table of a table.db."""

    table_id: str
    title: str
    schema: list[Column]
    schema_version: int


# --- SQL identifiers -----------------------------------------------------------


def _deny_attach_authorizer(action: int, _arg1, _arg2, _db_name, _trigger) -> int:
    """SQLite authorizer refusing ATTACH (and DETACH) at compile time, as
    chat/gemini_api/tool_handlers/project_db.py does for project databases."""
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _ident(key: str) -> str:
    """A column key as a double-quoted SQL identifier. Only keys that pass
    :func:`is_valid_column_key` are ever interpolated into SQL."""
    if not is_valid_column_key(key):
        raise ValueError(f"Not a valid column key: {key!r}")
    return f'"{key}"'


def sql_type(column: Column) -> str:
    """The SQL column type of ``column`` in ``rows``."""
    return "INTEGER" if column.type == "boolean" else "TEXT"


def unique_index_name(key: str) -> str:
    """The name of the unique index on column ``key`` (unquoted; it is
    built from a validated key, so quoting it is safe)."""
    if not is_valid_column_key(key):
        raise ValueError(f"Not a valid column key: {key!r}")
    return "ux_rows_" + key


def _cell_list(schema: Sequence[Column]) -> str:
    return ", ".join(_ident(c.key) for c in schema)


# --- Guarded open ------------------------------------------------------------


def _unsafe(name: str, why: str) -> TableStorageError:
    return TableStorageError("unsafe_path", f"Refusing to open {name}: {why}.")


def _check_real_dir(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise TableStorageError("storage_missing", "Table storage is missing.") from None
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(path.name, "not a directory")


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """Whether ``exc`` is SQLite's "database is locked" / busy error."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@contextmanager
def _busy_as_refusal() -> Iterator[None]:
    """Re-raise a busy/locked ``OperationalError`` as ``busy``; any other
    error passes through unchanged."""
    try:
        yield
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise TableStorageError("busy", "The table is busy; try again.") from exc
        raise


def _check_side_files(db: Path) -> None:
    for suffix in _SIDE_SUFFIXES:
        side = db.with_name(db.name + suffix)
        try:
            st = os.lstat(side)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise _unsafe(side.name, "not a regular file")


def _open_db_file(db: Path, *, wal: bool = True) -> sqlite3.Connection:
    """Open ``db`` through the guards described in the module docstring and
    return a configured connection (caller closes it). ``wal`` False leaves
    the journal mode alone (the download copy, which must not grow
    ``-wal`` / ``-shm`` files).

    The side files are checked before the connect and again after the
    pragmas, by which point SQLite has opened ``-wal`` / ``-shm``. What
    remains is the window between the first check and SQLite's own open of
    a side file (a planted symlink there is followed once, then refused),
    and a ``-journal`` file, which only a rollback-journal database (the
    download copy) creates, per transaction, after both checks."""
    _check_real_dir(db.parent)
    try:
        fd = os.open(db, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise TableStorageError("storage_missing", "Table storage is missing.") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _unsafe(db.name, "it is a symbolic link") from None
        raise _unsafe(db.name, exc.strerror or "cannot open") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _unsafe(db.name, "not a regular file")
        _check_side_files(db)
        timeout = TABLE_DB_BUSY_TIMEOUT_S
        conn = sqlite3.connect(
            str(db),
            timeout=timeout,
            isolation_level=None,
            check_same_thread=True,
        )
        try:
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
            conn.set_authorizer(_deny_attach_authorizer)
            after = os.stat(db)
            if (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
                raise _unsafe(db.name, "the file changed while it was opened")
            with _busy_as_refusal():
                conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
                if wal:
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
            _check_side_files(db)
        except BaseException:
            conn.close()
            raise
    finally:
        os.close(fd)
    return conn


@contextmanager
def connect(table_dir: Path) -> Iterator[sqlite3.Connection]:
    """A guarded, configured connection to ``table_dir/table.db``, closed on
    exit. Autocommit (``isolation_level=None``): writers open their own
    transaction with :func:`_write_txn`."""
    conn = _open_db_file(Path(table_dir) / TABLE_DB_NAME)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_txn(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; ``ROLLBACK`` on any exception. A
    lock held past the busy timeout at ``BEGIN`` or ``COMMIT`` is ``busy``.
    A residual ``IntegrityError`` (the Python checks should have caught it)
    is logged and re-raised as ``constraint_failed`` with a fixed message,
    since SQLite's text names internal column keys. ``dry_run`` ends a
    block that completed normally with ``ROLLBACK`` instead of ``COMMIT``,
    so every row, change entry and ``sqlite_sequence`` bump is undone."""
    with _busy_as_refusal():
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        with _busy_as_refusal():
            conn.execute("ROLLBACK" if dry_run else "COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, sqlite3.IntegrityError):
            logger.warning("Table write broke a table constraint: %s", exc)
            raise TableStorageError(
                "constraint_failed",
                "The write broke a table constraint; nothing was written.",
            ) from exc
        raise


# --- Small helpers -----------------------------------------------------------


def _check_aware(now: datetime) -> None:
    """A naive ``now`` would be read as local time; it is a caller bug."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    _check_aware(now)
    return now.astimezone(timezone.utc)


def _utc_iso(now: datetime) -> str:
    """Fixed-width UTC ISO 8601 with microseconds and ``+00:00`` (sorts
    lexicographically in time order). A naive ``now`` is a ValueError."""
    _check_aware(now)
    return now.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=_JSON_SEPARATORS)


def _check_source(source: object) -> str:
    """A write source must be ``<prefix><non-empty id>``; anything else is a
    caller bug (ValueError)."""
    if isinstance(source, str):
        for prefix in SOURCE_PREFIXES:
            if source.startswith(prefix) and len(source) > len(prefix):
                return source
    raise ValueError(f"Invalid write source: {source!r}")


def _check_ids(ids: Iterable[object]) -> list[int]:
    out = []
    for value in ids:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Row ids must be integers, got {value!r}")
        out.append(value)
    return out


def _plural(count: int, noun: str = "row") -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _summary(op: str, *, inserted: int = 0, updated: int = 0, deleted: int = 0) -> str:
    """One-line change summary: "Inserted 1 row", "Updated 3 rows",
    "Upserted 5 rows (2 inserted, 3 updated)", "Deleted 7 rows"."""
    if op == "insert":
        return f"Inserted {_plural(inserted)}"
    if op == "update":
        return f"Updated {_plural(updated)}"
    if op == "upsert":
        return (
            f"Upserted {_plural(inserted + updated)} "
            f"({inserted} inserted, {updated} updated)"
        )
    if op == "delete":
        return f"Deleted {_plural(deleted)}"
    raise ValueError(f"Unknown row op: {op!r}")


def _count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]


# --- Create and meta ---------------------------------------------------------


def _check_new_schema(schema: Sequence[Column]) -> None:
    if not schema or len(schema) > TABLE_MAX_COLUMNS:
        raise ValueError(f"A table needs 1 to {TABLE_MAX_COLUMNS} columns.")
    keys = [c.key for c in schema]
    for key in keys:
        if not is_valid_column_key(key):
            raise ValueError(f"Not a valid column key: {key!r}")
    if len(set(keys)) != len(keys):
        raise ValueError("Column keys must be unique.")


def _remove_new_storage(table_dir: Path) -> None:
    """Remove what a failed :func:`create_table_db` created so a retry can
    start clean."""
    db = table_dir / TABLE_DB_NAME
    for path in (db, *(db.with_name(db.name + s) for s in _SIDE_SUFFIXES)):
        with suppress(OSError):
            path.unlink(missing_ok=True)
    with suppress(OSError):
        os.rmdir(table_dir)


def create_table_db(
    table_dir: Path,
    *,
    table_id: str,
    title: str,
    schema: Sequence[Column],
    schema_version: int = 1,
    source: str,
    actor_user_id: int | None,
    now: datetime | None = None,
) -> int:
    """Create ``table_dir`` and its ``table.db``: the DDL, the ``meta``
    rows and change #1, in one transaction. Returns the change id (1).

    ``table_dir`` must not exist (``storage_exists`` for an entry of any
    kind); its parent is created when missing. On any failure after the
    directory was made, everything created is removed again.

    Change #1: ``kind`` "schema", ``op`` "schema", ``summary`` "Created
    table", ``schema_before`` 0, ``schema_after`` ``schema_version``,
    ``row_ids`` ``[]``, ``before`` ``{}``, ``after`` ``{"op":
    "create_table", "title": title, "columns": dump_schema(schema)}``.

    The schema was validated by the caller (``parse_columns``); an empty
    or oversized schema, or invalid or duplicate keys, raise ValueError.
    """
    _check_new_schema(schema)
    _check_source(source)
    table_dir = Path(table_dir)
    try:
        table_dir.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(table_dir)
    except FileExistsError:
        raise TableStorageError("storage_exists", "Table storage already exists.") from None
    try:
        fd = os.open(
            table_dir / TABLE_DB_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
        )
        os.close(fd)
        now = _resolve_now(now)
        with connect(table_dir) as conn, _write_txn(conn):
            _create_table_ddl(conn, schema)
            write_meta(
                conn,
                schema=schema,
                schema_version=schema_version,
                title=title,
                table_id=table_id,
            )
            return append_change(
                conn,
                now=now,
                source=source,
                actor_user_id=actor_user_id,
                kind="schema",
                op="schema",
                summary="Created table",
                schema_before=0,
                schema_after=schema_version,
                row_ids=[],
                before={},
                after={
                    "op": "create_table",
                    "title": title,
                    "columns": dump_schema(schema),
                },
            )
    except BaseException:
        _remove_new_storage(table_dir)
        raise


def _create_table_ddl(conn: sqlite3.Connection, schema: Sequence[Column]) -> None:
    """Create ``rows`` (with ``schema``'s columns and unique indexes),
    ``changes`` and ``meta`` in the caller's transaction. A restore builds
    its fresh file with the same statements, so ``sqlite_master`` matches
    a table created here."""
    columns = ", ".join(f"{_ident(c.key)} {sql_type(c)}" for c in schema)
    conn.execute(
        "CREATE TABLE rows (_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "_created_at TEXT NOT NULL, _updated_at TEXT NOT NULL, "
        f"{columns})"
    )
    for column in schema:
        if column.unique:
            conn.execute(
                f'CREATE UNIQUE INDEX "{unique_index_name(column.key)}" '
                f"ON rows({_ident(column.key)})"
            )
    conn.execute(
        "CREATE TABLE changes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL, source TEXT NOT NULL, "
        "actor_user_id INTEGER, kind TEXT NOT NULL, op TEXT NOT NULL, "
        "summary TEXT NOT NULL, schema_before INTEGER NOT NULL, "
        "schema_after INTEGER NOT NULL, row_ids TEXT NOT NULL, "
        "before TEXT NOT NULL, after TEXT NOT NULL, "
        "size_bytes INTEGER NOT NULL)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def write_meta(
    conn: sqlite3.Connection,
    *,
    schema: Sequence[Column] | None = None,
    schema_version: int | None = None,
    title: str | None = None,
    table_id: str | None = None,
) -> None:
    """Upsert the given ``meta`` keys (the others are left alone). Runs in
    the caller's transaction."""
    values: dict[str, str] = {}
    if schema is not None:
        values["schema"] = _json(dump_schema(schema))
    if schema_version is not None:
        values["schema_version"] = str(int(schema_version))
    if title is not None:
        values["title"] = title
    if table_id is not None:
        values["table_id"] = table_id
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        list(values.items()),
    )


def _meta_values(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def read_meta(table_dir: Path) -> TableMeta:
    """The table's ``meta`` (schema re-validated through ``load_schema``)."""
    with connect(table_dir) as conn, _busy_as_refusal():
        values = _meta_values(conn)
    try:
        return TableMeta(
            table_id=values["table_id"],
            title=values["title"],
            schema=load_schema(json.loads(values["schema"])),
            schema_version=int(values["schema_version"]),
        )
    except (KeyError, ValueError, TypeError, AttributeError, TableSchemaError) as exc:
        raise TableStorageError(
            "corrupt_storage", "The table file's metadata is missing or invalid."
        ) from exc


# --- Change log --------------------------------------------------------------


# Columns of ``changes`` in DDL order (a restore copies the log by them).
CHANGE_COLUMNS = (
    "id",
    "created_at",
    "source",
    "actor_user_id",
    "kind",
    "op",
    "summary",
    "schema_before",
    "schema_after",
    "row_ids",
    "before",
    "after",
    "size_bytes",
)


def append_change(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    source: str,
    actor_user_id: int | None,
    kind: str,
    op: str,
    summary: str,
    schema_before: int,
    schema_after: int,
    row_ids: Sequence[int],
    before: Any,
    after: Any,
    too_large_hint: str | None = None,
) -> int:
    """Insert one ``changes`` entry; returns its id. Runs in the caller's
    transaction. ``kind`` is ``rows``, ``schema`` or ``restore``.
    ``before`` / ``after`` are JSON-ready images; ``size_bytes`` is their
    UTF-8 encoded size, and above ``TABLE_CHANGE_MAX_BYTES`` the change is
    refused with :class:`ChangeTooLargeError` (its message ending in
    ``too_large_hint`` when given) before anything is inserted. The log is
    never pruned."""
    before_json = _json(before)
    after_json = _json(after)
    size_bytes = len(before_json.encode("utf-8")) + len(after_json.encode("utf-8"))
    if size_bytes > TABLE_CHANGE_MAX_BYTES:
        raise ChangeTooLargeError(size_bytes, hint=too_large_hint)
    cursor = conn.execute(
        "INSERT INTO changes (created_at, source, actor_user_id, kind, op, summary, "
        "schema_before, schema_after, row_ids, before, after, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _utc_iso(now),
            source,
            actor_user_id,
            kind,
            op,
            summary,
            schema_before,
            schema_after,
            _json(sorted(row_ids)),
            before_json,
            after_json,
            size_bytes,
        ),
    )
    return cursor.lastrowid


# --- Shared row-write machinery ----------------------------------------------


def _check_row_list(rows: object) -> list:
    """A row payload must be a non-empty list of at most
    ``TABLE_WRITE_MAX_ROWS`` entries (checked before the transaction). A
    non-list is a caller bug (ValueError); the tools' JSON schemas send
    arrays."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError("Rows must be a list.")
    if not rows:
        raise TableStorageError("no_rows", "No rows were given; nothing was written.")
    if len(rows) > TABLE_WRITE_MAX_ROWS:
        raise TooManyRowsError(len(rows))
    return list(rows)


def _check_schema_matches(
    conn: sqlite3.Connection, schema: Sequence[Column], schema_version: int
) -> None:
    """Refuse with :class:`SchemaMismatchError` unless the live ``rows``
    columns are exactly ``schema``'s keys and the stored ``schema_version``
    equals ``schema_version``."""
    live = {
        row[1] for row in conn.execute("PRAGMA table_info(rows)").fetchall()
    } - set(RESERVED_COLUMNS)
    stored = _meta_values(conn).get("schema_version")
    if live != {c.key for c in schema} or stored != str(schema_version):
        raise SchemaMismatchError(
            "The table's schema changed since it was read "
            f"(now version {stored}); reload the table and retry."
        )


class _Problems:
    """Collects invalid_rows problems and orders them by row, then within a
    row: row-level problems (``column`` None), then ``_id``, then schema
    columns in schema order, then any other column (unknown names and the
    read-only timestamps) in the order reported."""

    def __init__(self, schema: Sequence[Column]):
        self._slots = {c.name: i for i, c in enumerate(schema)}
        self._items: list[tuple[tuple[int, int, int], dict]] = []

    def _slot(self, column: str | None) -> int:
        if column is None:
            return -2
        if column == ROW_ID:
            return -1
        return self._slots.get(column, len(self._slots))

    def add(self, row: int, column: str | None, code: str, reason: str) -> None:
        slot = self._slot(column)
        self._items.append(
            (
                (row, slot, len(self._items)),
                {"row": row, "column": column, "code": code, "reason": reason},
            )
        )

    def add_value_error(self, row: int, exc: TableValueError) -> None:
        self.add(row, exc.column, exc.code, exc.detail)

    def __bool__(self) -> bool:
        return bool(self._items)

    def raise_if_any(self) -> None:
        if self._items:
            raise InvalidRowsError([p for _, p in sorted(self._items, key=lambda i: i[0])])


def _row_values(
    problems: _Problems,
    index: int,
    row: object,
    by_name: Mapping[str, Column],
    *,
    id_allowed: bool,
) -> dict[str, Any] | None:
    """The payload row's ``{column key: raw value}`` for its known columns.
    Reports ``invalid_row`` (not an object; returns None),
    ``read_only_column`` (``_created_at``, ``_updated_at``, and ``_id``
    unless ``id_allowed``, in which case ``_id`` is skipped for the caller
    to read) and ``unknown_column``."""
    if not isinstance(row, Mapping):
        problems.add(index, None, "invalid_row", "each row must be an object.")
        return None
    values: dict[str, Any] = {}
    for name, value in row.items():
        if name == ROW_ID and id_allowed:
            continue
        if name in RESERVED_COLUMNS:
            problems.add(
                index,
                name,
                "read_only_column",
                f"{name} is set by the server.",
            )
            continue
        column = by_name.get(name) if isinstance(name, str) else None
        if column is None:
            problems.add(
                index,
                field_label(name),
                "unknown_column",
                f"there is no column named {clip_repr(name)}.",
            )
            continue
        values[column.key] = value
    return values


def _insert_values(
    problems: _Problems, index: int, schema: Sequence[Column], raw: Mapping[str, Any]
) -> dict[str, Any]:
    """Every column's insert value (:func:`apply_default`), keyed by key.
    A column whose value is refused is reported and left out."""
    values: dict[str, Any] = {}
    for column in schema:
        try:
            values[column.key] = apply_default(
                column, column.key in raw, raw.get(column.key)
            )
        except TableValueError as exc:
            problems.add_value_error(index, exc)
    return values


def _patch_values(
    problems: _Problems, index: int, schema: Sequence[Column], raw: Mapping[str, Any]
) -> dict[str, Any]:
    """The patched columns' values (:func:`canonicalize`; an explicit null
    clears), keyed by key. A refused value is reported and left out."""
    values: dict[str, Any] = {}
    for column in schema:
        if column.key in raw:
            try:
                values[column.key] = canonicalize(column, raw[column.key])
            except TableValueError as exc:
                problems.add_value_error(index, exc)
    return values


def _check_unique(
    conn: sqlite3.Connection,
    problems: _Problems,
    schema: Sequence[Column],
    changing: Sequence[tuple[int, Mapping[str, Any]]],
    touched_ids: Collection[int],
    *,
    unchanged: Mapping[int, Mapping[str, Any]] | None = None,
) -> None:
    """Check every unique column against the state after the write.

    ``changing`` pairs each written row's payload index with the logical
    cells it writes, keyed by column key: every cell of an inserted row,
    the named cells of a patch. ``unchanged`` maps each touched existing
    ``_id`` to its stored unique cells the patch leaves alone (see
    :func:`_split_patched`); ``touched_ids`` are excluded from the
    stored-row query, so those cells must come through here. A missing
    key or None never conflicts.

    A problem always lands on a row that writes the value: on the later of
    two changing rows ("also used by row N of this call"), and on a row
    taking a value an unchanged cell of a touched row or an untouched
    stored row already holds ("already used by row _id N")."""
    touched_json = _json(sorted(touched_ids))
    for column in schema:
        if not column.unique:
            continue
        # value -> ("stored", _id) or ("call", payload index)
        seen: dict[Any, tuple[str, int]] = {}
        for row_id, cells in (unchanged or {}).items():
            value = to_storage(column, cells.get(column.key))
            if value is not None:
                seen[value] = ("stored", row_id)
        for index, values in changing:
            if column.key not in values:
                continue
            value = to_storage(column, values[column.key])
            if value is None:
                continue
            if value in seen:
                kind, holder = seen[value]
                reason = (
                    f"value {clip_repr(value)} is already used by row _id {holder}."
                    if kind == "stored"
                    else f"value {clip_repr(value)} is also used by row {holder} "
                    "of this call."
                )
                problems.add(index, column.name, "duplicate_value", reason)
                continue
            seen[value] = ("call", index)
            hit = conn.execute(
                f"SELECT _id FROM rows WHERE {_ident(column.key)} = ? "
                "AND _id NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
                (value, touched_json),
            ).fetchone()
            if hit is not None:
                problems.add(
                    index,
                    column.name,
                    "duplicate_value",
                    f"value {clip_repr(value)} is already used by row _id {hit[0]}.",
                )


def _check_table_room(conn: sqlite3.Connection, adding: int) -> int:
    """Refuse with :class:`TableFullError` when ``adding`` new rows would
    take the table past ``TABLE_MAX_ROWS``; returns the current count."""
    current = _count(conn)
    if adding and current + adding > TABLE_MAX_ROWS:
        raise TableFullError(current, adding)
    return current


def _insert_row(
    conn: sqlite3.Connection,
    schema: Sequence[Column],
    values: Mapping[str, Any],
    stamp: str,
) -> int:
    """Insert one row from logical values keyed by key (a missing key is
    NULL) with both timestamps ``stamp``; returns the new ``_id``."""
    cells = _cell_list(schema)
    marks = ", ".join("?" for _ in schema)
    cursor = conn.execute(
        f"INSERT INTO rows (_created_at, _updated_at, {cells}) VALUES (?, ?, {marks})",
        (stamp, stamp, *(to_storage(c, values.get(c.key)) for c in schema)),
    )
    return cursor.lastrowid


def _insert_image_row(
    conn: sqlite3.Connection, schema: Sequence[Column], image: Mapping[str, Any]
) -> None:
    """Insert one row image (see the module docstring) verbatim: its
    ``_id``, both timestamps and every ``schema`` cell in stored form (a
    key the image lacks is NULL, keys outside ``schema`` are ignored).
    A restore uses it to replay a row under its original ``_id``."""
    cells = _cell_list(schema)
    marks = ", ".join("?" for _ in schema)
    conn.execute(
        f"INSERT INTO rows (_id, _created_at, _updated_at, {cells}) "
        f"VALUES (?, ?, ?, {marks})",
        (
            image["_id"],
            image["_created_at"],
            image["_updated_at"],
            *(image.get(c.key) for c in schema),
        ),
    )


def _row_images(
    conn: sqlite3.Connection, schema: Sequence[Column], ids: Iterable[int]
) -> list[dict]:
    """Full stored rows for ``ids`` (missing ids omitted), sorted by
    ``_id``, in change-image form (see the module docstring)."""
    keys = [c.key for c in schema]
    cursor = conn.execute(
        f"SELECT _id, _created_at, _updated_at, {_cell_list(schema)} FROM rows "
        "WHERE _id IN (SELECT value FROM json_each(?)) ORDER BY _id",
        (_json(sorted(set(ids))),),
    )
    return [
        dict(zip(("_id", "_created_at", "_updated_at", *keys), row))
        for row in cursor.fetchall()
    ]


def _row_id_problem(row_id: object) -> str | None:
    """Why ``row_id`` is not a usable ``_id`` (None when it is one)."""
    if not isinstance(row_id, int) or isinstance(row_id, bool):
        return f"_id must be an integer, got {clip_repr(row_id)}."
    return None


def _existing_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> set[int]:
    """The ``ids`` that exist in ``rows``."""
    cursor = conn.execute(
        "SELECT _id FROM rows WHERE _id IN (SELECT value FROM json_each(?))",
        (_json(sorted(set(ids))),),
    )
    return {row[0] for row in cursor.fetchall()}


def _existing_unique_cells(
    conn: sqlite3.Connection, schema: Sequence[Column], ids: Iterable[int]
) -> dict[int, dict[str, Any]]:
    """``{_id: {unique column key: logical value}}`` for the ``ids`` that
    exist (missing ids are absent, so this doubles as the existence
    check). :func:`_split_patched` turns them into a patched row's
    unchanged unique cells."""
    unique = [c for c in schema if c.unique]
    cells = "".join(f", {_ident(c.key)}" for c in unique)
    cursor = conn.execute(
        f"SELECT _id{cells} FROM rows WHERE _id IN (SELECT value FROM json_each(?))",
        (_json(sorted(set(ids))),),
    )
    return {
        row[0]: {c.key: from_storage(c, v) for c, v in zip(unique, row[1:])}
        for row in cursor.fetchall()
    }


def _split_patched(
    existing: Mapping[str, Any], raw: Mapping[str, Any], values: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A patched row's ``(changing, unchanged)`` cells for
    :func:`_check_unique`: every column the patch names (a refused value
    counts as cleared, so it raises no extra unique problem), and the
    stored unique cells it leaves alone."""
    changing = {key: values.get(key) for key in raw}
    unchanged = {key: value for key, value in existing.items() if key not in raw}
    return changing, unchanged


def _apply_patches(
    conn: sqlite3.Connection,
    schema: Sequence[Column],
    patches: Sequence[tuple[int, Mapping[str, Any]]],
    stamp: str,
) -> None:
    """Write each ``(_id, {key: logical value})`` patch and set its
    ``_updated_at`` to ``stamp``. Every patched unique cell is cleared
    first, so values swapped between rows in one call never trip the
    unique index mid-write (the final state was checked already)."""
    for column in schema:
        if not column.unique:
            continue
        ids = [row_id for row_id, values in patches if column.key in values]
        if ids:
            conn.execute(
                f"UPDATE rows SET {_ident(column.key)} = NULL "
                "WHERE _id IN (SELECT value FROM json_each(?))",
                (_json(ids),),
            )
    for row_id, values in patches:
        columns = [c for c in schema if c.key in values]
        sets = "".join(f", {_ident(c.key)} = ?" for c in columns)
        conn.execute(
            f"UPDATE rows SET _updated_at = ?{sets} WHERE _id = ?",
            (stamp, *(to_storage(c, values[c.key]) for c in columns), row_id),
        )


def _dry_images(dry_run: bool, before: list[dict], after: list[dict]) -> dict:
    """The :class:`RowWriteResult` image fields: the change entry's images
    on a dry run, nothing (the empty defaults) after a real write."""
    if not dry_run:
        return {}
    return {"before_images": before, "after_images": after}


# --- Row writes ----------------------------------------------------------------


def insert_rows(
    table_dir: Path,
    schema: Sequence[Column],
    *,
    schema_version: int,
    source: str,
    actor_user_id: int | None,
    rows: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
    dry_run: bool = False,
) -> RowWriteResult:
    """Insert ``rows`` (each ``{column name: value}``) in one transaction
    with one ``insert`` change. Omitted columns take their default; an
    explicit null does not. ``row_ids`` is in input order.

    Refusals (nothing written): ``no_rows``, :class:`TooManyRowsError`,
    :class:`SchemaMismatchError`, :class:`TableFullError`,
    :class:`InvalidRowsError` (every value and unique problem),
    :class:`ChangeTooLargeError`."""
    _check_source(source)
    rows = _check_row_list(rows)
    now = _resolve_now(now)
    stamp = _utc_iso(now)
    by_name = {c.name: c for c in schema}
    with connect(table_dir) as conn, _write_txn(conn, dry_run=dry_run):
        _check_schema_matches(conn, schema, schema_version)
        current = _check_table_room(conn, len(rows))
        problems = _Problems(schema)
        prepared: list[tuple[int, dict[str, Any]]] = []
        for index, row in enumerate(rows):
            raw = _row_values(problems, index, row, by_name, id_allowed=False)
            if raw is not None:
                prepared.append((index, _insert_values(problems, index, schema, raw)))
        _check_unique(conn, problems, schema, prepared, touched_ids=())
        problems.raise_if_any()
        ids = [_insert_row(conn, schema, values, stamp) for _, values in prepared]
        after = _row_images(conn, schema, ids)
        change_id = append_change(
            conn,
            now=now,
            source=source,
            actor_user_id=actor_user_id,
            kind="rows",
            op="insert",
            summary=_summary("insert", inserted=len(ids)),
            schema_before=schema_version,
            schema_after=schema_version,
            row_ids=ids,
            before=[],
            after=after,
        )
    return RowWriteResult(
        op="insert",
        inserted=len(ids),
        updated=0,
        deleted=0,
        row_ids=ids,
        inserted_ids=list(ids),
        updated_ids=[],
        row_count=current + len(ids),
        change_id=None if dry_run else change_id,
        **_dry_images(dry_run, [], after),
    )


def update_rows(
    table_dir: Path,
    schema: Sequence[Column],
    *,
    schema_version: int,
    source: str,
    actor_user_id: int | None,
    updates: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
    dry_run: bool = False,
) -> RowWriteResult:
    """Patch existing rows in one transaction with one ``update`` change.

    Each update is ``{"_id": int, <column name>: value, ...}``: only the
    named columns change, an explicit null clears a cell, and every
    touched row's ``_updated_at`` becomes ``now`` (``_created_at`` is
    kept). Unique columns are checked against the final state, so values
    swapped between rows in one call are fine. A patch that sets every
    named cell to its current value still counts as updated: it bumps
    ``_updated_at`` and is part of the change. ``updated_ids`` is in input
    order, ``row_ids`` sorted.

    Problems (one :class:`InvalidRowsError`, nothing written):
    ``invalid_row`` (not an object), ``invalid_row_id`` (``_id`` missing
    or not an integer), ``unknown_row``, ``duplicate_row_id`` (the same
    ``_id`` twice), ``empty_patch`` (no column besides ``_id``),
    ``read_only_column``, ``unknown_column``, every value problem from
    :func:`canonicalize` (``required`` when a required cell is cleared)
    and ``duplicate_value``. Other refusals: ``no_rows``,
    :class:`TooManyRowsError`, :class:`SchemaMismatchError`,
    :class:`ChangeTooLargeError`."""
    _check_source(source)
    updates = _check_row_list(updates)
    now = _resolve_now(now)
    stamp = _utc_iso(now)
    by_name = {c.name: c for c in schema}
    with connect(table_dir) as conn, _write_txn(conn, dry_run=dry_run):
        _check_schema_matches(conn, schema, schema_version)
        problems = _Problems(schema)
        # (payload index, _id or None when unusable, raw patch, values)
        parsed: list[tuple[int, int | None, dict[str, Any], dict[str, Any]]] = []
        first_index: dict[int, int] = {}
        for index, update in enumerate(updates):
            raw = _row_values(problems, index, update, by_name, id_allowed=True)
            if raw is None:
                continue
            row_id = update.get(ROW_ID)
            why = _row_id_problem(row_id) if ROW_ID in update else "_id is required."
            if why is not None:
                problems.add(index, ROW_ID, "invalid_row_id", why)
                row_id = None
            elif row_id in first_index:
                problems.add(
                    index,
                    ROW_ID,
                    "duplicate_row_id",
                    f"_id {row_id} is also updated by row {first_index[row_id]} "
                    "of this call.",
                )
                row_id = None
            else:
                first_index[row_id] = index
            if not set(update) - {ROW_ID}:
                problems.add(
                    index,
                    None,
                    "empty_patch",
                    "the update names no column to change.",
                )
            values = _patch_values(problems, index, schema, raw)
            parsed.append((index, row_id, raw, values))
        existing = _existing_unique_cells(conn, schema, first_index)
        changing: list[tuple[int, dict[str, Any]]] = []
        unchanged: dict[int, dict[str, Any]] = {}
        for index, row_id, raw, values in parsed:
            if row_id is None:
                # Which row it would patch is unknown, so a unique check
                # could only guess; its other problems are reported.
                continue
            if row_id not in existing:
                problems.add(
                    index,
                    ROW_ID,
                    "unknown_row",
                    f"there is no row with _id {row_id}.",
                )
            else:
                cells, kept = _split_patched(existing[row_id], raw, values)
                changing.append((index, cells))
                unchanged[row_id] = kept
        _check_unique(
            conn,
            problems,
            schema,
            changing,
            touched_ids=list(existing),
            unchanged=unchanged,
        )
        problems.raise_if_any()
        patches = [(row_id, values) for _, row_id, _, values in parsed]
        ids = sorted(row_id for row_id, _ in patches)
        before = _row_images(conn, schema, ids)
        _apply_patches(conn, schema, patches, stamp)
        after = _row_images(conn, schema, ids)
        change_id = append_change(
            conn,
            now=now,
            source=source,
            actor_user_id=actor_user_id,
            kind="rows",
            op="update",
            summary=_summary("update", updated=len(ids)),
            schema_before=schema_version,
            schema_after=schema_version,
            row_ids=ids,
            before=before,
            after=after,
        )
        count = _count(conn)
    return RowWriteResult(
        op="update",
        inserted=0,
        updated=len(ids),
        deleted=0,
        row_ids=ids,
        inserted_ids=[],
        updated_ids=[row_id for row_id, _ in patches],
        row_count=count,
        change_id=None if dry_run else change_id,
        **_dry_images(dry_run, before, after),
    )


def upsert_rows(
    table_dir: Path,
    schema: Sequence[Column],
    *,
    schema_version: int,
    source: str,
    actor_user_id: int | None,
    key_column: str,
    rows: Sequence[Mapping[str, Any]],
    now: datetime | None = None,
    dry_run: bool = False,
) -> RowWriteResult:
    """Insert or patch ``rows`` matched on the unique column named
    ``key_column``, in one transaction with one ``upsert`` change.

    A row whose canonical key value is already stored patches that row
    like :func:`update_rows` (omitted columns untouched, ``_created_at``
    kept); any other row is inserted like :func:`insert_rows` (defaults
    applied). The table-row cap counts the inserts only. The change's
    ``before`` holds only the pre-existing rows, ``after`` every written
    row. A matched row that changes nothing (only the key, or every cell
    already at its value) still counts as updated: it bumps ``_updated_at``
    and is part of the change. ``inserted_ids`` / ``updated_ids`` are in
    input order, ``row_ids`` sorted.

    A row whose key is missing or refused is validated by the insert rules
    without its key column (there is no stored row it could patch); a row
    repeating an earlier row's key is validated like that earlier row.

    :class:`UpsertKeyError` before any row work: ``unknown_key_column``,
    ``key_not_unique``. Problems (one :class:`InvalidRowsError`, nothing
    written): ``missing_key`` (no or a null key value), ``duplicate_key``
    (the same canonical key twice), the key's own value problems, plus
    those of :func:`insert_rows` / :func:`update_rows`. Other refusals:
    ``no_rows``, :class:`TooManyRowsError`, :class:`SchemaMismatchError`,
    :class:`TableFullError`, :class:`ChangeTooLargeError`."""
    _check_source(source)
    key = find_column(schema, key_column)
    if key is None:
        raise UpsertKeyError(
            "unknown_key_column",
            f"There is no column named {clip_repr(key_column)} to match rows on.",
        )
    if not key.unique:
        raise UpsertKeyError(
            "key_not_unique",
            f"Column {clip_repr(key.name)} is not unique, so it cannot match rows; "
            "upsert needs a unique key column.",
        )
    rows = _check_row_list(rows)
    now = _resolve_now(now)
    stamp = _utc_iso(now)
    by_name = {c.name: c for c in schema}
    with connect(table_dir) as conn, _write_txn(conn, dry_run=dry_run):
        _check_schema_matches(conn, schema, schema_version)
        problems = _Problems(schema)
        # (payload index, stored key or None when unusable, raw row,
        # stored key of the earlier row this one repeats, or None)
        parsed: list[tuple[int, Any, dict[str, Any], Any]] = []
        first_index: dict[Any, int] = {}
        for index, row in enumerate(rows):
            raw = _row_values(problems, index, row, by_name, id_allowed=False)
            if raw is None:
                continue
            stored_key = None
            repeats = None
            try:
                stored_key = to_storage(key, coerce_value(key, raw.get(key.key)))
            except TableValueError as exc:
                problems.add_value_error(index, exc)
            else:
                if stored_key is None:
                    problems.add(
                        index,
                        key.name,
                        "missing_key",
                        f"each row needs a value in the key column {clip_repr(key.name)}.",
                    )
                elif stored_key in first_index:
                    problems.add(
                        index,
                        key.name,
                        "duplicate_key",
                        f"key value {clip_repr(stored_key)} is also used by row "
                        f"{first_index[stored_key]} of this call.",
                    )
                    repeats, stored_key = stored_key, None
                else:
                    first_index[stored_key] = index
            if stored_key is None and repeats is None:
                raw.pop(key.key, None)
            parsed.append((index, stored_key, raw, repeats))
        found: dict[int, int] = {}  # payload index -> existing _id
        for index, stored_key, _, _ in parsed:
            if stored_key is not None:
                hit = conn.execute(
                    f"SELECT _id FROM rows WHERE {_ident(key.key)} = ?", (stored_key,)
                ).fetchone()
                if hit is not None:
                    found[index] = hit[0]
        inserting = sum(
            1
            for index, stored_key, _, _ in parsed
            if stored_key is not None and index not in found
        )
        current = _check_table_room(conn, inserting)
        existing = _existing_unique_cells(conn, schema, found.values())
        changing: list[tuple[int, dict[str, Any]]] = []
        unchanged: dict[int, dict[str, Any]] = {}
        patches: list[tuple[int, dict[str, Any]]] = []
        inserts: list[dict[str, Any]] = []
        non_key = [c for c in schema if c is not key]
        for index, stored_key, raw, repeats in parsed:
            if index in found:
                values = _patch_values(problems, index, schema, raw)
                row_id = found[index]
                cells, kept = _split_patched(existing[row_id], raw, values)
                changing.append((index, cells))
                unchanged[row_id] = kept
                patches.append((row_id, values))
            elif stored_key is not None:
                values = _insert_values(problems, index, schema, raw)
                changing.append((index, values))
                inserts.append(values)
            elif repeats is not None and first_index[repeats] in found:
                # Repeats a key that patches a stored row: patch rules.
                # The row is not written, so it skips the unique check.
                _patch_values(problems, index, schema, raw)
            elif repeats is not None:
                # Repeats a key that inserts: insert rules.
                _insert_values(problems, index, schema, raw)
            else:
                # Missing or refused key: no stored row to patch, so the
                # insert rules apply to the other columns.
                _insert_values(problems, index, non_key, raw)
        _check_unique(
            conn,
            problems,
            schema,
            changing,
            touched_ids=list(found.values()),
            unchanged=unchanged,
        )
        problems.raise_if_any()
        updated_ids = [row_id for row_id, _ in patches]
        before = _row_images(conn, schema, updated_ids)
        _apply_patches(conn, schema, patches, stamp)
        inserted_ids = [_insert_row(conn, schema, values, stamp) for values in inserts]
        ids = sorted(updated_ids + inserted_ids)
        after = _row_images(conn, schema, ids)
        change_id = append_change(
            conn,
            now=now,
            source=source,
            actor_user_id=actor_user_id,
            kind="rows",
            op="upsert",
            summary=_summary(
                "upsert", inserted=len(inserted_ids), updated=len(updated_ids)
            ),
            schema_before=schema_version,
            schema_after=schema_version,
            row_ids=ids,
            before=before,
            after=after,
        )
    return RowWriteResult(
        op="upsert",
        inserted=len(inserted_ids),
        updated=len(updated_ids),
        deleted=0,
        row_ids=ids,
        inserted_ids=inserted_ids,
        updated_ids=updated_ids,
        row_count=current + len(inserted_ids),
        change_id=None if dry_run else change_id,
        **_dry_images(dry_run, before, after),
    )


def delete_rows(
    table_dir: Path,
    schema: Sequence[Column],
    *,
    schema_version: int,
    source: str,
    actor_user_id: int | None,
    row_ids: Sequence[int] | None = None,
    where: tuple[str, Sequence[Any]] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RowWriteResult:
    """Delete rows by ``_id`` or by a prepared filter, in one transaction
    with one ``delete`` change (``before`` the deleted rows, ``after``
    ``[]``). Exactly one of ``row_ids`` / ``where`` (``invalid_selector``
    otherwise).

    ``row_ids``: a non-empty list (``no_rows``) of at most
    ``TABLE_WRITE_MAX_ROWS`` ids (:class:`TooManyRowsError`); problems
    ``invalid_row_id``, ``unknown_row`` and ``duplicate_row_id`` (``row``
    = index in ``row_ids``, ``column`` None) refuse the whole call.

    ``where``: ``(where_sql, params)`` from the filter module, run as
    ``SELECT _id FROM rows WHERE (<where_sql>)`` with ``params`` (a list
    or tuple) bound.
    ``where_sql`` is interpolated, so it must be trusted SQL built from
    validated column keys with every value bound in ``params``; it must
    never contain model or user text. More than ``TABLE_WRITE_MAX_ROWS``
    matches refuse with :class:`TooManyRowsError` carrying the matching
    count; no match writes nothing and returns ``deleted`` 0 and
    ``change_id`` None.

    ``row_ids`` in the result is sorted. Other refusals:
    :class:`SchemaMismatchError`, :class:`ChangeTooLargeError`."""
    _check_source(source)
    if (row_ids is None) == (where is None):
        raise TableStorageError(
            "invalid_selector", "Give exactly one of row_ids or a filter."
        )
    if row_ids is not None:
        row_ids = _check_row_list(row_ids)
    else:
        if (
            not isinstance(where, (tuple, list))
            or len(where) != 2
            or not isinstance(where[0], str)
            or not isinstance(where[1], (tuple, list))
        ):
            raise ValueError("where must be a (where_sql, params) pair.")
        where_sql, params = where[0], tuple(where[1])
    now = _resolve_now(now)
    with connect(table_dir) as conn, _write_txn(conn, dry_run=dry_run):
        _check_schema_matches(conn, schema, schema_version)
        if row_ids is not None:
            problems = _Problems(schema)
            first_index: dict[int, int] = {}
            for index, row_id in enumerate(row_ids):
                why = _row_id_problem(row_id)
                if why is not None:
                    problems.add(index, None, "invalid_row_id", why)
                elif row_id in first_index:
                    problems.add(
                        index,
                        None,
                        "duplicate_row_id",
                        f"_id {row_id} is also listed at index {first_index[row_id]}.",
                    )
                else:
                    first_index[row_id] = index
            existing = _existing_ids(conn, first_index)
            for row_id, index in first_index.items():
                if row_id not in existing:
                    problems.add(
                        index,
                        None,
                        "unknown_row",
                        f"there is no row with _id {row_id}.",
                    )
            problems.raise_if_any()
            ids = sorted(first_index)
        else:
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT _id FROM rows WHERE ({where_sql}) ORDER BY _id", params
                ).fetchall()
            ]
            if len(ids) > TABLE_WRITE_MAX_ROWS:
                raise TooManyRowsError(len(ids))
            if not ids:
                return RowWriteResult(
                    op="delete",
                    inserted=0,
                    updated=0,
                    deleted=0,
                    row_ids=[],
                    inserted_ids=[],
                    updated_ids=[],
                    row_count=_count(conn),
                    change_id=None,
                )
        before = _row_images(conn, schema, ids)
        conn.execute(
            "DELETE FROM rows WHERE _id IN (SELECT value FROM json_each(?))",
            (_json(ids),),
        )
        change_id = append_change(
            conn,
            now=now,
            source=source,
            actor_user_id=actor_user_id,
            kind="rows",
            op="delete",
            summary=_summary("delete", deleted=len(ids)),
            schema_before=schema_version,
            schema_after=schema_version,
            row_ids=ids,
            before=before,
            after=[],
        )
        count = _count(conn)
    return RowWriteResult(
        op="delete",
        inserted=0,
        updated=0,
        deleted=len(ids),
        row_ids=ids,
        inserted_ids=[],
        updated_ids=[],
        row_count=count,
        change_id=None if dry_run else change_id,
        **_dry_images(dry_run, before, []),
    )


# --- Schema changes ----------------------------------------------------------


class RowsViolateError(TableStorageError):
    """A schema change refused because existing rows break a rule a
    :class:`ViolationCheck` guards (``rows_violate``).

    Attributes: ``column_key``, ``column`` (the column's name in the new
    schema), ``check`` (``"required"``, ``"unique"`` or ``"enum_option"``),
    ``option`` (the removed option for ``enum_option``, else None),
    ``row_ids`` (the offending ``_id``\\ s ascending, at most
    ``TABLE_ROWS_VIOLATE_MAX_IDS``) and ``total`` (every offending row)."""

    def __init__(
        self,
        *,
        column_key: str,
        column: str,
        check: str,
        option: str | None,
        row_ids: list[int],
        total: int,
    ):
        ids = ", ".join(str(i) for i in row_ids)
        if total > len(row_ids):
            ids += f", and {total - len(row_ids)} more"
        rows = _plural(total)
        if check == "required":
            what = (
                f"Column {clip_repr(column)} cannot be made required: "
                f"{rows} {'has' if total == 1 else 'have'} no value"
            )
        elif check == "unique":
            what = (
                f"Column {clip_repr(column)} cannot be made unique: {rows} "
                f"{'shares' if total == 1 else 'share'} a value with another row"
            )
        else:
            what = (
                f"Option {clip_repr(option)} cannot be removed from column "
                f"{clip_repr(column)}: {rows} still {'uses' if total == 1 else 'use'} it"
            )
        super().__init__(
            "rows_violate",
            f"{what} (_id {ids}). Fix those rows first; nothing was changed.",
        )
        self.column_key = column_key
        self.column = column
        self.check = check
        self.option = option
        self.row_ids = row_ids
        self.total = total


@dataclass(frozen=True)
class SchemaChangeResult:
    """Outcome of :func:`apply_schema_change`: the new change's id, the new
    ``schema_version``, ``columns`` (the plan's columns), ``title`` (the
    table title after the change), ``row_ids`` (every ``_id`` whose stored
    cells the change touched, sorted; the change entry's ``row_ids``) and
    ``row_count``."""

    change_id: int
    schema_version: int
    columns: list[Column]
    title: str
    row_ids: list[int]
    row_count: int


def _schema_too_large_hint(operations: Sequence[Mapping[str, Any]]) -> str:
    """The ``change_too_large`` hint for a schema change: splitting only
    helps a batch of several operations."""
    if len(operations) > 1:
        return (
            "This schema change touches too many cells to record; "
            "split it into smaller schema changes."
        )
    return (
        "This schema change touches too many cells to record; change "
        "the affected rows with row writes in smaller batches first."
    )

# Longest stretch of a column name, option or title in a change summary.
_SUMMARY_NAME_MAX = 60


class _DryRun(Exception):
    """Raised inside the transaction of a dry run so it rolls back."""


def _clip(text: object) -> str:
    text = field_label(text)
    if len(text) > _SUMMARY_NAME_MAX:
        return text[:_SUMMARY_NAME_MAX] + "…"
    return text


def schema_summary(operations: Sequence[Mapping[str, Any]]) -> str:
    """One line for a schema change: the single operation spelled out
    ("Added column Status"), else "<N> schema changes"."""
    if len(operations) != 1:
        return f"{len(operations)} schema changes"
    op = operations[0]
    kind = op.get("op")
    name = _clip(op.get("name", ""))
    if kind == "add_column":
        return f"Added column {_clip(op['column']['name'])}"
    if kind == "drop_column":
        return f"Dropped column {name}"
    if kind == "rename_column":
        return f"Renamed column {name} to {_clip(op['new_name'])}"
    if kind == "reorder_columns":
        return "Reordered columns"
    if kind == "set_description":
        return f"Changed description of {name}"
    if kind == "set_required":
        return f"Made {name} {'required' if op['required'] else 'optional'}"
    if kind == "set_unique":
        return f"Made {name} unique" if op["unique"] else f"Removed unique from {name}"
    if kind == "set_default":
        return f"Changed default of {name}"
    if kind == "set_precision":
        return f"Changed precision of {name}"
    if kind == "add_enum_option":
        return f"Added option {_clip(op['option'])} to {name}"
    if kind == "rename_enum_option":
        return (
            f"Renamed option {_clip(op['option'])} to {_clip(op['new_option'])} "
            f"in {name}"
        )
    if kind == "remove_enum_option":
        return f"Removed option {_clip(op['option'])} from {name}"
    if kind == "set_enum_multiple":
        return f"Made {name} {'multi-select' if op['multiple'] else 'single-select'}"
    if kind == "rename_table":
        return f"Renamed table to {_clip(op['title'])}"
    return "1 schema change"



def _ddl_columns(conn: sqlite3.Connection) -> list[str]:
    """The physical ``rows`` column names in ``PRAGMA table_info`` order."""
    return [row[1] for row in conn.execute("PRAGMA table_info(rows)").fetchall()]


def _unique_indexes(conn: sqlite3.Connection) -> list[str]:
    """The names of the unique indexes on ``rows`` in ``sqlite_master``
    order (creation order; ``PRAGMA index_list`` follows it)."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'rows' AND sql IS NOT NULL ORDER BY rowid"
        ).fetchall()
    ]


def _violating_ids(
    conn: sqlite3.Connection, check: ViolationCheck
) -> tuple[list[int], int]:
    """``(first TABLE_ROWS_VIOLATE_MAX_IDS offending _ids ascending, total)``
    for one check against the current (in-transaction) rows."""
    col = _ident(check.column_key)
    params: tuple[Any, ...] = ()
    if check.check == "required":
        where = f"{col} IS NULL"
    elif check.check == "unique":
        where = (
            f"{col} IS NOT NULL AND {col} IN (SELECT {col} FROM rows "
            f"WHERE {col} IS NOT NULL GROUP BY {col} HAVING COUNT(*) > 1)"
        )
    elif check.check == "enum_option":
        if check.multiple:
            where = f"EXISTS (SELECT 1 FROM json_each(rows.{col}) WHERE value = ?)"
        else:
            where = f"{col} = ?"
        params = (check.option,)
    else:
        raise ValueError(f"Unknown violation check: {check.check!r}")
    total = conn.execute(f"SELECT COUNT(*) FROM rows WHERE {where}", params).fetchone()[0]
    ids = [
        row[0]
        for row in conn.execute(
            f"SELECT _id FROM rows WHERE {where} ORDER BY _id LIMIT ?",
            (*params, TABLE_ROWS_VIOLATE_MAX_IDS),
        ).fetchall()
    ]
    return ids, total


def _run_check(
    conn: sqlite3.Connection, check: ViolationCheck, names: Mapping[str, str]
) -> None:
    ids, total = _violating_ids(conn, check)
    if total:
        raise RowsViolateError(
            column_key=check.column_key,
            column=names.get(check.column_key, check.column_key),
            check=check.check,
            option=check.option,
            row_ids=ids,
            total=total,
        )


def _check_plan_keys(schema: Sequence[Column], plan: SchemaChangePlan) -> None:
    """Every key in the plan's columns must be in ``schema`` or added by the
    plan (a plan built from another schema is a caller bug)."""
    known = {c.key for c in schema} | {
        e.column_key for e in plan.effects if isinstance(e, AddColumnDDL)
    }
    for column in plan.columns:
        if column.key not in known:
            raise ValueError(f"The plan's column {column.key!r} is not in the schema.")
    if not plan.columns or len(plan.columns) > TABLE_MAX_COLUMNS:
        raise ValueError(f"A table needs 1 to {TABLE_MAX_COLUMNS} columns.")


def _multi_sort_key(options: Sequence[str]):
    rank = {o: i for i, o in enumerate(options)}
    return lambda value: (rank.get(value, len(rank)), value)


def _cells_changed(
    conn: sqlite3.Connection,
    originals: Mapping[str, Mapping[int, Any]],
    after_by_key: Mapping[str, Column],
) -> dict[str, int]:
    """Per surviving pre-existing column, the distinct cells whose stored
    value now differs from the pre-change one (a chained or round-trip
    enum rename counts each cell at most once, and not at all when it ends
    where it started)."""
    changed: dict[str, int] = {}
    for key, cells in originals.items():
        if key not in after_by_key or not cells:
            continue
        now_values = dict(
            conn.execute(f"SELECT _id, {_ident(key)} FROM rows").fetchall()
        )
        n = sum(
            1 for row_id, old in cells.items()
            if row_id in now_values and now_values[row_id] != old
        )
        if n:
            changed[key] = n
    return changed


def _execute_schema_change(
    conn: sqlite3.Connection,
    schema: Sequence[Column],
    plan: SchemaChangePlan,
    *,
    schema_version: int,
    title: str,
    now: datetime,
    source: str,
    actor_user_id: int | None,
    stats: dict | None = None,
) -> SchemaChangeResult:
    """The body of :func:`apply_schema_change`, inside the caller's
    transaction (see that docstring). ``stats``, when given, is filled in
    place with :func:`check_schema_violations`'s counts."""
    _check_schema_matches(conn, schema, schema_version)
    before_keys = {c.key for c in schema}
    after_by_key = {c.key: c for c in plan.columns}
    # Names for refusals: the new schema's, else the old one's.
    names = {c.key: c.name for c in schema} | {c.key: c.name for c in plan.columns}
    ddl_before = _ddl_columns(conn)

    dropped_values: dict[str, dict[str, Any]] = {}
    # key -> {_id: pre-change stored cell} for pre-existing columns whose
    # cells a rewrite or wrap touched; only a later drop of the column in
    # this batch reads it (dropped_values records pre-change values).
    originals: dict[str, dict[int, Any]] = {}
    backfilled: list[str] = []
    # Unique indexes this change created, in creation order (a re-created
    # index moves to its latest creation).
    created: list[str] = []
    # Cell rewrites of pre-existing columns, in execution order (see
    # apply_schema_change); filtered to surviving columns at the end.
    rewrites: list[dict[str, Any]] = []
    # key -> cells each EnumRewrite / WrapMulti effect touched, summed.
    cells_rewritten: dict[str, int] = {}

    def count_rewrites(key: str, n: int) -> None:
        if n:
            cells_rewritten[key] = cells_rewritten.get(key, 0) + n

    def remember(key: str, row_id: int, stored: Any) -> None:
        if key in before_keys:
            originals.setdefault(key, {}).setdefault(row_id, stored)

    for effect in plan.effects:
        key = effect.column_key
        col = _ident(key)
        if isinstance(effect, ViolationCheck):
            _run_check(conn, effect, names)
        elif isinstance(effect, CaptureDroppedValues):
            if key not in before_keys:
                continue  # added in this batch: nothing to restore
            values = {
                row_id: stored
                for row_id, stored in conn.execute(
                    f"SELECT _id, {col} FROM rows WHERE {col} IS NOT NULL ORDER BY _id"
                ).fetchall()
            }
            # A cell this batch rewrote before the drop goes back to its
            # pre-change value.
            for row_id, stored in originals.pop(key, {}).items():
                if stored is None:
                    values.pop(row_id, None)
                else:
                    values[row_id] = stored
            dropped_values[key] = {str(i): values[i] for i in sorted(values)}
        elif isinstance(effect, AddColumnDDL):
            column = after_by_key.get(key)
            kind = sql_type(column) if column is not None else _added_type(plan, key)
            conn.execute(f"ALTER TABLE rows ADD COLUMN {col} {kind}")
        elif isinstance(effect, DropColumnDDL):
            if unique_index_name(key) in created:
                created.remove(unique_index_name(key))
            conn.execute(f'DROP INDEX IF EXISTS "{unique_index_name(key)}"')
            conn.execute(f"ALTER TABLE rows DROP COLUMN {col}")
        elif isinstance(effect, CreateUniqueIndex):
            try:
                conn.execute(
                    f'CREATE UNIQUE INDEX "{unique_index_name(key)}" ON rows({col})'
                )
            except sqlite3.IntegrityError:
                _run_check(conn, ViolationCheck("unique", key), names)
                raise
            if unique_index_name(key) in created:
                created.remove(unique_index_name(key))
            created.append(unique_index_name(key))
        elif isinstance(effect, DropUniqueIndex):
            if unique_index_name(key) in created:
                created.remove(unique_index_name(key))
            conn.execute(f'DROP INDEX IF EXISTS "{unique_index_name(key)}"')
        elif isinstance(effect, Backfill):
            column = after_by_key[key]
            value = effect.value
            if column.type == "enum":
                # The column's multiple flag when the backfill ran (a later
                # set_enum_multiple wraps it).
                column = replace(column, multiple=isinstance(value, (tuple, list)))
            conn.execute(f"UPDATE rows SET {col} = ?", (to_storage(column, value),))
            backfilled.append(key)
        elif isinstance(effect, EnumRewrite):
            if effect.multiple:
                final = after_by_key.get(key)  # None: dropped later in the batch
                order = _multi_sort_key(final.options or () if final else ())
                hits = conn.execute(
                    f"SELECT _id, {col} FROM rows WHERE EXISTS "
                    f"(SELECT 1 FROM json_each(rows.{col}) WHERE value = ?) ORDER BY _id",
                    (effect.old_option,),
                ).fetchall()
                count_rewrites(key, len(hits))
                for row_id, stored in hits:
                    values = [
                        effect.new_option if v == effect.old_option else v
                        for v in json.loads(stored)
                    ]
                    new = _json(sorted(set(values), key=order))
                    remember(key, row_id, stored)
                    conn.execute(
                        f"UPDATE rows SET {col} = ? WHERE _id = ?", (new, row_id)
                    )
                if hits:
                    rewrites.append({
                        "kind": "enum_rename_multi", "key": key,
                        "old": effect.old_option, "new": effect.new_option,
                        "cells": {str(i): v for i, v in hits},
                    })
            else:
                hits = conn.execute(
                    f"SELECT _id, {col} FROM rows WHERE {col} = ? ORDER BY _id",
                    (effect.old_option,),
                ).fetchall()
                count_rewrites(key, len(hits))
                for row_id, stored in hits:
                    remember(key, row_id, stored)
                conn.execute(
                    f"UPDATE rows SET {col} = ? WHERE {col} = ?",
                    (effect.new_option, effect.old_option),
                )
                if hits:
                    rewrites.append({
                        "kind": "enum_rename", "key": key,
                        "old": effect.old_option, "new": effect.new_option,
                        "row_ids": [i for i, _ in hits],
                    })
        elif isinstance(effect, WrapMulti):
            hits = conn.execute(
                f"SELECT _id, {col} FROM rows WHERE {col} IS NOT NULL ORDER BY _id"
            ).fetchall()
            count_rewrites(key, len(hits))
            for row_id, stored in hits:
                remember(key, row_id, stored)
                conn.execute(
                    f"UPDATE rows SET {col} = ? WHERE _id = ?", (_json([stored]), row_id)
                )
            if hits:
                rewrites.append(
                    {"kind": "wrap", "key": key, "row_ids": [i for i, _ in hits]}
                )
        else:  # pragma: no cover - a new effect kind must be handled here
            raise ValueError(f"Unknown schema effect: {effect!r}")

    # Guard against a plan built with table_empty=True for a table that has
    # rows: every column that is newly required must have no null cell.
    old_by_key = {c.key: c for c in schema}
    for column in plan.columns:
        old = old_by_key.get(column.key)
        if column.required and (old is None or not old.required):
            _run_check(conn, ViolationCheck("required", column.key), names)

    touched: set[int] = set()
    surviving = [
        step for step in rewrites
        if step["key"] in before_keys and step["key"] in after_by_key
    ]
    for step in surviving:
        if step["kind"] == "enum_rename_multi":
            touched.update(int(i) for i in step["cells"])
        else:
            touched.update(step["row_ids"])
    backfill: dict[str, dict[str, Any]] = {}
    for key in backfilled:
        if key not in after_by_key:
            continue
        first = conn.execute(f"SELECT {_ident(key)} FROM rows LIMIT 1").fetchone()
        if first is None:
            continue
        backfill[key] = {"value": first[0]}
    if backfill:
        touched.update(row[0] for row in conn.execute("SELECT _id FROM rows"))
    if stats is not None:
        stats["cells_rewritten"] = {
            key: n for key, n in cells_rewritten.items() if key in after_by_key
        }
        stats["cells_changed"] = _cells_changed(conn, originals, after_by_key)
        stats["rows_backfilled"] = _count(conn) if backfill else 0
        stats["dropped_values"] = {
            key: len(values) for key, values in dropped_values.items()
        }
    for values in dropped_values.values():
        touched.update(int(i) for i in values)

    new_version = schema_version + 1
    new_title = plan.new_title or title
    write_meta(conn, schema=plan.columns, schema_version=new_version, title=new_title)
    row_ids = sorted(touched)
    change_id = append_change(
        conn,
        now=now,
        source=source,
        actor_user_id=actor_user_id,
        kind="schema",
        op="schema",
        summary=schema_summary(plan.operations),
        schema_before=schema_version,
        schema_after=new_version,
        row_ids=row_ids,
        before={
            "columns": dump_schema(schema),
            "title": title,
            "ddl_columns": ddl_before,
            "dropped_values": dropped_values,
            "rewrites": surviving,
        },
        after={
            "columns": dump_schema(plan.columns),
            "title": new_title,
            "ddl_columns": _ddl_columns(conn),
            "unique_indexes": _unique_indexes(conn),
            "created_indexes": created,
            "operations": plan.operations,
            "backfill": backfill,
        },
        too_large_hint=_schema_too_large_hint(plan.operations),
    )
    return SchemaChangeResult(
        change_id=change_id,
        schema_version=new_version,
        columns=list(plan.columns),
        title=new_title,
        row_ids=row_ids,
        row_count=_count(conn),
    )


def _added_type(plan: SchemaChangePlan, key: str) -> str:
    """SQL type of a column the batch adds and drops again: it is gone from
    ``plan.columns``, so read it from its ``add_column`` operation."""
    for op in plan.operations:
        if op.get("op") == "add_column" and op["column"].get("key") == key:
            return "INTEGER" if op["column"].get("type") == "boolean" else "TEXT"
    return "TEXT"


def apply_schema_change(
    table_dir: Path,
    schema: Sequence[Column],
    plan: SchemaChangePlan,
    *,
    schema_version: int,
    source: str,
    actor_user_id: int | None,
    title: str,
    now: datetime | None = None,
) -> SchemaChangeResult:
    """Execute ``plan`` (from ``apply_schema_operations(schema, ops,
    table_empty=row_count == 0)``) on the live table in one ``BEGIN
    IMMEDIATE`` transaction with one ``schema`` change entry.

    ``schema`` / ``schema_version`` / ``title`` are the table's current
    columns, version and title as the caller loaded them from quest.db.
    Updating quest.db (``table_store.update_schema``: schema, version,
    title, row count) is the caller's job, after this returns.

    In the transaction: :func:`_check_schema_matches`, then
    ``plan.effects`` exactly in order (they are already the batch's net
    effects; operations are never re-derived): a :class:`ViolationCheck`
    runs against the rows as the effects before it left them (so a check
    on a column the batch just added sees it), a :class:`CaptureDroppedValues`
    saves the column's values, column DDL (``ADD COLUMN`` with no NOT NULL
    or DEFAULT; ``DROP COLUMN`` drops the column's unique index first),
    unique index create / drop, :class:`Backfill` (``to_storage`` of the
    default on every row), :class:`EnumRewrite` (cells holding the old
    option; a multi cell is rewritten, deduplicated and sorted by the new
    columns' option order) and :class:`WrapMulti` (each non-null cell ``v``
    becomes ``[v]``). Then a required check on every column of
    ``plan.columns`` that is newly required (added required, or made
    required): it guards against a plan built with ``table_empty=True``
    for a table that has rows, which emits no backfill. The service should
    still compute ``row_count`` for ``table_empty`` under the table lock.
    Then ``meta`` (new schema, ``schema_version + 1``, title), the change
    entry. Rows' ``_updated_at`` is never touched by a schema change, even
    where cells were rewritten, so a restore that replays the change
    (:mod:`chat.tables.revert`) reproduces the rows byte-equal.

    Change entry: ``kind`` / ``op`` "schema", ``summary`` the one
    operation spelled out ("Added column Status", "Renamed option A to B
    in Tags") or "<N> schema changes", ``schema_before`` /
    ``schema_after`` the old and new version, ``row_ids`` every ``_id``
    with a captured dropped value, a backfilled cell (every row) or a
    cell named by a ``rewrites`` step, and the images (every key always
    present, possibly empty; JSON object keys holding an ``_id`` are its
    decimal string; every cell is in stored form)::

        before = {"columns": dump_schema(schema), "title": title,
                  "ddl_columns": [rows column names, PRAGMA table_info order,
                                  incl. _id, _created_at, _updated_at],
                  "dropped_values": {key: {"<_id>": stored}},
                  "rewrites": [step, ...]}
        after  = {"columns": dump_schema(plan.columns), "title": new title,
                  "ddl_columns": [... after the DDL],
                  "unique_indexes": [index names, sqlite_master order],
                  "created_indexes": [index names this change created],
                  "operations": plan.operations,
                  "backfill": {key: {"value": stored}}}

        step = {"kind": "wrap", "key": key, "row_ids": [...]}
             | {"kind": "enum_rename", "key": key, "old": str, "new": str,
                "row_ids": [...]}
             | {"kind": "enum_rename_multi", "key": key, "old": str,
                "new": str, "cells": {"<_id>": stored before this step}}

    - ``dropped_values``: for each column of ``schema`` the batch drops, its
      non-null cells as they were before the change (a cell the batch
      rewrote before dropping it is recorded with its original value),
      for the History detail view. A column the batch adds and drops
      again is not recorded.
    - ``rewrites``: every cell rewrite of a column present in both schemas,
      in execution order, one step per effect that changed at least one
      cell (``row_ids`` ascending). Steps on a column the batch adds or
      drops are left out (an added column only ever holds its backfill,
      a dropped one is gone). A restore replays the steps forward in
      order, which reproduces the exact stored cells even after chained
      renames or a wrap followed by renames:

      - ``wrap``: each listed non-null cell ``v`` becomes the compact
        JSON list ``[v]``.
      - ``enum_rename`` (single enum): each listed cell holds ``old`` and
        becomes ``new``.
      - ``enum_rename_multi``: each listed cell's recorded stored string
        with ``old`` replaced by ``new``, deduplicated and sorted by the
        option order of the column in ``after["columns"]`` (options not
        in it last, then by value), as compact JSON.

      A cell renamed and renamed back within one batch still appears in
      both steps.
    - ``backfill``: for each surviving column the batch adds with a
      backfill on a non-empty table, the stored value every row ended up
      with (after any later rewrite or wrap in the batch). The column is
      new, so the before value is NULL.
    - ``ddl_columns``: ``ADD COLUMN`` only appends, so a restore that
      drops the columns gone from ``after`` and appends the new ones in
      ``after["ddl_columns"]`` order reproduces the physical order; it
      checks the result against ``after["ddl_columns"]``.
    - ``unique_indexes``: the unique index names on ``rows`` after the
      change in ``sqlite_master`` (creation) order; ``created_indexes``:
      the ones this change created (including one dropped and re-created),
      in creation order. A new index lands after every existing
      ``sqlite_master`` entry, so a restore that drops the created ones and
      creates them again in that order reproduces ``sqlite_master`` and
      ``PRAGMA index_list`` order; it checks the result against
      ``unique_indexes``.

    Refusals (nothing written, the file unchanged):
    :class:`SchemaMismatchError` (stale schema or version),
    :class:`RowsViolateError` (the first failing check, in effect order,
    then the newly-required guard; also raised if creating a unique index
    hits a duplicate the checks did not), :class:`ChangeTooLargeError`
    (with a hint that suggests splitting only for a multi-operation batch), ``busy``. A plan whose columns name
    a key neither in ``schema`` nor added by the plan is a ValueError."""
    _check_source(source)
    _check_plan_keys(schema, plan)
    now = _resolve_now(now)
    with connect(table_dir) as conn, _busy_as_refusal(), _write_txn(conn):
        return _execute_schema_change(
            conn,
            schema,
            plan,
            schema_version=schema_version,
            title=title,
            now=now,
            source=source,
            actor_user_id=actor_user_id,
        )


def check_schema_violations(
    table_dir: Path,
    schema: Sequence[Column],
    plan: SchemaChangePlan,
    *,
    schema_version: int,
) -> dict:
    """Dry run of :func:`apply_schema_change`: execute the whole change in
    a transaction that is always rolled back, so it raises exactly what the
    real call would (:class:`SchemaMismatchError`,
    :class:`RowsViolateError`, :class:`ChangeTooLargeError`, ``busy``).
    Nothing is ever written. The title comes from ``meta``. It takes the
    write lock for its duration, like a write.

    Returns, when the change would apply, what it would touch (JSON-ready,
    for the ``change_table_schema`` preview)::

        {"cells_rewritten": {key: n},   # cells each enum-rename / wrap
                                        # effect touches, summed in effect
                                        # order; columns of plan.columns only
         "cells_changed": {key: n},     # distinct cells of each surviving
                                        # pre-existing column whose stored
                                        # value differs after the change
         "rows_backfilled": n,          # rows a surviving added column's
                                        # backfill fills (the row count), or 0
         "dropped_values": {key: n}}    # non-null cells of each dropped
                                        # pre-existing column
    """
    _check_plan_keys(schema, plan)
    now = _resolve_now(None)
    stats: dict = {}
    with connect(table_dir) as conn, _busy_as_refusal():
        try:
            with _write_txn(conn):
                title = _meta_values(conn).get("title", "")
                _execute_schema_change(
                    conn,
                    schema,
                    plan,
                    schema_version=schema_version,
                    title=title,
                    now=now,
                    source="ui:dry-run",
                    actor_user_id=None,
                    stats=stats,
                )
                raise _DryRun
        except _DryRun:
            pass
    return stats


# --- Reads -----------------------------------------------------------------


def _logical_rows(schema: Sequence[Column], cursor: sqlite3.Cursor) -> list[dict]:
    out = []
    for row in cursor.fetchall():
        item = {"_id": row[0], "_created_at": row[1], "_updated_at": row[2]}
        for column, stored in zip(schema, row[3:]):
            item[column.key] = from_storage(column, stored)
        out.append(item)
    return out


def _select_rows(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()
) -> sqlite3.Cursor:
    """Run a read naming ``schema``'s columns; a column the live table lacks
    means quest.db and table.db disagree (:class:`SchemaMismatchError`)."""
    try:
        return conn.execute(sql, params)
    except sqlite3.OperationalError as exc:
        if "no such column" in str(exc):
            raise SchemaMismatchError(
                "The table's schema changed since it was read; reload the table "
                "and retry."
            ) from exc
        raise


def row_count(table_dir: Path) -> int:
    """Number of rows in the table."""
    with connect(table_dir) as conn, _busy_as_refusal():
        return _count(conn)


def dump_rows(table_dir: Path, schema: Sequence[Column]) -> list[dict]:
    """Every row ordered by ``_id`` as ``{"_id", "_created_at",
    "_updated_at", <key>: logical value}`` with every schema column. A
    schema column the table lacks is :class:`SchemaMismatchError`."""
    with connect(table_dir) as conn, _busy_as_refusal():
        cursor = _select_rows(
            conn,
            f"SELECT _id, _created_at, _updated_at, {_cell_list(schema)} "
            "FROM rows ORDER BY _id"
        )
        return _logical_rows(schema, cursor)


def get_rows_by_ids(
    table_dir: Path, schema: Sequence[Column], ids: Iterable[int]
) -> list[dict]:
    """The rows with these ``_id``s in :func:`dump_rows` form, ``_id``
    order, missing ids omitted. A schema column the table lacks is
    :class:`SchemaMismatchError`."""
    wanted = _check_ids(ids)
    with connect(table_dir) as conn, _busy_as_refusal():
        cursor = _select_rows(
            conn,
            f"SELECT _id, _created_at, _updated_at, {_cell_list(schema)} FROM rows "
            "WHERE _id IN (SELECT value FROM json_each(?)) ORDER BY _id",
            (_json(sorted(set(wanted))),),
        )
        return _logical_rows(schema, cursor)


_CHANGE_SUMMARY_FIELDS = (
    "id",
    "created_at",
    "source",
    "actor_user_id",
    "kind",
    "op",
    "summary",
    "schema_before",
    "schema_after",
    "size_bytes",
)
_CHANGE_JSON_FIELDS = ("row_ids", "before", "after")


def get_change(table_dir: Path, change_id: int) -> dict | None:
    """One change with ``row_ids``, ``before`` and ``after`` decoded, or
    None when it does not exist."""
    fields = (*_CHANGE_SUMMARY_FIELDS, *_CHANGE_JSON_FIELDS)
    with connect(table_dir) as conn, _busy_as_refusal():
        row = conn.execute(
            f"SELECT {', '.join(fields)} FROM changes WHERE id = ?", (change_id,)
        ).fetchone()
    if row is None:
        return None
    change = dict(zip(fields, row))
    for field in _CHANGE_JSON_FIELDS:
        change[field] = json.loads(change[field])
    return change


def _check_list_args(limit: object, before_id: object) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"limit must be an integer, got {limit!r}")
    if before_id is not None and (
        not isinstance(before_id, int) or isinstance(before_id, bool)
    ):
        raise ValueError(f"before_id must be an integer or None, got {before_id!r}")
    return max(1, min(limit, _LIST_CHANGES_MAX))


def _list_changes(
    conn: sqlite3.Connection, *, limit: int, before_id: int | None
) -> list[dict]:
    """The body of :func:`list_changes` on an open connection (arguments
    already checked). A restore entry whose ``after`` is not JSON, or an
    entry whose ``row_ids`` is malformed JSON, is ``corrupt_storage``."""
    sql = (
        f"SELECT {', '.join(_CHANGE_SUMMARY_FIELDS)}, CASE WHEN kind = 'restore' "
        "THEN json_extract(after, '$.restore_to') END, "
        "json_array_length(row_ids) FROM changes"
    )
    params: list[Any] = []
    if before_id is not None:
        sql += " WHERE id < ?"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise
        raise TableStorageError(
            "corrupt_storage", "The table's change log is invalid."
        ) from exc
    return [
        dict(zip((*_CHANGE_SUMMARY_FIELDS, "restore_to", "row_count"), row))
        for row in rows
    ]


def list_changes(
    table_dir: Path, *, limit: int = 50, before_id: int | None = None
) -> list[dict]:
    """Changes newest first, summary fields only (no images or row ids)
    plus ``restore_to`` (a restore entry's target id, else None) and
    ``row_count`` (how many row ids the change records);
    ``before_id`` (an int or None) pages to older entries. ``limit`` is
    clamped to 1.._LIST_CHANGES_MAX. The same page with ``in_effect``
    flags comes from :func:`chat.tables.revert.list_changes_in_effect`."""
    limit = _check_list_args(limit, before_id)
    with connect(table_dir) as conn, _busy_as_refusal():
        return _list_changes(conn, limit=limit, before_id=before_id)


# --- Download copy -----------------------------------------------------------


@contextmanager
def _attach_allowed(conn: sqlite3.Connection, filename: str) -> Iterator[None]:
    """Let ``conn`` ATTACH exactly ``filename`` until the block ends.

    ``VACUUM INTO <file>`` compiles to an ATTACH of ``<file>`` and a plain
    ``VACUUM`` to an ATTACH of ``""`` (a temporary database), so the
    deny-ATTACH authorizer refuses both; each runs inside this block,
    which still denies every other ATTACH and restores the plain guard on
    exit."""

    def authorizer(action: int, arg1, arg2, db_name, trigger) -> int:
        if action == sqlite3.SQLITE_ATTACH and arg1 == filename:
            return sqlite3.SQLITE_OK
        return _deny_attach_authorizer(action, arg1, arg2, db_name, trigger)

    conn.set_authorizer(authorizer)
    try:
        yield
    finally:
        conn.set_authorizer(_deny_attach_authorizer)


def copy_for_download(
    table_dir: Path, dest: Path, *, include_history: bool = False
) -> Path:
    """Write a self-contained single-file SQLite copy of the table to
    ``dest`` (``rows`` and ``meta``, plus ``changes`` when
    ``include_history``) and return ``dest``.

    ``dest`` must not exist (``copy_exists``, a symlink included) and its
    parent must be a real directory. The copy is opened without WAL and
    left in rollback-journal mode (no ``-wal`` / ``-shm`` file); a partial
    copy is removed on failure."""
    dest = Path(dest)
    if os.path.lexists(dest):
        raise TableStorageError("copy_exists", f"{dest.name} already exists.")
    _check_real_dir(dest.parent)
    created = False
    try:
        with connect(table_dir) as conn, _attach_allowed(conn, str(dest)):
            created = True
            conn.execute("VACUUM INTO ?", (str(dest),))
        copy = _open_db_file(dest, wal=False)
        try:
            if not include_history:
                copy.execute("DROP TABLE changes")
                copy.execute("DELETE FROM sqlite_sequence WHERE name = 'changes'")
            copy.execute("PRAGMA journal_mode=DELETE")
            if not include_history:
                with _attach_allowed(copy, ""):
                    copy.execute("VACUUM")
        finally:
            copy.close()
    except BaseException:
        if created:
            for path in (dest, *(dest.with_name(dest.name + s) for s in _SIDE_SUFFIXES)):
                with suppress(OSError):
                    path.unlink(missing_ok=True)
        raise
    return dest
