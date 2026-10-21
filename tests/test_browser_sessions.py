"""Tests for the public-project browser sessions (chat/browser_sessions.py).

Covers the sidecar ``podman run`` argv (the public network posture, no
credential, only the state directory mounted), the session manager with
podman and the agent socket stubbed out (start on first call, reuse,
per-user and server caps, dropped on an unreachable agent, stop, idle
reaping), the boot-time stale-container sweep and the availability check
behind the admin feature gate.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import chat.browser_sessions as bs
from chat.browser_sessions import (
    BrowserError,
    BrowserSession,
    BrowserSessionManager,
    build_browser_podman_cmd,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# podman argv
# ---------------------------------------------------------------------------


class TestBuildBrowserPodmanCmd:
    def test_public_network_posture_without_credentials(self, tmp_path):
        cmd = build_browser_podman_cmd(
            "quest-browser-9400-c1", tmp_path / "browser", "quest-browser-sidecar-local", "9400",
        )
        assert cmd[0] == "podman"
        assert "run" in cmd and "-d" in cmd and "--rm" in cmd
        assert cmd[cmd.index("--name") + 1] == "quest-browser-9400-c1"
        assert "quest.browser.instance=9400" in cmd
        # Same slirp posture as the public script sandbox: host loopback
        # unmapped, IPv6 off, DNS pinned to the slirp resolver.
        assert "--network=slirp4netns:allow_host_loopback=false,enable_ipv6=false" in cmd
        assert "--sysctl=net.ipv6.conf.all.disable_ipv6=1" in cmd
        assert "--dns=10.0.2.3" in cmd and "--dns-search=." in cmd
        # Root entrypoint + privilege drop choreography and the seccomp
        # profile, as the script sandbox.
        assert "--userns=keep-id" in cmd and "--user=0:0" in cmd
        assert "--cap-add=NET_ADMIN" in cmd and "--cap-add=SETPCAP" in cmd
        assert any(c.startswith("--security-opt=seccomp=") for c in cmd)
        joined = " ".join(cmd)
        assert "QUEST_API_KEY" not in joined and "QUEST_PORT" not in joined
        assert cmd[-1] == "quest-browser-sidecar-local"

    def test_mounts_only_the_state_dir(self, tmp_path):
        state = tmp_path / "browser"
        cmd = build_browser_podman_cmd("n", state, "img", "1")
        mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
        assert mounts == [f"{state}:/browser-state:Z"]
        assert "/workspace" not in " ".join(cmd)
        assert "/project" not in " ".join(cmd)

    def test_resource_limits_fit_chromium(self, tmp_path):
        cmd = build_browser_podman_cmd("n", tmp_path, "img", "1")
        assert f"--memory={bs.BROWSER_CONTAINER_MEMORY}" in cmd
        assert f"--cpus={bs.BROWSER_CONTAINER_CPUS}" in cmd
        assert f"--shm-size={bs.BROWSER_CONTAINER_SHM}" in cmd
        assert "HOME=/tmp" in cmd


class TestNaming:
    def test_container_name_carries_instance_and_conversation(self, monkeypatch):
        monkeypatch.setattr(bs, "instance_key", lambda: "9400")
        assert bs.container_name("abc") == "quest-browser-9400-abc"

    def test_connect_path_is_short_whatever_the_state_dir(self, tmp_path):
        # Unix socket paths max out at 108 bytes; the session connects
        # through /proc/self/fd/<dir fd>/agent.sock instead of the full path.
        deep = tmp_path / ("x" * 60) / ("y" * 60) / "browser"
        deep.mkdir(parents=True)
        session = BrowserSession(conversation_id="c", user_id=1, name="n", state_dir=deep)
        assert len(str(session.socket_path)) > 108
        path = session.connect_path
        assert len(path) < 60 and path.startswith("/proc/self/fd/")
        (deep / "agent.sock").write_text("")
        assert Path(path).exists() and Path(path).samefile(deep / "agent.sock")
        assert session.connect_path == path  # the descriptor is reused
        session.close_fd()
        assert session.dir_fd is None

    def test_state_dir_is_outside_the_workspace(self):
        conversation_id = str(uuid.uuid4())
        state = bs.browser_state_dir(conversation_id)
        assert state.name == "browser"
        assert state.parent.name == conversation_id
        assert "workspace" not in state.parts


# ---------------------------------------------------------------------------
# Session manager (podman + agent stubbed)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(monkeypatch, tmp_path):
    """A manager whose containers are fake: ``_start`` records a session
    and ``_request`` answers from a scripted table."""
    mgr = BrowserSessionManager()
    stopped: list[str] = []
    started: list[str] = []
    responses: dict[str, object] = {}

    async def fake_start(conversation_id, user_id):
        started.append(conversation_id)
        return BrowserSession(
            conversation_id=conversation_id, user_id=user_id,
            name=f"quest-browser-test-{conversation_id}",
            state_dir=tmp_path / conversation_id,
        )

    async def fake_request(session, method, action, params, timeout=0):
        answer = responses.get(action, {"ok": True, "action": action, "params": params})
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def fake_stop_container(session):
        stopped.append(session.name)

    monkeypatch.setattr(mgr, "_start", fake_start)
    monkeypatch.setattr(mgr, "_request", fake_request)
    monkeypatch.setattr(mgr, "_stop_container", fake_stop_container)
    mgr.test_started = started
    mgr.test_stopped = stopped
    mgr.test_responses = responses
    return mgr


class TestManagerLifecycle:
    def test_first_call_starts_then_reuses(self, manager):
        r1 = _run(manager.call("c1", 7, "navigate", {"url": "https://x"}))
        r2 = _run(manager.call("c1", 7, "snapshot", {}))
        assert r1["params"] == {"url": "https://x"}
        assert r2["action"] == "snapshot"
        assert manager.test_started == ["c1"]
        session = manager.get("c1")
        assert session.calls == 2 and session.user_id == 7

    def test_per_user_cap(self, manager, monkeypatch):
        monkeypatch.setattr(bs, "BROWSER_MAX_PER_USER", 1)
        _run(manager.call("c1", 7, "snapshot", {}))
        with pytest.raises(BrowserError) as exc:
            _run(manager.call("c2", 7, "snapshot", {}))
        assert "browser sessions open" in str(exc.value)
        # Another user is unaffected, and the existing session still works.
        _run(manager.call("c3", 8, "snapshot", {}))
        _run(manager.call("c1", 7, "snapshot", {}))
        assert manager.test_started == ["c1", "c3"]

    def test_server_cap(self, manager, monkeypatch):
        monkeypatch.setattr(bs, "BROWSER_MAX_TOTAL", 1)
        _run(manager.call("c1", 7, "snapshot", {}))
        with pytest.raises(BrowserError) as exc:
            _run(manager.call("c2", 8, "snapshot", {}))
        assert "capacity" in str(exc.value)

    def test_unreachable_agent_drops_the_session(self, manager):
        _run(manager.call("c1", 7, "snapshot", {}))
        manager.test_responses["click"] = BrowserError("The browser session is not reachable: gone")
        with pytest.raises(BrowserError):
            _run(manager.call("c1", 7, "click", {"ref": "e1"}))
        assert manager.get("c1") is None
        assert manager.test_stopped == ["quest-browser-test-c1"]
        # The next call starts a fresh container.
        manager.test_responses.pop("click")
        _run(manager.call("c1", 7, "click", {"ref": "e1"}))
        assert manager.test_started == ["c1", "c1"]

    def test_agent_error_results_are_relayed_not_raised(self, manager):
        # An action the agent ran but that failed is an ordinary result.
        manager.test_responses["navigate"] = {"error": "TimeoutError: Page.goto: Timeout"}
        result = _run(manager.call("c1", 7, "navigate", {"url": "https://slow"}))
        assert result["error"].startswith("TimeoutError")
        assert manager.get("c1") is not None

    def test_stop_and_stop_all(self, manager):
        _run(manager.call("c1", 7, "snapshot", {}))
        _run(manager.call("c2", 8, "snapshot", {}))
        assert _run(manager.stop("c1", reason="test")) is True
        assert _run(manager.stop("c1")) is False
        assert manager.get("c1") is None and manager.get("c2") is not None
        _run(manager.stop_all())
        assert manager.sessions() == []
        assert manager.test_stopped == [
            "quest-browser-test-c1", "quest-browser-test-c2",
        ]

    def test_reap_idle_stops_only_idle_unlocked_sessions(self, manager, monkeypatch):
        monkeypatch.setattr(bs, "BROWSER_IDLE_SECONDS", 100)
        _run(manager.call("c1", 7, "snapshot", {}))
        _run(manager.call("c2", 8, "snapshot", {}))
        _run(manager.call("c3", 9, "snapshot", {}))
        manager.get("c1").last_used -= 200          # idle past the limit
        manager.get("c2").last_used -= 200          # idle but mid-call

        async def reap_with_c2_busy():
            async with manager.get("c2").lock:
                return await manager.reap_idle()

        assert _run(reap_with_c2_busy()) == 1
        assert manager.get("c1") is None
        assert manager.get("c2") is not None and manager.get("c3") is not None


class TestManagerStart:
    """``_start`` against a stubbed ``podman run``."""

    def _manager_with_podman(self, monkeypatch, tmp_path, returncode=0, stderr=b""):
        mgr = BrowserSessionManager()
        calls: list[list[str]] = []

        class FakeProc:
            def __init__(self, rc, err):
                self.returncode = rc
                self._err = err

            async def communicate(self):
                return b"cid\n", self._err

        async def fake_exec(*argv, **kw):
            calls.append(list(argv))
            return FakeProc(returncode, stderr)

        async def fake_run_podman(*args, timeout=0):
            calls.append(["podman", *args])
            return 0, "", ""

        async def fake_wait_ready(session):
            return None

        monkeypatch.setattr(bs.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(bs, "_run_podman", fake_run_podman)
        monkeypatch.setattr(bs, "browser_state_dir", lambda cid: tmp_path / cid / "browser")
        monkeypatch.setattr(bs, "instance_key", lambda: "9400")
        monkeypatch.setattr(mgr, "_wait_ready", fake_wait_ready)
        return mgr, calls

    def test_start_runs_podman_and_registers(self, monkeypatch, tmp_path):
        mgr, calls = self._manager_with_podman(monkeypatch, tmp_path)
        session = _run(mgr.acquire("c1", 7))
        assert session.name == "quest-browser-9400-c1"
        assert session.state_dir.is_dir()
        run_argv = next(c for c in calls if "run" in c)
        assert "--name" in run_argv and "quest-browser-9400-c1" in run_argv
        # A leftover container of that name is removed first.
        assert ["podman", "rm", "-f", "quest-browser-9400-c1"] in calls
        assert mgr.get("c1") is session

    def test_missing_image_is_a_clear_error(self, monkeypatch, tmp_path):
        mgr, _ = self._manager_with_podman(
            monkeypatch, tmp_path, returncode=125,
            stderr=b"Error: quest-browser-sidecar-local: image not known",
        )
        with pytest.raises(BrowserError) as exc:
            _run(mgr.acquire("c1", 7))
        assert "not built" in str(exc.value)
        assert mgr.get("c1") is None

    def test_not_ready_stops_the_container(self, monkeypatch, tmp_path):
        mgr, calls = self._manager_with_podman(monkeypatch, tmp_path)

        async def never_ready(session):
            raise BrowserError("The browser did not become ready")

        monkeypatch.setattr(mgr, "_wait_ready", never_ready)
        with pytest.raises(BrowserError):
            _run(mgr.acquire("c1", 7))
        assert ["podman", "stop", "-t", "5", "quest-browser-9400-c1"] in calls
        assert mgr.get("c1") is None


# ---------------------------------------------------------------------------
# Host-level helpers
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_no_podman(self, monkeypatch):
        monkeypatch.setattr(bs.shutil, "which", lambda name: None)
        available, reason = bs.browser_availability(use_cache=False)
        assert available is False and "podman" in reason

    def test_image_missing(self, monkeypatch):
        monkeypatch.setattr(bs.shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(
            bs.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        available, reason = bs.browser_availability(use_cache=False)
        assert available is False and "not built" in reason

    def test_image_present(self, monkeypatch):
        monkeypatch.setattr(bs.shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(
            bs.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="abc123\n", stderr=""),
        )
        assert bs.browser_availability(use_cache=False) == (True, None)

    def test_feature_gate_consults_availability(self, monkeypatch):
        from chat.routes.admin import _feature_availability
        from config.feature_gates import FEATURE_BROWSER
        monkeypatch.setattr(bs, "browser_availability", lambda: (False, "nope"))
        assert _feature_availability(FEATURE_BROWSER) == (False, "nope")


class TestStaleSweep:
    def test_removes_only_this_instance_label(self, monkeypatch):
        monkeypatch.setattr(bs.shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(bs, "instance_key", lambda: "9400")
        calls: list[list[str]] = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[1] == "ps":
                return SimpleNamespace(returncode=0, stdout="id1\nid2\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(bs.subprocess, "run", fake_run)
        assert bs.remove_stale_containers() == 2
        assert calls[0][-1] == "label=quest.browser.instance=9400"
        assert calls[1] == ["podman", "rm", "-f", "id1", "id2"]

    def test_nothing_to_remove(self, monkeypatch):
        monkeypatch.setattr(bs.shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(
            bs.subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        assert bs.remove_stale_containers() == 0
