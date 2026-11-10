"""The task service: the rules behind every Quest Task route and the send
path.

Task lifecycle (spec issue #213, sections 7 and 10): :func:`create_task`,
:func:`update_task`, the user's brief edit and restore (:func:`set_brief`,
:func:`restore_brief`), :func:`set_task_archived`, the read
acknowledgement (:func:`mark_task_read`), and the deletes
(:func:`delete_task`, :func:`delete_run`), which stop live turns, cancel
wait handles, close the run conversations' open approval cards and sweep
their directories. Each checks the gate and ownership itself and publishes
through chat/tasks/events.py; the route-facing shapes are built in
chat/tasks/thread.py.

A run starts in two steps (spec issue #213, section 7): ``POST
/tasks/{id}/runs`` creates the run's conversation
(:func:`create_run_conversation`, an early refusal for the UI), and the
first ``send_message`` on that conversation allocates the ``task_runs`` row
(:func:`start_run`, the authoritative check). :func:`start_run` is where
invariant 6 (one active run per task) is enforced from the send path; the
store's ``allocate_run`` does the transactional check, so lifting the rule
later is one change here and there.

:func:`answer_question` answers a run's open question (``ask_task_question``,
chat/tasks/questions.py) and continues the run.

The startup sweeps (:func:`run_startup_sweeps`) settle what a restart
leaves behind: a ``running`` run whose in-memory turn is gone, and run
conversations created but never sent.

Every refusal is a ``db.task_store.TaskStoreError`` subclass with a stable
``code``, so routes (through ``chat.tasks.http.task_http_exception``) and
the send path put it straight into their error bodies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import shutil
from typing import Any, Optional

from chat.tasks.constants import (
    TASK_ABANDONED_RUN_CONVERSATION_MAX_AGE,
    TASK_RUN_ACTIVE_STATUSES,
    TASK_RUN_STATUS_FAILED,
    tasks_disabled_message,
)
from chat.tasks.questions import (
    CORRELATION_KIND,
    HANDLE_KIND,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_CANCELLED,
    TaskQuestionNotFoundError,
    TaskQuestionResolvedError,
    question_view,
    rewrite_question_message,
    validate_answer,
)
from db import task_store, tool_wait_handle_store
from db.models import ToolWaitHandleStatus
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
# Answering a run's question
# ---------------------------------------------------------------------------


async def answer_question(
    user: dict, task_id: str, handle_id: str, answer: Any, *, app,
) -> dict:
    """Answer a task run's open question (``POST
    /tasks/{id}/questions/{handle_id}/answer``) and continue the run.

    Resolves the question's ``task_question`` wait handle with the answer,
    writes the outcome into the run's transcript message, publishes the
    resolution and the thread change, and kicks the run's resume. While
    another card of the same tool-call batch (an approval card) is still
    pending, the resume gate holds the run until that one is resolved too.

    Args:
        user: The caller's user dict.
        task_id: The task the question belongs to.
        handle_id: The question's wait-handle id.
        answer: The user's answer (validated here).
        app: The FastAPI app, for the headless resume.

    Returns:
        The answered question (``chat.tasks.questions.question_view``).

    Raises:
        TasksDisabledError, TaskNotFoundError (missing or foreign task),
        TaskQuestionNotFoundError (missing, foreign, or not a question of
        this task), TaskQuestionResolvedError (already answered, or
        cancelled: a Stop, or its run is no longer active),
        TaskValidationError ``invalid_answer``.
    """
    require_tasks_enabled(user)
    task = await task_store.get_task(user["id"], task_id)
    if task is None:
        raise TaskNotFoundError(task_id)

    # Invariant 7: a missing handle, another kind, another user's, or one
    # of another task's run all read the same.
    handle = await tool_wait_handle_store.get_handle_for_user(user["id"], handle_id)
    if handle is None or handle.get("kind") != HANDLE_KIND:
        raise TaskQuestionNotFoundError(handle_id)
    conversation_id = handle["conversation_id"]
    run = await task_store.get_run_by_conversation(conversation_id)
    if run is None or run["task_id"] != task_id:
        raise TaskQuestionNotFoundError(handle_id)

    if handle["status"] != ToolWaitHandleStatus.PENDING:
        view = question_view(handle)
        raise TaskQuestionResolvedError(view["status"], view["answer"])
    if run["status"] not in TASK_RUN_ACTIVE_STATUSES:
        # A pending handle left on a finished run: nothing would read the
        # answer.
        raise TaskQuestionResolvedError(QUESTION_STATUS_CANCELLED)

    answer = validate_answer(answer)

    # resolve_handle overwrites the correlation, so pass the run through.
    updated = await tool_wait_handle_store.resolve_handle(
        handle_id,
        new_status=ToolWaitHandleStatus.ACCEPTED.value,
        response={"answer": answer},
        correlation_kind=CORRELATION_KIND,
        correlation_id=run["id"],
        user_id=user["id"],
    )
    if updated is None:
        # Lost a race with another answer or a Stop.
        current = await tool_wait_handle_store.get_handle_for_user(
            user["id"], handle_id,
        )
        if current is None:
            raise TaskQuestionNotFoundError(handle_id)
        view = question_view(current)
        raise TaskQuestionResolvedError(view["status"], view["answer"])
    logger.info(
        "Task question %s answered (task=%s, run=%s, conversation=%s, user=%s)",
        handle_id, task_id, run["id"], conversation_id, user["id"],
    )

    rewrite_question_message(
        conversation_id, handle_id, status=QUESTION_STATUS_ANSWERED, answer=answer,
    )

    from chat.realtime import events as realtime_events
    from chat.realtime.bus import bus

    try:
        bus.publish_to_user(
            user["id"],
            realtime_events.make_wait_handle_resolved(
                conversation_id=conversation_id,
                handle_id=updated["id"],
                kind=HANDLE_KIND,
                status=updated["status"],
                response={"answer": answer},
            ),
        )
    except Exception:
        logger.debug(
            "[task_question] publish wait_handle_resolved failed", exc_info=True,
        )
    # The thread changed (the question shows its answer); the list row did
    # not (the run is still waiting until its resume starts).
    try:
        await task_store.bump_event_seq(task_id)
    except Exception:
        logger.warning(
            "Bumping event_seq of task %s after an answer failed",
            task_id, exc_info=True,
        )
    from chat.tasks.events import publish_task_state

    await publish_task_state(task_id, list_changed=False)

    from chat.wait_handles import resume as wait_resume

    try:
        wait_resume.maybe_kick_resume(app, user, conversation_id)
    except Exception:
        logger.exception(
            "Failed to kick the resume of task run %s (conversation=%s)",
            run["id"], conversation_id,
        )

    return question_view(updated)


# ---------------------------------------------------------------------------
# Task CRUD, brief, archive, read
# ---------------------------------------------------------------------------


async def _require_owned(user: dict, task_id: str) -> dict:
    """Gate, then ownership (missing and foreign read the same)."""
    require_tasks_enabled(user)
    task = await task_store.get_task(user["id"], task_id)
    if task is None:
        raise TaskNotFoundError(task_id)
    return task


def _check_model_allowed(model: Any) -> None:
    """Refuse a task default model unticked for private conversations. A
    None or malformed value is left to the store's shape check."""
    if not isinstance(model, str) or not model.strip():
        return
    from config.model_selection import is_model_allowed

    if not is_model_allowed(model.strip(), public=False):
        raise TaskModelNotAllowedError(model.strip())


async def create_task(
    user: dict,
    *,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
    instructions: Optional[str] = None,
    model: Optional[str] = None,
    thinking_effort: Optional[str] = None,
) -> dict:
    """Create a task (``POST /tasks``) and publish ``task_list_changed``.

    The title is the user's ``title`` when given (``title_is_default``
    False); else cut from ``prompt`` (the New Task composer's first
    message, which is not stored: it only names the task, so its first run
    may still rename it); else ``task_store.DEFAULT_TASK_TITLE``. Both
    fallbacks keep ``title_is_default`` True.

    Returns:
        The task dict.

    Raises:
        TasksDisabledError, TaskModelNotAllowedError, TaskValidationError
        (``invalid_title``, ``invalid_instructions``, ``invalid_model``,
        ``invalid_thinking_effort``), TaskCapReachedError.
    """
    require_tasks_enabled(user)
    if title is not None:
        task_title, title_is_default = title, False
    elif prompt is not None:
        task_title, title_is_default = task_store.title_from_prompt(prompt), True
    else:
        task_title, title_is_default = task_store.DEFAULT_TASK_TITLE, True
    _check_model_allowed(model)
    task = await task_store.create_task(
        user["id"],
        title=task_title,
        title_is_default=title_is_default,
        instructions=instructions or "",
        model=model,
        thinking_effort=thinking_effort,
    )
    logger.info("Task created (task=%s, user=%s)", task["id"], user["id"])
    from chat.tasks.events import publish_task_list_changed

    publish_task_list_changed(user["id"])
    return task


async def update_task(
    user: dict,
    task_id: str,
    *,
    title: Any = task_store.UNSET,
    instructions: Any = task_store.UNSET,
    model: Any = task_store.UNSET,
    thinking_effort: Any = task_store.UNSET,
) -> dict:
    """Edit the user-owned fields (``PATCH /tasks/{id}``); fields left at
    ``task_store.UNSET`` stay, None clears ``model`` / ``thinking_effort``
    (a None ``title`` is refused by the store). Publishes ``task_changed``
    and ``task_list_changed`` when something changed.

    Returns:
        The task dict.

    Raises:
        TasksDisabledError, TaskNotFoundError, TaskModelNotAllowedError,
        TaskValidationError.
    """
    before = await _require_owned(user, task_id)
    if model is not task_store.UNSET:
        _check_model_allowed(model)
    task = await task_store.update_task(
        user["id"], task_id,
        title=title, instructions=instructions,
        model=model, thinking_effort=thinking_effort,
    )
    if task["event_seq"] != before["event_seq"]:
        from chat.tasks.events import publish_task_state

        await publish_task_state(task_id)
    return task


async def set_brief(
    user: dict, task_id: str, brief: Any, *, expected_version: int,
) -> dict:
    """The user's brief edit (``PUT /tasks/{id}/brief``), allowed while a
    run is active (the run's own write then gets ``brief_conflict``).
    Publishes ``task_changed`` (the list row does not change).

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        TasksDisabledError, TaskNotFoundError, StaleBriefError,
        TaskValidationError (``invalid_brief``, ``brief_too_long``).
    """
    await _require_owned(user, task_id)
    state = await task_store.set_brief(
        task_id, brief, expected_version=expected_version, source="ui",
    )
    from chat.tasks.events import publish_task_state

    await publish_task_state(task_id, list_changed=False)
    return state


async def restore_brief(
    user: dict, task_id: str, version: int, *, expected_version: int,
) -> dict:
    """Write stored version ``version`` as a new brief version (``POST
    /tasks/{id}/brief/versions/{version}/restore``, source
    ``restore:<version>``). Publishes ``task_changed``.

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        TasksDisabledError, TaskNotFoundError, BriefVersionNotFoundError,
        StaleBriefError, TaskValidationError (``brief_too_long``).
    """
    await _require_owned(user, task_id)
    state = await task_store.restore_brief_version(
        task_id, version, expected_version=expected_version,
    )
    from chat.tasks.events import publish_task_state

    await publish_task_state(task_id, list_changed=False)
    return state


async def set_task_archived(user: dict, task_id: str, archived: bool) -> dict:
    """Archive or unarchive a task (``PUT /tasks/{id}/archive`` /
    ``unarchive``) and publish ``task_changed`` and ``task_list_changed``.

    Archiving is refused while a run is active, and marks the task read up
    to its ``event_seq`` (as archiving a conversation does): an archived
    task carries no unread dot. Unarchiving counts against the task cap.

    Returns:
        The task dict.

    Raises:
        TasksDisabledError, TaskNotFoundError, TaskRunActiveError (archive),
        TaskCapReachedError (unarchive).
    """
    require_tasks_enabled(user)
    task = await task_store.set_task_archived(user["id"], task_id, archived)
    if archived and task["read_seq"] < task["event_seq"]:
        await task_store.mark_task_read(
            user["id"], task_id, up_to_seq=task["event_seq"],
        )
        task = await task_store.get_task(user["id"], task_id) or task
    logger.info(
        "Task %s (task=%s, user=%s)",
        "archived" if archived else "unarchived", task_id, user["id"],
    )
    from chat.tasks.events import publish_task_state

    await publish_task_state(task_id)
    return task


async def mark_task_read(user: dict, task_id: str, *, seq: int) -> dict:
    """Acknowledge the thread up to ``seq`` (``POST /tasks/{id}/read``,
    sent by a client showing the thread in a focused window) and publish
    ``task_list_changed`` when ``read_seq`` moved.

    An admin impersonating the owner changes nothing: the admin looking at
    the thread is not the owner reading it.

    Returns:
        The task dict after the acknowledgement.

    Raises:
        TasksDisabledError, TaskNotFoundError.
    """
    task = await _require_owned(user, task_id)
    if "_impersonator_uid" in user:
        return task
    moved = await task_store.mark_task_read(user["id"], task_id, up_to_seq=seq)
    if not moved:
        return task
    from chat.tasks.events import publish_task_list_changed

    publish_task_list_changed(user["id"])
    return await task_store.get_task(user["id"], task_id) or task


# ---------------------------------------------------------------------------
# Deleting a task or a run (spec 10)
# ---------------------------------------------------------------------------

# How long a delete waits for the cancelled live turns of the doomed
# conversations to unwind (a cancelled turn flushes its transcript on the
# way out; the directory sweep must come after that flush).
_DELETE_CANCEL_WAIT_SECONDS = 10.0


async def _cancel_live_runs(conversation_ids) -> None:
    """Cancel the in-flight send run and headless resume of each
    conversation and wait (bounded) for them to finish."""
    from chat.realtime.socket import cancel_conversation_runs

    cancelled: list[asyncio.Task] = []
    for conversation_id in conversation_ids:
        cancelled.extend(cancel_conversation_runs(conversation_id))
    if not cancelled:
        return
    _done, pending = await asyncio.wait(cancelled, timeout=_DELETE_CANCEL_WAIT_SECONDS)
    if pending:
        logger.warning(
            "%d cancelled task run turn(s) did not finish within %.0fs; "
            "sweeping their conversations anyway",
            len(pending), _DELETE_CANCEL_WAIT_SECONDS,
        )


async def _close_conversation_cards(user_id: int, conversation_ids) -> list[int]:
    """Cancel the pending wait handles of the conversations that have any
    (one query finds them) and close their open action requests as
    ``stopped`` (``action_requests`` has no foreign key on the
    conversation, so a card of a deleted run would otherwise stay
    approvable in the inbox). Best-effort per conversation.

    Returns:
        The ids of the action requests closed.
    """
    from chat.gemini_api.session import cancel_pending_wait_handles_for_conversation
    from db import action_request_store

    ids = list(dict.fromkeys(conversation_ids))
    if not ids:
        return []
    try:
        pending = await tool_wait_handle_store.list_pending_for_conversations(ids)
    except Exception:
        logger.exception("Listing pending wait handles of task conversations failed")
        pending = []
    for conversation_id in dict.fromkeys(h["conversation_id"] for h in pending):
        try:
            await cancel_pending_wait_handles_for_conversation(user_id, conversation_id)
        except Exception:
            logger.exception(
                "Cancelling pending wait handles of %s failed", conversation_id,
            )
    try:
        return await action_request_store.stop_open_action_requests_for_conversations(
            user_id, ids, reason="task_run_deleted",
        )
    except Exception:
        logger.exception("Closing open action requests of task conversations failed")
        return []


async def _sweep_conversations(user_id: int, conversation_ids) -> None:
    """After the rows are gone: cancel any live turn again (a run started
    in the race window), drop the in-memory sessions and remove each
    conversation's ``CHATS_DIR`` directory. Best-effort per id."""
    from chat.gemini_api.session import remove_chat_session
    from chat.storage import ChatStorage

    ids = list(dict.fromkeys(conversation_ids))
    await _cancel_live_runs(ids)
    for conversation_id in ids:
        try:
            remove_chat_session(user_id, conversation_id)
            conversation_dir = ChatStorage.get_conversation_dir(conversation_id)
            await asyncio.to_thread(shutil.rmtree, conversation_dir, ignore_errors=True)
        except Exception:
            logger.exception(
                "Removing task run conversation %s failed", conversation_id,
            )


async def _publish_request_count_changed(user_id: int) -> None:
    """Best-effort: the inbox badge counts moved (cards were closed)."""
    from chat.realtime import events as realtime_events
    from chat.realtime.bus import bus
    from db.action_request_store import count_action_requests_by_status

    try:
        counts = await count_action_requests_by_status(user_id)
        bus.publish_to_user(user_id, realtime_events.make_request_count_changed(counts))
    except Exception:
        logger.debug(
            "[tasks] publish request_count_changed failed (user_id=%s)",
            user_id, exc_info=True,
        )


async def delete_task(user: dict, task_id: str) -> None:
    """Delete a task with its runs, their conversations and its workspace
    (``DELETE /tasks/{id}``).

    1. Cancel every live turn of the task's conversations and wait for
       them (bounded), then stop the active run (``stopped``, its handles
       cancelled).
    2. Cancel the pending wait handles and close the open approval cards of
       every run conversation.
    3. Delete the rows (runs, brief versions and conversations cascade).
    4. Sweep the conversations (live turns again, in-memory sessions,
       ``CHATS_DIR`` directories), then ``TASKS_DIR/<id>``.
    5. Publish ``task_changed`` (no active run; an open thread refetches
       and gets 404), ``task_list_changed``, and ``request_count_changed``
       when cards were closed.

    Raises:
        TasksDisabledError, TaskNotFoundError.
    """
    from chat.storage import ChatStorage
    from chat.tasks.runs import stop_task_run

    task = await _require_owned(user, task_id)
    user_id = user["id"]

    conversation_ids = await task_store.list_task_conversation_ids(task_id)
    await _cancel_live_runs(conversation_ids)
    active = await task_store.get_active_run(task_id)
    if active is not None:
        await stop_task_run(user_id, active["conversation_id"])
    closed = await _close_conversation_cards(user_id, conversation_ids)
    task = await task_store.get_task(user_id, task_id) or task

    deleted_ids = await task_store.delete_task(user_id, task_id)
    known_ids = set(conversation_ids)
    late_ids = [cid for cid in deleted_ids if cid not in known_ids]
    if late_ids:
        # A run conversation created between the read and the delete.
        closed += await _close_conversation_cards(user_id, late_ids)
    logger.info(
        "Task deleted (task=%s, user=%s, conversations=%d)",
        task_id, user_id, len(deleted_ids),
    )

    await _sweep_conversations(user_id, list(conversation_ids) + late_ids)
    try:
        task_dir = ChatStorage.get_task_dir(task_id)
        await asyncio.to_thread(shutil.rmtree, task_dir, ignore_errors=True)
    except Exception:
        logger.exception("Removing the directory of task %s failed", task_id)

    from chat.tasks.events import publish_task_changed, publish_task_list_changed

    # The delete bumped event_seq once more before removing the row.
    publish_task_changed(user_id, {**task, "event_seq": task["event_seq"] + 1}, None)
    publish_task_list_changed(user_id)
    if closed:
        await _publish_request_count_changed(user_id)


async def delete_run(user: dict, task_id: str, run_id: str) -> None:
    """Delete a finished run's log entry and its conversation (``DELETE
    /tasks/{id}/runs/{run_id}``). Its ``seq`` is never reused; docs, tables
    and task files it created stay.

    The store delete is the atomic refusal; then the conversation's live
    turn (one still flushing after its completion), pending wait handles
    and open approval cards are closed, its in-memory session dropped and
    its ``CHATS_DIR`` directory removed. Publishes ``task_changed`` and
    ``task_list_changed``, and ``request_count_changed`` when cards were
    closed.

    Raises:
        TasksDisabledError, TaskNotFoundError, TaskRunNotFoundError (also a
        run of another task), TaskRunActiveError.
    """
    require_tasks_enabled(user)
    user_id = user["id"]
    conversation_id = await task_store.delete_run(user_id, task_id, run_id)
    logger.info(
        "Task run deleted (task=%s, run=%s, conversation=%s, user=%s)",
        task_id, run_id, conversation_id, user_id,
    )
    await _cancel_live_runs([conversation_id])
    closed = await _close_conversation_cards(user_id, [conversation_id])
    await _sweep_conversations(user_id, [conversation_id])

    from chat.tasks.events import publish_task_state

    await publish_task_state(task_id)
    if closed:
        await _publish_request_count_changed(user_id)


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
