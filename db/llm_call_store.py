"""Per-provider raw LLM call token usage data access layer.

Provides async functions to record and query LLM API call token usage.
Each function opens a fresh AsyncSessionLocal() session (same pattern as
other db/*_store.py modules).

Storage is raw-first: ``record_api_call()`` dispatches on provider and
writes each provider's NATIVE usage fields verbatim into its own table
(``llm_calls_gemini`` / ``llm_calls_anthropic`` /
``llm_calls_openrouter``) -- no normalization, no coalescing at write time. All calculation/aggregation logic (billing
buckets, dashboard display, future $ math) lives in the read-side queries
here, which stay malleable as pricing understanding evolves.

Rows are never deleted when conversations or users are removed -- these
tables serve as an append-only analytics log.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import Select, case, func, literal, select

from db.engine import AsyncSessionLocal
from db.llm_pricing import LONG_CONTEXT_THRESHOLD, estimate_cost_usd
from db.models import (
    ApiCallType,
    LlmCallAnthropic,
    LlmCallGemini,
    LlmCallOpenRouter,
)

logger = logging.getLogger(__name__)

# Native usage columns per table, in raw_usage key order. Used both by the
# write-path dispatch (copy raw_usage keys verbatim) and the read-path
# aggregation (SUM each column).
_GEMINI_USAGE_FIELDS = (
    "prompt_token_count",
    "candidates_token_count",
    "cached_content_token_count",
    "thoughts_token_count",
    "tool_use_prompt_token_count",
    "total_token_count",
)
_ANTHROPIC_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
)
_OPENROUTER_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cached_prompt_tokens",
    "reasoning_tokens",
    "total_tokens",
    # Accounting fields OpenRouter reports per request (see the
    # LlmCallOpenRouter column comments); read queries prefer the reported
    # USD amount over the list-price estimate.
    "cost",
    "upstream_inference_cost",
    "is_byok",
)

_USAGE_FIELDS_BY_PROVIDER = {
    "gemini": _GEMINI_USAGE_FIELDS,
    "anthropic": _ANTHROPIC_USAGE_FIELDS,
    "openrouter": _OPENROUTER_USAGE_FIELDS,
}


async def record_api_call(
    conversation_id: str,
    user_id: int,
    model: str,
    call_type: ApiCallType,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    agent_name: Optional[str] = None,
    cached_tokens: int = 0,
    provider: Optional[str] = None,
    backend: Optional[str] = None,
    level: int = 1,
    raw_usage: Optional[dict] = None,
) -> Optional[dict]:
    """Record a single LLM API call with provider-native token usage.

    Dispatches on ``provider`` to the matching raw table and copies the
    ``raw_usage`` keys into that table's native columns verbatim (absent
    keys stay NULL). When ``raw_usage`` is empty (the provider returned no
    usage object), falls back to the coalesced params so a degraded stream
    still records magnitudes -- the coalesced ``input_tokens`` /
    ``output_tokens`` / ``cached_tokens`` params exist ONLY for that
    fallback; no normalized column is ever written.

    Args:
        conversation_id: UUID of the conversation.
        user_id: Owner's integer user ID.
        model: Model name used for this call.
        call_type: ApiCallType.TOP_LEVEL or ApiCallType.SUB_AGENT.
        input_tokens: Coalesced input-token count (fallback only).
        output_tokens: Coalesced output-token count (fallback only).
        duration_ms: Call duration in milliseconds.
        agent_name: Sub-agent display name (None for top-level).
        cached_tokens: Coalesced cached-token count (fallback only).
        provider: Provider that produced this row
            ("gemini"/"anthropic"/"openrouter"). If None, inferred from the
            model prefix (claude* -> anthropic).
        backend: SDK transport backend ("genapi"/"vertex"/"openrouter").
        level: Sub-agent nesting depth (1 = top-level/1st-level, 2 = nested).
        raw_usage: Lossless provider-native token-count fields for this call
            (None/empty when the provider returned no usage object).

    Returns:
        The newly created row as a dict, or None when the provider is
        unknown (logged and skipped -- analytics loss must not break a turn).
    """
    resolved = provider
    if resolved is None:
        # Defensive/legacy inference: every Anthropic model id starts with
        # "claude" (chat/llm/config.py MODEL_REGISTRY).
        resolved = "anthropic" if model.startswith("claude") else "gemini"

    usage = raw_usage or {}
    if resolved == "gemini":
        native = {field: usage.get(field) for field in _GEMINI_USAGE_FIELDS}
        if not usage:
            # Degraded-stream fallback from the coalesced params. The Gemini
            # coalesced values are verbatim copies of these native fields.
            native["prompt_token_count"] = input_tokens
            native["candidates_token_count"] = output_tokens
            native["cached_content_token_count"] = cached_tokens
        row_cls = LlmCallGemini
    elif resolved == "anthropic":
        native = {field: usage.get(field) for field in _ANTHROPIC_USAGE_FIELDS}
        if not usage:
            # Degraded-stream fallback: cached_tokens merged read+creation,
            # so attributing it all to cache_read is an approximation (the
            # merged total is exact); creation columns stay NULL.
            native["input_tokens"] = input_tokens
            native["output_tokens"] = output_tokens
            native["cache_read_input_tokens"] = cached_tokens
        row_cls = LlmCallAnthropic
    elif resolved == "openrouter":
        native = {field: usage.get(field) for field in _OPENROUTER_USAGE_FIELDS}
        if not usage:
            # Degraded-stream fallback: the OpenRouter coalesced values are
            # verbatim copies of these native fields.
            native["prompt_tokens"] = input_tokens
            native["completion_tokens"] = output_tokens
            native["cached_prompt_tokens"] = cached_tokens
        row_cls = LlmCallOpenRouter
    else:
        logger.warning(
            "record_api_call: unknown provider %r for model %s -- skipping "
            "(add a raw table + dispatcher branch to record it)",
            resolved, model,
        )
        return None

    async with AsyncSessionLocal() as db:
        api_call = row_cls(
            conversation_id=conversation_id,
            user_id=user_id,
            model=model,
            call_type=str(call_type),
            agent_name=agent_name,
            backend=backend,
            level=level,
            duration_ms=duration_ms,
            raw_usage=(raw_usage or None),
            **native,
        )
        db.add(api_call)
        await db.commit()
        await db.refresh(api_call)
        return _api_call_to_dict(api_call, resolved)


async def get_conversation_usage(conversation_id: str) -> list[dict]:
    """Return all raw API call records for a conversation, ordered by created_at.

    Reads both per-provider tables and merges the rows chronologically;
    each dict is tagged with its ``provider`` and carries that provider's
    native usage columns. Debug/test reader -- no production callers.

    Args:
        conversation_id: UUID of the conversation.

    Returns:
        List of API call dicts.
    """
    async with AsyncSessionLocal() as db:
        rows: list[dict] = []
        for row_cls, provider in (
            (LlmCallGemini, "gemini"),
            (LlmCallAnthropic, "anthropic"),
            (LlmCallOpenRouter, "openrouter"),
        ):
            result = await db.execute(
                select(row_cls)
                .where(row_cls.conversation_id == conversation_id)
                .order_by(row_cls.created_at)
            )
            rows.extend(_api_call_to_dict(c, provider) for c in result.scalars().all())
    rows.sort(key=lambda r: r["created_at"] or "")
    return rows


async def get_latest_context_tokens(
    conversation_id: str,
    provider: str,
) -> Optional[int]:
    """Return the latest top-level call's context size for a conversation.

    "Context size" is the input-side token count of the most recent
    TOP_LEVEL call in the given provider's raw table -- the same per-call
    formula the pricing tiers key on (Gemini: ``prompt_token_count``;
    Anthropic: ``input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens``). This approximates what the next turn
    will re-read, which is what the expensive-resume warning
    (chat/expensive_resume.py) needs.

    Sub-agent rows are excluded so a small sub-agent call recorded after
    the top-level turn never masks the real conversation context.

    Args:
        conversation_id: UUID of the conversation.
        provider: "gemini" or "anthropic" (anything else returns None).

    Returns:
        Token count, or None when the provider is unknown or the
        conversation has no recorded top-level calls.
    """
    if provider == "gemini":
        row_cls = LlmCallGemini
        ctx_fields = ("prompt_token_count",)
    elif provider == "anthropic":
        row_cls = LlmCallAnthropic
        ctx_fields = (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    elif provider == "openrouter":
        row_cls = LlmCallOpenRouter
        ctx_fields = ("prompt_tokens",)
    else:
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(*(getattr(row_cls, field) for field in ctx_fields))
            .where(
                row_cls.conversation_id == conversation_id,
                row_cls.call_type == str(ApiCallType.TOP_LEVEL),
            )
            # id is the cheap monotonic tiebreak for same-timestamp rows.
            .order_by(row_cls.id.desc())
            .limit(1)
        )
        row = result.first()
    if row is None:
        return None
    return sum(int(value or 0) for value in row)


async def get_latest_top_level_call_at(
    conversation_id: str,
    provider: str,
) -> Optional[datetime]:
    """Return the ``created_at`` of the latest top-level call for a conversation.

    Companion to :func:`get_latest_context_tokens` (same row selection): the
    compaction-aware expensive-resume check needs to know whether any real
    turn ran *after* the last compaction, in which case the recorded context
    size is authoritative again. Returns None for unknown providers or
    conversations with no recorded top-level calls.
    """
    if provider == "gemini":
        row_cls = LlmCallGemini
    elif provider == "anthropic":
        row_cls = LlmCallAnthropic
    elif provider == "openrouter":
        row_cls = LlmCallOpenRouter
    else:
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(row_cls.created_at)
            .where(
                row_cls.conversation_id == conversation_id,
                row_cls.call_type == str(ApiCallType.TOP_LEVEL),
            )
            .order_by(row_cls.id.desc())
            .limit(1)
        )
        row = result.first()
    return row[0] if row else None


async def get_usage_by_model_for_conversations(
    conversation_ids: list[str],
) -> dict[str, dict]:
    """Return per-model provider-native token usage for a batch of conversations.

    Issues one grouped query per provider table filtered to the given
    conversation ids (two queries total for the whole batch -- no N+1),
    grouped by ``(conversation_id, model)`` and summing each table's native
    usage columns. The results are merged in Python into a per-conversation
    breakdown so the admin dashboard can show, for each conversation, one
    line per model used (including any sub-agent models) plus a coarse
    conversation-level total.

    Cost estimation (context-tier pricing): the aggregated per-model sums
    alone are NOT a valid input for $ math on tier-priced models -- summing
    across calls destroys the per-call context size the tier keys on
    (Gemini: ``prompt_token_count``; Anthropic: ``input_tokens +
    cache_read_input_tokens + cache_creation_input_tokens``). The grouped
    query therefore also groups on a per-call long-context flag (context >
    ``LONG_CONTEXT_THRESHOLD``), prices each same-tier bucket via
    ``db/llm_pricing.py`` (cost is linear in the token fields within a
    tier), and merges the buckets back into one entry per model.

    Reported cost: OpenRouter rows carry the USD amount the provider
    reported charging (``cost``, plus ``upstream_inference_cost`` on BYOK
    rows). The query additionally groups on a per-call "has reported cost"
    flag; buckets
    with it use the summed reported amount instead of the estimate, and
    every cost figure is accompanied by a ``cost_source`` --
    ``"reported"`` (every call priced from provider-reported amounts),
    ``"estimated"`` (every call list-price estimated), ``"mixed"`` (some
    of each, e.g. rows recorded before capture existed) or None when the
    figure itself is None.

    Args:
        conversation_ids: Conversation UUIDs to aggregate. Conversations with
            no recorded calls simply do not appear in the result.

    Returns:
        Mapping of conversation_id ->
        {
            "models": [
                # One entry per model, ordered by descending total_tokens.
                # Discriminated on "provider"; "metrics" carries that
                # provider's summed native fields (ints, 0 when all-NULL);
                # "estimated_cost_usd" is the list-price estimate, or None
                # when the model has no entry in db/llm_pricing.py:
                {"model": str, "provider": "gemini", "call_count": N,
                 "total_tokens": N,  # prompt + candidates + thoughts + tool_use_prompt
                 "estimated_cost_usd": float | None,
                 "cost_source": "reported" | "estimated" | "mixed" | None,
                 "metrics": {"prompt_token_count": N,
                             "cached_content_token_count": N,
                             "candidates_token_count": N,
                             "thoughts_token_count": N,
                             "tool_use_prompt_token_count": N}},
                {"model": str, "provider": "anthropic", "call_count": N,
                 "total_tokens": N,  # input + output + cache_read + cache_creation
                 "estimated_cost_usd": float | None,
                 "metrics": {"input_tokens": N, "output_tokens": N,
                             "cache_read_input_tokens": N,
                             "cache_creation_input_tokens": N,
                             "cache_creation_5m_input_tokens": N,
                             "cache_creation_1h_input_tokens": N}},
            ],
            # Coarse magnitude; "estimated_cost_usd" is None when ANY model
            # in the conversation lacks pricing (a partial sum would read as
            # a full conversation cost).
            "total": {"call_count": N, "total_tokens": N,
                      "estimated_cost_usd": float | None,
                      "cost_source": "reported" | "estimated" | "mixed" | None},
        }
    """
    if not conversation_ids:
        return {}

    by_key, _user_ids = await _collect_usage_buckets(
        conversation_ids=conversation_ids
    )
    return _group_buckets_by_conversation(by_key)


async def get_usage_by_model_for_conversation_query(
    conversation_id_select: Select,
) -> dict[str, dict]:
    """``get_usage_by_model_for_conversations`` over a conversation-id query.

    Same per-conversation result shape, but the conversations are named by
    a single-column ``SELECT`` (e.g. every conversation a routine created)
    that runs as an ``IN (subquery)`` inside the grouped queries, so the
    caller never materialises the id list. Conversations with no recorded
    calls do not appear in the result.
    """
    by_key, _user_ids = await _collect_usage_buckets(
        conversation_id_select=conversation_id_select
    )
    return _group_buckets_by_conversation(by_key)


async def get_most_expensive_conversations(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 30,
) -> list[dict]:
    """Return the conversations with the highest estimated cost in a window.

    Aggregates the raw ``llm_calls_*`` tables across ALL conversations whose
    calls fall inside ``[start, end)`` (either bound optional -- both None
    means all time) using the same tier-bucketed grouped queries as
    ``get_usage_by_model_for_conversations``, then ranks conversations by
    their KNOWN cost -- the sum of the per-model estimates that have a
    pricing entry -- so a conversation mixing priced and unpriced models
    still ranks by what can be priced. The displayed totals keep the usual
    convention (``estimated_cost_usd`` is None when any model lacks
    pricing). Ties break on total_tokens.

    Args:
        start: Inclusive lower bound on ``created_at`` (UTC), or None.
        end: Exclusive upper bound on ``created_at`` (UTC), or None.
        limit: Number of top conversations to return.

    Returns:
        Ranked list (most expensive first) of
        ``{"conversation_id", "user_id", "models", "total"}`` dicts, where
        ``models``/``total`` match the get_usage_by_model_for_conversations
        shapes and ``user_id`` comes from the call rows themselves so
        deleted conversations still attribute their spend to an owner.
    """
    by_key, user_ids = await _collect_usage_buckets(start=start, end=end)
    by_conversation = _group_buckets_by_conversation(by_key)

    def known_cost(entry: dict) -> float:
        return sum(
            m["estimated_cost_usd"] or 0.0 for m in entry["models"]
        )

    ranked = sorted(
        by_conversation.items(),
        key=lambda kv: (known_cost(kv[1]), kv[1]["total"]["total_tokens"]),
        reverse=True,
    )[:limit]
    return [
        {
            "conversation_id": conv_id,
            "user_id": user_ids.get(conv_id),
            "models": entry["models"],
            "total": entry["total"],
        }
        for conv_id, entry in ranked
    ]


async def get_usage_by_user(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    routine_id_by_conversation: Optional[dict[str, str]] = None,
) -> dict[int, dict]:
    """Return per-user token usage and cost aggregates for a date window.

    Aggregates the raw ``llm_calls_*`` tables across ALL conversations whose
    calls fall inside ``[start, end)`` (either bound optional) and folds the
    (conversation, model) buckets by the ``user_id`` recorded on the call
    rows, so deleted conversations and deleted users keep their spend
    attributed. ``routine_id_by_conversation`` (conversation id -> the
    ``routine_id`` on its surviving row -- the store has no routine knowledge
    of its own) splits the cost figure in two and additionally breaks the
    routine half out per routine; conversations absent from the mapping are
    treated as non-routine.

    Returns:
        Mapping of user_id -> {
            "models": [...],   # get_usage_by_model_for_conversations entry
                               # shape, one per model across ALL of the
                               # user's conversations (routines included)
            "total": {...},    # same coarse total shape/None convention
            # Distinct conversations with at least one call in the window:
            "conversation_count": N,          # non-routine only
            "routine_conversation_count": N,
            # Cost split; each is None when any model in that split lacks
            # pricing (the usual partial-sum convention, per split), with
            # a cost_source companion (same values as the model entries,
            # None while the split has no priced contribution):
            "cost_excluding_routines_usd": float | None,
            "cost_excluding_routines_source": str | None,
            "cost_routines_usd": float | None,
            "cost_routines_source": str | None,
            # Sum of the priced per-model estimates (both splits) -- the
            # ranking key, never None:
            "known_cost_usd": float,
            # The routine split broken out per routine, sorted by known
            # cost descending (ties: routine id) -- one entry per routine
            # with at least one call in the window:
            "routines": [
                {
                    "routine_id": str,
                    "conversation_count": N,
                    # None when any model under this routine is unpriced
                    # (same per-split convention, one split per routine):
                    "cost_usd": float | None,
                    "cost_source": str | None,
                    "known_cost_usd": float,
                },
                ...
            ],
        }
    """
    routine_by_conv = routine_id_by_conversation or {}
    by_key, user_ids = await _collect_usage_buckets(start=start, end=end)

    by_user: dict[int, dict] = {}
    for (conv_id, _model), model_entry in by_key.items():
        user_id = user_ids[conv_id]
        entry = by_user.setdefault(
            user_id,
            {
                "models_by_name": {},
                "total": {
                    "call_count": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "cost_source": None,
                },
                "conversations": set(),
                "routine_conversations": set(),
                "cost_excluding_routines_usd": 0.0,
                "cost_excluding_routines_source": None,
                "cost_routines_usd": 0.0,
                "cost_routines_source": None,
                "known_cost_usd": 0.0,
                "routines": {},
            },
        )

        routine_id = routine_by_conv.get(conv_id)
        is_routine = routine_id is not None
        entry["routine_conversations" if is_routine else "conversations"].add(conv_id)
        cost = model_entry["estimated_cost_usd"]
        source = model_entry["cost_source"]
        if cost is not None:
            entry["known_cost_usd"] += cost
        # A partial sum would read as the split's full cost, so an unpriced
        # model nulls its split (add_cost).
        if is_routine:
            add_cost(entry, "cost_routines_usd", "cost_routines_source", cost, source)
            # Per-routine breakdown of the routine split: the same
            # null-on-unpriced convention, applied per routine, so one
            # unpriced model only hides its own routine's figure.
            routine_entry = entry["routines"].setdefault(
                routine_id,
                {
                    "conversations": set(),
                    "cost_usd": 0.0,
                    "cost_source": None,
                    "known_cost_usd": 0.0,
                },
            )
            routine_entry["conversations"].add(conv_id)
            if cost is not None:
                routine_entry["known_cost_usd"] += cost
            add_cost(routine_entry, "cost_usd", "cost_source", cost, source)
        else:
            add_cost(
                entry,
                "cost_excluding_routines_usd",
                "cost_excluding_routines_source",
                cost,
                source,
            )

        # Merge into the per-user per-model breakdown. Same-tier pricing is
        # linear in the token fields, and the tier split happened per call
        # upstream, so summing per-model entries across conversations is
        # exact.
        merged = entry["models_by_name"].get(model_entry["model"])
        if merged is None:
            entry["models_by_name"][model_entry["model"]] = {
                **model_entry,
                "metrics": dict(model_entry["metrics"]),
            }
        else:
            merged["call_count"] += model_entry["call_count"]
            merged["total_tokens"] += model_entry["total_tokens"]
            for field, value in model_entry["metrics"].items():
                merged["metrics"][field] += value
            add_cost(merged, "estimated_cost_usd", "cost_source", cost, source)

        total = entry["total"]
        total["call_count"] += model_entry["call_count"]
        total["total_tokens"] += model_entry["total_tokens"]
        add_cost(total, "estimated_cost_usd", "cost_source", cost, source)

    result: dict[int, dict] = {}
    for user_id, entry in by_user.items():
        models = sorted(
            entry["models_by_name"].values(),
            key=lambda m: m["total_tokens"],
            reverse=True,
        )
        for model_entry in models:
            if model_entry["estimated_cost_usd"] is not None:
                model_entry["estimated_cost_usd"] = round(
                    model_entry["estimated_cost_usd"], 6
                )
        total = entry["total"]
        if total["estimated_cost_usd"] is not None:
            total["estimated_cost_usd"] = round(total["estimated_cost_usd"], 6)
        routines = [
            {
                "routine_id": routine_id,
                "conversation_count": len(routine_entry["conversations"]),
                "cost_usd": (
                    None if routine_entry["cost_usd"] is None
                    else round(routine_entry["cost_usd"], 6)
                ),
                "cost_source": routine_entry["cost_source"],
                "known_cost_usd": round(routine_entry["known_cost_usd"], 6),
            }
            for routine_id, routine_entry in entry["routines"].items()
        ]
        routines.sort(key=lambda r: (-r["known_cost_usd"], r["routine_id"]))
        result[user_id] = {
            "models": models,
            "total": total,
            "conversation_count": len(entry["conversations"]),
            "routine_conversation_count": len(entry["routine_conversations"]),
            "cost_excluding_routines_usd": (
                None if entry["cost_excluding_routines_usd"] is None
                else round(entry["cost_excluding_routines_usd"], 6)
            ),
            "cost_excluding_routines_source": entry["cost_excluding_routines_source"],
            "cost_routines_usd": (
                None if entry["cost_routines_usd"] is None
                else round(entry["cost_routines_usd"], 6)
            ),
            "cost_routines_source": entry["cost_routines_source"],
            "known_cost_usd": round(entry["known_cost_usd"], 6),
            "routines": routines,
        }
    return result


async def get_usage_by_model(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    routine_id_by_conversation: Optional[dict[str, str]] = None,
    top_users: int = 10,
) -> list[dict]:
    """Return per-model token usage, cost and audience aggregates for a window.

    Aggregates the raw ``llm_calls_*`` tables across ALL conversations whose
    calls fall inside ``[start, end)`` (either bound optional) and folds the
    buckets by model: one entry per distinct model id with its total spend,
    how many distinct users and conversations used it, the share of its
    spend accrued in routine-created conversations
    (``routine_id_by_conversation`` -- conversation id -> ``routine_id`` on
    its surviving row, same contract as ``get_usage_by_user``) and the share
    accrued by sub-agent calls (the rows' ``call_type``, so an Opus
    sub-agent spawned from a Gemini conversation lands on the Opus row),
    plus the ``top_users`` most expensive users of the model.

    Cost figures follow the usual convention: ``None`` when the model has
    no pricing entry and no provider-reported amounts (then every split of
    that model is None too), with a ``cost_source`` companion. Sorted by
    known cost descending (ties: total tokens, then model id).

    Returns:
        [
            {
                "model": str,
                "provider": str,
                # get_usage_by_model_for_conversations entry shape for this
                # one model (merged across every conversation/call type):
                "usage": {...},
                "known_cost_usd": float,      # ranking key, never None
                "user_count": N,              # distinct users (call rows)
                "conversation_count": N,      # distinct conversations
                "cost_routines_usd": float | None,
                "cost_routines_source": str | None,
                "cost_subagents_usd": float | None,
                "cost_subagents_source": str | None,
                "users": [                    # top ``top_users`` by cost
                    {
                        "user_id": int,
                        "call_count": N,
                        "cost_usd": float | None,
                        "cost_source": str | None,
                        "known_cost_usd": float,
                    },
                    ...
                ],
            },
            ...
        ]
    """
    routine_by_conv = routine_id_by_conversation or {}
    by_key, user_ids = await _collect_usage_buckets(
        start=start, end=end, by_call_type=True
    )

    by_model: dict[str, dict] = {}
    for (conv_id, model, call_type), bucket in by_key.items():
        user_id = user_ids[conv_id]
        cost = bucket["estimated_cost_usd"]
        source = bucket["cost_source"]
        entry = by_model.get(model)
        if entry is None:
            usage = {k: v for k, v in bucket.items() if k != "call_type"}
            usage["metrics"] = dict(bucket["metrics"])
            entry = by_model[model] = {
                "usage": usage,
                "known_cost_usd": 0.0,
                "users": {},
                "conversations": set(),
                "cost_routines_usd": 0.0,
                "cost_routines_source": None,
                "cost_subagents_usd": 0.0,
                "cost_subagents_source": None,
            }
        else:
            # Same-tier pricing is linear in the token fields and the tier
            # split happened per call upstream, so summing buckets across
            # conversations and call types is exact.
            usage = entry["usage"]
            usage["call_count"] += bucket["call_count"]
            usage["total_tokens"] += bucket["total_tokens"]
            for field, value in bucket["metrics"].items():
                usage["metrics"][field] += value
            add_cost(usage, "estimated_cost_usd", "cost_source", cost, source)

        if cost is not None:
            entry["known_cost_usd"] += cost
        entry["conversations"].add(conv_id)
        if conv_id in routine_by_conv:
            add_cost(entry, "cost_routines_usd", "cost_routines_source", cost, source)
        if call_type == str(ApiCallType.SUB_AGENT):
            add_cost(
                entry, "cost_subagents_usd", "cost_subagents_source", cost, source
            )

        user_entry = entry["users"].setdefault(
            user_id,
            {
                "user_id": user_id,
                "call_count": 0,
                "cost_usd": 0.0,
                "cost_source": None,
                "known_cost_usd": 0.0,
            },
        )
        user_entry["call_count"] += bucket["call_count"]
        if cost is not None:
            user_entry["known_cost_usd"] += cost
        add_cost(user_entry, "cost_usd", "cost_source", cost, source)

    def _rounded(value: Optional[float]) -> Optional[float]:
        return None if value is None else round(value, 6)

    result = []
    for model, entry in by_model.items():
        usage = entry["usage"]
        usage["estimated_cost_usd"] = _rounded(usage["estimated_cost_usd"])
        users = sorted(
            entry["users"].values(),
            key=lambda u: (-u["known_cost_usd"], -u["call_count"], u["user_id"]),
        )
        result.append({
            "model": model,
            "provider": usage["provider"],
            "usage": usage,
            "known_cost_usd": round(entry["known_cost_usd"], 6),
            "user_count": len(entry["users"]),
            "conversation_count": len(entry["conversations"]),
            "cost_routines_usd": _rounded(entry["cost_routines_usd"]),
            "cost_routines_source": entry["cost_routines_source"],
            "cost_subagents_usd": _rounded(entry["cost_subagents_usd"]),
            "cost_subagents_source": entry["cost_subagents_source"],
            "users": [
                {
                    **u,
                    "cost_usd": _rounded(u["cost_usd"]),
                    "known_cost_usd": round(u["known_cost_usd"], 6),
                }
                for u in users[:top_users]
            ],
        })
    result.sort(key=lambda m: (
        -m["known_cost_usd"], -m["usage"]["total_tokens"], m["model"],
    ))
    return result


async def get_latest_context_tokens_for_conversations(
    conversation_ids: list[str],
) -> dict[str, int]:
    """Batched variant of ``get_latest_context_tokens`` across both providers.

    For each conversation, finds the most recent TOP_LEVEL call in each
    provider's raw table (one grouped MAX(id) subquery per table -- no N+1)
    and returns that call's input-side context size, picking the later call
    when a conversation has top-level rows in both tables (model switches).
    Sub-agent rows are excluded, same as the single-conversation variant.

    Returns:
        Mapping of conversation_id -> context token count; conversations
        with no recorded top-level calls are absent.
    """
    if not conversation_ids:
        return {}

    specs = (
        (LlmCallGemini, ("prompt_token_count",)),
        (
            LlmCallAnthropic,
            (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ),
        ),
        (LlmCallOpenRouter, ("prompt_tokens",)),
    )
    # conversation_id -> (created_at sort key, context tokens)
    latest: dict[str, tuple[datetime, int]] = {}
    async with AsyncSessionLocal() as db:
        for row_cls, ctx_fields in specs:
            latest_ids = (
                select(func.max(row_cls.id))
                .where(
                    row_cls.conversation_id.in_(conversation_ids),
                    row_cls.call_type == str(ApiCallType.TOP_LEVEL),
                )
                .group_by(row_cls.conversation_id)
            )
            result = await db.execute(
                select(
                    row_cls.conversation_id,
                    row_cls.created_at,
                    *(getattr(row_cls, field) for field in ctx_fields),
                )
                .where(row_cls.id.in_(latest_ids))
            )
            for conv_id, created_at, *values in result.all():
                context = sum(int(value or 0) for value in values)
                # SQLite hands back naive datetimes; normalize defensively so
                # the cross-provider comparison can never mix aware and naive.
                sort_key = (
                    created_at.replace(tzinfo=None)
                    if created_at is not None
                    else datetime.min
                )
                current = latest.get(conv_id)
                if current is None or sort_key > current[0]:
                    latest[conv_id] = (sort_key, context)
    return {conv_id: context for conv_id, (_ts, context) in latest.items()}


# (table, provider, metric fields, fields summed into total_tokens,
# fields whose per-call sum is the context size the pricing tier keys on,
# reported-cost spec -- ``(marker column, per-row USD expression builder)``
# where a non-NULL marker column means the provider reported the call's
# cost and the builder maps the row class to that amount; None for
# providers that report token counts only).
# Gemini total = summed total_token_count when reported (candidates
# excludes thoughts; prompt includes cached, counted once). Anthropic
# reports no total, so it is the sum of all four buckets.
_PROVIDER_AGG_SPECS = (
    (
        LlmCallGemini,
        "gemini",
        (
            "prompt_token_count",
            "cached_content_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "tool_use_prompt_token_count",
        ),
        (
            "prompt_token_count",
            "candidates_token_count",
            "thoughts_token_count",
            "tool_use_prompt_token_count",
        ),
        ("prompt_token_count",),
        None,
    ),
    (
        LlmCallAnthropic,
        "anthropic",
        _ANTHROPIC_USAGE_FIELDS,
        (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ),
        (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ),
        None,
    ),
    # OpenRouter total = prompt + completion (prompt includes cached, counted
    # once; completion includes reasoning, so total_tokens is not re-summed).
    (
        LlmCallOpenRouter,
        "openrouter",
        (
            "prompt_tokens",
            "cached_prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
        ),
        (
            "prompt_tokens",
            "completion_tokens",
        ),
        ("prompt_tokens",),
        ("cost", lambda row_cls: _openrouter_reported_cost(row_cls)),
    ),
)


def _openrouter_reported_cost(row_cls):
    """Per-row USD amount an OpenRouter call cost: what OpenRouter charged
    the account (``cost``) plus, only on bring-your-own-key requests, the
    upstream provider's charge billed to the user's own key. OpenRouter has
    been observed populating ``upstream_inference_cost`` (equal to
    ``cost``) on non-BYOK requests too, hence the ``is_byok`` gate rather
    than a plain sum."""
    return func.coalesce(row_cls.cost, 0.0) + case(
        (row_cls.is_byok.is_(True), func.coalesce(row_cls.upstream_inference_cost, 0.0)),
        else_=0.0,
    )

COST_SOURCE_REPORTED = "reported"
COST_SOURCE_ESTIMATED = "estimated"
COST_SOURCE_MIXED = "mixed"


def add_cost(
    target: dict,
    value_key: str,
    source_key: str,
    cost: Optional[float],
    source: Optional[str],
) -> None:
    """Fold one bucket's cost into ``target[value_key]`` with the
    null-on-unpriced convention, tracking where the figure came from in
    ``target[source_key]``.

    The value starts at 0.0 with a None source ("nothing folded in yet");
    an unpriced contribution (``cost`` None) nulls both for good, since a
    partial sum would read as the whole figure. Otherwise the source
    becomes the contribution's source on the first fold and degrades to
    ``"mixed"`` as soon as a differently-sourced contribution arrives.
    """
    if target[value_key] is None or cost is None:
        target[value_key] = None
        target[source_key] = None
        return
    target[value_key] += cost
    current = target[source_key]
    if current is None or current == source:
        target[source_key] = source
    else:
        target[source_key] = COST_SOURCE_MIXED


async def _collect_usage_buckets(
    conversation_ids: Optional[list[str]] = None,
    conversation_id_select: Optional[Select] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    by_call_type: bool = False,
) -> tuple[dict[tuple, dict], dict[str, int]]:
    """Run the grouped per-provider queries and merge the tier buckets.

    Shared core of the aggregation entry points: filters by conversation
    ids (batch endpoint), by a single-column ``SELECT`` of conversation ids
    (``conversation_id_select``, applied as an ``IN (subquery)`` so an
    unbounded set -- e.g. every run of a routine -- never has to be
    materialised into a bind-parameter list), and/or a ``created_at``
    window (cost analysis), one grouped query per provider table either
    way. The query splits each (conversation, model) into (at most) a
    normal- and a long-context bucket so tier pricing is exact; the
    buckets merge back into one entry per key.

    ``by_call_type`` additionally splits the buckets on the rows'
    ``call_type`` (top-level vs sub-agent), for callers that need to tell
    sub-agent spend apart (the models report); the key then gains the
    call type as a third element and each entry carries a ``call_type``.

    Returns:
        ``(by_key, user_ids)``: ``by_key`` maps (conversation_id, model) --
        or (conversation_id, model, call_type) -- to a merged model entry;
        ``user_ids`` maps conversation_id -> owner user_id as recorded on
        the call rows (available even when the conversation row itself has
        been deleted).
    """
    by_key: dict[tuple, dict] = {}
    user_ids: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        for (
            row_cls, provider, metric_fields, total_fields, ctx_fields, cost_spec
        ) in _PROVIDER_AGG_SPECS:
            context_size = sum(
                func.coalesce(getattr(row_cls, field), 0) for field in ctx_fields
            )
            long_context = case(
                (context_size > LONG_CONTEXT_THRESHOLD, 1), else_=0
            )
            if cost_spec is not None:
                # Rows carrying a provider-reported amount are bucketed
                # apart from the ones that must be estimated, so a mixed
                # window prices each row the best way it can.
                marker_field, cost_expr = cost_spec
                has_reported = case(
                    (getattr(row_cls, marker_field).isnot(None), 1), else_=0
                )
                reported_cost = func.coalesce(func.sum(cost_expr(row_cls)), 0.0)
            else:
                has_reported = literal(0)
                reported_cost = literal(0.0)
            group_cols = [
                row_cls.conversation_id, row_cls.model, long_context, has_reported,
            ]
            if by_call_type:
                group_cols.append(row_cls.call_type)
                call_type_col = row_cls.call_type
            else:
                call_type_col = literal(None)
            stmt = (
                select(
                    row_cls.conversation_id,
                    row_cls.model,
                    long_context,
                    has_reported,
                    call_type_col,
                    func.count(row_cls.id),
                    # A conversation has exactly one owner; MAX picks it
                    # without widening the GROUP BY.
                    func.max(row_cls.user_id),
                    reported_cost,
                    *(
                        func.coalesce(func.sum(getattr(row_cls, field)), 0)
                        for field in metric_fields
                    ),
                )
                .group_by(*group_cols)
            )
            if conversation_ids is not None:
                stmt = stmt.where(row_cls.conversation_id.in_(conversation_ids))
            if conversation_id_select is not None:
                stmt = stmt.where(
                    row_cls.conversation_id.in_(conversation_id_select)
                )
            if start is not None:
                stmt = stmt.where(row_cls.created_at >= start)
            if end is not None:
                stmt = stmt.where(row_cls.created_at < end)
            result = await db.execute(stmt)
            for (
                conv_id, model, is_long, is_reported, call_type, call_count,
                user_id, reported, *sums
            ) in result.all():
                metrics = {
                    field: int(value)
                    for field, value in zip(metric_fields, sums)
                }
                total_tokens = sum(metrics[field] for field in total_fields)
                if is_reported:
                    cost: Optional[float] = float(reported)
                    source: Optional[str] = COST_SOURCE_REPORTED
                else:
                    cost = estimate_cost_usd(
                        provider, model, metrics, long_context=bool(is_long)
                    )
                    source = COST_SOURCE_ESTIMATED if cost is not None else None
                user_ids[conv_id] = int(user_id)
                key = (conv_id, model, call_type) if by_call_type else (conv_id, model)
                entry = by_key.get(key)
                if entry is None:
                    entry = {
                        "model": model,
                        "provider": provider,
                        "call_count": int(call_count),
                        "total_tokens": total_tokens,
                        "estimated_cost_usd": cost,
                        "cost_source": source,
                        "metrics": metrics,
                    }
                    if by_call_type:
                        entry["call_type"] = call_type
                    by_key[key] = entry
                    continue
                entry["call_count"] += int(call_count)
                entry["total_tokens"] += total_tokens
                for field in metric_fields:
                    entry["metrics"][field] += metrics[field]
                add_cost(entry, "estimated_cost_usd", "cost_source", cost, source)
    return by_key, user_ids


def _group_buckets_by_conversation(
    by_key: dict[tuple[str, str], dict],
) -> dict[str, dict]:
    """Fold merged model entries into the per-conversation result shape."""
    by_conversation: dict[str, dict] = {}
    for (conv_id, _model), model_entry in by_key.items():
        if model_entry["estimated_cost_usd"] is not None:
            # Round away float-accumulation noise; sub-microdollar precision
            # is meaningless for an estimate.
            model_entry["estimated_cost_usd"] = round(
                model_entry["estimated_cost_usd"], 6
            )
        entry = by_conversation.setdefault(
            conv_id,
            {
                "models": [],
                "total": {
                    "call_count": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "cost_source": None,
                },
            },
        )
        entry["models"].append(model_entry)
        total = entry["total"]
        total["call_count"] += model_entry["call_count"]
        total["total_tokens"] += model_entry["total_tokens"]
        # A partial sum would read as the whole conversation's cost, so an
        # unpriced model nulls the total (add_cost).
        add_cost(
            total,
            "estimated_cost_usd",
            "cost_source",
            model_entry["estimated_cost_usd"],
            model_entry["cost_source"],
        )
        if total["estimated_cost_usd"] is not None:
            total["estimated_cost_usd"] = round(total["estimated_cost_usd"], 6)

    # Stable, useful ordering: heaviest model first within each conversation.
    for entry in by_conversation.values():
        entry["models"].sort(key=lambda m: m["total_tokens"], reverse=True)

    return by_conversation


def _api_call_to_dict(api_call, provider: str) -> dict:
    """Convert an LlmCall* ORM instance to a plain dict."""
    fields = _USAGE_FIELDS_BY_PROVIDER[provider]
    row = {
        "id": api_call.id,
        "conversation_id": api_call.conversation_id,
        "user_id": api_call.user_id,
        "model": api_call.model,
        "call_type": api_call.call_type,
        "agent_name": api_call.agent_name,
        "provider": provider,
        "backend": api_call.backend,
        "level": api_call.level,
        "raw_usage": api_call.raw_usage,
        "duration_ms": api_call.duration_ms,
        "created_at": api_call.created_at.isoformat() if api_call.created_at else None,
    }
    for field in fields:
        row[field] = getattr(api_call, field)
    return row
