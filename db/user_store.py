"""User data access layer, replacing JSON file operations.

Provides the same function signatures as the original load_users/save_users/
get_user_by_api_key/get_user_by_email functions, but backed by async SQLite.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import select, or_

from config.encryption import hash_api_key
from db.engine import AsyncSessionLocal
from db.models import User, UserServiceCredential


async def _user_dict(db, user: User) -> dict:
    """Project a User row to its dict, attaching plugin service credentials.

    Per-user credentials for plugin services live in the
    ``user_service_credentials`` table (plugins cannot add users columns);
    they ride on the user dict as ``service_credentials`` -- a service ->
    row-dict map, present only when non-empty (matching to_dict's
    omit-when-absent style) -- so the sync ``get_user_connected_services()``
    can evaluate plugin connection predicates without a DB round-trip. The
    extra query is skipped entirely while no loaded plugin declares a
    per-user connection.
    """
    d = user.to_dict()
    from config.plugins import get_loaded_plugins
    if not any(p.user_connection is not None for p in get_loaded_plugins()):
        return d
    result = await db.execute(
        select(UserServiceCredential).where(
            UserServiceCredential.user_id == user.id
        )
    )
    rows = result.scalars().all()
    if rows:
        d["service_credentials"] = {row.service: row.to_dict() for row in rows}
    return d


# ---------------------------------------------------------------------------
# Drop-in replacements for the original four functions
# ---------------------------------------------------------------------------

async def get_user_by_api_key(api_key: str) -> Optional[dict]:
    """Find a user by their API key. Returns user dict or None.

    ``users.api_key`` is encrypted at rest with a random nonce, so the
    lookup goes through the SHA-256 ``api_key_hash`` column instead.
    """
    if not api_key:
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.api_key_hash == hash_api_key(api_key))
        )
        user = result.scalars().first()
        return await _user_dict(db, user) if user else None


async def get_user_by_email(email: str) -> Optional[dict]:
    """Find a user by their email. Returns user dict or None."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        return await _user_dict(db, user) if user else None


async def get_user_by_id(user_id: int) -> Optional[dict]:
    """Find a user by their integer ID. Returns user dict or None."""
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        return await _user_dict(db, user) if user else None


async def get_user_by_slack_user_id(slack_user_id: str) -> Optional[dict]:
    """Find the Quest user whose Slack OAuth user_id matches.

    Used by the Slack Socket Mode worker to resolve the Quest account
    for an incoming DM. Per-user Slack OAuth data lives in the generic
    ``user_service_credentials`` table (service="slack", token JSON in
    ``oauth_blob`` -- written by the Slack plugin's OAuth callback), so
    this matches against the row's blob rather than a users column.
    Returns None if no user has connected Slack with that user_id.
    """
    if not slack_user_id:
        return None
    async with AsyncSessionLocal() as db:
        # oauth_blob is encrypted at rest, so SQL cannot json_extract the
        # user_id: load every Slack row (one per connected user -- a small
        # set) and match after the ORM has decrypted the blobs.
        result = await db.execute(
            select(UserServiceCredential).where(
                UserServiceCredential.service == "slack"
            )
        )
        match = next(
            (
                row for row in result.scalars().all()
                if isinstance(row.oauth_blob, dict)
                and row.oauth_blob.get("user_id") == slack_user_id
            ),
            None,
        )
        if match is None:
            return None
        user = await db.get(User, match.user_id)
        return await _user_dict(db, user) if user else None


async def list_all_users() -> list[dict]:
    """Return all users with safe fields only (id, email, name).

    Ordered by name. No limit since the app has a small, bounded user base.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).order_by(User.name))
        users = result.scalars().all()
        return [
            {"id": u.id, "email": u.email, "name": u.name}
            for u in users
        ]


async def list_user_signup_dates() -> dict[int, datetime]:
    """Return every user's ``created_at`` keyed by user id.

    Feeds the admin Total Usage report's new-user counts; a narrow
    projection so the roster lookup stays cheap next to ``list_all_users``.
    Timestamps come back as stored (naive UTC).
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User.id, User.created_at))
        return {user_id: created_at for user_id, created_at in result.all()}


async def search_users(
    query: str,
    exclude_user_id: Optional[int] = None,
    limit: int = 10,
) -> list[dict]:
    """Search users by name or email (case-insensitive substring match).

    Returns a list of ``{"id": int, "email": str, "name": str}`` dicts.
    Sensitive fields (api_key, settings, OAuth data) are never included.

    Returns an empty list if *query* is fewer than 2 characters.
    """
    if len(query) < 2:
        return []

    pattern = f"%{query}%"
    async with AsyncSessionLocal() as db:
        stmt = select(User).where(
            or_(
                User.name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
        if exclude_user_id is not None:
            stmt = stmt.where(User.id != exclude_user_id)
        stmt = stmt.limit(limit)

        result = await db.execute(stmt)
        users = result.scalars().all()
        return [
            {"id": u.id, "email": u.email, "name": u.name}
            for u in users
        ]


# ---------------------------------------------------------------------------
# Granular update functions (replacing load_users/modify/save_users pattern)
# ---------------------------------------------------------------------------

async def create_user(
    email: str,
    name: str,
    api_key: str,
    google_oauth: Optional[dict] = None,
    settings: Optional[dict] = None,
    created_at=None,
    google_sub: Optional[str] = None,
) -> dict:
    """Create a new user record. Returns user dict."""
    async with AsyncSessionLocal() as db:
        user = User(
            email=email,
            name=name,
            api_key=api_key,
            google_oauth=google_oauth,
            settings=settings or {},
            google_sub=google_sub,
        )
        if created_at:
            user.created_at = created_at
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return await _user_dict(db, user)


async def update_user_field(email: str, **fields) -> Optional[dict]:
    """Update specific fields on a user record.

    Usage:
        update_user_field("user@example.com", name="New Name", settings={"key": "val"})
        update_user_field("user@example.com", google_oauth={...})
        update_user_field("user@example.com", api_key="new_key")

    Returns updated user dict, or None if user not found.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if not user:
            return None
        for key, value in fields.items():
            if hasattr(user, key):
                setattr(user, key, value)
        await db.commit()
        await db.refresh(user)
        return await _user_dict(db, user)


async def update_user_settings(email: str, settings_update: dict) -> Optional[dict]:
    """Merge new settings into existing user settings (partial update).

    Returns updated user dict, or None if user not found.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if not user:
            return None
        # Create a new dict to ensure SQLAlchemy detects the mutation
        current_settings = dict(user.settings or {})
        current_settings.update(settings_update)
        user.settings = current_settings
        await db.commit()
        await db.refresh(user)
        return await _user_dict(db, user)


async def remove_user_fields(email: str, fields: list[str]) -> Optional[dict]:
    """Set specific fields to None (e.g., removing connector tokens).

    Usage:
        remove_user_fields("user@example.com",
                           ["google_services_oauth", "airtable_token"])

    Returns updated user dict, or None if user not found.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if not user:
            return None
        for field in fields:
            if hasattr(user, field):
                setattr(user, field, None)
        await db.commit()
        await db.refresh(user)
        return await _user_dict(db, user)


async def delete_user(email: str) -> bool:
    """Delete a user record. Returns True if deleted, False if not found."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if not user:
            return False
        await db.delete(user)
        await db.commit()
        return True
