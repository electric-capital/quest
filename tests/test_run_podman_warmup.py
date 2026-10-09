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


# ---------------------------------------------------------------------------
# Superseded-image removal after a rebuild
# ---------------------------------------------------------------------------

OLD = "a" * 64
NEW = "b" * 64


class _FakePodman:
    """Scripted ``subprocess.run`` for the image helpers: ``images -q``
    answers with the current ID, ``images --filter id=`` with the tags,
    and ``rmi`` is recorded."""

    def __init__(self, current_id, tags):
        self.current_id = current_id
        self.tags = tags
        self.removed = []

    def __call__(self, argv, **kwargs):
        result = _Result(0)
        if argv[:2] == ["podman", "rmi"]:
            self.removed.append(argv)
        elif "--filter" in argv:
            result.stdout = "\n".join(self.tags) + "\n"
        elif argv[:3] == ["podman", "images", "-q"]:
            # ``--no-trunc`` output carries the digest prefix
            result.stdout = (f"sha256:{self.current_id}" if self.current_id else "") + "\n"
        return result


def test_removes_untagged_previous_build(monkeypatch):
    fake = _FakePodman(current_id=NEW, tags=["<none>:<none>"])
    monkeypatch.setattr(run.subprocess, "run", fake)
    assert run.remove_superseded_podman_image("quest-script-runner-staging", OLD) is True
    assert fake.removed == [["podman", "rmi", "-f", OLD]]


def test_keeps_image_when_rebuild_was_a_cache_hit(monkeypatch):
    fake = _FakePodman(current_id=OLD, tags=["localhost/quest-script-runner-staging:latest"])
    monkeypatch.setattr(run.subprocess, "run", fake)
    assert run.remove_superseded_podman_image("quest-script-runner-staging", OLD) is False
    assert fake.removed == []


def test_keeps_image_still_tagged_by_another_name(monkeypatch):
    # A -local and a -prod image built from identical sources share one ID.
    fake = _FakePodman(current_id=NEW, tags=["localhost/quest-script-runner-prod:latest"])
    monkeypatch.setattr(run.subprocess, "run", fake)
    assert run.remove_superseded_podman_image("quest-script-runner-local", OLD) is False
    assert fake.removed == []


def test_no_previous_image_is_a_noop(monkeypatch):
    fake = _FakePodman(current_id=NEW, tags=[])
    monkeypatch.setattr(run.subprocess, "run", fake)
    assert run.remove_superseded_podman_image("quest-script-runner-staging", None) is False
    assert fake.removed == []


def test_image_id_and_tags_parsing(monkeypatch):
    fake = _FakePodman(current_id=NEW, tags=["<none>:<none>", "localhost/x:latest", ""])
    monkeypatch.setattr(run.subprocess, "run", fake)
    assert run.get_podman_image_id("x") == NEW
    assert run.podman_image_tags(NEW) == ["localhost/x:latest"]
    fake.current_id = ""
    assert run.get_podman_image_id("x") is None
