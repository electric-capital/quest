"""Caps and shared error text for Quest Tasks.

Every limit the spec leaves tunable lives here so the store, the context
builder, the tools, the routes and the tests agree on one number. Every cap
that feeds the task context (brief, notes, summary, window) is enforced at
write time, not at prompt-build time, so the context stays bounded however
many runs a task has.
"""

# --- Task text (``tasks`` row) ---------------------------------------------

TASK_TITLE_MAX_CHARS = 120
TASK_INSTRUCTIONS_MAX_CHARS = 8_000
TASK_BRIEF_MAX_CHARS = 12_000

# Stored brief versions per task (``task_brief_versions``); the oldest are
# pruned in the transaction that writes a new one.
TASK_BRIEF_MAX_VERSIONS = 50

# Unarchived tasks per user; creating (or unarchiving) beyond it is refused.
MAX_ACTIVE_TASKS_PER_USER = 200

# --- Run log entry (``task_runs`` row) -------------------------------------

TASK_RUN_SUMMARY_MAX_CHARS = 200
TASK_RUN_RESULT_MAX_CHARS = 50_000
TASK_RUN_NOTES_MAX_CHARS = 4_000

# Run statuses. ``running`` and ``waiting`` are the active ones (one active
# run per task in v1).
TASK_RUN_STATUS_RUNNING = "running"
TASK_RUN_STATUS_WAITING = "waiting"
TASK_RUN_STATUS_COMPLETED = "completed"
TASK_RUN_STATUS_FAILED = "failed"
TASK_RUN_STATUS_STOPPED = "stopped"
TASK_RUN_STATUSES = (
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_WAITING,
    TASK_RUN_STATUS_COMPLETED,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_STOPPED,
)
TASK_RUN_ACTIVE_STATUSES = frozenset({
    TASK_RUN_STATUS_RUNNING, TASK_RUN_STATUS_WAITING,
})
TASK_RUN_FINISHED_STATUSES = frozenset({
    TASK_RUN_STATUS_COMPLETED, TASK_RUN_STATUS_FAILED, TASK_RUN_STATUS_STOPPED,
})

# The ``conversations.origin`` value of a run conversation.
TASK_CONVERSATION_ORIGIN = "task"

# --- Task context (chat/tasks/context.py) ----------------------------------

# How much of a run's prompt the log window shows.
TASK_PROMPT_EXCERPT_CHARS = 500
# The log window: full entries, newest first, within both limits.
TASK_LOG_WINDOW_MAX_ENTRIES = 8
TASK_LOG_WINDOW_MAX_CHARS = 20_000
# One-line index entries after the window; the rest are counted.
TASK_LOG_INDEX_MAX_ENTRIES = 100
# Asserted upper bound of the whole task context block.
TASK_CONTEXT_MAX_CHARS = 50_000

# --- Questions (ask_task_question) -----------------------------------------

TASK_QUESTION_MAX_CHARS = 2_000
TASK_ANSWER_MAX_CHARS = 8_000
TASK_QUESTION_MAX_OPTIONS = 6

# --- Completion (complete_task_run) ----------------------------------------

# Follow-up turns sent to a run that ended without complete_task_run.
MAX_TASK_COMPLETION_NUDGES = 1

# --- Feature gate ----------------------------------------------------------

# The connected-services pseudo-key (api/instructions.py
# PSEUDO_SERVICE_KEYS) that hides the task tools and the system skill from
# prompts while the ``tasks`` gate (config/feature_gates.py FEATURE_TASKS)
# is closed for the user.
TASKS_SERVICE_KEY = "tasks"


def task_not_found_message(task_id: str) -> str:
    """The ONE not-found text.

    Used verbatim for a task that does not exist AND for a task owned by
    someone else (invariant 7: a foreign task behaves exactly like a missing
    one). Never build a task not-found message any other way.
    """
    return f"Task not found: {task_id}"


def tasks_disabled_message() -> str:
    """Model/user-facing text when the ``tasks`` feature gate is closed."""
    return (
        "Quest Tasks is not enabled for this user. An admin can enable it "
        "in Settings > Features."
    )
