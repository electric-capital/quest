"""OAuth callback error pages must HTML-escape query-controlled input.

Every callback renders ``?error=`` into an HTML page BEFORE any state or
session check, so the page is reachable by anyone who can get a victim to
open a link.  Unescaped, the parameter is a reflected XSS in the app origin
(#279219): the injected script runs with the victim's ambient session cookie
and can e.g. POST /api/reset-api-key to obtain a reusable API key.
"""

from html import escape

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import google_login, google_services, popup_helpers, ramp
from plugins.github import oauth as github_oauth
from plugins.google_admin import oauth as google_admin_oauth
from plugins.m365 import oauth as m365_oauth
from plugins.slack import oauth as slack_oauth
from plugins.twitter import oauth as twitter_oauth

PAYLOAD = "</p><script>document.body.dataset.pwned='1'</script><p>"


def _client(*routers) -> TestClient:
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("router", "path"),
    [
        (google_login.router, "/auth/callback"),
        (google_services.router, "/auth/google-services/callback"),
        (ramp.router, "/auth/ramp/callback"),
        (slack_oauth.router, "/auth/slack/callback"),
        (github_oauth.router, "/auth/github/callback"),
        (twitter_oauth.router, "/auth/twitter/callback"),
        (m365_oauth.router, "/auth/m365/callback"),
        (google_admin_oauth.router, "/auth/google-admin/callback"),
    ],
)
def test_callback_error_param_is_html_escaped(router, path):
    client = _client(router)
    response = client.get(path, params={"error": PAYLOAD}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<script>" not in response.text
    assert escape(PAYLOAD) in response.text


def test_m365_error_description_is_html_escaped():
    client = _client(m365_oauth.router)
    response = client.get(
        "/auth/m365/callback",
        params={"error": "access_denied", "error_description": PAYLOAD},
        follow_redirects=False,
    )
    assert "<script>" not in response.text
    assert escape(PAYLOAD) in response.text


def test_popup_error_page_escapes_message_in_html_and_js():
    message = "</p><script>alert(1)</script>' + alert(2) + '"
    page = popup_helpers.generate_oauth_popup_error_page("GitHub", message)
    assert "<script>" not in page
    assert escape(message) in page
    # The onclick handler gets a JSON string literal, attribute-escaped, so
    # a quote in the message cannot break out of the JS string.
    assert "error: &quot;" in page
    assert "error: '" not in page
