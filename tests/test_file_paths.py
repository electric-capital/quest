"""Unit tests for the chat:// / proj:// path parser
(chat/gemini_api/tool_handlers/file_paths.py)."""

import pytest

from chat.gemini_api.tool_handlers.file_paths import (
    SPACE_CHAT,
    SPACE_PROJECT,
    SpacePathError,
    format_space_path,
    parse_space_path,
)


@pytest.mark.parametrize("raw,expected", [
    ("chat://notes.md", (SPACE_CHAT, "notes.md")),
    ("proj://reports/q3.md", (SPACE_PROJECT, "reports/q3.md")),
    ("proj://reports/", (SPACE_PROJECT, "reports")),
    ("chat://./a//b/./c.txt", (SPACE_CHAT, "a/b/c.txt")),
    # Like the old tools: a backslash is a filename character and nothing
    # is stripped.
    ("chat://a\\b.txt", (SPACE_CHAT, "a\\b.txt")),
    ("chat://x ", (SPACE_CHAT, "x ")),
    ("chat:// spaced/name.md", (SPACE_CHAT, " spaced/name.md")),
    ("chat://.hidden", (SPACE_CHAT, ".hidden")),
    ("chat://project:plan.md", (SPACE_CHAT, "project:plan.md")),
])
def test_valid(raw, expected):
    assert parse_space_path(raw, allow_root=False) == expected


@pytest.mark.parametrize("raw,expected", [
    ("chat://", (SPACE_CHAT, "")),
    ("proj://", (SPACE_PROJECT, "")),
    ("proj://.", (SPACE_PROJECT, "")),
    ("proj://reports", (SPACE_PROJECT, "reports")),
])
def test_root_allowed(raw, expected):
    assert parse_space_path(raw, allow_root=True) == expected


@pytest.mark.parametrize("raw", ["chat://", "proj://", "proj://./"])
def test_root_refused_without_allow_root(raw):
    with pytest.raises(SpacePathError):
        parse_space_path(raw, allow_root=False)


@pytest.mark.parametrize("raw", [
    None, 5, ["chat://a"], {"path": "chat://a"},
    "notes.md", "/notes.md", "", "workspace/notes.md",
    "project://a", "file://a", "CHAT://a", "chat:/a", "chat:a",
    "chat:///etc/passwd", "proj://\\x",
    "chat://../x", "proj://a/../../b", "chat://a/..",
    "proj://a..b/c", "chat://notes..md", " chat://x", "\tproj://x",
    "chat://a\x00b",
])
def test_invalid(raw):
    with pytest.raises(SpacePathError) as excinfo:
        parse_space_path(raw, allow_root=True)
    err = excinfo.value
    assert isinstance(err, ValueError)
    assert err.code == "invalid_path"
    assert "chat://" in err.message and "proj://" in err.message


def test_format_round_trip():
    assert format_space_path(SPACE_PROJECT, "a/b.md") == "proj://a/b.md"
    assert parse_space_path(format_space_path(SPACE_CHAT, "x/y"), allow_root=False) == (
        SPACE_CHAT, "x/y",
    )
