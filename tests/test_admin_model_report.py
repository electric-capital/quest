"""Tests for the admin Models report row projection (chat/routes/admin.py).

The per-model fold itself (``get_usage_by_model()``) is covered in
test_system_reports_analytics.py; this checks ``_model_user_view()``, the
top-user projection incl. the placeholder for a deleted user.
"""

from chat.routes.admin import _model_user_view


def test_model_user_view_labels_user_and_drops_ranking_key():
    usage = {
        "user_id": 7,
        "call_count": 12,
        "cost_usd": 3.5,
        "cost_source": "mixed",
        "known_cost_usd": 3.5,
    }
    info = {"id": 7, "email": "alice@example.com", "name": "Alice"}

    view = _model_user_view(usage, info)
    assert view == {
        "user_id": 7,
        "user_email": "alice@example.com",
        "user_name": "Alice",
        "call_count": 12,
        "cost_usd": 3.5,
        "cost_source": "mixed",
    }
    assert "known_cost_usd" not in view


def test_model_user_view_placeholders_deleted_user_and_null_name():
    usage = {
        "user_id": 8,
        "call_count": 1,
        "cost_usd": None,
        "cost_source": None,
        "known_cost_usd": 0.0,
    }
    assert _model_user_view(usage, None) == {
        "user_id": 8,
        "user_email": "",
        "user_name": "(unknown user)",
        "call_count": 1,
        "cost_usd": None,
        "cost_source": None,
    }
    # A user row with a NULL name yields an empty name, not "None".
    assert _model_user_view(usage, {"id": 8, "email": "b@x.io", "name": None})[
        "user_name"
    ] == ""
