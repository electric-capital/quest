"""Tests for run.py's sandbox image warm-up (``warm_podman_image``).

The first ``--userns=keep-id`` run of a freshly built image makes podman copy
every layer with shifted ownership (tens of seconds for the ~1 GB sandbox
images). run.py pays that cost once at startup so it never lands inside a
script's own timeout, where the kill discards the partial copy and every
subsequent short-timeout run starts the copy over.
"""

import subprocess

import run


class _Result:
    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def test_warm_runs_noop_keepid_container(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Result(0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    elapsed = run.warm_podman_image("quest-script-runner-staging")

    assert elapsed is not None and elapsed >= 0
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == ["podman", "run", "--rm"]
    # The same userns mapping the sandbox handler uses -- that is what keys
    # the id-mapped layer copy.
    assert "--userns=keep-id" in argv
    # No entrypoint (iptables/socat would refuse to start without caps) and
    # no slirp4netns setup: only the layer copy matters.
    assert argv[argv.index("--entrypoint") + 1] == "/bin/true"
    assert "--network=none" in argv
    assert argv[-1] == "quest-script-runner-staging"
    assert kwargs["timeout"] == run.PODMAN_WARMUP_TIMEOUT_SECONDS


def test_warm_reports_failure_as_none(monkeypatch):
    monkeypatch.setattr(run.subprocess, "run", lambda argv, **kw: _Result(125))
    assert run.warm_podman_image("quest-script-runner-staging") is None


def test_warm_timeout_is_none_not_raise(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    assert run.warm_podman_image("quest-script-runner-staging") is None


def test_warm_missing_podman_is_none_not_raise(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    assert run.warm_podman_image("quest-script-runner-staging") is None
