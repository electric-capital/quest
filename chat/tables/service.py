"""Quest Tables service layer: the ONE read/write path for table files.

The model-facing table tools, the table action requests (``create_table``,
``change_table_schema``, ``write_table_rows``) and the HTTP routes call the
functions below; nothing else opens a ``table.db``. The action requests'
precards preview with :func:`preview_row_write` /
:func:`preview_schema_change` (dry runs: nothing written, bumped or
published) and their executors pin what the card showed through
:func:`apply_row_write`'s ``expected_schema_version`` /
``expected_rows_digest`` (:func:`rows_digest`) and
:func:`apply_schema_change`'s ``expected_schema_version``. The HTTP routes also get the owner-only
metadata edit and share roster (:func:`update_metadata`,
:func:`add_share`, :func:`remove_share`), the editors-only history
(:func:`list_changes`, :func:`get_change`) and downloads
(:func:`export_table`) here. Every operation follows
the same pipeline in this one place::

    feature gate -> table lookup (db/table_store) -> resolve_table_access
      -> [per-table lock] re-fetch + re-resolve
      -> table file op (chat/tables/storage|filters|revert, in asyncio.to_thread)
      -> quest.db bump (table_store.update_after_write / update_schema)
      -> realtime events (chat/tables/events.py)

Errors
------

Refusals decided here are :class:`TableError` (``.code`` machine-readable,
``str(e)`` model/user-facing) or one of its subclasses:
:class:`TableDisabled` (``tables_disabled``, raised before any DB work),
:class:`TableNotFound` (``table_not_found``; a hidden table and a missing
id raise byte-equal text after the same DB work: one ``get_table`` plus
the same doc-source lookup), :class:`TableApprovalRequired`
(``approval_required``, carries the verdict and the ``write_table_rows``
suggested request), :class:`TableDenied` (``denied``, the verdict's deny
reason), :class:`TableForbidden` (``forbidden``, owner-only actions) and
:class:`StaleSchema` (``stale_schema``, carries the current table dict),
:class:`StaleUpdate` (``stale_update``, a metadata edit's
``expected_updated_at`` is stale; carries the current table dict)
and :class:`RowsChanged` (``rows_changed``, the rows a card showed changed
before its approval; carries their current values).
Other ``TableError`` codes: ``invalid_args``, ``project_not_found``,
``mode_mismatch``, ``duplicate_title``, ``table_limit``,
``invalid_request``, ``cannot_share_with_owner``, ``user_not_found``,
``share_not_found``. History
refusals for readers are :class:`TableForbidden` with
:data:`HISTORY_FORBIDDEN_MESSAGE`.

Errors from the layers below propagate UNCHANGED so the tools and routes
map them by their ``.code``: :class:`chat.tables.storage.TableStorageError`
and its subclasses (``invalid_rows``, ``too_many_rows``, ``table_full``,
``change_too_large``, ``unknown_key_column`` / ``key_not_unique``,
``schema_mismatch``, ``rows_violate``, ``busy``, ``no_rows``,
``invalid_selector``, ``unknown_change``, ``corrupt_storage`` ...),
:class:`chat.tables.filters.FilterError` and
:class:`chat.tables.schema.TableSchemaError` (column specs and schema
operations). None of them is a :class:`TableError`.

The per-table lock
------------------

Each table id has one ``asyncio.Lock`` (single event loop per process).
It is held by every reader AND every writer of ``table.db`` for the whole
file call: :func:`chat.tables.revert.restore_to` swaps the file in with
``os.replace`` and requires that no other connection is open at that
moment (an open reader could leave a WAL the new file would inherit, a
writer committing between its re-check and the rename would be lost).
Storage functions open and close one connection per call, so holding the
lock across the call is enough. Writers also hold it through the quest.db
bump and the events, so ``row_count`` / ``schema_version`` /
``updated_at`` land in write order. Every lock holder first resolves access
outside the lock (a hidden or denied table never takes or creates a lock)
and then re-fetches the row and re-resolves under it (a share revoked or
a schema changed while the call queued changes the outcome). Writes,
restores and deletes are shielded from cancellation (the Stop button
mid-turn) so table.db and quest.db never disagree. If a schema change's
or restore's quest.db bump is lost anyway (``update_schema`` failed after
table.db committed), the next call under the lock hits the schema
version check and records table.db's schema in quest.db
(:func:`_heal_lost_schema_bump`) before refusing with ``schema_mismatch``,
so a retry works. The project-delete and
account-delete directory sweeps (:func:`delete_table_dirs`) run after the
rows are gone without the lock; a call still queued on the lock then fails
its re-fetch with :class:`TableNotFound`.

Realtime publishers run on the event loop only, never inside
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import time
import hashlib
import json
import logging
import os
import shutil
import stat
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from chat.storage import ChatStorage
from chat.tables import filters, revert, schema_ops, storage
from chat.tables.access import (
    TableAccess,
    approval_required_message,
    not_found_message,
    resolve_table_access,
)
from chat.tables.constants import (
    TABLE_SLOW_READ_LOG_S,
    table_not_found_message,
    tables_disabled_message,
)
from chat.tables.schema import (
    Column,
    dump_schema,
    from_storage,
    load_schema,
    parse_columns,
)
from config import feature_gates
from db import table_store

logger = logging.getLogger(__name__)

__all__ = [
    "ROW_OPS",
    "TABLE_SCOPES",
    "Caller",
    "HISTORY_FORBIDDEN_MESSAGE",
    "RowsChanged",
    "StaleSchema",
    "StaleUpdate",
    "TableApprovalRequired",
    "TableDenied",
    "TableDisabled",
    "TableError",
    "TableForbidden",
    "TableNotFound",
    "add_share",
    "apply_restore",
    "apply_row_write",
    "apply_schema_change",
    "caller_for_ui",
    "create_table",
    "delete_table",
    "delete_table_dirs",
    "export_table",
    "get_change",
    "get_change_entry",
    "get_visible_table",
    "list_changes",
    "list_visible_tables",
    "preview_row_write",
    "preview_schema_change",
    "query_rows",
    "remove_share",
    "require_enabled",
    "resolve",
    "rows_digest",
    "sample_rows",
    "update_metadata",
]

ROW_OPS = ("insert", "update", "upsert", "delete")
# list_visible_tables scopes, as docs: ``user`` = user tables, ``project``
# = this conversation's project tables (from a standalone conversation:
# the tables of all of the user's own projects, read-only), ``public`` =
# the public tables readable from here (a private project's doc sources;
# a public conversation's own project), ``all`` = everything visible.
TABLE_SCOPES = ("user", "project", "public", "all")
SAMPLE_ROWS_DEFAULT = 5


# ---------------------------------------------------------------------------
# Errors and the caller
# ---------------------------------------------------------------------------


class TableError(Exception):
    """A table operation was refused. ``code`` is machine-readable,
    ``str(e)`` (also ``message``) is model/user-facing."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TableDisabled(TableError):
    """The ``tables`` feature gate is closed for the user."""

    def __init__(self) -> None:
        super().__init__("tables_disabled", tables_disabled_message())


class TableNotFound(TableError):
    """Missing OR hidden table: the same text either way (invariant 2)."""

    def __init__(self, table_id: str):
        super().__init__("table_not_found", table_not_found_message(table_id))
        self.table_id = table_id


class TableApprovalRequired(TableError):
    """The row write needs a ``write_table_rows`` action request.

    ``access`` is the verdict; ``suggested_request`` is ``{"request_type":
    "write_table_rows", "params": {"table_id", "operation", ...params}}``.
    """

    def __init__(self, access: TableAccess, suggested_request: Optional[dict] = None):
        super().__init__("approval_required", approval_required_message(access))
        self.access = access
        self.suggested_request = suggested_request


class TableDenied(TableError):
    """The verdict denies the action; ``str(e)`` is the deny reason."""

    def __init__(self, reason: str):
        super().__init__("denied", reason)


class TableForbidden(TableError):
    """An owner-only action by someone who can see but does not own the table."""

    def __init__(self, message: str):
        super().__init__("forbidden", message)


class StaleSchema(TableError):
    """``expected_schema_version`` no longer matches; ``current`` is the
    current table dict (with shares)."""

    def __init__(self, current: dict):
        super().__init__(
            "stale_schema",
            "The table's columns were changed by someone else; reload the "
            "table and try again.",
        )
        self.current = current


class StaleUpdate(TableError):
    """``expected_updated_at`` no longer matches (metadata edit); ``current``
    is the current table dict (with shares)."""

    def __init__(self, current: dict):
        super().__init__(
            "stale_update",
            "The table was changed by someone else; reload it and try again.",
        )
        self.current = current


# Shown to readers asking for a table's history (editors only, like docs:
# before images can hold rows the owner deleted before sharing).
HISTORY_FORBIDDEN_MESSAGE = "History is available to people who can edit this table."


# Most rows a RowsChanged message spells out.
ROWS_CHANGED_MESSAGE_MAX = 10
# Longest stretch of one row's values in a RowsChanged message.
_ROWS_CHANGED_ROW_CHARS = 300


class RowsChanged(TableError):
    """The rows a ``write_table_rows`` card showed changed before its
    approval (``expected_rows_digest`` no longer matches): a row's value
    changed, a row vanished, or the write now touches other rows (a filter
    delete matching another row, an upsert key now matching a stored row).

    ``op`` is the row op; ``by_filter`` is True for a filter delete.
    ``current_rows`` are the rows the write would touch now, before it
    (``{"_id", <column name>: logical value}``); ``vanished_ids`` the
    ``_id``\\ s the request names that no longer exist. ``str(e)`` names
    their current values (at most :data:`ROWS_CHANGED_MESSAGE_MAX` rows),
    e.g. ``Row 7 now: {"Status": "Done"}`` / ``Row 9 no longer exists``.
    When no row is left to show it says so per op: an upsert "now matches
    no stored rows (it would insert them instead)", a filter delete "now
    matches no rows".
    """

    def __init__(
        self,
        op: str,
        current_rows: list[dict],
        vanished_ids: Sequence[int] = (),
        *,
        by_filter: bool = False,
    ):
        parts = [f"Row {row_id} no longer exists" for row_id in vanished_ids]
        for row in current_rows:
            values = {k: v for k, v in row.items() if k != "_id"}
            text = json.dumps(values, ensure_ascii=False)
            if len(text) > _ROWS_CHANGED_ROW_CHARS:
                text = text[:_ROWS_CHANGED_ROW_CHARS] + "…"
            parts.append(f"Row {row['_id']} now: {text}")
        shown = parts[:ROWS_CHANGED_MESSAGE_MAX]
        if len(parts) > len(shown):
            shown.append(f"and {len(parts) - len(shown)} more")
        if shown:
            detail = "; ".join(shown)
        elif op == "upsert":
            detail = "The upsert now matches no stored rows (it would insert them instead)"
        elif op == "delete" and by_filter:
            detail = "The filter now matches no rows"
        else:
            detail = "The write now touches no rows"
        super().__init__(
            "rows_changed",
            "The rows changed since this write was proposed; nothing was "
            f"written. {detail}.",
        )
        self.op = op
        self.by_filter = by_filter
        self.current_rows = current_rows
        self.vanished_ids = list(vanished_ids)


@dataclass(frozen=True)
class Caller:
    """Who is asking, from which kind of run.

    ``run_kind`` is one of ``chat.tables.access.RUN_KINDS`` (incl. ``"ui"``).
    Sandbox scripts and the UI have no conversation context:
    ``project_id=None``, ``is_public=False`` (scripts and the UI also pass
    ``conversation_id=None``).
    """

    user: dict
    conversation_id: Optional[str]
    project_id: Optional[str]
    is_public: bool
    run_kind: str


def caller_for_ui(user: dict) -> Caller:
    """The caller of an HTTP route (``run_kind="ui"``)."""
    return Caller(user=user, conversation_id=None, project_id=None,
                  is_public=False, run_kind="ui")


def require_enabled(caller: Caller) -> None:
    """Raise :class:`TableDisabled` unless the tables gate is open for the user."""
    email = (caller.user or {}).get("email") or ""
    if not feature_gates.tables_enabled_for(email):
        raise TableDisabled()


# ---------------------------------------------------------------------------
# Helpers: access, sources, the lock, events
# ---------------------------------------------------------------------------


def _has_doc_sources(caller: Caller) -> bool:
    """Only a conversation of a private project has doc sources."""
    return bool(
        caller.project_id
        and not caller.is_public
        and caller.run_kind not in ("script", "ui")
    )


async def _doc_sources(caller: Caller) -> frozenset[str]:
    """The public projects whose public tables this caller's project may
    read (``project_doc_sources``, "Docs & Tables access"); empty for every
    caller that is not a private project conversation."""
    if not _has_doc_sources(caller):
        return frozenset()
    from db import project_store
    return frozenset(await project_store.list_doc_source_project_ids(caller.project_id))


def _reaches_own_projects(caller: Caller) -> bool:
    """Whether this caller is a standalone conversation, which reads the
    tables of all of the user's own projects (docs access rule 2): no
    project, not public, not a script and not the UI."""
    return bool(
        caller.project_id is None
        and not caller.is_public
        and caller.run_kind not in ("script", "ui")
    )


def resolve(
    caller: Caller, table: dict, doc_source_project_ids: frozenset[str] = frozenset(),
) -> TableAccess:
    """``resolve_table_access`` for this caller (``table`` with shares)."""
    return resolve_table_access(
        table,
        user_id=caller.user["id"],
        is_public=caller.is_public,
        project_id=caller.project_id,
        run_kind=caller.run_kind,
        doc_source_project_ids=doc_source_project_ids,
    )


def _check_table_id(table_id) -> None:
    if not isinstance(table_id, str) or not table_id.strip():
        raise TableError("invalid_args", "table_id must be a non-empty string.")


async def _lookup(caller: Caller, table_id) -> tuple[dict, TableAccess]:
    """Lookup + verdict; a missing or hidden table raises TableNotFound.

    The doc-source lookup runs before the None check, so a hidden table
    and a nonexistent id cost the same DB work (invariant 2)."""
    _check_table_id(table_id)
    table = await table_store.get_table(table_id, with_shares=True)
    sources = await _doc_sources(caller)
    access = resolve(caller, table, sources) if table is not None else None
    if not_found_message(table_id, access) is not None:
        raise TableNotFound(table_id)
    return table, access


def _table_dir(table_id: str) -> Path:
    return ChatStorage.get_table_dir(table_id)


# Per-table asyncio locks (module docstring). Weak values: holders and
# waiters keep a strong reference, so the map does not grow with every
# table id ever touched.
_table_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


def _table_lock(key: str) -> asyncio.Lock:
    lock = _table_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _table_locks[key] = lock
    return lock


async def _shielded(coro):
    """Run ``coro`` to completion even if the awaiting task is cancelled.

    A write is file op -> quest.db bump -> events; cancelling between the
    first two would leave ``row_count`` / ``schema_version`` stale and no
    event sent. The inner task keeps running; the cancellation still
    propagates to the caller.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        def _log_done(t: "asyncio.Future") -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.warning(
                    "[tables] write finished with an error after the caller "
                    "was cancelled: %s", t.exception(),
                )
        task.add_done_callback(_log_done)
        raise


async def _finish_before_cancel(coro):
    """Run ``coro`` to completion; if the awaiting task is cancelled, wait
    for ``coro`` to finish first and only then raise ``CancelledError``.

    Unlike :func:`_shielded` (which returns to the cancelled caller at
    once), the caller does not resume until the work is done: a download
    route that removes its temp directory on cancellation must not do so
    while a thread still writes into it, and the table lock must not be
    released while ``VACUUM INTO`` still reads ``table.db``."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "[tables] work finished with an error after the caller was "
                "cancelled: %s", task.exception(),
            )
        raise asyncio.CancelledError
    return task.result()


def _events():
    """chat/tables/events.py, imported lazily: it pulls in chat.realtime,
    whose package import reaches chat.gemini_api, which imports the table
    tool handlers -- and so this module."""
    from chat.tables import events
    return events


def _write_source(write_source: Optional[str], caller: Caller) -> str:
    """``write_source`` when given, else ``conversation:<id>``, else
    ``ui:<user_id>`` for the UI. Any other caller must pass one (the
    storage layer only accepts the documented source prefixes)."""
    if write_source:
        return write_source
    if caller.conversation_id:
        return f"conversation:{caller.conversation_id}"
    if caller.run_kind == "ui":
        return f"ui:{caller.user['id']}"
    raise TableError("invalid_args", "write_source is required for this caller.")


def _actor(actor_user_id: Optional[int], caller: Caller) -> int:
    return caller.user["id"] if actor_user_id is None else actor_user_id


async def _file_call(table: dict, fn, *args, **kwargs):
    """Run one table.db call in a thread (the caller holds the table lock).

    A :class:`chat.tables.storage.SchemaMismatchError` first goes through
    :func:`_heal_lost_schema_bump`, then propagates unchanged (its text
    already says "reload the table and retry")."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except storage.SchemaMismatchError:
        await _heal_lost_schema_bump(table)
        raise


async def _heal_lost_schema_bump(table: dict) -> None:
    """Record in quest.db a schema change or restore whose quest.db bump
    was lost (table.db committed, then ``update_schema`` failed or the
    process died). Every table.db call checks the schema version, so
    without this the table would refuse every read and write for good.

    Only the one unambiguous case is healed: table.db's ``meta`` is exactly
    one version ahead of quest.db (a lost bump blocks every later change,
    so it can never be more). quest.db then takes table.db's schema, row
    count and the newest change's source, and the audience is told.
    Best-effort: any failure is logged and the caller's error stands.
    Lock held; event loop only."""
    table_dir = _table_dir(table["id"])
    try:
        meta = await asyncio.to_thread(storage.read_meta, table_dir)
        if meta.schema_version != table["schema_version"] + 1:
            return
        count = await asyncio.to_thread(storage.row_count, table_dir)
        newest = await asyncio.to_thread(storage.list_changes, table_dir, limit=1)
        source = newest[0]["source"] if newest else table.get("last_write_source")
        updated = await table_store.update_schema(
            table["id"],
            schema=dump_schema(meta.schema),
            expected_schema_version=table["schema_version"],
            row_count=count,
            last_write_source=source,
        )
    except Exception:
        logger.warning(
            "[tables] could not reconcile quest.db with table.db for %s",
            table["id"], exc_info=True,
        )
        return
    if updated is None:
        return
    logger.warning(
        "[tables] recorded a lost schema bump for %s (schema_version %s -> %s)",
        table["id"], table["schema_version"], updated["schema_version"],
    )
    _events().publish_table_write(updated)


# ---------------------------------------------------------------------------
# Visibility + reads
# ---------------------------------------------------------------------------


async def get_visible_table(caller: Caller, table_id: str) -> tuple[dict, TableAccess]:
    """Return ``(table, access)`` for a table this caller may see. No file
    IO, no lock.

    Raises:
        TableDisabled, TableNotFound (missing OR hidden), TableError
        ``invalid_args`` (not a non-empty string).
    """
    require_enabled(caller)
    return await _lookup(caller, table_id)


async def list_visible_tables(
    caller: Caller, *, scope: str = "all", limit: Optional[int] = None,
) -> list[tuple[dict, TableAccess]]:
    """Visible tables in ``scope`` (:data:`TABLE_SCOPES`), newest-updated
    first, at most ``limit`` (None = all). No file IO.

    Candidates come from ``table_store.list_accessible_tables`` (owned or
    shared with the caller): user tables (never for a public conversation:
    user tables are always private), this conversation's project tables
    (from a standalone conversation: the tables of all of the user's own
    projects, read-only), and the public tables of the doc-source projects
    (a private project conversation). Each candidate goes through the access rule; pages
    through the store's keyset cursor so hidden rows never eat into
    ``limit``. Payload shaping and limit clamping are the tool's job.
    """
    require_enabled(caller)
    if scope is None:
        scope = "all"
    if scope not in TABLE_SCOPES:
        raise TableError("invalid_args", f"scope must be one of: {', '.join(TABLE_SCOPES)}.")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise TableError("invalid_args", "limit must be a positive integer.")

    include_user = scope in ("user", "all") and not caller.is_public
    include_project = bool(caller.project_id) and (
        scope in ("project", "all") or (scope == "public" and caller.is_public)
    )
    include_own_projects = scope in ("project", "all") and _reaches_own_projects(caller)
    sources = await _doc_sources(caller) if scope in ("public", "all") else frozenset()
    if not include_user and not include_project and not include_own_projects and not sources:
        return []

    out: list[tuple[dict, TableAccess]] = []
    before = None
    while True:
        rows = await table_store.list_accessible_tables(
            caller.user["id"],
            project_id=caller.project_id,
            include_user_tables=include_user,
            include_project_tables=include_project,
            include_owned_project_tables=include_own_projects,
            source_project_ids=sources,
            mode="public" if caller.is_public else None,
            limit=limit,
            before=before,
        )
        for row in rows:
            access = resolve(caller, row, sources)
            if not access.visible:
                continue
            out.append((row, access))
            if limit is not None and len(out) >= limit:
                return out
        if limit is None or len(rows) < limit:
            return out
        last = rows[-1]
        before = (datetime.fromisoformat(last["updated_at"]), last["id"])


def _caller_email(caller: Caller) -> str:
    return (caller.user or {}).get("email") or ""


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


async def _locked_read(caller: Caller, table_id: str, fn):
    """Gate + lookup outside the lock, then under the lock re-fetch,
    re-resolve and run ``fn(table_dir, columns, table)`` in a thread.
    Returns ``(table, access, result)``.

    Diagnostics (the ``chat`` logger, ``[tables]`` prefix): a file call
    that raises logs ``read of <id> failed`` at WARNING with the error
    code, the lock wait and call durations, the caller and
    :func:`storage.describe_table_dir` of the directory (what was on disk
    at that moment); a read that succeeds but whose lock wait or file call
    passed ``TABLE_SLOW_READ_LOG_S`` logs ``slow read of <id>`` the same
    way. The exception propagates unchanged either way."""
    require_enabled(caller)
    await _lookup(caller, table_id)  # hidden / missing never take a lock
    lock_requested = time.monotonic()
    async with _table_lock(table_id):
        lock_wait = time.monotonic() - lock_requested
        table, access = await _lookup(caller, table_id)
        columns = load_schema(table["schema"])
        table_dir = _table_dir(table["id"])
        started = time.monotonic()
        try:
            result = await _file_call(table, fn, table_dir, columns, table)
        except BaseException as exc:
            logger.warning(
                "[tables] read of %s failed: %s (%s) lock_wait=%dms call=%dms %s user=%s",
                table["id"], getattr(exc, "code", type(exc).__name__), exc,
                _ms(lock_wait), _ms(time.monotonic() - started),
                storage.describe_table_dir(table_dir), _caller_email(caller),
            )
            raise
        call = time.monotonic() - started
        if lock_wait >= TABLE_SLOW_READ_LOG_S or call >= TABLE_SLOW_READ_LOG_S:
            logger.warning(
                "[tables] slow read of %s: lock_wait=%dms call=%dms %s user=%s",
                table["id"], _ms(lock_wait), _ms(call),
                storage.describe_table_dir(table_dir), _caller_email(caller),
            )
        return table, access, result


async def query_rows(
    caller: Caller,
    table_id: str,
    *,
    filter: Any = None,  # noqa: A002 - the DSL's own name
    sort: Any = None,
    limit: Any = None,
    offset: Any = None,
    columns: Optional[list] = None,
    count_only: bool = False,
    max_limit: Optional[int] = None,
) -> tuple[dict, TableAccess, filters.QueryResult]:
    """Run a filtered, sorted, paged query (:func:`filters.run_query`)
    under the table lock. ``max_limit`` defaults to ``TABLE_QUERY_MAX_ROWS``
    (the grid route passes ``TABLE_GRID_MAX_LIMIT``). Rows are
    ``{"_id", "_updated_at", <name>: value}``.

    Raises:
        TableDisabled, TableNotFound, FilterError, TableStorageError.
    """
    def _run(table_dir: Path, schema, table: dict) -> filters.QueryResult:
        kwargs = {} if max_limit is None else {"max_limit": max_limit}
        return filters.run_query(
            table_dir, schema,
            filter=filter, sort=sort, limit=limit, offset=offset,
            columns=columns, count_only=count_only,
            schema_version=table["schema_version"], **kwargs,
        )

    return await _locked_read(caller, table_id, _run)


async def sample_rows(
    caller: Caller, table_id: str, n: int = SAMPLE_ROWS_DEFAULT,
) -> tuple[dict, TableAccess, list[dict]]:
    """The first ``n`` rows by ``_id`` (the query row shape), with the
    table and the verdict: everything ``get_table`` needs in one locked
    call. Raises like :func:`query_rows`."""
    result_table, access, result = await query_rows(caller, table_id, limit=n)
    return result_table, access, result.rows


# ---------------------------------------------------------------------------
# Row writes
# ---------------------------------------------------------------------------


def _clean_row_params(op: str, params: dict) -> dict:
    """Shape-check ``params`` for ``op`` (storage validates the content)."""
    if op not in ROW_OPS:
        raise TableError("invalid_args", f"operation must be one of: {', '.join(ROW_OPS)}.")
    if not isinstance(params, dict):
        raise TableError("invalid_args", "params must be an object.")
    if op == "insert":
        return {"rows": params.get("rows")}
    if op == "update":
        return {"updates": params.get("updates")}
    if op == "upsert":
        key_column = params.get("key_column")
        if not isinstance(key_column, str) or not key_column:
            raise TableError("invalid_args", "key_column must be a column name.")
        return {"key_column": key_column, "rows": params.get("rows")}
    has_ids = params.get("row_ids") is not None
    has_filter = params.get("filter") is not None
    if has_ids == has_filter:
        raise TableError("invalid_args", "Give exactly one of row_ids or filter.")
    return {"row_ids": params["row_ids"]} if has_ids else {"filter": params["filter"]}


def _suggested_request(table_id: str, op: str, clean: dict) -> dict:
    return {
        "request_type": "write_table_rows",
        "params": {"table_id": table_id, "operation": op, **clean},
    }


def _check_rows_not_denied(access: TableAccess) -> None:
    if access.rows == "denied":
        raise TableDenied(access.deny_reason)


def _run_row_op(
    table_dir: Path, columns, op: str, clean: dict, *,
    schema_version: int, source: str, actor_user_id: int, dry_run: bool = False,
) -> storage.RowWriteResult:
    common = dict(schema_version=schema_version, source=source,
                  actor_user_id=actor_user_id, dry_run=dry_run)
    if op == "insert":
        return storage.insert_rows(table_dir, columns, rows=clean["rows"], **common)
    if op == "update":
        return storage.update_rows(table_dir, columns, updates=clean["updates"], **common)
    if op == "upsert":
        return storage.upsert_rows(
            table_dir, columns, key_column=clean["key_column"], rows=clean["rows"], **common,
        )
    if "row_ids" in clean:
        return storage.delete_rows(table_dir, columns, row_ids=clean["row_ids"], **common)
    where = filters.compile_filter(columns, clean["filter"])
    return storage.delete_rows(table_dir, columns, where=where, **common)


def rows_digest(images: Iterable[dict]) -> str:
    """sha256 hex digest of row images (stored form, keyed by column key):
    each row's ``_id`` and stored cells, timestamps left out, as canonical
    JSON sorted by ``_id``. Equal over unchanged rows; differs when any
    row's cell changes, a row disappears or the set of rows changes.

    :func:`preview_row_write` computes it over a dry run's before images
    (``rows_digest``) and :func:`apply_row_write` recomputes it at approve
    time (``expected_rows_digest``) with this same function."""
    rows = sorted(
        (
            {k: v for k, v in image.items() if k not in ("_created_at", "_updated_at")}
            for image in images
        ),
        key=lambda row: row["_id"],
    )
    canonical = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _logical_images(columns: Sequence[Column], images: Iterable[dict]) -> list[dict]:
    """Row images as ``{"_id", <column name>: logical value}`` (timestamps
    dropped), for previews and :class:`RowsChanged`."""
    out = []
    for image in images:
        row: dict[str, Any] = {"_id": image.get("_id")}
        for column in columns:
            row[column.name] = from_storage(column, image.get(column.key))
        out.append(row)
    return out


def _named_ids(op: str, clean: dict) -> list[int]:
    """The ``_id``\\ s a request names explicitly (update ``updates``,
    delete ``row_ids``); valid integers only."""
    if op == "update" and isinstance(clean.get("updates"), (list, tuple)):
        ids = [u.get("_id") for u in clean["updates"] if isinstance(u, dict)]
    elif op == "delete" and isinstance(clean.get("row_ids"), (list, tuple)):
        ids = list(clean["row_ids"])
    else:
        return []
    return [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]


def _row_result(result: storage.RowWriteResult, table: dict) -> dict:
    return {
        "op": result.op,
        "inserted": result.inserted,
        "updated": result.updated,
        "deleted": result.deleted,
        "row_ids": list(result.row_ids),
        "inserted_ids": list(result.inserted_ids),
        "updated_ids": list(result.updated_ids),
        "change_id": result.change_id,
        "row_count": result.row_count,
        "updated_at": table["updated_at"],
        "schema_version": table["schema_version"],
    }


async def apply_row_write(
    caller: Caller,
    table_id: str,
    op: str,
    params: dict,
    *,
    write_source: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    bypass_approval: bool = False,
    expected_schema_version: Optional[int] = None,
    expected_rows_digest: Optional[str] = None,
) -> dict:
    """Run one row write (``op`` in :data:`ROW_OPS`).

    ``params`` are the tool args minus ``table_id``: insert ``{rows}``,
    update ``{updates}``, upsert ``{key_column, rows}``, delete
    ``{row_ids}`` or ``{filter}`` (exactly one; a filter is compiled
    against the schema under the lock). The change's source is
    ``write_source`` or ``conversation:<id>`` (``ui:<user_id>`` for the
    UI); ``actor_user_id`` defaults to the caller.

    The ``write_table_rows`` executor (A13) calls this at approve time with
    ``bypass_approval=True``: an ``approval`` verdict then proceeds, while
    hidden/missing (the share was revoked or the table deleted meanwhile)
    and ``denied`` still refuse. Without it, an ``approval`` verdict first
    dry-runs the write under the lock (storage ``dry_run=True``: every
    check against the live schema and rows, nothing written), so invalid
    rows raise the same storage error as on the free path, and only a
    write that would succeed raises :class:`TableApprovalRequired` with
    its suggested request.

    The executor also pins what the card showed, checked under the lock
    after the re-fetch: ``expected_schema_version`` (the version at
    proposal) must equal the table's, else :class:`StaleSchema`; with
    ``expected_rows_digest`` (the preview's ``rows_digest``) the write is
    first dry-run in the same lock hold and :func:`rows_digest` of its
    before images must match, else :class:`RowsChanged` (a request naming
    an ``_id`` that no longer exists raises it too, instead of storage's
    ``unknown_row``). ``expected_rows_digest`` needs
    ``expected_schema_version`` (``invalid_args`` otherwise): the digest
    covers row ids and stored cells keyed by column key, not the schema,
    so a schema change in between (a renamed option, a changed rule)
    would go unnoticed without the version pin. Every other validation error of the live schema and
    rows (an enum option removed meanwhile, a unique clash) raises the
    storage error unchanged. The real write then runs in the same lock
    hold.

    Returns:
        ``{op, inserted, updated, deleted, row_ids, inserted_ids,
        updated_ids, change_id, row_count, updated_at, schema_version}``
        (``change_id`` None only for a filter delete that matched nothing;
        nothing is bumped or published then).

    Raises:
        TableDisabled, TableNotFound, TableDenied, TableApprovalRequired,
        StaleSchema, RowsChanged, TableError ``invalid_args``, FilterError,
        TableStorageError.
    """
    require_enabled(caller)
    clean = _clean_row_params(op, params)
    if expected_schema_version is not None and (
        isinstance(expected_schema_version, bool)
        or not isinstance(expected_schema_version, int)
    ):
        raise TableError("invalid_args", "expected_schema_version must be an integer.")
    if expected_rows_digest is not None and not isinstance(expected_rows_digest, str):
        raise TableError("invalid_args", "expected_rows_digest must be a string.")
    if expected_rows_digest is not None and expected_schema_version is None:
        # The digest does not cover the schema; only the version pin does.
        raise TableError(
            "invalid_args",
            "expected_rows_digest needs expected_schema_version: the digest "
            "does not cover the table's schema.",
        )
    table, access = await _lookup(caller, table_id)
    _check_rows_not_denied(access)
    source = _write_source(write_source, caller)
    actor = _actor(actor_user_id, caller)

    async def _locked() -> dict:
        async with _table_lock(table["id"]):
            fresh, fresh_access = await _lookup(caller, table_id)
            _check_rows_not_denied(fresh_access)
            if (
                expected_schema_version is not None
                and expected_schema_version != fresh["schema_version"]
            ):
                raise StaleSchema(fresh)
            needs_card = fresh_access.rows == "approval" and not bypass_approval
            columns = load_schema(fresh["schema"])
            if expected_rows_digest is not None:
                await _check_rows_digest(
                    fresh, columns, op, clean, expected_rows_digest,
                    source=source, actor_user_id=actor,
                )
            result = await _file_call(
                fresh, _run_row_op, _table_dir(fresh["id"]), columns, op, clean,
                schema_version=fresh["schema_version"], source=source,
                actor_user_id=actor, dry_run=needs_card,
            )
            if needs_card:
                raise TableApprovalRequired(
                    fresh_access, _suggested_request(fresh["id"], op, clean),
                )
            out = _row_result(result, fresh)
            if result.change_id is None:
                return out
            updated = await table_store.update_after_write(
                fresh["id"], row_count=result.row_count, last_write_source=source,
            )
            if updated is None:
                raise TableNotFound(table_id)
            _events().publish_table_write(updated)
            out["updated_at"] = updated["updated_at"]
            return out

    return await _shielded(_locked())


async def _check_rows_digest(
    table: dict, columns, op: str, clean: dict, expected: str, *,
    source: str, actor_user_id: int,
) -> None:
    """Dry-run the write and compare :func:`rows_digest` of its before
    images with ``expected``; :class:`RowsChanged` on a mismatch or when a
    named ``_id`` vanished. Lock held."""
    table_dir = _table_dir(table["id"])
    try:
        dry = await _file_call(
            table, _run_row_op, table_dir, columns, op, clean,
            schema_version=table["schema_version"], source=source,
            actor_user_id=actor_user_id, dry_run=True,
        )
    except storage.InvalidRowsError as exc:
        if not exc.problems or any(p["code"] != "unknown_row" for p in exc.problems):
            raise
        named = _named_ids(op, clean)
        alive = await _file_call(table, storage.get_rows_by_ids, table_dir, columns, named)
        alive_ids = {row["_id"] for row in alive}
        current = [
            {"_id": row["_id"], **{c.name: row[c.key] for c in columns}} for row in alive
        ]
        raise RowsChanged(
            op, current, vanished_ids=sorted({i for i in named if i not in alive_ids}),
        ) from None
    if rows_digest(dry.before_images) != expected:
        raise RowsChanged(
            op, _logical_images(columns, dry.before_images),
            by_filter=op == "delete" and "filter" in clean,
        )


async def preview_row_write(
    caller: Caller, table_id: str, op: str, params: dict,
) -> tuple[dict, TableAccess, dict]:
    """Dry-run one row write under the table lock and return ``(table,
    access, result)``: ``result`` has :func:`apply_row_write`'s shape with
    ``change_id`` None (the ids an insert would assign, the rows an update,
    upsert or delete would touch, the row count after). Every check of the
    real write runs against the live schema and rows and nothing is
    written, bumped or published (except that a ``schema_mismatch`` may
    first let :func:`_heal_lost_schema_bump` record an earlier lost schema
    bump in quest.db, as on every table.db call). The ``write_table_rows``
    precard (A13) uses it.

    ``result`` also carries the change's row images for the card:
    ``before_rows`` / ``after_rows`` (``{"_id", <column name>: logical
    value}``, ``_id`` order, timestamps dropped; insert: ``[]`` / the new
    rows under the ids the dry run assigned; update: the touched rows
    before / after; upsert: the pre-existing matched rows only / every
    written row; delete: the rows to delete / ``[]``) and ``rows_digest``
    (:func:`rows_digest` of the before images), which the executor passes
    back as ``apply_row_write(expected_rows_digest=...)``.

    Any row verdict but ``denied`` may preview (``approval`` included).

    Raises:
        TableDisabled, TableNotFound (missing OR hidden), TableDenied,
        TableError ``invalid_args``, FilterError, TableStorageError.
    """
    require_enabled(caller)
    clean = _clean_row_params(op, params)
    table, access = await _lookup(caller, table_id)
    _check_rows_not_denied(access)
    async with _table_lock(table["id"]):
        fresh, fresh_access = await _lookup(caller, table_id)
        _check_rows_not_denied(fresh_access)
        columns = load_schema(fresh["schema"])
        result = await _file_call(
            fresh, _run_row_op, _table_dir(fresh["id"]), columns, op, clean,
            schema_version=fresh["schema_version"], source="ui:dry-run",
            actor_user_id=caller.user["id"], dry_run=True,
        )
        out = _row_result(result, fresh)
        out["before_rows"] = _logical_images(columns, result.before_images)
        out["after_rows"] = _logical_images(columns, result.after_images)
        out["rows_digest"] = rows_digest(result.before_images)
        return fresh, fresh_access, out


# ---------------------------------------------------------------------------
# Schema changes and restores
# ---------------------------------------------------------------------------


async def _record_schema(
    table: dict, *, columns, row_count: int, source: str, title: Optional[str],
) -> dict:
    """quest.db after a landed schema change or restore: the new schema
    (version bumped by one, matching table.db's meta), row count and
    source, then the title when it changed. ``table`` is the row the
    change was applied to."""
    try:
        updated = await table_store.update_schema(
            table["id"],
            schema=dump_schema(columns),
            expected_schema_version=table["schema_version"],
            row_count=row_count,
            last_write_source=source,
        )
    except table_store.StaleTableSchemaError as exc:
        # Cannot happen while every schema writer holds the lock.
        logger.error("[tables] quest.db schema moved under the lock for %s", table["id"])
        raise StaleSchema(exc.current) from None
    if updated is None:
        raise TableNotFound(table["id"])
    if title is not None and title != updated["title"]:
        try:
            renamed = await table_store.update_table_metadata(table["id"], title=title)
        except (table_store.DuplicateTableTitleError, table_store.TableValidationError):
            logger.warning(
                "[tables] could not record title %r for %s in quest.db",
                title, table["id"], exc_info=True,
            )
        else:
            if renamed is not None:
                updated = renamed
    return updated


async def preview_schema_change(
    caller: Caller,
    table_id: str,
    operations: list,
    *,
    expected_schema_version: int,
) -> dict:
    """Dry run of :func:`apply_schema_change` for the
    ``change_table_schema`` precard (A12): the same gate, verdict
    (``schema == "approval"``, else :class:`TableDenied`), version check,
    plan and ``rows_violate`` checks, under the table lock, with nothing
    written, bumped or published (except that a ``schema_mismatch`` may
    first let :func:`_heal_lost_schema_bump` record an earlier lost schema
    bump in quest.db, as on every table.db call). A ``rename_table`` to a title another
    table of the same (owner, project, mode) uses refuses with
    ``duplicate_title`` (an invalid title with ``invalid_request``), as
    the real change would.

    Returns:
        ``{"table": the fresh table dict (with shares), "access": the
        verdict, "plan": the SchemaChangePlan, "columns_before": [Column],
        "columns_after": plan.columns, "schema_diff":
        schema_ops.schema_diff(before, after), "stats":
        storage.check_schema_violations' counts ({"cells_rewritten": {key:
        n}, "cells_changed": {key: n}, "rows_backfilled": n,
        "dropped_values": {key: n}}),
        "row_count": n, "new_title": plan.new_title}``. Column keys appear
        in ``schema_diff`` and ``stats``; the card strips them.

    Raises:
        TableDisabled, TableNotFound, TableDenied, StaleSchema, TableError
        (``invalid_args``, ``duplicate_title``, ``invalid_request``),
        TableSchemaError (``SchemaOperationError``), TableStorageError
        (``rows_violate``, ``change_too_large``, ``schema_mismatch``,
        ``busy``).
    """
    require_enabled(caller)
    if isinstance(expected_schema_version, bool) or not isinstance(expected_schema_version, int):
        raise TableError("invalid_args", "expected_schema_version must be an integer.")
    table, access = await _lookup(caller, table_id)
    if access.schema != "approval":
        raise TableDenied(access.schema_deny_reason)
    async with _table_lock(table["id"]):
        fresh, fresh_access = await _lookup(caller, table_id)
        if fresh_access.schema != "approval":
            raise TableDenied(fresh_access.schema_deny_reason)
        if expected_schema_version != fresh["schema_version"]:
            raise StaleSchema(fresh)
        columns = load_schema(fresh["schema"])
        table_dir = _table_dir(fresh["id"])
        count = await asyncio.to_thread(storage.row_count, table_dir)
        plan = schema_ops.apply_schema_operations(
            columns, operations, table_empty=count == 0,
        )
        if plan.new_title is not None and plan.new_title != fresh["title"]:
            try:
                clean_title = table_store._validate_title(plan.new_title)
            except table_store.TableValidationError as exc:
                raise TableError("invalid_request", str(exc)) from None
            if await table_store.title_taken(
                fresh["owner_id"], fresh["project_id"], fresh["mode"], clean_title,
                exclude_table_id=fresh["id"],
            ):
                raise TableError(
                    "duplicate_title",
                    str(table_store._duplicate_title_error(clean_title, fresh["project_id"])),
                )
        stats = await _file_call(
            fresh, storage.check_schema_violations, table_dir, columns, plan,
            schema_version=fresh["schema_version"],
        )
        return {
            "table": fresh,
            "access": fresh_access,
            "plan": plan,
            "columns_before": columns,
            "columns_after": list(plan.columns),
            "schema_diff": schema_ops.schema_diff(columns, plan.columns),
            "stats": stats,
            "row_count": count,
            "new_title": plan.new_title,
        }


async def apply_schema_change(
    caller: Caller,
    table_id: str,
    operations: list,
    *,
    expected_schema_version: int,
    write_source: Optional[str] = None,
    actor_user_id: Optional[int] = None,
) -> dict:
    """Apply a list of schema operations (one change).

    For ``PUT /tables/{id}/schema`` (a ``ui`` caller: the owner) and the
    ``create_table`` / ``change_table_schema`` executors (a ``top_level``
    caller at approve time). The verdict must be ``schema == "approval"``
    (the card's approval, or the owner's own Save in the UI), else
    :class:`TableDenied` with ``schema_deny_reason``.

    Under the lock (shielded): re-resolve, check ``expected_schema_version``
    (:class:`StaleSchema`), count rows, plan with
    ``schema_ops.apply_schema_operations(table_empty=row_count == 0)``,
    then ``storage.apply_schema_change`` in a thread and
    ``table_store.update_schema``. A ``rename_table`` operation is
    recorded in quest.db first through ``update_table_metadata`` (so a
    title collision refuses with ``duplicate_title`` before table.db
    changes) and rolled back if the file change fails.

    Returns:
        ``{change_id, schema_version, row_count, row_ids, columns
        (dump_schema), title, updated_at}``.

    Raises:
        TableDisabled, TableNotFound, TableDenied, StaleSchema, TableError
        (``invalid_args``, ``duplicate_title``), TableSchemaError
        (``SchemaOperationError``), TableStorageError (``rows_violate``,
        ``change_too_large``, ``schema_mismatch``, ``busy``).
    """
    require_enabled(caller)
    if isinstance(expected_schema_version, bool) or not isinstance(expected_schema_version, int):
        raise TableError("invalid_args", "expected_schema_version must be an integer.")
    table, access = await _lookup(caller, table_id)
    if access.schema != "approval":
        raise TableDenied(access.schema_deny_reason)
    source = _write_source(write_source, caller)
    actor = _actor(actor_user_id, caller)

    async def _locked() -> dict:
        async with _table_lock(table["id"]):
            fresh, fresh_access = await _lookup(caller, table_id)
            if fresh_access.schema != "approval":
                raise TableDenied(fresh_access.schema_deny_reason)
            if expected_schema_version != fresh["schema_version"]:
                raise StaleSchema(fresh)
            columns = load_schema(fresh["schema"])
            table_dir = _table_dir(fresh["id"])
            count = await asyncio.to_thread(storage.row_count, table_dir)
            plan = schema_ops.apply_schema_operations(
                columns, operations, table_empty=count == 0,
            )
            old_title = fresh["title"]
            renamed = plan.new_title is not None and plan.new_title != old_title
            if renamed:
                try:
                    await table_store.update_table_metadata(fresh["id"], title=plan.new_title)
                except table_store.DuplicateTableTitleError as exc:
                    raise TableError("duplicate_title", str(exc)) from None
                except table_store.TableValidationError as exc:
                    raise TableError("invalid_request", str(exc)) from None
            try:
                result = await _file_call(
                    fresh, storage.apply_schema_change, table_dir, columns, plan,
                    schema_version=fresh["schema_version"], source=source,
                    actor_user_id=actor, title=old_title,
                )
            except BaseException:
                if renamed:
                    try:
                        await table_store.update_table_metadata(fresh["id"], title=old_title)
                    except Exception:
                        logger.warning(
                            "[tables] could not roll back the title of %s",
                            fresh["id"], exc_info=True,
                        )
                raise
            updated = await _record_schema(
                fresh, columns=result.columns, row_count=result.row_count,
                source=source, title=result.title,
            )
            _events().publish_table_write(updated)
            return {
                "change_id": result.change_id,
                "schema_version": updated["schema_version"],
                "row_count": result.row_count,
                "row_ids": list(result.row_ids),
                "columns": dump_schema(result.columns),
                "title": updated["title"],
                "updated_at": updated["updated_at"],
            }

    return await _shielded(_locked())


async def apply_restore(
    caller: Caller,
    table_id: str,
    change_id: int,
    *,
    write_source: Optional[str] = None,
    actor_user_id: Optional[int] = None,
) -> dict:
    """Restore the table to the state right after ``change_id``.

    Editors only: the verdict must be ``rows == "free"`` for the caller's
    run kind. The restore route (A16) passes a ``ui`` caller, for which
    that means owner or write-share recipient (a recipient may restore
    past a schema change it could not make itself; see
    chat/tables/access.py rule 3). Else :class:`TableDenied`.

    Under the lock (shielded): ``revert.restore_to`` in a thread (source
    ``revert:<change_id>`` unless given), then quest.db is ALWAYS updated
    through ``table_store.update_schema``: a restore always bumps
    ``schema_version`` by one in table.db's meta (the restore entry's
    ``schema_after``), and may change the columns, the row count and the
    title, so recording the restored columns, row count and the +1
    version keeps quest.db's schema JSON and ``schema_version`` equal to
    the restored table.db meta. A restored title that collides with
    another table's title is left unrecorded in quest.db (logged).

    Returns:
        ``{change_id (the new restore entry), restored_to, schema_version,
        row_count, columns (dump_schema), title, updated_at}``.

    Raises:
        TableDisabled, TableNotFound, TableDenied, TableStorageError
        (``unknown_change``, ``schema_mismatch``, ``corrupt_storage``,
        ``busy``), TableError ``invalid_args`` (``change_id`` not an
        integer).
    """
    require_enabled(caller)
    if isinstance(change_id, bool) or not isinstance(change_id, int):
        raise TableError("invalid_args", "change_id must be an integer.")
    table, access = await _lookup(caller, table_id)
    if access.rows != "free":
        raise TableDenied(access.deny_reason or "Restoring needs edit access to this table.")
    source = write_source or f"revert:{change_id}"
    actor = _actor(actor_user_id, caller)

    async def _locked() -> dict:
        async with _table_lock(table["id"]):
            fresh, fresh_access = await _lookup(caller, table_id)
            if fresh_access.rows != "free":
                raise TableDenied(
                    fresh_access.deny_reason or "Restoring needs edit access to this table."
                )
            columns = load_schema(fresh["schema"])
            result = await _file_call(
                fresh, revert.restore_to, _table_dir(fresh["id"]), columns, change_id,
                schema_version=fresh["schema_version"], source=source,
                actor_user_id=actor,
            )
            updated = await _record_schema(
                fresh, columns=result.columns, row_count=result.row_count,
                source=source, title=result.title,
            )
            _events().publish_table_write(updated)
            return {
                "change_id": result.change_id,
                "restored_to": result.restored_to,
                "schema_version": updated["schema_version"],
                "row_count": result.row_count,
                "columns": dump_schema(result.columns),
                "title": updated["title"],
                "updated_at": updated["updated_at"],
            }

    return await _shielded(_locked())


# ---------------------------------------------------------------------------
# Create and delete
# ---------------------------------------------------------------------------


async def create_table(
    caller: Caller,
    *,
    title: str,
    description: str = "",
    project_id: Optional[str] = None,
    columns: list,
    write_source: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    owner_id: Optional[int] = None,
) -> dict:
    """Create a table: ``table.db`` with change #1 "Created table", then
    the quest.db row. For ``POST /tables`` (A15, a ``ui`` caller) and the
    ``create_table`` executor (A11, at approve time).

    Authorization is the callers' job (the route's ownership checks, the
    card's pre-card and approve-time checks); this function only checks
    the gate, the column specs (``schema.parse_columns``; errors propagate
    as ``ColumnSpecError``), the title / description, and for a project
    table that the project exists and belongs to the owner. The owner is
    ``owner_id`` or the caller. Mode: ``private`` for a user table, the
    project's ``public`` flag for a project table; a conversation caller
    (anything but ``ui``) must be in that same mode (``mode_mismatch``, the
    taint rule: a private conversation never creates a public table).

    Order (the docs order): lay out the directory under a fresh id, then
    insert the row (a crash in between leaves an invisible orphan
    directory, never a row without a file); a failing insert removes the
    directory again; the whole sequence is shielded from cancellation.
    Creates are serialized per owner, so the store's
    check-then-insert title rule and per-owner cap hold.
    Publishes ``table_list_changed`` to the owner (no shares yet).

    Returns:
        The store's table dict (with ``shares: []``).

    Raises:
        TableDisabled, TableError (``invalid_args``, ``project_not_found``,
        ``mode_mismatch``, ``duplicate_title``, ``table_limit``,
        ``invalid_request``), ColumnSpecError, TableStorageError.
    """
    require_enabled(caller)
    try:
        clean_title = table_store._validate_title(title)
        clean_description = table_store._validate_description(description)
    except table_store.TableValidationError as exc:
        raise TableError("invalid_request", str(exc)) from None
    parsed = parse_columns(columns)
    owner = caller.user["id"] if owner_id is None else owner_id
    mode = "private"
    if project_id is not None:
        from db import project_store

        project = await project_store.get_project(owner, project_id)
        if project is None:
            raise TableError("project_not_found", "Project not found.")
        mode = "public" if project.get("public") else "private"
    if caller.run_kind != "ui" and (mode == "public") != bool(caller.is_public):
        raise TableError(
            "mode_mismatch",
            "This conversation's mode does not match the table's; the table "
            "cannot be created here.",
        )
    source = _write_source(write_source, caller)
    actor = _actor(actor_user_id, caller)
    table_id = str(uuid.uuid4())
    table_dir = _table_dir(table_id)

    async def _locked() -> dict:
        lock_requested = time.monotonic()
        async with _table_lock(f"owner:{owner}"):
            lock_wait = time.monotonic() - lock_requested
            try:
                file_started = time.monotonic()
                await asyncio.to_thread(
                    storage.create_table_db, table_dir,
                    table_id=table_id, title=clean_title, schema=parsed,
                    source=source, actor_user_id=actor,
                )
                row_started = time.monotonic()
                table = await table_store.create_table(
                    owner, clean_title,
                    mode=mode, schema=dump_schema(parsed), description=clean_description,
                    project_id=project_id, last_write_source=source, table_id=table_id,
                )
                row_done = time.monotonic()
            except BaseException as exc:
                try:
                    await asyncio.to_thread(_remove_dir, table_dir)
                except Exception:
                    logger.warning("[tables] cleanup of %s failed", table_id, exc_info=True)
                if isinstance(exc, table_store.DuplicateTableTitleError):
                    raise TableError("duplicate_title", str(exc)) from None
                if isinstance(exc, table_store.TableLimitError):
                    raise TableError("table_limit", str(exc)) from None
                if isinstance(exc, table_store.TableValidationError):
                    raise TableError("invalid_request", str(exc)) from None
                raise
            _events().publish_table_list_changed(table["owner_id"])
            # The stamp a later "read of <id> failed" line is matched against.
            logger.info(
                "[tables] created %s (%s table, %s, %d columns) source=%s user=%s "
                "lock_wait=%dms file=%dms row=%dms %s",
                table_id, "project" if project_id is not None else "user", mode,
                len(parsed), source, _caller_email(caller), _ms(lock_wait),
                _ms(row_started - file_started), _ms(row_done - row_started),
                storage.describe_table_dir(table_dir),
            )
            return table

    return await _shielded(_locked())


async def delete_table(caller: Caller, table_id: str) -> dict:
    """Owner-only delete, under the table lock (shielded): the row (shares
    go with it), then the directory (best-effort, logged), then
    ``table_changed {updated_at: null}`` and ``table_list_changed`` to the
    audience captured under the lock BEFORE the rows go.

    Returns:
        The deleted table dict (with shares, as it was).

    Raises:
        TableDisabled, TableNotFound (hidden/missing), TableForbidden.
    """
    require_enabled(caller)
    table, _access = await _lookup(caller, table_id)
    if table["owner_id"] != caller.user["id"]:
        raise TableForbidden("Only the table's owner can delete it.")

    async def _locked() -> dict:
        async with _table_lock(table["id"]):
            current = await table_store.get_table(table["id"], with_shares=True)
            if current is None:
                raise TableNotFound(table_id)
            audience = _events().table_audience(current)
            table_dir = _table_dir(current["id"])
            if not await table_store.delete_table(current["id"]):
                raise TableNotFound(table_id)
            try:
                await asyncio.to_thread(_remove_dir, table_dir)
            except Exception:
                logger.warning(
                    "[tables] could not remove the directory of deleted table %s",
                    current["id"], exc_info=True,
                )
            events = _events()
            events.publish_table_gone_to(audience, current["id"])
            events.publish_table_list_changed_to(audience)
            return current

    return await _shielded(_locked())


# ---------------------------------------------------------------------------
# Metadata, history, export and shares (the HTTP routes)
# ---------------------------------------------------------------------------


def _require_owner(table: dict, caller: Caller, message: str) -> None:
    if table["owner_id"] != caller.user["id"]:
        raise TableForbidden(message)


async def update_metadata(
    caller: Caller,
    table_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    require_approval: Optional[bool] = None,
    expected_updated_at: Optional[str] = None,
) -> dict:
    """Owner-only rename / description / ``require_approval`` edit
    (``PUT /tables/{id}``), under the table lock (shielded) through
    ``table_store.update_table_metadata``. A metadata-only change is not a
    table.db change (no change-log entry; table.db's ``meta.title`` keeps
    the title of the last schema change or restore). Publishes
    ``table_changed`` when ``updated_at`` moved (something changed).

    Returns:
        The table dict with shares.

    Raises:
        TableDisabled, TableNotFound, TableForbidden, StaleUpdate,
        TableError (``duplicate_title``, ``invalid_request``,
        ``invalid_args``).
    """
    require_enabled(caller)
    table, _access = await _lookup(caller, table_id)
    owner_message = "Only the table's owner can change its settings."
    _require_owner(table, caller, owner_message)

    async def _locked() -> dict:
        async with _table_lock(table["id"]):
            fresh, _fresh_access = await _lookup(caller, table_id)
            _require_owner(fresh, caller, owner_message)
            try:
                updated = await table_store.update_table_metadata(
                    fresh["id"],
                    title=title,
                    description=description,
                    require_approval=require_approval,
                    expected_updated_at=expected_updated_at,
                )
            except table_store.StaleTableError as exc:
                raise StaleUpdate(exc.current) from None
            except table_store.DuplicateTableTitleError as exc:
                raise TableError("duplicate_title", str(exc)) from None
            except table_store.TableValidationError as exc:
                raise TableError("invalid_request", str(exc)) from None
            if updated is None:
                raise TableNotFound(table_id)
            if updated["updated_at"] != fresh["updated_at"]:
                _events().publish_table_write(updated)
            return updated

    return await _shielded(_locked())


def _is_editor(access: TableAccess) -> bool:
    """Editors (history, restore, downloads with history), for ``ui``
    callers ONLY: the ``ui`` rows verdict ``"free"``, i.e. the owner or a
    write-share recipient. For any other run kind ``rows == "free"`` means
    something else (e.g. an owner's unshared table in a conversation), so
    :func:`_require_ui` guards every use. The owner's ``require_approval``
    switch never touches the ``ui`` verdict (chat/artifact_access.py), so
    the owner stays an editor with it on."""
    return access.rows == "free"


def _require_ui(caller: Caller) -> None:
    """History and history downloads are Quest UI features only."""
    if caller.run_kind != "ui":
        raise TableError("invalid_args", "Table history is only available in the Quest UI.")


def _require_editor(access: TableAccess) -> None:
    if not _is_editor(access):
        raise TableForbidden(HISTORY_FORBIDDEN_MESSAGE)


def _check_int(value, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TableError("invalid_args", f"{name} must be an integer.")


async def _editor_read(caller: Caller, table_id: str, fn):
    """:func:`_locked_read` for editors only: the editor check runs on the
    verdict outside the lock and again on the re-resolved one under it.
    ``fn(table_dir, columns, table)`` runs in a thread."""
    require_enabled(caller)
    _require_ui(caller)
    _table, access = await _lookup(caller, table_id)
    _require_editor(access)
    async with _table_lock(table_id):
        table, access = await _lookup(caller, table_id)
        _require_editor(access)
        columns = load_schema(table["schema"])
        result = await _file_call(table, fn, _table_dir(table["id"]), columns, table)
        return table, access, result


async def list_changes(
    caller: Caller,
    table_id: str,
    *,
    limit: int = 50,
    before_id: Optional[int] = None,
) -> tuple[dict, TableAccess, list[dict]]:
    """A page of the change log, newest first (``GET /tables/{id}/changes``).
    Editors only (:data:`HISTORY_FORBIDDEN_MESSAGE`). Each entry: the
    ``storage.list_changes`` summary fields (``id, created_at, source,
    actor_user_id, kind, op, summary, schema_before, schema_after,
    size_bytes, restore_to, row_count``) plus ``in_effect``
    (:func:`chat.tables.revert.list_changes_in_effect`). ``limit`` is
    clamped by storage; ``before_id`` pages to older entries.

    Raises:
        TableDisabled, TableNotFound, TableForbidden, TableError
        ``invalid_args`` (also for a non-``ui`` caller), TableStorageError.
    """
    require_enabled(caller)
    _check_int(limit, "limit")
    _check_int(before_id, "before_id", optional=True)

    def _run(table_dir: Path, _columns, _table: dict) -> list[dict]:
        return revert.list_changes_in_effect(table_dir, limit=limit, before_id=before_id)

    return await _editor_read(caller, table_id, _run)


async def get_change(
    caller: Caller, table_id: str, change_id: int,
) -> tuple[dict, TableAccess, dict]:
    """One change with ``row_ids``, ``before`` and ``after`` decoded
    (``storage.get_change``), editors only. Shaping and size caps are the
    route's job.

    Raises:
        TableDisabled, TableNotFound, TableForbidden, TableError
        ``invalid_args`` (``change_id`` not an integer),
        ``revert.UnknownChangeError`` (``unknown_change``), TableStorageError.
    """
    require_enabled(caller)
    _check_int(change_id, "change_id")

    def _run(table_dir: Path, _columns, _table: dict) -> dict:
        change = storage.get_change(table_dir, change_id)
        if change is None:
            raise revert.UnknownChangeError(change_id)
        return change

    return await _editor_read(caller, table_id, _run)


async def get_change_entry(
    caller: Caller, table_id: str, change_id: int,
) -> tuple[dict, TableAccess, dict, dict]:
    """:func:`get_change` plus the change's list entry (the
    :func:`list_changes` fields incl. ``in_effect`` and ``row_count``),
    both read in ONE locked editor read so they describe the same log
    (``GET /tables/{id}/changes/{change_id}``).

    Returns:
        ``(table, access, change, entry)``.

    Raises:
        As :func:`get_change`.
    """
    require_enabled(caller)
    _check_int(change_id, "change_id")

    def _run(table_dir: Path, _columns, _table: dict) -> tuple[dict, dict]:
        change = storage.get_change(table_dir, change_id)
        if change is None:
            raise revert.UnknownChangeError(change_id)
        page = revert.list_changes_in_effect(table_dir, limit=1, before_id=change_id + 1)
        if not page or page[0]["id"] != change_id:
            raise revert.UnknownChangeError(change_id)
        return change, page[0]

    table, access, (change, entry) = await _editor_read(caller, table_id, _run)
    return table, access, change, entry


EXPORT_FORMATS = ("csv", "sqlite")


async def export_table(
    caller: Caller,
    table_id: str,
    *,
    fmt: str,
    include_history: bool = False,
    dest_dir: Path,
) -> tuple[dict, Path]:
    """Write a download copy of the table into ``dest_dir`` (an existing
    real directory the caller created and removes; ``ValueError``
    otherwise) and return ``(table, path)``.

    Every reader may export. ``fmt="csv"``: the rows are snapshotted with
    ``storage.dump_rows`` under the table lock and written to
    ``dest_dir/table.csv`` outside it (:func:`chat.tables.export.write_csv`,
    in a thread). ``fmt="sqlite"``: ``storage.copy_for_download`` to
    ``dest_dir/table.sqlite`` under the lock (in a thread), without the
    ``changes`` table unless ``include_history``, which is editors only
    (:class:`TableForbidden`; ``ui`` callers only, ``invalid_args``
    otherwise) and only valid with ``sqlite``. A cancelled call returns
    only after the file work has finished (:func:`_finish_before_cancel`),
    so the caller may then remove ``dest_dir`` safely.

    Raises:
        TableDisabled, TableNotFound, TableForbidden, TableError
        ``invalid_args``, TableStorageError, ValueError (bad ``dest_dir``).
    """
    require_enabled(caller)
    if fmt not in EXPORT_FORMATS:
        raise TableError("invalid_args", f"format must be one of: {', '.join(EXPORT_FORMATS)}.")
    if not isinstance(include_history, bool):
        raise TableError("invalid_args", "include_history must be a boolean.")
    if include_history and fmt != "sqlite":
        raise TableError("invalid_args", "include_history is only available for sqlite downloads.")
    dest_dir = Path(dest_dir)
    try:
        st = os.lstat(dest_dir)
    except FileNotFoundError:
        raise ValueError(f"dest_dir {dest_dir} does not exist") from None
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"dest_dir {dest_dir} is not a real directory")

    if include_history:
        _require_ui(caller)

    _table, access = await _lookup(caller, table_id)
    if include_history:
        _require_editor(access)

    async def _work() -> tuple[dict, Path]:
        async with _table_lock(table_id):
            table, fresh_access = await _lookup(caller, table_id)
            if include_history:
                _require_editor(fresh_access)
            table_dir = _table_dir(table["id"])
            columns = load_schema(table["schema"])
            if fmt == "sqlite":
                path = await _file_call(
                    table, storage.copy_for_download, table_dir, dest_dir / "table.sqlite",
                    include_history=include_history,
                )
                return table, path
            rows = await _file_call(table, storage.dump_rows, table_dir, columns)
        from chat.tables import export
        path = await asyncio.to_thread(export.write_csv, dest_dir / "table.csv", columns, rows)
        return table, path

    return await _finish_before_cancel(_work())


def _publish_roster_change(before: dict, after: dict) -> None:
    """A share roster changed: ``table_changed {updated_at: null}`` and
    ``table_list_changed`` to the old and new audiences together, so a
    removed recipient's viewer closes and every list refreshes."""
    events = _events()
    audience = events.tables_audience([before, after])
    events.publish_table_gone_to(audience, after["id"])
    events.publish_table_list_changed_to(audience)


_SHARE_OWNER_MESSAGE = "Only the table's owner can manage its sharing."


async def add_share(
    caller: Caller,
    table_id: str,
    *,
    user_id: Optional[int],
    permission: str,
) -> tuple[dict, bool]:
    """Owner-only: grant ``permission`` (``read`` / ``write``) to
    ``user_id`` or to everyone (None), an upsert (``table_store.add_share``:
    one row per recipient). Under the table lock (shielded), so a queued
    read or write re-resolves with the new roster. ``changed`` is False
    when the recipient already had this permission (nothing published).
    Does not bump ``updated_at``.

    Returns:
        ``(table with shares, changed)``.

    Raises:
        TableDisabled, TableNotFound, TableForbidden, TableError
        (``cannot_share_with_owner``, ``user_not_found``,
        ``invalid_request``).
    """
    require_enabled(caller)
    table, _access = await _lookup(caller, table_id)
    _require_owner(table, caller, _SHARE_OWNER_MESSAGE)

    async def _locked() -> tuple[dict, bool]:
        async with _table_lock(table["id"]):
            before, _fresh_access = await _lookup(caller, table_id)
            _require_owner(before, caller, _SHARE_OWNER_MESSAGE)
            if user_id is not None and user_id == before["owner_id"]:
                raise TableError(
                    "cannot_share_with_owner",
                    "You own this table; share it with someone else.",
                )
            previous = next(
                (s for s in before["shares"] if s["user_id"] == user_id), None,
            )
            try:
                await table_store.add_share(before["id"], user_id, permission)
            except table_store.TableValidationError as exc:
                # The table may be gone (an account delete does not take
                # the table lock): the store then refuses with a plain
                # TableValidationError or a ShareTargetGoneError.
                if await table_store.get_table(before["id"], with_shares=False) is None:
                    raise TableNotFound(table_id) from None
                if isinstance(exc, table_store.ShareTargetGoneError):
                    raise TableError("user_not_found", "That user no longer exists.") from None
                raise TableError("invalid_request", str(exc)) from None
            after = await table_store.get_table(before["id"], with_shares=True)
            if after is None:
                raise TableNotFound(table_id)
            changed = previous is None or previous["permission"] != permission
            if changed:
                _publish_roster_change(before, after)
            return after, changed

    return await _shielded(_locked())


async def remove_share(caller: Caller, table_id: str, share_id: int) -> dict:
    """Owner-only: remove one share row of this table, under the table lock
    (shielded). The old and new audiences are told (the removed recipient
    gets ``table_changed {updated_at: null}``). Does not bump
    ``updated_at``.

    Returns:
        The table with shares.

    Raises:
        TableDisabled, TableNotFound, TableForbidden, TableError
        ``share_not_found`` (also for a non-integer id).
    """
    require_enabled(caller)
    table, _access = await _lookup(caller, table_id)
    _require_owner(table, caller, _SHARE_OWNER_MESSAGE)
    if isinstance(share_id, bool) or not isinstance(share_id, int):
        raise TableError("share_not_found", "Share not found.")

    async def _locked() -> dict:
        async with _table_lock(table["id"]):
            before, _fresh_access = await _lookup(caller, table_id)
            _require_owner(before, caller, _SHARE_OWNER_MESSAGE)
            if not any(s["id"] == share_id for s in before["shares"]) or not (
                await table_store.remove_share(before["id"], share_id)
            ):
                raise TableError("share_not_found", "Share not found.")
            after = await table_store.get_table(before["id"], with_shares=True)
            if after is None:
                raise TableNotFound(table_id)
            _publish_roster_change(before, after)
            return after

    return await _shielded(_locked())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _remove_dir(path: Path) -> None:
    """Remove a table directory; missing is fine, a symlink is unlinked,
    never followed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def delete_table_dirs(table_ids: list[str]) -> None:
    """Remove deleted tables' directories (sync: run in ``asyncio.to_thread``).

    For the project-delete and account-delete sweeps, after the rows are
    gone. Each id goes through ``ChatStorage.get_table_dir``; a failure is
    logged, never raised (the delete has already succeeded)."""
    for table_id in table_ids:
        try:
            _remove_dir(_table_dir(table_id))
        except Exception:
            logger.warning(
                "[tables] could not remove the directory of table %s", table_id,
                exc_info=True,
            )
