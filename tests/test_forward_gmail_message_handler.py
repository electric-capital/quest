"""Tests for the forward_gmail_message action_request handler.

Covers ``ForwardGmailMessageHandler.validate_params`` (unknown-key
rejection, message id / recipient / subject / note shapes),
``build_forward_message`` (the MIME graft over single-part, alternative,
related-with-cid and mixed-with-attachment originals, header stripping,
note escaping, undecodable charsets, attachment-only originals),
``validate_against_upstream`` (card enrichment, default subject, 404 and
missing-connection rejection) and ``execute`` (scope pre-check, the raw
fetch + media-upload send with a mocked ``make_authenticated_request``).
"""

import asyncio
import base64
from email import message_from_bytes, policy
from email.message import EmailMessage

import httpx
import pytest


def _run(coro):
    return asyncio.run(coro)


def _handler():
    from chat.action_request_types.forward_gmail_message import ForwardGmailMessageHandler
    return ForwardGmailMessageHandler()


def _build(raw, **kw):
    from chat.action_request_types.forward_gmail_message import build_forward_message
    defaults = dict(
        sender="me@example.com", to=["receipts@example.com"], cc=None,
        bcc=None, subject="Fwd: Your receipt", note=None,
    )
    defaults.update(kw)
    return build_forward_message(raw, **defaults)


def _roundtrip(msg: EmailMessage) -> EmailMessage:
    """Serialize and re-parse, as Gmail will see the bytes."""
    return message_from_bytes(msg.as_bytes(), policy=policy.default)


_ORIGINAL_HEADERS = {
    "From": "Acme Billing <billing@acme.test>",
    "To": "me@example.com",
    "Subject": "Your receipt",
    "Date": "Mon, 06 Apr 2026 10:00:00 -0700",
    "Message-ID": "<orig-123@acme.test>",
    "Received": "from mx.acme.test by mx.google.com",
    "DKIM-Signature": "v=1; a=rsa-sha256; d=acme.test; s=sel; bh=abc; b=def",
    "List-Unsubscribe": "<mailto:unsub@acme.test>",
}

_PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
_PDF = b"%PDF-1.4\n" + bytes(range(256)) * 8


def _single_part_plain() -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    for k, v in _ORIGINAL_HEADERS.items():
        msg[k] = v
    msg.set_content("Thanks for your order.\nTotal: $12.00\n")
    return msg.as_bytes()


_HTML = (
    "<html><head><style>.total{font-weight:bold}</style></head>"
    "<body style=\"margin:0\"><table><tr><td class=\"total\">Total</td>"
    "<td>$12.00 — café</td></tr></table>"
    "<img src=\"cid:logo.png\"><img src=\"https://acme.test/pixel.gif\"></body></html>"
)


def _alternative() -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    for k, v in _ORIGINAL_HEADERS.items():
        msg[k] = v
    msg.set_content("Total: $12.00 — café\n")
    msg.add_alternative(_HTML, subtype="html")
    return msg.as_bytes()


def _mixed_with_related_and_attachment() -> bytes:
    """mixed( alternative( plain, related( html, cid image ) ), pdf )."""
    msg = EmailMessage(policy=policy.SMTP)
    for k, v in _ORIGINAL_HEADERS.items():
        msg[k] = v
    msg.set_content("Total: $12.00\n")
    msg.add_alternative(_HTML, subtype="html")
    html_part = msg.get_payload()[1]
    html_part.add_related(_PNG, maintype="image", subtype="png", cid="<logo.png>",
                          filename="logo.png", disposition="inline")
    msg.add_attachment(_PDF, maintype="application", subtype="pdf",
                       filename="receipt.pdf")
    return msg.as_bytes()


def _attachment_only() -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    for k, v in _ORIGINAL_HEADERS.items():
        msg[k] = v
    msg.make_mixed()
    msg.add_attachment(_PDF, maintype="application", subtype="pdf",
                       filename="receipt.pdf")
    return msg.as_bytes()


def _bogus_charset_html() -> bytes:
    raw = (
        "From: billing@acme.test\r\nTo: me@example.com\r\nSubject: Your receipt\r\n"
        "Date: Mon, 06 Apr 2026 10:00:00 -0700\r\nMIME-Version: 1.0\r\n"
        "Content-Type: multipart/alternative; boundary=\"BB\"\r\n\r\n"
        "--BB\r\nContent-Type: text/plain; charset=\"us-ascii\"\r\n\r\nTotal: $12.00\r\n"
        "--BB\r\nContent-Type: text/html; charset=\"x-no-such-charset\"\r\n\r\n"
        "<p>Total: $12.00</p>\r\n--BB--\r\n"
    )
    return raw.encode("ascii")


def _text(part) -> str:
    """Decoded text content with the wire CRLFs normalized to LF."""
    return part.get_content().replace("\r\n", "\n")


def _walk_leaves(msg):
    return [p for p in msg.walk() if not p.is_multipart()]


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_handler_metadata():
    from db.models import ActionRequestType

    handler = _handler()
    assert handler.type_name == ActionRequestType.FORWARD_GMAIL_MESSAGE
    assert handler.display_name == "Forward Gmail Message"
    assert handler.approve_label == "Forward"
    assert handler.resolved_label == "Forwarded"


def test_handler_is_registered_and_in_tool_enum():
    from chat.action_request_types import get_handler
    from chat.llm.tool_schemas import TOOL_CALL_REGISTRY  # noqa: F401 -- import side effects

    assert get_handler("forward_gmail_message") is not None


def test_summary_snippet():
    handler = _handler()
    snippet = handler.summary_snippet({
        "to": ["receipts@example.com"], "original_subject": "Your receipt",
    })
    assert snippet == "Your receipt → receipts@example.com"
    assert handler.summary_snippet({}) == ""


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_params_minimal():
    out = _handler().validate_params({
        "message_id": " 18f3c2a9b7e1d4f0 ", "to": "receipts@example.com",
    })
    assert out == {"message_id": "18f3c2a9b7e1d4f0", "to": ["receipts@example.com"]}


def test_validate_params_rejects_unknown_field():
    with pytest.raises(ValueError, match="Unknown parameter for forward_gmail_message"):
        _handler().validate_params({
            "message_id": "abc", "to": "a@b.co", "original_from": "x",
        })


@pytest.mark.parametrize("params,match", [
    ({"to": "a@b.co"}, "message_id"),
    ({"message_id": "", "to": "a@b.co"}, "message_id"),
    ({"message_id": "<orig-123@acme.test>", "to": "a@b.co"}, "Gmail message id"),
    ({"message_id": "abc"}, "to"),
    ({"message_id": "abc", "to": ""}, "at least one"),
    ({"message_id": "abc", "to": "not-an-address"}, "invalid email"),
    ({"message_id": "abc", "to": "a@b"}, "invalid email"),
    ({"message_id": "abc", "to": 42}, "to must be"),
    ({"message_id": "abc", "to": ["a@b.co", 7]}, "entries must be strings"),
    ({"message_id": "abc", "to": "a@b.co", "cc": "nope"}, "cc contains an invalid"),
    ({"message_id": "abc", "to": "a@b.co", "subject": "   "}, "subject"),
    ({"message_id": "abc", "to": "a@b.co", "subject": 3}, "subject must be a string"),
    ({"message_id": "abc", "to": "a@b.co", "note": "x" * 4001}, "note must be at most"),
    ({"message_id": "abc", "to": "a@b.co", "note": ["x"]}, "note must be a string"),
    ({"message_id": "abc", "to": [f"u{i}@b.co" for i in range(21)]}, "at most 20"),
])
def test_validate_params_rejections(params, match):
    with pytest.raises(ValueError, match=match):
        _handler().validate_params(params)


def test_validate_params_recipient_shapes():
    out = _handler().validate_params({
        "message_id": "abc",
        "to": "Receipts <receipts@example.com>, other@example.com, receipts@example.com",
        "cc": ["boss@example.com"],
        "bcc": [],
        "subject": "  Fwd:   Your   receipt ",
        "note": "  Receipt for April.  ",
    })
    assert out["to"] == ["receipts@example.com", "other@example.com"]
    assert out["cc"] == ["boss@example.com"]
    assert "bcc" not in out
    assert out["subject"] == "Fwd: Your receipt"
    assert out["note"] == "Receipt for April."


def test_validate_params_drops_empty_note():
    out = _handler().validate_params({"message_id": "abc", "to": "a@b.co", "note": "   "})
    assert "note" not in out


# ---------------------------------------------------------------------------
# build_forward_message
# ---------------------------------------------------------------------------


def test_graft_single_part_plain():
    fwd = _roundtrip(_build(_single_part_plain(), note="See attached receipt."))
    assert fwd["From"] == "me@example.com"
    assert fwd["To"] == "receipts@example.com"
    assert fwd["Subject"] == "Fwd: Your receipt"
    for dropped in ("Received", "DKIM-Signature", "Message-ID", "List-Unsubscribe", "Date"):
        assert fwd[dropped] is None, dropped
    assert not fwd.is_multipart()
    body = _text(fwd)
    assert body.startswith("See attached receipt.\n\n---------- Forwarded message ---------\n")
    assert "From: Acme Billing <billing@acme.test>" in body
    assert "Date: Mon, 06 Apr 2026 10:00:00 -0700" in body
    assert "Subject: Your receipt" in body
    assert "To: me@example.com" in body
    assert body.endswith("Thanks for your order.\nTotal: $12.00\n")


def test_graft_alternative_keeps_html_and_inserts_after_body_tag():
    fwd = _roundtrip(_build(_alternative(), note="Note <b>with</b> markup\nline 2"))
    assert fwd.get_content_type() == "multipart/alternative"
    plain, html_part = _walk_leaves(fwd)
    assert plain.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "text/html"

    text = _text(plain)
    assert text.startswith("Note <b>with</b> markup\nline 2\n\n---------- Forwarded message")
    assert "Total: $12.00 — café" in text

    html = _text(html_part)
    # Original head/style/table survive untouched.
    assert "<style>.total{font-weight:bold}</style>" in html
    assert "<td>$12.00 — café</td>" in html
    assert '<img src="https://acme.test/pixel.gif">' in html
    # Attribution lands right after the <body ...> tag, with the note escaped.
    body_open = '<body style="margin:0">'
    assert html.index(body_open) < html.index("gmail_attr")
    assert html.index("gmail_attr") < html.index("<table>")
    assert "Note &lt;b&gt;with&lt;/b&gt; markup<br>line 2" in html
    assert "<b>with</b>" not in html
    assert "From: Acme Billing &lt;billing@acme.test&gt;" in html


def test_graft_mixed_keeps_attachments_and_cid_bytes():
    fwd = _roundtrip(_build(_mixed_with_related_and_attachment(), cc=["boss@example.com"],
                            bcc=["audit@example.com"]))
    assert fwd["Cc"] == "boss@example.com"
    assert fwd["Bcc"] == "audit@example.com"
    assert fwd.get_content_type() == "multipart/mixed"

    leaves = {p.get_content_type(): p for p in _walk_leaves(fwd)}
    assert set(leaves) == {"text/plain", "text/html", "image/png", "application/pdf"}

    assert "---------- Forwarded message ---------" in _text(leaves["text/plain"])
    html = _text(leaves["text/html"])
    assert "---------- Forwarded message ---------" in html
    assert '<img src="cid:logo.png">' in html

    png = leaves["image/png"]
    assert png["Content-ID"] == "<logo.png>"
    assert png.get_content_disposition() == "inline"
    assert png.get_content() == _PNG

    pdf = leaves["application/pdf"]
    assert pdf.get_filename() == "receipt.pdf"
    assert pdf.get_content() == _PDF

    # Structure: mixed( alternative( plain, related( html, png ) ), pdf )
    alt, pdf_part = fwd.get_payload()
    assert alt.get_content_type() == "multipart/alternative"
    assert pdf_part.get_content_type() == "application/pdf"
    related = alt.get_payload()[1]
    assert related.get_content_type() == "multipart/related"


def test_graft_attachment_only_original_is_wrapped():
    fwd = _roundtrip(_build(_attachment_only()))
    assert fwd.get_content_type() == "multipart/mixed"
    first, second = fwd.get_payload()
    assert first.get_content_type() == "text/plain"
    assert _text(first).startswith("---------- Forwarded message ---------")
    assert second.get_content_type() == "multipart/mixed"
    (pdf,) = second.get_payload()
    assert pdf.get_filename() == "receipt.pdf"
    assert pdf.get_content() == _PDF


def test_graft_undecodable_html_part_is_left_verbatim():
    fwd = _roundtrip(_build(_bogus_charset_html()))
    plain, html_part = _walk_leaves(fwd)
    assert "---------- Forwarded message ---------" in _text(plain)
    assert html_part.get_content_type() == "text/html"
    assert html_part.get_param("charset") == "x-no-such-charset"
    assert html_part.get_payload() == "<p>Total: $12.00</p>"


def test_graft_output_is_crlf_7bit_clean():
    data = _build(_alternative()).as_bytes()
    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert max(data) < 128


# ---------------------------------------------------------------------------
# validate_against_upstream
# ---------------------------------------------------------------------------


_USER = {
    "email": "me@example.com",
    "google_services_oauth": {
        "access_token": "tok",
        "scopes": ["https://www.googleapis.com/auth/gmail.compose"],
    },
}


def _full_message_json(subject="Your receipt"):
    return {
        "id": "18f3c2a9b7e1d4f0",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Acme Billing <billing@acme.test>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Mon, 06 Apr 2026 10:00:00 -0700"},
            ],
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"size": 12}},
                {"mimeType": "application/pdf", "filename": "receipt.pdf",
                 "body": {"attachmentId": "ATT1", "size": 2048 * 60}},
            ],
        },
    }


class _FakeRequests:
    """Stand-in for make_authenticated_request recording every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, client, user, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_requests(monkeypatch, responses):
    import chat.action_request_types.forward_gmail_message as mod
    fake = _FakeRequests(responses)
    monkeypatch.setattr(mod, "make_authenticated_request", fake)
    return fake


def test_upstream_enriches_card_and_defaults_subject(monkeypatch):
    fake = _patch_requests(monkeypatch, [httpx.Response(200, json=_full_message_json())])
    params = _handler().validate_params({"message_id": "18f3c2a9b7e1d4f0", "to": "r@x.co"})
    out = _run(_handler().validate_against_upstream(params, _USER))

    method, url, kwargs = fake.calls[0]
    assert method == "GET"
    assert url.endswith("/users/me/messages/18f3c2a9b7e1d4f0")
    assert kwargs["params"] == {"format": "full"}

    assert out["subject"] == "Fwd: Your receipt"
    assert out["original_subject"] == "Your receipt"
    assert out["original_from"] == "Acme Billing <billing@acme.test>"
    assert out["original_date"] == "Mon, 06 Apr 2026 10:00:00 -0700"
    assert out["original_to"] == "me@example.com"
    assert out["original_attachments"] == [
        {"filename": "receipt.pdf", "mime_type": "application/pdf", "size": 2048 * 60},
    ]


def test_upstream_keeps_existing_fwd_prefix_and_explicit_subject(monkeypatch):
    _patch_requests(monkeypatch, [httpx.Response(200, json=_full_message_json("FW: Your receipt"))])
    out = _run(_handler().validate_against_upstream(
        {"message_id": "abc", "to": ["r@x.co"]}, _USER,
    ))
    assert out["subject"] == "FW: Your receipt"

    _patch_requests(monkeypatch, [httpx.Response(200, json=_full_message_json())])
    out = _run(_handler().validate_against_upstream(
        {"message_id": "abc", "to": ["r@x.co"], "subject": "April receipt"}, _USER,
    ))
    assert out["subject"] == "April receipt"


def test_upstream_404_rejects_same_turn(monkeypatch):
    _patch_requests(monkeypatch, [httpx.Response(404, json={"error": {"message": "nope"}})])
    with pytest.raises(ValueError, match="was not found"):
        _run(_handler().validate_against_upstream({"message_id": "abc", "to": ["r@x.co"]}, _USER))


def test_upstream_requires_google_connection():
    with pytest.raises(ValueError, match="Google Services not connected"):
        _run(_handler().validate_against_upstream(
            {"message_id": "abc", "to": ["r@x.co"]}, {"email": "me@example.com"},
        ))


def test_upstream_network_failure_still_renders_card(monkeypatch):
    _patch_requests(monkeypatch, [httpx.ConnectError("boom")])
    out = _run(_handler().validate_against_upstream({"message_id": "abc", "to": ["r@x.co"]}, _USER))
    assert out["subject"] == "Fwd:"
    assert "original_subject" not in out


def test_render_preview_lists_recipients_original_and_attachments():
    fields = _run(_handler().render_preview({
        "to": ["r@x.co"], "cc": ["c@x.co"], "subject": "Fwd: Your receipt",
        "original_subject": "Your receipt", "original_from": "billing@acme.test",
        "original_date": "Mon, 06 Apr 2026 10:00:00 -0700",
        "original_attachments": [{"filename": "receipt.pdf", "size": 2048 * 60}],
        "note": "For April.",
    }))
    as_dict = {f["key"]: f["value"] for f in fields}
    assert as_dict["To"] == "r@x.co"
    assert as_dict["Cc"] == "c@x.co"
    assert as_dict["Subject"] == "Fwd: Your receipt"
    assert as_dict["Forwarding"] == (
        "Your receipt (from billing@acme.test on Mon, 06 Apr 2026 10:00:00 -0700)"
    )
    assert as_dict["Attachments"] == "receipt.pdf (120 KB)"
    assert as_dict["Note"] == "For April."


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _execute(params, user=_USER):
    return _run(_handler().execute(params, user, conversation_id=None, project_id=None))


def test_execute_requires_connection_and_send_scope():
    with pytest.raises(RuntimeError, match="Google Services not connected"):
        _execute({"message_id": "abc", "to": ["r@x.co"], "subject": "Fwd: x"},
                 {"email": "me@example.com"})
    no_scope = {"email": "me@example.com", "google_services_oauth": {
        "access_token": "tok", "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }}
    with pytest.raises(RuntimeError, match="send access requires reconnecting"):
        _execute({"message_id": "abc", "to": ["r@x.co"], "subject": "Fwd: x"}, no_scope)


def test_execute_fetches_raw_and_sends_via_media_upload(monkeypatch):
    raw_b64 = base64.urlsafe_b64encode(_mixed_with_related_and_attachment()).decode()
    fake = _patch_requests(monkeypatch, [
        httpx.Response(200, json={"id": "18f3c2a9b7e1d4f0", "raw": raw_b64}),
        httpx.Response(200, json={"id": "SENT1", "threadId": "THR1"}),
    ])
    result = _execute({
        "message_id": "18f3c2a9b7e1d4f0", "to": ["receipts@example.com"],
        "subject": "Fwd: Your receipt", "note": "April receipt.",
        "original_attachments": [{"filename": "receipt.pdf", "size": 1}],
    })

    (get_method, get_url, get_kwargs), (send_method, send_url, send_kwargs) = fake.calls
    assert get_method == "GET"
    assert get_url.endswith("/users/me/messages/18f3c2a9b7e1d4f0")
    assert get_kwargs["params"] == {"format": "raw"}

    assert send_method == "POST"
    assert send_url == "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
    assert send_kwargs["params"] == {"uploadType": "media"}
    assert send_kwargs["headers"]["Content-Type"] == "message/rfc822"

    sent = message_from_bytes(send_kwargs["content"], policy=policy.default)
    assert sent["From"] == "me@example.com"
    assert sent["To"] == "receipts@example.com"
    assert sent["Subject"] == "Fwd: Your receipt"
    assert sent["DKIM-Signature"] is None
    leaves = {p.get_content_type(): p for p in _walk_leaves(sent)}
    assert leaves["application/pdf"].get_content() == _PDF
    assert "April receipt." in _text(leaves["text/html"])

    assert result == {
        "success": True,
        "message_id": "SENT1",
        "thread_id": "THR1",
        "forwarded_message_id": "18f3c2a9b7e1d4f0",
        "to": ["receipts@example.com"],
        "cc": [],
        "bcc": [],
        "subject": "Fwd: Your receipt",
        "attachments": ["receipt.pdf"],
    }


def test_execute_reports_deleted_message_and_send_refusal(monkeypatch):
    _patch_requests(monkeypatch, [httpx.Response(404, json={"error": {"message": "gone"}})])
    with pytest.raises(RuntimeError, match="no longer exists"):
        _execute({"message_id": "abc", "to": ["r@x.co"], "subject": "Fwd: x"})

    raw_b64 = base64.urlsafe_b64encode(_single_part_plain()).decode()
    _patch_requests(monkeypatch, [
        httpx.Response(200, json={"raw": raw_b64}),
        httpx.Response(400, json={"error": {"message": "Recipient address required"}}),
    ])
    with pytest.raises(RuntimeError, match="Gmail refused to send the forward: Recipient address required"):
        _execute({"message_id": "abc", "to": ["r@x.co"], "subject": "Fwd: x"})
