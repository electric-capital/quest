"""Tests for the direct vendor-API provider instance kinds -- ``anthropic``
(the Claude API), ``openai`` and ``gemini`` (the Gemini Developer API) --
and the first-run quick setup behind the composer's model setup card.

Covers the registry entries and credential-file bootstrap in
``config/inference_providers.py``, model resolution through the shared
instance machinery (provider / backend / thinking levels from the snapshot),
the per-instance provider objects (``AnthropicApiProvider`` /
``GeminiApiProvider`` built from the stored key; the OpenAI request shape of
``OpenRouterProvider``), the three catalog modules against a stubbed
``urllib`` (normalization, registry enrichment, pagination, caching and
error degradation), the pricing of qualified Claude / Gemini rows in
``db/llm_pricing.py``, and the admin endpoints (catalog route, snapshots on
add, ``POST /admin/inference-providers/quick-setup``). No network: every
HTTP call is stubbed.
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

import chat.llm.anthropic_catalog as ac
import chat.llm.gemini_catalog as gc
import chat.llm.openai_catalog as oc
import config.inference_providers as ip
from chat.llm.catalog_cache import CatalogError
from chat.llm.config import (
    get_provider_instance,
    reasoning_from_registry_entry,
    registry_entry_for_wire_id,
    resolve_model,
)
from chat.llm.openrouter_provider import OpenRouterProvider

ADMIN_USER = {"id": 1, "email": "admin@example.com", "is_admin": True}


def _run(coro):
    return asyncio.run(coro)


def _instance(kind, instance_id=None, models=()):
    return {"id": instance_id or kind, "kind": kind, "label": "", "models": list(models)}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _stub_http(monkeypatch, handler):
    """Route ``urllib.request.urlopen`` to ``handler(url, headers) -> payload``;
    the handler may raise ``urllib.error.HTTPError``. Records every request."""
    calls: list[tuple[str, dict]] = []

    def fake_urlopen(request, timeout=None):
        headers = {k.lower(): v for k, v in request.header_items()}
        calls.append((request.full_url, headers))
        payload = handler(request.full_url, headers)
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _http_error(code, message):
    body = json.dumps({"error": {"type": "authentication_error", "message": message}}).encode()
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body))


# ---------------------------------------------------------------------------
# Registry + config store
# ---------------------------------------------------------------------------

def test_direct_kinds_registry_entries():
    anthropic = ip.INSTANCE_KINDS["anthropic"]
    assert anthropic["provider"] == "anthropic" and anthropic["backend"] == "anthropic"
    assert anthropic["upstream_url"] == "https://api.anthropic.com" and anthropic["catalog"] == "anthropic"
    openai = ip.INSTANCE_KINDS["openai"]
    assert openai["provider"] == "openrouter" and openai["backend"] == "openai"
    assert openai["upstream_url"] == "https://api.openai.com/v1" and openai["catalog"] == "openai"
    gemini = ip.INSTANCE_KINDS["gemini"]
    assert gemini["provider"] == "gemini" and gemini["backend"] == "gemini" and gemini["catalog"] == "gemini"
    for kind in ("anthropic", "openai", "gemini"):
        assert ip.INSTANCE_KINDS[kind]["key_required"] is True
        assert ip.is_endpoint_kind(kind) is False
    # Display order: the direct kinds lead
    assert list(ip.INSTANCE_KINDS)[:3] == ["anthropic", "openai", "gemini"]


def test_kind_for_credential_file_recognizes_direct_kinds():
    assert ip.kind_for_credential_file("anthropic") == "anthropic"
    assert ip.kind_for_credential_file("anthropic-2") == "anthropic"
    assert ip.kind_for_credential_file("openai") == "openai"
    assert ip.kind_for_credential_file("gemini-3") == "gemini"


def test_prebaked_anthropic_credential_file_bootstraps_an_anthropic_instance():
    ip.write_inference_credentials(
        "anthropic", {"api_key": "sk-ant-secret", "models": ["claude-sonnet-5"]},
    )
    config = ip.load_inference_config()
    inst = {i["id"]: i for i in config["instances"]}["anthropic"]
    assert inst["kind"] == "anthropic" and inst["label"] == "Anthropic"
    assert [m["id"] for m in inst["models"]] == ["claude-sonnet-5"]
    assert ip.instance_configured(inst) is True


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def test_registry_entry_for_wire_id_matches_aliases_and_dated_ids():
    assert registry_entry_for_wire_id("anthropic", "claude-sonnet-5")[0] == "claude-sonnet-5"
    assert registry_entry_for_wire_id("anthropic", "claude-haiku-4-5-20251001")[0] == "claude-haiku-4.5"
    assert registry_entry_for_wire_id("anthropic", "claude-haiku-4.5")[0] == "claude-haiku-4.5"
    assert registry_entry_for_wire_id("gemini", "gemini-3.8-flash")[0] == "gemini-3.8-flash"
    # Provider-scoped
    assert registry_entry_for_wire_id("gemini", "claude-sonnet-5") is None
    assert registry_entry_for_wire_id("anthropic", "claude-future-9") is None


def test_reasoning_from_registry_entry_round_trips_the_thinking_levels():
    from chat.llm.config import MODEL_REGISTRY, thinking_efforts_from_reasoning

    opus = MODEL_REGISTRY["claude-opus-4-8"]
    reasoning = reasoning_from_registry_entry(opus)
    assert reasoning["mandatory"] is False and reasoning["default_effort"] == "medium"
    levels, default = thinking_efforts_from_reasoning(reasoning)
    assert levels == ("none", "low", "medium", "high", "xhigh", "max") and default == "medium"
    sonnet = MODEL_REGISTRY["claude-sonnet-5-5"]
    levels, default = thinking_efforts_from_reasoning(reasoning_from_registry_entry(sonnet))
    assert levels == ("low", "medium", "high", "xhigh", "max") and default == "high"
    assert reasoning_from_registry_entry(MODEL_REGISTRY["claude-haiku-4.5"]) is None


def test_direct_instance_models_resolve_to_their_vendor_provider():
    ip.upsert_instance(_instance("anthropic", models=[{
        "id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5",
        "context_length": 1_000_000, "max_completion_tokens": 128_000,
        "reasoning": {"supported_efforts": ["low", "medium", "high"], "mandatory": True, "default_effort": "high"},
    }]))
    ip.upsert_instance(_instance("gemini", models=["gemini-3.8-flash"]))
    ip.upsert_instance(_instance("openai", models=["gpt-5"]))

    spec = resolve_model("anthropic:claude-sonnet-5-5")
    assert spec.provider == "anthropic" and spec.backend == "anthropic"
    assert spec.wire_id == "claude-sonnet-5-5" and spec.instance_id == "anthropic"
    assert spec.max_output_tokens == 64_000  # capped at MAX_INSTANCE_OUTPUT_TOKENS
    assert spec.thinking_efforts == ("low", "medium", "high") and spec.thinking_effort == "high"
    assert spec.refusal_fallback_models == ()
    assert spec.family is None

    gemini = resolve_model("gemini:gemini-3.8-flash")
    assert gemini.provider == "gemini" and gemini.backend == "gemini"
    openai = resolve_model("openai:gpt-5")
    assert openai.provider == "openrouter" and openai.backend == "openai"


def test_direct_instance_models_count_as_configured_only_with_a_key(monkeypatch):
    from chat.llm.config import get_configured_models

    ip.upsert_instance(_instance("anthropic", models=["claude-sonnet-5"]))
    assert "anthropic:claude-sonnet-5" not in get_configured_models()
    ip.write_inference_credentials("anthropic", {"api_key": "sk-ant-secret"})
    assert "anthropic:claude-sonnet-5" in get_configured_models()


# ---------------------------------------------------------------------------
# Provider objects
# ---------------------------------------------------------------------------

def test_provider_factory_builds_api_providers_per_instance(monkeypatch):
    import chat.llm.config as llm_config
    from chat.llm.anthropic_provider import AnthropicApiProvider, AnthropicProvider
    from chat.llm.gemini_provider import GeminiApiProvider, GeminiProvider

    monkeypatch.setattr(llm_config, "_provider_instances", {})
    vertex = get_provider_instance("anthropic")
    assert type(vertex) is AnthropicProvider
    direct = get_provider_instance("anthropic", "anthropic")
    assert isinstance(direct, AnthropicApiProvider) and direct.instance_id == "anthropic"
    assert get_provider_instance("anthropic", "anthropic") is direct
    assert get_provider_instance("anthropic") is vertex
    assert type(get_provider_instance("gemini")) is GeminiProvider
    assert isinstance(get_provider_instance("gemini", "gemini-2"), GeminiApiProvider)


def test_anthropic_api_provider_client_uses_the_stored_key(monkeypatch):
    from chat.llm.anthropic_provider import AnthropicApiProvider

    ip.upsert_instance(_instance("anthropic", models=["claude-sonnet-5"]))
    provider = AnthropicApiProvider("anthropic")
    with pytest.raises(ValueError, match="Anthropic API key not configured"):
        provider._get_client("us-east5")
    ip.write_inference_credentials("anthropic", {"api_key": "sk-ant-secret"})
    client = provider._get_client("us-east5", ("claude-opus-4-8",))
    assert client.api_key == "sk-ant-secret"
    assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
    # One client regardless of region / chain; reset drops it
    assert provider._get_client("global") is client
    provider.reset_cached_clients()
    assert provider._get_client("global") is not client
    # The wire id and the output cap come from the instance snapshot
    assert provider._get_vertex_model_id("anthropic:claude-sonnet-5") == "claude-sonnet-5"
    assert provider._get_fallback_chain("anthropic:claude-sonnet-5") == ()


def test_anthropic_vertex_provider_still_resolves_registry_models():
    from chat.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider()
    assert provider._get_vertex_model_id("claude-haiku-4.5") == "claude-haiku-4-5"
    assert provider._get_vertex_model_id("unknown-model") == "unknown-model"
    assert provider._get_fallback_chain("claude-opus-5") == ("claude-opus-4-8",)


def test_gemini_api_provider_client_uses_the_stored_key(monkeypatch):
    import chat.llm.gemini_provider as gp

    built = []

    class FakeClient:
        def __init__(self, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(gp.genai, "Client", FakeClient)
    ip.upsert_instance(_instance("gemini", models=["gemini-3.8-flash"]))
    provider = gp.GeminiApiProvider("gemini")
    with pytest.raises(ValueError, match="Gemini API key not configured"):
        provider._get_client()
    ip.write_inference_credentials("gemini", {"api_key": "AIza-secret"})
    client = provider._get_client()
    assert built == [{"api_key": "AIza-secret"}]
    assert provider._get_client() is client
    assert gp._model_vertex_id("gemini:gemini-3.8-flash") == "gemini-3.8-flash"
    assert gp._model_vertex_id("gemini-3.8-flash") == "gemini-3.8-flash"


def test_openai_instance_endpoint_and_request_shape(monkeypatch):
    ip.upsert_instance(_instance("openai", models=[{
        "id": "gpt-5", "name": "GPT-5", "context_length": 400_000, "max_completion_tokens": 128_000,
        "reasoning": {"supported_efforts": ["minimal", "low", "medium", "high"], "mandatory": True},
    }]))
    ip.write_inference_credentials("openai", {"api_key": "sk-openai"})
    provider = OpenRouterProvider("openai")
    endpoint = provider._endpoint()
    assert endpoint["base_url"] == "https://api.openai.com/v1" and endpoint["kind"] == "openai"
    assert endpoint["openrouter"] is False and endpoint["headers"] is None

    captured: dict = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)

            async def empty():
                if False:
                    yield None
            if kwargs.get("stream"):
                return empty()
            return None

    class FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    fake = FakeClient()
    def fake_get_client():
        # What the real _get_client() records from _endpoint() for this kind
        provider._kind = "openai"
        provider._is_openrouter = False
        return fake
    monkeypatch.setattr(provider, "_get_client", fake_get_client)
    # Health check asks for a few completion tokens, not ``max_tokens``
    _run(provider.check_model_access("openai:gpt-5"))
    assert captured["max_completion_tokens"] == 16 and "max_tokens" not in captured

    captured.clear()
    session = provider.create_session("openai:gpt-5", "sys", [], thinking_effort="high")

    async def drain():
        async for _ in provider.send_message_stream(session, "hi"):
            pass
    _run(drain())
    assert captured["model"] == "gpt-5"
    assert captured["max_completion_tokens"] == 64_000 and "max_tokens" not in captured
    assert captured["reasoning_effort"] == "high"
    assert "extra_body" not in captured  # no OpenRouter accounting opt-in, no ``reasoning`` object


# ---------------------------------------------------------------------------
# Catalogs
# ---------------------------------------------------------------------------

def test_anthropic_catalog_enriches_from_the_registry_and_paginates(monkeypatch):
    pages = {
        None: {"data": [
            {"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
            {"type": "model", "id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5"},
            {"type": "model", "id": "claude-opus-4-8", "display_name": "Claude Opus 4.8"},
        ], "has_more": True, "last_id": "claude-opus-4-8"},
        "claude-opus-4-8": {"data": [
            {"type": "model", "id": "claude-future-9", "display_name": "Claude Future 9"},
        ], "has_more": False, "last_id": "claude-future-9"},
    }

    def handler(url, headers):
        assert headers["x-api-key"] == "sk-ant-secret" and headers["anthropic-version"] == ac.API_VERSION
        after = url.split("after_id=")[1] if "after_id=" in url else None
        return pages[after]

    calls = _stub_http(monkeypatch, handler)
    result = ac.get_catalog("sk-ant-secret")
    assert result["error"] is None and len(calls) == 2
    rows = {m["id"]: m for m in result["models"]}
    sonnet = rows["claude-sonnet-5"]
    assert sonnet["registry_id"] == "claude-sonnet-5" and sonnet["context_length"] == 1_000_000
    assert sonnet["max_completion_tokens"] == 128_000
    assert sonnet["pricing"] == {"prompt": 2.0, "completion": 10.0, "cache_read": 0.2}
    assert sonnet["reasoning"] is None and sonnet["capabilities"] == ["tools", "vision"]
    haiku = rows["claude-haiku-4-5-20251001"]
    assert haiku["registry_id"] == "claude-haiku-4.5" and haiku["context_length"] == 200_000
    opus = rows["claude-opus-4-8"]
    assert opus["reasoning"]["default_effort"] == "medium" and opus["detail"] == "thinking"
    unknown = rows["claude-future-9"]
    assert "registry_id" not in unknown and unknown["pricing"] is None
    assert unknown["context_length"] == ac.DEFAULT_CONTEXT_LENGTH
    # Undated registry models only, one per registry entry
    assert ac.suggested_wire_ids(result["models"]) == ["claude-sonnet-5", "claude-opus-4-8"]

    # Served from the cache afterwards (incl. the registry_id), refetched on refresh
    again = ac.get_catalog("sk-ant-secret")
    assert len(calls) == 2 and again["models"][0]["registry_id"] == "claude-sonnet-5"
    ac.get_catalog("sk-ant-secret", refresh=True)
    assert len(calls) == 4


def test_anthropic_catalog_degrades_on_a_bad_key(monkeypatch):
    def handler(url, headers):
        raise _http_error(401, "invalid x-api-key")

    _stub_http(monkeypatch, handler)
    result = ac.get_catalog("sk-ant-bad")
    assert result["models"] == [] and result["stale"] is True
    assert result["error"] == "HTTP 401: invalid x-api-key"
    assert ac.get_catalog(None)["error"] == "No API key saved yet."


def test_gemini_catalog_keeps_chat_models_with_their_published_limits(monkeypatch):
    payload = {"models": [
        {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash",
         "inputTokenLimit": 1_048_576, "outputTokenLimit": 65_536,
         "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/gemini-9-preview", "displayName": "Gemini 9 Preview",
         "inputTokenLimit": 2_000_000, "outputTokenLimit": 100_000,
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "displayName": "Embedding",
         "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3.8-flash-live", "displayName": "Live",
         "supportedGenerationMethods": ["bidiGenerateContent", "generateContent"]},
        {"name": "models/imagen-4", "displayName": "Imagen", "supportedGenerationMethods": ["predict"]},
    ]}

    def handler(url, headers):
        assert headers["x-goog-api-key"] == "AIza-secret"
        return payload

    _stub_http(monkeypatch, handler)
    result = gc.get_catalog("AIza-secret")
    rows = {m["id"]: m for m in result["models"]}
    assert set(rows) == {"gemini-3.8-flash", "gemini-9-preview"}
    flash = rows["gemini-3.8-flash"]
    assert flash["registry_id"] == "gemini-3.8-flash"
    assert flash["context_length"] == 1_048_576 and flash["max_completion_tokens"] == 65_536
    assert flash["pricing"] == {"prompt": 1.5, "completion": 7.5, "cache_read": 0.15}
    assert flash["reasoning"]["supported_efforts"] == ["low", "medium", "high"]
    preview = rows["gemini-9-preview"]
    assert "registry_id" not in preview and preview["pricing"] is None
    assert preview["context_length"] == 2_000_000
    assert gc.suggested_wire_ids(result["models"]) == ["gemini-3.8-flash"]


def test_openai_catalog_filters_to_chat_models_and_enriches_known_families(monkeypatch):
    payload = {"object": "list", "data": [
        {"id": "gpt-5", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4.1-2025-04-14", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4.1", "object": "model", "owned_by": "openai"},
        {"id": "o4-mini", "object": "model", "owned_by": "openai"},
        {"id": "gpt-7-turbo", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4o-realtime-preview", "object": "model", "owned_by": "openai"},
        {"id": "gpt-4o-audio-preview", "object": "model", "owned_by": "openai"},
        {"id": "text-embedding-3-large", "object": "model", "owned_by": "openai"},
        {"id": "whisper-1", "object": "model", "owned_by": "openai"},
        {"id": "gpt-5-codex", "object": "model", "owned_by": "openai"},
        {"id": "dall-e-3", "object": "model", "owned_by": "openai"},
        {"id": "omni-moderation-latest", "object": "model", "owned_by": "openai"},
    ]}

    def handler(url, headers):
        assert headers["authorization"] == "Bearer sk-openai"
        return payload

    _stub_http(monkeypatch, handler)
    result = oc.get_catalog("sk-openai")
    ids = [m["id"] for m in result["models"]]
    # Known families first (undated before dated), unknown chat ids last
    assert ids == ["gpt-5", "gpt-4.1", "o4-mini", "gpt-4.1-2025-04-14", "gpt-7-turbo"]
    rows = {m["id"]: m for m in result["models"]}
    assert rows["gpt-5"]["pricing"] == {"prompt": 1.25, "completion": 10.0, "cache_read": 0.125}
    assert rows["gpt-5"]["reasoning"]["supported_efforts"] == ["minimal", "low", "medium", "high"]
    dated = rows["gpt-4.1-2025-04-14"]
    assert dated["name"] == "GPT-4.1 (2025-04-14)" and dated["context_length"] == 1_047_576
    assert dated["reasoning"] is None
    unknown = rows["gpt-7-turbo"]
    assert unknown["name"] == "GPT-7-turbo" and unknown["pricing"] is None
    assert unknown["context_length"] is None and unknown["capabilities"] == ["tools"]
    assert oc.suggested_wire_ids(result["models"]) == ["gpt-5", "gpt-4.1", "o4-mini"]


def test_openai_catalog_error_on_empty_list(monkeypatch):
    _stub_http(monkeypatch, lambda url, headers: {"object": "list", "data": [{"id": "whisper-1"}]})
    result = oc.get_catalog("sk-openai")
    assert result["models"] == [] and "no chat models" in result["error"]


# ---------------------------------------------------------------------------
# Pricing of qualified vendor rows
# ---------------------------------------------------------------------------

def test_claude_api_rows_price_from_the_snapshot_then_the_registry_table():
    from db.llm_pricing import estimate_cost_usd, long_context_threshold

    metrics = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    vertex = estimate_cost_usd("anthropic", "claude-sonnet-5", metrics)
    # Snapshotted at list price: identical to the Vertex row of the same model
    ip.upsert_instance(_instance("anthropic", models=[{
        "id": "claude-sonnet-5", "pricing": {"prompt": 2.0, "completion": 10.0, "cache_read": 0.2},
    }, {"id": "claude-haiku-5-5"}]))
    assert estimate_cost_usd("anthropic", "anthropic:claude-sonnet-5", metrics) == pytest.approx(vertex)
    # A custom snapshot wins (cache read as a multiplier of the input rate)
    ip.upsert_instance(_instance("anthropic", models=[{
        "id": "claude-sonnet-5", "pricing": {"prompt": 1.0, "completion": 5.0, "cache_read": 0.5},
    }, {"id": "claude-haiku-5-5"}]))
    assert estimate_cost_usd("anthropic", "anthropic:claude-sonnet-5", metrics) == pytest.approx(1.0 + 5.0 + 0.5)
    # No snapshot: the registry model the wire id names
    assert estimate_cost_usd("anthropic", "anthropic:claude-haiku-5-5", metrics) == pytest.approx(
        estimate_cost_usd("anthropic", "claude-haiku-5-5", metrics)
    )
    assert long_context_threshold("anthropic", "anthropic:claude-haiku-5-5") == 100_000
    assert estimate_cost_usd("anthropic", "anthropic:claude-future-9", metrics) is None


def test_gemini_api_rows_price_like_their_vertex_twin():
    from db.llm_pricing import estimate_cost_usd

    metrics = {"prompt_token_count": 2_000_000, "cached_content_token_count": 1_000_000,
               "candidates_token_count": 500_000, "thoughts_token_count": 500_000}
    vertex = estimate_cost_usd("gemini", "gemini-3.8-flash", metrics)
    ip.upsert_instance(_instance("gemini", models=[{
        "id": "gemini-3.8-flash", "pricing": {"prompt": 1.5, "completion": 7.5, "cache_read": 0.15},
    }, {"id": "gemini-4-argon"}]))
    assert estimate_cost_usd("gemini", "gemini:gemini-3.8-flash", metrics) == pytest.approx(vertex)
    assert estimate_cost_usd("gemini", "gemini:gemini-4-argon", metrics) == pytest.approx(
        estimate_cost_usd("gemini", "gemini-4-argon", metrics)
    )
    assert estimate_cost_usd("gemini", "gemini:gemini-9", metrics) is None


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def health_store(tmp_path, monkeypatch):
    import chat.llm.health as health

    fresh = health.ModelHealthStore(tmp_path / "model_health.json")
    monkeypatch.setattr(health, "_store", fresh)
    return fresh


@pytest.fixture
def admin(tmp_path, monkeypatch, health_store):
    import chat.routes.admin as admin
    import config.server_config as server_config

    monkeypatch.setattr(server_config, "SERVER_CONFIG_FILE", tmp_path / "server_config.json")
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("GEMINI_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.setattr(admin, "is_admin", lambda email: email == ADMIN_USER["email"])
    return admin


@pytest.fixture
def scheduled(monkeypatch):
    import chat.llm.health as health

    calls: list[list[str]] = []
    monkeypatch.setattr(health, "schedule_model_rechecks", lambda ids: calls.append(list(ids)))
    return calls


@pytest.fixture
def anthropic_catalog(monkeypatch):
    """Serve a canned Anthropic catalog (the key must be ``sk-ant-good``)."""
    def handler(url, headers):
        if headers["x-api-key"] != "sk-ant-good":
            raise _http_error(401, "invalid x-api-key")
        return {"data": [
            {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
            {"id": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5"},
            {"id": "claude-sonnet-5-20260801", "display_name": "Claude Sonnet 5"},
        ], "has_more": False, "last_id": "x"}
    return _stub_http(monkeypatch, handler)


@pytest.fixture
def checks(monkeypatch):
    """Make health checks pass or fail without a provider call."""
    import chat.llm.health as health

    state = {"ok": True, "checked": []}

    async def fake_check(model_id):
        state["checked"].append(model_id)
        return {"model": model_id, "ok": state["ok"], "error": None if state["ok"] else "invalid x-api-key"}

    monkeypatch.setattr(health, "check_model", fake_check)
    return state


def test_admin_catalog_route_serves_the_vendor_list_with_the_stored_key(admin, anthropic_catalog):
    _run(admin.admin_create_inference_instance(admin.InstanceCreate(kind="anthropic"), user=ADMIN_USER))
    result = _run(admin.admin_instance_catalog("anthropic", q="", user=ADMIN_USER))
    assert result == {"models": [], "error": "No API key saved yet."}
    _run(admin.admin_update_inference_instance(
        "anthropic", admin.InstanceUpdate(api_key="sk-ant-good"), user=ADMIN_USER,
    ))
    result = _run(admin.admin_instance_catalog("anthropic", q="haiku", user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == ["claude-haiku-5-5"] and result["error"] is None


def test_admin_add_snapshots_from_the_vendor_catalog(admin, anthropic_catalog, scheduled):
    _run(admin.admin_create_inference_instance(admin.InstanceCreate(kind="anthropic"), user=ADMIN_USER))
    _run(admin.admin_update_inference_instance(
        "anthropic", admin.InstanceUpdate(api_key="sk-ant-good"), user=ADMIN_USER,
    ))
    _run(admin.admin_update_inference_instance(
        "anthropic",
        admin.InstanceUpdate(models=[admin.InstanceModelUpdate(id="claude-haiku-5-5")]),
        user=ADMIN_USER,
    ))
    stored = ip.get_instance("anthropic")["models"][0]
    assert stored["name"] == "Claude Haiku 5.5" and stored["context_length"] == 1_000_000
    assert stored["pricing"]["prompt"] == 0.10
    assert stored["reasoning"]["supported_efforts"] == ["low", "medium", "high", "xhigh", "max"]
    spec = resolve_model("anthropic:claude-haiku-5-5")
    assert spec.thinking_efforts == ("low", "medium", "high", "xhigh", "max") and spec.thinking_effort == "medium"
    # Models added from Settings start private-disallowed, as for every kind
    from config.model_selection import is_model_allowed
    assert is_model_allowed("anthropic:claude-haiku-5-5", public=False) is False


def test_quick_setup_connects_adds_suggestions_and_checks_the_first(admin, anthropic_catalog, checks, scheduled):
    from chat.llm.config import get_available_models
    from config.model_selection import is_model_allowed

    result = _run(admin.admin_quick_setup_inference(
        admin.QuickSetupRequest(kind="anthropic", api_key=" sk-ant-good "), user=ADMIN_USER,
    ))
    assert result["instance"]["id"] == "anthropic" and result["instance"]["configured"] is True
    assert result["added"] == ["anthropic:claude-sonnet-5", "anthropic:claude-haiku-5-5"]
    assert result["check"]["model"] == "anthropic:claude-sonnet-5" and result["check"]["ok"] is True
    assert result["catalog_error"] is None
    assert checks["checked"] == ["anthropic:claude-sonnet-5"]
    assert scheduled == [["anthropic:claude-haiku-5-5"]]
    assert "sk-ant-good" not in json.dumps(result)
    assert get_available_models() == ["anthropic:claude-sonnet-5", "anthropic:claude-haiku-5-5"]
    # Quick-setup models are usable in private conversations right away
    assert is_model_allowed("anthropic:claude-sonnet-5", public=False) is True


def test_quick_setup_with_a_bad_key_leaves_the_instance_empty_and_is_retryable(admin, anthropic_catalog, checks, scheduled):
    from chat.llm.config import get_available_models

    checks["ok"] = False
    result = _run(admin.admin_quick_setup_inference(
        admin.QuickSetupRequest(kind="anthropic", api_key="sk-ant-bad"), user=ADMIN_USER,
    ))
    assert result["added"] == [] and result["check"] is None
    assert result["catalog_error"] == "HTTP 401: invalid x-api-key"
    assert ip.get_instance("anthropic")["models"] == [] and get_available_models() == []

    # The catalog came back (cached from a good key) but the probe fails
    ip.write_inference_credentials("anthropic", {"api_key": "sk-ant-good"})
    ac.get_catalog("sk-ant-good", refresh=True)
    result = _run(admin.admin_quick_setup_inference(
        admin.QuickSetupRequest(kind="anthropic", api_key="sk-ant-bad"), user=ADMIN_USER,
    ))
    assert result["added"] == [] and result["check"]["ok"] is False
    assert result["check"]["error"] == "invalid x-api-key"
    assert ip.get_instance("anthropic")["models"] == [] and scheduled == []
    # No verdict lingers for the removed probe
    assert "anthropic:claude-sonnet-5" not in _run_store_keys()

    # A retry with a good key reuses the same (still model-less) instance
    checks["ok"] = True
    result = _run(admin.admin_quick_setup_inference(
        admin.QuickSetupRequest(kind="anthropic", api_key="sk-ant-good"), user=ADMIN_USER,
    ))
    assert result["instance"]["id"] == "anthropic" and len(result["added"]) == 2
    assert [i["id"] for i in ip.list_instances()] == ["anthropic"]


def _run_store_keys():
    from chat.llm.health import get_model_health_store

    return set(get_model_health_store().get_all())


def test_quick_setup_validates_its_input(admin):
    for body in (
        admin.QuickSetupRequest(kind="nope", api_key="x"),
        admin.QuickSetupRequest(kind="openai", api_key="  "),
        admin.QuickSetupRequest(kind="local", base_url=""),
        admin.QuickSetupRequest(kind="local", base_url="ftp://box"),
        admin.QuickSetupRequest(kind="local", base_url="http://box:8080", api_type="grpc"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            _run(admin.admin_quick_setup_inference(body, user=ADMIN_USER))
        assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_quick_setup_inference(
            admin.QuickSetupRequest(kind="openai", api_key="sk"),
            user={"id": 2, "email": "nobody@example.com", "is_admin": False},
        ))
    assert exc_info.value.status_code == 403


def test_quick_setup_for_a_self_hosted_server_adds_every_reported_model(admin, checks, scheduled, monkeypatch):
    import chat.llm.local_catalog as local_catalog

    async def fake_discover(instance):
        assert instance["base_url"] == "http://box:8080" and instance["api_type"] == "ollama"
        return {"models": [
            {"id": "qwen2.5:7b", "name": "Qwen2.5 7b", "context_length": 32_768,
             "max_completion_tokens": None, "pricing": {"prompt": 0.0, "completion": 0.0}},
            {"id": "llama3:8b", "name": "Llama3 8b", "context_length": 8_192,
             "max_completion_tokens": None, "pricing": {"prompt": 0.0, "completion": 0.0}},
        ], "error": None}

    monkeypatch.setattr(local_catalog, "discover_models", fake_discover)
    result = _run(admin.admin_quick_setup_inference(
        admin.QuickSetupRequest(kind="local", base_url="box:8080/v1/", api_type="ollama"), user=ADMIN_USER,
    ))
    assert result["instance"]["base_url"] == "http://box:8080" and result["instance"]["api_type"] == "ollama"
    assert result["added"] == ["local:qwen2.5:7b", "local:llama3:8b"]
    assert checks["checked"] == ["local:qwen2.5:7b"] and scheduled == [["local:llama3:8b"]]


def test_quick_setup_with_nothing_to_suggest_connects_the_key_only(admin, checks, scheduled, monkeypatch):
    import chat.llm.openrouter_catalog as catalog

    monkeypatch.setattr(
        catalog, "get_catalog",
        lambda refresh=False: {"models": [{"id": "x/y", "name": "Y", "context_length": 1, "max_completion_tokens": None, "pricing": None, "reasoning": None}], "fetched_at": 1.0, "stale": False, "error": None},
    )
    result = _run(admin.admin_quick_setup_inference(
        admin.QuickSetupRequest(kind="openrouter", api_key="sk-or-x"), user=ADMIN_USER,
    ))
    assert result["added"] == [] and result["check"] is None and result["instance"]["configured"] is True
    assert checks["checked"] == [] and scheduled == []


def test_suggested_models_per_kind():
    from chat.llm.model_setup import suggested_models

    legacy = [m["id"] for m in ip._LEGACY_OPENROUTER_MODELS]
    router_models = [{"id": legacy[0], "capabilities": ["tools"]}, {"id": "other/model"}]
    assert suggested_models(_instance("openrouter"), router_models) == [legacy[0]]
    assert suggested_models(_instance("trustedrouter"), router_models) == [legacy[0]]
    assert suggested_models(_instance("trustedrouter"), []) == []
    # NEAR AI does not list the legacy OpenRouter ids; it has its own pair.
    assert suggested_models(_instance("nearai"), router_models) == []
    near_models = [{"id": "Qwen/Qwen3.8-27B"}, {"id": "z-ai/glm-5.3-flash"}, {"id": "other/model"}]
    assert suggested_models(_instance("nearai"), near_models) == ["z-ai/glm-5.3-flash", "Qwen/Qwen3.8-27B"]
    from db.llm_pricing import fireworks_priced_wire_ids
    priced = fireworks_priced_wire_ids()[0]
    fw_models = [{"id": priced, "capabilities": ["tools"]}, {"id": "accounts/fireworks/models/other", "capabilities": ["tools"]}]
    assert suggested_models(_instance("fireworks"), fw_models) == [priced]
    assert suggested_models(_instance("fireworks"), [{"id": priced, "capabilities": []}]) == []
