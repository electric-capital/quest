"""Asyncio in-process pub/sub bus for the persistent WebSocket.

Two channel namespaces:

* per-conversation -- routed to clients that have called ``subscribe`` on
  that conversation. Carries durable ``message_appended`` events (Phase 2)
  and transient ``text_delta`` / ``tool_started`` / ``sub_agent_*`` events
  (Phase 3).
* per-user -- routed to every connection authenticated as that user. Carries
  ``request_count_changed``, ``conversation_list_changed``,
  ``routine_list_changed``, ``doc_list_changed``, ``doc_changed`` and
  ``wait_handle_resolved``. :meth:`Bus.connected_user_ids` lets a publisher
  fan a per-user event out to every connected user.

The bus is a module-level singleton because the FastAPI deploy is
intentionally single-process; see :doc:`/architecture/slack-socket-mode`.
On ``QueueFull`` the event is dropped and the queue is marked overflowed --
the WS sender uses that flag to decide whether to emit ``resync`` (per-
conversation overflow) or close the socket (per-user overflow).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Per-subscriber bounded queue size. 256 events is generous enough to absorb
# a brief tab stall during a chatty turn without dropping; if a queue fills,
# the subscriber is treated as too slow and gets a resync (or close).
DEFAULT_QUEUE_MAXSIZE = 256


class SubscriberQueue(asyncio.Queue):
    """Asyncio queue with an "overflowed" sticky flag.

    Set by :meth:`Bus.publish_*` when ``put_nowait`` raises ``QueueFull``,
    consumed by the persistent WS sender to decide whether to resync the
    affected conversation or close the connection.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        super().__init__(maxsize=maxsize)
        self.overflowed: bool = False
        # Per-conversation overflow marker -- set when a conversation channel
        # publish overflows, so the sender can resync only the affected
        # conversation rather than tearing down the entire socket.
        self.overflowed_conversations: set[str] = set()


class Bus:
    """Singleton in-process pub/sub keyed by conversation_id and user_id."""

    def __init__(self) -> None:
        self._conversation_subs: dict[str, set[SubscriberQueue]] = {}
        self._user_subs: dict[int, set[SubscriberQueue]] = {}

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe_conversation(
        self, conversation_id: str, queue: SubscriberQueue,
    ) -> None:
        self._conversation_subs.setdefault(conversation_id, set()).add(queue)

    def unsubscribe_conversation(
        self, conversation_id: str, queue: SubscriberQueue,
    ) -> None:
        subs = self._conversation_subs.get(conversation_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            self._conversation_subs.pop(conversation_id, None)

    def subscribe_user(self, user_id: int, queue: SubscriberQueue) -> None:
        self._user_subs.setdefault(user_id, set()).add(queue)

    def unsubscribe_user(self, user_id: int, queue: SubscriberQueue) -> None:
        subs = self._user_subs.get(user_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            self._user_subs.pop(user_id, None)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish_to_conversation(
        self, conversation_id: str, event: dict[str, Any],
    ) -> None:
        """Non-blocking fan-out to every subscriber on a conversation channel.

        Marks the queue's ``overflowed_conversations`` set on overflow so the
        per-connection sender can issue ``resync`` for only this conversation.
        """
        subs = self._conversation_subs.get(conversation_id)
        if not subs:
            return
        for queue in list(subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                queue.overflowed = True
                queue.overflowed_conversations.add(conversation_id)
                logger.warning(
                    "[realtime-bus] Conversation queue overflow "
                    "(conversation=%s); subscriber will receive resync",
                    conversation_id,
                )

    def publish_to_user(self, user_id: int, event: dict[str, Any]) -> None:
        """Non-blocking fan-out to every subscriber on a user's global channel.

        On overflow the queue's ``overflowed`` flag is set; the sender closes
        the connection (a global queue overflow indicates a stuck consumer
        that should reconnect from scratch).
        """
        subs = self._user_subs.get(user_id)
        if not subs:
            return
        for queue in list(subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                queue.overflowed = True
                logger.warning(
                    "[realtime-bus] User queue overflow (user_id=%s); "
                    "subscriber will be closed",
                    user_id,
                )

    # ------------------------------------------------------------------
    # Introspection (used by tests / Phase 2 buffer eviction)
    # ------------------------------------------------------------------

    def conversation_subscriber_count(self, conversation_id: str) -> int:
        return len(self._conversation_subs.get(conversation_id, ()))

    def user_subscriber_count(self, user_id: int) -> int:
        return len(self._user_subs.get(user_id, ()))

    def connected_user_ids(self) -> list[int]:
        """Ids of every user with at least one live connection right now.

        A snapshot (empty channels are dropped on unsubscribe), so the
        caller may publish while iterating. Used for install-wide fan-out,
        e.g. a Quest Docs write to a doc shared with everyone.
        """
        return list(self._user_subs)


# Module-level singleton. Imported by routes, the flush callback, and tests.
bus = Bus()
