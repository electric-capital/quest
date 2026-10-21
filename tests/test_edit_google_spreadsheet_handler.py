"""Tests for the edit_google_spreadsheet action_request handler.

Covers the A1-range helpers, cell normalization/comparison,
``validate_params`` (unknown keys, range shapes, grid dimensions, cell
types), ``validate_against_upstream`` (live current_values verification,
tab/bounds checks, context-window capture, transient-failure
fallthrough), ``render_preview`` (spreadsheet_diff grid payload), and
``execute`` (scope pre-check, approve-time re-verification, the
values.update call) via a mocked ``make_authenticated_request``.
"""

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


def _handler():
    from chat.action_request_types.edit_google_spreadsheet import EditGoogleSpreadsheetHandler
    return EditGoogleSpreadsheetHandler()


SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg"


def _params(**overrides):
    params = {
        "spreadsheet_id": SHEET_ID,
        "tab": "Sheet1",
        "range": "B2:C3",
        "values": [["Q3", 125000], ["Q4", 150000]],
        "current_values": [["Q3", "100,000"], ["Q4", ""]],
    }
    params.update(overrides)
    return params


def _user(scopes=("https://www.googleapis.com/auth/spreadsheets",)):
    return {
        "id": 1,
        "google_services_oauth": {
            "access_token": "tok",
            "scopes": list(scopes),
        },
    }


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def _fake_sheets_api(monkeypatch, *, meta=None, values_by_option=None, captured=None):
    """Patch make_authenticated_request with a URL-routing fake.

    values_by_option maps "FORMATTED_VALUE" / "UNFORMATTED_VALUE" to the
    2d array returned for values GETs with that render option.
    """
    meta = meta or {
        "properties": {"title": "Budget 2026"},
        "sheets": [
            {
                "properties": {
                    "title": "Sheet1",
                    "sheetId": 42,
                    "gridProperties": {"rowCount": 100, "columnCount": 26},
                }
            }
        ],
    }
    values_by_option = dict(values_by_option or {})
    # Most tests exercise formula-free sheets where the FORMULA rendering
    # equals the unformatted one; tests about formulas pass it explicitly.
    if "FORMULA" not in values_by_option and "UNFORMATTED_VALUE" in values_by_option:
        values_by_option["FORMULA"] = values_by_option["UNFORMATTED_VALUE"]

    async def _fake_request(client, user, method, url, **kwargs):
        if captured is not None:
            captured.append({"method": method, "url": url, "kwargs": kwargs})
        if "?fields=" in url:
            return _FakeResponse(meta)
        if method == "PUT":
            return _FakeResponse({"updatedRange": "'Sheet1'!B2:C3", "updatedCells": 4})
        for option, values in values_by_option.items():
            if f"valueRenderOption={option}" in url:
                return _FakeResponse({"values": values})
        return _FakeResponse({}, status_code=500)

    monkeypatch.setattr(
        "chat.action_request_types.edit_google_spreadsheet.make_authenticated_request",
        _fake_request,
    )


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_handler_metadata():
    from db.models import ActionRequestType

    handler = _handler()
    assert handler.type_name == ActionRequestType.EDIT_GOOGLE_SPREADSHEET
    assert handler.display_name == "Edit Google Sheet"
    assert handler.approve_label == "Apply"


def test_handler_is_registered():
    from chat.action_request_types import get_handler

    assert get_handler("edit_google_spreadsheet") is not None


# ---------------------------------------------------------------------------
# A1 helpers
# ---------------------------------------------------------------------------


def test_col_letter_roundtrip():
    from chat.action_request_types.edit_google_spreadsheet import _col_to_index, _index_to_col

    for index, letters in [(1, "A"), (26, "Z"), (27, "AA"), (52, "AZ"), (703, "AAA")]:
        assert _col_to_index(letters) == index
        assert _index_to_col(index) == letters


def test_parse_range_single_cell_and_rect():
    from chat.action_request_types.edit_google_spreadsheet import _parse_range

    assert _parse_range("B2") == (2, 2, 2, 2)
    assert _parse_range("B2:D5") == (2, 2, 5, 4)
    # Reversed corners are normalized.
    assert _parse_range("D5:B2") == (2, 2, 5, 4)
    # $ anchors are tolerated.
    assert _parse_range("$B$2:$D$5") == (2, 2, 5, 4)


def test_parse_range_rejects_sheet_prefix_and_unbounded():
    from chat.action_request_types.edit_google_spreadsheet import _parse_range

    with pytest.raises(ValueError, match="tab"):
        _parse_range("Sheet1!B2:D5")
    for bad in ("A:A", "1:3", "A1:B", "", "B0"):
        with pytest.raises(ValueError):
            _parse_range(bad)


def test_normalize_and_compare_cells():
    from chat.action_request_types.edit_google_spreadsheet import (
        _cell_matches_live,
        _cells_equal,
        _normalize_cell,
    )

    assert _normalize_cell(None) == ""
    assert _normalize_cell(True) == "TRUE"
    assert _normalize_cell(1234.0) == "1234"
    assert _normalize_cell("  x  ") == "x"
    assert _cells_equal("1234.50", 1234.5)
    assert not _cells_equal("a", "b")
    # Claimed value may match the formatted, unformatted, or formula
    # rendering for plain cells...
    assert _cell_matches_live("1,234.50", "1,234.50", 1234.5, 1234.5)
    assert _cell_matches_live(1234.5, "1,234.50", 1234.5, 1234.5)
    assert not _cell_matches_live("999", "1,234.50", 1234.5, 1234.5)
    # ...but a formula cell only matches its formula text.
    assert _cell_matches_live("=C2-D2", "50", 50, "=C2-D2")
    assert not _cell_matches_live("50", "50", 50, "=C2-D2")


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def test_validate_params_happy_path_normalizes():
    handler = _handler()
    out = handler.validate_params(_params(range="C3:B2"))
    assert out["range"] == "B2:C3"
    assert out["spreadsheet_id"] == SHEET_ID
    assert out["tab"] == "Sheet1"
    assert out["values"] == [["Q3", 125000], ["Q4", 150000]]


def test_validate_params_rejects_unknown_field():
    handler = _handler()
    with pytest.raises(ValueError, match="Unknown parameter for edit_google_spreadsheet"):
        handler.validate_params(_params(value_input_option="RAW"))


def test_validate_params_rejects_server_injected_keys():
    handler = _handler()
    with pytest.raises(ValueError, match="Unknown parameter"):
        handler.validate_params(_params(context_grid={}))


def test_validate_params_rejects_full_url_as_spreadsheet_id():
    handler = _handler()
    with pytest.raises(ValueError, match="spreadsheet_id"):
        handler.validate_params(
            _params(spreadsheet_id=f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit")
        )


def test_validate_params_rejects_missing_tab():
    handler = _handler()
    with pytest.raises(ValueError, match="tab"):
        handler.validate_params(_params(tab="  "))


def test_validate_params_rejects_row_count_mismatch():
    handler = _handler()
    with pytest.raises(ValueError, match="values has 1 row"):
        handler.validate_params(_params(values=[["only", "one"]]))


def test_validate_params_allows_cells_up_to_the_sheets_maximum():
    # Google Sheets accepts up to 50,000 characters per cell; a user hit the
    # old 5,000-character cap writing long text, so the handler must accept
    # the full Sheets range. current_values go through the same check, so
    # a cell that already holds long text must also be claimable.
    from chat.action_request_types.edit_google_spreadsheet import MAX_CELL_CHARS

    assert MAX_CELL_CHARS == 50_000
    handler = _handler()
    long_text = "x" * MAX_CELL_CHARS
    out = handler.validate_params(
        _params(values=[[long_text, 1], ["Q4", 2]],
                current_values=[[long_text, ""], ["Q4", ""]])
    )
    assert out["values"][0][0] == long_text


def test_validate_params_rejects_cell_over_the_sheets_maximum():
    from chat.action_request_types.edit_google_spreadsheet import MAX_CELL_CHARS

    handler = _handler()
    with pytest.raises(ValueError, match=r"values\[0\]\[0\] exceeds the 50,000-character cell limit"):
        handler.validate_params(_params(values=[["x" * (MAX_CELL_CHARS + 1), 1], ["Q4", 2]]))


def test_validate_params_rejects_col_count_mismatch():
    handler = _handler()
    with pytest.raises(ValueError, match=r"current_values\[0\] has 3 cell"):
        handler.validate_params(
            _params(current_values=[["a", "b", "c"], ["d", "e"]])
        )


def test_validate_params_rejects_null_in_values():
    handler = _handler()
    with pytest.raises(ValueError, match="null"):
        handler.validate_params(_params(values=[["Q3", None], ["Q4", 1]]))


def test_validate_params_allows_null_in_current_values():
    handler = _handler()
    out = handler.validate_params(_params(current_values=[["Q3", None], [None, ""]]))
    assert out["current_values"] == [["Q3", None], [None, ""]]


def test_validate_params_rejects_nested_list_cell():
    handler = _handler()
    with pytest.raises(ValueError, match="string, number, or"):
        handler.validate_params(_params(values=[["Q3", ["nested"]], ["Q4", 1]]))


def test_validate_params_rejects_oversize_range():
    handler = _handler()
    with pytest.raises(ValueError, match="limited to"):
        handler.validate_params(_params(range="A1:BZ500", values=[[]], current_values=[[]]))


def test_validate_params_rejects_too_many_cells():
    handler = _handler()
    # 100 rows x 26 cols = 2600 cells > 1000 but within row/col caps.
    with pytest.raises(ValueError, match="1000 cells"):
        handler.validate_params(_params(range="A1:Z100", values=[[]], current_values=[[]]))


# ---------------------------------------------------------------------------
# validate_against_upstream
# ---------------------------------------------------------------------------


def _window_values():
    # Window is A1:E5 (target B2:C3 expanded by 2, clamped at the sheet
    # edge). Rows are ragged on purpose -- the API omits trailing blanks.
    return [
        ["h1", "h2", "h3"],
        ["r2", "Q3", "100,000", "x"],
        ["r3", "Q4"],
        ["r4"],
    ]


def test_upstream_match_injects_context_and_title(monkeypatch):
    handler = _handler()
    captured = []
    _fake_sheets_api(
        monkeypatch,
        values_by_option={
            "FORMATTED_VALUE": _window_values(),
            "UNFORMATTED_VALUE": [["Q3", 100000], ["Q4"]],
        },
        captured=captured,
    )

    out = _run(handler.validate_against_upstream(
        handler.validate_params(_params()), _user(),
    ))

    assert out["spreadsheet_title"] == "Budget 2026"
    assert out["sheet_gid"] == 42
    grid = out["context_grid"]
    assert grid["start_row"] == 1 and grid["start_col"] == 1
    # 5 rows x 5 cols, padded dense.
    assert len(grid["rows"]) == 5
    assert all(len(row) == 5 for row in grid["rows"])
    assert grid["rows"][1][1] == "Q3"
    assert grid["rows"][1][2] == "100,000"
    # The window fetch used the quoted tab and the expanded range.
    window_urls = [c["url"] for c in captured if "FORMATTED_VALUE" in c["url"]]
    assert any("A1%3AE5" in url for url in window_urls)


def test_upstream_mismatch_raises_with_cell_diff(monkeypatch):
    handler = _handler()
    _fake_sheets_api(
        monkeypatch,
        values_by_option={
            "FORMATTED_VALUE": _window_values(),
            "UNFORMATTED_VALUE": [["Q3", 100000], ["Q4"]],
        },
    )

    params = handler.validate_params(
        _params(current_values=[["Q3", "999"], ["WRONG", ""]])
    )
    with pytest.raises(ValueError, match="C2.*B3") as excinfo:
        _run(handler.validate_against_upstream(params, _user()))
    assert "have not read" in str(excinfo.value)


def test_upstream_unknown_tab_lists_available(monkeypatch):
    handler = _handler()
    _fake_sheets_api(monkeypatch)
    params = handler.validate_params(_params(tab="Nope"))
    with pytest.raises(ValueError, match="Available tabs.*Sheet1"):
        _run(handler.validate_against_upstream(params, _user()))


def test_upstream_range_beyond_grid_rejected(monkeypatch):
    handler = _handler()
    _fake_sheets_api(monkeypatch)
    params = handler.validate_params(
        _params(
            range="B101:C102",
            values=[["a", "b"], ["c", "d"]],
            current_values=[["", ""], ["", ""]],
        )
    )
    with pytest.raises(ValueError, match="beyond the tab's grid"):
        _run(handler.validate_against_upstream(params, _user()))


def test_upstream_404_spreadsheet_rejected(monkeypatch):
    handler = _handler()

    async def _fake_request(client, user, method, url, **kwargs):
        return _FakeResponse({}, status_code=404)

    monkeypatch.setattr(
        "chat.action_request_types.edit_google_spreadsheet.make_authenticated_request",
        _fake_request,
    )
    params = handler.validate_params(_params())
    with pytest.raises(ValueError, match="Spreadsheet not found"):
        _run(handler.validate_against_upstream(params, _user()))


def test_upstream_network_error_falls_through(monkeypatch):
    handler = _handler()

    async def _fake_request(client, user, method, url, **kwargs):
        raise ConnectionError("boom")

    monkeypatch.setattr(
        "chat.action_request_types.edit_google_spreadsheet.make_authenticated_request",
        _fake_request,
    )
    params = handler.validate_params(_params())
    out = _run(handler.validate_against_upstream(params, _user()))
    # Unverified but not rejected; execute() re-checks at Approve time.
    assert out == params
    assert "context_grid" not in out


def test_upstream_skipped_without_google_connection():
    handler = _handler()
    params = handler.validate_params(_params())
    out = _run(handler.validate_against_upstream(
        params, {"id": 1, "google_services_oauth": {}},
    ))
    assert out == params


# ---------------------------------------------------------------------------
# render_preview
# ---------------------------------------------------------------------------


def test_render_preview_with_context_grid():
    handler = _handler()
    params = handler.validate_params(_params())
    params["spreadsheet_title"] = "Budget 2026"
    params["context_grid"] = {
        "start_row": 1,
        "start_col": 1,
        "rows": [
            ["h1", "h2", "h3", "", ""],
            ["r2", "Q3", "100,000", "x", ""],
            ["r3", "Q4", "", "", ""],
            ["r4", "", "", "", ""],
            ["", "", "", "", ""],
        ],
    }

    fields = _run(handler.render_preview(params))
    assert fields[0] == {"key": "Spreadsheet", "value": "Budget 2026"}
    assert fields[1] == {"key": "Tab", "value": "Sheet1"}
    assert fields[2]["value"].startswith("B2:C3")

    changes = fields[3]
    assert changes["type"] == "spreadsheet_diff"
    grid = changes["grid"]
    assert grid["start_row"] == 1 and grid["start_col"] == 1
    # The all-blank context row 5 and column E are trimmed; row 1 (headers)
    # and column A/D (labels, "x") survive.
    assert len(grid["current"]) == 4
    assert all(len(row) == 4 for row in grid["current"])
    assert grid["target_start_row"] == 2 and grid["target_start_col"] == 2
    assert grid["target_end_row"] == 3 and grid["target_end_col"] == 3
    assert grid["new"] == [["Q3", "125000"], ["Q4", "150000"]]
    # B2 "Q3" -> "Q3" unchanged; C2 100,000 -> 125000 changed;
    # B3 "Q4" -> "Q4" unchanged; C3 "" -> 150000 changed.
    assert grid["changed"] == [[False, True], [False, True]]
    assert "2 of 4" in changes["value"]


def test_render_preview_without_context_falls_back_to_target():
    handler = _handler()
    params = handler.validate_params(_params())

    fields = _run(handler.render_preview(params))
    changes = fields[3]
    grid = changes["grid"]
    # Window degenerates to the target range itself.
    assert grid["start_row"] == 2 and grid["start_col"] == 2
    assert grid["current"] == [["Q3", "100,000"], ["Q4", ""]]
    assert grid["changed"] == [[False, True], [False, True]]
    # Falls back to the raw id when no title was captured.
    assert fields[0]["value"] == SHEET_ID


def test_render_preview_trims_all_blank_context():
    handler = _handler()
    params = handler.validate_params(
        _params(values=[["a", "b"], ["c", "d"]], current_values=[["Q3", "100"], ["Q4", ""]])
    )
    params["context_grid"] = {
        "start_row": 1,
        "start_col": 1,
        "rows": [
            ["", "", "", "", ""],
            ["", "Q3", "100", "", ""],
            ["", "Q4", "", "", ""],
            ["", "", "", "", ""],
            ["", "", "", "", ""],
        ],
    }

    grid = _run(handler.render_preview(params))[3]["grid"]
    # Every context row/col around the target is blank -> the window
    # collapses to the target rectangle itself.
    assert grid["start_row"] == 2 and grid["start_col"] == 2
    assert grid["current"] == [["Q3", "100"], ["Q4", ""]]
    # Target rows are never trimmed, even the blank-ish ones.
    assert grid["changed"] == [[True, True], [True, True]]


# ---------------------------------------------------------------------------
# Formula cells
# ---------------------------------------------------------------------------


def _formula_window_options():
    # Target B2:C3; window A1:E5. C3 holds a formula.
    return {
        "FORMATTED_VALUE": [
            ["h1", "h2", "h3"],
            ["r2", "Q3", "100,000"],
            ["r3", "Q4", "500"],
        ],
        "FORMULA": [
            ["h1", "h2", "h3"],
            ["r2", "Q3", 100000],
            ["r3", "Q4", "=SUM(A1:A2)"],
        ],
        "UNFORMATTED_VALUE": [["Q3", 100000], ["Q4", 500]],
    }


def test_upstream_rejects_computed_value_for_formula_cell(monkeypatch):
    handler = _handler()
    _fake_sheets_api(monkeypatch, values_by_option=_formula_window_options())

    params = handler.validate_params(
        _params(current_values=[["Q3", "100,000"], ["Q4", "500"]])
    )
    with pytest.raises(ValueError, match="formula") as excinfo:
        _run(handler.validate_against_upstream(params, _user()))
    assert "=SUM(A1:A2)" in str(excinfo.value)
    assert "valueRenderOption=FORMULA" in str(excinfo.value)


def test_upstream_accepts_formula_text_and_displays_it(monkeypatch):
    handler = _handler()
    _fake_sheets_api(monkeypatch, values_by_option=_formula_window_options())

    params = handler.validate_params(
        _params(current_values=[["Q3", "100,000"], ["Q4", "=SUM(A1:A2)"]])
    )
    out = _run(handler.validate_against_upstream(params, _user()))
    rows = out["context_grid"]["rows"]
    # The context grid shows the formula text, not the computed value...
    assert rows[2][2] == "=SUM(A1:A2)"
    # ...while non-formula cells keep the formatted rendering.
    assert rows[1][2] == "100,000"


def test_execute_rejects_computed_value_for_formula_cell(monkeypatch):
    handler = _handler()
    captured = []
    _fake_sheets_api(
        monkeypatch,
        values_by_option={
            "FORMATTED_VALUE": [["Q3", "100,000"], ["Q4", "500"]],
            "UNFORMATTED_VALUE": [["Q3", 100000], ["Q4", 500]],
            "FORMULA": [["Q3", 100000], ["Q4", "=SUM(A1:A2)"]],
        },
        captured=captured,
    )

    params = handler.validate_params(
        _params(current_values=[["Q3", "100,000"], ["Q4", "500"]])
    )
    with pytest.raises(RuntimeError, match="formula"):
        _run(handler.execute(params, _user()))
    assert not [c for c in captured if c["method"] == "PUT"]


def test_execute_accepts_formula_text(monkeypatch):
    handler = _handler()
    captured = []
    _fake_sheets_api(
        monkeypatch,
        values_by_option={
            "FORMATTED_VALUE": [["Q3", "100,000"], ["Q4", "500"]],
            "UNFORMATTED_VALUE": [["Q3", 100000], ["Q4", 500]],
            "FORMULA": [["Q3", 100000], ["Q4", "=SUM(A1:A2)"]],
        },
        captured=captured,
    )

    params = handler.validate_params(
        _params(current_values=[["Q3", "100,000"], ["Q4", "=SUM(A1:A2)"]])
    )
    result = _run(handler.execute(params, _user()))
    assert result["success"] is True
    assert len([c for c in captured if c["method"] == "PUT"]) == 1


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_rejects_when_not_connected():
    handler = _handler()
    with pytest.raises(RuntimeError, match="not connected"):
        _run(handler.execute(_params(), {"id": 1, "google_services_oauth": {}}))


def test_execute_rejects_when_missing_write_scope():
    handler = _handler()
    user = _user(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    with pytest.raises(RuntimeError, match="reconnecting Google Services"):
        _run(handler.execute(_params(), user))


def test_execute_applies_update(monkeypatch):
    handler = _handler()
    captured = []
    _fake_sheets_api(
        monkeypatch,
        values_by_option={
            "FORMATTED_VALUE": [["Q3", "100,000"], ["Q4"]],
            "UNFORMATTED_VALUE": [["Q3", 100000], ["Q4"]],
        },
        captured=captured,
    )

    params = handler.validate_params(_params())
    params["sheet_gid"] = 42
    result = _run(handler.execute(params, _user()))

    assert result["success"] is True
    assert result["updated_cells"] == 4
    assert result["range"] == "'Sheet1'!B2:C3"
    assert result["url"].endswith("#gid=42")

    puts = [c for c in captured if c["method"] == "PUT"]
    assert len(puts) == 1
    assert "valueInputOption=USER_ENTERED" in puts[0]["url"]
    body = puts[0]["kwargs"]["json"]
    assert body["values"] == [["Q3", 125000], ["Q4", 150000]]
    assert body["majorDimension"] == "ROWS"


def test_execute_rejects_when_sheet_changed(monkeypatch):
    handler = _handler()
    captured = []
    _fake_sheets_api(
        monkeypatch,
        values_by_option={
            "FORMATTED_VALUE": [["Q3", "999,999"], ["Q4"]],
            "UNFORMATTED_VALUE": [["Q3", 999999], ["Q4"]],
        },
        captured=captured,
    )

    params = handler.validate_params(_params())
    with pytest.raises(RuntimeError, match="changed since this request"):
        _run(handler.execute(params, _user()))
    # No write happened.
    assert not [c for c in captured if c["method"] == "PUT"]
