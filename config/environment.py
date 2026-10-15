"""Runtime environment (run mode) resolution.

Quest has three run modes, selected by the ``QUEST_ENV`` environment
variable (normally set by ``run.py``):

- ``local``:   developer machine / agent sandbox. No OAuth login (canned
               accounts via ``/auth/dev-login``), no domain restriction,
               throwaway per-run database, and missing credentials degrade
               gracefully instead of failing startup. The legacy value
               ``dev`` is accepted as an alias.
- ``staging``: automated deployment against real credentials and upstream
               integrations. Behaves like prod (domain restriction on,
               secure cookies) but uses a distinct cookie name so staging
               and prod sessions never collide in a shared browser.
- ``prod``:    production. The default when ``QUEST_ENV`` is unset or
               unrecognized (fail toward the most restrictive mode).

Consumers should use the semantic helpers below instead of comparing mode
strings, so every behavior difference between modes stays auditable in
this one file.

This module intentionally uses **only** the Python standard library so it
can be imported as early as ``config.paths``.
"""

import os

LOCAL = "local"
STAGING = "staging"
PROD = "prod"

# Legacy spellings accepted for backward compatibility.
_ALIASES = {
    "dev": LOCAL,
    "development": LOCAL,
    "production": PROD,
}
_VALID_ENVS = {LOCAL, STAGING, PROD}

# Email of the canned admin account seeded in local mode. Shared between
# the local seeder (scripts/seed_local.py) and the default admin_emails
# list applied when no server_config.json is present in local mode.
LOCAL_CANNED_ADMIN_EMAIL = "admin@quest.local"


def get_quest_env() -> str:
    """Return the canonical run mode: ``local``, ``staging``, or ``prod``.

    Reads the environment variable on every call (cheap) so tests can
    monkeypatch ``QUEST_ENV`` without module reloads.
    """
    raw = os.environ.get("QUEST_ENV", PROD).strip().lower()
    raw = _ALIASES.get(raw, raw)
    if raw not in _VALID_ENVS:
        return PROD
    return raw


def is_local() -> bool:
    return get_quest_env() == LOCAL


def is_staging() -> bool:
    return get_quest_env() == STAGING


def is_prod() -> bool:
    return get_quest_env() == PROD


# ---------------------------------------------------------------------------
# Capability flags
#
# Feature code should key off these, not off get_quest_env() comparisons.
# ---------------------------------------------------------------------------

def allow_dev_login() -> bool:
    """Canned-account / arbitrary-email login without OAuth (local only)."""
    return is_local()


def enforce_domain() -> bool:
    """Restrict access to the allowed email domain (staging and prod)."""
    return not is_local()


def cookie_name() -> str:
    """Session cookie name; per-mode so sessions never collide across modes."""
    env = get_quest_env()
    return "quest_session" if env == PROD else f"quest_session_{env}"


def cookie_secure() -> bool:
    """Whether session cookies get the ``Secure`` attribute.

    True in staging (assumed to sit behind HTTPS) and in any mode whose
    configured public URL (``app_base_url``, see get_app_base_url() in
    config/server_config.py) is ``https://`` -- the URL users browse, so
    the browser always has a secure origin to return the cookie to. A
    deployment without an https ``app_base_url`` keeps plain cookies so a
    plain-HTTP install still signs in.

    The lazy import keeps this module importable as early as config.paths
    (config.server_config is stdlib-only as well).
    """
    if is_staging():
        return True
    from config.server_config import get_app_base_url

    return get_app_base_url().strip().lower().startswith("https://")


def image_suffix() -> str:
    """Suffix for per-mode container image tags (quest-script-runner-*)."""
    return get_quest_env()
