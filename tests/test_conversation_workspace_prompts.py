"""Conversation workspaces, phase 2: legacy detection and prompting (issue #70).

* ``_resolve_workspace_notice_flags`` (run at the start of every
  ``run_conversation_turn``): legacy detection runs once per project
  conversation, keyed on the ``own_workspace`` marker and an assistant
  message in the history -- never on the workspace dir's presence -- and
  never touches a standalone conversation;
* the three prompt variants (standard, sub-agent, public-project) carry the
  "Two file spaces" paragraph and the flag-conditioned legacy / converted
  notes in project conversations only, and a standalone conversation's
  prompt is byte-for-byte what it was before the change;
* the flags reach the prompt builders from ``run_conversation_turn`` and
  ``_run_sub_agent``;
* the ``system:workspace`` / ``system:routines`` catalog texts.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api import conversation as conv_mod
from chat.gemini_api import session as session_mod
from chat.gemini_api.system_prompt import (
    get_public_project_system_prompt,
    get_sub_agent_system_prompt,
    get_system_prompt,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TWO_SPACES = "**Two file spaces:**"
LEGACY_NOTE = (
    "This chat started before conversation workspaces existed: files it "
    "created earlier are in the project workspace. Use `list_project_files` / "
    "`get_project_file` to find them."
)
CONVERTED_NOTE = (
    "This chat was just turned into a project. Its existing files are in the "
    "chat workspace; the project workspace is empty. Use `copy_file_to_project` "
    "for files that should become shared project files, or do so when the "
    "user asks."
)
PROJECT_EXAMPLE = "shutil.copy('/workspace/out.pdf', '/project/out.pdf')"
SIX_TOOLS = (
    "list_project_files", "get_project_file", "write_project_file",
    "edit_project_file", "copy_file_to_project", "copy_project_file",
)
COPY_SENTENCE = (
    "In a project conversation, project files must be copied into this "
    "conversation's workspace first (`copy_project_file`)."
)


def _storage():
    # Other suites reload chat.storage (in place); look it up at call time.
    return importlib.import_module("chat.storage")


@pytest.fixture()
def chats_dir(tmp_path, monkeypatch):
    chats = tmp_path / "chats"
    monkeypatch.setattr(_storage(), "CHATS_DIR", chats, raising=True)
    monkeypatch.setattr(_storage(), "PROJECTS_DIR", tmp_path / "projects", raising=True)
    return chats


def _write_history(chats: Path, cid: str, data: dict) -> Path:
    (chats / cid).mkdir(parents=True, exist_ok=True)
    path = chats / cid / "chat_history.json"
    path.write_text(json.dumps(data))
    return path


def _history(path: Path) -> dict:
    return json.loads(path.read_text())


_USER_MSG = {"type": "text", "role": "user", "content": "hi"}
_ASSISTANT_MSG = {"type": "text", "role": "assistant", "content": "hello"}
_TOOL_USE_MSG = {
    "type": "tool_use", "role": "assistant", "tool_name": "write_workspace_file",
    "tool_input": {"path": "a.md"}, "tool_id": "t1",
}


# ---------------------------------------------------------------------------
# Legacy detection
# ---------------------------------------------------------------------------


class TestLegacyDetection:
    def test_standalone_never_touched(self, chats_dir):
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        before = path.read_text()
        assert conv_mod._resolve_workspace_notice_flags(1, "c1", None) == {}
        assert path.read_text() == before
        assert not (chats_dir / "c1" / "workspace").exists()

    def test_fresh_project_conversation_is_not_legacy(self, chats_dir):
        # create_project_conversation sets own_workspace at creation.
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "own_workspace": True,
            "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        before = path.read_text()
        flags = conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert flags == {"own_workspace": True}
        assert path.read_text() == before
        assert (chats_dir / "c1" / "workspace").is_dir()

    @pytest.mark.parametrize("assistant", [_ASSISTANT_MSG, _TOOL_USE_MSG])
    def test_legacy_flag_set_once(self, chats_dir, monkeypatch, assistant):
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG, assistant],
        })
        flags = conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert flags == {"legacy_shared_workspace": True, "own_workspace": True}
        data = _history(path)
        assert data["legacy_shared_workspace"] is True
        assert data["own_workspace"] is True
        assert data["messages"] == [_USER_MSG, assistant]
        assert (chats_dir / "c1" / "workspace").is_dir()

        # Second turn: own_workspace is set, so the history is not scanned
        # again and the flags stay stable.
        def _boom(_cid):
            raise AssertionError("history re-scanned")

        monkeypatch.setattr(
            conv_mod.ChatStorage, "get_conversation", staticmethod(_boom),
        )
        before = path.read_text()
        again = conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert again == {"legacy_shared_workspace": True, "own_workspace": True}
        assert path.read_text() == before

    def test_no_assistant_message_is_not_legacy(self, chats_dir):
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG],
        })
        flags = conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert flags == {"own_workspace": True}
        data = _history(path)
        assert "legacy_shared_workspace" not in data
        assert data["own_workspace"] is True

        # Later turns (now with an assistant reply) never re-detect.
        data["messages"].append(_ASSISTANT_MSG)
        path.write_text(json.dumps(data))
        assert conv_mod._resolve_workspace_notice_flags(1, "c1", "p1") == {
            "own_workspace": True,
        }

    def test_workspace_dir_presence_is_not_the_signal(self, chats_dir):
        # The dir is mkdir'd lazily by many paths; a legacy conversation may
        # well have one already.
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        (chats_dir / "c1" / "workspace").mkdir()
        flags = conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert flags["legacy_shared_workspace"] is True
        assert _history(path)["legacy_shared_workspace"] is True

    def test_converted_flag_returned(self, chats_dir):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "own_workspace": True,
            "converted_from_standalone": True,
            "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        flags = conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert flags == {"own_workspace": True, "converted_from_standalone": True}

    def test_new_detection_drops_cached_session(self, chats_dir, monkeypatch):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        monkeypatch.setitem(session_mod._active_chats, (1, "c1"), ("m", object()))
        monkeypatch.setitem(session_mod._active_chats, (1, "c2"), ("m", object()))
        conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert (1, "c1") not in session_mod._active_chats
        assert (1, "c2") in session_mod._active_chats

    def test_steady_state_keeps_cached_session(self, chats_dir, monkeypatch):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "own_workspace": True,
            "legacy_shared_workspace": True, "messages": [_ASSISTANT_MSG],
        })
        monkeypatch.setitem(session_mod._active_chats, (1, "c1"), ("m", object()))
        conv_mod._resolve_workspace_notice_flags(1, "c1", "p1")
        assert (1, "c1") in session_mod._active_chats

    def test_missing_history_is_harmless(self, chats_dir):
        assert conv_mod._resolve_workspace_notice_flags(1, "c1", "p1") == {
            "own_workspace": True,
        }
        assert not (chats_dir / "c1" / "chat_history.json").exists()

    def test_failure_never_raises(self, chats_dir, monkeypatch):
        def _boom(_cid):
            raise OSError("disk on fire")

        monkeypatch.setattr(
            conv_mod.ChatStorage, "get_conversation_flags", staticmethod(_boom),
        )
        assert conv_mod._resolve_workspace_notice_flags(1, "c1", "p1") == {}


# ---------------------------------------------------------------------------
# Prompt variants
# ---------------------------------------------------------------------------


def _standard(**kw):
    return get_system_prompt("KEY", base_url="http://x", user_email="u@e", **kw)


def _sub(**kw):
    return get_sub_agent_system_prompt("A", "KEY", base_url="http://x", user_email="u@e", **kw)


def _public(**kw):
    return get_public_project_system_prompt(user_email="u@e", **kw)


_NOTE_FLAGS = [
    (False, False), (True, False), (False, True), (True, True),
]


class TestPrompts:
    @pytest.mark.parametrize("build", [_standard, _sub], ids=["standard", "sub_agent"])
    def test_two_spaces_only_in_project_conversations(self, build):
        project = build(has_project=True)
        standalone = build(has_project=False)
        assert TWO_SPACES in project
        assert "`/project` in scripts" in project
        assert TWO_SPACES not in standalone
        assert "/project" not in standalone

    def test_public_prompt_has_two_spaces_and_mounts(self):
        prompt = _public()
        assert TWO_SPACES in prompt
        assert "mounted read-write at `/workspace` and the project's shared workspace at `/project`" in prompt
        assert "The project workspace is mounted read-write at `/workspace`" not in prompt
        assert "uploads to this conversation or to the project workspace are fair game" in prompt
        assert PROJECT_EXAMPLE in prompt

    def test_standard_project_example(self):
        assert PROJECT_EXAMPLE in _standard(has_project=True)
        assert "To promote a finished file" not in _standard(has_project=False)

    @pytest.mark.parametrize("build,project_kw", [
        (_standard, {"has_project": True}),
        (_sub, {"has_project": True}),
        (_public, {}),
    ], ids=["standard", "sub_agent", "public"])
    @pytest.mark.parametrize("legacy,converted", _NOTE_FLAGS)
    def test_notes_follow_flags(self, build, project_kw, legacy, converted):
        prompt = build(
            legacy_shared_workspace=legacy,
            converted_from_standalone=converted,
            **project_kw,
        )
        assert (LEGACY_NOTE in prompt) is legacy
        assert (CONVERTED_NOTE in prompt) is converted
        assert TWO_SPACES in prompt

    @pytest.mark.parametrize("build", [_standard, _sub], ids=["standard", "sub_agent"])
    @pytest.mark.parametrize("legacy,converted", _NOTE_FLAGS)
    def test_standalone_ignores_flags(self, build, legacy, converted):
        assert build(
            has_project=False,
            legacy_shared_workspace=legacy,
            converted_from_standalone=converted,
        ) == build(has_project=False)

    def test_no_shared_workspace_claims(self):
        for prompt in (_standard(has_project=True), _sub(has_project=True), _public()):
            assert "shared project workspace" not in prompt.lower()
            assert "project workspace is mounted read-write at `/workspace`" not in prompt


# ---------------------------------------------------------------------------
# Invariant 2: standalone prompts are byte-for-byte what they were
# ---------------------------------------------------------------------------

# The last commit before the two-file-spaces prompt text (its tool-tier
# exclusion already uses PROJECT_ONLY_TOOL_CALL_TOOLS, so its standalone
# prompts are the reference). The test skips when the object is missing
# (shallow clone, squash-merged history).
_BASELINE_COMMIT = "a222348"


def _load_baseline_module(tmp_path):
    try:
        source = subprocess.run(
            ["git", "show", f"{_BASELINE_COMMIT}:chat/gemini_api/system_prompt.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"baseline commit {_BASELINE_COMMIT} not available")
    path = tmp_path / "baseline_system_prompt.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("baseline_system_prompt", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - drift guard
        pytest.skip(f"baseline module no longer importable: {exc}")
    return module


def test_standalone_prompts_match_baseline(tmp_path):
    from chat.gemini_api import system_prompt as current

    baseline = _load_baseline_module(tmp_path)
    for cs in (None, {}, {"google": True, "slack": True, "docs": True}):
        for slack in (False, True):
            for routine in (False, True):
                for nested in (False, True):
                    kw = dict(
                        base_url="http://x", custom_system_prompt="cp",
                        connected_services=cs, user_name="U", user_email="u@e",
                        skills_content="sk", has_project=False, is_slack=slack,
                        nested_subagents=nested, is_routine=routine,
                    )
                    assert current.get_system_prompt("KEY", **kw) == \
                        baseline.get_system_prompt("KEY", **kw), kw
        for can_nest in (False, True):
            kw = dict(
                base_url="http://x", connected_services=cs, user_email="u@e",
                has_project=False, can_nest=can_nest,
            )
            assert current.get_sub_agent_system_prompt("A", "KEY", **kw) == \
                baseline.get_sub_agent_system_prompt("A", "KEY", **kw), kw
    assert current.get_user_subagent_system_prompt(
        "KEY", target_user_email="t@e", caller_email="c@e",
    ) == baseline.get_user_subagent_system_prompt(
        "KEY", target_user_email="t@e", caller_email="c@e",
    )
    assert current.get_inference_api_system_prompt("KEY", user_email="u@e") == \
        baseline.get_inference_api_system_prompt("KEY", user_email="u@e")


# ---------------------------------------------------------------------------
# Threading the flags into the builders
# ---------------------------------------------------------------------------


def _fake_provider():
    from chat.llm.base import UsageStats

    provider = MagicMock()
    provider.get_pending_tool_use_args = MagicMock(return_value=[])
    provider.repair_session_history = MagicMock(return_value=0)
    provider.get_usage = MagicMock(
        return_value=UsageStats(input_tokens=1, output_tokens=1, cached_tokens=0),
    )
    provider.create_session.return_value = MagicMock()

    async def _stream(_chat, _message):
        if False:
            yield None  # pragma: no cover

    provider.send_message_stream = _stream
    return provider


@pytest.mark.parametrize("public", [False, True])
def test_run_conversation_turn_passes_flags(chats_dir, monkeypatch, public):
    path = _write_history(chats_dir, "c1", {
        "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
    })
    captured: dict = {}

    def _capture(*_a, **kw):
        captured.update(kw)
        return "system prompt"

    monkeypatch.setattr(conv_mod, "load_server_config", lambda: {"gemini": {"model": "fake"}})
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_system_prompt", _capture)
    import chat.gemini_api.system_prompt as sp_mod
    monkeypatch.setattr(sp_mod, "get_public_project_system_prompt", _capture)
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
    monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: object())
    monkeypatch.setattr(conv_mod, "record_api_call", AsyncMock())
    provider = _fake_provider()
    monkeypatch.setattr(conv_mod, "get_provider_instance", lambda _n, _i=None: provider)
    import config.model_selection as model_selection
    monkeypatch.setattr(model_selection, "is_model_allowed", lambda *a, **kw: True)

    import db.conversation_store as conv_store
    monkeypatch.setattr(conv_store, "update_conversation_model", AsyncMock())
    import db.project_store as project_store
    monkeypatch.setattr(
        project_store, "get_project",
        AsyncMock(return_value={"id": "p1", "guide": "", "public": public}),
    )
    import db.skill_store as skill_store
    monkeypatch.setattr(skill_store, "get_user_autoloaded_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr(skill_store, "get_project_autoloaded_skills", AsyncMock(return_value=[]))
    import api.instructions as instructions
    monkeypatch.setattr(instructions, "get_user_connected_services", lambda _u: {})

    async def _on_event(_e):
        return None

    asyncio.run(conv_mod.run_conversation_turn(
        app=None, user={"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"},
        message="again", conversation_id="c1", timezone="UTC", model="fake",
        on_event=_on_event, project_id="p1",
    ))
    assert captured["legacy_shared_workspace"] is True
    assert captured["converted_from_standalone"] is False
    assert _history(path)["legacy_shared_workspace"] is True


@pytest.mark.parametrize("project_id", [None, "p1"])
def test_run_sub_agent_passes_parent_flags(chats_dir, project_id):
    from chat.gemini_api.sub_agent import _run_sub_agent

    _write_history(chats_dir, "c1", {
        "id": "c1", "project_id": "p1", "own_workspace": True,
        "legacy_shared_workspace": True, "converted_from_standalone": True,
        "messages": [_ASSISTANT_MSG],
    })
    provider = _fake_provider()
    usage = MagicMock()
    usage.input_tokens = usage.output_tokens = usage.cached_tokens = 0
    usage.cache_creation_tokens = usage.cache_read_tokens = 0
    provider.get_usage.return_value = usage
    user = {"id": 1, "email": "t@example.com", "name": "T", "api_key": "k", "settings": {}}
    with patch(
        "chat.gemini_api.sub_agent.record_api_call", new=AsyncMock()
    ), patch(
        "api.instructions.get_user_connected_services", return_value={}
    ), patch(
        "chat.gemini_api.sub_agent.compute_new_input_tokens", return_value=0
    ), patch(
        "chat.gemini_api.sub_agent.compute_total_context_tokens", return_value=0
    ):
        asyncio.run(_run_sub_agent(
            app=MagicMock(), provider=provider, user=user,
            conversation_id="c1", timezone="UTC",
            model="claude-haiku-4.5", agent_name="A", prompt="go",
            project_id=project_id,
        ))
    prompt = provider.create_session.call_args.kwargs["system_prompt"]
    assert (LEGACY_NOTE in prompt) is (project_id is not None)
    assert (CONVERTED_NOTE in prompt) is (project_id is not None)
    assert (TWO_SPACES in prompt) is (project_id is not None)


def test_attached_filenames_sentence():
    wrapped = conv_mod._wrap_message_with_metadata(
        "hi", "UTC", attached_filenames=["a.pdf"],
    )
    assert (
        "Files attached to this message (in this chat's workspace, readable "
        "via your file tools): a.pdf"
    ) in wrapped


# ---------------------------------------------------------------------------
# System skill catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def _content(self, skill_id):
        from chat.system_skills import catalog

        builder = {
            "system:workspace": catalog._workspace_content,
            "system:routines": catalog._routines_content,
            "system:action_requests": catalog._action_requests_content,
            "system:user_subagents": catalog._user_subagents_content,
            "system:quest_docs": catalog._quest_docs_content,
        }[skill_id]
        return builder("http://x", "KEY")

    def test_workspace_lists_two_spaces_and_six_tools(self):
        text = self._content("system:workspace")
        assert "Standalone conversations have one file space" in text
        assert "Project conversations have two" in text
        assert "per-conversation workspace directory mounted" not in text
        for name in SIX_TOOLS:
            assert f"**{name}(" in text, name
        assert PROJECT_EXAMPLE in text
        assert 'subprocess.run(["python3", "/project/etl.py"]' in text
        assert COPY_SENTENCE in text
        assert ".temp/" in text

    def test_routines_paragraph(self):
        text = self._content("system:routines")
        assert "every routine run is a fresh\nconversation" in text
        assert "`get_project_file` / `write_project_file`" in text
        assert "/project/" in text

    def test_copy_first_in_attachment_texts(self):
        assert COPY_SENTENCE in self._content("system:action_requests")
        docs = " ".join(self._content("system:quest_docs").split())
        assert COPY_SENTENCE.replace("In a project", "in a project")[:-1] in docs

    def test_subagent_responses_in_conversation_space(self):
        text = " ".join(self._content("system:user_subagents").split())
        assert "under `.subagent_responses/` (the conversation workspace, never the project workspace" in text

    def test_no_shared_workspace_claims(self):
        for sid in ("system:workspace", "system:routines", "system:action_requests"):
            text = self._content(sid).lower()
            assert "shared project workspace" not in text
            assert "this is the shared project workspace" not in text


def test_catalog_descriptions_unchanged_in_enumeration():
    # The enumeration feeds every prompt (standalone included): the
    # description strings must not pick up project text.
    from chat.system_skills import build_system_skills_enumeration

    text = build_system_skills_enumeration(None, False)
    assert "/project" not in text
    assert "project_file" not in text
