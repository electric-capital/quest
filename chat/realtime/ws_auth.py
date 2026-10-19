"""Authentication for WebSocket endpoints.

Shared by the persistent realtime socket (``chat/realtime/socket.py``) and
the live-dictation socket (``chat/routes/transcribe_live.py``): on open, the
signed session cookie is tried first, then an ``api_key`` query parameter.
Either yields the user dict, or the endpoint closes the socket with its own
auth-failed close code. The login-admission check (``check_user_allowed``)
runs here too, so a socket can never be opened by an account that could not
sign in.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import WebSocket

from auth.config import COOKIE_NAME
from auth.session import get_user_from_cookie
from db.user_store import get_user_by_api_key

from chat.auth import check_user_allowed

logger = logging.getLogger(__name__)


async def authenticate_websocket(
    websocket: WebSocket, api_key: Optional[str],
) -> Optional[dict]:
    """Return the authenticated user dict, or ``None`` to refuse the socket."""
    user: Optional[dict] = None

    signed_cookie = websocket.cookies.get(COOKIE_NAME)
    if signed_cookie:
        try:
            user = await get_user_from_cookie(signed_cookie)
        except Exception:
            logger.exception("[ws] cookie auth raised")
            user = None

    if user is None and api_key:
        try:
            user = await get_user_by_api_key(api_key)
        except Exception:
            logger.exception("[ws] api_key auth raised")
            user = None

    if user is None:
        return None

    if not check_user_allowed(user.get("email", "")):
        return None

    return user
