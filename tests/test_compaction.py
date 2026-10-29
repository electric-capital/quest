"""Tests for chat/compaction.py (conversation-context compaction)."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from chat.compaction import (
    CompactionError,
    build_summary_message_text,
    compact_conversation,
    compaction_count,
    compaction_newer_than,
    extract_preserved_records,
    find_cut_index,
    render_preserved_records,
    serialize_span,
)
from chat.llm.base import StreamEvent


def _run(coro):
    return asyncio.run(coro)


def _big_text(tokens: int) -> str:
    # chars/4 heuristic: 4 chars per wanted token
    return "x" * (tokens * 4)


# ---------------------------------------------------------------------------
# Cut-point selection
# ---------------------------------------------------------------------------

def _anthropic_turn(user_text: str, with_tool: bool = False) -> list:
    """One user->assistant exchange, optionally with a tool round-trip."""
    msgs = [{"role": "user", "content": user_text}]
    if with_tool:
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "get_workspace_file",
                 "input": {"path": "a.txt"}},
            ],
        })
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "data"},
            ],
        })
    msgs.append({
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
    })
    return msgs


def test_find_cut_index_anthropic_never_cuts_at_tool_result():
    history = []
    for i in range(30):
        history.extend(_anthropic_turn(_big_text(2000), with_tool=True))
    cut = find_cut_index(history, "anthropic", keep_recent_tokens=10_000)
    assert cut > 0
    # The kept span must start at a genuine user message.
    entry = history[cut]
    assert entry["role"] == "user"
    assert isinstance(entry["content"], str)


def test_find_cut_index_returns_zero_when_history_fits():
    history = _anthropic_turn("hello") + _anthropic_turn("world")
    assert find_cut_index(history, "anthropic") == 0


def test_find_cut_index_gemini_skips_function_response_entries():
    history = []
    for i in range(30):
        history.append({"role": "user", "parts": [{"text": _big_text(2000)}]})
        history.append({
            "role": "model",
            "parts": [{"function_call": {"name": "authed_get",
                                         "args": {"url": "https://x"}}}],
        })
        history.append({
            "role": "user",
            "parts": [{"function_response": {"name": "authed_get",
                                             "response": {"result": "ok"}}}],
        })
        history.append({"role": "model", "parts": [{"text": "done"}]})
    cut = find_cut_index(history, "gemini", keep_recent_tokens=10_000)
    assert cut > 0
    entry = history[cut]
    assert entry["role"] == "user"
    assert "function_response" not in json.dumps(entry)


def test_find_cut_index_unknown_provider():
    assert find_cut_index([{}] * 10, "nope") == 0


# ---------------------------------------------------------------------------
# Preserved-record extraction
# ---------------------------------------------------------------------------

def _anthropic_tool_exchange(name: str, args: dict, result: str, tool_id: str) -> list:
    return [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": args},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": result},
        ]},
    ]


def test_extract_preserved_records_anthropic():
    span = [{"role": "user", "content": "please do things"}]
    span += _anthropic_tool_exchange(
        "get_workspace_file", {"path": "read_me.txt"}, "contents", "t1")
    span += _anthropic_tool_exchange(
        "write_workspace_file", {"path": "out.csv", "content": "a,b"}, "ok", "t2")
    span += _anthropic_tool_exchange(
        "get_workspace_file", {"path": "out.csv"}, "a,b", "t3")
    span += _anthropic_tool_exchange(
        "authed_get", {"url": "https://api.github.com/repos/x"}, "{}", "t4")
    span += _anthropic_tool_exchange(
        "authed_post", {"url": "https://logging.googleapis.com/v2/entries:list"},
        "{}", "t5")
    span += _anthropic_tool_exchange(
        "run_python", {"script": "print(21 * 2)"}, "stdout: 42", "t6")
    span += _anthropic_tool_exchange(
        "agent_task",
        {"name": "Researcher", "description": "Research things",
         "prompt": "Find the answer"},
        "The answer is 42.", "t7")
    span.append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})

    records = extract_preserved_records(span, "anthropic")
    # out.csv was modified, so it is excluded from read_files.
    assert records["read_files"] == ["read_me.txt"]
    assert records["modified_files"] == ["out.csv"]
    assert records["authed_requests"] == [
        "GET https://api.github.com/repos/x",
        "POST https://logging.googleapis.com/v2/entries:list",
    ]
    assert records["python_calls"] == [
        {"call": "run_python", "script": "print(21 * 2)", "result": "stdout: 42"},
    ]
    assert records["sub_agent_tasks"] == [{
        "name": "Researcher",
        "description": "Research things",
        "prompt": "Find the answer",
        "response": "The answer is 42.",
    }]


def test_extract_preserved_records_gemini_matches_by_name():
    span = [
        {"role": "user", "parts": [{"text": "go"}]},
        {"role": "model", "parts": [
            {"function_call": {"name": "run_python",
                               "args": {"script": "print('hi')"}}},
        ]},
        {"role": "user", "parts": [
            {"function_response": {"name": "run_python",
                                   "response": {"result": "stdout: hi"}}},
        ]},
        {"role": "model", "parts": [{"text": "done"}]},
    ]
    records = extract_preserved_records(span, "gemini")
    assert records["python_calls"] == [
        {"call": "run_python", "script": "print('hi')", "result": "stdout: hi"},
    ]


def test_render_preserved_records_skips_empty_sections():
    records = {
        "read_files": ["a.txt"],
        "modified_files": [],
        "authed_requests": [],
        "python_calls": [],
        "sub_agent_tasks": [],
    }
    text = render_preserved_records(records)
    assert "<read-files>" in text
    assert "- a.txt" in text
    assert "<modified-files>" not in text
    assert "<python-calls>" not in text


def test_render_preserved_records_empty_is_empty_string():
    records = {k: [] for k in (
        "read_files", "modified_files", "authed_requests",
        "python_calls", "sub_agent_tasks",
    )}
    assert render_preserved_records(records) == ""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_serialize_span_anthropic_labels_and_truncates():
    span = [{"role": "user", "content": "question"}]
    span += _anthropic_tool_exchange(
        "get_workspace_file", {"path": "a.txt"}, "y" * 5000, "t1")
    span.append({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "secret", "signature": "sig"},
        {"type": "text", "text": "answer"},
    ]})
    text = serialize_span(span, "anthropic")
    assert "[User]: question" in text
    assert "[Assistant tool call]: get_workspace_file" in text
    assert "[Tool result (get_workspace_file)]" in text
    assert "chars truncated" in text
    assert "[Assistant]: answer" in text
    assert "secret" not in text  # thinking blocks are dropped


# ---------------------------------------------------------------------------
# New-history assembly
# ---------------------------------------------------------------------------

def test_build_new_history_anthropic_strips_kept_thinking_blocks():
    """Kept Anthropic assistant turns lose their thinking/redacted_thinking
    blocks: the summary turn changes the prefix they were bound to, so
    replaying them is at best ignored and at worst a 400 (Opus 5.5 on
    accounts subject to the prefix check). Text and tool_use survive."""
    from chat.compaction import _build_new_history

    kept = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "blob"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
        ]},
        {"role": "assistant", "content": "plain string turn"},
    ]
    original = json.loads(json.dumps(kept))

    history = _build_new_history("anthropic", "SUMMARY", kept)

    assert history[0] == {"role": "user", "content": "SUMMARY"}
    assert history[1]["role"] == "assistant"
    assert history[2:] == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
        ]},
        {"role": "assistant", "content": "plain string turn"},
    ]
    assert kept == original  # input not mutated


def test_build_new_history_gemini_keeps_span_verbatim():
    from chat.compaction import _build_new_history

    kept = [{"role": "user", "parts": [{"text": "q", "thought": True}]}]
    history = _build_new_history("gemini", "SUMMARY", kept)
    assert history[2:] == kept


# ---------------------------------------------------------------------------
# Compaction sidecar helpers
# ---------------------------------------------------------------------------

def test_compaction_newer_than():
    meta = {"compacted_at": "2026-08-10T12:00:00+00:00"}
    earlier = datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)
    naive_earlier = datetime(2026, 8, 10, 11, 0)
    assert compaction_newer_than(meta, earlier)
    assert compaction_newer_than(meta, naive_earlier)
    assert not compaction_newer_than(meta, later)
    assert compaction_newer_than(meta, None)
    assert not compaction_newer_than(None, earlier)
    assert not compaction_newer_than({}, earlier)
    assert not compaction_newer_than({"compacted_at": "garbage"}, earlier)


# ---------------------------------------------------------------------------
# End-to-end compact_conversation with a fake provider
# ---------------------------------------------------------------------------

class FakeAnthropicProvider:
    def __init__(self):
        self.summarize_calls = []
        self.cache_writes_disabled_sessions = []

    def get_pending_tool_use_args_from_history(self, history):
        return []

    def create_session(self, model, system_prompt, tools, history=None):
        return {"model": model, "system": system_prompt}

    def disable_cache_writes(self, session):
        self.cache_writes_disabled_sessions.append(session)

    async def send_message_stream(self, session, message):
        self.summarize_calls.append(message)
        yield StreamEvent(type="text", text="## Goal\nSummarized goal.")


def _install_compaction_fakes(monkeypatch, tmp_path, provider, history):
    """Point every filesystem/session dependency at tmp_path fakes."""
    from chat.storage import ChatStorage

    monkeypatch.setattr(
        ChatStorage, "_get_conversation_dir",
        staticmethod(lambda conversation_id: tmp_path),
    )

    appended = []

    async def fake_append(conversation_id, messages):
        appended.extend(messages)
        return [(i + 1, m) for i, m in enumerate(messages)]

    monkeypatch.setattr(
        ChatStorage, "append_structured_messages", staticmethod(fake_append),
    )
    monkeypatch.setattr(
        "chat.storage._publish_appended_to_bus", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "chat.gemini_api.session.remove_chat_session", lambda *a: True,
    )
    monkeypatch.setattr(
        "chat.llm.config.get_provider_instance", lambda name, instance_id=None: provider,
    )

    # Seed the on-disk envelope the real loader/writer will use.
    with open(tmp_path / "sdk_history.json", "w") as f:
        json.dump({"provider": "anthropic", "history": history}, f)

    return appended


def _long_anthropic_history() -> list:
    history = []
    for i in range(40):
        history.extend(_anthropic_turn(f"message {i} " + _big_text(2000)))
    return history


def test_compact_conversation_end_to_end(monkeypatch, tmp_path):
    provider = FakeAnthropicProvider()
    history = _long_anthropic_history()
    appended = _install_compaction_fakes(monkeypatch, tmp_path, provider, history)

    result = _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))

    assert result["messages_summarized"] > 0
    assert result["messages_kept"] > 0
    assert result["messages_summarized"] + result["messages_kept"] == len(history)
    assert result["tokens_after_estimate"] < result["tokens_before_estimate"]
    assert "Summarized goal." in result["summary"]

    # The one-off summarizer session must opt out of prompt-cache writes.
    assert len(provider.cache_writes_disabled_sessions) == 1

    # Old envelope archived verbatim.
    archive = tmp_path / result["archive_file"]
    assert archive.exists()
    with open(archive) as f:
        archived = json.load(f)
    assert archived["history"] == history

    # New envelope: summary + ack + kept tail, same provider.
    with open(tmp_path / "sdk_history.json") as f:
        envelope = json.load(f)
    assert envelope["provider"] == "anthropic"
    new_history = envelope["history"]
    assert new_history[0]["role"] == "user"
    assert "[CONTEXT SUMMARY]" in new_history[0]["content"]
    assert new_history[1]["role"] == "assistant"
    assert new_history[2:] == history[result["messages_summarized"]:]

    # Sidecar and display marker written.
    with open(tmp_path / "compaction_meta.json") as f:
        meta = json.load(f)
    assert meta["count"] == 1
    assert meta["tokens_after_estimate"] == result["tokens_after_estimate"]
    assert meta["archives"][0]["file"] == result["archive_file"]
    assert len(appended) == 1
    assert appended[0]["type"] == "compaction"


_DOC_NOTES_SEEN = {"docs": {"doc-1": {"instructions": 3, "memories": 7}}}
_DOC_READS = {"doc_ids": ["doc-1"]}


def _seed_doc_notes(tmp_path):
    """Seed the doc notes seen record and doc_reads.json beside
    sdk_history.json (the fakes point the conversation dir at tmp_path)."""
    (tmp_path / "doc_notes_seen.json").write_text(json.dumps(_DOC_NOTES_SEEN))
    (tmp_path / "doc_reads.json").write_text(json.dumps(_DOC_READS))


def test_compact_conversation_deletes_doc_notes_seen_keeps_doc_reads(
    monkeypatch, tmp_path,
):
    # The summary may drop a delivered doc notes block, so the record of
    # what the model was shown goes; the read gate's record stays.
    provider = FakeAnthropicProvider()
    _install_compaction_fakes(
        monkeypatch, tmp_path, provider, _long_anthropic_history(),
    )
    _seed_doc_notes(tmp_path)

    result = _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))

    assert result["messages_summarized"] > 0
    assert not (tmp_path / "doc_notes_seen.json").exists()
    assert json.loads((tmp_path / "doc_reads.json").read_text()) == _DOC_READS


def test_compact_conversation_clears_doc_notes_seen_before_replacing_history(
    monkeypatch, tmp_path,
):
    # A failure while replacing the history leaves the full history with no
    # record (one extra delivery), never the compacted history with a
    # stale record.
    provider = FakeAnthropicProvider()
    history = _long_anthropic_history()
    _install_compaction_fakes(monkeypatch, tmp_path, provider, history)
    _seed_doc_notes(tmp_path)

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "chat.gemini_api.history._write_sdk_history_file", failing_write,
    )
    with pytest.raises(OSError):
        _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))

    assert not (tmp_path / "doc_notes_seen.json").exists()
    assert json.loads((tmp_path / "doc_reads.json").read_text()) == _DOC_READS
    with open(tmp_path / "sdk_history.json") as f:
        assert json.load(f)["history"] == history


class _FailingStreamProvider(FakeAnthropicProvider):
    async def send_message_stream(self, session, message):
        raise RuntimeError("summarizer unavailable")
        yield  # pragma: no cover -- makes this an async generator


def _suspended_provider():
    provider = FakeAnthropicProvider()
    provider.get_pending_tool_use_args_from_history = (
        lambda history: [("t9", "wait_for_handles", {})]
    )
    return provider


@pytest.mark.parametrize(
    "make_provider, make_history, model, code",
    [
        (
            FakeAnthropicProvider,
            lambda: _anthropic_turn("hi") + _anthropic_turn("again"),
            "claude-opus-4-8",
            "nothing_to_compact",
        ),
        (
            FakeAnthropicProvider,
            _long_anthropic_history,
            "gemini-3.5-flash",
            "provider_mismatch",
        ),
        (
            _suspended_provider,
            _long_anthropic_history,
            "claude-opus-4-8",
            "conversation_suspended",
        ),
        (
            _FailingStreamProvider,
            _long_anthropic_history,
            "claude-opus-4-8",
            "summarization_failed",
        ),
    ],
)
def test_compact_conversation_refusal_keeps_doc_notes_seen(
    monkeypatch, tmp_path, make_provider, make_history, model, code,
):
    # A refusal leaves the history as it was, so the record still holds.
    _install_compaction_fakes(monkeypatch, tmp_path, make_provider(), make_history())
    _seed_doc_notes(tmp_path)

    with pytest.raises(CompactionError) as exc_info:
        _run(compact_conversation(1, "conv-1", model))

    assert exc_info.value.code == code
    assert json.loads((tmp_path / "doc_notes_seen.json").read_text()) == _DOC_NOTES_SEEN
    assert json.loads((tmp_path / "doc_reads.json").read_text()) == _DOC_READS


# ---------------------------------------------------------------------------
# Refusing while a model run is active (POST /conversations/{id}/compact)
# ---------------------------------------------------------------------------


# Each puts a run task into one registry the run drivers keep; monkeypatch
# removes it again at teardown.
def _register_send_run(monkeypatch, conversation_id, task):
    from chat.realtime import socket as realtime_socket

    monkeypatch.setitem(realtime_socket._active_send_runs, conversation_id, task)


def _register_resume(monkeypatch, conversation_id, task):
    from chat.wait_handles import resume as wait_resume

    monkeypatch.setitem(wait_resume._active_resumes, conversation_id, task)


def _register_slack_run(monkeypatch, conversation_id, task):
    from chat import slack_driven_runtime

    monkeypatch.setitem(slack_driven_runtime._active_runs, conversation_id, task)


RUN_REGISTRIES = {
    "ws_send": _register_send_run,
    "wait_handle_resume": _register_resume,
    "slack": _register_slack_run,
}


def _snapshot(tmp_path):
    """The files a refused compaction must leave exactly as they were."""
    return {
        p.name: p.read_bytes() for p in sorted(tmp_path.iterdir()) if p.is_file()
    }


def _install_route_fakes(monkeypatch, model="claude-opus-4-8"):
    import chat.routes.conversations as conv_routes
    from db import tool_wait_handle_store

    async def owned(user_id, conversation_id):
        return {"id": conversation_id, "model": model}

    async def no_pending(user_id, conversation_id):
        return []

    monkeypatch.setattr(conv_routes, "require_owned_conversation", owned)
    monkeypatch.setattr(tool_wait_handle_store, "list_pending_for_conversation", no_pending)
    return conv_routes.compact_conversation_endpoint


@pytest.mark.parametrize("registry", sorted(RUN_REGISTRIES))
def test_compact_route_refuses_while_a_run_is_active(monkeypatch, tmp_path, registry):
    from fastapi import HTTPException

    provider = FakeAnthropicProvider()
    _install_compaction_fakes(monkeypatch, tmp_path, provider, _long_anthropic_history())
    _seed_doc_notes(tmp_path)
    endpoint = _install_route_fakes(monkeypatch)
    before = _snapshot(tmp_path)

    async def scenario():
        run = asyncio.get_running_loop().create_future()
        RUN_REGISTRIES[registry](monkeypatch, "conv-1", run)
        with pytest.raises(HTTPException) as exc_info:
            await endpoint("conv-1", user={"id": 1})
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "conversation_busy"
        assert "in progress" in exc_info.value.detail["message"]
        # Refused before any work: no summary call, nothing on disk moved.
        assert provider.summarize_calls == []
        assert _snapshot(tmp_path) == before

        # Once the run has ended (its task done) the compaction goes through.
        run.set_result(None)
        return await endpoint("conv-1", user={"id": 1})

    result = _run(scenario())
    assert result["messages_summarized"] > 0
    assert not (tmp_path / "doc_notes_seen.json").exists()


def test_compact_route_proceeds_with_no_run(monkeypatch, tmp_path):
    provider = FakeAnthropicProvider()
    _install_compaction_fakes(monkeypatch, tmp_path, provider, _long_anthropic_history())
    endpoint = _install_route_fakes(monkeypatch)

    result = _run(endpoint("conv-1", user={"id": 1}))
    assert result["messages_summarized"] > 0


class _RunDuringSummaryProvider(FakeAnthropicProvider):
    """A summarizer during whose call a run starts (and, optionally, saves
    the history), as another tab could."""

    def __init__(self, tmp_path, monkeypatch, *, register=None, save=False):
        super().__init__()
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.register = register
        self.save = save

    async def send_message_stream(self, session, message):
        if self.register is not None:
            run = asyncio.get_running_loop().create_future()
            self.register(self.monkeypatch, "conv-1", run)
        if self.save:
            # The way every history writer saves: an atomic replace.
            from chat.storage import write_json_atomic

            envelope = json.loads((self.tmp_path / "sdk_history.json").read_text())
            envelope["history"] = envelope["history"] + _anthropic_turn("sent meanwhile")
            write_json_atomic(self.tmp_path / "sdk_history.json", envelope)
        async for event in super().send_message_stream(session, message):
            yield event


@pytest.mark.parametrize(
    "register, save",
    [
        pytest.param(_register_send_run, False, id="run-started"),
        pytest.param(_register_resume, False, id="resume-started"),
        pytest.param(_register_slack_run, False, id="slack-run-started"),
        pytest.param(None, True, id="run-started-and-saved"),
    ],
)
def test_compact_conversation_refuses_when_a_run_starts_during_the_summary(
    monkeypatch, tmp_path, register, save,
):
    provider = _RunDuringSummaryProvider(tmp_path, monkeypatch, register=register, save=save)
    _install_compaction_fakes(monkeypatch, tmp_path, provider, _long_anthropic_history())
    _seed_doc_notes(tmp_path)

    with pytest.raises(CompactionError) as exc_info:
        _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))

    assert exc_info.value.code == "conversation_busy"
    assert len(provider.summarize_calls) == 1
    # Nothing replaced: the history on disk is whatever the run left there,
    # no archive or meta was written, and both sidecars are untouched.
    envelope = json.loads((tmp_path / "sdk_history.json").read_text())
    assert ("sent meanwhile" in json.dumps(envelope)) is save
    assert not list(tmp_path.glob("sdk_history.compacted-*.json"))
    assert not (tmp_path / "compaction_meta.json").exists()
    assert json.loads((tmp_path / "doc_notes_seen.json").read_text()) == _DOC_NOTES_SEEN
    assert json.loads((tmp_path / "doc_reads.json").read_text()) == _DOC_READS


def test_compact_conversation_second_compaction_increments_archive(
    monkeypatch, tmp_path,
):
    provider = FakeAnthropicProvider()
    history = _long_anthropic_history()
    _install_compaction_fakes(monkeypatch, tmp_path, provider, history)

    first = _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))
    assert first["archive_file"] == "sdk_history.compacted-001.json"

    # Grow the (now compacted) history again so a second cut exists.
    with open(tmp_path / "sdk_history.json") as f:
        envelope = json.load(f)
    grown = envelope["history"] + _long_anthropic_history()
    with open(tmp_path / "sdk_history.json", "w") as f:
        json.dump({"provider": "anthropic", "history": grown}, f)

    second = _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))
    assert second["archive_file"] == "sdk_history.compacted-002.json"
    with open(tmp_path / "compaction_meta.json") as f:
        meta = json.load(f)
    assert meta["count"] == 2
    assert len(meta["archives"]) == 2
    assert compaction_count("conv-1") == 2


def test_compaction_count_without_or_with_bad_sidecar(monkeypatch, tmp_path):
    from chat.storage import ChatStorage

    monkeypatch.setattr(
        ChatStorage, "_get_conversation_dir",
        staticmethod(lambda conversation_id: tmp_path),
    )
    # Never compacted (or deleted): no sidecar.
    assert compaction_count("conv-1") == 0

    (tmp_path / "compaction_meta.json").write_text('{"count": "x"}')
    assert compaction_count("conv-1") == 0
    (tmp_path / "compaction_meta.json").write_text("not json")
    assert compaction_count("conv-1") == 0


def test_compact_conversation_rejects_small_history(monkeypatch, tmp_path):
    provider = FakeAnthropicProvider()
    history = _anthropic_turn("hi") + _anthropic_turn("again")
    _install_compaction_fakes(monkeypatch, tmp_path, provider, history)

    with pytest.raises(CompactionError) as exc_info:
        _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))
    assert exc_info.value.code == "nothing_to_compact"


def test_compact_conversation_rejects_provider_mismatch(monkeypatch, tmp_path):
    provider = FakeAnthropicProvider()
    _install_compaction_fakes(
        monkeypatch, tmp_path, provider, _long_anthropic_history(),
    )
    # History on disk says anthropic; a Gemini model must be refused.
    with pytest.raises(CompactionError) as exc_info:
        _run(compact_conversation(1, "conv-1", "gemini-3.5-flash"))
    assert exc_info.value.code == "provider_mismatch"


def test_compact_conversation_rejects_suspended(monkeypatch, tmp_path):
    provider = FakeAnthropicProvider()
    provider.get_pending_tool_use_args_from_history = (
        lambda history: [("t9", "wait_for_handles", {})]
    )
    _install_compaction_fakes(
        monkeypatch, tmp_path, provider, _long_anthropic_history(),
    )
    with pytest.raises(CompactionError) as exc_info:
        _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))
    assert exc_info.value.code == "conversation_suspended"


def test_compact_conversation_missing_history(monkeypatch, tmp_path):
    provider = FakeAnthropicProvider()
    _install_compaction_fakes(monkeypatch, tmp_path, provider, [])
    (tmp_path / "sdk_history.json").unlink()
    with pytest.raises(CompactionError) as exc_info:
        _run(compact_conversation(1, "conv-1", "claude-opus-4-8"))
    assert exc_info.value.code == "no_history"


def test_estimate_compaction_cost_usd(monkeypatch, tmp_path):
    from chat.compaction import estimate_compaction_cost_usd
    from chat.storage import ChatStorage

    monkeypatch.setattr(
        ChatStorage, "_get_conversation_dir",
        staticmethod(lambda conversation_id: tmp_path),
    )
    with open(tmp_path / "sdk_history.json", "w") as f:
        json.dump({"provider": "anthropic",
                   "history": _long_anthropic_history()}, f)

    cost = estimate_compaction_cost_usd("conv-1", "claude-opus-4-8")
    assert cost is not None
    assert 0 < cost < 5  # serialized span is far smaller than raw history

    # Provider mismatch and tiny histories return None.
    assert estimate_compaction_cost_usd("conv-1", "gemini-3.5-flash") is None
    with open(tmp_path / "sdk_history.json", "w") as f:
        json.dump({"provider": "anthropic",
                   "history": _anthropic_turn("hi")}, f)
    assert estimate_compaction_cost_usd("conv-1", "claude-opus-4-8") is None
    (tmp_path / "sdk_history.json").unlink()
    assert estimate_compaction_cost_usd("conv-1", "claude-opus-4-8") is None


def test_build_summary_message_text_includes_appendix():
    text = build_summary_message_text("## Goal\nX", "<preserved-records>\n</preserved-records>")
    assert text.startswith("[CONTEXT SUMMARY]")
    assert text.endswith("[END CONTEXT SUMMARY]")
    assert "<preserved-records>" in text
    assert "## Goal" in text
