"""Caps and shared error text for Quest Tables.

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
# The filter, sort and paging limits are in "Queries and filters" below;
# listing limits live with the tools and routes that enforce them.
TABLE_QUERY_MAX_ROWS = 1000
TABLE_QUERY_MAX_BYTES = 100 * 1024

# table.db connections wait this long on a locked database.
TABLE_DB_BUSY_TIMEOUT_S = 5

# --- Queries and filters (chat/tables/filters.py) --------------------------

# query_rows paging: rows per page when ``limit`` is omitted, and the
# largest ``offset``. ``GET /tables/{id}/rows`` (the grid) caps a page at
# TABLE_GRID_MAX_LIMIT instead of TABLE_QUERY_MAX_ROWS; the route enforces it.
TABLE_QUERY_DEFAULT_LIMIT = 100
TABLE_QUERY_MAX_OFFSET = 100_000
TABLE_GRID_MAX_LIMIT = 200

# Filter DSL: nesting depth (a bare leaf is 1, each and/or/not adds 1) and
# leaves per filter.
TABLE_FILTER_MAX_DEPTH = 3
TABLE_FILTER_MAX_LEAVES = 20
# Values in one ``in`` / ``contains_any`` / ``contains_all`` list. The spec
# sets no number; this is a default.
TABLE_FILTER_IN_MAX_VALUES = 100

# Sort keys per query.
TABLE_SORT_MAX_KEYS = 3

# --- Columns and values ----------------------------------------------------

TABLE_COLUMN_TYPES = ("text", "number", "url", "boolean", "enum")
# Column ``unique`` is allowed on these types only.
TABLE_UNIQUE_COLUMN_TYPES = ("text", "number", "url")

TABLE_COLUMN_NAME_MAX_LEN = 80
# Immutable server-assigned column key: prefix + this many hex digits
# (``c_1a2b3c4d``); also the SQL column name in ``rows``.
TABLE_COLUMN_KEY_PREFIX = "c_"
TABLE_COLUMN_KEY_HEX_LEN = 8
TABLE_TEXT_MAX_CHARS = 10_000
TABLE_NUMBER_MAX_DIGITS = 38  # significant digits of a canonical Decimal
TABLE_NUMBER_MAX_PRECISION = 10  # column ``precision`` is 0..10
TABLE_URL_MAX_CHARS = 2048
TABLE_ENUM_MAX_OPTIONS = 200
TABLE_ENUM_OPTION_MAX_LEN = 100
# Column ``description``; matches the table description cap.
TABLE_COLUMN_DESCRIPTION_MAX_LEN = 500

# Operations in one schema change (one change_table_schema card or UI save).
TABLE_SCHEMA_MAX_OPERATIONS = 200

# Schema operations refused because of existing rows (``rows_violate``)
# name at most this many offending row ids.
TABLE_ROWS_VIOLATE_MAX_IDS = 20

# --- Change log (``changes`` in table.db) ----------------------------------

# Before/after images of one change; a bigger write is refused with
# ``change_too_large``, so a restore replays one bounded entry at a time.
# The log itself is kept in full (no pruning).
TABLE_CHANGE_MAX_BYTES = 5 * 1024 * 1024

# --- Feature gate ----------------------------------------------------------

# The connected-services pseudo-key (api/instructions.py
# PSEUDO_SERVICE_KEYS) that hides the table tools and the system skill from
# prompts while the ``tables`` gate (config/feature_gates.py
# FEATURE_TABLES) is closed for the user.
TABLES_SERVICE_KEY = "tables"


def table_not_found_message(table_id: str) -> str:
    """The ONE not-found text.

    Used verbatim for a table that does not exist AND for a table the caller
    may not see (invariant 2 in the spec: a hidden table behaves exactly
    like a nonexistent one). Never build a not-found message any other way.
    """
    return f"Table not found: {table_id}"


def tables_disabled_message() -> str:
    """Model/user-facing text when the ``tables`` feature gate is closed."""
    return (
        "Quest Tables is not enabled for this user. An admin can enable it "
        "in Settings > Features."
    )

# --- History detail (GET /tables/{id}/changes/{change_id}) -----------------

# Most row images (before/after pairs) one change detail response carries,
# and the most bytes of images it spells out; the rest is summarized.
TABLE_CHANGE_DETAIL_MAX_ROWS = 50
TABLE_CHANGE_DETAIL_MAX_BYTES = 200 * 1024
