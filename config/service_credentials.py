"""Per-service upstream credential store.

Server-level credentials for upstream API integrations live in individual
per-service JSON files under ``DATA_DIR / "service_credentials"`` (one
``<service>.json`` per service, mode 0600, directory mode 0700) so admins
can manage them from the Settings UI without touching the filesystem.

Historically all server credentials lived in the consolidated
``server_credentials.json`` at the project root (except Twitter/X, which
had its own ``twitter_credentials.json``). ``migrate_legacy_credentials()``
runs at startup and copies known sections from those files into the
per-service store. When a service exists in both places the per-service
file wins: loaders call ``read_service_credentials()`` first and only fall
back to the legacy file when the store has nothing for that service. The
legacy files are never modified or deleted.

Store files are encrypted at rest: the JSON document is wrapped as
``{"encrypted": "qenc1:..."}`` via ``config/encryption.py`` (context label
``service_credentials/<service>``). Reads tolerate a plaintext JSON file --
local-mode pre-baking (``run.py``, stdlib-only and running before the
encryption password is known) writes plaintext, and
``encrypt_plaintext_credential_files()`` (the ``config.encryption init``
step and the app lifespan) converts those in place. Keeping the file a JSON
object means the stdlib-only presence test in ``run.py`` keeps working.

This module keeps its top-level imports to the Python standard library
(mirroring ``config/paths.py``) so it can be imported very early in the
application lifecycle; the encryption module is stdlib at import time too.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from config import encryption
from config.paths import PROJECT_ROOT, SERVICE_CREDENTIALS_DIR

logger = logging.getLogger(__name__)

LEGACY_CREDENTIALS_FILE = PROJECT_ROOT / "server_credentials.json"
# Twitter/X historically kept its credentials in a standalone file rather
# than a server_credentials.json section. (Telegram's legacy location --
# the ``telegram_app_info`` section of server_config.json -- is handled by
# the Telegram plugin's own post_load hook, plugins/telegram/upstream.py.)
LEGACY_TWITTER_CREDENTIALS_FILE = PROJECT_ROOT / "twitter_credentials.json"

# Core services managed by the per-service store. Each key doubles as the
# section name in the legacy server_credentials.json and the file stem of
# the per-service file (<service>.json). Extend this tuple when a new
# CORE upstream integration moves to the store; plugins are added at load
# time via register_plugin_service(). Because migrate_legacy_credentials()
# runs in the app lifespan (after plugin load), a plugin service whose id
# matches a legacy location (the slack and github plugins'
# server_credentials.json sections, the twitter plugin's standalone
# twitter_credentials.json -- see the special case in
# read_legacy_service_credentials) still gets it migrated into its store
# file.
CORE_SERVICES = ("google_oauth", "ramp", "coingecko", "smtp")

# The full store roster: core services plus loaded plugins' credential
# services. Rebound (never mutated) by register_plugin_service() so callers
# that import the module and read the attribute at call time always see the
# current roster.
KNOWN_SERVICES = CORE_SERVICES


def register_plugin_service(service: str) -> None:
    """Add a plugin's credential service to the store roster.

    Called at plugin load time (config/service_specs.py) so
    ``data/service_credentials/<plugin id>.json`` becomes a valid store
    file. Raises ValueError on a collision with an already-known service.
    """
    global KNOWN_SERVICES
    if service in KNOWN_SERVICES:
        raise ValueError(f"Credential service already known: {service!r}")
    KNOWN_SERVICES = KNOWN_SERVICES + (service,)


def service_credentials_path(service: str) -> Path:
    """Return the per-service credential file path for a known service."""
    if service not in KNOWN_SERVICES:
        raise ValueError(f"Unknown credential service: {service!r}")
    return SERVICE_CREDENTIALS_DIR / f"{service}.json"


def read_service_credentials(service: str) -> dict | None:
    """Read one service's credentials from the per-service store.

    Returns None when the file is missing, unreadable, or not a non-empty
    JSON object -- callers treat None as "not in the store" and fall back
    to the legacy consolidated file.
    """
    path = service_credentials_path(service)
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable service credential file: %s", path)
        return None
    if not isinstance(data, dict) or not data:
        logger.warning("Ignoring malformed service credential file: %s", path)
        return None
    data = _unwrap_encrypted_file(data, _file_aad(service), path)
    if not isinstance(data, dict) or not data:
        logger.warning("Ignoring malformed service credential file: %s", path)
        return None
    return data


def _file_aad(service: str) -> str:
    return f"service_credentials/{service}"


def _is_encrypted_file(data: dict) -> bool:
    return set(data.keys()) == {"encrypted"} and encryption.is_encrypted(data["encrypted"])


def _unwrap_encrypted_file(data: dict, aad: str, path: Path) -> object:
    """Decrypt an ``{"encrypted": ...}`` document; pass plaintext through."""
    if not _is_encrypted_file(data):
        return data
    try:
        return encryption.decrypt_json(data["encrypted"], aad)
    except encryption.EncryptionError as exc:
        logger.error("Cannot decrypt credential file %s: %s", path, exc)
        return None


def _wrap_encrypted_file(credentials: dict, aad: str) -> dict:
    return {"encrypted": encryption.encrypt_json(credentials, aad)}


def write_service_credentials(service: str, credentials: dict) -> None:
    """Atomically write one service's credentials, encrypted, with restrictive
    permissions.

    The file is written to a same-directory temp file (created 0600 by
    ``mkstemp``) and moved into place with ``os.replace`` so a crash mid-write
    can never leave a truncated credential file behind.
    """
    path = service_credentials_path(service)
    SERVICE_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_path = tempfile.mkstemp(
        dir=SERVICE_CREDENTIALS_DIR, prefix=f".{service}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(_wrap_encrypted_file(credentials, _file_aad(service)), f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # mkstemp already creates the temp file 0600; chmod defensively in case
    # a pre-existing umask/ACL oddity loosened it.
    os.chmod(path, 0o600)


def encrypt_plaintext_credential_files() -> list[str]:
    """Encrypt any store file still holding plaintext JSON; returns the
    services converted. Idempotent -- encrypted files are left alone."""
    converted: list[str] = []
    for service in KNOWN_SERVICES:
        path = service_credentials_path(service)
        raw = _read_json_object(path)
        if raw is None or _is_encrypted_file(raw):
            continue
        write_service_credentials(service, raw)
        converted.append(service)
        logger.info("Encrypted plaintext credential file %s", path)
    return converted


def read_legacy_service_credentials(service: str) -> dict | None:
    """Read one service's credentials from its legacy location.

    Most services are sections of the consolidated server_credentials.json;
    Twitter/X is the entire standalone twitter_credentials.json. Returns None
    when the legacy file is missing, unreadable, or has no non-empty object
    for the service.
    """
    if service not in KNOWN_SERVICES:
        raise ValueError(f"Unknown credential service: {service!r}")
    if service == "twitter":
        return _read_json_object(LEGACY_TWITTER_CREDENTIALS_FILE)
    data = _read_json_object(LEGACY_CREDENTIALS_FILE)
    section = data.get(service) if data else None
    if not isinstance(section, dict) or not section:
        return None
    return section


def _read_json_object(path: Path) -> dict | None:
    """Read a JSON file, returning None unless it holds a non-empty object."""
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable legacy credentials file: %s", path)
        return None
    if not isinstance(data, dict) or not data:
        return None
    return data


def migrate_legacy_credentials() -> list[str]:
    """Copy known services from their legacy credential files into the store.

    Runs at startup. A service that already has a per-service file is left
    untouched (the new location always wins), so the migration is idempotent
    and never clobbers credentials saved through the admin UI. Returns the
    names of the services migrated on this run.
    """
    migrated: list[str] = []
    for service in KNOWN_SERVICES:
        if read_service_credentials(service) is not None:
            continue
        legacy = read_legacy_service_credentials(service)
        if legacy is None:
            continue
        write_service_credentials(service, legacy)
        migrated.append(service)
        logger.info(
            "Migrated %s credentials from the legacy credentials file to %s",
            service,
            service_credentials_path(service),
        )
    return migrated
