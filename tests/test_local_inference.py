"""Tests for self-hosted ("local") inference provider instances.

Covers the ``local`` instance kind in ``config/inference_providers.py``
(base-URL normalization, api_type defaulting, readiness without a key),
model resolution + provider-class selection in ``chat/llm/config.py``
(colon-bearing Ollama wire ids, ``OllamaProvider`` for the ``ollama`` API
type), the endpoint resolution of ``OpenRouterProvider`` for self-hosted
servers, the Ollama native transport in ``chat/llm/ollama_provider.py``
(message conversion both ways, NDJSON streaming against a stubbed httpx
client, usage capture, error surfacing), live model discovery in
``chat/llm/local_catalog.py`` against ``httpx.MockTransport`` fixtures
shaped like real llama.cpp / Ollama answers, and the admin endpoint
additions (endpoint fields, optional key, per-model overrides, the
per-instance catalog route). No network: every HTTP call is stubbed.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

import config.inference_providers as ip
from chat.llm.ollama_provider import OllamaError, OllamaProvider, to_ollama_messages
from chat.llm.openrouter_provider import OpenRouterProvider

ADMIN_USER = {"id": 1, "email": "admin@example.com", "is_admin": True}


def _run(coro):
    return asyncio.run(coro)


def _local(instance_id="local", api_type="openai", base_url="http://127.0.0.1:8080", models=()):
    return {
        "id": instance_id,
        "kind": "local",
        "label": "Box",
        "base_url": base_url,
        "api_type": api_type,
        "models": list(models),
    }


# ---------------------------------------------------------------------------
# Config store
# ---------------------------------------------------------------------------

def test_normalize_base_url():
    assert ip.normalize_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert ip.normalize_base_url("  192.168.1.5:11434 ") == "http://192.168.1.5:11434"
    assert ip.normalize_base_url("https://llm.example/v1") == "https://llm.example"
    assert ip.normalize_base_url("http://h:1/prefix/v1/") == "http://h:1/prefix"
    for bad in ("", "   ", "ftp://x", "http:///x", None, 5):
        assert ip.normalize_base_url(bad) is None


def test_local_instance_normalization_and_defaults():
    stored = ip.upsert_instance({
        "id": "local", "kind": "local", "label": "", "base_url": "10.0.0.2:8080/",
        "api_type": "bogus", "models": ["qwen2.5:0.5b"],
    })
    assert stored["label"] == "Self-hosted"
    assert stored["base_url"] == "http://10.0.0.2:8080"
    assert stored["api_type"] == "openai"
    assert stored["models"][0]["id"] == "qwen2.5:0.5b"
    # Round-trips through the file, endpoint fields intact
    assert ip.get_instance("local")["base_url"] == "http://10.0.0.2:8080"
    # OpenRouter instances never carry endpoint fields
    other = ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "base_url": "x", "models": []})
    assert "base_url" not in other and "api_type" not in other


def test_local_instance_configured_without_key():
    ip.upsert_instance(_local())
    assert ip.instance_configured(ip.get_instance("local")) is True
    ip.upsert_instance(_local(base_url=None))
    assert ip.instance_configured(ip.get_instance("local")) is False
    # OpenRouter instances still need their key
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": []})
    assert ip.instance_configured(ip.get_instance("openrouter")) is False
    ip.write_inference_credentials("openrouter", {"api_key": "k"})
    assert ip.instance_configured(ip.get_instance("openrouter")) is True


def test_new_local_instance_ids():
    assert ip.new_instance_id("local") == "local"
    ip.upsert_instance(_local())
    assert ip.new_instance_id("local") == "local-2"


# ---------------------------------------------------------------------------
# Resolution + provider selection
# ---------------------------------------------------------------------------

def test_resolve_local_model_with_colon_wire_id(monkeypatch):
    from chat.llm.config import (
        get_backend_for_model,
        get_configured_models,
        resolve_model,
    )
    import chat.llm.health as health

    monkeypatch.setattr(health, "_store", health.ModelHealthStore(ip.INFERENCE_PROVIDERS_FILE.parent / "h.json"))
    ip.upsert_instance(_local(api_type="ollama", models=[
        {"id": "qwen2.5:0.5b", "name": "Qwen 0.5B", "context_length": 8192,
         "pricing": {"prompt": 0, "completion": 0}},
    ]))
    spec = resolve_model("local:qwen2.5:0.5b")
    assert spec is not None
    assert (spec.instance_id, spec.wire_id) == ("local", "qwen2.5:0.5b")
    assert spec.provider == "openrouter" and spec.backend == "local"
    assert spec.display_name == "Qwen 0.5B" and spec.max_input_tokens == 8192
    assert get_backend_for_model("local:qwen2.5:0.5b") == "local"
    # Configured with no key at all (self-hosted), hidden once the URL is gone
    assert "local:qwen2.5:0.5b" in get_configured_models()
    ip.upsert_instance({**_local(api_type="ollama"), "base_url": None,
                        "models": ip.get_instance("local")["models"]})
    assert "local:qwen2.5:0.5b" not in get_configured_models()


def test_local_model_pricing_is_zero_not_unknown():
    from db.llm_pricing import estimate_cost_usd

    ip.upsert_instance(_local(models=[
        {"id": "m", "pricing": {"prompt": 0.0, "completion": 0.0}},
    ]))
    assert ip.instance_model_pricing("m") == {"prompt": 0.0, "completion": 0.0}
    assert estimate_cost_usd(
        "openrouter", "local:m", {"prompt_tokens": 1000, "completion_tokens": 100}, False,
    ) == 0.0


def test_provider_class_follows_api_type(monkeypatch):
    import chat.llm.config as llm_config

    monkeypatch.setattr(llm_config, "_provider_instances", {})
    ip.upsert_instance(_local("local", api_type="openai"))
    ip.upsert_instance(_local("local-2", api_type="ollama", base_url="http://o:11434"))
    openai_provider = llm_config.get_provider_instance("openrouter", "local")
    ollama_provider = llm_config.get_provider_instance("openrouter", "local-2")
    assert type(openai_provider) is OpenRouterProvider
    assert isinstance(ollama_provider, OllamaProvider)
    # Cached per instance; dropping one forgets it
    assert llm_config.get_provider_instance("openrouter", "local-2") is ollama_provider
    llm_config.drop_provider_instance("openrouter", "local-2")
    assert llm_config.get_provider_instance("openrouter", "local-2") is not ollama_provider


def test_openrouter_provider_endpoint_for_local_instance():
    ip.upsert_instance(_local(base_url="http://box:8080"))
    provider = OpenRouterProvider("local")
    endpoint = provider._endpoint()
    assert endpoint == {
        "base_url": "http://box:8080/v1", "api_key": "no-key",
        "headers": None, "openrouter": False,
    }
    ip.write_inference_credentials("local", {"api_key": "secret"})
    assert provider._endpoint()["api_key"] == "secret"
    # No base URL -> descriptive error naming the settings section
    ip.upsert_instance(_local(base_url=None))
    with pytest.raises(ValueError, match="Settings > Inference Providers"):
        OpenRouterProvider("local")._endpoint()


def test_openrouter_provider_endpoint_for_openrouter_instance():
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": []})
    ip.write_inference_credentials("openrouter", {"api_key": "sk-or"})
    endpoint = OpenRouterProvider("openrouter")._endpoint()
    assert endpoint["openrouter"] is True
    assert endpoint["base_url"].startswith("https://openrouter.ai")
    assert endpoint["api_key"] == "sk-or"


# ---------------------------------------------------------------------------
# Ollama message conversion
# ---------------------------------------------------------------------------

def test_to_ollama_messages_converts_tool_calls_and_results():
    history = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": [
            {"type": "text", "text": "{\"temp\": 21}"}, {"type": "text", "text": "note"},
        ]},
        {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
    ]
    out = to_ollama_messages("sys", history)
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}},
        ]},
        {"role": "tool", "content": "{\"temp\": 21}\nnote", "tool_name": "get_weather"},
        {"role": "user", "content": "thanks"},
    ]


def test_to_ollama_messages_tolerates_bad_arguments_and_unknown_tool_ids():
    out = to_ollama_messages("", [
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "c", "function": {"name": "t", "arguments": "not json"}},
        ]},
        {"role": "tool", "tool_call_id": "other", "content": "r"},
    ])
    assert out[0]["tool_calls"][0]["function"]["arguments"] == {}
    assert out[1] == {"role": "tool", "content": "r"}


# ---------------------------------------------------------------------------
# Ollama transport against a stubbed httpx client
# ---------------------------------------------------------------------------

def _ndjson(*chunks: dict) -> bytes:
    return "".join(json.dumps(c) + "\n" for c in chunks).encode()


class _Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return httpx.Response(status, content=body)


@pytest.fixture
def ollama(monkeypatch):
    """An OllamaProvider whose HTTP client answers from canned responses."""
    ip.upsert_instance(_local("local", api_type="ollama", base_url="http://o:11434", models=[
        {"id": "qwen2.5:0.5b", "context_length": 8192, "max_completion_tokens": 256},
    ]))
    provider = OllamaProvider("local")

    def install(*responses):
        recorder = _Recorder(responses)
        provider._http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
        return recorder

    return provider, install


def _events(provider, session, message):
    async def collect():
        return [e async for e in provider.send_message_stream(session, message)]
    return _run(collect())


def test_ollama_stream_text_tool_calls_and_usage(ollama):
    provider, install = ollama
    recorder = install((200, _ndjson(
        {"message": {"role": "assistant", "content": "Let me "}, "done": False},
        {"message": {"role": "assistant", "content": "check."}, "done": False},
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "function": {"index": 0, "name": "get_weather", "arguments": {"city": "Paris"}}},
            {"function": {"name": "get_time", "arguments": {}}},
        ]}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
         "prompt_eval_count": 150, "prompt_eval_cached_count": 20, "eval_count": 12},
    )))
    session = provider.create_session("local:qwen2.5:0.5b", "Be terse.", [{
        "name": "get_weather", "description": "d",
        "parameters": {"type": "object", "properties": {}},
    }])
    events = _events(provider, session, "weather?")

    assert [e.type for e in events] == ["text", "text", "tool_call", "tool_call"]
    assert events[2].tool_id == "call_abc" and events[2].tool_args == {"city": "Paris"}
    assert events[3].tool_id.startswith("call_") and events[3].tool_name == "get_time"

    # Request shape: native endpoint, system prompt first, per-model num_ctx
    request = recorder.requests[0]
    assert str(request.url) == "http://o:11434/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:0.5b" and body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "Be terse."}
    assert body["options"] == {"num_ctx": 8192, "num_predict": 256}
    assert body["tools"][0]["function"]["name"] == "get_weather"
    assert "Authorization" not in request.headers

    # History persisted in the OpenAI shape (arguments as a JSON string)
    last = session.messages[-1]
    assert last["role"] == "assistant" and last["content"] == "Let me check."
    assert last["tool_calls"][0]["function"]["arguments"] == "{\"city\": \"Paris\"}"
    assert provider.get_pending_tool_uses(session) == [("call_abc", "get_weather"), (events[3].tool_id, "get_time")]

    usage = provider.get_usage(session)
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (150, 12, 20)
    assert usage.raw_usage == {
        "prompt_tokens": 150, "completion_tokens": 12, "total_tokens": 162,
        "cached_prompt_tokens": 20,
    }


def test_ollama_tool_results_round_trip_and_bearer_key(ollama):
    provider, install = ollama
    ip.write_inference_credentials("local", {"api_key": "tok"})
    recorder = install((200, _ndjson(
        {"message": {"role": "assistant", "content": "21C"}, "done": True,
         "prompt_eval_count": 5, "eval_count": 1},
    )))
    session = provider.create_session("local:qwen2.5:0.5b", "", [])
    session.messages = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}},
        ]},
    ]
    results = provider.format_tool_results(session, [{"tool_id": "call_1", "result": "{\"temp\": 21}"}])
    events = _events(provider, session, results)
    assert [e.text for e in events] == ["21C"]
    body = json.loads(recorder.requests[0].content)
    assert body["messages"][-1] == {"role": "tool", "content": "{\"temp\": 21}", "tool_name": "get_weather"}
    assert body["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"city": "Paris"}
    assert recorder.requests[0].headers["Authorization"] == "Bearer tok"
    assert session.messages[-1] == {"role": "assistant", "content": "21C"}


def test_ollama_http_and_stream_errors_surface(ollama):
    from chat.llm.health import extract_error_message

    provider, install = ollama
    session = provider.create_session("local:qwen2.5:0.5b", "", [])
    install((404, b'{"error":"model \'qwen2.5:0.5b\' not found"}'))
    with pytest.raises(OllamaError) as exc_info:
        _events(provider, session, "hi")
    assert extract_error_message(exc_info.value) == "HTTP 404: model 'qwen2.5:0.5b' not found"

    install((200, _ndjson({"error": "out of memory"})))
    with pytest.raises(OllamaError, match="out of memory"):
        _events(provider, session, "hi")

    # check_model_access uses the same error path
    install((500, b"boom"))
    with pytest.raises(OllamaError, match="boom"):
        _run(provider.check_model_access("local:qwen2.5:0.5b"))


def test_ollama_check_model_access_request(ollama):
    provider, install = ollama
    recorder = install((200, json.dumps({"message": {"role": "assistant", "content": "k"}, "done": True}).encode()))
    _run(provider.check_model_access("local:qwen2.5:0.5b"))
    body = json.loads(recorder.requests[0].content)
    assert body["stream"] is False and body["options"] == {"num_predict": 1}
    assert body["model"] == "qwen2.5:0.5b"


def test_ollama_unlisted_model_uses_default_context(ollama):
    provider, install = ollama
    recorder = install((200, _ndjson({"message": {"content": "x"}, "done": True})))
    session = provider.create_session("local:not-listed:7b", "", [])
    _events(provider, session, "hi")
    body = json.loads(recorder.requests[0].content)
    assert body["options"]["num_ctx"] == ip.DEFAULT_OLLAMA_CONTEXT_LENGTH


# ---------------------------------------------------------------------------
# Live discovery (local_catalog)
# ---------------------------------------------------------------------------

LLAMACPP_MODELS = {
    "object": "list",
    "data": [{
        "id": "qwen2.5-0.5b-instruct", "object": "model", "owned_by": "llamacpp",
        "meta": {"n_ctx": 16384, "n_ctx_train": 32768, "n_params": 630167424, "ftype": "Q4_K - Medium"},
    }],
}
OLLAMA_TAGS = {
    "models": [
        {"name": "qwen2.5:0.5b", "model": "qwen2.5:0.5b", "size": 397821319,
         "details": {"family": "qwen2", "parameter_size": "494.03M", "quantization_level": "Q4_K_M",
                     "context_length": 32768},
         "capabilities": ["completion", "tools"]},
        # Older-server shape: no context / capabilities in the tag entry
        {"name": "llama3.2:1b", "details": {"family": "llama", "parameter_size": "1.2B",
                                              "quantization_level": "Q8_0"}},
    ],
}


@pytest.fixture
def mock_http(monkeypatch):
    """Route local_catalog's httpx.AsyncClient through a MockTransport."""
    routes: dict[tuple[str, str], object] = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        target = routes.get((request.method, request.url.path))
        if target is None:
            return httpx.Response(404, content=b"nope")
        if isinstance(target, Exception):
            raise target
        return httpx.Response(200, json=target)

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return routes, seen


def test_discover_openai_llamacpp_shape(mock_http):
    from chat.llm.local_catalog import discover_models

    routes, seen = mock_http
    routes[("GET", "/v1/models")] = LLAMACPP_MODELS
    ip.write_inference_credentials("local", {"api_key": "tok"})
    result = _run(discover_models(_local()))
    assert result["error"] is None
    [model] = result["models"]
    assert model["id"] == "qwen2.5-0.5b-instruct"
    assert model["name"] == "Qwen2.5 0.5b Instruct"
    assert model["context_length"] == 16384  # loaded n_ctx beats n_ctx_train
    assert model["pricing"] == {"prompt": 0.0, "completion": 0.0}
    assert model["detail"] == "630M params · Q4_K - Medium · llamacpp"
    assert model["capabilities"] is None
    assert seen[0].headers["Authorization"] == "Bearer tok"
    # /props only consulted when a context is missing
    assert [r.url.path for r in seen] == ["/v1/models"]


def test_discover_openai_falls_back_to_props_for_context(mock_http):
    from chat.llm.local_catalog import discover_models

    routes, seen = mock_http
    routes[("GET", "/v1/models")] = {"data": [{"id": "a"}, {"id": "b", "meta": {"n_ctx_train": 4096}}]}
    routes[("GET", "/props")] = {"default_generation_settings": {"n_ctx": 2048}}
    result = _run(discover_models(_local()))
    assert {m["id"]: m["context_length"] for m in result["models"]} == {"a": 2048, "b": 4096}
    # A server without /props (vLLM etc.) just leaves None
    del routes[("GET", "/props")]
    result = _run(discover_models(_local()))
    assert {m["id"]: m["context_length"] for m in result["models"]} == {"a": None, "b": 4096}


def test_discover_ollama_tags_and_show(mock_http):
    from chat.llm.local_catalog import discover_models

    routes, seen = mock_http
    routes[("GET", "/api/tags")] = OLLAMA_TAGS
    routes[("POST", "/api/show")] = {
        "model_info": {"llama.context_length": 131072, "llama.embedding_length": 2048},
        "capabilities": ["completion"],
    }
    result = _run(discover_models(_local(api_type="ollama", base_url="http://o:11434")))
    assert result["error"] is None
    by_id = {m["id"]: m for m in result["models"]}
    assert by_id["qwen2.5:0.5b"]["context_length"] == 32768
    assert by_id["qwen2.5:0.5b"]["capabilities"] == ["completion", "tools"]
    assert by_id["qwen2.5:0.5b"]["detail"] == "qwen2 · 494.03M · Q4_K_M · trained on 32K ctx"
    # Training context capped at the default num_ctx; capabilities from /api/show
    assert by_id["llama3.2:1b"]["context_length"] == ip.DEFAULT_OLLAMA_CONTEXT_LENGTH
    assert by_id["llama3.2:1b"]["capabilities"] == ["completion"]
    assert by_id["llama3.2:1b"]["name"] == "Llama3.2 1b"
    # Only the entry lacking metadata was looked up
    shows = [json.loads(r.content)["model"] for r in seen if r.url.path == "/api/show"]
    assert shows == ["llama3.2:1b"]


def test_discover_reports_server_errors(mock_http):
    from chat.llm.local_catalog import discover_models

    routes, _seen = mock_http
    result = _run(discover_models(_local()))  # nothing routed -> 404
    assert result["models"] == [] and "HTTP 404" in result["error"]
    routes[("GET", "/v1/models")] = httpx.ConnectError("connection refused")
    result = _run(discover_models(_local()))
    assert result["models"] == [] and "connection refused" in result["error"]
    routes[("GET", "/v1/models")] = {"unexpected": True}
    assert "unexpected response shape" in _run(discover_models(_local()))["error"]
    with pytest.raises(ValueError):
        _run(discover_models(_local(base_url=None)))


def test_friendly_name_and_default_snapshot():
    from chat.llm.local_catalog import default_snapshot, friendly_name

    assert friendly_name("/models/llama-3.2-3b-instruct-q4_k_m.gguf") == "Llama 3.2 3b Instruct Q4 K M"
    assert friendly_name("meta-llama/Llama-3.1-8B-Instruct") == "Llama 3.1 8B Instruct"
    assert friendly_name("") == ""
    assert default_snapshot(_local()) == {"pricing": {"prompt": 0.0, "completion": 0.0}}
    assert default_snapshot(_local(api_type="ollama")) == {
        "pricing": {"prompt": 0.0, "completion": 0.0},
        "context_length": ip.DEFAULT_OLLAMA_CONTEXT_LENGTH,
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


def test_admin_list_exposes_kinds_and_api_types(admin):
    ip.upsert_instance(_local(api_type="ollama"))
    listing = _run(admin.admin_list_inference_providers(user=ADMIN_USER))
    kinds = {k["kind"]: k for k in listing["kinds"]}
    assert kinds["local"]["endpoint"] is True and kinds["openrouter"]["endpoint"] is False
    assert [t["id"] for t in listing["api_types"]] == ["openai", "ollama"]
    [instance] = listing["instances"]
    assert instance["kind"] == "local" and instance["configured"] is True
    assert instance["base_url"] == "http://127.0.0.1:8080" and instance["api_type"] == "ollama"
    assert instance["key_required"] is False and instance["credentials"]["api_key_set"] is False


def test_admin_create_local_instance(admin):
    created = _run(admin.admin_create_inference_instance(
        admin.InstanceCreate(kind="local", label="GPU box"), user=ADMIN_USER,
    ))
    assert created["id"] == "local" and created["label"] == "GPU box"
    assert created["configured"] is False and created["base_url"] is None
    assert created["api_type"] == "openai"


def test_admin_update_local_endpoint_and_optional_key(admin, monkeypatch):
    import chat.llm.config as llm_config

    resets: list[str] = []
    monkeypatch.setattr(llm_config, "reset_provider_client_caches", lambda: resets.append("reset"))
    monkeypatch.setattr(llm_config, "drop_provider_instance", lambda p, i: resets.append(f"drop:{p}:{i}"))
    ip.upsert_instance(_local(base_url=None))

    # Empty key on a self-hosted instance is fine (key optional)
    body = admin.InstanceUpdate(base_url=" 127.0.0.1:9191/ ", api_type="ollama", api_key="")
    status = _run(admin.admin_update_inference_instance("local", body, user=ADMIN_USER))
    assert status["base_url"] == "http://127.0.0.1:9191" and status["api_type"] == "ollama"
    assert status["configured"] is True
    assert resets == ["reset", "drop:openrouter:local"]

    # Unchanged endpoint -> nothing dropped
    body = admin.InstanceUpdate(base_url="http://127.0.0.1:9191", api_type="ollama", label="L")
    _run(admin.admin_update_inference_instance("local", body, user=ADMIN_USER))
    assert resets == ["reset", "drop:openrouter:local"]

    for bad in (admin.InstanceUpdate(base_url="ftp://x"), admin.InstanceUpdate(api_type="grpc")):
        with pytest.raises(HTTPException) as exc_info:
            _run(admin.admin_update_inference_instance("local", bad, user=ADMIN_USER))
        assert exc_info.value.status_code == 400

    # Fixed-upstream kinds reject endpoint fields
    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": []})
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance(
            "openrouter", admin.InstanceUpdate(base_url="http://x"), user=ADMIN_USER,
        ))
    assert exc_info.value.status_code == 400
    # ...and still require a key
    with pytest.raises(HTTPException):
        _run(admin.admin_update_inference_instance(
            "openrouter", admin.InstanceUpdate(api_key=""), user=ADMIN_USER,
        ))


def test_admin_update_local_models_snapshot_and_overrides(admin, monkeypatch):
    import chat.llm.local_catalog as local_catalog

    discovered = [{
        "id": "qwen2.5:0.5b", "name": "Qwen2.5 0.5b", "context_length": 32768,
        "max_completion_tokens": None, "pricing": {"prompt": 0.0, "completion": 0.0},
        "detail": "", "capabilities": ["tools"],
    }]

    async def fake_discover(instance):
        return {"models": discovered, "error": None}

    monkeypatch.setattr(local_catalog, "discover_models", fake_discover)
    ip.upsert_instance(_local(api_type="ollama"))

    body = admin.InstanceUpdate(models=[
        admin.InstanceModelUpdate(id="qwen2.5:0.5b"),
        admin.InstanceModelUpdate(id="custom:7b", name="  My Custom  ", context_length=4096),
    ])
    status = _run(admin.admin_update_inference_instance("local", body, user=ADMIN_USER))
    rows = {m["wire_id"]: m for m in status["models"]}
    assert rows["qwen2.5:0.5b"]["display_name"] == "Qwen2.5 0.5b"
    assert rows["qwen2.5:0.5b"]["max_input_tokens"] == 32768
    assert rows["qwen2.5:0.5b"]["id"] == "local:qwen2.5:0.5b"
    assert rows["custom:7b"]["display_name"] == "My Custom"
    assert rows["custom:7b"]["max_input_tokens"] == 4096
    stored = {m["id"]: m for m in ip.get_instance("local")["models"]}
    assert stored["qwen2.5:0.5b"]["pricing"] == {"prompt": 0.0, "completion": 0.0}
    assert stored["custom:7b"]["pricing"] == {"prompt": 0.0, "completion": 0.0}

    # Later override of an existing model keeps its snapshot otherwise
    body = admin.InstanceUpdate(models=[
        admin.InstanceModelUpdate(id="qwen2.5:0.5b", name="Renamed", context_length=16384),
        admin.InstanceModelUpdate(id="custom:7b", enabled=False),
    ])
    status = _run(admin.admin_update_inference_instance("local", body, user=ADMIN_USER))
    rows = {m["wire_id"]: m for m in status["models"]}
    assert rows["qwen2.5:0.5b"]["display_name"] == "Renamed"
    assert rows["qwen2.5:0.5b"]["max_input_tokens"] == 16384
    assert rows["custom:7b"]["display_name"] == "My Custom" and rows["custom:7b"]["enabled"] is False

    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_update_inference_instance("local", admin.InstanceUpdate(models=[
            admin.InstanceModelUpdate(id="x", context_length=0),
        ]), user=ADMIN_USER))
    assert exc_info.value.status_code == 400


def test_admin_instance_catalog_endpoint(admin, monkeypatch):
    import chat.llm.local_catalog as local_catalog

    calls: list[str] = []

    async def fake_discover(instance):
        calls.append(instance["id"])
        return {"models": [
            {"id": "qwen2.5:0.5b", "name": "Qwen", "context_length": 1, "max_completion_tokens": None,
             "pricing": None, "detail": "", "capabilities": None},
            {"id": "llama3:8b", "name": "Llama", "context_length": 1, "max_completion_tokens": None,
             "pricing": None, "detail": "", "capabilities": None},
        ], "error": None}

    monkeypatch.setattr(local_catalog, "discover_models", fake_discover)
    ip.upsert_instance(_local())
    result = _run(admin.admin_instance_catalog("local", q="llama", user=ADMIN_USER))
    assert [m["id"] for m in result["models"]] == ["llama3:8b"] and result["error"] is None
    assert calls == ["local"]

    # No URL yet: empty with an explanation, no discovery attempted
    ip.upsert_instance(_local(base_url=None))
    result = _run(admin.admin_instance_catalog("local", user=ADMIN_USER))
    assert result["models"] == [] and result["error"] and calls == ["local"]

    ip.upsert_instance({"id": "openrouter", "kind": "openrouter", "models": []})
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("openrouter", user=ADMIN_USER))
    assert exc_info.value.status_code == 400
    with pytest.raises(HTTPException) as exc_info:
        _run(admin.admin_instance_catalog("local", user={"id": 2, "email": "x@y", "is_admin": False}))
    assert exc_info.value.status_code == 403
