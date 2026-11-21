"""Tests for sandbox container naming, removal and sweeps
(chat/gemini_api/sandbox_containers.py) and the shared run helper in
chat/gemini_api/tool_handlers/sandbox.py.

Sandbox containers run without ``--rm``: the handler returns as soon as the
container exits and removes it in the background; a timeout or a cancelled
run force-removes it so the script stops; a boot sweep and a periodic prune
clear anything left behind.
"""

import ast
import asyncio
import sys
import time
from pathlib import Path

import pytest

import chat.gemini_api.sandbox_containers as sc
import chat.gemini_api.tool_handlers.sandbox as sandbox
from chat.gemini_api.tool_handlers import _build_script_podman_cmd


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def podman_calls(monkeypatch):
    """Record every podman call the module makes instead of running podman."""
    calls: list[list[str]] = []

    async def _fake(*args, timeout=60.0):
        calls.append(list(args))
        return 0, "", ""

    monkeypatch.setattr(sc, "_podman", _fake)
    monkeypatch.setattr(sc, "sandbox_instance_key", lambda: "9123")
    return calls


class TestNames:

    def test_name_carries_instance_and_is_unique(self, monkeypatch):
        monkeypatch.setattr(sc, "sandbox_instance_key", lambda: "9123")
        a, b = sc.sandbox_container_name(), sc.sandbox_container_name()
        assert a.startswith("quest-sandbox-9123-")
        assert a != b


class TestPodmanArgv:

    def test_named_run_has_name_and_label_and_no_rm(self, monkeypatch):
        monkeypatch.setattr(sandbox, "get_sandbox_runtime", lambda: None)
        monkeypatch.setattr(sc, "sandbox_instance_key", lambda: "9123")
        cmd = _build_script_podman_cmd(
            "/ws", ["python3"], "k", container_name="quest-sandbox-9123-abc",
        )
        assert cmd[:6] == [
            "podman", "run",
            "--name", "quest-sandbox-9123-abc",
            "--label", "quest.sandbox.instance=9123",
        ]
        assert "--rm" not in cmd

    def test_unnamed_run_keeps_rm(self, monkeypatch):
        monkeypatch.setattr(sandbox, "get_sandbox_runtime", lambda: None)
        cmd = _build_script_podman_cmd("/ws", ["python3"], "k")
        assert cmd[:3] == ["podman", "run", "--rm"]
        assert "--name" not in cmd

    def test_named_interactive_keeps_i_flag(self, monkeypatch):
        monkeypatch.setattr(sandbox, "get_sandbox_runtime", lambda: None)
        cmd = _build_script_podman_cmd(
            "/ws", ["python3"], "k", interactive=True, container_name="n",
        )
        assert "-i" in cmd
        assert cmd.index("-i") > cmd.index("--label")


class TestRemoval:

    def test_remove_container_forces_without_grace(self, podman_calls):
        _run(sc.remove_container("quest-sandbox-9123-abc"))
        assert podman_calls == [
            ["rm", "-f", "-t", "0", "--ignore", "quest-sandbox-9123-abc"],
        ]

    def test_remove_container_swallows_failures(self, monkeypatch):
        async def _boom(*args, timeout=60.0):
            raise OSError("podman vanished")

        monkeypatch.setattr(sc, "_podman", _boom)
        _run(sc.remove_container("x"))  # must not raise

    def test_scheduled_removal_runs_in_background(self, podman_calls):
        async def main():
            sc.schedule_removal("quest-sandbox-9123-abc")
            assert podman_calls == []  # not awaited by the caller
            await asyncio.gather(*sc._pending_removals)

        _run(main())
        assert podman_calls == [
            ["rm", "-f", "-t", "0", "--ignore", "quest-sandbox-9123-abc"],
        ]
        assert not sc._pending_removals

    def test_prune_only_this_instance_and_old_enough(self, podman_calls):
        _run(sc.prune_stopped_containers())
        assert podman_calls == [[
            "container", "prune", "-f",
            "--filter", "label=quest.sandbox.instance=9123",
            "--filter", "until=10m",
        ]]


class TestBootSweep:

    def test_removes_every_container_of_this_instance(self, monkeypatch):
        runs = []

        def _fake_run(argv, **kwargs):
            runs.append(argv)

            class R:
                stdout = "aaa\nbbb\n" if argv[1] == "ps" else ""
                returncode = 0
            return R()

        monkeypatch.setattr(sc.shutil, "which", lambda n: "/usr/bin/podman")
        monkeypatch.setattr(sc.subprocess, "run", _fake_run)
        monkeypatch.setattr(sc, "sandbox_instance_key", lambda: "9123")
        assert sc.remove_stale_sandbox_containers() == 2
        assert runs == [
            ["podman", "ps", "-a", "-q", "--filter", "label=quest.sandbox.instance=9123"],
            ["podman", "rm", "-f", "-t", "0", "aaa", "bbb"],
        ]

    def test_no_podman_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(sc.shutil, "which", lambda n: None)
        assert sc.remove_stale_sandbox_containers() == 0


def test_lifespan_sweeps_at_boot_and_runs_the_prune():
    """quest.py's lifespan removes stale containers at boot, starts the
    prune loop and cancels it on shutdown. Checked on the source: importing
    quest loads every plugin into process-global registries."""
    source = (Path(__file__).resolve().parents[1] / "quest.py").read_text()
    lifespan = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    body = ast.get_source_segment(source, lifespan)
    assert "asyncio.to_thread(remove_stale_sandbox_containers)" in body
    assert "asyncio.create_task(sandbox_prune_loop())" in body
    assert "sandbox_prune_task.cancel()" in body
    # Shutdown also removes this instance's containers: a run cancelled at
    # shutdown may never get its background removal.
    assert body.count("asyncio.to_thread(remove_stale_sandbox_containers)") == 2


def _py(code: str) -> list[str]:
    """A stand-in for the podman argv: a real child process."""
    return [sys.executable, "-c", code]


class TestRunSandboxContainer:

    def test_normal_exit_returns_output_and_schedules_removal(self, monkeypatch):
        scheduled = []
        monkeypatch.setattr(sandbox, "schedule_removal", scheduled.append)
        out, err, rc, timed_out = _run(sandbox._run_sandbox_container(
            _py("import sys; print('hi'); sys.exit(3)"), "ctr-1", timeout=10,
        ))
        assert (out.strip(), rc, timed_out) == (b"hi", 3, False)
        assert scheduled == ["ctr-1"]

    def test_no_stdin_means_no_stdin_kwarg(self, monkeypatch):
        """run_script passes no stdin: the spawn must not get a stdin kwarg
        (the child inherits the server's stdin, as before)."""
        seen = {}

        async def _spawn(*argv, **kwargs):
            seen.update(kwargs)
            raise FileNotFoundError("podman")

        monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _spawn)
        with pytest.raises(FileNotFoundError):
            _run(sandbox._run_sandbox_container(["podman"], "ctr-1", timeout=10))
        assert "stdin" not in seen

    def test_stdin_is_delivered(self, monkeypatch):
        monkeypatch.setattr(sandbox, "schedule_removal", lambda n: None)
        out, _, rc, _ = _run(sandbox._run_sandbox_container(
            _py("import sys; print(sys.stdin.read().upper())"), "ctr-1",
            timeout=10, stdin=b"abc",
        ))
        assert (out.strip(), rc) == (b"ABC", 0)

    def test_timeout_force_removes_container_before_returning(self, monkeypatch):
        removed, scheduled = [], []

        async def _remove(name):
            removed.append(name)

        monkeypatch.setattr(sandbox, "remove_container", _remove)
        monkeypatch.setattr(sandbox, "schedule_removal", scheduled.append)
        out, err, _, timed_out = _run(sandbox._run_sandbox_container(
            _py("import time; time.sleep(30)"), "ctr-1", timeout=0.5,
        ))
        assert timed_out is True
        assert (out, err) == (b"", b"")
        assert removed == ["ctr-1"]
        assert scheduled == []

    def test_cancelled_run_kills_client_and_removes_container(self, podman_calls):
        async def main():
            task = asyncio.create_task(sandbox._run_sandbox_container(
                _py("import time; time.sleep(30)"), "ctr-1", timeout=60,
            ))
            await asyncio.sleep(0.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.gather(*sc._pending_removals)

        start = time.monotonic()
        _run(main())
        # The client was killed (not left to sleep 30 s) and then removed.
        assert time.monotonic() - start < 5
        assert podman_calls == [["rm", "-f", "-t", "0", "--ignore", "ctr-1"]]
