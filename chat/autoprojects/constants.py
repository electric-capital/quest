"""Caps and shared error text for AutoProjects.

Every limit the spec leaves tunable lives here so the store, the context
builder, the tools, the routes and the tests agree on one number. Every cap
that feeds the AutoProject context (brief, notes, summary, window) is enforced at
write time, not at prompt-build time, so the context stays bounded however
many runs an AutoProject has.
"""

from datetime import timedelta

# --- AutoProject text (``autoprojects`` row) ---------------------------------------------

AUTOPROJECT_TITLE_MAX_CHARS = 120
AUTOPROJECT_BRIEF_MAX_CHARS = 12_000

# Stored brief versions per AutoProject (``autoproject_brief_versions``); the oldest are
# pruned in the transaction that writes a new one.
AUTOPROJECT_BRIEF_MAX_VERSIONS = 50

# Unarchived AutoProjects per user; creating (or unarchiving) beyond it is refused.
MAX_ACTIVE_AUTOPROJECTS_PER_USER = 200

# --- Run log entry (``autoproject_runs`` row) -------------------------------------

# The run's prompt (the run conversation's one user message), stored verbatim.
AUTOPROJECT_PROMPT_MAX_CHARS = 50_000

AUTOPROJECT_RUN_SUMMARY_MAX_CHARS = 200
AUTOPROJECT_RUN_RESULT_MAX_CHARS = 50_000
AUTOPROJECT_RUN_NOTES_MAX_CHARS = 4_000

# Run statuses. ``running`` and ``waiting`` are the active ones (one active
# run per AutoProject in v1).
AUTOPROJECT_RUN_STATUS_RUNNING = "running"
AUTOPROJECT_RUN_STATUS_WAITING = "waiting"
AUTOPROJECT_RUN_STATUS_COMPLETED = "completed"
AUTOPROJECT_RUN_STATUS_FAILED = "failed"
AUTOPROJECT_RUN_STATUS_STOPPED = "stopped"
AUTOPROJECT_RUN_STATUSES = (
    AUTOPROJECT_RUN_STATUS_RUNNING,
    AUTOPROJECT_RUN_STATUS_WAITING,
    AUTOPROJECT_RUN_STATUS_COMPLETED,
    AUTOPROJECT_RUN_STATUS_FAILED,
    AUTOPROJECT_RUN_STATUS_STOPPED,
)
AUTOPROJECT_RUN_ACTIVE_STATUSES = frozenset({
    AUTOPROJECT_RUN_STATUS_RUNNING, AUTOPROJECT_RUN_STATUS_WAITING,
})
AUTOPROJECT_RUN_FINISHED_STATUSES = frozenset({
    AUTOPROJECT_RUN_STATUS_COMPLETED, AUTOPROJECT_RUN_STATUS_FAILED,
    AUTOPROJECT_RUN_STATUS_STOPPED,
})

# The ``conversations.origin`` value of a run conversation.
AUTOPROJECT_CONVERSATION_ORIGIN = "autoproject"

# A run conversation created by ``POST /autoprojects/{id}/runs`` but never sent
# (no ``autoproject_runs`` row) is deleted by the startup sweep once it is older
# than this (chat/autoprojects/service.py sweep_abandoned_run_conversations).
AUTOPROJECT_ABANDONED_RUN_CONVERSATION_MAX_AGE = timedelta(days=1)
# Creating a run conversation first deletes the AutoProject's earlier unsent
# ones older than this, so repeated starts that never send cannot pile up
# conversations between restarts. The composer creates the conversation and
# sends on it at once, so only a failed start leaves one behind.
AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE = timedelta(minutes=10)

# --- AutoProject context (chat/autoprojects/context.py) ----------------------------------

# How much of a run's prompt the log window shows.
AUTOPROJECT_PROMPT_EXCERPT_CHARS = 500
# The log window: full entries, newest first, within both limits.
AUTOPROJECT_LOG_WINDOW_MAX_ENTRIES = 8
AUTOPROJECT_LOG_WINDOW_MAX_CHARS = 20_000
# One-line index entries after the window; the rest are counted.
AUTOPROJECT_LOG_INDEX_MAX_ENTRIES = 100
# Characters of summary (or prompt, for a run without one) on an index
# line, whitespace collapsed.
AUTOPROJECT_LOG_INDEX_SUMMARY_CHARS = 120
# Characters of all index lines together. The builder lowers this further
# when the rest of the block leaves less room (below).
AUTOPROJECT_LOG_INDEX_MAX_CHARS = 8_000
# How much of a failed run's ``error`` the context shows (``error`` has no
# write-time cap).
AUTOPROJECT_RUN_ERROR_EXCERPT_CHARS = 300
# Asserted upper bound of the whole AutoProject context block (the worst-case
# size budget is in chat/autoprojects/context.py's module docstring).
AUTOPROJECT_CONTEXT_MAX_CHARS = 50_000

# --- Questions (ask_autoproject_question) -----------------------------------------

AUTOPROJECT_QUESTION_MAX_CHARS = 2_000
AUTOPROJECT_ANSWER_MAX_CHARS = 8_000
AUTOPROJECT_QUESTION_MAX_OPTIONS = 6
# An option is a clickable answer label, so it stays short.
AUTOPROJECT_QUESTION_OPTION_MAX_CHARS = 200

# --- Completion (complete_autoproject_run) ----------------------------------------

# Follow-up turns sent to a run that ended without complete_autoproject_run.
MAX_AUTOPROJECT_COMPLETION_NUDGES = 1

# --- Feature gate ----------------------------------------------------------

# The connected-services pseudo-key (api/instructions.py
# PSEUDO_SERVICE_KEYS) that hides the AutoProject tools and the system skill from
# prompts while the ``autoprojects`` gate (config/feature_gates.py FEATURE_AUTOPROJECTS)
# is closed for the user.
AUTOPROJECTS_SERVICE_KEY = "autoprojects"


def autoproject_not_found_message(autoproject_id: str) -> str:
    """The ONE not-found text.

    Used verbatim for an AutoProject that does not exist AND for an AutoProject owned by
    someone else (invariant 7: a foreign AutoProject behaves exactly like a missing
    one). Never build an AutoProject not-found message any other way.
    """
    return f"AutoProject not found: {autoproject_id}"


def autoprojects_disabled_message() -> str:
    """Model/user-facing text when the ``autoprojects`` feature gate is closed."""
    return (
        "AutoProjects are not enabled for this user. An admin can enable them "
        "in Settings > Features."
    )


# --- Run log read tools (list_autoproject_runs, read_autoproject_run) --------------------

# Entries per list_autoproject_runs page, newest first; the tool pages the whole log
# with ``offset``. An entry is one short line's worth (seq, date, status,
# summary), so a page stays small.
AUTOPROJECT_RUN_LIST_PAGE_SIZE = 50
