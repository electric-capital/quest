"""Data access for email/password sign-in: password hashes and one-time links.

The password hash never leaves this module except as the fingerprint on the
user dict (``password_fp``, see ``User.to_dict``). Setting a password also
advances the account's ``session_epoch``. One-time set-password links live
in ``password_tokens``; only their SHA-256 is stored.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, or_, select, update

from db.engine import AsyncSessionLocal
from db.models import PasswordToken, User

TOKEN_PURPOSES = ("invite", "reset")


def _now() -> datetime:
    # Naive UTC: SQLite DateTime columns round-trip without tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get_password_hash(email: str) -> Optional[str]:
    """The stored hash for *email*'s account, or None (no account / no password)."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User.password_hash).where(User.email == email)
        )
        return result.scalar_one_or_none()


async def set_password_hash(email: str, password_hash: str) -> bool:
    """Store a new hash, end every session of the account and void every
    outstanding link for it.

    Sessions end by bumping ``session_epoch`` (see get_user_from_cookie in
    auth/session.py), which also covers sessions issued without a password
    fingerprint. Returns False when no account has this email.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(User).where(User.email == email).values(
                password_hash=password_hash,
                session_epoch=User.session_epoch + 1,
            )
        )
        await db.execute(
            update(PasswordToken)
            .where(PasswordToken.email == email, PasswordToken.used_at.is_(None))
            .values(used_at=_now())
        )
        await db.commit()
        return result.rowcount > 0


async def create_password_token(email: str, purpose: str, ttl: timedelta) -> str:
    """Mint a one-time link token for *email*; returns the raw token."""
    if purpose not in TOKEN_PURPOSES:
        raise ValueError(f"Unknown password token purpose: {purpose!r}")
    raw = secrets.token_urlsafe(32)
    now = _now()
    async with AsyncSessionLocal() as db:
        db.add(PasswordToken(
            email=email,
            token_hash=_hash_token(raw),
            purpose=purpose,
            created_at=now,
            expires_at=now + ttl,
        ))
        await db.commit()
    return raw


async def get_valid_password_token(raw: str) -> Optional[dict]:
    """``{"email", "purpose", "expires_at"}`` for an unused, unexpired token."""
    if not raw:
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PasswordToken).where(PasswordToken.token_hash == _hash_token(raw))
        )
        row = result.scalars().first()
        if row is None or row.used_at is not None or row.expires_at <= _now():
            return None
        return {"email": row.email, "purpose": row.purpose, "expires_at": row.expires_at}


async def consume_password_token(raw: str) -> Optional[dict]:
    """Atomically mark a valid token used; returns its info or None.

    The conditional UPDATE makes a double-submit race resolve to exactly
    one winner.
    """
    info = await get_valid_password_token(raw)
    if info is None:
        return None
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(PasswordToken)
            .where(
                PasswordToken.token_hash == _hash_token(raw),
                PasswordToken.used_at.is_(None),
            )
            .values(used_at=_now())
        )
        await db.commit()
        if result.rowcount != 1:
            return None
    return info


async def purge_expired_password_tokens() -> int:
    """Delete tokens that expired or were used more than a day ago."""
    cutoff = _now() - timedelta(days=1)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(PasswordToken).where(
                or_(PasswordToken.expires_at < cutoff, PasswordToken.used_at < cutoff)
            )
        )
        await db.commit()
        return result.rowcount
