"""Mechanical checks for the docs in docs/ and plugins/*/docs/.

These enforce the parts of docs/STYLE_GUIDE.md that a script can verify, so
the docs cannot quietly regrow the problems a manual cleanup removed:

- docs/index.md lists every doc exactly once, as one short table row;
- backticked repo file paths name files that exist;
- relative markdown links resolve, anchors included;
- no history markers (devplan / phase references, status sections);
- every doc stays within its word budget.

Pure file reads: no app imports, no fixtures.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
INDEX = DOCS_DIR / "index.md"
STYLE_GUIDE = DOCS_DIR / "STYLE_GUIDE.md"

# docs/STYLE_GUIDE.md "Docs Index": one row per doc, summary under this many
# characters. The index is pulled into every agent session via CLAUDE.md.
INDEX_SUMMARY_MAX_CHARS = 150

# docs/STYLE_GUIDE.md "Size Budgets". A doc over budget is split or trimmed
# before anything is added to it.
DEFAULT_WORD_BUDGET = 6000

# Docs that were already over DEFAULT_WORD_BUDGET when the budget was
# introduced. Each ceiling may only go DOWN: lower it when the doc shrinks,
# never raise it to make room for new text -- trim or split the doc instead.
OVER_BUDGET_CEILINGS = {
    "docs/architecture/action-requests.md": 10473,
    "docs/architecture/database.md": 10856,
    "docs/architecture/quest-docs.md": 8439,
    "docs/architecture/realtime.md": 7310,
    "docs/architecture/llm-providers.md": 7500,
    "docs/architecture/skill-library.md": 7000,
    "docs/architecture/inference-providers.md": 7000,
}

# Backticked spans ending in one of these are treated as repo file paths.
_PATH_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".json", ".md", ".sh",
    ".toml", ".yml", ".yaml", ".html", ".ini", ".txt",
)

# Docs commonly write frontend paths relative to frontend/ or frontend/src/
# and plugin paths relative to the plugin directory, so a path counts as
# existing if it resolves against any of these bases (or the doc's own
# directory / its parent).
_PATH_BASES = (ROOT, ROOT / "frontend", ROOT / "frontend" / "src", ROOT / "frontend" / "src" / "components")

# Build output and installed packages: docs may name files there, but they
# exist only after a build or install, so whether they resolve depends on the
# machine (the backend CI job never builds the frontend). Not checked.
_GENERATED_PREFIXES = ("frontend/dist/", "dist/", "frontend/node_modules/", "node_modules/", ".venv/")

_HISTORY_MARKERS = re.compile(
    r"devplans?[ /]?\d+"
    r"|\bphase \d"
    r"|not yet implemented"
    r"|future enhancements"
    r"|modified files"
    r"|testing considerations",
    re.IGNORECASE,
)

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_INDEX_ROW = re.compile(r"^\| \[[^\]]+\]\(([^)]+)\) \| (.+) \|$")


def _doc_files() -> list[Path]:
    return sorted([*DOCS_DIR.rglob("*.md"), *ROOT.glob("plugins/*/docs/*.md")])


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _prose_lines(path: Path) -> list[str]:
    """The doc's lines with fenced code blocks blanked (line numbers kept)."""
    lines: list[str] = []
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            lines.append("")
            continue
        lines.append("" if in_fence else line)
    return lines


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


_anchor_cache: dict[Path, set[str]] = {}


def _anchors(path: Path) -> set[str]:
    if path not in _anchor_cache:
        seen: dict[str, int] = {}
        anchors: set[str] = set()
        for line in _prose_lines(path):
            match = _HEADING.match(line)
            if not match:
                continue
            slug = _slug(match.group(2))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
        _anchor_cache[path] = anchors
    return _anchor_cache[path]


def _looks_like_repo_path(span: str) -> str | None:
    """Return the path a backticked span names, or None if it isn't one."""
    path = re.sub(r":\d+(-\d+)?$", "", span.strip()).split("::")[0]
    if not re.fullmatch(r"[\w.\-/]+", path):
        return None  # placeholders (<id>, {id}), globs, spaces, URLs with ':'
    if "/" not in path.rstrip("/"):
        return None  # a bare file name or a single runtime directory
    if path.startswith(("/", "~", "data/")) or (path.startswith(".") and not path.startswith(("./", "../"))):
        return None  # URL routes, home paths, runtime data, dot-dirs
    if path.startswith(_GENERATED_PREFIXES):
        return None  # build output, present only after a build
    if not path.endswith(_PATH_EXTENSIONS):
        return None
    return path


def test_index_lists_every_doc_once() -> None:
    linked: list[Path] = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        match = _INDEX_ROW.match(line)
        if match:
            linked.append((DOCS_DIR / match.group(1)).resolve())
    docs = {p.resolve() for p in _doc_files()} - {INDEX.resolve()}
    missing = sorted(_rel(p) for p in docs - set(linked))
    dangling = sorted(str(p) for p in set(linked) - docs)
    duplicated = sorted({_rel(p) for p in linked if linked.count(p) > 1})
    assert not missing, f"docs missing from docs/index.md: {missing}"
    assert not dangling, f"docs/index.md links to files that don't exist: {dangling}"
    assert not duplicated, f"docs listed more than once in docs/index.md: {duplicated}"


def test_index_rows_are_short() -> None:
    problems = []
    for number, line in enumerate(INDEX.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("| ["):
            continue
        match = _INDEX_ROW.match(line)
        if not match or "|" in match.group(2).replace("\\|", ""):
            problems.append(f"line {number}: not a two-cell row (unescaped '|'?)")
        elif len(match.group(2)) > INDEX_SUMMARY_MAX_CHARS:
            problems.append(
                f"line {number} ({match.group(1)}): summary is {len(match.group(2))} chars, "
                f"limit {INDEX_SUMMARY_MAX_CHARS} -- say what the doc covers, not what the code does"
            )
    assert not problems, "docs/index.md rows:\n" + "\n".join(problems)


def test_backticked_paths_exist() -> None:
    missing = []
    for doc in _doc_files():
        bases = (*_PATH_BASES, doc.parent, doc.parent.parent)
        for number, line in enumerate(_prose_lines(doc), 1):
            for span in _CODE_SPAN.findall(line):
                path = _looks_like_repo_path(span)
                if path and not any((base / path).exists() for base in bases):
                    missing.append(f"{_rel(doc)}:{number}: `{path}`")
    assert not missing, "docs name files that don't exist:\n" + "\n".join(missing)


def test_relative_links_resolve() -> None:
    broken = []
    for doc in _doc_files():
        for number, line in enumerate(_prose_lines(doc), 1):
            for target in _LINK.findall(_CODE_SPAN.sub("", line)):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, anchor = target.partition("#")
                linked = doc if not file_part else (doc.parent / file_part).resolve()
                if not linked.exists():
                    broken.append(f"{_rel(doc)}:{number}: {target} (no such file)")
                elif anchor and linked.suffix == ".md" and anchor not in _anchors(linked):
                    broken.append(f"{_rel(doc)}:{number}: {target} (no such heading)")
    assert not broken, "broken doc links:\n" + "\n".join(broken)


def test_no_history_markers() -> None:
    found = []
    for doc in _doc_files():
        if doc == STYLE_GUIDE:
            continue  # quotes the banned phrases as examples
        for number, line in enumerate(_prose_lines(doc), 1):
            for match in _HISTORY_MARKERS.finditer(line):
                found.append(f"{_rel(doc)}:{number}: {match.group(0)!r}")
    assert not found, (
        "history or status markers in docs (STYLE_GUIDE.md 'Write the End State'):\n" + "\n".join(found)
    )


def test_docs_within_word_budget() -> None:
    over = []
    for doc in _doc_files():
        if doc == INDEX:
            continue  # bounded per row by test_index_rows_are_short
        words = len(doc.read_text(encoding="utf-8").split())
        budget = OVER_BUDGET_CEILINGS.get(_rel(doc), DEFAULT_WORD_BUDGET)
        if words > budget:
            over.append(f"{_rel(doc)}: {words} words, budget {budget}")
    assert not over, (
        "docs over their word budget -- trim or split before adding (STYLE_GUIDE.md 'Size Budgets'):\n"
        + "\n".join(over)
    )


def test_over_budget_ceilings_are_tight() -> None:
    """A ceiling far above the doc's size is slack that lets it regrow."""
    slack = []
    for rel, ceiling in OVER_BUDGET_CEILINGS.items():
        words = len((ROOT / rel).read_text(encoding="utf-8").split())
        if words <= DEFAULT_WORD_BUDGET:
            slack.append(f"{rel}: {words} words is within the default budget; drop its entry")
        elif ceiling - words > 500:
            slack.append(f"{rel}: {words} words, ceiling {ceiling}; lower the ceiling")
    assert not slack, "OVER_BUDGET_CEILINGS needs updating:\n" + "\n".join(slack)
