"""AutoProjects data access layer.

Owns every write to the ``autoprojects``, ``autoproject_runs`` and
``autoproject_brief_versions`` tables. Async functions that each open
a fresh ``AsyncSessionLocal()`` session (imported at module level so tests
can monkeypatch it) and return plain dicts with ISO datetimes; the shapes
are documented on :func:`_autoproject_to_dict`, :func:`_run_to_dict` and
:func:`_brief_state`.

Scoping: functions taking ``user_id`` are owner-scoped, and an AutoProject
owned by someone else raises (or reads as) exactly what a missing AutoProject
does. The rest take an AutoProject or run id only and are for callers that
already did the access check (the routes' guard,
``chat.autoprojects.http.require_autoproject_for_route`` over
``service.require_owned_autoproject``; the run's own AutoProject resolved from
``ToolContext`` in tools; the run driver).

Concurrency: SQLite takes its single write lock at a transaction's first
write, and reads before it see whatever was last committed. Every
check-then-write here (the AutoProject cap, the one active run per
AutoProject, the brief's version token, the conversation sweep of an
AutoProject delete) therefore issues its UPDATE or INSERT first and checks
inside the same transaction, rolling back on refusal, so two concurrent
writers cannot both pass a check.

Counters on the ``autoprojects`` row: ``event_seq`` is bumped by every write
that changes what the AutoProject page shows and by AutoProject edits (the
realtime ``autoproject_changed`` event carries it), ``last_activity_at`` moves
on create and on every run write (it orders the sidebar), and the unread pair
follows the conversation scheme (``unread_seq > read_seq``, both only move
forward).

Caps and statuses come from chat/autoprojects/constants.py. The filesystem
(the AutoProject workspace, the run conversations' directories) is never
touched here; delete functions return the ids the caller sweeps.
"""

from collections.abc import Iterable
from datetime import datetime, timezone
import re
from typing import Any, Optional
import uuid

from sqlalchemy import case, delete, func, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from chat.autoprojects.constants import (
    MAX_ACTIVE_AUTOPROJECTS_PER_USER,
    AUTOPROJECT_BRIEF_MAX_CHARS,
    AUTOPROJECT_BRIEF_MAX_VERSIONS,
    AUTOPROJECT_CONTEXT_MAX_CHARS,
    AUTOPROJECT_PROMPT_MAX_CHARS,
    AUTOPROJECT_RUN_ACTIVE_STATUSES,
    AUTOPROJECT_RUN_FINISHED_STATUSES,
    AUTOPROJECT_RUN_NOTES_MAX_CHARS,
    AUTOPROJECT_RUN_RESULT_MAX_CHARS,
    AUTOPROJECT_RUN_STATUS_COMPLETED,
    AUTOPROJECT_RUN_STATUS_FAILED,
    AUTOPROJECT_RUN_STATUS_RUNNING,
    AUTOPROJECT_RUN_STATUS_STOPPED,
    AUTOPROJECT_RUN_STATUS_WAITING,
    AUTOPROJECT_RUN_SUMMARY_MAX_CHARS,
    AUTOPROJECT_TITLE_MAX_CHARS,
    autoproject_not_found_message,
)
from db.engine import AsyncSessionLocal
from db.models import Conversation, AutoProject, AutoProjectBriefVersion, AutoProjectRun, User

# Fallback title when a first prompt has no text to cut one from.
DEFAULT_AUTOPROJECT_TITLE = "Untitled AutoProject"

# Length bounds of the free-form model fields (the column sizes).
_MODEL_MAX_CHARS = 100
_THINKING_EFFORT_MAX_CHARS = 20

# Page size bounds: the AutoProject page's run list (list_runs_page) and the
# plain listings (list_run_index, and list_context_runs, from which the
# context builder reads the window and the index, about
# AUTOPROJECT_LOG_WINDOW_MAX_ENTRIES + AUTOPROJECT_LOG_INDEX_MAX_ENTRIES rows,
# in one call).
_RUNS_PAGE_MAX_LIMIT = 100
_LIST_RUNS_MAX_LIMIT = 500

# Statuses set_run_status writes (``completed`` goes through complete_run).
_SETTABLE_RUN_STATUSES = frozenset({
    AUTOPROJECT_RUN_STATUS_RUNNING,
    AUTOPROJECT_RUN_STATUS_WAITING,
    AUTOPROJECT_RUN_STATUS_FAILED,
    AUTOPROJECT_RUN_STATUS_STOPPED,
})

# ``autoproject_brief_versions.source``: run:<run id> | ui | restore:<version>.
_BRIEF_SOURCE_RE = re.compile(r"^(?:ui|run:[0-9A-Za-z-]{1,36}|restore:\d{1,9})$")


class _Unset:
    """Type of :data:`UNSET`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


# "Leave this field alone" for update_autoproject(); None means "clear" where a
# field is nullable.
UNSET: Any = _Unset()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AutoProjectStoreError(Exception):
    """Base of every refusal this module raises.

    ``code`` is the stable machine code routes and tools put straight into
    their error bodies, ``message`` the human text, ``details`` any extra
    fields of the body (caps, lengths, the active run).
    """

    code = "autoproject_store_error"

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


class AutoProjectNotFoundError(AutoProjectStoreError):
    """The AutoProject does not exist, or belongs to someone else (same text)."""

    code = "autoproject_not_found"

    def __init__(self, autoproject_id: str):
        super().__init__(autoproject_not_found_message(autoproject_id))
        self.autoproject_id = autoproject_id


class AutoProjectCapReachedError(AutoProjectStoreError):
    """The user already has ``MAX_ACTIVE_AUTOPROJECTS_PER_USER`` unarchived AutoProjects."""

    code = "autoprojects_cap_reached"

    def __init__(self, cap: int):
        super().__init__(
            f"AutoProject limit reached: you can have at most {cap} AutoProjects that are "
            "not archived. Archive or delete one first.",
            details={"cap": cap},
        )


class AutoProjectValidationError(AutoProjectStoreError, ValueError):
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


class AutoProjectArchivedError(AutoProjectStoreError):
    """The AutoProject is archived, so it takes no new run."""

    code = "autoproject_archived"

    def __init__(self, autoproject_id: str):
        super().__init__(
            "This AutoProject is archived. Unarchive it to start a new run.",
            details={"autoproject_id": autoproject_id},
        )


class AutoProjectRunActiveError(AutoProjectStoreError):
    """The AutoProject has an active run (``running`` or ``waiting``) and the
    operation needs none (starting another run, archive, run delete).
    ``details["active_run"]`` is ``{id, seq, status}``."""

    code = "autoproject_run_active"

    def __init__(self, active_run: dict, message: Optional[str] = None):
        summary = active_run_summary(active_run)
        super().__init__(
            message or (
                f"Run {summary['seq']} of this AutoProject is still {summary['status']}. "
                "Wait for it to finish or stop it first."
            ),
            details={"active_run": summary},
        )
        self.active_run = summary


class AutoProjectRunNotFoundError(AutoProjectStoreError):
    """The run does not exist (in that AutoProject)."""

    code = "autoproject_run_not_found"

    def __init__(self, run_id: str):
        super().__init__(f"AutoProject run not found: {run_id}")
        self.run_id = run_id


class AutoProjectRunFinishedError(AutoProjectStoreError):
    """A write that needs an active run hit a finished one."""

    code = "autoproject_run_finished"

    def __init__(self, run_id: str, status: str):
        super().__init__(
            f"This AutoProject run has already finished ({status}).",
            details={"status": status},
        )
        self.run_id = run_id
        self.status = status


class AutoProjectRunExistsError(AutoProjectStoreError):
    """The conversation already has its run: a run conversation takes
    exactly one user message."""

    code = "autoproject_run_single_message"

    def __init__(self, conversation_id: str):
        super().__init__(
            "This AutoProject run already has its message. Send the next prompt "
            "from the AutoProject instead.",
            details={"conversation_id": conversation_id},
        )


class StaleBriefError(AutoProjectStoreError):
    """``expected_brief_version`` is not the brief's current version (someone
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


class BriefVersionNotFoundError(AutoProjectStoreError):
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
        raise AutoProjectValidationError(
            code,
            f"{label} is {len(text)} characters; the limit is {cap}.",
            cap=cap,
            actual=len(text),
        )
    return text


def normalize_title(title) -> str:
    """Return the title stripped with whitespace collapsed to single spaces.

    Raises:
        AutoProjectValidationError ``invalid_title``: not a string, empty, or over
            ``AUTOPROJECT_TITLE_MAX_CHARS`` after normalizing.
    """
    if not isinstance(title, str):
        raise AutoProjectValidationError(
            "invalid_title", "The AutoProject title must be a string.",
        )
    clean = _collapse(title)
    if not clean:
        raise AutoProjectValidationError("invalid_title", "The AutoProject title cannot be empty.")
    return _check_len(clean, AUTOPROJECT_TITLE_MAX_CHARS, "invalid_title", "The AutoProject title")


def title_from_prompt(prompt: str) -> str:
    """The default title cut from a first prompt.

    The first non-empty line with whitespace collapsed, cut to
    ``AUTOPROJECT_TITLE_MAX_CHARS`` with a trailing "…" when cut;
    :data:`DEFAULT_AUTOPROJECT_TITLE` when the prompt has no text.
    """
    if not isinstance(prompt, str):
        return DEFAULT_AUTOPROJECT_TITLE
    for line in prompt.splitlines():
        clean = _collapse(line)
        if clean:
            break
    else:
        return DEFAULT_AUTOPROJECT_TITLE
    return cut_text(clean, AUTOPROJECT_TITLE_MAX_CHARS)


def validate_brief(content) -> str:
    """Return the stripped brief (empty allowed).

    Raises:
        AutoProjectValidationError ``invalid_brief`` (not a string) or
            ``brief_too_long`` (over ``AUTOPROJECT_BRIEF_MAX_CHARS``).
    """
    if not isinstance(content, str):
        raise AutoProjectValidationError("invalid_brief", "The brief must be a string.")
    return _check_len(
        content.strip(), AUTOPROJECT_BRIEF_MAX_CHARS, "brief_too_long", "The brief",
    )


def validate_run_entry(summary, result, notes) -> tuple[str, str, str]:
    """Return ``(summary, result, notes)`` stripped (None -> "").

    Checked in that order, so the first field over its cap is the one
    reported.

    Raises:
        AutoProjectValidationError ``invalid_summary`` / ``invalid_result`` /
            ``invalid_notes`` (not a string), or ``summary_too_long`` /
            ``result_too_long`` / ``notes_too_long`` (over
            ``AUTOPROJECT_RUN_SUMMARY_MAX_CHARS`` / ``AUTOPROJECT_RUN_RESULT_MAX_CHARS`` /
            ``AUTOPROJECT_RUN_NOTES_MAX_CHARS``).
    """
    cleaned = []
    for name, value, cap in (
        ("summary", summary, AUTOPROJECT_RUN_SUMMARY_MAX_CHARS),
        ("result", result, AUTOPROJECT_RUN_RESULT_MAX_CHARS),
        ("notes", notes, AUTOPROJECT_RUN_NOTES_MAX_CHARS),
    ):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise AutoProjectValidationError(
                f"invalid_{name}", f"The run's {name} must be a string."
            )
        cleaned.append(
            _check_len(value.strip(), cap, f"{name}_too_long", f"The run's {name}")
        )
    return cleaned[0], cleaned[1], cleaned[2]


def _validate_prompt(prompt) -> str:
    """Return the prompt verbatim; refuse a non-string, blank or over-long
    (``AUTOPROJECT_PROMPT_MAX_CHARS``) one as ``invalid_prompt``."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise AutoProjectValidationError("invalid_prompt", "The prompt cannot be empty.")
    if len(prompt) > AUTOPROJECT_PROMPT_MAX_CHARS:
        raise AutoProjectValidationError(
            "invalid_prompt",
            f"The prompt is {len(prompt)} characters; the limit is "
            f"{AUTOPROJECT_PROMPT_MAX_CHARS}. Put long material in a file and "
            "point at it from the prompt.",
            cap=AUTOPROJECT_PROMPT_MAX_CHARS,
            actual=len(prompt),
        )
    return prompt


def _validate_model(model) -> Optional[str]:
    """Shape check of a model id (None clears). Whether the model is
    selectable is the caller's check (``is_model_allowed``)."""
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip() or len(model) > _MODEL_MAX_CHARS:
        raise AutoProjectValidationError("invalid_model", "Invalid model id.")
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
        raise AutoProjectValidationError("invalid_thinking_effort", "Invalid thinking level.")
    return effort.strip()


def _validate_source(source) -> str:
    if not isinstance(source, str) or not _BRIEF_SOURCE_RE.match(source):
        raise ValueError(
            f"Invalid brief source {source!r}: expected 'run:<run_id>', 'ui' "
            "or 'restore:<version>'."
        )
    return source


def _validate_expected_version(expected_brief_version) -> int:
    if (
        isinstance(expected_brief_version, bool) or not isinstance(expected_brief_version, int)
        or expected_brief_version < 0
    ):
        raise ValueError("expected_brief_version must be a non-negative integer.")
    return expected_brief_version


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _autoproject_to_dict(autoproject: AutoProject) -> dict:
    """Convert an AutoProject ORM instance to the AutoProject dict.

    Shape::

        {"id", "user_id", "title", "title_is_default",
         "brief", "brief_version", "brief_updated_at", "model",
         "thinking_effort", "archived", "next_run_seq", "event_seq",
         "unread_seq", "read_seq", "created_at", "updated_at",
         "last_activity_at",                    # ISO strings
         "unread"}                              # unread_seq > read_seq
    """
    return {
        "id": autoproject.id,
        "user_id": autoproject.user_id,
        "title": autoproject.title,
        "title_is_default": bool(autoproject.title_is_default),
        "brief": autoproject.brief,
        "brief_version": autoproject.brief_version,
        "brief_updated_at": _iso(autoproject.brief_updated_at),
        "model": autoproject.model,
        "thinking_effort": autoproject.thinking_effort,
        "archived": bool(autoproject.archived),
        "next_run_seq": autoproject.next_run_seq,
        "event_seq": autoproject.event_seq,
        "unread_seq": autoproject.unread_seq,
        "read_seq": autoproject.read_seq,
        "created_at": _iso(autoproject.created_at),
        "updated_at": _iso(autoproject.updated_at),
        "last_activity_at": _iso(autoproject.last_activity_at),
        "unread": (autoproject.unread_seq or 0) > (autoproject.read_seq or 0),
    }


def _run_to_dict(run: AutoProjectRun, *, include_result: bool = True) -> dict:
    """Convert an AutoProjectRun ORM instance to the run dict.

    Shape::

        {"id", "autoproject_id", "seq", "conversation_id", "prompt", "status",
         "summary", "result", "notes",          # None until completed
         "handoff_missing", "error", "model",
         "context_brief_version",               # None until frozen
         "brief_conflict_version",              # None until a brief_conflict
         "started_at", "finished_at"}           # ISO strings

    The frozen ``context`` text itself is never included (the column is
    deferred; :func:`get_run_context` reads it). With
    ``include_result=False`` the ``result`` key is left out and the
    attribute is never read (for rows loaded with ``result`` deferred).
    """
    row = {
        "id": run.id,
        "autoproject_id": run.autoproject_id,
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
        "brief_conflict_version": run.brief_conflict_version,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    })
    return row


def active_run_summary(run) -> Optional[dict]:
    """The ``active_run`` shape of a run: ``{id, seq, status}`` from a run
    dict or an ``AutoProjectRun`` row; None for None."""
    if run is None:
        return None
    if isinstance(run, dict):
        return {"id": run["id"], "seq": run["seq"], "status": run["status"]}
    return {"id": run.id, "seq": run.seq, "status": run.status}


def _brief_state(autoproject: AutoProject) -> dict:
    """The brief part of an AutoProject: ``{brief, brief_version, brief_updated_at}``."""
    return {
        "brief": autoproject.brief,
        "brief_version": autoproject.brief_version,
        "brief_updated_at": _iso(autoproject.brief_updated_at),
    }


def cut_text(text: str, cap: int) -> str:
    """``text`` when it fits in ``cap`` characters, else cut to fit with a
    trailing "…"."""
    if len(text) <= cap:
        return text
    return text[: max(cap - 1, 0)].rstrip() + "…"


# ---------------------------------------------------------------------------
# Private query helpers (run inside the caller's session)
# ---------------------------------------------------------------------------


async def _reload_autoproject(db, autoproject_id: str) -> Optional[AutoProject]:
    """The row as stored (naive-UTC datetimes), bypassing the identity map."""
    return await db.get(AutoProject, autoproject_id, populate_existing=True)


async def _owned_autoproject(db, user_id: int, autoproject_id: str) -> Optional[AutoProject]:
    autoproject = await db.get(AutoProject, autoproject_id, populate_existing=True)
    if autoproject is None or autoproject.user_id != user_id:
        return None
    return autoproject


async def _first_active_run(db, autoproject_id: str) -> Optional[AutoProjectRun]:
    return (await db.execute(
        select(AutoProjectRun)
        .where(
            AutoProjectRun.autoproject_id == autoproject_id,
            AutoProjectRun.status.in_(AUTOPROJECT_RUN_ACTIVE_STATUSES),
        )
        .order_by(AutoProjectRun.seq.asc())
        .limit(1)
    )).scalar_one_or_none()


async def _count_unarchived(db, user_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(AutoProject)
        .where(AutoProject.user_id == user_id, AutoProject.archived == False)  # noqa: E712
    )).scalar_one()


async def _touch_autoproject(db, autoproject_id: str, *, activity: bool, now: datetime) -> None:
    """Bump ``event_seq`` (and ``last_activity_at`` for a run write)."""
    values: dict = {AutoProject.event_seq: AutoProject.event_seq + 1}
    if activity:
        values[AutoProject.last_activity_at] = now
    await db.execute(
        update(AutoProject)
        .where(AutoProject.id == autoproject_id)
        .values(values)
        .execution_options(synchronize_session=False)
    )


# ---------------------------------------------------------------------------
# AutoProjects
# ---------------------------------------------------------------------------


async def create_autoproject(
    user_id: int,
    *,
    title: str,
    title_is_default: bool = False,
    model: Optional[str] = None,
    thinking_effort: Optional[str] = None,
    autoproject_id: Optional[str] = None,
) -> dict:
    """Create an AutoProject.

    Args:
        user_id: The owner.
        title: Normalized through :func:`normalize_title`.
        title_is_default: True when ``title`` was cut from the first prompt
            (:func:`title_from_prompt`), so the first run may still name the
            AutoProject.
        model, thinking_effort: Defaults for new runs (None = the user's
            default); shape-checked only.
        autoproject_id: Optional pre-generated id (lets a service lay out the AutoProject
            directory first); a uuid4 when omitted.

    Returns:
        The AutoProject dict.

    Raises:
        AutoProjectValidationError, AutoProjectCapReachedError (the user has
        ``MAX_ACTIVE_AUTOPROJECTS_PER_USER`` unarchived AutoProjects; counted after the
        insert in the same transaction, so concurrent creates cannot both
        slip under the cap).
    """
    clean_title = normalize_title(title)
    clean_model = _validate_model(model)
    clean_effort = _validate_thinking_effort(thinking_effort)
    now = _now()
    new_id = autoproject_id or str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        db.add(AutoProject(
            id=new_id,
            user_id=user_id,
            title=clean_title,
            title_is_default=bool(title_is_default),
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
        if await _count_unarchived(db, user_id) > MAX_ACTIVE_AUTOPROJECTS_PER_USER:
            await db.rollback()
            raise AutoProjectCapReachedError(MAX_ACTIVE_AUTOPROJECTS_PER_USER)
        await db.commit()
        return _autoproject_to_dict(await _reload_autoproject(db, new_id))


async def get_autoproject(user_id: int, autoproject_id: str) -> Optional[dict]:
    """The AutoProject dict, or None when missing or owned by someone else."""
    async with AsyncSessionLocal() as db:
        autoproject = await _owned_autoproject(db, user_id, autoproject_id)
        return _autoproject_to_dict(autoproject) if autoproject is not None else None


async def get_autoproject_by_id(autoproject_id: str) -> Optional[dict]:
    """The AutoProject dict whoever owns it, or None (server-side callers only:
    ``ToolContext``, the run driver)."""
    async with AsyncSessionLocal() as db:
        autoproject = await db.get(AutoProject, autoproject_id)
        return _autoproject_to_dict(autoproject) if autoproject is not None else None


async def list_autoprojects(user_id: int, *, archived: bool = False) -> list[dict]:
    """The user's unarchived (or, with ``archived=True``, archived) AutoProjects,
    newest ``last_activity_at`` first.

    Each row is the AutoProject dict plus ``active_run``: ``{id, seq, status}`` or
    None. The active runs come from one query over the listed AutoProjects.
    """
    async with AsyncSessionLocal() as db:
        listed = (
            select(AutoProject.id)
            .where(AutoProject.user_id == user_id, AutoProject.archived == bool(archived))
        )
        autoprojects = (await db.execute(
            select(AutoProject)
            .where(AutoProject.user_id == user_id, AutoProject.archived == bool(archived))
            .order_by(
                AutoProject.last_activity_at.desc(), AutoProject.created_at.desc(),
                AutoProject.id,
            )
        )).scalars().all()
        active_runs = (await db.execute(
            select(AutoProjectRun)
            .where(
                AutoProjectRun.autoproject_id.in_(listed),
                AutoProjectRun.status.in_(AUTOPROJECT_RUN_ACTIVE_STATUSES),
            )
            .order_by(AutoProjectRun.seq.desc())
        )).scalars().all()
        # Lowest seq wins if an AutoProject ever has more than one (not in v1).
        active_by_autoproject = {
            run.autoproject_id: active_run_summary(run) for run in active_runs
        }
        rows = []
        for autoproject in autoprojects:
            row = _autoproject_to_dict(autoproject)
            row["active_run"] = active_by_autoproject.get(autoproject.id)
            rows.append(row)
        return rows


async def update_autoproject(
    user_id: int,
    autoproject_id: str,
    *,
    title: Any = UNSET,
    model: Any = UNSET,
    thinking_effort: Any = UNSET,
) -> dict:
    """Edit the user-owned fields of an AutoProject. Fields left at :data:`UNSET`
    stay; ``model`` and ``thinking_effort`` accept None to clear.

    Setting ``title`` (even to the same text) clears ``title_is_default``:
    the user has named the AutoProject, so no run may rename it. ``event_seq`` and
    ``updated_at`` move only when something changed.

    Returns:
        The AutoProject dict.

    Raises:
        AutoProjectValidationError, AutoProjectNotFoundError.
    """
    values: dict = {}
    if title is not UNSET:
        values["title"] = normalize_title(title)
    if model is not UNSET:
        values["model"] = _validate_model(model)
    if thinking_effort is not UNSET:
        values["thinking_effort"] = _validate_thinking_effort(thinking_effort)

    async with AsyncSessionLocal() as db:
        autoproject = await _owned_autoproject(db, user_id, autoproject_id)
        if autoproject is None:
            raise AutoProjectNotFoundError(autoproject_id)
        changed = False
        for field, value in values.items():
            if getattr(autoproject, field) != value:
                setattr(autoproject, field, value)
                changed = True
        if "title" in values and autoproject.title_is_default:
            autoproject.title_is_default = False
            changed = True
        if not changed:
            return _autoproject_to_dict(autoproject)
        autoproject.event_seq = AutoProject.event_seq + 1
        autoproject.updated_at = _now()
        await db.commit()
        return _autoproject_to_dict(await _reload_autoproject(db, autoproject_id))


async def set_title_from_run(autoproject_id: str, title: str) -> bool:
    """Apply a run's ``autoproject_title``, only while the title is still the one
    cut from the first prompt.

    The ``title_is_default`` check is in the UPDATE's WHERE and the UPDATE
    clears the flag, so at most one model-side rename ever lands and none
    after the user renamed the AutoProject.

    Returns:
        Whether the title was applied.

    Raises:
        AutoProjectValidationError ``invalid_title``.
    """
    clean = normalize_title(title)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AutoProject)
            .where(
                AutoProject.id == autoproject_id,
                AutoProject.title_is_default == True,  # noqa: E712
            )
            .values({
                AutoProject.title: clean,
                AutoProject.title_is_default: False,
                AutoProject.event_seq: AutoProject.event_seq + 1,
                AutoProject.updated_at: _now(),
            })
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


async def set_autoproject_archived(user_id: int, autoproject_id: str, archived: bool) -> dict:
    """Archive (True) or unarchive (False) an AutoProject.

    A no-op when the AutoProject is already in that state. Only the flag,
    ``event_seq`` and ``updated_at`` change; ``last_activity_at`` is kept so
    an unarchived AutoProject returns to its place in the list.

    Returns:
        The AutoProject dict.

    Raises:
        AutoProjectNotFoundError; AutoProjectRunActiveError when archiving an
        AutoProject with an active run; AutoProjectCapReachedError when
        unarchiving would exceed ``MAX_ACTIVE_AUTOPROJECTS_PER_USER``.
    """
    archived = bool(archived)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AutoProject)
            .where(
                AutoProject.id == autoproject_id,
                AutoProject.user_id == user_id,
                AutoProject.archived != archived,
            )
            .values({
                AutoProject.archived: archived,
                AutoProject.event_seq: AutoProject.event_seq + 1,
                AutoProject.updated_at: _now(),
            })
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            autoproject = await _owned_autoproject(db, user_id, autoproject_id)
            if autoproject is None:
                raise AutoProjectNotFoundError(autoproject_id)
            return _autoproject_to_dict(autoproject)
        if archived:
            active = await _first_active_run(db, autoproject_id)
            if active is not None:
                # Read before the rollback expires the instance.
                summary = active_run_summary(active)
                await db.rollback()
                raise AutoProjectRunActiveError(
                    summary,
                    f"Run {summary['seq']} of this AutoProject is still "
                    f"{summary['status']}. Stop it before archiving the AutoProject.",
                )
        elif await _count_unarchived(db, user_id) > MAX_ACTIVE_AUTOPROJECTS_PER_USER:
            await db.rollback()
            raise AutoProjectCapReachedError(MAX_ACTIVE_AUTOPROJECTS_PER_USER)
        await db.commit()
        return _autoproject_to_dict(await _reload_autoproject(db, autoproject_id))


async def delete_autoproject(user_id: int, autoproject_id: str) -> list[str]:
    """Delete an AutoProject row; its runs, brief versions and run conversations
    cascade through the foreign keys.

    Returns the ids of every conversation that carried this ``autoproject_id``
    (abandoned run conversations with no run row included), collected
    inside the deleting transaction, so the caller can sweep their
    ``CHATS_DIR`` directories. Neither the filesystem nor a live run is
    touched here: the caller stops an active run first and removes
    ``AUTOPROJECTS_DIR/<id>`` after.

    Raises:
        AutoProjectNotFoundError.
    """
    async with AsyncSessionLocal() as db:
        # Take the write lock first so no run conversation can be added
        # between the collection and the delete.
        result = await db.execute(
            update(AutoProject)
            .where(AutoProject.id == autoproject_id, AutoProject.user_id == user_id)
            .values({AutoProject.event_seq: AutoProject.event_seq + 1})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise AutoProjectNotFoundError(autoproject_id)
        conversation_ids = set((await db.execute(
            select(Conversation.id).where(Conversation.autoproject_id == autoproject_id)
        )).scalars().all())
        conversation_ids.update((await db.execute(
            select(AutoProjectRun.conversation_id)
            .where(AutoProjectRun.autoproject_id == autoproject_id)
        )).scalars().all())
        # Delete the conversations explicitly too: a run whose conversation
        # somehow lacks the autoproject_id still loses its transcript row.
        if conversation_ids:
            await db.execute(
                delete(Conversation)
                .where(Conversation.id.in_(conversation_ids))
                .execution_options(synchronize_session=False)
            )
        await db.execute(
            delete(AutoProject)
            .where(AutoProject.id == autoproject_id)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return sorted(conversation_ids)


async def list_autoproject_conversation_ids(autoproject_id: str) -> list[str]:
    """Every conversation id that belongs to the AutoProject: ``conversations``
    rows carrying its ``autoproject_id`` (abandoned run conversations included)
    plus every run's ``conversation_id``. Not owner-scoped.

    The AutoProject-delete service reads it BEFORE :func:`delete_autoproject` to stop
    live turns, cancel wait handles and close approval cards while the
    conversations still exist; :func:`delete_autoproject` returns the same set
    collected inside its own transaction.
    """
    async with AsyncSessionLocal() as db:
        ids = set((await db.execute(
            select(Conversation.id).where(Conversation.autoproject_id == autoproject_id)
        )).scalars().all())
        ids.update((await db.execute(
            select(AutoProjectRun.conversation_id)
            .where(AutoProjectRun.autoproject_id == autoproject_id)
        )).scalars().all())
    return sorted(ids)


async def list_latest_active_autoprojects(limit: int = 20) -> list[dict]:
    """The most recently active AutoProjects across all users, newest
    ``last_activity_at`` first (admin System Reports; not owner-scoped).

    Joined to ``users`` for the owner identity. Each row carries the run
    figures of the AutoProject: ``run_count`` (runs that still exist; a deleted
    run is gone), ``failed_run_count`` and ``active_days``, the number of
    distinct UTC days on which a run started. A run starts with the user's
    one message, so that is the AutoProject's count of days with a user
    message, read without opening a chat history. Archived AutoProjects are
    listed (flagged).
    """
    runs = (
        select(
            AutoProjectRun.autoproject_id.label("autoproject_id"),
            func.count(AutoProjectRun.id).label("run_count"),
            func.sum(case(
                (AutoProjectRun.status == AUTOPROJECT_RUN_STATUS_FAILED, 1), else_=0,
            )).label("failed_run_count"),
            # SQLite stores the naive-UTC DateTime as "YYYY-MM-DD HH:MM:SS";
            # date() keeps the day part.
            func.count(func.distinct(func.date(AutoProjectRun.started_at))).label("active_days"),
        )
        .group_by(AutoProjectRun.autoproject_id)
        .subquery()
    )
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(
                AutoProject, User.email, User.name,
                runs.c.run_count, runs.c.failed_run_count, runs.c.active_days,
            )
            .join(User, AutoProject.user_id == User.id)
            .outerjoin(runs, runs.c.autoproject_id == AutoProject.id)
            .order_by(AutoProject.last_activity_at.desc(), AutoProject.id)
            .limit(limit)
        )
        return [
            {
                "id": autoproject.id,
                "title": autoproject.title,
                "archived": bool(autoproject.archived),
                "user_id": autoproject.user_id,
                "user_email": email,
                "user_name": name or "",
                "last_activity_at": _iso(autoproject.last_activity_at),
                "run_count": int(run_count or 0),
                "failed_run_count": int(failed_run_count or 0),
                "active_days": int(active_days or 0),
            }
            for autoproject, email, name, run_count, failed_run_count, active_days
            in result.all()
        ]


async def list_autoproject_ids_for_user(user_id: int) -> list[str]:
    """Every AutoProject id of the user, archived included (the account-delete
    sweep of ``AUTOPROJECTS_DIR``)."""
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(AutoProject.id)
            .where(AutoProject.user_id == user_id)
            .order_by(AutoProject.created_at)
        )).scalars().all())


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


async def allocate_run(
    autoproject_id: str,
    *,
    conversation_id: str,
    prompt: str,
    model: Optional[str] = None,
) -> dict:
    """Start a run: allocate its ``seq`` and insert it as ``running``.

    One write transaction that bumps ``next_run_seq``, ``event_seq`` and
    ``last_activity_at`` first (taking the write lock), then refuses an
    archived AutoProject or one with an active run (one active run per
    AutoProject; this is the one place it is checked), then inserts the run with the pre-bump
    ``next_run_seq`` as its ``seq``. A refusal rolls back, so its seq is not
    consumed; a seq once used is never reused, run deletes included.

    Args:
        autoproject_id: The AutoProject.
        conversation_id: The run's conversation, created for this AutoProject
            (``conversations.autoproject_id == autoproject_id``).
        prompt: The user's message, stored verbatim.
        model: The model the run uses.

    Returns:
        The run dict.

    Raises:
        AutoProjectValidationError ``invalid_prompt``, AutoProjectNotFoundError,
        AutoProjectArchivedError, AutoProjectRunActiveError, AutoProjectRunExistsError (the
        conversation already has its run).
        ValueError: the conversation is not a run conversation of this AutoProject.
    """
    clean_prompt = _validate_prompt(prompt)
    now = _now()
    async with AsyncSessionLocal() as db:
        bumped = (await db.execute(
            update(AutoProject)
            .where(AutoProject.id == autoproject_id)
            .values({
                AutoProject.next_run_seq: AutoProject.next_run_seq + 1,
                AutoProject.event_seq: AutoProject.event_seq + 1,
                AutoProject.last_activity_at: now,
            })
            .returning(AutoProject.next_run_seq, AutoProject.archived)
            .execution_options(synchronize_session=False)
        )).first()
        if bumped is None:
            await db.rollback()
            raise AutoProjectNotFoundError(autoproject_id)
        next_run_seq, archived = bumped
        if archived:
            await db.rollback()
            raise AutoProjectArchivedError(autoproject_id)
        active = await _first_active_run(db, autoproject_id)
        if active is not None:
            summary = active_run_summary(active)
            await db.rollback()
            raise AutoProjectRunActiveError(summary)
        conv_autoproject_id = (await db.execute(
            select(Conversation.autoproject_id).where(Conversation.id == conversation_id)
        )).first()
        if conv_autoproject_id is None or conv_autoproject_id[0] != autoproject_id:
            await db.rollback()
            raise ValueError(
                f"Conversation {conversation_id} is not a run conversation of "
                f"AutoProject {autoproject_id}."
            )
        run = AutoProjectRun(
            id=str(uuid.uuid4()),
            autoproject_id=autoproject_id,
            seq=next_run_seq - 1,
            conversation_id=conversation_id,
            prompt=clean_prompt,
            status=AUTOPROJECT_RUN_STATUS_RUNNING,
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
            raise AutoProjectRunExistsError(conversation_id) from None
        await db.commit()
        return _run_to_dict(await db.get(AutoProjectRun, run.id, populate_existing=True))


async def get_run(run_id: str) -> Optional[dict]:
    """The run dict, or None."""
    async with AsyncSessionLocal() as db:
        run = await db.get(AutoProjectRun, run_id)
        return _run_to_dict(run) if run is not None else None


async def get_run_by_conversation(conversation_id: str) -> Optional[dict]:
    """The run whose conversation this is, or None (no run row yet, or not
    a run conversation)."""
    async with AsyncSessionLocal() as db:
        run = (await db.execute(
            select(AutoProjectRun).where(AutoProjectRun.conversation_id == conversation_id)
        )).scalar_one_or_none()
        return _run_to_dict(run) if run is not None else None


async def get_run_by_seq(autoproject_id: str, seq: int) -> Optional[dict]:
    """Run number ``seq`` of the AutoProject, or None (never allocated, or deleted)."""
    async with AsyncSessionLocal() as db:
        run = (await db.execute(
            select(AutoProjectRun).where(
                AutoProjectRun.autoproject_id == autoproject_id, AutoProjectRun.seq == seq,
            )
        )).scalar_one_or_none()
        return _run_to_dict(run) if run is not None else None


async def get_active_run(autoproject_id: str) -> Optional[dict]:
    """The AutoProject's active run (``running`` or ``waiting``), or None."""
    async with AsyncSessionLocal() as db:
        run = await _first_active_run(db, autoproject_id)
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
    never reopened). Finished statuses set ``finished_at``. The AutoProject's
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
        AUTOPROJECT_RUN_ACTIVE_STATUSES if expected_from is None else frozenset(expected_from)
    )
    now = _now()
    values: dict = {AutoProjectRun.status: status}
    if status in AUTOPROJECT_RUN_FINISHED_STATUSES:
        values[AutoProjectRun.finished_at] = now
    if status == AUTOPROJECT_RUN_STATUS_FAILED:
        values[AutoProjectRun.error] = error
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            update(AutoProjectRun)
            .where(AutoProjectRun.id == run_id, AutoProjectRun.status.in_(allowed))
            .values(values)
            .returning(AutoProjectRun.autoproject_id)
            .execution_options(synchronize_session=False)
        )).first()
        if row is None:
            await db.rollback()
            return None
        await _touch_autoproject(db, row[0], activity=True, now=now)
        await db.commit()
        return _run_to_dict(await db.get(AutoProjectRun, run_id, populate_existing=True))


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
    filled in because the run never called ``complete_autoproject_run``.

    Returns:
        The run dict.

    Raises:
        AutoProjectValidationError, AutoProjectRunNotFoundError, AutoProjectRunFinishedError.
    """
    clean_summary, clean_result, clean_notes = validate_run_entry(summary, result, notes)
    now = _now()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            update(AutoProjectRun)
            .where(
                AutoProjectRun.id == run_id,
                AutoProjectRun.status.in_(AUTOPROJECT_RUN_ACTIVE_STATUSES),
            )
            .values({
                AutoProjectRun.summary: clean_summary,
                AutoProjectRun.result: clean_result,
                AutoProjectRun.notes: clean_notes,
                AutoProjectRun.handoff_missing: bool(handoff_missing),
                AutoProjectRun.status: AUTOPROJECT_RUN_STATUS_COMPLETED,
                AutoProjectRun.error: None,
                AutoProjectRun.finished_at: now,
            })
            .returning(AutoProjectRun.autoproject_id)
            .execution_options(synchronize_session=False)
        )).first()
        if row is None:
            await db.rollback()
            run = await db.get(AutoProjectRun, run_id, populate_existing=True)
            if run is None:
                raise AutoProjectRunNotFoundError(run_id)
            raise AutoProjectRunFinishedError(run_id, run.status)
        await _touch_autoproject(db, row[0], activity=True, now=now)
        await db.commit()
        return _run_to_dict(await db.get(AutoProjectRun, run_id, populate_existing=True))


async def save_run_entry(
    run_id: str, *, summary: str, result: str, notes: str,
    brief_conflict_version: Optional[int] = None,
) -> dict:
    """Save an active run's log entry WITHOUT finishing the run.

    The pending-entry state of a ``brief_conflict`` (chat/autoprojects/runs.py):
    ``complete_autoproject_run`` could not apply its brief because the user edited
    it during the run, so the entry is kept while the model merges and
    retries. An active run whose ``summary`` is not None has a pending
    entry; :func:`complete_run` later overwrites or finalizes it.
    ``brief_conflict_version``, when given, records the brief version the
    conflict handed the run: the only ``expected_brief_version`` its next
    ``complete_autoproject_run`` may use.

    The entry goes through :func:`validate_run_entry` first, so a refused
    entry writes nothing. ``status``, ``finished_at`` and the AutoProject's
    ``event_seq`` are left alone: a pending entry changes nothing the
    AutoProject page shows.

    Returns:
        The run dict.

    Raises:
        AutoProjectValidationError, AutoProjectRunNotFoundError, AutoProjectRunFinishedError.
    """
    clean_summary, clean_result, clean_notes = validate_run_entry(summary, result, notes)
    values: dict = {
        AutoProjectRun.summary: clean_summary,
        AutoProjectRun.result: clean_result,
        AutoProjectRun.notes: clean_notes,
    }
    if brief_conflict_version is not None:
        values[AutoProjectRun.brief_conflict_version] = int(brief_conflict_version)
    async with AsyncSessionLocal() as db:
        result_proxy = await db.execute(
            update(AutoProjectRun)
            .where(
                AutoProjectRun.id == run_id,
                AutoProjectRun.status.in_(AUTOPROJECT_RUN_ACTIVE_STATUSES),
            )
            .values(values)
            .execution_options(synchronize_session=False)
        )
        if result_proxy.rowcount != 1:
            await db.rollback()
            run = await db.get(AutoProjectRun, run_id, populate_existing=True)
            if run is None:
                raise AutoProjectRunNotFoundError(run_id)
            raise AutoProjectRunFinishedError(run_id, run.status)
        await db.commit()
        return _run_to_dict(await db.get(AutoProjectRun, run_id, populate_existing=True))


async def get_run_context(run_id: str) -> Optional[tuple[str, int]]:
    """The run's frozen starting context as ``(context,
    context_brief_version)``, or None when it is not frozen yet or the run
    is missing."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(AutoProjectRun.context, AutoProjectRun.context_brief_version)
            .where(AutoProjectRun.id == run_id)
        )).first()
    if row is None or row[0] is None or row[1] is None:
        return None
    return row[0], row[1]


async def freeze_run_context(
    run_id: str, *, context: str, brief_version: int,
) -> tuple[str, int]:
    """Freeze the run's starting context: the ``## AutoProject`` block its system
    prompt is built with and the brief version that block shows.

    One compare-and-set UPDATE that writes both columns only while
    ``context`` is still NULL, so the first writer wins and the frozen pair
    never changes afterwards; a later (or concurrent, losing) caller gets
    the pair that is stored. The run's status is not checked: freezing is
    part of building a turn, whatever the run's state.

    Returns:
        The ``(context, context_brief_version)`` stored on the row.

    Raises:
        AutoProjectRunNotFoundError.
        ValueError: ``context`` is not a string or is over
            ``AUTOPROJECT_CONTEXT_MAX_CHARS``, or ``brief_version`` is not a
            non-negative integer.
    """
    if not isinstance(context, str) or len(context) > AUTOPROJECT_CONTEXT_MAX_CHARS:
        raise ValueError(
            "context must be a string of at most "
            f"{AUTOPROJECT_CONTEXT_MAX_CHARS} characters."
        )
    if (
        isinstance(brief_version, bool) or not isinstance(brief_version, int)
        or brief_version < 0
    ):
        raise ValueError("brief_version must be a non-negative integer.")
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AutoProjectRun)
            .where(AutoProjectRun.id == run_id, AutoProjectRun.context.is_(None))
            .values({
                AutoProjectRun.context: context,
                AutoProjectRun.context_brief_version: brief_version,
            })
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await db.commit()
            return context, brief_version
        await db.rollback()
        row = (await db.execute(
            select(AutoProjectRun.context, AutoProjectRun.context_brief_version)
            .where(AutoProjectRun.id == run_id)
        )).first()
        if row is None:
            raise AutoProjectRunNotFoundError(run_id)
        return row[0], row[1]


async def delete_run(user_id: int, autoproject_id: str, run_id: str) -> str:
    """Delete a finished run's log entry together with its conversation.

    Deletes the conversation row (the run row cascades from it) and returns
    its id for the caller's ``CHATS_DIR`` sweep. ``next_run_seq`` is left
    alone, so the seq is never reused. Bumps the AutoProject's ``event_seq``.

    Raises:
        AutoProjectNotFoundError, AutoProjectRunNotFoundError (not a run of that AutoProject),
        AutoProjectRunActiveError (the run is still running or waiting).
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AutoProject)
            .where(AutoProject.id == autoproject_id, AutoProject.user_id == user_id)
            .values({AutoProject.event_seq: AutoProject.event_seq + 1})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise AutoProjectNotFoundError(autoproject_id)
        run = (await db.execute(
            select(AutoProjectRun).where(
                AutoProjectRun.id == run_id,
                AutoProjectRun.autoproject_id == autoproject_id,
            )
        )).scalar_one_or_none()
        if run is None:
            await db.rollback()
            raise AutoProjectRunNotFoundError(run_id)
        if run.status in AUTOPROJECT_RUN_ACTIVE_STATUSES:
            summary = active_run_summary(run)
            await db.rollback()
            raise AutoProjectRunActiveError(
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
            delete(AutoProjectRun)
            .where(AutoProjectRun.id == run_id)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return conversation_id


async def list_runs_page(
    autoproject_id: str, *, before_seq: Optional[int] = None, limit: int = 20,
) -> dict:
    """One page of the AutoProject's runs, newest first, keyset-paged on ``seq``.

    Returns:
        ``{"runs": [run dict, ...], "next_before_seq": int | None}``: at
        most ``limit`` (clamped to 1..100) runs with ``seq < before_seq``
        (all when None), and the ``before_seq`` of the next older page, or
        None when there is nothing older.
    """
    limit = max(1, min(int(limit), _RUNS_PAGE_MAX_LIMIT))
    stmt = select(AutoProjectRun).where(AutoProjectRun.autoproject_id == autoproject_id)
    if before_seq is not None:
        stmt = stmt.where(AutoProjectRun.seq < before_seq)
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            stmt.order_by(AutoProjectRun.seq.desc()).limit(limit + 1)
        )).scalars().all()
    has_more = len(runs) > limit
    runs = runs[:limit]
    return {
        "runs": [_run_to_dict(r) for r in runs],
        "next_before_seq": runs[-1].seq if has_more else None,
    }


async def get_run_seqs(autoproject_id: str, run_ids: Iterable[str]) -> dict[str, int]:
    """``{run_id: seq}`` for the runs of ``autoproject_id`` among ``run_ids``
    (one query). Deleted runs and runs of another AutoProject are absent. The
    AutoProject page resolves brief sources ``run:<id>`` to "run N" with it."""
    ids = sorted({rid for rid in run_ids if isinstance(rid, str) and rid})
    if not ids:
        return {}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(AutoProjectRun.id, AutoProjectRun.seq)
            .where(AutoProjectRun.autoproject_id == autoproject_id, AutoProjectRun.id.in_(ids))
        )).all()
    return {run_id: seq for run_id, seq in rows}


async def list_context_runs(autoproject_id: str, *, before_seq: int, limit: int) -> list[dict]:
    """The runs the AutoProject context of run ``before_seq`` shows: ``seq <
    before_seq``, newest first, at most ``limit`` (clamped to 1..500).

    The ``result`` column (up to ``AUTOPROJECT_RUN_RESULT_MAX_CHARS`` each) is
    never loaded: each row is the run dict without its ``result`` key. The
    context builder (chat/autoprojects/context.py) reads no result, so loading the
    window and the index costs no more than their prompts, summaries and
    notes.
    """
    limit = max(1, min(int(limit), _LIST_RUNS_MAX_LIMIT))
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(AutoProjectRun)
            .options(defer(AutoProjectRun.result, raiseload=True))
            .where(
                AutoProjectRun.autoproject_id == autoproject_id,
                AutoProjectRun.seq < int(before_seq),
            )
            .order_by(AutoProjectRun.seq.desc())
            .limit(limit)
        )).scalars().all()
        return [_run_to_dict(r, include_result=False) for r in runs]


async def list_run_index(
    autoproject_id: str, *, offset: int = 0, limit: int = 50, prompt_chars: int = 200,
) -> list[dict]:
    """One page of the AutoProject's run log as index rows, newest first,
    offset-paged (``limit`` clamped to 1..500). For the ``list_autoproject_runs``
    tool (chat/gemini_api/tool_handlers/autoproject_runs.py).

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
                AutoProjectRun.id, AutoProjectRun.seq, AutoProjectRun.status,
                AutoProjectRun.summary, AutoProjectRun.handoff_missing,
                AutoProjectRun.started_at, AutoProjectRun.finished_at,
                func.substr(AutoProjectRun.prompt, 1, max(0, int(prompt_chars))),
            )
            .where(AutoProjectRun.autoproject_id == autoproject_id)
            .order_by(AutoProjectRun.seq.desc())
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


async def count_runs(autoproject_id: str, *, before_seq: Optional[int] = None) -> int:
    """How many runs the AutoProject has (deleted runs not counted); with
    ``before_seq``, only those with ``seq < before_seq``."""
    stmt = (
        select(func.count())
        .select_from(AutoProjectRun)
        .where(AutoProjectRun.autoproject_id == autoproject_id)
    )
    if before_seq is not None:
        stmt = stmt.where(AutoProjectRun.seq < int(before_seq))
    async with AsyncSessionLocal() as db:
        return (await db.execute(stmt)).scalar_one()


async def list_runs_with_status(status: str) -> list[dict]:
    """Every run in ``status``, across AutoProjects, oldest first (the
    startup sweep of ``waiting`` runs). Not owner-scoped."""
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            select(AutoProjectRun)
            .where(AutoProjectRun.status == status)
            .order_by(AutoProjectRun.started_at, AutoProjectRun.id)
        )).scalars().all()
        return [_run_to_dict(r) for r in runs]


async def fail_interrupted_runs(error: str = "interrupted by a restart") -> list[dict]:
    """Startup sweep: mark every ``running`` run ``failed`` with ``error``.

    ``waiting`` runs are left alone: their wait handles survive a restart
    and resume them. Each affected AutoProject's ``event_seq`` and
    ``last_activity_at`` move in the same transaction.

    Returns:
        The affected runs (run dicts, after the change).
    """
    now = _now()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            update(AutoProjectRun)
            .where(AutoProjectRun.status == AUTOPROJECT_RUN_STATUS_RUNNING)
            .values({
                AutoProjectRun.status: AUTOPROJECT_RUN_STATUS_FAILED,
                AutoProjectRun.error: error,
                AutoProjectRun.finished_at: now,
            })
            .returning(AutoProjectRun.id, AutoProjectRun.autoproject_id)
            .execution_options(synchronize_session=False)
        )).all()
        if not rows:
            await db.rollback()
            return []
        for autoproject_id in {autoproject_id for _, autoproject_id in rows}:
            await _touch_autoproject(db, autoproject_id, activity=True, now=now)
        await db.commit()
        runs = (await db.execute(
            select(AutoProjectRun)
            .where(AutoProjectRun.id.in_([run_id for run_id, _ in rows]))
            .order_by(AutoProjectRun.autoproject_id, AutoProjectRun.seq)
            .execution_options(populate_existing=True)
        )).scalars().all()
        return [_run_to_dict(r) for r in runs]


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


async def _write_brief(
    db, autoproject_id: str, content: str, *, expected_brief_version: int, source: str,
) -> dict:
    """The one brief write, inside the caller's session: conditional UPDATE
    on the version token, the new version's row, then pruning to
    ``AUTOPROJECT_BRIEF_MAX_VERSIONS``. Commits on success, rolls back on refusal.
    """
    now = _now()
    result = await db.execute(
        update(AutoProject)
        .where(AutoProject.id == autoproject_id, AutoProject.brief_version == expected_brief_version)
        .values({
            AutoProject.brief: content,
            AutoProject.brief_version: AutoProject.brief_version + 1,
            AutoProject.brief_updated_at: now,
            AutoProject.event_seq: AutoProject.event_seq + 1,
            AutoProject.updated_at: now,
        })
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        autoproject = await _reload_autoproject(db, autoproject_id)
        if autoproject is None:
            raise AutoProjectNotFoundError(autoproject_id)
        raise StaleBriefError(_brief_state(autoproject))
    new_version = expected_brief_version + 1
    db.add(AutoProjectBriefVersion(
        autoproject_id=autoproject_id,
        version=new_version,
        content=content,
        source=source,
        created_at=now,
    ))
    await db.flush()
    kept = (
        select(AutoProjectBriefVersion.id)
        .where(AutoProjectBriefVersion.autoproject_id == autoproject_id)
        .order_by(AutoProjectBriefVersion.version.desc())
        .limit(AUTOPROJECT_BRIEF_MAX_VERSIONS)
    )
    await db.execute(
        delete(AutoProjectBriefVersion)
        .where(
            AutoProjectBriefVersion.autoproject_id == autoproject_id,
            AutoProjectBriefVersion.id.not_in(kept),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return _brief_state(await _reload_autoproject(db, autoproject_id))


async def set_brief(
    autoproject_id: str, content: str, *, expected_brief_version: int, source: str,
) -> dict:
    """Write a new brief.

    In one transaction: the conditional UPDATE (``brief_version`` must still
    be ``expected_brief_version``; it becomes ``expected_brief_version + 1``), an
    ``autoproject_brief_versions`` row holding the new text under the new version
    with ``source`` as its author, and pruning of that AutoProject's versions
    beyond ``AUTOPROJECT_BRIEF_MAX_VERSIONS`` (oldest first). Not owner-scoped:
    callers do the access check.

    Args:
        content: Through :func:`validate_brief` (stripped; empty allowed).
        expected_brief_version: The ``brief_version`` the writer started from.
        source: ``run:<run_id>``, ``ui`` or ``restore:<version>``.

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        AutoProjectValidationError, StaleBriefError (``.current`` is the brief as
        it now is), AutoProjectNotFoundError.
        ValueError: malformed ``source`` or ``expected_brief_version``.
    """
    clean = validate_brief(content)
    _validate_source(source)
    _validate_expected_version(expected_brief_version)
    async with AsyncSessionLocal() as db:
        return await _write_brief(
            db, autoproject_id, clean, expected_brief_version=expected_brief_version, source=source,
        )


async def list_brief_versions(autoproject_id: str, *, excerpt_chars: int = 200) -> list[dict]:
    """The stored brief versions, newest first, as ``{version, source,
    created_at, excerpt}`` (the text with whitespace collapsed, cut to
    ``excerpt_chars`` with a trailing "…" when cut)."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                AutoProjectBriefVersion.version,
                AutoProjectBriefVersion.source,
                AutoProjectBriefVersion.created_at,
                AutoProjectBriefVersion.content,
            )
            .where(AutoProjectBriefVersion.autoproject_id == autoproject_id)
            .order_by(AutoProjectBriefVersion.version.desc())
        )).all()
    return [
        {
            "version": version,
            "source": source,
            "created_at": _iso(created_at),
            "excerpt": cut_text(_collapse(content), excerpt_chars),
        }
        for version, source, created_at, content in rows
    ]


async def get_brief_version(autoproject_id: str, version: int) -> Optional[dict]:
    """One stored version as ``{version, source, created_at, content}``, or
    None (never written, or pruned)."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(AutoProjectBriefVersion).where(
                AutoProjectBriefVersion.autoproject_id == autoproject_id,
                AutoProjectBriefVersion.version == version,
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


async def get_brief_version_source(autoproject_id: str, version: int) -> Optional[str]:
    """The ``source`` of stored version ``version`` (``ui``, ``run:<id>``,
    ``restore:<n>``), or None (version 0, never written, or pruned). Reads
    no brief text."""
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(AutoProjectBriefVersion.source).where(
                AutoProjectBriefVersion.autoproject_id == autoproject_id,
                AutoProjectBriefVersion.version == version,
            )
        )).scalar_one_or_none()


async def restore_brief_version(
    autoproject_id: str, version: int, *, expected_brief_version: int,
) -> dict:
    """Write stored version ``version``'s text as a new brief version with
    ``source = "restore:<version>"``, through the same transaction as
    :func:`set_brief` (so a restore during a run takes the same conflict
    path).

    Returns:
        ``{brief, brief_version, brief_updated_at}`` after the write.

    Raises:
        AutoProjectNotFoundError, BriefVersionNotFoundError, StaleBriefError,
        AutoProjectValidationError (the stored text is over today's cap).
    """
    _validate_expected_version(expected_brief_version)
    async with AsyncSessionLocal() as db:
        content = (await db.execute(
            select(AutoProjectBriefVersion.content).where(
                AutoProjectBriefVersion.autoproject_id == autoproject_id,
                AutoProjectBriefVersion.version == version,
            )
        )).scalar_one_or_none()
        if content is None:
            if await db.get(AutoProject, autoproject_id) is None:
                raise AutoProjectNotFoundError(autoproject_id)
            raise BriefVersionNotFoundError(version)
        return await _write_brief(
            db, autoproject_id, validate_brief(content),
            expected_brief_version=expected_brief_version, source=f"restore:{int(version)}",
        )


# ---------------------------------------------------------------------------
# Event and unread seqs
# ---------------------------------------------------------------------------


async def bump_event_seq(autoproject_id: str) -> Optional[int]:
    """Bump ``event_seq`` for a change to the AutoProject page made outside
    this module (a question posted or answered). Returns the new seq, or None when the
    AutoProject is gone."""
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            update(AutoProject)
            .where(AutoProject.id == autoproject_id)
            .values({AutoProject.event_seq: AutoProject.event_seq + 1})
            .returning(AutoProject.event_seq)
            .execution_options(synchronize_session=False)
        )).first()
        await db.commit()
        return row[0] if row is not None else None


async def mark_autoproject_unread(autoproject_id: str) -> bool:
    """Raise ``unread_seq`` to ``event_seq`` (the run left something for the
    user to look at). Returns whether it moved; it never lowers."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AutoProject)
            .where(
                AutoProject.id == autoproject_id,
                AutoProject.event_seq > AutoProject.unread_seq,
            )
            .values({AutoProject.unread_seq: AutoProject.event_seq})
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1


async def mark_autoproject_read(user_id: int, autoproject_id: str, *, up_to_seq: int) -> bool:
    """Mark the AutoProject read up to ``up_to_seq``: ``read_seq`` becomes
    ``max(read_seq, min(up_to_seq, event_seq))``, so a late or repeated
    acknowledgement never hides newer output.

    Returns:
        Whether ``read_seq`` moved.

    Raises:
        AutoProjectNotFoundError: missing, or owned by someone else.
    """
    target = func.min(literal(int(up_to_seq)), AutoProject.event_seq)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AutoProject)
            .where(
                AutoProject.id == autoproject_id, AutoProject.user_id == user_id,
                target > AutoProject.read_seq,
            )
            .values({AutoProject.read_seq: target})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            await db.commit()
            return True
        await db.rollback()
        if await _owned_autoproject(db, user_id, autoproject_id) is None:
            raise AutoProjectNotFoundError(autoproject_id)
        return False
