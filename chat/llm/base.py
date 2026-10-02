"""Abstract base class and common types for LLM providers.

Defines the LLMProvider interface that each backend (Gemini, Anthropic)
must implement, along with shared data types for streaming events,
usage statistics, and tool specifications.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, TypedDict


class ToolSpec(TypedDict, total=False):
    """Canonical tool specification in JSON Schema format.

    This is the provider-agnostic representation of a tool. Each provider
    converts these to its own native format (Gemini FunctionDeclaration,
    Anthropic tool dict, etc.).

    ``description`` is the default/generic description.  If a tool needs
    provider-specific wording (e.g. because the file-handling mechanism
    differs), set ``provider_descriptions`` with per-provider overrides.
    The converter functions will prefer the provider-specific description
    when available.
    """
    name: str
    description: str
    parameters: dict  # JSON Schema object
    provider_descriptions: dict[str, str]  # e.g. {"gemini": "...", "anthropic": "..."}
    # Optional connected_services key gating the tool's system-prompt
    # enumeration (NOT its callability). Tools whose service the user has
    # not connected are omitted from the "Dynamic Tools" section.
    requires_service: str


@dataclass
class StreamEvent:
    """A normalized streaming event from any LLM provider.

    Attributes:
        type: Event type -- "text", "tool_call", "usage", or
            "model_fallback".
        text: Text content (for type="text").
        tool_name: Tool name (for type="tool_call").
        tool_args: Tool arguments dict (for type="tool_call").
        tool_id: Provider-assigned tool call ID (for type="tool_call").
        tool_args_error: Non-empty when the provider could not parse the
            model's tool-call arguments (for type="tool_call"; e.g. a
            chat-completions model emitting malformed ``function.arguments``
            JSON). ``tool_args`` is then empty and the conversation loop
            returns this message to the model as the tool result instead of
            dispatching the call.
        usage: Usage statistics (for type="usage").
        data: Auxiliary payload for event types with no dedicated fields.
            For type="model_fallback" (the Anthropic refusal-fallback seam):
            from_model / to_model / category plus from_display / to_display.
    """
    type: str  # "text", "tool_call", "usage", "model_fallback"
    text: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_id: str = ""
    tool_args_error: str = ""
    usage: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)


@dataclass
class UsageStats:
    """Token usage statistics from an LLM API call.

    Attributes:
        input_tokens: Number of input/prompt tokens. For Gemini this includes
            cached tokens; for Anthropic this excludes cached tokens.
        output_tokens: Number of output/completion tokens.
        cached_tokens: Number of cached input tokens. For Gemini this is the
            cache-hit count (subset of input_tokens). For Anthropic this is
            the sum of cache_read_tokens + cache_creation_tokens.
        cache_creation_tokens: Tokens being cached for the first time
            (Anthropic-specific; always 0 for Gemini).
        cache_read_tokens: Tokens served from an existing cache
            (Anthropic-specific; always 0 for Gemini).
        raw_usage: The lossless, provider-native token-count fields exactly
            as the provider returned them for this call (field names verbatim).
            Captured for later cost/analytics; the coalesced integer fields
            above remain the common, provider-blind summary. Empty dict when
            the provider returned no usage object. For Gemini this includes
            thoughts_token_count / tool_use_prompt_token_count /
            total_token_count which the coalesced fields drop; for Anthropic
            it preserves the cache_read / cache_creation split.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    raw_usage: dict = field(default_factory=dict)


def compute_new_input_tokens(usage: UsageStats, provider_name: str) -> int:
    """Compute the number of new (uncached) input tokens for a given provider.

    "New input tokens" means tokens being processed for the first time,
    not served from cache.

    For Gemini and OpenRouter: input_tokens includes cached, so
        new = input - cached.
    For Anthropic: input_tokens excludes cached, but cache_creation_tokens
        are new tokens being written to cache, so new = input + cache_creation.

    Args:
        usage: UsageStats from a provider's get_usage() call.
        provider_name: "gemini", "anthropic", or "openrouter".

    Returns:
        The number of new input tokens.
    """
    if provider_name in ("gemini", "openrouter"):
        return usage.input_tokens - usage.cached_tokens
    elif provider_name == "anthropic":
        return usage.input_tokens + usage.cache_creation_tokens
    else:
        # Unknown provider -- fall back to raw input_tokens
        return usage.input_tokens


def compute_total_context_tokens(usage: UsageStats, provider_name: str) -> int:
    """Compute the total context window usage for a single turn.

    This is the total number of tokens occupying the model's context window
    after this turn, including system prompt, all history, and the current
    turn's input.

    For Gemini and OpenRouter: input_tokens already includes cached tokens,
        so it represents the full context size directly.
    For Anthropic: input_tokens excludes cached tokens, so we need to
        add cache_read_tokens and cache_creation_tokens to get the full
        context size.

    Args:
        usage: UsageStats from a provider's get_usage() call for a single turn.
        provider_name: "gemini", "anthropic", or "openrouter".

    Returns:
        The total number of tokens in the context window.
    """
    if provider_name == "anthropic":
        return usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
    else:
        # Gemini, OpenRouter, and unknown providers: input_tokens is the
        # total context
        return usage.input_tokens


class LLMProvider(ABC):
    """Abstract base class for LLM provider implementations.

    Each provider (Gemini, Anthropic) implements this interface to provide
    a unified API for the conversation loop. The conversation loop in
    conversation.py and sub_agent.py uses only this interface, never
    touching provider-specific SDK types directly.
    """

    @abstractmethod
    def create_session(
        self,
        model: str,
        system_prompt: str,
        tools: list[ToolSpec],
        history: list | None = None,
    ) -> Any:
        """Create a new conversation session.

        Args:
            model: Model identifier string.
            system_prompt: System instruction text.
            tools: List of canonical tool specifications.
            history: Optional previously saved history to restore.

        Returns:
            A session object (type varies by provider). For Gemini, this
            is an AsyncChat. For Anthropic, a dict holding messages and config.
        """
        ...

    @abstractmethod
    async def send_message_stream(
        self,
        session: Any,
        message: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Send a message and stream the response as normalized events.

        Args:
            session: Session object from create_session().
            message: The message to send. For the first user message, this
                is a string. For tool results, this is provider-specific
                (returned by format_tool_results).

        Yields:
            StreamEvent objects with type "text" or "tool_call".
        """
        ...

    @abstractmethod
    def format_tool_results(
        self,
        session: Any,
        tool_results: list[dict[str, Any]],
    ) -> Any:
        """Format tool execution results for sending back to the model.

        Args:
            session: Session object (some providers need it to update state).
            tool_results: List of dicts with keys:
                - name: Tool name
                - result: Result string
                - tool_id: The tool_call ID from the StreamEvent
                - extra_parts: Optional list of extra content parts
                  (e.g., uploaded file references for Gemini)

        Returns:
            A message object in the provider's expected format, ready to
            be passed as the `message` argument to send_message_stream().
        """
        ...

    @abstractmethod
    def append_user_text(self, formatted_results: Any, text: str) -> None:
        """Append a user-text block to formatted tool results.

        Used when resuming a Slack-driven conversation whose last assistant
        turn has dangling tool_use blocks but no pending Slack reply tool.
        The tool_results close the dangling tool_uses, and the user's next
        message must reach the model as ordinary input on the same user
        turn. This method appends a provider-appropriate text block to the
        list returned by ``format_tool_results``, modifying it in place.

        Args:
            formatted_results: The formatted results list returned by
                format_tool_results(). Modified in place.
            text: The user text to append.
        """
        ...

    @abstractmethod
    def get_usage(self, session: Any) -> UsageStats:
        """Extract the latest token usage statistics.

        Called after send_message_stream() completes to get per-turn usage.

        Args:
            session: Session object that may hold accumulated usage.

        Returns:
            UsageStats with token counts.
        """
        ...

    @abstractmethod
    def save_history(self, session: Any) -> list[dict]:
        """Serialize session history for persistence.

        Args:
            session: Session object to serialize.

        Returns:
            A JSON-serializable list of dicts.
        """
        ...

    @abstractmethod
    def get_pending_tool_uses(self, session: Any) -> list[tuple[str, str]]:
        """Return pending tool_use blocks on the last assistant turn.

        Returns ``[(tool_id, tool_name), ...]`` for tool_use blocks in the
        last assistant message of the session that have no matching
        tool_result in a later message.

        Used by the conversation resume path: when a Slack-driven run is
        restarted while suspended inside send_slack_reply_and_get_response,
        the loaded session ends on an assistant turn whose tool_use has no
        matching tool_result. The caller formats the user's next message
        as a tool_result closing those tool_uses, so the resumed API call
        has the required user(tool_result) -> assistant alternation.

        Args:
            session: Session object to inspect.

        Returns:
            A list of (tool_id, tool_name) tuples; empty when there are
            no dangling tool_use blocks (the normal case).
        """
        ...

    @abstractmethod
    def get_pending_tool_use_args(self, session: Any) -> list[tuple[str, str, dict]]:
        """Return pending tool_use blocks with their arguments.

        Same shape as ``get_pending_tool_uses`` but also returns each
        tool_use's argument dict. The resume path needs the args of a
        dangling ``wait_for_handles`` call to know which handle ids to
        re-query from the DB.

        Args:
            session: Session object to inspect.

        Returns:
            A list of ``(tool_id, tool_name, args)`` tuples; empty when
            there are no dangling tool_use blocks.
        """
        ...

    @abstractmethod
    def get_pending_tool_use_args_from_history(
        self, history: list[dict],
    ) -> list[tuple[str, str, dict]]:
        """Like ``get_pending_tool_use_args`` but on serialized history.

        Operates on the dict shape produced by ``save_history`` (i.e., the
        contents of ``sdk_history.json``). Used by the headless resume path
        in ``chat.wait_handles.resume`` to detect a dangling tool_use
        without rebuilding a live session.
        """
        ...

    async def check_model_access(self, model: str) -> None:
        """Issue a minimal live inference call to verify the model works.

        Backs the admin per-model health check (``chat.llm.health``): the
        call must go through the same client construction and model-id
        resolution as real conversations so it surfaces the same failures
        (missing credentials, model not enabled on the backend, quota).

        Raises the provider SDK exception on failure; returns None on
        success. Implementations must keep the call as small as the API
        allows (single-digit output tokens).
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support model health checks"
        )

    def disable_cache_writes(self, session: Any) -> None:
        """Mark a session so its requests write nothing to the prompt cache.

        Used for one-off sessions whose prompt will never be reused (e.g.
        the compaction summarizer): an explicit cache write there is pure
        surcharge. Default no-op -- Gemini's implicit caching has no
        explicit write cost to opt out of. The Anthropic override stops
        adding cache_control breakpoints for the session.
        """
        return None

    def repair_session_history(self, session: Any) -> int:
        """Repair provider-invariant violations in the session history.

        Concurrent runs appending to one shared session can interleave
        messages and orphan a mid-history tool call (see the Anthropic
        override). Providers with strict pairing rules repair the
        history in place; the default is a no-op for providers whose
        API tolerates such histories.

        Returns:
            Number of blocks repaired (0 when nothing was changed).
        """
        return 0

    @abstractmethod
    def load_history(self, data: list[dict]) -> list:
        """Deserialize saved history for session restoration.

        Args:
            data: Previously serialized history data.

        Returns:
            History in the format expected by create_session(history=...).
        """
        ...

    @abstractmethod
    async def upload_file(
        self,
        file_path: str,
        mime_type: str,
        display_name: str = "",
        model: str = "",
    ) -> Any | None:
        """Upload a file for inline analysis.

        Providers that don't support file upload return None.

        Args:
            file_path: Local path to the file.
            mime_type: MIME type string.
            display_name: Human-readable name for the file.
            model: Optional model id, used by providers that select a
                transport per-model (e.g. Gemini genapi vs Vertex).

        Returns:
            A provider-specific file reference (e.g., types.Part for Gemini),
            or None if the provider doesn't support file uploads.
        """
        ...

    @abstractmethod
    def make_file_part(self, file_ref: Any) -> Any:
        """Create a content part from an uploaded file reference.

        Args:
            file_ref: File reference returned by upload_file().

        Returns:
            A content part that can be included in extra_parts of tool results.
        """
        ...

    @abstractmethod
    def make_text_part(self, text: str) -> Any:
        """Create a provider-specific text content part.

        Used to build extra_parts entries (e.g., advisory warnings) that
        accompany a tool result but are not part of the result string itself.

        Args:
            text: The text content.

        Returns:
            A provider-specific content part (e.g., Gemini Part.from_text,
            Anthropic text content block dict).
        """
        ...

    @abstractmethod
    def inject_turn_warning(self, formatted_results: Any, warning_text: str) -> None:
        """Append a text warning to formatted tool results.

        Used by the sub-agent turn loop to inject an approaching-limit
        warning into the tool results being sent back to the model.

        Args:
            formatted_results: The formatted results list returned by
                format_tool_results(). Modified in place.
            warning_text: The warning message to inject.
        """
        ...
