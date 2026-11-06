"""Encryption at rest for stored secrets.

Every secret Quest persists -- per-user OAuth token blobs and API keys in
the database, the admin-managed service / inference credential files -- is
encrypted with a single random 256-bit **data-encryption key (DEK)**. The
DEK never touches disk in the clear: it is wrapped (AES-256-GCM) with a
key derived from an operator-chosen **password** via Argon2id and stored
in ``<data_dir>/encryption_key.json``. Rotating the password re-wraps the
DEK; nothing else has to be re-encrypted.

Password resolution at startup (``resolve_password()``), first match wins:

1. ``QUEST_ENCRYPTION_PASSWORD`` environment variable;
2. the file named by ``QUEST_ENCRYPTION_PASSWORD_FILE`` (systemd
   ``LoadCredential`` / secret-manager friendly);
3. in local mode only, the fixed ``LOCAL_DEV_PASSWORD`` sentinel (the
   local data dir is a throwaway, so this is deliberately a public value;
   it is REFUSED in staging/prod both when creating a key file and when
   unlocking one);
4. an interactive prompt when stdin is a TTY (``run.py`` does this and
   exports the answer for the alembic / uvicorn / seeder subprocesses);
5. otherwise startup fails with :class:`PasswordNotAvailableError`.

Ciphertext format (``encrypt_str``): ``qenc1:<key_id>:<base64url(nonce ||
ciphertext || tag)>``. The ``key_id`` is a random identifier minted with
the key file so a value encrypted under a different key fails with a clear
error rather than a bare tag mismatch. Each call uses a fresh random
96-bit nonce. Callers pass an ``aad`` label (e.g. ``"users.google_oauth"``)
bound into the GCM tag so a ciphertext copied into another column is
rejected. Anything not starting with ``qenc1:`` is treated by the readers
as legacy plaintext (see ``is_encrypted``) -- the one-shot Alembic
migration and the startup file sweep encrypt those, and the tolerant read
covers any row written by a pre-encryption process still running during an
upgrade.

Threat model: this protects a copied database file / credential directory
/ backup. It does NOT protect against an attacker on the running host, who
can read the DEK from process memory or the password from the environment.

Top-level imports here are standard library only so ``run.py`` (which must
stay stdlib-only) can import the constants and password resolution; the
``cryptography`` package is imported lazily inside the functions that need
it.

CLI (run by ``run.py`` before migrations)::

    uv run python -m config.encryption init             # create or verify
    uv run python -m config.encryption check            # verify only
    uv run python -m config.encryption rotate-password  # re-wrap the DEK
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.environment import is_local
from config.paths import DATA_DIR

logger = logging.getLogger(__name__)

# Environment variables consulted by resolve_password() / encryption_key_path().
PASSWORD_ENV = "QUEST_ENCRYPTION_PASSWORD"
PASSWORD_FILE_ENV = "QUEST_ENCRYPTION_PASSWORD_FILE"
KEY_FILE_ENV = "QUEST_ENCRYPTION_KEY_FILE"

KEY_FILE_NAME = "encryption_key.json"

# Fixed password for local-mode runs. Local data directories are throwaway
# and pre-baked from dev-config.json, so local secrets are effectively
# unencrypted -- this value is public by design. It is refused outside
# local mode (see _check_password_allowed) so a local data dir copied into
# a real deployment can never leave production secrets behind a
# password that lives in the repo.
LOCAL_DEV_PASSWORD = "quest-local-dev-insecure"

ENVELOPE_PREFIX = "qenc1:"

# Argon2id parameters (OWASP-recommended class: 64 MiB, 3 passes, 4 lanes;
# roughly 0.1-0.3 s per derivation). Stored in the key file alongside the
# salt so they can be raised later without invalidating existing files.
_KDF_PARAMS = {"iterations": 3, "memory_cost": 64 * 1024, "lanes": 4, "length": 32}
_KEY_FILE_VERSION = 1
_WRAP_AAD = b"quest-encryption-key-file-v1"
_NONCE_BYTES = 12


class EncryptionError(Exception):
    """Base class for encryption-at-rest failures."""


class PasswordNotAvailableError(EncryptionError):
    """No password could be resolved from the environment or a prompt."""


class WrongPasswordError(EncryptionError):
    """The password does not unwrap the key file."""


class KeyFileMissingError(EncryptionError):
    """The key file does not exist and creation was not requested."""


class SentinelPasswordError(EncryptionError):
    """The local-mode sentinel password was used outside local mode."""


class KeyMismatchError(EncryptionError):
    """A ciphertext was produced under a different key than the loaded one."""


# ---------------------------------------------------------------------------
# Paths and password resolution
# ---------------------------------------------------------------------------

def encryption_key_path() -> Path:
    """Return the key file path (``QUEST_ENCRYPTION_KEY_FILE`` override, else
    ``<data_dir>/encryption_key.json``)."""
    override = os.environ.get(KEY_FILE_ENV)
    if override:
        return Path(override)
    return DATA_DIR / KEY_FILE_NAME


def _read_password_file(path: str) -> str | None:
    try:
        return Path(path).read_text().strip() or None
    except OSError as exc:
        raise PasswordNotAvailableError(
            f"Cannot read the encryption password file {path!r}: {exc}"
        ) from exc


def resolve_password(*, interactive: bool = False, confirm: bool = False) -> str | None:
    """Resolve the encryption password (see the module docstring for the order).

    ``interactive`` enables the TTY prompt fallback (only when stdin is a
    TTY); ``confirm`` asks twice, for key-file creation. Returns None when
    nothing is available and prompting is disabled or impossible.
    """
    env_value = os.environ.get(PASSWORD_ENV)
    if env_value:
        return env_value
    file_path = os.environ.get(PASSWORD_FILE_ENV)
    if file_path:
        value = _read_password_file(file_path)
        if value:
            return value
    if is_local():
        return LOCAL_DEV_PASSWORD
    if interactive and sys.stdin.isatty():
        return prompt_for_password(confirm=confirm)
    return None


def prompt_for_password(*, confirm: bool = False) -> str:
    """Prompt on the TTY for the encryption password (hidden input)."""
    while True:
        if confirm:
            print("No encryption key file found -- choose a password that will")
            print("protect the stored credentials. You will need it (or the")
            print(f"{PASSWORD_ENV} / {PASSWORD_FILE_ENV} environment")
            print("variable) on every startup.")
        password = getpass.getpass("Encryption password (input hidden): ").strip()
        if not password:
            print("! The password cannot be empty.")
            continue
        if password == LOCAL_DEV_PASSWORD and not is_local():
            print("! That value is the local-mode development sentinel and cannot be used here.")
            continue
        if not confirm:
            return password
        again = getpass.getpass("Confirm encryption password: ").strip()
        if again == password:
            return password
        print("! The passwords do not match; try again.")


def _check_password_allowed(password: str) -> None:
    if not password:
        raise PasswordNotAvailableError("The encryption password cannot be empty.")
    if password == LOCAL_DEV_PASSWORD and not is_local():
        raise SentinelPasswordError(
            "The local-mode development password cannot be used outside local "
            "mode. Set a real password via QUEST_ENCRYPTION_PASSWORD (or "
            "QUEST_ENCRYPTION_PASSWORD_FILE) for staging/prod."
        )


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def _derive_wrap_key(password: str, salt: bytes, params: dict) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    kdf = Argon2id(
        salt=salt,
        length=int(params["length"]),
        iterations=int(params["iterations"]),
        lanes=int(params["lanes"]),
        memory_cost=int(params["memory_cost"]),
    )
    return kdf.derive(password.encode("utf-8"))


def _wrap_dek(dek: bytes, password: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = secrets.token_bytes(16)
    wrap_key = _derive_wrap_key(password, salt, _KDF_PARAMS)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    wrapped = AESGCM(wrap_key).encrypt(nonce, dek, _WRAP_AAD)
    return {
        "kdf": {"name": "argon2id", "salt": _b64e(salt), **_KDF_PARAMS},
        "wrapped_key": {"nonce": _b64e(nonce), "ciphertext": _b64e(wrapped)},
    }


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def create_key_file(password: str, path: Optional[Path] = None) -> dict:
    """Generate a fresh DEK, wrap it with ``password``, write the key file.

    Refuses to overwrite an existing file (that would orphan every
    ciphertext produced under the old key).
    """
    _check_password_allowed(password)
    path = path or encryption_key_path()
    if path.exists():
        raise EncryptionError(f"Refusing to overwrite the existing key file {path}")
    dek = secrets.token_bytes(32)
    data = {
        "version": _KEY_FILE_VERSION,
        "key_id": secrets.token_hex(4),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **_wrap_dek(dek, password),
    }
    _write_private_json(path, data)
    logger.info("Created encryption key file %s (key id %s)", path, data["key_id"])
    return data


def read_key_file(path: Optional[Path] = None) -> dict:
    path = path or encryption_key_path()
    if not path.exists():
        raise KeyFileMissingError(f"Encryption key file not found: {path}")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise EncryptionError(f"Unreadable encryption key file {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != _KEY_FILE_VERSION:
        raise EncryptionError(f"Unsupported encryption key file format: {path}")
    return data


def unwrap_key_file(password: str, data: dict) -> tuple[bytes, str]:
    """Return ``(dek, key_id)`` or raise :class:`WrongPasswordError`."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _check_password_allowed(password)
    try:
        kdf = data["kdf"]
        wrapped = data["wrapped_key"]
        wrap_key = _derive_wrap_key(password, _b64d(kdf["salt"]), kdf)
        dek = AESGCM(wrap_key).decrypt(
            _b64d(wrapped["nonce"]), _b64d(wrapped["ciphertext"]), _WRAP_AAD
        )
    except InvalidTag:
        raise WrongPasswordError(
            "The encryption password does not unlock the key file. Check "
            f"{PASSWORD_ENV} / {PASSWORD_FILE_ENV}."
        ) from None
    except (KeyError, TypeError, ValueError) as exc:
        raise EncryptionError(f"Malformed encryption key file: {exc}") from exc
    return dek, str(data["key_id"])


def verify_password(password: str, path: Optional[Path] = None) -> bool:
    """Whether ``password`` unwraps the key file.

    False for a wrong, empty or sentinel password; a missing or malformed
    key file still raises (that is a deployment problem, not a bad guess).
    Backs the first-run setup unlock (auth/first_run.py): the operator who
    started the server is the one who knows this password.
    """
    data = read_key_file(path)
    try:
        unwrap_key_file(password, data)
    except (WrongPasswordError, PasswordNotAvailableError, SentinelPasswordError):
        return False
    return True


def rotate_password(old_password: str, new_password: str, path: Optional[Path] = None) -> None:
    """Re-wrap the DEK under ``new_password`` (ciphertexts are unaffected)."""
    path = path or encryption_key_path()
    data = read_key_file(path)
    dek, _key_id = unwrap_key_file(old_password, data)
    _check_password_allowed(new_password)
    data.update(_wrap_dek(dek, new_password))
    data["rotated_at"] = datetime.now(timezone.utc).isoformat()
    _write_private_json(path, data)
    reset_cache()


# ---------------------------------------------------------------------------
# Process-wide loaded key
# ---------------------------------------------------------------------------

_LOADED: tuple[bytes, str] | None = None


def reset_cache() -> None:
    """Forget the loaded DEK (tests, password rotation)."""
    global _LOADED
    _LOADED = None


def load_data_key(*, create: bool = False, interactive: bool = False) -> tuple[bytes, str]:
    """Resolve the password, unwrap (or create) the key file, cache the DEK.

    ``create`` allows generating a key file when none exists (the ``init``
    CLI step and local mode); a plain server start refuses so a mistyped
    ``QUEST_DATA_DIR`` can never silently start a fresh key beside real
    data. Raises a subclass of :class:`EncryptionError` on failure.
    """
    global _LOADED
    if _LOADED is not None:
        return _LOADED
    path = encryption_key_path()
    exists = path.exists()
    if not exists and not create:
        raise KeyFileMissingError(
            f"Encryption key file not found: {path}. Run "
            "`uv run python -m config.encryption init` (run.py does this "
            "automatically) to create it."
        )
    password = resolve_password(interactive=interactive, confirm=not exists)
    if not password:
        raise PasswordNotAvailableError(
            "No encryption password available. Set QUEST_ENCRYPTION_PASSWORD "
            "or QUEST_ENCRYPTION_PASSWORD_FILE, or start from an interactive "
            "terminal to be prompted."
        )
    if exists:
        data = read_key_file(path)
    else:
        data = create_key_file(password, path)
    _LOADED = unwrap_key_file(password, data)
    return _LOADED


def get_data_key() -> tuple[bytes, str]:
    """Return the cached ``(dek, key_id)``; local mode may auto-create the file.

    Outside local mode the key file must already exist (see
    :func:`load_data_key`).
    """
    if _LOADED is not None:
        return _LOADED
    return load_data_key(create=is_local())


# ---------------------------------------------------------------------------
# Value encryption
# ---------------------------------------------------------------------------

def is_encrypted(value: object) -> bool:
    """Whether ``value`` is a ``qenc1:`` envelope string."""
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def encrypt_str(plaintext: str, aad: str) -> str:
    """Encrypt ``plaintext`` into a ``qenc1:`` envelope bound to ``aad``."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    dek, key_id = get_data_key()
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
    return f"{ENVELOPE_PREFIX}{key_id}:{_b64e(nonce + ct)}"


def decrypt_str(envelope: str, aad: str) -> str:
    """Decrypt a ``qenc1:`` envelope produced by :func:`encrypt_str`."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not is_encrypted(envelope):
        raise EncryptionError("Value is not an encrypted envelope")
    try:
        _prefix, key_id, payload = envelope.split(":", 2)
        raw = _b64d(payload)
    except ValueError as exc:
        raise EncryptionError("Malformed encrypted envelope") from exc
    dek, loaded_key_id = get_data_key()
    if key_id != loaded_key_id:
        raise KeyMismatchError(
            f"Value was encrypted under key id {key_id!r} but the loaded key "
            f"file has id {loaded_key_id!r} (wrong data directory or key file?)"
        )
    if len(raw) <= _NONCE_BYTES:
        raise EncryptionError("Malformed encrypted envelope")
    try:
        plain = AESGCM(dek).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], aad.encode("utf-8"))
    except InvalidTag:
        raise EncryptionError(
            f"Encrypted value failed authentication (context {aad!r}); it was "
            "tampered with or moved from another column"
        ) from None
    return plain.decode("utf-8")


def encrypt_json(value: object, aad: str) -> str:
    """JSON-serialize ``value`` and encrypt it."""
    return encrypt_str(json.dumps(value, separators=(",", ":"), sort_keys=True), aad)


def decrypt_json(envelope: str, aad: str) -> object:
    return json.loads(decrypt_str(envelope, aad))


def hash_api_key(api_key: str) -> str:
    """SHA-256 hex digest used to look up ``users.api_key`` without decrypting."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m config.encryption",
        description="Manage the encryption-at-rest key file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the key file if missing, otherwise verify the password")
    sub.add_parser("check", help="verify the password unlocks the existing key file")
    sub.add_parser("rotate-password", help="re-wrap the key under a new password (prompts)")
    args = parser.parse_args(argv)

    try:
        if args.command in ("init", "check"):
            _dek, key_id = load_data_key(create=args.command == "init", interactive=True)
            print(f"+ Encryption key unlocked (key id {key_id}, file {encryption_key_path()})")
            if args.command == "init":
                from config.service_credentials import encrypt_plaintext_credential_files
                from config.inference_providers import encrypt_plaintext_inference_files
                for name in encrypt_plaintext_credential_files() + encrypt_plaintext_inference_files():
                    print(f"+ Encrypted credential file for {name}")
            return 0
        if args.command == "rotate-password":
            if not sys.stdin.isatty():
                print("! rotate-password needs an interactive terminal", file=sys.stderr)
                return 1
            old = resolve_password(interactive=True) or ""
            data = read_key_file()
            unwrap_key_file(old, data)
            print("Choose the new password.")
            new = prompt_for_password(confirm=True)
            rotate_password(old, new)
            print(f"+ Password rotated for {encryption_key_path()}")
            return 0
    except EncryptionError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in run.py
    sys.exit(_cli(sys.argv[1:]))
