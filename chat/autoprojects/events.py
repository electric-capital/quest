"""Realtime publishing for AutoProjects.

Two per-user globals on the persistent WebSocket, built by
chat/realtime/events.py: ``autoproject_list_changed`` (the sidebar section and the
archived list re-fetch) and ``autoproject_changed {autoproject_id, event_seq,
brief_version, active_run}`` (an open thread of that AutoProject re-fetches its
newest page; its brief panel re-fetches when ``brief_version`` moved).

Audience: the AutoProject's owner only (AutoProjects are private, invariant 7).

Who publishes what:

- chat/autoprojects/runs.py: every run status change (``autoproject_changed`` and
  ``autoproject_list_changed``: the row's ``active_run`` or ``unread`` moved) and a
  run's brief write (``autoproject_changed`` alone).
- chat/autoprojects/service.py: a run starting (both).

Live progress of a run is not published here: the thread subscribes to the
run's conversation and reads its existing streaming events.

Publishing is best-effort: a failure is logged at debug and never undoes
the write. Event-loop thread only (``bus.publish_to_user`` is not
thread-safe). Tools never publish directly.
"""

import logging
from typing import Optional

from chat.realtime import events as realtime_events
from chat.realtime.bus import bus
from db import autoproject_store

logger = logging.getLogger(__name__)

__all__ = [
    "active_run_summary",
    "publish_autoproject_changed",
    "publish_autoproject_list_changed",
    "publish_autoproject_state",
]


def active_run_summary(run: Optional[dict]) -> Optional[dict]:
    """The ``active_run`` shape of a run dict: ``{id, seq, status}``, or
    None when ``run`` is None."""
    if run is None:
        return None
    return {"id": run["id"], "seq": run["seq"], "status": run["status"]}


def publish_autoproject_list_changed(user_id: int) -> None:
    """Best-effort: tell the user's tabs their AutoProject list may have changed."""
    try:
        bus.publish_to_user(user_id, realtime_events.make_autoproject_list_changed())
    except Exception:
        logger.debug(
            "[autoprojects] publish autoproject_list_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )


def publish_autoproject_changed(
    user_id: int, autoproject: dict, active_run: Optional[dict],
) -> None:
    """Best-effort: tell the user's tabs an AutoProject's thread or brief changed.

    Args:
        user_id: The AutoProject's owner.
        autoproject: The AutoProject dict after the change (``id``, ``event_seq`` and
            ``brief_version`` are read).
        active_run: ``{id, seq, status}`` of the AutoProject's active run, or None
            (see :func:`active_run_summary`).
    """
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_autoproject_changed(
                autoproject["id"], autoproject["event_seq"],
                autoproject["brief_version"], active_run,
            ),
        )
    except Exception:
        logger.debug(
            "[autoprojects] publish autoproject_changed failed (user_id=%s, autoproject_id=%s)",
            user_id, autoproject.get("id"), exc_info=True,
        )


async def publish_autoproject_state(autoproject_id: str, *, list_changed: bool = True) -> None:
    """Publish the AutoProject's current state to its owner: ``autoproject_changed`` with
    the stored counters and active run, then ``autoproject_list_changed`` when
    ``list_changed``.

    Reads the AutoProject and its active run fresh, so callers need not carry
    them. A missing AutoProject (deleted meanwhile) publishes nothing. Never
    raises.
    """
    try:
        autoproject = await autoproject_store.get_autoproject_by_id(autoproject_id)
        if autoproject is None:
            return
        active = await autoproject_store.get_active_run(autoproject_id)
    except Exception:
        logger.debug(
            "[autoprojects] loading AutoProject state for publish failed (autoproject_id=%s)",
            autoproject_id, exc_info=True,
        )
        return
    publish_autoproject_changed(autoproject["user_id"], autoproject, active_run_summary(active))
    if list_changed:
        publish_autoproject_list_changed(autoproject["user_id"])
