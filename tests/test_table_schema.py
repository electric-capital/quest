"""Quest Tables column model and value canonicalization (chat/tables/schema.py)."""

import dataclasses
import json
import time

import pytest

from chat.tables.constants import (
    TABLE_COLUMN_DESCRIPTION_MAX_LEN,
    TABLE_COLUMN_NAME_MAX_LEN,
    TABLE_ENUM_MAX_OPTIONS,
    TABLE_ENUM_OPTION_MAX_LEN,
    TABLE_MAX_COLUMNS,
    TABLE_TEXT_MAX_CHARS,
    TABLE_TITLE_MAX_LEN,
    TABLE_URL_MAX_CHARS,
)
from chat.tables.schema import (
    Column,
    ColumnSpecError,
    TableSchemaError,
    TableValueError,
    apply_default,
    canonicalize,
    clip_repr,
    coerce_option,
    coerce_value,
    dump_schema,
    find_column,
    from_storage,
    is_valid_column_key,
    load_schema,
    name_taken,
    new_column_key,
    parse_column_spec,
    parse_columns,
    to_storage,
    validate_column_name,
    validate_table_title,
)

TEXT = Column(key="c_00000001", name="Notes", type="text")
NUMBER = Column(key="c_00000002", name="Price", type="number")
URL = Column(key="c_00000003", name="Link", type="url")
BOOL = Column(key="c_00000004", name="Done", type="boolean")
ENUM = Column(key="c_00000005", name="Status", type="enum", options=("A", "B", "C"))
MULTI = Column(
    key="c_00000006", name="Tags", type="enum", options=("A", "B", "C"), multiple=True
)


def num(precision):
    return dataclasses.replace(NUMBER, precision=precision)


def _cases(table):
    """(id, *args) tuples -> parametrize values with readable ids."""
    return [pytest.param(*row[1:], id=row[0]) for row in table]


def assert_value_error(column, value, code):
    with pytest.raises(TableValueError) as info:
        coerce_value(column, value)
    assert info.value.code == code
    assert info.value.column == column.name
    assert column.name in str(info.value)


# --- text -----------------------------------------------------------------------

TEXT_ACCEPT = [
    ("plain", "hello", "hello"),
    ("empty", "", ""),
    ("nfc", "é", "é"),
    ("crlf", "a\r\nb", "a\nb"),
    ("lone-cr", "a\rb\r", "a\nb\n"),
    ("mixed-newlines", "a\r\n\rb\n", "a\n\nb\n"),
    ("max-len", "x" * TABLE_TEXT_MAX_CHARS, "x" * TABLE_TEXT_MAX_CHARS),
    (
        "len-after-nfc",
        "é" * TABLE_TEXT_MAX_CHARS,
        "é" * TABLE_TEXT_MAX_CHARS,
    ),
    (
        "len-after-crlf",
        "\r\n" * TABLE_TEXT_MAX_CHARS,
        "\n" * TABLE_TEXT_MAX_CHARS,
    ),
    ("whitespace-kept", "  x  ", "  x  "),
    ("cr-cr-lf", "a\r\r\nb", "a\n\nb"),
    ("controls-kept", "a\tb\x01\x7f", "a\tb\x01\x7f"),
    ("format-and-separators-kept", "a\u200bb\u202ec\u2028d\u2029\u200d\x9b",
     "a\u200bb\u202ec\u2028d\u2029\u200d\x9b"),
    ("surrogate-pair-ok", "\U0001F600", "\U0001F600"),
    (
        "raw-4x-bound-shrinks",
        "e\u0301" * TABLE_TEXT_MAX_CHARS,
        "\u00e9" * TABLE_TEXT_MAX_CHARS,
    ),
]

TEXT_REJECT = [
    ("too-long", "x" * (TABLE_TEXT_MAX_CHARS + 1), "too_long"),
    ("too-long-raw", "\r\n" * (2 * TABLE_TEXT_MAX_CHARS) + "x", "too_long"),
    ("lone-surrogate", "\ud800", "invalid_text"),
    ("lone-surrogate-json", json.loads('"a\\udc00b"'), "invalid_text"),
    ("nul", "a\x00b", "invalid_text"),
    ("int", 5, "wrong_type"),
    ("float", 1.5, "wrong_type"),
    ("bool", True, "wrong_type"),
    ("list", ["a"], "wrong_type"),
    ("dict", {"a": 1}, "wrong_type"),
]


@pytest.mark.parametrize("value,expected", _cases(TEXT_ACCEPT))
def test_text_accept(value, expected):
    assert coerce_value(TEXT, value) == expected


@pytest.mark.parametrize("value,code", _cases(TEXT_REJECT))
def test_text_reject(value, code):
    assert_value_error(TEXT, value, code)


# --- number ---------------------------------------------------------------------

NUMBER_ACCEPT = [
    ("trailing-zero", None, "1.10", "1.1"),
    ("neg-zero", None, "-0", "0"),
    ("neg-zero-frac", None, "-0.00", "0"),
    ("zero-exp", None, "0e-999999999", "0"),
    ("exp-upper", None, "1E+2", "100"),
    ("exp-lower", None, "1e2", "100"),
    ("exp-neg", None, "15e-1", "1.5"),
    ("float-whole", None, 1.0, "1"),
    ("float-tenth", None, 0.1, "0.1"),
    ("float-neg", None, -2.75, "-2.75"),
    ("int", None, 42, "42"),
    ("int-neg", None, -7, "-7"),
    ("int-zero", None, 0, "0"),
    ("int-1e37", None, 10**37, "1" + "0" * 37),
    ("int-38-digits", None, int("9" * 38), "9" * 38),
    ("str-38-digits", None, "9" * 38, "9" * 38),
    ("frac-38-digits", None, "0." + "1" * 38, "0." + "1" * 38),
    ("frac-38-tiny", None, "1e-38", "0." + "0" * 37 + "1"),
    ("plus-sign", None, "+5", "5"),
    ("zero-huge-exp", None, "0e999999999999999999999999", "0"),
    ("neg-zero-huge-exp", None, "-0.0E+999999999999999999999999", "0"),
    ("p2-1e-38-to-zero", 2, "1e-38", "0"),
    ("p2-1e-39-to-zero", 2, "1e-39", "0"),
    ("p2-tiny-exp", 2, "1e-999999999", "0"),
    ("p2-neg-tiny", 2, "-1e-50", "0"),
    ("p2-boundary-1e-4", 2, "0.0001", "0"),
    ("p2-below-half", 2, "0.0049", "0"),
    ("p2-half-even-down", 2, "0.005", "0"),
    ("p2-above-half", 2, "0.0051", "0.01"),
    ("p2-rounds-up", 2, "0.0099", "0.01"),
    ("p0-boundary", 0, "0.09", "0"),
    ("p0-half-even", 0, "0.5", "0"),
    ("p0-above-half", 0, "0.51", "1"),
    ("p10-boundary", 10, "1e-12", "0"),
    ("p10-keeps", 10, "6e-11", "0.0000000001"),
    ("p2-float-subnormal", 2, 5e-324, "0"),
    ("leading-dot", None, ".5", "0.5"),
    ("trailing-dot", None, "5.", "5"),
    ("surrounding-space", None, "  42  ", "42"),
    ("leading-zeros", None, "007.50", "7.5"),
    ("neg-frac", None, "-0.5", "-0.5"),
    ("p2-half-even-down", 2, "1.005", "1"),
    ("p2-half-even-up", 2, "1.015", "1.02"),
    ("p2-half-even-even", 2, "1.025", "1.02"),
    ("p0-2.5", 0, "2.5", "2"),
    ("p0-3.5", 0, "3.5", "4"),
    ("p0-neg-2.5", 0, "-2.5", "-2"),
    ("p2-neg-rounds-to-zero", 2, "-0.001", "0"),
    ("p2-keeps-short", 2, "1.5", "1.5"),
    ("p10", 10, "3.14159265358979", "3.1415926536"),
    ("p10-int", 10, 7, "7"),
    ("p2-float", 2, 2.675, "2.68"),
    ("p0-long-input", 0, "1." + "0" * 50 + "1", "1"),
]

NUMBER_REJECT = [
    ("int-39-digits", None, 10**38, "too_many_digits"),
    ("str-39-digits", None, "1" * 39, "too_many_digits"),
    ("frac-39-digits", None, "0." + "1" * 39, "too_many_digits"),
    ("int-plus-frac-39", None, "1" * 20 + "." + "1" * 19, "too_many_digits"),
    ("1e50", None, "1E+50", "too_many_digits"),
    ("1e-45", None, "1e-45", "too_many_digits"),
    ("1e-39", None, "1e-39", "too_many_digits"),
    ("huge-exp", None, "1e999999999", "too_many_digits"),
    ("huge-exp-p2", 2, "1e999999999", "too_many_digits"),
    ("enormous-exp", None, "1e99999999999999999999999", "too_many_digits"),
    ("float-huge", None, 1e300, "too_many_digits"),
    ("rounds-up-to-39", 0, "9" * 38 + ".5", "too_many_digits"),
    ("whitespace-only", None, "   ", "invalid_number"),
    ("empty", None, "", "invalid_number"),
    ("dot", None, ".", "invalid_number"),
    ("thousands", None, "1,234.5", "invalid_number"),
    ("underscore", None, "1_000", "invalid_number"),
    ("NaN", None, "NaN", "invalid_number"),
    ("nan", None, "nan", "invalid_number"),
    ("Infinity", None, "Infinity", "invalid_number"),
    ("neg-inf", None, "-inf", "invalid_number"),
    ("sNaN", None, "sNaN", "invalid_number"),
    ("hex", None, "0x10", "invalid_number"),
    ("arabic-indic", None, "١٢", "invalid_number"),
    ("fullwidth", None, "１", "invalid_number"),
    ("units", None, "5 kg", "invalid_number"),
    ("double-sign", None, "--5", "invalid_number"),
    ("exp-only", None, "e5", "invalid_number"),
    ("inner-space", None, "1 000", "invalid_number"),
    ("float-nan", None, float("nan"), "invalid_number"),
    ("float-inf", None, float("inf"), "invalid_number"),
    ("float-neg-inf", None, float("-inf"), "invalid_number"),
    ("true", None, True, "wrong_type"),
    ("false", None, False, "wrong_type"),
    ("list", None, [1], "wrong_type"),
    ("dict", None, {"v": 1}, "wrong_type"),
    ("long-string", None, "1" * 1000, "too_long"),
]


@pytest.mark.parametrize("precision,value,expected", _cases(NUMBER_ACCEPT))
def test_number_accept(precision, value, expected):
    assert coerce_value(num(precision), value) == expected


@pytest.mark.parametrize("precision,value,code", _cases(NUMBER_REJECT))
def test_number_reject(precision, value, code):
    assert_value_error(num(precision), value, code)


def test_number_huge_exponent_is_fast():
    start = time.perf_counter()
    for value in ("1e999999999", "1e-999999999", "-9e999999999"):
        for precision in (None, 2):
            if precision is not None and "e-" in value:
                assert coerce_value(num(precision), value) == "0"
                continue
            with pytest.raises(TableValueError):
                coerce_value(num(precision), value)
    assert time.perf_counter() - start < 1


@pytest.mark.parametrize("precision", [None, 2], ids=["no-precision", "p2"])
def test_number_unrepresentable_exponent_with_traps_off(precision):
    import decimal

    with decimal.localcontext() as ctx:
        ctx.traps[decimal.InvalidOperation] = False
        assert_value_error(num(precision), "1e99999999999999999999999", "too_many_digits")
        assert coerce_value(num(precision), "1.5") == "1.5"


def test_number_ignores_thread_decimal_context():
    import decimal

    with decimal.localcontext() as ctx:
        ctx.prec = 5
        ctx.rounding = decimal.ROUND_DOWN
        assert coerce_value(NUMBER, "9" * 38) == "9" * 38
        assert coerce_value(num(0), "2.5") == "2"
        assert coerce_value(num(0), "3.5") == "4"


# --- url ------------------------------------------------------------------------

_LONG_URL = "https://example.com/"
_MAX_URL = _LONG_URL + "a" * (TABLE_URL_MAX_CHARS - len(_LONG_URL))

URL_ACCEPT = [
    ("http", "http://example.com", "http://example.com"),
    ("https-path", "https://example.com/a?b=c#d", "https://example.com/a?b=c#d"),
    ("scheme-case-kept", "HTTP://Example.com/X", "HTTP://Example.com/X"),
    ("stripped", "  https://example.com/ \n", "https://example.com/"),
    ("userinfo", "https://user:pw@example.com/", "https://user:pw@example.com/"),
    ("ipv6", "http://[::1]:8080/x", "http://[::1]:8080/x"),
    ("port", "http://host:8080", "http://host:8080"),
    ("max-len", _MAX_URL, _MAX_URL),
    ("port-0", "http://host:0/", "http://host:0/"),
    ("port-65535", "https://host:65535", "https://host:65535"),
    ("idn-host", "http://bücher.de/straße", "http://bücher.de/straße"),
    ("percent-encoded", "https://example.com/a%20b?q=%E2%9C%93#x%2Fy",
     "https://example.com/a%20b?q=%E2%9C%93#x%2Fy"),
    ("stripped-unicode-space", "\u3000https://example.com/\u00a0",
     "https://example.com/"),
]

URL_REJECT = [
    ("too-long", _MAX_URL + "a", "too_long"),
    ("no-host", "http://", "invalid_url"),
    ("empty-host", "http:///path", "invalid_url"),
    ("ftp", "ftp://x", "invalid_url"),
    ("javascript", "javascript:alert(1)", "invalid_url"),
    ("mailto", "mailto:a@b", "invalid_url"),
    ("no-scheme", "example.com", "invalid_url"),
    ("inner-space", "http://exa mple.com", "invalid_url"),
    ("control-char", "http://exa\x01mple.com", "invalid_url"),
    ("del-char", "http://example.com/\x7f", "invalid_url"),
    ("tab", "http://example.com/\tx", "invalid_url"),
    ("bad-ipv6", "http://[::1", "invalid_url"),
    ("bad-port", "http://host:99999", "invalid_url"),
    ("port-65536", "https://host:65536", "invalid_url"),
    ("backslash-userinfo", "http://evil.com\\@good.com/", "invalid_url"),
    ("backslash-path", "https://example.com/a\\b", "invalid_url"),
    ("nbsp", "http://exa\u00a0mple.com", "invalid_url"),
    ("ideographic-space", "https://example.com/\u3000x", "invalid_url"),
    ("line-separator", "https://example.com/\u2028x", "invalid_url"),
    ("paragraph-separator", "https://example.com/\u2029x", "invalid_url"),
    ("c1-control", "http://a.com/\x9b", "invalid_url"),
    ("c1-nel", "http://a.com/\x85x", "invalid_url"),
    ("zero-width-space", "http://a.com/\u200bx", "invalid_url"),
    ("bidi-override", "http://a.com/\u202egpj.exe", "invalid_url"),
    ("zero-width-joiner", "http://a.com/\u200d", "invalid_url"),
    ("bom", "http://\ufeffa.com/", "invalid_url"),
    ("lone-surrogate", "https://example.com/\ud800", "invalid_url"),
    ("nul", "https://example.com/\x00", "invalid_url"),
    ("empty", "", "invalid_url"),
    ("whitespace-only", "   ", "invalid_url"),
    ("int", 5, "wrong_type"),
    ("bool", True, "wrong_type"),
    ("list", ["http://x"], "wrong_type"),
]


@pytest.mark.parametrize("value,expected", _cases(URL_ACCEPT))
def test_url_accept(value, expected):
    assert coerce_value(URL, value) == expected


@pytest.mark.parametrize("value,code", _cases(URL_REJECT))
def test_url_reject(value, code):
    assert_value_error(URL, value, code)


# --- boolean --------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
def test_boolean_accept(value):
    result = coerce_value(BOOL, value)
    assert result is value


@pytest.mark.parametrize(
    "value",
    [0, 1, "true", "yes", "false", 1.0, []],
    ids=["zero", "one", "str-true", "str-yes", "str-false", "float", "list"],
)
def test_boolean_reject(value):
    assert_value_error(BOOL, value, "wrong_type")


# --- enum -----------------------------------------------------------------------

ENUM_ACCEPT = [
    ("single-exact", ENUM, "B", "B"),
    ("multi-dedupe-sort", MULTI, ["C", "A", "C"], ["A", "C"]),
    ("multi-single", MULTI, ["B"], ["B"]),
    ("multi-empty", MULTI, [], None),
    ("multi-tuple", MULTI, ("B", "A"), ["A", "B"]),
]

ENUM_REJECT = [
    ("single-case", ENUM, "a", "unknown_option"),
    ("single-unknown", ENUM, "Z", "unknown_option"),
    ("single-int", ENUM, 1, "wrong_type"),
    ("single-list", ENUM, ["A"], "wrong_type"),
    ("multi-bare-string", MULTI, "A", "wrong_type"),
    ("multi-unknown", MULTI, ["A", "Z"], "unknown_option"),
    ("multi-case", MULTI, ["a"], "unknown_option"),
    ("multi-non-str", MULTI, ["A", 1], "wrong_type"),
    ("multi-dict", MULTI, {"A": True}, "wrong_type"),
]


@pytest.mark.parametrize("column,value,expected", _cases(ENUM_ACCEPT))
def test_enum_accept(column, value, expected):
    assert coerce_value(column, value) == expected


@pytest.mark.parametrize("column,value,code", _cases(ENUM_REJECT))
def test_enum_reject(column, value, code):
    assert_value_error(column, value, code)


# --- required, canonicalize, apply_default --------------------------------------


@pytest.mark.parametrize(
    "column,value",
    [
        pytest.param(dataclasses.replace(TEXT, required=True), None, id="text-none"),
        pytest.param(dataclasses.replace(MULTI, required=True), None, id="multi-none"),
        pytest.param(dataclasses.replace(MULTI, required=True), [], id="multi-empty"),
    ],
)
def test_required(column, value):
    assert coerce_value(column, value) is None
    with pytest.raises(TableValueError) as info:
        canonicalize(column, value)
    assert info.value.code == "required"
    assert info.value.column == column.name


def test_canonicalize_optional_none():
    assert canonicalize(TEXT, None) is None
    assert canonicalize(MULTI, []) is None
    assert canonicalize(NUMBER, "1.50") == "1.5"


def test_coerce_none_is_none_for_every_type():
    for column in (TEXT, NUMBER, URL, BOOL, ENUM, MULTI):
        assert coerce_value(column, None) is None


TEXT_DEFAULT = dataclasses.replace(TEXT, default="dflt")
REQ_DEFAULT = dataclasses.replace(TEXT, required=True, default="dflt")
REQ_NO_DEFAULT = dataclasses.replace(TEXT, required=True)
MULTI_DEFAULT = dataclasses.replace(MULTI, default=["A"])

APPLY_DEFAULT = [
    ("omitted-uses-default", TEXT_DEFAULT, False, None, "dflt"),
    ("explicit-none-optional", TEXT_DEFAULT, True, None, None),
    ("present-value", TEXT_DEFAULT, True, "x", "x"),
    ("omitted-required-default", REQ_DEFAULT, False, None, "dflt"),
    ("omitted-no-default-optional", TEXT, False, None, None),
    ("omitted-ignores-value-arg", TEXT, False, "ignored", None),
    ("present-canonicalized", TEXT, True, "a\r\nb", "a\nb"),
    ("multi-default", MULTI_DEFAULT, False, None, ["A"]),
]


@pytest.mark.parametrize("column,present,value,expected", _cases(APPLY_DEFAULT))
def test_apply_default(column, present, value, expected):
    assert apply_default(column, present, value) == expected


@pytest.mark.parametrize(
    "column,present",
    [
        pytest.param(REQ_NO_DEFAULT, False, id="omitted-required-no-default"),
        pytest.param(REQ_DEFAULT, True, id="explicit-none-required-with-default"),
    ],
)
def test_apply_default_required(column, present):
    with pytest.raises(TableValueError) as info:
        apply_default(column, present, None)
    assert info.value.code == "required"


def test_apply_default_returns_a_copy_of_list_default():
    value = apply_default(MULTI_DEFAULT, False)
    value.append("B")
    assert MULTI_DEFAULT.default == ("A",)
    assert apply_default(MULTI_DEFAULT, False) == ["A"]
    assert type(apply_default(MULTI_DEFAULT, False)) is list


# --- storage --------------------------------------------------------------------

UNICODE_MULTI = Column(
    key="c_00000007", name="Cities", type="enum", options=("Zürich", "東京"), multiple=True
)

STORAGE = [
    ("text", TEXT, "héllo", "héllo"),
    ("number", NUMBER, "1.5", "1.5"),
    ("url", URL, "https://x.y/", "https://x.y/"),
    ("bool-true", BOOL, True, 1),
    ("bool-false", BOOL, False, 0),
    ("enum", ENUM, "A", "A"),
    ("multi", MULTI, ["A", "C"], '["A","C"]'),
    ("multi-unicode", UNICODE_MULTI, ["Zürich", "東京"], '["Zürich","東京"]'),
    ("none", TEXT, None, None),
    ("none-bool", BOOL, None, None),
    ("none-multi", MULTI, None, None),
]


@pytest.mark.parametrize("column,value,stored", _cases(STORAGE))
def test_storage_round_trip(column, value, stored):
    assert to_storage(column, value) == stored
    assert type(to_storage(column, value)) is type(stored)
    back = from_storage(column, stored)
    assert back == value
    assert type(back) is type(value)


# --- column keys and names ------------------------------------------------------


@pytest.mark.parametrize(
    "key,ok",
    [
        ("c_1a2b3c4d", True),
        ("c_00000000", True),
        ("c_XYZ", False),
        ("c_1234567", False),
        ("c_123456789", False),
        ("c_1A2B3C4D", False),
        ("d_12345678", False),
        ("c_1234567g", False),
        ("c_12345678\n", False),
        (12345678, False),
        (None, False),
    ],
    ids=lambda v: repr(v),
)
def test_is_valid_column_key(key, ok):
    assert is_valid_column_key(key) is ok


def test_new_column_key_avoids_existing(monkeypatch):
    from chat.tables import schema

    tokens = iter(["00000001", "00000001", "abcdef01"])
    monkeypatch.setattr(schema.secrets, "token_hex", lambda n: next(tokens))
    assert new_column_key({"c_00000001"}) == "c_abcdef01"


def test_new_column_key_shape():
    keys = {new_column_key(()) for _ in range(50)}
    assert all(is_valid_column_key(k) for k in keys)


@pytest.mark.parametrize(
    "name,ok",
    [
        ("Status", True),
        ("x" * TABLE_COLUMN_NAME_MAX_LEN, True),
        ("Two words", True),
        ("", False),
        ("x" * (TABLE_COLUMN_NAME_MAX_LEN + 1), False),
        (" lead", False),
        ("trail ", False),
        ("   ", False),
        ("tab\t", False),
        (5, False),
        (None, False),
        ("a\x00b", False),
        ("a\nb", False),
        ("a\x85b", False),
        ("a\ud800", False),
        ("Prix é 東京", True),
        ("\U0001f4ca Sales", True),
        ("a\u200bb", False),
        ("a\u202eb", False),
        ("a\u2028b", False),
        ("a\u2029b", False),
        ("\U0001f468\u200d\U0001f469", False),
    ],
    ids=["simple", "max-len", "inner-space", "empty", "too-long", "leading-space",
         "trailing-space", "whitespace-only", "trailing-tab", "int", "none",
         "nul", "inner-newline", "c1-control", "surrogate", "unicode", "emoji",
         "zero-width-space", "bidi-override", "line-separator", "paragraph-separator",
         "zwj-sequence"],
)
def test_validate_column_name(name, ok):
    if ok:
        assert validate_column_name(name) == name
    else:
        with pytest.raises(ColumnSpecError) as info:
            validate_column_name(name)
        assert info.value.code == "invalid_name"


def test_name_taken_and_find_column():
    cols = [TEXT, NUMBER]
    assert name_taken(cols, "notes")
    assert name_taken(cols, "NOTES")
    assert not name_taken(cols, "notes", except_key=TEXT.key)
    assert not name_taken(cols, "Other")
    assert find_column(cols, "Notes") is TEXT
    assert find_column(cols, "notes") is None
    assert find_column(cols, "Missing") is None


# --- parse_column_spec ----------------------------------------------------------

EXISTING = [Column(key="c_aaaaaaaa", name="Existing", type="text")]

SPEC_ACCEPT = [
    (
        "text-minimal",
        {"name": "Notes", "type": "text"},
        {"type": "text", "required": False, "unique": False, "default": None,
         "options": None, "multiple": False, "precision": None, "description": ""},
    ),
    (
        "name-max-len",
        {"name": "x" * TABLE_COLUMN_NAME_MAX_LEN, "type": "text"},
        {"name": "x" * TABLE_COLUMN_NAME_MAX_LEN},
    ),
    ("name-not-normalized", {"name": "Café", "type": "text"}, {"name": "Café"}),
    ("unique-text", {"name": "N", "type": "text", "unique": True}, {"unique": True}),
    ("unique-number", {"name": "N", "type": "number", "unique": True}, {"unique": True}),
    ("unique-url", {"name": "N", "type": "url", "unique": True}, {"unique": True}),
    ("unique-false-boolean", {"name": "N", "type": "boolean", "unique": False}, {"unique": False}),
    (
        "unique-false-enum",
        {"name": "N", "type": "enum", "options": ["A"], "unique": False},
        {"unique": False},
    ),
    ("flags-null", {"name": "N", "type": "text", "required": None}, {"required": False}),
    ("required", {"name": "N", "type": "text", "required": True}, {"required": True}),
    ("description-stripped", {"name": "N", "type": "text", "description": "  hi  "},
     {"description": "hi"}),
    ("description-null", {"name": "N", "type": "text", "description": None}, {"description": ""}),
    ("description-newlines-tabs", {"name": "N", "type": "text",
                                   "description": "line 1\n\tline 2"},
     {"description": "line 1\n\tline 2"}),
    ("description-format-chars", {"name": "N", "type": "text",
                                  "description": "a\u200bb\u202ec\u2028d\x85e"},
     {"description": "a\u200bb\u202ec\u2028d\x85e"}),
    ("description-max", {"name": "N", "type": "text",
                         "description": "d" * TABLE_COLUMN_DESCRIPTION_MAX_LEN},
     {"description": "d" * TABLE_COLUMN_DESCRIPTION_MAX_LEN}),
    ("options-one", {"name": "N", "type": "enum", "options": ["A"]}, {"options": ("A",)}),
    ("options-max", {"name": "N", "type": "enum",
                     "options": [f"o{i}" for i in range(TABLE_ENUM_MAX_OPTIONS)]},
     {"options": tuple(f"o{i}" for i in range(TABLE_ENUM_MAX_OPTIONS))}),
    ("option-max-len", {"name": "N", "type": "enum",
                        "options": ["o" * TABLE_ENUM_OPTION_MAX_LEN]},
     {"options": ("o" * TABLE_ENUM_OPTION_MAX_LEN,)}),
    ("options-order-kept", {"name": "N", "type": "enum", "options": ["b", "a"]},
     {"options": ("b", "a")}),
    ("multiple", {"name": "N", "type": "enum", "options": ["A"], "multiple": True},
     {"multiple": True}),
    ("multiple-false-text", {"name": "N", "type": "text", "multiple": False}, {"multiple": False}),
    ("options-null-text", {"name": "N", "type": "text", "options": None}, {"options": None}),
    ("precision-0", {"name": "N", "type": "number", "precision": 0}, {"precision": 0}),
    ("precision-10", {"name": "N", "type": "number", "precision": 10}, {"precision": 10}),
    ("precision-null-text", {"name": "N", "type": "text", "precision": None}, {"precision": None}),
    ("default-text", {"name": "N", "type": "text", "default": "a\r\nb"}, {"default": "a\nb"}),
    ("default-number", {"name": "N", "type": "number", "default": 1.50}, {"default": "1.5"}),
    ("default-rounded", {"name": "N", "type": "number", "precision": 1, "default": "2.25"},
     {"default": "2.2"}),
    ("default-url", {"name": "N", "type": "url", "default": " https://x.y "},
     {"default": "https://x.y"}),
    ("default-bool", {"name": "N", "type": "boolean", "default": False}, {"default": False}),
    ("default-enum", {"name": "N", "type": "enum", "options": ["A", "B"], "default": "B"},
     {"default": "B"}),
    ("default-multi", {"name": "N", "type": "enum", "options": ["A", "B"], "multiple": True,
                       "default": ["B", "A"]}, {"default": ("A", "B")}),
    ("default-multi-empty", {"name": "N", "type": "enum", "options": ["A"], "multiple": True,
                             "default": []}, {"default": None}),
    ("default-null", {"name": "N", "type": "text", "default": None}, {"default": None}),
    ("required-with-default", {"name": "N", "type": "text", "required": True, "default": "x"},
     {"required": True, "default": "x"}),
    ("client-key", {"name": "N", "type": "text", "key": "c_0123abcd"}, {"key": "c_0123abcd"}),
    ("key-null", {"name": "N", "type": "text", "key": None}, {}),
]


@pytest.mark.parametrize("spec,expected", _cases(SPEC_ACCEPT))
def test_parse_column_spec_accept(spec, expected):
    column = parse_column_spec(spec, existing=EXISTING, allow_key="key" in spec)
    assert isinstance(column, Column)
    assert is_valid_column_key(column.key)
    assert column.key != EXISTING[0].key
    for field, value in expected.items():
        assert getattr(column, field) == value, field


def _many_columns(n):
    return [Column(key=f"c_{i:08x}", name=f"col{i}", type="text") for i in range(n)]


SPEC_REJECT = [
    ("not-a-dict", ["name"], "invalid_spec"),
    ("none", None, "invalid_spec"),
    ("unknown-field", {"name": "N", "type": "text", "colour": "red"}, "unknown_field"),
    ("missing-name", {"type": "text"}, "missing_field"),
    ("missing-type", {"name": "N"}, "missing_field"),
    ("name-empty", {"name": "", "type": "text"}, "invalid_name"),
    ("name-81", {"name": "x" * (TABLE_COLUMN_NAME_MAX_LEN + 1), "type": "text"}, "invalid_name"),
    ("name-leading-space", {"name": " N", "type": "text"}, "invalid_name"),
    ("name-trailing-space", {"name": "N ", "type": "text"}, "invalid_name"),
    ("name-whitespace-only", {"name": "  ", "type": "text"}, "invalid_name"),
    ("name-int", {"name": 5, "type": "text"}, "invalid_name"),
    ("name-null", {"name": None, "type": "text"}, "invalid_name"),
    ("name-reserved-id", {"name": "_id", "type": "text"}, "reserved_name"),
    ("name-reserved-created", {"name": "_created_at", "type": "text"}, "reserved_name"),
    ("name-reserved-updated-case", {"name": "_Updated_AT", "type": "text"}, "reserved_name"),
    ("name-duplicate-case", {"name": "EXISTING", "type": "text"}, "duplicate_name"),
    ("type-unknown", {"name": "N", "type": "date"}, "invalid_type"),
    ("type-case", {"name": "N", "type": "Text"}, "invalid_type"),
    ("type-int", {"name": "N", "type": 1}, "invalid_type"),
    ("required-str", {"name": "N", "type": "text", "required": "yes"}, "invalid_field"),
    ("unique-int", {"name": "N", "type": "text", "unique": 1}, "invalid_field"),
    ("multiple-str", {"name": "N", "type": "enum", "options": ["A"], "multiple": "true"},
     "invalid_field"),
    ("description-int", {"name": "N", "type": "text", "description": 5}, "invalid_description"),
    ("description-too-long", {"name": "N", "type": "text",
                              "description": "d" * (TABLE_COLUMN_DESCRIPTION_MAX_LEN + 1)},
     "invalid_description"),
    ("enum-no-options", {"name": "N", "type": "enum"}, "missing_field"),
    ("enum-options-null", {"name": "N", "type": "enum", "options": None}, "missing_field"),
    ("options-zero", {"name": "N", "type": "enum", "options": []}, "invalid_options"),
    ("options-201", {"name": "N", "type": "enum",
                     "options": [f"o{i}" for i in range(TABLE_ENUM_MAX_OPTIONS + 1)]},
     "invalid_options"),
    ("options-string", {"name": "N", "type": "enum", "options": "A,B"}, "invalid_options"),
    ("option-empty", {"name": "N", "type": "enum", "options": [""]}, "invalid_option"),
    ("option-101", {"name": "N", "type": "enum",
                    "options": ["o" * (TABLE_ENUM_OPTION_MAX_LEN + 1)]}, "invalid_option"),
    ("option-leading-space", {"name": "N", "type": "enum", "options": [" A"]}, "invalid_option"),
    ("option-whitespace-only", {"name": "N", "type": "enum", "options": ["  "]},
     "invalid_option"),
    ("option-int", {"name": "N", "type": "enum", "options": [1]}, "invalid_option"),
    ("option-surrogate", {"name": "N", "type": "enum", "options": ["A\ud800"]},
     "invalid_option"),
    ("option-tab", {"name": "N", "type": "enum", "options": ["a\tb"]}, "invalid_option"),
    ("option-nul", {"name": "N", "type": "enum", "options": ["a\x00"]}, "invalid_option"),
    ("option-c1", {"name": "N", "type": "enum", "options": ["a\x9b"]}, "invalid_option"),
    ("option-zero-width", {"name": "N", "type": "enum", "options": ["a\u200b"]},
     "invalid_option"),
    ("option-bidi", {"name": "N", "type": "enum", "options": ["\u202eA"]}, "invalid_option"),
    ("option-line-separator", {"name": "N", "type": "enum", "options": ["a\u2028b"]},
     "invalid_option"),
    ("description-nul", {"name": "N", "type": "text", "description": "a\x00b"},
     "invalid_description"),
    ("description-surrogate", {"name": "N", "type": "text", "description": "\udfff"},
     "invalid_description"),
    ("name-surrogate", {"name": "\ud800", "type": "text"}, "invalid_name"),
    ("default-text-surrogate", {"name": "N", "type": "text", "default": "\ud800"},
     "invalid_default"),
    ("option-duplicate-case", {"name": "N", "type": "enum", "options": ["Done", "done"]},
     "duplicate_option"),
    ("options-on-text", {"name": "N", "type": "text", "options": ["A"]}, "field_not_allowed"),
    ("multiple-on-text", {"name": "N", "type": "text", "multiple": True}, "field_not_allowed"),
    ("precision-on-text", {"name": "N", "type": "text", "precision": 2}, "field_not_allowed"),
    ("precision-on-enum", {"name": "N", "type": "enum", "options": ["A"], "precision": 0},
     "field_not_allowed"),
    ("precision-neg", {"name": "N", "type": "number", "precision": -1}, "invalid_precision"),
    ("precision-11", {"name": "N", "type": "number", "precision": 11}, "invalid_precision"),
    ("precision-bool", {"name": "N", "type": "number", "precision": True}, "invalid_precision"),
    ("precision-float", {"name": "N", "type": "number", "precision": 1.5}, "invalid_precision"),
    ("precision-str", {"name": "N", "type": "number", "precision": "2"}, "invalid_precision"),
    ("unique-boolean", {"name": "N", "type": "boolean", "unique": True}, "unique_not_allowed"),
    ("unique-enum", {"name": "N", "type": "enum", "options": ["A"], "unique": True},
     "unique_not_allowed"),
    ("default-text-int", {"name": "N", "type": "text", "default": 5}, "invalid_default"),
    ("default-text-too-long", {"name": "N", "type": "text",
                               "default": "x" * (TABLE_TEXT_MAX_CHARS + 1)}, "invalid_default"),
    ("default-number-bad", {"name": "N", "type": "number", "default": "1,5"}, "invalid_default"),
    ("default-number-digits", {"name": "N", "type": "number", "precision": 0,
                               "default": "9" * 38 + ".9"}, "invalid_default"),
    ("default-url-bad", {"name": "N", "type": "url", "default": "ftp://x"}, "invalid_default"),
    ("default-bool-int", {"name": "N", "type": "boolean", "default": 1}, "invalid_default"),
    ("default-enum-unknown", {"name": "N", "type": "enum", "options": ["A"], "default": "B"},
     "invalid_default"),
    ("default-multi-string", {"name": "N", "type": "enum", "options": ["A"], "multiple": True,
                              "default": "A"}, "invalid_default"),
    ("key-malformed", {"name": "N", "type": "text", "key": "c_XYZ"}, "invalid_key"),
    ("key-short", {"name": "N", "type": "text", "key": "c_1234567"}, "invalid_key"),
    ("key-uppercase", {"name": "N", "type": "text", "key": "c_ABCDEF12"}, "invalid_key"),
    ("key-prefix", {"name": "N", "type": "text", "key": "d_12345678"}, "invalid_key"),
    ("key-int", {"name": "N", "type": "text", "key": 12345678}, "invalid_key"),
    ("key-duplicate", {"name": "N", "type": "text", "key": "c_aaaaaaaa"}, "duplicate_key"),
]


@pytest.mark.parametrize("spec,code", _cases(SPEC_REJECT))
def test_parse_column_spec_reject(spec, code):
    allow_key = isinstance(spec, dict) and "key" in spec
    with pytest.raises(ColumnSpecError) as info:
        parse_column_spec(spec, existing=EXISTING, allow_key=allow_key)
    assert info.value.code == code
    assert isinstance(info.value, TableSchemaError)
    assert str(info.value) == info.value.message
    if isinstance(spec, dict) and isinstance(spec.get("name"), str):
        assert info.value.column == spec["name"]


@pytest.mark.parametrize("name", ["_id", "_ID", "_created_at", "_UPDATED_at"])
def test_rename_column_to_reserved_name_is_refused(name):
    from chat.tables.schema_ops import SchemaOperationError, apply_schema_operations

    columns = [Column(key="c_aaaaaaaa", name="Link", type="text")]
    ops = [{"op": "rename_column", "name": "Link", "new_name": name}]
    with pytest.raises(SchemaOperationError) as info:
        apply_schema_operations(columns, ops, table_empty=True)
    assert (info.value.code, info.value.column) == ("reserved_name", name)


@pytest.mark.parametrize("name", ["_idx", "id", "created_at", "_id_", "x_id"])
def test_names_near_reserved_ones_are_accepted(name):
    assert validate_column_name(name) == name


@pytest.mark.parametrize(
    "key", ["c_0123abcd", None, "bad", 5, ["a"], {}, "c_aaaaaaaa"],
    ids=["valid", "null", "malformed", "int", "list", "dict", "existing"],
)
def test_parse_column_spec_refuses_key_by_default(key):
    with pytest.raises(ColumnSpecError) as info:
        parse_column_spec({"name": "N", "type": "text", "key": key}, existing=EXISTING)
    assert (info.value.code, info.value.field) == ("unknown_field", "key")
    assert "assigned by the server" in info.value.message


def test_parse_columns_refuses_key_by_default():
    specs = [{"name": "A", "type": "text"}, {"name": "B", "type": "text",
                                             "key": "c_11111111"}]
    with pytest.raises(ColumnSpecError) as info:
        parse_columns(specs)
    assert (info.value.code, info.value.field) == ("unknown_field", "key")
    columns = parse_columns(specs, allow_key=True)
    assert columns[1].key == "c_11111111"
    assert is_valid_column_key(columns[0].key)


def test_parse_column_spec_default_error_names_reason():
    with pytest.raises(ColumnSpecError) as info:
        parse_column_spec({"name": "Price", "type": "number", "default": "abc"})
    assert info.value.field == "default"
    assert "Price" in str(info.value)
    assert "decimal" in str(info.value)


def test_parse_column_spec_too_many_columns():
    full = _many_columns(TABLE_MAX_COLUMNS)
    with pytest.raises(ColumnSpecError) as info:
        parse_column_spec({"name": "One more", "type": "text"}, existing=full)
    assert info.value.code == "too_many_columns"
    column = parse_column_spec({"name": "Last", "type": "text"}, existing=full[:-1])
    assert column.name == "Last"


# --- parse_columns --------------------------------------------------------------


def test_parse_columns_ok():
    columns = parse_columns(
        [
            {"name": "Title", "type": "text", "required": True},
            {"name": "Status", "type": "enum", "options": ["Open", "Closed"]},
        ]
    )
    assert [c.name for c in columns] == ["Title", "Status"]
    assert len({c.key for c in columns}) == 2


def test_parse_columns_max():
    specs = [{"name": f"c{i}", "type": "text"} for i in range(TABLE_MAX_COLUMNS)]
    assert len(parse_columns(specs)) == TABLE_MAX_COLUMNS


@pytest.mark.parametrize(
    "specs,code",
    [
        pytest.param([], "no_columns", id="empty"),
        pytest.param(
            [{"name": f"c{i}", "type": "text"} for i in range(TABLE_MAX_COLUMNS + 1)],
            "too_many_columns",
            id="101",
        ),
        pytest.param(
            [{"name": "Title", "type": "text"}, {"name": "title", "type": "number"}],
            "duplicate_name",
            id="duplicate-name",
        ),
        pytest.param(
            [{"name": "A", "type": "text", "key": "c_11111111"},
             {"name": "B", "type": "text", "key": "c_11111111"}],
            "duplicate_key",
            id="duplicate-key",
        ),
        pytest.param({"name": "A", "type": "text"}, "invalid_spec", id="not-a-list"),
        pytest.param(["A"], "invalid_spec", id="item-not-a-dict"),
    ],
)
def test_parse_columns_reject(specs, code):
    with pytest.raises(ColumnSpecError) as info:
        parse_columns(specs, allow_key=True)
    assert info.value.code == code


# --- Column round trip, load_schema ---------------------------------------------

ROUND_TRIP_COLUMNS = [
    TEXT,
    dataclasses.replace(TEXT, required=True, unique=True, default="x", description="d"),
    dataclasses.replace(NUMBER, precision=2, default="1.5"),
    URL,
    dataclasses.replace(BOOL, default=True),
    dataclasses.replace(ENUM, default="B"),
    dataclasses.replace(MULTI, default=["A", "C"]),
    UNICODE_MULTI,
]


@pytest.mark.parametrize("column", ROUND_TRIP_COLUMNS, ids=lambda c: f"{c.type}-{c.key}")
def test_column_round_trip(column):
    data = column.to_dict()
    assert list(data) == [
        "key", "name", "type", "required", "unique", "default",
        "options", "multiple", "precision", "description",
    ]
    assert Column.from_dict(data) == column
    assert Column.from_dict(data).to_dict() == data
    text = json.dumps(data)
    assert json.dumps(Column.from_dict(json.loads(text)).to_dict()) == text


def test_to_dict_options_is_list():
    assert ENUM.to_dict()["options"] == ["A", "B", "C"]
    assert TEXT.to_dict()["options"] is None


def _stored(**changes):
    data = dataclasses.replace(ENUM, default="A").to_dict()
    data.update(changes)
    return data


def _without(field):
    data = TEXT.to_dict()
    del data[field]
    return data


FROM_DICT_REJECT = [
    ("not-a-dict", ["x"], "invalid_spec"),
    ("missing-key", _without("key"), "missing_field"),
    ("missing-description", _without("description"), "missing_field"),
    ("missing-default", _without("default"), "missing_field"),
    ("extra-field", _stored(extra=1), "unknown_field"),
    ("key-null", _stored(key=None), "invalid_key"),
    ("key-bad", _stored(key="c_ZZZZZZZZ"), "invalid_key"),
    ("type-bad", _stored(type="date"), "invalid_type"),
    ("name-bad", _stored(name=" x"), "invalid_name"),
    ("required-int", _stored(required=0), "invalid_field"),
    ("multiple-null", _stored(multiple=None), "invalid_field"),
    ("description-null", _stored(description=None), "invalid_description"),
    ("options-empty", _stored(options=[]), "invalid_options"),
    ("options-dup", _stored(options=["A", "a"]), "duplicate_option"),
    ("default-unknown", _stored(default="Z"), "invalid_default"),
    ("precision-on-enum", _stored(precision=2), "field_not_allowed"),
    ("unique-on-enum", _stored(unique=True), "unique_not_allowed"),
]


@pytest.mark.parametrize("data,code", _cases(FROM_DICT_REJECT))
def test_from_dict_reject(data, code):
    with pytest.raises(ColumnSpecError) as info:
        Column.from_dict(data)
    assert info.value.code == code


def test_load_and_dump_schema():
    columns = list(ROUND_TRIP_COLUMNS[:1]) + [NUMBER, URL, BOOL, ENUM, MULTI]
    data = dump_schema(columns)
    assert load_schema(json.loads(json.dumps(data))) == columns
    assert load_schema([]) == []


@pytest.mark.parametrize(
    "data,code",
    [
        pytest.param({"key": "c_00000001"}, "invalid_spec", id="not-a-list"),
        pytest.param(
            [TEXT.to_dict(), dataclasses.replace(NUMBER, name="NOTES").to_dict()],
            "duplicate_name",
            id="duplicate-name",
        ),
        pytest.param(
            [TEXT.to_dict(), dataclasses.replace(NUMBER, key=TEXT.key).to_dict()],
            "duplicate_key",
            id="duplicate-key",
        ),
        pytest.param([TEXT.to_dict(), {"name": "x"}], "missing_field", id="bad-column"),
        pytest.param(
            dump_schema(_many_columns(TABLE_MAX_COLUMNS + 1)),
            "too_many_columns",
            id="too-many",
        ),
    ],
)
def test_load_schema_reject(data, code):
    with pytest.raises(ColumnSpecError) as info:
        load_schema(data)
    assert info.value.code == code


# --- error classes --------------------------------------------------------------


def test_table_value_error_attributes():
    with pytest.raises(TableValueError) as info:
        canonicalize(NUMBER, "abc")
    err = info.value
    assert err.code == "invalid_number"
    assert err.column == "Price"
    assert str(err) == err.message
    assert "Price" in err.message
    assert isinstance(err, ValueError)
    assert not isinstance(err, TableSchemaError)


def test_column_spec_error_attributes():
    err = ColumnSpecError("invalid_name", "bad", column="X", field="name")
    assert (err.code, err.message, err.column, err.field) == ("invalid_name", "bad", "X", "name")
    assert str(err) == "bad"
    assert isinstance(err, TableSchemaError) and isinstance(err, ValueError)


def test_table_value_error_detail_has_no_column_prefix():
    with pytest.raises(TableValueError) as info:
        coerce_value(NUMBER, "abc")
    err = info.value
    assert err.message == f"Column 'Price': {err.detail}"
    assert "Price" not in err.detail
    plain = TableValueError("x", "msg", column="C")
    assert plain.detail == "msg"


def test_unknown_option_message_lists_options():
    with pytest.raises(TableValueError) as info:
        coerce_value(ENUM, "Z")
    assert info.value.detail == "'Z' is not one of the options (options: 'A', 'B', 'C')."
    many = Column(key="c_00000008", name="Many", type="enum",
                  options=tuple(f"o{i}" for i in range(13)), multiple=True)
    with pytest.raises(TableValueError) as info:
        coerce_value(many, ["o1", "nope"])
    assert info.value.code == "unknown_option"
    assert "'o9' and 3 more" in info.value.detail
    assert "'o10'" not in info.value.detail


# --- coerce_option --------------------------------------------------------------


def test_coerce_option():
    assert coerce_option(MULTI, "B") == "B"
    assert coerce_option(ENUM, "C") == "C"
    assert_option_error = [(MULTI, "b", "unknown_option"), (MULTI, ["B"], "wrong_type"),
                           (MULTI, None, "wrong_type"), (ENUM, 1, "wrong_type")]
    for column, value, code in assert_option_error:
        with pytest.raises(TableValueError) as info:
            coerce_option(column, value)
        assert info.value.code == code
    with pytest.raises(ValueError):
        coerce_option(TEXT, "B")


# --- immutability ---------------------------------------------------------------


@pytest.mark.parametrize("column", ROUND_TRIP_COLUMNS, ids=lambda c: f"{c.type}-{c.key}")
def test_column_is_hashable(column):
    hash(column)
    assert column in {column}
    assert Column.from_dict(column.to_dict()) in {column}


def test_column_copies_input_lists():
    options = ["A", "B"]
    default = ["B"]
    direct = Column(key="c_0000000a", name="L", type="enum", options=options,
                    multiple=True, default=default)
    parsed = parse_column_spec({"name": "P", "type": "enum", "options": options,
                                "multiple": True, "default": default})
    options.append("Z")
    default.append("A")
    for column in (direct, parsed):
        assert column.options == ("A", "B")
        assert column.default == ("B",)
        assert column.to_dict()["options"] == ["A", "B"]
        assert column.to_dict()["default"] == ["B"]
        hash(column)
    assert dataclasses.replace(direct, default=["A"]).default == ("A",)


def test_from_dict_normalizes_stored_values():
    data = dataclasses.replace(NUMBER, default="1").to_dict()
    data.update(description=" pad ", default=1.5)
    column = Column.from_dict(data)
    assert column.description == "pad"
    assert column.default == "1.5"


# --- validate_table_title -------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        pytest.param("Inventory", "Inventory", id="plain"),
        pytest.param("  Inventory \n", "Inventory", id="stripped"),
        pytest.param("t" * TABLE_TITLE_MAX_LEN, "t" * TABLE_TITLE_MAX_LEN, id="max"),
        pytest.param("Caf\u00e9 \U0001f600", "Caf\u00e9 \U0001f600", id="unicode"),
        pytest.param("\U0001f4ca Sales", "\U0001f4ca Sales", id="emoji"),
    ],
)
def test_validate_table_title_accept(title, expected):
    assert validate_table_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("t" * (TABLE_TITLE_MAX_LEN + 1), id="too-long"),
        pytest.param(7, id="int"),
        pytest.param(None, id="none"),
        pytest.param(["a"], id="list"),
        pytest.param("a\nb", id="newline"),
        pytest.param("a\tb", id="tab"),
        pytest.param("a\x00b", id="nul"),
        pytest.param("a\x85b", id="c1-control"),
        pytest.param("a\u200bb", id="zero-width-space"),
        pytest.param("a\u202eb", id="bidi-override"),
        pytest.param("a\u2028b", id="line-separator"),
        pytest.param("a\u2029b", id="paragraph-separator"),
        pytest.param("a\ud800b", id="surrogate"),
    ],
)
def test_validate_table_title_reject(title):
    with pytest.raises(TableSchemaError) as info:
        validate_table_title(title)
    assert (info.value.code, info.value.field) == ("invalid_title", "title")


# --- Error message echo limits ----------------------------------------------------


def test_clip_repr():
    assert clip_repr("abc") == "'abc'"
    assert clip_repr(5) == "'5'"
    clipped = clip_repr("x" * 1000)
    assert clipped == repr("x" * 100) + "\u2026"


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({"name": "n" * 100_000, "type": "text"}, id="long-name"),
        pytest.param({"name": " " + "n" * 50, "type": "text"}, id="name-whitespace"),
        pytest.param({"name": "N", "type": "text", "f" * 100_000: 1}, id="unknown-field"),
        pytest.param({"name": "N", "type": "enum", "options": [" " + "o" * 99]},
                     id="option-whitespace"),
    ],
)
def test_error_messages_are_bounded(spec):
    with pytest.raises(ColumnSpecError) as info:
        parse_column_spec(spec)
    assert len(info.value.message) < 400


def test_unknown_option_message_is_bounded():
    with pytest.raises(TableValueError) as info:
        coerce_value(ENUM, "z" * 100_000)
    assert len(info.value.message) < 400
