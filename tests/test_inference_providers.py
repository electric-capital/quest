"""Tests for inference-provider configuration.

Covers ``config/inference_providers.py`` (qualified model ids, the
per-instance API-key store with restrictive permissions, the provider
configuration store with its legacy-layout bootstrap, Vertex environment
detection), model resolution in ``chat/llm/config.py`` (``resolve_model``,
``get_configured_models`` over the Vertex disabled set + instance keys, the
model-health filter in ``get_available_models``), OpenRouter pricing
snapshots in ``db/llm_pricing.py``, and the admin inference-provider
endpoints in ``chat/routes/admin.py`` (masked reads, keep-key-on-empty
writes, instance CRUD, catalog typeahead, admin gating). Everything runs
against tmp_path -- no real data directory or network involved (the
autouse fixture in tests/conftest.py points the stores there).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest
from fastapi import HTTPException

import config.inference_providers as ip
import config.server_config as scfg


SA_KEY = {
    "type": "service_account",
    "project_id": "sa-project",
    "client_email": "vertex-runner@sa-project.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
}

DEEPSEEK = "deepseek/deepseek-v4-flash-0731"
QWEN = "qwen/qwen3.8-27b"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the config file, env and gcloud lookups at tmp_path.

    (The credential store and provider config file are already redirected
    by the autouse conftest fixture.)
    """
    monkeypatch.setattr(scfg, "SERVER_CONFIG_FILE", tmp_path / "server_config.json")
    for var in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "ANTHROPIC_VERTEX_REGION",
        "GEMINI_VERTEX_PROJECT_ID",
        "GEMINI_VERTEX_REGION",
    ):
        monkeypatch.delenv(var, raising=False)
    # Keep the dev machine's real gcloud ADC out of detection results.
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))
    return ip.INFERENCE_CREDENTIALS_DIR, ip.INFERENCE_PROVIDERS_FILE


def _write_vertex_config(gemini_project: str = "", anthropic_project: str = ""):
    """Write a server_config.json with the given Vertex project ids."""
    config = {}
    if gemini_project:
        config["gemini_vertex"] = {"vertex_project_id": gemini_project}
    if anthropic_project:
        config["anthropic"] = {"vertex_project_id": anthropic_project}
    scfg.SERVER_CONFIG_FILE.write_text(json.dumps(config))


def _run(coro):
    return asyncio.run(coro)


def _instance(instance_id="openrouter-2", label="Work", models=()):
    return {
        "id": instance_id,
        "kind": "openrouter",
        "label": label,
        "models": list(models),
    }


# ---------------------------------------------------------------------------
# Qualified model ids
# ---------------------------------------------------------------------------

def test_split_model_id_shapes():
    assert ip.split_model_id("claude-opus-4-8") == (None, "claude-opus-4-8")
    assert ip.split_model_id(f"openrouter:{DEEPSEEK}") == ("openrouter", DEEPSEEK)
    # OpenRouter's own ":variant" suffix is not a qualifier
    assert ip.split_model_id("vendor/model:free") == (None, "vendor/model:free")
    assert ip.split_model_id("work-2:vendor/model:free") == ("work-2", "vendor/model:free")
    # The left part must be a valid instance id
    assert ip.split_model_id("Not Valid:vendor/model") == (None, "Not Valid:vendor/model")
    assert ip.split_model_id("") == (None, "")
    assert ip.split_model_id(None) == (None, None)


def test_bare_and_canonical_ids():
    assert ip.is_bare_openrouter_id(DEEPSEEK)
    assert not ip.is_bare_openrouter_id(f"openrouter:{DEEPSEEK}")
    assert not ip.is_bare_openrouter_id("claude-opus-4-8")
    assert ip.canonical_model_id(DEEPSEEK) == f"openrouter:{DEEPSEEK}"
    assert ip.canonical_model_id("claude-opus-4-8") == "claude-opus-4-8"
    assert ip.qualify_model_id("x", "a/b") == "x:a/b"


def test_instance_id_format():
    assert ip.is_valid_instance_id("openrouter")
    assert ip.is_valid_instance_id("openrouter-2")
    assert not ip.is_valid_instance_id("Open")
    assert not ip.is_valid_instance_id("a:b")
    assert not ip.is_valid_instance_id("a/b")
    assert not ip.is_valid_instance_id("-x")
    assert not ip.is_valid_instance_id(7)


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------

def test_write_then_read_round_trip(store):
    ip.write_inference_credentials("openrouter-2", {"api_key": "fk-new"})
    assert ip.read_inference_credentials("openrouter-2") == {"api_key": "fk-new"}
    assert ip.effective_api_key("openrouter-2") == ("fk-new", "store")


def test_write_sets_restrictive_permissions(store):
    store_dir, _ = store
    ip.write_inference_credentials("openrouter", {"api_key": "k"})
    file_mode = stat.S_IMODE(os.stat(store_dir / "openrouter.json").st_mode)
    dir_mode = stat.S_IMODE(os.stat(store_dir).st_mode)
    assert file_mode == 0o600
    assert dir_mode == 0o700


def test_write_leaves_no_temp_files(store):
    store_dir, _ = store
    ip.write_inference_credentials("openrouter", {"api_key": "k"})
    assert sorted(p.name for p in store_dir.iterdir()) == ["openrouter.json"]


def test_read_missing_and_malformed(store):
    store_dir, _ = store
    assert ip.read_inference_credentials("openrouter") is None
    assert ip.effective_api_key("openrouter") == (None, None)
    store_dir.mkdir(parents=True)
    (store_dir / "openrouter.json").write_text("{not json")
    assert ip.read_inference_credentials("openrouter") is None


def test_invalid_instance_id_rejected(store):
    for bad in ("nope:x", "A", "../x"):
        with pytest.raises(ValueError):
            ip.read_inference_credentials(bad)
        with pytest.raises(ValueError):
            ip.write_inference_credentials(bad, {"api_key": "k"})


def test_list_credential_instance_ids_and_delete(store):
    store_dir, _ = store
    ip.write_inference_credentials("openrouter", {"api_key": "a"})
    ip.write_inference_credentials("openrouter-2", {"api_key": "b"})
    store_dir.mkdir(exist_ok=True)
    (store_dir / "Bad Name.json").write_text("{}")
    (store_dir / ".hidden.json").write_text("{}")
    assert ip.list_credential_instance_ids() == ["openrouter", "openrouter-2"]
    assert ip.delete_inference_credentials("openrouter") is True
    assert ip.delete_inference_credentials("openrouter") is False
    assert ip.list_credential_instance_ids() == ["openrouter-2"]


# ---------------------------------------------------------------------------
# Provider configuration store
# ---------------------------------------------------------------------------

def test_config_empty_by_default(store):
    _, config_file = store
    assert ip.load_inference_config() == {
        "version": 1, "vertex": {"disabled_models": []}, "instances": [],
    }
    # Nothing to synthesize -> nothing persisted
    assert not config_file.exists()


def test_legacy_openrouter_credential_bootstraps_seeded_instance(store):
    """A pre-instances install (just openrouter.json) becomes the
    ``openrouter`` instance with the historical curated models, persisted."""
    _, config_file = store
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or"})
    config = ip.load_inference_config()
    assert [i["id"] for i in config["instances"]] == ["openrouter"]
    inst = config["instances"][0]
    assert inst["kind"] == "openrouter"
    assert inst["label"] == "OpenRouter"
    assert [m["id"] for m in inst["models"]] == [DEEPSEEK, QWEN]
    assert all(m["enabled"] for m in inst["models"])
    assert inst["models"][0]["pricing"]["prompt"] == 0.08
    assert config_file.exists()
    # Idempotent: a second load reads the persisted file, no duplicates
    assert len(ip.load_inference_config()["instances"]) == 1


def test_other_credential_file_bootstraps_empty_instance(store):
    ip.write_inference_credentials("openrouter-work", {"api_key": "sk-or"})
    config = ip.load_inference_config()
    assert config["instances"] == [{
        "id": "openrouter-work",
        "kind": "openrouter",
        "label": "OpenRouter (openrouter-work)",
        "models": [],
    }]


def test_config_malformed_file_treated_as_empty(store):
    _, config_file = store
    config_file.write_text("{not json")
    assert ip.load_inference_config()["instances"] == []
    config_file.write_text(json.dumps([1, 2]))
    assert ip.load_inference_config()["instances"] == []


def test_save_normalizes_entries(store):
    ip.save_inference_config({
        "vertex": {"disabled_models": ["b", "a", "a", 7, ""]},
        "instances": [
            {"id": "openrouter", "kind": "openrouter", "label": "  ", "models": [
                DEEPSEEK,  # bare string form
                {"id": " x/y ", "enabled": False, "name": "", "context_length": -1,
                 "pricing": {"prompt": 1, "completion": "bad"}},
                {"id": DEEPSEEK},  # duplicate dropped
                {"nope": True},
            ]},
            {"id": "BAD", "kind": "openrouter"},
            {"id": "openrouter", "kind": "openrouter"},  # duplicate id dropped
            {"id": "other", "kind": "unknown-kind"},
        ],
    })
    config = ip.load_inference_config()
    assert config["vertex"]["disabled_models"] == ["a", "b"]
    assert len(config["instances"]) == 1
    inst = config["instances"][0]
    assert inst["label"] == "OpenRouter"
    assert inst["models"] == [
        {"id": DEEPSEEK, "enabled": True, "name": DEEPSEEK, "context_length": None,
         "max_completion_tokens": None, "pricing": None},
        {"id": "x/y", "enabled": False, "name": "x/y", "context_length": None,
         "max_completion_tokens": None, "pricing": None},
    ]


def test_vertex_disabled_models_round_trip(store):
    assert ip.vertex_disabled_models() == set()
    assert ip.set_vertex_disabled_models(["claude-opus-4-6", "gemini-3.8-flash"]) == [
        "claude-opus-4-6", "gemini-3.8-flash",
    ]
    assert ip.vertex_disabled_models() == {"claude-opus-4-6", "gemini-3.8-flash"}


def test_new_instance_id_skips_config_entries_and_orphan_files(store):
    assert ip.new_instance_id("openrouter") == "openrouter"
    ip.upsert_instance(_instance("openrouter", label="Personal"))
    assert ip.new_instance_id("openrouter") == "openrouter-2"
    # An orphaned credential file also counts as taken
    ip.write_inference_credentials("openrouter-2", {"api_key": "stale"})
    ip.load_inference_config()  # synthesizes openrouter-2 from the file
    assert ip.new_instance_id("openrouter") == "openrouter-3"
    with pytest.raises(ValueError):
        ip.new_instance_id("nope")


def test_upsert_and_delete_instance(store):
    ip.upsert_instance(_instance("openrouter-2", models=[{"id": DEEPSEEK}]))
    ip.write_inference_credentials("openrouter-2", {"api_key": "k"})
    ip.upsert_instance(_instance("openrouter-2", label="Renamed", models=[{"id": QWEN}]))
    inst = ip.get_instance("openrouter-2")
    assert inst["label"] == "Renamed"
    assert [m["id"] for m in inst["models"]] == [QWEN]
    with pytest.raises(ValueError):
        ip.upsert_instance({"id": "bad id", "kind": "openrouter"})

    assert ip.delete_instance("openrouter-2") is True
    assert ip.get_instance("openrouter-2") is None
    # The credential file went too -- otherwise the next load would
    # resurrect the instance from it.
    assert ip.read_inference_credentials("openrouter-2") is None
    assert ip.list_instances() == []
    assert ip.delete_instance("openrouter-2") is False


def test_instance_model_pricing_lookup(store):
    ip.upsert_instance(_instance("openrouter", models=[
        {"id": DEEPSEEK, "pricing": {"prompt": 0.1, "completion": 0.2, "cache_read": 0.01}},
        {"id": QWEN},
    ]))
    assert ip.instance_model_pricing(DEEPSEEK) == {
        "prompt": 0.1, "completion": 0.2, "cache_read": 0.01,
    }
    assert ip.instance_model_pricing(QWEN) is None
    assert ip.instance_model_pricing("unknown/x") is None


# ---------------------------------------------------------------------------
# Model resolution (chat/llm/config.py)
# ---------------------------------------------------------------------------

def test_registry_holds_vertex_models_only():
    from chat.llm.config import MODEL_REGISTRY

    assert all(e["provider"] in ("gemini", "anthropic") for e in MODEL_REGISTRY.values())
    assert all("/" not in model_id for model_id in MODEL_REGISTRY)


def test_resolve_vertex_model(store):
    from chat.llm.config import resolve_model

    spec = resolve_model("claude-opus-4-8")
    assert spec.provider == "anthropic"
    assert spec.backend == "vertex"
    assert spec.instance_id is None
    assert spec.family == "anthropic"
    assert spec.wire_id == "claude-opus-4-8"
    assert spec.enabled and spec.listed and not spec.deprecated
    assert spec.provider_label == "Claude on Vertex"
    assert resolve_model("gemini-3.1-flash-lite-preview").wire_id == "gemini-3.1-flash-lite"
    assert resolve_model("gemini-3.8-flash").family == "gemini_vertex"
    assert resolve_model("gemini-3.1-pro-preview").deprecated is True
    ip.set_vertex_disabled_models(["claude-opus-4-8"])
    assert resolve_model("claude-opus-4-8").enabled is False


def test_resolve_instance_model(store):
    from chat.llm.config import get_backend_for_model, get_provider_for_model, resolve_model

    ip.upsert_instance(_instance("openrouter-2", label="Work", models=[
        {"id": DEEPSEEK, "name": "DeepSeek V4 Flash", "context_length": 1_310_720,
         "max_completion_tokens": 384_000},
        {"id": QWEN, "enabled": False},
    ]))
    spec = resolve_model(f"openrouter-2:{DEEPSEEK}")
    assert spec.id == f"openrouter-2:{DEEPSEEK}"
    assert spec.wire_id == DEEPSEEK
    assert spec.provider == "openrouter"
    assert spec.backend == "openrouter"
    assert spec.instance_id == "openrouter-2"
    assert spec.family is None
    assert spec.display_name == "DeepSeek V4 Flash"
    assert spec.provider_label == "Work"
    assert spec.max_input_tokens == 1_310_720
    assert spec.max_output_tokens == ip.MAX_INSTANCE_OUTPUT_TOKENS  # capped
    assert spec.enabled and spec.listed
    assert resolve_model(f"openrouter-2:{QWEN}").enabled is False
    assert get_provider_for_model(f"openrouter-2:{DEEPSEEK}") == "openrouter"
    assert get_backend_for_model(f"openrouter-2:{DEEPSEEK}") == "openrouter"

    # A model the instance no longer lists still resolves (old
    # conversations keep their provider) but is unlisted and disabled,
    # with the conservative default limits.
    gone = resolve_model("openrouter-2:vendor/retired")
    assert gone.provider == "openrouter"
    assert not gone.listed and not gone.enabled
    assert gone.display_name == "vendor/retired"
    assert gone.max_input_tokens == ip.DEFAULT_INSTANCE_CONTEXT_LENGTH
    assert gone.max_output_tokens == ip.DEFAULT_INSTANCE_MAX_OUTPUT_TOKENS


def test_resolve_bare_legacy_id_maps_to_legacy_instance(store):
    from chat.llm.config import resolve_model

    assert resolve_model(DEEPSEEK) is None  # no openrouter instance yet
    ip.upsert_instance(_instance("openrouter", models=[{"id": DEEPSEEK}]))
    spec = resolve_model(DEEPSEEK)
    assert spec.id == f"openrouter:{DEEPSEEK}"
    assert spec.instance_id == "openrouter"


def test_resolve_unknown(store):
    from chat.llm.config import get_backend_for_model, get_provider_for_model, resolve_model

    assert resolve_model("nope-1") is None
    assert resolve_model("missing-instance:vendor/x") is None
    assert resolve_model("") is None
    assert get_backend_for_model("nope-1") is None
    with pytest.raises(ValueError):
        get_provider_for_model("nope-1")


def test_helper_lookups(store):
    from chat.llm.config import (
        get_max_input_tokens,
        get_model_display_name,
        model_instance_id,
        public_model_catalog,
    )

    ip.upsert_instance(_instance("openrouter", label="OpenRouter", models=[{"id": DEEPSEEK, "name": "DS"}]))
    assert get_model_display_name("claude-opus-4-8") == "Claude Opus 4.8"
    assert get_model_display_name(f"openrouter:{DEEPSEEK}") == "DS"
    assert get_model_display_name("nope") == "nope"
    assert get_max_input_tokens("claude-opus-4-8") == 1_000_000
    assert get_max_input_tokens("nope") == 0
    assert model_instance_id("claude-opus-4-8") is None
    assert model_instance_id(f"openrouter:{DEEPSEEK}") == "openrouter"
    assert model_instance_id(DEEPSEEK) is None

    catalog = public_model_catalog()
    by_id = {m["id"]: m for m in catalog}
    assert by_id["gemini-3.1-pro-preview"]["deprecated"] is True
    assert by_id[f"openrouter:{DEEPSEEK}"] == {
        "id": f"openrouter:{DEEPSEEK}",
        "display_name": "DS",
        "provider": "openrouter",
        "provider_label": "OpenRouter",
        "max_input_tokens": ip.DEFAULT_INSTANCE_CONTEXT_LENGTH,
        "deprecated": False,
        # Admin Model Selection fields (config/model_selection.py), unset here
        "slot": None,
        "public_slot": None,
        "descriptor": "",
        "allow_private": True,
        "allow_public": True,
    }


def test_provider_instances_keyed_by_instance(store, monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(llm_config, "_provider_instances", {})
    default = llm_config.get_provider_instance("openrouter")
    legacy = llm_config.get_provider_instance("openrouter", "openrouter")
    other = llm_config.get_provider_instance("openrouter", "openrouter-2")
    assert default is legacy
    assert other is not legacy
    assert other.instance_id == "openrouter-2"
    llm_config.drop_provider_instance("openrouter", "openrouter-2")
    assert llm_config.get_provider_instance("openrouter", "openrouter-2") is not other
    with pytest.raises(ValueError):
        llm_config.get_provider_instance("nope")


# ---------------------------------------------------------------------------
# Configured / available models
# ---------------------------------------------------------------------------

def _vertex_ids(provider=None, include_deprecated=False):
    from chat.llm.config import MODEL_REGISTRY

    return [
        m for m, e in MODEL_REGISTRY.items()
        if (provider is None or e["provider"] == provider)
        and (include_deprecated or not e.get("deprecated"))
    ]


def test_gemini_models_require_vertex_project(store, health_store):
    from chat.llm.config import get_available_models

    gemini_models = _vertex_ids("gemini")
    assert gemini_models
    assert not any(m in get_available_models() for m in gemini_models)
    _write_vertex_config(gemini_project="gv-project")
    after = get_available_models()
    assert all(m in after for m in gemini_models)


def test_anthropic_project_covers_gemini_via_fallback(store, health_store):
    """gemini_vertex inherits the Anthropic project id, so one project id
    configures every Vertex model."""
    from chat.llm.config import get_configured_models

    _write_vertex_config(anthropic_project="one-project")
    assert get_configured_models() == _vertex_ids()


def test_disabled_vertex_models_are_not_configured(store, health_store):
    from chat.llm.config import get_configured_models

    _write_vertex_config(anthropic_project="one-project")
    ip.set_vertex_disabled_models(["claude-opus-4-6", "gemini-3.8-flash"])
    configured = get_configured_models()
    assert "claude-opus-4-6" not in configured
    assert "gemini-3.8-flash" not in configured
    assert configured == [
        m for m in _vertex_ids() if m not in ("claude-opus-4-6", "gemini-3.8-flash")
    ]


def test_instance_models_require_key_and_enabled_flag(store, health_store):
    from chat.llm.config import get_configured_models

    ip.upsert_instance(_instance("openrouter-2", models=[
        {"id": DEEPSEEK}, {"id": QWEN, "enabled": False},
    ]))
    assert get_configured_models() == []
    ip.write_inference_credentials("openrouter-2", {"api_key": "sk-or"})
    assert get_configured_models() == [f"openrouter-2:{DEEPSEEK}"]
    # Vertex ids come first, instance ids after, in admin order
    _write_vertex_config(anthropic_project="p")
    assert get_configured_models() == _vertex_ids() + [f"openrouter-2:{DEEPSEEK}"]


def test_legacy_openrouter_layout_keeps_models_configured(store, health_store):
    """Upgrade path: only openrouter.json exists -> the two historical
    models are configured under their qualified ids."""
    from chat.llm.config import get_configured_models

    ip.write_inference_credentials("openrouter", {"api_key": "sk-or"})
    assert get_configured_models() == [f"openrouter:{DEEPSEEK}", f"openrouter:{QWEN}"]


def test_available_models_health_filter(store, health_store):
    """A failing health verdict hides a model from the picker; a later
    passing check brings it back. Never-checked models stay visible."""
    from chat.llm.config import get_available_models, get_configured_models

    _write_vertex_config(gemini_project="gv-project")
    configured = get_configured_models()
    assert "gemini-3.5-flash-lite" in configured
    assert get_available_models() == configured

    _run(health_store.record("gemini-3.5-flash-lite", False, "quota exhausted"))
    available = get_available_models()
    assert "gemini-3.5-flash-lite" not in available
    assert available == [m for m in configured if m != "gemini-3.5-flash-lite"]

    _run(health_store.record("gemini-3.5-flash-lite", True, None))
    assert get_available_models() == configured


def test_available_models_malformed_health_entry_kept(store, health_store):
    from chat.llm.config import get_available_models

    _write_vertex_config(gemini_project="gv-project")
    health_store._statuses["gemini-3.5-flash-lite"] = {"weird": True}
    assert "gemini-3.5-flash-lite" in get_available_models()


# ---------------------------------------------------------------------------
# OpenRouter pricing snapshots (db/llm_pricing.py)
# ---------------------------------------------------------------------------

def test_openrouter_pricing_prefers_instance_snapshot(store):
    from db.llm_pricing import estimate_cost_usd

    metrics = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    # Static fallback for the historical model
    assert estimate_cost_usd("openrouter", DEEPSEEK, metrics) == pytest.approx(0.08)
    # Qualified ids strip the instance before lookup
    assert estimate_cost_usd("openrouter", f"openrouter:{DEEPSEEK}", metrics) == pytest.approx(0.08)
    # An instance snapshot wins over the static table
    ip.upsert_instance(_instance("openrouter-2", models=[
        {"id": DEEPSEEK, "pricing": {"prompt": 1.0, "completion": 2.0, "cache_read": 0.5}},
    ]))
    assert estimate_cost_usd("openrouter", f"openrouter-2:{DEEPSEEK}", metrics) == pytest.approx(1.0)
    assert estimate_cost_usd("openrouter", DEEPSEEK, metrics) == pytest.approx(1.0)
    cached = {"prompt_tokens": 1_000_000, "cached_prompt_tokens": 1_000_000}
    assert estimate_cost_usd("openrouter", f"openrouter-2:{DEEPSEEK}", cached) == pytest.approx(0.5)
    # Unknown everywhere -> no estimate
    assert estimate_cost_usd("openrouter", "openrouter-2:vendor/unknown", metrics) is None


# ---------------------------------------------------------------------------
# Vertex environment detection
# ---------------------------------------------------------------------------

def test_vertex_detection_nothing_configured(store):
    status = ip.vertex_environment_status()
    assert status["configured"] is False
    assert status["credentials"]["source"] is None
    assert status["anthropic"]["configured"] is False
    assert status["gemini_vertex"]["configured"] is False


def test_vertex_detection_env_service_account(store, tmp_path, monkeypatch):
    key_file = tmp_path / "sa.json"
    key_file.write_text(json.dumps(SA_KEY))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key_file))
    creds = ip.vertex_environment_status()["credentials"]
    assert creds["source"] == "env"
    assert creds["key_path"] == str(key_file)
    assert creds["service_account_email"] == SA_KEY["client_email"]
    assert creds["project_id"] == "sa-project"
    assert creds["problem"] is None


def test_vertex_detection_env_unreadable_key(store, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "missing.json")
    )
    creds = ip.vertex_environment_status()["credentials"]
    assert creds["source"] == "env"
    assert creds["problem"]


def test_vertex_detection_gcloud_adc(store, tmp_path):
    gcloud_dir = tmp_path / "gcloud"
    gcloud_dir.mkdir()
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"type": "authorized_user", "quota_project_id": "adc-project"})
    )
    creds = ip.vertex_environment_status()["credentials"]
    assert creds["source"] == "gcloud_adc"
    assert creds["project_id"] == "adc-project"


def test_vertex_sections_report_config_sources(store, tmp_path):
    scfg.SERVER_CONFIG_FILE.write_text(
        json.dumps({"anthropic": {"vertex_project_id": "cfg-project"}})
    )
    status = ip.vertex_environment_status()
    assert status["configured"] is True
    assert status["anthropic"]["vertex_project_id"] == "cfg-project"
    assert status["anthropic"]["project_source"] == "server_config"
    assert status["gemini_vertex"]["vertex_project_id"] == "cfg-project"
    assert status["gemini_vertex"]["project_source"] == "anthropic_fallback"
    assert status["gemini_vertex"]["vertex_region"] == "global"


def test_vertex_sections_env_override_source(store, monkeypatch):
    monkeypatch.setenv("GEMINI_VERTEX_PROJECT_ID", "env-project")
    status = ip.vertex_environment_status()
    assert status["gemini_vertex"]["vertex_project_id"] == "env-project"
    assert status["gemini_vertex"]["project_source"] == "env"
    assert status["anthropic"]["configured"] is False


# ---------------------------------------------------------------------------
# Admin endpoints (chat/routes/admin.py)
# ---------------------------------------------------------------------------

ADMIN_USER = {"id": 1, "email": "admin@example.com"}
NOBODY = {"id": 2, "email": "user@example.com"}


@pytest.fixture
def health_store(tmp_path, monkeypatch):
    """Swap the model-health store singleton for a tmp_path-backed one."""
    import chat.llm.health as health

    fresh = health.ModelHealthStore(tmp_path / "model_health.json")
    monkeypatch.setattr(health, "_store", fresh)
    return fresh


@pytest.fixture
def admin_routes(store, health_store, monkeypatch):
    import chat.routes.admin as admin
    monkeypatch.setattr(admin, "is_admin", lambda email: email == ADMIN_USER["email"])
    return admin


@pytest.fixture
def scheduled(monkeypatch):
    """Capture background recheck requests instead of running them."""
    import chat.llm.health as health

    calls: list[list[str]] = []
    monkeypatch.setattr(health, "schedule_model_rechecks", lambda ids: calls.append(list(ids)))
    return calls


@pytest.fixture
def fake_catalog(monkeypatch):
    """Serve a canned OpenRouter catalog instead of fetching."""
    import chat.llm.openrouter_catalog as catalog

    models = [
        {"id": DEEPSEEK, "name": "DeepSeek: V4 Flash", "context_length": 1_310_720,
         "max_completion_tokens": 384_000,
         "pricing": {"prompt": 0.15, "completion": 0.6, "cache_read": 0.003}},
        {"id": QWEN, "name": "Qwen: 3.8 27B", "context_length": 1_000_000,
         "max_completion_tokens": None, "pricing": {"prompt": 0.45, "completion": 3.2}},
        {"id": "meta/llama-9", "name": "Meta: Llama 9", "context_length": 256_000,
         "max_completion_tokens": 32_000, "pricing": None},
    ]
    monkeypatch.setattr(
        catalog, "get_catalog",
        lambda refresh=False: {"models": models, "fetched_at": 1.0, "stale": False, "error": None},
    )
    return models


def test_admin_endpoints_require_admin(admin_routes):
    admin = admin_routes
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_list_inference_providers(user=NOBODY))
    assert exc_info.value.status_code == 403
    for coro in (
        admin.admin_update_vertex_models(admin.VertexModelsUpdate(disabled_models=[]), user=NOBODY),
        admin.admin_create_inference_instance(admin.InstanceCreate(), user=NOBODY),
        admin.admin_update_inference_instance("openrouter", admin.InstanceUpdate(), user=NOBODY),
        admin.admin_delete_inference_instance("openrouter", user=NOBODY),
        admin.admin_openrouter_catalog(user=NOBODY),
        admin.admin_test_inference_model(admin.InferenceModelTestRequest(model="claude-sonnet-5"), user=NOBODY),
    ):
        with pytest.raises(HTTPException) as exc_info:
            _run(coro)
        assert exc_info.value.status_code == 403


def test_admin_list_shape(admin_routes, health_store):
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or-secret"})
    _run(health_store.record("claude-opus-4-8", False, "not enabled"))
    result = _run(admin_routes.admin_list_inference_providers(user=ADMIN_USER))
    assert "sk-or-secret" not in json.dumps(result)
    assert result["kinds"] == [
        {"kind": "openrouter", "label": "OpenRouter", "endpoint": False},
        {"kind": "local", "label": "Self-hosted", "endpoint": True},
    ]
    assert [t["id"] for t in result["api_types"]] == ["openai", "ollama"]

    vertex = result["vertex"]
    assert vertex["kind"] == "detected"
    assert vertex["configured"] is False
    assert "credentials" in vertex["detail"]
    rows = {m["id"]: m for m in vertex["models"]}
    assert list(rows) == _vertex_ids()  # deprecated excluded
    assert rows["claude-opus-4-8"]["family"] == "anthropic"
    assert rows["gemini-3.8-flash"]["family"] == "gemini_vertex"
    assert rows["claude-haiku-4.5"]["wire_id"] == "claude-haiku-4-5"
    assert rows["claude-opus-4-8"]["status"]["error"] == "not enabled"
    assert rows["claude-sonnet-5"]["status"] is None
    assert all(m["enabled"] for m in vertex["models"])

    assert len(result["instances"]) == 1
    inst = result["instances"][0]
    assert inst["id"] == "openrouter"
    assert inst["kind"] == "openrouter"
    assert inst["configured"] is True
    assert inst["source"] == "store"
    assert inst["credentials"] == {"api_key_set": True}
    assert inst["hint"] == "sk-or-v1-..."
    assert [m["id"] for m in inst["models"]] == [f"openrouter:{DEEPSEEK}", f"openrouter:{QWEN}"]
    assert inst["models"][0]["wire_id"] == DEEPSEEK
    assert inst["models"][0]["family"] is None


def test_admin_put_vertex_disabled_models(admin_routes, scheduled):
    _write_vertex_config(anthropic_project="p")
    body = admin_routes.VertexModelsUpdate(disabled_models=["claude-opus-4-6", "gemini-3.8-flash"])
    result = _run(admin_routes.admin_update_vertex_models(body, user=ADMIN_USER))
    rows = {m["id"]: m["enabled"] for m in result["models"]}
    assert rows["claude-opus-4-6"] is False
    assert rows["gemini-3.8-flash"] is False
    assert rows["claude-opus-4-8"] is True
    assert ip.vertex_disabled_models() == {"claude-opus-4-6", "gemini-3.8-flash"}
    assert scheduled == []  # nothing re-enabled

    # Re-enabling a (configured) model schedules its recheck
    body = admin_routes.VertexModelsUpdate(disabled_models=["gemini-3.8-flash"])
    _run(admin_routes.admin_update_vertex_models(body, user=ADMIN_USER))
    assert scheduled == [["claude-opus-4-6"]]


def test_admin_put_vertex_unknown_model_400(admin_routes):
    body = admin_routes.VertexModelsUpdate(disabled_models=["claude-opus-4-8", "nope"])
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_update_vertex_models(body, user=ADMIN_USER))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "unknown_model"
    assert ip.vertex_disabled_models() == set()


def test_admin_create_instance(admin_routes):
    first = _run(admin_routes.admin_create_inference_instance(
        admin_routes.InstanceCreate(label="Personal"), user=ADMIN_USER,
    ))
    assert first["id"] == "openrouter"
    assert first["label"] == "Personal"
    assert first["configured"] is False
    assert first["models"] == []
    second = _run(admin_routes.admin_create_inference_instance(
        admin_routes.InstanceCreate(), user=ADMIN_USER,
    ))
    assert second["id"] == "openrouter-2"
    assert second["label"] == "OpenRouter"
    assert [i["id"] for i in ip.list_instances()] == ["openrouter", "openrouter-2"]

    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_create_inference_instance(
            admin_routes.InstanceCreate(kind="nope"), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400


def test_admin_update_instance_key_and_label(admin_routes, scheduled, monkeypatch):
    import chat.llm.config as llm_config

    resets: list[bool] = []
    monkeypatch.setattr(llm_config, "reset_provider_client_caches", lambda: resets.append(True))
    ip.upsert_instance(_instance("openrouter", models=[{"id": DEEPSEEK}]))

    body = admin_routes.InstanceUpdate(api_key="  sk-or-new  ", label="Team")
    status = _run(admin_routes.admin_update_inference_instance("openrouter", body, user=ADMIN_USER))
    assert status["configured"] is True
    assert status["label"] == "Team"
    assert "sk-or-new" not in json.dumps(status)
    assert ip.read_inference_credentials("openrouter") == {"api_key": "sk-or-new"}
    assert resets == [True]
    # Every configured model of the instance is rechecked under the new key
    assert scheduled == [[f"openrouter:{DEEPSEEK}"]]

    # Empty key keeps the stored one; label-only save touches nothing else
    body = admin_routes.InstanceUpdate(api_key="", label="Team 2")
    status = _run(admin_routes.admin_update_inference_instance("openrouter", body, user=ADMIN_USER))
    assert status["label"] == "Team 2"
    assert ip.read_inference_credentials("openrouter") == {"api_key": "sk-or-new"}
    assert resets == [True]
    assert len(scheduled) == 1


def test_admin_update_instance_empty_key_without_stored_key_400(admin_routes):
    ip.upsert_instance(_instance("openrouter"))
    body = admin_routes.InstanceUpdate(api_key="")
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_update_inference_instance("openrouter", body, user=ADMIN_USER))
    assert exc_info.value.status_code == 400


def test_admin_update_instance_unknown_404(admin_routes):
    for instance_id in ("nope", "bad id", "vertex"):
        with pytest.raises(HTTPException) as exc_info:
            _run(admin_routes.admin_update_inference_instance(
                instance_id, admin_routes.InstanceUpdate(label="x"), user=ADMIN_USER,
            ))
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error"] == "unknown_instance"


def test_admin_update_instance_models_snapshot_from_catalog(
    admin_routes, scheduled, fake_catalog, health_store,
):
    ip.upsert_instance(_instance("openrouter", models=[
        {"id": QWEN, "name": "kept as-is", "context_length": 5},
    ]))
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or"})
    _run(health_store.record(f"openrouter:{QWEN}", True, None))
    _run(health_store.record("openrouter:old/gone", False, "x"))
    ip.upsert_instance(_instance("openrouter", models=[
        {"id": QWEN, "name": "kept as-is", "context_length": 5}, {"id": "old/gone"},
    ]))

    body = admin_routes.InstanceUpdate(models=[
        admin_routes.InstanceModelUpdate(id=DEEPSEEK),
        admin_routes.InstanceModelUpdate(id=QWEN, enabled=False),
        admin_routes.InstanceModelUpdate(id=" custom/model "),
        admin_routes.InstanceModelUpdate(id=DEEPSEEK),  # duplicate ignored
    ])
    status = _run(admin_routes.admin_update_inference_instance("openrouter", body, user=ADMIN_USER))
    rows = status["models"]
    assert [m["wire_id"] for m in rows] == [DEEPSEEK, QWEN, "custom/model"]
    assert [m["enabled"] for m in rows] == [True, False, True]
    # New model: metadata snapshotted from the catalog
    assert rows[0]["display_name"] == "DeepSeek: V4 Flash"
    stored = {m["id"]: m for m in ip.get_instance("openrouter")["models"]}
    assert stored[DEEPSEEK]["context_length"] == 1_310_720
    assert stored[DEEPSEEK]["max_completion_tokens"] == 384_000
    assert stored[DEEPSEEK]["pricing"] == {"prompt": 0.15, "completion": 0.6, "cache_read": 0.003}
    # Existing model: its stored snapshot is kept (only the flag changes)
    assert stored[QWEN]["name"] == "kept as-is"
    assert stored[QWEN]["context_length"] == 5
    assert stored[QWEN]["enabled"] is False
    # Custom id the catalog does not list: defaults
    assert stored["custom/model"]["name"] == "custom/model"
    assert stored["custom/model"]["pricing"] is None
    # Removed model's verdict is dropped, kept model's stays
    assert health_store.get("openrouter:old/gone") is None
    assert health_store.get(f"openrouter:{QWEN}")["ok"] is True
    # Only enabled + credentialed models are rechecked
    assert scheduled == [[f"openrouter:{DEEPSEEK}", "openrouter:custom/model"]]


def test_admin_delete_instance(admin_routes, health_store, monkeypatch):
    import chat.llm.config as llm_config

    dropped: list[tuple] = []
    monkeypatch.setattr(
        llm_config, "drop_provider_instance", lambda p, i: dropped.append((p, i))
    )
    ip.upsert_instance(_instance("openrouter-2", models=[{"id": DEEPSEEK}]))
    ip.write_inference_credentials("openrouter-2", {"api_key": "k"})
    _run(health_store.record(f"openrouter-2:{DEEPSEEK}", True, None))

    result = _run(admin_routes.admin_delete_inference_instance("openrouter-2", user=ADMIN_USER))
    assert result == {"success": True}
    assert ip.get_instance("openrouter-2") is None
    assert ip.read_inference_credentials("openrouter-2") is None
    assert health_store.get(f"openrouter-2:{DEEPSEEK}") is None
    assert dropped == [("openrouter", "openrouter-2")]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_delete_inference_instance("openrouter-2", user=ADMIN_USER))
    assert exc_info.value.status_code == 404


def test_admin_catalog_search(admin_routes, fake_catalog):
    result = _run(admin_routes.admin_openrouter_catalog(q="qwen", user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == [QWEN]
    assert result["error"] is None and result["stale"] is False
    result = _run(admin_routes.admin_openrouter_catalog(q="deep", limit=1, user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == [DEEPSEEK]
    # Name matches too; empty query lists the head of the catalog
    assert [m["id"] for m in _run(admin_routes.admin_openrouter_catalog(q="llama", user=ADMIN_USER))["models"]] == ["meta/llama-9"]
    assert len(_run(admin_routes.admin_openrouter_catalog(user=ADMIN_USER))["models"]) == 3


# ---------------------------------------------------------------------------
# OpenRouter catalog cache (chat/llm/openrouter_catalog.py)
# ---------------------------------------------------------------------------

def test_catalog_normalizes_entries_and_caches(monkeypatch):
    import chat.llm.openrouter_catalog as catalog

    raw = [
        {"id": DEEPSEEK, "name": "DeepSeek: V4 Flash", "context_length": 1310720,
         "top_provider": {"max_completion_tokens": 384000},
         "pricing": {"prompt": "0.00000015", "completion": "0.0000006", "input_cache_read": "0.000000003"}},
        {"id": "free/model", "name": "", "context_length": "x", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "", "name": "junk"},
        "not a dict",
    ]
    fetches: list[int] = []

    def _fetch():
        fetches.append(1)
        return [m for m in (catalog._normalize_entry(x) for x in raw) if m]

    monkeypatch.setattr(catalog, "fetch_catalog", _fetch)
    first = catalog.get_catalog()
    assert fetches == [1]
    assert [m["id"] for m in first["models"]] == [DEEPSEEK, "free/model"]
    ds = first["models"][0]
    assert ds["context_length"] == 1310720
    assert ds["max_completion_tokens"] == 384000
    assert ds["pricing"] == {"prompt": pytest.approx(0.15), "completion": pytest.approx(0.6), "cache_read": pytest.approx(0.003)}
    free = first["models"][1]
    assert free["name"] == "free/model" and free["context_length"] is None
    assert free["pricing"] == {"prompt": 0.0, "completion": 0.0}
    # Second call within the TTL is served from the cache file -- and the
    # cached (already per-1M) entries must NOT be normalized a second time
    second = catalog.get_catalog()
    assert fetches == [1]
    assert [m["id"] for m in second["models"]] == [DEEPSEEK, "free/model"]
    assert second["models"][0] == ds
    assert second["models"][1] == free
    assert catalog.get_catalog(refresh=True) and fetches == [1, 1]
    assert catalog.catalog_snapshot(first["models"], "free/model") == {
        "name": "free/model", "context_length": None, "max_completion_tokens": None,
        "pricing": {"prompt": 0.0, "completion": 0.0},
    }
    assert catalog.catalog_snapshot(first["models"], "nope") is None


def test_catalog_fetch_failure_serves_stale_or_empty(monkeypatch):
    import chat.llm.openrouter_catalog as catalog

    def _boom():
        raise OSError("network down")

    monkeypatch.setattr(catalog, "fetch_catalog", _boom)
    result = catalog.get_catalog()
    assert result["models"] == [] and result["stale"] is True
    assert "network down" in result["error"]

    # With a cache on disk, the stale copy is served
    catalog._write_cache({"fetched_at": 1.0, "models": [{"id": QWEN, "name": "q"}]})
    result = catalog.get_catalog()
    assert [m["id"] for m in result["models"]] == [QWEN]
    assert result["stale"] is True and result["error"]


def test_catalog_search_ranks_id_prefix_first():
    from chat.llm.openrouter_catalog import search_catalog

    models = [
        {"id": "a/qwen-x", "name": "Alpha"},
        {"id": "qwen/one", "name": "Qwen One"},
        {"id": "b/other", "name": "mentions Qwen"},
        {"id": "c/none", "name": "nothing"},
    ]
    assert [m["id"] for m in search_catalog(models, "qwen")] == ["qwen/one", "a/qwen-x", "b/other"]
    assert [m["id"] for m in search_catalog(models, "", limit=2)] == ["a/qwen-x", "qwen/one"]


# ---------------------------------------------------------------------------
# Model health check (chat/llm/health.py + admin endpoint)
# ---------------------------------------------------------------------------

class _FakeProvider:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.checked: list[str] = []

    async def check_model_access(self, model: str) -> None:
        self.checked.append(model)
        if self.exc is not None:
            raise self.exc


@pytest.fixture
def fake_provider(monkeypatch):
    """Route health checks to a swappable fake provider instance."""
    import chat.llm.config as llm_config

    holder = {"provider": _FakeProvider(), "keys": []}

    def _get(name, instance_id=None):
        holder["keys"].append((name, instance_id))
        return holder["provider"]

    monkeypatch.setattr(llm_config, "get_provider_instance", _get)
    return holder


def test_check_model_ok(fake_provider):
    from chat.llm.health import check_model

    result = _run(check_model("claude-opus-4-8"))
    assert result == {"model": "claude-opus-4-8", "ok": True, "error": None}
    assert fake_provider["provider"].checked == ["claude-opus-4-8"]
    assert fake_provider["keys"] == [("anthropic", None)]


def test_check_model_resolves_instance(store, fake_provider):
    from chat.llm.health import check_model

    ip.upsert_instance(_instance("openrouter-2", models=[{"id": DEEPSEEK}]))
    result = _run(check_model(f"openrouter-2:{DEEPSEEK}"))
    assert result["ok"] is True
    assert fake_provider["keys"] == [("openrouter", "openrouter-2")]
    assert fake_provider["provider"].checked == [f"openrouter-2:{DEEPSEEK}"]


def test_check_model_unknown(fake_provider):
    from chat.llm.health import check_model

    result = _run(check_model("nope-1"))
    assert result["ok"] is False
    assert "Unknown model" in result["error"]
    assert fake_provider["provider"].checked == []


def test_check_model_failure_extracts_anthropic_body(fake_provider):
    from chat.llm.health import check_model

    exc = RuntimeError("Error code: 404")
    exc.status_code = 404
    exc.body = {
        "error": {
            "code": 404,
            "message": "Publisher Model claude-opus-4-8 was not found",
            "status": "NOT_FOUND",
        }
    }
    fake_provider["provider"] = _FakeProvider(exc=exc)
    result = _run(check_model("claude-opus-4-8"))
    assert result["ok"] is False
    assert result["error"] == (
        "HTTP 404: Publisher Model claude-opus-4-8 was not found"
    )


def test_check_model_failure_extracts_genai_message(fake_provider):
    from chat.llm.health import check_model

    exc = RuntimeError("boom")
    exc.code = 429
    exc.message = "Quota exceeded for quota metric 'GenerateContent requests'"
    fake_provider["provider"] = _FakeProvider(exc=exc)
    result = _run(check_model("gemini-3-flash-preview"))
    assert result["ok"] is False
    assert result["error"] == (
        "HTTP 429: Quota exceeded for quota metric 'GenerateContent requests'"
    )


def test_check_model_failure_plain_exception_and_truncation(fake_provider):
    from chat.llm.health import _MAX_ERROR_LENGTH, check_model

    fake_provider["provider"] = _FakeProvider(exc=ValueError("x" * 2000))
    result = _run(check_model("gemini-3-flash-preview"))
    assert result["ok"] is False
    assert len(result["error"]) == _MAX_ERROR_LENGTH
    assert result["error"].endswith("...")


def test_admin_test_model_endpoint(admin_routes, fake_provider, health_store):
    body = admin_routes.InferenceModelTestRequest(model="claude-sonnet-5")
    result = _run(admin_routes.admin_test_inference_model(body, user=ADMIN_USER))
    assert result["model"] == "claude-sonnet-5"
    assert result["ok"] is True
    assert result["error"] is None
    assert result["checked_at"]
    stored = health_store.get("claude-sonnet-5")
    assert stored["ok"] is True
    assert stored["checked_at"] == result["checked_at"]


def test_admin_test_model_bare_legacy_id_records_qualified(admin_routes, fake_provider, health_store):
    ip.upsert_instance(_instance("openrouter", models=[{"id": DEEPSEEK}]))
    body = admin_routes.InferenceModelTestRequest(model=DEEPSEEK)
    result = _run(admin_routes.admin_test_inference_model(body, user=ADMIN_USER))
    assert result["model"] == f"openrouter:{DEEPSEEK}"
    assert health_store.get(f"openrouter:{DEEPSEEK}")["ok"] is True


def test_admin_test_model_unknown_404(admin_routes, fake_provider):
    body = admin_routes.InferenceModelTestRequest(model="nope-1")
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_test_inference_model(body, user=ADMIN_USER))
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "unknown_model"


def test_admin_test_model_disabled_400(admin_routes, fake_provider):
    ip.set_vertex_disabled_models(["claude-sonnet-5"])
    body = admin_routes.InferenceModelTestRequest(model="claude-sonnet-5")
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_routes.admin_test_inference_model(body, user=ADMIN_USER))
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "model_disabled"
    assert fake_provider["provider"].checked == []


# ---------------------------------------------------------------------------
# Model-health store
# ---------------------------------------------------------------------------

def test_store_record_persists_and_reloads(health_store, tmp_path):
    import chat.llm.health as health

    status = _run(health_store.record("claude-sonnet-5", False, "boom"))
    assert status["ok"] is False
    assert status["error"] == "boom"
    assert status["checked_at"]

    reloaded = health.ModelHealthStore(tmp_path / "model_health.json")
    assert reloaded.get("claude-sonnet-5")["error"] == "boom"
    assert reloaded.get("missing") is None


def test_store_forget_drops_and_persists(health_store, tmp_path):
    import chat.llm.health as health

    _run(health_store.record("a", True, None))
    _run(health_store.record("b", True, None))
    _run(health_store.forget(["a", "missing"]))
    assert health_store.get("a") is None
    assert health_store.get("b")["ok"] is True
    reloaded = health.ModelHealthStore(tmp_path / "model_health.json")
    assert reloaded.get("a") is None and reloaded.get("b")


def test_store_run_check_records_verdict(health_store, fake_provider):
    fake_provider["provider"] = _FakeProvider(exc=ValueError("no access"))
    result = _run(health_store.run_check("claude-opus-5"))
    assert result["ok"] is False
    assert result["error"] == "no access"
    assert health_store.get("claude-opus-5")["ok"] is False

    fake_provider["provider"] = _FakeProvider()
    result = _run(health_store.run_check("claude-opus-5"))
    assert result["ok"] is True
    assert health_store.get("claude-opus-5")["ok"] is True


def test_store_ignores_malformed_file(tmp_path):
    import chat.llm.health as health

    path = tmp_path / "model_health.json"
    path.write_text("{not json")
    assert health.ModelHealthStore(path).get_all() == {}


def test_startup_checks_cover_configured_models(health_store, fake_provider, monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(
        llm_config,
        "get_configured_models",
        lambda: ["claude-sonnet-5", "gemini-3-flash-preview"],
    )
    _run(health_store.run_startup_checks())
    assert sorted(fake_provider["provider"].checked) == [
        "claude-sonnet-5",
        "gemini-3-flash-preview",
    ]
    assert health_store.get("claude-sonnet-5")["ok"] is True
    assert health_store.get("gemini-3-flash-preview")["ok"] is True


def test_startup_checks_skip_disabled_models(store, health_store, fake_provider):
    """Disabled models are never checked: the sweep iterates the
    configured list, which excludes them."""
    _write_vertex_config(anthropic_project="p")
    ip.set_vertex_disabled_models([m for m in _vertex_ids() if m != "claude-sonnet-5"])
    _run(health_store.run_startup_checks())
    assert fake_provider["provider"].checked == ["claude-sonnet-5"]


def test_startup_checks_noop_when_unconfigured(health_store, fake_provider, monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(llm_config, "get_configured_models", lambda: [])
    _run(health_store.run_startup_checks())
    assert fake_provider["provider"].checked == []
    assert health_store.get_all() == {}


def test_startup_checks_sweep_health_hidden_models(health_store, fake_provider, monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(
        llm_config, "get_configured_models", lambda: ["claude-sonnet-5"]
    )
    _run(health_store.record("claude-sonnet-5", False, "was broken"))
    _run(health_store.run_startup_checks())
    assert fake_provider["provider"].checked == ["claude-sonnet-5"]
    assert health_store.get("claude-sonnet-5")["ok"] is True


def test_run_startup_model_checks_never_raises(monkeypatch):
    import chat.llm.health as health

    class _ExplodingStore:
        async def run_startup_checks(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(health, "_store", _ExplodingStore())
    _run(health.run_startup_model_checks())  # must not raise


def test_schedule_model_rechecks_runs_and_records(health_store, fake_provider):
    import chat.llm.health as health

    async def _go():
        task = health.schedule_model_rechecks(
            ["claude-sonnet-5", "gemini-3-flash-preview"]
        )
        assert task is not None
        await task

    _run(_go())
    assert fake_provider["provider"].checked == [
        "claude-sonnet-5",
        "gemini-3-flash-preview",
    ]
    assert health_store.get("claude-sonnet-5")["ok"] is True
    assert health_store.get("gemini-3-flash-preview")["ok"] is True


def test_schedule_model_rechecks_empty_is_noop(health_store):
    import chat.llm.health as health

    async def _go():
        assert health.schedule_model_rechecks([]) is None

    _run(_go())
