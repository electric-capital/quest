"""Quest Tables reads: the filter DSL, sort and paging, compiled to SQL
(see docs/api/tables-api.md).

Pure SQL building plus one thin executor (:func:`run_query`). The tools
(``query_rows``, filter-based ``delete_rows``) and the
``GET /tables/{id}/rows`` route all go through here.

Grammar (JSON, validated against the schema)::

    node := {"and": [node, ...]}          non-empty list
          | {"or":  [node, ...]}          non-empty list
          | {"not": node}
          | {"column": "<name>", "op": "<op>", "value": ...}

A node is exactly one of these shapes; any other key is refused. Depth: a
bare leaf is 1 and each ``and`` / ``or`` / ``not`` adds 1, at most
``TABLE_FILTER_MAX_DEPTH``; at most ``TABLE_FILTER_MAX_LEAVES`` leaves.
Both are checked while descending, so a hostile input is refused without
being walked in full.

Ops per column type: every type takes ``eq``, ``ne``, ``in``, ``is_null``
and ``not_null``; text and url add ``contains``, ``starts_with`` and
``ends_with``; number and date add ``lt``, ``lte``, ``gt``, ``gte`` and
``between``; a multiple enum adds ``contains`` (one option),
``contains_any``, ``contains_all`` and ``is_empty``. Boolean and single
enum take only the common ops.

Operands are validated and canonicalized like writes
(:func:`chat.tables.schema.coerce_value` then ``to_storage``), so
``"10"``, ``10``, ``10.0`` and ``"1e1"`` select the same rows of a number
column. Exceptions:

- every number operand (``eq`` / ``ne`` / ``in`` and the range bounds)
  is canonicalized with the column ``precision`` removed. Changing a
  column's precision never rewrites stored cells, so ``10.05`` written
  before ``precision`` became 1 is still stored as ``"10.05"`` and must
  be matched by ``eq 10.05``; and rounding a range bound would change the
  result (``lt 10.05`` on a precision-1 column must still include 10.0).
  Equality is exact against the stored canonical string.
- text substring operands are non-empty text, NFC-normalized with CR/LF
  turned into LF like text cells; url substring operands are checked the
  same way (no NUL or unpaired surrogates) but not rewritten, since url
  cells are stored as given. Both are capped at the column type's length
  and need not be valid URLs.
- a multi-enum ``eq`` / ``ne`` operand and each ``in`` element is a list
  of at most ``TABLE_ENUM_MAX_OPTIONS`` options (``too_many_values``).
- a multi-enum ``contains`` takes one option; ``contains_any`` /
  ``contains_all`` take a non-empty list of options.
- a date operand is a ``YYYY-MM-DD`` string like a date cell; a
  ``between`` pair needs low <= high as dates.

SQL semantics:

- ``eq``: ``= ?``; a NULL cell never matches. ``in``: ``IN (?, ...)``;
  NULL cells never match.
- ``ne``: null-safe ``IS NOT ?``, so a NULL cell matches ("status is not
  done" includes rows with no status).
- ``is_null`` / ``not_null``: ``IS NULL`` / ``IS NOT NULL``; ``is_empty``
  (multi enum) is ``IS NULL`` because an empty list is stored as NULL.
- ``not``: the set complement of its child, ``NOT COALESCE((child), 0)``,
  so NULL cells are included.
- ``and`` / ``or``: parenthesized children joined with AND / OR.
- text/url substring ops: ``LIKE ? ESCAPE '\\'`` with ``\\``, ``%`` and
  ``_`` escaped in the operand, so they match literally. SQLite LIKE
  folds case for ASCII letters only (no ICU): ``"É"`` does not match
  ``"é"``.
- number ranges compare ``CAST(cell AS REAL)`` to ``CAST(? AS REAL)``
  (approximate past 15 significant digits); number ``eq`` / ``ne`` / ``in``
  compare the canonical string exactly.
- date ranges compare the stored string with ``<`` / ``BETWEEN``: the
  canonical ``YYYY-MM-DD`` form sorts as text in date order, so no cast.
- booleans bind 0 / 1.
- multi enum: cells are always compact JSON arrays (written through
  ``to_storage``), so ``eq`` compares that string, and ``contains`` /
  ``contains_any`` / ``contains_all`` look inside with ``json_each``
  (which yields nothing for NULL).

Sort: up to ``TABLE_SORT_MAX_KEYS`` keys, nulls last in both directions,
numbers through ``CAST(... AS REAL)``, text/url/enum/date by the raw stored
string (binary collation), always ending with ``_id ASC`` as a tiebreaker
so pages are stable. The row fields ``_id``, ``_created_at`` and
``_updated_at`` are sortable but not filterable.

Safety: column names are lookups only. Every identifier in the SQL comes
from a validated column key quoted by :func:`chat.tables.storage._ident`
(or is one of the fixed row fields), and every value is a bound
parameter; no user or model text is ever interpolated.

The caps come from :mod:`chat.tables.constants` imported by name; this
module's globals are the patch point for tests, read at call time.
"""

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal, Union

from chat.tables import storage
from chat.tables.constants import (
    TABLE_ENUM_MAX_OPTIONS,
    TABLE_FILTER_IN_MAX_VALUES,
    TABLE_FILTER_MAX_DEPTH,
    TABLE_FILTER_MAX_LEAVES,
    TABLE_QUERY_DEFAULT_LIMIT,
    TABLE_QUERY_MAX_OFFSET,
    TABLE_QUERY_MAX_ROWS,
    TABLE_SORT_MAX_KEYS,
    TABLE_TEXT_MAX_CHARS,
    TABLE_URL_MAX_CHARS,
)
from chat.tables.schema import (
    RESERVED_COLUMN_NAMES,
    Column,
    TableValueError,
    clip_repr,
    coerce_option,
    coerce_value,
    find_column,
    from_storage,
    to_storage,
)
from chat.tables.storage import _ident

# --- Errors ------------------------------------------------------------------


class FilterError(ValueError):
    """A filter, sort, column list or page refused.

    ``code``: ``invalid_filter``, ``unknown_column``, ``unknown_op``,
    ``op_not_allowed``, ``invalid_value``, ``too_deep``,
    ``too_many_leaves``, ``too_many_values``, ``invalid_sort``,
    ``too_many_sort_keys``, ``invalid_limit``, ``invalid_offset``,
    ``invalid_columns``. ``message``: human text (also ``str(err)``).
    ``path``: the offending node, ``""`` for the filter root, then e.g.
    ``and[1].not.value``, ``sort[2].direction``, ``columns[0]``, ``limit``.
    """

    def __init__(self, code: str, message: str, path: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def to_payload(self) -> dict:
        """The tool / route error body."""
        return {"error": self.code, "message": self.message, "path": self.path}


# --- Ops -----------------------------------------------------------------------

_COMMON_OPS = ("eq", "ne", "in", "is_null", "not_null")
_SUBSTRING_OPS = ("contains", "starts_with", "ends_with")
_RANGE_OPS = ("lt", "lte", "gt", "gte")
_MULTI_OPS = ("contains", "contains_any", "contains_all", "is_empty")
_NULLARY_OPS = ("is_null", "not_null", "is_empty")

ALL_OPS = frozenset(_COMMON_OPS + _SUBSTRING_OPS + _RANGE_OPS + ("between",) + _MULTI_OPS)

# The operand shape of each op, for the UI filter builder and route docs:
# "none" (no value), "one" (a single value; on a multiple enum the one
# value of ``eq`` / ``ne`` is itself a list of options), "list" (a
# non-empty list) or "pair" (``[low, high]``).
OP_OPERANDS: Mapping[str, Literal["none", "one", "list", "pair"]] = {
    "eq": "one",
    "ne": "one",
    "in": "list",
    "is_null": "none",
    "not_null": "none",
    "is_empty": "none",
    "contains": "one",
    "starts_with": "one",
    "ends_with": "one",
    "lt": "one",
    "lte": "one",
    "gt": "one",
    "gte": "one",
    "between": "pair",
    "contains_any": "list",
    "contains_all": "list",
}

_RANGE_SQL = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}

# How many column names an unknown_column message lists.
_NAMES_HINT_MAX = 10


def allowed_ops(column: Column) -> tuple[str, ...]:
    """The filter ops ``column``'s type accepts, in a stable order."""
    if column.type in ("text", "url"):
        return _COMMON_OPS + _SUBSTRING_OPS
    if column.type in ("number", "date"):
        return _COMMON_OPS + _RANGE_OPS + ("between",)
    if column.type == "enum" and column.multiple:
        return _COMMON_OPS + _MULTI_OPS
    return _COMMON_OPS


def _type_label(column: Column) -> str:
    if column.type == "enum":
        return "multiple enum" if column.multiple else "single enum"
    return column.type


# --- Tree ----------------------------------------------------------------------


@dataclass(frozen=True)
class Leaf:
    """One validated comparison. ``value`` is the canonical logical operand
    (a list for ``in`` / ``between`` / ``contains_any`` / ``contains_all``,
    the normalized substring for text ops, None for nullary ops), for
    echoing back. ``params`` are exactly the values :func:`to_sql` binds,
    in order (storage forms; a LIKE pattern for substring ops; the options
    then their count for ``contains_all``)."""

    column: Column
    op: str
    value: Any
    params: tuple


@dataclass(frozen=True)
class And:
    children: tuple


@dataclass(frozen=True)
class Or:
    children: tuple


@dataclass(frozen=True)
class Not:
    child: Any


FilterNode = Union[Leaf, And, Or, Not]


# --- Column lookup -------------------------------------------------------------


def _names_hint(schema: Sequence[Column]) -> str:
    names = [clip_repr(c.name) for c in schema[:_NAMES_HINT_MAX]]
    hint = ", ".join(names)
    if len(schema) > _NAMES_HINT_MAX:
        hint += f" and {len(schema) - _NAMES_HINT_MAX} more"
    return hint


def _match_column(schema: Sequence[Column], name: str) -> Column | None:
    """Exact name match, else the one case-insensitive match (names are
    unique case-insensitively)."""
    column = find_column(schema, name)
    if column is not None:
        return column
    folded = name.casefold()
    for candidate in schema:
        if candidate.name.casefold() == folded:
            return candidate
    return None


def _reserved_name(name: str) -> str | None:
    folded = name.casefold()
    for reserved in RESERVED_COLUMN_NAMES:
        if reserved == folded:
            return reserved
    return None


def _lookup(schema: Sequence[Column], name: str, path: str, what: str) -> Column:
    if _reserved_name(name) is not None:
        raise FilterError(
            "unknown_column",
            f"{clip_repr(name)} is a row field and cannot be {what}.",
            path,
        )
    column = _match_column(schema, name)
    if column is None:
        raise FilterError(
            "unknown_column",
            f"No column named {clip_repr(name)} (columns: {_names_hint(schema)}).",
            path,
        )
    return column


# --- Filter parsing ------------------------------------------------------------


def _join(path: str, part: str) -> str:
    return f"{path}.{part}" if path else part


def _value_error(path: str, exc: TableValueError, hint: str = "") -> FilterError:
    return FilterError("invalid_value", exc.message + hint, path)


def _canonical(column: Column, value: Any, path: str) -> tuple[Any, Any]:
    """(logical, stored) for an eq/ne/in operand, refusing null. Numbers
    are canonicalized without the column precision (module docstring)."""
    if (
        column.type == "enum"
        and column.multiple
        and isinstance(value, (list, tuple))
        and len(value) > TABLE_ENUM_MAX_OPTIONS
    ):
        raise FilterError(
            "too_many_values",
            f"Column {clip_repr(column.name)}: at most {TABLE_ENUM_MAX_OPTIONS} "
            f"options in one value (got {len(value)}).",
            path,
        )
    if column.type == "number":
        column = replace(column, precision=None, default=None)
    try:
        logical = coerce_value(column, value)
    except TableValueError as exc:
        raise _value_error(path, exc) from None
    if logical is None:
        if value is None:
            raise FilterError(
                "invalid_value",
                f"Column {clip_repr(column.name)}: a null value cannot be compared; "
                "use the is_null or not_null op.",
                path,
            )
        # Only an empty multi-enum list coerces to None.
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: an empty list cannot be compared; "
            "use the is_empty op.",
            path,
        )
    return logical, to_storage(column, logical)


def _range_bound(column: Column, value: Any, path: str) -> str:
    """A number or date range bound; a number is canonicalized without the
    column precision."""
    if value is None:
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: a range bound cannot be null.",
            path,
        )
    try:
        return coerce_value(replace(column, precision=None, default=None), value)
    except TableValueError as exc:
        raise _value_error(path, exc) from None


def _substring(column: Column, value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: expected a non-empty string.",
            path,
        )
    cap = TABLE_URL_MAX_CHARS if column.type == "url" else TABLE_TEXT_MAX_CHARS
    if column.type == "url":
        # Url cells are stored as given: check, never rewrite.
        if len(value) > cap:
            raise FilterError(
                "invalid_value",
                f"Column {clip_repr(column.name)}: text is longer than {cap} characters.",
                path,
            )
        if "\x00" in value or _SURROGATE_RE.search(value):
            raise FilterError(
                "invalid_value",
                f"Column {clip_repr(column.name)}: text contains a NUL character or "
                "an unpaired surrogate.",
                path,
            )
        return value
    as_text = replace(column, type="text", precision=None, default=None)
    try:
        text = coerce_value(as_text, value)
    except TableValueError as exc:
        raise _value_error(path, exc) from None
    if len(text) > cap:
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: text is longer than {cap} characters.",
            path,
        )
    return text


def _like_pattern(op: str, text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if op == "contains":
        return f"%{escaped}%"
    if op == "starts_with":
        return f"{escaped}%"
    return f"%{escaped}"


def _value_list(column: Column, value: Any, path: str) -> list:
    if not isinstance(value, (list, tuple)) or not value:
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: expected a non-empty list.",
            path,
        )
    if len(value) > TABLE_FILTER_IN_MAX_VALUES:
        raise FilterError(
            "too_many_values",
            f"Column {clip_repr(column.name)}: at most {TABLE_FILTER_IN_MAX_VALUES} "
            f"values in one list (got {len(value)}).",
            path,
        )
    return list(value)


def _parse_leaf(schema: Sequence[Column], node: dict, path: str) -> Leaf:
    name = node.get("column")
    op = node.get("op")
    if not isinstance(name, str):
        raise FilterError(
            "invalid_filter", "A filter leaf needs a column name string.", _join(path, "column")
        )
    if not isinstance(op, str):
        raise FilterError(
            "invalid_filter", "A filter leaf needs an op string.", _join(path, "op")
        )
    column = _lookup(schema, name, _join(path, "column"), "filtered")
    ops = allowed_ops(column)
    if op not in ops:
        listed = ", ".join(ops)
        if op in ALL_OPS:
            raise FilterError(
                "op_not_allowed",
                f"Op {clip_repr(op)} is not allowed on {_type_label(column)} column "
                f"{clip_repr(column.name)} (allowed: {listed}).",
                _join(path, "op"),
            )
        raise FilterError(
            "unknown_op",
            f"Unknown op {clip_repr(op)} (allowed on {_type_label(column)} column "
            f"{clip_repr(column.name)}: {listed}).",
            _join(path, "op"),
        )

    vpath = _join(path, "value")
    has_value = "value" in node
    value = node.get("value")
    if op in _NULLARY_OPS:
        if value is not None:
            raise FilterError(
                "invalid_value", f"Op {op!r} takes no value.", vpath
            )
        return Leaf(column, op, None, ())
    if not has_value:
        raise FilterError("invalid_value", f"Op {op!r} needs a value.", vpath)

    multi = column.type == "enum" and column.multiple
    if op in ("eq", "ne"):
        logical, stored = _canonical(column, value, vpath)
        return Leaf(column, op, logical, (stored,))
    if op == "in":
        items = _value_list(column, value, vpath)
        logicals: list = []
        stored_seen: list = []
        for index, item in enumerate(items):
            logical, stored = _canonical(column, item, f"{vpath}[{index}]")
            if stored not in stored_seen:
                stored_seen.append(stored)
                logicals.append(logical)
        return Leaf(column, op, logicals, tuple(stored_seen))
    if multi:
        if op == "contains":
            try:
                option = coerce_option(column, value)
            except TableValueError as exc:
                raise _value_error(vpath, exc) from None
            return Leaf(column, op, option, (option,))
        items = _value_list(column, value, vpath)
        options: list[str] = []
        for index, item in enumerate(items):
            try:
                option = coerce_option(column, item)
            except TableValueError as exc:
                raise _value_error(f"{vpath}[{index}]", exc) from None
            if option not in options:
                options.append(option)
        params = tuple(options)
        if op == "contains_all":
            params += (len(options),)
        return Leaf(column, op, options, params)
    if op in _SUBSTRING_OPS:
        text = _substring(column, value, vpath)
        return Leaf(column, op, text, (_like_pattern(op, text),))
    if op in _RANGE_OPS:
        bound = _range_bound(column, value, vpath)
        return Leaf(column, op, bound, (bound,))
    # between
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: between takes a [low, high] list.",
            vpath,
        )
    lo = _range_bound(column, value[0], f"{vpath}[0]")
    hi = _range_bound(column, value[1], f"{vpath}[1]")
    # Canonical dates compare as text; numbers as decimals.
    out_of_order = lo > hi if column.type == "date" else Decimal(lo) > Decimal(hi)
    if out_of_order:
        raise FilterError(
            "invalid_value",
            f"Column {clip_repr(column.name)}: between needs low <= high "
            f"(got {lo} > {hi}).",
            vpath,
        )
    return Leaf(column, op, [lo, hi], (lo, hi))


_SURROGATE_RE = re.compile("[\ud800-\udfff]")

_LEAF_KEYS = frozenset(("column", "op", "value"))


class _Counter:
    def __init__(self) -> None:
        self.leaves = 0


def _parse_node(
    schema: Sequence[Column], node: Any, path: str, depth: int, counter: _Counter
) -> FilterNode:
    if depth > TABLE_FILTER_MAX_DEPTH:
        raise FilterError(
            "too_deep",
            f"The filter is nested more than {TABLE_FILTER_MAX_DEPTH} levels deep.",
            path,
        )
    if not isinstance(node, dict):
        raise FilterError("invalid_filter", "A filter node must be an object.", path)
    keys = set(node)
    if len(keys) == 1 and keys <= {"and", "or", "not"}:
        (shape,) = keys
        if shape == "not":
            child = _parse_node(schema, node["not"], _join(path, "not"), depth + 1, counter)
            return Not(child)
        items = node[shape]
        if not isinstance(items, list) or not items:
            raise FilterError(
                "invalid_filter",
                f"{shape!r} takes a non-empty list of filter nodes.",
                _join(path, shape),
            )
        # Every child holds at least one leaf: refuse a huge list unwalked.
        if counter.leaves + len(items) > TABLE_FILTER_MAX_LEAVES:
            raise FilterError(
                "too_many_leaves",
                f"A filter can have at most {TABLE_FILTER_MAX_LEAVES} conditions.",
                _join(path, shape),
            )
        children = tuple(
            _parse_node(schema, item, f"{_join(path, shape)}[{index}]", depth + 1, counter)
            for index, item in enumerate(items)
        )
        return And(children) if shape == "and" else Or(children)
    if keys & {"and", "or", "not"} or not keys <= _LEAF_KEYS:
        extra = sorted(
            (str(k) for k in keys - _LEAF_KEYS if k not in ("and", "or", "not")),
        )
        detail = (
            f"unknown key {clip_repr(extra[0])}" if extra else "mixed node shapes"
        )
        raise FilterError(
            "invalid_filter",
            f"A filter node is exactly one of and, or, not or a "
            f"{{column, op, value}} leaf ({detail}).",
            path,
        )
    counter.leaves += 1
    if counter.leaves > TABLE_FILTER_MAX_LEAVES:
        raise FilterError(
            "too_many_leaves",
            f"A filter can have at most {TABLE_FILTER_MAX_LEAVES} conditions.",
            path,
        )
    return _parse_leaf(schema, node, path)


def parse_filter(schema: Sequence[Column], node: Any) -> FilterNode | None:
    """Validate a filter against ``schema`` into a tree; None means no
    filter. Raises :class:`FilterError`."""
    if node is None:
        return None
    return _parse_node(schema, node, "", 1, _Counter())


# --- SQL -----------------------------------------------------------------------


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _leaf_sql(leaf: Leaf) -> str:
    column, op = leaf.column, leaf.op
    cell = _ident(column.key)
    if op == "eq":
        return f"{cell} = ?"
    if op == "ne":
        return f"{cell} IS NOT ?"
    if op == "in":
        return f"{cell} IN ({_placeholders(len(leaf.params))})"
    if op in ("is_null", "is_empty"):
        return f"{cell} IS NULL"
    if op == "not_null":
        return f"{cell} IS NOT NULL"
    if column.type == "enum":
        each = f"json_each({cell})"
        if op == "contains":
            return f"EXISTS (SELECT 1 FROM {each} WHERE json_each.value = ?)"
        if op == "contains_any":
            return (
                f"EXISTS (SELECT 1 FROM {each} WHERE json_each.value IN "
                f"({_placeholders(len(leaf.params))}))"
            )
        if op == "contains_all":
            return (
                f"(SELECT COUNT(DISTINCT json_each.value) FROM {each} WHERE "
                f"json_each.value IN ({_placeholders(len(leaf.params) - 1)})) = ?"
            )
    if op in _SUBSTRING_OPS:
        return f"{cell} LIKE ? ESCAPE '\\'"
    if column.type == "date":
        if op in _RANGE_SQL:
            return f"{cell} {_RANGE_SQL[op]} ?"
        if op == "between":
            return f"{cell} BETWEEN ? AND ?"
    if op in _RANGE_SQL:
        return f"CAST({cell} AS REAL) {_RANGE_SQL[op]} CAST(? AS REAL)"
    if op == "between":
        return f"CAST({cell} AS REAL) BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)"
    raise ValueError(f"Unhandled filter op {op!r}")  # parse_filter refuses it


def _node_sql(node: FilterNode, params: list) -> str:
    if isinstance(node, Leaf):
        params.extend(node.params)
        return _leaf_sql(node)
    if isinstance(node, Not):
        return f"NOT COALESCE(({_node_sql(node.child, params)}), 0)"
    joiner = " AND " if isinstance(node, And) else " OR "
    return joiner.join(f"({_node_sql(child, params)})" for child in node.children)


def to_sql(tree: FilterNode | None) -> tuple[str, list]:
    """A parsed filter as ``(where_sql, params)``, the shape
    ``storage.delete_rows(where=...)`` takes; ``("1", [])`` for no filter."""
    if tree is None:
        return "1", []
    params: list = []
    sql = _node_sql(tree, params)
    return sql, params


def compile_filter(schema: Sequence[Column], node: Any) -> tuple[str, list]:
    """:func:`parse_filter` then :func:`to_sql`, for a filter-based delete.
    A missing filter is refused (``invalid_filter``) rather than compiled
    to ``"1"``, so a delete can never select every row by omission; reads
    use :func:`parse_filter` / :func:`to_sql`, where None means no filter."""
    if node is None:
        raise FilterError("invalid_filter", "A filter is required.", "")
    return to_sql(parse_filter(schema, node))


# --- Sort ----------------------------------------------------------------------

# Row fields: name -> SQL expression (fixed text, never user input).
_ROW_FIELD_SQL = {"_id": "_id", "_created_at": '"_created_at"', "_updated_at": '"_updated_at"'}


@dataclass(frozen=True)
class SortKey:
    """One validated sort key. ``name`` is the column name (or the row
    field ``_id`` / ``_created_at`` / ``_updated_at``) for echoing back;
    ``key`` the column key (or the row field); ``type`` the column type, or
    ``"row_field"``."""

    name: str
    key: str
    type: str
    direction: Literal["asc", "desc"]


_SORT_KEYS = frozenset(("column", "direction"))


def parse_sort(schema: Sequence[Column], sort: Any) -> tuple[SortKey, ...]:
    """Validate ``[{column, direction?}]`` (direction ``asc`` by default);
    None or ``[]`` is no sort. Raises :class:`FilterError`."""
    if sort is None:
        return ()
    if not isinstance(sort, list):
        raise FilterError("invalid_sort", "sort must be a list of {column, direction}.", "sort")
    if len(sort) > TABLE_SORT_MAX_KEYS:
        raise FilterError(
            "too_many_sort_keys",
            f"At most {TABLE_SORT_MAX_KEYS} sort keys (got {len(sort)}).",
            "sort",
        )
    keys: list[SortKey] = []
    for index, entry in enumerate(sort):
        path = f"sort[{index}]"
        if not isinstance(entry, dict):
            raise FilterError("invalid_sort", "A sort key must be an object.", path)
        extra = [k for k in entry if k not in _SORT_KEYS]
        if extra:
            raise FilterError(
                "invalid_sort", f"Unknown sort field {clip_repr(extra[0])}.", path
            )
        name = entry.get("column")
        if not isinstance(name, str):
            raise FilterError(
                "invalid_sort", "A sort key needs a column name string.", f"{path}.column"
            )
        direction = entry.get("direction")
        if direction is None:
            direction = "asc"
        if direction not in ("asc", "desc"):
            raise FilterError(
                "invalid_sort",
                f"Sort direction must be 'asc' or 'desc', not {clip_repr(direction)}.",
                f"{path}.direction",
            )
        reserved = _reserved_name(name)
        if reserved is not None:
            key = SortKey(reserved, reserved, "row_field", direction)
        else:
            column = _lookup(schema, name, f"{path}.column", "sorted")
            key = SortKey(column.name, column.key, column.type, direction)
        if any(k.key == key.key for k in keys):
            raise FilterError(
                "invalid_sort", f"Column {clip_repr(key.name)} is sorted twice.", path
            )
        keys.append(key)
    return tuple(keys)


def _sort_expr(key: SortKey) -> str:
    if key.type == "row_field":
        return _ROW_FIELD_SQL[key.key]
    cell = _ident(key.key)
    if key.type == "number":
        return f"CAST({cell} AS REAL)"
    return cell


def to_order_by(keys: Sequence[SortKey]) -> str:
    """The text after ``ORDER BY``: nulls last in both directions, always
    ending with ``_id ASC`` unless ``_id`` is already a key."""
    parts = []
    for key in keys:
        expr = _sort_expr(key)
        parts.append(f"({expr} IS NULL), {expr} {key.direction.upper()}")
    if not any(k.key == "_id" for k in keys):
        parts.append("_id ASC")
    return ", ".join(parts)


# --- Columns and paging ------------------------------------------------------


def resolve_columns(
    schema: Sequence[Column], names: Sequence[str] | None
) -> tuple[Column, ...]:
    """The output columns: every schema column for None, else the named
    ones in the order given, duplicates collapsed. The row fields ``_id``,
    ``_created_at`` and ``_updated_at`` are accepted and skipped (``_id``
    and ``_updated_at`` are in every output row anyway)."""
    if names is None:
        return tuple(schema)
    if not isinstance(names, list) or not names:
        raise FilterError(
            "invalid_columns", "columns must be a non-empty list of column names.", "columns"
        )
    if len(names) > len(schema) + len(RESERVED_COLUMN_NAMES):
        raise FilterError(
            "invalid_columns",
            f"columns lists {len(names)} names but the table has {len(schema)} columns.",
            "columns",
        )
    out: list[Column] = []
    for index, name in enumerate(names):
        path = f"columns[{index}]"
        if not isinstance(name, str):
            raise FilterError("invalid_columns", "Every column name must be a string.", path)
        if _reserved_name(name) is not None:
            continue
        column = _lookup(schema, name, path, "selected")
        if column not in out:
            out.append(column)
    return tuple(out)


def check_page(
    limit: Any, offset: Any, *, max_limit: int = TABLE_QUERY_MAX_ROWS
) -> tuple[int, int]:
    """Validate ``limit`` (1..``max_limit``, default
    ``TABLE_QUERY_DEFAULT_LIMIT`` clamped to ``max_limit``) and ``offset``
    (0..``TABLE_QUERY_MAX_OFFSET``, default 0)."""
    if limit is None:
        limit = min(TABLE_QUERY_DEFAULT_LIMIT, max_limit)
    elif not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_limit:
        raise FilterError(
            "invalid_limit", f"limit must be a whole number from 1 to {max_limit}.", "limit"
        )
    if offset is None:
        offset = 0
    elif (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 0 <= offset <= TABLE_QUERY_MAX_OFFSET
    ):
        raise FilterError(
            "invalid_offset",
            f"offset must be a whole number from 0 to {TABLE_QUERY_MAX_OFFSET}.",
            "offset",
        )
    return limit, offset


def build_count(where: tuple[str, list]) -> tuple[str, list]:
    """``SELECT COUNT(*)`` over the rows ``where`` selects."""
    where_sql, params = where
    return f"SELECT COUNT(*) FROM rows WHERE ({where_sql})", list(params)


def build_select(
    columns: Sequence[Column],
    where: tuple[str, list],
    order_by: str,
    *,
    limit: int,
    offset: int,
) -> tuple[str, list]:
    """One page of rows: ``_id``, ``_updated_at`` then ``columns``; params
    are the where params then limit and offset.

    ``where[0]`` and ``order_by`` are interpolated, so they must be trusted
    SQL from :func:`to_sql` and :func:`to_order_by` (built from validated
    column keys, every value bound); never user or model text."""
    where_sql, params = where
    fields = ["_id", "_updated_at"] + [_ident(c.key) for c in columns]
    sql = (
        f"SELECT {', '.join(fields)} FROM rows WHERE ({where_sql}) "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?"
    )
    return sql, [*params, limit, offset]


# --- Executor ------------------------------------------------------------------


@dataclass(frozen=True)
class QueryResult:
    """``rows``: ``{"_id", "_updated_at", <name>: logical value}`` in the
    requested column order. ``total``: the filtered count, ignoring paging.
    ``limit`` / ``offset``: the page actually used (defaults applied)."""

    rows: list[dict]
    total: int
    limit: int
    offset: int


def run_query(
    table_dir: Path,
    schema: Sequence[Column],
    *,
    filter: Any = None,  # noqa: A002 - the DSL's own name
    sort: Any = None,
    limit: Any = None,
    offset: Any = None,
    columns: Sequence[str] | None = None,
    count_only: bool = False,
    max_limit: int = TABLE_QUERY_MAX_ROWS,
    schema_version: int | None = None,
) -> QueryResult:
    """Validate the request (raising :class:`FilterError` before any I/O),
    then count and select in one read transaction so ``total`` and
    ``rows`` agree. A schema column missing from table.db, or a stored
    schema version other than ``schema_version`` when given, is
    :class:`chat.tables.storage.SchemaMismatchError`; a database locked
    past the busy timeout is ``TableStorageError`` ``busy``. Synchronous."""
    where = to_sql(parse_filter(schema, filter))
    order_by = to_order_by(parse_sort(schema, sort))
    selected = resolve_columns(schema, columns)
    page_limit, page_offset = check_page(limit, offset, max_limit=max_limit)

    with storage.connect(table_dir) as conn:
        with storage._busy_as_refusal():
            conn.execute("BEGIN")
            try:
                if schema_version is not None:
                    storage._check_schema_matches(conn, schema, schema_version)
                sql, params = build_count(where)
                total = storage._select_rows(conn, sql, params).fetchone()[0]
                rows: list[dict] = []
                if not count_only:
                    sql, params = build_select(
                        selected, where, order_by, limit=page_limit, offset=page_offset
                    )
                    for record in storage._select_rows(conn, sql, params).fetchall():
                        item = {"_id": record[0], "_updated_at": record[1]}
                        for column, stored in zip(selected, record[2:]):
                            item[column.name] = from_storage(column, stored)
                        rows.append(item)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    with suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                raise
    return QueryResult(rows=rows, total=total, limit=page_limit, offset=page_offset)
