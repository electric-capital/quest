"""Quest Tasks data access layer.

Owns every write to the ``tasks``, ``task_runs`` and ``task_brief_versions``
tables (spec issue #213, sections 3 and 4). Async functions that each open
a fresh ``AsyncSessionLocal()`` session (imported at module level so tests
can monkeypatch it) and return plain dicts with ISO datetimes; the shapes
are documented on :func:`_task_to_dict`, :func:`_run_to_dict` and
:func:`_brief_state`.

Scoping: functions taking ``user_id`` are owner-scoped, and a task owned by
someone else raises (or reads as) exactly what a missing task does
(invariant 7). The rest take a task or run id only and are for callers that
already did the access check (``require_owned_task`` in routes, the run's
own task resolved from ``ToolContext`` in tools, the run driver).

Concurrency: SQLite takes its single write lock at a transaction's first
write, and reads before it see whatever was last committed. Every
check-then-write here (the task cap, invariant 6's one active run, the
brief's version token, the conversation sweep of a task delete) therefore
issues its UPDATE or INSERT first and checks inside the same transaction,
rolling back on refusal, so two concurrent writers cannot both pass a check.

Counters on the ``tasks`` row: ``event_seq`` is bumped by every write that
changes what the thread shows and by task edits (the realtime
``task_changed`` event carries it), ``last_activity_at`` moves on create and
on every run write (it orders the sidebar), and the unread pair follows the
conversation scheme (``unread_seq > read_seq``, both only move forward).

Caps and statuses come from chat/tasks/constants.py. The filesystem (the
task workspace, the run conversations' directories) is never touched here;
delete functions return the ids the caller sweeps.
"""

from collections.abc import Iterable
from datetime import datetime, timezone
import re
from typing import Any, Optional
import uuid

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from chat.tasks.constants import (
    MAX_ACTIVE_TASKS_PER_USER,
    TASK_BRIEF_MAX_CHARS,
    TASK_BRIEF_MAX_VERSIONS,
    TASK_CONTEXT_MAX_CHARS,
    TASK_INSTRUCTIONS_MAX_CHARS,
    TASK_RUN_ACTIVE_STATUSES,
    TASK_RUN_FINISHED_STATUSES,
    TASK_RUN_NOTES_MAX_CHARS,
    TASK_RUN_RESULT_MAX_CHARS,
    TASK_RUN_STATUS_COMPLETED,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STOPPED,
    TASK_RUN_STATUS_WAITING,
    TASK_RUN_SUMMARY_MAX_CHARS,
    TASK_TITLE_MAX_CHARS,
    task_not_found_message,
)
from db.engine import AsyncSessionLocal
from db.models import Conversation, Task, TaskBriefVersion, TaskRun

# Fallback title when a first prompt has no text to cut one from.
DEFAULT_TASK_TITLE = "Untitled task"

# Length bounds of the free-form model fields (the column sizes).
_MODEL_MAX_CHARS = 100
_THINKING_EFFORT_MAX_CHARS = 20

# Page size bounds: the thread page (list_runs_page) and the plain listings
# (list_runs, and list_context_runs, from which the context builder reads
# the window and the index, about TASK_LOG_WINDOW_MAX_ENTRIES +
# TASK_LOG_INDEX_MAX_ENTRIES rows, in one call).
_RUNS_PAGE_MAX_LIMIT = 100
_LIST_RUNS_MAX_LIMIT = 500

# Statuses set_run_status writes (``completed`` goes through complete_run).
_SETTABLE_RUN_STATUSES = frozenset({
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_WAITING,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_STOPPED,
})

# ``task_brief_versions.source``: run:<run id> | ui | restore:<version>.
_BRIEF_SOURCE_RE = re.compile(r"^(?:ui|run:[0-9A-Za-z-]{1,36}|restore:\d{1,9})$")


class _Unset:
    """Type of :data:`UNSET`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


# "Leave this field alone" for update_task(); None means "clear" where a
# field is nullable.
UNSET: Any = _Unset()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaskStoreError(Exception):
    """Base of every refusal this module raises.

    ``code`` is the stable machine code routes and tools put straight into
    their error bodies, ``message`` the human text, ``details`` any extra
    fields of the body (caps, lengths, the active run).
    """

    code = "task_store_error"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[dict] = None,
    ):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details: dict = dict(details or {})


class TaskNotFoundError(TaskStoreError):
    """The task does not exist, or belongs to someone else (same text)."""

    code = "task_not_found"

    def __init__(self, task_id: str):
        super().__init__(task_not_found_message(task_id))
        self.task_id = task_id


class TaskCapReachedError(TaskStoreError):
    """The user already has ``MAX_ACTIVE_TASKS_PER_USER`` unarchived tasks."""

    code = "tasks_cap_reached"

    def __init__(self, cap: int):
        super().__init__(
            f"Task limit reached: you can have at most {cap} tasks that are "
            "not archived. Archive or delete one first.",
            details={"cap": cap},
        )


class TaskValidationError(TaskStoreError, ValueError):
    """Invalid input; ``code`` names the field (``invalid_title``,
    ``brief_too_long``, ...). ``details`` carries ``cap`` and ``actual``
    (lengths) for the size refusals."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        cap: Optional[int] = None,
        actual: Optional[int] = None,
    ):
        details = {}
        if cap is not None:
            details["cap"] = cap
        if actual is not None:
            details["actual"] = actual
        super().__init__(message, code=code, details=details)


class TaskArchivedError(TaskStoreError):
    """The task is archived, so it takes no new run."""

    code = "task_archived"

    def __init__(self, task_id: str):
        super().__init__(
            "This task is archived. Unarchive it to start a new run.",
            details={"task_id": task_id},
        )


class TaskRunActiveError(TaskStoreError):
    """The task has an active run (``running`` or ``waiting``) and the
    operation needs none (invariant 6, archive, run delete).
    ``details["active_run"]`` is ``{id, seq, status}``."""

    code = "task_run_active"

    def __init__(self, active_run: dict, message: Optional[str] = None):
        summary = {
            "id": active_run["id"],
            "seq": active_run["seq"],
            "status": active_run["status"],
        }
        super().__init__(
            message or (
                f"Run {summary['seq']} of this task is still {summary['status']}. "
                "Wait for it to finish or stop it first."
            ),
            details={"active_run": summary},
        )
        self.active_run = summary


class TaskRunNotFoundError(TaskStoreError):
    """The run does not exist (in that task)."""

    code = "task_run_not_found"

    def __init__(self, run_id: str):
        super().__init__(f"Task run not found: {run_id}")
        self.run_id = run_id


class TaskRunFinishedError(TaskStoreError):
    """A write that needs an active run hit a finished one."""

    code = "task_run_finished"

    def __init__(self, run_id: str, status: str):
        super().__init__(
            f"This task run has already finished ({status}).",
            details={"status": status},
        )
        self.run_id = run_id
        self.status = status


class TaskRunExistsError(TaskStoreError):
    """The conversation already has its run: a run conversation takes
    exactly one user message (invariant 1)."""

    code = "task_run_single_message"

    def __init__(self, conversation_id: str):
        super().__init__(
            "This task run already has its message. Send the next prompt "
            "from the task instead.",
            details={"conversation_id": conversation_id},
        )


class StaleBriefError(TaskStoreError):
    """``expected_version`` is not the brief's current version (someone
    wrote it in between). ``current`` is ``{brief, brief_version,
    brief_updated_at}`` so the caller can show or merge it without a second
    read."""

    code = "stale_brief"

    def __init__(self, current: dict):
        super().__init__(
            "The brief was changed since it was loaded.",
            details={"current": current},
        )
        self.current = current


class BriefVersionNotFoundError(TaskStoreError):
    """No stored brief version with that number (never written, or pruned)."""

    code = "brief_version_not_found"

    def __init__(self, version: int):
        super().__init__(f"Brief version not found: {version}")
        self.version = version


# ---------------------------------------------------------------------------
# Validation (pure; callers may run these before writing anything)
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    """Collapse every whitespace run (newlines included) to one space."""
    return " ".join(text.split())


def _check_len(text: str, cap: int, code: str, label: str) -> str:
    if len(text) > cap:
        raise TaskValidationError(
            code,
            f"{label} is {len(text)} characters; the limit is {cap}.",
            cap=cap,
            actual=len(text),
        )
    return text


def normalize_title(title) -> str:
    """Return the title stripped with whitespace collapsed to single spaces.

    Raises:
        TaskValidationError ``invalid_title``: not a string, empty, or over
            ``TASK_TITLE_MAX_CHARS`` after normalizing.
    """
    if not isinstance(title, str):
        raise TaskValidationError("invalid_title", "The task title must be a string.")
    clean = _collapse(title)
    if not clean:
        raise TaskValidationError("invalid_title", "The task title cannot be empty.")
    return _check_len(clean, TASK_TITLE_MAX_CHARS, "invalid_title", "The task title")


def title_from_prompt(prompt: str) -> str:
    """The default title cut from a first prompt.

    The first non-empty line with whitespace collapsed, cut to
    ``TASK_TITLE_MAX_CHARS`` with a trailing "…" when cut;
    :data:`DEFAULT_TASK_TITLE` when the prompt has no text.
    """
    if not isinstance(prompt, str):
        return DEFAULT_TASK_TITLE
    for line in prompt.splitlines():
        clean = _collapse(line)
        if clean:
            break
    else:
        return DEFAULT_TASK_TITLE
    if len(clean) <= TASK_TITLE_MAX_CHARS:
        return clean
    return clean[: TASK_TITLE_MAX_CHARS - 1].rstrip() + "…"


def validate_instructions(text) -> str:
    """Return the stripped instructions (None -> "").

    Raises:
        TaskValidationError ``invalid_instructions``: not a string or over
            ``TASK_INSTRUCTIONS_MAX_CHARS``.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        raise TaskValidationError(
            "invalid_instructions", "The task instructions must be a string."
        )
    return _check_len(
        text.strip(), TASK_INSTRUCTIONS_MAX_CHARS, "invalid_instructions",
        "The task instructions",
    )


def validate_brief(content) -> str:
    """Return the stripped brief (empty allowed).

    Raises:
        TaskValidationError ``invalid_brief`` (not a string) or
            ``brief_too_long`` (over ``TASK_BRIEF_MAX_CHARS``).
    """
    if not isinstance(content, str):
        raise TaskValidationError("invalid_brief", "The brief must be a string.")
    return _check_len(
        content.strip(), TASK_BRIEF_MAX_CHARS, "brief_too_long", "The brief",
    )


def validate_run_entry(summary, result, notes) -> tuple[str, str, str]:
    """Return ``(summary, result, notes)`` stripped (None -> "").

    Checked in that order, so the first field over its cap is the one
    reported.

    Raises:
        TaskValidationError ``invalid_summary`` / ``invalid_result`` /
            ``invalid_notes`` (not a string), or ``summary_too_long`` /
            ``result_too_long`` / ``notes_too_long`` (over
            ``TASK_RUN_SUMMARY_MAX_CHARS`` / ``TASK_RUN_RESULT_MAX_CHARS`` /
            ``TASK_RUN_NOTES_MAX_CHARS``).
    """
    cleaned = []
    for name, value, cap in (
        ("summary", summary, TASK_RUN_SUMMARY_MAX_CHARS),
        ("result", result, TASK_RUN_RESULT_MAX_CHARS),
        ("notes", notes, TASK_RUN_NOTES_MAX_CHARS),
    ):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise TaskValidationError(
                f"invalid_{name}", f"The run's {name} must be a string."
            )
        cleaned.append(
            _check_len(value.strip(), cap, f"{name}_too_long", f"The run's {name}")
        )
    return cleaned[0], cleaned[1], cleaned[2]


def _validate_prompt(prompt) -> str:
    """Return the prompt verbatim; refuse a non-string or blank one."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise TaskValidationError("invalid_prompt", "The prompt cannot be empty.")
    return prompt


def _validate_model(model) -> Optional[str]:
    """Shape check of a model id (None clears). Whether the model is
    selectable is the caller's check (``is_model_allowed``)."""
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip() or len(model) > _MODEL_MAX_CHARS:
        raise TaskValidationError("invalid_model", "Invalid model id.")
    return model.strip()


def _validate_thinking_effort(effort) -> Optional[str]:
    """Shape check of a thinking level (None clears). Whether the level
    fits the model is the caller's check."""
    if effort is None:
        return None
    if (
        not isinstance(effort, str) or not effort.strip()
        or len(effort) > _THINKING_EFFORT_MAX_CHARS
    ):
        raise TaskValidationError("invalid_thinking_effort", "Invalid thinking level.")
    return effort.strip()


def _validate_source(source) -> str:
    if not isinstance(source, str) or not _BRIEF_SOURCE_RE.match(source):
        raise ValueError(
            f"Invalid brief source {source!r}: expected 'run:<run_id>', 'ui' "
            "or 'restore:<version>'."
        )
    return source


def _validate_expected_version(expected_version) -> int:
    if (
        isinstance(expected_version, bool) or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ValueError("expected_version must be a non-negative integer.")
    return expected_version


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _task_to_dict(task: Task) -> dict:
    """Convert a Task ORM instance to the task dict.

    Shape::

        {"id", "user_id", "title", "title_is_default", "instructions",
         "brief", "brief_version", "brief_updated_at", "model",
         "thinking_effort", "archived", "next_run_seq", "event_seq",
         "unread_seq", "read_seq", "created_at", "updated_at",
         "last_activity_at",                    # ISO strings
         "unread"}                              # unread_seq > read_seq
    """
    return {
        "id": task.id,
        "user_id": task.user_id,
        "title": task.title,
        "title_is_default": bool(task.title_is_default),
        "instructions": task.instructions,
        "brief": task.brief,
        "brief_version": task.brief_version,
        "brief_updated_at": _iso(task.brief_updated_at),
        "model": task.model,
        "thinking_effort": task.thinking_effort,
        "archived": bool(task.archived),
        "next_run_seq": task.next_run_seq,
        "event_seq": task.event_seq,
        "unread_seq": task.unread_seq,
        "read_seq": task.read_seq,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "last_activity_at": _iso(task.last_activity_at),
        "unread": (task.unread_seq or 0) > (task.read_seq or 0),
    }


def _run_to_dict(run: TaskRun, *, include_result: bool = True) -> dict:
    """Convert a TaskRun ORM instance to the run dict.

    Shape::

        {"id", "task_id", "seq", "conversation_id", "prompt", "status",
         "summary", "result", "notes",          # None until completed
         "handoff_missing", "error", "model",
         "context_brief_version",               # None until frozen
         "started_at", "finished_at"}           # ISO strings

    The frozen ``context`` text itself is never included (the column is
    deferred; :func:`get_run_context` reads it). With
    ``include_result=False`` the ``result`` key is left out and the
    attribute is never read (for rows loaded with ``result`` deferred).
    """
    row = {
        "id": run.id,
        "task_id": run.task_id,
        "seq": run.seq,
        "conversation_id": run.conversation_id,
        "prompt": run.prompt,
        "status": run.status,
        "summary": run.summary,
    }
    if include_result:
        row["result"] = run.result
    row.update({
        "notes": run.notes,
        "handoff_missing": bool(run.handoff_missing),
        "error": run.error,
        "model": run.model,
        "context_brief_version": run.context_brief_version,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    })
    return row


def _active_run_summary(run: TaskRun) -> dict:
    return {"id": run.id, "seq": run.seq, "status": run.status}


def _brief_state(task: Task) -> dict:
    """The brief part of a task: ``{brief, brief_version, brief_updated_at}``."""
    return {
        "brief": task.brief,
        "brief_version": task.brief_version,
        "brief_updated_at": _iso(task.brief_updated_at),
    }


def _excerpt(content: str, chars: int) -> str:
    clean = _collapse(content)
    if len(clean) <= chars:
        return clean
    return clean[: max(chars - 1, 0)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Private query helpers (run inside the caller's session)
# ---------------------------------------------------------------------------


async def _reload_task(db, task_id: str) -> Optional[Task]:
    """The row as stored (naive-UTC datetimes), bypassing the identity map."""
    return await db.get(Task, task_id, populate_existing=True)


async def _owned_task(db, user_id: int, task_id: str) -> Optional[Task]:
    task = await db.get(Task, task_id, populate_existing=True)
    if task is None or task.user_id != user_id:
        return None
    return task


async def _first_active_run(db, task_id: str) -> Optional[TaskRun]:
    return (await db.execute(
        select(TaskRun)
        .where(
            TaskRun.task_id == task_id,
            TaskRun.status.in_(TASK_RUN_ACTIVE_STATUSES),
        )
        .order_by(TaskRun.seq.asc())
        .limit(1)
    )).scalar_one_or_none()


async def _count_unarchived(db, user_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(Task)
        .where(Task.user_id == user_id, Task.archived == False)  # noqa: E712
    )).scalar_one()


async def _touch_task(db, task_id: str, *, activity: bool, now: datetime) -> None:
    """Bump ``event_seq`` (and ``last_activity_at`` for a run write)."""
    values: dict = {Task.event_seq: Task.event_seq + 1}
    if activity:
        values[Task.last_activity_at] = now
    await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(values)
        .execution_options(synchronize_session=False)
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


async def create_task(
    user_id: int,
    *,
    title: str,
    title_is_default: bool = False,
    instructions: str = "",
    model: Optional[str] = None,
    thinking_effort: Optional[str] = None,
    task_id: Optional[str] = None,
) -> dict:
    """Create a task.

    Args:
        user_id: The owner.
        title: Normalized through :func:`normalize_title`.
        title_is_default: True when ``title`` was cut from the first prompt
            (:func:`title_from_prompt`), so the first run may still name the
            task.
        instructions: Through :func:`validate_instructions`.
        model, thinking_effort: Defaults for new runs (None = the user's
            default); shape-checked only.
        task_id: Optional pre-generated id (lets a service lay out the task
            directory first); a uuid4 when omitted.

    Returns:
        The task dict.

    Raises:
        TaskValidationError, TaskCapReachedError (the user has
        ``MAX_ACTIVE_TASKS_PER_USER`` unarchived tasks; counted after the
        insert in the same transaction, so concurrent creates cannot both
        slip under the cap).
    """
    clean_title = normalize_title(title)
    clean_instructions = validate_instructions(instructions)
    clean_model = _validate_model(model)
    clean_effort = _validate_thinking_effort(thinking_effort)
    now = _now()
    new_id = task_id or str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        db.add(Task(
            id=new_id,
            user_id=user_id,
            title=clean_title,
            title_is_default=bool(title_is_default),
            instructions=clean_instructions,
            model=clean_model,
            thinking_effort=clean_effort,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        ))
        await db.flush()
        # The insert holds the write lock, so this count includes every
        # committed create and ours, and no other create can commit before
        # we do.
        if await _count_unarchived(db, user_id) > MAX_ACTIVE_TASKS_PER_USER:
            await db.rollback()
            raise TaskCapReachedError(MAX_ACTIVE_TASKS_PER_USER)
        await db.commit()
        return _task_to_dict(await _reload_task(db, new_id))


async def get_task(user_id: int, task_id: str) -> Optional[dict]:
    """The task dict, or None when missing or owned by someone else."""
    async with AsyncSessionLocal() as db:
        task = await _owned_task(db, user_id, task_id)
        return _task_to_dict(task) if task is not None else None


async def get_task_by_id(task_id: str) -> Optional[dict]:
    """The task dict whoever owns it, or None (server-side callers only:
    ``ToolContext``, the run driver)."""
    async with AsyncSessionLocal() as db:
        task = await db.get(Task, task_id)
        return _task_to_dict(task) if task is not None else None


async def list_tasks(user_id: int, *, archived: bool = False) -> list[dict]:
    """The user's unarchived (or, with ``archived=True``, archived) tasks,
    newest ``last_activity_at`` first.

    Each row is the task dict plus ``active_run``: ``{id, seq, status}`` or
    None. The active runs come from one query over the listed tasks.
    """
    async with AsyncSessionLocal() as db:
        listed = (
            select(Task.id)
            .where(Task.user_id == user_id, Task.archived == bool(archived))
        )
        tasks = (await db.execute(
            select(Task)
            .where(Task.user_id == user_id, Task.archived == bool(archived))
            .order_by(Task.last_activity_at.desc(), Task.created_at.desc(), Task.id)
        )).scalars().all()
        active_runs = (await db.execute(
            select(TaskRun)
            .where(
                TaskRun.task_id.in_(listed),
                TaskRun.status.in_(TASK_RUN_ACTIVE_STATUSES),
            )
            .order_by(TaskRun.seq.desc())
        )).scalars().all()
        # Lowest seq wins if a task ever has more than one (not in v1).
        active_by_task = {run.task_id: _active_run_summary(run) for run in active_runs}
        rows = []
        for task in tasks:
            row = _task_to_dict(task)
            row["active_run"] = active_by_task.get(task.id)
            rows.append(row)
        return rows


async def count_active_tasks(user_id: int) -> int:
    """How many unarchived tasks the user has (what the cap counts)."""
    async with AsyncSessionLocal() as db:
        return await _count_unarchived(db, user_id)


async def update_task(
    user_id: int,
    task_id: str,
    *,
    title: Any = UNSET,
    instructions: Any = UNSET,
    model: Any = UNSET,
    thinking_effort: Any = UNSET,
) -> dict:
    """Edit the user-owned fields of a task. Fields left at :data:`UNSET`
    stay; ``model`` and ``thinking_effort`` accept None to clear.

    Setting ``title`` (even to the same text) clears ``title_is_default``:
    the user has named the task, so no run may rename it. ``event_seq`` and
    ``updated_at`` move only when something changed.

    Returns:
        The task dict.

    Raises:
        TaskValidationError, TaskNotFoundError.
    """
    values: dict = {}
    if title is not UNSET:
        values["title"] = normalize_title(title)
    if instructions is not UNSET:
        values["instructions"] = validate_instructions(instructions)
    if model is not UNSET:
        values["model"] = _validate_model(model)
    if thinking_effort is not UNSET:
        values["thinking_effort"] = _validate_thinking_effort(thinking_effort)

    async with AsyncSessionLocal() as db:
        task = await _owned_task(db, user_id, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        changed = False
        for field, value in values.items():
            if getattr(task, field) != value:
                setattr(task, field, value)
                changed = True
        if "title" in values and task.title_is_default:
            task.title_is_default = False
            changed = True
        if not changed:
            return _task_to_dict(task)
        task.event_seq = Task.event_seq + 1
        task.updated_at = _now()
        await db.commit()
        return _task_to_dict(await _reload_task(db, task_id))


async def set_title_from_run(task_id: str, title: str) -> bool:
    """Apply a run's ``task_title``, only while the title is still the one
    cut from the first prompt.

    The ``title_is_default`` check is in the UPDATE's WHERE and the UPDATE
    clears the flag, so at most one model-side rename ever lands and none
    after the user renamed the task (invariant 4).

    Returns:
        Whether the title was applied.

    Raises:
        TaskValidationError ``invalid_title``.
    """
    clean = normalize_title(title)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.title_is_default == True)  # noqa: E712
            .values({
                Task.title: clean,
                Task.title_is_default: False,
                Task.event_seq: Task.event_seq + 1,
                Task.updated_at: _now(),
            })
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


async def set_task_archived(user_id: int, task_id: str, archived: bool) -> dict:
    """Archive (True) or unarchive (False) a task.

    A no-op when the task is already in that state. Only the flag,
    ``event_seq`` and ``updated_at`` change; ``last_activity_at`` is kept so
    an unarchived task returns to its place in the list.

    Returns:
        The task dict.

    Raises:
        TaskNotFoundError; TaskRunActiveError when archiving a task with an
        active run; TaskCapReachedError when unarchiving would exceed
        ``MAX_ACTIVE_TASKS_PER_USER``.
    """
    archived = bool(archived)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.user_id == user_id,
                Task.archived != archived,
            )
            .values({
                Task.archived: archived,
                Task.event_seq: Task.event_seq + 1,
                Task.updated_at: _now(),
            })
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            task = await _owned_task(db, user_id, task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            return _task_to_dict(task)
        if archived:
            active = await _first_active_run(db, task_id)
            if active is not None:
                # Read before the rollback expires the instance.
                summary = _active_run_summary(active)
                await db.rollback()
                raise TaskRunActiveError(
                    summary,
                    f"Run {summary['seq']} of this task is still "
                    f"{summary['status']}. Stop it before archiving the task.",
                )
        elif await _count_unarchived(db, user_id) > MAX_ACTIVE_TASKS_PER_USER:
            await db.rollback()
            raise TaskCapReachedError(MAX_ACTIVE_TASKS_PER_USER)
        await db.commit()
        return _task_to_dict(await _reload_task(db, task_id))


async def delete_task(user_id: int, task_id: str) -> list[str]:
    """Delete a task row; its runs, brief versions and run conversations
    cascade through the foreign keys.

    Returns the ids of every conversation that carried this ``task_id``
    (abandoned run conversations with no run row included), collected
    inside the deleting transaction, so the caller can sweep their
    ``CHATS_DIR`` directories. Neither the filesystem nor a live run is
    touched here: the caller stops an active run first and removes
    ``TASKS_DIR/<id>`` after.

    Raises:
        TaskNotFoundError.
    """
    async with AsyncSessionLocal() as db:
        # Take the write lock first so no run conversation can be added
        # between the collection and the delete.
        result = await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.user_id == user_id)
            .values({Task.event_seq: Task.event_seq + 1})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise TaskNotFoundError(task_id)
        conversation_ids = set((await db.execute(
            select(Conversation.id).where(Conversation.task_id == task_id)
        )).scalars().all())
        conversation_ids.update((await db.execute(
            select(TaskRun.conversation_id).where(TaskRun.task_id == task_id)
        )).scalars().all())
        # Delete the conversations explicitly too: a run whose conversation
        # somehow lacks the task_id still loses its transcript row.
        if conversation_ids:
            await db.execute(
                delete(Conversation)
                .where(Conversation.id.in_(conversation_ids))
                .execution_options(synchronize_session=False)
            )
        await db.execute(
            delete(Task)
            .where(Task.id == task_id)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return sorted(conversation_ids)


async def list_task_ids_for_user(user_id: int) -> list[str]:
    """Every task id of the user, archived included (the account-delete
    sweep of ``TASKS_DIR``)."""
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(Task.id).where(Task.user_id == user_id).order_by(Task.created_at)
        )).scalars().all())


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def allocate_run(
    task_id: str,
    *,
    conversation_id: str,
    prompt: str,
    model: Optional[str] = None,
) -> dict:
    """Start a run: allocate its ``seq`` and insert it as ``running``.

    One write transaction that bumps ``next_run_seq``, ``event_seq`` and
    ``last_activity_at`` first (taking the write lock), then refuses an
    archived task or one with an active run (invariant 6; this is the one
    place it is checked), then inserts the run with the pre-bump
    ``next_run_seq`` as its ``seq``. A refusal rolls back, so its seq is not
    consumed; a seq once used is never reused, run deletes included.

    Args:
        task_id: The task.
        conversation_id: The run's conversation, created for this task
            (``conversations.task_id == task_id``).
        prompt: The user's message, stored verbatim.
        model: The model the run uses.

    Returns:
        The run dict.

    Raises:
        TaskValidationError ``invalid_prompt``, TaskNotFoundError,
        TaskArchivedError, TaskRunActiveError, TaskRunExistsError (the
        conversation already has its run).
        ValueError: the conversation is not a run conversation of this task.
    """
    clean_prompt = _validate_prompt(prompt)
    now = _now()
    async with AsyncSessionLocal() as db:
        bumped = (await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values({
                Task.next_run_seq: Task.next_run_seq + 1,
                Task.event_seq: Task.event_seq + 1,
                Task.last_activity_at: now,
            })
            .returning(Task.next_run_seq, Task.archived)
            .execution_options(synchronize_session=False)
        )).first()
        if bumped is None:
            await db.rollback()
            raise TaskNotFoundError(task_id)
        next_run_seq, archived = bumped
        if archived:
            await db.rollback()
            raise TaskArchivedError(task_id)
        active = await _first_active_run(db, task_id)
        if active is not None:
            summary = _active_run_summary(active)
            await db.rollback()
            raise TaskRunActiveError(summary)
        conv_task_id = (await db.execute(
            select(Conversation.task_id).where(Conversation.id == conversation_id)
        )).first()
        if conv_task_id is None or conv_task_id[0] != task_id:
            await db.rollback()
            raise ValueError(
                f"Conversation {conversation_id} is not a run conversation of "
                f"task {task_id}."
            )
        run = TaskRun(
            id=str(uuid.uuid4()),
            task_id=task_id,
            seq=next_run_seq - 1,
            conversation_id=conversation_id,
            prompt=clean_prompt,
            status=TASK_RUN_STATUS_RUNNING,
            model=model,
            started_at=now,
        )
        db.add(run)
        try:
            await db.flush()
        except IntegrityError:
            # The unique conversation_id index: this conversation already
            # has its run.
            await db.rollback()
            raise TaskRunExistsError(conversation_id) from None
        await db.commit()
        return _run_to_dict(await db.get(TaskRun, run.id, populate_existing=True))


async def get_run(run_id: str) -> Optional[dict]:
    """The run dict, or None."""
    async with AsyncSessionLocal() as db:
        run = await db.get(TaskRun, run_id)
        return _run_to_dict(run) if run is not None else None


async def get_run_by_conversation(conversation_id: str) -> Optional[dict]:
    """The run whose conversation this is, or None (no run row yet, or not
    a run conversation)."""
    async with AsyncSessionLocal() as db:
        run = (await db.execute(
            select(TaskRun).where(TaskRun.conversation_id == conversation_id)
        )).scalar_one_or_none()
        return _run_to_dict(run) if run is not None else None


async def get_run_by_seq(task_id: str, seq: int) -> Optional[dict]:
    """Run number ``seq`` of the task, or None (never allocated, or deleted)."""
    async with AsyncSessionLocal() as db:
        run = (await db.execute(
            select(TaskRun).where(TaskRun.task_id == task_id, TaskRun.seq == seq)
        )).scalar_one_or_none()
        return _run_to_dict(run) if run is not None else None


async def get_active_run(task_id: str) -> Optional[dict]:
    """The task's active run (``running`` or ``waiting``), or None."""
    async with AsyncSessionLocal() as db:
        run = await _first_active_run(db, task_id)
        return _run_to_dict(run) if run is not None else None


async def set_run_status(
    run_id: str,
    status: str,
    *,
    error: Optional[str] = None,
    expected_from: Optional[Iterable[str]] = None,
) -> Optional[dict]:
    """Move a run to ``running``, ``waiting``, ``failed`` (with ``error``)
    or ``stopped``.

    ``expected_from`` is the set of statuses the run must be in, checked in
    the UPDATE's WHERE (default: the active statuses, so a finished run is
    never reopened). Finished statuses set ``finished_at``. The task's
    ``event_seq`` and ``last_activity_at`` move in the same transaction.

    Returns:
        The run dict, or None when the run is missing or was not in an
        expected status (nothing changed).

    Raises:
        ValueError: ``status`` is ``completed`` (use :func:`complete_run`)
            or unknown.
    """
    if status not in _SETTABLE_RUN_STATUSES:
        raise ValueError(
            f"set_run_status cannot set {status!r}; completed runs go through "
            "complete_run()."
        )
    allowed = (
        TASK_RUN_ACTIVE_STATUSES if expected_from is None else frozenset(expected_from)
    )
    now = _now()
    values: dict = {TaskRun.status: status}
    if status in TASK_RUN_FINISHED_STATUSES:
        values[TaskRun.finished_at] = now
    if status == TASK_RUN_STATUS_FAILED:
        values[TaskRun.error] = error
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id, TaskRun.status.in_(allowed))
            .values(values)
            .returning(TaskRun.task_id)
            .execution_options(synchronize_session=False)
        )).first()
        if row is None:
            await db.rollback()
            return None
        await _touch_task(db, row[0], activity=True, now=now)
        await db.commit()
        return _run_to_dict(await db.get(TaskRun, run_id, populate_existing=True))


async def complete_run(
    run_id: str,
    *,
    summary: str,
    result: str,
    notes: str,
    handoff_missing: bool = False,
) -> dict:
    """Write an active run's log entry and mark it ``completed``.

    The entry goes through :func:`validate_run_entry` first, so a refused
    entry writes nothing. ``handoff_missing`` marks an entry the run driver
    filled in because the run never called ``complete_task_run``.

    Returns:
        The run dict.

    Raises:
        TaskValidationError, TaskRunNotFoundError, TaskRunFinishedError.
    """
    clean_summary, clean_result, clean_notes = validate_run_entry(summary, result, notes)
    now = _now()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            update(TaskRun)
            .where(
                TaskRun.id == run_id,
                TaskRun.status.in_(TASK_RUN_ACTIVE_STATUSES),
            )
            .values({
                TaskRun.summary: clean_summary,
                TaskRun.result: clean_result,
                TaskRun.notes: clean_notes,
                TaskRun.handoff_missing: bool(handoff_missing),
                TaskRun.status: TASK_RUN_STATUS_COMPLETED,
                TaskRun.error: None,
                TaskRun.finished_at: now,
            })
            .returning(TaskRun.task_id)
            .execution_options(synchronize_session=False)
        )).first()
        if row is None:
            await db.rollback()
            run = await db.get(TaskRun, run_id, populate_existing=True)
            if run is None:
                raise TaskRunNotFoundError(run_id)
            raise TaskRunFinishedError(run_id, run.status)
        await _touch_task(db, row[0], activity=True, now=now)
        await db.commit()
        return _run_to_dict(await db.get(TaskRun, run_id, populate_existing=True))


async def save_run_entry(
    run_id: str, *, summary: str, result: str, notes: str,
) -> dict:
    """Save an active run's log entry WITHOUT finishing the run.

    The pending-entry state of a ``brief_conflict`` (chat/tasks/runs.py):
    ``complete_task_run`` could not apply its brief because the user edited
    it during the run, so the entry is kept while the model merges and
    retries. An active run whose ``summary`` is not None has a pending
    entry; :func:`complete_run` later overwrites or finalizes it.

    The entry goes through :func:`validate_run_entry` first, so a refused
    entry writes nothing. ``status``, ``finished_at`` and the task's
    ``event_seq`` are left alone: a pending entry is not a thread change.

    Returns:
        The run dict.

    Raises:
        TaskValidationError, TaskRunNotFoundError, TaskRunFinishedError.
    """
    clean_summary, clean_result, clean_notes = validate_run_entry(summary, result, notes)
    async with AsyncSessionLocal() as db:
        result_proxy = await db.execute(
            update(TaskRun)
            .where(
                TaskRun.id == run_id,
                TaskRun.status.in_(TASK_RUN_ACTIVE_STATUSES),
            )
            .values({
                TaskRun.summary: clean_summary,
                TaskRun.result: clean_result,
                TaskRun.notes: clean_notes,
            })
            .execution_options(synchronize_session=False)
        )
        if result_proxy.rowcount != 1:
            await db.rollback()
            run = await db.get(TaskRun, run_id, populate_existing=True)
            if run is None:
                raise TaskRunNotFoundError(run_id)
            raise TaskRunFinishedError(run_id, run.status)
        await db.commit()
        return _run_to_dict(await db.get(TaskRun, run_id, populate_existing=True))


async def get_run_context(run_id: str) -> Optional[tuple[str, int]]:
    """The run's frozen starting context as ``(context,
    context_brief_version)``, or None when it is not frozen yet or the run
    is missing."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(TaskRun.context, TaskRun.context_brief_version)
            .where(TaskRun.id == run_id)
        )).first()
    if row is None or row[0] is None or row[1] is None:
        return None
    return row[0], row[1]


async def freeze_run_context(
    run_id: str, *, context: str, brief_version: int,
) -> tuple[str, int]:
    """Freeze the run's starting context: the ``## Task`` block its system
    prompt is built with and the brief version that block shows.

    One compare-and-set UPDATE that writes both columns only while
    ``context`` is still NULL, so the first writer wins and the frozen pair
    never changes afterwards; a later (or concurrent, losing) caller gets
    the pair that is stored. The run's status is not checked: freezing is
    part of building a turn, whatever the run's state.

    Returns:
        The ``(context, context_brief_version)`` stored on the row.

    Raises:
        TaskRunNotFoundError.
        ValueError: ``context`` is not a string or is over
            ``TASK_CONTEXT_MAX_CHARS``, or ``brief_version`` is not a
            non-negative integer.
    """
    if not isinstance(context, str) or len(context) > TASK_CONTEXT_MAX_CHARS:
        raise ValueError(
            "context must be a string of at most "
            f"{TASK_CONTEXT_MAX_CHARS} characters."
        )
    if (
        isinstance(brief_version, bool) or not isinstance(brief_version, int)
        or brief_version < 0
    ):
        raise ValueError("brief_version must be a non-negative integer.")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id, TaskRun.context.is_(None))
            .values({
                TaskRun.context: context,
                TaskRun.context_brief_version: brief_version,
            })
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await db.commit()
            return context, brief_version
        await db.rollback()
        row = (await db.execute(
            select(TaskRun.context, TaskRun.context_brief_version)
            .where(TaskRun.id == run_id)
        )).first()
        if row is None:
            raise TaskRunNotFoundError(run_id)
        return row[0], row[1]


async def delete_run(user_id: int, task_id: str, run_id: str) -> str:
    """Delete a finished run's log entry together with its conversation.

    Deletes the conversation row (the run row cascades from it) and returns
    its id for the caller's ``CHATS_DIR`` sweep. ``next_run_seq`` is left
    alone, so the seq is never reused. Bumps the task's ``event_seq``.

    Raises:
        TaskNotFoundError, TaskRunNotFoundError (not a run of that task),
        TaskRunActiveError (the run is still running or waiting).
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.user_id == user_id)
            .values({Task.event_seq: Task.event_seq + 1})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise TaskNotFoundError(task_id)
        run = (await db.execute(
            select(TaskRun).where(TaskRun.id == run_id, TaskRun.task_id == task_id)
        )).scalar_one_or_none()
        if run is None:
            await db.rollback()
            raise TaskRunNotFoundError(run_id)
        if run.status in TASK_RUN_ACTIVE_STATUSES:
            summary = _active_run_summary(run)
            await db.rollback()
            raise TaskRunActiveError(
                summary,
                f"Run {summary['seq']} is still {summary['status']}. Stop it "
                "before deleting it.",
            )
        conversation_id = run.conversation_id
        await db.execute(
            delete(Conversation)
            .where(Conversation.id == conversation_id)
            .execution_options(synchronize_session=False)
        )
        # The FK cascade already removed it; this covers a database opened
        # without PRAGMA foreign_keys.
        await db.execute(
            delete(TaskRun)
            .where(TaskRun.id == run_id)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return conversation_id


async def list_runs_page(
    task_id: str, *, before_seq: Optional[int] = None, limit: int = 20,
) -> dict:
    """One page of the thread, newest first, keyset-paged on ``seq``.

    Returns:
        ``{"runs": [run dict, ...], "next_before_seq": int | None}``: at
        most ``limit`` (clamped to 1..100) runs with ``seq < before_seq``
        (all when None), and the ``before_seq`` of the next older page, or
        None when there is nothing older.
    """
    limit = max(1, min(int(limit), _RUNS_PAGE_MAX_LIMIT))
    stmt = select(TaskRun).where(TaskRun.task_id == task_id)
    if before_seq is not None:
        stmt = stmt.where(TaskRun.seq < before_seq)
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            stmt.order_by(TaskRun.seq.desc()).limit(limit + 1)
        )).scalars().all()
    has_more = len(runs) > limit
    runs = runs[:limit]
    return {
        "runs": [_run_to_dict(r) for r in runs],
        "next_before_seq": runs[-1].seq if has_more else None,
    }


async def list_runs(task_id: str, *, offset: int = 0, limit: int = 50) -> list[dict]:
    """The task's runs newest first, offset-paged (``limit`` clamped to
    1..500), results included. For the ``list_task_runs`` tool; the
    context builder uses :func:`list_context_runs`."""
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), _LIST_RUNS_MAX_LIMIT))
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(TaskRun)
            .where(TaskRun.task_id == task_id)
            .order_by(TaskRun.seq.desc())
            .offset(offset)
            .limit(limit)
        )).scalars().all()
        return [_run_to_dict(r) for r in runs]


async def list_context_runs(task_id: str, *, before_seq: int, limit: int) -> list[dict]:
    """The runs the task context of run ``before_seq`` shows: ``seq <
    before_seq``, newest first, at most ``limit`` (clamped to 1..500).

    The ``result`` column (up to ``TASK_RUN_RESULT_MAX_CHARS`` each) is
    never loaded: each row is the run dict without its ``result`` key. The
    context builder (chat/tasks/context.py) reads no result, so loading the
    window and the index costs no more than their prompts, summaries and
    notes.
    """
    limit = max(1, min(int(limit), _LIST_RUNS_MAX_LIMIT))
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(TaskRun)
            .options(defer(TaskRun.result, raiseload=True))
            .where(TaskRun.task_id == task_id, TaskRun.seq < int(before_seq))
            .order_by(TaskRun.seq.desc())
            .limit(limit)
        )).scalars().all()
        return [_run_to_dict(r, include_result=False) for r in runs]


async def list_run_index(
    task_id: str, *, offset: int = 0, limit: int = 50, prompt_chars: int = 200,
) -> list[dict]:
    """One page of the task's run log as index rows, newest first,
    offset-paged (``limit`` clamped to 1..500). For the ``list_task_runs``
    tool (chat/gemini_api/tool_handlers/task_runs.py).

    Reads only the short columns: ``result``, ``notes``, ``error`` and the
    frozen ``context`` are never loaded, and of the prompt only its first
    ``prompt_chars`` characters, so a page costs the same however long the
    runs' texts are.

    Returns:
        ``[{"id", "seq", "status", "summary", "handoff_missing",
        "started_at", "finished_at", "prompt_head"}, ...]`` with ISO
        datetimes; ``summary`` is None until the run has an entry (an
        active run's may be a pending, unpublished one: the caller decides
        whether to show it).
    """
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), _LIST_RUNS_MAX_LIMIT))
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                TaskRun.id, TaskRun.seq, TaskRun.status, TaskRun.summary,
                TaskRun.handoff_missing, TaskRun.started_at, TaskRun.finished_at,
                func.substr(TaskRun.prompt, 1, max(0, int(prompt_chars))),
            )
            .where(TaskRun.task_id == task_id)
            .order_by(TaskRun.seq.desc())
            .offset(offset)
            .limit(limit)
        )).all()
    return [
        {
            "id": row[0],
            "seq": row[1],
            "status": row[2],
            "summary": row[3],
            "handoff_missing": bool(row[4]),
            "started_at": _iso(row[5]),
            "finished_at": _iso(row[6]),
            "prompt_head": row[7] or "",
        }
        for row in rows
    ]


async def count_runs(task_id: str, *, before_seq: Optional[int] = None) -> int:
    """How many runs the task has (deleted runs not counted); with
    ``before_seq``, only those with ``seq < before_seq``."""
    stmt = select(func.count()).select_from(TaskRun).where(TaskRun.task_id == task_id)
    if before_seq is not None:
        stmt = stmt.where(TaskRun.seq < int(before_seq))
    async with AsyncSessionLocal() as db:
        return (await db.execute(stmt)).scalar_one()


async def fail_interrupted_runs(error: str = "interrupted by a restart") -> list[dict]:
    """Startup sweep: mark every ``running`` run ``failed`` with ``error``.

    ``waiting`` runs are left alone: their wait handles survive a restart
    and resume them. Each affected task's ``event_seq`` and
    ``last_activity_at`` move in the same transaction.

    Returns:
        The affected runs (run dicts, after the change).
    """
    now = _now()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            update(TaskRun)
            .where(TaskRun.status == TASK_RUN_STATUS_RUNNING)
            .values({
                TaskRun.status: TASK_RUN_STATUS_FAILED,
                TaskRun.error: error,
                TaskRun.finished_at: now,
            })
            .returning(TaskRun.id, TaskRun.task_id)
            .execution_options(synchronize_session=False)
        )).all()
        if not rows:
            await db.rollback()
            return []
        for task_id in {task_id for _, task_id in rows}:
            await _touch_task(db, task_id, activity=True, now=now)
        await db.commit()
        runs = (await db.execute(
            select(TaskRun)
            .where(TaskRun.id.in_([run_id for run_id, _ in rows]))
            .order_by(TaskRun.task_id, TaskRun.seq)
            .execution_options(populate_existing=True)
        )).scalars().all()
        return [_run_to_dict(r) for r in runs]


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


async def _write_brief(
    db, task_id: str, content: str, *, expected_version: int, source: str,
) -> dict:
    """The one brief write, inside the caller's session: conditional UPDATE
    on the version token, the new version's row, then pruning to
    ``TASK_BRIEF_MAX_VERSIONS``. Commits on success, rolls back on refusal.
    """
    now = _now()
    result = await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.brief_version == expected_version)
        .values({
            Task.brief: content,
            Task.brief_version: Task.brief_version + 1,
            Task.brief_updated_at: now,
            Task.event_seq: Task.event_seq + 1,
            Task.updated_at: now,
        })
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        task = await _reload_task(db, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        raise StaleBriefError(_brief_state(task))
    new_version = expected_version + 1
    db.add(TaskBriefVersion(
        task_id=task_id,
        version=new_version,
        content=content,
        source=source,
        created_at=now,
    ))
    await db.flush()
    kept = (
        select(TaskBriefVersion.id)
        .where(TaskBriefVersion.task_id == task_id)
        .order_by(TaskBriefVersion.version.desc())
        .limit(TASK_BRIEF_MAX_VERSIONS)
    )
    await db.execute(
        delete(TaskBriefVersion)
        .where(
            TaskBriefVersion.task_id == task_id,
            TaskBriefVersion.id.not_in(kept),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return _brief_state(await _reload_task(db, task_id))


async def set_brief(
    task_id: str, content: str, *, expected_version: int, source: str,
) -> dict:
    """Write a new brief.

    In one transaction: the conditional UPDATE (``brief_version`` must still
    be ``expected_version``; it becomes ``expected_version + 1``), a
    ``task_brief_versions`` row holding the new text under the new version
    with ``source`` as its author, and pruning of that task's versions
    beyond ``TASK_BRIEF_MAX_VERSIONS`` (oldest first). Not owner-scoped:
    callers do the access check.

    Args:
        content: Through :func:`validate_brief` (stripped; empty allowed).
        expected_version: The ``brief_version`` the writer started from.
        source: ``run:<run_id>``, ``ui`` or ``restore:<version>``.

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        TaskValidationError, StaleBriefError (``.current`` is the brief as
        it now is), TaskNotFoundError.
        ValueError: malformed ``source`` or ``expected_version``.
    """
    clean = validate_brief(content)
    _validate_source(source)
    _validate_expected_version(expected_version)
    async with AsyncSessionLocal() as db:
        return await _write_brief(
            db, task_id, clean, expected_version=expected_version, source=source,
        )


async def list_brief_versions(task_id: str, *, excerpt_chars: int = 200) -> list[dict]:
    """The stored brief versions, newest first, as ``{version, source,
    created_at, excerpt}`` (the text with whitespace collapsed, cut to
    ``excerpt_chars`` with a trailing "…" when cut)."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                TaskBriefVersion.version,
                TaskBriefVersion.source,
                TaskBriefVersion.created_at,
                TaskBriefVersion.content,
            )
            .where(TaskBriefVersion.task_id == task_id)
            .order_by(TaskBriefVersion.version.desc())
        )).all()
    return [
        {
            "version": version,
            "source": source,
            "created_at": _iso(created_at),
            "excerpt": _excerpt(content, excerpt_chars),
        }
        for version, source, created_at, content in rows
    ]


async def get_brief_version(task_id: str, version: int) -> Optional[dict]:
    """One stored version as ``{version, source, created_at, content}``, or
    None (never written, or pruned)."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(TaskBriefVersion).where(
                TaskBriefVersion.task_id == task_id,
                TaskBriefVersion.version == version,
            )
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "version": row.version,
            "source": row.source,
            "created_at": _iso(row.created_at),
            "content": row.content,
        }


async def restore_brief_version(
    task_id: str, version: int, *, expected_version: int,
) -> dict:
    """Write stored version ``version``'s text as a new brief version with
    ``source = "restore:<version>"``, through the same transaction as
    :func:`set_brief` (so a restore during a run takes the same conflict
    path).

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        TaskNotFoundError, BriefVersionNotFoundError, StaleBriefError,
        TaskValidationError (the stored text is over today's cap).
    """
    _validate_expected_version(expected_version)
    async with AsyncSessionLocal() as db:
        content = (await db.execute(
            select(TaskBriefVersion.content).where(
                TaskBriefVersion.task_id == task_id,
                TaskBriefVersion.version == version,
            )
        )).scalar_one_or_none()
        if content is None:
            if await db.get(Task, task_id) is None:
                raise TaskNotFoundError(task_id)
            raise BriefVersionNotFoundError(version)
        return await _write_brief(
            db, task_id, validate_brief(content),
            expected_version=expected_version, source=f"restore:{int(version)}",
        )


# ---------------------------------------------------------------------------
# Event and unread seqs
# ---------------------------------------------------------------------------


async def bump_event_seq(task_id: str) -> Optional[int]:
    """Bump ``event_seq`` for a thread change made outside this module (a
    question posted or answered). Returns the new seq, or None when the
    task is gone."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values({Task.event_seq: Task.event_seq + 1})
            .returning(Task.event_seq)
            .execution_options(synchronize_session=False)
        )).first()
        await db.commit()
        return row[0] if row is not None else None


async def mark_task_unread(task_id: str) -> bool:
    """Raise ``unread_seq`` to ``event_seq`` (the run left something for the
    user to look at). Returns whether it moved; it never lowers."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.event_seq > Task.unread_seq)
            .values({Task.unread_seq: Task.event_seq})
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


async def mark_task_read(user_id: int, task_id: str, *, up_to_seq: int) -> bool:
    """Acknowledge the thread up to ``up_to_seq``: ``read_seq`` becomes
    ``max(read_seq, min(up_to_seq, event_seq))``, so a late or repeated
    acknowledgement never hides newer output.

    Returns:
        Whether ``read_seq`` moved.

    Raises:
        TaskNotFoundError: missing, or owned by someone else.
    """
    target = func.min(literal(int(up_to_seq)), Task.event_seq)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.user_id == user_id, target > Task.read_seq)
            .values({Task.read_seq: target})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await db.commit()
            return True
        await db.rollback()
        if await _owned_task(db, user_id, task_id) is None:
            raise TaskNotFoundError(task_id)
        return False
