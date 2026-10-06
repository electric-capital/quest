#!/usr/bin/env python3
"""Startup script for Quest.

IMPORTANT: This script intentionally uses ONLY Python 3 standard library modules.
Do not add any third-party dependencies. This ensures the script can run on minimal
Python installations without needing pip or any package manager.

Compatibility: Python 3.8+

Usage:
  python run.py            # Local mode (default), port 9000, throwaway DB
  python run.py --local    # Same as above
  python run.py --staging  # Staging mode, port 8000, persistent data dir
  python run.py --prod     # Production mode, port 8000

Local-mode options:
  --keep-data [DIR]  Reuse the most recent local run's data directory
                     (or DIR when given) instead of creating a fresh one.

Prod-mode options:
  --bootstrap        Re-run the first-run configuration wizard even when the
                     deployment is already configured (existing values become
                     the prompt defaults).

Run modes (see config/environment.py for the semantics):
  local    Throwaway per-run database seeded with canned accounts and demo
           data (skipped with --keep-data). No OAuth needed: the sign-in
           screen offers one-click canned-account login. Missing credential
           files and container runtimes degrade gracefully.
  staging  Behaves like prod (domain restriction, real credentials) with a
           distinct cookie name; meant to be launched by deploy automation.
  prod     Current production behavior, plus a first-run bootstrap wizard
           (scripts/bootstrap_prod.py) when the deployment is missing
           admin_emails or Google OAuth credentials.

This script performs the following steps:
1. Install frontend dependencies and build React frontend
   (npm install in local, npm ci in staging/prod, then npm run build)
2. Check backend dependencies (uv sync)
3. Unlock the encryption-at-rest key (config/encryption.py): resolve the
   password (QUEST_ENCRYPTION_PASSWORD / _PASSWORD_FILE env, the fixed
   local-mode sentinel, or a TTY prompt), create the key file on first
   run, verify it otherwise
4. Run database migrations (uv run alembic upgrade head), plus seed data
   in local mode when the database is fresh
5. Validate the script-runner Podman image and auto-rebuild if needed
   (failures are non-fatal in local mode)
6. Start uvicorn server
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from typing import Optional

# Throwaway local-mode data directories live under data/local-runs/; the
# most recent LOCAL_RUNS_KEEP are retained, older ones are pruned at startup.
LOCAL_RUNS_DIRNAME = "local-runs"
LOCAL_RUNS_KEEP = 5

# Local mode: a JSON file at this well-known name in the checkout's parent
# directory holds shared developer config that is pre-baked into every local
# instance on the box (one file serves every quest checkout). Recognized keys:
#   vertex_service_account  The full service-account key JSON object. Written
#                           to <data_dir>/vertex-service-account.json and
#                           exported as GOOGLE_APPLICATION_CREDENTIALS so both
#                           Vertex integrations (Claude and Vertex-backed
#                           Gemini) authenticate via ADC.
#   service_credentials     Mapping of service name -> credentials object
#                           (e.g. "google_oauth"), copied verbatim into the
#                           per-service credential store at
#                           <data_dir>/service_credentials/<service>.json
#                           unless that file already exists, so the service
#                           shows up pre-configured in the admin Settings >
#                           Service Credentials section.
#   inference_credentials   Mapping of provider-instance id -> credentials
#                           object (e.g. "openrouter": {"api_key": "..."}),
#                           copied verbatim into the inference-credential
#                           store at
#                           <data_dir>/inference_credentials/<instance>.json
#                           unless that file already exists, so API-key LLM
#                           instances (Settings > Inference Providers) come
#                           up pre-configured in every local instance. The
#                           id "openrouter" is the legacy instance seeded
#                           with the historical curated models; any other
#                           id becomes an empty OpenRouter instance.
#   oauth_hostname          Hostname exported as QUEST_OAUTH_HOSTNAME so
#                           OAuth callback URLs use it instead of the raw
#                           request host (Google rejects private-IP
#                           redirect URIs); see oauth_base_url() in
#                           auth/config.py.
#   allowed_login_domain    Domain exported as QUEST_ALLOWED_LOGIN_DOMAIN;
#                           the Google login callback checks emails against
#                           it instead of the hardcoded company domain (the
#                           check still runs -- local-mode-only override,
#                           see allowed_login_domain() in auth/config.py).
DEV_CONFIG_FILENAME = "dev-config.json"

# dev-config.json keys exported verbatim as environment variables in local
# mode: (config key, env var, human label for the startup printout). The
# app-side consumers honor each env var over their own config files.
DEV_CONFIG_ENV_EXPORTS = [
    ("oauth_hostname", "QUEST_OAUTH_HOSTNAME", "OAuth callback hostname"),
    ("allowed_login_domain", "QUEST_ALLOWED_LOGIN_DOMAIN", "Google-login allowed domain"),
]

# Legacy well-known name for a bare Vertex service-account key in the parent
# directory; honored as a fallback when dev-config.json carries no
# vertex_service_account.
LEGACY_VERTEX_CREDENTIALS_FILENAME = "vertex-service-account.json"

# Service names in dev-config.json's service_credentials mapping double as
# file stems inside the data directory, so restrict them to safe characters.
SERVICE_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def get_data_dir(project_root: Path) -> Path:
    """Resolve the data directory (QUEST_DATA_DIR env, server_config.json, default).

    Mirrors the precedence in config/paths.py, which the app itself uses.

    Args:
        project_root: Path to the project root directory.

    Returns:
        The resolved data directory path, defaulting to project_root / "data".
    """
    env_override = os.environ.get("QUEST_DATA_DIR")
    if env_override:
        p = Path(env_override)
        if p.is_absolute():
            return p
        return (project_root / p).resolve()
    config_file = project_root / "server_config.json"
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            raw = config.get("data_dir")
            if raw:
                p = Path(raw)
                if p.is_absolute():
                    return p
                return (project_root / p).resolve()
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return project_root / "data"


def podman_image_exists(image_name: str) -> bool:
    """Check if a Podman image exists locally.

    Args:
        image_name: Name of the Podman image to check.

    Returns:
        True if the image exists, False otherwise.
    """
    result = subprocess.run(
        ["podman", "images", "-q", image_name],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def get_podman_image_created_time(image_name: str) -> Optional[datetime]:
    """Get the creation timestamp of a Podman image.

    Args:
        image_name: Name of the Podman image.

    Returns:
        The creation datetime, or None if the image doesn't exist.
    """
    result = subprocess.run(
        ["podman", "inspect", "--format", "{{.Created}}", image_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    timestamp_str = result.stdout.strip()
    # Podman returns ISO timestamp with nanoseconds, same format as Docker
    try:
        if "." in timestamp_str:
            base, frac = timestamp_str.split(".")
            frac = frac.rstrip("Z")[:6].ljust(6, "0")
            timestamp_str = f"{base}.{frac}+00:00"
        else:
            timestamp_str = timestamp_str.rstrip("Z") + "+00:00"
        return datetime.fromisoformat(timestamp_str)
    except ValueError:
        return None


def should_rebuild_podman_image(
    project_root: Path,
    image_name: str,
    dockerfile_name: str,
    extra_sources: Optional[list[str]] = None,
) -> bool:
    """Check if an image's source files are newer than the built Podman image.

    Args:
        project_root: Path to the project root directory.
        image_name: Name of the Podman image.
        dockerfile_name: Name of the Dockerfile to check against.
        extra_sources: Additional source files (e.g. the entrypoint script
            COPY'd into the image) whose changes should also trigger a rebuild.

    Returns:
        True if any source file is newer and the image should be rebuilt.
    """
    dockerfile = project_root / dockerfile_name
    if not dockerfile.exists():
        return False

    image_created = get_podman_image_created_time(image_name)
    if image_created is None:
        return False

    sources = [dockerfile] + [
        project_root / name for name in (extra_sources or [])
    ]
    for source in sources:
        if not source.exists():
            continue
        source_mtime = datetime.fromtimestamp(
            source.stat().st_mtime,
            tz=timezone.utc,
        )
        if source_mtime > image_created:
            return True
    return False


def prepare_local_data_dir(project_root: Path, keep_data: Optional[str]) -> Path:
    """Pick the throwaway data directory for a local run.

    By default a fresh timestamped directory is created under
    ``data/local-runs/`` and older run directories are pruned (keeping the
    ``LOCAL_RUNS_KEEP`` most recent). With ``--keep-data`` the most recent
    existing run directory (or an explicitly given directory) is reused so
    state survives across restarts while iterating.
    """
    runs_root = project_root / "data" / LOCAL_RUNS_DIRNAME
    runs_root.mkdir(parents=True, exist_ok=True)

    if keep_data and keep_data != "latest":
        return Path(keep_data).resolve()

    existing = sorted(p for p in runs_root.iterdir() if p.is_dir())
    if keep_data == "latest":
        if existing:
            return existing[-1]
        print("! --keep-data: no previous local run found, creating a fresh one")

    run_dir = runs_root / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    for stale in existing[: -(LOCAL_RUNS_KEEP - 1)] if LOCAL_RUNS_KEEP > 1 else existing:
        print(f"  Pruning old local run data: {stale.name}")
        shutil.rmtree(stale, ignore_errors=True)

    return run_dir


def load_dev_config(project_root: Path) -> dict:
    """Read the shared parent-directory dev-config.json (local mode).

    Returns an empty dict when the file is absent or malformed; a broken
    shared config must never prevent a local instance from starting.
    """
    path = project_root.parent / DEV_CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"! Ignoring unreadable {path}: {e}")
        return {}
    if not isinstance(data, dict):
        print(f"! Ignoring {path}: expected a JSON object")
        return {}
    return data


def _write_private_json(path: Path, data: dict) -> None:
    """Write a JSON file created with 0600 permissions."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def setup_vertex_credentials(project_root: Path, data_dir: Path, dev_config: dict) -> None:
    """Export GOOGLE_APPLICATION_CREDENTIALS for local mode.

    The key comes from dev-config.json's ``vertex_service_account`` object
    (materialized into the run's data directory), falling back to the legacy
    bare key file in the parent directory. An externally-set
    GOOGLE_APPLICATION_CREDENTIALS always wins.
    """
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return

    key = dev_config.get("vertex_service_account")
    if isinstance(key, dict) and key:
        key_path = data_dir / LEGACY_VERTEX_CREDENTIALS_FILENAME
        _write_private_json(key_path, key)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(key_path)
        print(f"Vertex AI credentials: {DEV_CONFIG_FILENAME} -> {key_path}")
        print()
        return

    legacy_key = project_root.parent / LEGACY_VERTEX_CREDENTIALS_FILENAME
    if legacy_key.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(legacy_key)
        print(f"Vertex AI credentials: {legacy_key}")
        print('  Note: this bare key file is deprecated; move its contents into')
        print(f'  {project_root.parent / DEV_CONFIG_FILENAME} under "vertex_service_account".')
        print()


def setup_server_vertex_credentials(data_dir: Path) -> None:
    """Export GOOGLE_APPLICATION_CREDENTIALS in staging/prod.

    The prod bootstrap wizard copies the operator's GCP service-account key
    to <data_dir>/vertex-service-account.json; point ADC at it so both
    Vertex integrations (Claude and Vertex-backed Gemini) authenticate
    without any manual env-var setup. An externally-set
    GOOGLE_APPLICATION_CREDENTIALS always wins.
    """
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    key_path = data_dir / LEGACY_VERTEX_CREDENTIALS_FILENAME
    if key_path.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(key_path)
        print(f"Vertex AI credentials: {key_path}")
        print()


def export_dev_config_env(dev_config: dict) -> None:
    """Export DEV_CONFIG_ENV_EXPORTS entries as env vars (local mode).

    Only non-empty string values are exported; an externally-set env var
    always wins over the dev-config value.
    """
    for key, env_var, label in DEV_CONFIG_ENV_EXPORTS:
        if os.environ.get(env_var):
            continue
        value = dev_config.get(key)
        if isinstance(value, str) and value:
            os.environ[env_var] = value
            print(f"{label}: {value} (from {DEV_CONFIG_FILENAME})")
            print()


def prebake_service_credentials(data_dir: Path, dev_config: dict) -> list[str]:
    """Copy dev-config.json service credentials into the per-service store.

    Mirrors the layout of config/service_credentials.py (directory 0700,
    files 0600) without importing app code. An existing per-service file
    always wins so credentials saved through the admin UI (e.g. in a
    --keep-data directory) are never clobbered. Returns the services baked.
    """
    services = dev_config.get("service_credentials")
    if not isinstance(services, dict):
        return []

    store = data_dir / "service_credentials"
    baked: list[str] = []
    for service, creds in sorted(services.items()):
        if (
            not isinstance(service, str)
            or not service
            or not set(service) <= SERVICE_NAME_CHARS
            or not isinstance(creds, dict)
            or not creds
        ):
            print(f"! {DEV_CONFIG_FILENAME}: skipping malformed service_credentials entry {service!r}")
            continue
        target = store / f"{service}.json"
        if target.exists():
            continue
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private_json(target, creds)
        baked.append(service)
        print(f"Pre-baked {service} credentials from {DEV_CONFIG_FILENAME}")
    if baked:
        print()
    return baked


def prebake_inference_credentials(data_dir: Path, dev_config: dict) -> list[str]:
    """Copy dev-config.json inference credentials into the per-provider store.

    Mirrors prebake_service_credentials for the inference-credential store
    (config/inference_providers.py layout: directory 0700, files 0600). An
    existing per-provider file always wins so keys saved through the admin
    UI (e.g. in a --keep-data directory) are never clobbered. Returns the
    providers baked.
    """
    providers = dev_config.get("inference_credentials")
    if not isinstance(providers, dict):
        return []

    store = data_dir / "inference_credentials"
    baked: list[str] = []
    for provider, creds in sorted(providers.items()):
        if (
            not isinstance(provider, str)
            or not provider
            or not set(provider) <= SERVICE_NAME_CHARS
            or not isinstance(creds, dict)
            or not creds
        ):
            print(f"! {DEV_CONFIG_FILENAME}: skipping malformed inference_credentials entry {provider!r}")
            continue
        target = store / f"{provider}.json"
        if target.exists():
            continue
        store.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private_json(target, creds)
        baked.append(provider)
        print(f"Pre-baked {provider} inference credentials from {DEV_CONFIG_FILENAME}")
    if baked:
        print()
    return baked


def resolve_encryption_password(local_mode: bool) -> str:
    """Resolve the encryption-at-rest password before any build step.

    Order (config/encryption.py resolve_password): QUEST_ENCRYPTION_PASSWORD,
    QUEST_ENCRYPTION_PASSWORD_FILE, the fixed local-mode sentinel, then an
    interactive prompt (confirmed twice when the key file does not exist
    yet). A non-interactive staging/prod startup with no password aborts
    with instructions -- there is nothing sensible to start without it.
    """
    from config.encryption import (
        LOCAL_DEV_PASSWORD,
        PASSWORD_ENV,
        PASSWORD_FILE_ENV,
        encryption_key_path,
        resolve_password,
    )

    key_path = encryption_key_path()
    password = resolve_password(interactive=True, confirm=not key_path.exists())
    if not password:
        print("! No encryption password available for the credential store.")
        print(f"  Set {PASSWORD_ENV} (or {PASSWORD_FILE_ENV} pointing at a file")
        print("  holding the password) in the service environment, or start from")
        print("  an interactive terminal to be prompted. See")
        print("  docs/architecture/encryption-at-rest.md.")
        sys.exit(1)
    if password == LOCAL_DEV_PASSWORD and not local_mode:
        print("! The local-mode development encryption password cannot be used")
        print("  in staging/prod. Choose a real password.")
        sys.exit(1)
    return password


def unlock_encryption_key(project_root: Path, password: str) -> None:
    """Export the password for the subprocesses and create/verify the key file.

    ``config.encryption init`` (run under uv so the cryptography package is
    available -- run.py itself stays stdlib-only) creates
    ``<data_dir>/encryption_key.json`` on first run, verifies the password
    unlocks it otherwise, and encrypts any plaintext credential store files
    left by the bootstrap wizard / local pre-baking. The alembic, seeder,
    and uvicorn subprocesses inherit the exported password.
    """
    from config.encryption import PASSWORD_ENV

    os.environ[PASSWORD_ENV] = password
    try:
        subprocess.run(
            ["uv", "run", "python", "-m", "config.encryption", "init"],
            cwd=project_root,
            check=True,
        )
    except subprocess.CalledProcessError:
        print("! Could not unlock the credential encryption key (see the message above).")
        sys.exit(1)


def print_local_service_summary(project_root: Path, data_dir: Path) -> None:
    """Print which optional credentials/services are configured (local mode).

    Everything listed here is optional in local mode: missing entries mean
    the corresponding connector/model is unavailable, not that startup fails.
    """
    def _read_json(path: Path) -> dict:
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {}

    creds = _read_json(project_root / "server_credentials.json")
    config = _read_json(project_root / "server_config.json")
    # Per-service credential store (admin UI managed) wins over the legacy
    # files; a reused --keep-data directory may carry one.
    service_store = data_dir / "service_credentials"

    def _service_configured(service: str, legacy: dict | bool) -> bool:
        return bool(_read_json(service_store / f"{service}.json") or legacy)

    google_oauth_configured = _service_configured(
        "google_oauth", creds.get("google_oauth")
    )

    anthropic_project = (
        os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
        or config.get("anthropic", {}).get("vertex_project_id")
    )
    gemini_vertex_project = (
        os.environ.get("GEMINI_VERTEX_PROJECT_ID")
        or config.get("gemini_vertex", {}).get("vertex_project_id")
        or anthropic_project
    )
    # One credential file per provider instance (Settings > Inference
    # Providers; dev-config pre-baking). A store file is encrypted
    # ({"encrypted": ...}) once the app has run; a freshly pre-baked one is
    # still plaintext ({"api_key": ...}).
    inference_store = data_dir / "inference_credentials"
    openrouter_configured = any(
        _read_json(path).get("api_key") or _read_json(path).get("encrypted")
        for path in (
            sorted(inference_store.glob("*.json")) if inference_store.is_dir() else []
        )
    )

    checks = [
        ("Anthropic on Vertex (Claude models)", bool(anthropic_project)),
        ("Gemini on Vertex (all Gemini models)", bool(gemini_vertex_project)),
        ("Provider instances (OpenRouter keys / self-hosted servers)", openrouter_configured),
        ("Google OAuth (login + Google services)", google_oauth_configured),
        ("Slack", _service_configured("slack", creds.get("slack"))),
        ("GitHub OAuth", _service_configured("github", creds.get("github"))),
        ("Twitter/X", _service_configured(
            "twitter", (project_root / "twitter_credentials.json").exists()
        )),
        ("Ramp OAuth", _service_configured("ramp", creds.get("ramp"))),
        ("Telegram", _service_configured(
            "telegram", bool(config.get("telegram_app_info"))
        )),
        ("CoinGecko", _service_configured("coingecko", creds.get("coingecko"))),
    ]
    print("Optional service credentials (missing ones degrade gracefully):")
    for label, ok in checks:
        print(f"  {'+' if ok else '-'} {label}")
    if not any(ok for _, ok in checks[:3]):
        print("  ! No LLM credentials found: the app will run but chat is disabled.")
    print()


def main() -> None:
    """Run the setup and start the server."""
    parser = argparse.ArgumentParser(description="Start the Quest server.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--prod", action="store_true", help="Run in production mode (port 8000)")
    group.add_argument("--staging", action="store_true", help="Run in staging mode (port 8000, prod-like behavior, distinct cookie)")
    group.add_argument("--local", action="store_true", help="Run in local mode (port 9000, throwaway seeded DB, canned-account login) [default]")
    group.add_argument("--dev", action="store_true", help="Deprecated alias for --local")
    parser.add_argument(
        "--keep-data",
        nargs="?",
        const="latest",
        default=None,
        metavar="DIR",
        help="(local mode) reuse the most recent local run's data directory, or DIR when given, instead of creating a fresh throwaway one",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="(prod mode) re-run the first-run configuration wizard even when the deployment is already configured",
    )
    args = parser.parse_args()

    if args.prod:
        mode = "prod"
    elif args.staging:
        mode = "staging"
    else:
        mode = "local"
        if args.dev:
            print("Note: --dev is deprecated; use --local (same behavior).")

    local_mode = mode == "local"
    project_root = Path(__file__).parent.resolve()

    # In local mode, check dev_config.json for a custom port
    port = "8000"
    if local_mode:
        port = "9000"
        dev_config_file = project_root / "dev_config.json"
        if dev_config_file.exists():
            try:
                with open(dev_config_file) as f:
                    dev_config = json.load(f)
                port = str(dev_config.get("dev_port", port))
            except (json.JSONDecodeError, KeyError):
                pass

    mode_label = {"local": "Local", "staging": "Staging", "prod": "Production"}[mode]

    # Set environment variables so the app can detect the run mode
    # (see config/environment.py) and know which port the server is on.
    os.environ["QUEST_ENV"] = mode
    os.environ["QUEST_PORT"] = port

    # Sandbox tool API port (loopback-only second server serving just the
    # script-facing endpoints; script containers are confined to it -- see
    # chat/sandbox_api.py). Defaults to the main port + 1, which keeps the
    # per-checkout convention collision-free (9x00 -> 9x01).
    sandbox_port = os.environ.get("QUEST_SANDBOX_PORT") or str(int(port) + 1)
    os.environ["QUEST_SANDBOX_PORT"] = sandbox_port

    # Local mode: point QUEST_DATA_DIR at a throwaway per-run directory so
    # the database, chats, workspaces, and secret key are all isolated from
    # any real data. An externally-set QUEST_DATA_DIR wins.
    if local_mode and not os.environ.get("QUEST_DATA_DIR"):
        local_data_dir = prepare_local_data_dir(project_root, args.keep_data)
        os.environ["QUEST_DATA_DIR"] = str(local_data_dir)
        print(f"Local data directory: {local_data_dir}")
        print()

    data_dir = get_data_dir(project_root)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Prod mode: first-run bootstrap wizard when the deployment is missing
    # the config a usable instance cannot run without (admin_emails, Google
    # OAuth). Runs before the build steps so the operator is prompted
    # immediately; aborts non-interactive unconfigured startups.
    if mode == "prod":
        from scripts.bootstrap_prod import maybe_run_bootstrap
        maybe_run_bootstrap(project_root, data_dir, force=args.bootstrap)

    # Local mode: pre-bake shared developer config from the parent
    # directory's dev-config.json into this run's data directory (Vertex
    # service-account key exported as ADC, per-service credentials into the
    # service_credentials store).
    if local_mode:
        dev_config = load_dev_config(project_root)
        setup_vertex_credentials(project_root, data_dir, dev_config)
        export_dev_config_env(dev_config)
        prebake_service_credentials(data_dir, dev_config)
        prebake_inference_credentials(data_dir, dev_config)
    else:
        # Staging/prod: pick up the bootstrap-written service-account key
        # as ADC so Vertex-backed models work without manual env setup.
        setup_server_vertex_credentials(data_dir)

    # Resolve the credential-encryption password now (prompting if needed)
    # so the operator is not kept waiting through the build steps; it is
    # only exported to the environment right before the step that needs it.
    encryption_password = resolve_encryption_password(local_mode)

    frontend_dir = project_root / "frontend"

    # Add node_modules/.bin to PATH so npm build tools are available
    node_bin = str(frontend_dir / "node_modules" / ".bin")
    os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")

    print(f"=== Quest {mode_label} Mode Setup ===")
    print()

    # Step 1: Install frontend dependencies and build React frontend
    print("[1/6] Building React frontend...")
    # In staging/prod, use `npm ci` so package-lock.json is treated as
    # authoritative and never rewritten on the deployed box (different npm
    # versions or healing passes can otherwise dirty the lockfile on every
    # sync).
    npm_install_cmd = ["npm", "install"] if local_mode else ["npm", "ci"]
    subprocess.run(
        npm_install_cmd,
        cwd=frontend_dir,
        check=True,
    )
    subprocess.run(
        ["npm", "run", "build"],
        cwd=frontend_dir,
        check=True,
    )
    print("+ Frontend build complete")
    print()

    # Step 2: Ensure backend dependencies are installed
    print("[2/6] Checking backend dependencies...")
    subprocess.run(
        ["uv", "sync"],
        cwd=project_root,
        check=True,
    )
    print("+ Backend dependencies ready")
    print()

    # Step 3: Unlock (or create) the encryption-at-rest key. Must precede
    # the migrations: the secret-encryption migration and every ORM write
    # of a credential column need the unwrapped key.
    print("[3/6] Unlocking credential encryption...")
    unlock_encryption_key(project_root, encryption_password)
    print()

    # Step 4: Run database migrations (+ seed data on a fresh local DB)
    print("[4/6] Running database migrations...")
    # Ensure logs directory exists for the large_tool_results RotatingFileHandler
    # configured in logging_config.json (must exist before uvicorn applies the config).
    (data_dir / "logs").mkdir(exist_ok=True)
    # Rename a database created before the praixy -> quest project rename
    # (praixy.db and WAL/SHM sidecars) so existing deployments keep their data.
    from config.paths import migrate_legacy_database_file
    migrate_legacy_database_file()
    fresh_database = not (data_dir / "quest.db").exists()
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=project_root,
        check=True,
    )
    print("+ Database migrations applied")

    if local_mode and fresh_database:
        print("  Seeding local demo data (canned accounts, sample project/chats)...")
        subprocess.run(
            ["uv", "run", "python", "scripts/seed_local.py"],
            cwd=project_root,
            check=True,
        )
        print("+ Local demo data seeded")
    print()

    # Step 5: Validate the script-runner Podman image. In local mode a
    # missing/broken container runtime is non-fatal: the corresponding
    # tools are simply unavailable at runtime.
    print("[5/6] Validating container images...")
    quest_env = os.environ["QUEST_ENV"]  # Already set above

    def validate_script_runner_image() -> None:
        # Script runner images (Podman) -- optional, only if Dockerfile
        # exists. Two separate images: the restricted default sandbox and
        # the internet-enabled public-project sandbox, each with its own
        # Dockerfile + entrypoint so their tooling can diverge.
        images = [
            (
                f"quest-script-runner-{quest_env}",
                "Dockerfile.script-runner",
                ["script-runner-entry.sh", "script-runner-soffice.sh"],
            ),
            (
                f"quest-script-runner-public-{quest_env}",
                "Dockerfile.script-runner-public",
                ["script-runner-entry-public.sh", "script-runner-soffice.sh"],
            ),
        ]
        for image_name, dockerfile, entry_sources in images:
            if not (project_root / dockerfile).exists():
                continue
            if not podman_image_exists(image_name):
                print(f"  Building Podman image '{image_name}'...")
                subprocess.run(
                    ["podman", "build", "--network=host", "-t", image_name, "-f", dockerfile, "."],
                    cwd=project_root,
                    check=True,
                )
                print(f"+ Podman image '{image_name}' built successfully")
            else:
                print(f"+ Podman image '{image_name}' found")
                if should_rebuild_podman_image(project_root, image_name, dockerfile, extra_sources=entry_sources):
                    print(f"! {dockerfile} sources have been modified since image was built")
                    print(f"  Rebuilding Podman image '{image_name}'...")
                    subprocess.run(
                        ["podman", "build", "--network=host", "-t", image_name, "-f", dockerfile, "."],
                        cwd=project_root,
                        check=True,
                    )
                    print(f"+ Podman image '{image_name}' rebuilt successfully")

    if local_mode:
        try:
            validate_script_runner_image()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"! Podman image setup failed ({e}).")
            print("  Continuing without it: run_script/run_python tools will be")
            print("  unavailable this run.")
    else:
        validate_script_runner_image()

    print()

    if local_mode:
        print_local_service_summary(project_root, data_dir)

    # Step 6: Start backend server
    print(f"[6/6] Starting backend server ({mode_label.lower()} mode)...")
    from config.version import describe_release
    print(f"  Release: {describe_release()}")
    print(f"Server will run on http://0.0.0.0:{port}")
    print()
    print("Access points:")
    print(f"  - Chat app: http://localhost:{port}/")
    print(f"  - Auth:     http://localhost:{port}/auth/")
    print(f"  - API docs: http://localhost:{port}/api-docs")
    print(f"  - Sandbox tool API (script containers): http://127.0.0.1:{sandbox_port}/")
    print()
    print("Press Ctrl+C to stop the server")
    print()

    # Build the logging config: start from logging_config.json, patch the
    # log file path to match the configured data directory, and optionally
    # set DEBUG levels for dev mode.
    with open(project_root / "logging_config.json") as f:
        log_config = json.load(f)

    # Patch the log file handler to use the resolved data directory
    log_file_path = str(data_dir / "logs" / "large_tool_results.jsonl")
    handler = log_config.get("handlers", {}).get("large_tool_results_file")
    if handler:
        handler["filename"] = log_file_path

    if local_mode:
        for logger_name in ("chat", "auth", "quest"):
            if logger_name in log_config.get("loggers", {}):
                log_config["loggers"][logger_name]["level"] = "DEBUG"

    log_fd, log_config_path = tempfile.mkstemp(suffix=".json", prefix="logging_config_")
    with os.fdopen(log_fd, "w") as f:
        json.dump(log_config, f, indent=4)

    uvicorn_cmd = [
        "uv", "run", "uvicorn", "quest:app",
        "--host", "0.0.0.0",
        "--port", port,
        "--log-config", log_config_path,
    ]

    try:
        subprocess.run(uvicorn_cmd, cwd=project_root, check=True)
    except subprocess.CalledProcessError as e:
        if e.returncode in (130, 143):
            # 130 = SIGINT (128+2), 143 = SIGTERM (128+15) — expected graceful shutdown
            print()
            print("Server stopped.")
        else:
            raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("Server stopped.")
        sys.exit(0)
