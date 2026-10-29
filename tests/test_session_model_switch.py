"""Tests for get_or_create_chat() model-switch logic in chat.gemini_api.session.

Regression tests ensuring that:
- Same-provider model switches preserve conversation history (Bug 2 fix: commit df521cf)
- Cross-provider switches use compatible disk history or start fresh
- Session store integrity is maintained across switches
- The doc notes seen record (doc_notes_seen.json, issue #187 spec 4.2) is
  deleted when a session starts without the conversation's history and
  kept when the history is carried over
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

import chat.docs.notes as notes_mod
import chat.storage as storage_mod
from chat.gemini_api.session import get_or_create_chat, _active_chats


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_active_chats():
    """Clear _active_chats before and after each test."""
    saved = dict(_active_chats)
    _active_chats.clear()
    yield
    _active_chats.clear()
    _active_chats.update(saved)


@pytest.fixture(autouse=True)
def chats_root(tmp_path, monkeypatch):
    """Point CHATS_DIR at a tmp dir: the cross-provider paths clear the
    doc notes seen record, which must never touch the real data dir."""
    root = tmp_path / "chats"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", root, raising=True)
    return root


@pytest.fixture()
def mock_provider():
    """Return a MagicMock provider with configurable session/history methods."""
    provider = MagicMock()
    # create_session returns a dict so we can inspect what was passed
    provider.create_session.side_effect = (
        lambda model, system_prompt, tools, history=None, thinking_effort=None: {
            "model": model,
            "history": history,
            "thinking_effort": thinking_effort,
        }
    )
    # Like Gemini: the level is baked in at creation, so a change rebuilds
    provider.set_thinking_effort.return_value = False
    # save_history returns a fake history list
    provider.save_history.return_value = [{"role": "user", "content": "hello"}]
    # load_history is an identity transform
    provider.load_history.side_effect = lambda data: data
    return provider


# We patch TOP_LEVEL_TOOLS so the lazy import inside get_or_create_chat()
# doesn't pull in heavy dependencies.
@pytest.fixture(autouse=True)
def _patch_top_level_tools():
    with patch("chat.llm.tool_schemas.TOP_LEVEL_TOOLS", []):
        yield


# ---------------------------------------------------------------------------
# Tests: same model returns cached session
# ---------------------------------------------------------------------------

class TestSameModelReturnsExistingSession:
    """When the model hasn't changed, the cached session is returned as-is."""

    def test_same_model_returns_cached_session(self, mock_provider):
        fake_session = {"model": "gemini-3.1-pro-preview", "history": None}
        _active_chats[(1, "conv-1")] = ("gemini-3.1-pro-preview", fake_session)

        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "gemini-3.1-pro-preview", "system prompt",
        )
        assert result is fake_session
        assert _active_chats[(1, "conv-1")] == ("gemini-3.1-pro-preview", fake_session)
        mock_provider.create_session.assert_not_called()


class TestThinkingEffortChange:
    """A new thinking level on the same model switches the session in
    place when the provider can, otherwise rebuilds it with its history."""

    class _Session:
        def __init__(self, model, thinking_effort):
            self.model = model
            self.thinking_effort = thinking_effort

    def test_in_place_switch_keeps_session(self, mock_provider):
        session = self._Session("claude-opus-5-5", None)
        _active_chats[(1, "conv-1")] = ("claude-opus-5-5", session)

        def _switch(sess, level):
            sess.thinking_effort = level
            return True

        mock_provider.set_thinking_effort.side_effect = _switch
        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-opus-5-5", "system prompt", thinking_effort="max",
        )
        assert result is session and session.thinking_effort == "max"
        mock_provider.create_session.assert_not_called()
        # Same level again: nothing to switch
        mock_provider.set_thinking_effort.reset_mock()
        get_or_create_chat(mock_provider, 1, "conv-1", "claude-opus-5-5", "system prompt", thinking_effort="max")
        mock_provider.set_thinking_effort.assert_not_called()

    def test_baked_in_level_rebuilds_from_history(self, mock_provider):
        session = self._Session("gemini-4-argon", "low")
        _active_chats[(1, "conv-1")] = ("gemini-4-argon", session)

        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "gemini-4-argon", "system prompt", thinking_effort="high",
        )
        # Rebuilt like a same-provider model switch: history carried over
        mock_provider.save_history.assert_called_once_with(session)
        assert result == {
            "model": "gemini-4-argon",
            "history": [{"role": "user", "content": "hello"}],
            "thinking_effort": "high",
        }
        assert _active_chats[(1, "conv-1")] == ("gemini-4-argon", result)


# ---------------------------------------------------------------------------
# Tests: same-provider model switch preserves history
# ---------------------------------------------------------------------------

class TestSameProviderModelSwitch:
    """Switching between models of the same provider should preserve history."""

    def test_anthropic_model_switch_preserves_history(self, mock_provider):
        old_session = {"model": "claude-haiku-4.5", "history": None}
        _active_chats[(1, "conv-1")] = ("claude-haiku-4.5", old_session)

        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "system prompt",
        )

        # A new session was created (not the old one)
        assert result is not old_session
        # save_history was called on the old session to extract history
        mock_provider.save_history.assert_called_once_with(old_session)
        # create_session received non-None history
        call_kwargs = mock_provider.create_session.call_args
        assert call_kwargs.kwargs.get("history") is not None or (
            call_kwargs.args[3] if len(call_kwargs.args) > 3 else call_kwargs.kwargs.get("history")
        ) is not None
        # The stored model is now the new model
        assert _active_chats[(1, "conv-1")][0] == "claude-sonnet-4-6"

    def test_gemini_model_switch_preserves_history(self, mock_provider):
        old_session = {"model": "gemini-3.1-pro-preview", "history": None}
        _active_chats[(1, "conv-1")] = ("gemini-3.1-pro-preview", old_session)

        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "gemini-3-flash-preview", "system prompt",
        )

        assert result is not old_session
        mock_provider.save_history.assert_called_once_with(old_session)
        assert _active_chats[(1, "conv-1")][0] == "gemini-3-flash-preview"

    def test_same_provider_switch_falls_back_to_disk_on_save_failure(self, mock_provider):
        old_session = {"model": "claude-haiku-4.5", "history": None}
        _active_chats[(1, "conv-1")] = ("claude-haiku-4.5", old_session)

        # save_history raises an exception
        mock_provider.save_history.side_effect = Exception("extraction failed")

        disk_history = [{"role": "user", "content": "disk hello"}]
        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "system prompt",
            disk_history=disk_history, disk_provider="anthropic",
        )

        assert result is not old_session
        # load_history should have been called with the disk history
        mock_provider.load_history.assert_called_once_with(disk_history)
        assert _active_chats[(1, "conv-1")][0] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Tests: cross-provider model switch
# ---------------------------------------------------------------------------

class TestCrossProviderModelSwitch:
    """Switching across providers uses compatible disk history or starts fresh."""

    def test_cross_provider_switch_uses_compatible_disk_history(self, mock_provider):
        old_session = {"model": "gemini-3.1-pro-preview", "history": None}
        _active_chats[(1, "conv-1")] = ("gemini-3.1-pro-preview", old_session)

        disk_history = [{"role": "assistant", "content": "anthropic data"}]
        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "system prompt",
            disk_history=disk_history, disk_provider="anthropic",
        )

        # New session created with disk history
        assert result is not old_session
        mock_provider.load_history.assert_called_once_with(disk_history)
        # Old session removed, new one stored
        assert _active_chats[(1, "conv-1")][0] == "claude-sonnet-4-6"

    def test_cross_provider_switch_no_compatible_disk_history(self, mock_provider):
        old_session = {"model": "gemini-3.1-pro-preview", "history": None}
        _active_chats[(1, "conv-1")] = ("gemini-3.1-pro-preview", old_session)

        # disk_provider is gemini but new model is anthropic -- incompatible
        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "system prompt",
            disk_history=[{"role": "model", "parts": []}], disk_provider="gemini",
        )

        assert result is not old_session
        # No history loaded -- provider.load_history should not have been called
        mock_provider.load_history.assert_not_called()
        # Session created with history=None
        create_call = mock_provider.create_session.call_args
        assert create_call.kwargs.get("history") is None
        assert _active_chats[(1, "conv-1")][0] == "claude-sonnet-4-6"

    def test_cross_provider_switch_no_disk_history_at_all(self, mock_provider):
        old_session = {"model": "gemini-3.1-pro-preview", "history": None}
        _active_chats[(1, "conv-1")] = ("gemini-3.1-pro-preview", old_session)

        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "system prompt",
        )

        assert result is not old_session
        mock_provider.load_history.assert_not_called()
        create_call = mock_provider.create_session.call_args
        assert create_call.kwargs.get("history") is None
        assert _active_chats[(1, "conv-1")][0] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Tests: new session creation (no prior cached session)
# ---------------------------------------------------------------------------

class TestNewSessionCreation:
    """When no cached session exists, a new one is created from scratch."""

    def test_new_session_with_history(self, mock_provider):
        history = [{"role": "model", "parts": []}]
        get_or_create_chat(
            mock_provider, 1, "conv-1", "gemini-3.1-pro-preview", "system prompt",
            history=history,
        )

        mock_provider.load_history.assert_called_once_with(history)
        mock_provider.create_session.assert_called_once()
        assert (1, "conv-1") in _active_chats
        assert _active_chats[(1, "conv-1")][0] == "gemini-3.1-pro-preview"

    def test_new_session_without_history(self, mock_provider):
        result = get_or_create_chat(
            mock_provider, 1, "conv-1", "gemini-3.1-pro-preview", "system prompt",
        )

        mock_provider.load_history.assert_not_called()
        create_call = mock_provider.create_session.call_args
        assert create_call.kwargs.get("history") is None
        assert _active_chats[(1, "conv-1")] == ("gemini-3.1-pro-preview", result)


# ---------------------------------------------------------------------------
# Tests: session store integrity
# ---------------------------------------------------------------------------

class TestSessionStoreIntegrity:
    """The _active_chats store stays consistent after model switches."""

    def test_model_switch_updates_stored_model(self, mock_provider):
        old_session = {"model": "claude-haiku-4.5", "history": None}
        _active_chats[(1, "conv-1")] = ("claude-haiku-4.5", old_session)

        new_session = get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "system prompt",
        )

        stored_model, stored_session = _active_chats[(1, "conv-1")]
        assert stored_model == "claude-sonnet-4-6"
        assert stored_session is new_session

    def test_different_conversations_independent(self, mock_provider):
        session_1 = {"model": "claude-haiku-4.5", "history": None}
        session_2 = {"model": "gemini-3.1-pro-preview", "history": None}
        _active_chats[(1, "conv-1")] = ("claude-haiku-4.5", session_1)
        _active_chats[(1, "conv-2")] = ("gemini-3.1-pro-preview", session_2)

        get_or_create_chat(
            mock_provider, 1, "conv-1", "claude-sonnet-4-6", "prompt",
        )

        # conv-2 must be untouched
        assert _active_chats[(1, "conv-2")] == ("gemini-3.1-pro-preview", session_2)


# ---------------------------------------------------------------------------
# Tests: doc notes seen record (issue #187 spec 4.2)
# ---------------------------------------------------------------------------

CONV = "conv-1"
DOC = "doc-1"
SEEN_FILE = "doc_notes_seen.json"
DOC_READS_FILE = "doc_reads.json"
DOC_READS_TEXT = json.dumps({"doc_ids": [DOC]})


class _Session:
    def __init__(self, model, thinking_effort=None):
        self.model = model
        self.thinking_effort = thinking_effort


def _seed_doc_notes(chats_root):
    """Give conversation CONV a doc notes seen record and a doc_reads.json."""
    conv_dir = chats_root / CONV
    conv_dir.mkdir(parents=True, exist_ok=True)
    # Other suites reload chat.storage (in place); look it up at call time.
    storage_mod.ChatStorage.record_doc_notes_seen(CONV, DOC, 3, 7)
    (conv_dir / DOC_READS_FILE).write_text(DOC_READS_TEXT)
    assert (conv_dir / SEEN_FILE).exists()
    return conv_dir


def _assert_notes_record(conv_dir, *, kept):
    seen = storage_mod.ChatStorage.get_doc_notes_seen(CONV, DOC)
    if kept:
        assert seen == notes_mod.NotesSeen(instructions=3, memories=7)
    else:
        assert not (conv_dir / SEEN_FILE).exists()
        assert seen is None
    # doc_reads.json is never touched.
    assert (conv_dir / DOC_READS_FILE).read_text() == DOC_READS_TEXT


class TestDocNotesSeenRecord:
    """The record goes when the new session lacks the conversation's
    history (the delivered notes blocks are gone from the model's
    context) and stays when the history is carried over."""

    # -- cleared --------------------------------------------------------

    def test_cross_provider_switch_without_compatible_disk_history_clears(
        self, mock_provider, chats_root,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("gemini-3.1-pro-preview", {"model": "gemini-3.1-pro-preview"})

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
            disk_history=[{"role": "model", "parts": []}], disk_provider="gemini",
        )

        assert result["history"] is None
        _assert_notes_record(conv_dir, kept=False)

    def test_cross_provider_switch_with_no_disk_history_clears(
        self, mock_provider, chats_root,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("gemini-3.1-pro-preview", {"model": "gemini-3.1-pro-preview"})

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
        )

        assert result["history"] is None
        _assert_notes_record(conv_dir, kept=False)

    def test_same_provider_save_failure_without_compatible_disk_copy_clears(
        self, mock_provider, chats_root,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("claude-haiku-4.5", {"model": "claude-haiku-4.5"})
        mock_provider.save_history.side_effect = Exception("extraction failed")

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
            disk_history=[{"role": "model", "parts": []}], disk_provider="gemini",
        )

        assert result["history"] is None
        _assert_notes_record(conv_dir, kept=False)

    def test_no_live_session_with_incompatible_disk_history_clears(
        self, mock_provider, chats_root,
    ):
        # After a restart or eviction the caller discards another
        # provider's disk history and passes history=None.
        conv_dir = _seed_doc_notes(chats_root)

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
            history=None,
            disk_history=[{"role": "model", "parts": []}], disk_provider="gemini",
        )

        assert result["history"] is None
        _assert_notes_record(conv_dir, kept=False)

    # -- kept -----------------------------------------------------------

    def test_cross_provider_switch_with_compatible_disk_history_keeps(
        self, mock_provider, chats_root,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("gemini-3.1-pro-preview", {"model": "gemini-3.1-pro-preview"})
        disk_history = [{"role": "assistant", "content": "anthropic data"}]

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
            disk_history=disk_history, disk_provider="anthropic",
        )

        assert result["history"] == disk_history
        _assert_notes_record(conv_dir, kept=True)

    def test_thinking_effort_rebuild_keeps(self, mock_provider, chats_root):
        conv_dir = _seed_doc_notes(chats_root)
        session = _Session("gemini-4-argon", "low")
        _active_chats[(1, CONV)] = ("gemini-4-argon", session)
        # mock_provider.set_thinking_effort returns False: rebuild via save_history

        result = get_or_create_chat(
            mock_provider, 1, CONV, "gemini-4-argon", "system prompt",
            thinking_effort="high",
        )

        mock_provider.save_history.assert_called_once_with(session)
        assert result["history"] == [{"role": "user", "content": "hello"}]
        _assert_notes_record(conv_dir, kept=True)

    def test_in_place_thinking_effort_switch_keeps(self, mock_provider, chats_root):
        conv_dir = _seed_doc_notes(chats_root)
        session = _Session("claude-opus-5-5")
        _active_chats[(1, CONV)] = ("claude-opus-5-5", session)
        mock_provider.set_thinking_effort.return_value = True

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-opus-5-5", "system prompt",
            thinking_effort="max",
        )

        assert result is session
        mock_provider.create_session.assert_not_called()
        _assert_notes_record(conv_dir, kept=True)

    def test_same_provider_model_switch_keeps(self, mock_provider, chats_root):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("claude-haiku-4.5", {"model": "claude-haiku-4.5"})

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
        )

        assert result["history"] == [{"role": "user", "content": "hello"}]
        _assert_notes_record(conv_dir, kept=True)

    def test_same_provider_save_failure_with_compatible_disk_copy_keeps(
        self, mock_provider, chats_root,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("claude-haiku-4.5", {"model": "claude-haiku-4.5"})
        mock_provider.save_history.side_effect = Exception("extraction failed")
        disk_history = [{"role": "user", "content": "disk hello"}]

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
            disk_history=disk_history, disk_provider="anthropic",
        )

        assert result["history"] == disk_history
        _assert_notes_record(conv_dir, kept=True)

    def test_no_live_session_seeded_from_compatible_history_keeps(
        self, mock_provider, chats_root,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        disk_history = [{"role": "assistant", "content": "anthropic data"}]

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
            history=disk_history,
            disk_history=disk_history, disk_provider="anthropic",
        )

        assert result["history"] == disk_history
        _assert_notes_record(conv_dir, kept=True)

    def test_session_without_any_history_clears(self, mock_provider, chats_root):
        # No live session and no disk history (e.g. the first save of the
        # history failed): the new session holds no notes block, so a
        # record left over could only be stale.
        conv_dir = _seed_doc_notes(chats_root)

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
        )

        assert result["history"] is None
        _assert_notes_record(conv_dir, kept=False)

    def test_empty_carried_over_history_clears(self, mock_provider, chats_root):
        # An empty list is no history either (a `history is None` check
        # would miss it).
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("claude-haiku-4.5", {"model": "claude-haiku-4.5"})
        mock_provider.save_history.return_value = []

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
        )

        assert result["history"] is None
        _assert_notes_record(conv_dir, kept=False)

    def test_clear_touches_only_this_conversation(self, mock_provider, chats_root):
        conv_dir = _seed_doc_notes(chats_root)
        other_dir = chats_root / "conv-2"
        other_dir.mkdir()
        storage_mod.ChatStorage.record_doc_notes_seen("conv-2", DOC, 3, 7)
        _active_chats[(1, CONV)] = ("gemini-3.1-pro-preview", {"model": "gemini-3.1-pro-preview"})

        get_or_create_chat(mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt")

        _assert_notes_record(conv_dir, kept=False)
        assert storage_mod.ChatStorage.get_doc_notes_seen("conv-2", DOC) == notes_mod.NotesSeen(3, 7)

    def test_first_session_of_a_new_conversation_creates_no_file_and_logs_nothing(
        self, mock_provider, chats_root, monkeypatch, caplog,
    ):
        conv_dir = chats_root / CONV
        conv_dir.mkdir(parents=True)
        # chat/logging_config.py turns propagation off for the "chat" logger.
        monkeypatch.setattr(logging.getLogger("chat"), "propagate", True)
        caplog.set_level(logging.INFO, logger="chat.gemini_api.session")

        get_or_create_chat(mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt")

        assert list(conv_dir.iterdir()) == []
        assert not [r for r in caplog.records if "doc notes seen" in r.getMessage()]

    # -- failure --------------------------------------------------------

    def test_failed_clear_is_logged_and_session_still_created(
        self, mock_provider, chats_root, monkeypatch, caplog,
    ):
        conv_dir = _seed_doc_notes(chats_root)
        _active_chats[(1, CONV)] = ("gemini-3.1-pro-preview", {"model": "gemini-3.1-pro-preview"})

        def _boom(conversation_id):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(
            storage_mod.ChatStorage, "clear_doc_notes_seen", staticmethod(_boom),
        )
        # chat/logging_config.py turns propagation off for the "chat" logger.
        monkeypatch.setattr(logging.getLogger("chat"), "propagate", True)
        caplog.set_level(logging.WARNING, logger="chat.gemini_api.session")

        result = get_or_create_chat(
            mock_provider, 1, CONV, "claude-sonnet-4-6", "system prompt",
        )

        assert result == {"model": "claude-sonnet-4-6", "history": None, "thinking_effort": None}
        assert _active_chats[(1, CONV)] == ("claude-sonnet-4-6", result)
        warnings = [
            r for r in caplog.records
            if r.name == "chat.gemini_api.session" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "doc notes seen" in warnings[0].getMessage()
        assert warnings[0].exc_info is not None
        # The clear never ran, so the record is still there.
        _assert_notes_record(conv_dir, kept=True)
