"""The shared ``wait_handle_resolved`` publisher (chat/wait_handles/publish.py)."""

from chat.realtime import bus as _rt_bus
from chat.wait_handles.publish import publish_wait_handle_resolved

_ROW = {"id": "h-1", "conversation_id": "c-1", "kind": "slack_reply", "status": "accepted"}


def _capture(monkeypatch) -> list:
    events: list = []
    monkeypatch.setattr(
        _rt_bus, "publish_to_user",
        lambda user_id, event: events.append((user_id, event)),
    )
    return events


def test_event_is_built_from_the_row(monkeypatch):
    events = _capture(monkeypatch)
    publish_wait_handle_resolved(7, _ROW)
    assert events == [(7, {
        "type": "wait_handle_resolved",
        "conversation_id": "c-1",
        "handle_id": "h-1",
        "kind": "slack_reply",
        "status": "accepted",
    })]


def test_response_and_request_id_are_included_when_given(monkeypatch):
    events = _capture(monkeypatch)
    publish_wait_handle_resolved(7, _ROW, response={"answer": "yes"}, request_id=42)
    assert events[0][1]["response"] == {"answer": "yes"}
    assert events[0][1]["request_id"] == 42


def test_default_kind_applies_only_when_the_row_has_none(monkeypatch):
    events = _capture(monkeypatch)
    publish_wait_handle_resolved(7, _ROW, default_kind="action_request")
    publish_wait_handle_resolved(7, {**_ROW, "kind": None}, default_kind="action_request")
    publish_wait_handle_resolved(7, {**_ROW, "kind": None})
    assert [e["kind"] for _, e in events] == ["slack_reply", "action_request", ""]


def test_conversation_id_override(monkeypatch):
    events = _capture(monkeypatch)
    publish_wait_handle_resolved(7, _ROW, conversation_id="c-other")
    assert events[0][1]["conversation_id"] == "c-other"


def test_a_failing_publish_never_reaches_the_caller(monkeypatch):
    def boom(user_id, event):
        raise RuntimeError("bus down")

    monkeypatch.setattr(_rt_bus, "publish_to_user", boom)
    publish_wait_handle_resolved(7, _ROW)
