"""Doc notes realtime (issue #187): the ``doc_notes_changed`` and
``doc_note_suggestions_changed`` envelopes, and ``doc_notes_changed``'s
fan-out to a doc's audience through chat/docs/events.py.

``bus.publish_to_user`` and ``bus.connected_user_ids`` are monkeypatched on
the bus singleton, as tests/test_docs_service.py's ``docs_env`` does: the
first records ``(user_id, event)``, the second says who is connected now.
"""

import pytest

from chat.docs import events as doc_events
from chat.realtime import bus
from chat.realtime import events as realtime_events

DOC_ID = "33333333-3333-4333-8333-333333333333"
CONVERSATION_ID = "44444444-4444-4444-8444-444444444444"
OWNER = 1
BOB = 2
CAROL = 3


def _doc(shares=(), *, owner_id=OWNER):
    return {
        "id": DOC_ID,
        "owner_id": owner_id,
        "project_id": None,
        "title": "Plan",
        "mode": "private",
        "updated_at": "2026-10-05T00:00:00",
        "shares": [
            {"id": i, "user_id": user_id, "permission": permission}
            for i, (user_id, permission) in enumerate(shares, start=1)
        ],
    }


def _state(instructions_version=4, memories_version=7):
    """A full ``get_notes_state()`` dict: the text must never travel."""
    return {
        "doc_id": DOC_ID,
        "instructions": "Keep the tone formal.",
        "has_instructions": True,
        "instructions_version": instructions_version,
        "instructions_updated_at": "2026-10-05T00:00:00",
        "instructions_updated_by": OWNER,
        "memories_version": memories_version,
        "memory_count": 2,
    }


def _event(instructions_version=4, memories_version=7):
    return {
        "type": "doc_notes_changed",
        "doc_id": DOC_ID,
        "instructions_version": instructions_version,
        "memories_version": memories_version,
    }


@pytest.fixture
def published(monkeypatch):
    sent: list[tuple[int, dict]] = []
    monkeypatch.setattr(bus, "publish_to_user", lambda user_id, ev: sent.append((user_id, ev)))
    monkeypatch.setattr(bus, "connected_user_ids", lambda: [])
    return sent


def connected(monkeypatch, *user_ids):
    """Pretend ``user_ids`` have a live WebSocket right now."""
    monkeypatch.setattr(bus, "connected_user_ids", lambda: list(user_ids))


def recipients(sent):
    return [user_id for user_id, _ev in sent]


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def test_doc_notes_changed_envelope():
    envelope = realtime_events.make_doc_notes_changed(DOC_ID, 3, 0)
    assert envelope == {
        "type": "doc_notes_changed",
        "doc_id": DOC_ID,
        "instructions_version": 3,
        "memories_version": 0,
    }
    # The wire order is pinned too.
    assert list(envelope) == ["type", "doc_id", "instructions_version", "memories_version"]


def test_doc_note_suggestions_changed_envelope():
    envelope = realtime_events.make_doc_note_suggestions_changed(CONVERSATION_ID, 2, 5)
    assert envelope == {
        "type": "doc_note_suggestions_changed",
        "conversation_id": CONVERSATION_ID,
        "pending_count": 2,
        "total_pending": 5,
    }
    assert list(envelope) == ["type", "conversation_id", "pending_count", "total_pending"]


def test_suggestions_envelope_is_the_project_memory_twin():
    """Same payload keys as ``project_memory_suggestions_changed``; only the
    type differs."""
    ours = realtime_events.make_doc_note_suggestions_changed(CONVERSATION_ID, 1, 1)
    twin = realtime_events.make_project_memory_suggestions_changed(CONVERSATION_ID, 1, 1)
    assert {**ours, "type": None} == {**twin, "type": None}


# ---------------------------------------------------------------------------
# publish_doc_notes_changed_for / _to
# ---------------------------------------------------------------------------


def test_publishers_are_exported():
    assert "publish_doc_notes_changed_for" in doc_events.__all__
    assert "publish_doc_notes_changed_to" in doc_events.__all__


def test_unshared_doc_reaches_the_owner_only(published, monkeypatch):
    connected(monkeypatch, BOB, CAROL)
    doc_events.publish_doc_notes_changed_for(_doc(), _state())
    assert published == [(OWNER, _event())]


def test_reaches_owner_and_every_direct_recipient_deduped_owner_first(published):
    """Read and write shares alike; a recipient listed twice (or the owner
    named by a share row) hears once, the owner first."""
    doc = _doc([(CAROL, "read"), (OWNER, "read"), (BOB, "write"), (CAROL, "write")])
    doc_events.publish_doc_notes_changed_for(doc, _state())
    assert recipients(published) == [OWNER, CAROL, BOB]
    assert all(ev == _event() for _u, ev in published)


def test_only_the_notes_event_and_only_the_versions(published):
    """No ``doc_changed`` / ``doc_list_changed`` with it (notes never bump
    ``updated_at``), and nothing of the state but its two versions."""
    doc_events.publish_doc_notes_changed_for(_doc([(BOB, "read")]), _state(9, 12))
    assert {ev["type"] for _u, ev in published} == {"doc_notes_changed"}
    for _u, ev in published:
        assert ev == _event(9, 12)
        assert "Keep the tone formal." not in repr(ev)


def test_everyone_share_reaches_every_connected_user(published, monkeypatch):
    doc = _doc([(None, "read"), (BOB, "write")])
    connected(monkeypatch, CAROL, BOB, OWNER, 777)
    doc_events.publish_doc_notes_changed_for(doc, _state())
    assert recipients(published) == [OWNER, BOB, CAROL, 777]
    assert all(ev == _event() for _u, ev in published)


def test_doc_without_shares_key_is_owner_only(published, monkeypatch):
    """Best-effort events never fail on a partial dict (``doc_audience``)."""
    connected(monkeypatch, BOB)
    doc = _doc()
    del doc["shares"]
    doc_events.publish_doc_notes_changed_for(doc, _state())
    assert recipients(published) == [OWNER]


def test_captured_audience_resolves_connected_users_at_publish_time(published, monkeypatch):
    audience = doc_events.doc_audience(_doc([(None, "read")]))
    connected(monkeypatch, 555)
    doc_events.publish_doc_notes_changed_to(audience, DOC_ID, 1, 2)
    assert published == [(OWNER, _event(1, 2)), (555, _event(1, 2))]


def test_explicit_audience(published):
    audience = doc_events.DocAudience(user_ids=(CAROL, BOB))
    doc_events.publish_doc_notes_changed_to(audience, DOC_ID, 0, 1)
    assert published == [(CAROL, _event(0, 1)), (BOB, _event(0, 1))]


def test_a_failure_for_one_user_is_swallowed(monkeypatch):
    sent: list[tuple[int, dict]] = []

    def publish(user_id, ev):
        if user_id == BOB:
            raise RuntimeError("queue gone")
        sent.append((user_id, ev))

    monkeypatch.setattr(bus, "publish_to_user", publish)
    connected(monkeypatch, 777)
    doc = _doc([(BOB, "write"), (CAROL, "read"), (None, "read")])
    doc_events.publish_doc_notes_changed_for(doc, _state())
    assert recipients(sent) == [OWNER, CAROL, 777]


def test_listing_connected_users_failing_still_reaches_explicit_users(published, monkeypatch):
    def boom():
        raise RuntimeError("bus down")

    monkeypatch.setattr(bus, "connected_user_ids", boom)
    doc_events.publish_doc_notes_changed_for(_doc([(None, "read"), (BOB, "read")]), _state())
    assert recipients(published) == [OWNER, BOB]
