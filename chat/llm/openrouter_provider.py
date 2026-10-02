"""OpenAI chat-completions provider implementation using the openai SDK.

OpenRouter (https://openrouter.ai) exposes many third-party models behind
one OpenAI-compatible chat-completions API, authenticated by a single API
key. The key is managed as an editable API-key inference provider
(``config/inference_providers.py``), so personal deployments can run
non-Vertex models with nothing but a key pasted into Settings >
Inference Providers.

The same class serves **self-hosted** instances (kind ``local`` with API
type ``openai``): the instance's own base URL replaces OpenRouter's, the
key is optional (sent as a bearer token only when stored) and the
OpenRouter-only request extensions are left out. Every mainstream local
server -- llama.cpp ``llama-server``, vLLM, LM Studio, LocalAI, SGLang,
Ollama's compatibility layer -- speaks this protocol. Self-hosted
instances with API type ``ollama`` use the ``OllamaProvider`` subclass
(chat/llm/ollama_provider.py), which keeps this class's session/history
format but talks Ollama's native API.

Session history uses the OpenAI chat message format (plain dicts):
``{"role": "user"|"assistant"|"tool", "content": ...}`` with assistant
tool calls in ``message["tool_calls"]`` and each tool result as its own
``role="tool"`` message referencing ``tool_call_id``. The system prompt is
NOT part of the stored history -- it is prepended at the API-call boundary,
mirroring how the Anthropic provider keeps ``system`` out of ``messages``.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from chat.llm.base import LLMProvider, StreamEvent, UsageStats, ToolSpec
from chat.llm.tool_schemas import to_openai_tools

logger = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    """Coerce a usage accounting field (float, int, or numeric string) to
    float; None for anything else, so a malformed value is skipped rather
    than recorded."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# Replayed in place of malformed ``function.arguments`` so the stored
# history stays valid for the next request (chat-completions servers reject
# the WHOLE request when any historical tool call carries non-JSON
# arguments, which would otherwise poison the conversation permanently).
EMPTY_ARGUMENTS_JSON = "{}"

# How much of the malformed argument text is echoed back to the model.
_RAW_ARGUMENTS_PREVIEW_CHARS = 2000


def parse_tool_arguments(raw: Any) -> tuple[dict, str]:
    """Parse a tool call's ``function.arguments`` into a dict.

    Returns ``(arguments, error)``: ``error`` is empty on success and
    otherwise a model-facing explanation (parse error position or wrong
    JSON type) with a preview of the raw text, in which case ``arguments``
    is ``{}``. An empty/absent value is a valid no-argument call.
    """
    if isinstance(raw, dict):
        return raw, ""
    if raw is None:
        return {}, ""
    if not isinstance(raw, str):
        raw = str(raw)
    if not raw.strip():
        return {}, ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, (
            f"Tool call arguments were not valid JSON ({exc.msg} at "
            f"line {exc.lineno} column {exc.colno}). Received: "
            f"{_preview(raw)}"
        )
    if not isinstance(parsed, dict):
        return {}, (
            "Tool call arguments must be a JSON object, got "
            f"{type(parsed).__name__}. Received: {_preview(raw)}"
        )
    return parsed, ""


def _preview(raw: str) -> str:
    if len(raw) <= _RAW_ARGUMENTS_PREVIEW_CHARS:
        return raw
    return raw[:_RAW_ARGUMENTS_PREVIEW_CHARS] + "... (truncated)"


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Optional attribution headers OpenRouter uses for its app rankings; harmless
# to send and they identify this deployment's traffic in the OpenRouter
# dashboard.
_OPENROUTER_HEADERS = {"X-Title": "Quest"}


@dataclass
class OpenRouterSession:
    """Stateless session container for OpenRouter conversations.

    The chat-completions API is stateless (no persistent chat object), so
    the "session" is just a data container holding the messages history,
    system prompt, tool definitions, and model name -- same pattern as
    AnthropicSession.
    """
    model: str
    system_prompt: str
    tools: list[dict]
    messages: list[dict] = field(default_factory=list)
    last_usage: dict = field(default_factory=dict)


class OpenRouterProvider(LLMProvider):
    """LLMProvider implementation for OpenRouter's OpenAI-compatible API."""

    def __init__(self, instance_id: str | None = None):
        # One provider object per configured OpenRouter instance
        # (config/inference_providers.py); each reads its own API key.
        # None selects the legacy single-configuration instance.
        from config.inference_providers import LEGACY_OPENROUTER_INSTANCE_ID

        self.instance_id = instance_id or LEGACY_OPENROUTER_INSTANCE_ID
        self._client = None
        # Set alongside the client: whether OpenRouter-only request
        # extensions (usage accounting opt-in) apply to this endpoint.
        self._is_openrouter = True

    def _endpoint(self) -> dict:
        """Resolve where this instance's requests go.

        Returns ``{"base_url", "api_key", "headers", "openrouter"}``.
        OpenRouter instances need their stored key; a self-hosted instance
        needs its base URL and uses the stored key only when one exists
        (the openai SDK insists on a non-empty key, so a placeholder is
        sent otherwise -- local servers without ``--api-key`` ignore it).

        Raises:
            ValueError: descriptive "not configured" message for the admin.
        """
        from config.inference_providers import (
            effective_api_key,
            get_instance,
            is_endpoint_kind,
        )

        instance = get_instance(self.instance_id)
        api_key, _source = effective_api_key(self.instance_id)
        if instance is not None and is_endpoint_kind(instance["kind"]):
            base_url = instance.get("base_url")
            if not base_url:
                raise ValueError(
                    f"No server URL configured for self-hosted inference "
                    f"instance '{self.instance_id}'. Set it in Settings > "
                    "Inference Providers (admin only)."
                )
            return {
                "base_url": f"{base_url}/v1",
                "api_key": api_key or "no-key",
                "headers": None,
                "openrouter": False,
            }
        if not api_key:
            raise ValueError(
                f"OpenRouter API key not configured for instance "
                f"'{self.instance_id}'. Add it in Settings > Inference "
                "Providers (admin only)."
            )
        return {
            "base_url": OPENROUTER_BASE_URL,
            "api_key": api_key,
            "headers": _OPENROUTER_HEADERS,
            "openrouter": True,
        }

    def _get_client(self):
        """Return a cached AsyncOpenAI client for this instance's endpoint.

        The API key comes from this instance's file in the
        inference-credential store (admin Settings > Inference Providers);
        a self-hosted instance's base URL from its config entry.
        """
        if self._client is not None:
            return self._client

        from openai import AsyncOpenAI

        endpoint = self._endpoint()
        self._is_openrouter = endpoint["openrouter"]
        self._client = AsyncOpenAI(
            base_url=endpoint["base_url"],
            api_key=endpoint["api_key"],
            default_headers=endpoint["headers"],
        )
        return self._client

    def reset_cached_clients(self) -> None:
        """Drop the cached client so a newly saved API key takes effect."""
        self._client = None

    def _get_openrouter_model_id(self, model: str) -> str:
        """Strip the instance qualifier: ``openrouter:deepseek/x`` -> the
        wire id ``deepseek/x`` (bare legacy ids pass through; a self-hosted
        ``local:qwen2.5:0.5b`` keeps the colon inside its wire id)."""
        from config.inference_providers import split_model_id
        return split_model_id(model)[1]

    async def check_model_access(self, model: str) -> None:
        """Issue a minimal completion call to verify the model actually works.

        Used by the admin inference-provider health check: catches a revoked
        or unfunded key, a model id OpenRouter no longer serves, and quota
        exhaustion. max_tokens=1 keeps the cost negligible.
        """
        client = self._get_client()
        await client.chat.completions.create(
            model=self._get_openrouter_model_id(model),
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )

    def create_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list | None = None,
    ) -> Any:
        """Create an OpenRouter session (stateless container)."""
        return OpenRouterSession(
            model=model,
            system_prompt=system_prompt,
            tools=to_openai_tools(tools),
            messages=list(history) if history else [],
        )

    async def send_message_stream(
        self,
        session: Any,
        message: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat-completions response as normalized StreamEvents.

        Text deltas are yielded live; tool calls stream as argument
        fragments keyed by index, so they are accumulated and emitted as
        complete tool_call events once the stream ends. The final usage
        chunk (``stream_options.include_usage``) is captured into
        ``session.last_usage``.
        """
        client = self._get_client()

        if isinstance(message, str):
            session.messages.append({"role": "user", "content": message})
        elif isinstance(message, list):
            # Tool results are already complete role-tagged messages
            # (role="tool" entries, optionally followed by user text).
            session.messages.extend(message)

        session.last_usage = {}

        from chat.llm.config import resolve_model
        spec = resolve_model(session.model)
        max_tokens = spec.max_output_tokens if spec else 8192

        api_kwargs: dict = {
            "model": self._get_openrouter_model_id(session.model),
            "max_tokens": max_tokens,
            "messages": (
                [{"role": "system", "content": session.system_prompt}]
                + session.messages
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._is_openrouter:
            # OpenRouter-only extension: ask for the USD amount charged for
            # this request in the final usage chunk (``usage.cost``), which
            # the analytics layer prefers over its list-price estimate.
            # Self-hosted servers get a plain request (some reject unknown
            # top-level fields).
            api_kwargs["extra_body"] = {"usage": {"include": True}}
        if session.tools:
            api_kwargs["tools"] = session.tools

        # Accumulators: text so far, and per-index partial tool calls
        # (OpenAI streams tool-call name/arguments as fragments keyed by
        # ``index``; the id arrives on the first fragment).
        accumulated_text = ""
        tool_calls_by_index: dict[int, dict] = {}

        def _finalized_tool_calls() -> list[dict]:
            # Malformed arguments are replayed as ``{}``: the raw text would
            # 400 every later request for this conversation, and the model
            # learns what went wrong from the error tool result the
            # conversation loop returns for the call.
            calls = []
            for index in sorted(tool_calls_by_index):
                partial = tool_calls_by_index[index]
                raw_arguments = partial["arguments"] or EMPTY_ARGUMENTS_JSON
                _args, error = parse_tool_arguments(raw_arguments)
                calls.append({
                    "id": partial["id"],
                    "type": "function",
                    "function": {
                        "name": partial["name"],
                        "arguments": (
                            EMPTY_ARGUMENTS_JSON if error else raw_arguments
                        ),
                    },
                })
            return calls

        def _append_assistant_message(include_tool_calls: bool) -> None:
            tool_calls = _finalized_tool_calls() if include_tool_calls else []
            if not accumulated_text and not tool_calls:
                return
            assistant_message: dict = {
                "role": "assistant",
                "content": accumulated_text or None,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            session.messages.append(assistant_message)

        try:
            stream = await client.chat.completions.create(**api_kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    self._capture_usage(session, usage)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    accumulated_text += delta.content
                    yield StreamEvent(type="text", text=delta.content)
                for tc_delta in delta.tool_calls or []:
                    index = tc_delta.index or 0
                    partial = tool_calls_by_index.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if tc_delta.id:
                        partial["id"] = tc_delta.id
                    function = tc_delta.function
                    if function is not None:
                        if function.name:
                            partial["name"] += function.name
                        if function.arguments:
                            partial["arguments"] += function.arguments
        except asyncio.CancelledError:
            # Flush partial text so save_history() captures it; in-progress
            # tool calls are dropped (incomplete argument JSON can never be
            # dispatched or replayed).
            _append_assistant_message(include_tool_calls=False)
            raise

        # Assign fallback ids to any tool call the provider streamed without
        # one (seen from some OpenRouter upstreams) -- tool results must
        # reference a non-empty tool_call_id.
        for partial in tool_calls_by_index.values():
            if not partial["id"]:
                partial["id"] = f"call_{uuid.uuid4().hex[:24]}"

        _append_assistant_message(include_tool_calls=True)

        for index in sorted(tool_calls_by_index):
            partial = tool_calls_by_index[index]
            tool_args, error = parse_tool_arguments(partial["arguments"])
            if error:
                logger.warning(
                    "Model %s streamed malformed arguments for tool call %s "
                    "(%s): %s",
                    session.model, partial["name"], partial["id"],
                    partial["arguments"][:200],
                )
            yield StreamEvent(
                type="tool_call",
                tool_name=partial["name"],
                tool_args=tool_args,
                tool_id=partial["id"],
                tool_args_error=error,
            )

    @staticmethod
    def _capture_usage(session: Any, usage: Any) -> None:
        """Record the stream's usage object into session.last_usage.

        Flattens the two nested detail objects into the raw keys the
        analytics layer stores (``cached_prompt_tokens`` from
        ``prompt_tokens_details.cached_tokens``, ``reasoning_tokens`` from
        ``completion_tokens_details.reasoning_tokens``), skipping fields the
        provider did not populate -- same convention as the Gemini provider.

        The OpenRouter-specific accounting extension fields (returned
        because the request opts in with ``usage: {"include": true}``) are
        captured the same way: ``cost`` (USD charged by OpenRouter),
        ``upstream_inference_cost`` (``cost_details.upstream_inference_cost``,
        the upstream provider's charge on BYOK requests) and ``is_byok``.
        The openai SDK keeps unknown usage fields as pydantic extras, so
        plain attribute access reaches them.
        """
        last = session.last_usage
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value is not None:
                last[key] = value
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(prompt_details, "cached_tokens", None)
        if cached is not None:
            last["cached_prompt_tokens"] = cached
        completion_details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(completion_details, "reasoning_tokens", None)
        if reasoning is not None:
            last["reasoning_tokens"] = reasoning
        cost = _as_float(getattr(usage, "cost", None))
        if cost is not None:
            last["cost"] = cost
        cost_details = getattr(usage, "cost_details", None)
        if isinstance(cost_details, dict):
            upstream = cost_details.get("upstream_inference_cost")
        else:
            upstream = getattr(cost_details, "upstream_inference_cost", None)
        upstream = _as_float(upstream)
        if upstream is not None:
            last["upstream_inference_cost"] = upstream
        is_byok = getattr(usage, "is_byok", None)
        if isinstance(is_byok, bool):
            last["is_byok"] = is_byok

    def format_tool_results(
        self,
        session: Any,
        tool_results: list[dict[str, Any]],
    ) -> Any:
        """Format tool results as a list of role="tool" chat messages.

        Returned list entries are complete messages (unlike Anthropic's
        content blocks); send_message_stream() extends the history with
        them directly. extra_parts (advisory text parts from
        make_text_part) are folded into the tool message as a content-part
        array; file parts are unsupported (upload_file returns None) and
        any None entries are skipped.
        """
        messages = []
        for tr in tool_results:
            result_content = tr["result"]
            extra_parts = [p for p in tr.get("extra_parts", []) if p is not None]
            if extra_parts:
                content: Any = [
                    {"type": "text", "text": result_content},
                    *extra_parts,
                ]
            else:
                content = result_content
            messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_id"],
                "content": content,
            })
        return messages

    def append_user_text(self, formatted_results: Any, text: str) -> None:
        """Append a user message after the tool-result messages."""
        formatted_results.append({"role": "user", "content": text})

    def get_usage(self, session: Any) -> UsageStats:
        """Extract token usage from the last stream.

        ``prompt_tokens`` INCLUDES the cached portion
        (``prompt_tokens_details.cached_tokens`` is the cache-hit subset) --
        the Gemini-style convention, which the coalesced fields follow.
        """
        usage = getattr(session, "last_usage", {})
        raw_usage = {
            key: usage[key]
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_prompt_tokens",
                "reasoning_tokens",
                "cost",
                "upstream_inference_cost",
                "is_byok",
            )
            if key in usage
        }
        return UsageStats(
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_tokens=usage.get("cached_prompt_tokens", 0),
            raw_usage=raw_usage,
        )

    def save_history(self, session: Any) -> list[dict]:
        """Serialize session history (already plain JSON-safe dicts)."""
        return session.messages

    def load_history(self, data: list[dict]) -> list:
        """Deserialize saved history for session restoration."""
        return data

    def get_pending_tool_uses(self, session: Any) -> list[tuple[str, str]]:
        """Return pending tool calls on the last assistant message."""
        return [
            (tid, tname)
            for tid, tname, _args in self.get_pending_tool_use_args(session)
        ]

    def get_pending_tool_use_args(self, session: Any) -> list[tuple[str, str, dict]]:
        """Return pending tool calls with their parsed arguments."""
        messages = getattr(session, "messages", None) or []
        return self.get_pending_tool_use_args_from_history(list(messages))

    def get_pending_tool_use_args_from_history(
        self, history: list[dict],
    ) -> list[tuple[str, str, dict]]:
        """Inspect serialized history for unanswered tool calls.

        Tool results immediately follow their assistant message as
        ``role="tool"`` messages, so a dangling tool call can only live on
        the final message -- checking ``history[-1]`` is sufficient, same
        as the Anthropic provider.
        """
        if not history:
            return []
        try:
            last = history[-1]
            if not isinstance(last, dict) or last.get("role") != "assistant":
                return []
            pending: list[tuple[str, str, dict]] = []
            for tool_call in last.get("tool_calls") or []:
                if not isinstance(tool_call, dict) or not tool_call.get("id"):
                    continue
                function = tool_call.get("function") or {}
                raw_args = function.get("arguments")
                try:
                    args = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                pending.append(
                    (tool_call["id"], function.get("name", "") or "", args)
                )
            return pending
        except Exception:
            logger.debug(
                "Failed to inspect pending tool calls on OpenRouter history",
                exc_info=True,
            )
            return []

    def repair_session_history(self, session: Any) -> int:
        """Repair orphaned tool_calls / tool messages in place.

        The chat-completions API enforces the same pairing invariants as
        Anthropic: every assistant ``tool_calls`` entry must be answered by
        a following ``role="tool"`` message, and every tool message must
        reference a tool call from the preceding assistant message. A
        dangling tool call on the FINAL message is left alone -- that is the
        legitimate suspend shape the resume bucket closes.

        Also replaces malformed ``function.arguments`` (non-JSON text a
        weaker model streamed before this guard existed) with ``{}``: the
        server rejects the whole request over any such historical call, so
        without this the conversation could never take another turn.
        """
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list):
            return 0
        repaired = self._repair_malformed_arguments(messages)
        if len(messages) < 2:
            return repaired

        def _tool_call_ids(msg: Any) -> set[str]:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                return set()
            return {
                tc["id"] for tc in msg.get("tool_calls") or []
                if isinstance(tc, dict) and tc.get("id")
            }

        def _interrupted_result(tool_id: str) -> dict:
            return {
                "role": "tool",
                "tool_call_id": tool_id,
                "content": json.dumps({
                    "status": "interrupted",
                    "note": (
                        "The previous tool call was interrupted before "
                        "completing. Retry if needed."
                    ),
                }),
            }

        # Pass 1: every tool call before the final message must be answered
        # by the immediately following tool messages; close missing ones
        # with synthetic "interrupted" results.
        i = 0
        while i < len(messages) - 1:
            missing = _tool_call_ids(messages[i])
            if missing:
                j = i + 1
                while j < len(messages):
                    msg = messages[j]
                    if not isinstance(msg, dict) or msg.get("role") != "tool":
                        break
                    missing.discard(msg.get("tool_call_id"))
                    j += 1
                if missing:
                    for offset, tool_id in enumerate(sorted(missing)):
                        messages.insert(i + 1 + offset, _interrupted_result(tool_id))
                    repaired += len(missing)
            i += 1

        # Pass 2: drop tool messages that do not answer a tool call from the
        # nearest preceding assistant message (including duplicates).
        idx = 0
        current_ids: set[str] = set()
        answered: set[str] = set()
        while idx < len(messages):
            msg = messages[idx]
            role = msg.get("role") if isinstance(msg, dict) else None
            if role == "assistant":
                current_ids = _tool_call_ids(msg)
                answered = set()
            elif role == "tool":
                tool_id = msg.get("tool_call_id")
                if tool_id not in current_ids or tool_id in answered:
                    messages.pop(idx)
                    repaired += 1
                    continue
                answered.add(tool_id)
            else:
                current_ids = set()
                answered = set()
            idx += 1

        return repaired

    @staticmethod
    def _repair_malformed_arguments(messages: list) -> int:
        """Replace non-JSON-object ``function.arguments`` with ``{}`` in place.

        Returns the number of tool calls rewritten.
        """
        repaired = 0
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tool_call in msg.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                raw = function.get("arguments")
                if raw is None:
                    continue
                _args, error = parse_tool_arguments(raw)
                if error:
                    function["arguments"] = EMPTY_ARGUMENTS_JSON
                    repaired += 1
        return repaired

    async def upload_file(
        self,
        file_path: str,
        mime_type: str,
        display_name: str = "",
        model: str = "",
    ) -> Any | None:
        """File attachments are not supported; always returns None.

        The tool-handler fallback path (inline text extraction / structured
        errors) covers unsupported-attachment messaging.
        """
        return None

    def make_file_part(self, file_ref: Any) -> Any:
        """Pass-through (upload_file never produces a ref)."""
        return file_ref

    def make_text_part(self, text: str) -> Any:
        """Create an OpenAI text content part dict."""
        return {"type": "text", "text": text}

    def inject_turn_warning(self, formatted_results: Any, warning_text: str) -> None:
        """Append a user-text warning message to formatted tool results."""
        formatted_results.append({"role": "user", "content": warning_text})
