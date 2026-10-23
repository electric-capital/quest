"""Read queries behind the admin System Reports sections.

Each report is a grouped query over one ledger (``query_*``, run on a
store cursor inside ``asyncio.to_thread``) plus a pure fold
(``build_*_report``) that joins the labels the caller resolved from
``quest.db`` -- skill names and kinds, routine conversations -- and shapes
the response. The fold is pure so the tests can pin the row contract
without a store. Nothing here returns content: ids, names and counts only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

AUTOLOAD_SOURCES = ("autoload_user", "autoload_project", "autoload_routine", "subagent_run")
MODEL_SOURCES = ("tool_load_skills", "tool_get_skill", "create_skill")

SKILL_KINDS = ("system", "private", "shared", "public", "project", "deleted")


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """The ledger stores naive UTC; the range window arrives tz-aware."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _window_clause(start_dt: Optional[datetime], end_dt: Optional[datetime]) -> tuple[str, list]:
    clauses, params = [], []
    if start_dt is not None:
        clauses.append("created_at >= ?")
        params.append(_naive(start_dt))
    if end_dt is not None:
        clauses.append("created_at < ?")
        params.append(_naive(end_dt))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def query_skill_usage(
    cur, start_dt: Optional[datetime] = None, end_dt: Optional[datetime] = None,
) -> list[dict]:
    """One row per (skill, conversation, user) with loads in the half-open
    ``[start_dt, end_dt)`` window: the load counts by source family, the
    routine id any row carried, and the newest load."""
    where, params = _window_clause(start_dt, end_dt)
    auto = ", ".join(f"'{s}'" for s in AUTOLOAD_SOURCES)
    model = ", ".join(f"'{s}'" for s in MODEL_SOURCES)
    rows = cur.execute(
        f"""
        SELECT skill_id, conversation_id, user_id,
               COUNT(*) AS load_count,
               COUNT(*) FILTER (WHERE source IN ({auto})) AS autoload_count,
               COUNT(*) FILTER (WHERE source = 'composer') AS composer_count,
               COUNT(*) FILTER (WHERE source IN ({model})) AS model_load_count,
               COUNT(*) FILTER (WHERE source = 'backfill') AS backfill_count,
               MAX(routine_id) AS routine_id,
               MAX(created_at) AS last_used_at
        FROM skill_loads{where}
        GROUP BY skill_id, conversation_id, user_id
        """,
        params,
    ).fetchall()
    keys = (
        "skill_id", "conversation_id", "user_id", "load_count", "autoload_count",
        "composer_count", "model_load_count", "backfill_count", "routine_id",
        "last_used_at",
    )
    return [dict(zip(keys, row)) for row in rows]


def query_backfill(cur, name: str) -> Optional[dict]:
    """``{"completed_at", "row_count"}`` of a finished backfill, else None."""
    row = cur.execute(
        "SELECT completed_at, row_count FROM _backfills WHERE name = ? "
        "ORDER BY completed_at DESC LIMIT 1",
        [name],
    ).fetchone()
    if row is None:
        return None
    return {"completed_at": row[0].isoformat(), "row_count": row[1]}


def build_skills_report(
    usage_rows: Iterable[dict],
    labels: dict[str, dict],
    system_names: dict[str, str],
    routine_conversations: set[str],
    backfill: Optional[dict] = None,
) -> dict:
    """Fold the per-(skill, conversation, user) usage into one row per skill.

    ``labels`` is ``list_skill_labels()`` for the DB skill ids seen (name,
    visibility, project and creator; a missing id is a deleted skill),
    ``system_names`` maps ``system:*`` ids to catalog names,
    ``routine_conversations`` is the set of conversation ids a routine
    created (from ``quest.db``; a row's own ``routine_id`` also counts, so a
    deleted routine conversation keeps its attribution).

    Row: ``skill_id``, ``name``, ``kind`` (one of ``SKILL_KINDS``),
    ``owner_email`` / ``owner_name`` / ``project_id`` (DB skills only),
    ``conversation_count``, ``routine_conversation_count``, ``user_count``,
    ``load_count`` with its ``autoload_count`` / ``composer_count`` /
    ``model_load_count`` / ``backfill_count`` split, ``last_used_at``
    (naive-UTC ISO). Sorted by conversations, then loads, then name.
    """
    per_skill: dict[str, dict] = {}
    all_conversations: set[str] = set()
    for row in usage_rows:
        entry = per_skill.setdefault(row["skill_id"], {
            "conversations": set(), "routine_conversations": set(), "users": set(),
            "load_count": 0, "autoload_count": 0, "composer_count": 0,
            "model_load_count": 0, "backfill_count": 0, "last_used_at": None,
        })
        conv = row["conversation_id"]
        entry["conversations"].add(conv)
        all_conversations.add(conv)
        if row.get("routine_id") or conv in routine_conversations:
            entry["routine_conversations"].add(conv)
        entry["users"].add(row["user_id"])
        for key in ("load_count", "autoload_count", "composer_count",
                    "model_load_count", "backfill_count"):
            entry[key] += int(row.get(key) or 0)
        last = row.get("last_used_at")
        if last is not None and (entry["last_used_at"] is None or last > entry["last_used_at"]):
            entry["last_used_at"] = last

    skills = []
    for skill_id, entry in per_skill.items():
        name, kind = skill_id, "deleted"
        owner_email = owner_name = project_id = None
        if skill_id.startswith("system:"):
            kind = "system"
            name = system_names.get(skill_id, skill_id)
        elif skill_id in labels:
            label = labels[skill_id]
            name = label.get("name") or skill_id
            kind = label.get("visibility") if label.get("visibility") in SKILL_KINDS else "private"
            owner_email = label.get("creator_email")
            owner_name = label.get("creator_name")
            project_id = label.get("project_id")
        skills.append({
            "skill_id": skill_id,
            "name": name,
            "kind": kind,
            "owner_email": owner_email,
            "owner_name": owner_name,
            "project_id": project_id,
            "conversation_count": len(entry["conversations"]),
            "routine_conversation_count": len(entry["routine_conversations"]),
            "user_count": len(entry["users"]),
            "load_count": entry["load_count"],
            "autoload_count": entry["autoload_count"],
            "composer_count": entry["composer_count"],
            "model_load_count": entry["model_load_count"],
            "backfill_count": entry["backfill_count"],
            "last_used_at": (
                entry["last_used_at"].isoformat() if entry["last_used_at"] else None
            ),
        })
    skills.sort(key=lambda r: (-r["conversation_count"], -r["load_count"], r["name"].lower()))
    return {
        "skills": skills,
        "conversation_count": len(all_conversations),
        "load_count": sum(r["load_count"] for r in skills),
        "backfill": backfill,
    }
