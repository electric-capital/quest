"""Tests for run.py's base-image refresh before a sandbox image (re)build
(``dockerfile_base_images`` / ``pull_base_images``).

``podman build`` only pulls a base image that is missing locally, so without
an explicit pull every rebuild keeps the ``python:3.12-slim`` the host fetched
the first time and a security fix in a newer base never reaches the sandboxes.
The pull is best-effort: an offline host builds with its local copy.
"""

import run


class _Result:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


def test_base_images_skip_stages_and_scratch(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM --platform=linux/amd64 golang:1.23 AS build\n"
        "RUN go build\n"
        "FROM scratch\n"
        "FROM build\n"
        "from python:3.12-slim\n"
        "FROM python:3.12-slim\n"
    )
    assert run.dockerfile_base_images(dockerfile) == ["golang:1.23", "python:3.12-slim"]


def test_shipped_dockerfiles_pull_the_python_base():
    root = run.Path(__file__).resolve().parent.parent
    for name in ("Dockerfile.script-runner", "Dockerfile.browser-sidecar"):
        assert run.dockerfile_base_images(root / name) == ["python:3.12-slim"], name


def test_pull_runs_podman_pull_per_base(monkeypatch, tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM a:1 AS x\nFROM b:2\n")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Result(0)

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    run.pull_base_images(tmp_path, "Dockerfile")
    assert calls == [["podman", "pull", "-q", "a:1"], ["podman", "pull", "-q", "b:2"]]


def test_pull_failure_is_reported_not_raised(monkeypatch, tmp_path, capsys):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    monkeypatch.setattr(
        run.subprocess,
        "run",
        lambda argv, **kw: _Result(125, "Error: pinging container registry: no such host\n"),
    )
    run.pull_base_images(tmp_path, "Dockerfile")  # must not raise
    out = capsys.readouterr().out
    assert "Could not pull 'python:3.12-slim'" in out
    assert "no such host" in out
    assert "local copy" in out
