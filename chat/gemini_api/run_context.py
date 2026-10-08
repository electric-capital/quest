"""Per-run context bundle for the top-level conversation loop.

``run_conversation_turn`` resolves a large amount of per-run state up front
(provider, session, flags, prompt inputs) and then threads it through the
turn loop and its tool arms. :class:`RunContext` bundles that state into
one object so extracted helpers take ``ctx`` instead of ever-growing
parameter lists -- the calling convention for the per-tool handler
functions is ``handler(ctx, tool_id, args)``.

The dataclass is frozen: every field is resolved once during run setup
and never rebound. Two fields hold deliberately-mutable objects:

* ``structured_messages`` is the caller's shared ``messages_out`` list --
  append-only, and it must remain the SAME list object so the caller can
  recover partial results after a CancelledError or suspend sentinel.
* ``usage_acc`` accumulates token usage across turns.
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from chat.gemini_api.usage import UsageAccumulator


@dataclass(frozen=True)
class RunContext:
    """Read-only bundle of one run_conversation_turn invocation's resolved state.

    Constructed after run setup completes (model/provider resolved, flags
    read, prompts built, session created), before the first model turn.
    """

    # Invocation inputs
    app: Any
    user: dict[str, Any]
    conversation_id: str
    timezone: str
    model: str
    origin: str
    project_id: str | None
    routine_id: str | None
    slack_context: dict | None

    # Provider + session (chat is the provider-native session object)
    provider: Any
    provider_name: str
    chat: Any

    # Origin / flag booleans (see chat/conversation_flags.py and
    # config/feature_gates.py for the gating rules)
    is_slack_origin: bool
    is_user_subagent: bool
    is_inference_api: bool
    is_public: bool
    nested_subagents: bool
    user_subagents_enabled: bool
    user_subagents_gate_open: bool

    # Cross-user subagent linkage (origin="user_subagent" only)
    subagent_run: dict | None
    subagent_caller: dict | None

    # Prompt inputs shared with sub-agent spawns
    custom_prompt: str
    resolved_project_guide: str
    resolved_skills_content: str

    # Output channels. on_event is the capturing wrapper (sub-agent event
    # capture + boundary SDK-history save), already bound to this run's
    # chat/provider. structured_messages is the caller's messages_out
    # list -- append-only, never rebind or copy it.
    on_event: Callable[[dict], Awaitable[None]]
    structured_messages: list[dict]

    # Cross-turn token accumulation (mutable by design)
    usage_acc: UsageAccumulator

    # Workspace notice flags of a project conversation (chat_history.json,
    # see chat.storage.CONVERSATION_NOTICE_FLAGS), resolved once at turn
    # start and handed to sub-agent spawns for their prompts. Empty for a
    # standalone conversation. Treat as read-only.
    workspace_notice_flags: dict[str, bool] = field(default_factory=dict)
