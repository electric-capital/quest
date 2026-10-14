"""Quest Tables data access layer.

Async CRUD over the ``data_tables`` and ``data_table_shares`` tables,
returning plain dicts (the shape documented on :func:`_table_to_dict`).
The rows and the change log live on disk in ``table.db``; this module only
keeps the metadata, the schema JSON with its version, the cached row
count, and the shares.

Same async pattern as db/doc_store.py: each function opens a fresh
``AsyncSessionLocal()`` session (imported at module level so tests can
monkeypatch it). Timestamps are written as ``datetime.now(timezone.utc)``
and read back naive-UTC from SQLite (every write path commits then
refreshes, so returned dicts always carry the stored form). Two
optimistic-concurrency tokens: the ``updated_at`` ISO string for metadata
edits (:func:`update_table_metadata`) and ``schema_version`` for schema
changes (:func:`update_schema`).

Access decisions do NOT live here. Row and schema writes go through the
tables service, which calls :func:`update_after_write` /
:func:`update_schema` once the ``table.db`` write landed.
"""

from collections.abc import Collection
from datetime import datetime, timezone
from typing import Iterable, Optional
import uuid

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from chat.tables.constants import (
    TABLE_DESCRIPTION_MAX_LEN,
    TABLE_MAX_PER_OWNER,
    TABLE_MODES,
    TABLE_SHARE_PERMISSIONS,
    TABLE_TITLE_MAX_LEN,
    table_not_found_message,
)
from db.engine import AsyncSessionLocal
from db.models import DataTable, DataTableShare

# Chunk size for ``IN (...)`` lookups, well under SQLite's bound-parameter cap.
_IN_CHUNK = 500


class TableValidationError(ValueError):
    """Invalid input to a table store function (bad title, mode, schema...)."""


class DuplicateTableTitleError(ValueError):
    """Another table of the same owner and scope already uses this title.

    Titles are unique case-insensitively per ``(owner_id, project_id, mode)``,
    where a NULL ``project_id`` (user table) is its own scope.
    """


class TableLimitError(ValueError):
    """The owner already has ``TABLE_MAX_PER_OWNER`` tables."""


class ShareTargetGoneError(TableValidationError):
    """:func:`add_share` lost a race: the table or the recipient user was
    deleted between the caller's checks and the insert (the foreign key
    refused the row). The caller re-checks which one is gone."""


class StaleTableError(ValueError):
    """``expected_updated_at`` did not match the row (concurrent metadata edit).

    Carries the current row (with shares) so the route can return it in
    the 409 body without a second GET.
    """

    def __init__(self, current: dict):
        super().__init__("Table was modified by another writer")
        self.current = current


class StaleTableSchemaError(ValueError):
    """``expected_schema_version`` did not match the row (concurrent schema
    change). Deliberately not a :class:`StaleTableError` subclass: it maps
    to its own ``stale_schema`` outcome. Carries the current row."""

    def __init__(self, current: dict):
        super().__init__("Table schema was changed by another writer")
        self.current = current


# ---------------------------------------------------------------------------
# Validation + serialization
# ---------------------------------------------------------------------------


def _validate_title(title) -> str:
    """Return the stripped title, or raise :class:`TableValidationError`."""
    if not isinstance(title, str):
        raise TableValidationError("Table title must be a string.")
    stripped = title.strip()
    if not stripped:
        raise TableValidationError("Table title cannot be empty.")
    if len(stripped) > TABLE_TITLE_MAX_LEN:
        raise TableValidationError(
            f"Table title exceeds maximum length of {TABLE_TITLE_MAX_LEN} characters."
        )
    return stripped


def _validate_description(description) -> str:
    """Return the stripped description (None -> ""), or raise."""
    if description is None:
        return ""
    if not isinstance(description, str):
        raise TableValidationError("Table description must be a string.")
    stripped = description.strip()
    if len(stripped) > TABLE_DESCRIPTION_MAX_LEN:
        raise TableValidationError(
            f"Table description exceeds maximum length of "
            f"{TABLE_DESCRIPTION_MAX_LEN} characters."
        )
    return stripped


def _validate_mode(mode) -> str:
    if mode not in TABLE_MODES:
        raise TableValidationError(
            f"Invalid table mode {mode!r}. Must be one of: {', '.join(TABLE_MODES)}."
        )
    return mode


def _validate_permission(permission) -> str:
    if permission not in TABLE_SHARE_PERMISSIONS:
        raise TableValidationError(
            f"Invalid share permission {permission!r}. Must be one of: "
            f"{', '.join(TABLE_SHARE_PERMISSIONS)}."
        )
    return permission


def _validate_count(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TableValidationError(f"{label} must be a non-negative integer.")
    return value


def _validate_schema(schema) -> list:
    """Shape check only: a list of column dicts. Column-level validation
    (types, keys, options) belongs to the tables schema module, which runs
    before anything reaches the store."""
    if not isinstance(schema, list) or not all(isinstance(c, dict) for c in schema):
        raise TableValidationError("Table schema must be a list of column objects.")
    return schema


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _share_to_dict(share: DataTableShare) -> dict:
    """Convert a DataTableShare ORM instance to the share dict shape."""
    return {
        "id": share.id,
        "user_id": share.user_id,
        "permission": share.permission,
        "created_at": _iso(share.created_at),
    }


def _table_to_dict(
    table: DataTable, shares: Optional[Iterable[DataTableShare]] = None,
) -> dict:
    """Convert a DataTable ORM instance to the table dict.

    Shape (``scope`` is derived by callers: ``"project" if project_id else
    "user"``)::

        {"id", "owner_id", "project_id", "title", "description", "mode",
         "schema",                              # ordered column list
         "schema_version", "row_count", "last_write_source",
         "require_approval",                    # bool, owner's switch
         "created_at", "updated_at",            # ISO strings
         "shares": [{"id", "user_id", "permission", "created_at"}]}

    The ``shares`` key is present only when ``shares`` is provided (an
    empty iterable yields ``[]``).
    """
    data = {
        "id": table.id,
        "owner_id": table.owner_id,
        "project_id": table.project_id,
        "title": table.title,
        "description": table.description,
        "mode": table.mode,
        "schema": table.table_schema,
        "schema_version": table.schema_version,
        "row_count": table.row_count,
        "last_write_source": table.last_write_source,
        "require_approval": bool(table.require_approval),
        "created_at": _iso(table.created_at),
        "updated_at": _iso(table.updated_at),
    }
    if shares is not None:
        data["shares"] = [_share_to_dict(s) for s in shares]
    return data


def _duplicate_title_error(title: str, project_id: Optional[str]) -> DuplicateTableTitleError:
    return DuplicateTableTitleError(
        f"A table titled '{title}' already exists"
        + (" in this project." if project_id else ".")
    )


async def _title_taken(
    db,
    owner_id: int,
    project_id: Optional[str],
    mode: str,
    title: str,
    exclude_table_id: Optional[str] = None,
) -> bool:
    """True when another table in ``(owner_id, project_id, mode)`` uses ``title``.

    Scoped per mode so a public conversation's ``create_table`` never
    learns from the collision error whether a private table of that title
    exists. Check-then-insert, compared with ``str.casefold()`` in Python
    (SQLite ``lower()`` folds ASCII only) -- the same rules and the same
    documented race as ``db.doc_store._title_taken``.
    """
    stmt = select(DataTable.id, DataTable.title).where(
        DataTable.owner_id == owner_id, DataTable.mode == mode,
    )
    if project_id is None:
        stmt = stmt.where(DataTable.project_id.is_(None))
    else:
        stmt = stmt.where(DataTable.project_id == project_id)
    if exclude_table_id is not None:
        stmt = stmt.where(DataTable.id != exclude_table_id)
    wanted = title.casefold()
    result = await db.execute(stmt)
    return any(t.strip().casefold() == wanted for _id, t in result.all())


async def _load_shares(db, table_ids: list[str]) -> dict[str, list[DataTableShare]]:
    """Return ``{table_id: [DataTableShare, ...]}`` (ordered by share id)."""
    by_table: dict[str, list[DataTableShare]] = {t: [] for t in table_ids}
    for i in range(0, len(table_ids), _IN_CHUNK):
        chunk = table_ids[i:i + _IN_CHUNK]
        result = await db.execute(
            select(DataTableShare)
            .where(DataTableShare.table_id.in_(chunk))
            .order_by(DataTableShare.id.asc())
        )
        for share in result.scalars().all():
            by_table.setdefault(share.table_id, []).append(share)
    return by_table


async def _table_dict_with_shares(db, table: DataTable) -> dict:
    shares = (await _load_shares(db, [table.id]))[table.id]
    return _table_to_dict(table, shares)


def _keyset_page(stmt, *, limit: Optional[int], before: Optional[tuple[datetime, str]]):
    """Apply the list order ``(updated_at DESC, id DESC)``, the keyset
    cursor (rows strictly after ``before``) and the optional ``limit``."""
    if before is not None:
        before_ts, before_id = before
        stmt = stmt.where(
            or_(
                DataTable.updated_at < before_ts,
                and_(DataTable.updated_at == before_ts, DataTable.id < before_id),
            )
        )
    stmt = stmt.order_by(DataTable.updated_at.desc(), DataTable.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt


async def _tables_with_shares(db, stmt) -> list[dict]:
    tables = (await db.execute(stmt)).scalars().all()
    if not tables:
        return []
    shares = await _load_shares(db, [t.id for t in tables])
    return [_table_to_dict(t, shares.get(t.id, [])) for t in tables]


def _share_exists_for(user_id: int):
    """A share row on the table for ``user_id`` or for everyone."""
    return exists().where(
        DataTableShare.table_id == DataTable.id,
        or_(DataTableShare.user_id == user_id, DataTableShare.user_id.is_(None)),
    )


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


async def create_table(
    owner_id: int,
    title: str,
    *,
    mode: str,
    schema: list,
    description: str = "",
    project_id: Optional[str] = None,
    last_write_source: Optional[str] = None,
    table_id: Optional[str] = None,
) -> dict:
    """Create a table row (``schema_version`` 1, ``row_count`` 0).

    Args:
        owner_id: Owning user (the project owner for project tables).
        title: 1..200 chars after stripping; unique case-insensitively per
            ``(owner_id, project_id, mode)``.
        mode: ``"private"`` or ``"public"`` (project tables: the caller
            passes the project's mode). A user table must be ``"private"``.
        schema: Ordered column list, already validated by the caller.
        description: 0..500 chars after stripping.
        project_id: None for a user table.
        last_write_source: ``conversation:<id>`` / ``ui:<user_id>`` /
            ``action_request:<id>``.
        table_id: Optional pre-generated id (lets the service lay out the
            directory first); a uuid4 is generated when omitted.

    Returns:
        The table dict with ``shares: []``.

    Raises:
        TableValidationError: invalid title / description / mode / schema,
            or a public user table (user tables are always private).
        DuplicateTableTitleError: title collision in the same scope.
        TableLimitError: the owner already has ``TABLE_MAX_PER_OWNER`` tables.

    The title check and the per-owner cap are both check-then-insert: two
    simultaneous creates can both pass (a duplicate title, or one table
    over the cap). The tables service serializes creates per owner, so the
    store documents the race instead of guarding it.
    """
    clean_title = _validate_title(title)
    clean_description = _validate_description(description)
    _validate_mode(mode)
    if project_id is None and mode != "private":
        raise TableValidationError(
            "User tables are always private; only project tables can be public."
        )
    _validate_schema(schema)

    async with AsyncSessionLocal() as db:
        if await _title_taken(db, owner_id, project_id, mode, clean_title):
            raise _duplicate_title_error(clean_title, project_id)
        owned = (await db.execute(
            select(func.count()).select_from(DataTable)
            .where(DataTable.owner_id == owner_id)
        )).scalar_one()
        if owned >= TABLE_MAX_PER_OWNER:
            raise TableLimitError(
                f"Table limit reached: an owner can have at most "
                f"{TABLE_MAX_PER_OWNER} tables (TABLE_MAX_PER_OWNER)."
            )
        now = datetime.now(timezone.utc)
        table = DataTable(
            id=table_id or str(uuid.uuid4()),
            owner_id=owner_id,
            project_id=project_id,
            title=clean_title,
            description=clean_description,
            mode=mode,
            table_schema=schema,
            schema_version=1,
            row_count=0,
            last_write_source=last_write_source,
            created_at=now,
            updated_at=now,
        )
        db.add(table)
        await db.commit()
        await db.refresh(table)
        return _table_to_dict(table, [])


async def get_table(table_id: str, *, with_shares: bool = True) -> Optional[dict]:
    """Return the table dict (with ``shares`` unless ``with_shares=False``), or None."""
    async with AsyncSessionLocal() as db:
        table = await db.get(DataTable, table_id)
        if not with_shares:
            return _table_to_dict(table) if table is not None else None
        # The shares query runs for a missing id too, so a hidden table and
        # a nonexistent one cost the same two queries (invariant 2).
        shares = await _load_shares(db, [table_id])
        if table is None:
            return None
        return _table_to_dict(table, shares.get(table_id, []))


async def list_accessible_tables(
    user_id: int,
    *,
    project_id: Optional[str] = None,
    include_user_tables: bool = True,
    include_project_tables: bool = False,
    source_project_ids: Collection[str] = (),
    mode: Optional[str] = None,
    owned_only: bool = False,
    limit: Optional[int] = None,
    before: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """Tables the user owns or holds a share on, newest first, with shares.

    Candidates are tables where ``owner_id == user_id`` or a share row
    exists for the user or for everyone -- only the first with
    ``owned_only``. This is the candidate set only: the caller still runs
    every row through the access rule.

    Scope filter (OR-ed):
        - ``include_user_tables``: user tables (``project_id IS NULL``);
        - ``include_project_tables`` with ``project_id``: that project's tables;
        - ``source_project_ids``: the public tables (``mode="public"``) of
          those projects -- a private project's linked sources, readable
          from its conversations.
      With none, the result is empty.

    Args:
        user_id: The viewing user.
        project_id: Project whose tables to include (with include_project_tables).
        include_user_tables: Include user (non-project) tables.
        include_project_tables: Include ``project_id``'s tables.
        source_project_ids: Projects whose public tables to include.
        mode: Optional ``"private"`` / ``"public"`` filter, so the SQL
            ``LIMIT`` applies to the set a public caller can see.
        owned_only: Only tables ``user_id`` owns.
        limit: Optional row cap (callers fetch ``limit + 1`` for has_more).
        before: Optional keyset cursor ``(updated_at, id)``: only rows
            strictly after this pair under ``(updated_at DESC, id DESC)``.

    Returns:
        Table dicts, each with ``shares``.
    """
    scope_conds = []
    if include_user_tables:
        scope_conds.append(DataTable.project_id.is_(None))
    if include_project_tables and project_id:
        scope_conds.append(DataTable.project_id == project_id)
    if source_project_ids:
        scope_conds.append(
            and_(
                DataTable.project_id.in_(list(source_project_ids)),
                DataTable.mode == "public",
            )
        )
    if not scope_conds:
        return []
    if mode is not None:
        _validate_mode(mode)

    if owned_only:
        relationship = DataTable.owner_id == user_id
    else:
        relationship = or_(DataTable.owner_id == user_id, _share_exists_for(user_id))

    async with AsyncSessionLocal() as db:
        stmt = select(DataTable).where(relationship, or_(*scope_conds))
        if mode is not None:
            stmt = stmt.where(DataTable.mode == mode)
        stmt = _keyset_page(stmt, limit=limit, before=before)
        return await _tables_with_shares(db, stmt)


async def list_tables_shared_with(
    user_id: int,
    *,
    limit: Optional[int] = None,
    before: Optional[tuple[datetime, str]] = None,
) -> list[dict]:
    """Tables shared WITH the user that they do not own, newest first.

    Candidates: ``owner_id != user_id`` and a share row for the user or for
    everyone; user and project tables alike. Same order and keyset cursor
    as :func:`list_accessible_tables`; the caller still applies the access
    rule.
    """
    async with AsyncSessionLocal() as db:
        stmt = select(DataTable).where(
            DataTable.owner_id != user_id, _share_exists_for(user_id),
        )
        stmt = _keyset_page(stmt, limit=limit, before=before)
        return await _tables_with_shares(db, stmt)


async def update_table_metadata(
    table_id: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    require_approval: Optional[bool] = None,
    expected_updated_at: Optional[str] = None,
) -> Optional[dict]:
    """Rename a table, change its description and/or flip ``require_approval``.

    ``expected_updated_at``, when given, must equal the row's current
    ``updated_at`` ISO string, else :class:`StaleTableError` (carrying the
    current row) is raised before anything changes -- also when another
    writer lands between the read and this write (the UPDATE is
    conditional on the ``updated_at`` that was read). ``updated_at`` is
    bumped only when a field actually changes.

    Returns:
        The updated table dict with shares, or None when it does not exist.

    Raises:
        TableValidationError, DuplicateTableTitleError, StaleTableError.
    """
    clean_title = _validate_title(title) if title is not None else None
    clean_description = (
        _validate_description(description) if description is not None else None
    )
    if require_approval is not None and not isinstance(require_approval, bool):
        raise TableValidationError("require_approval must be a boolean.")

    async with AsyncSessionLocal() as db:
        table = await db.get(DataTable, table_id)
        if table is None:
            return None

        if expected_updated_at is not None and _iso(table.updated_at) != expected_updated_at:
            raise StaleTableError(current=await _table_dict_with_shares(db, table))

        values = {}
        if clean_title is not None and clean_title != table.title:
            if await _title_taken(
                db, table.owner_id, table.project_id, table.mode, clean_title,
                exclude_table_id=table.id,
            ):
                raise _duplicate_title_error(clean_title, table.project_id)
            values["title"] = clean_title
        if clean_description is not None and clean_description != table.description:
            values["description"] = clean_description
        if require_approval is not None and require_approval != bool(table.require_approval):
            values["require_approval"] = require_approval

        if values:
            # Conditional UPDATE: with a token, the row must still carry the
            # updated_at read above, so a writer that landed in between makes
            # this a stale update instead of a silent lost update.
            stmt = update(DataTable).where(DataTable.id == table_id)
            if expected_updated_at is not None:
                stmt = stmt.where(DataTable.updated_at == table.updated_at)
            values["updated_at"] = datetime.now(timezone.utc)
            result = await db.execute(
                stmt.values(**values).execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await db.rollback()
                current = await db.get(DataTable, table_id, populate_existing=True)
                if current is None:
                    return None
                raise StaleTableError(current=await _table_dict_with_shares(db, current))
            await db.commit()
            await db.refresh(table)
        return await _table_dict_with_shares(db, table)


async def update_after_write(
    table_id: str,
    *,
    row_count: int,
    last_write_source: Optional[str],
) -> Optional[dict]:
    """Record a landed row write (or row revert): ``row_count``, source,
    ``updated_at`` (always bumped).

    Returns:
        The table dict with shares, or None when the table does not exist.
    """
    _validate_count(row_count, "row_count")
    async with AsyncSessionLocal() as db:
        table = await db.get(DataTable, table_id)
        if table is None:
            return None
        table.row_count = row_count
        table.last_write_source = last_write_source
        table.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(table)
        return await _table_dict_with_shares(db, table)


async def update_schema(
    table_id: str,
    *,
    schema: list,
    expected_schema_version: int,
    last_write_source: Optional[str],
    row_count: Optional[int] = None,
) -> Optional[dict]:
    """Record a landed schema change: store ``schema``, bump
    ``schema_version`` by one, set the source and bump ``updated_at``.

    ``expected_schema_version`` is the concurrency token: it must equal the
    row's current ``schema_version``, else :class:`StaleTableSchemaError`
    (carrying the current row) is raised before anything changes.
    ``row_count`` is updated too when given (a revert restoring rows).

    Returns:
        The table dict with shares, or None when the table does not exist.

    Raises:
        TableValidationError, StaleTableSchemaError.
    """
    _validate_schema(schema)
    _validate_count(expected_schema_version, "expected_schema_version")
    if row_count is not None:
        _validate_count(row_count, "row_count")
    values = {
        DataTable.table_schema: schema,
        DataTable.schema_version: DataTable.schema_version + 1,
        DataTable.last_write_source: last_write_source,
        DataTable.updated_at: datetime.now(timezone.utc),
    }
    if row_count is not None:
        values[DataTable.row_count] = row_count
    async with AsyncSessionLocal() as db:
        # One conditional UPDATE: of two writers holding the same version,
        # exactly one matches the WHERE; the other sees rowcount 0.
        result = await db.execute(
            update(DataTable)
            .where(
                DataTable.id == table_id,
                DataTable.schema_version == expected_schema_version,
            )
            .values(values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            current = await db.get(DataTable, table_id, populate_existing=True)
            if current is None:
                return None
            raise StaleTableSchemaError(current=await _table_dict_with_shares(db, current))
        await db.commit()
        table = await db.get(DataTable, table_id, populate_existing=True)
        return await _table_dict_with_shares(db, table)


async def delete_table(table_id: str) -> bool:
    """Delete a table row and its shares. Returns False when it did not exist.

    The directory is NOT touched; callers remove it after this returns.
    """
    async with AsyncSessionLocal() as db:
        table = await db.get(DataTable, table_id)
        if table is None:
            return False
        # Explicit, so the shares go even on a connection without
        # PRAGMA foreign_keys (the FK cascade covers the normal case).
        await db.execute(delete(DataTableShare).where(DataTableShare.table_id == table_id))
        await db.delete(table)
        await db.commit()
        return True


async def list_table_ids_for_project(project_id: str) -> list[str]:
    """Ids of every table in a project (collect BEFORE deleting the project,
    then sweep the directories)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DataTable.id)
            .where(DataTable.project_id == project_id)
            .order_by(DataTable.id)
        )
        return list(result.scalars().all())


async def list_table_ids_for_user(user_id: int) -> list[str]:
    """Ids of every table the user owns, user and project tables alike
    (collect BEFORE deleting the account, then sweep the directories)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DataTable.id)
            .where(DataTable.owner_id == user_id)
            .order_by(DataTable.id)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Shares
# ---------------------------------------------------------------------------


async def add_share(table_id: str, user_id: Optional[int], permission: str) -> dict:
    """Grant ``permission`` on a table to one user, or to everyone (None).

    Upsert semantics: an existing grant for the same recipient (including
    the single "everyone" row) has its permission updated instead of a
    second row being inserted. Sharing a table with its own owner is
    refused. Does not bump the table's ``updated_at``.

    Returns:
        The share dict ``{"id", "user_id", "permission", "created_at"}``.

    Raises:
        TableValidationError: bad permission, unknown table, or owner recipient.
        ShareTargetGoneError: (a TableValidationError) the table or the
            recipient was deleted while the row was being inserted.
    """
    _validate_permission(permission)
    if user_id is not None and (isinstance(user_id, bool) or not isinstance(user_id, int)):
        raise TableValidationError("Share user_id must be an integer or None.")

    async with AsyncSessionLocal() as db:
        table = await db.get(DataTable, table_id)
        if table is None:
            raise TableValidationError(table_not_found_message(table_id))
        if user_id is not None and user_id == table.owner_id:
            raise TableValidationError("A table cannot be shared with its owner.")

        stmt = select(DataTableShare).where(DataTableShare.table_id == table_id)
        if user_id is None:
            stmt = stmt.where(DataTableShare.user_id.is_(None))
        else:
            stmt = stmt.where(DataTableShare.user_id == user_id)
        stmt = stmt.order_by(DataTableShare.id)

        async def _update(existing: DataTableShare) -> dict:
            if existing.permission != permission:
                existing.permission = permission
                await db.commit()
                await db.refresh(existing)
            return _share_to_dict(existing)

        existing = (await db.execute(stmt)).scalars().first()
        if existing is not None:
            return await _update(existing)

        share = DataTableShare(
            table_id=table_id,
            user_id=user_id,
            permission=permission,
            created_at=datetime.now(timezone.utc),
        )
        db.add(share)
        try:
            await db.commit()
        except IntegrityError as exc:
            # A concurrent add_share won the unique index (per-user row or
            # the partial "everyone" index): fall back to updating that row.
            await db.rollback()
            existing = (await db.execute(stmt)).scalars().first()
            if existing is None:
                # No competing row, so a foreign key refused the insert.
                raise ShareTargetGoneError(
                    "The table or the share recipient no longer exists."
                ) from exc
            return await _update(existing)
        await db.refresh(share)
        return _share_to_dict(share)


async def remove_share(table_id: str, share_id: int) -> bool:
    """Delete one share row of a table. False when no such row on that table.

    Like :func:`add_share`, does not bump the table's ``updated_at``."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(DataTableShare).where(
                DataTableShare.id == share_id, DataTableShare.table_id == table_id,
            )
        )
        await db.commit()
        return result.rowcount > 0


async def list_shares(table_id: str) -> list[dict]:
    """All share rows of a table, oldest first."""
    async with AsyncSessionLocal() as db:
        shares = (await _load_shares(db, [table_id]))[table_id]
        return [_share_to_dict(s) for s in shares]
