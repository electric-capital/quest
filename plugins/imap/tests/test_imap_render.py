"""Unit tests for the IMAP plugin's MIME -> markdown rendering (plugins/imap/render.py)."""

import re
from email.message import EmailMessage

import pytest

import chat.storage as storage_mod
from api.gmail.helpers import _get_cached_url_mapping
from plugins.imap import render
from plugins.imap.render import (
    MAX_BODY_BYTES,
    attachment_payload,
    format_address_list,
    latest_url_cache_key,
    list_attachments,
    parse_message,
    render_batch_markdown,
    render_message_markdown,
    url_cache_key,
)

CONV_ID = "11111111-2222-3333-4444-555555555555"
LONG_URL = "https://tracking.example.com/click?id=" + "a" * 80
IDENTITY = dict(folder="INBOX", uid=42, uidvalidity=777, account="ada@mail.example.com")


@pytest.fixture
def chats_dir(tmp_path, monkeypatch):
    path = tmp_path / "chats"
    monkeypatch.setattr(storage_mod, "CHATS_DIR", path, raising=True)
    return path


def _render(raw, **kwargs):
    params = dict(IDENTITY)
    params.update(kwargs)
    return render_message_markdown(raw, **params)


def _plain(body="Hello Ada.\nSecond line.", **headers) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = headers.get("subject", "Quarterly report")
    msg["From"] = headers.get("from_", "Grace Hopper <grace@example.com>")
    msg["To"] = headers.get("to", "ada@mail.example.com")
    msg["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
    msg["Message-ID"] = "<abc@example.com>"
    msg.set_content(body)
    return msg.as_bytes()


def _sections(doc: str) -> tuple[str, str, str]:
    header, body, attachments = doc.split("\n\n---\n\n")
    return header, body, attachments


class TestCacheKeys:
    def test_url_cache_key_format(self):
        assert url_cache_key("a@b.c", "INBOX", 9, 3) == "imap:a@b.c:INBOX:9:3"

    def test_latest_key_cannot_collide_with_uidvalidity(self):
        assert latest_url_cache_key("a@b.c", "INBOX", 3) == "imap:a@b.c:INBOX:latest:3"


class TestPlainText:
    def test_header_block_and_body(self):
        doc = _render(_plain(), flags=["\\Seen", "\\Flagged"])
        header, body, attachments = _sections(doc)
        assert header.splitlines()[0] == "Subject: Quarterly report"
        assert "From: Grace Hopper <grace@example.com>" in header
        assert "To: ada@mail.example.com" in header
        assert "Date: Mon, 12 Oct 2026 10:00:00 +0000" in header
        assert "Message-ID: <abc@example.com>" in header
        assert "Account: ada@mail.example.com" in header
        assert "Folder: INBOX" in header
        assert "UID: 42" in header
        assert "UIDVALIDITY: 777" in header
        assert "Flags: \\Seen, \\Flagged" in header
        assert "Cc:" not in header
        assert "Replaced URL count" not in header
        assert body.startswith("## Body\n\nHello Ada.\nSecond line.")
        assert attachments == "## Attachments\n\n_No attachments._"

    def test_flags_omitted_when_none_and_none_marker_when_empty(self):
        assert "Flags:" not in _render(_plain())
        assert "Flags: (none)" in _render(_plain(), flags=[])

    def test_plain_text_urls_replaced_and_cached(self, chats_dir):
        raw = _plain(f"Click {LONG_URL} now, or https://x.io for short.")
        doc = _render(raw, conversation_id=CONV_ID)
        assert "Click (#1#) now" in doc
        assert "https://x.io" in doc
        assert LONG_URL not in doc
        assert "Replaced URL count: 1" in doc
        key = url_cache_key("ada@mail.example.com", "INBOX", 777, 42)
        assert _get_cached_url_mapping(CONV_ID, key) == {1: LONG_URL}
        latest = latest_url_cache_key("ada@mail.example.com", "INBOX", 42)
        assert _get_cached_url_mapping(CONV_ID, latest) == {1: LONG_URL}

    def test_explicit_cache_key(self, chats_dir):
        _render(_plain(f"see {LONG_URL}"), conversation_id=CONV_ID, cache_key="custom")
        assert _get_cached_url_mapping(CONV_ID, "custom") == {1: LONG_URL}

    def test_no_replacement_without_conversation_or_when_disabled(self, chats_dir):
        raw = _plain(f"see {LONG_URL}")
        assert LONG_URL in _render(raw)
        assert LONG_URL in _render(raw, conversation_id=CONV_ID, replace_urls=False)

    def test_cache_failure_leaves_urls_intact(self, chats_dir):
        doc = _render(_plain(f"see {LONG_URL}"), conversation_id="../../etc")
        assert LONG_URL in doc
        assert "Replaced URL count" not in doc


class TestHtml:
    HTML = (
        "<html><head><style>body{color:red}</style>"
        "<script>alert('pwn')</script></head><body>"
        "<p>Hi <b>Ada</b></p>"
        f"<p><a href=\"{LONG_URL}\">Read more</a></p>"
        "<img src=\"https://evil.example.com/pixel.gif?u=ada-tracking-identifier-long\" alt=\"pixel\">"
        "<iframe src=\"https://evil.example.com/frame\"></iframe>"
        "</body></html>"
    )

    def _html_only(self) -> bytes:
        msg = EmailMessage()
        msg["Subject"] = "Newsletter"
        msg["From"] = "news@example.com"
        msg.set_content(self.HTML, subtype="html")
        return msg.as_bytes()

    def test_html_only_fallback_with_url_cache(self, chats_dir):
        doc = _render(self._html_only(), conversation_id=CONV_ID)
        _, body, _ = _sections(doc)
        assert "Hi **Ada**" in body
        assert "[Read more](#1#)" in body
        assert "Replaced URL count: 2" in doc
        mapping = _get_cached_url_mapping(
            CONV_ID, url_cache_key("ada@mail.example.com", "INBOX", 777, 42)
        )
        assert mapping[1] == LONG_URL
        assert mapping[2].startswith("https://evil.example.com/pixel.gif")

    def test_html_does_not_leak_raw_markup(self):
        _, body, _ = _sections(_render(self._html_only()))
        for needle in ("<script", "alert(", "<style", "color:red", "<img", "<iframe", "<p>", "<html"):
            assert needle not in body
        # Remote image survives only as inert markdown text, never fetched.
        assert "![pixel](https://evil.example.com/pixel.gif" in body

    def test_include_html_emits_fenced_raw_html(self):
        doc = _render(self._html_only(), include_html=True)
        assert "## Body (HTML)\n\n```html\n" in doc
        assert "<script>alert('pwn')</script>" in doc

    def test_include_html_without_html_part_shows_plain(self):
        doc = _render(_plain("just text"), include_html=True)
        assert "## Body\n\njust text" in doc

    def test_deeply_nested_html_falls_back_to_text(self):
        msg = EmailMessage()
        msg.set_content("<div>" * 5000 + "deep text" + "</div>" * 5000, subtype="html")
        doc = _render(msg.as_bytes())
        assert "deep text" in doc
        assert "<div>" not in doc


class TestAlternativeAndAttachments:
    def _alternative_with_attachments(self) -> bytes:
        msg = EmailMessage()
        msg["Subject"] = "Mixed"
        msg["From"] = "a@example.com"
        msg.set_content("plain version")
        msg.add_alternative("<p>html version</p>", subtype="html")
        # inline image inside the html alternative via related
        html_part = msg.get_payload()[1]
        html_part.add_related(b"\x89PNG\r\n\x1a\nfake", maintype="image", subtype="png",
                              cid="<logo>", filename="logo.png",
                              disposition="inline")
        msg.make_mixed()
        msg.add_attachment(b"%PDF-1.4 data", maintype="application", subtype="pdf",
                           filename="résumé 2026.pdf")
        inner = EmailMessage()
        inner["Subject"] = "Forwarded"
        inner["From"] = "b@example.com"
        inner.set_content("inner body")
        msg.add_attachment(inner)
        return msg.as_bytes()

    def test_multipart_alternative_prefers_plain(self):
        doc = _render(self._alternative_with_attachments())
        _, body, _ = _sections(doc)
        assert "plain version" in body
        assert "html version" not in body

    def test_attachment_listing(self):
        raw = self._alternative_with_attachments()
        atts = list_attachments(parse_message(raw))
        assert [a.index for a in atts] == [1, 2, 3]
        assert [a.mime for a in atts] == ["image/png", "application/pdf", "message/rfc822"]
        assert atts[0].filename == "logo.png"
        assert atts[0].disposition == "inline"
        assert atts[0].size == len(b"\x89PNG\r\n\x1a\nfake")
        assert atts[1].filename == "résumé 2026.pdf"
        assert atts[1].size == len(b"%PDF-1.4 data")
        assert attachment_payload(atts[1]) == b"%PDF-1.4 data"
        assert atts[2].filename is None
        eml = attachment_payload(atts[2])
        assert b"Subject: Forwarded" in eml and b"inner body" in eml
        assert atts[2].size == len(eml)

        doc = _render(raw)
        _, body, section = _sections(doc)
        assert "inner body" not in body  # attached message is not descended into
        assert "- **logo.png** -- `image/png`, 12 bytes, inline, attachment_index: `1`" in section
        assert "**résumé 2026.pdf** -- `application/pdf`" in section
        assert "- **(unnamed)** -- `message/rfc822`" in section
        assert "attachment_index: `3`" in section

    def test_listing_is_deterministic(self):
        raw = self._alternative_with_attachments()
        first = [(a.index, a.filename, a.mime, a.size) for a in list_attachments(parse_message(raw))]
        second = [(a.index, a.filename, a.mime, a.size) for a in list_attachments(parse_message(raw))]
        assert first == second

    def test_rfc2231_filename(self):
        raw = (
            b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
            b"--XX\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--XX\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename*=utf-8''%E6%97%A5%E6%9C%AC.txt\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\naGVsbG8=\r\n--XX--\r\n"
        )
        atts = list_attachments(parse_message(raw))
        assert len(atts) == 1
        assert atts[0].filename == "日本.txt"
        assert atts[0].size == 5
        assert attachment_payload(atts[0]) == b"hello"

    def test_rfc2047_filename_and_text_attachment(self):
        raw = (
            b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
            b"--XX\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--XX\r\nContent-Type: text/csv\r\n"
            b"Content-Disposition: attachment; filename=\"=?utf-8?q?caf=C3=A9.csv?=\"\r\n\r\na,b\r\n"
            b"--XX--\r\n"
        )
        atts = list_attachments(parse_message(raw))
        assert [(a.filename, a.mime) for a in atts] == [("café.csv", "text/csv")]


class TestHeaders:
    def test_encoded_word_headers(self):
        raw = (
            b"Subject: =?utf-8?b?w4ljbGFpciDwn5qA?=\r\n"
            b"From: =?iso-8859-1?q?Andr=E9?= <andre@example.com>\r\n"
            b"To: ada@example.com\r\n\r\nbody\r\n"
        )
        doc = _render(raw)
        assert "Subject: Éclair \U0001f680" in doc
        assert "From: André <andre@example.com>" in doc

    def test_crlf_injection_flattened(self):
        raw = (
            b"Subject: =?utf-8?q?hello=0D=0AFrom:_attacker=0A=0A##_Body?=\r\n"
            b"From: a@example.com\r\n\r\nreal body\r\n"
        )
        doc = _render(raw)
        header, body, _ = _sections(doc)
        subject_line = header.splitlines()[0]
        assert subject_line.startswith("Subject: hello")
        assert "attacker" in subject_line
        assert "\r" not in doc
        assert all(re.match(r"^[A-Za-z][A-Za-z -]*: ", line) for line in header.splitlines())
        assert len([ln for ln in doc.splitlines() if ln.startswith("## Body")]) == 1
        assert "From: a@example.com" in header

    def test_missing_headers(self):
        doc = _render(b"\r\nno headers at all\r\n")
        assert "Subject: (no subject)" in doc
        assert "From: (unknown)" in doc
        assert "Date: (unknown)" in doc
        assert "Message-ID: (none)" in doc
        assert "no headers at all" in doc

    def test_raw_8bit_headers(self):
        doc = _render(b"Subject: caf\xc3\xa9 \xff\xfe\r\nFrom: x@y.z\r\n\r\nb\r\n")
        doc.encode("utf-8")  # no lone surrogates
        assert "Subject: café" in doc


class TestRobustness:
    def test_truncation_note(self):
        body = ("x" * 99 + "\n") * (MAX_BODY_BYTES // 100 + 500)
        raw = _plain(body)
        doc = _render(raw)
        _, body_section, _ = _sections(doc)
        assert "_[Body truncated: showing the first 2,097,152 of" in body_section
        assert len(body_section) < MAX_BODY_BYTES + 500

    def test_html_capped_before_conversion(self, monkeypatch):
        seen = {}

        def fake_convert(html):
            seen["len"] = len(html)
            return "converted"

        monkeypatch.setattr(render, "convert_html_to_markdown", fake_convert)
        msg = EmailMessage()
        msg.set_content("<p>" + "y" * (MAX_BODY_BYTES * 2) + "</p>", subtype="html")
        doc = _render(msg.as_bytes())
        assert seen["len"] <= MAX_BODY_BYTES
        assert "converted" in doc
        assert "Body truncated" in doc

    def test_broken_charset(self):
        raw = (
            b"Subject: x\r\nContent-Type: text/plain; charset=x-no-such-charset\r\n\r\n"
            b"caf\xc3\xa9 ok\r\n"
        )
        assert "café ok" in _render(raw)

    def test_non_text_codec_charset(self):
        raw = b"Content-Type: text/plain; charset=rot13\r\n\r\nhello\r\n"
        assert "hello" in _render(raw)

    def test_unknown_transfer_encoding(self):
        raw = (
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: x-weird\r\n\r\n"
            b"payload text\r\n"
        )
        assert "payload text" in _render(raw)

    def test_bad_base64(self):
        raw = (
            b"Content-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            b"!!!not base64@@@\r\n"
        )
        _render(raw)  # must not raise

    def test_missing_boundary(self):
        raw = b"Content-Type: multipart/mixed\r\n\r\nsome text without parts\r\n"
        doc = _render(raw)
        assert doc.startswith("Subject: (no subject)")

    def test_no_body(self):
        doc = _render(b"Subject: empty\r\n\r\n")
        assert "## Body\n\n_(empty body)_" in doc
        assert "_No attachments._" in doc

    def test_empty_and_garbage_input(self):
        assert "_(empty body)_" in _render(b"")
        _render(b"\x00\xff\xfe" * 1000)
        _render(None)

    def test_deeply_nested_multiparts_bounded(self):
        depth = 400
        raw = b"Content-Type: multipart/mixed; boundary=b0\r\n\r\n"
        for i in range(1, depth):
            raw += b"--b%d\r\nContent-Type: multipart/mixed; boundary=b%d\r\n\r\n" % (i - 1, i)
        raw += b"--b%d\r\nContent-Type: text/plain\r\n\r\ndeep\r\n" % (depth - 1)
        doc = _render(raw)
        assert "## Body" in doc

    def test_many_parts_bounded(self):
        msg = EmailMessage()
        msg.set_content("body")
        for i in range(800):
            msg.add_attachment(b"x", maintype="application", subtype="octet-stream",
                               filename=f"f{i}.bin")
        atts = list_attachments(parse_message(msg.as_bytes()))
        assert 0 < len(atts) < 500
        assert [a.index for a in atts] == list(range(1, len(atts) + 1))

    def test_blank_plain_falls_back_to_html(self):
        msg = EmailMessage()
        msg.set_content("   \n")
        msg.add_alternative("<p>the real content</p>", subtype="html")
        doc = _render(msg.as_bytes())
        assert "the real content" in doc


class TestAddressList:
    def test_header_string(self):
        assert format_address_list('"Ada L" <ada@x.io>, bob@y.io') == "Ada L <ada@x.io>, bob@y.io"

    def test_encoded_word_string(self):
        assert format_address_list("=?utf-8?q?Andr=C3=A9?= <a@x.io>") == "André <a@x.io>"

    def test_tuples_and_dicts(self):
        assert format_address_list([("Ada", "ada@x.io"), ("", "b@y.io")]) == "Ada <ada@x.io>, b@y.io"
        assert format_address_list([
            {"name": "Ada", "address": "ada@x.io"},
            {"name": None, "mailbox": "b", "host": "y.io"},
            {"name": "a@b.c", "email": "a@b.c"},
        ]) == "Ada <ada@x.io>, b@y.io, a@b.c"

    def test_empty_and_crlf(self):
        assert format_address_list(None) == ""
        assert format_address_list([]) == ""
        assert "\n" not in format_address_list([("Evil\r\nName", "e@x.io")])


class TestBatch:
    def test_batch_with_errors(self):
        doc = render_batch_markdown([
            {"uid": 1, "ok": True, "markdown": "MSG1"},
            {"uid": 2, "ok": False, "error_code": "not_found", "error_message": "gone\r\nnow"},
            {"uid": 3, "ok": True, "markdown": "MSG3"},
        ])
        assert doc.startswith("# IMAP messages batch\n\nFetched 2 message(s). 1 error(s).")
        assert "## Batch errors\n\n- **2** -- `not_found`: gone now" in doc
        assert doc.index("## Batch errors") < doc.index("MSG1")
        assert doc.endswith("MSG1\n\n===\n\nMSG3")

    def test_batch_all_ok(self):
        doc = render_batch_markdown([{"uid": 5, "ok": True, "markdown": "M"}])
        assert "## Batch errors" not in doc
        assert doc.endswith("\n\n===\n\nM")
