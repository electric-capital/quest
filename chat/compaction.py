"""Conversation-context compaction (prototype).

Replaces the older portion of a conversation's model-facing history
(``sdk_history.json``) with a single structured summary message plus a
deterministic "preserved records" appendix, keeping the most recent
~KEEP_RECENT_TOKENS tokens of history verbatim. Modeled on the pi coding
agent's compaction design (cut at safe message boundaries, never between a
tool call and its result; summarize the older span with a one-off LLM call).

Key properties:
- ``sdk_history.json`` stays the single source of truth for the *current*
  model-facing history. The pre-compaction file is preserved verbatim as
  ``sdk_history.compacted-NNN.json`` in the conversation directory before
  the new envelope is written, so no history is ever lost.
- Beyond the summary, a deterministic appendix preserves records extracted
  from the compacted span: files read/modified, ``authed_get``/``authed_post``
  URLs, ``run_python``/``run_script`` calls and their results, and top-level
  sub-agent (``agent_task*``) calls and responses.
- A ``compaction_meta.json`` sidecar records when compaction happened and
  the estimated post-compaction context size, so the expensive-resume check
  (chat/expensive_resume.py) stops warning once the history has shrunk --
  the ``llm_calls_*`` tables only learn the new context size after the next
  real turn.
- The display transcript (``chat_history.json``) is never rewritten; a
  ``compaction`` marker message is appended so the user can see what
  happened and read the summary the model will see.

Entry point: :func:`compact_conversation`, called by
``POST /conversations/{id}/compact`` (chat/routes/conversations.py), which
the FE offers from the expensive-resume warning card.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# Tokens of recent history kept verbatim after a compaction. Same default
# as pi's keepRecentTokens.
KEEP_RECENT_TOKENS = 20_000

# Conservative chars-per-token heuristic used for cut-point math only (the
# authoritative context size still comes from provider usage after the next
# turn).
_CHARS_PER_TOKEN = 4

# Per-item truncation caps for serialized/preserved content.
_SERIALIZED_RESULT_MAX_CHARS = 2_000
_SERIALIZED_ARGS_MAX_CHARS = 500
_PRESERVED_RESULT_MAX_CHARS = 2_000
_PRESERVED_PROMPT_MAX_CHARS = 600
_PRESERVED_SCRIPT_MAX_CHARS = 400
_PRESERVED_URLS_MAX = 150

# Cap on the serialized-conversation text sent to the summarizer. Beyond
# this, the middle of the conversation is elided (the head carries the goal,
# the tail the latest state).
_SUMMARIZER_INPUT_MAX_CHARS = 600_000

_COMPACTION_META_FILENAME = "compaction_meta.json"

_SUMMARIZATION_SYSTEM_PROMPT = """You are a conversation summarizer for an AI assistant platform.
You will be given a serialized transcript of a conversation between a user and an AI assistant that made tool calls.
Produce ONLY the requested structured summary. Do not continue the conversation, do not address the user, do not add commentary before or after the summary."""

_SUMMARIZATION_PROMPT = """Summarize the conversation transcript below so a fresh assistant instance can seamlessly continue the work. The transcript may begin with an earlier [CONTEXT SUMMARY] block from a previous compaction -- merge its information into your summary rather than repeating it verbatim.

Use exactly these markdown sections:

## Goal
What the user is ultimately trying to accomplish.

## Constraints & Preferences
Requirements, style preferences, and corrections the user has given.

## Progress
### Done
### In Progress
### Blocked

## Key Decisions
Decisions made and their rationale.

## Next Steps
What should happen next, in order.

## Critical Context
Exact values needed later: identifiers, URLs, file paths, dates, numbers, names, error messages, and any exact strings that must not be lost.

Transcript:

{conversation}"""


class CompactionError(Exception):
    """Raised when a conversation cannot be compacted. ``code`` is a stable
    machine-readable reason for the API layer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Token estimation and cut-point selection
# ---------------------------------------------------------------------------

def _estimate_entry_tokens(entry: Any) -> int:
    """chars/4 estimate over the JSON serialization of one history entry."""
    try:
        return max(1, len(json.dumps(entry, default=str)) // _CHARS_PER_TOKEN)
    except Exception:
        return 1


def _estimate_history_tokens(history: list) -> int:
    return sum(_estimate_entry_tokens(entry) for entry in history)


def _is_valid_cut_anthropic(entry: Any) -> bool:
    """A genuine user message (no tool_result blocks) is a safe cut point."""
    if not isinstance(entry, dict) or entry.get("role") != "user":
        return False
    content = entry.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def _gemini_parts(entry: Any) -> list:
    parts = entry.get("parts") if isinstance(entry, dict) else None
    return parts if isinstance(parts, list) else []


def _gemini_function_response(part: Any) -> Optional[dict]:
    if not isinstance(part, dict):
        return None
    fr = part.get("function_response") or part.get("functionResponse")
    return fr if isinstance(fr, dict) else None


def _gemini_function_call(part: Any) -> Optional[dict]:
    if not isinstance(part, dict):
        return None
    fc = part.get("function_call") or part.get("functionCall")
    return fc if isinstance(fc, dict) else None


def _is_valid_cut_gemini(entry: Any) -> bool:
    """A user Content with no function_response parts is a safe cut point."""
    if not isinstance(entry, dict) or entry.get("role") != "user":
        return False
    parts = _gemini_parts(entry)
    if not parts:
        return False
    return not any(_gemini_function_response(part) for part in parts)


_CUT_VALIDATORS = {
    "anthropic": _is_valid_cut_anthropic,
    "gemini": _is_valid_cut_gemini,
}


def find_cut_index(
    history: list,
    provider_name: str,
    keep_recent_tokens: int = KEEP_RECENT_TOKENS,
) -> int:
    """Return the index splitting history into [summarize) / [keep...).

    Walks backward from the newest entry accumulating estimated tokens until
    the keep budget is exceeded, then snaps to the nearest safe boundary --
    a genuine user message, so a tool call is never separated from its
    result. Prefers the nearest boundary at-or-before the budget point
    (keeping more); falls back to the nearest one after it. Returns 0 when
    there is nothing to summarize (history fits the budget or no safe
    boundary exists).
    """
    is_valid = _CUT_VALIDATORS.get(provider_name)
    if is_valid is None or len(history) < 4:
        return 0

    accumulated = 0
    candidate = 0
    for i in range(len(history) - 1, -1, -1):
        accumulated += _estimate_entry_tokens(history[i])
        if accumulated > keep_recent_tokens:
            candidate = i + 1
            break
    else:
        return 0  # everything fits in the keep budget

    for i in range(min(candidate, len(history) - 1), 0, -1):
        if is_valid(history[i]):
            return i
    for i in range(candidate + 1, len(history)):
        if is_valid(history[i]):
            return i
    return 0


# ---------------------------------------------------------------------------
# Normalized event walk over provider-native history
# ---------------------------------------------------------------------------

def _text_of_tool_result_content(content: Any) -> str:
    """Flatten an Anthropic tool_result content (str or block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(block.get("text", ""))
            elif isinstance(block, dict):
                chunks.append(f"[{block.get('type', 'attachment')} omitted]")
        return "\n".join(chunks)
    return str(content)


def _walk_anthropic(span: list) -> Iterator[dict]:
    for msg in span:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                yield {"kind": "user_text", "text": content}
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        yield {"kind": "user_text", "text": block.get("text", "")}
                    elif block.get("type") == "tool_result":
                        yield {
                            "kind": "tool_result",
                            "id": block.get("tool_use_id", ""),
                            "name": "",
                            "text": _text_of_tool_result_content(block.get("content")),
                        }
        elif role == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    yield {"kind": "assistant_text", "text": block.get("text", "")}
                elif block.get("type") == "tool_use":
                    args = block.get("input")
                    yield {
                        "kind": "tool_call",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "args": args if isinstance(args, dict) else {},
                    }
                # thinking / redacted_thinking blocks carry no durable info


def _walk_gemini(span: list) -> Iterator[dict]:
    for entry in span:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        for part in _gemini_parts(entry):
            if not isinstance(part, dict):
                continue
            fc = _gemini_function_call(part)
            fr = _gemini_function_response(part)
            if fc is not None:
                args = fc.get("args") or fc.get("arguments") or {}
                yield {
                    "kind": "tool_call",
                    "id": fc.get("id", "") or "",
                    "name": fc.get("name", "") or "",
                    "args": args if isinstance(args, dict) else {},
                }
            elif fr is not None:
                response = fr.get("response")
                text = ""
                if isinstance(response, dict):
                    text = str(response.get("result", response))
                elif response is not None:
                    text = str(response)
                yield {
                    "kind": "tool_result",
                    "id": fr.get("id", "") or "",
                    "name": fr.get("name", "") or "",
                    "text": text,
                }
            elif part.get("text"):
                # Skip model "thought" parts; keep ordinary text.
                if part.get("thought"):
                    continue
                kind = "user_text" if role == "user" else "assistant_text"
                yield {"kind": kind, "text": part["text"]}


_WALKERS = {
    "anthropic": _walk_anthropic,
    "gemini": _walk_gemini,
}


def _pair_tool_events(events: list[dict]) -> list[tuple[str, dict, str]]:
    """Match tool_result events back to their tool_call.

    Returns ``[(tool_name, args, result_text), ...]`` in call order.
    Anthropic results carry only the tool_use id; Gemini results carry the
    tool name (and sometimes an id). Match by id when possible, else FIFO
    by name.
    """
    pending: list[dict] = []
    paired: list[tuple[str, dict, str]] = []
    for event in events:
        if event["kind"] == "tool_call":
            pending.append(event)
        elif event["kind"] == "tool_result":
            match = None
            if event.get("id"):
                match = next(
                    (c for c in pending if c.get("id") and c["id"] == event["id"]),
                    None,
                )
            if match is None and event.get("name"):
                match = next(
                    (c for c in pending if c["name"] == event["name"]), None,
                )
            if match is None and pending:
                match = pending[0]
            if match is not None:
                pending.remove(match)
                paired.append((match["name"], match["args"], event["text"]))
    return paired


# ---------------------------------------------------------------------------
# Serialization for the summarizer
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n[... {omitted} chars truncated ...]"


def serialize_span(span: list, provider_name: str) -> str:
    """Flatten a history span to plain text for the summarization call."""
    walker = _WALKERS[provider_name]
    events = list(walker(span))
    # Resolve result names so [Tool result] lines are labeled for Anthropic.
    names_by_id = {
        e["id"]: e["name"] for e in events
        if e["kind"] == "tool_call" and e.get("id")
    }
    lines = []
    for event in events:
        if event["kind"] == "user_text":
            lines.append(f"[User]: {event['text']}")
        elif event["kind"] == "assistant_text":
            lines.append(f"[Assistant]: {event['text']}")
        elif event["kind"] == "tool_call":
            try:
                args_json = json.dumps(event["args"], default=str)
            except Exception:
                args_json = str(event["args"])
            lines.append(
                f"[Assistant tool call]: {event['name']}"
                f"({_truncate(args_json, _SERIALIZED_ARGS_MAX_CHARS)})"
            )
        elif event["kind"] == "tool_result":
            name = event.get("name") or names_by_id.get(event.get("id", ""), "")
            label = f" ({name})" if name else ""
            lines.append(
                f"[Tool result{label}]: "
                f"{_truncate(event['text'], _SERIALIZED_RESULT_MAX_CHARS)}"
            )
    text = "\n".join(lines)
    if len(text) > _SUMMARIZER_INPUT_MAX_CHARS:
        head = _SUMMARIZER_INPUT_MAX_CHARS // 3
        tail = _SUMMARIZER_INPUT_MAX_CHARS - head
        text = (
            text[:head]
            + "\n[... middle of conversation omitted from summarization input ...]\n"
            + text[-tail:]
        )
    return text


# ---------------------------------------------------------------------------
# Preserved records
# ---------------------------------------------------------------------------

def extract_preserved_records(span: list, provider_name: str) -> dict:
    """Extract the deterministic records kept verbatim across compaction.

    Returns a dict with ``read_files``, ``modified_files``,
    ``authed_requests``, ``python_calls``, and ``sub_agent_tasks``.
    """
    events = list(_WALKERS[provider_name](span))
    paired = _pair_tool_events(events)

    read_files: list[str] = []
    modified_files: list[str] = []
    authed_requests: list[str] = []
    python_calls: list[dict] = []
    sub_agent_tasks: list[dict] = []

    def _add_unique(seq: list[str], value: str) -> None:
        if value and value not in seq:
            seq.append(value)

    for name, args, result in paired:
        if name == "get_workspace_file":
            _add_unique(read_files, str(args.get("path", "")))
        elif name in ("write_workspace_file", "edit_workspace_file"):
            _add_unique(modified_files, str(args.get("path", "")))
        elif name in ("authed_get", "authed_post"):
            verb = "GET" if name == "authed_get" else "POST"
            _add_unique(authed_requests, f"{verb} {args.get('url', '')}")
        elif name == "run_python":
            python_calls.append({
                "call": "run_python",
                "script": _truncate(
                    str(args.get("script", "")), _PRESERVED_SCRIPT_MAX_CHARS,
                ),
                "result": _truncate(result, _PRESERVED_RESULT_MAX_CHARS),
            })
        elif name == "run_script":
            script_args = str(args.get("args", "") or "")
            label = str(args.get("path", ""))
            if script_args:
                label = f"{label} {script_args}"
            python_calls.append({
                "call": "run_script",
                "script": label,
                "result": _truncate(result, _PRESERVED_RESULT_MAX_CHARS),
            })
        elif name == "agent_task":
            sub_agent_tasks.append({
                "name": str(args.get("name", "sub-agent")),
                "description": str(args.get("description", "")),
                "prompt": _truncate(
                    str(args.get("prompt", "")), _PRESERVED_PROMPT_MAX_CHARS,
                ),
                "response": _truncate(result, _PRESERVED_RESULT_MAX_CHARS),
            })
        elif name in ("agent_task_parallel", "agent_task_parallel_template"):
            tasks = args.get("tasks")
            task_names = (
                ", ".join(
                    str(t.get("name", "?")) for t in tasks if isinstance(t, dict)
                )
                if isinstance(tasks, list) else ""
            )
            sub_agent_tasks.append({
                "name": f"parallel sub-agents ({task_names})" if task_names
                        else "parallel sub-agents",
                "description": "",
                "prompt": "",
                "response": _truncate(result, _PRESERVED_RESULT_MAX_CHARS),
            })

    # A file that was modified is more usefully listed there alone.
    read_files = [f for f in read_files if f not in modified_files]
    if len(authed_requests) > _PRESERVED_URLS_MAX:
        overflow = len(authed_requests) - _PRESERVED_URLS_MAX
        authed_requests = authed_requests[:_PRESERVED_URLS_MAX]
        authed_requests.append(f"[... and {overflow} more requests]")

    return {
        "read_files": read_files,
        "modified_files": modified_files,
        "authed_requests": authed_requests,
        "python_calls": python_calls,
        "sub_agent_tasks": sub_agent_tasks,
    }


def render_preserved_records(records: dict) -> str:
    """Render extracted records as the deterministic appendix text."""
    sections = []
    if records["read_files"]:
        body = "\n".join(f"- {f}" for f in records["read_files"])
        sections.append(f"<read-files>\n{body}\n</read-files>")
    if records["modified_files"]:
        body = "\n".join(f"- {f}" for f in records["modified_files"])
        sections.append(f"<modified-files>\n{body}\n</modified-files>")
    if records["authed_requests"]:
        body = "\n".join(f"- {u}" for u in records["authed_requests"])
        sections.append(f"<authed-requests>\n{body}\n</authed-requests>")
    if records["python_calls"]:
        chunks = []
        for i, call in enumerate(records["python_calls"], 1):
            chunks.append(
                f"--- {call['call']} #{i} ---\n"
                f"script: {call['script']}\n"
                f"result: {call['result']}"
            )
        sections.append(
            "<python-calls>\n" + "\n".join(chunks) + "\n</python-calls>"
        )
    if records["sub_agent_tasks"]:
        chunks = []
        for task in records["sub_agent_tasks"]:
            desc = f" ({task['description']})" if task["description"] else ""
            chunk = f"--- sub-agent \"{task['name']}\"{desc} ---"
            if task["prompt"]:
                chunk += f"\nprompt: {task['prompt']}"
            chunk += f"\nresponse: {task['response']}"
            chunks.append(chunk)
        sections.append(
            "<sub-agent-tasks>\n" + "\n".join(chunks) + "\n</sub-agent-tasks>"
        )
    if not sections:
        return ""
    return (
        "<preserved-records>\n"
        "Records below were extracted verbatim from the compacted history.\n"
        + "\n".join(sections)
        + "\n</preserved-records>"
    )


# ---------------------------------------------------------------------------
# Summary message construction
# ---------------------------------------------------------------------------

_ACK_TEXT = (
    "Understood. I've reviewed the summary of the earlier conversation and "
    "will continue from here."
)


def build_summary_message_text(summary: str, appendix: str) -> str:
    parts = [
        "[CONTEXT SUMMARY]",
        "Earlier conversation history was compacted to save context space. "
        "The summary below replaces the compacted messages; the most recent "
        "messages follow unchanged.",
        "",
        summary,
    ]
    if appendix:
        parts += ["", appendix]
    parts += ["", "[END CONTEXT SUMMARY]"]
    return "\n".join(parts)


_ANTHROPIC_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def _strip_anthropic_thinking(kept: list) -> list:
    """Drop thinking blocks from the kept span of an Anthropic history.

    Anthropic thinking blocks are bound to the exact request prefix they
    were produced under (system prompt, tools, and every earlier message).
    Compaction replaces everything before the kept span with a new summary
    turn, so every kept block fails that prefix check on replay: older
    accounts get the block silently ignored, while accounts created on or
    after 2026-08-31 get a 400 on Opus 5.5 (and Fable-class) models. The
    API strips prior-turn thinking before the model sees it anyway, so
    removing the blocks loses nothing. Removing *all* thinking blocks is an
    allowed edit (removing some from the middle is not). Assistant turns
    that were only thinking never persist (provider guard), so stripping
    can't leave an empty assistant message behind.
    """
    stripped = []
    for msg in kept:
        content = msg.get("content") if isinstance(msg, dict) else None
        if msg.get("role") != "assistant" or not isinstance(content, list):
            stripped.append(msg)
            continue
        blocks = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") in _ANTHROPIC_THINKING_BLOCK_TYPES)
        ]
        if len(blocks) == len(content):
            stripped.append(msg)
        else:
            stripped.append({**msg, "content": blocks})
    return stripped


def _build_new_history(
    provider_name: str, summary_text: str, kept: list,
) -> list:
    if provider_name == "anthropic":
        return [
            {"role": "user", "content": summary_text},
            {"role": "assistant", "content": [{"type": "text", "text": _ACK_TEXT}]},
        ] + _strip_anthropic_thinking(kept)
    return [
        {"role": "user", "parts": [{"text": summary_text}]},
        {"role": "model", "parts": [{"text": _ACK_TEXT}]},
    ] + kept


# ---------------------------------------------------------------------------
# Compaction metadata sidecar
# ---------------------------------------------------------------------------

def _meta_file(conversation_id: str):
    from chat.storage import ChatStorage
    return ChatStorage._get_conversation_dir(conversation_id) / _COMPACTION_META_FILENAME


def load_compaction_meta(conversation_id: str) -> Optional[dict]:
    """Read the compaction sidecar; None when the conversation was never
    compacted (or the sidecar is unreadable)."""
    try:
        path = _meta_file(conversation_id)
        if not path.exists():
            return None
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning(
            "Failed to read compaction meta for conversation %s",
            conversation_id, exc_info=True,
        )
        return None


def _write_compaction_meta(conversation_id: str, meta: dict) -> None:
    with open(_meta_file(conversation_id), "w") as f:
        json.dump(meta, f, indent=2)


def compaction_newer_than(meta: Optional[dict], reference: Optional[datetime]) -> bool:
    """True when the sidecar's compacted_at is after ``reference`` (or the
    reference is unknown). Naive datetimes are treated as UTC."""
    if not meta or not meta.get("compacted_at"):
        return False
    try:
        compacted_at = datetime.fromisoformat(str(meta["compacted_at"]))
    except ValueError:
        return False
    if compacted_at.tzinfo is None:
        compacted_at = compacted_at.replace(tzinfo=timezone.utc)
    if reference is None:
        return True
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return compacted_at > reference


# ---------------------------------------------------------------------------
# Cost estimation for the summarization call
# ---------------------------------------------------------------------------

# Assumed output size of the structured summary, for the cost estimate.
_ASSUMED_SUMMARY_OUTPUT_TOKENS = 2_000


def estimate_compaction_cost_usd(
    conversation_id: str, model: str,
) -> Optional[float]:
    """Rough list-price USD cost of the one-off summarization call.

    Simulates the compaction split on the saved history and prices the
    serialized span (per-result truncation makes this far smaller than the
    raw context) as plain input plus an assumed ~2K-token summary output.
    No cache-write rate applies -- the summarizer session opts out of cache
    writes. Best-effort: returns None whenever the history is missing,
    mismatched, uncompactable, or the model has no pricing entry. Shown on
    the expensive-resume card so the user can weigh the one-time cost
    against the per-message resume cost.
    """
    try:
        from chat.gemini_api.history import _load_sdk_history
        from chat.llm.config import get_provider_for_model
        from db.llm_pricing import estimate_cost_usd, long_context_threshold

        loaded = _load_sdk_history(conversation_id)
        if loaded is None:
            return None
        history, disk_provider = loaded
        provider_name = get_provider_for_model(model)
        if provider_name != disk_provider:
            return None
        cut = find_cut_index(history, provider_name)
        if cut <= 0:
            return None
        serialized = serialize_span(history[:cut], provider_name)
        input_tokens = (
            len(serialized) + len(_SUMMARIZATION_PROMPT)
        ) // _CHARS_PER_TOKEN
        if provider_name == "anthropic":
            metrics = {
                "input_tokens": input_tokens,
                "output_tokens": _ASSUMED_SUMMARY_OUTPUT_TOKENS,
            }
        else:
            metrics = {
                "prompt_token_count": input_tokens,
                "candidates_token_count": _ASSUMED_SUMMARY_OUTPUT_TOKENS,
            }
        cost = estimate_cost_usd(
            provider_name, model, metrics,
            long_context=(
                input_tokens > long_context_threshold(provider_name, model)
            ),
        )
        return round(cost, 2) if cost is not None else None
    except Exception:
        logger.debug(
            "estimate_compaction_cost_usd failed (conversation=%s)",
            conversation_id, exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# The summarization LLM call
# ---------------------------------------------------------------------------

async def _summarize(provider, model: str, conversation_text: str) -> str:
    """One-off summarization call on an ephemeral session (no tools)."""
    session = provider.create_session(
        model=model,
        system_prompt=_SUMMARIZATION_SYSTEM_PROMPT,
        tools=[],
        history=None,
    )
    # This prompt is never reused, so a prompt-cache write would be pure
    # surcharge (Anthropic bills cache writes at 1.25x input).
    provider.disable_cache_writes(session)
    prompt = _SUMMARIZATION_PROMPT.format(conversation=conversation_text)
    chunks: list[str] = []
    async for event in provider.send_message_stream(session, prompt):
        if event.type == "text":
            chunks.append(event.text)
    summary = "".join(chunks).strip()
    if not summary:
        raise CompactionError(
            "summarization_failed", "The summarization call returned no text.",
        )
    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def compact_conversation(
    user_id: int,
    conversation_id: str,
    model: str,
    keep_recent_tokens: int = KEEP_RECENT_TOKENS,
) -> dict:
    """Compact a conversation's model-facing history.

    Loads ``sdk_history.json``, splits it at a safe boundary, summarizes the
    older span with a one-off LLM call on ``model``, archives the old file,
    writes the new envelope (summary + ack + kept messages), updates the
    ``compaction_meta.json`` sidecar, drops the in-memory session, and
    appends a ``compaction`` marker to the display transcript.

    Raises CompactionError with a stable ``code`` on any expected failure.
    Returns a JSON-ready result dict for the API layer.
    """
    from chat.gemini_api.constants import _SDK_HISTORY_FILENAME
    from chat.gemini_api.history import _load_sdk_history, _write_sdk_history_file
    from chat.gemini_api.session import remove_chat_session
    from chat.llm.config import (
        get_provider_for_model,
        get_provider_instance,
        model_instance_id,
    )
    from chat.storage import ChatStorage, _publish_appended_to_bus

    loaded = _load_sdk_history(conversation_id)
    if loaded is None:
        raise CompactionError(
            "no_history", "This conversation has no saved model history.",
        )
    history, disk_provider = loaded

    try:
        provider_name = get_provider_for_model(model)
    except ValueError:
        raise CompactionError("unknown_model", f"Unknown model: {model}")
    if provider_name != disk_provider:
        raise CompactionError(
            "provider_mismatch",
            f"The saved history is for provider '{disk_provider}' but the "
            f"conversation's model '{model}' uses '{provider_name}'.",
        )
    provider = get_provider_instance(provider_name, model_instance_id(model))

    if provider.get_pending_tool_use_args_from_history(history):
        raise CompactionError(
            "conversation_suspended",
            "This conversation is suspended waiting on a tool result and "
            "cannot be compacted right now.",
        )

    cut = find_cut_index(history, provider_name, keep_recent_tokens)
    if cut <= 0:
        raise CompactionError(
            "nothing_to_compact",
            "The conversation history is already small enough (or has no "
            "safe compaction boundary).",
        )
    span, kept = history[:cut], history[cut:]

    tokens_before = _estimate_history_tokens(history)
    conversation_text = serialize_span(span, provider_name)
    records = extract_preserved_records(span, provider_name)

    try:
        summary = await _summarize(provider, model, conversation_text)
    except CompactionError:
        raise
    except Exception as e:
        logger.exception(
            "Compaction summarization failed (conversation=%s)", conversation_id,
        )
        raise CompactionError(
            "summarization_failed", f"The summarization call failed: {e}",
        )

    summary_text = build_summary_message_text(
        summary, render_preserved_records(records),
    )
    new_history = _build_new_history(provider_name, summary_text, kept)
    tokens_after = _estimate_history_tokens(new_history)

    # Archive the pre-compaction envelope, then atomically replace it.
    prior_meta = load_compaction_meta(conversation_id) or {}
    count = int(prior_meta.get("count", 0)) + 1
    conversation_dir = ChatStorage._get_conversation_dir(conversation_id)
    archive_name = f"sdk_history.compacted-{count:03d}.json"
    shutil.copy2(
        conversation_dir / _SDK_HISTORY_FILENAME,
        conversation_dir / archive_name,
    )
    _write_sdk_history_file(conversation_id, new_history, provider_name)

    compacted_at = datetime.now(timezone.utc).isoformat()
    archives = list(prior_meta.get("archives", []))
    archives.append({
        "file": archive_name,
        "compacted_at": compacted_at,
        "messages_summarized": cut,
    })
    _write_compaction_meta(conversation_id, {
        "count": count,
        "compacted_at": compacted_at,
        "tokens_before_estimate": tokens_before,
        "tokens_after_estimate": tokens_after,
        "archives": archives,
    })

    # Drop the cached in-memory session so the next turn reloads the
    # compacted history from disk.
    remove_chat_session(user_id, conversation_id)

    # Append a display-only marker so the user sees what happened and can
    # read exactly what the model will see going forward.
    result = {
        "messages_summarized": cut,
        "messages_kept": len(kept),
        "tokens_before_estimate": tokens_before,
        "tokens_after_estimate": tokens_after,
        "archive_file": archive_name,
        "summary": summary_text,
    }
    try:
        appended = await ChatStorage.append_structured_messages(
            conversation_id,
            [{"type": "compaction", "role": "assistant", **result}],
        )
        _publish_appended_to_bus(conversation_id, appended)
    except Exception:
        logger.warning(
            "Failed to append compaction marker for conversation %s",
            conversation_id, exc_info=True,
        )

    logger.info(
        "Compacted conversation %s: %d messages summarized, %d kept, "
        "~%d -> ~%d tokens (archive=%s)",
        conversation_id, cut, len(kept), tokens_before, tokens_after,
        archive_name,
    )
    return result
