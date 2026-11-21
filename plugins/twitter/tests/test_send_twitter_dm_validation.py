"""Format-validation tests for SendTwitterDmHandler.validate_params.

These tests pin the participant_id and dm_conversation_id format checks
plus the mutual-exclusivity rule added on top of the existing required-
field / unknown-key validation. The unknown-key behaviour is covered by
``test_unknown_top_level_key_rejected`` below; this file otherwise
focuses on the new regex checks so a malformed Twitter id surfaces as a
synchronous ``Invalid parameters: ...`` tool result on the model's same
turn instead of persisting to an action_requests row and exploding
inside ``execute()`` with an opaque Twitter HTTP 400.
"""

import pytest

from plugins.twitter.handlers import SendTwitterDmHandler


# ---------------------------------------------------------------------------
# Happy paths -- the validator must round-trip a well-formed payload
# without rewriting the id (Twitter ids round-trip exactly into the URL
# composition in execute()).
# ---------------------------------------------------------------------------


def test_accepts_dm_conversation_id_group_form():
    handler = SendTwitterDmHandler()
    out = handler.validate_params({"dm_conversation_id": "123456789", "message": "hi"})
    assert out == {"dm_conversation_id": "123456789", "message": "hi"}


def test_accepts_dm_conversation_id_one_to_one_form():
    handler = SendTwitterDmHandler()
    out = handler.validate_params({"dm_conversation_id": "123-456", "message": "hi"})
    assert out["dm_conversation_id"] == "123-456"


def test_accepts_participant_id_numeric_string():
    handler = SendTwitterDmHandler()
    out = handler.validate_params({"participant_id": "1234567890", "message": "hi"})
    assert out["participant_id"] == "1234567890"


def test_rejects_model_supplied_recipient_name():
    # recipient_name is server-injected by enrich_params_for_preview from
    # the delivery id; a model-chosen label would let the approval card
    # name one account while the DM goes to another.
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="recipient_name"):
        handler.validate_params({
            "participant_id": "1234567890",
            "message": "hi",
            "recipient_name": "@alice (Alice)",
        })


def test_strips_whitespace_in_ids():
    handler = SendTwitterDmHandler()
    out = handler.validate_params({
        "dm_conversation_id": "  123-456  ",
        "message": "hi",
    })
    assert out["dm_conversation_id"] == "123-456"

    out2 = handler.validate_params({
        "participant_id": "  1234567890  ",
        "message": "hi",
    })
    assert out2["participant_id"] == "1234567890"


# ---------------------------------------------------------------------------
# Existing rejections (regression guards).
# ---------------------------------------------------------------------------


def test_missing_message_rejected():
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="message"):
        handler.validate_params({"participant_id": "1234567890"})


def test_message_over_10000_chars_rejected():
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="10,000"):
        handler.validate_params({
            "participant_id": "1234567890",
            "message": "x" * 10001,
        })


def test_missing_both_ids_rejected():
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="Either dm_conversation_id or participant_id"):
        handler.validate_params({"message": "hi"})


def test_unknown_top_level_key_rejected():
    """Regression guard: keys not in the allow-list are rejected up front."""
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="Unknown parameter for send_twitter_dm") as exc:
        handler.validate_params({
            "participant_id": "1234567890",
            "message": "hi",
            "tweet_id": "t",
        })
    assert "tweet_id" in str(exc.value)


# ---------------------------------------------------------------------------
# New participant_id format rejections.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("participant_id", [
    pytest.param("@alice", id="at-handle"),
    pytest.param("https://twitter.com/alice", id="url"),
    # A `<smaller>-<larger>` shape is a dm_conversation_id, not a
    # participant_id; the model confusing the two is a real failure mode.
    pytest.param("123-456", id="with-dash"),
    pytest.param("+1234567890", id="sign"),
    pytest.param("1" * 21, id="too-many-digits"),
    pytest.param("1234abc567", id="letters"),
])
def test_rejects_malformed_participant_id(participant_id):
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="participant_id"):
        handler.validate_params({"participant_id": participant_id, "message": "hi"})


# ---------------------------------------------------------------------------
# New dm_conversation_id format rejections.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dm_conversation_id", [
    pytest.param("https://twitter.com/messages/123-456", id="url"),
    pytest.param("@alice", id="at-handle"),
    pytest.param("abc-456", id="letters"),
    pytest.param("12-34-56", id="three-segments"),
    pytest.param("-456", id="empty-first-segment"),
    pytest.param("123-", id="empty-second-segment"),
])
def test_rejects_malformed_dm_conversation_id(dm_conversation_id):
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="dm_conversation_id"):
        handler.validate_params({
            "dm_conversation_id": dm_conversation_id,
            "message": "hi",
        })


# ---------------------------------------------------------------------------
# New mutual-exclusivity rejection.
# ---------------------------------------------------------------------------


def test_rejects_both_ids_present():
    handler = SendTwitterDmHandler()
    with pytest.raises(ValueError, match="exactly one of dm_conversation_id or participant_id"):
        handler.validate_params({
            "dm_conversation_id": "123-456",
            "participant_id": "789",
            "message": "hi",
        })


# ---------------------------------------------------------------------------
# Error-message hygiene.
# ---------------------------------------------------------------------------


def test_error_message_truncates_long_offending_value():
    handler = SendTwitterDmHandler()
    long_bogus = "z" * 500  # no valid digits, > 100 chars
    with pytest.raises(ValueError) as exc:
        handler.validate_params({"participant_id": long_bogus, "message": "hi"})
    # The raw 500-char string must not be echoed back in full.
    assert "z" * 500 not in str(exc.value)
    assert "..." in str(exc.value)
