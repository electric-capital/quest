"""Quest Tables schema operations, their side effects, and the schema diff
(see docs/architecture/quest-tables.md).

Pure functions: :func:`apply_schema_operations` validates a list of
operation dicts against a working copy of the columns and returns a
:class:`SchemaChangePlan` (the new columns, the ordered side effects the
table file storage must execute, the new title, and the normalized
operations for the change log). :func:`schema_diff` compares two column
lists by key for previews. No SQLite, no I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import Any, ClassVar, Union

from chat.tables.constants import (
    TABLE_NUMBER_MAX_PRECISION,
    TABLE_SCHEMA_MAX_OPERATIONS,
    TABLE_TITLE_MAX_LEN,
    TABLE_UNIQUE_COLUMN_TYPES,
)
from chat.tables.schema import (
    Column,
    ColumnSpecError,
    TableSchemaError,
    TableValueError,
    coerce_value,
    find_column,
    name_taken,
    new_column_key,
    parse_column_spec,
    validate_column_description,
    validate_column_name,
)

# --- Errors ------------------------------------------------------------------


class SchemaOperationError(TableSchemaError):
    """A schema operation list refused by :func:`apply_schema_operations`.

    Extra attributes: ``op_index`` (0-based index of the failing operation,
    None for list-level refusals) and ``op`` (its operation name when known).
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        op_index: int | None = None,
        op: str | None = None,
        column: str | None = None,
        field: str | None = None,
    ):
        super().__init__(code, message, column=column, field=field)
        self.op_index = op_index
        self.op = op


# --- Side effects ------------------------------------------------------------


def _copy_value(value: Any) -> Any:
    """A JSON-ready copy: a list or tuple (a multi-enum value, or a
    :class:`Column`'s tuple-held multi default) becomes a fresh list."""
    return list(value) if isinstance(value, (list, tuple)) else value


class _Effect:
    kind: ClassVar[str]

    def to_dict(self) -> dict:
        """``{"kind": ..., **fields}``, JSON-ready."""
        data: dict[str, Any] = {"kind": self.kind}
        for f in fields(self):  # type: ignore[arg-type]
            data[f.name] = _copy_value(getattr(self, f.name))
        return data


@dataclass(frozen=True)
class AddColumnDDL(_Effect):
    """``ALTER TABLE rows ADD COLUMN``."""

    column_key: str
    kind: ClassVar[str] = "add_column"


@dataclass(frozen=True)
class DropColumnDDL(_Effect):
    """``ALTER TABLE rows DROP COLUMN``."""

    column_key: str
    kind: ClassVar[str] = "drop_column"


@dataclass(frozen=True)
class CaptureDroppedValues(_Effect):
    """Save every value of the column into the change's before-image; always
    emitted right before the column's :class:`DropColumnDDL`."""

    column_key: str
    kind: ClassVar[str] = "capture_dropped"


@dataclass(frozen=True)
class CreateUniqueIndex(_Effect):
    column_key: str
    kind: ClassVar[str] = "create_unique_index"


@dataclass(frozen=True)
class DropUniqueIndex(_Effect):
    column_key: str
    kind: ClassVar[str] = "drop_unique_index"


@dataclass(frozen=True)
class Backfill(_Effect):
    """Set ``value`` (canonical logical value, a list for a multi enum) on
    every existing row."""

    column_key: str
    value: Any
    kind: ClassVar[str] = "backfill"


@dataclass(frozen=True)
class EnumRewrite(_Effect):
    """Rewrite ``old_option`` to ``new_option`` in every cell (inside the
    JSON list when ``multiple``)."""

    column_key: str
    old_option: str
    new_option: str
    multiple: bool
    kind: ClassVar[str] = "enum_rewrite"


@dataclass(frozen=True)
class ViolationCheck(_Effect):
    """Refuse the change with ``rows_violate`` when existing rows break the
    rule: ``check`` is ``"required"`` (a null cell), ``"unique"`` (duplicate
    non-null values) or ``"enum_option"`` (a cell holding ``option``)."""

    check: str
    column_key: str
    option: str | None = None
    multiple: bool = False
    kind: ClassVar[str] = "violation_check"


@dataclass(frozen=True)
class WrapMulti(_Effect):
    """Single-enum cells become one-element JSON lists (``"A"`` -> ``["A"]``)."""

    column_key: str
    kind: ClassVar[str] = "wrap_multi"


SchemaSideEffect = Union[
    AddColumnDDL,
    DropColumnDDL,
    CaptureDroppedValues,
    CreateUniqueIndex,
    DropUniqueIndex,
    Backfill,
    EnumRewrite,
    ViolationCheck,
    WrapMulti,
]


# --- Plan --------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaChangePlan:
    """Result of :func:`apply_schema_operations`.

    ``columns``: the new schema. ``effects``: the side effects in execution
    order. ``new_title``: the last ``rename_table`` title (stripped), else
    None. ``operations``: one normalized dict per input operation, for the
    change log's after-image (see :func:`apply_schema_operations`).
    """

    columns: list[Column]
    effects: list[SchemaSideEffect]
    new_title: str | None
    operations: list[dict]


# --- Operations --------------------------------------------------------------

# op -> (required params, optional params). Type checks happen per op.
_OP_PARAMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "add_column": (("column",), ("after",)),
    "rename_column": (("name", "new_name"), ()),
    "drop_column": (("name",), ()),
    "reorder_columns": (("names",), ()),
    "set_description": (("name", "description"), ()),
    "set_required": (("name", "required"), ()),
    "set_unique": (("name", "unique"), ()),
    "set_default": (("name", "default"), ()),
    "set_precision": (("name", "precision"), ()),
    "add_enum_option": (("name", "option"), ("after",)),
    "rename_enum_option": (("name", "option", "new_option"), ()),
    "remove_enum_option": (("name", "option"), ()),
    "set_enum_multiple": (("name", "multiple"), ()),
    "rename_table": (("title",), ()),
}

_TYPE_CHANGE_OPS = ("change_column_type", "set_type", "set_column_type")

_ENUM_OPS = (
    "add_enum_option",
    "rename_enum_option",
    "remove_enum_option",
    "set_enum_multiple",
)


class _Batch:
    """Working state while applying one operation list."""

    def __init__(self, columns: Sequence[Column], table_empty: bool):
        self.columns: list[Column] = list(columns)
        # Every key the table had or gained in this batch: a fresh key must
        # not reuse one, or the diff and the change log would conflate two
        # different columns.
        self.seen_keys: set[str] = {c.key for c in columns}
        self.table_empty = table_empty
        self.effects: list[SchemaSideEffect] = []
        self.title: str | None = None
        self.index = 0
        self.op = ""

    # -- errors

    def error(
        self, code: str, message: str, *, column: str | None = None,
        field: str | None = None,
    ) -> SchemaOperationError:
        return SchemaOperationError(
            code, message, op_index=self.index, op=self.op, column=column, field=field
        )

    def wrap(self, exc: ColumnSpecError) -> SchemaOperationError:
        prefix = f"Operation {self.index + 1} ({self.op}): "
        return self.error(
            exc.code, prefix + exc.message, column=exc.column, field=exc.field
        )

    # -- param helpers

    def str_param(self, op: Mapping, field: str, *, nullable: bool = False) -> Any:
        value = op[field] if field in op else None
        if value is None and nullable:
            return None
        if not isinstance(value, str):
            raise self.error(
                "invalid_field",
                f"Operation {self.index + 1} ({self.op}): {field} must be a string.",
                field=field,
            )
        return value

    def bool_param(self, op: Mapping, field: str) -> bool:
        value = op[field]
        if not isinstance(value, bool):
            raise self.error(
                "invalid_field",
                f"Operation {self.index + 1} ({self.op}): {field} must be true or false.",
                field=field,
            )
        return value

    def column(self, op: Mapping, field: str = "name") -> Column:
        name = self.str_param(op, field)
        column = find_column(self.columns, name)
        if column is None:
            raise self.error(
                "unknown_column",
                f"Operation {self.index + 1} ({self.op}): no column named {name!r}.",
                column=name,
                field=field,
            )
        return column

    def enum_column(self, op: Mapping) -> Column:
        column = self.column(op)
        if column.type != "enum":
            raise self.error(
                "wrong_column_type",
                f"Operation {self.index + 1} ({self.op}): column {column.name!r} is "
                f"{column.type}, not enum.",
                column=column.name,
                field="name",
            )
        return column

    def put(self, column: Column) -> None:
        self.columns = [column if c.key == column.key else c for c in self.columns]

    def check_options(self, column: Column, options: list[str]) -> tuple[str, ...]:
        """Run a changed option list through parse_column_spec's option rules."""
        spec = {"name": column.name, "type": "enum", "options": options,
                "multiple": column.multiple}
        try:
            return parse_column_spec(spec).options  # type: ignore[return-value]
        except ColumnSpecError as exc:
            raise self.wrap(exc) from None

    def option_param(self, column: Column, op: Mapping, field: str) -> str:
        option = self.str_param(op, field)
        if option not in (column.options or ()):
            raise self.error(
                "unknown_option",
                f"Operation {self.index + 1} ({self.op}): column {column.name!r} has "
                f"no option {option[:100]!r}.",
                column=column.name,
                field=field,
            )
        return option

    def coerce_default(self, column: Column, value: Any) -> Any:
        try:
            return coerce_value(column, value)
        except TableValueError as exc:
            raise self.error(
                "invalid_default",
                f"Operation {self.index + 1} ({self.op}): invalid default: {exc.message}",
                column=column.name,
                field="default",
            ) from None


def _validate_op_shape(batch: _Batch, op: Any) -> str:
    """Check the op dict's name and params; return the op name."""
    if not isinstance(op, Mapping):
        raise batch.error(
            "invalid_operations", f"Operation {batch.index + 1} must be an object."
        )
    if "op" not in op:
        raise batch.error(
            "missing_field",
            f"Operation {batch.index + 1} has no 'op' field.",
            field="op",
        )
    name = op["op"]
    if not isinstance(name, str):
        raise batch.error(
            "invalid_field", f"Operation {batch.index + 1}: 'op' must be a string.",
            field="op",
        )
    batch.op = name
    if name in _TYPE_CHANGE_OPS or "type" in op:
        raise batch.error(
            "type_change_not_supported",
            f"Operation {batch.index + 1} ({name}): column types cannot be changed. "
            "Add a new column of the new type and copy the values with row writes.",
            field="type" if "type" in op else "op",
        )
    if name not in _OP_PARAMS:
        raise batch.error(
            "unknown_operation",
            f"Operation {batch.index + 1}: unknown operation {name[:80]!r}. Known "
            f"operations: {', '.join(_OP_PARAMS)}.",
            field="op",
        )
    required, optional = _OP_PARAMS[name]
    for field in op:
        if field != "op" and field not in required and field not in optional:
            raise batch.error(
                "unknown_field",
                f"Operation {batch.index + 1} ({name}): unknown field {str(field)!r}.",
                field=str(field),
            )
    for field in required:
        if field not in op:
            raise batch.error(
                "missing_field",
                f"Operation {batch.index + 1} ({name}): missing field {field!r}.",
                field=field,
            )
    return name


# Each handler validates and applies one op to the batch and returns the
# normalized op dict.


def _add_column(b: _Batch, op: Mapping) -> dict:
    spec = op["column"]
    if not isinstance(spec, Mapping):
        raise b.error(
            "invalid_field",
            f"Operation {b.index + 1} (add_column): column must be a column spec object.",
            field="column",
        )
    after_name = b.str_param(op, "after", nullable=True)
    spec = dict(spec)
    key = spec.get("key")
    if key is None:
        spec["key"] = new_column_key(b.seen_keys)
    elif key in b.seen_keys:
        name = spec.get("name") if isinstance(spec.get("name"), str) else None
        raise b.error(
            "duplicate_key",
            f"Operation {b.index + 1} (add_column): key {key!r} is already used.",
            column=name,
            field="key",
        )
    try:
        column = parse_column_spec(spec, existing=b.columns)
    except ColumnSpecError as exc:
        raise b.wrap(exc) from None
    if column.required and column.default is None and not b.table_empty:
        raise b.error(
            "required_needs_default",
            f"Operation {b.index + 1} (add_column): column {column.name!r} is required "
            "but the table has rows; give it a default to fill them.",
            column=column.name,
            field="default",
        )
    after_key = None
    if after_name is None:
        b.columns.append(column)
    else:
        anchor = b.column(op, "after")
        after_key = anchor.key
        position = [c.key for c in b.columns].index(anchor.key) + 1
        b.columns.insert(position, column)
    b.seen_keys.add(column.key)

    b.effects.append(AddColumnDDL(column.key))
    if column.required and column.default is not None and not b.table_empty:
        b.effects.append(Backfill(column.key, _copy_value(column.default)))
    if column.unique:
        b.effects.append(ViolationCheck("unique", column.key))
        b.effects.append(CreateUniqueIndex(column.key))
    return {"op": "add_column", "column": column.to_dict(), "after": after_name,
            "after_key": after_key}


def _rename_column(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    new_name = b.str_param(op, "new_name")
    try:
        validate_column_name(new_name)
    except ColumnSpecError as exc:
        raise b.wrap(exc) from None
    if name_taken(b.columns, new_name, except_key=column.key):
        raise b.error(
            "duplicate_name",
            f"Operation {b.index + 1} (rename_column): a column named {new_name!r} "
            "already exists (names are compared case-insensitively).",
            column=new_name,
            field="new_name",
        )
    b.put(replace(column, name=new_name))
    return {"op": "rename_column", "name": column.name, "new_name": new_name,
            "key": column.key}


def _drop_column(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    if len(b.columns) == 1:
        raise b.error(
            "last_column",
            f"Operation {b.index + 1} (drop_column): {column.name!r} is the only "
            "column; a table needs at least one.",
            column=column.name,
            field="name",
        )
    b.columns = [c for c in b.columns if c.key != column.key]
    b.effects.append(CaptureDroppedValues(column.key))
    if column.unique:
        b.effects.append(DropUniqueIndex(column.key))
    b.effects.append(DropColumnDDL(column.key))
    return {"op": "drop_column", "name": column.name, "key": column.key}


def _reorder_columns(b: _Batch, op: Mapping) -> dict:
    names = op["names"]
    if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
        raise b.error(
            "invalid_field",
            f"Operation {b.index + 1} (reorder_columns): names must be a list of "
            "column names.",
            field="names",
        )
    current = [c.name for c in b.columns]
    if len(names) != len(current) or len(set(names)) != len(names) or set(
        names
    ) != set(current):
        raise b.error(
            "not_a_permutation",
            f"Operation {b.index + 1} (reorder_columns): names must list every current "
            f"column exactly once (current columns: {current}).",
            field="names",
        )
    by_name = {c.name: c for c in b.columns}
    b.columns = [by_name[n] for n in names]
    return {"op": "reorder_columns", "names": list(names),
            "keys": [by_name[n].key for n in names]}


def _set_description(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    description = b.str_param(op, "description", nullable=True)
    try:
        description = validate_column_description(description, column.name)
    except ColumnSpecError as exc:
        raise b.wrap(exc) from None
    b.put(replace(column, description=description))
    return {"op": "set_description", "name": column.name, "description": description,
            "key": column.key}


def _set_required(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    required = b.bool_param(op, "required")
    if required and not column.required:
        b.effects.append(ViolationCheck("required", column.key))
    b.put(replace(column, required=required))
    return {"op": "set_required", "name": column.name, "required": required,
            "key": column.key}


def _set_unique(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    unique = b.bool_param(op, "unique")
    if unique and column.type not in TABLE_UNIQUE_COLUMN_TYPES:
        raise b.error(
            "unique_not_allowed",
            f"Operation {b.index + 1} (set_unique): unique is only allowed on "
            f"{', '.join(TABLE_UNIQUE_COLUMN_TYPES)} columns; {column.name!r} is "
            f"{column.type}.",
            column=column.name,
            field="unique",
        )
    if unique and not column.unique:
        b.effects.append(ViolationCheck("unique", column.key))
        b.effects.append(CreateUniqueIndex(column.key))
    elif not unique and column.unique:
        b.effects.append(DropUniqueIndex(column.key))
    b.put(replace(column, unique=unique))
    return {"op": "set_unique", "name": column.name, "unique": unique, "key": column.key}


def _set_default(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    default = b.coerce_default(column, op["default"])
    b.put(replace(column, default=default))
    return {"op": "set_default", "name": column.name, "default": _copy_value(default),
            "key": column.key}


def _set_precision(b: _Batch, op: Mapping) -> dict:
    column = b.column(op)
    precision = op["precision"]
    if precision is not None and (
        not isinstance(precision, int) or isinstance(precision, bool)
    ):
        raise b.error(
            "invalid_field",
            f"Operation {b.index + 1} (set_precision): precision must be a whole number "
            "or null.",
            column=column.name,
            field="precision",
        )
    if column.type != "number":
        raise b.error(
            "wrong_column_type",
            f"Operation {b.index + 1} (set_precision): column {column.name!r} is "
            f"{column.type}; precision is only for number columns.",
            column=column.name,
            field="name",
        )
    if precision is not None and not 0 <= precision <= TABLE_NUMBER_MAX_PRECISION:
        raise b.error(
            "invalid_precision",
            f"Operation {b.index + 1} (set_precision): precision must be from 0 to "
            f"{TABLE_NUMBER_MAX_PRECISION}.",
            column=column.name,
            field="precision",
        )
    updated = replace(column, precision=precision)
    updated = replace(updated, default=b.coerce_default(updated, column.default))
    b.put(updated)
    return {"op": "set_precision", "name": column.name, "precision": precision,
            "key": column.key}


def _add_enum_option(b: _Batch, op: Mapping) -> dict:
    column = b.enum_column(op)
    option = b.str_param(op, "option")
    after = b.str_param(op, "after", nullable=True)
    options = list(column.options or ())
    if after is None:
        options.append(option)
    else:
        b.option_param(column, op, "after")
        options.insert(options.index(after) + 1, option)
    b.put(replace(column, options=b.check_options(column, options)))
    return {"op": "add_enum_option", "name": column.name, "option": option,
            "after": after, "key": column.key}


def _rename_enum_option(b: _Batch, op: Mapping) -> dict:
    column = b.enum_column(op)
    old = b.option_param(column, op, "option")
    new = b.str_param(op, "new_option")
    normalized = {"op": "rename_enum_option", "name": column.name, "option": old,
                  "new_option": new, "key": column.key}
    if new == old:
        return normalized
    options = [new if o == old else o for o in column.options or ()]
    default = column.default  # a tuple for a multi enum
    if column.multiple and default is not None:
        default = tuple(new if o == old else o for o in default)
    elif default == old:
        default = new
    b.put(replace(column, options=b.check_options(column, options), default=default))
    b.effects.append(EnumRewrite(column.key, old, new, column.multiple))
    return normalized


def _remove_enum_option(b: _Batch, op: Mapping) -> dict:
    column = b.enum_column(op)
    option = b.option_param(column, op, "option")
    options = column.options or ()
    if len(options) == 1:
        raise b.error(
            "last_option",
            f"Operation {b.index + 1} (remove_enum_option): {option!r} is the only "
            f"option of {column.name!r}; an enum needs at least one.",
            column=column.name,
            field="option",
        )
    default = column.default
    if default == option or (isinstance(default, tuple) and option in default):
        raise b.error(
            "default_uses_option",
            f"Operation {b.index + 1} (remove_enum_option): the default of "
            f"{column.name!r} uses {option!r}; change the default first.",
            column=column.name,
            field="option",
        )
    b.put(replace(column, options=tuple(o for o in options if o != option)))
    b.effects.append(
        ViolationCheck("enum_option", column.key, option=option, multiple=column.multiple)
    )
    return {"op": "remove_enum_option", "name": column.name, "option": option,
            "key": column.key}


def _set_enum_multiple(b: _Batch, op: Mapping) -> dict:
    column = b.enum_column(op)
    multiple = b.bool_param(op, "multiple")
    if not multiple and column.multiple:
        raise b.error(
            "multi_to_single",
            f"Operation {b.index + 1} (set_enum_multiple): {column.name!r} allows "
            "several options; it cannot be turned back into a single-option column.",
            column=column.name,
            field="multiple",
        )
    if multiple and not column.multiple:
        default = None if column.default is None else (column.default,)
        b.put(replace(column, multiple=True, default=default))
        b.effects.append(WrapMulti(column.key))
    return {"op": "set_enum_multiple", "name": column.name, "multiple": multiple,
            "key": column.key}


def _rename_table(b: _Batch, op: Mapping) -> dict:
    title = op["title"]
    stripped = title.strip() if isinstance(title, str) else ""
    if not stripped or len(stripped) > TABLE_TITLE_MAX_LEN:
        raise b.error(
            "invalid_title",
            f"Operation {b.index + 1} (rename_table): the title must be text of 1 to "
            f"{TABLE_TITLE_MAX_LEN} characters.",
            field="title",
        )
    b.title = stripped
    return {"op": "rename_table", "title": stripped}


_HANDLERS = {
    "add_column": _add_column,
    "rename_column": _rename_column,
    "drop_column": _drop_column,
    "reorder_columns": _reorder_columns,
    "set_description": _set_description,
    "set_required": _set_required,
    "set_unique": _set_unique,
    "set_default": _set_default,
    "set_precision": _set_precision,
    "add_enum_option": _add_enum_option,
    "rename_enum_option": _rename_enum_option,
    "remove_enum_option": _remove_enum_option,
    "set_enum_multiple": _set_enum_multiple,
    "rename_table": _rename_table,
}


def apply_schema_operations(
    columns: Sequence[Column], ops: Sequence[Mapping], *, table_empty: bool
) -> SchemaChangePlan:
    """Validate and apply ``ops`` in order to a copy of ``columns``.

    Each operation sees the result of the ones before it; the first refusal
    raises :class:`SchemaOperationError` (``op_index`` 0-based) and nothing
    is applied. ``table_empty`` only decides whether a required
    ``add_column`` needs a default and whether a :class:`Backfill` is
    emitted; row-level checks are emitted either way. The inputs are never
    mutated.

    ``SchemaChangePlan.operations`` holds one normalized dict per input
    operation, ``op`` first, params canonicalized (stripped title and
    description, canonical defaults). Every operation that addresses a
    column by name also carries that column's immutable ``key`` (``keys``
    for ``reorder_columns``, ``after_key`` for ``add_column``'s ``after``)
    so a replay or undo can find the column after later renames.
    ``add_column``'s ``column`` is the full :meth:`Column.to_dict` including
    the assigned key, and its ``after`` is always present (None = appended).
    The names in a normalized op are the names at the time the op ran.
    """
    batch = _Batch(columns, table_empty)
    if not isinstance(ops, (list, tuple)):
        raise SchemaOperationError(
            "invalid_operations", "Operations must be a list of operation objects."
        )
    if not ops:
        raise SchemaOperationError("no_operations", "No schema operations were given.")
    if len(ops) > TABLE_SCHEMA_MAX_OPERATIONS:
        raise SchemaOperationError(
            "too_many_operations",
            f"At most {TABLE_SCHEMA_MAX_OPERATIONS} operations per schema change "
            f"(got {len(ops)}).",
        )
    normalized: list[dict] = []
    for index, op in enumerate(ops):
        batch.index = index
        batch.op = None  # type: ignore[assignment]
        name = _validate_op_shape(batch, op)
        normalized.append(_HANDLERS[name](batch, op))
    return SchemaChangePlan(
        columns=batch.columns,
        effects=batch.effects,
        new_title=batch.title,
        operations=normalized,
    )


# --- Diff --------------------------------------------------------------------


def _options_delta(before: Column | None, after: Column | None) -> tuple[list, list]:
    if before is None or after is None:
        return [], []
    if before.type != "enum" and after.type != "enum":
        return [], []
    old = list(before.options or ())
    new = list(after.options or ())
    return [o for o in new if o not in old], [o for o in old if o not in new]


def _entry(before: Column | None, after: Column | None) -> dict:
    before_dict = before.to_dict() if before is not None else None
    after_dict = after.to_dict() if after is not None else None
    changes: dict[str, dict] = {}
    if before_dict is not None and after_dict is not None:
        for field, old in before_dict.items():
            new = after_dict[field]
            if field != "key" and old != new:
                changes[field] = {"before": old, "after": new}
    if before is None:
        status = "added"
    elif after is None:
        status = "dropped"
    elif "name" in changes:
        status = "renamed"
    elif changes:
        status = "changed"
    else:
        status = "unchanged"
    added, removed = _options_delta(before, after)
    return {
        "key": (after or before).key,  # type: ignore[union-attr]
        "status": status,
        "before": before_dict,
        "after": after_dict,
        "changes": changes,
        "options_added": added,
        "options_removed": removed,
    }


def schema_diff(before: Sequence[Column], after: Sequence[Column]) -> dict:
    """Compare two schemas by column key (a rename is a rename, not a drop
    plus an add). JSON-serializable; see the module docs for the shape.

    Entries follow the after order; each dropped column sits right after
    its nearest surviving predecessor in the before order (first when it
    has none). ``options_added`` / ``options_removed`` list enum options by
    exact string for columns on both sides, else are empty.
    """
    before_by_key = {c.key: c for c in before}
    after_by_key = {c.key: c for c in after}

    dropped_after: dict[str | None, list[Column]] = {}
    anchor: str | None = None
    for column in before:
        if column.key in after_by_key:
            anchor = column.key
        else:
            dropped_after.setdefault(anchor, []).append(column)

    entries = [_entry(c, None) for c in dropped_after.get(None, [])]
    for column in after:
        entries.append(_entry(before_by_key.get(column.key), column))
        entries.extend(_entry(c, None) for c in dropped_after.get(column.key, []))

    surviving_before = [c.key for c in before if c.key in after_by_key]
    surviving_after = [c.key for c in after if c.key in before_by_key]
    order_changed = surviving_before != surviving_after
    return {
        "columns": entries,
        "order_changed": order_changed,
        "before_order": [c.name for c in before],
        "after_order": [c.name for c in after],
        "changed": order_changed or any(e["status"] != "unchanged" for e in entries),
    }
