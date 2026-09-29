"""Inference-provider configuration: provider instances, per-instance model
lists, the Vertex enabled-model set, and the per-instance API-key store.

Two kinds of inference configuration are surfaced in Settings > Inference
Providers:

- **Vertex AI** (exactly one): credentials come from the environment (a
  service-account key file pointed at by ``GOOGLE_APPLICATION_CREDENTIALS``,
  or gcloud Application Default Credentials) plus ``vertex_project_id`` /
  ``vertex_region`` in ``server_config.json`` / env vars. The panel only
  *displays* what was detected -- it does not edit Vertex credentials. Its
  model catalog is fixed (the ``MODEL_REGISTRY`` in chat/llm/config.py);
  the one piece of admin state is the set of models an admin has DISABLED
  (``vertex.disabled_models``): a disabled model is hidden from the picker
  and never health-checked, but existing conversations keep running on it.

- **Provider instances** (zero or more): each is one configuration of a
  provider kind (:data:`INSTANCE_KINDS`) with an admin-chosen label and its
  own admin-chosen model list. ``openrouter`` instances hold an API key and
  pick models from the OpenRouter catalog (or a typed custom id);
  ``local`` instances point at a self-hosted inference server (``base_url``
  + ``api_type``, optional key) and pick models from what that server
  reports (``chat/llm/local_catalog.py``). Several instances of the same
  kind can coexist (e.g. a personal and a team OpenRouter key with
  different curated models, or a llama.cpp box and an Ollama box).

**Qualified model ids.** Models served by an instance are identified
everywhere (``conversations.model``, ``routines.model``, user defaults, the
health store, analytics rows) by ``<instance_id>:<wire_model_id>``, e.g.
``openrouter:deepseek/deepseek-v4-flash-0731``. Instance ids never contain
``:`` or ``/`` (see :data:`INSTANCE_ID_RE`), while every OpenRouter wire id
contains a ``/`` before any ``:variant`` suffix, so "split on the first colon
and check the left part is an instance id" is unambiguous
(:func:`split_model_id`). Vertex model ids stay bare. The legacy single
OpenRouter configuration (credential file ``openrouter.json``) is instance
:data:`LEGACY_OPENROUTER_INSTANCE_ID`; alembic migration ``b8e4d2a7c1f5``
prefixed the bare ids stored before instances existed, and
``resolve_model`` in chat/llm/config.py still accepts a bare OpenRouter id
as belonging to that instance.

**Storage.** Non-secret configuration lives in ``DATA_DIR /
"inference_providers.json"`` (:func:`load_inference_config` /
:func:`save_inference_config`, atomic writes, same scheme as
``feature_gates.json``). API keys live in one file per instance under
``DATA_DIR / "inference_credentials" / "<instance_id>.json"`` (mode 0600,
directory 0700, encrypted at rest in the same ``{"encrypted": "qenc1:..."}``
wrapper as the service-credential store -- see config/service_credentials.py;
plaintext files written by local-mode pre-baking are converted by
:func:`encrypt_plaintext_inference_files`). The instance id IS the credential
file stem, which is what lets ``run.py`` pre-bake keys from the parent
directory dev-config.json (``inference_credentials: {"<instance_id>":
{"api_key": ...}}``) and what makes the legacy ``openrouter.json`` file the
``openrouter`` instance without any migration: :func:`load_inference_config`
synthesizes an instance entry for every credential file that has none.

This module sticks to the Python standard library (mirroring
``config/service_credentials.py``) so it can be imported early and from the
db layer (``db/llm_pricing.py`` reads per-model pricing snapshots from here);
the only non-stdlib touchpoint is a lazy import of ``load_server_config``
inside ``vertex_environment_status()``.
"""

import copy
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from config.paths import INFERENCE_CREDENTIALS_DIR, INFERENCE_PROVIDERS_FILE
from config.service_credentials import (
    _is_encrypted_file,
    _read_json_object,
    _unwrap_encrypted_file,
    _wrap_encrypted_file,
)

logger = logging.getLogger(__name__)

# Provider-instance kinds an admin can add. ``provider`` is the LLMProvider
# implementation name (chat/llm/config.py get_provider_instance), ``backend``
# the cost-analytics transport label recorded on llm_calls_* rows, ``hint``
# the API-key input placeholder, ``key_required`` whether the instance is
# unusable without a stored key (a self-hosted server usually has none:
# the key is optional and sent as a bearer token only when set), and
# ``endpoint`` whether the instance carries its own ``base_url`` +
# ``api_type`` (self-hosted servers) instead of a fixed upstream.
#
# Both kinds run on the ``openrouter`` LLMProvider family -- the OpenAI
# chat-completions message format, history shape and analytics table
# (``llm_calls_openrouter``) -- because every self-hosted server speaks
# that protocol (llama.cpp, vLLM, LM Studio, LocalAI, Ollama's ``/v1``
# shim...). The transport differs per instance: the ``openai`` API type
# goes through the openai SDK with the instance's base URL, the ``ollama``
# API type through Ollama's native ``/api/chat`` (chat/llm/ollama_provider.py)
# so the per-request context window can be set. ``backend`` tells the two
# apart on analytics rows.
INSTANCE_KINDS: dict[str, dict] = {
    "openrouter": {
        "label": "OpenRouter",
        "hint": "sk-or-v1-...",
        "provider": "openrouter",
        "backend": "openrouter",
        "key_required": True,
        "endpoint": False,
    },
    "local": {
        "label": "Self-hosted",
        "hint": "optional — only if the server checks one",
        "provider": "openrouter",
        "backend": "local",
        "key_required": False,
        "endpoint": True,
    },
}

# API types a self-hosted (``endpoint``) instance can speak. ``openai`` is
# the OpenAI chat-completions protocol every local server implements
# (llama.cpp ``llama-server``, vLLM, LM Studio, LocalAI, SGLang, TGI,
# text-generation-webui, Ollama's compatibility layer): requests go to
# ``<base_url>/v1/chat/completions`` and the model list comes from
# ``<base_url>/v1/models``. ``ollama`` is Ollama's native API
# (``/api/chat``, ``/api/tags``, ``/api/show``), preferred over its OpenAI
# shim because only the native API accepts a per-request ``num_ctx`` --
# Ollama otherwise runs every model at a small default context window
# (4096 tokens without a GPU) and silently truncates the prompt.
LOCAL_API_TYPES: dict[str, dict] = {
    "openai": {
        "label": "OpenAI-compatible",
        "description": "llama.cpp, vLLM, LM Studio, LocalAI, SGLang… (/v1/chat/completions)",
    },
    "ollama": {
        "label": "Ollama",
        "description": "Ollama's native API (/api/chat) — sets the context window per request",
    },
}
DEFAULT_LOCAL_API_TYPE = "openai"

# Context window requested from Ollama (``options.num_ctx``) for a model
# whose training context is unknown or larger than this. Ollama sizes the
# KV cache from it on every load, so it is deliberately modest; the admin
# can raise it per model (``context_length`` on the model entry).
DEFAULT_OLLAMA_CONTEXT_LENGTH = 32_768

# Self-hosted models are free per token (the hardware is already paid
# for), so their pricing snapshot is an explicit zero rather than "unknown"
# -- a null would make every cost report that includes them show no total.
LOCAL_MODEL_PRICING: dict = {"prompt": 0.0, "completion": 0.0}


def is_endpoint_kind(kind: str) -> bool:
    """True for kinds carrying their own base URL (self-hosted servers)."""
    return bool(INSTANCE_KINDS.get(kind, {}).get("endpoint"))


def normalize_base_url(value) -> str | None:
    """Normalize an admin-entered server URL; None when unusable.

    Accepts ``http://`` / ``https://`` origins with an optional path prefix
    (a reverse proxy may mount the server under one); a bare ``host:port``
    gets ``http://``. Trailing slashes and a trailing ``/v1`` are dropped
    so the API-type transports can append their own paths.
    """
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    scheme, _, rest = url.partition("://")
    if scheme.lower() not in ("http", "https") or not rest or rest.startswith("/"):
        return None
    url = f"{scheme.lower()}://{rest}".rstrip("/")
    if url.lower().endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url or None

# Instance ids double as credential file stems and as the left part of
# qualified model ids, so they are restricted to a URL/file-safe slug with
# no ``:`` (the qualified-id separator) and no ``/`` (present in every
# OpenRouter wire id, which is what makes bare legacy ids recognizable).
INSTANCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# The single pre-instances OpenRouter configuration: credential file
# ``openrouter.json`` and, since the data migration, the ``openrouter:``
# prefix on every model id that used to be stored bare.
LEGACY_OPENROUTER_INSTANCE_ID = "openrouter"

# Models the legacy configuration served (the former ``backend:
# "openrouter"`` MODEL_REGISTRY entries), seeded into the synthesized
# ``openrouter`` instance so an upgraded install keeps its picker entries.
# Limits/pricing mirror the old registry + price-table values.
_LEGACY_OPENROUTER_MODELS: list[dict] = [
    {
        "id": "deepseek/deepseek-v4-flash-0731",
        "enabled": True,
        "name": "DeepSeek V4 Flash 0731",
        "context_length": 1_310_720,
        "max_completion_tokens": 64_000,
        "pricing": {"prompt": 0.08, "completion": 0.18, "cache_read": 0.016},
    },
    {
        "id": "qwen/qwen3.8-27b",
        "enabled": True,
        "name": "Qwen3.8 27B",
        "context_length": 1_000_000,
        "max_completion_tokens": 64_000,
        "pricing": {"prompt": 0.45, "completion": 3.20, "cache_read": 0.05},
    },
]

# Limits assumed for an instance model whose catalog metadata is unknown
# (custom ids the OpenRouter catalog does not list). Conservative on
# purpose: the context figure only drives UI meters and sub-agent turn
# limits, and the output cap is what the API request asks for.
DEFAULT_INSTANCE_CONTEXT_LENGTH = 128_000
DEFAULT_INSTANCE_MAX_OUTPUT_TOKENS = 8_192

# ``max_completion_tokens`` snapshotted from the catalog is capped here in
# line with the registry's Vertex entries (the request asks for this many).
MAX_INSTANCE_OUTPUT_TOKENS = 64_000


# ---------------------------------------------------------------------------
# Model id helpers (pure string functions -- never touch disk)
# ---------------------------------------------------------------------------


def is_valid_instance_id(instance_id) -> bool:
    return isinstance(instance_id, str) and bool(INSTANCE_ID_RE.match(instance_id))


def qualify_model_id(instance_id: str, wire_id: str) -> str:
    """Build the qualified id an instance-served model is stored under."""
    return f"{instance_id}:{wire_id}"


def split_model_id(model_id: str) -> tuple[str | None, str]:
    """Split a stored model id into ``(instance_id, wire_id)``.

    ``instance_id`` is None for Vertex ids and for bare legacy OpenRouter ids
    (``deepseek/...``): only a left part that is a valid instance id (no
    ``/``) counts as a qualifier, so a wire id's own ``:variant`` suffix can
    never be mistaken for one. Never raises.
    """
    if not isinstance(model_id, str) or ":" not in model_id:
        return None, model_id
    head, _, tail = model_id.partition(":")
    if tail and is_valid_instance_id(head):
        return head, tail
    return None, model_id


def is_bare_openrouter_id(model_id: str) -> bool:
    """True for a pre-instances OpenRouter id stored without a qualifier."""
    return (
        isinstance(model_id, str)
        and "/" in model_id
        and split_model_id(model_id)[0] is None
    )


def canonical_model_id(model_id: str) -> str:
    """Map a bare legacy OpenRouter id to its qualified form; identity otherwise."""
    if is_bare_openrouter_id(model_id):
        return qualify_model_id(LEGACY_OPENROUTER_INSTANCE_ID, model_id)
    return model_id


# ---------------------------------------------------------------------------
# Per-instance credential store
# ---------------------------------------------------------------------------


def inference_credentials_path(instance_id: str) -> Path:
    """Return the credential file path for an instance id (format-checked)."""
    if not is_valid_instance_id(instance_id):
        raise ValueError(f"Invalid inference provider instance id: {instance_id!r}")
    return INFERENCE_CREDENTIALS_DIR / f"{instance_id}.json"


def _file_aad(instance_id: str) -> str:
    return f"inference_credentials/{instance_id}"


def read_inference_credentials(instance_id: str) -> dict | None:
    """Read one instance's credentials from the store.

    Returns None when the file is missing, unreadable, or not a non-empty
    JSON object -- callers treat None as "not configured".
    """
    path = inference_credentials_path(instance_id)
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable inference credential file: %s", path)
        return None
    if not isinstance(data, dict) or not data:
        logger.warning("Ignoring malformed inference credential file: %s", path)
        return None
    data = _unwrap_encrypted_file(data, _file_aad(instance_id), path)
    if not isinstance(data, dict) or not data:
        logger.warning("Ignoring malformed inference credential file: %s", path)
        return None
    return data


def write_inference_credentials(instance_id: str, credentials: dict) -> None:
    """Atomically write one instance's credentials, encrypted, with
    restrictive permissions.

    Same temp-file + ``os.replace`` scheme as the service-credential store so
    a crash mid-write can never leave a truncated credential file behind.
    """
    path = inference_credentials_path(instance_id)
    INFERENCE_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_path = tempfile.mkstemp(
        dir=INFERENCE_CREDENTIALS_DIR, prefix=f".{instance_id}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(_wrap_encrypted_file(credentials, _file_aad(instance_id)), f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def delete_inference_credentials(instance_id: str) -> bool:
    """Remove an instance's credential file; True when one was removed."""
    path = inference_credentials_path(instance_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def list_credential_instance_ids() -> list[str]:
    """Instance ids that have a credential file in the store (sorted)."""
    if not INFERENCE_CREDENTIALS_DIR.is_dir():
        return []
    ids = []
    for path in INFERENCE_CREDENTIALS_DIR.iterdir():
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        if is_valid_instance_id(path.stem):
            ids.append(path.stem)
    return sorted(ids)


def encrypt_plaintext_inference_files() -> list[str]:
    """Encrypt any credential file still holding plaintext JSON; returns the
    instance ids converted. Idempotent."""
    converted: list[str] = []
    for instance_id in list_credential_instance_ids():
        path = inference_credentials_path(instance_id)
        raw = _read_json_object(path)
        if raw is None or _is_encrypted_file(raw):
            continue
        write_inference_credentials(instance_id, raw)
        converted.append(instance_id)
        logger.info("Encrypted plaintext inference credential file %s", path)
    return converted


def effective_api_key(instance_id: str) -> tuple[str | None, str | None]:
    """Return ``(api_key, source)`` for an instance.

    ``source`` is ``"store"`` or None when the instance has no usable key.
    (Kept as a tuple for callers written against the former store-or-legacy
    precedence; there is no legacy location for instance keys.)
    """
    stored = read_inference_credentials(instance_id)
    if stored and stored.get("api_key"):
        return stored["api_key"], "store"
    return None, None


# ---------------------------------------------------------------------------
# Provider configuration store (inference_providers.json)
# ---------------------------------------------------------------------------


def _as_positive_int(value, default: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value <= 0:
        return default
    return int(value)


def _normalize_pricing(value) -> dict | None:
    """Normalize a snapshot ``{prompt, completion, cache_read?}`` ($/1M tokens)."""
    if not isinstance(value, dict):
        return None
    out: dict = {}
    for key in ("prompt", "completion", "cache_read"):
        rate = value.get(key)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            continue
        out[key] = float(rate)
    if "prompt" not in out or "completion" not in out:
        return None
    return out


def normalize_instance_model(entry) -> dict | None:
    """Normalize one instance model entry; None when it has no usable id.

    Accepts a bare wire-id string (enabled, no metadata) or the full dict
    shape written by the admin endpoints. Unknown keys are dropped.
    """
    if isinstance(entry, str):
        entry = {"id": entry}
    if not isinstance(entry, dict):
        return None
    wire_id = entry.get("id")
    if not isinstance(wire_id, str) or not wire_id.strip():
        return None
    wire_id = wire_id.strip()
    name = entry.get("name")
    return {
        "id": wire_id,
        "enabled": entry.get("enabled", True) is not False,
        "name": name.strip() if isinstance(name, str) and name.strip() else wire_id,
        "context_length": _as_positive_int(entry.get("context_length"), None),
        "max_completion_tokens": _as_positive_int(entry.get("max_completion_tokens"), None),
        "pricing": _normalize_pricing(entry.get("pricing")),
    }


def _normalize_instance(entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    instance_id = entry.get("id")
    kind = entry.get("kind")
    if not is_valid_instance_id(instance_id) or kind not in INSTANCE_KINDS:
        return None
    label = entry.get("label")
    models: list[dict] = []
    seen: set[str] = set()
    for raw in entry.get("models") or []:
        model = normalize_instance_model(raw)
        if model is None or model["id"] in seen:
            continue
        seen.add(model["id"])
        models.append(model)
    normalized = {
        "id": instance_id,
        "kind": kind,
        "label": (
            label.strip() if isinstance(label, str) and label.strip()
            else INSTANCE_KINDS[kind]["label"]
        ),
        "models": models,
    }
    if is_endpoint_kind(kind):
        api_type = entry.get("api_type")
        normalized["base_url"] = normalize_base_url(entry.get("base_url"))
        normalized["api_type"] = (
            api_type if api_type in LOCAL_API_TYPES else DEFAULT_LOCAL_API_TYPE
        )
    return normalized


def instance_configured(instance: dict) -> bool:
    """Whether an instance can serve requests at all: a stored API key for
    key-required kinds, a base URL for self-hosted endpoints (their key is
    optional). A config-presence check, not a live probe."""
    kind = INSTANCE_KINDS[instance["kind"]]
    if kind["endpoint"]:
        return bool(instance.get("base_url"))
    return bool(effective_api_key(instance["id"])[0])


def _empty_config() -> dict:
    return {"version": 1, "vertex": {"disabled_models": []}, "instances": []}


def _read_config_file() -> dict | None:
    """The normalized on-disk config, or None when the file is absent/malformed."""
    if not INFERENCE_PROVIDERS_FILE.exists():
        return None
    try:
        with open(INFERENCE_PROVIDERS_FILE, "r") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable inference providers file: %s", INFERENCE_PROVIDERS_FILE)
        return None
    if not isinstance(loaded, dict):
        logger.warning("Ignoring malformed inference providers file: %s", INFERENCE_PROVIDERS_FILE)
        return None
    config = _empty_config()
    vertex = loaded.get("vertex")
    if isinstance(vertex, dict) and isinstance(vertex.get("disabled_models"), list):
        config["vertex"]["disabled_models"] = sorted({
            m for m in vertex["disabled_models"] if isinstance(m, str) and m
        })
    seen: set[str] = set()
    for raw in loaded.get("instances") or []:
        instance = _normalize_instance(raw)
        if instance is None or instance["id"] in seen:
            continue
        seen.add(instance["id"])
        config["instances"].append(instance)
    return config


def _synthesize_missing_instances(config: dict) -> bool:
    """Add an ``openrouter``-kind instance for every credential file without
    a config entry (legacy single-key layout, dev-config pre-baking).

    The legacy ``openrouter`` instance is seeded with the models the old
    fixed registry served; other ids start with an empty model list for the
    admin to fill. Returns True when anything was added.
    """
    known = {inst["id"] for inst in config["instances"]}
    added = False
    for instance_id in list_credential_instance_ids():
        if instance_id in known:
            continue
        legacy = instance_id == LEGACY_OPENROUTER_INSTANCE_ID
        config["instances"].append({
            "id": instance_id,
            "kind": "openrouter",
            "label": (
                INSTANCE_KINDS["openrouter"]["label"] if legacy
                else f"{INSTANCE_KINDS['openrouter']['label']} ({instance_id})"
            ),
            "models": copy.deepcopy(_LEGACY_OPENROUTER_MODELS) if legacy else [],
        })
        logger.info(
            "Synthesized inference provider instance %r from its credential file",
            instance_id,
        )
        added = True
    return added


def load_inference_config() -> dict:
    """Return the normalized provider configuration (re-read on every call).

    ``{"version": 1, "vertex": {"disabled_models": [...]}, "instances":
    [{"id", "kind", "label", "models": [{"id", "enabled", "name",
    "context_length", "max_completion_tokens", "pricing"}]}]}``. A missing or
    malformed file means "nothing disabled, no instances" -- except that a
    credential file with no instance entry always gets one synthesized (and
    persisted) so a pre-instances install keeps its OpenRouter models.
    """
    config = _read_config_file()
    persist = False
    if config is None:
        config = _empty_config()
    if _synthesize_missing_instances(config):
        persist = True
    if persist:
        try:
            save_inference_config(config)
        except OSError:
            logger.warning(
                "Failed to persist synthesized inference providers config",
                exc_info=True,
            )
    return config


def save_inference_config(config: dict) -> None:
    """Persist the full provider configuration atomically (normalizing)."""
    normalized = _empty_config()
    vertex = config.get("vertex") or {}
    normalized["vertex"]["disabled_models"] = sorted({
        m for m in (vertex.get("disabled_models") or []) if isinstance(m, str) and m
    })
    seen: set[str] = set()
    for raw in config.get("instances") or []:
        instance = _normalize_instance(raw)
        if instance is None or instance["id"] in seen:
            continue
        seen.add(instance["id"])
        normalized["instances"].append(instance)
    INFERENCE_PROVIDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=INFERENCE_PROVIDERS_FILE.parent, prefix=".inference_providers.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(normalized, f, indent=2)
        os.replace(tmp_path, INFERENCE_PROVIDERS_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def list_instances() -> list[dict]:
    return load_inference_config()["instances"]


def get_instance(instance_id: str) -> dict | None:
    for instance in list_instances():
        if instance["id"] == instance_id:
            return instance
    return None


def vertex_disabled_models() -> set[str]:
    return set(load_inference_config()["vertex"]["disabled_models"])


def set_vertex_disabled_models(model_ids) -> list[str]:
    """Replace the Vertex disabled set; returns the stored (sorted) list."""
    config = load_inference_config()
    config["vertex"]["disabled_models"] = sorted({str(m) for m in model_ids if m})
    save_inference_config(config)
    return config["vertex"]["disabled_models"]


def new_instance_id(kind: str) -> str:
    """First free id of the form ``<kind>``, ``<kind>-2``, ``<kind>-3``, ...

    Both the config entries and orphaned credential files count as taken so
    a new instance can never silently adopt a stale key file.
    """
    if kind not in INSTANCE_KINDS:
        raise ValueError(f"Unknown inference provider kind: {kind!r}")
    taken = {inst["id"] for inst in list_instances()} | set(list_credential_instance_ids())
    candidate = kind
    n = 1
    while candidate in taken:
        n += 1
        candidate = f"{kind}-{n}"
    return candidate


def upsert_instance(instance: dict) -> dict:
    """Insert or replace one instance entry (by id); returns the stored entry."""
    normalized = _normalize_instance(instance)
    if normalized is None:
        raise ValueError("Invalid inference provider instance")
    config = load_inference_config()
    for index, existing in enumerate(config["instances"]):
        if existing["id"] == normalized["id"]:
            config["instances"][index] = normalized
            break
    else:
        config["instances"].append(normalized)
    save_inference_config(config)
    return normalized


def delete_instance(instance_id: str) -> bool:
    """Remove an instance entry AND its credential file; True when it existed.

    The credential file must go too: :func:`load_inference_config` would
    otherwise resurrect the instance from the orphaned file.
    """
    config = load_inference_config()
    remaining = [inst for inst in config["instances"] if inst["id"] != instance_id]
    existed = len(remaining) != len(config["instances"])
    if existed:
        config["instances"] = remaining
        save_inference_config(config)
    removed_file = delete_inference_credentials(instance_id)
    return existed or removed_file


def instance_model_pricing(wire_id: str) -> dict | None:
    """Pricing snapshot ($/1M tokens) for an instance-served wire id, if any
    configured instance lists it with pricing. Used by db/llm_pricing.py."""
    for instance in list_instances():
        for model in instance["models"]:
            if model["id"] == wire_id and model["pricing"]:
                return model["pricing"]
    return None


# ---------------------------------------------------------------------------
# Vertex AI environment detection (display-only)
# ---------------------------------------------------------------------------


def _gcloud_adc_path() -> Path:
    """Well-known gcloud Application Default Credentials file location."""
    config_dir = os.environ.get("CLOUDSDK_CONFIG")
    base = Path(config_dir) if config_dir else Path.home() / ".config" / "gcloud"
    return base / "application_default_credentials.json"


def _detect_google_credentials() -> dict:
    """Describe where Google credentials for Vertex would come from.

    Checks ``GOOGLE_APPLICATION_CREDENTIALS`` first (a service-account key
    file; this is how both run.py and the prod bootstrap wire Vertex auth),
    then the gcloud ADC well-known file. Metadata-server credentials (GCE)
    are intentionally not probed -- that would add network latency to a
    settings request -- so ``source`` may be None on a GCE deployment that
    relies on the instance service account.
    """
    info: dict = {
        "source": None,
        "key_path": None,
        "service_account_email": None,
        "project_id": None,
        "problem": None,
    }
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    candidates = (
        [("env", Path(env_path))] if env_path else [("gcloud_adc", _gcloud_adc_path())]
    )
    for source, path in candidates:
        if source != "env" and not path.exists():
            continue
        info["source"] = source
        info["key_path"] = str(path)
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            info["problem"] = "The credentials file could not be read."
            return info
        info["service_account_email"] = data.get("client_email") or None
        # Service-account keys carry project_id; gcloud user ADC carries at
        # most a quota_project_id.
        info["project_id"] = (
            data.get("project_id") or data.get("quota_project_id") or None
        )
        return info
    return info


def _vertex_section_status(config: dict, raw_config: dict, section: str) -> dict:
    """Project one Vertex config section (anthropic / gemini_vertex) with sources.

    ``config`` is the merged ``load_server_config()`` result (env overrides and
    the gemini_vertex->anthropic project fallback already applied); ``raw_config``
    is the unmerged server_config.json content, used to tell which layer a
    value actually came from.
    """
    env_var = {
        "anthropic": "ANTHROPIC_VERTEX_PROJECT_ID",
        "gemini_vertex": "GEMINI_VERTEX_PROJECT_ID",
    }[section]
    project_id = config[section]["vertex_project_id"]
    raw_project = (raw_config.get(section) or {}).get("vertex_project_id", "")
    if not project_id:
        source = None
    elif os.environ.get(env_var):
        source = "env"
    elif raw_project:
        source = "server_config"
    else:
        # Only reachable for gemini_vertex, via the anthropic project fallback
        # in load_server_config().
        source = "anthropic_fallback"
    return {
        "configured": bool(project_id),
        "vertex_project_id": project_id,
        "vertex_region": config[section]["vertex_region"],
        "project_source": source,
    }


def vertex_environment_status() -> dict:
    """Snapshot of the detected Vertex AI environment for the admin panel.

    Everything here is read-only display data: which credentials were found
    in the environment and which project/region each Vertex-backed model
    family (Anthropic, Gemini) would use, with the layer each value came from.
    """
    from config.server_config import SERVER_CONFIG_FILE, load_server_config

    config = load_server_config()
    raw_config = _read_json_object(SERVER_CONFIG_FILE) or {}
    anthropic = _vertex_section_status(config, raw_config, "anthropic")
    gemini_vertex = _vertex_section_status(config, raw_config, "gemini_vertex")
    return {
        "credentials": _detect_google_credentials(),
        "anthropic": anthropic,
        "gemini_vertex": gemini_vertex,
        "configured": anthropic["configured"] or gemini_vertex["configured"],
    }
