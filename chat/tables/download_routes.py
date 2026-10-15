"""Quest Tables download route (spec section 7, phase A16).

``GET /app/api/tables/{id}/download?format=csv|sqlite&include_history=false``

Same conventions as chat/tables/routes.py (auth dependency, 403
``tables_disabled`` first, 404 ``table_not_found`` with one body for a
missing, hidden or malformed id). Every reader may download; order after
visibility:

1. ``format`` (default ``csv``) not ``csv`` / ``sqlite`` -> 400
   ``invalid_format``; ``include_history`` not ``true`` / ``1`` /
   ``false`` / ``0`` / empty (case-insensitive) -> 400 ``invalid_request``.
2. ``include_history=true`` from a non-editor (anyone but the owner and
   write-share recipients) -> 403 ``forbidden`` ("History is available to
   people who can edit this table.").
3. ``include_history=true`` with ``format=csv`` -> 400 ``invalid_request``.

The file is built by ``service.export_table`` into a fresh
``tempfile.mkdtemp(prefix="quest-table-dl-")`` directory (CSV:
chat/tables/export.py rules; SQLite: a ``VACUUM INTO`` copy with ``rows``
and ``meta``, plus ``changes`` only with ``include_history``) and sent as
an attachment named after the table title (``<title>.csv`` /
``<title>.sqlite``; ``routes._download_filename`` +
``routes._attachment_disposition``), media type ``text/csv;
charset=utf-8`` or ``application/vnd.sqlite3``.

The temporary directory is removed on every path: on any error (or
cancellation) while exporting, and once the response has been sent. The
latter is a ``try/finally`` around the response's own ``__call__``
(:class:`_TempDirFileResponse`, synchronous ``rmtree``) rather than a
``BackgroundTask``:
Starlette runs a response's background task only after the last body
chunk was sent, so a ``send`` that raises (client gone, depending on the
server) would skip it; the ``finally`` runs either way.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.tables import service, storage
from chat.tables.routes import (
    _attachment_disposition,
    _download_filename,
    _get_table_for_ui,
    _http_error,
    _raise_table_error,
    _require_tables_enabled,
)
from chat.tables.schema import TableSchemaError

router = APIRouter(
    prefix="/app/api",
    tags=["tables"],
)

DOWNLOAD_FORMATS = ("csv", "sqlite")
_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "sqlite": "application/vnd.sqlite3",
}
TEMP_DIR_PREFIX = "quest-table-dl-"


_TRUE = ("true", "1")
_FALSE = ("false", "0", "")


def _parse_bool(raw: Optional[str]) -> bool:
    """``include_history``: true/1 or false/0/empty (case-insensitive),
    else 400 ``invalid_request``."""
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise _http_error(400, "invalid_request", "include_history must be true or false.")


def _remove_tree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


class _TempDirFileResponse(FileResponse):
    """A FileResponse that removes its temporary directory once sent (or
    once sending failed)."""

    def __init__(self, *args, temp_dir: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._temp_dir = temp_dir

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Synchronous: a to_thread hop could itself be cancelled. The
            # directory holds one file, so this is quick.
            _remove_tree(self._temp_dir)


@router.get("/tables/{table_id}/download")
async def download_table(
    table_id: str,
    download_format: str = Query("csv", alias="format"),
    include_history: Optional[str] = Query(None),
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Download the table as CSV or SQLite (module docstring)."""
    _require_tables_enabled(user)
    table, access = await _get_table_for_ui(user, table_id)
    if download_format not in DOWNLOAD_FORMATS:
        raise _http_error(
            400, "invalid_format",
            f"format must be one of: {', '.join(DOWNLOAD_FORMATS)}.",
        )
    include_history = _parse_bool(include_history)
    if include_history and access.rows != "free":
        raise _http_error(403, "forbidden", service.HISTORY_FORBIDDEN_MESSAGE)
    if include_history and download_format != "sqlite":
        raise _http_error(
            400, "invalid_request", "include_history is only available for sqlite downloads.",
        )

    temp_dir = tempfile.mkdtemp(prefix=TEMP_DIR_PREFIX)
    try:
        exported, path = await service.export_table(
            service.caller_for_ui(user),
            table["id"],
            fmt=download_format,
            include_history=include_history,
            dest_dir=Path(temp_dir),
        )
    except BaseException as exc:
        # Synchronous: this also runs while the request is being cancelled.
        _remove_tree(temp_dir)
        if isinstance(exc, (service.TableError, storage.TableStorageError, TableSchemaError)):
            _raise_table_error(exc, table_id)
        raise
    return _TempDirFileResponse(
        path=str(path),
        media_type=_MEDIA_TYPES[download_format],
        headers={
            "Content-Disposition": _attachment_disposition(
                _download_filename(exported["title"], download_format),
            ),
        },
        temp_dir=temp_dir,
    )
