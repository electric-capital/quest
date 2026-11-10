"""The task service: the one place for the rules that start a task run.

A run starts in two steps (spec issue #213, section 7): ``POST
/tasks/{id}/runs`` creates the run's conversation
(:func:`create_run_conversation`, an early refusal for the UI), and the
first ``send_message`` on that conversation allocates the ``task_runs`` row
(:func:`start_run`, the authoritative check). :func:`start_run` is where
invariant 6 (one active run per task) is enforced from the send path; the
store's ``allocate_run`` does the transactional check, so lifting the rule
later is one change here and there.

The startup sweeps (:func:`run_startup_sweeps`) settle what a restart
leaves behind: a ``running`` run whose in-memory turn is gone, and run
conversations created but never sent.

Every refusal is a ``db.task_store.TaskStoreError`` subclass with a stable
``code``, so routes and the send path put it straight into their error
bodies. Later phases add the rest of the task lifecycle here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import shutil
from typing import Optional

from chat.tasks.constants import (
    TASK_ABANDONED_RUN_CONVERSATION_MAX_AGE,
    TASK_RUN_STATUS_FAILED,
    tasks_disabled_message,
)
from db import task_store
from db.task_store import (
    TaskArchivedError,
    TaskNotFoundError,
    TaskRunActiveError,
    TaskRunExistsError,
    TaskStoreError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TasksDisabledError(TaskStoreError):
    """The ``tasks`` feature gate is closed for the user (spec 9)."""

    code = "tasks_disabled"

    def __init__(self) -> None:
        super().__init__(tasks_disabled_message())


class TaskModelNotAllowedError(TaskStoreError):
    """The run's model is unticked for private conversations in the admin
    Model Selection table (``config.model_selection``)."""

    code = "model_not_allowed"

    def __init__(self, model: str) -> None:
        from chat.llm.config import get_model_display_name

        try:
            name = get_model_display_name(model)
        except Exception:
            name = model
        super().__init__(
            f"The model {name} is not allowed in private conversations. "
            "Pick another model from the model menu and send again.",
            details={"model": model},
        )
        self.model = model


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def require_tasks_enabled(user: dict) -> None:
    """Raise :class:`TasksDisabledError` unless the gate is open for ``user``."""
    from config.feature_gates import tasks_enabled_for

    if not tasks_enabled_for(user.get("email") or ""):
        raise TasksDisabledError()


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------


async def create_run_conversation(user: dict, task_id: str) -> str:
    """Create the conversation for the task's next run (``POST
    /tasks/{id}/runs``).

    An early refusal for the UI: the gate, ownership, the archive flag and
    the one-active-run rule are checked here so the composer learns of them
    before the user sends, but nothing is reserved. :func:`start_run`
    re-checks all of it when the message arrives.

    Args:
        user: The caller's user dict.
        task_id: The task.

    Returns:
        The new conversation id (``origin="task"``, ``task_id`` set, no run
        row yet).

    Raises:
        TasksDisabledError, TaskNotFoundError (missing or foreign),
        TaskArchivedError, TaskRunActiveError.
    """
    require_tasks_enabled(user)
    task = await task_store.get_task(user["id"], task_id)
    if task is None:
        raise TaskNotFoundError(task_id)
    if task["archived"]:
        raise TaskArchivedError(task_id)
    active = await task_store.get_active_run(task_id)
    if active is not None:
        raise TaskRunActiveError(active)

    from chat.storage import ChatStorage

    return await ChatStorage.create_task_run_conversation(user["id"], task_id)


@dataclass(frozen=True)
class StartedRun:
    """A run :func:`start_run` allocated, with what its turn runs on."""

    run: dict
    task: dict
    model: str
    thinking_effort: Optional[str]


def _resolve_run_model(composer_model: Optional[str], task: dict) -> str:
    """The run's model: the composer's pick, else the task's default, else
    the server default, with the deprecated-model remap applied the way
    ``run_conversation_turn`` applies it (so the run row records the model
    that actually runs)."""
    from chat.gemini_api.constants import _DEPRECATED_MODELS
    from config.server_config import load_server_config

    model = composer_model or task.get("model")
    if not model:
        model = load_server_config()["gemini"]["model"]
    return _DEPRECATED_MODELS.get(model, model)


async def start_run(
    user: dict,
    conversation: dict,
    *,
    prompt: str,
    model: Optional[str] = None,
    thinking_effort: Optional[str] = None,
) -> StartedRun:
    """Accept the single message of a run conversation and allocate its run.

    THE one place the send path enforces invariant 6 (one active run per
    task) and invariant 1 (one user message per run conversation). Called
    by ``_handle_send_message`` before the user message is appended, so a
    refused send leaves nothing in the transcript.

    Args:
        user: The sender's user dict.
        conversation: The run conversation's meta dict
            (``db.conversation_store.get_conversation_meta``), owned by
            ``user``.
        prompt: The user's message, stored verbatim as the run's prompt.
        model: The composer's model pick, if any.
        thinking_effort: The composer's thinking level, if any.

    Returns:
        The started run, with the model and thinking level its turn uses.

    Raises:
        ValueError: ``conversation`` is not a run conversation.
        TasksDisabledError, TaskRunExistsError (the conversation already has
        its message), TaskNotFoundError, TaskModelNotAllowedError,
        TaskArchivedError, TaskRunActiveError, TaskValidationError
        ``invalid_prompt``.
    """
    task_id = conversation.get("task_id")
    if not task_id:
        raise ValueError(
            f"Conversation {conversation.get('id')} is not a task run conversation."
        )
    require_tasks_enabled(user)

    # Invariant 1: a run conversation takes exactly one user message. The
    # unique index on task_runs.conversation_id (allocate_run) catches a
    # race between two first sends.
    if int(conversation.get("last_message_seq") or 0) > 0:
        raise TaskRunExistsError(conversation["id"])

    task = await task_store.get_task(user["id"], task_id)
    if task is None:
        raise TaskNotFoundError(task_id)

    run_model = _resolve_run_model(model, task)
    run_effort = thinking_effort or task.get("thinking_effort")

    # A task run is always private (invariant 8).
    from config.model_selection import is_model_allowed

    if not is_model_allowed(run_model, public=False):
        raise TaskModelNotAllowedError(run_model)

    run = await task_store.allocate_run(
        task_id,
        conversation_id=conversation["id"],
        prompt=prompt,
        model=run_model,
    )
    logger.info(
        "Task run started (task=%s, run=%s, seq=%s, conversation=%s, model=%s)",
        task_id, run["id"], run["seq"], conversation["id"], run_model,
    )
    # The task gained an active run (thread and list row both changed).
    from chat.tasks.events import publish_task_state

    await publish_task_state(task_id)
    return StartedRun(
        run=run, task=task, model=run_model, thinking_effort=run_effort,
    )


async def fail_unstarted_run(run_id: str, error: str) -> None:
    """Mark a run ``failed`` whose turn never started (the send path gave
    up after :func:`start_run`), then mark its task unread and publish (a
    failed run, spec 10). Best-effort: logged, never raises."""
    try:
        updated = await task_store.set_run_status(
            run_id, TASK_RUN_STATUS_FAILED, error=error,
        )
    except Exception:
        logger.exception("Failing unstarted task run %s failed", run_id)
        return
    if updated is None:
        logger.warning(
            "Unstarted task run %s was not active; left as it was", run_id,
        )
        return
    logger.info("Task run %s failed before its turn started: %s", run_id, error)
    try:
        await task_store.mark_task_unread(updated["task_id"])
    except Exception:
        logger.warning(
            "Marking task %s unread after a failed start failed",
            updated["task_id"], exc_info=True,
        )
    from chat.tasks.events import publish_task_state

    await publish_task_state(updated["task_id"])


# ---------------------------------------------------------------------------
# Startup sweeps
# ---------------------------------------------------------------------------


async def fail_interrupted_runs_at_startup() -> list[dict]:
    """Fail every ``running`` run: its in-memory turn did not survive the
    restart. ``waiting`` runs are untouched; their wait handles survive and
    resume them.

    Each affected task is marked unread (spec 10: a failed run marks the
    task unread when no client is focused on it, and none is at startup).

    Returns:
        The failed runs.
    """
    runs = await task_store.fail_interrupted_runs()
    for task_id in {run["task_id"] for run in runs}:
        try:
            await task_store.mark_task_unread(task_id)
        except Exception:
            logger.exception("Marking task %s unread after a restart failed", task_id)
    if runs:
        logger.info("Failed %d task run(s) interrupted by a restart", len(runs))
    return runs


async def sweep_abandoned_run_conversations(
    *, now: Optional[datetime] = None,
) -> list[str]:
    """Delete run conversations created but never sent (no run row) once
    they are older than ``TASK_ABANDONED_RUN_CONVERSATION_MAX_AGE``, rows
    first, then their ``CHATS_DIR`` directories (best-effort per id).

    Returns:
        The deleted conversation ids.
    """
    from chat.storage import ChatStorage
    from db.conversation_store import delete_abandoned_task_conversations

    cutoff = (now or datetime.now(timezone.utc)) - TASK_ABANDONED_RUN_CONVERSATION_MAX_AGE
    conversation_ids = await delete_abandoned_task_conversations(cutoff)
    for conversation_id in conversation_ids:
        try:
            conversation_dir = ChatStorage.get_conversation_dir(conversation_id)
            await asyncio.to_thread(shutil.rmtree, conversation_dir, ignore_errors=True)
        except Exception:
            logger.exception(
                "Removing abandoned task run conversation %s failed", conversation_id,
            )
    if conversation_ids:
        logger.info(
            "Deleted %d abandoned task run conversation(s)", len(conversation_ids),
        )
    return conversation_ids


async def run_startup_sweeps() -> None:
    """Run both startup sweeps; one failing does not skip the other."""
    try:
        await fail_interrupted_runs_at_startup()
    except Exception:
        logger.exception("Failing interrupted task runs at startup failed")
    try:
        await sweep_abandoned_run_conversations()
    except Exception:
        logger.exception("Sweeping abandoned task run conversations failed")
