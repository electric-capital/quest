"""Baseline browser security headers for every HTTP response.

Quest never needs to be framed by another page and never relies on the
browser guessing a content type, so both are switched off for the whole
app instead of route by route:

* ``X-Content-Type-Options: nosniff`` -- a response is only ever treated as
  the type it declares (file downloads are served as ``octet-stream`` or a
  fixed image type; this keeps a browser from second-guessing that).
* ``X-Frame-Options: DENY`` and ``Content-Security-Policy: frame-ancestors
  'none'`` -- no page, same-site or not, can put the app in a frame and
  trick a signed-in user into clicking an approval card (clickjacking).

A header a route already set is left alone, so a route can still choose a
stricter or different value.
"""

from __future__ import annotations

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware adding ``SECURITY_HEADERS`` to HTTP responses.

    Pure ASGI (not ``BaseHTTPMiddleware``) so streaming responses and file
    downloads pass through untouched; WebSocket and lifespan scopes are not
    wrapped at all.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in SECURITY_HEADERS
                    if name not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
