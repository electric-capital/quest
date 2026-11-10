"""Task questions: a task run asks the user something in the task thread.

A task run of a Quest Task that cannot go on without the user calls
``ask_task_question(question, options?)`` (the loop arm in
``chat/gemini_api/turn_tools.py``). The arm posts the question as a pending
``task_question`` wait handle, records it in the run's transcript and
suspends the run; the task driver (``chat/tasks/runs.py``) then marks the
run ``waiting``. The user answers from the thread
(``chat.tasks.service.answer_question``), which resolves the handle and
kicks the resume, and the run continues with ``{"answer": "..."}`` as the
tool result. A Stop cancels the handle and the question reads as
cancelled everywhere.

Questions have no table: the wait-handle row is the question (spec issue
#213, section 4.1). Its ``payload`` is ``{question, options}``, its
``response`` ``{answer}``, and it is correlated to the run
(``correlation_kind="task_run"``, ``correlation_id=<run id>``) from
creation. The thread groups questions by the run's conversation, not by the
correlation, so a row always lands under its own run.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from chat.tasks.constants import (
    TASK_ANSWER_MAX_CHARS,
    TASK_QUESTION_MAX_CHARS,
    TASK_QUESTION_MAX_OPTIONS,
    TASK_QUESTION_OPTION_MAX_CHARS,
)
from db import tool_wait_handle_store
from db.models import ToolWaitHandleKind, ToolWaitHandleStatus
from db.task_store import TaskStoreError, TaskValidationError

logger = logging.getLogger(__name__)

# Tool name (the model calls it, top-level or through ``tool_call``),
# durable chat-history message type (the run view renders it) and
# wait-handle kind (the resume bucket and the thread match on it). Kept as
# three names like chat/connect_requests.py, although two of them share a
# spelling.
TOOL_NAME = "ask_task_question"
MESSAGE_TYPE = "task_question"
HANDLE_KIND = ToolWaitHandleKind.TASK_QUESTION.value
# ``correlation_kind`` of a question's handle; ``correlation_id`` is the run id.
CORRELATION_KIND = "task_run"

# A question's status in the thread and in the transcript message. A
# handle that ended any other way than an answer (a Stop, a completion
# cancelling open handles) reads as cancelled.
QUESTION_STATUS_PENDING = "pending"
QUESTION_STATUS_ANSWERED = "answered"
QUESTION_STATUS_CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaskQuestionNotFoundError(TaskStoreError):
    """No such question in this task: the handle is missing, of another
    kind, another user's, or its run belongs to another task. All read the
    same (invariant 7)."""

    code = "question_not_found"

    def __init__(self, handle_id: str):
        super().__init__(f"Question not found: {handle_id}")
        self.handle_id = handle_id


class TaskQuestionResolvedError(TaskStoreError):
    """The question is no longer open: answered already, or cancelled (a
    Stop, or its run finished). ``details`` carries its thread ``status``
    and ``answer`` (None unless answered)."""

    code = "question_resolved"

    def __init__(self, status: str, answer: Optional[str] = None):
        if status == QUESTION_STATUS_ANSWERED:
            message = "This question has already been answered."
        else:
            message = "This question was cancelled; its task run is no longer waiting for it."
        super().__init__(message, details={"status": status, "answer": answer})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_question(question: Any, options: Any) -> tuple[str, list[str]]:
    """Check the arguments of ``ask_task_question``.

    Args:
        question: The question text (required).
        options: Suggested answers, a list of strings, or None.

    Returns:
        The stripped question and the stripped options, duplicates dropped
        in order (``[]`` when none were given).

    Raises:
        TaskValidationError: ``invalid_question``, ``question_too_long``,
        ``invalid_options``, ``too_many_options`` or ``option_too_long``
        (the size refusals with ``cap`` and ``actual``). The messages say
        what to change, for the model.
    """
    if not isinstance(question, str) or not question.strip():
        raise TaskValidationError(
            "invalid_question",
            "Pass the question to ask the user as a non-empty string in "
            "'question'.",
        )
    text = question.strip()
    if len(text) > TASK_QUESTION_MAX_CHARS:
        raise TaskValidationError(
            "question_too_long",
            f"The question is {len(text)} characters. Shorten it to at most "
            f"{TASK_QUESTION_MAX_CHARS} characters.",
            cap=TASK_QUESTION_MAX_CHARS,
            actual=len(text),
        )

    if options is None:
        return text, []
    if not isinstance(options, list):
        raise TaskValidationError(
            "invalid_options",
            "'options' must be a list of short answer strings, or left out.",
        )
    cleaned: list[str] = []
    for option in options:
        if not isinstance(option, str) or not option.strip():
            raise TaskValidationError(
                "invalid_options",
                "Every entry of 'options' must be a non-empty string. Remove "
                "the empty or non-text entries.",
            )
        label = option.strip()
        if len(label) > TASK_QUESTION_OPTION_MAX_CHARS:
            raise TaskValidationError(
                "option_too_long",
                f"An option is {len(label)} characters. Shorten each option "
                f"to at most {TASK_QUESTION_OPTION_MAX_CHARS} characters; "
                "put longer explanations in the question.",
                cap=TASK_QUESTION_OPTION_MAX_CHARS,
                actual=len(label),
            )
        cleaned.append(label)
    deduped = list(dict.fromkeys(cleaned))
    if len(deduped) > TASK_QUESTION_MAX_OPTIONS:
        raise TaskValidationError(
            "too_many_options",
            f"There are {len(deduped)} options. Offer at most "
            f"{TASK_QUESTION_MAX_OPTIONS}; the user can always type their own "
            "answer.",
            cap=TASK_QUESTION_MAX_OPTIONS,
            actual=len(deduped),
        )
    return text, deduped


def validate_answer(answer: Any) -> str:
    """Check the user's answer to a question.

    Returns:
        The stripped answer.

    Raises:
        TaskValidationError: ``invalid_answer`` for a non-string, a blank
        answer, or one over ``TASK_ANSWER_MAX_CHARS`` (with ``cap`` and
        ``actual``).
    """
    if not isinstance(answer, str):
        raise TaskValidationError("invalid_answer", "The answer must be text.")
    text = answer.strip()
    if not text:
        raise TaskValidationError("invalid_answer", "The answer cannot be empty.")
    if len(text) > TASK_ANSWER_MAX_CHARS:
        raise TaskValidationError(
            "invalid_answer",
            f"The answer is {len(text)} characters; the limit is "
            f"{TASK_ANSWER_MAX_CHARS}.",
            cap=TASK_ANSWER_MAX_CHARS,
            actual=len(text),
        )
    return text


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def question_status(handle: dict) -> str:
    """The thread status of a question's wait handle: ``pending``,
    ``answered`` (accepted) or ``cancelled`` (every other terminal status)."""
    status = handle.get("status")
    if status == ToolWaitHandleStatus.PENDING:
        return QUESTION_STATUS_PENDING
    if status == ToolWaitHandleStatus.ACCEPTED:
        return QUESTION_STATUS_ANSWERED
    return QUESTION_STATUS_CANCELLED


def _answer_of(handle: dict) -> Optional[str]:
    if question_status(handle) != QUESTION_STATUS_ANSWERED:
        return None
    answer = (handle.get("response") or {}).get("answer")
    return answer if isinstance(answer, str) else None


def question_view(handle: dict) -> dict:
    """A question as the thread and ``read_task_run`` show it.

    Shape::

        {"handle_id", "question", "options", "answer", "status",
         "asked_at", "answered_at"}

    ``answer`` and ``answered_at`` are None unless the question was
    answered; ``status`` is :func:`question_status`.
    """
    payload = handle.get("payload") or {}
    status = question_status(handle)
    answered = status == QUESTION_STATUS_ANSWERED
    return {
        "handle_id": handle["id"],
        "question": payload.get("question") or "",
        "options": list(payload.get("options") or []),
        "answer": _answer_of(handle),
        "status": status,
        "asked_at": handle.get("created_at"),
        "answered_at": handle.get("resolved_at") if answered else None,
    }


async def list_run_questions(runs: list[dict]) -> dict[str, list[dict]]:
    """The questions of a page of runs, keyed by run id, each list oldest
    first as :func:`question_view` dicts.

    One query for the whole page. Questions are grouped by the run's
    conversation (``run["conversation_id"]``), never by the handle's
    correlation. Every run id of ``runs`` is in the result, with ``[]``
    when it asked nothing.

    Args:
        runs: Run dicts (``db.task_store`` shape; ``id`` and
            ``conversation_id`` are read), already scoped to the caller.
    """
    by_run: dict[str, list[dict]] = {run["id"]: [] for run in runs}
    run_by_conversation = {run["conversation_id"]: run["id"] for run in runs}
    handles = await tool_wait_handle_store.list_handles_for_conversations(
        list(run_by_conversation), kind=HANDLE_KIND,
    )
    for handle in handles:
        run_id = run_by_conversation.get(handle["conversation_id"])
        if run_id is not None:
            by_run[run_id].append(question_view(handle))
    return by_run


# ---------------------------------------------------------------------------
# Transcript message
# ---------------------------------------------------------------------------


def make_question_message(handle: dict) -> dict:
    """The durable ``task_question`` transcript message for a new question.

    Shape: ``{"type": "task_question", "wait_handle_id", "status":
    "pending", "question", "options"}``; the loop adds the timestamp. The
    ``status`` values are the thread's (:func:`question_status`), and
    answering adds ``answer`` (:func:`rewrite_question_message`).
    """
    payload = handle.get("payload") or {}
    return {
        "type": MESSAGE_TYPE,
        "wait_handle_id": handle["id"],
        "status": QUESTION_STATUS_PENDING,
        "question": payload.get("question") or "",
        "options": list(payload.get("options") or []),
    }


def rewrite_question_message(
    conversation_id: str,
    handle_id: str,
    *,
    status: str,
    answer: Optional[str] = None,
) -> None:
    """Write a question's outcome into its transcript message: ``status``,
    plus ``answer`` when answered. Best-effort: logged, never raises."""
    from chat.storage import ChatStorage

    fields: dict[str, Any] = {"status": status}
    if status == QUESTION_STATUS_ANSWERED:
        fields["answer"] = answer
    try:
        ChatStorage.update_task_question_message(conversation_id, handle_id, fields)
    except Exception:
        logger.warning(
            "[task_question] transcript rewrite failed for %s (conversation=%s)",
            handle_id, conversation_id, exc_info=True,
        )


def mark_questions_cancelled(rows: list[dict]) -> None:
    """Rewrite the transcript message of every cancelled question among
    ``rows`` (wait-handle dicts just cancelled, of any kind) to
    ``cancelled``. Best-effort: logged, never raises."""
    for row in rows:
        if (
            row.get("kind") == HANDLE_KIND
            and question_status(row) == QUESTION_STATUS_CANCELLED
        ):
            rewrite_question_message(
                row["conversation_id"], row["id"],
                status=QUESTION_STATUS_CANCELLED,
            )


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


async def post_question(
    *,
    user_id: int,
    conversation_id: str,
    run_id: str,
    tool_id: str,
    question: str,
    options: list[str],
) -> dict:
    """Create the pending wait handle of a new question and return it.

    The arguments are already validated (:func:`validate_question`). The
    handle never expires: an unanswered question keeps its run ``waiting``
    until the user answers or stops it.

    Args:
        user_id: The run's owner.
        conversation_id: The run's conversation.
        run_id: The run (the handle's ``correlation_id``).
        tool_id: The provider-native id of the dangling tool_use, which the
            resume bucket matches the row on.
        question: The stripped question.
        options: The stripped, de-duplicated options.
    """
    return await tool_wait_handle_store.create_handle(
        user_id=user_id,
        conversation_id=conversation_id,
        kind=HANDLE_KIND,
        tool_id=tool_id,
        payload={"question": question, "options": list(options)},
        correlation_kind=CORRELATION_KIND,
        correlation_id=run_id,
    )
