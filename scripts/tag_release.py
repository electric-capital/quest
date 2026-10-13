#!/usr/bin/env python3
"""Tag a Quest release.

Creates an annotated ``v<MAJOR>.<MINOR>.<PATCH>`` git tag on the current
commit and pushes it to ``origin``. The running server reads the nearest
such tag at startup (config/version.py) and shows it in Settings > About
and on ``GET /app/api/version``; pushing the tag also triggers the
``release.yml`` workflow, which publishes a GitHub Release.

Every release needs highlights, given with ``-F <file>`` (a markdown
bullet list) or ``-m <text>``. The tag message is ``Quest v<version>``
followed by the highlights, and the release workflow puts the highlights
under a "Highlights" heading above GitHub's generated list of merged PRs.

Usage::

    uv run python scripts/tag_release.py 1.4.0 -F highlights.md            # tag + push
    uv run python scripts/tag_release.py 1.4.0 -F highlights.md --no-push  # tag only
    uv run python scripts/tag_release.py 1.4.0 -m "- Public projects GA"
    uv run python scripts/tag_release.py --next patch -F highlights.md     # bump the latest tag

Guards (each can be bypassed with ``--force``): the working tree must be
clean, HEAD must be on ``main`` and match ``origin/main``, and the version
must be greater than the latest existing release tag.

Every ``v*`` tag is a protected release tag on GitHub (the "release tags"
ruleset in .github/rulesets/release-tags.json): only maintainers on the
ruleset's bypass list can push one, and a pushed tag can never be moved
or deleted. A broken release therefore gets a new patch tag, never a
re-pointed one. If the push is rejected, the tag stays local -- hand it
to a maintainer or have them run this script.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.version import RELEASE_TAG_RE  # noqa: E402

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def push_tag(tag: str) -> None:
    """Push the tag, translating a ruleset rejection into actionable advice."""
    result = subprocess.run(
        ["git", "push", "origin", tag], capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    if result.returncode == 0:
        return
    stderr = result.stderr.strip()
    hint = ""
    if "rule" in stderr.lower() or "protected" in stderr.lower():
        hint = (
            "\n\nThe remote's 'release tags' ruleset only lets maintainers on its bypass "
            "list create v* tags. The tag still exists locally; ask a maintainer to push "
            f"it (git push origin {tag}) or to run this script themselves."
        )
    sys.exit(f"git push origin {tag} failed:\n{stderr}{hint}")


def parse_version(version: str) -> tuple[int, int, int, str]:
    m = SEMVER_RE.match(version)
    if not m:
        sys.exit(
            f"Invalid version {version!r}: expected MAJOR.MINOR.PATCH "
            "(optionally with a -suffix), e.g. 1.4.0 or 1.4.0-rc1"
        )
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or ""


def sort_key(version: str) -> tuple:
    major, minor, patch, suffix = parse_version(version)
    # A pre-release sorts below the final release of the same core.
    return (major, minor, patch, 1 if not suffix else 0, suffix)


def existing_release_versions() -> list[str]:
    tags = git("tag", "--list", "v*").splitlines()
    versions = [t[1:] for t in tags if RELEASE_TAG_RE.match(t)]
    return sorted(versions, key=sort_key)


def bump(latest: str | None, part: str) -> str:
    if latest is None:
        return {"major": "1.0.0", "minor": "0.1.0", "patch": "0.0.1"}[part]
    major, minor, patch, _ = parse_version(latest)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", nargs="?", help="release version, e.g. 1.4.0 (no 'v' prefix)")
    parser.add_argument(
        "--next", choices=["major", "minor", "patch"],
        help="derive the version by bumping the latest release tag",
    )
    highlights = parser.add_mutually_exclusive_group(required=True)
    highlights.add_argument("-m", "--message", help="release highlights (markdown bullet list)")
    highlights.add_argument("-F", "--file", type=Path, help="file holding the release highlights")
    parser.add_argument("--no-push", action="store_true", help="create the tag locally only")
    parser.add_argument("--force", action="store_true", help="skip the clean-tree / main / ordering guards")
    args = parser.parse_args()

    if bool(args.version) == bool(args.next):
        parser.error("give exactly one of <version> or --next")
    body = (args.file.read_text() if args.file else args.message).strip()
    if not body:
        parser.error("the release highlights are empty")

    latest = (existing_release_versions() or [None])[-1]
    version = args.version or bump(latest, args.next)
    parse_version(version)
    tag = f"v{version}"

    if git("tag", "--list", tag):
        sys.exit(f"Tag {tag} already exists.")

    if not args.force:
        problems = []
        if git("status", "--porcelain"):
            problems.append("working tree is not clean")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            problems.append(f"HEAD is on {branch!r}, not 'main'")
        git("fetch", "origin", "--tags")
        if git("rev-parse", "HEAD") != git("rev-parse", "origin/main", check=False):
            problems.append("HEAD does not match origin/main (pull or push first)")
        # Re-read after the fetch so a tag pushed by someone else counts.
        latest = (existing_release_versions() or [None])[-1]
        if latest and sort_key(version) <= sort_key(latest):
            problems.append(f"version {version} is not greater than the latest release {latest}")
        if problems:
            sys.exit("Refusing to tag:\n  - " + "\n  - ".join(problems) + "\n(use --force to override)")

    message = f"Quest {tag}\n\n{body}"
    # whitespace cleanup keeps "#" lines (markdown headings) that the default strips.
    git("tag", "-a", tag, "--cleanup=whitespace", "-m", message)
    print(f"Created tag {tag} on {git('rev-parse', '--short', 'HEAD')}:\n{message}")

    if args.no_push:
        print(f"Not pushed. Push later with: git push origin {tag}")
        return
    push_tag(tag)
    print(f"Pushed {tag} to origin. The release workflow will publish the GitHub Release.")


if __name__ == "__main__":
    main()
