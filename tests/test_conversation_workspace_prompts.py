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
import json
import os
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

TWO_SPACES = "**Two file spaces:**"
LEGACY_NOTE = (
    "This chat started before conversation workspaces existed: files it "
    "created earlier may be in the project workspace. Use `list_project_files` "
    "/ `get_project_file` to find them."
)
CONVERTED_NOTE = (
    "This chat was converted from a standalone chat into this project. Files "
    "it created before the conversion are in this chat's workspace, not the "
    "project workspace; use `copy_file_to_project` for any that should become "
    "shared project files, or when the user asks."
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


def _detect(cid: str, project_id):
    """One turn's detection: read chat_history.json once, then decide."""
    try:
        data = _storage().ChatStorage.get_conversation(cid)
    except Exception:
        data = None
    return conv_mod._resolve_workspace_notice_flags(1, cid, project_id, data)


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
        assert _detect("c1", None) == {}
        assert path.read_text() == before
        assert not (chats_dir / "c1" / "workspace").exists()

    def test_fresh_project_conversation_is_not_legacy(self, chats_dir):
        # create_project_conversation sets own_workspace at creation.
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "own_workspace": True,
            "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        before = path.read_text()
        flags = _detect("c1", "p1")
        assert flags == {"own_workspace": True}
        assert path.read_text() == before
        assert (chats_dir / "c1" / "workspace").is_dir()

    @pytest.mark.parametrize("assistant", [_ASSISTANT_MSG, _TOOL_USE_MSG])
    def test_legacy_flag_set_once(self, chats_dir, monkeypatch, assistant):
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG, assistant],
        })
        flags = _detect("c1", "p1")
        assert flags == {"legacy_shared_workspace": True, "own_workspace": True}
        data = _history(path)
        assert data["legacy_shared_workspace"] is True
        assert data["own_workspace"] is True
        assert data["messages"] == [_USER_MSG, assistant]
        assert (chats_dir / "c1" / "workspace").is_dir()

        # Second turn: own_workspace is set, so nothing is decided or
        # written again and the flags stay stable.
        def _boom(*_a, **_kw):
            raise AssertionError("flags rewritten")

        monkeypatch.setattr(
            conv_mod.ChatStorage, "set_conversation_flags", staticmethod(_boom),
        )
        before = path.read_text()
        again = _detect("c1", "p1")
        assert again == {"legacy_shared_workspace": True, "own_workspace": True}
        assert path.read_text() == before

    def test_no_assistant_message_is_not_legacy(self, chats_dir):
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG],
        })
        flags = _detect("c1", "p1")
        assert flags == {"own_workspace": True}
        data = _history(path)
        assert "legacy_shared_workspace" not in data
        assert data["own_workspace"] is True

        # Later turns (now with an assistant reply) never re-detect.
        data["messages"].append(_ASSISTANT_MSG)
        path.write_text(json.dumps(data))
        assert _detect("c1", "p1") == {
            "own_workspace": True,
        }

    def test_workspace_dir_presence_is_not_the_signal(self, chats_dir):
        # The dir is mkdir'd lazily by many paths; a legacy conversation may
        # well have one already.
        path = _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        (chats_dir / "c1" / "workspace").mkdir()
        flags = _detect("c1", "p1")
        assert flags["legacy_shared_workspace"] is True
        assert _history(path)["legacy_shared_workspace"] is True

    def test_converted_flag_returned(self, chats_dir):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "own_workspace": True,
            "converted_from_standalone": True,
            "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        flags = _detect("c1", "p1")
        assert flags == {"own_workspace": True, "converted_from_standalone": True}

    def test_new_detection_drops_cached_session(self, chats_dir, monkeypatch):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
        })
        monkeypatch.setitem(session_mod._active_chats, (1, "c1"), ("m", object()))
        monkeypatch.setitem(session_mod._active_chats, (1, "c2"), ("m", object()))
        _detect("c1", "p1")
        assert (1, "c1") not in session_mod._active_chats
        assert (1, "c2") in session_mod._active_chats

    def test_steady_state_keeps_cached_session(self, chats_dir, monkeypatch):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "own_workspace": True,
            "legacy_shared_workspace": True, "messages": [_ASSISTANT_MSG],
        })
        monkeypatch.setitem(session_mod._active_chats, (1, "c1"), ("m", object()))
        _detect("c1", "p1")
        assert (1, "c1") in session_mod._active_chats

    def test_missing_history_is_harmless(self, chats_dir):
        assert _detect("c1", "p1") == {
            "own_workspace": True,
        }
        assert not (chats_dir / "c1" / "chat_history.json").exists()

    def test_one_rewrite_on_detection(self, chats_dir, monkeypatch):
        _write_history(chats_dir, "c1", {
            "id": "c1", "project_id": "p1", "messages": [_ASSISTANT_MSG],
        })
        real = conv_mod.ChatStorage.set_conversation_flags
        calls = []

        def _spy(cid, flags):
            calls.append(dict(flags))
            real(cid, flags)

        monkeypatch.setattr(
            conv_mod.ChatStorage, "set_conversation_flags", staticmethod(_spy),
        )
        _detect("c1", "p1")
        assert calls == [{"own_workspace": True, "legacy_shared_workspace": True}]

    def test_failure_never_raises(self, chats_dir, monkeypatch):
        def _boom(_data):
            raise OSError("disk on fire")

        monkeypatch.setattr(
            conv_mod.ChatStorage, "conversation_flags_from", staticmethod(_boom),
        )
        assert _detect("c1", "p1") == {}


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
# Invariant 2: standalone prompts carry no project-workspace text
# ---------------------------------------------------------------------------

_PROJECT_MARKERS = (
    TWO_SPACES, LEGACY_NOTE, CONVERTED_NOTE, "**Earlier files:**",
    "**Converted chat:**", "/project", "_project_file", "copy_file_to_project",
)


def _without_allowed_mentions(prompt: str) -> str:
    """Drop the text a standalone prompt may legitimately carry.

    The run_script / run_python tool lines, and the copy-first sentence on
    conversation-path parameters (e.g. ``add_doc_image``), which tells the
    model what to do *if* it is in a project conversation.
    """
    lines = [
        line for line in prompt.splitlines()
        if not line.lstrip().startswith(("9. **run_script", "10. **run_python",
                                         "5. **run_script", "6. **run_python"))
    ]
    return "\n".join(lines).replace(COPY_SENTENCE, "")


@pytest.mark.parametrize("connected_services", [None, {}, {"google": True, "docs": True}])
@pytest.mark.parametrize("legacy,converted", _NOTE_FLAGS)
def test_standalone_prompts_have_no_project_text(connected_services, legacy, converted):
    from chat.gemini_api.system_prompt import (
        get_inference_api_system_prompt,
        get_user_subagent_system_prompt,
    )

    flags = dict(legacy_shared_workspace=legacy, converted_from_standalone=converted)
    prompts = {
        "standard": get_system_prompt(
            "KEY", connected_services=connected_services, has_project=False, **flags,
        ),
        "routine": get_system_prompt(
            "KEY", connected_services=connected_services, has_project=False,
            is_routine=True, **flags,
        ),
        "sub_agent": get_sub_agent_system_prompt(
            "A", "KEY", connected_services=connected_services, has_project=False,
            can_nest=True, **flags,
        ),
        "user_subagent": get_user_subagent_system_prompt(
            "KEY", connected_services=connected_services,
        ),
        "inference": get_inference_api_system_prompt(
            "KEY", connected_services=connected_services,
        ),
    }
    for kind, prompt in prompts.items():
        text = _without_allowed_mentions(prompt)
        for marker in _PROJECT_MARKERS:
            assert marker not in text, (kind, marker)


# ---------------------------------------------------------------------------
# Golden snapshots (tests/snapshots/system_prompt_*.txt)
#
# Regenerate after an intended prompt change with
#   QUEST_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_conversation_workspace_prompts.py
# and review the diff. Plugin-registered dynamic tools are removed from the
# registry while rendering (their presence depends on whether some earlier
# test loaded the plugins), and connected_services is pinned to {} so no
# plugin skill, connector doc or service-gated tool is shown.
# ---------------------------------------------------------------------------

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

_SNAP_COMMON = dict(
    user_name="Snap User", user_email="snap@example.com",
    project_guide="Snapshot project instructions.",
)
_SNAP_FLAGS = dict(legacy_shared_workspace=True, converted_from_standalone=True)


def _snapshot_prompts() -> dict[str, str]:
    from chat.gemini_api.system_prompt import get_public_project_system_prompt as pub

    base = dict(base_url="http://quest.test", connected_services={}, **_SNAP_COMMON)
    return {
        "standalone_standard": get_system_prompt("SNAPSHOT-KEY", **base),
        "standalone_sub_agent": get_sub_agent_system_prompt(
            "Snap Agent", "SNAPSHOT-KEY", **base,
        ),
        "project_standard": get_system_prompt(
            "SNAPSHOT-KEY", has_project=True, **base, **_SNAP_FLAGS,
        ),
        "project_sub_agent": get_sub_agent_system_prompt(
            "Snap Agent", "SNAPSHOT-KEY", has_project=True, **base, **_SNAP_FLAGS,
        ),
        "public_project": pub(**_SNAP_COMMON, **_SNAP_FLAGS),
    }


def _render_snapshots() -> dict[str, str]:
    from chat.llm.tool_schemas import PLUGIN_TOOL_NAMES, TOOL_CALL_REGISTRY

    core = {
        name: spec for name, spec in TOOL_CALL_REGISTRY.items()
        if name not in PLUGIN_TOOL_NAMES
    }
    with patch.dict(TOOL_CALL_REGISTRY, core, clear=True):
        return _snapshot_prompts()


@pytest.mark.parametrize("name", [
    "standalone_standard", "standalone_sub_agent", "project_standard",
    "project_sub_agent", "public_project",
])
def test_prompt_snapshot(name):
    rendered = _render_snapshots()[name]
    path = SNAPSHOT_DIR / f"system_prompt_{name}.txt"
    if os.environ.get("QUEST_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        path.write_text(rendered)
    assert path.exists(), (
        f"missing snapshot {path.name}; run with QUEST_UPDATE_SNAPSHOTS=1"
    )
    assert rendered == path.read_text(), (
        f"system prompt '{name}' differs from {path.name}; if the change is "
        "intended, regenerate with QUEST_UPDATE_SNAPSHOTS=1 and review the diff"
    )


def test_snapshots_capture_the_project_text():
    project = (SNAPSHOT_DIR / "system_prompt_project_standard.txt").read_text()
    standalone = (SNAPSHOT_DIR / "system_prompt_standalone_standard.txt").read_text()
    assert TWO_SPACES in project and LEGACY_NOTE in project and CONVERTED_NOTE in project
    assert TWO_SPACES not in standalone


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


_TURN_USER = {"id": 1, "email": "t@example.com", "api_key": "k", "name": "T"}


def _patch_turn_collaborators(monkeypatch, *, public: bool) -> None:
    """Stub run_conversation_turn's DB / config / provider collaborators."""
    monkeypatch.setattr(conv_mod, "load_server_config", lambda: {"gemini": {"model": "fake"}})
    monkeypatch.setattr(conv_mod, "get_provider_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "get_backend_for_model", lambda _m: "gemini")
    monkeypatch.setattr(conv_mod, "_load_sdk_history", lambda _cid: None)
    monkeypatch.setattr(conv_mod, "_save_sdk_history", lambda *a: None)
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


def _run_turn(conversation_id="c1", project_id="p1"):
    async def _on_event(_e):
        return None

    asyncio.run(conv_mod.run_conversation_turn(
        app=None, user=_TURN_USER, message="again",
        conversation_id=conversation_id, timezone="UTC", model="fake",
        on_event=_on_event, project_id=project_id,
    ))


@pytest.mark.parametrize("public", [False, True])
def test_run_conversation_turn_passes_flags(chats_dir, monkeypatch, public):
    path = _write_history(chats_dir, "c1", {
        "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
    })
    captured: dict = {}

    def _capture(*_a, **kw):
        captured.update(kw)
        return "system prompt"

    _patch_turn_collaborators(monkeypatch, public=public)
    monkeypatch.setattr(conv_mod, "get_system_prompt", _capture)
    import chat.gemini_api.system_prompt as sp_mod
    monkeypatch.setattr(sp_mod, "get_public_project_system_prompt", _capture)
    monkeypatch.setattr(conv_mod, "get_or_create_chat", lambda *a, **kw: object())

    _run_turn()
    assert captured["legacy_shared_workspace"] is True
    assert captured["converted_from_standalone"] is False
    assert _history(path)["legacy_shared_workspace"] is True


def test_detection_turn_rebuilds_stale_session(chats_dir, monkeypatch):
    """A cached session from before detection is dropped before
    get_or_create_chat, which then builds one with the legacy note."""
    _write_history(chats_dir, "c1", {
        "id": "c1", "project_id": "p1", "messages": [_USER_MSG, _ASSISTANT_MSG],
    })
    _patch_turn_collaborators(monkeypatch, public=False)
    key = (_TURN_USER["id"], "c1")
    monkeypatch.setitem(session_mod._active_chats, key, ("fake", object()))
    seen: dict = {}

    def _get_or_create_chat(_provider, user_id, conversation_id, _model, system_prompt, **_kw):
        seen["stale_present"] = (user_id, conversation_id) in session_mod._active_chats
        seen["system_prompt"] = system_prompt
        return object()

    monkeypatch.setattr(conv_mod, "get_or_create_chat", _get_or_create_chat)
    reads = []
    real_read = conv_mod.ChatStorage.get_conversation

    def _counting_read(cid):
        reads.append(cid)
        return real_read(cid)

    monkeypatch.setattr(
        conv_mod.ChatStorage, "get_conversation", staticmethod(_counting_read),
    )

    _run_turn()
    assert seen["stale_present"] is False
    assert LEGACY_NOTE in seen["system_prompt"]
    assert TWO_SPACES in seen["system_prompt"]
    # chat_history.json was parsed once for the guide snapshot + the flags.
    assert reads == ["c1"]


@pytest.mark.parametrize("project_id", [None, "p1"])
def test_run_sub_agent_passes_parent_flags(chats_dir, monkeypatch, project_id):
    from chat.gemini_api.sub_agent import _run_sub_agent

    # The flags arrive from the parent; the sub-agent never re-reads them.
    def _no_read(*_a, **_kw):
        raise AssertionError("sub-agent re-read chat_history.json")

    monkeypatch.setattr(conv_mod.ChatStorage, "get_conversation", staticmethod(_no_read))
    monkeypatch.setattr(conv_mod.ChatStorage, "get_conversation_flags", staticmethod(_no_read))
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
            workspace_notice_flags={
                "own_workspace": True, "legacy_shared_workspace": True,
                "converted_from_standalone": True,
            },
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
        assert COPY_SENTENCE in " ".join(text.split())
        assert ".temp/" in text

    def test_routines_paragraph(self):
        text = self._content("system:routines")
        assert "every routine run is a fresh\nconversation" in text
        assert "`get_project_file` / `write_project_file`" in text
        assert "/project/" in text

    def test_copy_first_in_attachment_texts(self):
        assert COPY_SENTENCE in " ".join(self._content("system:action_requests").split())
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
