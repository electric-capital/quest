"""Auth configuration: constants, credential loaders, and path definitions."""

import json
import os
import secrets

from fastapi import HTTPException

from config import environment
from config.paths import SECRET_KEY_FILE, PROJECT_ROOT

# Credential file paths
SERVER_CREDENTIALS_FILE = PROJECT_ROOT / "server_credentials.json"

# Cached server credentials (loaded once, reused)
_server_credentials_cache: dict | None = None

# Domain restriction. Placeholder default: real deployments set the
# ``allowed_login_domain`` key in server_config.json (the prod bootstrap
# wizard prompts for it), which takes precedence in allowed_login_domain().
ALLOWED_DOMAIN = "example.com"


def allowed_login_domain() -> str:
    """Email domain enforced for Google login and API access.

    Resolution order (the check itself always runs -- some domain is
    always enforced):

    1. Local mode only: QUEST_ALLOWED_LOGIN_DOMAIN (pre-baked by run.py
       from the shared dev-config.json ``allowed_login_domain`` key) so
       developers can complete a real Google sign-in with a test account.
       The env var is deliberately ignored outside local mode -- a stray
       environment variable must never widen staging/prod access.
    2. ``allowed_login_domain`` in server_config.json (all modes; written
       by the prod bootstrap wizard) so a deployment for another
       organization can admit its own domain. Config-file changes require
       filesystem access to the server, unlike environment variables.
    3. The built-in ALLOWED_DOMAIN default.
    """
    if environment.is_local():
        override = os.environ.get("QUEST_ALLOWED_LOGIN_DOMAIN", "").strip().lstrip("@")
        if override:
            return override

    from config.server_config import load_server_config

    configured = str(
        load_server_config().get("allowed_login_domain") or ""
    ).strip().lstrip("@")
    if configured:
        return configured
    return ALLOWED_DOMAIN


def allowed_login_emails() -> list:
    """Individual email addresses allowed to sign in alongside the domain.

    Read fresh from the ``allowed_login_emails`` list in server_config.json
    each call (like allowed_login_domain, so config edits apply without a
    restart), lowercased for comparison. Lets a deployment admit specific
    accounts outside the allowed domain -- e.g. a personal deployment
    whitelisting individual @gmail.com accounts without opening the whole
    gmail.com domain. Empty when unconfigured.
    """
    from config.server_config import load_server_config

    raw = load_server_config().get("allowed_login_emails")
    if not isinstance(raw, list):
        return []
    return [str(e).strip().lower() for e in raw if str(e).strip()]


def is_login_allowed(email: str) -> bool:
    """Whether this email may sign in: allowed domain OR whitelisted email.

    Some restriction always applies: with no ``allowed_login_emails``
    configured this is exactly the historical domain check
    (allowed_login_domain() falls back to the hardcoded ALLOWED_DOMAIN).
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    if email in allowed_login_emails():
        return True
    return email.endswith(f"@{allowed_login_domain().lower()}")


def login_restriction_description() -> str:
    """Human-readable description of who may sign in, for UI messages.

    Deliberately never enumerates the email whitelist: the description is
    shown on unauthenticated surfaces (sign-in page, /app/api/config).
    """
    if allowed_login_emails():
        return "approved accounts"
    return f"@{allowed_login_domain()} accounts"

LOGIN_METHOD_GOOGLE = "google"
LOGIN_METHOD_PASSWORD = "password"
LOGIN_METHODS = (LOGIN_METHOD_GOOGLE, LOGIN_METHOD_PASSWORD)


def login_method() -> str:
    """The deployment's active sign-in method: "google" or "password".

    Read fresh from the ``login_method`` key in server_config.json on every
    call so the admin switch (and hand edits) apply without a restart.
    Unset or unknown values mean "google": deployments that predate
    password sign-in keep their behavior. Exactly one method is active --
    the Google login routes refuse to sign anyone in while this is
    "password", and every password route (plus every password-issued
    session cookie) is refused while it is "google". Accounts are keyed by
    email in both, so switching methods keeps every account.
    """
    from config.server_config import load_server_config

    value = str(load_server_config().get("login_method") or "").strip().lower()
    return value if value in LOGIN_METHODS else LOGIN_METHOD_GOOGLE


def is_password_login() -> bool:
    return login_method() == LOGIN_METHOD_PASSWORD


# Login scopes - minimal, stable, only for identifying the user
LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Google service scopes - for accessing Google APIs, can evolve over time
GOOGLE_SERVICE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # Draft creation
    "https://www.googleapis.com/auth/gmail.modify",  # Label management, message archiving
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",  # Create/edit app-created files (e.g., save markdown as Google Docs)
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/spreadsheets",  # Cell writes via the edit_google_spreadsheet action request
    "https://www.googleapis.com/auth/presentations.readonly",  # Read access to Google Slides
    "https://www.googleapis.com/auth/tasks.readonly",
    # Platform-wide Google Cloud scope (Resource Manager, Compute, GKE, Logging).
    # This is the FULL cloud-platform scope, not a read-only variant: Compute
    # Engine rejects cloud-platform.read-only and GKE/Container has no read-only
    # scope at all (both require this full scope). Read-only behavior is enforced
    # at the APPLICATION layer by the authed_get/authed_post allow-lists (which
    # only permit whitelisted GET/POST read verbs), NOT by the OAuth scope.
    "https://www.googleapis.com/auth/cloud-platform",
]

# Combined for backward compatibility during migration
SCOPES = LOGIN_SCOPES + GOOGLE_SERVICE_SCOPES

# Slack OAuth scopes moved to the slack plugin (plugins/slack/upstream.py).
# The slack credential loaders below stay in core because the (core) Slack
# Socket Mode worker needs the bot / Socket Mode tokens too.
# GitHub OAuth scopes/config moved to the github plugin
# (plugins/github/upstream.py). Twitter/X scopes/config moved to the
# twitter plugin (plugins/twitter/upstream.py).

# Ramp OAuth 2.0 scopes: every read scope grantable to a Ramp developer app
# (the admin-created Ramp app must have all of these enabled -- requesting a
# scope the app lacks makes Ramp reject the authorize request, which is also
# why applications:read / incorporation:read / spend_requests:read from
# Ramp's OpenAPI spec are absent: apps cannot enable them). cards:read_vault
# (full card numbers/CVVs) is deliberately excluded, and offline_access is
# required for refresh tokens -- Ramp access tokens expire and refresh tokens
# rotate on every use.
RAMP_SCOPES = [
    "accounting:read",
    "ai_spend:read",
    "attendee_types:read",
    "audit_logs:read",
    "bank_accounts:read",
    "bank_feeds:read",
    "bills:read",
    "budgets:read",
    "business:read",
    "cards:read",
    "cashbacks:read",
    "custom_forms:read",
    "custom_records:read",
    "departments:read",
    "entities:read",
    "external_attendees:read",
    "funds:read",
    "item_receipts:read",
    "limits:read",
    "locations:read",
    "memos:read",
    "merchants:read",
    "purchase_orders:read",
    "receipt_integrations:read",
    "receipts:read",
    "reimbursements:read",
    "repayments:read",
    "spend_programs:read",
    "statements:read",
    "tasks:read",
    "transactions:read",
    "transfers:read",
    "treasury:read",
    "trips:read",
    "unified_requests:read",
    "users:read",
    "vendors:read",
    "offline_access",
]

# Session cookie configuration (per-mode name and Secure flag come from
# the central environment module so modes never share sessions). The
# Secure flag is resolved once at startup: a changed app_base_url applies
# after a restart.
COOKIE_NAME = environment.cookie_name()
COOKIE_SECURE = environment.cookie_secure()
# Bump this to invalidate all existing sessions. Version 3 is the timed
# cookie carrying the session epoch (auth/session.py).
COOKIE_VERSION = 3

def generate_api_key() -> str:
    """Generate a random API key (32+ characters)."""
    return secrets.token_urlsafe(32)


def oauth_base_url(request) -> str:
    """Base URL for building OAuth redirect/callback URLs.

    When the deployment's full public URL is configured (the top-level
    ``app_base_url`` key in server_config.json, env override
    ``QUEST_APP_BASE_URL``; see get_app_base_url() in
    config/server_config.py) it is used verbatim -- users browse the app
    through that URL, so callbacks must share its origin.

    Otherwise: some OAuth providers (notably Google) reject redirect URIs
    that use a raw IP address. The optional top-level ``oauth_hostname``
    key in server_config.json (env override: ``QUEST_OAUTH_HOSTNAME``)
    replaces the hostname of the incoming request when building callback
    URLs; scheme and port are preserved. Developers point that hostname at
    the server's IP in /etc/hosts and browse the app through it so session
    cookies and the OAuth callback share an origin.

    With no override configured this is just the request's own base URL.
    """
    from urllib.parse import urlsplit, urlunsplit

    from config.server_config import get_app_base_url, load_server_config

    app_base_url = get_app_base_url()
    if app_base_url:
        return app_base_url

    base = str(request.base_url).rstrip("/")
    hostname = load_server_config().get("oauth_hostname", "")
    if not hostname:
        return base
    parts = urlsplit(base)
    netloc = hostname if parts.port is None else f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _load_server_credentials() -> dict:
    """Load and cache the consolidated server credentials from server_credentials.json.

    Returns:
        The full credentials dictionary.

    Raises:
        HTTPException: If server_credentials.json is missing.
    """
    global _server_credentials_cache
    if _server_credentials_cache is not None:
        return _server_credentials_cache
    if not SERVER_CREDENTIALS_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail="Server credentials not configured. Place server_credentials.json in project root (see server_credentials.example.json)."
        )
    with open(SERVER_CREDENTIALS_FILE, "r") as f:
        _server_credentials_cache = json.load(f)
    return _server_credentials_cache


def load_google_oauth_config() -> dict:
    """Load the full Google OAuth config dict (including the 'web' wrapper).

    Prefers the per-service credential store (data/service_credentials/
    google_oauth.json, managed via the admin Settings UI) and falls back to
    the legacy consolidated server_credentials.json. Store reads are fresh
    on every call so admin updates take effect without a restart.

    Returns:
        Dict with structure {"web": {...}} suitable for Flow.from_client_config().
    """
    from config.service_credentials import read_service_credentials

    stored = read_service_credentials("google_oauth")
    if stored:
        return stored
    try:
        creds = _load_server_credentials()
    except HTTPException:
        creds = {}
    google_oauth = creds.get("google_oauth")
    if not google_oauth:
        raise HTTPException(
            status_code=500,
            detail=(
                "Google OAuth credentials not configured. Set them in "
                "Settings > Service Credentials (admin) or in "
                "server_credentials.json."
            )
        )
    return google_oauth


def load_client_config() -> dict:
    """Load OAuth client config (the inner web/installed dict) from server_credentials.json."""
    google_oauth = load_google_oauth_config()
    return google_oauth.get("web", google_oauth.get("installed", {}))


def load_slack_client_config() -> dict:
    """Load Slack OAuth client config (client_id/client_secret plus tokens).

    Prefers the per-service credential store and falls back to the "slack"
    section of the legacy server_credentials.json. Store reads are fresh on
    every call so admin updates take effect without a restart.

    The "slack" store service is registered by the in-tree Slack plugin;
    when the plugin failed to load, the store lookup raises ValueError
    ("unknown service") and we treat that the same as "not in the store".
    """
    from config.service_credentials import read_service_credentials

    try:
        stored = read_service_credentials("slack")
    except ValueError:
        stored = None
    if stored:
        return stored
    try:
        creds = _load_server_credentials()
    except HTTPException:
        creds = {}
    slack = creds.get("slack")
    if not slack:
        raise HTTPException(
            status_code=500,
            detail=(
                "Slack credentials not configured. Set them in "
                "Settings > Service Credentials (admin) or in "
                "server_credentials.json."
            )
        )
    return slack


def load_slack_bot_token() -> str:
    """Load the shared Slack bot token.

    The bot token (xoxb-...) is installed once at the org level by an admin
    and stored alongside client_id/client_secret in the Slack service
    credentials. It is used for write operations (e.g., chat.postMessage)
    and org-wide queries (e.g., auth.teams.list) that are not user-specific.

    Raises:
        HTTPException: If Slack credentials are missing or have no bot_token.
    """
    config = load_slack_client_config()
    bot_token = config.get("bot_token")
    if not bot_token:
        raise HTTPException(
            status_code=500,
            detail=(
                "Slack bot token not configured. Set it in "
                "Settings > Service Credentials (admin) or add a 'bot_token' "
                "field to the 'slack' section in server_credentials.json."
            )
        )
    return bot_token


def load_slack_socket_mode_token() -> str | None:
    """Load the Slack App-Level Token used for Socket Mode connections.

    The App-Level Token (xapp-...) is distinct from both the bot token
    (xoxb-...) and per-user OAuth tokens. It authenticates the Socket Mode
    WebSocket connection and requires the 'connections:write' scope.

    Socket Mode is optional plumbing, so this loader returns None when the
    token or the entire Slack config is missing -- callers are expected to
    disable Socket Mode rather than crash.
    """
    try:
        slack = load_slack_client_config()
    except HTTPException:
        # No Slack credentials at all; Socket Mode can't run but the rest
        # of the server should still boot.
        return None
    token = slack.get("socket_mode_token")
    if not token:
        return None
    return token


def load_coingecko_api_key() -> str:
    """Load the CoinGecko Pro API key.

    Prefers the per-service credential store (data/service_credentials/
    coingecko.json, managed via the admin Settings UI) and falls back to the
    "coingecko" section of the legacy server_credentials.json. Store reads
    are fresh on every call so admin updates take effect without a restart.

    The API key is used for authenticated requests to the CoinGecko Pro API
    (https://pro-api.coingecko.com/api/v3/).

    Returns:
        The CoinGecko Pro API key string.

    Raises:
        HTTPException: If no CoinGecko API key is configured anywhere.
    """
    from config.service_credentials import read_service_credentials

    stored = read_service_credentials("coingecko")
    if stored and stored.get("api_key"):
        return stored["api_key"]
    try:
        creds = _load_server_credentials()
    except HTTPException:
        creds = {}
    api_key = (creds.get("coingecko") or {}).get("api_key")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "CoinGecko API key not configured. Set it in "
                "Settings > Service Credentials (admin) or add a 'coingecko' "
                "section with an 'api_key' field to server_credentials.json."
            )
        )
    return api_key


def load_ramp_client_config() -> dict:
    """Load Ramp OAuth 2.0 client config from the per-service credential store.

    Ramp has no legacy credentials location, so the per-service store
    (data/service_credentials/ramp.json, managed via the admin Settings UI)
    is the only source. Store reads are fresh on every call so admin updates
    take effect without a restart.

    Expected structure:
        { "client_id": "...", "client_secret": "..." }
    """
    from config.service_credentials import read_service_credentials

    stored = read_service_credentials("ramp")
    if stored:
        return stored
    raise HTTPException(
        status_code=500,
        detail=(
            "Ramp OAuth credentials not configured. Set them in "
            "Settings > Service Credentials (admin)."
        )
    )


def get_secret_key() -> str:
    """Get or generate the secret key for signing cookies."""
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text().strip()
    # Generate a new secret key
    key = secrets.token_urlsafe(32)
    SECRET_KEY_FILE.write_text(key)
    return key
