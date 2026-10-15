"""Anthropic provider implementation using the anthropic[vertex] SDK.

Uses AnthropicVertex client for Claude models on Google Cloud Vertex AI.
Authentication is via Application Default Credentials (ADC) -- no API key needed.
"""

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from chat.llm.base import LLMProvider, StreamEvent, UsageStats, ToolSpec
from chat.llm.file_limits import (
    ANTHROPIC_IMAGE_MAX_BYTES,
    ANTHROPIC_VERTEX_MAX_ATTACH_BYTES,
)
from chat.llm.tool_schemas import to_anthropic_tools

logger = logging.getLogger(__name__)

# Betas the refusal-fallback middleware stamps on the ``anthropic-beta``
# header of every request it handles (the primary request included).
#
# The SDK default is ``("fallback-credit-2026-07-01",)``, which makes
# refusals mint a fallback-credit token so the retry reprices the
# already-cached span and can continue from partial output. Vertex AI
# rejects that beta value outright ("Unexpected value(s)
# `fallback-credit-2026-07-01` for the `anthropic-beta` header", HTTP 400),
# so with the default every Opus 5 request -- refused or not -- failed
# before reaching the model. Sending no beta keeps the middleware working
# on Vertex: a refusal *before* any output streamed still retries on the
# fallback chain (the common safety-classifier case), while a refusal
# that arrives mid-output has no credit token to chain on and surfaces as
# ``AnthropicStreamRefusal`` with the partial text persisted. Revisit
# once Vertex accepts a ``fallback-credit-*`` beta.
REFUSAL_FALLBACK_BETAS: tuple[str, ...] = ()


class AnthropicStreamRefusal(RuntimeError):
    """Raised when an Anthropic stream ends with ``stop_reason: "refusal"``.

    Opus-5-class models run safety classifiers that can decline a request
    with a normal HTTP 200 whose stream simply stops with this reason --
    previously an invisible empty turn. For models with a
    ``refusal_fallback_models`` registry entry the SDK's refusal-fallback
    middleware retries the request on each fallback model before this
    surfaces, so reaching here means every configured model declined (or
    the model has no fallbacks configured). The conversation loop surfaces
    the message as a durable error bubble.
    """

    def __init__(
        self,
        model: str,
        category: str | None = None,
        explanation: str | None = None,
        had_fallbacks: bool = False,
    ):
        self.category = category
        self.explanation = explanation
        detail = f" (category: {category})" if category else ""
        parts = [
            f"{model} declined this request via its safety classifiers{detail}."
        ]
        if explanation:
            parts.append(explanation)
        if had_fallbacks:
            parts.append("The configured fallback models declined it as well.")
        parts.append(
            "Try rephrasing the request, or switch the conversation to a "
            "different model."
        )
        super().__init__(" ".join(parts))

# MIME types supported for inline content in Anthropic tool_result blocks.
# Images are sent as {"type": "image", "source": {"type": "base64", ...}}.
# PDFs are sent as {"type": "document", "source": {"type": "base64", ...}}.
_ANTHROPIC_IMAGE_MIME_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
})

_ANTHROPIC_DOCUMENT_MIME_TYPES = frozenset({
    "application/pdf",
})

_ANTHROPIC_SUPPORTED_MIME_TYPES = _ANTHROPIC_IMAGE_MIME_TYPES | _ANTHROPIC_DOCUMENT_MIME_TYPES

# Attachment size caps live in chat/llm/file_limits.py (single source of
# truth): 5MB per image (Anthropic API limit) and an effective per-file cap
# for documents derived from the Vertex 30MB request-payload ceiling.
_ANTHROPIC_IMAGE_MAX_SIZE = ANTHROPIC_IMAGE_MAX_BYTES


@dataclass
class AnthropicSession:
    """Stateless session container for Anthropic conversations.

    Anthropic's API is stateless (no persistent chat object), so the
    "session" is just a data container holding the messages history,
    system prompt, tool definitions, and model name.
    """
    model: str
    system_prompt: str
    tools: list[dict]
    messages: list[dict] = field(default_factory=list)
    last_usage: dict = field(default_factory=dict)
    # The user's thinking level for the conversation (``"none"`` = thinking
    # off); None runs at the registry entry's ``thinking_effort``.
    thinking_effort: str | None = None
    # When False, no cache_control breakpoints are added at the API-call
    # boundary, so the request writes nothing to the prompt cache. Used for
    # one-off sessions (e.g. the compaction summarizer) whose prompt will
    # never be reused -- a cache write there is pure surcharge.
    cache_writes_enabled: bool = True
    # anthropic.BetaFallbackState for models with refusal_fallback_models
    # configured (None otherwise). Entered around every request of this
    # session so follow-up turns are pinned to the fallback model that
    # accepted, instead of re-refusing on the primary each turn. Run-scoped:
    # not serialized with the history, so a resumed conversation starts
    # unpinned and tries the primary model again.
    fallback_state: Any = None


class AnthropicProvider(LLMProvider):
    """LLMProvider implementation for Anthropic Claude via Vertex AI."""

    def __init__(self):
        self._clients = {}

    def _resolve_region(self, model: str) -> str:
        """Resolve the Vertex region for a model.

        A per-model ``vertex_region`` override (e.g. ``"global"`` for models
        only served on the global endpoint) takes precedence; otherwise fall
        back to the ``anthropic.vertex_region`` config default (us-east5).
        """
        from chat.llm.config import MODEL_REGISTRY
        from config.server_config import load_server_config

        entry = MODEL_REGISTRY.get(model, {})
        override = entry.get("vertex_region")
        if override:
            return override

        config = load_server_config()
        return config.get("anthropic", {}).get("vertex_region", "us-east5")

    def _get_fallback_chain(self, model: str) -> tuple[str, ...]:
        """Resolve the model's refusal-fallback chain from the registry.

        Returns the ``refusal_fallback_models`` entries (internal registry
        ids, in order) that name valid Anthropic registry models; invalid
        entries are skipped with a warning rather than breaking the send
        path. Empty tuple when the model has no fallbacks configured.
        """
        from chat.llm.config import MODEL_REGISTRY

        chain = MODEL_REGISTRY.get(model, {}).get("refusal_fallback_models") or []
        valid = []
        for fallback in chain:
            fb_entry = MODEL_REGISTRY.get(fallback)
            if not fb_entry or fb_entry.get("provider") != "anthropic":
                logger.warning(
                    "Ignoring refusal fallback %r configured for %s: not an "
                    "Anthropic model in MODEL_REGISTRY",
                    fallback, model,
                )
                continue
            valid.append(fallback)
        return tuple(valid)

    def _build_fallback_entries(
        self, fallback_chain: tuple[str, ...],
    ) -> list[dict]:
        """Middleware patch entries, one per fallback model.

        Each entry is applied by the SDK middleware as a patch against the
        ORIGINAL request params (a field set overrides, an explicit None
        unsets, absent keeps), so the per-model request knobs -- max_tokens,
        adaptive thinking, effort -- are re-derived from the fallback
        model's own registry entry instead of inherited from the refused
        model's request.
        """
        from chat.llm.config import MODEL_REGISTRY

        entries: list[dict] = []
        for fallback in fallback_chain:
            fb_entry = MODEL_REGISTRY.get(fallback, {})
            patch: dict = {
                "model": fb_entry.get("vertex_model_id", fallback),
                "max_tokens": fb_entry.get("max_output_tokens", 8192),
            }
            effort = fb_entry.get("thinking_effort")
            if effort:
                patch["thinking"] = {"type": "adaptive"}
                patch["output_config"] = {"effort": effort}
            else:
                patch["thinking"] = None
                patch["output_config"] = None
            entries.append(patch)
        return entries

    def _display_name_for_model(self, model_id: str) -> str:
        """Best-effort display name for a fallback-seam model string.

        Seam blocks carry wire model ids: the caller-spelled primary (our
        vertex id for it) or a fallback entry's vertex id. Match registry
        keys first, then ``vertex_model_id`` values, falling back to the
        raw string for anything unrecognized.
        """
        from chat.llm.config import MODEL_REGISTRY

        entry = MODEL_REGISTRY.get(model_id)
        if entry:
            return entry.get("display_name", model_id)
        for key, candidate in MODEL_REGISTRY.items():
            if candidate.get("vertex_model_id") == model_id:
                return candidate.get("display_name", key)
        return model_id

    def _get_client(self, region: str, fallback_chain: tuple[str, ...] = ()):
        """Return an AnthropicVertex client, cached by (region, fallbacks).

        Loads project_id from server_config.json.
        Uses Google Cloud Application Default Credentials (ADC).

        When ``fallback_chain`` is non-empty the client carries the SDK's
        ``BetaRefusalFallbackMiddleware``, which transparently retries
        safety-classifier refusals on the chain's models (Vertex has no
        server-side ``fallbacks`` request param). The middleware only acts
        on ``client.beta.messages`` requests -- the send path uses that
        surface -- and its fallback list is fixed at construction, hence
        the cache key includes the chain.
        """
        cache_key = (region, fallback_chain)
        if cache_key in self._clients:
            return self._clients[cache_key]

        from anthropic import AsyncAnthropicVertex as AnthropicVertex
        from config.server_config import load_server_config

        config = load_server_config()
        project_id = config.get("anthropic", {}).get("vertex_project_id", "")

        if not project_id:
            raise ValueError(
                "Anthropic Vertex AI project_id not configured. "
                "Add 'anthropic.vertex_project_id' to server_config.json."
            )

        kwargs: dict = {}
        if fallback_chain:
            from anthropic import BetaRefusalFallbackMiddleware

            kwargs["middleware"] = [
                BetaRefusalFallbackMiddleware(
                    self._build_fallback_entries(fallback_chain),
                    # Never let the SDK default in: Vertex 400s on it.
                    betas=REFUSAL_FALLBACK_BETAS,
                ),
            ]

        client = AnthropicVertex(
            project_id=project_id,
            region=region,
            **kwargs,
        )
        self._clients[cache_key] = client
        return client

    def _get_vertex_model_id(self, model: str) -> str:
        """Resolve the Vertex AI model ID for a given model string.

        Maps our internal model ID (e.g. 'claude-haiku-4.5') to the
        Vertex AI model ID (e.g. 'claude-haiku-4-5@20251001').
        """
        from chat.llm.config import MODEL_REGISTRY
        entry = MODEL_REGISTRY.get(model, {})
        return entry.get("vertex_model_id", model)

    async def check_model_access(self, model: str) -> None:
        """Issue a minimal message call to verify the model actually works.

        Used by the admin inference-provider health check. Claude models on
        Vertex require per-model enablement in Model Garden, so a project can
        be fully configured while individual models still 403/404 -- only a
        live call catches that, along with quota exhaustion. max_tokens=1
        keeps the cost negligible.

        Raises the provider SDK exception on failure; returns None on success.
        """
        region = self._resolve_region(model)
        client = self._get_client(region)
        await client.messages.create(
            model=self._get_vertex_model_id(model),
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )

    def create_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list | None = None,
        thinking_effort: str | None = None,
    ) -> Any:
        """Create an Anthropic session (stateless container).

        Returns an AnthropicSession dataclass holding messages, config, etc.
        """
        anthropic_tools = to_anthropic_tools(tools)
        fallback_state = None
        if self._get_fallback_chain(model):
            # One state per session: entered around every request so
            # follow-up turns go straight to the fallback model that
            # accepted, instead of re-refusing on the primary each turn.
            from anthropic import BetaFallbackState

            fallback_state = BetaFallbackState()
        session = AnthropicSession(
            model=model,
            system_prompt=system_prompt,
            tools=anthropic_tools,
            messages=list(history) if history else [],
            thinking_effort=thinking_effort,
            fallback_state=fallback_state,
        )
        return session

    def set_thinking_effort(self, session: Any, thinking_effort: str | None) -> bool:
        # Stateless session: the level is only read at the request boundary.
        session.thinking_effort = thinking_effort
        return True

    async def send_message_stream(
        self,
        session: Any,
        message: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream an Anthropic response, yielding normalized StreamEvent objects.

        Handles the Anthropic streaming event structure:
        - content_block_start with type "text", "tool_use", thinking
          variants, or "fallback" (the refusal-fallback seam spliced in by
          the SDK middleware -- see ``refusal_fallback_models``)
        - content_block_delta with text deltas or input_json_delta
        - content_block_stop marks end of a content block
        - message_delta has stop_reason and usage

        Raises :class:`AnthropicStreamRefusal` when the stream ends with
        ``stop_reason: "refusal"`` -- i.e. the safety classifiers declined
        the request and (when fallbacks are configured) every fallback
        model declined it too.
        """
        region = self._resolve_region(session.model)
        fallback_chain = self._get_fallback_chain(session.model)
        client = self._get_client(region, fallback_chain)
        vertex_model = self._get_vertex_model_id(session.model)

        # Add user message to history
        if isinstance(message, str):
            session.messages.append({
                "role": "user",
                "content": message,
            })
        elif isinstance(message, list):
            # Tool results are already formatted as a user message content list
            session.messages.append({
                "role": "user",
                "content": message,
            })

        # Reset usage tracking
        session.last_usage = {}

        # Build the API call kwargs.
        # Prompt caching: convert system prompt from string to block-format
        # list with cache_control annotation. This creates a stable cache
        # breakpoint after the system prompt without changing the
        # LLMProvider interface or AnthropicSession.system_prompt (which
        # remains a plain str).
        cache_writes = getattr(session, "cache_writes_enabled", True)
        system_blocks = [{
            "type": "text",
            "text": session.system_prompt,
        }]
        if cache_writes:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        # Prompt caching: add cache_control to the last 2 user-role
        # messages so the most recent conversation prefix gets cached.
        #
        # Vertex AI limits cache_control to 4 blocks per request. We
        # already use 2 (system prompt + last tool definition), leaving
        # a budget of 2 for conversation messages. By tagging only the
        # last 2 user messages we stay within the limit while still
        # caching the most valuable (most-recently-reused) prefix.
        #
        # We never mutate session.messages -- cache annotations stay at
        # the API call boundary.

        # First pass: find the indices of the last 2 user-role messages.
        # (None when cache writes are disabled for this session.)
        user_indices = [
            i for i, msg in enumerate(session.messages)
            if msg["role"] == "user"
        ] if cache_writes else []
        cache_indices = set(user_indices[-2:])  # last 2 (or fewer)

        # Second pass: copy messages, adding cache_control only at the
        # selected indices.
        cached_messages = []
        for i, msg in enumerate(session.messages):
            if i not in cache_indices:
                cached_messages.append(msg)
                continue

            content = msg["content"]
            if isinstance(content, str):
                # Plain text user message -> convert to block-format with
                # cache_control so the text forms a cache breakpoint.
                cached_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }],
                })
            elif isinstance(content, list):
                # List of content blocks (tool_result, text, etc.).
                # Add cache_control only to the LAST block in the list
                # so this message contributes exactly 1 cache_control
                # block to the request.
                cached_blocks = [dict(block) for block in content]
                if cached_blocks:
                    cached_blocks[-1]["cache_control"] = {"type": "ephemeral"}
                cached_messages.append({
                    "role": "user",
                    "content": cached_blocks,
                })
            else:
                # Unexpected content type -- pass through unchanged.
                cached_messages.append(msg)

        from chat.llm.config import MODEL_REGISTRY
        entry = MODEL_REGISTRY.get(session.model, {})
        max_tokens = entry.get("max_output_tokens", 8192)

        api_kwargs = {
            "model": vertex_model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": cached_messages,
        }

        # Adaptive thinking: the user's pick for the conversation
        # (``session.thinking_effort``, validated against the registry's
        # ``thinking_efforts``) or, when none, the registry entry's
        # ``thinking_effort``. A level gets ``thinking: adaptive`` plus the
        # matching ``output_config.effort``; no level -- or the explicit
        # ``none`` -- keeps the historical thinking-off behavior (omitting
        # ``thinking`` disables thinking on Opus 4.7/4.8). ``thinking.display``
        # stays at its default ("omitted"), so thinking blocks stream with
        # empty text -- they are still accumulated below because the API
        # requires them to be echoed back verbatim (with their signature)
        # on later turns.
        effort = session.thinking_effort or entry.get("thinking_effort")
        if effort and effort != "none":
            api_kwargs["thinking"] = {"type": "adaptive"}
            api_kwargs["output_config"] = {"effort": effort}
        if session.tools:
            # Prompt caching: add cache_control to the last tool definition
            # so the tools layer forms a second stable cache breakpoint.
            # We shallow-copy the list and the last dict to avoid mutating
            # session.tools (cache annotations stay at the API call boundary).
            if cache_writes:
                tools_with_cache = list(session.tools)
                last_tool = dict(tools_with_cache[-1])
                last_tool["cache_control"] = {"type": "ephemeral"}
                tools_with_cache[-1] = last_tool
                api_kwargs["tools"] = tools_with_cache
            else:
                api_kwargs["tools"] = session.tools

        # Track the content block currently being built. Anthropic streams
        # blocks strictly one at a time (start -> deltas -> stop), so a
        # single explicit ``current_block_type`` is enough to dispatch the
        # stop event -- more robust than inferring the type from which
        # accumulator happens to be non-empty (a thinking block with
        # display="omitted" has empty text but must still be kept).
        current_block_type = None
        current_tool_name = ""
        current_tool_id = ""
        current_tool_input_json = ""
        assistant_content_blocks = []
        current_text_block = ""
        current_thinking_text = ""
        current_thinking_signature = ""
        current_redacted_data = ""
        refusal = None

        # The refusal-fallback middleware consults the session's
        # BetaFallbackState (a contextvar entered via ``with``) when it
        # handles the request, and pins it to the fallback model that
        # accepted -- so follow-up turns of this session skip the refusing
        # primary. Sessions without fallbacks get a no-op context.
        fallback_state = getattr(session, "fallback_state", None)
        state_cm = (
            fallback_state if fallback_state is not None
            else contextlib.nullcontext()
        )

        try:
            # The beta messages surface is required for the refusal-fallback
            # middleware (it only handles ``client.beta.messages`` requests);
            # event shapes are identical to the non-beta surface for
            # everything this loop reads. No beta header is sent with it --
            # see REFUSAL_FALLBACK_BETAS.
            with state_cm:
                async with client.beta.messages.stream(**api_kwargs) as stream:
                    async for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block
                            current_block_type = block.type
                            if block.type == "text":
                                current_text_block = ""
                            elif block.type == "tool_use":
                                current_tool_name = block.name
                                current_tool_id = block.id
                                current_tool_input_json = ""
                            elif block.type == "thinking":
                                # Text and signature normally arrive via deltas,
                                # but seed from the start block defensively.
                                current_thinking_text = getattr(block, "thinking", "") or ""
                                current_thinking_signature = getattr(block, "signature", "") or ""
                            elif block.type == "redacted_thinking":
                                # Redacted thinking arrives whole in the start
                                # event as an opaque encrypted payload.
                                current_redacted_data = getattr(block, "data", "") or ""
                            elif block.type == "fallback":
                                # Refusal-fallback seam spliced in by the SDK
                                # middleware: the model so far declined via its
                                # safety classifiers and the stream continues
                                # on the fallback model. The seam itself is a
                                # client-side marker (the API rejects it if
                                # replayed), and the continuation rules forbid
                                # echoing pre-boundary thinking back -- keep
                                # only replayable output from before the seam.
                                seam = {
                                    "from_model": getattr(
                                        getattr(block, "from_", None), "model", "",
                                    ) or "",
                                    "to_model": getattr(
                                        getattr(block, "to", None), "model", "",
                                    ) or "",
                                    "category": getattr(
                                        getattr(block, "trigger", None), "category", None,
                                    ),
                                }
                                logger.info(
                                    "Anthropic refusal fallback: %s declined "
                                    "(category=%s); continuing on %s",
                                    seam["from_model"], seam["category"],
                                    seam["to_model"],
                                )
                                session.last_usage["refusal_fallback"] = seam
                                assistant_content_blocks[:] = [
                                    b for b in assistant_content_blocks
                                    if b.get("type") not in ("thinking", "redacted_thinking")
                                ]
                                # Surface the switch to the conversation
                                # loop, which persists a user-visible
                                # notice row in the transcript.
                                yield StreamEvent(
                                    type="model_fallback",
                                    data={
                                        **seam,
                                        "from_display": self._display_name_for_model(
                                            seam["from_model"],
                                        ),
                                        "to_display": self._display_name_for_model(
                                            seam["to_model"],
                                        ),
                                    },
                                )

                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                current_text_block += delta.text
                                yield StreamEvent(type="text", text=delta.text)
                            elif delta.type == "input_json_delta":
                                current_tool_input_json += delta.partial_json
                            elif delta.type == "thinking_delta":
                                current_thinking_text += delta.thinking
                            elif delta.type == "signature_delta":
                                current_thinking_signature += delta.signature

                        elif event.type == "content_block_stop":
                            if current_block_type == "tool_use":
                                # Parse the accumulated JSON input
                                try:
                                    tool_args = json.loads(current_tool_input_json) if current_tool_input_json else {}
                                except json.JSONDecodeError:
                                    tool_args = {}

                                assistant_content_blocks.append({
                                    "type": "tool_use",
                                    "id": current_tool_id,
                                    "name": current_tool_name,
                                    "input": tool_args,
                                })

                                yield StreamEvent(
                                    type="tool_call",
                                    tool_name=current_tool_name,
                                    tool_args=tool_args,
                                    tool_id=current_tool_id,
                                )
                                current_tool_name = ""
                                current_tool_id = ""
                                current_tool_input_json = ""
                            elif current_block_type == "text":
                                if current_text_block:
                                    assistant_content_blocks.append({
                                        "type": "text",
                                        "text": current_text_block,
                                    })
                                current_text_block = ""
                            elif current_block_type == "thinking":
                                # Keep the block even when the text is empty
                                # (display="omitted"): the API requires thinking
                                # blocks -- identified by their signature -- to be
                                # replayed unmodified in the assistant turn,
                                # especially across tool-use round trips.
                                if current_thinking_signature or current_thinking_text:
                                    assistant_content_blocks.append({
                                        "type": "thinking",
                                        "thinking": current_thinking_text,
                                        "signature": current_thinking_signature,
                                    })
                                current_thinking_text = ""
                                current_thinking_signature = ""
                            elif current_block_type == "redacted_thinking":
                                if current_redacted_data:
                                    assistant_content_blocks.append({
                                        "type": "redacted_thinking",
                                        "data": current_redacted_data,
                                    })
                                current_redacted_data = ""
                            current_block_type = None

                        elif event.type == "message_delta":
                            delta = getattr(event, "delta", None)
                            if delta is not None and getattr(delta, "stop_reason", None) == "refusal":
                                # A refusal that reaches this loop is terminal:
                                # with fallbacks configured the middleware
                                # already retried the whole chain. Captured
                                # here, raised after the stream closes.
                                details = getattr(delta, "stop_details", None)
                                refusal = {
                                    "category": getattr(details, "category", None),
                                    "explanation": getattr(details, "explanation", None),
                                }
                            if hasattr(event, 'usage') and event.usage:
                                session.last_usage["output_tokens"] = getattr(event.usage, 'output_tokens', 0) or 0

                        elif event.type == "message_start":
                            if hasattr(event, 'message') and hasattr(event.message, 'usage'):
                                usage = event.message.usage
                                session.last_usage["input_tokens"] = getattr(usage, 'input_tokens', 0) or 0
                                cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
                                cache_creation = getattr(usage, 'cache_creation_input_tokens', 0) or 0
                                session.last_usage["cache_read_tokens"] = cache_read
                                session.last_usage["cache_creation_tokens"] = cache_creation
                                session.last_usage["cached_tokens"] = cache_read + cache_creation
                                # Cache-creation TTL split (5m writes bill 1.25x,
                                # 1h writes 2x) -- captured only when the SDK
                                # populated the detail object, mirroring the
                                # Gemini skip-None convention. Raw-analytics only;
                                # the coalesced UsageStats fields are unaffected.
                                cache_creation_detail = getattr(usage, 'cache_creation', None)
                                for ttl_key, api_field in (
                                    ("cache_creation_5m_tokens", "ephemeral_5m_input_tokens"),
                                    ("cache_creation_1h_tokens", "ephemeral_1h_input_tokens"),
                                ):
                                    ttl_value = getattr(cache_creation_detail, api_field, None)
                                    if ttl_value is not None:
                                        session.last_usage[ttl_key] = ttl_value
            # Log cache effectiveness for debugging / verification
            logger.debug(
                "Anthropic cache stats: input=%d, cache_read=%d, "
                "cache_creation=%d, output=%d",
                session.last_usage.get("input_tokens", 0),
                session.last_usage.get("cache_read_tokens", 0),
                session.last_usage.get("cache_creation_tokens", 0),
                session.last_usage.get("output_tokens", 0),
            )
        except asyncio.CancelledError:
            # On cancellation, flush any in-progress text block that hasn't
            # been finalized via content_block_stop yet. An in-progress
            # thinking block is deliberately dropped: without its final
            # signature it can never be replayed, and the API rejects
            # modified/unsigned thinking blocks.
            if current_block_type == "text" and current_text_block:
                assistant_content_blocks.append({
                    "type": "text",
                    "text": current_text_block,
                })
            # Append whatever content blocks we accumulated so far to the
            # session history, so the provider's save_history() can capture
            # the partial assistant response. Skip the append when the turn
            # produced nothing but thinking blocks: the API strips prior-turn
            # thinking server-side, so an all-thinking assistant message
            # would replay as empty content and 400 every later request.
            has_replayable_block = any(
                block.get("type") in ("text", "tool_use")
                for block in assistant_content_blocks
            )
            if has_replayable_block:
                session.messages.append({
                    "role": "assistant",
                    "content": assistant_content_blocks,
                })
            raise

        # Record the assistant's response in the session history. Same
        # all-thinking guard as the cancellation path: a turn truncated by
        # max_tokens can end after thinking alone, and persisting it would
        # replay as an empty assistant message once the API strips
        # prior-turn thinking.
        if any(
            block.get("type") in ("text", "tool_use")
            for block in assistant_content_blocks
        ):
            session.messages.append({
                "role": "assistant",
                "content": assistant_content_blocks,
            })

        if refusal is not None:
            # The safety classifiers declined the request (and, when
            # fallbacks are configured, every fallback model declined it
            # too). Any partial replayable output was persisted above; the
            # conversation loop turns this into a durable error message
            # instead of the silent empty turn a refusal used to produce.
            from chat.llm.config import MODEL_REGISTRY

            display_name = MODEL_REGISTRY.get(session.model, {}).get(
                "display_name", session.model,
            )
            raise AnthropicStreamRefusal(
                display_name,
                category=refusal.get("category"),
                explanation=refusal.get("explanation"),
                had_fallbacks=bool(fallback_chain),
            )

    def format_tool_results(
        self,
        session: Any,
        tool_results: list[dict[str, Any]],
    ) -> Any:
        """Format tool results as Anthropic tool_result content blocks.

        Returns a list of content blocks to be wrapped in a user message
        by send_message_stream().

        When extra_parts are present (e.g., base64-encoded images or PDFs
        from upload_file()), the content field is converted to an array of
        content blocks: a text block with the result string, followed by
        each extra_part (image or document content block dicts).
        """
        content_blocks = []
        for tr in tool_results:
            result_content = tr["result"]
            extra_parts = tr.get("extra_parts", [])

            if extra_parts:
                # Convert to array format: text block + file content blocks
                content_array = [{"type": "text", "text": result_content}]
                for part in extra_parts:
                    if part is not None:
                        content_array.append(part)
                content_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tr["tool_id"],
                    "content": content_array,
                })
            else:
                content_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tr["tool_id"],
                    "content": result_content,
                })
        return content_blocks

    def append_user_text(self, formatted_results: Any, text: str) -> None:
        """Append an Anthropic text content block to formatted tool results."""
        formatted_results.append({"type": "text", "text": text})

    def get_usage(self, session: Any) -> UsageStats:
        """Extract token usage from the last Anthropic stream.

        ``raw_usage`` preserves the provider-native token counts -- crucially
        the ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` split
        (priced differently), plus the cache-creation TTL split
        (``cache_creation_5m_input_tokens`` / ``cache_creation_1h_input_tokens``,
        included only when the SDK reported them). Anthropic reports no single
        "total" field, so ``raw_usage`` omits it.
        """
        usage = getattr(session, 'last_usage', {})
        raw_usage = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_tokens", 0),
        }
        if "cache_creation_5m_tokens" in usage:
            raw_usage["cache_creation_5m_input_tokens"] = usage["cache_creation_5m_tokens"]
        if "cache_creation_1h_tokens" in usage:
            raw_usage["cache_creation_1h_input_tokens"] = usage["cache_creation_1h_tokens"]
        if "refusal_fallback" in usage:
            # The turn was served by a refusal-fallback model, not the
            # requested one ({from_model, to_model, category}). Analytics
            # marker only -- the coalesced fields are the serving attempt's
            # counts either way.
            raw_usage["refusal_fallback"] = usage["refusal_fallback"]
        return UsageStats(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cached_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            raw_usage=raw_usage,
        )

    def save_history(self, session: Any) -> list[dict]:
        """Serialize Anthropic session history to JSON-safe dicts.

        The messages are already plain dicts, so this is straightforward.
        We add a provider marker so load_history knows the format.
        """
        return session.messages

    def load_history(self, data: list[dict]) -> list:
        """Deserialize saved history for Anthropic session restoration."""
        return data

    def get_pending_tool_uses(self, session: Any) -> list[tuple[str, str]]:
        """Return pending tool_use blocks on the last assistant message."""
        return [
            (tid, tname)
            for tid, tname, _args in self.get_pending_tool_use_args(session)
        ]

    def get_pending_tool_use_args(self, session: Any) -> list[tuple[str, str, dict]]:
        """Return pending tool_use blocks with their input args."""
        messages = getattr(session, "messages", None) or []
        return self.get_pending_tool_use_args_from_history(list(messages))

    def get_pending_tool_use_args_from_history(
        self, history: list[dict],
    ) -> list[tuple[str, str, dict]]:
        """Inspect serialized Anthropic history for unanswered tool_use blocks.

        Anthropic keeps every block of one turn in a single assistant
        message, so checking ``history[-1]`` is sufficient -- a dangling
        tool_use can only live there.
        """
        if not history:
            return []
        try:
            last = history[-1]
            if not isinstance(last, dict) or last.get("role") != "assistant":
                return []
            content = last.get("content", [])
            if not isinstance(content, list):
                return []
            pending: list[tuple[str, str, dict]] = []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("id")
                ):
                    args = block.get("input", {})
                    if not isinstance(args, dict):
                        args = {}
                    pending.append(
                        (block["id"], block.get("name", "") or "", args)
                    )
            return pending
        except Exception:
            logger.debug(
                "Failed to inspect pending tool_use args on Anthropic history",
                exc_info=True,
            )
            return []

    def disable_cache_writes(self, session: Any) -> None:
        """Stop adding cache_control breakpoints for this session's requests."""
        session.cache_writes_enabled = False

    def repair_session_history(self, session: Any) -> int:
        """Repair orphaned tool_use / tool_result blocks in place.

        The Messages API enforces two pairing invariants: every
        ``tool_use`` in an assistant message must be answered by a
        ``tool_result`` in the immediately following message, and every
        ``tool_result`` must reference a ``tool_use`` id from the
        immediately preceding assistant message. Concurrent runs
        appending to one shared session can interleave messages and
        break both, after which every API call for the conversation is
        rejected with a 400. A dangling tool_use on the FINAL message is
        deliberately left alone -- that is the legitimate suspend shape
        the resume bucket closes with the real tool result.
        """
        messages = getattr(session, "messages", None)
        if not isinstance(messages, list) or len(messages) < 2:
            return 0
        repaired = 0

        def _tool_use_ids(msg: Any) -> set[str]:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                return set()
            content = msg.get("content")
            if not isinstance(content, list):
                return set()
            return {
                block["id"] for block in content
                if isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id")
            }

        def _interrupted_result(tool_id: str) -> dict:
            # Same marker the resume bucket uses for a cancelled tool call.
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": json.dumps({
                    "status": "interrupted",
                    "note": (
                        "The previous tool call was interrupted before "
                        "completing. Retry if needed."
                    ),
                }),
            }

        # Pass 1: every tool_use before the final message must be answered
        # in the very next message. Close any that are not with a
        # synthetic "interrupted" result.
        i = 0
        while i < len(messages) - 1:
            missing = _tool_use_ids(messages[i])
            if missing:
                nxt = messages[i + 1]
                nxt_content = nxt.get("content") if isinstance(nxt, dict) else None
                nxt_is_user = isinstance(nxt, dict) and nxt.get("role") == "user"
                if nxt_is_user and isinstance(nxt_content, list):
                    missing -= {
                        block.get("tool_use_id") for block in nxt_content
                        if isinstance(block, dict)
                        and block.get("type") == "tool_result"
                    }
                if missing:
                    synthetic = [
                        _interrupted_result(tid) for tid in sorted(missing)
                    ]
                    if nxt_is_user and isinstance(nxt_content, list):
                        nxt["content"] = synthetic + nxt_content
                    elif nxt_is_user and isinstance(nxt_content, str):
                        nxt["content"] = synthetic + [
                            {"type": "text", "text": nxt_content},
                        ]
                    else:
                        messages.insert(
                            i + 1, {"role": "user", "content": synthetic},
                        )
                    repaired += len(missing)
            i += 1

        # Pass 2: drop tool_result blocks that do not answer a tool_use
        # from the immediately preceding assistant message (including
        # duplicate answers for the same id).
        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            prev_ids = _tool_use_ids(messages[idx - 1]) if idx > 0 else set()
            kept: list = []
            answered: set[str] = set()
            dropped = 0
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                ):
                    tid = block.get("tool_use_id")
                    if tid not in prev_ids or tid in answered:
                        dropped += 1
                        continue
                    answered.add(tid)
                kept.append(block)
            if dropped:
                if not kept:
                    kept = [{
                        "type": "text",
                        "text": "[Removed: interrupted tool interaction.]",
                    }]
                msg["content"] = kept
                repaired += dropped

        return repaired

    async def upload_file(
        self,
        file_path: str,
        mime_type: str,
        display_name: str = "",
        model: str = "",
    ) -> Any | None:
        """Read a file and return a base64-encoded Anthropic content block dict.

        For supported image types (JPEG, PNG, GIF, WebP) and PDFs, returns a
        dict that can be included as a content block in a tool_result. For
        unsupported MIME types, returns None (the caller's fallback path will
        handle the error messaging).

        Args:
            file_path: Local path to the file.
            mime_type: MIME type string.
            display_name: Human-readable name for the file (unused but kept
                for interface compatibility).

        Returns:
            A dict representing an Anthropic image or document content block,
            or None if the MIME type is not supported.
        """
        if mime_type not in _ANTHROPIC_SUPPORTED_MIME_TYPES:
            return None

        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
        except Exception as e:
            logger.warning(
                "Failed to read file for Anthropic upload (path=%s): %s",
                file_path, e,
            )
            return None

        # Enforce size limit for images
        if mime_type in _ANTHROPIC_IMAGE_MIME_TYPES:
            if len(file_bytes) > _ANTHROPIC_IMAGE_MAX_SIZE:
                logger.info(
                    "Image too large for Anthropic inline upload "
                    "(path=%s, size=%d, limit=%d)",
                    file_path, len(file_bytes), _ANTHROPIC_IMAGE_MAX_SIZE,
                )
                return None

        # Defense-in-depth size limit for documents (PDFs): the Vertex API
        # rejects request payloads over 30MB, so an oversized document block
        # would fail the request (and every replay of the history). The
        # tool-handler pre-flight is the user-facing gate; this backstop
        # protects all other callers (e.g. composer attachments).
        if mime_type in _ANTHROPIC_DOCUMENT_MIME_TYPES:
            if len(file_bytes) > ANTHROPIC_VERTEX_MAX_ATTACH_BYTES:
                logger.info(
                    "Document too large for Anthropic inline upload "
                    "(path=%s, size=%d, limit=%d)",
                    file_path, len(file_bytes), ANTHROPIC_VERTEX_MAX_ATTACH_BYTES,
                )
                return None

        b64_data = base64.standard_b64encode(file_bytes).decode("ascii")

        if mime_type in _ANTHROPIC_IMAGE_MIME_TYPES:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": b64_data,
                },
            }
        elif mime_type in _ANTHROPIC_DOCUMENT_MIME_TYPES:
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": b64_data,
                },
            }

        # Should not be reached given the check at the top, but return None
        # defensively.
        return None

    def make_file_part(self, file_ref: Any) -> Any:
        """Return the content block dict as-is.

        The dict returned by upload_file() is already in the correct format
        for inclusion as an extra_part in tool results. It will be appended
        to the tool_result content array by format_tool_results().
        """
        return file_ref

    def make_text_part(self, text: str) -> Any:
        """Create an Anthropic text content block dict."""
        return {"type": "text", "text": text}

    def inject_turn_warning(self, formatted_results: Any, warning_text: str) -> None:
        """Append a text warning block to Anthropic formatted tool results."""
        formatted_results.append({"type": "text", "text": warning_text})
