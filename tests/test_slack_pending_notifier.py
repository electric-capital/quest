"""Tests for the Slack pending-request notifier daemon (chat/slack_notifier.py).

Exercises one poll cycle (``_poll_once``) with all collaborators patched at
their source modules (the notifier imports them lazily): the cross-user
open-request and pending-suggestion summary queries, the user lookup, the
bot-token loader, and the actual Slack send. No database or network is
touched. The merged counts and the DM wording are covered in
tests/test_memory_inbox.py.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from chat import slack_notifier
from chat.slack_notifier import (
    DEFAULT_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    resolve_interval_minutes,
)


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    slack_notifier._last_notified.clear()
    yield
    slack_notifier._last_notified.clear()


def _user(user_id=1, settings=None, connected=True):
    user = {"id": user_id, "email": f"user{user_id}@example.com"}
    if settings is not None:
        user["settings"] = settings
    if connected:
        user["service_credentials"] = {
            "slack": {"oauth_blob": {"access_token": "xoxp-x", "user_id": f"U{user_id:04d}"}},
        }
    return user


def _summary(user_id=1, open_count=2, age_minutes=DEFAULT_INTERVAL_MINUTES + 5):
    # Naive UTC, matching what the SQLite DateTime column returns.
    oldest = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=age_minutes,
    )
    return {"user_id": user_id, "open_count": open_count, "oldest_created_at": oldest}


def _suggestion_summary(user_id=1, pending_count=1, age_minutes=DEFAULT_INTERVAL_MINUTES + 5):
    oldest = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        minutes=age_minutes,
    )
    return {"user_id": user_id, "pending_count": pending_count, "oldest_created_at": oldest}


def _poll(summaries, users, bot_token="xoxb-test", suggestions=()):
    """Run one poll cycle; returns the mocked ``_send_reminder``."""
    users_by_id = {u["id"]: u for u in users}

    async def get_user(user_id):
        return users_by_id.get(user_id)

    if isinstance(bot_token, Exception):
        token_loader = lambda: (_ for _ in ()).throw(bot_token)  # noqa: E731
    else:
        token_loader = lambda: bot_token  # noqa: E731

    send = AsyncMock()
    with patch(
        "db.action_request_store.list_open_request_summaries",
        AsyncMock(return_value=summaries),
    ), patch(
        "chat.memory_inbox.list_pending_summaries",
        AsyncMock(return_value=list(suggestions)),
    ), patch("db.user_store.get_user_by_id", AsyncMock(side_effect=get_user)), \
         patch("auth.config.load_slack_bot_token", token_loader), \
         patch.object(slack_notifier, "_send_reminder", send):
        asyncio.run(slack_notifier._poll_once())
    return send


def test_reminds_user_with_old_open_requests():
    send = _poll([_summary(user_id=1, open_count=3)], [_user(1)])
    send.assert_awaited_once_with("xoxb-test", "U0001", 3, 0)
    assert 1 in slack_notifier._last_notified


def test_reminds_about_suggested_memories_alone():
    send = _poll([], [_user(1)], suggestions=[_suggestion_summary(user_id=1, pending_count=2)])
    send.assert_awaited_once_with("xoxb-test", "U0001", 0, 2)


def test_young_suggestions_not_yet_reminded():
    send = _poll([], [_user(1)], suggestions=[_suggestion_summary(user_id=1, age_minutes=10)])
    send.assert_not_awaited()


def test_old_suggestion_makes_a_young_request_due():
    # The age gate looks at the oldest unanswered item of either kind.
    send = _poll(
        [_summary(user_id=1, open_count=1, age_minutes=10)], [_user(1)],
        suggestions=[_suggestion_summary(user_id=1, pending_count=2)],
    )
    send.assert_awaited_once_with("xoxb-test", "U0001", 1, 2)


def test_throttles_within_interval():
    summaries = [_summary(user_id=1)]
    users = [_user(1)]
    send = _poll(summaries, users)
    send.assert_awaited_once()
    # Second cycle right away: still open, but recently notified.
    send2 = _poll(summaries, users)
    send2.assert_not_awaited()


def test_notifies_again_after_interval_elapses():
    _poll([_summary(user_id=1)], [_user(1)])
    slack_notifier._last_notified[1] = datetime.now(timezone.utc) - timedelta(
        minutes=DEFAULT_INTERVAL_MINUTES + 1,
    )
    send = _poll([_summary(user_id=1)], [_user(1)])
    send.assert_awaited_once()


def test_young_requests_not_yet_reminded():
    send = _poll([_summary(user_id=1, age_minutes=10)], [_user(1)])
    send.assert_not_awaited()


def test_disabled_setting_skips_user():
    user = _user(1, settings={"slack_pending_notifications_enabled": False})
    send = _poll([_summary(user_id=1)], [user])
    send.assert_not_awaited()


def test_custom_interval_shortens_both_gates():
    user = _user(1, settings={"slack_pending_notification_interval_minutes": 5})
    send = _poll([_summary(user_id=1, age_minutes=6)], [user])
    send.assert_awaited_once()
    slack_notifier._last_notified[1] = datetime.now(timezone.utc) - timedelta(minutes=6)
    send2 = _poll([_summary(user_id=1, age_minutes=6)], [user])
    send2.assert_awaited_once()


def test_slack_not_connected_skips_user():
    send = _poll([_summary(user_id=1)], [_user(1, connected=False)])
    send.assert_not_awaited()


def test_missing_bot_token_is_quiet():
    send = _poll(
        [_summary(user_id=1)], [_user(1)],
        bot_token=HTTPException(status_code=500, detail="not configured"),
    )
    send.assert_not_awaited()


def test_resolved_requests_clear_throttle_state():
    slack_notifier._last_notified[1] = datetime.now(timezone.utc)
    send = _poll([], [])
    send.assert_not_awaited()
    assert slack_notifier._last_notified == {}


def test_send_failure_still_records_attempt():
    users = [_user(1)]
    users_by_id = {u["id"]: u for u in users}

    async def get_user(user_id):
        return users_by_id.get(user_id)

    with patch(
        "db.action_request_store.list_open_request_summaries",
        AsyncMock(return_value=[_summary(user_id=1)]),
    ), patch("chat.memory_inbox.list_pending_summaries", AsyncMock(return_value=[])), \
         patch("db.user_store.get_user_by_id", AsyncMock(side_effect=get_user)), \
         patch("auth.config.load_slack_bot_token", lambda: "xoxb-test"), \
         patch.object(
             slack_notifier, "_send_reminder",
             AsyncMock(side_effect=RuntimeError("slack down")),
         ):
        asyncio.run(slack_notifier._poll_once())
    # The failed attempt is throttled like a success so the next poll tick
    # doesn't hammer Slack.
    assert 1 in slack_notifier._last_notified


def test_one_user_failure_does_not_block_others():
    users = {1: _user(1), 2: _user(2)}

    async def get_user(user_id):
        return users.get(user_id)

    send = AsyncMock(side_effect=[RuntimeError("boom"), None])
    with patch(
        "db.action_request_store.list_open_request_summaries",
        AsyncMock(return_value=[_summary(user_id=1), _summary(user_id=2)]),
    ), patch("chat.memory_inbox.list_pending_summaries", AsyncMock(return_value=[])), \
         patch("db.user_store.get_user_by_id", AsyncMock(side_effect=get_user)), \
         patch("auth.config.load_slack_bot_token", lambda: "xoxb-test"), \
         patch.object(slack_notifier, "_send_reminder", send):
        asyncio.run(slack_notifier._poll_once())
    assert send.await_count == 2


def test_resolve_interval_minutes_defaults_and_clamps():
    assert resolve_interval_minutes({}) == DEFAULT_INTERVAL_MINUTES
    assert resolve_interval_minutes(
        {"slack_pending_notification_interval_minutes": None}
    ) == DEFAULT_INTERVAL_MINUTES
    assert resolve_interval_minutes(
        {"slack_pending_notification_interval_minutes": 90}
    ) == 90
    assert resolve_interval_minutes(
        {"slack_pending_notification_interval_minutes": 1}
    ) == MIN_INTERVAL_MINUTES
    assert resolve_interval_minutes(
        {"slack_pending_notification_interval_minutes": 999999}
    ) == MAX_INTERVAL_MINUTES
    # Junk types fall back to the default rather than raising.
    assert resolve_interval_minutes(
        {"slack_pending_notification_interval_minutes": "60"}
    ) == DEFAULT_INTERVAL_MINUTES
    assert resolve_interval_minutes(
        {"slack_pending_notification_interval_minutes": True}
    ) == DEFAULT_INTERVAL_MINUTES


def test_reminder_copy_pluralizes():
    with patch("config.server_config.get_app_base_url", lambda: ""):
        fallback_one, _ = slack_notifier._build_reminder(1)
        fallback_many, blocks = slack_notifier._build_reminder(4)
    assert "1 unanswered request " in fallback_one
    assert "4 unanswered requests " in fallback_many
    assert blocks[0]["type"] == "section"
    assert "4 unanswered requests" in blocks[0]["text"]["text"]


def test_reminder_links_to_the_inbox_when_base_url_configured():
    with patch(
        "config.server_config.get_app_base_url",
        lambda: "https://quest.example.com",
    ):
        fallback, blocks = slack_notifier._build_reminder(2)
    assert "<https://quest.example.com/inbox|Open your inbox>" in blocks[0]["text"]["text"]
    # The plain-text fallback must stay free of mrkdwn link syntax.
    assert "<" not in fallback


def test_reminder_has_no_link_without_base_url():
    with patch("config.server_config.get_app_base_url", lambda: ""):
        _, blocks = slack_notifier._build_reminder(2)
    assert "/inbox" not in blocks[0]["text"]["text"]
    assert "<" not in blocks[0]["text"]["text"]
