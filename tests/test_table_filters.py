"""Quest Tables filter DSL, sort and paged reads (chat/tables/filters.py)."""

from datetime import datetime, timedelta, timezone

import pytest

import chat.tables.filters as filters_mod
from chat.tables import storage
from chat.tables.constants import TABLE_GRID_MAX_LIMIT, TABLE_QUERY_MAX_ROWS
from chat.tables.filters import (
    And,
    FilterError,
    Leaf,
    Not,
    Or,
    QueryResult,
    SortKey,
    allowed_ops,
    build_count,
    build_select,
    check_page,
    compile_filter,
    parse_filter,
    parse_sort,
    resolve_columns,
    run_query,
    to_order_by,
    to_sql,
)
from chat.tables.schema import Column, parse_columns

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
SOURCE = "conversation:abc"

K_NAME = "c_00000001"
K_PRICE = "c_00000002"
K_SCORE = "c_00000003"
K_SITE = "c_00000004"
K_DONE = "c_00000005"
K_STATUS = "c_00000006"
K_TAGS = "c_00000007"


def make_schema() -> list[Column]:
    return parse_columns(
        [
            {"key": K_NAME, "name": "Name", "type": "text"},
            {"key": K_PRICE, "name": "Price", "type": "number"},
            {"key": K_SCORE, "name": "Score", "type": "number", "precision": 1},
            {"key": K_SITE, "name": "Site", "type": "url"},
            {"key": K_DONE, "name": "Done", "type": "boolean"},
            {
                "key": K_STATUS,
                "name": "Status",
                "type": "enum",
                "options": ["Open", "Closed", "Waiting"],
            },
            {
                "key": K_TAGS,
                "name": "Tags",
                "type": "enum",
                "options": ["a", "b", "c"],
                "multiple": True,
            },
        ],
        allow_key=True,
    )


SCHEMA = make_schema()
ALL_IDS = set(range(1, 9))

# Row i is _id i. Stored forms in comments where canonicalization matters.
ROWS = [
    {  # 1
        "Name": "Apple pie",
        "Price": 10,
        "Score": 10,
        "Site": "https://example.com/a",
        "Done": True,
        "Status": "Open",
        "Tags": ["a", "b"],
    },
    {  # 2
        "Name": "banana split",
        "Price": "9",
        "Score": "10.1",
        "Site": "http://foo.org/x_y",
        "Done": False,
        "Status": "Closed",
        "Tags": ["b"],
    },
    {  # 3
        "Name": "50% off",
        "Price": "-3.5",
        "Score": 9.94,  # "9.9"
        "Site": "https://example.com/100%25",
        "Done": True,
        "Status": "Waiting",
        "Tags": ["c", "a", "b"],  # ["a","b","c"]
    },
    {  # 4: nulls everywhere but Name and Price
        "Name": "under_score",
        "Price": 100,
        "Score": None,
        "Site": None,
        "Done": None,
        "Status": None,
        "Tags": None,
    },
    {  # 5
        "Name": "back\\slash",
        "Price": None,
        "Score": 0,
        "Site": "https://EXAMPLE.org/b",
        "Done": False,
        "Status": "Open",
        "Tags": ["c"],
    },
    {  # 6
        "Name": "Élan",  # Élan
        "Price": 0.25,
        "Score": -1,
        "Site": "https://x.io",
        "Done": None,
        "Status": "Closed",
        "Tags": ["a", "c"],
    },
    {  # 7
        "Name": None,
        "Price": 10.0,  # "10"
        "Score": 10.04,  # "10"
        "Site": None,
        "Done": True,
        "Status": None,
        "Tags": [],  # NULL
    },
    {  # 8
        "Name": "élan vital",  # élan vital
        "Price": "1e3",  # "1000"
        "Score": 10.06,  # "10.1"
        "Site": "https://foo.org/apple",
        "Done": False,
        "Status": "Waiting",
        "Tags": ["b", "c"],
    },
]


def build_table(path, schema=SCHEMA, rows=ROWS):
    storage.create_table_db(
        path,
        table_id="t1",
        title="Things",
        schema=schema,
        source="ui:1",
        actor_user_id=1,
        now=NOW,
    )
    # One insert per row; _updated_at cycles through 3 stamps so its order
    # differs from _id order.
    for index, row in enumerate(rows, start=1):
        storage.insert_rows(
            path,
            schema,
            schema_version=1,
            source=SOURCE,
            actor_user_id=None,
            rows=[row],
            now=NOW + timedelta(seconds=index % 3),
        )
    return path


@pytest.fixture(scope="module")
def table_dir(tmp_path_factory):
    return build_table(tmp_path_factory.mktemp("filters") / "t1")


@pytest.fixture
def fresh_table(tmp_path):
    return build_table(tmp_path / "t1")


def ids_for(table_dir, flt) -> set[int]:
    where_sql, params = compile_filter(SCHEMA, flt)
    with storage.connect(table_dir) as conn:
        return {
            r[0]
            for r in conn.execute(f"SELECT _id FROM rows WHERE ({where_sql})", params)
        }


def leaf(column, op, *value):
    node = {"column": column, "op": op}
    if value:
        node["value"] = value[0]
    return node


def refused(flt, schema=SCHEMA) -> FilterError:
    with pytest.raises(FilterError) as info:
        parse_filter(schema, flt)
    return info.value


# --- Every op per type against a real table.db ---------------------------------

OP_CASES = [
    # text
    (leaf("Name", "eq", "Apple pie"), {1}),
    (leaf("Name", "ne", "Apple pie"), ALL_IDS - {1}),
    (leaf("Name", "in", ["Apple pie", "50% off", "nope"]), {1, 3}),
    (leaf("Name", "is_null"), {7}),
    (leaf("Name", "not_null"), ALL_IDS - {7}),
    (leaf("Name", "contains", "an"), {2, 6, 8}),
    (leaf("Name", "starts_with", "apple"), {1}),
    (leaf("Name", "ends_with", "SPLIT"), {2}),
    # url
    (leaf("Site", "eq", "https://x.io"), {6}),
    (leaf("Site", "ne", "https://x.io"), ALL_IDS - {6}),
    (leaf("Site", "in", ["https://x.io", "http://foo.org/x_y"]), {2, 6}),
    (leaf("Site", "is_null"), {4, 7}),
    (leaf("Site", "not_null"), ALL_IDS - {4, 7}),
    (leaf("Site", "contains", "example"), {1, 3, 5}),
    (leaf("Site", "starts_with", "http://"), {2}),
    (leaf("Site", "ends_with", "%25"), {3}),
    # number without precision: 10, 9, -3.5, 100, NULL, 0.25, 10, 1000
    (leaf("Price", "eq", 10), {1, 7}),
    (leaf("Price", "ne", 10), {2, 3, 4, 5, 6, 8}),
    (leaf("Price", "in", [9, "100", 9.0]), {2, 4}),
    (leaf("Price", "is_null"), {5}),
    (leaf("Price", "not_null"), ALL_IDS - {5}),
    (leaf("Price", "lt", 10), {2, 3, 6}),
    (leaf("Price", "lte", 10), {1, 2, 3, 6, 7}),
    (leaf("Price", "gt", 9), {1, 4, 7, 8}),
    (leaf("Price", "gte", 100), {4, 8}),
    (leaf("Price", "between", [0, 10]), {1, 2, 6, 7}),
    (leaf("Price", "between", [-3.5, 0.25]), {3, 6}),
    # number with precision 1: 10, 10.1, 9.9, NULL, 0, -1, 10, 10.1
    (leaf("Score", "eq", 10), {1, 7}),
    (leaf("Score", "ne", 10), {2, 3, 4, 5, 6, 8}),
    (leaf("Score", "in", ["10.1", 0]), {2, 5, 8}),
    (leaf("Score", "is_null"), {4}),
    (leaf("Score", "not_null"), ALL_IDS - {4}),
    (leaf("Score", "lt", 0), {6}),
    (leaf("Score", "lte", 0), {5, 6}),
    (leaf("Score", "gt", 10), {2, 8}),
    (leaf("Score", "gte", 10), {1, 2, 7, 8}),
    (leaf("Score", "between", [9.95, 10.05]), {1, 7}),
    # boolean: T, F, T, NULL, F, NULL, T, F
    (leaf("Done", "eq", True), {1, 3, 7}),
    (leaf("Done", "eq", False), {2, 5, 8}),
    (leaf("Done", "ne", True), {2, 4, 5, 6, 8}),
    (leaf("Done", "in", [True, False]), {1, 2, 3, 5, 7, 8}),
    (leaf("Done", "is_null"), {4, 6}),
    (leaf("Done", "not_null"), ALL_IDS - {4, 6}),
    # enum single: Open, Closed, Waiting, NULL, Open, Closed, NULL, Waiting
    (leaf("Status", "eq", "Open"), {1, 5}),
    (leaf("Status", "ne", "Open"), {2, 3, 4, 6, 7, 8}),
    (leaf("Status", "in", ["Open", "Waiting"]), {1, 3, 5, 8}),
    (leaf("Status", "is_null"), {4, 7}),
    (leaf("Status", "not_null"), {1, 2, 3, 5, 6, 8}),
    # enum multi: [a,b], [b], [a,b,c], NULL, [c], [a,c], NULL, [b,c]
    (leaf("Tags", "eq", ["b", "a"]), {1}),
    (leaf("Tags", "eq", ["a", "b", "b"]), {1}),
    (leaf("Tags", "ne", ["b"]), ALL_IDS - {2}),
    (leaf("Tags", "in", [["b"], ["c"], ["c"]]), {2, 5}),
    (leaf("Tags", "is_null"), {4, 7}),
    (leaf("Tags", "is_empty"), {4, 7}),
    (leaf("Tags", "not_null"), {1, 2, 3, 5, 6, 8}),
    (leaf("Tags", "contains", "a"), {1, 3, 6}),
    (leaf("Tags", "contains_any", ["a", "c"]), {1, 3, 5, 6, 8}),
    (leaf("Tags", "contains_all", ["a", "c"]), {3, 6}),
    (leaf("Tags", "contains_all", ["b"]), {1, 2, 3, 8}),
    (leaf("Tags", "contains_all", ["a", "a", "b"]), {1, 3}),
    # not: set complement, NULLs included
    ({"not": leaf("Status", "eq", "Open")}, {2, 3, 4, 6, 7, 8}),
    ({"not": leaf("Price", "lt", 10)}, {1, 4, 5, 7, 8}),
    ({"not": leaf("Tags", "contains", "a")}, {2, 4, 5, 7, 8}),
    ({"not": leaf("Name", "ne", "Apple pie")}, {1}),
    ({"not": leaf("Done", "is_null")}, ALL_IDS - {4, 6}),
    # and / or / nesting
    ({"and": [leaf("Done", "eq", True), leaf("Price", "gt", 9)]}, {1, 7}),
    ({"or": [leaf("Status", "eq", "Closed"), leaf("Tags", "contains", "a")]}, {1, 2, 3, 6}),
    (
        {
            "and": [
                {"or": [leaf("Status", "eq", "Open"), leaf("Status", "is_null")]},
                {"not": leaf("Price", "eq", 100)},
            ]
        },
        {1, 5, 7},
    ),
]


@pytest.mark.parametrize("flt,expected", OP_CASES, ids=[str(c[0]) for c in OP_CASES])
def test_ops_against_table(table_dir, flt, expected):
    assert ids_for(table_dir, flt) == expected


def test_every_allowed_op_is_covered():
    covered = set()

    def walk(node):
        if "column" in node:
            covered.add((node["column"], node["op"]))
        for key in ("and", "or"):
            for child in node.get(key, []):
                walk(child)
        if "not" in node:
            walk(node["not"])

    for flt, _ in OP_CASES:
        walk(flt)
    for column in SCHEMA:
        for op in allowed_ops(column):
            assert (column.name, op) in covered, (column.name, op)


def test_no_filter_is_everything(table_dir):
    assert to_sql(None) == ("1", [])
    assert parse_filter(SCHEMA, None) is None
    assert ids_for(table_dir, None) == ALL_IDS


# --- Value canonicalization ----------------------------------------------------


@pytest.mark.parametrize("value", ["10", 10, 10.0, "1e1", " 10.000 "])
def test_number_spellings_match_the_same_rows(table_dir, value):
    assert ids_for(table_dir, leaf("Price", "eq", value)) == {1, 7}
    tree = parse_filter(SCHEMA, leaf("Price", "eq", value))
    assert tree.params == ("10",)
    assert tree.value == "10"


def test_range_bound_keeps_its_precision(table_dir):
    # Rounding 10.05 to one decimal (half-even -> 10.0) would drop rows 1 and 7.
    assert ids_for(table_dir, leaf("Score", "lt", 10.05)) == {1, 3, 5, 6, 7}
    assert ids_for(table_dir, leaf("Score", "gt", 10.05)) == {2, 8}
    assert parse_filter(SCHEMA, leaf("Score", "lt", 10.05)).params == ("10.05",)


def test_eq_rounds_like_a_write(table_dir):
    tree = parse_filter(SCHEMA, leaf("Score", "eq", 10.04))
    assert tree.params == ("10",)
    assert ids_for(table_dir, leaf("Score", "eq", 10.04)) == {1, 7}
    assert ids_for(table_dir, leaf("Score", "eq", "10.06")) == {2, 8}


@pytest.mark.parametrize(
    "flt",
    [
        leaf("Done", "eq", "true"),
        leaf("Done", "eq", 1),
        leaf("Done", "in", [True, 0]),
        leaf("Status", "eq", "open"),
        leaf("Status", "in", ["Open", "Nope"]),
        leaf("Tags", "eq", ["a", "z"]),
        leaf("Tags", "eq", []),
        leaf("Tags", "eq", "a"),
        leaf("Tags", "contains", "z"),
        leaf("Tags", "contains", ["a"]),
        leaf("Tags", "contains_any", ["a", "z"]),
        leaf("Site", "eq", "not a url"),
        leaf("Site", "eq", "ftp://x.io"),
        leaf("Price", "eq", "ten"),
        leaf("Price", "eq", True),
        leaf("Price", "lt", "NaN"),
        leaf("Price", "lt", None),
        leaf("Name", "eq", 5),
        leaf("Name", "contains", ""),
        leaf("Name", "contains", 5),
        leaf("Name", "contains", "a\x00b"),
        leaf("Name", "contains", "\ud800"),
        leaf("Name", "eq", None),
        leaf("Price", "ne", None),
    ],
    ids=str,
)
def test_invalid_values(flt):
    err = refused(flt)
    assert err.code == "invalid_value"
    assert err.path.startswith("value")


def test_null_eq_hints_is_null():
    assert "is_null" in refused(leaf("Name", "eq", None)).message
    assert "is_empty" in refused(leaf("Tags", "eq", [])).message


def test_value_error_detail_kept():
    err = refused(leaf("Status", "eq", "Nope"))
    assert "not one of the options" in err.message


def test_in_element_path():
    err = refused({"and": [leaf("Status", "in", ["Open", "Nope"])]})
    assert err.path == "and[0].value[1]"


def test_url_substring_need_not_be_a_url(table_dir):
    assert ids_for(table_dir, leaf("Site", "contains", "foo")) == {2, 8}
    assert ids_for(table_dir, leaf("Site", "contains", "x_y")) == {2}


def test_substring_operand_is_normalized():
    tree = parse_filter(SCHEMA, leaf("Name", "contains", "é\r\nx"))
    assert tree.value == "é\nx"


def test_substring_length_caps(monkeypatch):
    long_url = "a" * 2049
    assert refused(leaf("Site", "contains", long_url)).code == "invalid_value"
    parse_filter(SCHEMA, leaf("Name", "contains", long_url))
    assert refused(leaf("Name", "contains", "a" * 10_001)).code == "invalid_value"


def test_in_dedupes_after_canonicalization():
    tree = parse_filter(SCHEMA, leaf("Price", "in", ["10", 10, 10.0, 9]))
    assert tree.params == ("10", "9")
    assert tree.value == ["10", "9"]
    tree = parse_filter(SCHEMA, leaf("Tags", "in", [["b", "a"], ["a", "b"]]))
    assert tree.params == ('["a","b"]',)


def test_in_bounds(monkeypatch):
    assert refused(leaf("Name", "in", [])).code == "invalid_value"
    assert refused(leaf("Name", "in", "x")).code == "invalid_value"
    monkeypatch.setattr(filters_mod, "TABLE_FILTER_IN_MAX_VALUES", 3)
    parse_filter(SCHEMA, leaf("Name", "in", ["a", "b", "c"]))
    err = refused(leaf("Name", "in", ["a", "b", "c", "d"]))
    assert (err.code, err.path) == ("too_many_values", "value")
    assert refused(leaf("Tags", "contains_any", ["a", "b", "c", "a"])).code == (
        "too_many_values"
    )
    assert refused(leaf("Tags", "contains_all", [])).code == "invalid_value"
    assert refused(leaf("Tags", "contains_any", [])).code == "invalid_value"


def test_contains_all_binds_deduplicated_count():
    tree = parse_filter(SCHEMA, leaf("Tags", "contains_all", ["c", "a", "c"]))
    assert tree.params == ("c", "a", 2)
    assert tree.value == ["c", "a"]


# --- LIKE escaping and case ----------------------------------------------------


@pytest.mark.parametrize(
    "op,value,expected",
    [
        ("contains", "%", {3}),
        ("contains", "_", {4}),
        ("contains", "\\", {5}),
        ("contains", "0% o", {3}),
        ("starts_with", "50%", {3}),
        ("ends_with", "_score", {4}),
        ("contains", "k\\s", {5}),
        ("contains", "APPLE", {1}),
        ("contains", "Pie", {1}),
        # SQLite LIKE folds ASCII only: É and é are different letters.
        ("contains", "É", {6}),
        ("starts_with", "é", {8}),
        ("starts_with", "ÉLAN", {6}),
    ],
)
def test_like_escaping_and_case(table_dir, op, value, expected):
    assert ids_for(table_dir, leaf("Name", op, value)) == expected


def test_like_pattern_shapes():
    def pattern(op, value):
        return parse_filter(SCHEMA, leaf("Name", op, value)).params[0]

    assert pattern("contains", "a%b_c\\d") == "%a\\%b\\_c\\\\d%"
    assert pattern("starts_with", "x") == "x%"
    assert pattern("ends_with", "x") == "%x"
    sql, _ = compile_filter(SCHEMA, leaf("Name", "contains", "x"))
    assert sql == f"\"{K_NAME}\" LIKE ? ESCAPE '\\'"


# --- Numbers via CAST ----------------------------------------------------------


@pytest.mark.parametrize("op", ["lt", "lte", "gt", "gte"])
def test_range_ops_use_cast(op):
    sql, params = compile_filter(SCHEMA, leaf("Price", op, 5))
    assert sql.startswith(f'CAST("{K_PRICE}" AS REAL) ')
    assert sql.endswith("CAST(? AS REAL)")
    assert params == ["5"]


def test_between_uses_cast():
    sql, params = compile_filter(SCHEMA, leaf("Price", "between", [1, 2]))
    assert sql == (
        f'CAST("{K_PRICE}" AS REAL) BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)'
    )
    assert params == ["1", "2"]


@pytest.mark.parametrize("op", ["eq", "ne", "in"])
def test_equality_ops_compare_strings(op):
    value = [10] if op == "in" else 10
    sql, _ = compile_filter(SCHEMA, leaf("Price", op, value))
    assert "CAST(" not in sql


def test_numeric_not_string_ordering(tmp_path):
    schema = parse_columns(
        [{"key": K_PRICE, "name": "N", "type": "number"}], allow_key=True
    )
    rows = [{"N": v} for v in ["9", "10", "-2", "-10", "0.5", "1.25"]]
    path = build_table(tmp_path / "nums", schema, rows)

    def ids(flt):
        sql, params = compile_filter(schema, flt)
        with storage.connect(path) as conn:
            return {r[0] for r in conn.execute(f"SELECT _id FROM rows WHERE {sql}", params)}

    assert ids(leaf("N", "gt", 9)) == {2}  # "10" < "9" as strings
    assert ids(leaf("N", "lt", -2)) == {4}
    assert ids(leaf("N", "between", [0.5, 1.25])) == {5, 6}
    result = run_query(path, schema, sort=[{"column": "N"}])
    assert [r["N"] for r in result.rows] == ["-10", "-2", "0.5", "1.25", "9", "10"]


def test_between_rules(table_dir):
    assert ids_for(table_dir, leaf("Price", "between", [9, 9])) == {2}
    assert ids_for(table_dir, leaf("Price", "between", ["-3.5", "9"])) == {2, 3, 6}
    for bad in ([10, 9], [1], [1, 2, 3], 5, "1,2", [None, 2], [1, "x"]):
        err = refused(leaf("Price", "between", bad))
        assert err.code == "invalid_value", bad
    assert refused(leaf("Price", "between", [1, "x"])).path == "value[1]"


# --- Rejections ----------------------------------------------------------------


@pytest.mark.parametrize(
    "flt,code,path",
    [
        (leaf("Nope", "eq", 1), "unknown_column", "column"),
        ({"and": [leaf("Nope", "eq", 1)]}, "unknown_column", "and[0].column"),
        (leaf("Name", "like", "x"), "unknown_op", "op"),
        (leaf("Name", "EQ", "x"), "unknown_op", "op"),
        (leaf("Status", "contains", "Open"), "op_not_allowed", "op"),
        (leaf("Name", "lt", "x"), "op_not_allowed", "op"),
        (leaf("Name", "is_empty"), "op_not_allowed", "op"),
        (leaf("Status", "contains_any", ["Open"]), "op_not_allowed", "op"),
        (leaf("Done", "contains", "x"), "op_not_allowed", "op"),
        (leaf("Price", "contains", "1"), "op_not_allowed", "op"),
        (leaf("Tags", "lt", 1), "op_not_allowed", "op"),
        (leaf("Tags", "starts_with", "a"), "op_not_allowed", "op"),
        (leaf("Site", "between", [1, 2]), "op_not_allowed", "op"),
        ({"column": "Name", "op": 5, "value": "x"}, "invalid_filter", "op"),
        ({"column": 5, "op": "eq", "value": "x"}, "invalid_filter", "column"),
        ({"column": "Name", "value": "x"}, "invalid_filter", "op"),
        ("Name = 1", "invalid_filter", ""),
        ([leaf("Name", "eq", "x")], "invalid_filter", ""),
        (5, "invalid_filter", ""),
        ({"column": "Name", "op": "eq", "value": "x", "x": 1}, "invalid_filter", ""),
        ({"and": [leaf("Name", "eq", "x")], "or": [leaf("Name", "eq", "x")]},
         "invalid_filter", ""),
        ({"and": [leaf("Name", "eq", "x")], "column": "Name"}, "invalid_filter", ""),
        ({"and": []}, "invalid_filter", "and"),
        ({"or": []}, "invalid_filter", "or"),
        ({"and": leaf("Name", "eq", "x")}, "invalid_filter", "and"),
        ({"not": [leaf("Name", "eq", "x")]}, "invalid_filter", "not"),
        ({"not": None}, "invalid_filter", "not"),
        ({"and": [{"not": "x"}]}, "invalid_filter", "and[0].not"),
        ({"AND": [leaf("Name", "eq", "x")]}, "invalid_filter", ""),
        (leaf("Name", "eq"), "invalid_value", "value"),
        (leaf("Name", "is_null", "x"), "invalid_value", "value"),
        (leaf("Tags", "is_empty", []), "invalid_value", "value"),
        ({"and": [{"not": leaf("Name", "eq", 1)}]}, "invalid_value", "and[0].not.value"),
    ],
    ids=str,
)
def test_rejections(flt, code, path):
    err = refused(flt)
    assert (err.code, err.path) == (code, path)
    assert str(err) == err.message


def test_null_value_allowed_on_nullary_ops(table_dir):
    assert ids_for(table_dir, leaf("Name", "is_null", None)) == {7}


def test_op_messages_name_allowed_ops():
    err = refused(leaf("Status", "contains", "Open"))
    assert "eq, ne, in, is_null, not_null" in err.message
    assert "single enum" in err.message
    err = refused(leaf("Name", "regex", "x"))
    assert "contains" in err.message and "'regex'" in err.message


def test_unknown_column_message_is_bounded():
    many = parse_columns(
        [{"name": f"col{i}", "type": "text"} for i in range(30)]
    )
    err = refused(leaf("x" * 5000, "eq", "y"), schema=many)
    assert err.code == "unknown_column"
    assert len(err.message) < 600
    assert "and 20 more" in err.message


def test_depth_limits():
    lf = leaf("Name", "eq", "x")
    parse_filter(SCHEMA, lf)
    parse_filter(SCHEMA, {"and": [{"or": [lf, lf]}]})
    parse_filter(SCHEMA, {"not": {"not": lf}})
    err = refused({"and": [{"or": [{"not": lf}]}]})
    assert (err.code, err.path) == ("too_deep", "and[0].or[0].not")
    err = refused({"not": {"not": {"not": lf}}})
    assert err.code == "too_deep"


def test_very_deep_input_refused_without_recursion_error():
    node = leaf("Name", "eq", "x")
    for i in range(20_000):
        node = {"not": node} if i % 2 else {"and": [node]}
    assert refused(node).code == "too_deep"


def test_leaf_limits():
    lf = leaf("Name", "eq", "x")
    parse_filter(SCHEMA, {"and": [lf] * 20})
    parse_filter(SCHEMA, {"and": [{"or": [lf] * 10}, {"or": [lf] * 10}]})
    err = refused({"and": [lf] * 21})
    assert (err.code, err.path) == ("too_many_leaves", "and")
    err = refused({"and": [{"or": [lf] * 10}, {"or": [lf] * 11}]})
    assert (err.code, err.path) == ("too_many_leaves", "and[1].or")


def test_huge_list_refused_before_walking():
    items = [leaf("Name", "eq", "x")] * 1_000_000
    err = refused({"and": items})
    assert err.code == "too_many_leaves"
    # Children past the cap are not even looked at.
    err = refused({"and": [leaf("Name", "eq", "x")] * 21 + [5]})
    assert err.code == "too_many_leaves"


def test_leaf_cap_is_patchable(monkeypatch):
    monkeypatch.setattr(filters_mod, "TABLE_FILTER_MAX_LEAVES", 2)
    monkeypatch.setattr(filters_mod, "TABLE_FILTER_MAX_DEPTH", 1)
    lf = leaf("Name", "eq", "x")
    parse_filter(SCHEMA, lf)
    assert refused({"not": lf}).code == "too_deep"


# --- Column lookup ---------------------------------------------------------------


def test_case_insensitive_column_match(table_dir):
    assert ids_for(table_dir, leaf("status", "eq", "Open")) == {1, 5}
    assert ids_for(table_dir, leaf("PRICE", "gt", 99)) == {4, 8}
    tree = parse_filter(SCHEMA, leaf("sTaTuS", "eq", "Open"))
    assert tree.column.name == "Status"


def test_exact_match_wins_over_casefold():
    # Names are unique case-insensitively, so this only checks the lookup order.
    tree = parse_filter(SCHEMA, leaf("Name", "eq", "x"))
    assert tree.column.key == K_NAME


@pytest.mark.parametrize("name", ["_id", "_created_at", "_updated_at", "_ID"])
def test_reserved_names_not_filterable_but_sortable(name):
    err = refused(leaf(name, "eq", 1))
    assert (err.code, err.path) == ("unknown_column", "column")
    (key,) = parse_sort(SCHEMA, [{"column": name}])
    assert key.type == "row_field"
    assert key.name == name.lower()


# --- Security ----------------------------------------------------------------------

HOSTILE_NAMES = [
    "a; DROP TABLE rows",
    "x --",
    "x' OR '1'='1",
    'x" OR 1=1 --',
    f'{K_NAME}',
    f'"{K_NAME}"',
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_column_names_refused(name):
    assert refused(leaf(name, "eq", "x")).code == "unknown_column"
    with pytest.raises(FilterError) as info:
        parse_sort(SCHEMA, [{"column": name}])
    assert (info.value.code, info.value.path) == ("unknown_column", "sort[0].column")
    with pytest.raises(FilterError) as info:
        resolve_columns(SCHEMA, ["Name", name])
    assert (info.value.code, info.value.path) == ("unknown_column", "columns[1]")


def test_column_names_never_reach_sql():
    names = ['we"ird', "it's", "100%", "a;b", "x -- y"]
    schema = parse_columns(
        [
            {"key": f"c_0000000{i}", "name": n, "type": "text"}
            for i, n in enumerate(names, start=1)
        ],
        allow_key=True,
    )
    flt = {"or": [leaf(n, "contains", "v") for n in names]}
    sql, params = compile_filter(schema, flt)
    order = to_order_by(parse_sort(schema, [{"column": n} for n in names[:3]]))
    select, _ = build_select(
        resolve_columns(schema, names), (sql, params), order, limit=1, offset=0
    )
    for text in (sql, order, select):
        for name in names:
            assert name not in text
    for i in range(1, len(names) + 1):
        assert storage._ident(f"c_0000000{i}") in sql


HOSTILE_VALUES = ["'; DROP TABLE rows; --", '" OR 1=1 --', "%' OR '1'='1"]


@pytest.mark.parametrize("value", HOSTILE_VALUES)
@pytest.mark.parametrize("op", ["eq", "ne", "in", "contains", "starts_with"])
def test_hostile_values_only_in_params(value, op):
    operand = [value] if op == "in" else value
    tree = parse_filter(SCHEMA, leaf("Name", op, operand))
    sql, params = to_sql(tree)
    assert value not in sql
    assert "DROP" not in sql and "OR 1" not in sql
    assert tree.value == operand
    if op in ("eq", "ne", "in"):
        assert params == [value]
    else:  # LIKE pattern: the value escaped, plus wildcards
        assert len(params) == 1 and value.replace("%", "\\%") in params[0]
    assert sql.count("?") == len(params)


def test_hostile_value_round_trip(fresh_table):
    evil = "'; DROP TABLE rows; --"
    storage.insert_rows(
        fresh_table,
        SCHEMA,
        schema_version=1,
        source=SOURCE,
        actor_user_id=None,
        rows=[{"Name": evil}, {"Name": '" OR 1=1 --'}],
    )
    assert run_query(fresh_table, SCHEMA, filter=leaf("Name", "eq", evil)).rows[0][
        "Name"
    ] == evil
    result = run_query(fresh_table, SCHEMA, filter=leaf("Name", "contains", "DROP TABLE"))
    assert [r["_id"] for r in result.rows] == [9]
    result = run_query(fresh_table, SCHEMA, filter=leaf("Name", "contains", "' OR"))
    assert result.total == 0

    deleted = storage.delete_rows(
        fresh_table,
        SCHEMA,
        schema_version=1,
        source=SOURCE,
        actor_user_id=None,
        where=compile_filter(SCHEMA, leaf("Name", "eq", evil)),
    )
    assert deleted.row_ids == [9]
    with storage.connect(fresh_table) as conn:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"rows", "changes", "meta"} <= tables
        assert conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 9


def test_filter_delete_matches_query(fresh_table):
    flt = {"and": [leaf("Done", "eq", False), leaf("Tags", "contains", "c")]}
    expected = {r["_id"] for r in run_query(fresh_table, SCHEMA, filter=flt).rows}
    assert expected == {5, 8}
    result = storage.delete_rows(
        fresh_table,
        SCHEMA,
        schema_version=1,
        source=SOURCE,
        actor_user_id=None,
        where=compile_filter(SCHEMA, flt),
    )
    assert set(result.row_ids) == expected
    assert run_query(fresh_table, SCHEMA).total == 6


def test_identifier_quoting_parity():
    for column in SCHEMA:
        sql, _ = compile_filter(SCHEMA, leaf(column.name, "is_null"))
        assert sql == f"{storage._ident(column.key)} IS NULL"
    assert filters_mod._ident is storage._ident


def test_tree_shape():
    tree = parse_filter(
        SCHEMA,
        {"and": [leaf("Done", "eq", True), {"or": [leaf("Name", "is_null")]}]},
    )
    assert isinstance(tree, And)
    first, second = tree.children
    assert isinstance(first, Leaf) and first.params == (1,) and first.value is True
    assert isinstance(second, Or) and isinstance(second.children[0], Leaf)
    sql, params = to_sql(tree)
    assert sql == f'("{K_DONE}" = ?) AND (("{K_NAME}" IS NULL))'
    assert params == [1]
    tree = parse_filter(SCHEMA, {"not": {"or": [leaf("Name", "is_null")]}})
    assert isinstance(tree, Not) and isinstance(tree.child, Or)
    assert to_sql(tree) == (f'NOT COALESCE((("{K_NAME}" IS NULL)), 0)', [])


# --- Sort --------------------------------------------------------------------------


def sorted_ids(table_dir, sort, **kw):
    return [r["_id"] for r in run_query(table_dir, SCHEMA, sort=sort, **kw).rows]


@pytest.mark.parametrize(
    "column,direction,expected",
    [
        ("Price", "asc", [3, 6, 2, 1, 7, 4, 8, 5]),
        ("Price", "desc", [8, 4, 1, 7, 2, 6, 3, 5]),
        ("Name", "asc", [3, 1, 5, 2, 4, 6, 8, 7]),
        ("Name", "desc", [8, 6, 4, 2, 5, 1, 3, 7]),
        ("Done", "asc", [2, 5, 8, 1, 3, 7, 4, 6]),
        ("Done", "desc", [1, 3, 7, 2, 5, 8, 4, 6]),
        ("Status", "asc", [2, 6, 1, 5, 3, 8, 4, 7]),
        ("Score", "desc", [2, 8, 1, 7, 3, 5, 6, 4]),
        ("_updated_at", "asc", [3, 6, 1, 4, 7, 2, 5, 8]),
        ("_updated_at", "desc", [2, 5, 8, 1, 4, 7, 3, 6]),
        ("_id", "desc", [8, 7, 6, 5, 4, 3, 2, 1]),
    ],
)
def test_sort_nulls_last(table_dir, column, direction, expected):
    assert sorted_ids(table_dir, [{"column": column, "direction": direction}]) == expected


def test_default_order_and_missing_direction(table_dir):
    assert sorted_ids(table_dir, None) == list(range(1, 9))
    assert sorted_ids(table_dir, []) == list(range(1, 9))
    assert sorted_ids(table_dir, [{"column": "Price"}]) == [3, 6, 2, 1, 7, 4, 8, 5]
    assert to_order_by(()) == "_id ASC"


def test_multi_key_sort(table_dir):
    sort = [{"column": "Done", "direction": "desc"}, {"column": "Price", "direction": "desc"}]
    assert sorted_ids(table_dir, sort) == [1, 7, 3, 8, 2, 5, 4, 6]


def test_paging_is_stable(table_dir):
    sort = [{"column": "Status"}]
    full = sorted_ids(table_dir, sort)
    pages = []
    for offset in range(0, 8, 3):
        pages += sorted_ids(table_dir, sort, limit=3, offset=offset)
    assert pages == full
    assert len(set(pages)) == 8
    # Equal values (Closed: 2 and 6) come out in _id order.
    assert full[:2] == [2, 6]


def test_order_by_text():
    keys = parse_sort(
        SCHEMA,
        [
            {"column": "Price", "direction": "desc"},
            {"column": "Name"},
            {"column": "_created_at", "direction": "desc"},
        ],
    )
    assert keys[0] == SortKey("Price", K_PRICE, "number", "desc")
    assert to_order_by(keys) == (
        f'(CAST("{K_PRICE}" AS REAL) IS NULL), CAST("{K_PRICE}" AS REAL) DESC, '
        f'("{K_NAME}" IS NULL), "{K_NAME}" ASC, '
        '("_created_at" IS NULL), "_created_at" DESC, _id ASC'
    )
    keys = parse_sort(SCHEMA, [{"column": "_id", "direction": "desc"}])
    assert to_order_by(keys) == "(_id IS NULL), _id DESC"
    assert "NOCASE" not in to_order_by(parse_sort(SCHEMA, [{"column": "Status"}]))


@pytest.mark.parametrize(
    "sort,code,path",
    [
        ("Price", "invalid_sort", "sort"),
        ({"column": "Price"}, "invalid_sort", "sort"),
        ([{"column": c} for c in ("Name", "Price", "Done", "Status")],
         "too_many_sort_keys", "sort"),
        ([{"column": "Price"}, {"column": "price", "direction": "desc"}],
         "invalid_sort", "sort[1]"),
        ([{"column": "_id"}, {"column": "_ID"}], "invalid_sort", "sort[1]"),
        ([{"column": "Price", "direction": "down"}], "invalid_sort", "sort[0].direction"),
        ([{"column": "Price", "direction": "DESC"}], "invalid_sort", "sort[0].direction"),
        ([{"column": "Price", "order": "asc"}], "invalid_sort", "sort[0]"),
        ([{"direction": "asc"}], "invalid_sort", "sort[0].column"),
        ([{"column": 3}], "invalid_sort", "sort[0].column"),
        (["Price"], "invalid_sort", "sort[0]"),
        ([{"column": "Nope"}], "unknown_column", "sort[0].column"),
    ],
    ids=str,
)
def test_sort_rejections(sort, code, path):
    with pytest.raises(FilterError) as info:
        parse_sort(SCHEMA, sort)
    assert (info.value.code, info.value.path) == (code, path)


def test_sort_case_insensitive_and_echo():
    (key,) = parse_sort(SCHEMA, [{"column": "tags", "direction": None}])
    assert (key.name, key.key, key.direction) == ("Tags", K_TAGS, "asc")


# --- Builders and paging --------------------------------------------------------------


def test_build_count_and_select_shapes():
    where = ('"c_00000001" = ?', ["x"])
    assert build_count(where) == (
        'SELECT COUNT(*) FROM rows WHERE ("c_00000001" = ?)',
        ["x"],
    )
    sql, params = build_select(
        [SCHEMA[1], SCHEMA[0]], where, "_id ASC", limit=10, offset=20
    )
    assert sql == (
        f'SELECT _id, _created_at, _updated_at, "{K_PRICE}", "{K_NAME}" FROM rows '
        'WHERE ("c_00000001" = ?) ORDER BY _id ASC LIMIT ? OFFSET ?'
    )
    assert params == ["x", 10, 20]
    sql, params = build_select([], ("1", []), "_id ASC", limit=1, offset=0)
    assert sql.startswith("SELECT _id, _created_at, _updated_at FROM rows")
    assert params == [1, 0]


@pytest.mark.parametrize(
    "limit,offset,kw,expected",
    [
        (None, None, {}, (100, 0)),
        (1, 0, {}, (1, 0)),
        (TABLE_QUERY_MAX_ROWS, 100_000, {}, (1000, 100_000)),
        (None, None, {"max_limit": TABLE_GRID_MAX_LIMIT}, (100, 0)),
        (200, 5, {"max_limit": TABLE_GRID_MAX_LIMIT}, (200, 5)),
        (None, None, {"max_limit": 50}, (50, 0)),
    ],
)
def test_check_page_ok(limit, offset, kw, expected):
    assert check_page(limit, offset, **kw) == expected


@pytest.mark.parametrize(
    "limit,offset,kw,code",
    [
        (0, 0, {}, "invalid_limit"),
        (1001, 0, {}, "invalid_limit"),
        (201, 0, {"max_limit": TABLE_GRID_MAX_LIMIT}, "invalid_limit"),
        (True, 0, {}, "invalid_limit"),
        ("10", 0, {}, "invalid_limit"),
        (10.0, 0, {}, "invalid_limit"),
        (10, -1, {}, "invalid_offset"),
        (10, 100_001, {}, "invalid_offset"),
        (10, False, {}, "invalid_offset"),
        (10, "0", {}, "invalid_offset"),
    ],
)
def test_check_page_refused(limit, offset, kw, code):
    with pytest.raises(FilterError) as info:
        check_page(limit, offset, **kw)
    assert info.value.code == code
    assert info.value.path == code.removeprefix("invalid_")


def test_resolve_columns():
    assert resolve_columns(SCHEMA, None) == tuple(SCHEMA)
    got = resolve_columns(SCHEMA, ["Tags", "name", "Tags", "_id", "NAME"])
    assert [c.name for c in got] == ["Tags", "Name"]
    assert resolve_columns(SCHEMA, ["_updated_at"]) == ()
    for bad, path in (([], "columns"), ("Name", "columns"), (["Name", 3], "columns[1]")):
        with pytest.raises(FilterError) as info:
            resolve_columns(SCHEMA, bad)
        assert (info.value.code, info.value.path) == ("invalid_columns", path)
    with pytest.raises(FilterError) as info:
        resolve_columns(SCHEMA, ["Name"] * 50)
    assert info.value.code == "invalid_columns"
    with pytest.raises(FilterError) as info:
        resolve_columns(SCHEMA, ["Name", "Nope"])
    assert (info.value.code, info.value.path) == ("unknown_column", "columns[1]")


# --- run_query ---------------------------------------------------------------------


def test_run_query_total_ignores_paging(table_dir):
    flt = leaf("Price", "not_null")
    result = run_query(table_dir, SCHEMA, filter=flt, limit=2, offset=1)
    assert isinstance(result, QueryResult)
    assert result.total == 7
    assert [r["_id"] for r in result.rows] == [2, 3]
    result = run_query(table_dir, SCHEMA, filter=flt, limit=5, offset=100)
    assert (result.total, result.rows) == (7, [])


def test_run_query_row_shape(table_dir):
    result = run_query(table_dir, SCHEMA, filter=leaf("Name", "eq", "Apple pie"))
    (row,) = result.rows
    assert row == {
        "_id": 1,
        "_updated_at": "2026-10-01T12:00:01.000000+00:00",
        "Name": "Apple pie",
        "Price": "10",
        "Score": "10",
        "Site": "https://example.com/a",
        "Done": True,
        "Status": "Open",
        "Tags": ["a", "b"],
    }
    assert list(row) == ["_id", "_updated_at", *[c.name for c in SCHEMA]]
    row4 = run_query(table_dir, SCHEMA, filter=leaf("Price", "eq", 100)).rows[0]
    assert row4["Done"] is None and row4["Tags"] is None
    row2 = run_query(table_dir, SCHEMA, filter=leaf("Done", "eq", False), limit=1).rows[0]
    assert row2["Done"] is False


def test_run_query_columns_subset(table_dir):
    result = run_query(
        table_dir, SCHEMA, columns=["Tags", "price"], sort=[{"column": "_id"}], limit=1
    )
    assert result.rows == [
        {"_id": 1, "_updated_at": "2026-10-01T12:00:01.000000+00:00",
         "Tags": ["a", "b"], "Price": "10"}
    ]
    assert list(result.rows[0]) == ["_id", "_updated_at", "Tags", "Price"]


def test_run_query_count_only(table_dir):
    result = run_query(table_dir, SCHEMA, filter=leaf("Done", "eq", True), count_only=True)
    assert result == QueryResult(rows=[], total=3)


def test_run_query_grid_limit(table_dir):
    with pytest.raises(FilterError) as info:
        run_query(table_dir, SCHEMA, limit=201, max_limit=TABLE_GRID_MAX_LIMIT)
    assert info.value.code == "invalid_limit"


@pytest.mark.parametrize(
    "kw",
    [
        {"filter": leaf("Nope", "eq", 1)},
        {"sort": [{"column": "Nope"}]},
        {"columns": ["Nope"]},
        {"limit": 0},
        {"offset": -1},
    ],
    ids=str,
)
def test_run_query_validates_before_io(tmp_path, kw):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FilterError):
        run_query(missing, SCHEMA, **kw)
    assert not missing.exists()
    # The same call with valid input does hit storage.
    with pytest.raises(storage.TableStorageError):
        run_query(missing, SCHEMA)


def test_run_query_schema_mismatch(table_dir):
    extra = parse_columns(
        [{"key": "c_0000ffff", "name": "Ghost", "type": "text"}], allow_key=True
    )
    schema = [*SCHEMA, *extra]
    with pytest.raises(storage.SchemaMismatchError):
        run_query(table_dir, schema)
    with pytest.raises(storage.SchemaMismatchError):
        run_query(table_dir, schema, filter=leaf("Ghost", "is_null"), count_only=True)
    with pytest.raises(storage.SchemaMismatchError):
        run_query(table_dir, schema, columns=["Ghost"])
    # The connection was not left inside a transaction.
    assert run_query(table_dir, SCHEMA, count_only=True).total == 8


def test_run_query_read_is_one_transaction(table_dir, monkeypatch):
    seen = []
    real = storage._select_rows

    def spy(conn, sql, params=()):
        seen.append(conn.in_transaction)
        return real(conn, sql, params)

    monkeypatch.setattr(storage, "_select_rows", spy)
    run_query(table_dir, SCHEMA)
    assert seen == [True, True]
