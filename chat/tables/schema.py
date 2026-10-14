"""Quest Tables columns and cell values (see docs/architecture/quest-tables.md).

Pure functions over the column model: parsing and validating column specs,
loading and dumping the stored schema JSON, and canonicalizing cell values.
No SQLite, no I/O; the store, the tools, the action requests and the routes
all call into this module so every value is checked by one set of rules.

Canonical logical values (what :func:`canonicalize` returns, what column
defaults hold and what models see):

- ``text``: the string, NFC-normalized, CRLF and lone CR turned into LF,
  at most ``TABLE_TEXT_MAX_CHARS`` code points after normalization; no
  NUL and no unpaired surrogates (neither can be stored as UTF-8 text).
- ``number``: a decimal string with no exponent, no leading ``+``, no
  trailing fractional zeros, every zero as ``"0"``; at most
  ``TABLE_NUMBER_MAX_DIGITS`` rendered digits (a lone leading ``0`` of a
  value below 1 does not count), rounded half-even to the column
  ``precision`` when set (a value that rounds to zero is ``"0"``).
- ``url``: the stripped ``http``/``https`` string exactly as given; no
  whitespace, control characters, backslashes or unpaired surrogates.
- ``boolean``: ``True`` / ``False`` (stored as 1 / 0).
- ``enum``: the option string; a ``multiple`` enum holds a deduplicated
  list sorted by option order (stored as compact JSON), an empty list
  being no value. A :class:`Column` holds a multi-enum default as a tuple
  (so columns stay immutable and hashable); every value this module hands
  out (:func:`coerce_value`, :func:`canonicalize`, :func:`apply_default`,
  :meth:`Column.to_dict`) is a fresh list.

:class:`TableValueError` codes: ``wrong_type``, ``too_long``,
``invalid_text``, ``invalid_number``, ``too_many_digits``,
``invalid_url``, ``unknown_option``, ``required``.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
import json
import math
import re
import secrets
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from chat.tables.constants import (
    TABLE_COLUMN_DESCRIPTION_MAX_LEN,
    TABLE_COLUMN_KEY_HEX_LEN,
    TABLE_COLUMN_KEY_PREFIX,
    TABLE_COLUMN_NAME_MAX_LEN,
    TABLE_COLUMN_TYPES,
    TABLE_ENUM_MAX_OPTIONS,
    TABLE_ENUM_OPTION_MAX_LEN,
    TABLE_MAX_COLUMNS,
    TABLE_NUMBER_MAX_DIGITS,
    TABLE_NUMBER_MAX_PRECISION,
    TABLE_TEXT_MAX_CHARS,
    TABLE_TITLE_MAX_LEN,
    TABLE_UNIQUE_COLUMN_TYPES,
    TABLE_URL_MAX_CHARS,
)

# --- Errors ------------------------------------------------------------------


class TableSchemaError(ValueError):
    """Base for schema-level refusals.

    Attributes: ``code`` (machine-readable), ``message`` (human text, also
    ``str(err)``), ``column`` (the column name when known) and ``field``
    (the spec or operation field at fault, when known).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        column: str | None = None,
        field: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.column = column
        self.field = field


class ColumnSpecError(TableSchemaError):
    """A column spec or stored column refused by :func:`parse_column_spec`,
    :func:`parse_columns`, :meth:`Column.from_dict` or :func:`load_schema`."""


class TableValueError(ValueError):
    """A cell value refused by :func:`canonicalize` / :func:`coerce_value`.

    Attributes: ``code`` (one of the codes in the module docstring),
    ``message`` (human text prefixed with the column, also ``str(err)``),
    ``detail`` (the same reason without the column prefix, for an
    invalid_rows problem whose ``column`` is reported separately) and
    ``column`` (the column name).
    """

    def __init__(
        self, code: str, message: str, *, column: str, detail: str | None = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = message if detail is None else detail
        self.column = column


# Longest stretch of a user-supplied name, option or field echoed in an
# error message; a longer one is cut and marked with an ellipsis.
_ECHO_MAX_CHARS = 100


def field_label(value: object) -> str:
    """A user-supplied field name as text: a string unchanged, anything
    else through ``str`` (with a placeholder when even that fails, as for
    an int too long to print)."""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - any odd object must still be labelled
        return f"<{type(value).__name__}>"


def clip_repr(value: object) -> str:
    """``repr`` of a user-supplied string for an error message, cut to
    100 characters with a trailing ellipsis when longer (non-strings go
    through :func:`field_label` first)."""
    text = field_label(value)
    if len(text) > _ECHO_MAX_CHARS:
        return repr(text[:_ECHO_MAX_CHARS]) + "\u2026"
    return repr(text)


# --- Column ------------------------------------------------------------------

_SPEC_FIELDS = (
    "key",
    "name",
    "type",
    "required",
    "unique",
    "default",
    "options",
    "multiple",
    "precision",
    "description",
)

_KEY_RE = re.compile(
    re.escape(TABLE_COLUMN_KEY_PREFIX) + "[0-9a-f]{%d}" % TABLE_COLUMN_KEY_HEX_LEN,
    re.ASCII,
)


@dataclass(frozen=True)
class Column:
    """One schema column. ``key`` is immutable; ``name`` is what people and
    models use. ``default`` is a canonical logical value or None, except
    that a multi-enum default is held as a tuple; ``options`` is a tuple.
    Both are normalized on construction (lists are copied into tuples), so
    a column is immutable and hashable however it was built."""

    key: str
    name: str
    type: str
    description: str = ""
    required: bool = False
    unique: bool = False
    default: Any = None
    options: tuple[str, ...] | None = None
    multiple: bool = False
    precision: int | None = None

    def __post_init__(self) -> None:
        if self.options is not None and not isinstance(self.options, tuple):
            object.__setattr__(self, "options", tuple(self.options))
        if isinstance(self.default, list):
            object.__setattr__(self, "default", tuple(self.default))

    def to_dict(self) -> dict:
        """JSON-ready dict with every field, in the stored key order."""
        default = list(self.default) if isinstance(self.default, tuple) else self.default
        return {
            "key": self.key,
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "unique": self.unique,
            "default": default,
            "options": list(self.options) if self.options is not None else None,
            "multiple": self.multiple,
            "precision": self.precision,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "Column":
        """Load a stored column, requiring every field and re-validating all
        of them (stored JSON is not trusted). Raises :class:`ColumnSpecError`."""
        if not isinstance(data, Mapping):
            raise ColumnSpecError("invalid_spec", "A stored column must be an object.")
        name = data.get("name") if isinstance(data.get("name"), str) else None
        unknown = [f for f in data if f not in _SPEC_FIELDS]
        if unknown:
            raise ColumnSpecError(
                "unknown_field",
                f"Unknown column field: {clip_repr(unknown[0])}.",
                column=name,
                field=field_label(unknown[0]),
            )
        for field in _SPEC_FIELDS:
            if field not in data:
                raise ColumnSpecError(
                    "missing_field",
                    f"Stored column is missing the {field!r} field.",
                    column=name,
                    field=field,
                )
        for field in ("required", "unique", "multiple"):
            if not isinstance(data[field], bool):
                raise ColumnSpecError(
                    "invalid_field",
                    f"Column field {field!r} must be true or false.",
                    column=name,
                    field=field,
                )
        if not isinstance(data["description"], str):
            raise ColumnSpecError(
                "invalid_description",
                "Column description must be a string.",
                column=name,
                field="description",
            )
        if data["key"] is None:
            raise ColumnSpecError(
                "invalid_key", "Stored column has no key.", column=name, field="key"
            )
        return _build_column(data, existing=())


def is_valid_column_key(key: object) -> bool:
    """True for ``c_`` followed by exactly 8 lowercase hex digits."""
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None


def new_column_key(existing_keys: Collection[str]) -> str:
    """A fresh random column key not in ``existing_keys``."""
    while True:
        key = TABLE_COLUMN_KEY_PREFIX + secrets.token_hex(TABLE_COLUMN_KEY_HEX_LEN // 2)
        if key not in existing_keys:
            return key


def validate_column_name(name: object) -> str:
    """Check a column name's shape (string, 1..80 chars, no leading or
    trailing whitespace, no control characters or unpaired surrogates) and
    return it unchanged. Uniqueness is the caller's job (:func:`name_taken`)."""
    if not isinstance(name, str):
        raise ColumnSpecError(
            "invalid_name", "Column name must be a string.", field="name"
        )
    if not name or not name.strip():
        raise ColumnSpecError(
            "invalid_name", "Column name must not be empty.", column=name, field="name"
        )
    if len(name) > TABLE_COLUMN_NAME_MAX_LEN:
        raise ColumnSpecError(
            "invalid_name",
            f"Column name {clip_repr(name)} is longer than "
            f"{TABLE_COLUMN_NAME_MAX_LEN} characters.",
            column=name,
            field="name",
        )
    if name != name.strip():
        raise ColumnSpecError(
            "invalid_name",
            f"Column name {clip_repr(name)} must not start or end with whitespace.",
            column=name,
            field="name",
        )
    problem = _text_problem(name, controls=False)
    if problem:
        raise ColumnSpecError(
            "invalid_name",
            f"Column name {clip_repr(name)} {problem}.",
            column=name,
            field="name",
        )
    return name


def validate_column_description(description: object, name: str | None) -> str:
    """Check a column description (None or a string of at most
    ``TABLE_COLUMN_DESCRIPTION_MAX_LEN`` characters after stripping; line
    breaks and tabs allowed, NUL and unpaired surrogates not) and return it
    stripped, None as ``""``. ``name`` labels the error."""
    if description is None:
        return ""
    label = (
        f"Column {clip_repr(name)}: description" if name is not None
        else "Column description"
    )
    if not isinstance(description, str):
        raise ColumnSpecError(
            "invalid_description",
            f"{label} must be a string.",
            column=name,
            field="description",
        )
    description = description.strip()
    if len(description) > TABLE_COLUMN_DESCRIPTION_MAX_LEN:
        raise ColumnSpecError(
            "invalid_description",
            f"{label} is longer than {TABLE_COLUMN_DESCRIPTION_MAX_LEN} characters.",
            column=name,
            field="description",
        )
    problem = _text_problem(description, controls=True)
    if problem:
        raise ColumnSpecError(
            "invalid_description",
            f"{label} {problem}.",
            column=name,
            field="description",
        )
    return description


def validate_table_title(title: object) -> str:
    """Check a table title (a string of 1..``TABLE_TITLE_MAX_LEN``
    characters after stripping, no control characters, line breaks
    included, and no unpaired surrogates) and return it stripped. Raises
    :class:`TableSchemaError` with code ``invalid_title``."""
    if not isinstance(title, str):
        raise TableSchemaError(
            "invalid_title", "The table title must be a string.", field="title"
        )
    stripped = title.strip()
    if not stripped or len(stripped) > TABLE_TITLE_MAX_LEN:
        raise TableSchemaError(
            "invalid_title",
            f"The table title must be 1 to {TABLE_TITLE_MAX_LEN} characters.",
            field="title",
        )
    problem = _text_problem(stripped, controls=False)
    if problem:
        raise TableSchemaError(
            "invalid_title", f"The table title {problem}.", field="title"
        )
    return stripped


def name_taken(
    columns: Sequence[Column], name: str, *, except_key: str | None = None
) -> bool:
    """True when another column (not ``except_key``) already uses ``name``,
    compared case-insensitively."""
    folded = name.casefold()
    return any(
        c.name.casefold() == folded for c in columns if c.key != except_key
    )


def find_column(columns: Sequence[Column], name: str) -> Column | None:
    """The column named exactly ``name`` (case-sensitive), or None."""
    for column in columns:
        if column.name == name:
            return column
    return None


_SURROGATE_RE = re.compile("[\ud800-\udfff]")
# Unicode category Cc is exactly C0, DEL and C1.
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def _text_problem(text: str, *, controls: bool) -> str | None:
    """Why ``text`` cannot be stored, or None. Unpaired surrogates never can
    (they do not encode as UTF-8). ``controls`` True allows control
    characters except NUL (free text); False refuses every Cc character
    (names and options)."""
    if _SURROGATE_RE.search(text):
        return "contains an unpaired surrogate (not valid Unicode text)"
    if controls:
        if "\x00" in text:
            return "contains a NUL character"
    elif _CONTROL_RE.search(text):
        return "contains a control character"
    return None


def _spec_error(code: str, message: str, name: str | None, field: str | None = None):
    return ColumnSpecError(code, message, column=name, field=field)


def _check_options(raw: object, name: str) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise _spec_error(
            "invalid_options",
            f"Column {clip_repr(name)}: options must be a list of strings.",
            name,
            "options",
        )
    if not 1 <= len(raw) <= TABLE_ENUM_MAX_OPTIONS:
        raise _spec_error(
            "invalid_options",
            f"Column {clip_repr(name)}: an enum needs 1 to {TABLE_ENUM_MAX_OPTIONS} options "
            f"(got {len(raw)}).",
            name,
            "options",
        )
    seen: set[str] = set()
    for option in raw:
        if not isinstance(option, str):
            raise _spec_error(
                "invalid_option",
                f"Column {clip_repr(name)}: every option must be a string.",
                name,
                "options",
            )
        if not 1 <= len(option) <= TABLE_ENUM_OPTION_MAX_LEN:
            raise _spec_error(
                "invalid_option",
                f"Column {clip_repr(name)}: options must be 1 to {TABLE_ENUM_OPTION_MAX_LEN} "
                "characters.",
                name,
                "options",
            )
        if option != option.strip():
            raise _spec_error(
                "invalid_option",
                f"Column {clip_repr(name)}: option {clip_repr(option)} must not start or end with "
                "whitespace.",
                name,
                "options",
            )
        problem = _text_problem(option, controls=False)
        if problem:
            raise _spec_error(
                "invalid_option",
                f"Column {clip_repr(name)}: option {clip_repr(option)} {problem}.",
                name,
                "options",
            )
        folded = option.casefold()
        if folded in seen:
            raise _spec_error(
                "duplicate_option",
                f"Column {clip_repr(name)}: option {clip_repr(option)} is listed twice "
                "(options are compared case-insensitively).",
                name,
                "options",
            )
        seen.add(folded)
    return tuple(raw)


def _build_column(spec: Mapping, *, existing: Sequence[Column]) -> Column:
    """Validate a spec whose unknown fields were already refused. Shared by
    :func:`parse_column_spec` (absent fields take defaults) and
    :meth:`Column.from_dict` (every field present)."""
    if "name" not in spec:
        raise _spec_error("missing_field", "Column spec needs a name.", None, "name")
    name = validate_column_name(spec["name"])
    if name_taken(existing, name):
        raise _spec_error(
            "duplicate_name",
            f"A column named {clip_repr(name)} already exists (names are compared "
            "case-insensitively).",
            name,
            "name",
        )

    if "type" not in spec:
        raise _spec_error(
            "missing_field", f"Column {clip_repr(name)} needs a type.", name, "type"
        )
    col_type = spec["type"]
    if not isinstance(col_type, str) or col_type not in TABLE_COLUMN_TYPES:
        raise _spec_error(
            "invalid_type",
            f"Column {clip_repr(name)}: type must be one of {', '.join(TABLE_COLUMN_TYPES)}.",
            name,
            "type",
        )

    flags = {}
    for field in ("required", "unique", "multiple"):
        value = spec.get(field)
        if value is None:
            value = False
        if not isinstance(value, bool):
            raise _spec_error(
                "invalid_field",
                f"Column {clip_repr(name)}: {field} must be true or false.",
                name,
                field,
            )
        flags[field] = value

    description = validate_column_description(spec.get("description"), name)

    raw_options = spec.get("options")
    if col_type == "enum":
        if raw_options is None:
            raise _spec_error(
                "missing_field",
                f"Column {clip_repr(name)}: an enum column needs options.",
                name,
                "options",
            )
        options = _check_options(raw_options, name)
    else:
        if raw_options is not None:
            raise _spec_error(
                "field_not_allowed",
                f"Column {clip_repr(name)}: options are only allowed on enum columns.",
                name,
                "options",
            )
        options = None
        if flags["multiple"]:
            raise _spec_error(
                "field_not_allowed",
                f"Column {clip_repr(name)}: multiple is only allowed on enum columns.",
                name,
                "multiple",
            )

    precision = spec.get("precision")
    if precision is not None:
        if col_type != "number":
            raise _spec_error(
                "field_not_allowed",
                f"Column {clip_repr(name)}: precision is only allowed on number columns.",
                name,
                "precision",
            )
        if (
            not isinstance(precision, int)
            or isinstance(precision, bool)
            or not 0 <= precision <= TABLE_NUMBER_MAX_PRECISION
        ):
            raise _spec_error(
                "invalid_precision",
                f"Column {clip_repr(name)}: precision must be a whole number from 0 to "
                f"{TABLE_NUMBER_MAX_PRECISION}.",
                name,
                "precision",
            )

    if flags["unique"] and col_type not in TABLE_UNIQUE_COLUMN_TYPES:
        raise _spec_error(
            "unique_not_allowed",
            f"Column {clip_repr(name)}: unique is only allowed on "
            f"{', '.join(TABLE_UNIQUE_COLUMN_TYPES)} columns.",
            name,
            "unique",
        )

    existing_keys = {c.key for c in existing}
    key = spec.get("key")
    if key is None:
        key = new_column_key(existing_keys)
    elif not is_valid_column_key(key):
        raise _spec_error(
            "invalid_key",
            f"Column {clip_repr(name)}: key must be {TABLE_COLUMN_KEY_PREFIX!r} followed by "
            f"{TABLE_COLUMN_KEY_HEX_LEN} lowercase hex digits.",
            name,
            "key",
        )
    elif key in existing_keys:
        raise _spec_error(
            "duplicate_key",
            f"Column {clip_repr(name)}: key {clip_repr(key)} is already used by another column.",
            name,
            "key",
        )

    column = Column(
        key=key,
        name=name,
        type=col_type,
        description=description,
        required=flags["required"],
        unique=flags["unique"],
        options=options,
        multiple=flags["multiple"],
        precision=precision,
    )
    raw_default = spec.get("default")
    if raw_default is None:
        return column
    try:
        default = coerce_value(column, raw_default)
    except TableValueError as exc:
        raise _spec_error(
            "invalid_default",
            f"Invalid default: {exc.message}",
            name,
            "default",
        ) from exc
    return replace(column, default=default)


def parse_column_spec(
    spec: Mapping, *, existing: Sequence[Column] = (), allow_key: bool = False
) -> Column:
    """Validate a model- or UI-supplied column spec against the columns it
    joins and return the :class:`Column` with a fresh key and the default
    canonicalized. Raises :class:`ColumnSpecError`.

    Column keys are server-assigned and never exposed to models, so a
    ``key`` field is refused with ``unknown_field`` unless ``allow_key``
    is True. With ``allow_key`` (server code that assigns keys itself) a
    ``key`` must be well formed (``invalid_key``) and unused by
    ``existing`` (``duplicate_key``); a missing or null key gets a fresh
    one."""
    if not isinstance(spec, Mapping):
        raise ColumnSpecError("invalid_spec", "A column spec must be an object.")
    name = spec.get("name") if isinstance(spec.get("name"), str) else None
    if len(existing) >= TABLE_MAX_COLUMNS:
        raise _spec_error(
            "too_many_columns",
            f"A table can have at most {TABLE_MAX_COLUMNS} columns.",
            name,
        )
    if "key" in spec and not allow_key:
        raise _spec_error(
            "unknown_field",
            "Unknown column field: 'key' (column keys are assigned by the server).",
            name,
            "key",
        )
    for field in spec:
        if field not in _SPEC_FIELDS:
            raise _spec_error(
                "unknown_field",
                f"Unknown column field: {clip_repr(field)}.",
                name,
                field_label(field),
            )
    return _build_column(spec, existing=existing)


def parse_columns(
    specs: Sequence[Mapping], *, allow_key: bool = False
) -> list[Column]:
    """Validate the full column list of a new table: 1..TABLE_MAX_COLUMNS
    specs, each checked against the ones before it. ``allow_key`` as for
    :func:`parse_column_spec`."""
    if not isinstance(specs, (list, tuple)):
        raise ColumnSpecError("invalid_spec", "Columns must be a list of column specs.")
    if not specs:
        raise ColumnSpecError("no_columns", "A table needs at least one column.")
    if len(specs) > TABLE_MAX_COLUMNS:
        raise ColumnSpecError(
            "too_many_columns",
            f"A table can have at most {TABLE_MAX_COLUMNS} columns (got {len(specs)}).",
        )
    columns: list[Column] = []
    for spec in specs:
        columns.append(parse_column_spec(spec, existing=columns, allow_key=allow_key))
    return columns


def load_schema(data: list) -> list[Column]:
    """Stored schema JSON (``data_tables.schema``) to columns, re-validating
    each column and the uniqueness of names and keys."""
    if not isinstance(data, list):
        raise ColumnSpecError("invalid_spec", "A stored schema must be a list.")
    if len(data) > TABLE_MAX_COLUMNS:
        raise ColumnSpecError(
            "too_many_columns",
            f"A table can have at most {TABLE_MAX_COLUMNS} columns (got {len(data)}).",
        )
    columns: list[Column] = []
    for item in data:
        column = Column.from_dict(item)
        if name_taken(columns, column.name):
            raise _spec_error(
                "duplicate_name",
                f"Stored schema has two columns named {clip_repr(column.name)}.",
                column.name,
                "name",
            )
        if any(c.key == column.key for c in columns):
            raise _spec_error(
                "duplicate_key",
                f"Stored schema uses key {column.key!r} twice.",
                column.name,
                "key",
            )
        columns.append(column)
    return columns


def dump_schema(columns: Sequence[Column]) -> list[dict]:
    """Columns to the stored schema JSON."""
    return [column.to_dict() for column in columns]


# --- Values ------------------------------------------------------------------

# Longest numeric string looked at; far above any valid literal, it only
# bounds the regex and Decimal work on hostile input.
_NUMBER_INPUT_MAX_CHARS = 400

_NUMBER_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII
)

# Private context: the thread default (prec 28) would silently round
# 38-digit values, and callers must not be able to change our rounding.
_DECIMAL_CONTEXT = Context(
    prec=100,
    rounding=ROUND_HALF_EVEN,
    Emax=999_999,
    Emin=-999_999,
    traps=[InvalidOperation, Overflow, DivisionByZero],
)

_URL_SCHEMES = ("http", "https")


# Raw text longer than this is refused before NFC: normalization expands a
# character at most about 3x and composition only shrinks, so no value
# that is valid after normalization is longer than this before it.
_TEXT_INPUT_MAX_CHARS = 4 * TABLE_TEXT_MAX_CHARS

# How many options an unknown_option message lists.
_OPTIONS_HINT_MAX = 10


def _value_error(column: Column, code: str, detail: str) -> TableValueError:
    return TableValueError(
        code, f"Column {clip_repr(column.name)}: {detail}", column=column.name, detail=detail
    )


def _unknown_option(column: Column, value: str) -> TableValueError:
    options = list(column.options or ())
    listed = ", ".join(clip_repr(o) for o in options[:_OPTIONS_HINT_MAX])
    if len(options) > _OPTIONS_HINT_MAX:
        listed += f" and {len(options) - _OPTIONS_HINT_MAX} more"
    return _value_error(
        column,
        "unknown_option",
        f"{clip_repr(value)} is not one of the options (options: {listed}).",
    )


def _coerce_text(column: Column, value: Any) -> str:
    if not isinstance(value, str):
        raise _value_error(column, "wrong_type", "expected a string.")
    if len(value) > _TEXT_INPUT_MAX_CHARS:
        raise _value_error(
            column,
            "too_long",
            f"text is longer than {TABLE_TEXT_MAX_CHARS} characters ({len(value)}).",
        )
    problem = _text_problem(value, controls=True)
    if problem:
        raise _value_error(column, "invalid_text", f"text {problem}.")
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > TABLE_TEXT_MAX_CHARS:
        raise _value_error(
            column,
            "too_long",
            f"text is longer than {TABLE_TEXT_MAX_CHARS} characters ({len(text)}).",
        )
    return text


def _too_many_digits(column: Column) -> TableValueError:
    return _value_error(
        column,
        "too_many_digits",
        f"number needs more than {TABLE_NUMBER_MAX_DIGITS} digits.",
    )


def _coerce_number(column: Column, value: Any) -> str:
    if isinstance(value, bool):
        raise _value_error(column, "wrong_type", "expected a number, not true/false.")
    if isinstance(value, int):
        # Bound before Decimal(): huge ints are slow to convert.
        if abs(value) >= 10**TABLE_NUMBER_MAX_DIGITS:
            raise _too_many_digits(column)
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise _value_error(column, "invalid_number", "number must be finite.")
        # repr is the shortest round-tripping form: 0.1 stays 0.1.
        number = Decimal(repr(value))
    elif isinstance(value, str):
        literal = value.strip()
        if len(literal) > _NUMBER_INPUT_MAX_CHARS:
            raise _value_error(column, "too_long", "number string is too long.")
        # The regex runs before Decimal(), which would also accept
        # underscores, NaN, Infinity and non-ASCII digits.
        if _NUMBER_RE.fullmatch(literal) is None:
            raise _value_error(
                column,
                "invalid_number",
                f"{literal[:40]!r} is not a plain decimal number (no thousands "
                "separators, units or NaN/Infinity).",
            )
        # Zero is zero whatever its exponent (0e999999999999999999999999).
        if not re.split("[eE]", literal, maxsplit=1)[0].strip("+-.0"):
            return "0"
        try:
            number = Decimal(literal)
        except InvalidOperation:  # exponent beyond what decimal represents
            raise _too_many_digits(column) from None
        # Decimal() signals through the caller's thread context: with
        # InvalidOperation untrapped an unrepresentable exponent comes back
        # as NaN instead of raising.
        if not number.is_finite():
            raise _too_many_digits(column)
    else:
        raise _value_error(column, "wrong_type", "expected a number or numeric string.")

    if number.is_zero():
        return "0"
    adjusted = number.adjusted()
    # Below 10**-(precision+1) the value rounds to zero at ``precision``
    # (it is under half a unit of the last place); answer before the
    # magnitude guard, which would call 1e-39 too long.
    if column.precision is not None and adjusted <= -(column.precision + 2):
        return "0"
    # Magnitude guard before any arithmetic, so 1e999999999 never expands:
    # 10**38 and up has 39+ integer digits, below 10**-38 needs 39+
    # fractional digits.
    if adjusted >= TABLE_NUMBER_MAX_DIGITS or adjusted < -TABLE_NUMBER_MAX_DIGITS:
        raise _too_many_digits(column)

    if column.precision is not None:
        try:
            number = number.quantize(
                Decimal(1).scaleb(-column.precision, _DECIMAL_CONTEXT),
                rounding=ROUND_HALF_EVEN,
                context=_DECIMAL_CONTEXT,
            )
        except (InvalidOperation, Overflow):
            raise _too_many_digits(column) from None
        if number.is_zero():
            return "0"

    # Exact rendering (no context rounding); the guard above bounds its size.
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    digits = sum(ch.isdigit() for ch in text)
    if text.lstrip("-").startswith("0."):
        digits -= 1
    if digits > TABLE_NUMBER_MAX_DIGITS:
        raise _too_many_digits(column)
    return text


def _coerce_url(column: Column, value: Any) -> str:
    if not isinstance(value, str):
        raise _value_error(column, "wrong_type", "expected a URL string.")
    url = value.strip()
    if not url:
        raise _value_error(column, "invalid_url", "URL is empty.")
    if len(url) > TABLE_URL_MAX_CHARS:
        raise _value_error(
            column, "too_long", f"URL is longer than {TABLE_URL_MAX_CHARS} characters."
        )
    if any(ch <= " " or ch == "\x7f" or ch.isspace() for ch in url):
        raise _value_error(
            column, "invalid_url", "URL contains spaces or control characters."
        )
    # urlsplit reads http://evil.com\@good.com/ as host good.com; browsers
    # treat the backslash as a slash and go to evil.com.
    if "\\" in url:
        raise _value_error(column, "invalid_url", "URL contains a backslash.")
    problem = _text_problem(url, controls=True)
    if problem:
        raise _value_error(column, "invalid_url", f"URL {problem}.")
    bad = _value_error(
        column, "invalid_url", "expected an http:// or https:// URL with a host."
    )
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _URL_SCHEMES or not parts.hostname:
            raise bad
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise bad from None
    return url


def _coerce_enum(column: Column, value: Any) -> str | list[str] | None:
    options = column.options or ()
    if not column.multiple:
        if not isinstance(value, str):
            raise _value_error(column, "wrong_type", "expected one option string.")
        if value not in options:
            raise _unknown_option(column, value)
        return value
    if not isinstance(value, (list, tuple)):
        raise _value_error(column, "wrong_type", "expected a list of option strings.")
    order = {option: index for index, option in enumerate(options)}
    chosen = set()
    for item in value:
        if not isinstance(item, str):
            raise _value_error(column, "wrong_type", "every element must be a string.")
        if item not in order:
            raise _unknown_option(column, item)
        chosen.add(item)
    if not chosen:
        return None
    return sorted(chosen, key=order.__getitem__)


def coerce_option(column: Column, value: Any) -> str:
    """One option of an enum column (single or multiple), checked by exact
    match: the operand of a multi-enum ``contains`` filter. Raises
    :class:`TableValueError` (``wrong_type``, ``unknown_option``), or
    ValueError when ``column`` is not an enum (a caller bug)."""
    if column.type != "enum":
        raise ValueError(f"coerce_option needs an enum column, not {column.type!r}.")
    if not isinstance(value, str):
        raise _value_error(column, "wrong_type", "expected one option string.")
    if value not in (column.options or ()):
        raise _unknown_option(column, value)
    return value


def coerce_value(column: Column, value: Any) -> Any:
    """Canonicalize ``value`` for ``column``'s type (see the module
    docstring). None stays None; ``required`` is not checked. Raises
    :class:`TableValueError`.

    Filter operands reuse this for ``eq`` / ``ne`` / ``in`` and the number
    comparisons, but not everywhere: the text/url substring ops
    (``contains``, ``starts_with``, ``ends_with``) take plain substrings,
    which need not be valid URLs; a multi-enum ``contains`` takes one
    option (:func:`coerce_option`); and ``[]`` on a multi-enum column
    coerces to None, so ``contains_any`` / ``contains_all`` must check for
    an empty list themselves."""
    if value is None:
        return None
    if column.type == "text":
        return _coerce_text(column, value)
    if column.type == "number":
        return _coerce_number(column, value)
    if column.type == "url":
        return _coerce_url(column, value)
    if column.type == "boolean":
        if not isinstance(value, bool):
            raise _value_error(column, "wrong_type", "expected true or false.")
        return value
    if column.type == "enum":
        return _coerce_enum(column, value)
    raise _value_error(column, "wrong_type", f"unknown column type {column.type!r}.")


def canonicalize(column: Column, value: Any) -> Any:
    """:func:`coerce_value`, refusing a missing value (None or an empty
    multi-enum list) on a required column with ``required``."""
    result = coerce_value(column, value)
    if result is None and column.required:
        raise _value_error(column, "required", "a value is required.")
    return result


def apply_default(column: Column, present: bool, value: Any = None) -> Any:
    """Insert-path value for one column: the column default when the key is
    omitted (``present`` False), otherwise :func:`canonicalize` of the
    value. An explicit null does not pick up the default."""
    if not present and column.default is not None:
        default = column.default
        return list(default) if isinstance(default, tuple) else default
    return canonicalize(column, value if present else None)


def to_storage(column: Column, value: Any) -> str | int | None:
    """Canonical logical value to its ``table.db`` cell form."""
    if value is None:
        return None
    if column.type == "boolean":
        return 1 if value else 0
    if column.type == "enum" and column.multiple:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def from_storage(column: Column, stored: Any) -> Any:
    """Inverse of :func:`to_storage`."""
    if stored is None:
        return None
    if column.type == "boolean":
        return bool(stored)
    if column.type == "enum" and column.multiple:
        return json.loads(stored)
    return stored
