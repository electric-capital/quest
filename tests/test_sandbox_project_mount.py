"""Sandbox mounts for project conversations (spec 00009 section 5).

Every conversation's container mounts its conversation workspace at
``/workspace`` (``:Z``); project conversations additionally mount the
project workspace at ``/project`` with the shared ``:z`` relabel, in both
the restricted and the public profile. After the container exits the
handlers publish ``file_list_changed`` for the conversation scope, plus the
project scope in project conversations. Standalone argv stays exactly as it
was before the project mount existed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import chat.gemini_api.tool_handlers.sandbox as sandbox_mod
from chat.gemini_api.tool_handlers import _build_script_podman_cmd


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _pinned_env(monkeypatch):
    """Make the argv deterministic: fixed runtime, seccomp path, uid/gid."""
    monkeypatch.setattr(sandbox_mod, "get_sandbox_runtime", lambda: "crun")
    monkeypatch.setattr(
        sandbox_mod, "get_sandbox_seccomp_profile_path", lambda: "/seccomp.json",
    )
    monkeypatch.setattr(sandbox_mod.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox_mod.os, "getgid", lambda: 1000)
    monkeypatch.setattr(sandbox_mod, "get_sandbox_port", lambda: 9301)
    monkeypatch.setattr(sandbox_mod, "get_script_runner_image", lambda: "img-r")
    monkeypatch.setattr(sandbox_mod, "get_public_script_runner_image", lambda: "img-p")


def _mounts(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]


# The restricted standalone argv as it was before the project mount was
# added -- pinned literally so any drift in the single-mount case fails.
_STANDALONE_RESTRICTED = [
    "podman", "--runtime", "crun", "run", "--rm",
    "--network=slirp4netns:allow_host_loopback=true,outbound_addr=127.0.0.1"
    ",enable_ipv6=false",
    "--sysctl=net.ipv6.conf.all.disable_ipv6=1",
    "--dns=none",
    "--userns=keep-id",
    "--user=0:0",
    "--cap-add=NET_ADMIN",
    "--cap-add=SETPCAP",
    "--security-opt=seccomp=/seccomp.json",
    "-v", "/ws:/workspace:Z",
    "-w", "/workspace",
    "--memory=512m",
    "--cpus=1",
    "-e", "HOME=/tmp",
    "-e", "QUEST_API_KEY=tok",
    "-e", "QUEST_PORT=9301",
    "-e", "QUEST_RUN_UID=1000",
    "-e", "QUEST_RUN_GID=1000",
    "img-r",
    "python3", "x.py",
]


class TestPodmanArgvMounts:
    def test_standalone_restricted_argv_unchanged(self, _pinned_env):
        cmd = _build_script_podman_cmd("/ws", ["python3", "x.py"], "tok")
        assert cmd == _STANDALONE_RESTRICTED
        assert _build_script_podman_cmd(
            "/ws", ["python3", "x.py"], "tok", project_dir=None,
        ) == cmd

    @pytest.mark.parametrize("public", [False, True])
    def test_standalone_has_only_the_workspace_mount(self, _pinned_env, public):
        cmd = _build_script_podman_cmd("/ws", ["bash"], "tok", public=public)
        assert _mounts(cmd) == ["/ws:/workspace:Z"]
        assert not any("/project" in a for a in cmd)

    @pytest.mark.parametrize("public", [False, True])
    def test_project_conversation_adds_shared_project_mount(self, _pinned_env, public):
        base = _build_script_podman_cmd("/ws", ["bash"], "tok", public=public)
        cmd = _build_script_podman_cmd(
            "/ws", ["bash"], "tok", public=public, project_dir="/proj",
        )
        assert _mounts(cmd) == ["/ws:/workspace:Z", "/proj:/project:z"]
        # Working directory stays the conversation workspace.
        assert cmd[cmd.index("-w") + 1] == "/workspace"
        # The only difference from the standalone argv is the extra mount,
        # placed right after the workspace mount.
        i = cmd.index("/proj:/project:z")
        assert cmd[:i - 1] + cmd[i + 1:] == base

    def test_public_profile_keeps_withholding_credentials(self, _pinned_env):
        cmd = _build_script_podman_cmd(
            "/ws", ["bash"], "tok", public=True, project_dir="/proj",
        )
        assert not any(a.startswith(("QUEST_API_KEY=", "QUEST_PORT=")) for a in cmd)
        assert "img-p" in cmd


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self, input=None):
        return b"ok\n", self._stderr

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture()
def stub(monkeypatch, tmp_path):
    """Stub the workspace resolvers, event publisher and podman spawn."""
    conv_dir = tmp_path / "conv"
    proj_dir = tmp_path / "proj"
    conv_dir.mkdir()
    proj_dir.mkdir()
    state = {
        "argv": None, "events": [], "project_calls": [],
        "process": _FakeProcess(), "spawn_error": None,
    }

    async def _conv_ws(conversation_id):
        return conv_dir

    async def _proj_ws(project_id):
        state["project_calls"].append(project_id)
        return proj_dir

    async def _spawn(*argv, **kwargs):
        if state["spawn_error"] is not None:
            raise state["spawn_error"]
        state["argv"] = list(argv)
        return state["process"]

    monkeypatch.setattr(sandbox_mod, "conversation_workspace_dir", _conv_ws)
    monkeypatch.setattr(sandbox_mod, "project_workspace_dir", _proj_ws)
    monkeypatch.setattr(
        sandbox_mod, "_publish_file_list_changed",
        lambda *a: state["events"].append(a),
    )
    monkeypatch.setattr(
        sandbox_mod, "get_sandbox_seccomp_profile_path",
        lambda: str(tmp_path / "seccomp.json"),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    (conv_dir / "s.py").write_text("print(1)\n")
    state["conv_dir"] = conv_dir
    state["proj_dir"] = proj_dir
    return state


def _call(handler, project_id=None, public=False, path="s.py"):
    if handler == "script":
        coro = sandbox_mod._handle_run_script(
            7, "conv-1", path, project_id=project_id, public=public,
        )
    else:
        coro = sandbox_mod._handle_run_python(
            7, "conv-1", "print(1)", project_id=project_id, public=public,
        )
    return json.loads(_run(coro))


@pytest.mark.parametrize("handler", ["script", "python"])
@pytest.mark.parametrize("public", [False, True])
class TestHandlersMountAndPublish:
    def test_standalone_mounts_workspace_only(self, stub, handler, public):
        result = _call(handler, public=public)
        assert result["exit_code"] == 0
        assert _mounts(stub["argv"]) == [f"{stub['conv_dir']}:/workspace:Z"]
        assert stub["project_calls"] == []
        assert stub["events"] == [(7, "conversation", "conv-1", None)]

    def test_project_conversation_mounts_project_and_publishes_both(
        self, stub, handler, public,
    ):
        result = _call(handler, project_id="proj-1", public=public)
        assert result["exit_code"] == 0
        assert stub["project_calls"] == ["proj-1"]
        assert _mounts(stub["argv"]) == [
            f"{stub['conv_dir']}:/workspace:Z",
            f"{stub['proj_dir']}:/project:z",
        ]
        assert stub["events"] == [
            (7, "conversation", "conv-1", "proj-1"),
            (7, "project", "conv-1", "proj-1"),
        ]

    def test_nonzero_exit_still_publishes(self, stub, handler, public):
        # The container ran (and may have written files) -- same as before.
        stub["process"] = _FakeProcess(returncode=1)
        result = _call(handler, project_id="proj-1", public=public)
        assert result["exit_code"] == 1
        assert [e[1] for e in stub["events"]] == ["conversation", "project"]

    def test_timeout_still_publishes_both_scopes(
        self, stub, handler, public, monkeypatch,
    ):
        async def _timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError
        monkeypatch.setattr(sandbox_mod.asyncio, "wait_for", _timeout)
        result = _call(handler, project_id="proj-1", public=public)
        assert result["timed_out"] is True
        assert stub["events"] == [
            (7, "conversation", "conv-1", "proj-1"),
            (7, "project", "conv-1", "proj-1"),
        ]

    def test_missing_image_publishes_nothing(self, stub, handler, public):
        stub["process"] = _FakeProcess(
            returncode=125, stderr=b"Error: image not known",
        )
        result = _call(handler, project_id="proj-1", public=public)
        assert "not found" in result["error"]
        assert stub["events"] == []

    def test_missing_podman_publishes_nothing(self, stub, handler, public):
        stub["spawn_error"] = FileNotFoundError("podman")
        result = _call(handler, project_id="proj-1", public=public)
        assert "Podman not found" in result["error"]
        assert stub["events"] == []


class TestRunScriptPathsStayConversationOnly:
    def test_project_file_is_not_found_with_copy_hint(self, stub):
        (stub["proj_dir"] / "etl.py").write_text("print(1)\n")
        result = _call("script", project_id="proj-1", path="etl.py")
        assert result["error"].startswith("File not found: etl.py.")
        assert "copy_project_file" in result["error"]
        assert "run_python" in result["error"]
        assert stub["argv"] is None
        assert stub["events"] == []

    def test_standalone_not_found_message_unchanged(self, stub):
        result = _call("script", path="missing.py")
        assert result == {"error": "File not found: missing.py"}
