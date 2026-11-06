"""First-run setup: the web flow that configures a fresh deployment.

A staging/prod deployment starts with nothing configured: no admin and no
way to sign in. The server starts anyway; ``GET /app/api/config`` reports
``setup_required`` and the frontend renders the setup screen
(frontend/src/components/SetupScreen.tsx) in place of the sign-in screen.
The only command-line input a deployment needs is the credential-encryption
password (run.py), and that same password unlocks this flow: the operator
who started the server is the only one who knows it, so nobody else who can
reach the unconfigured instance can claim the admin account.

Routes (unauthenticated -- there are no accounts yet -- and every one of
them 404s ``setup_not_required`` once the deployment is configured):

- ``POST /app/api/setup/unlock`` ``{password}``: verifies the encryption
  password against the key file and returns a short-lived signed setup
  token plus the form defaults (existing values on a partially configured
  deployment, the request host as the public-URL suggestion). Rate-limited
  per client and globally -- the password check is an Argon2id derivation.
- ``POST /app/api/setup/complete`` ``{token, ...answers}``: validates every
  answer first, then writes server_config.json (admin emails, sign-in
  method, admission policy, public URL, Vertex project id), the
  ``google_oauth`` credential store file, the Vertex service-account key
  copy, and -- under password sign-in -- the first admin's account and
  password hash straight into the database, signing that admin in with the
  response.

"Configured" means both hard prerequisites a usable instance cannot run
without are present (``missing_required_config()``): ``admin_emails`` and a
way to sign in (an admin with a password under password sign-in, Google
OAuth client credentials under Google sign-in). Everything else degrades to
a feature-level message and is configured later from the admin Settings
sections; LLM credentials in particular also have the composer's model
setup card.
"""

import asyncio
import json
import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from auth.config import (
    LOGIN_METHOD_GOOGLE,
    LOGIN_METHOD_PASSWORD,
    LOGIN_METHODS,
    get_secret_key,
    google_login_configured,
    load_client_config,
)
from auth.rate_limit import SlidingWindow
from auth.session import set_session_cookie
from config import environment
from config.password_hashing import hash_password, password_fingerprint, password_problem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app/api/setup")

# Setup tokens: minted by /unlock, required by /complete.
_TOKEN_SALT = "quest-first-run-setup"
TOKEN_MAX_AGE_SECONDS = 60 * 60

# Written into the data directory; run.py exports it as
# GOOGLE_APPLICATION_CREDENTIALS in staging/prod on every later start, and
# the completing request exports it into this process right away.
VERTEX_KEY_FILENAME = "vertex-service-account.json"

# Standard Google OAuth endpoint values, mirroring what the admin Settings
# form saves so the stored config matches a Google Cloud Console
# client-secret JSON.
GOOGLE_OAUTH_WEB_DEFAULTS = {
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
}

# Both redirect paths must be registered on the Google OAuth client: login
# and the separate Google-services (Gmail/Calendar/Drive) consent flow.
OAUTH_REDIRECT_PATHS = ("/auth/callback", "/auth/google-services/callback")

# Public mailbox providers: refused as the allowed login domain (admitting
# "gmail.com" would admit every Gmail user) and never suggested as the
# default from the admin's address.
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "fastmail.com",
})

# What missing_required_config() can report, with the operator-facing text.
MISSING_ADMIN_EMAILS = "admin_emails"
MISSING_ADMIN_PASSWORD = "admin_password"
MISSING_SIGN_IN_METHOD = "sign_in_method"
MISSING_GOOGLE_OAUTH = "google_oauth"
MISSING_MESSAGES = {
    MISSING_ADMIN_EMAILS: "admin_emails in server_config.json (nobody can reach the admin UI)",
    MISSING_ADMIN_PASSWORD: "a password for an admin account (nobody can sign in)",
    MISSING_SIGN_IN_METHOD: "a sign-in method: email + password, or Google OAuth client "
                            "credentials (sign-in is impossible without one)",
    MISSING_GOOGLE_OAUTH: "Google OAuth client credentials (sign-in is impossible without them)",
}

# Shape checks, written without regular expressions (a pattern such as
# ``[^@]+@[^@]+\.[^@]+`` backtracks polynomially on hostile input) and
# capped at the RFC 5321 lengths.
MAX_EMAIL_LENGTH = 254
MAX_DOMAIN_LENGTH = 253
_DOMAIN_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def _looks_like_domain(value: str) -> bool:
    """``label(.label)+`` of lowercase letters, digits and hyphens, with an
    alphabetic top-level label of two or more characters."""
    if not value or len(value) > MAX_DOMAIN_LENGTH:
        return False
    labels = value.split(".")
    if len(labels) < 2 or not all(labels):
        return False
    if any(set(label) - _DOMAIN_LABEL_CHARS for label in labels):
        return False
    tld = labels[-1]
    return len(tld) >= 2 and tld.isalpha()


def _looks_like_email(value: str) -> bool:
    """``local@host.tld`` with no whitespace and exactly one ``@``."""
    if not value or len(value) > MAX_EMAIL_LENGTH or any(c.isspace() for c in value):
        return False
    local, sep, host = value.partition("@")
    if not sep or not local or "@" in host:
        return False
    head, dot, tail = host.rpartition(".")
    return bool(dot and head and tail)


# Unlock attempts: five per client and thirty in total per 15 minutes. The
# password check costs an Argon2id derivation, so the global cap doubles
# as a CPU guard for the window the flow is exposed.
_unlock_by_ip = SlidingWindow(limit=5, window=15 * 60)
_unlock_global = SlidingWindow(limit=30, window=15 * 60)
_GLOBAL_KEY = "*"

# One completion at a time; a second request re-checks setup_required()
# under the lock and gets the 404.
_complete_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Is setup needed?
# ---------------------------------------------------------------------------

def configured_login_method(config: dict) -> Optional[str]:
    """The explicit ``login_method`` in server_config.json, or None."""
    value = str(config.get("login_method") or "").strip().lower()
    return value if value in LOGIN_METHODS else None


def _admin_emails(config: dict) -> list[str]:
    raw = config.get("admin_emails") or []
    return [str(e).strip().lower() for e in raw if str(e).strip()]


async def _any_admin_has_password(admin_emails: list[str]) -> bool:
    from db.password_store import get_password_hash

    for email in admin_emails:
        if await get_password_hash(email):
            return True
    return False


async def missing_required_config() -> list[str]:
    """Codes (``MISSING_*``) for the hard prerequisites this deployment lacks.

    Only the items that make the instance unusable for everyone count:
    admin_emails (nobody can administer) and a way to sign in -- an admin
    password under password sign-in, Google OAuth client credentials under
    Google sign-in (also what an absent ``login_method`` means).
    """
    from config.server_config import load_server_config

    config = load_server_config()
    admin_emails = _admin_emails(config)
    missing = []
    if not admin_emails:
        missing.append(MISSING_ADMIN_EMAILS)
    method = configured_login_method(config)
    if method == LOGIN_METHOD_PASSWORD:
        if admin_emails and not await _any_admin_has_password(admin_emails):
            missing.append(MISSING_ADMIN_PASSWORD)
    elif not google_login_configured():
        missing.append(MISSING_SIGN_IN_METHOD if method is None else MISSING_GOOGLE_OAUTH)
    return missing


async def setup_required() -> bool:
    """Whether the first-run setup flow is open on this deployment.

    Never in local mode (canned admin, dev login). Fails closed: an error
    while checking is logged and reported as "not required", so a broken
    check can never expose the unauthenticated flow on a configured
    instance.
    """
    if environment.is_local():
        return False
    try:
        return bool(await missing_required_config())
    except Exception:
        logger.exception("[setup] Could not determine whether first-run setup is required")
        return False


async def log_setup_notice() -> None:
    """Startup log line telling the operator the setup flow is waiting."""
    if environment.is_local():
        return
    missing = await missing_required_config()
    if not missing:
        return
    logger.warning(
        "[setup] This deployment is not configured yet (missing: %s). Open the "
        "app in a browser and complete first-run setup; it unlocks with the "
        "encryption password entered at startup.",
        "; ".join(MISSING_MESSAGES[code] for code in missing),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_secret_key(), salt=_TOKEN_SALT)


def mint_setup_token() -> str:
    return _serializer().dumps({"setup": True})


def setup_token_valid(token: str) -> bool:
    try:
        payload = _serializer().loads(token or "", max_age=TOKEN_MAX_AGE_SECONDS)
    except BadSignature:
        return False
    return isinstance(payload, dict) and payload.get("setup") is True


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _error(status: int, error: str, message: str, field: Optional[str] = None) -> HTTPException:
    detail = {"error": error, "message": message}
    if field:
        detail["field"] = field
    return HTTPException(status_code=status, detail=detail)


async def _require_setup_open() -> None:
    if not await setup_required():
        raise _error(404, "setup_not_required", "This deployment is already configured.")


def _vertex_key_path():
    from config.paths import DATA_DIR

    return DATA_DIR / VERTEX_KEY_FILENAME


def _write_private_json(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def _existing_google_web() -> dict:
    try:
        return dict(load_client_config() or {})
    except HTTPException:
        return {}


def _normalize_emails(raw: list, field: str, label: str) -> list[str]:
    emails: list[str] = []
    for value in raw or []:
        email = str(value or "").strip().lower()
        if not email:
            continue
        if not _looks_like_email(email):
            raise _error(400, "invalid_params", f"{label}: {email!r} is not an email address.", field)
        if email not in emails:
            emails.append(email)
    return emails


def normalize_base_url(raw: str) -> str:
    """``https://`` is assumed for a bare hostname; trailing slashes dropped.

    Raises ValueError for anything that is not an http(s) origin.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    value = value.rstrip("/")
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.query or parts.fragment):
        raise ValueError("Enter the URL users browse to, e.g. https://quest.example.com.")
    return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class UnlockRequest(BaseModel):
    password: str = ""


def setup_defaults(request: Request) -> dict:
    """Form defaults for the setup screen: what is already configured plus
    the suggestions the screen cannot compute itself."""
    from auth.config import oauth_base_url
    from chat.llm.config import get_configured_models
    from config.server_config import load_server_config

    config = load_server_config()
    method = configured_login_method(config)
    web = _existing_google_web()
    base_url = config.get("app_base_url", "").rstrip("/")
    legacy_host = config.get("oauth_hostname", "")
    return {
        "login_method": method or (
            LOGIN_METHOD_GOOGLE if google_login_configured() else LOGIN_METHOD_PASSWORD
        ),
        "login_method_configured": method is not None,
        "admin_emails": _admin_emails(config),
        "allowed_login_domain": str(config.get("allowed_login_domain") or "").strip().lstrip("@"),
        "allowed_login_emails": [
            str(e).strip().lower() for e in (config.get("allowed_login_emails") or []) if str(e).strip()
        ],
        # The configured public URL, a legacy hostname-only override, or the
        # address this request came in on.
        "app_base_url": base_url or (f"https://{legacy_host}" if legacy_host else oauth_base_url(request)),
        "app_base_url_configured": bool(base_url),
        "google_oauth_configured": google_login_configured(),
        "google_client_id": str(web.get("client_id") or ""),
        "google_project_id": str(web.get("project_id") or ""),
        "redirect_paths": list(OAUTH_REDIRECT_PATHS),
        "llm_configured": bool(get_configured_models()),
        "vertex_project_id": str(config.get("anthropic", {}).get("vertex_project_id") or ""),
        "vertex_key_present": _vertex_key_path().exists(),
        "public_email_domains": sorted(PUBLIC_EMAIL_DOMAINS),
    }


@router.post("/unlock")
async def unlock_setup(body: UnlockRequest, request: Request):
    """Prove operator identity with the encryption password; get a setup token."""
    from config import encryption

    await _require_setup_open()
    ip = _client_ip(request)
    if _unlock_by_ip.blocked(ip) or _unlock_global.blocked(_GLOBAL_KEY):
        raise _error(429, "rate_limited", "Too many attempts. Wait a few minutes and try again.")
    _unlock_by_ip.hit(ip)
    _unlock_global.hit(_GLOBAL_KEY)

    password = body.password.strip()
    ok = bool(password) and await asyncio.to_thread(encryption.verify_password, password)
    if not ok:
        logger.info("[setup] Failed setup unlock from %s", ip)
        raise _error(401, "wrong_password", "That is not the encryption password this server was started with.")
    _unlock_by_ip.reset(ip)
    logger.info("[setup] Setup unlocked from %s", ip)
    return {"token": mint_setup_token(), "defaults": setup_defaults(request)}


class CompleteRequest(BaseModel):
    token: str = ""
    login_method: str = LOGIN_METHOD_PASSWORD
    admin_emails: list[str] = []
    allowed_login_domain: str = ""
    allowed_login_emails: list[str] = []
    app_base_url: str = ""
    # Password sign-in: the password of admin_emails[0], who is signed in
    # by the response.
    admin_password: str = ""
    # Google sign-in: the OAuth client. An empty secret keeps a stored one.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_project_id: str = ""
    # Optional LLM credentials: a GCP service-account key object and the
    # Vertex project id (defaults to the key's project).
    vertex_service_account: Optional[dict] = None
    vertex_project_id: str = ""


class SetupPlan:
    """Validated answers, ready to write."""

    def __init__(self):
        self.config_updates: dict = {}
        self.google_oauth: Optional[dict] = None
        self.vertex_key: Optional[dict] = None
        self.admin_email: str = ""
        self.admin_password_hash: str = ""


async def validate_answers(body: CompleteRequest) -> SetupPlan:
    """Check every answer and build the write plan; 400 ``invalid_params``
    with the offending ``field`` on the first problem."""
    from config.server_config import raw_server_config

    plan = SetupPlan()
    stored = raw_server_config()

    method = (body.login_method or "").strip().lower()
    if method not in LOGIN_METHODS:
        raise _error(400, "invalid_params", "Choose email + password or Google sign-in.", "login_method")
    plan.config_updates["login_method"] = method

    admin_emails = _normalize_emails(body.admin_emails, "admin_emails", "Admin email")
    if not admin_emails:
        raise _error(400, "invalid_params", "Enter at least one admin email address.", "admin_emails")
    plan.config_updates["admin_emails"] = admin_emails
    plan.admin_email = admin_emails[0]

    domain = (body.allowed_login_domain or "").strip().lower().lstrip("@")
    if domain:
        if not _looks_like_domain(domain):
            raise _error(400, "invalid_params", f"{domain!r} is not a domain name.", "allowed_login_domain")
        if domain in PUBLIC_EMAIL_DOMAINS:
            raise _error(
                400, "invalid_params",
                f"{domain} is a public mailbox provider: allowing it would admit everyone with "
                "an address there. Leave the domain empty and list the individual addresses instead.",
                "allowed_login_domain",
            )
        plan.config_updates["allowed_login_domain"] = domain
    elif stored.get("allowed_login_domain"):
        plan.config_updates["allowed_login_domain"] = ""

    emails = _normalize_emails(body.allowed_login_emails, "allowed_login_emails", "Allowed email")
    # Admins must be able to sign in themselves: allow-list any admin
    # outside the allowed domain.
    for admin in admin_emails:
        if domain and admin.endswith("@" + domain):
            continue
        if admin not in emails:
            emails.append(admin)
    plan.config_updates["allowed_login_emails"] = emails

    try:
        base_url = normalize_base_url(body.app_base_url)
    except ValueError as exc:
        raise _error(400, "invalid_params", str(exc), "app_base_url")
    if base_url:
        plan.config_updates["app_base_url"] = base_url

    if method == LOGIN_METHOD_PASSWORD:
        from db.password_store import get_password_hash

        password = body.admin_password or ""
        if password:
            problem = password_problem(password)
            if problem:
                raise _error(400, "invalid_params", problem, "admin_password")
            plan.admin_password_hash = await asyncio.to_thread(hash_password, password)
        elif not await get_password_hash(plan.admin_email):
            raise _error(400, "invalid_params", "Choose a password for the first admin.", "admin_password")
    else:
        existing = _existing_google_web()
        client_id = (body.google_client_id or "").strip()
        client_secret = (body.google_client_secret or "").strip()
        if not client_id:
            if not (existing.get("client_id") and existing.get("client_secret")):
                raise _error(400, "invalid_params", "Enter the Google OAuth client id.", "google_client_id")
        else:
            if not client_secret:
                if existing.get("client_id") == client_id and existing.get("client_secret"):
                    client_secret = existing["client_secret"]
                else:
                    raise _error(400, "invalid_params", "Enter the Google OAuth client secret.", "google_client_secret")
            web = {"client_id": client_id, "client_secret": client_secret, **GOOGLE_OAUTH_WEB_DEFAULTS}
            project_id = (body.google_project_id or "").strip()
            if project_id:
                web["project_id"] = project_id
            if base_url:
                web["redirect_uris"] = [f"{base_url}{path}" for path in OAUTH_REDIRECT_PATHS]
            plan.google_oauth = {"web": web}

    key = body.vertex_service_account
    project_id = (body.vertex_project_id or "").strip()
    if key is not None:
        if not isinstance(key, dict) or key.get("type") != "service_account":
            raise _error(
                400, "invalid_params",
                'That is not a service-account key (expected a JSON object with "type": "service_account").',
                "vertex_service_account",
            )
        plan.vertex_key = key
        project_id = project_id or str(key.get("project_id") or "").strip()
        if not project_id:
            raise _error(400, "invalid_params", "Enter the GCP project id for Vertex AI.", "vertex_project_id")
    if project_id:
        anthropic = dict(stored.get("anthropic") or {}) if isinstance(stored.get("anthropic"), dict) else {}
        anthropic["vertex_project_id"] = project_id
        plan.config_updates["anthropic"] = anthropic
    return plan


async def apply_setup(plan: SetupPlan) -> None:
    """Write the plan: config file, credential store, key copy, account."""
    from config.server_config import update_server_config
    from config.service_credentials import write_service_credentials

    if plan.vertex_key is not None:
        key_path = _vertex_key_path()
        _write_private_json(key_path, plan.vertex_key)
        # Point ADC at it in this process too (run.py does the same on every
        # later start); an externally-set env var wins, as everywhere.
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(key_path))
    if plan.google_oauth is not None:
        write_service_credentials("google_oauth", plan.google_oauth)
    update_server_config(plan.config_updates)
    if plan.admin_password_hash:
        from auth.password_login import ensure_account
        from db.password_store import set_password_hash

        await ensure_account(plan.admin_email)
        await set_password_hash(plan.admin_email, plan.admin_password_hash)

    if "anthropic" in plan.config_updates:
        from chat.llm.config import get_configured_models, reset_provider_client_caches
        from chat.llm.health import schedule_model_rechecks

        reset_provider_client_caches()
        schedule_model_rechecks(get_configured_models())


@router.post("/complete")
async def complete_setup(body: CompleteRequest, request: Request):
    """Validate and write the configuration; sign the first admin in under
    password sign-in."""
    from db.password_store import get_password_hash
    from db.user_store import get_user_by_email

    await _require_setup_open()
    if not setup_token_valid(body.token):
        raise _error(401, "setup_locked", "Unlock setup with the encryption password first.")

    async with _complete_lock:
        await _require_setup_open()
        plan = await validate_answers(body)
        await apply_setup(plan)

    method = plan.config_updates["login_method"]
    logger.warning(
        "[setup] First-run setup completed from %s: admins %s, sign-in %s",
        _client_ip(request), plan.config_updates["admin_emails"], method,
    )
    result = {"login_method": method, "signed_in": False}
    if method != LOGIN_METHOD_PASSWORD:
        return result
    # Load after set_password_hash(): it advanced the session epoch.
    user = await get_user_by_email(plan.admin_email)
    stored = await get_password_hash(plan.admin_email)
    if not user or not stored:
        return result
    result["signed_in"] = True
    return set_session_cookie(JSONResponse(result), user, password_fingerprint(stored))

