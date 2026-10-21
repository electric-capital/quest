"""EditGoogleSpreadsheetHandler -- approve-to-write action request for Google Sheets.

The model issues a
``create_action_request(request_type="edit_google_spreadsheet",
params={"spreadsheet_id": "...", "tab": "...", "range": "B2:D5",
"values": [[...]], "current_values": [[...]]}, reasoning="...")`` call
which appends an inline approval card to the chat. On Approve, the
handler overwrites the target range via the Sheets v4
``values.update`` endpoint with ``valueInputOption=USER_ENTERED``.

Read-before-write enforcement: ``current_values`` must carry the cell
values the model just read for the target range. The handler compares
them against the live sheet twice -- at proposal time in
``validate_against_upstream()`` (same-turn rejection, no card) and again
at Approve time in ``execute()`` (TOCTOU close: the sheet may have
changed while the card sat open). A mismatch means the model is about to
overwrite data it never read, so the write is refused with a per-cell
diff telling it to re-read the range.

Proposal-time validation also captures a small context window (the
target range expanded by up to ``CONTEXT_CELLS`` rows/cols on every
side, clamped to the sheet bounds) that the preview card renders as a
spreadsheet table with row numbers and column letters, highlighting the
cells that actually change.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from auth.google_credentials import make_authenticated_request
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._drive import (
    _extract_google_api_error,
    _get_authorized_google_scopes,
)
from chat.action_request_types._param_validation import reject_unknown_params
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

SHEETS_API_BASE = "https://sheets.googleapis.com/v4"

SPREADSHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# Context rows/cols captured around the target range for the preview card.
CONTEXT_CELLS = 2

# Bounds on the replaced rectangle. The card renders every cell, so keep
# a single request at a size a human can actually review.
MAX_RANGE_ROWS = 200
MAX_RANGE_COLS = 52
MAX_RANGE_CELLS = 1000
# Google Sheets' own per-cell ceiling; the card clips long cells with an
# ellipsis, so there is no reason to be stricter than the API.
MAX_CELL_CHARS = 50_000

# `spreadsheet_title`, `sheet_gid`, and `context_grid` are injected by
# validate_against_upstream() after the model-supplied params validate;
# they are not model-suppliable and therefore not in the allow-list
# (parallels `calendar_name` / `folder_name`).
_ALLOWED_PARAMS = frozenset(
    {"spreadsheet_id", "tab", "range", "values", "current_values"}
)

_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")
_RANGE_RE = re.compile(
    r"^([A-Za-z]{1,3})([1-9][0-9]{0,6})(?::([A-Za-z]{1,3})([1-9][0-9]{0,6}))?$"
)


def _sheets_reauth_message() -> str:
    return (
        "Google Sheets write access requires reconnecting Google Services "
        "via Settings > Data Connections."
    )


def _col_to_index(letters: str) -> int:
    """Convert a column letter run ("A", "AZ") to a 1-based column index."""
    index = 0
    for ch in letters.upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def _index_to_col(index: int) -> str:
    """Convert a 1-based column index to its letter run (1 -> "A")."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _parse_range(range_str: str) -> tuple[int, int, int, int]:
    """Parse a bounded A1 rectangle into (row1, col1, row2, col2), 1-based.

    Normalized so row1 <= row2 and col1 <= col2. Raises ValueError on
    anything that is not a bounded rectangle (full-column ``A:A`` /
    full-row ``1:3`` ranges have no fixed dimensions to match ``values``
    against, and sheet-qualified ranges must use the ``tab`` param).
    """
    cleaned = range_str.replace("$", "").strip()
    if "!" in cleaned:
        raise ValueError(
            "range must not include a sheet name (got "
            f"'{range_str}'); pass the sheet name in the separate 'tab' "
            "parameter and the bare cell range (e.g. 'B2:D5') in 'range'."
        )
    match = _RANGE_RE.match(cleaned)
    if not match:
        raise ValueError(
            f"range must be a bounded A1 rectangle like 'B2' or 'B2:D5' "
            f"(got '{range_str}'). Unbounded column/row ranges are not "
            "supported because the values grid must match the range "
            "dimensions exactly."
        )
    col1 = _col_to_index(match.group(1))
    row1 = int(match.group(2))
    if match.group(3):
        col2 = _col_to_index(match.group(3))
        row2 = int(match.group(4))
    else:
        col2, row2 = col1, row1
    return (
        min(row1, row2),
        min(col1, col2),
        max(row1, row2),
        max(col1, col2),
    )


def _format_range(row1: int, col1: int, row2: int, col2: int) -> str:
    start = f"{_index_to_col(col1)}{row1}"
    end = f"{_index_to_col(col2)}{row2}"
    return start if start == end else f"{start}:{end}"


def _quote_sheet_title(tab: str) -> str:
    """Quote a sheet title for use in an A1 range string."""
    return "'" + tab.replace("'", "''") + "'"


def _normalize_cell(value) -> str:
    """Canonical string form of a cell for equality comparison."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _cells_equal(claimed, live) -> bool:
    c = _normalize_cell(claimed)
    l = _normalize_cell(live)
    if c == l:
        return True
    # Numeric fallback: "1234.50" (claimed from a formatted read) vs
    # 1234.5 (unformatted) should not count as a mismatch.
    if c and l:
        try:
            return float(c) == float(l)
        except ValueError:
            return False
    return False


def _is_formula(value) -> bool:
    return isinstance(value, str) and value.strip().startswith("=")


def _cell_matches_live(claimed, live_formatted, live_unformatted, live_formula) -> bool:
    """Whether a claimed current_values cell matches the live cell.

    Formula cells are strict: the claim must be the formula text itself
    (the FORMULA rendering), not the computed value -- otherwise a model
    that only read computed values could silently destroy formulas.
    Plain cells are lenient: the model may have read the range with the
    default FORMATTED_VALUE render option ("1,234.50"), with
    UNFORMATTED_VALUE (1234.5), or with FORMULA (same as unformatted for
    non-formula cells); any of those counts as having read the cell.
    """
    if _is_formula(live_formula):
        return _cells_equal(claimed, live_formula)
    return (
        _cells_equal(claimed, live_formatted)
        or _cells_equal(claimed, live_unformatted)
        or _cells_equal(claimed, live_formula)
    )


def _pad_grid(values: list, n_rows: int, n_cols: int) -> list[list]:
    """Pad a Sheets values payload to a full n_rows x n_cols rectangle.

    The API omits trailing empty cells and trailing empty rows; the
    comparison and preview logic want a dense rectangle.
    """
    padded = []
    for r in range(n_rows):
        row = list(values[r]) if r < len(values) else []
        row = row[:n_cols] + [""] * max(0, n_cols - len(row))
        padded.append(row)
    return padded


def _validate_grid(
    grid, name: str, n_rows: int, n_cols: int, *, allow_none: bool
) -> list[list]:
    """Validate a 2d cell array against the target range dimensions."""
    if not isinstance(grid, list) or not grid:
        raise ValueError(f"{name} must be a non-empty 2d array of cell values")
    if len(grid) != n_rows:
        raise ValueError(
            f"{name} has {len(grid)} row(s) but the range spans {n_rows} "
            "row(s); the dimensions must match exactly."
        )
    validated = []
    for r, row in enumerate(grid):
        if not isinstance(row, list):
            raise ValueError(f"{name}[{r}] must be a list of cell values")
        if len(row) != n_cols:
            raise ValueError(
                f"{name}[{r}] has {len(row)} cell(s) but the range spans "
                f"{n_cols} column(s); the dimensions must match exactly."
            )
        cells = []
        for c, cell in enumerate(row):
            if cell is None:
                if not allow_none:
                    raise ValueError(
                        f"{name}[{r}][{c}] is null. Use an empty string "
                        '"" to clear a cell; null is not allowed in '
                        "values because the Sheets API treats it as "
                        "'leave the cell untouched'."
                    )
            elif not isinstance(cell, (str, int, float, bool)):
                raise ValueError(
                    f"{name}[{r}][{c}] must be a string, number, or "
                    "boolean."
                )
            elif isinstance(cell, str) and len(cell) > MAX_CELL_CHARS:
                raise ValueError(
                    f"{name}[{r}][{c}] exceeds the {MAX_CELL_CHARS:,}-"
                    "character cell limit (the Google Sheets maximum)."
                )
            cells.append(cell)
        validated.append(cells)
    return validated


def _collect_mismatches(
    claimed: list[list],
    live_formatted: list[list],
    live_unformatted: list[list],
    live_formula: list[list],
    row1: int,
    col1: int,
    limit: int = 5,
) -> list[str]:
    """Compare the claimed grid against the live renderings.

    Returns human-readable per-cell mismatch lines (A1 addresses), at
    most ``limit`` plus a trailing "... and N more" marker.
    """
    mismatches = []
    total = 0
    for r, row in enumerate(claimed):
        for c, cell in enumerate(row):
            formula_cell = live_formula[r][c]
            if _cell_matches_live(
                cell, live_formatted[r][c], live_unformatted[r][c], formula_cell
            ):
                continue
            total += 1
            if len(mismatches) < limit:
                addr = f"{_index_to_col(col1 + c)}{row1 + r}"
                if _is_formula(formula_cell):
                    mismatches.append(
                        f"{addr}: the cell contains the formula "
                        f"{_normalize_cell(formula_cell)!r} but "
                        f"current_values says {_normalize_cell(cell)!r} "
                        "(formula cells must be claimed by their formula "
                        "text, not their computed value)"
                    )
                else:
                    mismatches.append(
                        f"{addr}: current_values says "
                        f"{_normalize_cell(cell)!r} but the sheet contains "
                        f"{_normalize_cell(live_formatted[r][c])!r}"
                    )
    if total > len(mismatches):
        mismatches.append(f"... and {total - len(mismatches)} more cell(s)")
    return mismatches


def _trim_blank_context(
    window: list[list],
    win_r1: int,
    win_c1: int,
    row1: int,
    col1: int,
    row2: int,
    col2: int,
) -> tuple[list[list], int, int]:
    """Drop all-blank context rows/cols from the window edges.

    Trims outermost-inward and never past the target rectangle, so a
    target that sits next to empty sheet area isn't padded with useless
    blank context rows/columns. Returns (window, win_r1, win_c1).
    """
    def _row_blank(r: int) -> bool:
        return all(_normalize_cell(cell) == "" for cell in window[r])

    def _col_blank(c: int) -> bool:
        return all(_normalize_cell(row[c]) == "" for row in window)

    top, bottom = 0, len(window)
    while top < bottom and win_r1 + top < row1 and _row_blank(top):
        top += 1
    while bottom > top and win_r1 + bottom - 1 > row2 and _row_blank(bottom - 1):
        bottom -= 1
    window = window[top:bottom]

    n_cols = max((len(row) for row in window), default=0)
    left, right = 0, n_cols
    while left < right and win_c1 + left < col1 and _col_blank(left):
        left += 1
    while right > left and win_c1 + right - 1 > col2 and _col_blank(right - 1):
        right -= 1
    window = [row[left:right] for row in window]

    return window, win_r1 + top, win_c1 + left


async def _fetch_values(
    user: dict,
    spreadsheet_id: str,
    range_a1: str,
    value_render_option: str,
) -> httpx.Response:
    url = (
        f"{SHEETS_API_BASE}/spreadsheets/"
        f"{quote(spreadsheet_id, safe='')}/values/{quote(range_a1, safe='')}"
        f"?valueRenderOption={value_render_option}"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await make_authenticated_request(client, user, "GET", url)


class EditGoogleSpreadsheetHandler(ActionRequestHandler):
    """Overwrite a cell range in a Google Spreadsheet after explicit approval.

    Params:
        spreadsheet_id (str): The spreadsheet id (from the Sheets URL).
        tab (str): The sheet (tab) title, passed separately from the range.
        range (str): Bounded A1 rectangle within the tab, e.g. "B2:D5" or
            a single cell "B2". No sheet prefix, no unbounded ranges.
        values (2d array): Replacement values, exactly matching the range
            dimensions. Cells are strings, numbers, or booleans; use ""
            to clear a cell (null is rejected -- the Sheets API would
            skip the cell instead of clearing it).
        current_values (2d array): The values currently in the range, as
            previously read by the model. Same dimensions as ``values``;
            null or "" for empty cells. Verified against the live sheet
            at proposal time and again at Approve time so the model
            cannot overwrite cells it has not read.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.EDIT_GOOGLE_SPREADSHEET

    @property
    def display_name(self) -> str:
        return "Edit Google Sheet"

    @property
    def approve_label(self) -> str:
        return "Apply"

    @property
    def resolved_label(self) -> str:
        return "Saved"

    def summary_snippet(self, params: dict) -> str:
        # Prefer the server-injected human title; fall back to the raw id.
        summary = str(params.get("spreadsheet_title")
                      or params.get("spreadsheet_id") or "")
        tab = str(params.get("tab") or "")
        cell_range = str(params.get("range") or "")
        if tab:
            summary += f" -- {tab}"
        if cell_range:
            summary += f"!{cell_range}"
        return summary

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        spreadsheet_id = params.get("spreadsheet_id")
        if not isinstance(spreadsheet_id, str) or not spreadsheet_id.strip():
            raise ValueError("Missing required parameter: spreadsheet_id")
        spreadsheet_id = spreadsheet_id.strip()
        if not _SPREADSHEET_ID_RE.match(spreadsheet_id):
            raise ValueError(
                f"spreadsheet_id does not look like a Google Sheets id: "
                f"'{spreadsheet_id[:80]}'. Pass the id from the "
                "spreadsheet URL (docs.google.com/spreadsheets/d/<id>/...), "
                "not the full URL."
            )

        tab = params.get("tab")
        if not isinstance(tab, str) or not tab.strip():
            raise ValueError("Missing required parameter: tab")
        tab = tab.strip()
        if len(tab) > 100:
            raise ValueError("tab must be at most 100 characters")

        range_str = params.get("range")
        if not isinstance(range_str, str) or not range_str.strip():
            raise ValueError("Missing required parameter: range")
        row1, col1, row2, col2 = _parse_range(range_str)

        n_rows = row2 - row1 + 1
        n_cols = col2 - col1 + 1
        if n_rows > MAX_RANGE_ROWS or n_cols > MAX_RANGE_COLS:
            raise ValueError(
                f"range spans {n_rows}x{n_cols} cells; a single "
                f"edit_google_spreadsheet request is limited to {MAX_RANGE_ROWS} "
                f"rows x {MAX_RANGE_COLS} columns. Split the edit into "
                "multiple requests."
            )
        if n_rows * n_cols > MAX_RANGE_CELLS:
            raise ValueError(
                f"range covers {n_rows * n_cols} cells; a single "
                f"edit_google_spreadsheet request is limited to "
                f"{MAX_RANGE_CELLS} cells. Split the edit into multiple "
                "requests."
            )

        if "values" not in params:
            raise ValueError("Missing required parameter: values")
        values = _validate_grid(
            params["values"], "values", n_rows, n_cols, allow_none=False
        )

        if "current_values" not in params:
            raise ValueError(
                "Missing required parameter: current_values. Read the "
                "target range first and pass the values you read, so the "
                "edit can be verified against the live sheet."
            )
        current_values = _validate_grid(
            params["current_values"],
            "current_values",
            n_rows,
            n_cols,
            allow_none=True,
        )

        return {
            "spreadsheet_id": spreadsheet_id,
            "tab": tab,
            "range": _format_range(row1, col1, row2, col2),
            "values": values,
            "current_values": current_values,
        }

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Verify current_values against the live sheet at proposal time.

        Also resolves the spreadsheet title, checks the tab exists,
        checks the range lies inside the sheet's grid, and captures the
        context window for the preview card. A mismatch or a bad
        target raises ValueError (same-turn rejection, no card).
        Transient failures (network, 401/403, 5xx) log a WARNING and
        fall through -- execute() re-verifies at Approve time, so the
        safety check is never skipped, only deferred.
        """
        if not (user.get("google_services_oauth") or {}).get("access_token"):
            # No Google connection: the card would be useless, but the
            # authoritative "connect Google Services" error belongs to
            # execute(); don't mask it as Invalid parameters.
            return params

        row1, col1, row2, col2 = _parse_range(params["range"])
        spreadsheet_id = params["spreadsheet_id"]
        tab = params["tab"]

        try:
            meta_url = (
                f"{SHEETS_API_BASE}/spreadsheets/"
                f"{quote(spreadsheet_id, safe='')}"
                "?fields=properties.title,sheets.properties"
            )
            async with httpx.AsyncClient(timeout=30.0) as client:
                meta_resp = await make_authenticated_request(
                    client, user, "GET", meta_url
                )
        except Exception:
            logger.warning(
                "[edit_google_spreadsheet] metadata fetch failed during proposal "
                "validation; deferring verification to execute()",
                exc_info=True,
            )
            return params

        if meta_resp.status_code == 404:
            raise ValueError(
                f"Spreadsheet not found: '{spreadsheet_id}'. Check the "
                "spreadsheet_id (from the docs.google.com/spreadsheets/d/"
                "<id>/... URL)."
            )
        if meta_resp.status_code >= 400:
            logger.warning(
                "[edit_google_spreadsheet] metadata fetch returned %s during "
                "proposal validation; deferring verification to execute()",
                meta_resp.status_code,
            )
            return params

        meta = meta_resp.json()
        sheet_props = None
        sheet_titles = []
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            sheet_titles.append(props.get("title", ""))
            if props.get("title") == tab:
                sheet_props = props
        if sheet_props is None:
            raise ValueError(
                f"Tab '{tab}' not found in spreadsheet "
                f"'{meta.get('properties', {}).get('title', spreadsheet_id)}'"
                f". Available tabs: {sheet_titles}. Tab titles are "
                "case-sensitive."
            )

        grid_props = sheet_props.get("gridProperties", {})
        sheet_rows = int(grid_props.get("rowCount", 0) or 0)
        sheet_cols = int(grid_props.get("columnCount", 0) or 0)
        if sheet_rows and row2 > sheet_rows or sheet_cols and col2 > sheet_cols:
            raise ValueError(
                f"range {params['range']} extends beyond the tab's grid "
                f"({sheet_rows} rows x {sheet_cols} columns). Shrink the "
                "range or resize the sheet first."
            )

        # Context window: target expanded by CONTEXT_CELLS on every side,
        # clamped to the sheet bounds.
        win_r1 = max(1, row1 - CONTEXT_CELLS)
        win_c1 = max(1, col1 - CONTEXT_CELLS)
        win_r2 = min(sheet_rows, row2 + CONTEXT_CELLS) if sheet_rows else row2 + CONTEXT_CELLS
        win_c2 = min(sheet_cols, col2 + CONTEXT_CELLS) if sheet_cols else col2 + CONTEXT_CELLS

        quoted_tab = _quote_sheet_title(tab)
        window_range = f"{quoted_tab}!{_format_range(win_r1, win_c1, win_r2, win_c2)}"
        target_range = f"{quoted_tab}!{params['range']}"

        try:
            window_resp = await _fetch_values(
                user, spreadsheet_id, window_range, "FORMATTED_VALUE"
            )
            window_formula_resp = await _fetch_values(
                user, spreadsheet_id, window_range, "FORMULA"
            )
            target_resp = await _fetch_values(
                user, spreadsheet_id, target_range, "UNFORMATTED_VALUE"
            )
        except Exception:
            logger.warning(
                "[edit_google_spreadsheet] values fetch failed during proposal "
                "validation; deferring verification to execute()",
                exc_info=True,
            )
            return params
        if (
            window_resp.status_code >= 400
            or window_formula_resp.status_code >= 400
            or target_resp.status_code >= 400
        ):
            logger.warning(
                "[edit_google_spreadsheet] values fetch returned %s/%s/%s during "
                "proposal validation; deferring verification to execute()",
                window_resp.status_code,
                window_formula_resp.status_code,
                target_resp.status_code,
            )
            return params

        win_rows = win_r2 - win_r1 + 1
        win_cols = win_c2 - win_c1 + 1
        window_grid = _pad_grid(
            window_resp.json().get("values", []), win_rows, win_cols
        )
        window_formula = _pad_grid(
            window_formula_resp.json().get("values", []), win_rows, win_cols
        )
        target_unformatted = _pad_grid(
            target_resp.json().get("values", []),
            row2 - row1 + 1,
            col2 - col1 + 1,
        )
        # The formatted / formula renderings of the target are slices of
        # the window fetches.
        off_r = row1 - win_r1
        off_c = col1 - win_c1
        n_cols = col2 - col1 + 1

        def _slice_target(grid):
            return [
                row[off_c : off_c + n_cols]
                for row in grid[off_r : off_r + (row2 - row1 + 1)]
            ]

        mismatches = _collect_mismatches(
            params["current_values"],
            _slice_target(window_grid),
            target_unformatted,
            _slice_target(window_formula),
            row1,
            col1,
        )
        if mismatches:
            raise ValueError(
                "current_values does not match the live sheet -- you may "
                "be about to overwrite data you have not read. "
                + "; ".join(mismatches)
                + ". Re-read the range (authed_get "
                f"{SHEETS_API_BASE}/spreadsheets/{spreadsheet_id}/values/"
                f"{tab}!{params['range']}?valueRenderOption=FORMULA) and "
                "pass exactly what you read as current_values."
            )

        params = dict(params)
        title = meta.get("properties", {}).get("title")
        if title:
            params["spreadsheet_title"] = str(title)
        if sheet_props.get("sheetId") is not None:
            params["sheet_gid"] = sheet_props["sheetId"]
        # Display grid: formula cells show their formula text (matching
        # what the model must claim and what `values` will contain for
        # formula writes); everything else shows the formatted rendering.
        display_rows = [
            [
                _normalize_cell(
                    window_formula[r][c]
                    if _is_formula(window_formula[r][c])
                    else window_grid[r][c]
                )
                for c in range(win_cols)
            ]
            for r in range(win_rows)
        ]
        params["context_grid"] = {
            "start_row": win_r1,
            "start_col": win_c1,
            "rows": display_rows,
        }
        return params

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        row1, col1, row2, col2 = _parse_range(params["range"])
        n_rows = row2 - row1 + 1
        n_cols = col2 - col1 + 1

        values = params.get("values") or []
        current_values = params.get("current_values") or []
        new_grid = [[_normalize_cell(cell) for cell in row] for row in values]

        context = params.get("context_grid")
        if (
            isinstance(context, dict)
            and isinstance(context.get("rows"), list)
            and context["rows"]
        ):
            win_r1 = int(context.get("start_row", row1))
            win_c1 = int(context.get("start_col", col1))
            win_rows = len(context["rows"])
            win_cols = max(len(r) for r in context["rows"])
            current_window = _pad_grid(context["rows"], win_rows, win_cols)
        else:
            # No captured context (proposal-time verification was
            # deferred, or an old card): render the target range alone
            # from the model-claimed current values.
            win_r1, win_c1 = row1, col1
            current_window = _pad_grid(
                [[_normalize_cell(cell) for cell in row] for row in current_values],
                n_rows,
                n_cols,
            )

        # All-blank context rows/cols add noise, not context -- trim them
        # from the window edges (never into the target rectangle).
        current_window, win_r1, win_c1 = _trim_blank_context(
            current_window, win_r1, win_c1, row1, col1, row2, col2
        )

        off_r = row1 - win_r1
        off_c = col1 - win_c1
        changed = []
        changed_count = 0
        for r in range(n_rows):
            row_flags = []
            for c in range(n_cols):
                window_row = (
                    current_window[off_r + r]
                    if 0 <= off_r + r < len(current_window)
                    else []
                )
                current_cell = (
                    window_row[off_c + c] if 0 <= off_c + c < len(window_row) else ""
                )
                is_changed = not _cells_equal(new_grid[r][c], current_cell)
                row_flags.append(is_changed)
                if is_changed:
                    changed_count += 1
            changed.append(row_flags)

        spreadsheet_label = params.get("spreadsheet_title") or params.get(
            "spreadsheet_id", ""
        )
        total = n_rows * n_cols
        return [
            {"key": "Spreadsheet", "value": str(spreadsheet_label)},
            {"key": "Tab", "value": str(params.get("tab", ""))},
            {
                "key": "Range",
                "value": f"{params['range']} ({n_rows}x{n_cols} cells)",
            },
            {
                "key": "Changes",
                "value": f"{changed_count} of {total} cell(s) changing",
                "type": "spreadsheet_diff",
                "grid": {
                    "start_row": win_r1,
                    "start_col": win_c1,
                    "target_start_row": row1,
                    "target_start_col": col1,
                    "target_end_row": row2,
                    "target_end_col": col2,
                    "current": current_window,
                    "new": new_grid,
                    "changed": changed,
                },
            },
        ]

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        google_services_oauth = user.get("google_services_oauth") or {}
        if not google_services_oauth.get("access_token"):
            raise RuntimeError(
                "Google Services not connected. Please connect Google "
                "Services via Settings > Data Connections."
            )
        if SPREADSHEETS_WRITE_SCOPE not in _get_authorized_google_scopes(user):
            raise RuntimeError(_sheets_reauth_message())

        row1, col1, row2, col2 = _parse_range(params["range"])
        spreadsheet_id = params["spreadsheet_id"]
        target_range = (
            f"{_quote_sheet_title(params['tab'])}!{params['range']}"
        )

        # Re-verify current_values against the live sheet: the card may
        # have sat open while the spreadsheet changed underneath it.
        try:
            formatted_resp = await _fetch_values(
                user, spreadsheet_id, target_range, "FORMATTED_VALUE"
            )
            unformatted_resp = await _fetch_values(
                user, spreadsheet_id, target_range, "UNFORMATTED_VALUE"
            )
            formula_resp = await _fetch_values(
                user, spreadsheet_id, target_range, "FORMULA"
            )
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                raise RuntimeError(_sheets_reauth_message()) from exc
            raise
        for resp in (formatted_resp, unformatted_resp, formula_resp):
            if resp.status_code in (401, 403):
                error_text = _extract_google_api_error(resp).lower()
                if (
                    "insufficient" in error_text
                    or "permission" in error_text
                    or "scope" in error_text
                ):
                    raise RuntimeError(_sheets_reauth_message())
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Google Sheets API error: {_extract_google_api_error(resp)}"
                )

        n_rows = row2 - row1 + 1
        n_cols = col2 - col1 + 1
        live_formatted = _pad_grid(
            formatted_resp.json().get("values", []), n_rows, n_cols
        )
        live_unformatted = _pad_grid(
            unformatted_resp.json().get("values", []), n_rows, n_cols
        )
        live_formula = _pad_grid(
            formula_resp.json().get("values", []), n_rows, n_cols
        )
        mismatches = _collect_mismatches(
            params["current_values"],
            live_formatted,
            live_unformatted,
            live_formula,
            row1,
            col1,
        )
        if mismatches:
            raise RuntimeError(
                "The spreadsheet changed since this request was created; "
                "refusing to overwrite values that were never read. "
                + "; ".join(mismatches)
                + ". Re-read the range and issue a new edit_google_spreadsheet "
                "request with fresh current_values."
            )

        update_url = (
            f"{SHEETS_API_BASE}/spreadsheets/"
            f"{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(target_range, safe='')}"
            "?valueInputOption=USER_ENTERED"
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await make_authenticated_request(
                    client,
                    user,
                    "PUT",
                    update_url,
                    json={
                        "range": target_range,
                        "majorDimension": "ROWS",
                        "values": params["values"],
                    },
                )
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                raise RuntimeError(_sheets_reauth_message()) from exc
            raise

        if response.status_code in (401, 403):
            error_text = _extract_google_api_error(response).lower()
            if (
                "insufficient" in error_text
                or "permission" in error_text
                or "scope" in error_text
            ):
                raise RuntimeError(_sheets_reauth_message())
        if response.status_code >= 400:
            raise RuntimeError(
                f"Google Sheets API error: {_extract_google_api_error(response)}"
            )

        payload = response.json()
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        if params.get("sheet_gid") is not None:
            url = f"{url}#gid={params['sheet_gid']}"
        return {
            "success": True,
            "spreadsheet_id": spreadsheet_id,
            "range": payload.get("updatedRange", target_range),
            "updated_cells": payload.get("updatedCells"),
            "url": url,
        }
