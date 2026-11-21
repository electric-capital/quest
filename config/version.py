"""Release version resolution from git tags.

Releases are marked by annotated git tags named ``v<MAJOR>.<MINOR>.<PATCH>``
(created with ``scripts/tag_release.py``). At process startup this module
asks the checkout's git metadata for the nearest such tag and derives the
release info shown in Settings > About and returned by ``GET /app/api/version``.

Stdlib-only so ``run.py`` can print the version before dependencies are
installed. Every git call is best-effort: a checkout without ``.git`` (a
tarball) or without any release tag simply reports ``version=None``.

Version strings:

- exactly on a tag:          ``1.4.0``
- N commits past a tag:      ``1.4.0+3.g1a2b3c4`` (semver build metadata)
- no release tag reachable:  ``None`` (the UI falls back to the short hash)
"""

import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Release tags: a ``v`` prefix and a plain semver core. Pre-release suffixes
# (``v1.4.0-rc1``) are accepted by the tag script and reported verbatim.
RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class ReleaseInfo:
    """Release metadata captured once at startup."""

    # Human-readable version without the ``v`` prefix, or None when no
    # release tag is reachable from HEAD (or git is unavailable).
    version: Optional[str]
    # The release tag itself (``v1.4.0``) when HEAD is at or past one.
    tag: Optional[str]
    # ISO-8601 date-time of the tag (tagger date for annotated tags, else
    # the tagged commit's date) -- what the About section shows as
    # "Released". None when there is no tag.
    released: Optional[str]
    # Number of commits between the tag and HEAD; 0 = exactly the release.
    commits_since_tag: int
    # Full commit hash of HEAD, or None when git is unavailable.
    git_hash: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _git(args: list[str], cwd: Path) -> Optional[str]:
    """Run a git command and return its stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def format_version(tag: Optional[str], commits_since_tag: int, git_hash: Optional[str]) -> Optional[str]:
    """Build the display version from the nearest tag and the distance to it."""
    if not tag:
        return None
    base = tag[1:] if tag.startswith("v") else tag
    if commits_since_tag <= 0 or not git_hash:
        return base
    return f"{base}+{commits_since_tag}.g{git_hash[:7]}"


def resolve_release_info(project_root: Optional[Path] = None) -> ReleaseInfo:
    """Inspect the git checkout at ``project_root`` and return its release info.

    Never raises: any git failure degrades the corresponding field to None.
    """
    root = project_root or PROJECT_ROOT
    git_hash = _git(["rev-parse", "HEAD"], root) or None

    tag = None
    if git_hash:
        # Nearest reachable release tag. ``--match`` keeps unrelated tags
        # (e.g. ``deploy-2026-01-01``) from being picked up.
        described = _git(["describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"], root)
        if described and RELEASE_TAG_RE.match(described):
            tag = described

    commits_since_tag = 0
    released = None
    if tag:
        count = _git(["rev-list", "--count", f"{tag}..HEAD"], root)
        if count and count.isdigit():
            commits_since_tag = int(count)
        # Annotated tags carry their own date; lightweight tags fall back
        # to the commit date via the second format placeholder.
        released = _git(
            [
                "for-each-ref",
                "--format=%(if)%(taggerdate:iso-strict)%(then)%(taggerdate:iso-strict)%(else)%(committerdate:iso-strict)%(end)",
                f"refs/tags/{tag}",
            ],
            root,
        ) or None

    return ReleaseInfo(
        version=format_version(tag, commits_since_tag, git_hash),
        tag=tag,
        released=released,
        commits_since_tag=commits_since_tag,
        git_hash=git_hash,
    )


# Captured at import time so the values stay accurate for the lifetime of
# the process even if the working tree moves underneath it (a deploy pulls
# new commits while the old server is still running).
RELEASE_INFO = resolve_release_info()


def describe_release(info: ReleaseInfo = RELEASE_INFO) -> str:
    """One-line human summary for startup banners: ``v1.4.0 (1a2b3c4)``."""
    short = (info.git_hash or "unknown")[:7]
    if info.version:
        return f"v{info.version} ({short})"
    return f"unreleased ({short})"
