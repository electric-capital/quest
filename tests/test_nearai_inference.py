"""Tests for NEAR AI Cloud inference provider instances (kind ``nearai``).

Covers the registry entry and credential-file bootstrap in
``config/inference_providers.py``, the fixed-upstream endpoint resolution
of ``OpenRouterProvider`` (NEAR AI base URL, bearer key, no OpenRouter
extras), model resolution / backend label / pricing through the shared
instance machinery, the catalog module ``chat/llm/nearai_catalog.py``
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

import chat.llm.nearai_catalog as na
import config.inference_providers as ip
from chat.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider

ADMIN_USER = {"id": 1, "email": "admin@example.com", "is_admin": True}
NEARAI_URL = "https://cloud-api.near.ai/v1"


def _run(coro):
    return asyncio.run(coro)


def _nearai(instance_id="nearai", models=()):
    return {"id": instance_id, "kind": "nearai", "label": "NEAR", "models": list(models)}


def _raw(
    wire_id, *, name=None, inputs=("text",), outputs=("text",), features=("tools",),
    prompt="0.0000003", completion="0.0000012", cache_read="0.000000006",
    context=1048576, max_output=393216,
):
    """One entry shaped like NEAR AI's ``GET /v1/models`` (OpenRouter row
    shape plus modality / feature lists)."""
    pricing = {"prompt": prompt, "completion": completion, "image": "0", "request": "0"}
    if cache_read is not None:
        pricing["input_cache_read"] = cache_read
    return {
        "id": wire_id, "object": "model", "created": 1774015272, "owned_by": wire_id.split("/")[0],
        "name": name or wire_id.rsplit("/", 1)[-1], "pricing": pricing,
        "context_length": context, "max_output_length": max_output,
        "architecture": {"inputModalities": list(inputs), "outputModalities": list(outputs)},
        "input_modalities": list(inputs), "output_modalities": list(outputs),
        "supported_sampling_parameters": ["max_tokens"] if features else [],
        "supported_features": list(features),
        "top_provider": {"context_length": context, "max_completion_tokens": max_output, "is_moderated": False},
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

def test_nearai_kind_registry_entry():
    spec = ip.INSTANCE_KINDS["nearai"]
    assert spec["label"] == "NEAR AI"
    assert spec["provider"] == "openrouter" and spec["backend"] == "nearai"
    assert spec["key_required"] is True and spec["endpoint"] is False
    assert spec["upstream_url"] == NEARAI_URL and spec["catalog"] == "nearai"
    assert ip.is_endpoint_kind("nearai") is False


def test_nearai_instance_normalization_and_readiness():
    stored = ip.upsert_instance({
        "id": "nearai", "kind": "nearai", "label": "", "base_url": "http://x",
        "models": ["deepseek/deepseek-v3.2"],
    })
    assert stored["label"] == "NEAR AI"
    # Fixed-upstream kinds never carry endpoint fields
    assert "base_url" not in stored and "api_type" not in stored
    assert stored["models"][0]["id"] == "deepseek/deepseek-v3.2"
    # Needs its key
    assert ip.instance_configured(ip.get_instance("nearai")) is False
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    assert ip.instance_configured(ip.get_instance("nearai")) is True


def test_new_nearai_instance_ids():
    assert ip.new_instance_id("nearai") == "nearai"
    ip.upsert_instance(_nearai())
    assert ip.new_instance_id("nearai") == "nearai-2"


def test_kind_for_credential_file_nearai():
    assert ip.kind_for_credential_file("nearai") == "nearai"
    assert ip.kind_for_credential_file("nearai-2") == "nearai"
    assert ip.kind_for_credential_file("nearaix") == "openrouter"
    assert ip.kind_for_credential_file("fireworks") == "fireworks"


def test_prebaked_nearai_credential_file_bootstraps_nearai_instance():
    """A dev-config ``inference_credentials: {"nearai": ...}`` entry (or a
    hand-placed key file) must come up as a NEAR AI instance seeded with
    the file's optional ``models`` list."""
    ip.write_inference_credentials("nearai", {
        "api_key": "sk-near",
        "models": ["deepseek/deepseek-v3.2", {"id": "moonshotai/kimi-k3", "name": "Kimi K3", "context_length": 1048576}],
    })
    ip.write_inference_credentials("nearai-2", {"api_key": "sk-other"})
    instances = {inst["id"]: inst for inst in ip.list_instances()}
    assert instances["nearai"]["kind"] == "nearai" and instances["nearai"]["label"] == "NEAR AI"
    assert [m["id"] for m in instances["nearai"]["models"]] == ["deepseek/deepseek-v3.2", "moonshotai/kimi-k3"]
    assert instances["nearai"]["models"][1]["name"] == "Kimi K3"
    assert instances["nearai-2"]["kind"] == "nearai" and instances["nearai-2"]["label"] == "NEAR AI (nearai-2)"
    assert instances["nearai-2"]["models"] == []
    assert ip.instance_configured(instances["nearai"]) is True


# ---------------------------------------------------------------------------
# Model resolution, backend label, pricing
# ---------------------------------------------------------------------------

def test_resolve_nearai_model():
    from chat.llm.config import (
        get_backend_for_model,
        get_configured_models,
        get_provider_for_model,
        resolve_model,
    )

    ip.upsert_instance(_nearai(models=[{
        "id": "deepseek/deepseek-v3.2", "enabled": True, "name": "DeepSeek V3.2",
        "context_length": 128000, "max_completion_tokens": None,
        "pricing": {"prompt": 0.3, "completion": 1.2, "cache_read": 0.006},
    }]))
    model_id = "nearai:deepseek/deepseek-v3.2"
    spec = resolve_model(model_id)
    assert spec is not None
    assert (spec.instance_id, spec.wire_id) == ("nearai", "deepseek/deepseek-v3.2")
    assert spec.provider == "openrouter" and spec.backend == "nearai"
    assert spec.display_name == "DeepSeek V3.2" and spec.max_input_tokens == 128000
    assert get_provider_for_model(model_id) == "openrouter"
    assert get_backend_for_model(model_id) == "nearai"
    assert ip.split_model_id(model_id) == ("nearai", "deepseek/deepseek-v3.2")
    assert ip.canonical_model_id(model_id) == model_id
    # The same wire id on an OpenRouter instance stays a distinct model
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": ["deepseek/deepseek-v3.2"]})
    assert resolve_model("openrouter:deepseek/deepseek-v3.2").backend == "openrouter"

    # Configured only once the key is stored
    assert model_id not in get_configured_models()
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    assert model_id in get_configured_models()


def test_nearai_pricing_uses_instance_snapshot():
    from db.llm_pricing import estimate_cost_usd

    ip.upsert_instance(_nearai(models=[{
        "id": "deepseek/deepseek-v3.2", "enabled": True,
        "pricing": {"prompt": 1.0, "completion": 2.0, "cache_read": 0.1},
    }]))
    cost = estimate_cost_usd(
        "openrouter", "nearai:deepseek/deepseek-v3.2",
        {"prompt_tokens": 1_000_000, "cached_prompt_tokens": 500_000, "completion_tokens": 1_000_000},
    )
    assert cost == pytest.approx(0.5 * 1.0 + 0.5 * 0.1 + 2.0)
    # A custom id with no snapshot has no estimate
    assert estimate_cost_usd("openrouter", "nearai:vendor/unlisted", {"prompt_tokens": 10}) is None


# ---------------------------------------------------------------------------
# Provider endpoint resolution
# ---------------------------------------------------------------------------

def test_openrouter_provider_endpoint_for_nearai_instance():
    ip.upsert_instance(_nearai())
    provider = OpenRouterProvider("nearai")
    with pytest.raises(ValueError, match="NEAR AI API key not configured"):
        provider._endpoint()
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    endpoint = provider._endpoint()
    assert endpoint == {
        "base_url": NEARAI_URL,
        "api_key": "sk-near",
        # NEAR AI never substitutes another model for the requested one
        "headers": {"x-no-aliasing": "true"},
        "openrouter": False,
        "kind": "nearai",
    }
    # The client is built on the NEAR AI URL with the no-aliasing header as
    # a default header (so it rides on every request), and the
    # OpenRouter-only request extras are switched off
    client = provider._get_client()
    assert str(client.base_url).rstrip("/") == NEARAI_URL
    assert client.default_headers["x-no-aliasing"] == "true"
    assert "X-Title" not in client.default_headers
    assert provider._is_openrouter is False
    assert NEARAI_URL != OPENROUTER_BASE_URL


def test_no_aliasing_header_is_nearai_only():
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "label": "OR", "models": []})
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or"})
    assert OpenRouterProvider("openrouter")._endpoint()["headers"] == {"X-Title": "Quest"}
    ip.upsert_instance({"id": "fireworks", "kind": "fireworks", "label": "FW", "models": []})
    ip.write_inference_credentials("fireworks", {"api_key": "fw-key"})
    assert OpenRouterProvider("fireworks")._endpoint()["headers"] is None


def test_provider_singleton_per_nearai_instance(monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(llm_config, "_provider_instances", {})
    ip.upsert_instance(_nearai())
    provider = llm_config.get_provider_instance("openrouter", "nearai")
    assert isinstance(provider, OpenRouterProvider) and provider.instance_id == "nearai"
    assert provider is llm_config.get_provider_instance("openrouter", "nearai")
    assert provider is not llm_config.get_provider_instance("openrouter", None)


# ---------------------------------------------------------------------------
# Catalog normalization
# ---------------------------------------------------------------------------

def test_normalize_entry_shape():
    entry = na._normalize_entry(_raw(
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
        # No TEE metadata passed -> proxy (fail closed)
        "tee": "proxy",
    }
    # No cache-read price -> plain prompt/completion pricing
    entry = na._normalize_entry(_raw("qwen/qwen3.7-max", cache_read=None, features=("tools", "json_mode")))
    assert entry["pricing"] == {"prompt": pytest.approx(0.3), "completion": pytest.approx(1.2)}
    assert entry["detail"] == "" and entry["capabilities"] == ["tools"]
    # A text model whose feature list lacks tools is flagged as such
    entry = na._normalize_entry(_raw("openai/gpt-6-sol", features=("structured_outputs", "reasoning")))
    assert entry["capabilities"] == [] and entry["detail"] == "reasoning"


def test_normalize_entry_unknown_features_claim_nothing():
    """NEAR AI publishes empty feature lists on freshly added models; the
    typeahead must not render that as "no tool support"."""
    entry = na._normalize_entry(_raw("anthropic/claude-opus-4-8", inputs=("text", "image"), features=()))
    assert entry["capabilities"] == ["vision"] and entry["detail"] == "vision"
    entry = na._normalize_entry(_raw("google/gemini-3.8-flash", features=()))
    assert entry["capabilities"] is None and entry["detail"] == ""


def test_normalize_entry_filters_non_chat_models():
    assert na._normalize_entry(_raw("black-forest-labs/FLUX.2-klein-4B", outputs=("image",), features=())) is None
    assert na._normalize_entry(_raw("openai/whisper-large-v3", inputs=("audio",), features=())) is None
    assert na._normalize_entry(_raw("Qwen/Qwen3-Embedding-0.6B", outputs=("embedding",), features=())) is None
    assert na._normalize_entry(_raw("typesafe/jev-1.13", outputs=("decisions",), features=())) is None
    # Text-in text-out but not chat models: dropped by id
    assert na._normalize_entry(_raw("Qwen/Qwen3-Reranker-0.6B", features=())) is None
    assert na._normalize_entry(_raw("vendor/text-embedding-3", features=())) is None
    # Names merely containing the words elsewhere stay
    assert na._normalize_entry(_raw("vendor/embeddinger-chat")) is not None
    # Sparse entries without modality lists are kept (never hide a chat model)
    sparse = {"id": "vendor/new-model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}
    assert na._normalize_entry(sparse)["capabilities"] is None
    # Unusable rows
    assert na._normalize_entry({"name": "no id"}) is None
    assert na._normalize_entry("nope") is None
    assert na.is_chat_model({"id": "x", "input_modalities": ["TEXT", "image"], "output_modalities": ["Text"]})
    # Camel-case architecture fallback when the flat lists are missing
    assert not na.is_chat_model({"id": "x", "architecture": {"inputModalities": ["audio"], "outputModalities": ["text"]}})


# ---------------------------------------------------------------------------
# Catalog fetch / cache
# ---------------------------------------------------------------------------

def _meta(wire_id, *, provider_type="vllm", verifiable=True, attestation=True):
    """One ``GET /v1/model/list`` item (the TEE metadata list)."""
    return {
        "modelId": wire_id,
        "inputCostPerToken": {"amount": 300, "scale": 9, "currency": "USD"},
        "metadata": {
            "verifiable": verifiable, "providerType": provider_type,
            "attestationSupported": attestation, "contextLength": 1048576,
            "modelDisplayName": wire_id.rsplit("/", 1)[-1],
        },
    }


@pytest.fixture
def fake_urlopen(monkeypatch):
    """Stub ``urllib.request.urlopen``. Requests to the model list return
    ``state["payload"]`` as the JSON body (an Exception value is raised
    instead) and are recorded in ``state["seen"]``; requests to the TEE
    metadata list return ``state["metadata"]`` (a list = one page holding
    it, a dict = that payload, an Exception = raised) and are recorded in
    ``state["seen_metadata"]``."""
    state: dict = {"payload": None, "metadata": [], "seen": [], "seen_metadata": []}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def urlopen(request, timeout=None):
        if request.full_url.startswith(na.MODEL_LIST_URL):
            state["seen_metadata"].append(request)
            target = state["metadata"]
            if isinstance(target, list):
                target = {"models": target, "total": len(target)}
        else:
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
    models = na.fetch_catalog()
    assert [m["id"] for m in models] == ["anthropic/claude-sonnet-5", "Qwen/Qwen3.8-27B", "z-ai/glm-5.3-flash"]
    request = fake_urlopen["seen"][0]
    assert request.full_url == na.CATALOG_URL
    # Public list: no credentials sent
    assert not request.has_header("Authorization")


def test_fetch_catalog_errors(fake_urlopen):
    fake_urlopen["payload"] = {"data": "nope"}
    with pytest.raises(na.NearAICatalogError, match="shape"):
        na.fetch_catalog()
    fake_urlopen["payload"] = {"data": [_raw("x/embedding-1", features=())]}
    with pytest.raises(na.NearAICatalogError, match="no chat models"):
        na.fetch_catalog()
    fake_urlopen["payload"] = urllib.error.HTTPError(na.CATALOG_URL, 503, "unavailable", {}, io.BytesIO(b""))
    with pytest.raises(na.NearAICatalogError, match="HTTP 503"):
        na.fetch_catalog()
    fake_urlopen["payload"] = urllib.error.URLError("dns down")
    with pytest.raises(na.NearAICatalogError, match="dns down"):
        na.fetch_catalog()


def test_get_catalog_caches_and_degrades(fake_urlopen, monkeypatch):
    fake_urlopen["payload"] = {"data": [_raw("deepseek/deepseek-v3.2", name="DeepSeek V3.2")]}
    first = na.get_catalog()
    assert [m["id"] for m in first["models"]] == ["deepseek/deepseek-v3.2"]
    assert first["stale"] is False and first["error"] is None
    assert len(fake_urlopen["seen"]) == 1
    # Fresh cache: no second request
    second = na.get_catalog()
    assert second["models"] == first["models"] and len(fake_urlopen["seen"]) == 1
    # refresh forces a fetch
    na.get_catalog(refresh=True)
    assert len(fake_urlopen["seen"]) == 2
    # Expired cache + failing fetch -> stale cache with the error
    monkeypatch.setattr(na, "CATALOG_TTL_SECONDS", 0)
    fake_urlopen["payload"] = urllib.error.URLError("offline")
    degraded = na.get_catalog()
    assert degraded["models"] == first["models"]
    assert degraded["stale"] is True and "offline" in degraded["error"]
    # No cache at all + failing fetch -> empty list with the error
    na.NEARAI_CATALOG_FILE.unlink()
    empty = na.get_catalog()
    assert empty == {"fetched_at": None, "models": [], "stale": True, "error": "<urlopen error offline>"}


def test_cache_round_trip_keeps_shape(fake_urlopen):
    fake_urlopen["payload"] = {"data": [
        _raw("deepseek/deepseek-v3.2", name="DeepSeek V3.2", inputs=("text", "image"), features=("tools", "reasoning")),
        _raw("google/gemini-3.8-flash", features=()),
    ]}
    fetched = na.get_catalog()["models"]
    cached = na._read_cache()["models"]
    assert cached == fetched
    assert cached[0]["pricing"]["cache_read"] == pytest.approx(0.006)
    assert cached[0]["capabilities"] == ["tools", "vision"] and cached[0]["detail"] == "vision · reasoning"
    assert cached[1]["capabilities"] is None
    # A hand-edited cache row missing fields degrades gracefully
    assert na._cached_entry({"id": "x", "pricing": {"prompt": 1}}) == {
        "id": "x", "name": "x", "context_length": None, "max_completion_tokens": None,
        "pricing": None, "detail": "", "capabilities": None, "reasoning": None, "tee": "proxy",
    }


def test_search_and_snapshot_apply_to_nearai_entries():
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
    # The tee label rides along when the entry carries one (NEAR AI only)
    models[1]["tee"] = "enclave"
    assert catalog_snapshot(models, "moonshotai/kimi-k3")["tee"] == "enclave"


# ---------------------------------------------------------------------------
# TEE metadata (enclave / proxy label)
# ---------------------------------------------------------------------------

def test_classify_entry_fails_closed():
    enclave = {"provider_type": "vllm", "verifiable": True, "attestation_supported": True}
    assert na.classify_entry(enclave) == "enclave"
    assert na.classify_entry({**enclave, "provider_type": "external"}) == "proxy"
    assert na.classify_entry({**enclave, "provider_type": "chutes"}) == "proxy"
    assert na.classify_entry({**enclave, "verifiable": False}) == "proxy"
    assert na.classify_entry({**enclave, "attestation_supported": None}) == "proxy"
    assert na.classify_entry(None) == "proxy"
    assert na.classify_entry({}) == "proxy"
    assert na.TEE_LABELS == ("enclave", "proxy") == ip.TEE_LABELS


def test_normalize_entry_carries_tee_label():
    enclave = na._normalize_entry(
        _raw("deepseek/deepseek-v3.2", features=("tools", "reasoning")),
        {"provider_type": "vllm", "verifiable": True, "attestation_supported": True},
    )
    assert enclave["tee"] == "enclave" and enclave["detail"] == "reasoning"
    proxy = na._normalize_entry(_raw("anthropic/claude-sonnet-5", inputs=("text", "image")))
    assert proxy["tee"] == "proxy" and proxy["detail"] == "vision"


def test_fetch_catalog_merges_tee_metadata_across_pages(fake_urlopen, monkeypatch):
    monkeypatch.setattr(na, "MODEL_LIST_PAGE_SIZE", 2)
    fake_urlopen["payload"] = {"data": [
        _raw("deepseek/deepseek-v3.2"), _raw("anthropic/claude-sonnet-5"),
        _raw("moonshotai/kimi-k3"), _raw("vendor/unlisted-in-metadata"),
    ]}
    pages = [
        {"models": [_meta("deepseek/deepseek-v3.2"), _meta("anthropic/claude-sonnet-5", provider_type="external", verifiable=False, attestation=False)], "total": 3},
        {"models": [_meta("moonshotai/kimi-k3", verifiable=False)], "total": 3},
    ]
    state = {"i": 0}
    real_urlopen = urllib.request.urlopen

    def paged(request, timeout=None):
        if request.full_url.startswith(na.MODEL_LIST_URL):
            fake_urlopen["seen_metadata"].append(request)
            page = pages[state["i"]]
            state["i"] += 1
            return io.BytesIO(json.dumps(page).encode())
        return real_urlopen(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", paged)
    models = {m["id"]: m["tee"] for m in na.fetch_catalog()}
    assert models == {
        "anthropic/claude-sonnet-5": "proxy",
        "deepseek/deepseek-v3.2": "enclave",
        "moonshotai/kimi-k3": "proxy",
        "vendor/unlisted-in-metadata": "proxy",
    }
    urls = [r.full_url for r in fake_urlopen["seen_metadata"]]
    assert urls == [f"{na.MODEL_LIST_URL}?limit=2&offset=0", f"{na.MODEL_LIST_URL}?limit=2&offset=2"]
    # Exactly one model-list request alongside
    assert len(fake_urlopen["seen"]) == 1


def test_fetch_catalog_survives_metadata_failure(fake_urlopen, caplog):
    fake_urlopen["payload"] = {"data": [_raw("deepseek/deepseek-v3.2")]}
    fake_urlopen["metadata"] = urllib.error.HTTPError(na.MODEL_LIST_URL, 500, "boom", {}, io.BytesIO(b""))
    with caplog.at_level("WARNING", logger="chat.llm.nearai_catalog"):
        models = na.fetch_catalog()
    assert [m["tee"] for m in models] == ["proxy"]
    assert "model metadata unavailable" in caplog.text
    # A malformed metadata page degrades the same way
    fake_urlopen["metadata"] = {"nope": 1}
    assert [m["tee"] for m in na.fetch_catalog()] == ["proxy"]


def test_cache_round_trip_keeps_tee_and_old_cache_defaults_to_proxy(fake_urlopen):
    fake_urlopen["payload"] = {"data": [_raw("deepseek/deepseek-v3.2"), _raw("moonshotai/kimi-k3")]}
    fake_urlopen["metadata"] = [_meta("deepseek/deepseek-v3.2")]
    first = na.get_catalog()["models"]
    assert {m["id"]: m["tee"] for m in first} == {"deepseek/deepseek-v3.2": "enclave", "moonshotai/kimi-k3": "proxy"}
    assert na.get_catalog()["models"] == first and len(fake_urlopen["seen"]) == 1
    # A cache written before the label existed is served as proxy
    cache = json.loads(na.NEARAI_CATALOG_FILE.read_text())
    for m in cache["models"]:
        m.pop("tee")
    cache["models"][1]["tee"] = "bogus"
    na.NEARAI_CATALOG_FILE.write_text(json.dumps(cache))
    assert [m["tee"] for m in na.get_catalog()["models"]] == ["proxy", "proxy"]


def test_instance_model_keeps_only_known_tee_labels():
    assert ip.normalize_instance_model({"id": "a/b", "tee": "enclave"})["tee"] == "enclave"
    assert ip.normalize_instance_model({"id": "a/b", "tee": "proxy"})["tee"] == "proxy"
    assert "tee" not in ip.normalize_instance_model({"id": "a/b", "tee": "secure"})
    assert "tee" not in ip.normalize_instance_model({"id": "a/b"})
    assert "tee" not in ip.normalize_instance_model("a/b")


def test_resolve_model_exposes_tee():
    from chat.llm.config import resolve_model

    ip.upsert_instance(_nearai(models=[
        {"id": "deepseek/deepseek-v3.2", "tee": "enclave"}, {"id": "vendor/custom"},
    ]))
    assert resolve_model("nearai:deepseek/deepseek-v3.2").tee == "enclave"
    assert resolve_model("nearai:vendor/custom").tee is None
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "label": "OR", "models": ["deepseek/deepseek-v3.2"]})
    assert resolve_model("openrouter:deepseek/deepseek-v3.2").tee is None


# ---------------------------------------------------------------------------
# Health check: catalog lookup instead of a billed completion
# ---------------------------------------------------------------------------

class _NoCompletions:
    """An openai client stand-in whose completion call must never run."""

    class chat:
        class completions:
            @staticmethod
            async def create(**kwargs):
                raise AssertionError(f"billed completion issued: {kwargs}")


def test_nearai_health_check_is_a_catalog_lookup(monkeypatch):
    calls: list[bool] = []

    def fake_get_catalog(refresh=False):
        calls.append(refresh)
        return {
            "models": [
                _catalog_entry("deepseek/deepseek-v3.2", "DeepSeek"),
                _catalog_entry("vendor/unknown-caps", "Unknown", capabilities=None),
                _catalog_entry("vendor/no-tools", "No tools", capabilities=("vision",)),
            ],
            "fetched_at": time.time(), "stale": False, "error": None,
        }

    monkeypatch.setattr(na, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_nearai(models=["deepseek/deepseek-v3.2"]))
    provider = OpenRouterProvider("nearai")
    provider._client = _NoCompletions()
    # No key yet: reported like every other kind, before any catalog read
    with pytest.raises(ValueError, match="NEAR AI API key not configured"):
        _run(provider.check_model_access("nearai:deepseek/deepseek-v3.2"))
    assert calls == []
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    _run(provider.check_model_access("nearai:deepseek/deepseek-v3.2"))
    # Unknown capabilities claim nothing, so the model passes
    _run(provider.check_model_access("nearai:vendor/unknown-caps"))
    assert calls == [False, False]
    with pytest.raises(ValueError, match="not in NEAR AI's catalog"):
        _run(provider.check_model_access("nearai:vendor/not-listed"))
    with pytest.raises(ValueError, match="without tool-calling support"):
        _run(provider.check_model_access("nearai:vendor/no-tools"))


def test_nearai_health_check_reports_unreachable_catalog(monkeypatch):
    monkeypatch.setattr(na, "get_catalog", lambda refresh=False: {
        "models": [], "fetched_at": None, "stale": True, "error": "<urlopen error offline>",
    })
    ip.upsert_instance(_nearai(models=["deepseek/deepseek-v3.2"]))
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    provider = OpenRouterProvider("nearai")
    provider._client = _NoCompletions()
    with pytest.raises(ValueError, match="NEAR AI catalog unavailable: <urlopen error offline>"):
        _run(provider.check_model_access("nearai:deepseek/deepseek-v3.2"))
    # A stale cache is still a catalog: a listed model passes, an unlisted one fails plainly
    monkeypatch.setattr(na, "get_catalog", lambda refresh=False: {
        "models": [_catalog_entry("deepseek/deepseek-v3.2", "DeepSeek")],
        "fetched_at": 1.0, "stale": True, "error": "HTTP 503",
    })
    _run(provider.check_model_access("nearai:deepseek/deepseek-v3.2"))
    with pytest.raises(ValueError, match="not in NEAR AI's catalog"):
        _run(provider.check_model_access("nearai:vendor/other"))


def test_other_kinds_still_issue_the_minimal_completion(monkeypatch):
    seen: list[dict] = []

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    seen.append(kwargs)

    monkeypatch.setattr(na, "get_catalog", lambda refresh=False: pytest.fail("catalog consulted"))
    ip.upsert_instance({"id": "fireworks", "kind": "fireworks", "label": "FW", "models": []})
    ip.write_inference_credentials("fireworks", {"api_key": "fw-key"})
    provider = OpenRouterProvider("fireworks")
    provider._client = _Client()
    _run(provider.check_model_access("fireworks:accounts/fireworks/models/x"))
    assert seen == [{
        "model": "accounts/fireworks/models/x", "max_tokens": 1,
        "messages": [{"role": "user", "content": "ok"}],
    }]


def test_check_model_uses_catalog_for_nearai_through_health(monkeypatch, tmp_path):
    """End to end through chat.llm.health: a NEAR AI verdict comes from the
    catalog, with the lookup error extracted as the stored message."""
    import chat.llm.health as health

    monkeypatch.setattr(health, "_store", health.ModelHealthStore(tmp_path / "model_health.json"))
    monkeypatch.setattr(na, "get_catalog", lambda refresh=False: {
        "models": [_catalog_entry("deepseek/deepseek-v3.2", "DeepSeek")],
        "fetched_at": time.time(), "stale": False, "error": None,
    })
    ip.upsert_instance(_nearai(models=["deepseek/deepseek-v3.2", "vendor/gone"]))
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    ok = _run(health.check_model("nearai:deepseek/deepseek-v3.2"))
    assert ok["ok"] is True
    bad = _run(health.check_model("nearai:vendor/gone"))
    assert bad["ok"] is False and "not in NEAR AI's catalog" in bad["error"]


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


def test_admin_create_and_list_nearai_instance(admin):
    created = _run(admin.admin_create_inference_instance(
        admin.InstanceCreate(kind="nearai", label="Team NEAR"), user=ADMIN_USER,
    ))
    assert created["id"] == "nearai" and created["kind"] == "nearai"
    assert created["kind_label"] == "NEAR AI" and created["label"] == "Team NEAR"
    assert created["configured"] is False and created["key_required"] is True
    assert created["hint"] == "sk-..." and created["base_url"] is None and created["api_type"] is None
    listing = _run(admin.admin_list_inference_providers(user=ADMIN_USER))
    assert [i["id"] for i in listing["instances"]] == ["nearai"]
    kinds = {k["kind"]: k for k in listing["kinds"]}
    assert kinds["nearai"] == {
        "kind": "nearai", "label": "NEAR AI", "endpoint": False, "catalog": "nearai",
        "hint": "sk-...", "key_required": True,
    }


def test_admin_update_nearai_rejects_endpoint_fields_and_requires_key(admin):
    ip.upsert_instance(_nearai())
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "nearai", admin.InstanceUpdate(base_url="http://x"), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    assert "fixed endpoint" in exc_info.value.detail["message"]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "nearai", admin.InstanceUpdate(api_key=""), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    status = _run(admin.admin_update_inference_instance(
        "nearai", admin.InstanceUpdate(api_key="sk-near"), user=ADMIN_USER,
    ))
    assert status["configured"] is True and status["credentials"]["api_key_set"] is True
    assert "sk-near" not in json.dumps(status)
    assert ip.effective_api_key("nearai") == ("sk-near", "store")


def test_admin_update_nearai_models_snapshot_from_catalog(admin, monkeypatch):
    calls: list[bool] = []

    def fake_get_catalog(refresh=False):
        calls.append(refresh)
        return {
            "models": [{**_catalog_entry(
                "deepseek/deepseek-v3.2", "DeepSeek V3.2", context=128000,
                pricing={"prompt": 0.3, "completion": 1.2, "cache_read": 0.006},
            ), "tee": "enclave"}],
            "fetched_at": time.time(), "stale": False, "error": None,
        }

    monkeypatch.setattr(na, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_nearai())
    # Snapshots need no key: the list is public
    body = admin.InstanceUpdate(models=[
        admin.InstanceModelUpdate(id="deepseek/deepseek-v3.2"),
        admin.InstanceModelUpdate(id="vendor/unlisted", name="Custom"),
    ])
    status = _run(admin.admin_update_inference_instance("nearai", body, user=ADMIN_USER))
    assert calls == [False]
    rows = {m["wire_id"]: m for m in status["models"]}
    assert rows["deepseek/deepseek-v3.2"]["display_name"] == "DeepSeek V3.2"
    assert rows["deepseek/deepseek-v3.2"]["max_input_tokens"] == 128000
    assert rows["deepseek/deepseek-v3.2"]["id"] == "nearai:deepseek/deepseek-v3.2"
    assert rows["vendor/unlisted"]["display_name"] == "Custom"
    assert rows["vendor/unlisted"]["max_input_tokens"] == ip.DEFAULT_INSTANCE_CONTEXT_LENGTH
    # The enclave / proxy badge: from the snapshot, absent on custom ids
    assert rows["deepseek/deepseek-v3.2"]["tee"] == "enclave"
    assert rows["vendor/unlisted"]["tee"] is None
    stored = {m["id"]: m for m in ip.get_instance("nearai")["models"]}
    assert stored["deepseek/deepseek-v3.2"]["tee"] == "enclave"
    assert "tee" not in stored["vendor/unlisted"]
    # NEAR AI publishes prices: the snapshot carries them, incl. cache reads
    assert stored["deepseek/deepseek-v3.2"]["pricing"] == {"prompt": 0.3, "completion": 1.2, "cache_read": 0.006}
    assert stored["vendor/unlisted"]["pricing"] is None
    assert ip.instance_model_pricing("deepseek/deepseek-v3.2") == {"prompt": 0.3, "completion": 1.2, "cache_read": 0.006}


def test_admin_instance_catalog_for_nearai(admin, monkeypatch):
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

    monkeypatch.setattr(na, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_nearai())
    # No key yet: the public list is still served
    result = _run(admin.admin_instance_catalog("nearai", q="kimi", user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == ["moonshotai/kimi-k3"]
    assert result["error"] == "HTTP 503" and calls == [False]
    result = _run(admin.admin_instance_catalog("nearai", refresh=True, user=ADMIN_USER))
    assert len(result["models"]) == 2 and calls == [False, True]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("nearai", user={"id": 2, "email": "x@y", "is_admin": False}))
    assert exc_info.value.status_code == 403


def test_admin_delete_nearai_instance_removes_key_file(admin):
    ip.upsert_instance(_nearai(models=["deepseek/deepseek-v3.2"]))
    ip.write_inference_credentials("nearai", {"api_key": "sk-near"})
    assert _run(admin.admin_delete_inference_instance("nearai", user=ADMIN_USER)) == {"success": True}
    assert ip.get_instance("nearai") is None
    assert ip.read_inference_credentials("nearai") is None
    assert ip.list_instances() == []
