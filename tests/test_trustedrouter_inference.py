"""Tests for TrustedRouter inference provider instances (kind ``trustedrouter``).

Covers the registry entry and credential-file bootstrap in
``config/inference_providers.py``, the fixed-upstream endpoint resolution
of ``OpenRouterProvider`` (TrustedRouter base URL, bearer key, no OpenRouter
extras), model resolution / backend label / pricing through the shared
instance machinery, the catalog module ``chat/llm/trustedrouter_catalog.py``
(OpenRouter-shaped normalization of the public ``/v1/models`` list, the
chat-model filter over modalities / ids, capability flags, caching and
error degradation against a stubbed ``urllib``), and the admin endpoint
behaviour (kinds listing, keyless per-instance catalog route, snapshots on
add). No network: every HTTP call is stubbed.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error
import urllib.request

import pytest
from fastapi import HTTPException

import chat.llm.trustedrouter_catalog as tr
import config.inference_providers as ip
from chat.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider

ADMIN_USER = {"id": 1, "email": "admin@example.com", "is_admin": True}
TRUSTEDROUTER_URL = "https://api.trustedrouter.com/v1"


def _run(coro):
    return asyncio.run(coro)


def _trustedrouter(instance_id="trustedrouter", models=()):
    return {"id": instance_id, "kind": "trustedrouter", "label": "TR", "models": list(models)}


def _raw(
    wire_id, *, name=None, inputs=("text",), outputs=("text",), features=("tools",),
    prompt="0.0000003", completion="0.0000012", cache_read="0.000000006",
    context=1048576, max_output=393216,
):
    """One entry shaped like TrustedRouter's ``GET /v1/models`` (the
    OpenRouter row shape: modalities under ``architecture``, capability
    flags in ``supported_parameters``)."""
    pricing = {"prompt": prompt, "completion": completion}
    if cache_read is not None:
        pricing["input_cache_read"] = cache_read
    return {
        "id": wire_id, "created": 0,
        "name": name or wire_id.rsplit("/", 1)[-1], "pricing": pricing,
        "context_length": context,
        "architecture": {
            "modality": f"{'+'.join(inputs)}->{'+'.join(outputs)}",
            "input_modalities": list(inputs), "output_modalities": list(outputs),
            "tokenizer": "unknown", "instruct_type": None,
        },
        "supported_parameters": list(features),
        "top_provider": {"context_length": context, "max_completion_tokens": max_output, "is_moderated": False},
        "per_request_limits": None,
    }


def _catalog_entry(wire_id, name, *, context=1048576, pricing=None, capabilities=("tools",), detail=""):
    return {
        "id": wire_id, "name": name, "context_length": context, "max_completion_tokens": None,
        "pricing": pricing, "detail": detail,
        "capabilities": list(capabilities) if capabilities is not None else None,
    }


# ---------------------------------------------------------------------------
# Registry + config store
# ---------------------------------------------------------------------------

def test_trustedrouter_kind_registry_entry():
    spec = ip.INSTANCE_KINDS["trustedrouter"]
    assert spec["label"] == "TrustedRouter"
    assert spec["provider"] == "openrouter" and spec["backend"] == "trustedrouter"
    assert spec["key_required"] is True and spec["endpoint"] is False
    assert spec["upstream_url"] == TRUSTEDROUTER_URL and spec["catalog"] == "trustedrouter"
    assert ip.is_endpoint_kind("trustedrouter") is False


def test_trustedrouter_instance_normalization_and_readiness():
    stored = ip.upsert_instance({
        "id": "trustedrouter", "kind": "trustedrouter", "label": "", "base_url": "http://x",
        "models": ["deepseek/deepseek-v3.2"],
    })
    assert stored["label"] == "TrustedRouter"
    # Fixed-upstream kinds never carry endpoint fields
    assert "base_url" not in stored and "api_type" not in stored
    assert stored["models"][0]["id"] == "deepseek/deepseek-v3.2"
    # Needs its key
    assert ip.instance_configured(ip.get_instance("trustedrouter")) is False
    ip.write_inference_credentials("trustedrouter", {"api_key": "sk-tr-test"})
    assert ip.instance_configured(ip.get_instance("trustedrouter")) is True


def test_new_trustedrouter_instance_ids():
    assert ip.new_instance_id("trustedrouter") == "trustedrouter"
    ip.upsert_instance(_trustedrouter())
    assert ip.new_instance_id("trustedrouter") == "trustedrouter-2"


def test_kind_for_credential_file_trustedrouter():
    assert ip.kind_for_credential_file("trustedrouter") == "trustedrouter"
    assert ip.kind_for_credential_file("trustedrouter-2") == "trustedrouter"
    assert ip.kind_for_credential_file("trustedrouterx") == "openrouter"
    assert ip.kind_for_credential_file("fireworks") == "fireworks"


def test_prebaked_trustedrouter_credential_file_bootstraps_trustedrouter_instance():
    """A dev-config ``inference_credentials: {"trustedrouter": ...}`` entry (or a
    hand-placed key file) must come up as a TrustedRouter instance seeded with
    the file's optional ``models`` list."""
    ip.write_inference_credentials("trustedrouter", {
        "api_key": "sk-tr-test",
        "models": ["deepseek/deepseek-v3.2", {"id": "moonshotai/kimi-k3", "name": "Kimi K3", "context_length": 1048576}],
    })
    ip.write_inference_credentials("trustedrouter-2", {"api_key": "sk-other"})
    instances = {inst["id"]: inst for inst in ip.list_instances()}
    assert instances["trustedrouter"]["kind"] == "trustedrouter" and instances["trustedrouter"]["label"] == "TrustedRouter"
    assert [m["id"] for m in instances["trustedrouter"]["models"]] == ["deepseek/deepseek-v3.2", "moonshotai/kimi-k3"]
    assert instances["trustedrouter"]["models"][1]["name"] == "Kimi K3"
    assert instances["trustedrouter-2"]["kind"] == "trustedrouter" and instances["trustedrouter-2"]["label"] == "TrustedRouter (trustedrouter-2)"
    assert instances["trustedrouter-2"]["models"] == []
    assert ip.instance_configured(instances["trustedrouter"]) is True


# ---------------------------------------------------------------------------
# Model resolution, backend label, pricing
# ---------------------------------------------------------------------------

def test_resolve_trustedrouter_model():
    from chat.llm.config import (
        get_backend_for_model,
        get_configured_models,
        get_provider_for_model,
        resolve_model,
    )

    ip.upsert_instance(_trustedrouter(models=[{
        "id": "deepseek/deepseek-v3.2", "enabled": True, "name": "DeepSeek V3.2",
        "context_length": 128000, "max_completion_tokens": None,
        "pricing": {"prompt": 0.3, "completion": 1.2, "cache_read": 0.006},
    }]))
    model_id = "trustedrouter:deepseek/deepseek-v3.2"
    spec = resolve_model(model_id)
    assert spec is not None
    assert (spec.instance_id, spec.wire_id) == ("trustedrouter", "deepseek/deepseek-v3.2")
    assert spec.provider == "openrouter" and spec.backend == "trustedrouter"
    assert spec.display_name == "DeepSeek V3.2" and spec.max_input_tokens == 128000
    assert get_provider_for_model(model_id) == "openrouter"
    assert get_backend_for_model(model_id) == "trustedrouter"
    assert ip.split_model_id(model_id) == ("trustedrouter", "deepseek/deepseek-v3.2")
    assert ip.canonical_model_id(model_id) == model_id
    # The same wire id on an OpenRouter instance stays a distinct model
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": ["deepseek/deepseek-v3.2"]})
    assert resolve_model("openrouter:deepseek/deepseek-v3.2").backend == "openrouter"

    # Configured only once the key is stored
    assert model_id not in get_configured_models()
    ip.write_inference_credentials("trustedrouter", {"api_key": "sk-tr-test"})
    assert model_id in get_configured_models()


def test_trustedrouter_pricing_uses_instance_snapshot():
    from db.llm_pricing import estimate_cost_usd

    ip.upsert_instance(_trustedrouter(models=[{
        "id": "deepseek/deepseek-v3.2", "enabled": True,
        "pricing": {"prompt": 1.0, "completion": 2.0, "cache_read": 0.1},
    }]))
    cost = estimate_cost_usd(
        "openrouter", "trustedrouter:deepseek/deepseek-v3.2",
        {"prompt_tokens": 1_000_000, "cached_prompt_tokens": 500_000, "completion_tokens": 1_000_000},
    )
    assert cost == pytest.approx(0.5 * 1.0 + 0.5 * 0.1 + 2.0)
    # A custom id with no snapshot has no estimate
    assert estimate_cost_usd("openrouter", "trustedrouter:vendor/unlisted", {"prompt_tokens": 10}) is None


# ---------------------------------------------------------------------------
# Provider endpoint resolution
# ---------------------------------------------------------------------------

def test_openrouter_provider_endpoint_for_trustedrouter_instance():
    ip.upsert_instance(_trustedrouter())
    provider = OpenRouterProvider("trustedrouter")
    with pytest.raises(ValueError, match="TrustedRouter API key not configured"):
        provider._endpoint()
    ip.write_inference_credentials("trustedrouter", {"api_key": "sk-tr-test"})
    endpoint = provider._endpoint()
    assert endpoint == {
        "base_url": TRUSTEDROUTER_URL,
        "api_key": "sk-tr-test",
        "headers": None,
        "openrouter": False,
    }
    # The client is built on the TrustedRouter URL and the OpenRouter-only
    # request extras are switched off
    client = provider._get_client()
    assert str(client.base_url).rstrip("/") == TRUSTEDROUTER_URL
    assert provider._is_openrouter is False
    assert TRUSTEDROUTER_URL != OPENROUTER_BASE_URL


def test_provider_singleton_per_trustedrouter_instance(monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(llm_config, "_provider_instances", {})
    ip.upsert_instance(_trustedrouter())
    provider = llm_config.get_provider_instance("openrouter", "trustedrouter")
    assert isinstance(provider, OpenRouterProvider) and provider.instance_id == "trustedrouter"
    assert provider is llm_config.get_provider_instance("openrouter", "trustedrouter")
    assert provider is not llm_config.get_provider_instance("openrouter", None)


# ---------------------------------------------------------------------------
# Catalog normalization
# ---------------------------------------------------------------------------

def test_normalize_entry_shape():
    entry = tr._normalize_entry(_raw(
        "deepseek/deepseek-v4.1-flash", name="DeepSeek V4.1 Flash", inputs=("text", "image"),
        features=("tools", "json_mode", "reasoning"),
    ))
    assert entry == {
        "id": "deepseek/deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "context_length": 1048576,
        "max_completion_tokens": 393216,
        # $/token strings converted to $/1M like the OpenRouter catalog
        "pricing": {"prompt": pytest.approx(0.3), "completion": pytest.approx(1.2), "cache_read": pytest.approx(0.006)},
        "detail": "vision · reasoning",
        "capabilities": ["tools", "vision"],
        # Reasoning accepted but no per-model object: the generic levels
        "reasoning": {},
    }
    # No cache-read price -> plain prompt/completion pricing
    entry = tr._normalize_entry(_raw("qwen/qwen3.7-max", cache_read=None, features=("tools", "json_mode")))
    assert entry["pricing"] == {"prompt": pytest.approx(0.3), "completion": pytest.approx(1.2)}
    assert entry["detail"] == "" and entry["capabilities"] == ["tools"]
    assert entry["reasoning"] is None
    # A text model whose feature list lacks tools is flagged as such
    entry = tr._normalize_entry(_raw("openai/gpt-6-sol", features=("structured_outputs", "reasoning")))
    assert entry["capabilities"] == [] and entry["detail"] == "reasoning"


def test_normalize_entry_unknown_features_claim_nothing():
    """An entry with no ``supported_parameters`` list says nothing about
    tools; the typeahead must not render that as "no tool support"."""
    entry = tr._normalize_entry(_raw("anthropic/claude-opus-4-8", inputs=("text", "image"), features=()))
    assert entry["capabilities"] == ["vision"] and entry["detail"] == "vision"
    entry = tr._normalize_entry(_raw("google/gemini-3.8-flash", features=()))
    assert entry["capabilities"] is None and entry["detail"] == ""


def test_normalize_entry_filters_non_chat_models():
    assert tr._normalize_entry(_raw("black-forest-labs/FLUX.2-klein-4B", outputs=("image",), features=())) is None
    assert tr._normalize_entry(_raw("openai/whisper-large-v3", inputs=("audio",), features=())) is None
    assert tr._normalize_entry(_raw("Qwen/Qwen3-Embedding-0.6B", outputs=("embedding",), features=())) is None
    assert tr._normalize_entry(_raw("typesafe/jev-1.13", outputs=("decisions",), features=())) is None
    # Text-in text-out but not chat models: dropped by id
    assert tr._normalize_entry(_raw("Qwen/Qwen3-Reranker-0.6B", features=())) is None
    assert tr._normalize_entry(_raw("vendor/text-embedding-3", features=())) is None
    # Names merely containing the words elsewhere stay
    assert tr._normalize_entry(_raw("vendor/embeddinger-chat")) is not None
    # Sparse entries without modality lists are kept (never hide a chat model)
    sparse = {"id": "vendor/new-model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
    assert tr._normalize_entry(sparse)["capabilities"] is None
    # Unusable rows
    assert tr._normalize_entry({"name": "no id"}) is None
    assert tr._normalize_entry("nope") is None
    assert tr.is_chat_model({"id": "x", "input_modalities": ["TEXT", "image"], "output_modalities": ["Text"]})
    # Top-level fallback when ``architecture`` carries no lists
    assert not tr.is_chat_model({"id": "x", "input_modalities": ["audio"], "output_modalities": ["text"]})
    assert not tr.is_chat_model({"id": "x", "architecture": {"input_modalities": ["text"], "output_modalities": ["video"]}})


# ---------------------------------------------------------------------------
# Catalog fetch / cache
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_urlopen(monkeypatch):
    """Stub ``urllib.request.urlopen``: ``state["payload"]`` is returned as
    the JSON body (an Exception value is raised instead); every request is
    recorded in ``state["seen"]``."""
    state: dict = {"payload": None, "seen": []}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def urlopen(request, timeout=None):
        state["seen"].append(request)
        target = state["payload"]
        if isinstance(target, Exception):
            raise target
        return _Response(json.dumps(target).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return state


def test_fetch_catalog_filters_sorts_and_dedupes(fake_urlopen):
    fake_urlopen["payload"] = {"object": "list", "data": [
        _raw("z-ai/glm-5.3-flash"),
        _raw("black-forest-labs/FLUX.2-klein-4B", outputs=("image",), features=()),
        _raw("Qwen/Qwen3.8-27B", inputs=("text", "image")),
        _raw("anthropic/claude-sonnet-5"),
        _raw("anthropic/claude-sonnet-5"),  # duplicate id
        "garbage",
    ]}
    models = tr.fetch_catalog()
    assert [m["id"] for m in models] == ["anthropic/claude-sonnet-5", "Qwen/Qwen3.8-27B", "z-ai/glm-5.3-flash"]
    request = fake_urlopen["seen"][0]
    assert request.full_url == tr.CATALOG_URL
    # Public list: no credentials sent
    assert not request.has_header("Authorization")


def test_fetch_catalog_errors(fake_urlopen):
    fake_urlopen["payload"] = {"data": "nope"}
    with pytest.raises(tr.TrustedRouterCatalogError, match="shape"):
        tr.fetch_catalog()
    fake_urlopen["payload"] = {"data": [_raw("x/embedding-1", features=())]}
    with pytest.raises(tr.TrustedRouterCatalogError, match="no chat models"):
        tr.fetch_catalog()
    fake_urlopen["payload"] = urllib.error.HTTPError(tr.CATALOG_URL, 503, "unavailable", {}, io.BytesIO(b""))
    with pytest.raises(tr.TrustedRouterCatalogError, match="HTTP 503"):
        tr.fetch_catalog()
    fake_urlopen["payload"] = urllib.error.URLError("dns down")
    with pytest.raises(tr.TrustedRouterCatalogError, match="dns down"):
        tr.fetch_catalog()


def test_get_catalog_caches_and_degrades(fake_urlopen, monkeypatch):
    fake_urlopen["payload"] = {"data": [_raw("deepseek/deepseek-v3.2", name="DeepSeek V3.2")]}
    first = tr.get_catalog()
    assert [m["id"] for m in first["models"]] == ["deepseek/deepseek-v3.2"]
    assert first["stale"] is False and first["error"] is None
    assert len(fake_urlopen["seen"]) == 1
    # Fresh cache: no second request
    second = tr.get_catalog()
    assert second["models"] == first["models"] and len(fake_urlopen["seen"]) == 1
    # refresh forces a fetch
    tr.get_catalog(refresh=True)
    assert len(fake_urlopen["seen"]) == 2
    # Expired cache + failing fetch -> stale cache with the error
    monkeypatch.setattr(tr, "CATALOG_TTL_SECONDS", 0)
    fake_urlopen["payload"] = urllib.error.URLError("offline")
    degraded = tr.get_catalog()
    assert degraded["models"] == first["models"]
    assert degraded["stale"] is True and "offline" in degraded["error"]
    # No cache at all + failing fetch -> empty list with the error
    tr.TRUSTEDROUTER_CATALOG_FILE.unlink()
    empty = tr.get_catalog()
    assert empty == {"fetched_at": None, "models": [], "stale": True, "error": "<urlopen error offline>"}


def test_cache_round_trip_keeps_shape(fake_urlopen):
    fake_urlopen["payload"] = {"data": [
        _raw("deepseek/deepseek-v3.2", name="DeepSeek V3.2", inputs=("text", "image"), features=("tools", "reasoning")),
        _raw("google/gemini-3.8-flash", features=()),
    ]}
    fetched = tr.get_catalog()["models"]
    cached = tr._read_cache()["models"]
    assert cached == fetched
    assert cached[0]["pricing"]["cache_read"] == pytest.approx(0.006)
    assert cached[0]["capabilities"] == ["tools", "vision"] and cached[0]["detail"] == "vision · reasoning"
    assert cached[1]["capabilities"] is None
    # A hand-edited cache row missing fields degrades gracefully
    assert tr._cached_entry({"id": "x", "pricing": {"prompt": 1}}) == {
        "id": "x", "name": "x", "context_length": None, "max_completion_tokens": None,
        "pricing": None, "detail": "", "capabilities": None, "reasoning": None,
    }


def test_search_and_snapshot_apply_to_trustedrouter_entries():
    from chat.llm.openrouter_catalog import catalog_snapshot, search_catalog

    models = [
        _catalog_entry("deepseek/deepseek-v3.2", "DeepSeek V3.2", pricing={"prompt": 0.3, "completion": 1.2}),
        _catalog_entry("moonshotai/kimi-k3", "Kimi K3"),
    ]
    assert [m["id"] for m in search_catalog(models, "kimi")] == ["moonshotai/kimi-k3"]
    assert catalog_snapshot(models, "deepseek/deepseek-v3.2") == {
        "name": "DeepSeek V3.2", "context_length": 1048576, "max_completion_tokens": None,
        "pricing": {"prompt": 0.3, "completion": 1.2},
    }


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(monkeypatch, tmp_path):
    import chat.llm.health as health
    import chat.routes.admin as admin_routes

    monkeypatch.setattr(admin_routes, "is_admin", lambda email: email == ADMIN_USER["email"])
    monkeypatch.setattr(health, "_store", health.ModelHealthStore(tmp_path / "model_health.json"))
    monkeypatch.setattr(health, "schedule_model_rechecks", lambda ids: None)
    return admin_routes


def test_admin_create_and_list_trustedrouter_instance(admin):
    created = _run(admin.admin_create_inference_instance(
        admin.InstanceCreate(kind="trustedrouter", label="Team TR"), user=ADMIN_USER,
    ))
    assert created["id"] == "trustedrouter" and created["kind"] == "trustedrouter"
    assert created["kind_label"] == "TrustedRouter" and created["label"] == "Team TR"
    assert created["configured"] is False and created["key_required"] is True
    assert created["hint"] == "sk-tr-v1-..." and created["base_url"] is None and created["api_type"] is None
    listing = _run(admin.admin_list_inference_providers(user=ADMIN_USER))
    assert [i["id"] for i in listing["instances"]] == ["trustedrouter"]
    kinds = {k["kind"]: k for k in listing["kinds"]}
    assert kinds["trustedrouter"] == {"kind": "trustedrouter", "label": "TrustedRouter", "endpoint": False, "catalog": "trustedrouter"}


def test_admin_update_trustedrouter_rejects_endpoint_fields_and_requires_key(admin):
    ip.upsert_instance(_trustedrouter())
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "trustedrouter", admin.InstanceUpdate(base_url="http://x"), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    assert "fixed endpoint" in exc_info.value.detail["message"]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "trustedrouter", admin.InstanceUpdate(api_key=""), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    status = _run(admin.admin_update_inference_instance(
        "trustedrouter", admin.InstanceUpdate(api_key="sk-tr-test"), user=ADMIN_USER,
    ))
    assert status["configured"] is True and status["credentials"]["api_key_set"] is True
    assert "sk-tr-test" not in json.dumps(status)
    assert ip.effective_api_key("trustedrouter") == ("sk-tr-test", "store")


def test_admin_update_trustedrouter_models_snapshot_from_catalog(admin, monkeypatch):
    calls: list[bool] = []

    def fake_get_catalog(refresh=False):
        calls.append(refresh)
        return {
            "models": [_catalog_entry(
                "deepseek/deepseek-v3.2", "DeepSeek V3.2", context=128000,
                pricing={"prompt": 0.3, "completion": 1.2, "cache_read": 0.006},
            )],
            "fetched_at": time.time(), "stale": False, "error": None,
        }

    monkeypatch.setattr(tr, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_trustedrouter())
    # Snapshots need no key: the list is public
    body = admin.InstanceUpdate(models=[
        admin.InstanceModelUpdate(id="deepseek/deepseek-v3.2"),
        admin.InstanceModelUpdate(id="vendor/unlisted", name="Custom"),
    ])
    status = _run(admin.admin_update_inference_instance("trustedrouter", body, user=ADMIN_USER))
    assert calls == [False]
    rows = {m["wire_id"]: m for m in status["models"]}
    assert rows["deepseek/deepseek-v3.2"]["display_name"] == "DeepSeek V3.2"
    assert rows["deepseek/deepseek-v3.2"]["max_input_tokens"] == 128000
    assert rows["deepseek/deepseek-v3.2"]["id"] == "trustedrouter:deepseek/deepseek-v3.2"
    assert rows["vendor/unlisted"]["display_name"] == "Custom"
    assert rows["vendor/unlisted"]["max_input_tokens"] == ip.DEFAULT_INSTANCE_CONTEXT_LENGTH
    stored = {m["id"]: m for m in ip.get_instance("trustedrouter")["models"]}
    # TrustedRouter publishes prices: the snapshot carries them, incl. cache reads
    assert stored["deepseek/deepseek-v3.2"]["pricing"] == {"prompt": 0.3, "completion": 1.2, "cache_read": 0.006}
    assert stored["vendor/unlisted"]["pricing"] is None
    assert ip.instance_model_pricing("deepseek/deepseek-v3.2") == {"prompt": 0.3, "completion": 1.2, "cache_read": 0.006}


def test_admin_instance_catalog_for_trustedrouter(admin, monkeypatch):
    calls: list[bool] = []

    def fake_get_catalog(refresh=False):
        calls.append(refresh)
        return {
            "models": [
                _catalog_entry("deepseek/deepseek-v3.2", "DeepSeek"),
                _catalog_entry("moonshotai/kimi-k3", "Kimi K3"),
            ],
            "fetched_at": time.time(), "stale": True, "error": "HTTP 503",
        }

    monkeypatch.setattr(tr, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_trustedrouter())
    # No key yet: the public list is still served
    result = _run(admin.admin_instance_catalog("trustedrouter", q="kimi", user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == ["moonshotai/kimi-k3"]
    assert result["error"] == "HTTP 503" and calls == [False]
    result = _run(admin.admin_instance_catalog("trustedrouter", refresh=True, user=ADMIN_USER))
    assert len(result["models"]) == 2 and calls == [False, True]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("trustedrouter", user={"id": 2, "email": "x@y", "is_admin": False}))
    assert exc_info.value.status_code == 403


def test_admin_delete_trustedrouter_instance_removes_key_file(admin):
    ip.upsert_instance(_trustedrouter(models=["deepseek/deepseek-v3.2"]))
    ip.write_inference_credentials("trustedrouter", {"api_key": "sk-tr-test"})
    assert _run(admin.admin_delete_inference_instance("trustedrouter", user=ADMIN_USER)) == {"success": True}
    assert ip.get_instance("trustedrouter") is None
    assert ip.read_inference_credentials("trustedrouter") is None
    assert ip.list_instances() == []
