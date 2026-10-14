"""Caps, retention, and shared error text for Quest Tables.

Every limit the spec leaves tunable lives here so the store, the table
file storage, the tools, the routes, the action requests and the tests
agree on one number.
"""

# --- Table metadata (quest.db ``data_tables``) -----------------------------

TABLE_TITLE_MAX_LEN = 200
TABLE_DESCRIPTION_MAX_LEN = 500

# A user table (no project) is always "private"; only project tables can be
# "public", copying their project's immutable ``public`` flag at creation.
TABLE_MODES = ("private", "public")
TABLE_SHARE_PERMISSIONS = ("read", "write")

# --- Size caps -------------------------------------------------------------

# Rows per table, columns per table, tables per owner. A write that would
# exceed a cap is refused with the cap named.
TABLE_MAX_ROWS = 100_000
TABLE_MAX_COLUMNS = 100
TABLE_MAX_PER_OWNER = 200

# Rows per write tool call / card / UI action (also the cap on a
# filter-based delete_rows).
TABLE_WRITE_MAX_ROWS = 500

# query_rows: rows per page and response size (rows halved until it fits).
TABLE_QUERY_MAX_ROWS = 1000
TABLE_QUERY_MAX_BYTES = 100 * 1024

# --- Columns and values ----------------------------------------------------

TABLE_COLUMN_TYPES = ("text", "number", "url", "boolean", "enum")
# Column ``unique`` is allowed on these types only.
TABLE_UNIQUE_COLUMN_TYPES = ("text", "number", "url")

TABLE_COLUMN_NAME_MAX_LEN = 80
TABLE_TEXT_MAX_CHARS = 10_000
TABLE_NUMBER_MAX_DIGITS = 38  # significant digits of a canonical Decimal
TABLE_NUMBER_MAX_PRECISION = 10  # column ``precision`` is 0..10
TABLE_URL_MAX_CHARS = 2048
TABLE_ENUM_MAX_OPTIONS = 200
TABLE_ENUM_OPTION_MAX_LEN = 100

# Schema operations refused because of existing rows (``rows_violate``)
# name at most this many offending row ids.
TABLE_ROWS_VIOLATE_MAX_IDS = 20

# --- Change log (``changes`` in table.db) ----------------------------------

# Before/after images of one change; a bigger write is refused with
# ``change_too_large`` so every recorded change stays undoable.
TABLE_CHANGE_MAX_BYTES = 5 * 1024 * 1024
# Pruned at write time: older than this, or beyond this count (newest kept).
TABLE_CHANGE_RETENTION_DAYS = 90
TABLE_CHANGE_MAX_COUNT = 2000

# --- Feature gate ----------------------------------------------------------

# The connected-services pseudo-key (api/instructions.py
# PSEUDO_SERVICE_KEYS) that hides the table tools and the system skill from
# prompts while the ``tables`` gate (config/feature_gates.py
# FEATURE_TABLES) is closed for the user.
TABLES_SERVICE_KEY = "tables"


def tables_disabled_message() -> str:
    """Model/user-facing text when the ``tables`` feature gate is closed."""
    return (
        "Quest Tables is not enabled for this user. An admin can enable it "
        "in Settings > Features."
    )
