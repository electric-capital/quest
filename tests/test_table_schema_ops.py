"""Quest Tables schema operations, side effects and diff (chat/tables/schema_ops.py)."""

import copy
import dataclasses
import json

import pytest

from chat.tables import schema_ops
from chat.tables.constants import (
    TABLE_MAX_COLUMNS,
    TABLE_SCHEMA_MAX_OPERATIONS,
    TABLE_TITLE_MAX_LEN,
)
from chat.tables.schema import Column, TableSchemaError, new_column_key, parse_columns
from chat.tables.schema_ops import (
    AddColumnDDL,
    Backfill,
    CaptureDroppedValues,
    CreateUniqueIndex,
    DropColumnDDL,
    DropUniqueIndex,
    EnumRewrite,
    SchemaChangePlan,
    SchemaOperationError,
    ViolationCheck,
    WrapMulti,
    apply_schema_operations,
    schema_diff,
)

K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS, K_TAGS = (
    f"c_0000000{i}" for i in range(1, 7)
)
K_NEW = "c_000000aa"
K_NEW2 = "c_000000bb"

SPECS = [
    {"key": K_NAME, "name": "Name", "type": "text", "unique": True},
    {"key": K_PRICE, "name": "Price", "type": "number", "precision": 2, "default": "1.25"},
    {"key": K_LINK, "name": "Link", "type": "url"},
    {"key": K_DONE, "name": "Done", "type": "boolean"},
    {"key": K_STATUS, "name": "Status", "type": "enum",
     "options": ["Todo", "Doing", "Done"], "default": "Todo"},
    {"key": K_TAGS, "name": "Tags", "type": "enum", "options": ["red", "green", "blue"],
     "multiple": True, "default": ["blue", "red"]},
]

NAMES = ["Name", "Price", "Link", "Done", "Status", "Tags"]


@pytest.fixture
def cols():
    return parse_columns(SPECS, allow_key=True)


@pytest.fixture(autouse=True)
def predictable_keys(monkeypatch):
    """Server-assigned keys are K_NEW, then K_NEW2, then random ones, each
    skipping keys already taken, so expected effects can name them."""

    def fake(existing_keys):
        for key in (K_NEW, K_NEW2):
            if key not in existing_keys:
                return key
        return new_column_key(existing_keys)

    monkeypatch.setattr(schema_ops, "new_column_key", fake)


def by_key(columns, key):
    return next(c for c in columns if c.key == key)


def effects(plan):
    return [e.to_dict() for e in plan.effects]


def run(columns, ops, table_empty=False):
    return apply_schema_operations(columns, ops, table_empty=table_empty)


# --- Side effect dataclasses -------------------------------------------------


def test_effect_to_dict_shapes():
    assert AddColumnDDL("c_1").to_dict() == {"kind": "add_column", "column_key": "c_1"}
    assert DropColumnDDL("c_1").to_dict()["kind"] == "drop_column"
    assert CaptureDroppedValues("c_1").to_dict()["kind"] == "capture_dropped"
    assert CreateUniqueIndex("c_1").to_dict()["kind"] == "create_unique_index"
    assert DropUniqueIndex("c_1").to_dict()["kind"] == "drop_unique_index"
    assert WrapMulti("c_1").to_dict()["kind"] == "wrap_multi"
    assert Backfill("c_1", ["a"]).to_dict() == {
        "kind": "backfill", "column_key": "c_1", "value": ["a"]}
    assert EnumRewrite("c_1", "a", "b", True).to_dict() == {
        "kind": "enum_rewrite", "column_key": "c_1", "old_option": "a",
        "new_option": "b", "multiple": True}
    assert ViolationCheck("required", "c_1").to_dict() == {
        "kind": "violation_check", "check": "required", "column_key": "c_1",
        "option": None, "multiple": False}
    with pytest.raises(dataclasses.FrozenInstanceError):
        AddColumnDDL("c_1").column_key = "x"  # type: ignore[misc]


# --- Single operations: success ----------------------------------------------

# (ops, table_empty, expected effects, expected normalized ops, check(columns))
SUCCESS_CASES = [
    pytest.param(
        [{"op": "add_column", "column": {"name": "Notes", "type": "text"}}],
        False,
        [{"kind": "add_column", "column_key": K_NEW}],
        [{"op": "add_column",
          "column": {"key": K_NEW, "name": "Notes", "type": "text", "required": False,
                     "unique": False, "default": None, "options": None,
                     "multiple": False, "precision": None, "description": ""},
          "after": None, "after_key": None}],
        lambda c: [x.name for x in c] == NAMES + ["Notes"],
        id="add_column-append",
    ),
    pytest.param(
        [{"op": "add_column", "after": "Name",
          "column": {"name": "Qty", "type": "number", "default": 3}}],
        False,
        [{"kind": "add_column", "column_key": K_NEW}],
        [{"op": "add_column",
          "column": {"key": K_NEW, "name": "Qty", "type": "number", "required": False,
                     "unique": False, "default": "3", "options": None,
                     "multiple": False, "precision": None, "description": ""},
          "after": "Name", "after_key": K_NAME}],
        lambda c: [x.name for x in c][:2] == ["Name", "Qty"],
        id="add_column-after",
    ),
    pytest.param(
        [{"op": "add_column", "column": {"name": "Must", "type": "text",
                                          "required": True}}],
        True,
        [{"kind": "add_column", "column_key": K_NEW}],
        None,
        lambda c: c[-1].required and c[-1].default is None,
        id="add_column-required-no-default-empty-table",
    ),
    pytest.param(
        [{"op": "add_column", "column": {"name": "Must", "type": "text",
                                          "required": True, "default": "x"}}],
        False,
        [{"kind": "add_column", "column_key": K_NEW},
         {"kind": "backfill", "column_key": K_NEW, "value": "x"}],
        None,
        lambda c: c[-1].default == "x",
        id="add_column-required-default-backfill",
    ),
    pytest.param(
        [{"op": "add_column", "column": {"name": "Must", "type": "text",
                                          "required": True, "default": "x"}}],
        True,
        [{"kind": "add_column", "column_key": K_NEW}],
        None,
        lambda c: c[-1].default == "x",
        id="add_column-required-default-empty-no-backfill",
    ),
    pytest.param(
        [{"op": "add_column", "column": {"name": "Sku", "type": "text",
                                          "unique": True}}],
        False,
        [{"kind": "add_column", "column_key": K_NEW},
         {"kind": "violation_check", "check": "unique", "column_key": K_NEW,
          "option": None, "multiple": False},
         {"kind": "create_unique_index", "column_key": K_NEW}],
        None,
        lambda c: c[-1].unique,
        id="add_column-unique",
    ),
    pytest.param(
        [{"op": "add_column", "column": {"name": "Sku", "type": "text",
                                          "unique": True, "required": True,
                                          "default": "a"}}],
        False,
        [{"kind": "add_column", "column_key": K_NEW},
         {"kind": "backfill", "column_key": K_NEW, "value": "a"},
         {"kind": "violation_check", "check": "unique", "column_key": K_NEW,
          "option": None, "multiple": False},
         {"kind": "create_unique_index", "column_key": K_NEW}],
        None,
        lambda c: c[-1].unique and c[-1].required,
        id="add_column-required-unique-order",
    ),
    pytest.param(
        [{"op": "rename_column", "name": "Price", "new_name": "Cost"}],
        False,
        [],
        [{"op": "rename_column", "name": "Price", "new_name": "Cost", "key": K_PRICE}],
        lambda c: by_key(c, K_PRICE).name == "Cost",
        id="rename_column",
    ),
    pytest.param(
        [{"op": "rename_column", "name": "Price", "new_name": "price"}],
        False,
        [],
        None,
        lambda c: by_key(c, K_PRICE).name == "price",
        id="rename_column-case-only",
    ),
    pytest.param(
        [{"op": "drop_column", "name": "Link"}],
        False,
        [{"kind": "capture_dropped", "column_key": K_LINK},
         {"kind": "drop_column", "column_key": K_LINK}],
        [{"op": "drop_column", "name": "Link", "key": K_LINK}],
        lambda c: [x.name for x in c] == ["Name", "Price", "Done", "Status", "Tags"],
        id="drop_column",
    ),
    pytest.param(
        [{"op": "drop_column", "name": "Name"}],
        False,
        [{"kind": "capture_dropped", "column_key": K_NAME},
         {"kind": "drop_unique_index", "column_key": K_NAME},
         {"kind": "drop_column", "column_key": K_NAME}],
        None,
        lambda c: find_name(c, "Name") is None,
        id="drop_column-unique",
    ),
    pytest.param(
        [{"op": "reorder_columns", "names": list(reversed(NAMES))}],
        False,
        [],
        [{"op": "reorder_columns", "names": list(reversed(NAMES)),
          "keys": [K_TAGS, K_STATUS, K_DONE, K_LINK, K_PRICE, K_NAME]}],
        lambda c: [x.name for x in c] == list(reversed(NAMES)),
        id="reorder_columns",
    ),
    pytest.param(
        [{"op": "set_description", "name": "Link", "description": "  Home page \n"}],
        False,
        [],
        [{"op": "set_description", "name": "Link", "description": "Home page",
          "key": K_LINK}],
        lambda c: by_key(c, K_LINK).description == "Home page",
        id="set_description",
    ),
    pytest.param(
        [{"op": "set_description", "name": "Link", "description": None}],
        False,
        [],
        None,
        lambda c: by_key(c, K_LINK).description == "",
        id="set_description-null",
    ),
    pytest.param(
        [{"op": "set_required", "name": "Link", "required": True}],
        True,
        [{"kind": "violation_check", "check": "required", "column_key": K_LINK,
          "option": None, "multiple": False}],
        [{"op": "set_required", "name": "Link", "required": True, "key": K_LINK}],
        lambda c: by_key(c, K_LINK).required,
        id="set_required-true-even-empty",
    ),
    pytest.param(
        [{"op": "set_unique", "name": "Link", "unique": True}],
        False,
        [{"kind": "violation_check", "check": "unique", "column_key": K_LINK,
          "option": None, "multiple": False},
         {"kind": "create_unique_index", "column_key": K_LINK}],
        [{"op": "set_unique", "name": "Link", "unique": True, "key": K_LINK}],
        lambda c: by_key(c, K_LINK).unique,
        id="set_unique-true",
    ),
    pytest.param(
        [{"op": "set_unique", "name": "Name", "unique": False}],
        False,
        [{"kind": "drop_unique_index", "column_key": K_NAME}],
        None,
        lambda c: not by_key(c, K_NAME).unique,
        id="set_unique-false",
    ),
    pytest.param(
        [{"op": "set_unique", "name": "Done", "unique": False}],
        False,
        [],
        None,
        lambda c: not by_key(c, K_DONE).unique,
        id="set_unique-false-on-boolean-noop",
    ),
    pytest.param(
        [{"op": "set_default", "name": "Price", "default": "3.14159"}],
        False,
        [],
        [{"op": "set_default", "name": "Price", "default": "3.14", "key": K_PRICE}],
        lambda c: by_key(c, K_PRICE).default == "3.14",
        id="set_default-canonical-rounded",
    ),
    pytest.param(
        [{"op": "set_default", "name": "Tags", "default": ["red", "green", "red"]}],
        False,
        [],
        [{"op": "set_default", "name": "Tags", "default": ["red", "green"],
          "key": K_TAGS}],
        lambda c: by_key(c, K_TAGS).default == ("red", "green"),
        id="set_default-multi",
    ),
    pytest.param(
        [{"op": "set_default", "name": "Status", "default": None}],
        False,
        [],
        None,
        lambda c: by_key(c, K_STATUS).default is None,
        id="set_default-clear",
    ),
    pytest.param(
        [{"op": "set_precision", "name": "Price", "precision": 1}],
        False,
        [],
        [{"op": "set_precision", "name": "Price", "precision": 1, "key": K_PRICE}],
        lambda c: by_key(c, K_PRICE).precision == 1 and by_key(c, K_PRICE).default == "1.2",
        id="set_precision-rerounds-default",
    ),
    pytest.param(
        [{"op": "set_precision", "name": "Price", "precision": None}],
        False,
        [],
        None,
        lambda c: by_key(c, K_PRICE).precision is None
        and by_key(c, K_PRICE).default == "1.25",
        id="set_precision-null",
    ),
    pytest.param(
        [{"op": "add_enum_option", "name": "Status", "option": "Blocked"}],
        False,
        [],
        [{"op": "add_enum_option", "name": "Status", "option": "Blocked", "after": None,
          "key": K_STATUS}],
        lambda c: by_key(c, K_STATUS).options == ("Todo", "Doing", "Done", "Blocked"),
        id="add_enum_option-append",
    ),
    pytest.param(
        [{"op": "add_enum_option", "name": "Status", "option": "Blocked",
          "after": "Todo"}],
        False,
        [],
        None,
        lambda c: by_key(c, K_STATUS).options == ("Todo", "Blocked", "Doing", "Done"),
        id="add_enum_option-after",
    ),
    pytest.param(
        [{"op": "rename_enum_option", "name": "Status", "option": "Todo",
          "new_option": "Open"}],
        False,
        [{"kind": "enum_rewrite", "column_key": K_STATUS, "old_option": "Todo",
          "new_option": "Open", "multiple": False}],
        [{"op": "rename_enum_option", "name": "Status", "option": "Todo",
          "new_option": "Open", "key": K_STATUS}],
        lambda c: by_key(c, K_STATUS).options == ("Open", "Doing", "Done")
        and by_key(c, K_STATUS).default == "Open",
        id="rename_enum_option-single-rewrites-default",
    ),
    pytest.param(
        [{"op": "rename_enum_option", "name": "Tags", "option": "blue",
          "new_option": "navy"}],
        False,
        [{"kind": "enum_rewrite", "column_key": K_TAGS, "old_option": "blue",
          "new_option": "navy", "multiple": True}],
        None,
        lambda c: by_key(c, K_TAGS).options == ("red", "green", "navy")
        and by_key(c, K_TAGS).default == ("red", "navy"),
        id="rename_enum_option-multi-rewrites-default",
    ),
    pytest.param(
        [{"op": "rename_enum_option", "name": "Status", "option": "Doing",
          "new_option": "DOING"}],
        False,
        [{"kind": "enum_rewrite", "column_key": K_STATUS, "old_option": "Doing",
          "new_option": "DOING", "multiple": False}],
        None,
        lambda c: by_key(c, K_STATUS).options == ("Todo", "DOING", "Done"),
        id="rename_enum_option-case-variant",
    ),
    pytest.param(
        [{"op": "remove_enum_option", "name": "Status", "option": "Done"}],
        False,
        [{"kind": "violation_check", "check": "enum_option", "column_key": K_STATUS,
          "option": "Done", "multiple": False}],
        [{"op": "remove_enum_option", "name": "Status", "option": "Done",
          "key": K_STATUS}],
        lambda c: by_key(c, K_STATUS).options == ("Todo", "Doing"),
        id="remove_enum_option-single",
    ),
    pytest.param(
        [{"op": "remove_enum_option", "name": "Tags", "option": "green"}],
        False,
        [{"kind": "violation_check", "check": "enum_option", "column_key": K_TAGS,
          "option": "green", "multiple": True}],
        None,
        lambda c: by_key(c, K_TAGS).options == ("red", "blue"),
        id="remove_enum_option-multi",
    ),
    pytest.param(
        [{"op": "set_enum_multiple", "name": "Status", "multiple": True}],
        False,
        [{"kind": "wrap_multi", "column_key": K_STATUS}],
        [{"op": "set_enum_multiple", "name": "Status", "multiple": True,
          "key": K_STATUS}],
        lambda c: by_key(c, K_STATUS).multiple and by_key(c, K_STATUS).default == ("Todo",),
        id="set_enum_multiple-wraps-default",
    ),
    pytest.param(
        [{"op": "rename_table", "title": "  Inventory  "}],
        False,
        [],
        [{"op": "rename_table", "title": "Inventory"}],
        lambda c: [x.name for x in c] == NAMES,
        id="rename_table",
    ),
]


def find_name(columns, name):
    return next((c for c in columns if c.name == name), None)


@pytest.mark.parametrize("ops, table_empty, expected_effects, expected_ops, check",
                         SUCCESS_CASES)
def test_operation_success(cols, ops, table_empty, expected_effects, expected_ops, check):
    plan = run(cols, ops, table_empty)
    assert isinstance(plan, SchemaChangePlan)
    assert effects(plan) == expected_effects
    if expected_ops is not None:
        assert plan.operations == expected_ops
    assert len(plan.operations) == len(ops)
    assert all(next(iter(op)) == "op" for op in plan.operations)
    assert check(plan.columns)
    json.dumps(plan.operations)
    json.dumps(effects(plan))


def test_rename_table_new_title(cols):
    assert run(cols, [{"op": "rename_table", "title": " T "}]).new_title == "T"
    assert run(cols, [{"op": "set_required", "name": "Done", "required": False}]).new_title is None


def test_add_column_assigns_fresh_key(cols):
    plan = run(cols, [{"op": "add_column", "column": {"name": "X", "type": "text"}}])
    new = plan.columns[-1]
    assert new.key not in {c.key for c in cols}
    assert plan.operations[0]["column"]["key"] == new.key
    assert plan.effects == [AddColumnDDL(new.key)]


# --- No-ops ------------------------------------------------------------------

NOOP_CASES = [
    pytest.param({"op": "rename_column", "name": "Link", "new_name": "Link"}, id="rename-same"),
    pytest.param({"op": "set_required", "name": "Link", "required": False}, id="required-same"),
    pytest.param({"op": "set_unique", "name": "Name", "unique": True}, id="unique-same"),
    pytest.param({"op": "set_default", "name": "Status", "default": "Todo"}, id="default-same"),
    pytest.param({"op": "set_precision", "name": "Price", "precision": 2}, id="precision-same"),
    pytest.param({"op": "set_description", "name": "Link", "description": ""},
                 id="description-same"),
    pytest.param({"op": "reorder_columns", "names": NAMES}, id="reorder-same"),
    pytest.param({"op": "rename_enum_option", "name": "Status", "option": "Todo",
                  "new_option": "Todo"}, id="rename-option-same"),
    pytest.param({"op": "set_enum_multiple", "name": "Tags", "multiple": True},
                 id="multiple-already"),
    pytest.param({"op": "set_enum_multiple", "name": "Status", "multiple": False},
                 id="single-stays-single"),
]


@pytest.mark.parametrize("op", NOOP_CASES)
def test_noop_operations_emit_nothing(cols, op):
    plan = run(cols, [op])
    assert plan.effects == []
    assert plan.columns == cols
    assert len(plan.operations) == 1
    assert schema_diff(cols, plan.columns)["changed"] is False


# --- Refusals ----------------------------------------------------------------

LONG_NAME = "x" * 81

# (ops, table_empty, code, op_index)
REFUSAL_CASES = [
    pytest.param({"op": "frobnicate"}, False, "unknown_operation", id="unknown-operation"),
    pytest.param({"name": "Link"}, False, "missing_field", id="missing-op"),
    pytest.param({"op": 5}, False, "invalid_field", id="op-not-string"),
    pytest.param("drop", False, "invalid_operations", id="op-not-object"),
    pytest.param({"op": "drop_column"}, False, "missing_field", id="missing-field"),
    pytest.param({"op": "drop_column", "name": "Link", "force": True}, False,
                 "unknown_field", id="unknown-field"),
    pytest.param({"op": "drop_column", "name": 3}, False, "invalid_field",
                 id="invalid-field-name"),
    pytest.param({"op": "set_required", "name": "Link", "required": "yes"}, False,
                 "invalid_field", id="invalid-field-bool"),
    pytest.param({"op": "set_enum_multiple", "name": "Status", "multiple": 1}, False,
                 "invalid_field", id="invalid-field-multiple"),
    pytest.param({"op": "set_precision", "name": "Price", "precision": "2"}, False,
                 "invalid_field", id="invalid-field-precision"),
    pytest.param({"op": "set_precision", "name": "Price", "precision": True}, False,
                 "invalid_field", id="invalid-field-precision-bool"),
    pytest.param({"op": "set_precision", "name": "Price", "precision": 11}, False,
                 "invalid_precision", id="invalid-precision-range"),
    pytest.param({"op": "reorder_columns", "names": "Name"}, False, "invalid_field",
                 id="invalid-field-names"),
    pytest.param({"op": "add_column", "column": "Notes"}, False, "invalid_field",
                 id="invalid-field-column"),
    pytest.param({"op": "set_description", "name": "Link", "description": 4}, False,
                 "invalid_field", id="invalid-field-description"),
    pytest.param({"op": "set_description", "name": "Link", "description": "d" * 501},
                 False, "invalid_description", id="description-too-long"),
    pytest.param({"op": "drop_column", "name": "Nope"}, False, "unknown_column",
                 id="unknown-column"),
    pytest.param({"op": "drop_column", "name": "link"}, False, "unknown_column",
                 id="unknown-column-case-sensitive"),
    pytest.param({"op": "add_column", "after": "Nope",
                  "column": {"name": "X", "type": "text"}}, False, "unknown_column",
                 id="add-after-unknown-column"),
    pytest.param({"op": "add_column", "column": {"name": "price", "type": "text"}},
                 False, "duplicate_name", id="duplicate-name-add"),
    pytest.param({"op": "rename_column", "name": "Link", "new_name": "PRICE"}, False,
                 "duplicate_name", id="duplicate-name-rename"),
    pytest.param({"op": "rename_column", "name": "Link", "new_name": " Link"}, False,
                 "invalid_name", id="rename-invalid-name"),
    pytest.param({"op": "rename_column", "name": "Link", "new_name": LONG_NAME}, False,
                 "invalid_name", id="rename-name-too-long"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "required": True}},
                 False, "required_needs_default", id="required-needs-default"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "date"}}, False,
                 "invalid_type", id="add-invalid-type"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "bogus": 1}},
                 False, "unknown_field", id="add-spec-unknown-field"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "boolean",
                                                 "unique": True}},
                 False, "unique_not_allowed", id="add-unique-not-allowed"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "number",
                                                 "default": "abc"}},
                 False, "invalid_default", id="add-invalid-default"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "key": K_LINK}},
                 False, "unknown_field", id="add-key-existing"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "key": "c_12345678"}},
                 False, "unknown_field", id="add-key-valid"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "key": None}},
                 False, "unknown_field", id="add-key-null"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "key": ["a"]}},
                 False, "unknown_field", id="add-key-list"),
    pytest.param({"op": "add_column", "column": {"name": "X", "type": "text",
                                                 "key": {}}},
                 False, "unknown_field", id="add-key-dict"),
    pytest.param({"op": "change_column_type", "name": "Link", "to": "text"}, False,
                 "type_change_not_supported", id="type-change-change_column_type"),
    pytest.param({"op": "set_type", "name": "Link"}, False,
                 "type_change_not_supported", id="type-change-set_type"),
    pytest.param({"op": "set_column_type", "name": "Link"}, False,
                 "type_change_not_supported", id="type-change-set_column_type"),
    pytest.param({"op": "set_required", "name": "Link", "required": True,
                  "type": "text"}, False, "type_change_not_supported",
                 id="type-change-type-param"),
    pytest.param({"op": "add_column", "type": "text",
                  "column": {"name": "X", "type": "text"}}, False,
                 "type_change_not_supported", id="type-change-type-param-on-add"),
    pytest.param({"op": "set_enum_multiple", "name": "Tags", "multiple": False}, False,
                 "multi_to_single", id="multi-to-single"),
    pytest.param({"op": "set_precision", "name": "Link", "precision": 2}, False,
                 "wrong_column_type", id="precision-on-non-number"),
    pytest.param({"op": "add_enum_option", "name": "Name", "option": "x"}, False,
                 "wrong_column_type", id="add-option-non-enum"),
    pytest.param({"op": "rename_enum_option", "name": "Done", "option": "a",
                  "new_option": "b"}, False, "wrong_column_type",
                 id="rename-option-non-enum"),
    pytest.param({"op": "remove_enum_option", "name": "Price", "option": "a"}, False,
                 "wrong_column_type", id="remove-option-non-enum"),
    pytest.param({"op": "set_enum_multiple", "name": "Link", "multiple": True}, False,
                 "wrong_column_type", id="multiple-non-enum"),
    pytest.param({"op": "set_unique", "name": "Status", "unique": True}, False,
                 "unique_not_allowed", id="unique-on-enum"),
    pytest.param({"op": "set_unique", "name": "Done", "unique": True}, False,
                 "unique_not_allowed", id="unique-on-boolean"),
    pytest.param({"op": "set_default", "name": "Status", "default": "Nope"}, False,
                 "invalid_default", id="default-unknown-option"),
    pytest.param({"op": "set_default", "name": "Done", "default": "true"}, False,
                 "invalid_default", id="default-wrong-type"),
    pytest.param({"op": "add_enum_option", "name": "Status", "option": "todo"}, False,
                 "duplicate_option", id="duplicate-option-case-insensitive"),
    pytest.param({"op": "add_enum_option", "name": "Status", "option": " x"}, False,
                 "invalid_option", id="add-option-whitespace"),
    pytest.param({"op": "add_enum_option", "name": "Status", "option": "o" * 101},
                 False, "invalid_option", id="add-option-too-long"),
    pytest.param({"op": "add_enum_option", "name": "Status", "option": "x",
                  "after": "Nope"}, False, "unknown_option", id="add-option-after-unknown"),
    pytest.param({"op": "rename_enum_option", "name": "Status", "option": "Doing",
                  "new_option": "done"}, False, "duplicate_option",
                 id="rename-option-duplicate"),
    pytest.param({"op": "rename_enum_option", "name": "Status", "option": "Doing",
                  "new_option": ""}, False, "invalid_option", id="rename-option-empty"),
    pytest.param({"op": "rename_enum_option", "name": "Status", "option": "Nope",
                  "new_option": "x"}, False, "unknown_option", id="rename-unknown-option"),
    pytest.param({"op": "remove_enum_option", "name": "Status", "option": "todo"}, False,
                 "unknown_option", id="remove-unknown-option-case-sensitive"),
    pytest.param({"op": "remove_enum_option", "name": "Status", "option": "Todo"}, False,
                 "default_uses_option", id="default-uses-option-single"),
    pytest.param({"op": "remove_enum_option", "name": "Tags", "option": "blue"}, False,
                 "default_uses_option", id="default-uses-option-multi"),
    pytest.param({"op": "set_description", "name": "Price", "description": "a\x00b"},
                 False, "invalid_description", id="description-nul"),
    pytest.param({"op": "set_description", "name": "Price", "description": "\ud800"},
                 False, "invalid_description", id="description-surrogate"),
    pytest.param({"op": "rename_column", "name": "Price", "new_name": "a\x00b"},
                 False, "invalid_name", id="rename-column-control-char"),
    pytest.param({"op": "rename_column", "name": "Price", "new_name": "\ud800"},
                 False, "invalid_name", id="rename-column-surrogate"),
    pytest.param({"op": "add_enum_option", "name": "Status", "option": "a\tb"},
                 False, "invalid_option", id="add-option-control-char"),
    pytest.param({"op": "rename_enum_option", "name": "Status", "option": "Doing",
                  "new_option": "\ud800"}, False, "invalid_option",
                 id="rename-option-surrogate"),
    pytest.param({"op": "reorder_columns", "names": NAMES[:-1]}, False,
                 "not_a_permutation", id="reorder-missing"),
    pytest.param({"op": "reorder_columns", "names": NAMES + ["Extra"]}, False,
                 "not_a_permutation", id="reorder-extra"),
    pytest.param({"op": "reorder_columns", "names": NAMES[:-1] + ["Name"]}, False,
                 "not_a_permutation", id="reorder-duplicate"),
    pytest.param({"op": "reorder_columns", "names": NAMES[:-1] + ["tags"]}, False,
                 "not_a_permutation", id="reorder-case-variant"),
    pytest.param({"op": "rename_table", "title": "   "}, False, "invalid_title",
                 id="title-blank"),
    pytest.param({"op": "rename_table", "title": "t" * (TABLE_TITLE_MAX_LEN + 1)},
                 False, "invalid_title", id="title-too-long"),
    pytest.param({"op": "rename_table", "title": 7}, False, "invalid_title",
                 id="title-not-string"),
    pytest.param({"op": "rename_table", "title": "a\nb"}, False, "invalid_title",
                 id="title-newline"),
    pytest.param({"op": "rename_table", "title": "a\x07b"}, False, "invalid_title",
                 id="title-control"),
    pytest.param({"op": "rename_table", "title": "a\ud800"}, False, "invalid_title",
                 id="title-surrogate"),
]


@pytest.mark.parametrize("op, table_empty, code", REFUSAL_CASES)
def test_operation_refusals(cols, op, table_empty, code):
    snapshot = copy.deepcopy(cols)
    # A harmless first op so the failing one has op_index 1.
    ops = [{"op": "set_description", "name": "Done", "description": "flag"}, op]
    with pytest.raises(SchemaOperationError) as info:
        run(cols, ops, table_empty)
    err = info.value
    assert err.code == code
    assert err.op_index == 1
    assert isinstance(err, TableSchemaError)
    assert str(err) == err.message
    if isinstance(op, dict) and isinstance(op.get("op"), str):
        assert err.op == op["op"]
    assert cols == snapshot


def test_refusal_carries_column_and_field(cols):
    with pytest.raises(SchemaOperationError) as info:
        run(cols, [{"op": "rename_column", "name": "Link", "new_name": "Price"}])
    assert (info.value.column, info.value.field, info.value.op) == (
        "Price", "new_name", "rename_column")


def test_add_required_without_default_on_empty_table_is_accepted(cols):
    op = {"op": "add_column", "column": {"name": "X", "type": "text", "required": True}}
    plan = run(cols, [op], table_empty=True)
    assert [e.kind for e in plan.effects] == ["add_column"]


def test_last_option(cols):
    one = [Column(key=K_NEW, name="Solo", type="enum", options=("only",))]
    with pytest.raises(SchemaOperationError) as info:
        run(one, [{"op": "remove_enum_option", "name": "Solo", "option": "only"}])
    assert (info.value.code, info.value.op_index) == ("last_option", 0)


def test_last_column(cols):
    with pytest.raises(SchemaOperationError) as info:
        run(cols[:1], [{"op": "drop_column", "name": "Name"}])
    assert (info.value.code, info.value.op_index) == ("last_column", 0)


def test_last_column_after_drops_in_same_batch(cols):
    ops = [{"op": "drop_column", "name": n} for n in NAMES]
    with pytest.raises(SchemaOperationError) as info:
        run(cols, ops)
    assert (info.value.code, info.value.op_index) == ("last_column", 5)


def test_too_many_columns(cols):
    specs = [{"name": f"c{i}", "type": "text"} for i in range(TABLE_MAX_COLUMNS)]
    full = parse_columns(specs)
    with pytest.raises(SchemaOperationError) as info:
        run(full, [{"op": "add_column", "column": {"name": "one more", "type": "text"}}])
    assert (info.value.code, info.value.op_index) == ("too_many_columns", 0)


def test_too_many_columns_within_batch(cols):
    room = TABLE_MAX_COLUMNS - len(cols)
    ops = [{"op": "add_column", "column": {"name": f"n{i}", "type": "text"}}
           for i in range(room + 1)]
    with pytest.raises(SchemaOperationError) as info:
        run(cols, ops)
    assert (info.value.code, info.value.op_index) == ("too_many_columns", room)


def test_too_many_enum_options(cols):
    from chat.tables.constants import TABLE_ENUM_MAX_OPTIONS

    full = [Column(key=K_NEW, name="E", type="enum",
                   options=tuple(f"o{i}" for i in range(TABLE_ENUM_MAX_OPTIONS)))]
    with pytest.raises(SchemaOperationError) as info:
        run(full, [{"op": "add_enum_option", "name": "E", "option": "extra"}])
    assert info.value.code == "invalid_options"


@pytest.mark.parametrize(
    "ops, code",
    [
        pytest.param([], "no_operations", id="empty"),
        pytest.param({"op": "rename_table", "title": "x"}, "invalid_operations",
                     id="not-a-list"),
        pytest.param(None, "invalid_operations", id="none"),
        pytest.param([{"op": "rename_table", "title": "x"}]
                     * (TABLE_SCHEMA_MAX_OPERATIONS + 1), "too_many_operations",
                     id="too-many"),
    ],
)
def test_list_level_refusals(cols, ops, code):
    with pytest.raises(SchemaOperationError) as info:
        run(cols, ops)
    assert info.value.code == code
    assert info.value.op_index is None


def test_max_operations_accepted(cols):
    ops = [{"op": "rename_table", "title": f"t{i}"} for i in range(TABLE_SCHEMA_MAX_OPERATIONS)]
    assert run(cols, ops).new_title == f"t{TABLE_SCHEMA_MAX_OPERATIONS - 1}"


# --- Multi-op batches --------------------------------------------------------


def test_add_rename_reorder(cols):
    ops = [
        {"op": "add_column", "column": {"name": "Qty", "type": "number"}},
        {"op": "rename_column", "name": "Qty", "new_name": "Quantity"},
        {"op": "reorder_columns", "names": ["Quantity"] + NAMES},
    ]
    plan = run(cols, ops)
    assert [c.name for c in plan.columns] == ["Quantity"] + NAMES
    assert effects(plan) == [{"kind": "add_column", "column_key": K_NEW}]
    assert plan.operations[1] == {"op": "rename_column", "name": "Qty",
                                  "new_name": "Quantity", "key": K_NEW}
    assert plan.operations[2]["keys"][0] == K_NEW


def test_rename_then_refer_by_new_name(cols):
    ops = [
        {"op": "rename_column", "name": "Link", "new_name": "Website"},
        {"op": "set_unique", "name": "Website", "unique": True},
    ]
    plan = run(cols, ops)
    assert by_key(plan.columns, K_LINK).unique
    assert plan.operations[1]["key"] == K_LINK
    with pytest.raises(SchemaOperationError) as info:
        run(cols, [ops[0], {"op": "set_unique", "name": "Link", "unique": True}])
    assert (info.value.code, info.value.op_index) == ("unknown_column", 1)


def test_rename_frees_old_name(cols):
    ops = [
        {"op": "rename_column", "name": "Link", "new_name": "Website"},
        {"op": "add_column", "column": {"name": "link", "type": "text"}},
    ]
    plan = run(cols, ops)
    assert [c.name for c in plan.columns][-2:] == ["Tags", "link"]


def test_duplicate_name_after_add_in_batch(cols):
    ops = [
        {"op": "add_column", "column": {"name": "Qty", "type": "number"}},
        {"op": "add_column", "column": {"name": "QTY", "type": "text"}},
    ]
    with pytest.raises(SchemaOperationError) as info:
        run(cols, ops)
    assert (info.value.code, info.value.op_index) == ("duplicate_name", 1)


def test_drop_column_added_in_same_batch(cols):
    ops = [
        {"op": "add_column", "column": {"name": "Tmp", "type": "text",
                                         "unique": True}},
        {"op": "drop_column", "name": "Tmp"},
    ]
    plan = run(cols, ops)
    assert plan.columns == cols
    # The unique check on a column the batch drops again is moot; the DDL
    # pairs stay.
    assert effects(plan) == [
        {"kind": "add_column", "column_key": K_NEW},
        {"kind": "create_unique_index", "column_key": K_NEW},
        {"kind": "capture_dropped", "column_key": K_NEW},
        {"kind": "drop_unique_index", "column_key": K_NEW},
        {"kind": "drop_column", "column_key": K_NEW},
    ]


def test_dropped_key_not_reused_in_batch(cols, monkeypatch):
    seen = []

    def recording(existing_keys):
        seen.append(set(existing_keys))
        return new_column_key(existing_keys)

    monkeypatch.setattr(schema_ops, "new_column_key", recording)
    ops = [
        {"op": "drop_column", "name": "Link"},
        {"op": "add_column", "column": {"name": "Link", "type": "url"}},
    ]
    plan = run(cols, ops)
    assert K_LINK in seen[0]
    assert find_name(plan.columns, "Link").key != K_LINK


def test_rename_table_twice_last_wins(cols):
    plan = run(cols, [{"op": "rename_table", "title": "A"},
                      {"op": "rename_table", "title": "B"}])
    assert plan.new_title == "B"
    assert plan.operations == [{"op": "rename_table", "title": "A"},
                               {"op": "rename_table", "title": "B"}]


def test_enum_ops_chain(cols):
    ops = [
        {"op": "add_enum_option", "name": "Status", "option": "Blocked", "after": "Doing"},
        {"op": "rename_enum_option", "name": "Status", "option": "Blocked",
         "new_option": "Stuck"},
        {"op": "set_default", "name": "Status", "default": "Stuck"},
        {"op": "set_enum_multiple", "name": "Status", "multiple": True},
        {"op": "remove_enum_option", "name": "Status", "option": "Todo"},
    ]
    plan = run(cols, ops)
    status = by_key(plan.columns, K_STATUS)
    assert status.options == ("Doing", "Stuck", "Done")
    assert status.multiple and status.default == ("Stuck",)
    assert effects(plan) == [
        {"kind": "enum_rewrite", "column_key": K_STATUS, "old_option": "Blocked",
         "new_option": "Stuck", "multiple": False},
        {"kind": "wrap_multi", "column_key": K_STATUS},
        {"kind": "violation_check", "check": "enum_option", "column_key": K_STATUS,
         "option": "Todo", "multiple": True},
    ]


def test_later_failure_leaves_inputs_unchanged(cols):
    snapshot = copy.deepcopy(cols)
    ops = [
        {"op": "add_column", "column": {"name": "Qty", "type": "number"}},
        {"op": "rename_column", "name": "Link", "new_name": "Website"},
        {"op": "drop_column", "name": "Name"},
        {"op": "set_precision", "name": "Website", "precision": 1},
    ]
    ops_snapshot = copy.deepcopy(ops)
    with pytest.raises(SchemaOperationError) as info:
        run(cols, ops)
    assert (info.value.code, info.value.op_index, info.value.op) == (
        "wrong_column_type", 3, "set_precision")
    assert cols == snapshot
    assert ops == ops_snapshot


def test_inputs_never_mutated_on_success(cols):
    snapshot = copy.deepcopy(cols)
    ops = [
        {"op": "add_column", "column": {"name": "Qty", "type": "number", "default": "1.50"}},
        {"op": "rename_enum_option", "name": "Tags", "option": "red", "new_option": "rose"},
        {"op": "set_enum_multiple", "name": "Status", "multiple": True},
        {"op": "set_default", "name": "Tags", "default": ["green"]},
        {"op": "reorder_columns", "names": list(reversed(NAMES)) + ["Qty"]},
    ]
    ops_snapshot = copy.deepcopy(ops)
    columns_tuple = tuple(cols)
    plan = run(columns_tuple, ops)
    assert list(columns_tuple) == snapshot
    assert ops == ops_snapshot
    assert "key" not in ops[0]["column"]
    assert plan.columns is not cols
    # Mutating the plan's normalized ops does not reach the columns.
    plan.operations[0]["column"]["name"] = "changed"
    assert plan.columns[-1].name == "Qty"


# --- schema_diff -------------------------------------------------------------


def entry(diff, key):
    return next(e for e in diff["columns"] if e["key"] == key)


def test_diff_identical(cols):
    diff = schema_diff(cols, list(cols))
    assert diff["changed"] is False
    assert diff["order_changed"] is False
    assert [e["status"] for e in diff["columns"]] == ["unchanged"] * 6
    assert all(e["changes"] == {} for e in diff["columns"])
    assert diff["before_order"] == diff["after_order"] == NAMES
    json.dumps(diff)


def test_diff_add(cols):
    new = Column(key=K_NEW, name="Qty", type="number")
    diff = schema_diff(cols, cols[:2] + [new] + cols[2:])
    assert [e["key"] for e in diff["columns"]][:3] == [K_NAME, K_PRICE, K_NEW]
    e = entry(diff, K_NEW)
    assert e["status"] == "added" and e["before"] is None
    assert e["after"] == new.to_dict()
    assert e["changes"] == {} and e["options_added"] == []
    assert diff["changed"] and not diff["order_changed"]


@pytest.mark.parametrize(
    "drop, expected_keys",
    [
        pytest.param([K_NAME], [K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS, K_TAGS],
                     id="first-goes-first"),
        pytest.param([K_LINK], [K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS, K_TAGS],
                     id="middle-after-predecessor"),
        pytest.param([K_TAGS], [K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS, K_TAGS],
                     id="last"),
        pytest.param([K_NAME, K_PRICE], [K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS, K_TAGS],
                     id="two-at-start"),
        pytest.param([K_LINK, K_DONE], [K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS, K_TAGS],
                     id="two-in-middle"),
    ],
)
def test_diff_drop_position(cols, drop, expected_keys):
    after = [c for c in cols if c.key not in drop]
    diff = schema_diff(cols, after)
    assert [e["key"] for e in diff["columns"]] == expected_keys
    for key in drop:
        e = entry(diff, key)
        assert e["status"] == "dropped" and e["after"] is None
        assert e["before"] == by_key(cols, key).to_dict()
    assert not diff["order_changed"]
    assert diff["changed"]


def test_diff_drop_position_after_reorder(cols):
    # after order: Status, Name, Price, Done, Tags; Link (dropped) followed Price.
    order = [K_STATUS, K_NAME, K_PRICE, K_DONE, K_TAGS]
    after = [by_key(cols, k) for k in order]
    diff = schema_diff(cols, after)
    assert [e["key"] for e in diff["columns"]] == [
        K_STATUS, K_NAME, K_PRICE, K_LINK, K_DONE, K_TAGS]
    assert diff["order_changed"]


def test_diff_rename(cols):
    after = [dataclasses.replace(c, name="Cost") if c.key == K_PRICE else c for c in cols]
    diff = schema_diff(cols, after)
    e = entry(diff, K_PRICE)
    assert e["status"] == "renamed"
    assert e["changes"] == {"name": {"before": "Price", "after": "Cost"}}
    assert {x["status"] for x in diff["columns"] if x["key"] != K_PRICE} == {"unchanged"}
    assert len(diff["columns"]) == 6


def test_diff_rename_with_other_change(cols):
    after = [dataclasses.replace(c, name="Cost", precision=0, default="1")
             if c.key == K_PRICE else c for c in cols]
    e = entry(schema_diff(cols, after), K_PRICE)
    assert e["status"] == "renamed"
    assert set(e["changes"]) == {"name", "precision", "default"}


@pytest.mark.parametrize(
    "key, changes",
    [
        pytest.param(K_LINK, {"required": True}, id="required"),
        pytest.param(K_NAME, {"unique": False}, id="unique"),
        pytest.param(K_STATUS, {"default": "Done"}, id="default"),
        pytest.param(K_PRICE, {"precision": 3}, id="precision"),
        pytest.param(K_DONE, {"description": "Finished?"}, id="description"),
        pytest.param(K_STATUS, {"multiple": True, "default": ["Todo"]}, id="multiple"),
    ],
)
def test_diff_attribute_change(cols, key, changes):
    after = [dataclasses.replace(c, **changes) if c.key == key else c for c in cols]
    diff = schema_diff(cols, after)
    e = entry(diff, key)
    assert e["status"] == "changed"
    before = by_key(cols, key).to_dict()
    assert e["changes"] == {f: {"before": before[f], "after": v} for f, v in changes.items()}
    assert diff["changed"] and not diff["order_changed"]


def test_diff_enum_options(cols):
    after = [dataclasses.replace(c, options=("Todo", "Blocked", "Done", "Archived"))
             if c.key == K_STATUS else c for c in cols]
    e = entry(schema_diff(cols, after), K_STATUS)
    assert e["status"] == "changed"
    assert e["options_added"] == ["Blocked", "Archived"]
    assert e["options_removed"] == ["Doing"]
    assert e["changes"]["options"] == {"before": ["Todo", "Doing", "Done"],
                                       "after": ["Todo", "Blocked", "Done", "Archived"]}
    assert entry(schema_diff(cols, after), K_TAGS)["options_added"] == []


def test_diff_reorder_only(cols):
    after = list(reversed(cols))
    diff = schema_diff(cols, after)
    assert diff["order_changed"] is True
    assert diff["changed"] is True
    assert [e["status"] for e in diff["columns"]] == ["unchanged"] * 6
    assert [e["key"] for e in diff["columns"]] == [c.key for c in after]
    assert diff["after_order"] == list(reversed(NAMES))


def test_diff_combined_from_plan(cols):
    ops = [
        {"op": "add_column", "after": "Name",
         "column": {"name": "Qty", "type": "number", "default": 0}},
        {"op": "rename_column", "name": "Link", "new_name": "Website"},
        {"op": "drop_column", "name": "Done"},
        {"op": "set_required", "name": "Website", "required": True},
        {"op": "rename_enum_option", "name": "Tags", "option": "green",
         "new_option": "lime"},
        {"op": "set_description", "name": "Price", "description": "USD"},
        {"op": "reorder_columns",
         "names": ["Name", "Qty", "Status", "Price", "Website", "Tags"]},
    ]
    plan = run(cols, ops)
    diff = schema_diff(cols, plan.columns)
    json.dumps(diff)
    assert [(e["key"], e["status"]) for e in diff["columns"]] == [
        (K_NAME, "unchanged"),
        (K_NEW, "added"),
        (K_STATUS, "unchanged"),
        (K_PRICE, "changed"),
        (K_LINK, "renamed"),
        (K_DONE, "dropped"),
        (K_TAGS, "changed"),
    ]
    assert entry(diff, K_LINK)["changes"] == {
        "name": {"before": "Link", "after": "Website"},
        "required": {"before": False, "after": True},
    }
    assert entry(diff, K_TAGS)["options_added"] == ["lime"]
    assert entry(diff, K_TAGS)["options_removed"] == ["green"]
    assert diff["order_changed"] is True
    assert diff["changed"] is True
    assert diff["before_order"] == NAMES
    assert diff["after_order"] == ["Name", "Qty", "Status", "Price", "Website", "Tags"]


# --- Multi-enum defaults: tuple inside the column, lists outside ---------------


def test_multi_default_backfill_and_ops_are_lists(cols):
    plan = run(cols, [
        {"op": "add_column", "column": {
            "name": "Labels", "type": "enum", "options": ["x", "y"],
            "multiple": True, "required": True, "default": ["y", "x"]}},
        {"op": "set_default", "name": "Tags", "default": ["green"]},
    ])
    backfill = next(e for e in plan.effects if isinstance(e, Backfill))
    assert backfill.value == ["x", "y"] and isinstance(backfill.value, list)
    assert plan.operations[0]["column"]["default"] == ["x", "y"]
    assert plan.operations[1]["default"] == ["green"]
    assert by_key(plan.columns, K_NEW).default == ("x", "y")
    for column in plan.columns:
        hash(column)
    json.dumps(plan.operations)


def test_multi_default_rewrite_and_wrap_keep_columns_hashable(cols):
    plan = run(cols, [
        {"op": "rename_enum_option", "name": "Tags", "option": "red", "new_option": "rose"},
        {"op": "set_enum_multiple", "name": "Status", "multiple": True},
    ])
    tags, status = by_key(plan.columns, K_TAGS), by_key(plan.columns, K_STATUS)
    assert tags.default == ("rose", "blue")
    assert status.default == ("Todo",)
    assert {tags, status}


def test_diff_multi_default_change_is_json_lists(cols):
    plan = run(cols, [{"op": "set_default", "name": "Tags", "default": ["green"]}])
    diff = schema_diff(cols, plan.columns)
    change = entry(diff, K_TAGS)["changes"]["default"]
    assert change == {"before": ["red", "blue"], "after": ["green"]}
    assert type(change["before"]) is list and type(change["after"]) is list
    json.dumps(diff)


# --- Odd input shapes never raise anything but SchemaOperationError -----------

ODD_VALUES = [
    pytest.param([], id="empty-list"),
    pytest.param(["a"], id="list"),
    pytest.param([["a"]], id="nested-list"),
    pytest.param([{}], id="list-of-dict"),
    pytest.param({}, id="empty-dict"),
    pytest.param({"a": 1}, id="dict"),
    pytest.param(("a",), id="tuple"),
    pytest.param(None, id="none"),
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param(10**400, id="huge-int"),
    pytest.param(1.5, id="float"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param("", id="empty-string"),
    pytest.param("x" * 100_000, id="huge-string"),
    pytest.param("\ud800", id="surrogate"),
    pytest.param("a\x00b", id="nul"),
]

# A valid instance of every operation; each param is swapped for odd values.
BASE_OPS = {
    "add_column": {"column": {"name": "X", "type": "text"}, "after": "Name"},
    "rename_column": {"name": "Link", "new_name": "Web"},
    "drop_column": {"name": "Link"},
    "reorder_columns": {"names": NAMES},
    "set_description": {"name": "Link", "description": "d"},
    "set_required": {"name": "Link", "required": True},
    "set_unique": {"name": "Link", "unique": True},
    "set_default": {"name": "Status", "default": "Doing"},
    "set_precision": {"name": "Price", "precision": 1},
    "add_enum_option": {"name": "Status", "option": "X", "after": "Todo"},
    "rename_enum_option": {"name": "Status", "option": "Todo", "new_option": "Open"},
    "remove_enum_option": {"name": "Status", "option": "Doing"},
    "set_enum_multiple": {"name": "Status", "multiple": True},
    "rename_table": {"title": "T"},
}

FUZZ_TARGETS = [
    pytest.param(op, field, id=f"{op}.{field}")
    for op, params in BASE_OPS.items()
    for field in params
] + [
    pytest.param("add_column", f"column.{field}", id=f"add_column.column.{field}")
    for field in ("name", "type", "required", "unique", "default", "options",
                  "multiple", "precision", "description", "key")
]


def test_base_ops_are_valid(cols):
    for op, params in BASE_OPS.items():
        run(cols, [{"op": op, **params}])


def _assert_only_operation_errors(columns, ops, table_empty=False):
    try:
        run(columns, ops, table_empty)
    except SchemaOperationError as exc:
        assert len(exc.message) < 1000
    # Anything else propagates and fails the test.


@pytest.mark.parametrize("value", ODD_VALUES)
@pytest.mark.parametrize("op, field", FUZZ_TARGETS)
def test_odd_param_values(cols, op, field, value):
    params = copy.deepcopy(BASE_OPS[op])
    if field.startswith("column."):
        spec_field = field.split(".", 1)[1]
        for spec_type in ("text", "number", "enum"):
            spec = {"name": "X", "type": spec_type}
            if spec_type == "enum":
                spec["options"] = ["A", "B"]
            spec[spec_field] = value
            for empty in (False, True):
                _assert_only_operation_errors(
                    cols, [{"op": op, **params, "column": spec}], empty)
    else:
        params[field] = value
        _assert_only_operation_errors(cols, [{"op": op, **params}])
        _assert_only_operation_errors(cols, [{"op": op, **params}], True)


@pytest.mark.parametrize("value", ODD_VALUES)
def test_odd_values_as_list_elements(cols, value):
    _assert_only_operation_errors(
        cols, [{"op": "reorder_columns", "names": NAMES[:-1] + [value]}])
    _assert_only_operation_errors(
        cols, [{"op": "set_default", "name": "Tags", "default": ["red", value]}])
    _assert_only_operation_errors(
        cols, [{"op": "add_column", "column": {"name": "E", "type": "enum",
                                               "options": ["A", value]}}])


@pytest.mark.parametrize("value", ODD_VALUES)
def test_odd_values_as_op_and_spec(cols, value):
    _assert_only_operation_errors(cols, [{"op": value}])
    _assert_only_operation_errors(cols, [value])
    _assert_only_operation_errors(cols, [{"op": "add_column", "column": value}])
    _assert_only_operation_errors(cols, value if isinstance(value, list) else [value])


@pytest.mark.parametrize(
    "field", [5, None, ("a",), 1.5, 10**5000, "f" * 100_000],
    ids=["int", "none", "tuple", "float", "huge-int", "huge-string"],
)
def test_odd_field_names(cols, field):
    _assert_only_operation_errors(cols, [{"op": "drop_column", "name": "Link", field: 1}])
    _assert_only_operation_errors(
        cols, [{"op": "add_column", "column": {"name": "X", "type": "text", field: 1}}])


@pytest.mark.parametrize(
    "op",
    [
        pytest.param({"op": "drop_column", "name": "n" * 100_000}, id="unknown-column"),
        pytest.param({"op": "f" * 100_000}, id="unknown-operation"),
        pytest.param({"op": "remove_enum_option", "name": "Status", "option": "o" * 100_000},
                     id="unknown-option"),
        pytest.param({"op": "rename_column", "name": "Link", "new_name": "n" * 100_000},
                     id="rename-too-long"),
        pytest.param({"op": "add_column", "column": {"name": "n" * 100_000, "type": "text"}},
                     id="add-too-long"),
        pytest.param({"op": "drop_column", "name": "Link", "f" * 100_000: 1},
                     id="unknown-field"),
        pytest.param({"op": "set_default", "name": "Status", "default": "d" * 100_000},
                     id="default-unknown-option"),
    ],
)
def test_error_messages_are_bounded(cols, op):
    with pytest.raises(SchemaOperationError) as info:
        run(cols, [op])
    assert len(info.value.message) < 500
    assert "…" in info.value.message


# --- Effects the rest of the batch makes moot ---------------------------------


def test_required_check_dropped_with_column(cols):
    plan = run(cols, [{"op": "set_required", "name": "Link", "required": True},
                      {"op": "drop_column", "name": "Link"}])
    assert plan.effects == [CaptureDroppedValues(K_LINK), DropColumnDDL(K_LINK)]


def test_required_check_dropped_when_unset_again(cols):
    plan = run(cols, [{"op": "set_required", "name": "Link", "required": True},
                      {"op": "set_required", "name": "Link", "required": False}])
    assert plan.effects == []


def test_required_check_kept_through_rename(cols):
    plan = run(cols, [{"op": "set_required", "name": "Link", "required": True},
                      {"op": "rename_column", "name": "Link", "new_name": "Web"}])
    assert plan.effects == [ViolationCheck("required", K_LINK)]


def test_unique_check_dropped_when_unset_again(cols):
    plan = run(cols, [{"op": "set_unique", "name": "Link", "unique": True},
                      {"op": "set_unique", "name": "Link", "unique": False}])
    assert plan.effects == [CreateUniqueIndex(K_LINK), DropUniqueIndex(K_LINK)]


def test_add_unique_then_drop(cols):
    plan = run(cols, [{"op": "add_column", "column": {"name": "Tmp", "type": "text",
                                                      "unique": True}},
                      {"op": "drop_column", "name": "Tmp"}])
    assert not any(isinstance(e, ViolationCheck) for e in plan.effects)
    assert plan.effects == [AddColumnDDL(K_NEW), CreateUniqueIndex(K_NEW),
                            CaptureDroppedValues(K_NEW), DropUniqueIndex(K_NEW),
                            DropColumnDDL(K_NEW)]


def test_backfill_dropped_with_column(cols):
    plan = run(cols, [{"op": "add_column", "column": {"name": "Must", "type": "text",
                                                      "required": True, "default": "x"}},
                      {"op": "drop_column", "name": "Must"}])
    assert plan.effects == [AddColumnDDL(K_NEW), CaptureDroppedValues(K_NEW),
                            DropColumnDDL(K_NEW)]


def test_enum_option_check_dropped_with_column(cols):
    plan = run(cols, [{"op": "remove_enum_option", "name": "Status", "option": "Doing"},
                      {"op": "drop_column", "name": "Status"}])
    assert plan.effects == [CaptureDroppedValues(K_STATUS), DropColumnDDL(K_STATUS)]


# --- Enum operation sequences -------------------------------------------------


def test_set_multiple_then_rename_option_rewrites_lists(cols):
    plan = run(cols, [{"op": "set_enum_multiple", "name": "Status", "multiple": True},
                      {"op": "rename_enum_option", "name": "Status", "option": "Todo",
                       "new_option": "Open"}])
    assert plan.effects == [WrapMulti(K_STATUS),
                            EnumRewrite(K_STATUS, "Todo", "Open", True)]
    assert by_key(plan.columns, K_STATUS).default == ("Open",)


def test_remove_option_then_rename_onto_its_name(cols):
    plan = run(cols, [{"op": "remove_enum_option", "name": "Status", "option": "Doing"},
                      {"op": "rename_enum_option", "name": "Status", "option": "Done",
                       "new_option": "Doing"}])
    assert plan.effects == [
        ViolationCheck("enum_option", K_STATUS, option="Doing", multiple=False),
        EnumRewrite(K_STATUS, "Done", "Doing", False),
    ]
    assert by_key(plan.columns, K_STATUS).options == ("Todo", "Doing")


def test_rename_option_then_remove_new_name(cols):
    plan = run(cols, [{"op": "rename_enum_option", "name": "Status", "option": "Done",
                       "new_option": "Finished"},
                      {"op": "remove_enum_option", "name": "Status", "option": "Finished"}])
    assert plan.effects == [
        EnumRewrite(K_STATUS, "Done", "Finished", False),
        ViolationCheck("enum_option", K_STATUS, option="Finished", multiple=False),
    ]
    assert by_key(plan.columns, K_STATUS).options == ("Todo", "Doing")


# --- More batches -------------------------------------------------------------


def test_drop_then_readd_same_name_gets_fresh_key(cols):
    plan = run(cols, [{"op": "drop_column", "name": "Link"},
                      {"op": "add_column", "column": {"name": "Link", "type": "url"},
                       "after": "Price"}])
    assert find_name(plan.columns, "Link").key == K_NEW
    assert plan.effects == [CaptureDroppedValues(K_LINK), DropColumnDDL(K_LINK),
                            AddColumnDDL(K_NEW)]
    diff = schema_diff(cols, plan.columns)
    assert entry(diff, K_LINK)["status"] == "dropped"
    assert entry(diff, K_NEW)["status"] == "added"
    assert [e["key"] for e in diff["columns"]] == [
        K_NAME, K_PRICE, K_LINK, K_NEW, K_DONE, K_STATUS, K_TAGS]
    assert not any(e["moved"] for e in diff["columns"])


def test_add_unique_required_default_on_empty_table(cols):
    plan = run(cols, [{"op": "add_column", "column": {
        "name": "Sku", "type": "text", "unique": True, "required": True,
        "default": "a"}}], table_empty=True)
    assert plan.effects == [AddColumnDDL(K_NEW), ViolationCheck("unique", K_NEW),
                            CreateUniqueIndex(K_NEW)]


def test_operations_and_effects_json_round_trip(cols):
    plan = run(cols, [
        {"op": "add_column", "after": "Name", "column": {
            "name": "Labels", "type": "enum", "options": ["x", "y"], "multiple": True,
            "required": True, "default": ["y", "x"], "description": "L"}},
        {"op": "rename_column", "name": "Link", "new_name": "Web"},
        {"op": "set_unique", "name": "Web", "unique": True},
        {"op": "set_required", "name": "Web", "required": True},
        {"op": "set_default", "name": "Tags", "default": ["green"]},
        {"op": "set_precision", "name": "Price", "precision": 1},
        {"op": "add_enum_option", "name": "Status", "option": "Blocked", "after": "Todo"},
        {"op": "rename_enum_option", "name": "Status", "option": "Blocked",
         "new_option": "Stuck"},
        {"op": "remove_enum_option", "name": "Status", "option": "Done"},
        {"op": "set_enum_multiple", "name": "Status", "multiple": True},
        {"op": "set_description", "name": "Done", "description": "flag"},
        {"op": "drop_column", "name": "Name"},
        {"op": "reorder_columns",
         "names": ["Labels", "Price", "Web", "Done", "Status", "Tags"]},
        {"op": "rename_table", "title": "T"},
    ])
    effect_dicts = [e.to_dict() for e in plan.effects]
    assert {d["kind"] for d in effect_dicts} >= {
        "add_column", "backfill", "violation_check", "create_unique_index",
        "enum_rewrite", "wrap_multi", "capture_dropped", "drop_unique_index",
        "drop_column"}
    assert json.loads(json.dumps(plan.operations)) == plan.operations
    assert json.loads(json.dumps(effect_dicts)) == effect_dicts


# --- schema_diff moved flag ---------------------------------------------------


def moved(diff):
    return {e["key"] for e in diff["columns"] if e["moved"]}


def test_diff_moved_none_when_identical(cols):
    diff = schema_diff(cols, list(cols))
    assert all(e["moved"] is False for e in diff["columns"])


def test_diff_moved_single_column(cols):
    after = [by_key(cols, K_TAGS)] + [c for c in cols if c.key != K_TAGS]
    assert moved(schema_diff(cols, after)) == {K_TAGS}
    after = [c for c in cols if c.key != K_NAME] + [by_key(cols, K_NAME)]
    assert moved(schema_diff(cols, after)) == {K_NAME}
    after = [by_key(cols, k) for k in (K_NAME, K_DONE, K_PRICE, K_LINK, K_STATUS, K_TAGS)]
    assert moved(schema_diff(cols, after)) == {K_DONE}


def test_diff_moved_swap_and_reverse_are_deterministic(cols):
    after = [by_key(cols, k) for k in (K_PRICE, K_NAME, K_LINK, K_DONE, K_STATUS, K_TAGS)]
    assert moved(schema_diff(cols, after)) == {K_NAME}
    reverse = schema_diff(cols, list(reversed(cols)))
    assert moved(reverse) == {K_NAME, K_PRICE, K_LINK, K_DONE, K_STATUS}
    assert moved(schema_diff(cols, list(reversed(cols)))) == moved(reverse)


def test_diff_adds_and_drops_move_nothing(cols):
    new = Column(key=K_NEW, name="Qty", type="number")
    after = [new] + [c for c in cols if c.key != K_LINK]
    diff = schema_diff(cols, after)
    assert moved(diff) == set()
    assert entry(diff, K_NEW)["moved"] is False
    assert entry(diff, K_LINK)["moved"] is False


def test_diff_renamed_and_moved(cols):
    plan = run(cols, [{"op": "rename_column", "name": "Link", "new_name": "Web"},
                      {"op": "reorder_columns",
                       "names": ["Web", "Name", "Price", "Done", "Status", "Tags"]}])
    diff = schema_diff(cols, plan.columns)
    e = entry(diff, K_LINK)
    assert (e["status"], e["moved"]) == ("renamed", True)
    assert moved(diff) == {K_LINK}
    assert diff["order_changed"] is True
    json.dumps(diff)


def test_diff_entry_keys(cols):
    plan = run(cols, [{"op": "drop_column", "name": "Link"}])
    for e in schema_diff(cols, plan.columns)["columns"]:
        assert set(e) == {"key", "status", "moved", "before", "after", "changes",
                          "options_added", "options_removed"}
