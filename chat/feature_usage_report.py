"""Pure folds behind the admin System Reports "Docs", "Tables" and
"Memories" sections.

Each builder takes the narrow all-rows projections of the stores (no
titles, no bodies, no memory text), the user roster and an optional
inclusive UTC date range, and returns ``{"users": [...], "total_users"}``:
one row per user who owns at least one item of the feature (lifetime), so
the section doubles as "who has adopted this", next to the roster size
for the "N of M users" summary. Lifetime figures describe what exists
now; the ``*_in_range`` figures count rows whose ``created_at`` (or
``updated_at``) falls inside the range, keyed on the UTC calendar day
like the other ranged reports (an open-ended range counts everything).

The handlers in chat/routes/admin.py run these in a worker thread; the
functions never touch the database or the filesystem.

Docs / Tables row (``noun`` is ``doc`` or ``table``):

- ``<noun>_count`` -- items the user owns now; ``user_<noun>_count`` /
  ``project_<noun>_count`` split on ``project_id``; ``public_<noun>_count``
  the ``mode == "public"`` subset; ``shared_<noun>_count`` the items with
  at least one share grant.
- ``model_written_count`` -- items whose latest body write came from a
  model (``last_write_source`` of the ``conversation:<id>`` or
  ``action_request:<id>`` forms) rather than a person in the UI (or, for
  tables, a History restore).
- ``created_in_range`` / ``updated_in_range`` -- items created, resp. last
  updated, inside the range. ``updated_at`` is the row's latest change of
  any kind (body, rename, share), one timestamp per item: a doc written
  ten times in the range counts once.
- ``last_updated_at`` -- the newest ``updated_at`` across the user's items.
- Docs add ``content_bytes`` (sum of the cached body sizes) and
  ``asset_count``; tables add ``row_total`` (sum of the cached row counts)
  and ``column_total``.

Memories row: ``memory_count`` (active, i.e. not archived) and
``archived_count`` with ``content_chars`` over the active ones;
``created_in_range``; the suggestion funnel for suggestions created in
the range (``suggested_in_range`` split into ``accepted_in_range``,
``dismissed_in_range`` and ``pending_in_range`` by current status); the
same figures for project memories (``project_memory_count`` across the
projects the user owns, ``project_count`` the projects among them that
hold a memory, ``project_created_in_range``) and project suggestions
(``project_suggested_in_range`` etc., attributed to the user whose
conversation raised them); and ``last_activity_at``, the newest memory
or suggestion timestamp of any kind.
"""

from datetime import date, datetime
from typing import Optional

from db.models import MemorySuggestionStatus

# ``last_write_source`` prefixes that mean a model wrote the body (see the
# Doc / DataTable column comments in db/models.py).
_MODEL_SOURCE_PREFIXES = ("conversation:", "action_request:")

UNKNOWN_USER = {"email": "", "name": "(unknown user)"}


def _in_range(
    ts: Optional[datetime], start: Optional[date], end: Optional[date]
) -> bool:
    if ts is None:
        return False
    day = ts.date()
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def _newest(current: Optional[datetime], candidate: Optional[datetime]) -> Optional[datetime]:
    if candidate is None:
        return current
    if current is None or candidate > current:
        return candidate
    return current


def _is_model_source(source: Optional[str]) -> bool:
    return bool(source) and source.startswith(_MODEL_SOURCE_PREFIXES)


def _identity(user_id: int, roster: dict[int, dict]) -> dict:
    info = roster.get(user_id, UNKNOWN_USER)
    return {
        "user_id": user_id,
        "user_email": info["email"] or "",
        "user_name": info["name"] or "",
    }


def _roster(users: list[dict]) -> dict[int, dict]:
    return {u["id"]: u for u in users}


def _fold_owned_items(
    rows: list[dict],
    users: list[dict],
    start: Optional[date],
    end: Optional[date],
    noun: str,
    sums: dict[str, str],
) -> dict:
    """The shared docs / tables fold: ``sums`` maps an output key to the
    row field it totals (``content_bytes`` <- ``content_size`` for docs)."""
    roster = _roster(users)
    per_user: dict[int, dict] = {}
    for row in rows:
        acc = per_user.get(row["owner_id"])
        if acc is None:
            acc = per_user[row["owner_id"]] = {
                f"{noun}_count": 0,
                f"user_{noun}_count": 0,
                f"project_{noun}_count": 0,
                f"public_{noun}_count": 0,
                f"shared_{noun}_count": 0,
                "model_written_count": 0,
                **{out_key: 0 for out_key in sums},
                "created_in_range": 0,
                "updated_in_range": 0,
                "_last_updated": None,
            }
        acc[f"{noun}_count"] += 1
        if row["project_id"]:
            acc[f"project_{noun}_count"] += 1
        else:
            acc[f"user_{noun}_count"] += 1
        if row["mode"] == "public":
            acc[f"public_{noun}_count"] += 1
        if row["share_count"] > 0:
            acc[f"shared_{noun}_count"] += 1
        if _is_model_source(row["last_write_source"]):
            acc["model_written_count"] += 1
        for out_key, in_key in sums.items():
            acc[out_key] += int(row[in_key] or 0)
        if _in_range(row["created_at"], start, end):
            acc["created_in_range"] += 1
        if _in_range(row["updated_at"], start, end):
            acc["updated_in_range"] += 1
        acc["_last_updated"] = _newest(acc["_last_updated"], row["updated_at"])

    out = []
    for user_id, acc in per_user.items():
        last_updated = acc.pop("_last_updated")
        out.append({
            **_identity(user_id, roster),
            **acc,
            "last_updated_at": _iso(last_updated),
        })
    out.sort(key=lambda r: (
        -r["updated_in_range"], -r[f"{noun}_count"], r["user_email"], r["user_id"],
    ))
    return {"users": out, "total_users": len(users)}


def build_docs_report(
    doc_rows: list[dict],
    users: list[dict],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict:
    """Fold ``list_all_doc_rows()`` per owner (row shape in the module
    docstring). Sorted by docs updated in range, then docs owned, then
    owner email."""
    return _fold_owned_items(
        doc_rows, users, start, end, "doc",
        {"content_bytes": "content_size", "asset_count": "asset_count"},
    )


def build_tables_report(
    table_rows: list[dict],
    users: list[dict],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict:
    """Fold ``list_all_table_rows()`` per owner (row shape in the module
    docstring). Sorted by tables updated in range, then tables owned, then
    owner email."""
    return _fold_owned_items(
        table_rows, users, start, end, "table",
        {"row_total": "row_count", "column_total": "column_count"},
    )


_SUGGESTION_STATUS_KEY = {
    MemorySuggestionStatus.ACCEPTED.value: "accepted",
    MemorySuggestionStatus.DISMISSED.value: "dismissed",
    MemorySuggestionStatus.PENDING.value: "pending",
}


def _empty_memory_row() -> dict:
    return {
        "memory_count": 0,
        "archived_count": 0,
        "content_chars": 0,
        "created_in_range": 0,
        "suggested_in_range": 0,
        "accepted_in_range": 0,
        "dismissed_in_range": 0,
        "pending_in_range": 0,
        "project_memory_count": 0,
        "project_archived_count": 0,
        "project_created_in_range": 0,
        "project_suggested_in_range": 0,
        "project_accepted_in_range": 0,
        "project_dismissed_in_range": 0,
        "project_pending_in_range": 0,
        "_projects": set(),
        "_last_activity": None,
    }


def _fold_memory_rows(acc_for, rows, start, end, prefix: str) -> None:
    for row in rows:
        acc = acc_for(row["user_id"])
        if row["archived"]:
            acc[f"{prefix}archived_count"] += 1
        else:
            acc[f"{prefix}memory_count"] += 1
            if prefix == "":
                acc["content_chars"] += int(row["content_length"] or 0)
        if _in_range(row["created_at"], start, end):
            acc[f"{prefix}created_in_range"] += 1
        if prefix:
            acc["_projects"].add(row["project_id"])
        acc["_last_activity"] = _newest(acc["_last_activity"], row["created_at"])
        acc["_last_activity"] = _newest(acc["_last_activity"], row["updated_at"])


def _fold_suggestion_rows(acc_for, rows, start, end, prefix: str) -> None:
    for row in rows:
        acc = acc_for(row["user_id"])
        if _in_range(row["created_at"], start, end):
            acc[f"{prefix}suggested_in_range"] += 1
            status_key = _SUGGESTION_STATUS_KEY.get(row["status"])
            if status_key:
                acc[f"{prefix}{status_key}_in_range"] += 1
        acc["_last_activity"] = _newest(acc["_last_activity"], row["created_at"])
        acc["_last_activity"] = _newest(acc["_last_activity"], row["resolved_at"])


def build_memories_report(
    memory_rows: list[dict],
    suggestion_rows: list[dict],
    project_memory_rows: list[dict],
    project_suggestion_rows: list[dict],
    users: list[dict],
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> dict:
    """Fold the four memory projections per user (row shape in the module
    docstring). A user gets a row when any of the four has a row for them.
    Sorted by in-range activity (memories created plus suggestions raised,
    personal and project), then active memories, then email."""
    roster = _roster(users)
    per_user: dict[int, dict] = {}

    def acc_for(user_id: int) -> dict:
        acc = per_user.get(user_id)
        if acc is None:
            acc = per_user[user_id] = _empty_memory_row()
        return acc

    _fold_memory_rows(acc_for, memory_rows, start, end, "")
    _fold_suggestion_rows(acc_for, suggestion_rows, start, end, "")
    _fold_memory_rows(acc_for, project_memory_rows, start, end, "project_")
    _fold_suggestion_rows(acc_for, project_suggestion_rows, start, end, "project_")

    out = []
    for user_id, acc in per_user.items():
        projects = acc.pop("_projects")
        last_activity = acc.pop("_last_activity")
        out.append({
            **_identity(user_id, roster),
            **acc,
            "project_count": len(projects),
            "last_activity_at": _iso(last_activity),
        })

    def activity(r: dict) -> int:
        return (
            r["created_in_range"] + r["suggested_in_range"]
            + r["project_created_in_range"] + r["project_suggested_in_range"]
        )

    out.sort(key=lambda r: (
        -activity(r), -(r["memory_count"] + r["project_memory_count"]),
        r["user_email"], r["user_id"],
    ))
    return {"users": out, "total_users": len(users)}
