# LLM Provider Abstraction Layer

This document describes the LLM provider abstraction layer in `chat/llm/`, which provides a unified interface for multiple LLM backends. The system currently supports Google Gemini (via the `google-genai` SDK), Anthropic Claude (via the `anthropic[vertex]` SDK on Google Cloud Vertex AI), and the OpenAI chat-completions family: OpenRouter-served third-party models (via the `openai` SDK against OpenRouter's OpenAI-compatible API, authenticated by an API key -- the non-Vertex path for personal deployments) and **self-hosted** models on a server the admin points at (llama.cpp, vLLM, LM Studio, Ollama and friends -- the same `openai` SDK against the server's `/v1`, or Ollama's native API through the `OllamaProvider` subclass; see [Inference Providers](inference-providers.md)).

## Overview

The provider abstraction decouples the conversation loop from any specific LLM SDK. The conversation loop in `chat/gemini_api/conversation.py` and sub-agent execution in `chat/gemini_api/sub_agent.py` interact only with the `LLMProvider` interface, never touching provider-specific SDK types directly. This allows adding new providers without modifying the conversation loop.

Tool definitions are maintained in a single, provider-agnostic format (JSON Schema) in `chat/llm/tool_schemas.py`. Converter functions translate these canonical definitions into each provider's native format at session creation time.

## LLMProvider Interface

The `LLMProvider` abstract base class in `chat/llm/base.py` defines the contract that each provider must implement. Key methods include `create_session()`, `send_message_stream()`, `format_tool_results()`, `get_usage()`, `save_history()`/`load_history()`, `upload_file()`/`make_file_part()`, `make_text_part()`, `inject_turn_warning()`, `get_pending_tool_uses()`, `get_pending_tool_use_args()`, and `append_user_text()`.

`get_pending_tool_use_args()` returns `(tool_id, tool_name, args)` tuples for tool_use blocks on the last assistant turn that have no matching tool_result (`get_pending_tool_uses()` is the older `(tool_id, tool_name)`-only form); `append_user_text()` appends a provider-appropriate text block to a formatted tool-results list. Both are used by the resume bucket logic at the top of `run_conversation_turn()` to close dangling tool_uses without isinstance branching -- see [Slack Socket Mode -- Restart Resilience](slack-socket-mode.md#restart-resilience) and [Wait Handles -- Suspend / Resume](wait-handles.md#suspend--resume).

The cancel-time helper `_save_interrupted_sdk_history()` no longer uses these methods -- it walks the on-disk envelope shape directly. See `chat/llm/base.py` for the full abstract method signatures and their docstrings.

## Shared Data Types

Defined in `chat/llm/base.py`:

- **`ToolSpec`** -- A `TypedDict` representing a provider-agnostic tool definition with `name`, `description`, `parameters` (JSON Schema object), and optional `provider_descriptions` (a `dict[str, str]` mapping provider keys like `"gemini"` or `"anthropic"` to provider-specific description overrides)
- **`StreamEvent`** -- A normalized streaming event with `type` (`"text"`, `"tool_call"`, or `"usage"`), plus type-specific fields (`text`, `tool_name`, `tool_args`, `tool_id`, `usage`)
- **`UsageStats`** -- Token usage statistics with `input_tokens`, `output_tokens`, `cached_tokens`, `cache_creation_tokens`, `cache_read_tokens`, and `raw_usage`.
  - For Gemini, `input_tokens` includes cached tokens and `cached_tokens` is the cache-hit subset.
  - For Anthropic, `input_tokens` excludes cached tokens, `cache_creation_tokens` tracks tokens being written to cache for the first time, `cache_read_tokens` tracks tokens served from an existing cache, and `cached_tokens` is their sum. The `cache_creation_tokens` and `cache_read_tokens` fields are always 0 for Gemini.
  - `raw_usage` is a dict of the lossless, provider-native token-count fields exactly as the provider returned them (field names verbatim), carried alongside the coalesced fields purely for cost/analytics. `record_api_call()` in `db/llm_call_store.py` copies its keys into the native columns of the per-provider raw tables (`llm_calls_gemini` / `llm_calls_anthropic`, see [Database -- LlmCallGemini and LlmCallAnthropic](database.md#llmcallgemini-and-llmcallanthropic-models)) and also stores the dict verbatim in each table's `raw_usage` catch-all column. It is empty when the provider returned no usage object
- **`compute_new_input_tokens(usage, provider_name)`** -- A helper function in `chat/llm/base.py` that computes the number of new (uncached) input tokens uniformly across providers. For Gemini: `input_tokens - cached_tokens` (since `input_tokens` includes cached). For Anthropic: `input_tokens + cache_creation_tokens` (since `input_tokens` excludes cached, but cache creation tokens are new work). This function is used by both `chat/gemini_api/conversation.py` and `chat/gemini_api/sub_agent.py` to produce a consistent `new_input_tokens` metric regardless of provider
- **`compute_total_context_tokens(usage, provider_name)`** -- A helper function in `chat/llm/base.py` that computes the total number of tokens occupying the model's context window after a turn. For Gemini: returns `input_tokens` directly (already includes cached tokens). For Anthropic: returns `input_tokens + cache_read_tokens + cache_creation_tokens` (since `input_tokens` excludes cached tokens). Used by `chat/gemini_api/conversation.py` to emit `context_tokens` in the stats event for the frontend context usage indicator, and by `chat/gemini_api/sub_agent.py` to check context window usage against `SUB_AGENT_CONTEXT_WARNING_THRESHOLD` for the sub-agent context window warning

## Model Registry

The `MODEL_REGISTRY` dict in `chat/llm/config.py` maps model IDs to their provider and metadata (display name, max input/output tokens). Currently registered: eight Gemini models (`gemini-3.1-pro-preview`, `gemini-3.1-flash-lite-preview`, `gemini-3-flash-preview`, and `gemini-3.5-flash` -- all four deprecated -- plus `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`) and nine Anthropic models (`claude-haiku-4.5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, `claude-opus-5-5`).

All entries include a `vertex_model_id` field for the Vertex AI publisher ID mapping (defaults to the registry key when absent). Anthropic entries may also carry a per-model `vertex_region` override (see [Per-Model Vertex Region](#per-model-vertex-region) below). All Anthropic models have a 200K-token input window except `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, and `claude-opus-5-5`, which have a 1M-token window. See `chat/llm/config.py` for the full registry entries.

Entries may carry a `deprecated: True` flag (currently `gemini-3.1-pro-preview`, `gemini-3-flash-preview`, `gemini-3.1-flash-lite-preview`, and `gemini-3.5-flash`): a deprecated model stays fully runnable -- existing conversations, routines, and stored per-user defaults keep working through the normal registry lookups -- but `get_available_models()` excludes it regardless of credentials, so it disappears from the new-conversation picker.

The frontend receives the flag with the model catalog on `GET /app/api/config` (`models`, see [Frontend](frontend.md)) and filters its selectors through `getSelectableModels()` in `frontend/src/constants/models.ts`, keeping deprecated entries only for display-name/context lookups and for showing a "(deprecated)" option on conversations, routines, and Slack defaults that already use it.

All Gemini entries run on the Vertex backend (`google-genai` in Vertex mode, `vertexai=True`, ADC + project/region). The developer-endpoint (`"genapi"`) transport was removed: Gemini thought signatures are not portable across the Vertex / AI Studio border, so a conversation whose history mixed backends failed with signature-validation errors on mid-conversation model switches. `gemini-3.1-flash-lite-preview` keeps its registry key but maps to the `gemini-3.1-flash-lite` Vertex publisher id (the `-preview` id 404s on Vertex). See [GeminiProvider](#geminiprovider) and [Gemini Vertex AI Configuration](#gemini-vertex-ai-configuration) below.

The registry holds Vertex models ONLY. OpenRouter and self-hosted models are not registry entries: they belong to admin-configured **provider instances** (`config/inference_providers.py`, edited in Settings > Inference Providers -- see [Inference Providers](inference-providers.md)), each an OpenRouter configuration with its own API key and its own admin-chosen model list. Instance-served models are identified everywhere (`conversations.model`, `routines.model`, per-user defaults, the health store, `llm_calls_openrouter.model`) by the qualified id `<instance_id>:<wire_id>` (e.g. `openrouter:deepseek/deepseek-v4-flash-0731`); `split_model_id()` parses it unambiguously because instance ids never contain `:` or `/` while every OpenRouter wire id contains a `/` before any `:variant` suffix. The pre-instances single OpenRouter key is instance `openrouter` (alembic migration `b8e4d2a7c1f5` prefixed the bare ids stored before, and a bare OpenRouter id still resolves to that instance as a fallback).

### Model resolution

`resolve_model(model_id)` in `chat/llm/config.py` is the one lookup path and returns a frozen `ModelSpec` (`id` = stored/qualified id, `wire_id` = what the API request carries, `provider`, `backend`, `instance_id`, `display_name`, `provider_label`, `max_input_tokens`, `max_output_tokens`, `deprecated`, `enabled`, `listed`, plus the Anthropic-only `vertex_region` / `thinking_effort` / `refusal_fallback_models`) or `None` for an unknown id. Vertex specs come from `MODEL_REGISTRY` with `enabled` reflecting the admin's Vertex disabled set; instance specs come from the instance's model snapshot (name, context/output limits from the OpenRouter catalog at add time, `enabled` = the per-model checkbox), and a model an instance no longer lists still resolves (`listed=False`, disabled, conservative default limits) so old conversations keep their provider. `list_model_specs()` enumerates every known model (registry order, then each instance's models); `public_model_catalog()` projects it for the frontend.

Thin wrappers keep the older call sites working: `get_provider_for_model(model_id)` returns the provider name (`"gemini"`, `"anthropic"`, or `"openrouter"`; `ValueError` for unknown ids), `get_backend_for_model(model_id)` the analytics backend label (`"vertex"` for every Vertex-served model, `"openrouter"` for OpenRouter instance models, `"local"` for self-hosted instance models, `None` for unknown ids so callers can record without raising -- persisted to the `backend` column of the `llm_calls_*` tables; historical rows recorded under `"genapi"` keep that label), `get_model_display_name()` / `get_max_input_tokens()` the obvious fields, and `model_instance_id(model_id)` the pure-parse instance qualifier (`None` for Vertex and bare ids).

Provider objects are keyed by `(provider, instance_id)`: `get_provider_instance(provider_name, instance_id=None)` returns the lazily-created singleton -- Vertex providers ignore `instance_id`, while `"openrouter"` gets one provider object per configured instance (each reading its own key or endpoint): an `OpenRouterProvider(instance_id)` for OpenRouter instances and for self-hosted instances with the `openai` API type, an `OllamaProvider(instance_id)` for self-hosted instances with the `ollama` API type (the admin endpoint drops the cached object when the API type changes), with `instance_id=None` selecting the legacy `openrouter` instance. Call sites that start from a model id pass `model_instance_id(model)`; `get_provider_for_model_instance(model_id)` combines resolution and lookup. `drop_provider_instance()` forgets an object when its instance is deleted; `reset_provider_client_caches()` drops cached SDK clients after a key change.

## Provider Implementations

### GeminiProvider

`GeminiProvider` in `chat/llm/gemini_provider.py` wraps the `google-genai` SDK in Vertex mode (the only Gemini transport; the developer-endpoint `"genapi"` transport was removed):

- **Authentication**: Google Cloud Application Default Credentials (ADC) -- no API key. Project ID and region come from the `gemini_vertex` section of `server_config.json` (see [Gemini Vertex AI Configuration](#gemini-vertex-ai-configuration) below)
- **Client cache**: One cached `genai.Client` (`_client`). Chat sessions hold the client's httpx connection pool, so the cache persists across turns. `_get_client()` lazily constructs `genai.Client(vertexai=True, project=..., location=...)`. `reset_cached_clients()` drops the cache when an admin saves new inference credentials (via `reset_provider_client_caches()` in `chat/llm/config.py`)
- **Session type**: Returns an `AsyncChat` object from `client.aio.chats.create()`, called with the `vertex_model_id` from the registry (identical to the registry key except `gemini-3.1-flash-lite-preview`, which maps to `gemini-3.1-flash-lite`)
- **Max output tokens**: `create_session()` passes `max_output_tokens` from `MODEL_REGISTRY` on `GenerateContentConfig` (falling back to 8192), so the Gemini models honor their 64-65k output budgets here
- **Streaming**: Iterates over SDK chunks, extracting text and function call parts from `candidates[0].content.parts`
- **Usage capture**: `get_usage()` populates the coalesced `input_tokens` / `output_tokens` / `cached_tokens` fields (which feed the in-UI stats sum) and a `raw_usage` dict capturing the lossless provider-native fields verbatim -- including `thoughts_token_count` / `tool_use_prompt_token_count` / `total_token_count`, which the coalesced fields drop. Only keys the SDK actually populated are included, so `raw_usage` is a faithful record of what the provider returned
- **Tool results**: Formatted as `types.Part.from_function_response()` objects
- **File upload**: Vertex has no `files.upload` endpoint, so `upload_file()` reads the file inline and returns a `types.Part.from_bytes(data=..., mime_type=...)` directly; `make_file_part()` passes it through. The inline MIME allow-list mirrors the Anthropic provider (JPEG/PNG/GIF/WebP/PDF -- see `_GEMINI_VERTEX_INLINE_MIME_TYPES` in `chat/llm/gemini_provider.py`), with a 7MB per-part size cap (`_GEMINI_VERTEX_INLINE_MAX_SIZE`, sourced from `GEMINI_VERTEX_MAX_ATTACH_BYTES` in `chat/llm/file_limits.py` -- see [Attachment Size Limits](#attachment-size-limits)). Oversized or unsupported files return `None` and the caller surfaces the fallback error to the model
- **Text part creation**: `make_text_part()` returns `types.Part.from_text(text=text)`, used for advisory `extra_parts` (e.g., limit cap notices from `ToolResultWithNotices`)
- **Turn warning injection**: Appends a `types.Part.from_text()` with the warning message to the formatted tool results list
- **History serialization**: Uses Pydantic `model_dump_json()` for SDK `Content` objects, tagged `provider: "gemini"` in the on-disk envelope

### AnthropicProvider

`AnthropicProvider` in `chat/llm/anthropic_provider.py` wraps the `anthropic[vertex]` SDK:

- **Authentication**: Uses Google Cloud Application Default Credentials (ADC) via `AsyncAnthropicVertex` -- no API key needed
- **Configuration**: Loads `vertex_project_id` from the `anthropic` section of `server_config.json`. The region is resolved per model by `_resolve_region(model)` -- a per-model `vertex_region` override in `MODEL_REGISTRY` wins, otherwise the `anthropic.vertex_region` config default (see [Configuration](#anthropic-vertex-ai-configuration) and [Per-Model Vertex Region](#per-model-vertex-region) below)
- **Client cache**: One `AsyncAnthropicVertex` client per (region, refusal-fallback chain), cached in a dict (`_clients`). `_get_client(region, fallback_chain)` lazily constructs a client for the resolved region; the same `vertex_project_id` is used for every region. Clients built for a non-empty fallback chain carry the SDK's `BetaRefusalFallbackMiddleware` (see **Refusal fallbacks** below); the health-check path uses the plain, middleware-free client
- **Session type**: Returns an `AnthropicSession` dataclass (stateless container holding messages, system prompt, tool definitions, and model name). Anthropic's API is stateless -- there is no persistent chat object
- **Max output tokens**: Dynamically looked up from `MODEL_REGISTRY` via `max_output_tokens` at each API call (falling back to 8192 if not found), allowing different Anthropic models to have different output token limits (e.g., Claude Opus 4.6 and Claude Opus 4.7 use 16,384, Claude Opus 4.8, Claude Sonnet 5, Claude Sonnet 5.5, Claude Opus 5, and Claude Opus 5.5 use 128,000, while Claude Haiku 4.5 and Claude Sonnet 4.6 use 8,192)
- **Streaming**: Processes `content_block_start`, `content_block_delta`, `content_block_stop`, `message_start`, and `message_delta` events; tracks the in-progress block via an explicit `current_block_type` and dispatches the stop event on it. Accumulates tool input JSON across delta events and yields a `StreamEvent` on `content_block_stop`. On `CancelledError`, flushes any in-progress text block and appends accumulated content blocks to `session.messages` before re-raising, so that `save_history()` can capture the partial assistant response
- **Adaptive thinking**: Gated per model by the `thinking_effort` key in `MODEL_REGISTRY` (currently `"medium"` on `claude-opus-4-8` and `claude-opus-5-5`, `"high"` on `claude-sonnet-5-5`). When set, every request carries `thinking: {"type": "adaptive"}` plus `output_config: {"effort": <level>}`; models without the key send no `thinking` param, which disables thinking on Opus 4.7/4.8 but runs adaptive thinking at the model's default effort on Opus 5 (thinking on by default).
  - Opus 5.5 cannot run with thinking off at all (`thinking: disabled` and manual `budget_tokens` both 400), so its registry entry pins the effort explicitly at the model's own `medium` default rather than relying on the API default.
  - Sonnet 5.5 is the same (`disabled` 400s; the lowest setting is `between_tools`, which is not used here), so its entry pins the model's own `high` default -- the level Sonnet 5 already runs at when the API default applies.
  - `thinking.display` stays at its default (`"omitted"`), so thinking blocks stream with empty text and nothing is yielded to the UI, but the blocks are still accumulated into `session.messages` -- `thinking` blocks with their `signature` (delivered via `signature_delta`) and `redacted_thinking` blocks verbatim -- because the API requires them to be replayed unmodified in the assistant turn, especially across tool-use round trips.
  - Two replay-safety guards: an assistant turn whose content is *only* thinking blocks is never persisted (the API strips prior-turn thinking server-side, so it would replay as an empty message and 400 the conversation), and on cancellation an in-progress thinking block without its final signature is dropped (unsigned thinking blocks are rejected on replay).
  - Keep a model's `thinking_effort` value constant: effort shapes the rendered prompt, so changing it between requests invalidates the prompt cache for the conversation. Thinking tokens are folded into Anthropic's `output_tokens`, so usage capture and pricing need no changes.
  - Thinking blocks are bound to the request prefix they were produced under (system prompt, tools, earlier messages) and, on Opus 5.5 and Sonnet 5.5, to the producing model: client-side compaction (`chat/compaction.py`) therefore strips `thinking`/`redacted_thinking` blocks from the kept span when it rebuilds an Anthropic history behind the new summary turn (older accounts would only have them ignored; accounts created on or after 2026-08-31 get a 400 when a prefix-mismatched Opus 5.5 or Sonnet 5.5 block is replayed).
  - A refusal fallback or model switch away from Opus 5.5 or Sonnet 5.5 runs without its thinking (dropped server-side, not an error).
  - Opus 5.5 also returns the model's between-tool-call narration as progress-update `thinking` blocks instead of `text` blocks, so at the default `display` no interim text streams to the UI between tool calls on that model. Sonnet 5.5 does the same for notes longer than a sentence or two (shorter remarks stay `text`).
  - Tests: `tests/test_anthropic_thinking.py`, `tests/test_compaction.py`
- **Refusal fallbacks**: Opus-5-class models and Sonnet 5.5 run safety classifiers that can decline a request with a normal HTTP 200 whose stream ends with `stop_reason: "refusal"` (benign cybersecurity work occasionally trips them; Opus 5.5 and Sonnet 5.5 add `bio` and `reasoning_extraction` categories, Sonnet 5.5 also `frontier_llm` and `general_harms`).
  - Models with a `refusal_fallback_models` entry in `MODEL_REGISTRY` (currently `claude-opus-5` and `claude-opus-5-5`, both -> `["claude-opus-4-8"]`, Anthropic's recommended fallback for cyber-category refusals, and `claude-sonnet-5-5` -> `["claude-sonnet-5"]`, the model Anthropic's own server-side default retries Sonnet 5.5 declines on) get the Anthropic SDK's client-side `BetaRefusalFallbackMiddleware` -- Vertex AI has no server-side `fallbacks` request param -- registered on the client.
  - On a refusal the middleware retries the request on each fallback model and splices the fallback's events onto the open stream behind a synthetic `fallback` seam content block.
  - Requirements and behavior:
    - the send path uses `client.beta.messages.stream` (the middleware only handles the beta surface);
    - the middleware is constructed with `betas=REFUSAL_FALLBACK_BETAS` (currently empty) instead of its SDK default `fallback-credit-2026-07-01` -- Vertex AI rejects that beta value with a 400 on *every* request the middleware handles, refused or not, which took Opus 5 down entirely -- so no fallback-credit token is minted;
    - a refusal *before* any output streamed still retries on the chain (the usual safety-classifier case), but a mid-output refusal has no credit to chain on and surfaces as `AnthropicStreamRefusal` with the partial text persisted, and the retry pays a cold prompt-cache write on the fallback model instead of being repriced;
    - each fallback entry is a patch against the original request that re-derives `max_tokens`/`thinking`/`output_config.effort` from the fallback model's own registry entry;
    - on a seam block the streaming loop drops accumulated pre-boundary `thinking`/`redacted_thinking` blocks (the continuation rules forbid echoing them) and never persists the seam itself (the API rejects it if replayed), records `{from_model, to_model, category}` in `session.last_usage["refusal_fallback"]` (surfaced through `UsageStats.raw_usage` into `llm_calls_anthropic.raw_usage`), logs at INFO, and yields a `model_fallback` StreamEvent (payload: from/to model ids + registry display names + category).
  - The switch is user-visible: `_stream_turn` in `chat/gemini_api/conversation.py` handles that event by flushing the pre-switch partial text as its own durable message, appending a `{"type": "model_fallback", ...}` marker row to the transcript, and emitting a `model_fallback` on_event -- the type is in `FLUSH_EVENT_TYPES` (chat/_flush_helper.py), so both rows persist to chat_history.json and fan out `message_appended` immediately.
  - The FE renders the row as a violet system notice ("<from> declined this request via its safety classifiers (<category>) — answered by <to>", the `model_fallback` branch in Message.tsx). Sub-agent runs ignore the event (fallback still works, no notice row).
  - A per-session `anthropic.BetaFallbackState` (created in `create_session`, entered around every request) pins follow-up turns of the run to the fallback model that accepted, so each turn doesn't re-refuse on the primary; the state is run-scoped -- a resumed conversation starts unpinned and tries the primary again.
  - A refusal that still reaches the streaming loop (the whole chain declined, or the model has no fallbacks) raises `AnthropicStreamRefusal` after persisting any partial replayable output -- the conversation loop's durable error surfacing turns it into a visible error bubble (with the refusal category) instead of the silent empty turn a refusal used to produce.
  - Tests: `tests/test_anthropic_refusal_fallback.py`
- **Tool results**: Formatted as `tool_result` content blocks with `tool_use_id`. When `extra_parts` are present (e.g., base64-encoded images or PDFs from `upload_file()`), the `content` field is converted to an array of content blocks: a text block with the result string followed by each extra part (image or document content block dicts)
- **File upload**: Supported for images (JPEG, PNG, GIF, WebP) and PDFs via base64 encoding. `upload_file()` reads the file, base64-encodes it, and returns a content block dict (`{"type": "image", "source": {"type": "base64", ...}}` for images, `{"type": "document", "source": {"type": "base64", ...}}` for PDFs).
  - Images are capped at 5MB and documents (PDFs) at the effective per-file cap derived from the Vertex 30MB request-payload ceiling; both caps come from `chat/llm/file_limits.py` (see [Attachment Size Limits](#attachment-size-limits)), and oversized files log and return `None` -- a defense-in-depth backstop behind the tool-handler pre-flight checks, protecting callers like composer attachments.
  - `make_file_part()` returns the dict as-is since it is already in the correct format for inclusion as an `extra_part` in tool results. Unsupported MIME types return `None` and the caller's fallback path handles the error messaging
- **Prompt caching**: Applies `cache_control: {"type": "ephemeral"}` breakpoints on up to 4 content blocks per request (the Vertex AI limit), reducing input token costs on cache hits. The 4 breakpoints are allocated as: system prompt (1), last tool definition (1), and last 2 user-role messages (2).
  - All cache annotations are applied to shallow copies at the API call boundary -- `session.system_prompt`, `session.tools`, and `session.messages` are never mutated. Cache effectiveness is logged at DEBUG level (visible in dev mode).
  - `session.last_usage` includes `cache_read_tokens` and `cache_creation_tokens` fields extracted from the `message_start` event. See [Prompt Caching](#prompt-caching) below for details
- **Text part creation**: `make_text_part()` returns `{"type": "text", "text": text}`, used for advisory `extra_parts` (e.g., limit cap notices from `ToolResultWithNotices`)
- **Turn warning injection**: Appends a `{"type": "text", "text": warning_text}` content block to the formatted tool results list
- **History serialization**: Messages are already plain dicts, so serialization is straightforward

### OpenRouterProvider

`OpenRouterProvider` in `chat/llm/openrouter_provider.py` wraps the `openai` SDK pointed at an OpenAI-compatible chat-completions API -- OpenRouter's (`https://openrouter.ai/api/v1`) for OpenRouter instances, or `<base_url>/v1` of a self-hosted server for `local` instances with the `openai` API type:

- **Authentication / endpoint**: One provider object per configured instance (`OpenRouterProvider(instance_id)`). `_endpoint()` resolves where requests go: an OpenRouter instance needs its API key from the inference-credential store (`effective_api_key(instance_id)` in `config/inference_providers.py`, editable in Settings > Inference Providers) and gets the OpenRouter attribution headers + accounting opt-in; a self-hosted instance needs its `base_url` from the instance config, sends the stored key as a bearer token only when one exists (a placeholder otherwise, since the SDK insists on a non-empty key and servers without `--api-key` ignore it) and gets a plain request (no OpenRouter-only `extra_body`, which some servers reject). `_get_client()` raises a descriptive `ValueError` when the instance has no key / no URL; `reset_cached_clients()` drops the cached `AsyncOpenAI` client so a newly saved key takes effect without a restart. The wire model id is the qualified id with its `<instance>:` prefix stripped (`split_model_id`); `max_tokens` comes from the instance model's catalog snapshot via `resolve_model()`
- **Session type**: An `OpenRouterSession` dataclass (stateless container, same pattern as `AnthropicSession`) holding OpenAI-format chat messages. The system prompt is NOT stored in the history -- it is prepended as a `role="system"` message at the API-call boundary
- **Streaming**: `stream=True` with `stream_options: {"include_usage": true}`. Text deltas are yielded live; tool calls stream as per-index name/argument fragments, so they are accumulated and emitted as complete `tool_call` events after the stream ends (with generated fallback ids for upstreams that omit them). Malformed `function.arguments` (a weaker model emitting non-JSON or a non-object) never reach dispatch or the replayed history: `parse_tool_arguments()` yields the `tool_call` event with empty `tool_args` and a model-facing `StreamEvent.tool_args_error` (parse position + raw preview), the conversation and sub-agent loops answer the call with the structured `invalid_tool_arguments_result()` error tool result instead of dispatching it, and the stored assistant message carries `{}` -- the server rejects every later request over any historical non-JSON arguments, which used to kill the conversation permanently. On `CancelledError`, partial text is flushed into the history (in-progress tool calls are dropped -- incomplete argument JSON can never be dispatched or replayed)
- **Usage capture**: The final usage chunk's `prompt_tokens` / `completion_tokens` / `total_tokens` plus the flattened detail fields `cached_prompt_tokens` (`prompt_tokens_details.cached_tokens`) and `reasoning_tokens` (`completion_tokens_details.reasoning_tokens`). `prompt_tokens` INCLUDES the cached subset (the Gemini-style convention), so `compute_new_input_tokens()` / `compute_total_context_tokens()` treat `"openrouter"` like `"gemini"`. Every request also sends the OpenRouter accounting opt-in `extra_body={"usage": {"include": True}}`, so the same chunk carries `cost` (USD charged by OpenRouter), `cost_details.upstream_inference_cost` (flattened to `upstream_inference_cost`; the upstream provider's charge on BYOK requests) and `is_byok`, captured into `raw_usage` verbatim (numeric strings coerced, anything malformed skipped) and persisted to the `llm_calls_openrouter.cost` / `upstream_inference_cost` / `is_byok` columns, where the admin cost reports prefer them over the list-price estimate (`upstream_inference_cost` counts only on BYOK rows -- OpenRouter also reports it, equal to `cost`, on ordinary requests). Vertex-served Gemini and Claude have no equivalent -- their usage objects are token counts only
- **Tool results**: Formatted as complete `role="tool"` messages referencing `tool_call_id` (unlike Anthropic's content blocks, the formatted list holds whole messages; `send_message_stream()` extends the history with them directly). `append_user_text()` / `inject_turn_warning()` append `role="user"` messages after the tool messages
- **History repair**: `repair_session_history()` enforces the same tool-call/tool-result pairing invariants as the Anthropic override (chat completions rejects unanswered `tool_calls`); a dangling tool call on the final message is left for the resume bucket; it also rewrites historical non-JSON `function.arguments` to `{}` so conversations persisted before the stream-time guard existed can take another turn
- **File upload**: Not supported -- `upload_file()` always returns `None` and the tool-handler fallback paths cover the messaging. `get_attach_limit_for_model()` resolves OpenRouter models to the smallest cap
- **Compaction**: Not supported for OpenRouter histories -- `_CUT_VALIDATORS` / `_WALKERS` in `chat/compaction.py` have no `"openrouter"` entry, so compaction reports nothing to compact

Tests: `tests/test_openrouter_provider.py`; the self-hosted endpoint resolution in `tests/test_local_inference.py`.

### OllamaProvider

`OllamaProvider` in `chat/llm/ollama_provider.py` is a subclass of `OpenRouterProvider` for self-hosted instances whose API type is `ollama`. Only the transport differs -- everything else (the `OpenRouterSession`, the OpenAI-format stored history and `sdk_history.json` envelope, tool-result formatting, pending-tool detection, history repair, `get_usage()`, the `llm_calls_openrouter` analytics table) is inherited, so the rest of the app keeps treating the model as an `"openrouter"`-family model and `_provider_name()` in `chat/gemini_api/history.py` still classifies it by `isinstance`.

- **Why native**: Ollama's OpenAI-compatible `/v1` shim has no way to set the context window per request; Ollama sizes every model at a small default (`num_ctx` 4096 on a GPU-less box, as its own startup log reports) and silently truncates the prompt beyond it -- for this app's system prompt that means losing the tool instructions. `POST /api/chat` takes `options.num_ctx`, so the provider sends the model's configured `context_length` (the `ModelSpec.max_input_tokens` of the instance snapshot; `DEFAULT_OLLAMA_CONTEXT_LENGTH` for an unlisted model) plus `num_predict` = the output cap on every request. The other native-only knobs (`keep_alive`, `think`) are left at the server defaults.
- **Message conversion** (`to_ollama_messages()`): the system prompt is prepended as a `system` message; assistant `tool_calls` carry their arguments as an object (Ollama's shape) and keep their id (recent Ollama versions emit and accept one; older ones ignore unknown fields); `role="tool"` results are tagged with `tool_name`, resolved from the preceding assistant message's calls; content-part arrays are flattened to text. Streamed chunks are converted back: `message.content` fragments become `text` events, `message.tool_calls` (arriving complete, arguments already an object -- a string is parsed through the same `parse_tool_arguments()` guard, so malformed text surfaces as `tool_args_error`) are collected and emitted as `tool_call` events after the stream ends, with the id Ollama sent or a generated `call_…` fallback, and the persisted assistant message has the same OpenAI shape the parent class writes (arguments as a JSON string).
- **Usage**: the final chunk's `prompt_eval_count` / `eval_count` (and `prompt_eval_cached_count` on recent versions) are recorded as `prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_prompt_tokens`, so `get_usage()` and the analytics row need no Ollama-specific fields.
- **Errors**: a non-2xx response or an `error` field in the stream raises `OllamaError(message, status_code)`, whose attributes `extract_error_message()` in `chat/llm/health.py` renders as `HTTP 404: model 'x' not found` etc. `check_model_access()` is a non-streaming one-token `/api/chat` call, which also loads the model -- so a model that does not fit in memory fails the health check rather than the first real send.
- **HTTP client**: an `httpx.AsyncClient` with a short connect timeout and a long read timeout (CPU prompt evaluation of a long context takes minutes); `reset_cached_clients()` drops it after a URL/key change.

Tests: `tests/test_local_inference.py` (stubbed `httpx.MockTransport`, no network).

## Attachment Size Limits

`chat/llm/file_limits.py` is the single source of truth for how many raw file bytes may be attached to a single model request, per provider/backend:

- **Anthropic on Vertex AI** -- `ANTHROPIC_VERTEX_MAX_ATTACH_BYTES`, an effective per-file cap (~15.75MB) derived from the Vertex 30MB request-payload ceiling after base64 inflation (4/3) and a headroom factor for the rest of the request; the derivation constants and comments are in the module. Images are additionally capped at `ANTHROPIC_IMAGE_MAX_BYTES` (5MB, an Anthropic API limit)
- **Gemini Vertex backend** -- `GEMINI_VERTEX_MAX_ATTACH_BYTES` (7MB per inline part; bytes are inlined into the request payload)

`get_attach_limit_for_model(model, mime_type)` resolves `(limit_bytes, backend_label)` for the model actually being called (conversation or sub-agent model). It never raises -- it runs inside tool handlers -- and unknown or empty model strings fall back to the smallest cap so a model-threading bug cannot reproduce an oversized-payload request failure. The module imports only from `chat.llm.config`, so both the providers and the `chat/gemini_api/tool_handlers/` handlers can import it without cycles.

Consumers: the pre-flight size checks in `_handle_get_workspace_file()` and `_handle_load_gmail_attachment()` reject oversized files with a structured tool error before any bytes attach to the request (see [Conversation Loop and Tool Integration](gemini-api.md) for the handler behavior and the design rationale); the providers use the constants as defense-in-depth caps in `upload_file()` for callers that bypass the tool handlers (e.g. composer attachments). Tests are in `tests/test_workspace_file_size_limits.py`.

## Canonical Tool Definitions

Tool definitions in `chat/llm/tool_schemas.py` use JSON Schema format and are organized into the same three tiers as the previous Gemini-specific declarations:

| Tier | Tools | Description |
|---|---|---|
| `BASE_TOOLS` | 9 tools | Shared by top-level agents and sub-agents (includes the `tool_call` meta tool) |
| `TOP_LEVEL_TOOLS` | 13 tools | `BASE_TOOLS` + `agent_task`, `agent_task_parallel`, `agent_task_parallel_template`, `create_action_request` |
| `SUB_AGENT_TOOLS` | 10 tools | `BASE_TOOLS` + `agent_task_response` |

Additionally, `TOOL_CALL_SPEC` defines the meta `tool_call` tool (included in `BASE_TOOLS`) and `TOOL_CALL_REGISTRY` maps tool names to their `ToolSpec` definitions for tools that are dispatched through `tool_call` rather than registered as individual LLM tools. See [Conversation Loop and Tool Integration -- Tool Declarations](gemini-api.md#tool-declarations) for the registry roster and per-tool descriptions; `TOOL_CALL_REGISTRY` in `chat/llm/tool_schemas.py` is the source of truth.

The `set_conversation_name` entry is excluded from sub-agent visibility via the `exclude` parameter on `_build_dynamic_tools_section()`. See [Conversation Loop and Tool Integration](gemini-api.md) for the meta tool pattern details.

The `agent_task` and `agent_task_parallel` tool descriptions list the valid sub-agent model IDs (`gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`, `claude-haiku-4.5`, `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, `claude-opus-5-5`) in the optional `model` parameter and note that Gemini 3.1 Pro (`gemini-3.1-pro-preview`) is NOT available to sub-agents. Gemini 3.1 Pro is enforced as off-limits for sub-agents via the `SUB_AGENT_DISALLOWED_MODELS` set in `chat/gemini_api/constants.py` -- see [Sub-Agent Model Restriction](gemini-api.md#sub-agent-model-restriction).

The `agent_task_parallel_template` tool restricts its required `model` parameter to cheaper models defined in `TEMPLATE_BATCH_ALLOWED_MODELS` in `chat/gemini_api/constants.py` (`claude-haiku-4.5`, `claude-sonnet-4-6`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, and `gemini-3.8-flash`). `TEMPLATE_BATCH_ALLOWED_MODELS` is deliberately duplicated in `chat/llm/tool_schemas.py` (to populate the tool's `model` enum) and kept in sync with the copy in `constants.py`.

### Provider-Specific Descriptions

Some tools need different descriptions depending on the provider (e.g., `get_workspace_file` and `load_gmail_attachment` describe different file delivery mechanisms for Gemini vs Anthropic). The `provider_descriptions` field on `ToolSpec` holds per-provider overrides. The `_apply_provider_descriptions(tools, provider)` helper in `chat/llm/tool_schemas.py` deep-copies the tool list, replaces each tool's `description` with the matching provider override (if present), and strips the `provider_descriptions` key so downstream converters only see a flat `description`. Both converter functions call this helper before converting.

### Converter Functions

- `to_gemini_declarations(tools)` -- Applies Gemini-specific description overrides via `_apply_provider_descriptions()`, then converts `ToolSpec` dicts to `types.FunctionDeclaration` objects for the Gemini SDK. Recursively maps JSON Schema types to Gemini `types.Schema` objects via `_json_schema_to_gemini_schema()`
- `to_anthropic_tools(tools)` -- Applies Anthropic-specific description overrides via `_apply_provider_descriptions()`, then converts `ToolSpec` dicts to Anthropic tool definition dicts with `name`, `description`, and `input_schema` keys
- `to_openai_tools(tools)` -- Applies `"openai"`-keyed description overrides, then converts `ToolSpec` dicts to OpenAI function-tool dicts (`{"type": "function", "function": {name, description, parameters}}`) for the OpenRouter provider

## Prompt Caching

The Anthropic provider implements prompt caching via `cache_control: {"type": "ephemeral"}` breakpoints to reduce input token costs on repeated API calls within a conversation. This is implemented entirely within `send_message_stream()` in `chat/llm/anthropic_provider.py`.

### Cache Breakpoint Allocation

Vertex AI limits `cache_control` to 4 blocks per request. The provider allocates these as: (1) system prompt, (2) last tool definition, (3-4) last 2 user-role messages. For user messages with string content, the string is converted to a single-element block-format list with `cache_control`. For user messages with list content (e.g., `tool_result` blocks), `cache_control` is added only to the last block in the list, so each message contributes exactly 1 cache breakpoint. See `send_message_stream()` in `chat/llm/anthropic_provider.py` for the implementation.

### Immutability Guarantee

Cache annotations only exist in the copies sent to the API. The original session data is never mutated:

- `session.system_prompt` remains a plain string
- `session.tools` list and its dicts are not modified (the last tool is shallow-copied)
- `session.messages` dicts are not modified (annotated messages are rebuilt as new dicts)

### Cache Usage Tracking

The `message_start` event from the Anthropic API includes `cache_read_input_tokens` and `cache_creation_input_tokens`. These are stored in `session.last_usage` as `cache_read_tokens` and `cache_creation_tokens`, and their sum is stored as `cached_tokens` for compatibility with the `UsageStats` type.

The `AnthropicProvider.get_usage()` method populates the `UsageStats` dataclass with all five coalesced fields (`input_tokens`, `output_tokens`, `cached_tokens`, `cache_creation_tokens`, `cache_read_tokens`) and also a `raw_usage` dict preserving the provider-native `input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`, plus the cache-creation TTL split `cache_creation_5m_input_tokens` / `cache_creation_1h_input_tokens` (5m writes bill 1.25x, 1h writes 2x) captured off the API's `usage.cache_creation` detail object -- included only when the SDK reported them, and raw-analytics only (the coalesced fields are unaffected).

Anthropic reports no single total, so `raw_usage` omits one. A DEBUG-level log line reports the cache token breakdown after each API call (see [Logging Architecture](logging.md) for how to enable DEBUG output in dev mode).

## Gemini Vertex AI Configuration

Gemini models authenticate via Google Cloud Application Default Credentials (ADC), the same mechanism the Anthropic provider uses. Configuration lives in the `gemini_vertex` section of `server_config.json`:

- `vertex_project_id` -- Google Cloud project id. Falls back to `anthropic.vertex_project_id` if unset (so a single-project deployment does not need to repeat itself)
- `vertex_region` -- Vertex location. Defaults to `"global"`, which is the only endpoint that serves Gemini 3.x publisher models -- regional endpoints (e.g. `us-central1`, `us-east5`) return 404 for these models. Older Gemini publisher models that still require a regional location can override this

Environment overrides: `GEMINI_VERTEX_PROJECT_ID` and `GEMINI_VERTEX_REGION` take precedence over the JSON file. Defaults and overrides are applied in `load_server_config()` in `config/server_config.py`. If `vertex_project_id` cannot be resolved (no `gemini_vertex` value, no `anthropic` fallback) and a user selects a Gemini model, `GeminiProvider._get_client()` raises a descriptive `ValueError`.

The region is intentionally **not** inherited from `anthropic.vertex_region`: Anthropic Vertex defaults to `us-east5` (the Claude region) which is not a valid Gemini location.

ADC credentials must be available on the machine (typically via `gcloud auth application-default login` in development or a service account in production).

## Anthropic Vertex AI Configuration

The Anthropic provider authenticates via Google Cloud Application Default Credentials (ADC). Configuration is in the `anthropic` section of `server_config.json` (`vertex_project_id` required, `vertex_region` defaults to `us-east5`). The `vertex_region` config value is the *default* region; individual models can override it (see [Per-Model Vertex Region](#per-model-vertex-region) below). If `vertex_project_id` is not configured and a user selects an Anthropic model, a `ValueError` is raised.

ADC credentials must be available on the machine (typically via `gcloud auth application-default login` in development or a service account in production).

### Per-Model Vertex Region

The Anthropic provider selects the Vertex region per model rather than using a single region for all Claude models. `_resolve_region(model)` in `chat/llm/anthropic_provider.py` returns the model's `vertex_region` override from `MODEL_REGISTRY` if present, otherwise the `anthropic.vertex_region` config default (`us-east5`). Clients are cached one-per-region in `_clients`.

`claude-opus-4-7`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-sonnet-5-5`, `claude-opus-5`, and `claude-opus-5-5` carry `"vertex_region": "global"` because Vertex AI serves these models only on the `global` endpoint (and the `us`/`eu` multi-region endpoints), not on individual regions like `us-east5` -- a regional request 429s with `RESOURCE_EXHAUSTED`. The older models (Opus 4.6, Sonnet 4.6, Haiku 4.5) have no override and continue to use the config default region.

## Design Decisions

**Why a provider abstraction instead of direct SDK calls?**
The conversation loop, sub-agent execution, and tool dispatch logic are complex. Without an abstraction, adding a new provider would require duplicating or heavily modifying these components. The `LLMProvider` interface isolates provider-specific concerns (session creation, streaming format, tool result formatting) behind a stable contract.

**Why JSON Schema for tool definitions?**
JSON Schema is the de facto standard for describing function signatures across LLM providers. Gemini uses a proprietary `types.Schema` format, while Anthropic uses JSON Schema directly. Maintaining a single canonical definition and converting at the boundary avoids drift between provider-specific copies.

**Why Vertex AI for Anthropic instead of the direct Anthropic API?**
The deployment environment already has Google Cloud infrastructure and credentials. Using Vertex AI avoids managing a separate Anthropic API key and allows billing to be consolidated through Google Cloud.

**Why a per-model backend flag on `GeminiProvider` instead of a second provider class?**
The two `google-genai` transports (developer endpoint vs Vertex) emit identical message shapes -- both produce SDK `Content` objects with the same `parts` structure -- so the on-disk history envelope, the `_active_chats` cache key (`provider: "gemini"`), and the frontend provider-lock all behave the same across backends.

A separate `GeminiVertexProvider` class would have forced cross-provider semantics on a backend swap: discarded in-memory history, a segmented model dropdown, and a new branch in `_provider_name()` in `chat/gemini_api/history.py`. The backend flag in `MODEL_REGISTRY` keeps the swap localized to client construction and file-upload transport, and the older Gemini models stay on the developer API unchanged.

**Why inline `Part.from_bytes` for Vertex file uploads instead of the File Upload API?**
The `client.aio.files.upload()` endpoint is part of the developer Gen AI API surface and is not available on Vertex. Vertex Gemini accepts inline base64 parts directly on the request, capped at a few MB per part. `GeminiProvider.upload_file()` reads the file inline and returns `types.Part.from_bytes(...)`; `make_file_part()` short-circuits when handed a fully-formed `Part`. The MIME allow-list mirrors the Anthropic provider for consistency, and the 7MB per-part cap comes from `chat/llm/file_limits.py` (see [Attachment Size Limits](#attachment-size-limits)). Audio/video are not on the allow-list today even though Vertex supports them inline -- revisit if needed.

**Why lazy singleton providers?**
Provider instances hold SDK client connections. Creating them lazily ensures that startup does not fail if a provider's credentials are not configured (e.g., if only Gemini is being used, the Anthropic provider is never instantiated).

**Why a per-model Vertex region instead of one region for all Claude models?**
Vertex AI serves the newest Claude models (Opus 4.7, Opus 4.8, and Sonnet 5) only on the `global` endpoint, not on individual regions like `us-east5`; a regional request returns a 429 `RESOURCE_EXHAUSTED` quota error. A single shared region cannot satisfy both the global-only models and the regional older models, so the provider resolves the region per model (override in `MODEL_REGISTRY`, falling back to the config default) and caches one client per region.

**Why base64 encoding for Anthropic file uploads instead of a File Upload API?**
Anthropic's API does not have a File Upload API equivalent to Gemini's. Instead, Anthropic supports inline base64-encoded content blocks for images and PDFs within tool results. `AnthropicProvider.upload_file()` reads the file, base64-encodes it, and returns a content block dict that `format_tool_results()` appends to the `tool_result` content array.

This approach works within the API's request constraints (5MB per image; a 30MB request-payload ceiling on Vertex, both captured as caps in `chat/llm/file_limits.py` -- see [Attachment Size Limits](#attachment-size-limits)) and keeps the same `upload_file()`/`make_file_part()` interface so the calling code in tool handlers remains provider-agnostic. Unsupported MIME types and oversized files return `None` and the caller handles the fallback.

**Why prompt caching for Anthropic and how are the 4 breakpoints allocated?**
Anthropic's Vertex AI API supports prompt caching with `cache_control: {"type": "ephemeral"}` breakpoints, but limits each request to 4. The system prompt and tool definitions are stable across turns and form natural cache anchors (breakpoints 1 and 2). The remaining 2 breakpoints go to the last 2 user-role messages, which represent the most-recently-reused conversation prefix -- earlier messages are already covered by the cache created when they were the "last 2". Annotations are applied to shallow copies at the API call boundary so the session's own data stays clean, avoiding subtle bugs if the same session is reused or serialized.

**Why is the provider locked after the first message?**
Message history format and caching semantics differ between providers (Gemini uses SDK `Content` objects with Pydantic serialization; Anthropic uses plain dict messages). Switching providers mid-conversation would require converting the existing history to a different format and would invalidate any cached context.

Locking the provider on the first message avoids these incompatibilities. Users can still switch between models within the same provider (e.g., Gemini Pro to Gemini Flash). The locking is enforced in the frontend (the shared `frontend/src/components/Composer.tsx` filters the model dropdown; `ConversationModelsContext.tsx` persists the lock in localStorage). See [Frontend Architecture](frontend.md) (Provider Locking) for the lock trigger sites and the home-composer draft-key exception.

**Why provider-specific tool descriptions?**
The file delivery mechanism differs between providers (Gemini uses a File Upload API with URI references; Anthropic uses base64-encoded inline content). Tool descriptions that mention "uploads to the Gemini File API" would be misleading when running on Anthropic. The `provider_descriptions` field on `ToolSpec` allows each tool to carry provider-specific description overrides that are applied at conversion time, keeping tool descriptions contextually accurate without duplicating the entire tool definition.
