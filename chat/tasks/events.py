"""Realtime publishing for Quest Tasks.

Two per-user globals on the persistent WebSocket, built by
chat/realtime/events.py: ``task_list_changed`` (the sidebar section and the
archived list re-fetch) and ``task_changed {task_id, event_seq,
brief_version, active_run}`` (an open thread of that task re-fetches its
newest page; its brief panel re-fetches when ``brief_version`` moved).

Audience: the task's owner only (tasks are private, invariant 7).

Who publishes what:

- chat/tasks/runs.py: every run status change (``task_changed`` and
  ``task_list_changed``: the row's ``active_run`` or ``unread`` moved) and a
  run's brief write (``task_changed`` alone).
- chat/tasks/service.py: a run starting (both).

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
from db import task_store

logger = logging.getLogger(__name__)

__all__ = [
    "active_run_summary",
    "publish_task_changed",
    "publish_task_list_changed",
    "publish_task_state",
]


def active_run_summary(run: Optional[dict]) -> Optional[dict]:
    """The ``active_run`` shape of a run dict: ``{id, seq, status}``, or
    None when ``run`` is None."""
    if run is None:
        return None
    return {"id": run["id"], "seq": run["seq"], "status": run["status"]}


def publish_task_list_changed(user_id: int) -> None:
    """Best-effort: tell the user's tabs their task list may have changed."""
    try:
        bus.publish_to_user(user_id, realtime_events.make_task_list_changed())
    except Exception:
        logger.debug(
            "[tasks] publish task_list_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )


def publish_task_changed(
    user_id: int, task: dict, active_run: Optional[dict],
) -> None:
    """Best-effort: tell the user's tabs a task's thread or brief changed.

    Args:
        user_id: The task's owner.
        task: The task dict after the change (``id``, ``event_seq`` and
            ``brief_version`` are read).
        active_run: ``{id, seq, status}`` of the task's active run, or None
            (see :func:`active_run_summary`).
    """
    try:
        bus.publish_to_user(
            user_id,
            realtime_events.make_task_changed(
                task["id"], task["event_seq"], task["brief_version"], active_run,
            ),
        )
    except Exception:
        logger.debug(
            "[tasks] publish task_changed failed (user_id=%s, task_id=%s)",
            user_id, task.get("id"), exc_info=True,
        )


async def publish_task_state(task_id: str, *, list_changed: bool = True) -> None:
    """Publish the task's current state to its owner: ``task_changed`` with
    the stored counters and active run, then ``task_list_changed`` when
    ``list_changed``.

    Reads the task and its active run fresh, so callers need not carry
    them. A missing task (deleted meanwhile) publishes nothing. Never
    raises.
    """
    try:
        task = await task_store.get_task_by_id(task_id)
        if task is None:
            return
        active = await task_store.get_active_run(task_id)
    except Exception:
        logger.debug(
            "[tasks] loading task state for publish failed (task_id=%s)",
            task_id, exc_info=True,
        )
        return
    publish_task_changed(task["user_id"], task, active_run_summary(active))
    if list_changed:
        publish_task_list_changed(task["user_id"])
