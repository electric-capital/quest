"""Tests for UberLLM inference provider instances (kind ``uberllm``).

Covers the registry entry and credential-file bootstrap in
``config/inference_providers.py``, the fixed-upstream endpoint resolution
of ``OpenRouterProvider`` (UberLLM base URL, bearer key, no OpenRouter
extras), model resolution / backend label through the shared instance
machinery, the catalog module ``chat/llm/uberllm_catalog.py`` (UberLLM's
``display_name`` and ``available`` fields on top of the shared
OpenRouter-shaped normalization, caching and error degradation against a
stubbed ``urllib``), and the keyless admin catalog route. No network:
every HTTP call is stubbed.
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

import chat.llm.uberllm_catalog as ub
import config.inference_providers as ip
from chat.llm.openrouter_provider import OpenRouterProvider

ADMIN_USER = {"id": 1, "email": "admin@example.com", "is_admin": True}
UBERLLM_URL = "https://api.uberllm.dev/v1"


def _run(coro):
    return asyncio.run(coro)


def _uberllm(instance_id="uberllm", models=()):
    return {"id": instance_id, "kind": "uberllm", "label": "UB", "models": list(models)}


def _raw(
    wire_id, *, display_name=None, inputs=("text",), outputs=("text",), features=("tools",),
    prompt="0.0000003", completion="0.0000012", cache_read="0.000000006",
    context=1048576, max_output=393216, available=True,
):
    """One entry shaped like UberLLM's ``GET /v1/models``: the OpenRouter
    row shape, with the human name in ``display_name`` (no ``name``) and an
    ``available`` flag."""
    pricing = {"prompt": prompt, "completion": completion, "unit": "1M tokens"}
    if cache_read is not None:
        pricing["input_cache_read"] = cache_read
    return {
        "id": wire_id, "object": "model", "type": "chat", "created": 0,
        "display_name": display_name or wire_id.rsplit("/", 1)[-1],
        "available": available, "pricing": pricing, "context_length": context,
        "max_output_tokens": max_output,
        "architecture": {"input_modalities": list(inputs), "output_modalities": list(outputs)},
        "input_modalities": list(inputs), "output_modalities": list(outputs),
        "supported_parameters": list(features),
        "top_provider": {"context_length": context, "max_completion_tokens": max_output},
    }


# ---------------------------------------------------------------------------
# Registry + config store
# ---------------------------------------------------------------------------

def test_uberllm_kind_registry_entry():
    spec = ip.INSTANCE_KINDS["uberllm"]
    assert spec["label"] == "UberLLM" and spec["hint"] == "ull_..."
    assert spec["provider"] == "openrouter" and spec["backend"] == "uberllm"
    assert spec["key_required"] is True and spec["endpoint"] is False
    assert spec["upstream_url"] == UBERLLM_URL and spec["catalog"] == "uberllm"
    assert ip.is_endpoint_kind("uberllm") is False


def test_kind_for_credential_file_uberllm():
    assert ip.kind_for_credential_file("uberllm") == "uberllm"
    assert ip.kind_for_credential_file("uberllm-2") == "uberllm"
    assert ip.kind_for_credential_file("uberllmx") == "openrouter"


def test_prebaked_uberllm_credential_file_bootstraps_uberllm_instance():
    ip.write_inference_credentials("uberllm", {
        "api_key": "ull_test", "models": ["deepseek/deepseek-v4-flash-0731"],
    })
    instances = {inst["id"]: inst for inst in ip.list_instances()}
    assert instances["uberllm"]["kind"] == "uberllm" and instances["uberllm"]["label"] == "UberLLM"
    assert [m["id"] for m in instances["uberllm"]["models"]] == ["deepseek/deepseek-v4-flash-0731"]
    assert ip.instance_configured(instances["uberllm"]) is True


# ---------------------------------------------------------------------------
# Model resolution + provider endpoint
# ---------------------------------------------------------------------------

def test_resolve_uberllm_model():
    from chat.llm.config import get_backend_for_model, get_provider_for_model, resolve_model

    ip.upsert_instance(_uberllm(models=[{
        "id": "deepseek/deepseek-v4-flash-0731", "enabled": True, "name": "DeepSeek V4 Flash",
        "context_length": 1048576, "max_completion_tokens": None,
        "pricing": {"prompt": 0.3, "completion": 1.2},
    }]))
    model_id = "uberllm:deepseek/deepseek-v4-flash-0731"
    spec = resolve_model(model_id)
    assert (spec.instance_id, spec.wire_id) == ("uberllm", "deepseek/deepseek-v4-flash-0731")
    assert spec.provider == "openrouter" and spec.backend == "uberllm"
    assert get_provider_for_model(model_id) == "openrouter"
    assert get_backend_for_model(model_id) == "uberllm"


def test_openrouter_provider_endpoint_for_uberllm_instance():
    ip.upsert_instance(_uberllm())
    provider = OpenRouterProvider("uberllm")
    with pytest.raises(ValueError, match="UberLLM API key not configured"):
        provider._endpoint()
    ip.write_inference_credentials("uberllm", {"api_key": "ull_test"})
    assert provider._endpoint() == {
        "base_url": UBERLLM_URL, "api_key": "ull_test", "headers": None,
        "openrouter": False, "kind": "uberllm",
    }
    client = provider._get_client()
    assert str(client.base_url).rstrip("/") == UBERLLM_URL
    assert provider._is_openrouter is False


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_normalize_entry_uses_display_name_and_drops_unavailable():
    entry = ub._normalize_entry(_raw(
        "deepseek/deepseek-v4.1-flash", display_name="DeepSeek V4.1 Flash", inputs=("text", "image"),
        features=("tools", "reasoning"),
    ))
    assert entry == {
        "id": "deepseek/deepseek-v4.1-flash",
        "name": "DeepSeek V4.1 Flash",
        "context_length": 1048576,
        "max_completion_tokens": 393216,
        "pricing": {"prompt": pytest.approx(0.3), "completion": pytest.approx(1.2), "cache_read": pytest.approx(0.006)},
        "detail": "vision · reasoning",
        "capabilities": ["tools", "vision"],
        "reasoning": {},
    }
    assert ub._normalize_entry(_raw("vendor/offline", available=False)) is None
    # A row without the flag is kept
    row = _raw("vendor/no-flag")
    del row["available"]
    assert ub._normalize_entry(row)["name"] == "no-flag"
    # Non-chat outputs are dropped like on the other routers
    assert ub._normalize_entry(_raw("typesafe/jev-1.13", outputs=("decisions",), features=())) is None
    assert ub._normalize_entry("nope") is None


@pytest.fixture
def fake_urlopen(monkeypatch):
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
        _raw("anthropic/claude-sonnet-5"),
        _raw("anthropic/claude-sonnet-5"),
        _raw("vendor/offline", available=False),
        "garbage",
    ]}
    assert [m["id"] for m in ub.fetch_catalog()] == ["anthropic/claude-sonnet-5", "z-ai/glm-5.3-flash"]
    request = fake_urlopen["seen"][0]
    assert request.full_url == ub.CATALOG_URL == f"{UBERLLM_URL}/models"
    assert not request.has_header("Authorization")


def test_fetch_catalog_errors(fake_urlopen):
    fake_urlopen["payload"] = {"data": "nope"}
    with pytest.raises(ub.UberLLMCatalogError, match="shape"):
        ub.fetch_catalog()
    fake_urlopen["payload"] = {"data": [_raw("vendor/offline", available=False)]}
    with pytest.raises(ub.UberLLMCatalogError, match="no chat models"):
        ub.fetch_catalog()
    fake_urlopen["payload"] = urllib.error.HTTPError(ub.CATALOG_URL, 503, "unavailable", {}, io.BytesIO(b""))
    with pytest.raises(ub.UberLLMCatalogError, match="HTTP 503"):
        ub.fetch_catalog()


def test_get_catalog_caches_and_degrades(fake_urlopen, monkeypatch):
    fake_urlopen["payload"] = {"data": [_raw("deepseek/deepseek-v4-flash-0731", display_name="DeepSeek V4 Flash")]}
    first = ub.get_catalog()
    assert [m["name"] for m in first["models"]] == ["DeepSeek V4 Flash"]
    assert first["stale"] is False and first["error"] is None
    assert ub.get_catalog()["models"] == first["models"] and len(fake_urlopen["seen"]) == 1
    assert ub._read_cache()["models"] == first["models"]
    monkeypatch.setattr(ub, "CATALOG_TTL_SECONDS", 0)
    fake_urlopen["payload"] = urllib.error.URLError("offline")
    degraded = ub.get_catalog()
    assert degraded["models"] == first["models"]
    assert degraded["stale"] is True and "offline" in degraded["error"]
    ub.UBERLLM_CATALOG_FILE.unlink()
    assert ub.get_catalog() == {"fetched_at": None, "models": [], "stale": True, "error": "<urlopen error offline>"}


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


def test_admin_instance_catalog_for_uberllm(admin, monkeypatch):
    calls: list[bool] = []

    def fake_get_catalog(refresh=False):
        calls.append(refresh)
        return {
            "models": [
                {"id": "deepseek/deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash", "context_length": 1048576,
                 "max_completion_tokens": None, "pricing": None, "detail": "", "capabilities": ["tools"]},
                {"id": "moonshotai/kimi-k3", "name": "Kimi K3", "context_length": 262144,
                 "max_completion_tokens": None, "pricing": None, "detail": "", "capabilities": ["tools"]},
            ],
            "fetched_at": time.time(), "stale": False, "error": None,
        }

    monkeypatch.setattr(ub, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_uberllm())
    # No key yet: the public list is still served
    result = _run(admin.admin_instance_catalog("uberllm", q="kimi", user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == ["moonshotai/kimi-k3"] and calls == [False]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("uberllm", user={"id": 2, "email": "x@y", "is_admin": False}))
    assert exc_info.value.status_code == 403
