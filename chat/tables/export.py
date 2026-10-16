"""CSV export of a Quest Table (``GET /tables/{id}/download?format=csv``).

Pure and synchronous: :func:`chat.tables.service.export_table` snapshots
the rows with ``storage.dump_rows`` under the per-table lock and then runs
:func:`write_csv` in ``asyncio.to_thread`` outside it.

The file is UTF-8 without a byte-order mark, written with the ``csv``
module's defaults (``QUOTE_MINIMAL``, ``"\\r\\n"`` line endings; a cell
holding a comma, a quote or a newline is quoted, so ``csv.reader`` gives
it back unchanged). The header row is the column names in schema order;
rows follow in ``_id`` order (the order ``dump_rows`` returns). Cells:
text and url as stored, numbers as their canonical decimal string,
dates as their ``YYYY-MM-DD`` string, booleans ``true`` / ``false``, a
single enum its option, a multiple enum its options joined with ``"; "``,
no value an empty cell. ``_id`` and the timestamps are not exported.

Formula injection: spreadsheet apps run a cell starting with ``=``, ``+``,
``-``, ``@``, a tab or a carriage return as a formula. A text, url or enum
cell (a multiple enum's joined string as a whole) and a header name that
starts with one of those characters gets a leading ``'``, which the apps
show as plain text. Number, date and boolean cells are never prefixed (a
negative number keeps its ``-``; a date starts with a digit), so the file
stays numeric where the table is.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Sequence

from chat.tables.schema import Column

__all__ = ["FORMULA_PREFIXES", "MULTI_ENUM_SEPARATOR", "csv_cell", "write_csv"]

MULTI_ENUM_SEPARATOR = "; "
# Leading characters a spreadsheet app treats as the start of a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _defuse(text: str) -> str:
    """``text`` with a leading ``'`` when it would start a formula."""
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def csv_cell(column: Column, value: Any) -> str:
    """One logical cell value (``dump_rows`` form) as CSV text."""
    if value is None:
        return ""
    if column.type == "boolean":
        return "true" if value else "false"
    if column.type in ("number", "date"):
        return str(value)
    if column.type == "enum" and column.multiple:
        return _defuse(MULTI_ENUM_SEPARATOR.join(value))
    return _defuse(str(value))


def write_csv(dest: Path, columns: Sequence[Column], rows: Iterable[dict]) -> Path:
    """Write ``rows`` (``dump_rows`` dicts keyed by column key) to ``dest``,
    which must not exist yet (opened with mode ``"x"``). Returns ``dest``;
    a partial file is removed on failure."""
    dest = Path(dest)
    fh = open(dest, "x", newline="", encoding="utf-8")
    # From here on the file is ours: remove it if writing fails.
    try:
        with fh:
            writer = csv.writer(fh)
            writer.writerow([_defuse(column.name) for column in columns])
            for row in rows:
                writer.writerow([csv_cell(c, row.get(c.key)) for c in columns])
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest
