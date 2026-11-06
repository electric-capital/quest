"""Password hashing for email/password sign-in.

scrypt from ``hashlib`` (memory-hard, no third-party dependency), stored as
a self-describing string ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>`` so the
cost parameters can be raised later without invalidating existing hashes.

Like ``config/encryption.py``, this module is stdlib-only.
"""

import base64
import hashlib
import hmac
import secrets

# scrypt cost: n=2**15, r=8 -> 32 MiB of memory and ~50-100 ms per hash.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        # OpenSSL's default 32 MiB cap is exactly 128*n*r; leave headroom.
        maxmem=256 * n * r,
        dklen=_HASH_BYTES,
    )


def password_problem(password: str) -> str | None:
    """Why *password* is unacceptable, or None when it is fine.

    Length-only policy (NIST SP 800-63B): no composition rules.
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    return None


def hash_password(password: str) -> str:
    """Hash *password* with a fresh random salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of *password* against a stored hash string.

    Malformed or missing hashes never verify.
    """
    if not stored or not isinstance(password, str):
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = _scrypt(password, salt, int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def burn_verify_time(password: str) -> None:
    """Spend the same time as a real verification (unknown-account logins).

    Keeps the login endpoint's response time from revealing whether an
    account exists.
    """
    _scrypt(password or "", b"\0" * _SALT_BYTES, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


def password_fingerprint(stored: str | None) -> str | None:
    """Short, non-reversible tag of the current password hash.

    Embedded in password-issued session cookies: a password change
    produces a new salt and therefore a new fingerprint, which invalidates
    every session issued under the old password.
    """
    if not stored:
        return None
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:16]
