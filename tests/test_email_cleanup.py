"""Tests for the email markdown cleanup logic in api.gmail.clean_email_markdown()."""

import pytest

from api.gmail import (
    clean_email_markdown,
    convert_html_to_markdown,
    _INVISIBLE_UNICODE_CODEPOINTS,
)


# ---------------------------------------------------------------------------
# Input -> expected output, one case per rule
# ---------------------------------------------------------------------------

_MIXED_NEWLINES_IN = (
    "# Newsletter Title\n\n"
    "First paragraph of content.\n\n"
    "Second paragraph.\n\n\n\n\n"
    "Third paragraph after excessive newlines.\n\n"
    "Fourth paragraph (double preserved).\n\n\n"
    "Fifth paragraph after triple."
)
_MIXED_NEWLINES_OUT = (
    "# Newsletter Title\n\n"
    "First paragraph of content.\n\n"
    "Second paragraph.\n\n"
    "Third paragraph after excessive newlines.\n\n"
    "Fourth paragraph (double preserved).\n\n"
    "Fifth paragraph after triple."
)


def _unchanged(text: str, id: str):
    return pytest.param(text, text, id=id)


@pytest.mark.parametrize("text, expected", [
    # Zero-width and other invisible characters are removed.
    pytest.param(
        "Hello\u200c \u200bWorld\u034f and\u00ad good\ufeff morning",
        "Hello World and good morning",
        id="common-zero-width-chars-removed",
    ),
    pytest.param("Hello\u200cWorld", "HelloWorld", id="visible-text-preserved"),
    # ... including inside markdown structure, without breaking it.
    pytest.param(
        "[Click\u200c here](https://example.com)",
        "[Click here](https://example.com)",
        id="inside-link-text",
    ),
    pytest.param(
        "![alt\u200b text](https://example.com/img.png)",
        "![alt text](https://example.com/img.png)",
        id="inside-image-alt",
    ),
    pytest.param("**bold\u034f text**", "**bold text**", id="inside-bold"),
    pytest.param("*italic\u00ad text*", "*italic text*", id="inside-italic"),
    pytest.param("# Heading\ufeff text", "# Heading text", id="inside-heading"),
    # Normal text passes through unchanged.
    _unchanged("Hello, world! This is a normal email.", "normal-ascii"),
    _unchanged("Bonjour le monde! Привет мир! 你好世界!", "normal-unicode"),
    _unchanged("Price: $19.99 — 50% off (limited time only)", "normal-punctuation"),
    _unchanged("", "empty-string"),
    # Runs of three or more newlines collapse to a paragraph break.
    pytest.param("line1\n\n\nline2", "line1\n\nline2", id="triple-newlines"),
    pytest.param("line1\n\n\n\n\n\nline2", "line1\n\nline2", id="six-newlines"),
    pytest.param("line1" + "\n" * 10 + "line2", "line1\n\nline2", id="ten-newlines"),
    # Paragraph breaks and single newlines are preserved.
    _unchanged("line1\n\nline2", "double-newlines-preserved"),
    _unchanged("para1\n\npara2\n\npara3", "multiple-paragraphs-preserved"),
    _unchanged("line1\nline2", "single-newline-preserved"),
    # Whitespace-only lines between newlines are collapsed.
    pytest.param("line1\n \n \n \nline2", "line1\n\nline2", id="spaces-between-newlines"),
    pytest.param("line1\n\t\n\t\n\t\nline2", "line1\n\nline2", id="tabs-between-newlines"),
    pytest.param(
        "line1\n \t \n\n \nline2", "line1\n\nline2",
        id="mixed-whitespace-between-newlines",
    ),
    # Realistic email markdown with mixed newline situations.
    pytest.param(_MIXED_NEWLINES_IN, _MIXED_NEWLINES_OUT, id="mixed-newline-sections"),
    # Trailing spaces and tabs are stripped from every line.
    pytest.param("line1   \nline2  \nline3", "line1\nline2\nline3", id="trailing-spaces"),
    pytest.param("line1\t\nline2\t\t\nline3", "line1\nline2\nline3", id="trailing-tabs"),
    pytest.param(
        "line1 \t \nline2\t \nline3", "line1\nline2\nline3",
        id="trailing-mixed-whitespace",
    ),
    # Unicode stripping and newline collapsing work together.
    pytest.param(
        "Hello\u200c World\n\n\n\n\nSecond\u200b paragraph\n\n\n\nThird\u034f line",
        "Hello World\n\nSecond paragraph\n\nThird line",
        id="combined-cleanup",
    ),
    pytest.param(
        "Hello\u200c   \n\n\n\nWorld\u200b   ", "Hello\n\nWorld",
        id="combined-with-trailing-whitespace",
    ),
])
def test_clean_email_markdown(text, expected):
    assert clean_email_markdown(text) == expected


class TestAllTargetCharactersRemoved:
    """Every character in the strip list is removed."""

    def test_all_target_characters_removed(self):
        # Build a string containing every invisible character between visible chars
        parts = []
        for i, cp in enumerate(sorted(_INVISIBLE_UNICODE_CODEPOINTS)):
            parts.append(f"a{chr(cp)}b")
        text = " ".join(parts)
        result = clean_email_markdown(text)
        # All invisible characters should be gone; only "ab" segments and spaces remain
        for cp in _INVISIBLE_UNICODE_CODEPOINTS:
            assert chr(cp) not in result, f"U+{cp:04X} was not removed"

    def test_only_invisible_chars_gives_empty(self):
        text = "".join(chr(cp) for cp in _INVISIBLE_UNICODE_CODEPOINTS)
        result = clean_email_markdown(text)
        assert result == ""


# ---------------------------------------------------------------------------
# End to end from HTML
# ---------------------------------------------------------------------------

class TestRealisticHtmlOutput:
    """Run realistic HTML through convert_html_to_markdown() then clean_email_markdown()."""

    def test_realistic_gmail_newsletter(self):
        # HTML representative of Gmail newsletters: hidden preview text with
        # ZWNJ padding, lots of <br> tags, nested tables producing blank lines.
        html = """
        <html><body>
        <div style="display:none;font-size:1px;color:#ffffff;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
            Preview text here\u200c\u200c\u200c\u200c\u200c\u200c\u200c\u200c\u200c\u200c
            \u200c\u200c\u200c\u200c\u200c\u200c\u200c\u200c\u200c\u200c
            &zwnj;&zwnj;&zwnj;&zwnj;&zwnj;&zwnj;&zwnj;&zwnj;
        </div>
        <br><br><br>
        <h1>Spring Sale!</h1>
        <br><br><br>
        <p>Don't miss our amazing deals.</p>
        <br><br><br><br>
        <p>Shop now at <a href="https://example.com/shop">our store</a>.</p>
        <br><br>
        <p>Thanks for reading.</p>
        </body></html>
        """

        markdown_raw = convert_html_to_markdown(html)

        # The raw markdown will contain ZWNJ characters and excessive newlines
        result = clean_email_markdown(markdown_raw)

        # No invisible Unicode characters should remain
        for cp in _INVISIBLE_UNICODE_CODEPOINTS:
            assert chr(cp) not in result, f"U+{cp:04X} still present in output"

        # No runs of 3+ newlines should remain
        import re
        triple_newlines = re.findall(r'\n{3,}', result)
        assert triple_newlines == [], f"Triple+ newlines found: {triple_newlines}"

        # Visible content should be preserved
        assert "Spring Sale!" in result
        assert "amazing deals" in result
        assert "Shop now" in result
        assert "our store" in result
        assert "Thanks for reading" in result
