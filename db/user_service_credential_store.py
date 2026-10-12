"""Data access layer for per-user plugin service credentials.

Plugin-contributed services store their per-user credentials in the
``user_service_credentials`` table instead of dedicated ``users`` columns.
The generic key routes in ``auth/service_key.py`` write here,
``db/user_store.py`` attaches the rows to every user dict, and
``get_user_connected_services()`` feeds each default row to the plugin's
``UserConnectionSpec.connected`` predicate.

A (user, service) holds one or more rows, one per **connection**, told
apart by ``account`` (the account address or a user-chosen name, stored
lower-cased so the unique ``(user_id, service, account)`` index alone makes
it case-insensitive). Exactly one row per (user, service) is the
**default** (``is_default``); a partial unique index forbids two defaults
and every function here keeps exactly one while any row exists (the first
row created is the default, deleting the default promotes the oldest
remaining row).

Two layers of API:

- The single-connection functions (``get_credential``, ``list_credentials``,
  ``upsert_credential``, ``delete_credential``, ``delete_all_credentials``)
  keep their historical signatures and operate on the DEFAULT row, except
  ``delete_credential``, which removes every row of the service. Plugins
  that are not multi-connection keep calling these and see exactly one
  row, as before.
- The connection functions (``list_connections``, ``list_all_connections``,
  ``get_connection``, ``create_connection``, ``update_connection``,
  ``set_default_connection``, ``delete_connection``,
  ``upsert_connection_by_account``) address individual rows.

Row dicts are ``UserServiceCredential.to_dict()``. Connection lists are
ordered default first, then by ``created_at``, then by ``id``.
"""

from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from db.engine import AsyncSessionLocal
from db.models import UserServiceCredential

MAX_ACCOUNT_LENGTH = 320
MAX_DESCRIPTION_LENGTH = 200

# Sentinel for "leave this field unchanged" in partial updates (None is a
# meaningful value: it clears the field).
_UNSET: Any = object()


class ConnectionExists(Exception):
    """A connection with this account already exists for (user, service)."""

    def __init__(self, service: str, account: str):
        self.service = service
        self.account = account
        super().__init__(
            f"A {service} connection for {account!r} already exists"
        )


class ConnectionLimit(Exception):
    """The (user, service) already holds the maximum number of connections."""

    def __init__(self, service: str, limit: int):
        self.service = service
        self.limit = limit
        super().__init__(
            f"At most {limit} {service} connections are allowed"
        )


def normalize_account(value: str) -> str:
    """Canonical stored form of an account: stripped and lower-cased.

    Raises ValueError when the result is empty or longer than 320
    characters (the column width; the longest valid email address).
    """
    if not isinstance(value, str):
        raise ValueError("account must be a string")
    account = value.strip().lower()
    if not account:
        raise ValueError("account must not be empty")
    if len(account) > MAX_ACCOUNT_LENGTH:
        raise ValueError(
            f"account must be at most {MAX_ACCOUNT_LENGTH} characters"
        )
    return account


def normalize_description(value: Optional[str]) -> Optional[str]:
    """Canonical stored form of a description: one line, trimmed.

    Every whitespace run (newlines included) collapses to one space; an
    empty result is None. Raises ValueError when longer than 200
    characters after normalization.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("description must be a string")
    description = " ".join(value.split())
    if not description:
        return None
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(
            f"description must be at most {MAX_DESCRIPTION_LENGTH} characters"
        )
    return description


def _lookup_account(value: str) -> str:
    """Lookup form of an account (no emptiness check: legacy rows are '')."""
    return value.strip().lower() if isinstance(value, str) else ""


_ORDER = (
    UserServiceCredential.is_default.desc(),
    UserServiceCredential.created_at,
    UserServiceCredential.id,
)


def _service_rows(user_id: int, service: str):
    return (
        select(UserServiceCredential)
        .where(
            UserServiceCredential.user_id == user_id,
            UserServiceCredential.service == service,
        )
        .order_by(*_ORDER)
    )


async def _list_service_rows(db, user_id: int, service: str) -> list:
    """The (user, service) rows in list order, as ORM objects."""
    result = await db.execute(_service_rows(user_id, service))
    return list(result.scalars().all())


async def _default_row(db, user_id: int, service: str):
    result = await db.execute(_service_rows(user_id, service).limit(1))
    return result.scalars().first()


async def _get_row(db, user_id: int, service: str, row_id: int):
    result = await db.execute(
        select(UserServiceCredential).where(
            UserServiceCredential.id == row_id,
            UserServiceCredential.user_id == user_id,
            UserServiceCredential.service == service,
        )
    )
    return result.scalars().first()


async def _clear_default(db, user_id: int, service: str) -> None:
    """Unset the current default and flush, so a new default can be set
    without tripping the partial unique index."""
    await db.execute(
        update(UserServiceCredential)
        .where(
            UserServiceCredential.user_id == user_id,
            UserServiceCredential.service == service,
            UserServiceCredential.is_default.is_(True),
        )
        .values(is_default=False)
        .execution_options(synchronize_session="fetch")
    )
    await db.flush()


# ---------------------------------------------------------------------------
# Single-connection API (operates on the default row)
# ---------------------------------------------------------------------------

async def get_credential(user_id: int, service: str) -> Optional[dict]:
    """Return the user's default connection row for a service, or None."""
    async with AsyncSessionLocal() as db:
        row = await _default_row(db, user_id, service)
        return row.to_dict() if row else None


async def list_credentials(user_id: int) -> dict[str, dict]:
    """Return the user's default connection rows as a service -> row map."""
    connections = await list_all_connections(user_id)
    return {service: rows[0] for service, rows in connections.items()}


async def upsert_credential(
    user_id: int,
    service: str,
    *,
    secret: Optional[str] = None,
    oauth_blob: Optional[dict] = None,
) -> dict:
    """Create or replace the user's DEFAULT connection row for a service.

    Both credential fields are written verbatim (a full replacement, not a
    merge): saving a new API key clears any stale ``oauth_blob`` and vice
    versa. When the service has no row yet, the new row is created as the
    default with account ''. ``account`` and ``description`` of an
    existing row are left alone.
    """
    for attempt in range(2):
        try:
            return await _write_default_credential(
                user_id, service, secret=secret, oauth_blob=oauth_blob,
            )
        except IntegrityError:
            # A concurrent writer created the first row between our read and
            # our commit: retry once, which now updates that default row.
            if attempt:
                raise
    raise AssertionError("unreachable")


async def _write_default_credential(
    user_id: int, service: str, *, secret: Optional[str], oauth_blob: Optional[dict],
) -> dict:
    async with AsyncSessionLocal() as db:
        row = await _default_row(db, user_id, service)
        if row is None:
            row = UserServiceCredential(
                user_id=user_id, service=service, account="", is_default=True,
            )
            db.add(row)
        row.secret = secret
        row.oauth_blob = oauth_blob
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise
        await db.refresh(row)
        return row.to_dict()


async def delete_credential(user_id: int, service: str) -> bool:
    """Delete EVERY connection row of a service (service-wide disconnect).

    Returns True if any row was removed.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(UserServiceCredential).where(
                UserServiceCredential.user_id == user_id,
                UserServiceCredential.service == service,
            )
        )
        await db.commit()
        return result.rowcount > 0


async def delete_all_credentials(user_id: int) -> int:
    """Delete all of a user's credential rows (disconnect-all paths).

    Account deletion does not need this -- the FK cascades -- but the
    logout-and-disconnect flow removes connector credentials while keeping
    the account.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(UserServiceCredential).where(
                UserServiceCredential.user_id == user_id
            )
        )
        await db.commit()
        return result.rowcount


# ---------------------------------------------------------------------------
# Connection API (addresses individual rows)
# ---------------------------------------------------------------------------

async def list_connections(user_id: int, service: str) -> list[dict]:
    """All of the user's connections to a service, default first."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(_service_rows(user_id, service))
        return [row.to_dict() for row in result.scalars().all()]


async def fetch_all_connections(db, user_id: int) -> dict[str, list[dict]]:
    """``list_all_connections`` inside a caller-owned session (one query).

    Used by ``db/user_store.py::_user_dict``, which already holds a session.
    """
    result = await db.execute(
        select(UserServiceCredential)
        .where(UserServiceCredential.user_id == user_id)
        .order_by(UserServiceCredential.service, *_ORDER)
    )
    connections: dict[str, list[dict]] = {}
    for row in result.scalars().all():
        connections.setdefault(row.service, []).append(row.to_dict())
    return connections


async def list_all_connections(user_id: int) -> dict[str, list[dict]]:
    """All of the user's connections as service -> rows (default first)."""
    async with AsyncSessionLocal() as db:
        return await fetch_all_connections(db, user_id)


async def get_connection(
    user_id: int,
    service: str,
    *,
    account: Optional[str] = None,
    row_id: Optional[int] = None,
) -> Optional[dict]:
    """One connection by account (case-insensitive) and/or row id.

    When both are given the row must match both. Raises ValueError when
    neither is given.
    """
    if account is None and row_id is None:
        raise ValueError("get_connection needs an account or a row_id")
    stmt = select(UserServiceCredential).where(
        UserServiceCredential.user_id == user_id,
        UserServiceCredential.service == service,
    )
    if row_id is not None:
        stmt = stmt.where(UserServiceCredential.id == row_id)
    if account is not None:
        stmt = stmt.where(
            UserServiceCredential.account == _lookup_account(account)
        )
    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        row = result.scalars().first()
        return row.to_dict() if row else None


async def create_connection(
    user_id: int,
    service: str,
    *,
    account: str,
    description: Optional[str] = None,
    secret: Optional[str] = None,
    oauth_blob: Optional[dict] = None,
    make_default: Optional[bool] = None,
    limit: Optional[int] = None,
) -> dict:
    """Create a new connection row.

    ``account`` and ``description`` are normalized (ValueError when
    invalid). The first row of a (user, service) is always the default;
    a later row becomes the default only with ``make_default=True`` (the
    old default is cleared in the same transaction). ``limit`` is the
    caller's per-(user, service) cap (None = no cap).

    Raises ConnectionExists when the account is already connected and
    ConnectionLimit when the (user, service) already holds ``limit`` rows.
    """
    account = normalize_account(account)
    description = normalize_description(description)
    for attempt in range(2):
        try:
            return await _insert_connection(
                user_id, service, account=account, description=description,
                secret=secret, oauth_blob=oauth_blob,
                make_default=make_default, limit=limit,
            )
        except IntegrityError:
            # The session is closed (and rolled back) by now. Lost a race
            # with a concurrent create of the same account: report it like
            # the pre-check would have.
            if await get_connection(user_id, service, account=account):
                raise ConnectionExists(service, account)
            # Otherwise a concurrent first row (create_connection or
            # upsert_credential) claimed the default between our read and
            # our commit: retry once so the default status is recomputed.
            if attempt:
                raise
    raise AssertionError("unreachable")


async def _insert_connection(
    user_id: int,
    service: str,
    *,
    account: str,
    description: Optional[str],
    secret: Optional[str],
    oauth_blob: Optional[dict],
    make_default: Optional[bool],
    limit: Optional[int],
) -> dict:
    """One create attempt in its own session (already-normalized inputs).

    Raises IntegrityError (after the session has rolled back and closed)
    when a concurrent writer tripped a unique index.
    """
    async with AsyncSessionLocal() as db:
        existing = await _list_service_rows(db, user_id, service)
        if any(row.account == account for row in existing):
            raise ConnectionExists(service, account)
        # Best-effort under concurrency: two creates racing past this check
        # can each insert, overshooting the cap by the number of concurrent
        # writers (bounded; nothing relies on the cap for safety).
        if limit is not None and len(existing) >= limit:
            raise ConnectionLimit(service, limit)
        is_default = not existing or bool(make_default)
        if is_default and existing:
            await _clear_default(db, user_id, service)
        row = UserServiceCredential(
            user_id=user_id,
            service=service,
            account=account,
            description=description,
            is_default=is_default,
            secret=secret,
            oauth_blob=oauth_blob,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise
        await db.refresh(row)
        return row.to_dict()


async def update_connection(
    user_id: int,
    service: str,
    row_id: int,
    *,
    description: Optional[str] = _UNSET,
    secret: Optional[str] = _UNSET,
    oauth_blob: Optional[dict] = _UNSET,
) -> Optional[dict]:
    """Partially update one connection; omitted fields stay unchanged.

    Never touches ``account`` or ``is_default``. Returns None when the row
    does not exist (for this user and service).
    """
    if description is not _UNSET:
        description = normalize_description(description)
    async with AsyncSessionLocal() as db:
        row = await _get_row(db, user_id, service, row_id)
        if row is None:
            return None
        if description is not _UNSET:
            row.description = description
        if secret is not _UNSET:
            row.secret = secret
        if oauth_blob is not _UNSET:
            row.oauth_blob = oauth_blob
        await db.commit()
        await db.refresh(row)
        return row.to_dict()


async def set_default_connection(
    user_id: int, service: str, row_id: int,
) -> Optional[dict]:
    """Make one connection the default (one transaction). None if missing."""
    async with AsyncSessionLocal() as db:
        row = await _get_row(db, user_id, service, row_id)
        if row is None:
            return None
        if not row.is_default:
            await _clear_default(db, user_id, service)
            row.is_default = True
            await db.commit()
            await db.refresh(row)
        return row.to_dict()


async def delete_connection(user_id: int, service: str, row_id: int) -> bool:
    """Delete one connection; promote the oldest remaining row if it was the
    default (same transaction). Returns False when the row does not exist.
    """
    async with AsyncSessionLocal() as db:
        row = await _get_row(db, user_id, service, row_id)
        if row is None:
            return False
        was_default = bool(row.is_default)
        await db.delete(row)
        await db.flush()
        if was_default:
            result = await db.execute(
                select(UserServiceCredential)
                .where(
                    UserServiceCredential.user_id == user_id,
                    UserServiceCredential.service == service,
                )
                .order_by(
                    UserServiceCredential.created_at, UserServiceCredential.id,
                )
                .limit(1)
            )
            successor = result.scalars().first()
            if successor is not None:
                successor.is_default = True
        await db.commit()
        return True


async def upsert_connection_by_account(
    user_id: int,
    service: str,
    *,
    account: str,
    description: Optional[str] = _UNSET,
    secret: Optional[str] = _UNSET,
    oauth_blob: Optional[dict] = _UNSET,
    limit: Optional[int] = None,
) -> dict:
    """Update the connection with this account in place, else create it.

    The OAuth-callback helper: re-authorising an already-connected account
    refreshes its credential instead of failing with ConnectionExists.
    Omitted fields stay unchanged on update and are empty on create. A new
    row follows ``create_connection`` rules (default only when first;
    ConnectionLimit beyond ``limit``).
    """
    account = normalize_account(account)
    for _attempt in range(2):
        existing = await get_connection(user_id, service, account=account)
        if existing is not None:
            updated = await update_connection(
                user_id, service, existing["id"],
                description=description, secret=secret, oauth_blob=oauth_blob,
            )
            if updated is not None:
                return updated
            continue  # deleted between the read and the write: create it
        try:
            return await create_connection(
                user_id, service,
                account=account,
                description=None if description is _UNSET else description,
                secret=None if secret is _UNSET else secret,
                oauth_blob=None if oauth_blob is _UNSET else oauth_blob,
                limit=limit,
            )
        except ConnectionExists:
            continue  # created concurrently: update it on the next pass
    raise ConnectionExists(service, account)
