"""Expensive-resume detection for long-idle, long-context conversations.

Resuming a long conversation after the provider's prompt cache has expired
re-reads the entire context at uncached input rates, so a single message
into a stale 300K+-token Opus conversation can cost dollars before the
model emits a word. This module decides when a conversation is "expensive
to resume" so the API/WS layers can warn the user (and require an explicit
acknowledgement) before running the turn.

Consumers:
- ``GET /conversations/{id}`` (chat/routes/conversations.py) surfaces the
  verdict as the ``expensive_resume`` response field so the FE can block
  the composer with a warning card.
- The ``send_message`` WS handler (chat/realtime/socket.py) rejects an
  unacknowledged send with reason ``expensive_resume_unacknowledged``
  unless the envelope carries ``expensive_resume_acknowledged: true``.

The rule list is a module-level constant: add an ``ExpensiveResumeRule``
entry to cover more models / thresholds. Rules are evaluated in order and
the first fully-matching rule wins.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpensiveResumeRule:
    """One "warn before resuming" configuration.

    A conversation triggers the warning when its model is one of
    ``models``, no message has been appended for at least
    ``min_idle_seconds`` (so the provider prompt cache is long expired),
    and the latest top-level LLM call's context size is at least
    ``min_context_tokens``.
    """

    # Exact model ids (MODEL_REGISTRY keys in chat/llm/config.py), matched
    # by equality. Every covered model is listed explicitly -- no prefix
    # matching -- because Vertex model naming schemes change over time and
    # a prefix could silently start (or stop) matching new ids. Keep this
    # list in sync with the registry when models are added.
    models: tuple[str, ...]
    min_context_tokens: int
    min_idle_seconds: int


EXPENSIVE_RESUME_RULES: tuple[ExpensiveResumeRule, ...] = (
    # Opus-family conversations over 300K context tokens idle for 2+ hours.
    ExpensiveResumeRule(
        models=(
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-opus-4-8",
            "claude-opus-5",
            "claude-opus-5-5",
        ),
        min_context_tokens=300_000,
        min_idle_seconds=2 * 60 * 60,
    ),
)


def _parse_last_message_at(value) -> Optional[datetime]:
    """Parse a ``last_message_at`` meta value into a UTC-aware datetime.

    Accepts datetimes (naive treated as UTC) and ISO 8601 strings with an
    optional trailing ``Z``. Returns None for anything unparseable.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _estimate_resume_cost_usd(
    provider: str,
    model: str,
    context_tokens: int,
) -> Optional[float]:
    """Rough list-price USD cost of re-processing the full context on resume.

    Reuses ``db/llm_pricing.py`` with a synthetic single-call metrics dict:
    for Anthropic the resumed turn re-writes the whole history into the
    prompt cache (the provider requests default-TTL ephemeral cache blocks,
    so the 5m cache-write rate applies); for Gemini the history lands as
    plain prompt tokens. Output tokens are excluded -- this is the price of
    the resume itself, before the model writes a word. None when the model
    has no pricing entry.
    """
    from db.llm_pricing import estimate_cost_usd, long_context_threshold
    if provider == "anthropic":
        metrics = {"cache_creation_input_tokens": context_tokens}
    else:
        metrics = {"prompt_token_count": context_tokens}
    cost = estimate_cost_usd(
        provider, model, metrics,
        long_context=context_tokens > long_context_threshold(provider, model),
    )
    return round(cost, 2) if cost is not None else None


def _provider_for_model(model: str) -> Optional[str]:
    """Resolve the provider whose ``llm_calls_*`` table holds this model's rows."""
    from chat.llm.config import get_provider_for_model
    try:
        return get_provider_for_model(model)
    except ValueError:
        # Retired/unregistered model ids: same defensive inference as
        # db/llm_call_store.record_api_call.
        return "anthropic" if model.startswith("claude") else "gemini"


def candidate_rules(
    model: Optional[str],
    last_message_at,
    last_message_seq,
    now: Optional[datetime] = None,
) -> tuple[list[ExpensiveResumeRule], int]:
    """Cheap DB-free pre-check: rules whose model + idle-time criteria match.

    Returns the matching rules (in configured order) plus the computed idle
    seconds. An empty list means the conversation cannot be expensive to
    resume regardless of its context size, so callers skip the token query
    (which keeps the common send path free of any llm_calls read).
    """
    if not model:
        # No model on the row means the server default applies at run time;
        # we can't know the resume cost, so never warn.
        return [], 0
    if int(last_message_seq or 0) == 0:
        # Fresh conversation: nothing to resume.
        return [], 0
    last_at = _parse_last_message_at(last_message_at)
    if last_at is None:
        return [], 0
    now = now or datetime.now(timezone.utc)
    idle_seconds = int((now - last_at).total_seconds())
    matched = [
        rule for rule in EXPENSIVE_RESUME_RULES
        if idle_seconds >= rule.min_idle_seconds and model in rule.models
    ]
    return matched, idle_seconds


async def check_expensive_resume(
    conversation_id: str,
    meta: dict,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Return the expensive-resume verdict for a conversation, or None.

    ``meta`` is the row dict from ``db.conversation_store.get_conversation_meta``
    (needs ``model``, ``last_message_at``, ``last_message_seq``). The context
    size is the latest top-level call's input-side token count from the
    provider's raw ``llm_calls_*`` table -- the same per-call context formula
    the pricing tiers key on.

    The returned dict is JSON-ready for the FE warning card:
    ``{model, context_tokens, idle_seconds, estimated_resume_cost_usd,
    min_context_tokens, min_idle_seconds}`` (``estimated_resume_cost_usd``
    is None for models without a pricing entry). Any internal failure
    returns None (the warning is best-effort and must never break a
    conversation load or send).
    """
    try:
        # Project conversations never warn: the card's alternatives don't
        # apply there (the workspace lives at the project level, so
        # Duplicate Workspace would copy nothing, and Create Project from
        # Chat is rejected upstream). The cheap in-project alternative --
        # starting a new conversation against the shared workspace -- is
        # already the product's recommended pattern; a project-aware
        # variant of the warning can add that option later.
        if meta.get("project_id"):
            return None

        model = meta.get("model")
        rules, idle_seconds = candidate_rules(
            model,
            meta.get("last_message_at"),
            meta.get("last_message_seq"),
            now=now,
        )
        if not rules:
            return None

        provider = _provider_for_model(model)
        if provider is None:
            return None

        from db.llm_call_store import get_latest_context_tokens
        context_tokens = await get_latest_context_tokens(conversation_id, provider)
        if context_tokens is None:
            return None

        # Compaction awareness: the llm_calls_* tables only learn the new
        # (smaller) context size after the next real turn runs, so a
        # just-compacted conversation would keep warning off its stale
        # pre-compaction row. When the last compaction is newer than the
        # latest recorded top-level call, trust the compaction sidecar's
        # post-compaction estimate instead (min() keeps the honest answer
        # if the estimate is somehow still huge).
        from chat.compaction import compaction_newer_than, load_compaction_meta
        compaction_meta = load_compaction_meta(conversation_id)
        if compaction_meta is not None:
            from db.llm_call_store import get_latest_top_level_call_at
            last_call_at = await get_latest_top_level_call_at(
                conversation_id, provider,
            )
            if compaction_newer_than(compaction_meta, last_call_at):
                tokens_after = compaction_meta.get("tokens_after_estimate")
                if isinstance(tokens_after, int):
                    context_tokens = min(context_tokens, tokens_after)

        for rule in rules:
            if context_tokens >= rule.min_context_tokens:
                from chat.compaction import estimate_compaction_cost_usd
                return {
                    "model": model,
                    "context_tokens": context_tokens,
                    "idle_seconds": idle_seconds,
                    "estimated_resume_cost_usd": _estimate_resume_cost_usd(
                        provider, model, context_tokens,
                    ),
                    # One-time cost of the "Compact this chat" option's
                    # summarization call (None when unavailable/unpriced).
                    "estimated_compaction_cost_usd": estimate_compaction_cost_usd(
                        conversation_id, model,
                    ),
                    "min_context_tokens": rule.min_context_tokens,
                    "min_idle_seconds": rule.min_idle_seconds,
                }
        return None
    except Exception:
        logger.exception(
            "check_expensive_resume failed (conversation=%s)", conversation_id,
        )
        return None
