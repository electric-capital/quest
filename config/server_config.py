"""Server configuration loading.

Home of ``load_server_config()`` (server_config.json with defaults and
env-var overrides). Historically this lived in ``chat/gemini_docker.py``;
it moved here when the Docker-based Gemini CLI chat mode was removed.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
SERVER_CREDENTIALS_FILE = PROJECT_ROOT / "server_credentials.json"
SERVER_CONFIG_FILE = PROJECT_ROOT / "server_config.json"


def load_server_config() -> dict:
    """Load server configuration from file.

    Returns:
        Configuration dictionary with defaults for all providers.
    """
    from config import environment

    # In local mode the seeded canned admin is an admin by default so a
    # zero-config checkout gets a working admin account; a server_config.json
    # admin_emails list still overrides this.
    default_admin_emails = (
        [environment.LOCAL_CANNED_ADMIN_EMAIL] if environment.is_local() else []
    )

    config = {
        "admin_emails": default_admin_emails,
        # Optional full public URL this deployment is accessed through
        # (e.g. "https://quest.example.com"). Used for absolute links the
        # app generates to itself outside of a request context (e.g.
        # Slack attribution) and, when set, as the base for OAuth
        # redirect/callback URLs. See get_app_base_url() below.
        "app_base_url": "",
        # Optional hostname override for OAuth redirect/callback URLs (some
        # providers, e.g. Google, reject raw-IP redirect URIs). Hostname
        # only -- scheme and port come from the incoming request.
        # app_base_url, when set, takes precedence for OAuth callbacks.
        "oauth_hostname": "",
        # Optional email-domain override for sign-in (empty = the built-in
        # ALLOWED_DOMAIN in auth/config.py). Set by the prod bootstrap
        # wizard so deployments outside the default company domain work.
        "allowed_login_domain": "",
        # Optional individual email addresses allowed to sign in in
        # addition to the allowed domain (see is_login_allowed in
        # auth/config.py). Lets a deployment admit accounts outside any
        # single domain, e.g. personal @gmail.com users.
        "allowed_login_emails": [],
        # How users sign in: "google" (Google OAuth) or "password" (email +
        # password accounts, see auth/password_login.py). Exactly one method
        # is active. Absent means "google" so deployments that predate
        # password sign-in keep working; the prod bootstrap wizard writes
        # "password" for new deployments. See login_method() in
        # auth/config.py.
        "login_method": "",
        "gemini": {
            "model": "gemini-3.1-pro-preview",
        },
        "anthropic": {
            "vertex_project_id": "",
            "vertex_region": "us-east5",
        },
        "gemini_vertex": {
            "vertex_project_id": "",
            # Gemini 3.x models (including gemini-3.5-flash) require the
            # ``global`` endpoint on Vertex AI; regional endpoints such as
            # ``us-central1`` return 404 for the publisher catalog. Set
            # ``GEMINI_VERTEX_REGION`` to override for older models that
            # still need a regional location.
            "vertex_region": "global",
        },
        # Local speech recognition for the composer's live dictation
        # (chat/live_transcription.py): a Qwen3-ASR checkpoint run in this
        # process on CPU or GPU. ``model`` is a Hugging Face repo id or a
        # local directory; empty = live dictation unavailable (the clip
        # mode on Gemini still works). ``device`` is a torch device string,
        # ``dtype`` a torch dtype name, ``threads`` the CPU thread count
        # (0 = torch's default) and ``preload`` whether the weights load at
        # startup instead of on the first session.
        "local_asr": {
            "model": "",
            "device": "cpu",
            "dtype": "bfloat16",
            "threads": 0,
            "preload": True,
        },
    }

    if SERVER_CONFIG_FILE.exists():
        with open(SERVER_CONFIG_FILE, "r") as f:
            user_config = json.load(f)
            # Merge user config
            if "gemini" in user_config:
                config["gemini"].update(user_config["gemini"])
            if "anthropic" in user_config:
                config["anthropic"].update(user_config["anthropic"])
            if "gemini_vertex" in user_config:
                config["gemini_vertex"].update(user_config["gemini_vertex"])
            if isinstance(user_config.get("local_asr"), dict):
                config["local_asr"].update(user_config["local_asr"])
            # Preserve top-level keys like admin_emails
            config["admin_emails"] = user_config.get(
                "admin_emails", default_admin_emails
            )
            config["app_base_url"] = user_config.get("app_base_url", "")
            config["oauth_hostname"] = user_config.get("oauth_hostname", "")
            config["allowed_login_domain"] = user_config.get(
                "allowed_login_domain", ""
            )
            config["allowed_login_emails"] = user_config.get(
                "allowed_login_emails", []
            )
            config["login_method"] = user_config.get("login_method", "")

    # Public app URL env var override
    env_app_base_url = os.getenv("QUEST_APP_BASE_URL")
    if env_app_base_url:
        config["app_base_url"] = env_app_base_url

    # OAuth hostname env var override
    env_oauth_hostname = os.getenv("QUEST_OAUTH_HOSTNAME")
    if env_oauth_hostname:
        config["oauth_hostname"] = env_oauth_hostname

    # Anthropic env var overrides
    env_project = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
    if env_project:
        config["anthropic"]["vertex_project_id"] = env_project
    env_region = os.getenv("ANTHROPIC_VERTEX_REGION")
    if env_region:
        config["anthropic"]["vertex_region"] = env_region

    # Gemini Vertex env var overrides
    env_gv_project = os.getenv("GEMINI_VERTEX_PROJECT_ID")
    if env_gv_project:
        config["gemini_vertex"]["vertex_project_id"] = env_gv_project
    env_gv_region = os.getenv("GEMINI_VERTEX_REGION")
    if env_gv_region:
        config["gemini_vertex"]["vertex_region"] = env_gv_region

    # Local ASR env var override (repo id or path; empty string disables)
    env_local_asr = os.getenv("QUEST_LOCAL_ASR_MODEL")
    if env_local_asr is not None:
        config["local_asr"]["model"] = env_local_asr

    # Fallback: if no gemini_vertex project id is configured, reuse the
    # Anthropic Vertex project id (region intentionally not inherited --
    # Anthropic Vertex defaults to us-east5 which is not a typical Gemini
    # region; Gemini 3.x requires the ``global`` endpoint).
    if not config["gemini_vertex"]["vertex_project_id"]:
        config["gemini_vertex"]["vertex_project_id"] = (
            config["anthropic"]["vertex_project_id"]
        )

    return config


def get_app_base_url() -> str:
    """Public base URL this deployment is accessed through, or "".

    The optional top-level ``app_base_url`` key in server_config.json (env
    override: ``QUEST_APP_BASE_URL``) holds the full external URL of the
    deployment, e.g. ``https://quest.example.com``. It is used wherever the
    app needs an absolute link to itself with no HTTP request in scope
    (e.g. the "created using Quest" attribution on outgoing Slack
    messages -- those links are omitted when unset) and, when set,
    takes precedence over the request host and ``oauth_hostname`` for OAuth
    redirect/callback URLs (see oauth_base_url() in auth/config.py).

    Returned without a trailing slash; "" when not configured.
    """
    return load_server_config().get("app_base_url", "").rstrip("/")


def check_vertex_credentials_consistency() -> None:
    """Log startup diagnostics when ADC credentials and Vertex config disagree.

    Model availability is a config-presence check (``vertex_project_id`` in
    server_config.json), while Vertex auth uses Application Default
    Credentials -- typically a service-account key file pointed at by
    ``GOOGLE_APPLICATION_CREDENTIALS``. The two can silently drift apart:

    - A key file is present but no ``vertex_project_id`` is configured, so
      every Vertex-backed model is hidden from the picker despite working
      credentials (warning).
    - A configured ``vertex_project_id`` differs from the key file's
      ``project_id``, so Vertex calls will likely fail at send time with an
      auth/permission error (error; a cross-project service account grant is
      possible but rare enough to flag).

    Best-effort diagnostics only: never raises, and silent when
    ``GOOGLE_APPLICATION_CREDENTIALS`` is unset (ADC may legitimately come
    from gcloud user credentials or the metadata server, which carry no local
    project id to compare).
    """
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path:
        return

    try:
        with open(key_path, "r") as f:
            key_project = json.load(f).get("project_id", "")
    except (OSError, ValueError):
        logger.warning(
            "[vertex-config] GOOGLE_APPLICATION_CREDENTIALS points at %s "
            "but the file could not be read as a service-account key",
            key_path,
        )
        return
    if not key_project:
        logger.warning(
            "[vertex-config] service-account key %s has no project_id field",
            key_path,
        )
        return

    config = load_server_config()
    configured = {
        section: config[section]["vertex_project_id"]
        for section in ("anthropic", "gemini_vertex")
        if config[section]["vertex_project_id"]
    }

    if not configured:
        logger.warning(
            "[vertex-config] service-account key %s (project %s) is set via "
            "GOOGLE_APPLICATION_CREDENTIALS, but no vertex_project_id is "
            "configured in server_config.json -- Vertex-backed models "
            "(Claude and Gemini 3.5+/3.6) are hidden from the model picker",
            key_path,
            key_project,
        )
        return

    for section, project_id in configured.items():
        if project_id != key_project:
            logger.error(
                "[vertex-config] %s.vertex_project_id is %r but the "
                "service-account key %s belongs to project %r -- Vertex "
                "calls will likely fail unless the account has been granted "
                "cross-project access",
                section,
                project_id,
                key_path,
                key_project,
            )


def update_server_config(updates: dict) -> None:
    """Merge top-level *updates* into server_config.json, preserving other keys.

    Used by the few admin actions that change deployment-level settings
    from the app (the sign-in method switch, invite-driven additions to
    ``allowed_login_emails``). Written atomically via a temp file + rename
    so a crash never leaves a truncated config behind.
    """
    config = {}
    if SERVER_CONFIG_FILE.exists():
        with open(SERVER_CONFIG_FILE, "r") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError("server_config.json does not hold a JSON object")
    config.update(updates)
    tmp = SERVER_CONFIG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.replace(tmp, SERVER_CONFIG_FILE)
