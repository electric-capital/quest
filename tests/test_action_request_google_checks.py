"""Shared Google Services checks for action-request types
(chat/action_request_types/_google.py)."""

from unittest.mock import MagicMock

import pytest

from chat.action_request_types._google import (
    GOOGLE_NOT_CONNECTED_MESSAGE,
    _extract_google_api_error,
    require_google_scope,
)

_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_REAUTH = "Reconnect Google Services."


def _user(scopes=(_SCOPE,), token="ya29.x") -> dict:
    return {"google_services_oauth": {"access_token": token, "scopes": list(scopes)}}


class TestRequireGoogleScope:
    def test_connected_with_the_scope_passes(self):
        require_google_scope(_user(), _SCOPE, _REAUTH)

    @pytest.mark.parametrize("user", [{}, {"google_services_oauth": None}, _user(token="")])
    def test_not_connected(self, user):
        with pytest.raises(RuntimeError) as exc:
            require_google_scope(user, _SCOPE, _REAUTH)
        assert str(exc.value) == GOOGLE_NOT_CONNECTED_MESSAGE

    def test_missing_scope_raises_the_reauth_message(self):
        with pytest.raises(RuntimeError) as exc:
            require_google_scope(_user(scopes=["other"]), _SCOPE, _REAUTH)
        assert str(exc.value) == _REAUTH

    def test_any_one_of_several_scopes_is_enough(self):
        wanted = {"gmail.send", "mail.google.com"}
        require_google_scope(_user(scopes=["mail.google.com"]), wanted, _REAUTH)
        with pytest.raises(RuntimeError):
            require_google_scope(_user(scopes=["gmail.readonly"]), wanted, _REAUTH)

    def test_non_string_scope_entries_are_ignored(self):
        with pytest.raises(RuntimeError):
            require_google_scope(_user(scopes=[None, 3]), _SCOPE, _REAUTH)


def _response(body=None, *, text="", status=400, bad_json=False):
    response = MagicMock()
    response.status_code = status
    response.text = text
    if bad_json:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = body
    return response


class TestExtractGoogleApiError:
    def test_error_message(self):
        assert _extract_google_api_error(_response({"error": {"message": "Nope"}})) == "Nope"

    def test_first_nested_error_message(self):
        body = {"error": {"errors": [{"message": "Inner"}]}}
        assert _extract_google_api_error(_response(body)) == "Inner"

    def test_string_error(self):
        assert _extract_google_api_error(_response({"error": "invalid_grant"})) == "invalid_grant"

    def test_falls_back_to_text_then_status(self):
        assert _extract_google_api_error(_response(bad_json=True, text="boom")) == "boom"
        assert _extract_google_api_error(_response(bad_json=True, status=502)) == "HTTP 502"
        assert _extract_google_api_error(_response({}, status=500)) == "HTTP 500"
