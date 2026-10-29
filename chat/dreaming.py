"""Dreaming: offline hygiene reviews of each user's saved memories.

A *dream* reads one user's active memories, asks the admin-chosen model
(config/dreaming.py) for clean-up proposals -- merge near-duplicates,
archive stale or contradicted facts, make relative dates absolute, shorten
over-long entries -- and registers each proposal as a ``memory_suggestions``
row (``source == "dream"``, no conversation) for the user to accept or
dismiss from the inbox's Memories tab. Nothing changes until they accept,
so a dream can only ever be wrong in the inbox, never in the memory store.
Every dream is a ``dream_runs`` ledger row (db/dream_run_store.py) with the
model, counts, token usage and list-price cost of its one model call.

The daemon (:func:`dreaming_loop`, an asyncio task started from the
FastAPI lifespan in quest.py like chat/slack_notifier.py) polls every
``POLL_INTERVAL_SECONDS`` and, while the ``dreaming`` feature gate is open,
dreams for each user the gate admits whose store has moved enough since
their last dream (:func:`skip_reason`). Dreams run one at a time so a
nightly sweep never competes with daytime users for provider quota. An
admin can also dream for themselves at once from Settings > Dreaming
(:func:`run_dream` with ``trigger="manual"``, which skips the pacing
rules but not the gate).

A dream proposes two shapes, both expressed with the fields the
``suggest_memory`` tool already uses so the review UI carries over:

- ``replace``: ``content`` + ``replaces_memory_id`` -- the memory's text
  rewritten (a merge, a shortened or clarified text, an absolute date);
- ``archive``: ``replaces_memory_id`` with no ``content`` -- archive the
  memory (stale, contradicted by a newer one, or merged into another
  proposal).

A merge of two memories is therefore one ``replace`` carrying the merged
text plus one ``archive`` for the other memory; the user reviews each row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import dream_run_store
from db import memory_suggestion_store as suggestion_store
from db.memory_store import MAX_MEMORY_SIZE, list_memories
from db.models import DreamRunStatus, MemorySuggestionSource

logger = logging.getLogger(__name__)

# How often the daemon looks for users due a dream.
POLL_INTERVAL_SECONDS = 15 * 60

# Pacing of scheduled dreams, per user: at least this long since the last
# run (of any outcome, so a failing dream is not retried every poll) and at
# least this many memories created since it. A first dream waits for a
# store worth reviewing.
MIN_HOURS_BETWEEN_DREAMS = 24
MIN_NEW_MEMORIES = 3
MIN_MEMORIES_FIRST_DREAM = 5

# Fewer active memories than this and there is nothing to compare.
MIN_MEMORIES_TO_DREAM = 2

# A ``running`` row older than this belongs to a dream the process lost
# (a restart mid-call); it no longer blocks the user's next dream.
STALE_RUNNING_HOURS = 1

# Proposals kept per dream; the prompt asks for at most this many too.
MAX_PROPOSALS_PER_DREAM = 10

# Automatic model choice: the first of these that is available and allowed
# in private conversations, else any such model. Stored (registry) ids.
MODEL_PREFERENCE = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5",
    "claude-sonnet-5",
)

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"

PROPOSAL_KINDS = ("replace", "archive")

# Serialises dreams (one model call in flight at a time) and remembers
# whose dream is running so a manual run never doubles a scheduled one.
_dream_lock = asyncio.Lock()
_dreaming_users: set[int] = set()


class DreamingUnavailable(Exception):
    """No model can run a dream right now (none configured / allowed)."""


class DreamError(Exception):
    """The model call ran but its answer could not be used."""


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

async def dreaming_loop() -> None:
    """Main loop: poll forever, dreaming for every user who is due."""
    logger.info(
        "[dreaming] daemon started (poll interval: %ds, min spacing: %dh)",
        POLL_INTERVAL_SECONDS, MIN_HOURS_BETWEEN_DREAMS,
    )
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            logger.info("[dreaming] daemon cancelled, shutting down")
            raise
        except Exception:
            logger.exception("[dreaming] error in poll cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _poll_once() -> None:
    """One sweep: a scheduled dream for each admitted user who is due."""
    from chat.auth import check_user_allowed
    from config.feature_gates import FEATURE_DREAMING, dreaming_enabled_for, is_feature_enabled
    from db.user_store import list_all_users

    if not is_feature_enabled(FEATURE_DREAMING):
        return
    for user in await list_all_users():
        email = user.get("email") or ""
        if not dreaming_enabled_for(email) or not check_user_allowed(email):
            continue
        if await skip_reason(user["id"], scheduled=True) is not None:
            continue
        await run_dream(user, TRIGGER_SCHEDULED)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def skip_reason(user_id: int, *, scheduled: bool) -> Optional[str]:
    """Why ``user_id`` should not dream now, or None when they may.

    Every dream needs ``MIN_MEMORIES_TO_DREAM`` active memories, no dream
    already running for the user and no dream suggestion still awaiting
    review (piling more onto an unreviewed batch helps nobody). A
    ``scheduled`` dream also waits ``MIN_HOURS_BETWEEN_DREAMS`` since the
    last run and for ``MIN_NEW_MEMORIES`` memories created since it
    (``MIN_MEMORIES_FIRST_DREAM`` when there was none); a manual run skips
    that pacing.
    """
    if user_id in _dreaming_users:
        return "running"
    if await suggestion_store.count_pending_for_user(
        user_id, source=MemorySuggestionSource.DREAM,
    ):
        return "pending_review"
    if await dream_run_store.count_memories_created_since(user_id, None) < MIN_MEMORIES_TO_DREAM:
        return "too_few_memories"

    latest = await dream_run_store.latest_run_for_user(user_id)
    now = datetime.now(timezone.utc)
    if latest is not None:
        started = _parse_ts(latest["started_at"]) or now
        if (
            latest["status"] == DreamRunStatus.RUNNING
            and now - started < timedelta(hours=STALE_RUNNING_HOURS)
        ):
            return "running"
    if not scheduled:
        return None

    if latest is not None:
        started = _parse_ts(latest["started_at"]) or now
        if now - started < timedelta(hours=MIN_HOURS_BETWEEN_DREAMS):
            return "too_soon"
        since = started.replace(tzinfo=None)
        needed = MIN_NEW_MEMORIES
    else:
        since = None
        needed = MIN_MEMORIES_FIRST_DREAM
    if await dream_run_store.count_memories_created_since(user_id, since) < needed:
        return "too_few_new_memories"
    return None


# ---------------------------------------------------------------------------
# Model choice
# ---------------------------------------------------------------------------

def dreamable_models() -> list[str]:
    """Stored ids of the models a dream may run on: available (configured,
    not failing health) and allowed in private conversations -- a dream
    reads private memories, so a public-only provider never sees them."""
    from chat.llm.config import get_available_models
    from config.model_selection import is_model_allowed

    return [m for m in get_available_models() if is_model_allowed(m, public=False)]


def resolve_dream_model(settings: dict) -> tuple[str, Optional[str], bool]:
    """``(model_id, thinking_effort, automatic)`` for the next dream.

    The admin's pinned model wins while it is dreamable; otherwise (and
    with no pin) the choice is automatic: the first of
    ``MODEL_PREFERENCE`` that is dreamable, else the first dreamable model.
    A pinned model that is not dreamable is logged and falls back rather
    than skipping the night. The thinking level is kept only when the
    chosen model lists it. Raises DreamingUnavailable with no candidate.
    """
    from chat.llm.config import resolve_model, resolve_thinking_effort

    candidates = dreamable_models()
    if not candidates:
        raise DreamingUnavailable(
            "No model is available for dreaming: none is configured, healthy "
            "and allowed in private conversations."
        )
    pinned = settings.get("model")
    automatic = True
    if pinned and pinned in candidates:
        model = pinned
        automatic = False
    else:
        if pinned:
            logger.warning(
                "[dreaming] configured model %s is not available; choosing automatically",
                pinned,
            )
        model = next((m for m in MODEL_PREFERENCE if m in candidates), candidates[0])
    effort = resolve_thinking_effort(resolve_model(model), settings.get("thinking_effort"))
    return model, effort, automatic


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

DREAM_SYSTEM_PROMPT = """You are reviewing one user's saved memories for Quest, an assistant that \
injects these memories into the system prompt of every conversation with the user. Your job is \
hygiene, not invention: make the store smaller, current and unambiguous without changing what it \
says about the user.

Propose a change only when you have a clear reason. Each proposal targets exactly one existing \
memory by its id and is one of:

- "replace": new text for that memory. Use it to merge near-duplicates (put the merged text on the \
memory you keep, and archive the others), to turn a relative date into an absolute one using the \
memory's created date, to shorten an over-long memory while keeping every fact and every "why", or \
to reword something ambiguous.
- "archive": retire that memory with no replacement. Use it for a memory contradicted or superseded \
by a newer one (archive the older unless the newer is clearly the mistake), for a temporary state \
that has long passed given today's date, and for each memory you merged into a "replace" proposal.

Rules:
- Never invent facts, never change the meaning of a memory, never drop the reason behind a \
correction or preference.
- A memory that is merely short, odd or old is fine; do not touch it.
- When two memories might be the same fact or might be two facts, leave them both alone.
- When unsure, propose nothing. An empty list is a good answer.
- At most {max_proposals} proposals, each memory id at most once, each "replace" text under \
{max_chars} characters and different from the current text.
- "reasoning" is one short sentence the user will read next to the proposal.

Answer with JSON only, no prose and no code fence:
{{"proposals": [{{"kind": "replace" | "archive", "memory_id": "<id>", "content": "<new text, replace only>", "reasoning": "<one sentence>"}}]}}"""

DREAM_USER_PROMPT = """Today is {today}. The user has {count} active memories, newest first. Quest \
shows at most the newest {max_injected} memories, {max_injected_chars} characters of them, in full \
in a conversation; this store is {total_chars} characters{over_budget}.

{memories}"""


def _render_memory(memory: dict) -> str:
    created = (memory.get("created_at") or "")[:10]
    attrs = f' id="{memory["id"]}"'
    if created:
        attrs += f' created="{created}"'
    return f"<memory{attrs}>\n{(memory.get('content') or '').strip()}\n</memory>"


def build_dream_prompts(memories: list[dict], today: Optional[datetime] = None) -> tuple[str, str]:
    """``(system_prompt, user_prompt)`` for a dream over ``memories``
    (active, newest first, as ``list_memories`` returns them)."""
    from chat.memory_prompt import PERSONAL_MEMORY_BUDGET as budget

    today = today or datetime.now(timezone.utc)
    rendered = "\n".join(_render_memory(m) for m in memories)
    total_chars = sum(len((m.get("content") or "").strip()) for m in memories)
    over_budget = ""
    if len(memories) > budget.full_count or total_chars > budget.full_chars:
        over_budget = (
            ", so the older memories reach conversations only as a one-line "
            "index: consolidation that brings it under the limits is worth proposing"
        )
    system = DREAM_SYSTEM_PROMPT.format(
        max_proposals=MAX_PROPOSALS_PER_DREAM, max_chars=MAX_MEMORY_SIZE,
    )
    user = DREAM_USER_PROMPT.format(
        today=today.strftime("%Y-%m-%d"),
        count=len(memories),
        max_injected=budget.full_count,
        max_injected_chars=f"{budget.full_chars:,}",
        total_chars=f"{total_chars:,}",
        over_budget=over_budget,
        memories=rendered,
    )
    return system, user


# ---------------------------------------------------------------------------
# Answer handling
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_proposals(text: str) -> list:
    """The ``proposals`` list from the model's answer. Tolerates a code
    fence and prose around the JSON object; raises DreamError otherwise."""
    cleaned = _FENCE_RE.sub("", text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise DreamError("The dream answered with no JSON object.")
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError as exc:
        raise DreamError(f"The dream's JSON could not be parsed: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("proposals"), list):
        raise DreamError('The dream\'s JSON has no "proposals" list.')
    return data["proposals"]


def validate_proposals(proposals: list, memories: list[dict]) -> tuple[list[dict], list[str]]:
    """Keep the proposals that can become suggestions; explain the rest.

    A kept proposal is ``{"kind", "memory_id", "content", "reasoning"}``
    (``content`` None for an archive) naming an active memory once, with
    replace text that is non-empty, within the memory size cap and
    different from the current text; at most ``MAX_PROPOSALS_PER_DREAM``.
    """
    by_id = {m["id"]: (m.get("content") or "").strip() for m in memories}
    kept: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(proposals):
        if not isinstance(raw, dict):
            rejected.append(f"#{i}: not an object")
            continue
        kind = raw.get("kind")
        memory_id = raw.get("memory_id")
        if kind not in PROPOSAL_KINDS:
            rejected.append(f"#{i}: unknown kind {kind!r}")
            continue
        if not isinstance(memory_id, str) or memory_id not in by_id:
            rejected.append(f"#{i}: unknown memory_id {memory_id!r}")
            continue
        if memory_id in seen:
            rejected.append(f"#{i}: memory {memory_id} proposed twice")
            continue
        content = None
        if kind == "replace":
            try:
                content = suggestion_store.normalize_content(raw.get("content"))
            except ValueError as exc:
                rejected.append(f"#{i}: {exc}")
                continue
            if content == by_id[memory_id]:
                rejected.append(f"#{i}: replace text equals the current text")
                continue
        reasoning = raw.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            reasoning = None
        if len(kept) >= MAX_PROPOSALS_PER_DREAM:
            rejected.append(f"#{i}: over the {MAX_PROPOSALS_PER_DREAM}-proposal cap")
            continue
        seen.add(memory_id)
        kept.append({
            "kind": kind, "memory_id": memory_id,
            "content": content, "reasoning": reasoning,
        })
    return kept, rejected


# ---------------------------------------------------------------------------
# The dream
# ---------------------------------------------------------------------------

async def _ask_model(model: str, effort: Optional[str], system: str, user: str):
    """One model call on an ephemeral session; ``(text, usage, provider_name)``."""
    from chat.llm.config import get_provider_for_model, get_provider_for_model_instance

    provider_name = get_provider_for_model(model)
    provider = get_provider_for_model_instance(model)
    session = provider.create_session(
        model=model, system_prompt=system, tools=[], history=None,
        thinking_effort=effort,
    )
    # The prompt is never reused, so a cache write would be pure surcharge.
    provider.disable_cache_writes(session)
    chunks: list[str] = []
    async for event in provider.send_message_stream(session, user):
        if event.type == "text":
            chunks.append(event.text)
    return "".join(chunks), provider.get_usage(session), provider_name


def _estimate_cost(provider_name: str, model: str, usage) -> Optional[float]:
    from db.llm_pricing import estimate_cost_usd, long_context_threshold

    metrics = dict(usage.raw_usage or {})
    if not metrics:
        if provider_name == "anthropic":
            metrics = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        else:
            metrics = {
                "prompt_token_count": usage.input_tokens,
                "candidates_token_count": usage.output_tokens,
            }
    try:
        return estimate_cost_usd(
            provider_name, model, metrics,
            long_context=usage.input_tokens > long_context_threshold(provider_name, model),
        )
    except Exception:  # pragma: no cover - pricing must never fail a dream
        logger.debug("[dreaming] cost estimate failed", exc_info=True)
        return None


async def run_dream(user: dict, trigger: str) -> dict:
    """Dream for ``user`` (``{"id", "email"}``) now and return the finished
    ``dream_runs`` row (``status`` completed or failed, never raises).

    Callers check :func:`skip_reason` first; this function only guards
    against two dreams for one user at once. Dreams are serialised across
    users by ``_dream_lock``.
    """
    user_id = user["id"]
    async with _dream_lock:
        if user_id in _dreaming_users:
            raise RuntimeError("a dream is already running for this user")
        _dreaming_users.add(user_id)
        try:
            return await _run_dream_locked(user, trigger)
        finally:
            _dreaming_users.discard(user_id)


async def _run_dream_locked(user: dict, trigger: str) -> dict:
    from chat.memory_suggestions import publish_changed
    from config.dreaming import read_dreaming_settings

    user_id = user["id"]
    run = await dream_run_store.create_run(user_id, trigger)
    fields: dict = {}
    started = time.monotonic()
    try:
        memories = await list_memories(user_id)
        fields["memories_read"] = len(memories)
        if len(memories) < MIN_MEMORIES_TO_DREAM:
            raise DreamError("Too few active memories to review.")

        model, effort, _automatic = resolve_dream_model(read_dreaming_settings())
        fields.update(model=model, thinking_effort=effort)
        system, prompt = build_dream_prompts(memories)
        text, usage, provider_name = await _ask_model(model, effort, system, prompt)
        fields.update(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            estimated_cost_usd=_estimate_cost(provider_name, model, usage),
        )

        kept, rejected = validate_proposals(parse_proposals(text), memories)
        if rejected:
            logger.info(
                "[dreaming] user_id=%s: %d proposal(s) dropped: %s",
                user_id, len(rejected), "; ".join(rejected),
            )
        created = 0
        for proposal in kept:
            try:
                await suggestion_store.create_suggestion(
                    user_id, None, proposal["content"],
                    reasoning=proposal["reasoning"],
                    replaces_memory_id=proposal["memory_id"],
                    source=MemorySuggestionSource.DREAM,
                    dream_run_id=run["id"],
                )
                created += 1
            except ValueError as exc:
                # The memory went away between the read and the write.
                logger.info("[dreaming] user_id=%s: proposal dropped: %s", user_id, exc)
        fields["suggestions_created"] = created
        if created:
            await publish_changed(user_id, None)
        finished = await dream_run_store.finish_run(run["id"], DreamRunStatus.COMPLETED, **fields)
        logger.info(
            "[dreaming] user_id=%s %s dream done in %.1fs on %s: %d memories read, "
            "%d suggestion(s), est. $%s",
            user_id, trigger, time.monotonic() - started, model, len(memories), created,
            fields.get("estimated_cost_usd"),
        )
        return finished or {**run, **fields, "status": DreamRunStatus.COMPLETED}
    except asyncio.CancelledError:
        await dream_run_store.finish_run(
            run["id"], DreamRunStatus.FAILED, error="interrupted by a server restart", **fields,
        )
        raise
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        logger.warning("[dreaming] user_id=%s %s dream failed: %s", user_id, trigger, message)
        finished = await dream_run_store.finish_run(
            run["id"], DreamRunStatus.FAILED, error=message[:500], **fields,
        )
        return finished or {**run, **fields, "status": DreamRunStatus.FAILED, "error": message}
