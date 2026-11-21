"""Format-validation tests for SendSlackMessageHandler.validate_params.

These tests pin the channel_id and thread_ts format checks added on top
of the existing required-field / unknown-key validation. The unknown-
key behaviour is covered by ``test_slack_handlers.py``; this file
focuses on the new regex/prefix checks so a malformed Slack id surfaces
as a synchronous ``Invalid parameters: ...`` tool result on the model's same
turn instead of persisting to an action_requests row and exploding
inside ``execute()`` with an opaque Slack error.
"""

import pytest

from plugins.slack.handlers import SendSlackMessageHandler


# ---------------------------------------------------------------------------
# Happy paths -- the validator must round-trip a well-formed payload
# without rewriting the id (Slack ids are case-sensitive).
# ---------------------------------------------------------------------------


def test_accepts_channel_prefix():
    handler = SendSlackMessageHandler()
    out = handler.validate_params({"channel_id": "C0123456789", "message": "hi"})
    assert out == {"channel_id": "C0123456789", "message": "hi"}


@pytest.mark.parametrize("channel_id", [
    # U-prefix must round-trip exactly; the conversations.open branch
    # inside execute() relies on the leading character.
    pytest.param("U0123456789", id="user"),
    pytest.param("G0123456789", id="group"),
    pytest.param("D0123456789", id="dm"),
    pytest.param("W0123456789", id="enterprise-user"),
])
def test_accepts_other_prefixes_without_rewrite(channel_id):
    handler = SendSlackMessageHandler()
    out = handler.validate_params({"channel_id": channel_id, "message": "hi"})
    assert out["channel_id"] == channel_id


def test_accepts_thread_ts_canonical_shape():
    handler = SendSlackMessageHandler()
    out = handler.validate_params({
        "channel_id": "C01234567",
        "message": "hi",
        "thread_ts": "1727991234.000200",
    })
    assert out["thread_ts"] == "1727991234.000200"


def test_empty_thread_ts_is_treated_as_absent():
    handler = SendSlackMessageHandler()
    out = handler.validate_params({
        "channel_id": "C01234567",
        "message": "hi",
        "thread_ts": "   ",
    })
    # Whitespace-only thread_ts must NOT trigger a format error and
    # must NOT appear in the validated dict.
    assert "thread_ts" not in out


# ---------------------------------------------------------------------------
# Existing rejections (regression guards).
# ---------------------------------------------------------------------------


def test_missing_channel_id_rejected():
    handler = SendSlackMessageHandler()
    with pytest.raises(ValueError, match="channel_id"):
        handler.validate_params({"message": "hi"})


def test_missing_message_rejected():
    handler = SendSlackMessageHandler()
    with pytest.raises(ValueError, match="message"):
        handler.validate_params({"channel_id": "C0123456789"})


def test_message_over_3000_chars_rejected():
    handler = SendSlackMessageHandler()
    with pytest.raises(ValueError, match="3000"):
        handler.validate_params({
            "channel_id": "C0123456789",
            "message": "x" * 3001,
        })


# ---------------------------------------------------------------------------
# New channel_id format rejections.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel_id", [
    pytest.param("#general", id="hash-channel-alias"),
    pytest.param("@alice", id="at-user-alias"),
    pytest.param(
        "https://example.slack.com/archives/C0123456/p17279912340002",
        id="permalink-url",
    ),
    pytest.param("c01234567", id="lowercase-prefix"),
    pytest.param("X01234567", id="unknown-prefix"),
    # Right prefix, but the tail mixes lower/upper case characters,
    # which Slack ids never do.
    pytest.param("Cabc12345", id="mixed-case-tail"),
    # Below the {8,} tail floor.
    pytest.param("C1234", id="too-short"),
    pytest.param(1234567, id="raw-integer"),
])
def test_rejects_malformed_channel_id(channel_id):
    handler = SendSlackMessageHandler()
    with pytest.raises(ValueError, match="channel_id"):
        handler.validate_params({"channel_id": channel_id, "message": "hi"})


def test_error_message_truncates_long_offending_value():
    handler = SendSlackMessageHandler()
    long_bogus = "z" * 500  # no valid prefix, > 100 chars
    with pytest.raises(ValueError) as exc:
        handler.validate_params({"channel_id": long_bogus, "message": "hi"})
    # The raw 500-char string must not be echoed back in full.
    assert "z" * 500 not in str(exc.value)
    assert "..." in str(exc.value)


# ---------------------------------------------------------------------------
# New thread_ts format rejections.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("thread_ts", [
    pytest.param("1727991234", id="without-dot"),
    pytest.param("p1727991234000200", id="permalink-style"),
    pytest.param("1727991234.0002ab", id="non-digit-microseconds"),
])
def test_rejects_malformed_thread_ts(thread_ts):
    handler = SendSlackMessageHandler()
    with pytest.raises(ValueError, match="thread_ts"):
        handler.validate_params({
            "channel_id": "C01234567",
            "message": "hi",
            "thread_ts": thread_ts,
        })
