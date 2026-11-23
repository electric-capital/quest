"""The AutoProject service: the rules behind every AutoProject route and the send
path.

AutoProject lifecycle: :func:`create_autoproject`, :func:`update_autoproject`,
the user's brief edit and restore (:func:`set_brief`, :func:`restore_brief`),
:func:`set_autoproject_archived`, the read acknowledgement
(:func:`mark_autoproject_read`), and the deletes (:func:`delete_autoproject`,
:func:`delete_run`), which stop live turns, cancel wait handles, close the run
conversations' open approval cards and sweep their directories. Each checks
the gate and ownership itself and publishes through chat/autoprojects/events.py;
the route-facing shapes are built in chat/autoprojects/views.py.

A run starts in two steps: ``POST /autoprojects/{id}/runs`` creates the run's
conversation (:func:`create_run_conversation`, an early refusal for the UI),
and the first ``send_message`` on that conversation allocates the
``autoproject_runs`` row (:func:`start_run`, the authoritative check).
:func:`start_run` is where the one-active-run-per-AutoProject rule is enforced
from the send path; the store's ``allocate_run`` does the transactional check,
so lifting the rule later is one change here and there.

:func:`answer_question` answers a run's open question (``ask_autoproject_question``,
chat/autoprojects/questions.py) and continues the run.

The startup sweeps (:func:`run_startup_sweeps`) settle what a restart
leaves behind: a ``running`` run whose in-memory turn is gone, and run
conversations created but never sent.

Every refusal is a ``db.autoproject_store.AutoProjectStoreError`` subclass with
a stable ``code``, so routes (through
``chat.autoprojects.http.autoproject_http_exception``) and the send path put it
straight into their error bodies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import shutil
from typing import Any, Optional

from chat.autoprojects import artifacts, events, runs
from chat.autoprojects.constants import (
    AUTOPROJECT_ABANDONED_RUN_CONVERSATION_MAX_AGE,
    AUTOPROJECT_RUN_ACTIVE_STATUSES,
    AUTOPROJECT_RUN_STATUS_FAILED,
    AUTOPROJECT_RUN_STATUS_WAITING,
    AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE,
    autoprojects_disabled_message,
)
from chat.autoprojects.questions import (
    CORRELATION_KIND,
    HANDLE_KIND,
    QUESTION_STATUS_ANSWERED,
    QUESTION_STATUS_CANCELLED,
    AutoProjectQuestionNotFoundError,
    AutoProjectQuestionResolvedError,
    question_view,
    rewrite_question_message,
    validate_answer,
)
from chat.gemini_api.session import remove_chat_session
from chat.realtime.socket import cancel_conversation_runs
from chat.storage import ChatStorage
from chat.wait_handles import resume as wait_resume
from chat.wait_handles.publish import publish_wait_handle_resolved
from db import autoproject_store, tool_wait_handle_store, user_store
from db.conversation_store import delete_abandoned_autoproject_conversations
from db.models import ToolWaitHandleStatus
from db.autoproject_store import (
    AutoProjectArchivedError,
    AutoProjectNotFoundError,
    AutoProjectRunActiveError,
    AutoProjectRunExistsError,
    AutoProjectStoreError,
)

logger = logging.getLogger(__name__)

# The ``error`` of a run a restart interrupted.
INTERRUPTED_BY_RESTART_ERROR = "interrupted by a restart"
# The ``result.reason`` of an action request closed because its run (or
# its AutoProject) was deleted.
RUN_DELETED_REASON = "autoproject_run_deleted"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AutoProjectsDisabledError(AutoProjectStoreError):
    """The ``autoprojects`` feature gate is closed for the user."""

    code = "autoprojects_disabled"

    def __init__(self) -> None:
        super().__init__(autoprojects_disabled_message())


class AutoProjectModelNotAllowedError(AutoProjectStoreError):
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


def require_autoprojects_enabled(user: dict) -> None:
    """Raise :class:`AutoProjectsDisabledError` unless the gate is open for ``user``."""
    from config.feature_gates import autoprojects_enabled_for

    if not autoprojects_enabled_for(user.get("email") or ""):
        raise AutoProjectsDisabledError()


# ---------------------------------------------------------------------------
# Starting a run
# ---------------------------------------------------------------------------


async def create_run_conversation(user: dict, autoproject_id: str) -> str:
    """Create the conversation for the AutoProject's next run (``POST
    /autoprojects/{id}/runs``).

    An early refusal for the UI: the gate, ownership, the archive flag and
    the one-active-run rule are checked here so the composer learns of them
    before the user sends, but nothing is reserved. :func:`start_run`
    re-checks all of it when the message arrives. The AutoProject's earlier
    run conversations that were never sent and are older than
    ``AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE`` are deleted first.

    Args:
        user: The caller's user dict.
        autoproject_id: The AutoProject.

    Returns:
        The new conversation id (``origin="autoproject"``, ``autoproject_id`` set, no run
        row yet).

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError (missing or foreign),
        AutoProjectArchivedError, AutoProjectRunActiveError.
    """
    autoproject = await require_owned_autoproject(user, autoproject_id)
    if autoproject["archived"]:
        raise AutoProjectArchivedError(autoproject_id)
    active = await autoproject_store.get_active_run(autoproject_id)
    if active is not None:
        raise AutoProjectRunActiveError(active)

    # Earlier starts of this AutoProject that never sent: drop them so
    # repeated starts cannot pile up conversations between restarts.
    try:
        await sweep_abandoned_run_conversations(
            autoproject_id=autoproject_id,
            max_age=AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE,
        )
    except Exception:
        logger.exception(
            "Sweeping unsent run conversations of AutoProject %s failed", autoproject_id,
        )
    return await ChatStorage.create_autoproject_run_conversation(user["id"], autoproject_id)


@dataclass(frozen=True)
class StartedRun:
    """A run :func:`start_run` allocated, with what its turn runs on."""

    run: dict
    autoproject: dict
    model: str
    thinking_effort: Optional[str]


def _resolve_run_model(composer_model: Optional[str], autoproject: dict) -> str:
    """The run's model: the composer's pick, else the AutoProject's default, else
    the server default, with the deprecated-model remap applied the way
    ``run_conversation_turn`` applies it (so the run row records the model
    that actually runs)."""
    from chat.gemini_api.constants import _DEPRECATED_MODELS
    from config.server_config import load_server_config

    model = composer_model or autoproject.get("model")
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

    THE one place the send path enforces one active run per AutoProject and
    one user message per run conversation. Called
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
        AutoProjectsDisabledError, AutoProjectRunExistsError (the conversation already has
        its message), AutoProjectNotFoundError, AutoProjectModelNotAllowedError,
        AutoProjectArchivedError, AutoProjectRunActiveError, AutoProjectValidationError
        ``invalid_prompt``.
    """
    autoproject_id = conversation.get("autoproject_id")
    if not autoproject_id:
        raise ValueError(
            f"Conversation {conversation.get('id')} is not an AutoProject run conversation."
        )
    require_autoprojects_enabled(user)

    # A run conversation takes exactly one user message. The
    # unique index on autoproject_runs.conversation_id (allocate_run) catches a
    # race between two first sends.
    if int(conversation.get("last_message_seq") or 0) > 0:
        raise AutoProjectRunExistsError(conversation["id"])

    autoproject = await autoproject_store.get_autoproject(user["id"], autoproject_id)
    if autoproject is None:
        raise AutoProjectNotFoundError(autoproject_id)

    run_model = _resolve_run_model(model, autoproject)
    run_effort = thinking_effort or autoproject.get("thinking_effort")

    # An AutoProject run is always private.
    from config.model_selection import is_model_allowed

    if not is_model_allowed(run_model, public=False):
        raise AutoProjectModelNotAllowedError(run_model)

    run = await autoproject_store.allocate_run(
        autoproject_id,
        conversation_id=conversation["id"],
        prompt=prompt,
        model=run_model,
    )
    logger.info(
        "AutoProject run started (autoproject=%s, run=%s, seq=%s, conversation=%s, model=%s)",
        autoproject_id, run["id"], run["seq"], conversation["id"], run_model,
    )
    # The AutoProject gained an active run (its page and list row both changed).
    await events.publish_autoproject_state(autoproject_id)
    return StartedRun(
        run=run, autoproject=autoproject, model=run_model, thinking_effort=run_effort,
    )


async def fail_unstarted_run(run_id: str, error: str) -> None:
    """Mark a run ``failed`` whose turn never started (the send path gave
    up after :func:`start_run`) and settle it like every finished run
    (``runs.end_run``: the AutoProject marked unread, events published).
    Best-effort: logged, never raises."""
    try:
        updated = await runs.end_run(run_id, AUTOPROJECT_RUN_STATUS_FAILED, error=error)
    except Exception:
        logger.exception("Failing unstarted AutoProject run %s failed", run_id)
        return
    if updated is None:
        logger.warning(
            "Unstarted AutoProject run %s was not active; left as it was", run_id,
        )
        return
    logger.info("AutoProject run %s failed before its turn started: %s", run_id, error)


# ---------------------------------------------------------------------------
# Answering a run's question
# ---------------------------------------------------------------------------


async def answer_question(
    user: dict, autoproject_id: str, handle_id: str, answer: Any, *, app,
) -> dict:
    """Answer an AutoProject run's open question (``POST
    /autoprojects/{id}/questions/{handle_id}/answer``) and continue the run.

    Resolves the question's ``autoproject_question`` wait handle with the answer,
    writes the outcome into the run's transcript message, publishes the
    resolution and the AutoProject page change, and kicks the run's resume. While
    another card of the same tool-call batch (an approval card) is still
    pending, the resume gate holds the run until that one is resolved too.

    Args:
        user: The caller's user dict.
        autoproject_id: The AutoProject the question belongs to.
        handle_id: The question's wait-handle id.
        answer: The user's answer (validated here).
        app: The FastAPI app, for the headless resume.

    Returns:
        The answered question (``chat.autoprojects.questions.question_view``).

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError (missing or foreign AutoProject),
        AutoProjectQuestionNotFoundError (missing, foreign, or not a question of
        this AutoProject), AutoProjectQuestionResolvedError (already answered, or
        cancelled: a Stop, or its run is no longer active),
        AutoProjectValidationError ``invalid_answer``.
    """
    await require_owned_autoproject(user, autoproject_id)

    # A missing handle, another kind, another user's, or one of another
    # AutoProject's run all read the same.
    handle = await tool_wait_handle_store.get_handle_for_user(user["id"], handle_id)
    if handle is None or handle.get("kind") != HANDLE_KIND:
        raise AutoProjectQuestionNotFoundError(handle_id)
    conversation_id = handle["conversation_id"]
    run = await autoproject_store.get_run_by_conversation(conversation_id)
    if run is None or run["autoproject_id"] != autoproject_id:
        raise AutoProjectQuestionNotFoundError(handle_id)

    if handle["status"] != ToolWaitHandleStatus.PENDING:
        view = question_view(handle)
        raise AutoProjectQuestionResolvedError(view["status"], view["answer"])
    if run["status"] not in AUTOPROJECT_RUN_ACTIVE_STATUSES:
        # A pending handle left on a finished run: nothing would read the
        # answer.
        raise AutoProjectQuestionResolvedError(QUESTION_STATUS_CANCELLED)

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
            raise AutoProjectQuestionNotFoundError(handle_id)
        view = question_view(current)
        raise AutoProjectQuestionResolvedError(view["status"], view["answer"])
    logger.info(
        "AutoProject question %s answered (autoproject=%s, run=%s, conversation=%s, user=%s)",
        handle_id, autoproject_id, run["id"], conversation_id, user["id"],
    )

    rewrite_question_message(
        conversation_id, handle_id, status=QUESTION_STATUS_ANSWERED, answer=answer,
    )

    publish_wait_handle_resolved(
        user["id"], updated, response={"answer": answer},
    )
    # The AutoProject page changed (the question shows its answer); the list row did
    # not (the run is still waiting until its resume starts).
    try:
        await autoproject_store.bump_event_seq(autoproject_id)
    except Exception:
        logger.warning(
            "Bumping event_seq of AutoProject %s after an answer failed",
            autoproject_id, exc_info=True,
        )
    await events.publish_autoproject_state(autoproject_id, list_changed=False)

    try:
        wait_resume.maybe_kick_resume(app, user, conversation_id)
    except Exception:
        logger.exception(
            "Failed to kick the resume of AutoProject run %s (conversation=%s)",
            run["id"], conversation_id,
        )

    return question_view(updated)


# ---------------------------------------------------------------------------
# AutoProject CRUD, brief, archive, read
# ---------------------------------------------------------------------------


async def require_owned_autoproject(user: dict, autoproject_id: str) -> dict:
    """The AutoProject ``user`` owns: the gate first, then ownership.

    The one access check of every AutoProject route and service function.

    Raises:
        AutoProjectsDisabledError; AutoProjectNotFoundError for a missing
        AutoProject and for one owned by someone else (the same text).
    """
    require_autoprojects_enabled(user)
    autoproject = await autoproject_store.get_autoproject(user["id"], autoproject_id)
    if autoproject is None:
        raise AutoProjectNotFoundError(autoproject_id)
    return autoproject


def _check_model_allowed(model: Any) -> None:
    """Refuse an AutoProject default model unticked for private conversations. A
    None or malformed value is left to the store's shape check."""
    if not isinstance(model, str) or not model.strip():
        return
    from config.model_selection import is_model_allowed

    if not is_model_allowed(model.strip(), public=False):
        raise AutoProjectModelNotAllowedError(model.strip())


async def create_autoproject(
    user: dict,
    *,
    title: Optional[str] = None,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    thinking_effort: Optional[str] = None,
) -> dict:
    """Create an AutoProject (``POST /autoprojects``) and publish ``autoproject_list_changed``.

    The title is the user's ``title`` when given (``title_is_default``
    False); else cut from ``prompt`` (the New AutoProject composer's first
    message, which is not stored: it only names the AutoProject, so its first run
    may still rename it); else ``autoproject_store.DEFAULT_AUTOPROJECT_TITLE``. Both
    fallbacks keep ``title_is_default`` True.

    Returns:
        The AutoProject dict.

    Raises:
        AutoProjectsDisabledError, AutoProjectModelNotAllowedError, AutoProjectValidationError
        (``invalid_title``, ``invalid_model``, ``invalid_thinking_effort``),
        AutoProjectCapReachedError.
    """
    require_autoprojects_enabled(user)
    if title is not None:
        autoproject_title, title_is_default = title, False
    elif prompt is not None:
        autoproject_title, title_is_default = autoproject_store.title_from_prompt(prompt), True
    else:
        autoproject_title, title_is_default = autoproject_store.DEFAULT_AUTOPROJECT_TITLE, True
    _check_model_allowed(model)
    autoproject = await autoproject_store.create_autoproject(
        user["id"],
        title=autoproject_title,
        title_is_default=title_is_default,
        model=model,
        thinking_effort=thinking_effort,
    )
    logger.info("AutoProject created (autoproject=%s, user=%s)", autoproject["id"], user["id"])
    events.publish_autoproject_list_changed(user["id"])
    return autoproject


async def update_autoproject(
    user: dict,
    autoproject_id: str,
    *,
    title: Any = autoproject_store.UNSET,
    model: Any = autoproject_store.UNSET,
    thinking_effort: Any = autoproject_store.UNSET,
) -> dict:
    """Edit the user-owned fields (``PATCH /autoprojects/{id}``); fields left at
    ``autoproject_store.UNSET`` stay, None clears ``model`` / ``thinking_effort``
    (a None ``title`` is refused by the store). Publishes ``autoproject_changed``
    and ``autoproject_list_changed`` when something changed.

    Returns:
        The AutoProject dict.

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError, AutoProjectModelNotAllowedError,
        AutoProjectValidationError.
    """
    before = await require_owned_autoproject(user, autoproject_id)
    if model is not autoproject_store.UNSET:
        _check_model_allowed(model)
    autoproject = await autoproject_store.update_autoproject(
        user["id"], autoproject_id,
        title=title, model=model, thinking_effort=thinking_effort,
    )
    if autoproject["event_seq"] != before["event_seq"]:
        await events.publish_autoproject_state(autoproject_id)
    return autoproject


async def set_brief(
    user: dict, autoproject_id: str, brief: Any, *, expected_brief_version: int,
) -> dict:
    """The user's brief edit (``PUT /autoprojects/{id}/brief``), allowed while a
    run is active (the run's own write then gets ``brief_conflict``).
    Publishes ``autoproject_changed`` (the list row does not change).

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError, StaleBriefError,
        AutoProjectValidationError (``invalid_brief``, ``brief_too_long``).
    """
    await require_owned_autoproject(user, autoproject_id)
    state = await autoproject_store.set_brief(
        autoproject_id, brief, expected_brief_version=expected_brief_version, source="ui",
    )
    await events.publish_autoproject_state(autoproject_id, list_changed=False)
    return state


async def restore_brief(
    user: dict, autoproject_id: str, version: int, *, expected_brief_version: int,
) -> dict:
    """Write stored version ``version`` as a new brief version (``POST
    /autoprojects/{id}/brief/versions/{version}/restore``, source
    ``restore:<version>``). Publishes ``autoproject_changed``.

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError, BriefVersionNotFoundError,
        StaleBriefError, AutoProjectValidationError (``brief_too_long``).
    """
    await require_owned_autoproject(user, autoproject_id)
    state = await autoproject_store.restore_brief_version(
        autoproject_id, version, expected_brief_version=expected_brief_version,
    )
    await events.publish_autoproject_state(autoproject_id, list_changed=False)
    return state


async def set_autoproject_archived(user: dict, autoproject_id: str, archived: bool) -> dict:
    """Archive or unarchive an AutoProject (``PUT /autoprojects/{id}/archive`` /
    ``unarchive``) and publish ``autoproject_changed`` and ``autoproject_list_changed``.

    Archiving is refused while a run is active, and marks the AutoProject read up
    to its ``event_seq`` (as archiving a conversation does): an archived
    AutoProject carries no unread dot. Unarchiving counts against the AutoProject cap.

    Returns:
        The AutoProject dict.

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError, AutoProjectRunActiveError (archive),
        AutoProjectCapReachedError (unarchive).
    """
    require_autoprojects_enabled(user)
    autoproject = await autoproject_store.set_autoproject_archived(
        user["id"], autoproject_id, archived,
    )
    if archived and autoproject["read_seq"] < autoproject["event_seq"]:
        await autoproject_store.mark_autoproject_read(
            user["id"], autoproject_id, up_to_seq=autoproject["event_seq"],
        )
        autoproject = (
            await autoproject_store.get_autoproject(user["id"], autoproject_id)
            or autoproject
        )
    logger.info(
        "AutoProject %s (autoproject=%s, user=%s)",
        "archived" if archived else "unarchived", autoproject_id, user["id"],
    )
    await events.publish_autoproject_state(autoproject_id)
    return autoproject


async def mark_autoproject_read(user: dict, autoproject_id: str, *, seq: int) -> dict:
    """Mark the AutoProject read up to ``seq`` (``POST /autoprojects/{id}/read``,
    sent by a client showing the AutoProject page in a focused window) and publish
    ``autoproject_list_changed`` when ``read_seq`` moved.

    An admin impersonating the owner changes nothing: the admin looking at
    the AutoProject page is not the owner reading it.

    Returns:
        The AutoProject dict after the acknowledgement.

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError.
    """
    autoproject = await require_owned_autoproject(user, autoproject_id)
    if "_impersonator_uid" in user:
        return autoproject
    moved = await autoproject_store.mark_autoproject_read(
        user["id"], autoproject_id, up_to_seq=seq,
    )
    if not moved:
        return autoproject
    events.publish_autoproject_list_changed(user["id"])
    return await autoproject_store.get_autoproject(user["id"], autoproject_id) or autoproject


# ---------------------------------------------------------------------------
# Deleting an AutoProject or a run
# ---------------------------------------------------------------------------

# How long a delete waits for the cancelled live turns of the doomed
# conversations to unwind (a cancelled turn flushes its transcript on the
# way out; the directory sweep must come after that flush).
_DELETE_CANCEL_WAIT_SECONDS = 10.0


async def _cancel_live_runs(conversation_ids) -> None:
    """Cancel the in-flight send run and headless resume of each
    conversation and wait (bounded) for them to finish."""
    cancelled: list[asyncio.Task] = []
    for conversation_id in conversation_ids:
        cancelled.extend(cancel_conversation_runs(conversation_id))
    if not cancelled:
        return
    _done, pending = await asyncio.wait(cancelled, timeout=_DELETE_CANCEL_WAIT_SECONDS)
    if pending:
        logger.warning(
            "%d cancelled AutoProject run turn(s) did not finish within %.0fs; "
            "sweeping their conversations anyway",
            len(pending), _DELETE_CANCEL_WAIT_SECONDS,
        )


async def _close_conversation_cards(user_id: int, conversation_ids) -> list[int]:
    """Close the open cards of conversations about to be deleted
    (``runs.close_conversation_cards``, reason ``autoproject_run_deleted``)."""
    return await runs.close_conversation_cards(
        user_id, conversation_ids, reason=RUN_DELETED_REASON,
    )


async def _sweep_conversations(user_id: int, conversation_ids) -> None:
    """After the rows are gone: cancel any live turn again (a run started
    in the race window), drop the in-memory sessions and remove each
    conversation's ``CHATS_DIR`` directory. Best-effort per id."""
    ids = list(dict.fromkeys(conversation_ids))
    await _cancel_live_runs(ids)
    for conversation_id in ids:
        try:
            remove_chat_session(user_id, conversation_id)
            conversation_dir = ChatStorage.get_conversation_dir(conversation_id)
            await asyncio.to_thread(shutil.rmtree, conversation_dir, ignore_errors=True)
        except Exception:
            logger.exception(
                "Removing AutoProject run conversation %s failed", conversation_id,
            )


async def delete_autoproject(user: dict, autoproject_id: str) -> None:
    """Delete an AutoProject with its runs, their conversations and its workspace
    (``DELETE /autoprojects/{id}``).

    1. Cancel every live turn of the AutoProject's conversations and wait for
       them (bounded), then stop the active run (``stopped``, its handles
       cancelled).
    2. Cancel the pending wait handles and close the open approval cards of
       every run conversation.
    3. Delete the rows (runs, brief versions, conversations and the
       AutoProject's Quest Docs and Quest Tables cascade; the docs and
       tables are captured first, ``chat/autoprojects/artifacts.py``).
    4. Sweep the directories of those docs and tables and tell their
       audiences, then the conversations (live turns again, in-memory
       sessions, ``CHATS_DIR`` directories), then ``AUTOPROJECTS_DIR/<id>``.
    5. Publish ``autoproject_changed`` (no active run; an open AutoProject
       page refetches and gets 404) and ``autoproject_list_changed``
       (``request_count_changed`` went out when cards were closed).

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError.
    """
    autoproject = await require_owned_autoproject(user, autoproject_id)
    user_id = user["id"]

    conversation_ids = await autoproject_store.list_autoproject_conversation_ids(autoproject_id)
    await _cancel_live_runs(conversation_ids)
    active = await autoproject_store.get_active_run(autoproject_id)
    if active is not None:
        await runs.stop_autoproject_run(user_id, active["conversation_id"])
    await _close_conversation_cards(user_id, conversation_ids)
    autoproject = await autoproject_store.get_autoproject(user_id, autoproject_id) or autoproject

    owned_artifacts = await artifacts.capture(autoproject_id)
    deleted_ids = await autoproject_store.delete_autoproject(user_id, autoproject_id)
    known_ids = set(conversation_ids)
    late_ids = [cid for cid in deleted_ids if cid not in known_ids]
    if late_ids:
        # A run conversation created between the read and the delete.
        await _close_conversation_cards(user_id, late_ids)
    logger.info(
        "AutoProject deleted (autoproject=%s, user=%s, conversations=%d)",
        autoproject_id, user_id, len(deleted_ids),
    )

    await artifacts.sweep(owned_artifacts, user_id)
    await _sweep_conversations(user_id, list(conversation_ids) + late_ids)
    try:
        autoproject_dir = ChatStorage.get_autoproject_dir(autoproject_id)
        await asyncio.to_thread(shutil.rmtree, autoproject_dir, ignore_errors=True)
    except Exception:
        logger.exception("Removing the directory of AutoProject %s failed", autoproject_id)

    # The delete bumped event_seq once more before removing the row.
    events.publish_autoproject_changed(
        user_id, {**autoproject, "event_seq": autoproject["event_seq"] + 1}, None,
    )
    events.publish_autoproject_list_changed(user_id)


async def delete_run(user: dict, autoproject_id: str, run_id: str) -> None:
    """Delete a finished run's log entry and its conversation (``DELETE
    /autoprojects/{id}/runs/{run_id}``). Its ``seq`` is never reused; docs, tables
    and AutoProject files it created stay.

    The store delete is the atomic refusal; then the conversation's live
    turn (one still flushing after its completion), pending wait handles
    and open approval cards are closed, its in-memory session dropped and
    its ``CHATS_DIR`` directory removed. Publishes ``autoproject_changed`` and
    ``autoproject_list_changed`` (and ``request_count_changed`` when cards
    were closed).

    Raises:
        AutoProjectsDisabledError, AutoProjectNotFoundError, AutoProjectRunNotFoundError (also a
        run of another AutoProject), AutoProjectRunActiveError.
    """
    require_autoprojects_enabled(user)
    user_id = user["id"]
    conversation_id = await autoproject_store.delete_run(user_id, autoproject_id, run_id)
    logger.info(
        "AutoProject run deleted (autoproject=%s, run=%s, conversation=%s, user=%s)",
        autoproject_id, run_id, conversation_id, user_id,
    )
    await _cancel_live_runs([conversation_id])
    await _close_conversation_cards(user_id, [conversation_id])
    await _sweep_conversations(user_id, [conversation_id])
    await events.publish_autoproject_state(autoproject_id)


# ---------------------------------------------------------------------------
# Startup sweeps
# ---------------------------------------------------------------------------


async def fail_interrupted_runs_at_startup() -> list[dict]:
    """Fail every ``running`` run: its in-memory turn did not survive the
    restart. Each is settled like every finished run
    (``runs.settle_finished_run``: its open cards closed, its AutoProject
    marked unread, since no client is focused on it at startup).

    Returns:
        The failed runs.
    """
    failed = await autoproject_store.fail_interrupted_runs(error=INTERRUPTED_BY_RESTART_ERROR)
    for run in failed:
        await runs.settle_finished_run(run, mark_unread=True)
    if failed:
        logger.info("Failed %d AutoProject run(s) interrupted by a restart", len(failed))
    return failed


async def resume_waiting_runs_at_startup(app=None) -> list[dict]:
    """Settle the ``waiting`` runs a restart left with nothing to wait on.

    A waiting run normally has a pending wait handle (a question or a card)
    whose resolution resumes it. One with none was answered (or its card
    resolved) just before the restart and lost its in-memory resume: its
    resume is kicked again (``maybe_kick_resume`` with the owner's user
    dict). When there is nothing to resume (no ``app``, the owner is gone,
    or the transcript has no dangling wait), the run is failed "interrupted
    by a restart" and settled. A waiting run with a pending handle is left
    alone.

    Returns:
        The runs failed here (resumed ones are not returned).
    """

    waiting = await autoproject_store.list_runs_with_status(AUTOPROJECT_RUN_STATUS_WAITING)
    if not waiting:
        return []
    pending = await tool_wait_handle_store.list_pending_for_conversations(
        [run["conversation_id"] for run in waiting],
    )
    held = {handle["conversation_id"] for handle in pending}
    failed: list[dict] = []
    for run in waiting:
        if run["conversation_id"] in held:
            continue
        kicked = False
        try:
            autoproject = await autoproject_store.get_autoproject_by_id(run["autoproject_id"])
            user = (
                await user_store.get_user_by_id(autoproject["user_id"])
                if autoproject is not None else None
            )
            if app is not None and user is not None:
                kicked = wait_resume.maybe_kick_resume(
                    app, user, run["conversation_id"],
                ) is not None
        except Exception:
            logger.exception(
                "Resuming waiting AutoProject run %s at startup failed", run["id"],
            )
        if kicked:
            logger.info(
                "Resumed waiting AutoProject run %s after a restart (conversation=%s)",
                run["id"], run["conversation_id"],
            )
            continue
        try:
            ended = await runs.end_run(
                run["id"], AUTOPROJECT_RUN_STATUS_FAILED, error=INTERRUPTED_BY_RESTART_ERROR,
            )
        except Exception:
            logger.exception("Failing waiting AutoProject run %s at startup failed", run["id"])
            continue
        if ended is not None:
            failed.append(ended)
    if failed:
        logger.info(
            "Failed %d waiting AutoProject run(s) left with nothing to resume by a restart",
            len(failed),
        )
    return failed


async def sweep_abandoned_run_conversations(
    *,
    now: Optional[datetime] = None,
    autoproject_id: Optional[str] = None,
    max_age: timedelta = AUTOPROJECT_ABANDONED_RUN_CONVERSATION_MAX_AGE,
) -> list[str]:
    """Delete run conversations created but never sent (no run row) once
    they are older than ``max_age``, rows first, then their ``CHATS_DIR``
    directories (best-effort per id). With ``autoproject_id``, only that
    AutoProject's, and only those with no message (creating a run
    conversation calls it so).

    Returns:
        The deleted conversation ids.
    """
    cutoff = (now or datetime.now(timezone.utc)) - max_age
    conversation_ids = await delete_abandoned_autoproject_conversations(
        cutoff, autoproject_id=autoproject_id,
    )
    for conversation_id in conversation_ids:
        try:
            conversation_dir = ChatStorage.get_conversation_dir(conversation_id)
            await asyncio.to_thread(shutil.rmtree, conversation_dir, ignore_errors=True)
        except Exception:
            logger.exception(
                "Removing abandoned AutoProject run conversation %s failed", conversation_id,
            )
    if conversation_ids:
        logger.info(
            "Deleted %d abandoned AutoProject run conversation(s)", len(conversation_ids),
        )
    return conversation_ids


async def run_startup_sweeps(app=None) -> None:
    """Run the startup sweeps; one failing does not skip the others.
    ``app`` (the FastAPI app) lets a waiting run resume
    (:func:`resume_waiting_runs_at_startup`)."""
    try:
        await fail_interrupted_runs_at_startup()
    except Exception:
        logger.exception("Failing interrupted AutoProject runs at startup failed")
    try:
        await resume_waiting_runs_at_startup(app)
    except Exception:
        logger.exception("Settling waiting AutoProject runs at startup failed")
    try:
        await sweep_abandoned_run_conversations()
    except Exception:
        logger.exception("Sweeping abandoned AutoProject run conversations failed")
