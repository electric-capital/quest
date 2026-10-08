"""Tests for Fireworks AI inference provider instances (kind ``fireworks``).

Covers the registry entry and credential-file bootstrap in
``config/inference_providers.py``, the fixed-upstream endpoint resolution
of ``OpenRouterProvider`` (Fireworks base URL, bearer key, no OpenRouter
extras), model resolution / backend label / pricing through the shared
instance machinery, the catalog module ``chat/llm/fireworks_catalog.py``
(normalization of the ``accounts/fireworks/models`` list shape incl. SKU
pricing, pagination, caching and error degradation against a stubbed
``urllib``), and the admin endpoint behaviour (kinds listing, per-instance
catalog route, snapshots on add). No network: every HTTP call is stubbed.
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

import chat.llm.fireworks_catalog as fw
import config.inference_providers as ip
from chat.llm.openrouter_provider import OPENROUTER_BASE_URL, OpenRouterProvider

ADMIN_USER = {"id": 1, "email": "admin@example.com", "is_admin": True}
FIREWORKS_URL = "https://api.fireworks.ai/inference/v1"


def _run(coro):
    return asyncio.run(coro)


def _fireworks(instance_id="fireworks", models=()):
    return {"id": instance_id, "kind": "fireworks", "label": "Fireworks", "models": list(models)}


def _money(dollars: float) -> dict:
    units = int(dollars)
    return {"currencyCode": "USD", "units": str(units), "nanos": round((dollars - units) * 1_000_000_000)}


def _raw_model(name, *, display=None, context=131072, tools=True, image=False, state="READY",
               serverless=True, prices=(0.9, 0.09, 0.9), deprecation=None, unit="1M tokens"):
    """One entry shaped like ``GET /v1/accounts/fireworks/models``."""
    entry = {
        "name": name,
        "displayName": display,
        "state": state,
        "kind": "HF_BASE_MODEL",
        "public": True,
        "contextLength": context,
        "supportsTools": tools,
        "supportsImageInput": image,
        "supportsServerless": serverless,
        "conversationConfig": {"style": "chatml"},
    }
    if deprecation:
        entry["deprecationDate"] = deprecation
    if serverless and prices:
        prompt, cached, completion = prices
        entry["serverlessModes"] = [{
            "name": f"{name}/serverlessModes/default",
            "skuInfos": [
                {"sku": "LLM input tokens (uncached)", "amount": _money(prompt), "unit": unit},
                {"sku": "LLM input tokens (cached)", "amount": _money(cached), "unit": unit},
                {"sku": "LLM output tokens", "amount": _money(completion), "unit": unit},
                {"sku": "Something per hour", "amount": _money(5), "unit": "hour"},
            ],
        }]
    return entry


# ---------------------------------------------------------------------------
# Registry + config store
# ---------------------------------------------------------------------------

def test_fireworks_kind_registry_entry():
    spec = ip.INSTANCE_KINDS["fireworks"]
    assert spec["provider"] == "openrouter" and spec["backend"] == "fireworks"
    assert spec["key_required"] is True and spec["endpoint"] is False
    assert spec["upstream_url"] == FIREWORKS_URL and spec["catalog"] == "fireworks"
    assert ip.is_endpoint_kind("fireworks") is False


def test_fireworks_instance_normalization_and_readiness():
    stored = ip.upsert_instance({
        "id": "fireworks", "kind": "fireworks", "label": "", "base_url": "http://x",
        "models": ["accounts/fireworks/models/deepseek-v3p1"],
    })
    assert stored["label"] == "Fireworks AI"
    # Fixed-upstream kinds never carry endpoint fields
    assert "base_url" not in stored and "api_type" not in stored
    assert stored["models"][0]["id"] == "accounts/fireworks/models/deepseek-v3p1"
    # Needs its key
    assert ip.instance_configured(ip.get_instance("fireworks")) is False
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    assert ip.instance_configured(ip.get_instance("fireworks")) is True


def test_new_fireworks_instance_ids():
    assert ip.new_instance_id("fireworks") == "fireworks"
    ip.upsert_instance(_fireworks())
    assert ip.new_instance_id("fireworks") == "fireworks-2"
    assert ip.new_instance_id("openrouter") == "openrouter"


def test_kind_for_credential_file():
    assert ip.kind_for_credential_file("fireworks") == "fireworks"
    assert ip.kind_for_credential_file("fireworks-2") == "fireworks"
    assert ip.kind_for_credential_file("openrouter") == "openrouter"
    assert ip.kind_for_credential_file("openrouter-3") == "openrouter"
    # Self-hosted instances have no key-only bootstrap; unknown stems stay OpenRouter
    assert ip.kind_for_credential_file("local") == "openrouter"
    assert ip.kind_for_credential_file("team-key") == "openrouter"
    assert ip.kind_for_credential_file("fireworksy") == "openrouter"


def test_prebaked_fireworks_credential_file_bootstraps_fireworks_instance():
    """A dev-config ``inference_credentials: {"fireworks": ...}`` entry (or a
    hand-placed key file) must come up as a Fireworks instance, not an
    OpenRouter one."""
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    ip.write_inference_credentials("fireworks-2", {"api_key": "fw_other"})
    instances = {inst["id"]: inst for inst in ip.list_instances()}
    assert instances["fireworks"]["kind"] == "fireworks"
    assert instances["fireworks"]["label"] == "Fireworks AI"
    assert instances["fireworks"]["models"] == []
    assert instances["fireworks-2"]["kind"] == "fireworks"
    assert instances["fireworks-2"]["label"] == "Fireworks AI (fireworks-2)"
    # Persisted, and the unchanged OpenRouter legacy seeding still applies
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or"})
    instances = {inst["id"]: inst for inst in ip.list_instances()}
    assert instances["openrouter"]["kind"] == "openrouter"
    assert instances["openrouter"]["models"]


# ---------------------------------------------------------------------------
# Model resolution, backend label, pricing
# ---------------------------------------------------------------------------

def test_resolve_fireworks_model():
    from chat.llm.config import (
        get_backend_for_model,
        get_configured_models,
        get_provider_for_model,
        resolve_model,
    )

    ip.upsert_instance(_fireworks(models=[{
        "id": "accounts/fireworks/models/deepseek-v3p1", "enabled": True, "name": "DeepSeek V3.1",
        "context_length": 163840, "max_completion_tokens": None,
        "pricing": {"prompt": 0.56, "completion": 1.68, "cache_read": 0.056},
    }]))
    model_id = "fireworks:accounts/fireworks/models/deepseek-v3p1"
    spec = resolve_model(model_id)
    assert spec is not None
    assert (spec.instance_id, spec.wire_id) == ("fireworks", "accounts/fireworks/models/deepseek-v3p1")
    assert spec.provider == "openrouter" and spec.backend == "fireworks"
    assert spec.display_name == "DeepSeek V3.1" and spec.max_input_tokens == 163840
    assert get_provider_for_model(model_id) == "openrouter"
    assert get_backend_for_model(model_id) == "fireworks"
    # A Fireworks wire id contains "/" like OpenRouter's, but stored
    # qualified it never falls into the bare-legacy-id fallback
    assert ip.split_model_id(model_id) == ("fireworks", "accounts/fireworks/models/deepseek-v3p1")
    assert ip.canonical_model_id(model_id) == model_id

    # Configured only once the key is stored
    assert model_id not in get_configured_models()
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    assert model_id in get_configured_models()


def test_fireworks_pricing_uses_instance_snapshot():
    from db.llm_pricing import estimate_cost_usd

    ip.upsert_instance(_fireworks(models=[{
        "id": "accounts/fireworks/models/deepseek-v3p1", "enabled": True,
        "pricing": {"prompt": 1.0, "completion": 2.0, "cache_read": 0.1},
    }]))
    cost = estimate_cost_usd(
        "openrouter", "fireworks:accounts/fireworks/models/deepseek-v3p1",
        {"prompt_tokens": 1_000_000, "cached_prompt_tokens": 500_000, "completion_tokens": 1_000_000},
    )
    assert cost == pytest.approx(0.5 * 1.0 + 0.5 * 0.1 + 2.0)
    # A custom id with no snapshot has no estimate
    assert estimate_cost_usd(
        "openrouter", "fireworks:accounts/me/deployments/abc", {"prompt_tokens": 10},
    ) is None


# ---------------------------------------------------------------------------
# Provider endpoint resolution
# ---------------------------------------------------------------------------

def test_openrouter_provider_endpoint_for_fireworks_instance():
    ip.upsert_instance(_fireworks())
    provider = OpenRouterProvider("fireworks")
    with pytest.raises(ValueError, match="Fireworks AI API key not configured"):
        provider._endpoint()
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    endpoint = provider._endpoint()
    assert endpoint == {
        "base_url": FIREWORKS_URL,
        "api_key": "fw_secret",
        "headers": None,
        "openrouter": False,
    }
    # The client is built on the Fireworks URL and the OpenRouter-only
    # request extras are switched off
    client = provider._get_client()
    assert str(client.base_url).rstrip("/") == FIREWORKS_URL
    assert provider._is_openrouter is False


def test_openrouter_provider_endpoint_unchanged_for_openrouter_instance():
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": []})
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or-secret"})
    endpoint = OpenRouterProvider("openrouter")._endpoint()
    assert endpoint["base_url"] == OPENROUTER_BASE_URL and endpoint["openrouter"] is True
    assert endpoint["headers"] == {"X-Title": "Quest"}


def test_provider_singleton_per_fireworks_instance(monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(llm_config, "_provider_instances", {})
    ip.upsert_instance(_fireworks())
    provider = llm_config.get_provider_instance("openrouter", "fireworks")
    assert isinstance(provider, OpenRouterProvider) and provider.instance_id == "fireworks"
    assert provider is llm_config.get_provider_instance("openrouter", "fireworks")
    assert provider is not llm_config.get_provider_instance("openrouter", None)


# ---------------------------------------------------------------------------
# Catalog module
# ---------------------------------------------------------------------------

def test_normalize_entry_shape_and_pricing():
    raw = _raw_model(
        "accounts/fireworks/models/deepseek-v3p1", display="DeepSeek V3.1",
        context=163840, tools=True, image=True, prices=(0.56, 0.056, 1.68),
    )
    entry = fw._normalize_entry(raw)
    assert entry == {
        "id": "accounts/fireworks/models/deepseek-v3p1",
        "name": "DeepSeek V3.1",
        "context_length": 163840,
        "max_completion_tokens": None,
        "pricing": {"prompt": pytest.approx(0.56), "completion": pytest.approx(1.68),
                    "cache_read": pytest.approx(0.056)},
        "detail": "vision",
        "capabilities": ["tools", "vision"],
    }
    # No display name: the last path segment; no tools: flagged by the typeahead
    plain = fw._normalize_entry(_raw_model("accounts/fireworks/models/x-7b", tools=False, prices=None))
    assert plain["name"] == "x-7b" and plain["capabilities"] == [] and plain["pricing"] is None
    # Per-1K SKUs scale to $/1M; an unknown unit yields no pricing
    per_k = fw._normalize_entry(_raw_model("accounts/fireworks/models/k", prices=(0.001, 0.0001, 0.002), unit="1K tokens"))
    assert per_k["pricing"] == {"prompt": pytest.approx(1.0), "completion": pytest.approx(2.0),
                                "cache_read": pytest.approx(0.1)}
    odd = fw._normalize_entry(_raw_model("accounts/fireworks/models/o", unit="widget"))
    assert odd["pricing"] is None


def test_normalize_entry_filters_unusable_models():
    assert fw._normalize_entry("nope") is None
    assert fw._normalize_entry({"displayName": "no name"}) is None
    assert fw._normalize_entry(_raw_model("accounts/fireworks/models/up", state="UPLOADING")) is None
    assert fw._normalize_entry(_raw_model("accounts/fireworks/models/ded", serverless=False)) is None
    # supportsServerless missing but a serverless mode present still counts
    raw = _raw_model("accounts/fireworks/models/m")
    del raw["supportsServerless"]
    assert fw._normalize_entry(raw) is not None
    import datetime
    today = datetime.date(2026, 10, 8)
    past = {"year": 2026, "month": 10, "day": 8}
    future = {"year": 2027, "month": 1, "day": 1}
    assert fw._normalize_entry(_raw_model("accounts/fireworks/models/old", deprecation=past), today) is None
    assert fw._normalize_entry(_raw_model("accounts/fireworks/models/new", deprecation=future), today) is not None
    assert fw._normalize_entry(_raw_model("accounts/fireworks/models/bad", deprecation={"year": "x"}), today) is not None


@pytest.fixture
def fake_urlopen(monkeypatch):
    """Stub ``urllib.request.urlopen`` with canned page responses keyed by
    ``pageToken``; records every request (URL + headers)."""
    pages: dict[str | None, object] = {}
    seen: list[urllib.request.Request] = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def urlopen(request, timeout=None):
        seen.append(request)
        from urllib.parse import parse_qs, urlparse
        token = parse_qs(urlparse(request.full_url).query).get("pageToken", [None])[0]
        target = pages.get(token)
        if isinstance(target, Exception):
            raise target
        if target is None:
            raise AssertionError(f"unexpected page token {token!r}")
        return _Response(json.dumps(target).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return pages, seen


def test_fetch_catalog_paginates_with_bearer_key(fake_urlopen):
    pages, seen = fake_urlopen
    pages[None] = {
        "models": [_raw_model("accounts/fireworks/models/b"), _raw_model("accounts/fireworks/models/a")],
        "nextPageToken": "p2", "totalSize": 3,
    }
    pages["p2"] = {
        "models": [_raw_model("accounts/fireworks/models/a"), _raw_model("accounts/fireworks/models/c")],
        "nextPageToken": "",
    }
    models = fw.fetch_catalog("fw_secret")
    assert [m["id"] for m in models] == [
        "accounts/fireworks/models/a", "accounts/fireworks/models/b", "accounts/fireworks/models/c",
    ]
    assert len(seen) == 2
    for request in seen:
        assert request.full_url.startswith(fw.CATALOG_URL + "?")
        assert "pageSize=200" in request.full_url
        assert request.get_header("Authorization") == "Bearer fw_secret"
    assert "pageToken=p2" in seen[1].full_url


def test_fetch_catalog_errors(fake_urlopen):
    pages, _seen = fake_urlopen
    with pytest.raises(fw.FireworksCatalogError, match="No API key"):
        fw.fetch_catalog("")
    body = json.dumps({"error": {"message": "You must provide an API key.", "code": "UNAUTHORIZED"}}).encode()
    pages[None] = urllib.error.HTTPError(fw.CATALOG_URL, 401, "Unauthorized", {}, io.BytesIO(body))
    with pytest.raises(fw.FireworksCatalogError, match="HTTP 401: You must provide an API key."):
        fw.fetch_catalog("bad")
    pages[None] = {"data": []}
    with pytest.raises(fw.FireworksCatalogError, match="Unexpected catalog response shape"):
        fw.fetch_catalog("k")
    pages[None] = {"models": [_raw_model("accounts/fireworks/models/x", serverless=False)]}
    with pytest.raises(fw.FireworksCatalogError, match="no serverless models"):
        fw.fetch_catalog("k")
    pages[None] = urllib.error.URLError("dns down")
    with pytest.raises(fw.FireworksCatalogError, match="dns down"):
        fw.fetch_catalog("k")


def test_get_catalog_caches_and_degrades(fake_urlopen, monkeypatch):
    pages, seen = fake_urlopen
    pages[None] = {"models": [_raw_model("accounts/fireworks/models/a", display="A")]}
    first = fw.get_catalog("fw_secret")
    assert first["stale"] is False and first["error"] is None
    assert [m["id"] for m in first["models"]] == ["accounts/fireworks/models/a"]
    assert fw.FIREWORKS_CATALOG_FILE.exists()
    # Fresh cache: served without a fetch, even without a key
    pages[None] = AssertionError("must not fetch")
    cached = fw.get_catalog(None)
    assert cached["models"] == first["models"] and cached["stale"] is False
    assert len(seen) == 1
    # Expired cache + failing fetch: stale cache with the error alongside
    monkeypatch.setattr(fw, "CATALOG_TTL_SECONDS", 0)
    pages[None] = urllib.error.URLError("offline")
    stale = fw.get_catalog("fw_secret")
    assert stale["stale"] is True and "offline" in stale["error"]
    assert [m["id"] for m in stale["models"]] == ["accounts/fireworks/models/a"]
    # No key and no usable cache: empty with the explanation
    fw.FIREWORKS_CATALOG_FILE.unlink()
    empty = fw.get_catalog(None)
    assert empty == {"fetched_at": None, "models": [], "stale": True, "error": "No API key saved yet."}
    # ``refresh`` re-fetches a fresh cache
    monkeypatch.setattr(fw, "CATALOG_TTL_SECONDS", 3600)
    pages[None] = {"models": [_raw_model("accounts/fireworks/models/b")]}
    fw.get_catalog("fw_secret")
    pages[None] = {"models": [_raw_model("accounts/fireworks/models/c")]}
    assert [m["id"] for m in fw.get_catalog("fw_secret")["models"]] == ["accounts/fireworks/models/b"]
    assert [m["id"] for m in fw.get_catalog("fw_secret", refresh=True)["models"]] == ["accounts/fireworks/models/c"]


def test_cache_round_trip_keeps_shape(fake_urlopen):
    pages, _seen = fake_urlopen
    pages[None] = {"models": [_raw_model("accounts/fireworks/models/a", display="A", image=True)]}
    fetched = fw.get_catalog("k")["models"]
    cached = fw._read_cache()["models"]
    assert cached == fetched
    # Malformed cache entries are dropped, a bad file ignored
    fw._write_cache({"fetched_at": time.time(), "models": [{"id": "", "name": "x"}, "junk"]})
    assert fw._read_cache()["models"] == []
    fw.FIREWORKS_CATALOG_FILE.write_text("{not json")
    assert fw._read_cache() is None


def test_search_and_snapshot_apply_to_fireworks_entries():
    from chat.llm.openrouter_catalog import catalog_snapshot, search_catalog

    models = [
        fw._normalize_entry(_raw_model("accounts/fireworks/models/llama-v3p1-8b-instruct", display="Llama 3.1 8B")),
        fw._normalize_entry(_raw_model("accounts/fireworks/models/deepseek-v3p1", display="DeepSeek V3.1")),
    ]
    assert [m["id"] for m in search_catalog(models, "deepseek")] == ["accounts/fireworks/models/deepseek-v3p1"]
    assert [m["id"] for m in search_catalog(models, "llama 3.1")] == ["accounts/fireworks/models/llama-v3p1-8b-instruct"]
    snapshot = catalog_snapshot(models, "accounts/fireworks/models/deepseek-v3p1")
    assert snapshot["name"] == "DeepSeek V3.1" and snapshot["context_length"] == 131072
    assert snapshot["pricing"]["prompt"] == pytest.approx(0.9)
    assert catalog_snapshot(models, "accounts/me/deployments/abc") is None


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


def test_admin_create_and_list_fireworks_instance(admin):
    created = _run(admin.admin_create_inference_instance(
        admin.InstanceCreate(kind="fireworks", label="Team Fireworks"), user=ADMIN_USER,
    ))
    assert created["id"] == "fireworks" and created["kind"] == "fireworks"
    assert created["kind_label"] == "Fireworks AI" and created["label"] == "Team Fireworks"
    assert created["configured"] is False and created["key_required"] is True
    assert created["hint"] == "fw_..." and created["base_url"] is None and created["api_type"] is None
    listing = _run(admin.admin_list_inference_providers(user=ADMIN_USER))
    assert [i["id"] for i in listing["instances"]] == ["fireworks"]
    kinds = {k["kind"]: k for k in listing["kinds"]}
    assert kinds["fireworks"] == {"kind": "fireworks", "label": "Fireworks AI", "endpoint": False, "catalog": "fireworks"}


def test_admin_update_fireworks_rejects_endpoint_fields_and_requires_key(admin):
    ip.upsert_instance(_fireworks())
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "fireworks", admin.InstanceUpdate(base_url="http://x"), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    assert "fixed endpoint" in exc_info.value.detail["message"]
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "fireworks", admin.InstanceUpdate(api_key=""), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    status = _run(admin.admin_update_inference_instance(
        "fireworks", admin.InstanceUpdate(api_key="fw_secret"), user=ADMIN_USER,
    ))
    assert status["configured"] is True and status["credentials"]["api_key_set"] is True
    assert "fw_secret" not in json.dumps(status)
    assert ip.effective_api_key("fireworks") == ("fw_secret", "store")


def test_admin_update_fireworks_models_snapshot_from_catalog(admin, monkeypatch):
    calls: list[tuple] = []

    def fake_get_catalog(api_key, refresh=False):
        calls.append((api_key, refresh))
        return {
            "models": [fw._normalize_entry(_raw_model(
                "accounts/fireworks/models/deepseek-v3p1", display="DeepSeek V3.1",
                context=163840, prices=(0.56, 0.056, 1.68),
            ))],
            "fetched_at": time.time(), "stale": False, "error": None,
        }

    monkeypatch.setattr(fw, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_fireworks())
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    body = admin.InstanceUpdate(models=[
        admin.InstanceModelUpdate(id="accounts/fireworks/models/deepseek-v3p1"),
        admin.InstanceModelUpdate(id="accounts/me/deployments/abc", name="My deployment"),
    ])
    status = _run(admin.admin_update_inference_instance("fireworks", body, user=ADMIN_USER))
    assert calls == [("fw_secret", False)]
    rows = {m["wire_id"]: m for m in status["models"]}
    assert rows["accounts/fireworks/models/deepseek-v3p1"]["display_name"] == "DeepSeek V3.1"
    assert rows["accounts/fireworks/models/deepseek-v3p1"]["max_input_tokens"] == 163840
    assert rows["accounts/fireworks/models/deepseek-v3p1"]["id"] == "fireworks:accounts/fireworks/models/deepseek-v3p1"
    # The custom id keeps conservative defaults, the admin's name, no pricing
    assert rows["accounts/me/deployments/abc"]["display_name"] == "My deployment"
    assert rows["accounts/me/deployments/abc"]["max_input_tokens"] == ip.DEFAULT_INSTANCE_CONTEXT_LENGTH
    stored = {m["id"]: m for m in ip.get_instance("fireworks")["models"]}
    assert stored["accounts/fireworks/models/deepseek-v3p1"]["pricing"] == {
        "prompt": pytest.approx(0.56), "completion": pytest.approx(1.68), "cache_read": pytest.approx(0.056),
    }
    assert stored["accounts/me/deployments/abc"]["pricing"] is None
    assert ip.instance_model_pricing("accounts/fireworks/models/deepseek-v3p1")["completion"] == pytest.approx(1.68)


def test_admin_instance_catalog_for_fireworks(admin, monkeypatch):
    calls: list[tuple] = []

    def fake_get_catalog(api_key, refresh=False):
        calls.append((api_key, refresh))
        return {
            "models": [
                fw._normalize_entry(_raw_model("accounts/fireworks/models/deepseek-v3p1", display="DeepSeek")),
                fw._normalize_entry(_raw_model("accounts/fireworks/models/llama-v3p1-8b-instruct", display="Llama")),
            ],
            "fetched_at": time.time(), "stale": True, "error": "HTTP 401: nope",
        }

    monkeypatch.setattr(fw, "get_catalog", fake_get_catalog)
    ip.upsert_instance(_fireworks())
    # No key yet: no fetch attempted
    result = _run(admin.admin_instance_catalog("fireworks", user=ADMIN_USER))
    assert result == {"models": [], "error": "No API key saved yet."} and calls == []
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    result = _run(admin.admin_instance_catalog("fireworks", q="llama", refresh=True, user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == ["accounts/fireworks/models/llama-v3p1-8b-instruct"]
    assert result["error"] == "HTTP 401: nope"
    assert calls == [("fw_secret", True)]
    # OpenRouter instances still use the shared catalog route
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": []})
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("openrouter", user=ADMIN_USER))
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("fireworks", user={"id": 2, "email": "x@y", "is_admin": False}))
    assert exc_info.value.status_code == 403


def test_admin_delete_fireworks_instance_removes_key_file(admin):
    ip.upsert_instance(_fireworks(models=["accounts/fireworks/models/a"]))
    ip.write_inference_credentials("fireworks", {"api_key": "fw_secret"})
    assert _run(admin.admin_delete_inference_instance("fireworks", user=ADMIN_USER)) == {"success": True}
    assert ip.get_instance("fireworks") is None
    assert ip.read_inference_credentials("fireworks") is None
    # Nothing to resurrect the instance from
    assert ip.list_instances() == []
