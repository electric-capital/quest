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
  row's ``_id`` is never handed out again: undo re-inserts a deleted row
  under its original ``_id``, and reuse would make a later insert collide
  with a pending undo and make a change's ``row_ids`` ambiguous.
- ``changes``: the change log (spec section 4.5), one entry per write in
  the same transaction as the write (spec invariant 6).
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
the connect. It sets WAL mode, the busy timeout and an authorizer that
denies ``ATTACH`` (the project database guard), as defense in depth even
though no model SQL ever reaches the file.

Writes run in one ``BEGIN IMMEDIATE`` transaction (:func:`_write_txn`):
schema match check, validation (every problem in every row collected),
caps, unique checks against the final state, the write, the change entry
and pruning. Any refusal rolls everything back, so nothing is written.
Values go through :func:`apply_default` (insert) or :func:`canonicalize`
(patches), then :func:`to_storage`; reads go through :func:`from_storage`.
Payload rows are keyed by column **name**; images, stored rows and
:func:`dump_rows` output are keyed by column **key**.

Row images (a ``rows`` change's ``before`` / ``after``) are lists of full
stored rows sorted by ``_id``: ``{"_id", "_created_at", "_updated_at",
<key>: <stored cell>}`` with every schema column present, cells in stored
form (booleans 0/1, multi-enums as their compact JSON string), so a rename
never breaks undo and replay is a plain INSERT or UPDATE.

The caps come from :mod:`chat.tables.constants` imported by name; this
module's globals are the patch point (tests monkeypatch
``chat.tables.storage.TABLE_MAX_ROWS`` and friends), read at call time.
"""

from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

from chat.tables.constants import (
    TABLE_CHANGE_MAX_BYTES,
    TABLE_CHANGE_MAX_COUNT,
    TABLE_CHANGE_RETENTION_DAYS,
    TABLE_DB_BUSY_TIMEOUT_S,
    TABLE_MAX_COLUMNS,
    TABLE_MAX_ROWS,
    TABLE_WRITE_MAX_ROWS,
)
from chat.tables.schema import (
    Column,
    TableValueError,
    apply_default,
    canonicalize,
    clip_repr,
    dump_schema,
    field_label,
    from_storage,
    is_valid_column_key,
    load_schema,
    to_storage,
)

TABLE_DB_NAME = "table.db"

# SQLite side files next to table.db; each must be a regular file if present.
_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")

# A write's ``source`` (also data_tables.last_write_source) starts with one
# of these, followed by a non-empty id.
SOURCE_PREFIXES = ("conversation:", "action_request:", "ui:", "revert:")

# Server-maintained columns of ``rows``.
ROW_ID = "_id"
RESERVED_COLUMNS = ("_id", "_created_at", "_updated_at")

_JSON_SEPARATORS = (",", ":")


# --- Errors ------------------------------------------------------------------


class TableStorageError(Exception):
    """A refusal from the table file layer. ``code`` is machine-readable,
    ``message`` is model/user-facing text (also ``str(err)``).

    Plain codes: ``unsafe_path``, ``storage_missing``, ``storage_exists``,
    ``corrupt_storage``, ``no_rows``, ``constraint_failed``,
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
    """The change's before/after images exceed ``TABLE_CHANGE_MAX_BYTES``."""

    def __init__(self, size_bytes: int, cap: int | None = None):
        cap = TABLE_CHANGE_MAX_BYTES if cap is None else cap
        super().__init__(
            "change_too_large",
            f"This write's change record would be {size_bytes} bytes, over the "
            f"{cap}-byte limit that keeps every change undoable. Split it into "
            "smaller writes.",
        )
        self.size_bytes = size_bytes
        self.cap = cap


class UpsertKeyError(TableStorageError):
    """The upsert ``key_column`` is unknown (``unknown_key_column``) or not
    unique (``key_not_unique``)."""


class SchemaMismatchError(TableStorageError):
    """The live table no longer matches the schema the caller passed."""

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
    the table after the commit. ``change_id``: None only when nothing was
    written (a where-delete that matched no row).
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


def _check_side_files(db: Path) -> None:
    for suffix in _SIDE_SUFFIXES:
        side = db.with_name(db.name + suffix)
        try:
            st = os.lstat(side)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise _unsafe(side.name, "not a regular file")


def _open_db_file(db: Path) -> sqlite3.Connection:
    """Open ``db`` through the guards described in the module docstring and
    return a configured connection (caller closes it)."""
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
        conn = sqlite3.connect(
            str(db),
            timeout=TABLE_DB_BUSY_TIMEOUT_S,
            isolation_level=None,
            check_same_thread=True,
        )
        try:
            conn.set_authorizer(_deny_attach_authorizer)
            after = os.stat(db)
            if (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
                raise _unsafe(db.name, "the file changed while it was opened")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(TABLE_DB_BUSY_TIMEOUT_S * 1000)}")
            conn.execute("PRAGMA synchronous=NORMAL")
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
def _write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``; ``ROLLBACK`` on any exception. A
    residual ``IntegrityError`` (the Python checks should have caught it) is
    re-raised as ``constraint_failed``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, sqlite3.IntegrityError):
            raise TableStorageError(
                "constraint_failed", f"The write broke a table constraint: {exc}"
            ) from exc
        raise


# --- Small helpers -----------------------------------------------------------


def _resolve_now(now: datetime | None) -> datetime:
    return datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)


def _utc_iso(now: datetime) -> str:
    """Fixed-width UTC ISO 8601 with microseconds and ``+00:00`` (sorts
    lexicographically in time order)."""
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
    """Undo a failed :func:`create_table_db` so a retry can start clean."""
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
                "before TEXT NOT NULL, after TEXT NOT NULL, reverted_by INTEGER, "
                "size_bytes INTEGER NOT NULL)"
            )
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
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
    with connect(table_dir) as conn:
        values = _meta_values(conn)
    try:
        return TableMeta(
            table_id=values["table_id"],
            title=values["title"],
            schema=load_schema(json.loads(values["schema"])),
            schema_version=int(values["schema_version"]),
        )
    except (KeyError, ValueError) as exc:
        raise TableStorageError(
            "corrupt_storage", "The table file's metadata is missing or invalid."
        ) from exc


# --- Change log --------------------------------------------------------------


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
) -> int:
    """Insert one ``changes`` entry and prune old ones; returns its id.
    Runs in the caller's transaction. ``before`` / ``after`` are JSON-ready
    images; ``size_bytes`` is their encoded size, and above
    ``TABLE_CHANGE_MAX_BYTES`` the change is refused with
    :class:`ChangeTooLargeError` before anything is inserted."""
    before_json = _json(before)
    after_json = _json(after)
    size_bytes = len(before_json.encode("utf-8")) + len(after_json.encode("utf-8"))
    if size_bytes > TABLE_CHANGE_MAX_BYTES:
        raise ChangeTooLargeError(size_bytes)
    cursor = conn.execute(
        "INSERT INTO changes (created_at, source, actor_user_id, kind, op, summary, "
        "schema_before, schema_after, row_ids, before, after, reverted_by, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
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
    change_id = cursor.lastrowid
    prune_changes(conn, now=now)
    return change_id


def prune_changes(conn: sqlite3.Connection, *, now: datetime) -> int:
    """Delete changes older than ``TABLE_CHANGE_RETENTION_DAYS`` before
    ``now``, then all but the newest ``TABLE_CHANGE_MAX_COUNT`` by id.
    Returns how many were deleted. Runs in the caller's transaction."""
    cutoff = _utc_iso(now - timedelta(days=TABLE_CHANGE_RETENTION_DAYS))
    deleted = conn.execute("DELETE FROM changes WHERE created_at < ?", (cutoff,)).rowcount
    deleted += conn.execute(
        "DELETE FROM changes WHERE id NOT IN "
        "(SELECT id FROM changes ORDER BY id DESC LIMIT ?)",
        (TABLE_CHANGE_MAX_COUNT,),
    ).rowcount
    return deleted


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
    """Collects invalid_rows problems and orders them by row, then schema
    column order (row-level problems first, unknown columns last)."""

    def __init__(self, schema: Sequence[Column]):
        self._slots = {c.name: i for i, c in enumerate(schema)}
        self._items: list[tuple[tuple[int, int, int], dict]] = []

    def add(self, row: int, column: str | None, code: str, reason: str) -> None:
        slot = -1 if column is None else self._slots.get(column, len(self._slots))
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
                index, name, "read_only_column", f"{name} is set by the server."
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
    final_rows: Sequence[tuple[int, Mapping[str, Any]]],
    touched_ids: Collection[int],
) -> None:
    """Check every unique column against the state after the write.

    ``final_rows`` pairs each written row's payload index with its final
    logical cell values keyed by column key; for an existing row this
    must include unique cells the patch leaves unchanged, since the row is
    excluded from the stored-row check. A missing key or None never
    conflicts. Reports ``duplicate_value`` on the later row of a pair
    inside the call, and on a row whose value an untouched stored row
    (not in ``touched_ids``) already holds, naming that row's ``_id``."""
    touched_json = _json(sorted(touched_ids))
    for column in schema:
        if not column.unique:
            continue
        seen: dict[Any, int] = {}
        for index, values in final_rows:
            value = to_storage(column, values.get(column.key))
            if value is None:
                continue
            if value in seen:
                problems.add(
                    index,
                    column.name,
                    "duplicate_value",
                    f"value {clip_repr(value)} is also used by row {seen[value]} "
                    "of this call.",
                )
                continue
            seen[value] = index
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
    with connect(table_dir) as conn, _write_txn(conn):
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
            after=_row_images(conn, schema, ids),
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
        change_id=change_id,
    )


# --- Reads -----------------------------------------------------------------


def _logical_rows(schema: Sequence[Column], cursor: sqlite3.Cursor) -> list[dict]:
    out = []
    for row in cursor.fetchall():
        item = {"_id": row[0], "_created_at": row[1], "_updated_at": row[2]}
        for column, stored in zip(schema, row[3:]):
            item[column.key] = from_storage(column, stored)
        out.append(item)
    return out


def row_count(table_dir: Path) -> int:
    """Number of rows in the table."""
    with connect(table_dir) as conn:
        return _count(conn)


def dump_rows(table_dir: Path, schema: Sequence[Column]) -> list[dict]:
    """Every row ordered by ``_id`` as ``{"_id", "_created_at",
    "_updated_at", <key>: logical value}`` with every schema column."""
    with connect(table_dir) as conn:
        cursor = conn.execute(
            f"SELECT _id, _created_at, _updated_at, {_cell_list(schema)} "
            "FROM rows ORDER BY _id"
        )
        return _logical_rows(schema, cursor)


def get_rows_by_ids(
    table_dir: Path, schema: Sequence[Column], ids: Iterable[int]
) -> list[dict]:
    """The rows with these ``_id``s in :func:`dump_rows` form, ``_id``
    order, missing ids omitted."""
    wanted = _check_ids(ids)
    with connect(table_dir) as conn:
        cursor = conn.execute(
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
    "reverted_by",
    "size_bytes",
)
_CHANGE_JSON_FIELDS = ("row_ids", "before", "after")


def get_change(table_dir: Path, change_id: int) -> dict | None:
    """One change with ``row_ids``, ``before`` and ``after`` decoded, or
    None when it does not exist (or was pruned)."""
    fields = (*_CHANGE_SUMMARY_FIELDS, *_CHANGE_JSON_FIELDS)
    with connect(table_dir) as conn:
        row = conn.execute(
            f"SELECT {', '.join(fields)} FROM changes WHERE id = ?", (change_id,)
        ).fetchone()
    if row is None:
        return None
    change = dict(zip(fields, row))
    for field in _CHANGE_JSON_FIELDS:
        change[field] = json.loads(change[field])
    return change


def list_changes(
    table_dir: Path, *, limit: int = 50, before_id: int | None = None
) -> list[dict]:
    """Changes newest first, summary fields only (no images or row ids);
    ``before_id`` pages to older entries."""
    sql = f"SELECT {', '.join(_CHANGE_SUMMARY_FIELDS)} FROM changes"
    params: list[Any] = []
    if before_id is not None:
        sql += " WHERE id < ?"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect(table_dir) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(zip(_CHANGE_SUMMARY_FIELDS, row)) for row in rows]


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
    parent must be a real directory. The copy is left in rollback-journal
    mode (no ``-wal`` file); a partial copy is removed on failure."""
    dest = Path(dest)
    if os.path.lexists(dest):
        raise TableStorageError("copy_exists", f"{dest.name} already exists.")
    _check_real_dir(dest.parent)
    created = False
    try:
        with connect(table_dir) as conn, _attach_allowed(conn, str(dest)):
            created = True
            conn.execute("VACUUM INTO ?", (str(dest),))
        copy = _open_db_file(dest)
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
