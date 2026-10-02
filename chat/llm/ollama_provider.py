"""Ollama native-API transport for self-hosted provider instances.

Ollama (https://ollama.com) also exposes an OpenAI-compatible ``/v1``
shim, but that shim has no way to set the context window per request:
Ollama sizes every model at a small default (``num_ctx`` 4096 on a
GPU-less box) and silently truncates the prompt beyond it, which for this
app's system prompt means losing the tool instructions. The native
``POST /api/chat`` endpoint takes ``options.num_ctx``, so self-hosted
instances with API type ``ollama`` (``config/inference_providers.py``) use
this subclass of :class:`OpenRouterProvider`.

Only the transport differs. The session, the stored history (OpenAI chat
format, ``role``/``content``/``tool_calls``/``tool_call_id``), tool-result
formatting, pending-tool detection, history repair and usage accounting
are inherited unchanged, so the rest of the app keeps treating the model
as an ``openrouter``-family model (same ``sdk_history.json`` envelope, same
``llm_calls_openrouter`` analytics table). Messages are converted to
Ollama's shape at the request boundary (tool arguments as objects, tool
results tagged with ``tool_name``) and the streamed NDJSON chunks back
into the OpenAI shape as they arrive. Ollama reports usage as
``prompt_eval_count`` / ``eval_count`` on the final chunk; they are
recorded under the OpenAI key names the analytics layer stores.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

from chat.llm.base import StreamEvent
from chat.llm.openrouter_provider import OpenRouterProvider, parse_tool_arguments

logger = logging.getLogger(__name__)

# Ollama runs on local hardware: prompt evaluation of a long context on a
# CPU box can take minutes, so the read timeout is generous while the
# connect timeout stays short (a down server should fail fast).
_CONNECT_TIMEOUT_SECONDS = 15
_READ_TIMEOUT_SECONDS = 600


class OllamaError(RuntimeError):
    """An error reported by the Ollama server (HTTP status or an ``error``
    field in the stream). ``status_code`` / ``message`` follow the attribute
    names ``chat/llm/health.py:extract_error_message`` understands."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _content_text(content: Any) -> str:
    """Flatten OpenAI message content (string, None, or a list of content
    parts) into the plain string Ollama's ``content`` field takes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(t for t in texts if t)
    return str(content)


def _parse_arguments(raw: Any) -> dict:
    """Arguments as a dict for the request boundary (malformed -> ``{}``)."""
    return parse_tool_arguments(raw)[0]


def to_ollama_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Convert OpenAI-format session history into Ollama ``/api/chat``
    messages, with the system prompt prepended.

    - assistant ``tool_calls`` keep their id (newer Ollama versions emit
      and accept one; older ones ignore unknown fields) and carry their
      arguments as an object instead of a JSON string;
    - ``role="tool"`` results are tagged with ``tool_name`` (Ollama's way
      of pairing a result with its call), resolved from the preceding
      assistant message's tool calls;
    - content-part arrays are flattened to text (this provider family
      never attaches binary parts).
    """
    out: list[dict] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    call_names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            converted: dict = {"role": "assistant", "content": _content_text(message.get("content"))}
            tool_calls = []
            for tc in message.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                function = tc.get("function") or {}
                name = function.get("name") or ""
                call_id = tc.get("id")
                if call_id:
                    call_names[call_id] = name
                entry: dict = {"function": {"name": name, "arguments": _parse_arguments(function.get("arguments"))}}
                if call_id:
                    entry["id"] = call_id
                tool_calls.append(entry)
            if tool_calls:
                converted["tool_calls"] = tool_calls
            out.append(converted)
        elif role == "tool":
            converted = {"role": "tool", "content": _content_text(message.get("content"))}
            name = call_names.get(message.get("tool_call_id") or "")
            if name:
                converted["tool_name"] = name
            out.append(converted)
        else:
            out.append({"role": role or "user", "content": _content_text(message.get("content"))})
    return out


class OllamaProvider(OpenRouterProvider):
    """LLMProvider for self-hosted Ollama servers over the native API."""

    def __init__(self, instance_id: str | None = None):
        super().__init__(instance_id)
        self._http = None

    def _server(self) -> tuple[str, dict]:
        """``(base_url, headers)`` for this instance; raises when unconfigured."""
        from config.inference_providers import effective_api_key, get_instance

        instance = get_instance(self.instance_id)
        base_url = (instance or {}).get("base_url")
        if not base_url:
            raise ValueError(
                f"No server URL configured for self-hosted inference "
                f"instance '{self.instance_id}'. Set it in Settings > "
                "Inference Providers (admin only)."
            )
        headers = {"Content-Type": "application/json"}
        api_key, _source = effective_api_key(self.instance_id)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return base_url, headers

    def _get_http(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(_READ_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS),
            )
        return self._http

    def reset_cached_clients(self) -> None:
        """Drop the cached HTTP client so a new URL/key takes effect."""
        super().reset_cached_clients()
        self._http = None

    @staticmethod
    def _error_from_body(status_code: int, body: bytes | str) -> OllamaError:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        message = text.strip()
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("error"):
            error = parsed["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        return OllamaError(message or f"Ollama request failed ({status_code})", status_code)

    def _request_options(self, model: str) -> dict:
        """``options`` for a request: the context window the admin
        configured for the model (``num_ctx``) and the output cap."""
        from chat.llm.config import resolve_model
        from config.inference_providers import DEFAULT_OLLAMA_CONTEXT_LENGTH

        spec = resolve_model(model)
        num_ctx = spec.max_input_tokens if spec and spec.listed else DEFAULT_OLLAMA_CONTEXT_LENGTH
        return {
            "num_ctx": num_ctx,
            "num_predict": spec.max_output_tokens if spec else 8192,
        }

    async def check_model_access(self, model: str) -> None:
        """Minimal non-streaming ``/api/chat`` call (one predicted token).

        Surfaces a model that is not pulled, a server that is down or
        rejects the key, and -- because Ollama loads the model to answer --
        a model that does not fit in memory.
        """
        base_url, headers = self._server()
        response = await self._get_http().post(
            f"{base_url}/api/chat",
            headers=headers,
            json={
                "model": self._get_openrouter_model_id(model),
                "messages": [{"role": "user", "content": "ok"}],
                "stream": False,
                "options": {"num_predict": 1},
            },
        )
        if response.status_code >= 400:
            raise self._error_from_body(response.status_code, response.content)
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise self._error_from_body(response.status_code, response.content)

    async def send_message_stream(
        self,
        session: Any,
        message: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream an ``/api/chat`` response as normalized StreamEvents.

        Text arrives as ``message.content`` fragments; tool calls arrive
        complete (arguments already an object) on one or more chunks and
        are emitted after the stream ends, mirroring the parent class so
        the persisted assistant message has the same OpenAI shape.
        """
        if isinstance(message, str):
            session.messages.append({"role": "user", "content": message})
        elif isinstance(message, list):
            session.messages.extend(message)

        session.last_usage = {}
        base_url, headers = self._server()
        wire_id = self._get_openrouter_model_id(session.model)
        body: dict = {
            "model": wire_id,
            "messages": to_ollama_messages(session.system_prompt, session.messages),
            "stream": True,
            "options": self._request_options(session.model),
        }
        if session.tools:
            body["tools"] = session.tools

        accumulated_text = ""
        tool_calls: list[dict] = []

        def _append_assistant_message(include_tool_calls: bool) -> None:
            calls = tool_calls if include_tool_calls else []
            if not accumulated_text and not calls:
                return
            assistant_message: dict = {
                "role": "assistant",
                "content": accumulated_text or None,
            }
            if calls:
                assistant_message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"]),
                        },
                    }
                    for call in calls
                ]
            session.messages.append(assistant_message)

        try:
            async with self._get_http().stream(
                "POST", f"{base_url}/api/chat", headers=headers, json=body,
            ) as response:
                if response.status_code >= 400:
                    raise self._error_from_body(response.status_code, await response.aread())
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("Skipping non-JSON Ollama stream line: %r", line[:200])
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        raise self._error_from_body(response.status_code, line)
                    msg = chunk.get("message") or {}
                    content = msg.get("content")
                    if content:
                        accumulated_text += content
                        yield StreamEvent(type="text", text=content)
                    for tc in msg.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        function = tc.get("function") or {}
                        # Ollama normally sends arguments as an object; a
                        # string is parsed, and malformed text surfaces as
                        # tool_args_error like the chat-completions path.
                        arguments, args_error = parse_tool_arguments(
                            function.get("arguments"),
                        )
                        tool_calls.append({
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "name": function.get("name") or "",
                            "arguments": arguments,
                            "error": args_error,
                        })
                    if chunk.get("done"):
                        self._capture_ollama_usage(session, chunk)
        except asyncio.CancelledError:
            _append_assistant_message(include_tool_calls=False)
            raise

        _append_assistant_message(include_tool_calls=True)

        for call in tool_calls:
            yield StreamEvent(
                type="tool_call",
                tool_name=call["name"],
                tool_args=call["arguments"],
                tool_id=call["id"],
                tool_args_error=call["error"],
            )

    @staticmethod
    def _capture_ollama_usage(session: Any, chunk: dict) -> None:
        """Record the final chunk's counters under the OpenAI key names.

        ``prompt_eval_count`` is the full prompt (including the part served
        from Ollama's prompt cache, reported separately as
        ``prompt_eval_cached_count`` on recent versions), matching the
        ``prompt_tokens``-includes-cached convention of this family.
        """
        last = session.last_usage
        prompt = chunk.get("prompt_eval_count")
        completion = chunk.get("eval_count")
        if isinstance(prompt, int):
            last["prompt_tokens"] = prompt
        if isinstance(completion, int):
            last["completion_tokens"] = completion
        if isinstance(prompt, int) and isinstance(completion, int):
            last["total_tokens"] = prompt + completion
        cached = chunk.get("prompt_eval_cached_count")
        if isinstance(cached, int):
            last["cached_prompt_tokens"] = cached
