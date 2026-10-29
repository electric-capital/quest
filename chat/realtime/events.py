"""Typed envelope helpers for events published on the persistent WS bus.

Centralising envelope construction here keeps the wire format consistent
across the many publish sites (REST handlers in chat/, the model loop, the
Slack runtime) and makes it cheap to extend the protocol later.

Each helper returns a plain ``dict`` so callers pass it straight to
``bus.publish_to_user`` or ``bus.publish_to_conversation``.
"""

from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# Per-user globals (Phase 1)
# ---------------------------------------------------------------------------


def make_request_count_changed(counts: dict[str, int]) -> dict[str, Any]:
    """Envelope for ``request_count_changed``.

    ``counts`` is the same shape as the REST ``GET /app/api/action-requests/counts``
    payload (``{"all": N, "open": N, "executed": N, "denied": N}``).
    """
    return {
        "type": "request_count_changed",
        "counts": counts,
    }


def make_conversation_list_changed(
    conversation_id: str,
    action: str,
) -> dict[str, Any]:
    """Envelope for ``conversation_list_changed``.

    ``action`` is one of: ``created``, ``archived``, ``unarchived``,
    ``renamed``, ``model_changed``, ``moved_to_project``.
    """
    return {
        "type": "conversation_list_changed",
        "conversation_id": conversation_id,
        "action": action,
    }


FILE_LIST_SCOPES = frozenset({"conversation", "project"})


def make_file_list_changed(
    conversation_id: Optional[str],
    project_id: Optional[str],
    scope: str,
) -> dict[str, Any]:
    """Envelope for ``file_list_changed``.

    Per-user global with a payload ``conversation_id`` so a tab can decide
    whether to refresh: the event is global because a project workspace
    write must reach sibling-conversation tabs that don't subscribe to the
    triggering ``conversation_id``.

    ``scope`` names the workspace whose listing changed and is set by the
    publisher -- it is NOT derived from ``project_id``:

    * ``"conversation"`` -- the conversation workspace of
      ``conversation_id`` (also for a project conversation, whose
      ``project_id`` is still carried);
    * ``"project"`` -- the shared workspace of ``project_id``;
      ``conversation_id`` is the triggering conversation, or None for a
      write through the project file routes.

    The FE filter keys off ``scope`` plus the matching id.

    Raises:
        ValueError: ``scope`` is not one of ``FILE_LIST_SCOPES``.
    """
    if scope not in FILE_LIST_SCOPES:
        raise ValueError(f"invalid file_list_changed scope: {scope!r}")
    return {
        "type": "file_list_changed",
        "conversation_id": conversation_id,
        "project_id": project_id,
        "scope": scope,
    }


def make_routine_list_changed(project_id: str) -> dict[str, Any]:
    """Envelope for ``routine_list_changed``.

    Per-user global published when an agent-driven routine write (the
    ``create_routine`` / ``edit_routine`` action requests) lands, so the
    sidebar's cached per-project routine list refreshes without a page
    reload. Deliberately carries no ``conversation_id`` -- the mutation is
    project-scoped and the event must reach every tab.
    """
    return {
        "type": "routine_list_changed",
        "project_id": project_id,
    }


def make_doc_list_changed() -> dict[str, Any]:
    """Envelope for ``doc_list_changed``.

    Per-user global published after any Quest Docs mutation that can change
    a list view (create, rename, delete, and every body/asset
    write, which bumps ``updated_at`` and so the sort order). Carries no
    ids: the sidebar and the All Docs view simply re-fetch their loaded
    window. Published through chat/docs/events.py only.
    """
    return {"type": "doc_list_changed"}


def make_doc_changed(doc_id: str, updated_at: Optional[str]) -> dict[str, Any]:
    """Envelope for ``doc_changed``.

    Per-user global published after every Quest Docs body or asset write so
    an open viewer of ``doc_id`` re-fetches. ``updated_at`` is the row's new
    ISO timestamp (the optimistic-concurrency token), letting a tab skip a
    re-fetch when it already shows that version; ``None`` (sent on delete,
    project delete and share changes) means the doc's existence or the
    receiver's access changed -- always re-fetch.
    """
    return {
        "type": "doc_changed",
        "doc_id": doc_id,
        "updated_at": updated_at,
    }


def make_doc_notes_changed(
    doc_id: str, instructions_version: int, memories_version: int,
) -> dict[str, Any]:
    """Envelope for ``doc_notes_changed``.

    Per-user global published after every change to a doc's notes (its
    owner's instructions or one of its memories, an accepted note
    suggestion included) so an open Notes panel of ``doc_id`` re-fetches
    them. ``instructions_version`` and ``memories_version`` are the doc's
    notes counters after the change, letting a panel skip a re-fetch when
    it already shows them. Notes never bump the doc's ``updated_at``, so
    this event never comes with a ``doc_changed`` and the body viewer
    ignores it. Carries no notes text. Published through
    chat/docs/events.py only, to the same audience as ``doc_changed``.
    """
    return {
        "type": "doc_notes_changed",
        "doc_id": doc_id,
        "instructions_version": instructions_version,
        "memories_version": memories_version,
    }


def make_table_list_changed() -> dict[str, Any]:
    """Envelope for ``table_list_changed``.

    Per-user global published after any Quest Tables mutation that can
    change a list view (create, delete, schema change, restore, and every
    row write, which bumps ``updated_at`` and so the sort order). Carries
    no ids: list views simply re-fetch their loaded window. Published
    through chat/tables/events.py only.
    """
    return {"type": "table_list_changed"}


def make_table_changed(
    table_id: str, updated_at: Optional[str], schema_version: Optional[int],
) -> dict[str, Any]:
    """Envelope for ``table_changed``.

    Per-user global published after every Quest Tables row write, schema
    change or restore so an open viewer of ``table_id`` re-fetches its
    grid (and its schema when ``schema_version`` moved). ``updated_at`` is
    the row's new ISO timestamp; ``None`` (sent on delete, project delete
    and share changes) means the table's existence or the receiver's
    access changed -- always re-fetch; ``schema_version`` may then be
    ``None`` too.
    """
    return {
        "type": "table_changed",
        "table_id": table_id,
        "updated_at": updated_at,
        "schema_version": schema_version,
    }


def make_memory_suggestions_changed(
    conversation_id: str,
    pending_count: int,
    total_pending: int,
) -> dict[str, Any]:
    """Envelope for ``memory_suggestions_changed``.

    Per-user global published whenever a conversation's pending
    memory-suggestion set changes (the ``suggest_memory`` tool registered
    one, or the user accepted / dismissed one from any tab). Carries the
    conversation's new pending count so the composer bubble updates
    without a fetch (the open panel re-fetches the list), and the user's
    pending count across all conversations (``total_pending``) for the
    sidebar inbox badge and the inbox's Memories tab.
    """
    return {
        "type": "memory_suggestions_changed",
        "conversation_id": conversation_id,
        "pending_count": pending_count,
        "total_pending": total_pending,
    }


def make_project_memory_suggestions_changed(
    conversation_id: str,
    pending_count: int,
    total_pending: int,
) -> dict[str, Any]:
    """Envelope for ``project_memory_suggestions_changed``.

    The project-memory twin of ``memory_suggestions_changed``: published
    when a conversation's pending ``suggest_project_memory`` set changes,
    driving the composer's separate project-memory bubble; ``total_pending``
    is the user's pending project-memory count across all conversations.
    """
    return {
        "type": "project_memory_suggestions_changed",
        "conversation_id": conversation_id,
        "pending_count": pending_count,
        "total_pending": total_pending,
    }


def make_doc_note_suggestions_changed(
    conversation_id: str,
    pending_count: int,
    total_pending: int,
) -> dict[str, Any]:
    """Envelope for ``doc_note_suggestions_changed``.

    The doc-notes twin of ``project_memory_suggestions_changed``: published
    to the reviewing user (by chat/docs/note_suggestions.py) when a
    conversation's pending ``suggest_doc_note`` set changes -- a suggestion
    registered, accepted or dismissed, an instructions suggestion
    superseded by a newer one (possibly from another of the user's
    conversations), or pending rows dropped with their deleted doc. Drives
    the composer's doc-notes bubble (``pending_count``, this conversation's
    pending count) and the inbox (``total_pending``, the user's pending
    doc-note count across all conversations).
    """
    return {
        "type": "doc_note_suggestions_changed",
        "conversation_id": conversation_id,
        "pending_count": pending_count,
        "total_pending": total_pending,
    }


def make_wait_handle_resolved(
    conversation_id: str,
    handle_id: str,
    kind: str,
    status: str,
    *,
    request_id: Optional[int] = None,
    response: Optional[dict] = None,
) -> dict[str, Any]:
    """Envelope for ``wait_handle_resolved``.

    ``kind`` is the handle kind (``slack_reply`` / ``action_request``);
    ``status`` is one of ``accepted`` / ``rejected`` / ``cancelled`` /
    ``timed_out`` / ``stopped``. ``request_id`` is set for action-request handles so
    the action-request card can match the event without a follow-up REST
    round trip.
    """
    envelope: dict[str, Any] = {
        "type": "wait_handle_resolved",
        "conversation_id": conversation_id,
        "handle_id": handle_id,
        "kind": kind,
        "status": status,
    }
    if request_id is not None:
        envelope["request_id"] = request_id
    if response is not None:
        envelope["response"] = response
    return envelope


# ---------------------------------------------------------------------------
# Per-conversation durable events (Phase 2)
# ---------------------------------------------------------------------------


def make_message_appended(conversation_id: str, seq: int) -> dict[str, Any]:
    """Envelope for ``message_appended``.

    Carries only ``{seq, conversation_id}`` -- clients REST-fetch the new
    tail. Phase 3 may switch to pushing full bodies.
    """
    return {
        "type": "message_appended",
        "conversation_id": conversation_id,
        "seq": seq,
    }


# ---------------------------------------------------------------------------
# Per-conversation transient events (Phase 3)
# ---------------------------------------------------------------------------


def make_text_delta(conversation_id: str, content: str) -> dict[str, Any]:
    return {
        "type": "text_delta",
        "conversation_id": conversation_id,
        "content": content,
    }


def make_tool_started(
    conversation_id: str,
    tool_id: str,
    tool_name: str,
) -> dict[str, Any]:
    return {
        "type": "tool_started",
        "conversation_id": conversation_id,
        "tool_id": tool_id,
        "tool_name": tool_name,
    }


def make_subscription_expired(conversation_id: str) -> dict[str, Any]:
    """Envelope for ``subscription_expired``.

    Sent by the sweep loop when a conversation subscription's TTL elapses
    without a refresh. The client typically responds by clearing any
    streaming buffer for that conv so a subsequent navigation back rebuilds
    canonical state via the catchup / resync path.
    """
    return {
        "type": "subscription_expired",
        "conversation_id": conversation_id,
    }
