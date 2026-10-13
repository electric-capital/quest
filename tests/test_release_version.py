"""Tests for release-version resolution from git tags (config/version.py)
and the release tagging script (scripts/tag_release.py)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import version as version_mod
from config.version import (
    RELEASE_TAG_RE,
    ReleaseInfo,
    describe_release,
    format_version,
    resolve_release_info,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TAG_SCRIPT = PROJECT_ROOT / "scripts" / "tag_release.py"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=_GIT_ENV,
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "one")
    return r


# ---------------------------------------------------------------------------
# resolve_release_info
# ---------------------------------------------------------------------------

class TestResolveReleaseInfo:
    def test_untagged_checkout_reports_hash_only(self, repo):
        head = git(repo, "rev-parse", "HEAD")
        info = resolve_release_info(repo)
        assert info == ReleaseInfo(
            version=None, tag=None, released=None, commits_since_tag=0, git_hash=head,
        )

    def test_exactly_on_annotated_tag(self, repo):
        git(repo, "tag", "-a", "v1.4.0", "-m", "Release v1.4.0")
        info = resolve_release_info(repo)
        assert info.version == "1.4.0"
        assert info.tag == "v1.4.0"
        assert info.commits_since_tag == 0
        assert info.released is not None
        # Annotated tag: tagger date, iso-strict.
        assert info.released[:4].isdigit() and "T" in info.released

    def test_commits_past_tag_carry_build_metadata(self, repo):
        git(repo, "tag", "-a", "v1.4.0", "-m", "Release v1.4.0")
        commit(repo, "two")
        head = commit(repo, "three")
        info = resolve_release_info(repo)
        assert info.tag == "v1.4.0"
        assert info.commits_since_tag == 2
        assert info.version == f"1.4.0+2.g{head[:7]}"

    def test_lightweight_tag_falls_back_to_commit_date(self, repo):
        git(repo, "tag", "v0.9.0")
        info = resolve_release_info(repo)
        assert info.version == "0.9.0"
        assert info.released == git(repo, "log", "-1", "--format=%cI")

    def test_non_release_tags_are_ignored(self, repo):
        git(repo, "tag", "deploy-2026-01-01")
        git(repo, "tag", "v-not-a-version")
        assert resolve_release_info(repo).version is None

    def test_nearest_tag_wins(self, repo):
        git(repo, "tag", "-a", "v1.0.0", "-m", "x")
        commit(repo, "two")
        git(repo, "tag", "-a", "v1.1.0", "-m", "y")
        info = resolve_release_info(repo)
        assert info.tag == "v1.1.0"
        assert info.commits_since_tag == 0

    def test_prerelease_tag_reported_verbatim(self, repo):
        git(repo, "tag", "-a", "v2.0.0-rc1", "-m", "rc")
        assert resolve_release_info(repo).version == "2.0.0-rc1"

    def test_no_git_directory_degrades_to_none(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        info = resolve_release_info(plain)
        assert info == ReleaseInfo(
            version=None, tag=None, released=None, commits_since_tag=0, git_hash=None,
        )

    def test_git_binary_missing_degrades_to_none(self, repo, monkeypatch):
        monkeypatch.setattr(version_mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        assert resolve_release_info(repo).git_hash is None

    def test_to_dict_is_the_endpoint_shape(self, repo):
        d = resolve_release_info(repo).to_dict()
        assert set(d) == {"version", "tag", "released", "commits_since_tag", "git_hash"}


class TestHelpers:
    @pytest.mark.parametrize("tag,expected", [
        ("v1.2.3", True), ("v0.0.1", True), ("v1.2.3-rc1", True), ("v1.2.3-beta.2", True),
        ("1.2.3", False), ("v1.2", False), ("v1.2.3.4", False), ("deploy-1", False), ("v1.2.3+meta", False),
    ])
    def test_release_tag_regex(self, tag, expected):
        assert bool(RELEASE_TAG_RE.match(tag)) is expected

    def test_format_version(self):
        assert format_version(None, 0, "abc") is None
        assert format_version("v1.0.0", 0, "abcdef1234") == "1.0.0"
        assert format_version("v1.0.0", 3, "abcdef1234") == "1.0.0+3.gabcdef1"
        # Distance without a hash can't be expressed; fall back to the base.
        assert format_version("v1.0.0", 3, None) == "1.0.0"

    def test_describe_release(self):
        assert describe_release(ReleaseInfo("1.4.0", "v1.4.0", None, 0, "1a2b3c4d5e")) == "v1.4.0 (1a2b3c4)"
        assert describe_release(ReleaseInfo(None, None, None, 0, "1a2b3c4d5e")) == "unreleased (1a2b3c4)"
        assert describe_release(ReleaseInfo(None, None, None, 0, None)) == "unreleased (unknown)"


# ---------------------------------------------------------------------------
# GET /app/api/version
# ---------------------------------------------------------------------------

def test_version_endpoint_returns_release_info():
    # Call the handler directly: spinning up the app lifespan here would
    # register every plugin into the live registries and break later
    # plugin-fixture tests in the same session.
    import asyncio
    from chat.routes.user import get_app_version

    body = asyncio.run(get_app_version())
    assert body == version_mod.RELEASE_INFO.to_dict()
    assert set(body) == {"version", "tag", "released", "commits_since_tag", "git_hash"}


# ---------------------------------------------------------------------------
# scripts/tag_release.py
# ---------------------------------------------------------------------------

def run_tag_script(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # The script resolves the repo from its own location; point its git
    # calls at the fixture repo via cwd override of PROJECT_ROOT.
    code = (
        "import runpy, sys, pathlib, scripts.tag_release as t; "
        f"t.PROJECT_ROOT = pathlib.Path({str(repo)!r}); "
        f"sys.argv = ['tag_release.py', *{list(args)!r}]; t.main()"
    )
    return subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True, env=_GIT_ENV,
    )


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    r = tmp_path / "work"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, "one")
    git(r, "remote", "add", "origin", str(origin))
    git(r, "push", "-q", "-u", "origin", "main")
    return r


class TestTagReleaseScript:
    def test_creates_annotated_tag_and_pushes(self, repo_with_origin):
        result = run_tag_script(repo_with_origin, "1.0.0", "-m", "- First release")
        assert result.returncode == 0, result.stderr
        assert git(repo_with_origin, "cat-file", "-t", "v1.0.0") == "tag"
        assert git(repo_with_origin, "tag", "-l", "--format=%(contents:subject)", "v1.0.0") == "Quest v1.0.0"
        assert git(repo_with_origin, "tag", "-l", "--format=%(contents:body)", "v1.0.0") == "- First release"
        assert "v1.0.0" in git(repo_with_origin, "ls-remote", "--tags", "origin")
        assert resolve_release_info(repo_with_origin).version == "1.0.0"

    def test_next_bumps_latest_tag(self, repo_with_origin):
        git(repo_with_origin, "tag", "-a", "v1.2.3", "-m", "x")
        git(repo_with_origin, "push", "-q", "origin", "v1.2.3")
        commit(repo_with_origin, "two")
        git(repo_with_origin, "push", "-q", "origin", "main")
        result = run_tag_script(repo_with_origin, "--next", "minor", "--no-push", "-m", "x")
        assert result.returncode == 0, result.stderr
        assert git(repo_with_origin, "tag", "--list", "v1.3.0") == "v1.3.0"
        assert "v1.3.0" not in git(repo_with_origin, "ls-remote", "--tags", "origin")

    def test_refuses_dirty_tree_and_non_main(self, repo_with_origin):
        git(repo_with_origin, "checkout", "-q", "-b", "feat")
        (repo_with_origin / "dirty").write_text("x")
        result = run_tag_script(repo_with_origin, "1.0.0", "-m", "x")
        assert result.returncode != 0
        assert "not clean" in result.stderr and "not 'main'" in result.stderr
        assert git(repo_with_origin, "tag", "--list") == ""

    def test_refuses_version_not_greater_than_latest(self, repo_with_origin):
        git(repo_with_origin, "tag", "-a", "v2.0.0", "-m", "x")
        result = run_tag_script(repo_with_origin, "1.9.9", "--no-push", "-m", "x")
        assert result.returncode != 0
        assert "not greater than the latest release 2.0.0" in result.stderr

    def test_refuses_existing_tag_and_bad_version(self, repo_with_origin):
        git(repo_with_origin, "tag", "v1.0.0")
        assert "already exists" in run_tag_script(repo_with_origin, "1.0.0", "--force", "-m", "x").stderr
        assert "Invalid version" in run_tag_script(repo_with_origin, "1.0", "--force", "-m", "x").stderr

    def test_requires_highlights(self, repo_with_origin, tmp_path):
        result = run_tag_script(repo_with_origin, "1.0.0", "--no-push")
        assert result.returncode != 0 and "-F/--file" in result.stderr
        empty = tmp_path / "empty.md"
        empty.write_text("  \n")
        result = run_tag_script(repo_with_origin, "1.0.0", "--no-push", "-F", str(empty))
        assert result.returncode != 0 and "highlights are empty" in result.stderr
        assert git(repo_with_origin, "tag", "--list") == ""

    def test_highlights_file_keeps_markdown_headings(self, repo_with_origin, tmp_path):
        notes = tmp_path / "highlights.md"
        notes.write_text("### Docs\n- Quest Docs\n\n### Fixes\n- Voice input on iOS\n")
        result = run_tag_script(repo_with_origin, "1.0.0", "--no-push", "-F", str(notes))
        assert result.returncode == 0, result.stderr
        body = git(repo_with_origin, "tag", "-l", "--format=%(contents:body)", "v1.0.0")
        assert body == "### Docs\n- Quest Docs\n\n### Fixes\n- Voice input on iOS"

    def test_force_bypasses_guards(self, repo_with_origin):
        git(repo_with_origin, "checkout", "-q", "-b", "feat")
        result = run_tag_script(repo_with_origin, "0.1.0", "--force", "--no-push", "-m", "hotfix")
        assert result.returncode == 0, result.stderr
        assert "hotfix" in git(repo_with_origin, "tag", "-l", "-n3", "v0.1.0")

    def test_rejected_push_explains_ruleset(self, repo_with_origin, tmp_path):
        # Simulate the GitHub "release tags" ruleset with a pre-receive hook
        # on the bare origin that refuses every v* tag.
        origin = tmp_path / "origin.git"
        hook = origin / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nwhile read old new ref; do\n"
            "  case \"$ref\" in refs/tags/v*) echo 'remote: error: GH013: Repository rule violations found for refs/tags/v1.0.0' >&2; exit 1;; esac\n"
            "done\nexit 0\n"
        )
        hook.chmod(0o755)
        result = run_tag_script(repo_with_origin, "1.0.0", "-m", "x")
        assert result.returncode != 0
        assert "Repository rule violations" in result.stderr
        assert "bypass list" in result.stderr
        # The local tag survives so a maintainer can push it.
        assert git(repo_with_origin, "tag", "--list", "v1.0.0") == "v1.0.0"
