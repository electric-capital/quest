"""Pure folds behind the admin System Reports "Tool Calls" and "Skills"
sections.

Both reports come from one scan of the conversation transcripts: for every
surviving conversation whose lifetime can overlap the selected range, the
model's ``tool_use`` messages inside the range (``ChatStorage
.scan_transcript_tool_usage``) and the conversation's ``skill_reads.json``
(the skill ids whose body the model has seen, any tier: auto-loads, composer
loads, ``load_skills`` / ``get_skill``, ``create_skill``).
``collect_transcript_usage`` does the walk with injectable readers (the admin
handler runs it in a worker thread; tests pass fakes) and yields one record
per conversation; the two builders fold those records and never touch the
database or the filesystem.

Record (per conversation): ``conversation_id``, ``user_id``, ``routine_id``,
``last_message_at`` (datetime or None), ``active`` (the user sent a message
inside the range), ``tool_calls`` (the scan's in-range call list) and
``skill_ids`` (the conversation's skill reads, lifetime).

Tool Calls row (one per tool name, the ``tool_call`` meta-tool unwrapped to
the tool it routed to): ``tool``, ``dynamic`` (issued through the meta-tool
rather than by its own name), ``call_count``, ``routine_call_count`` (calls
in routine-created conversations), ``conversation_count``, ``user_count``
(distinct conversation owners) and ``last_called_at`` (the newest in-range
call). Sorted by calls descending, then name. The response adds the totals
``call_count``, ``conversation_count`` (conversations with at least one
in-range call) and ``scanned_conversations`` (transcripts read).

Skills row (one per skill id seen): identity from the label lookup
(``skill_id``, ``name``, ``kind`` -- ``system`` / ``private`` / ``shared`` /
``public`` / ``project`` / ``deleted`` -- and the creator's ``owner_email`` /
``owner_name``), ``conversation_count`` (conversations active in range that
had the skill in context, plus any that loaded it in range),
``routine_conversation_count``, ``user_count``, ``load_count`` (explicit
``load_skills`` / ``get_skill`` calls naming the skill inside the range) and
``last_used_at`` (the newest ``last_message_at`` among those conversations).
Sorted by conversations descending, then loads, then name. The response adds
``conversation_count`` (active conversations with any skill in context) and
``scanned_conversations``.
"""

from datetime import date, datetime
from typing import Callable, Iterable, Optional

from chat.system_skills.catalog import SYSTEM_SKILL_PREFIX

DELETED_SKILL_NAME = "(deleted skill)"


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def collect_transcript_usage(
    conv_rows: Iterable[dict],
    start: Optional[date],
    end: Optional[date],
    scan: Callable[[str, Optional[str], Optional[str]], Optional[dict]],
    read_skill_ids: Callable[[str], list[str]],
) -> list[dict]:
    """Walk the conversations that can overlap the range and read each one.

    ``conv_rows`` is the ``list_conversation_activity_rows()`` projection;
    rows whose ``created_at`` is after the range or whose
    ``last_message_at`` is before it are skipped without a file read. A
    conversation whose transcript is missing yields no record.
    """
    start_day = start.isoformat() if start else None
    end_day = end.isoformat() if end else None
    records = []
    for row in conv_rows:
        created, last = row.get("created_at"), row.get("last_message_at")
        if end_day and created is not None and created.date().isoformat() > end_day:
            continue
        if start_day and last is not None and last.date().isoformat() < start_day:
            continue
        scanned = scan(row["id"], start_day, end_day)
        if scanned is None:
            continue
        records.append({
            "conversation_id": row["id"],
            "user_id": row["user_id"],
            "routine_id": row.get("routine_id"),
            "last_message_at": last,
            "active": scanned["active"],
            "tool_calls": scanned["tool_calls"],
            "skill_ids": list(read_skill_ids(row["id"])),
        })
    return records


def build_tool_calls_report(records: list[dict]) -> dict:
    """Fold the per-conversation records per tool name (row shape in the
    module docstring)."""
    per_tool: dict[str, dict] = {}
    conversations_with_calls = set()
    total_calls = 0
    for rec in records:
        is_routine = bool(rec["routine_id"])
        for call in rec["tool_calls"]:
            acc = per_tool.get(call["tool"])
            if acc is None:
                acc = per_tool[call["tool"]] = {
                    "dynamic": False,
                    "call_count": 0,
                    "routine_call_count": 0,
                    "_conversations": set(),
                    "_users": set(),
                    "_last": None,
                }
            acc["dynamic"] = acc["dynamic"] or bool(call["dynamic"])
            acc["call_count"] += 1
            if is_routine:
                acc["routine_call_count"] += 1
            acc["_conversations"].add(rec["conversation_id"])
            acc["_users"].add(rec["user_id"])
            ts = call["timestamp"]
            if acc["_last"] is None or ts > acc["_last"]:
                acc["_last"] = ts
            conversations_with_calls.add(rec["conversation_id"])
            total_calls += 1

    rows = []
    for tool, acc in per_tool.items():
        rows.append({
            "tool": tool,
            "dynamic": acc["dynamic"],
            "call_count": acc["call_count"],
            "routine_call_count": acc["routine_call_count"],
            "conversation_count": len(acc["_conversations"]),
            "user_count": len(acc["_users"]),
            "last_called_at": acc["_last"],
        })
    rows.sort(key=lambda r: (-r["call_count"], r["tool"]))
    return {
        "tools": rows,
        "call_count": total_calls,
        "conversation_count": len(conversations_with_calls),
        "scanned_conversations": len(records),
    }


def _skill_identity(skill_id: str, labels: dict[str, dict]) -> dict:
    label = labels.get(skill_id)
    if skill_id.startswith(SYSTEM_SKILL_PREFIX):
        return {
            "skill_id": skill_id,
            "name": label["name"] if label else skill_id[len(SYSTEM_SKILL_PREFIX):],
            "kind": "system",
            "owner_email": None,
            "owner_name": None,
            "project_id": None,
        }
    if label is None:
        return {
            "skill_id": skill_id,
            "name": DELETED_SKILL_NAME,
            "kind": "deleted",
            "owner_email": None,
            "owner_name": None,
            "project_id": None,
        }
    kind = "project" if label.get("project_id") else (label.get("visibility") or "private")
    return {
        "skill_id": skill_id,
        "name": label["name"],
        "kind": kind,
        "owner_email": label.get("creator_email"),
        "owner_name": label.get("creator_name"),
        "project_id": label.get("project_id"),
    }


def build_skills_report(records: list[dict], labels: dict[str, dict]) -> dict:
    """Fold the per-conversation records per skill id (row shape in the
    module docstring). ``labels`` maps skill ids to ``{name, visibility,
    project_id, creator_email, creator_name}`` for DB skills and ``{name}``
    for system skills; ids without a label are reported as deleted."""
    per_skill: dict[str, dict] = {}
    active_with_skills = set()

    def acc_for(skill_id: str) -> dict:
        acc = per_skill.get(skill_id)
        if acc is None:
            acc = per_skill[skill_id] = {
                "load_count": 0,
                "_conversations": set(),
                "_routine_conversations": set(),
                "_users": set(),
                "_last": None,
            }
        return acc

    def touch(acc: dict, rec: dict) -> None:
        acc["_conversations"].add(rec["conversation_id"])
        if rec["routine_id"]:
            acc["_routine_conversations"].add(rec["conversation_id"])
        acc["_users"].add(rec["user_id"])
        last = rec["last_message_at"]
        if last is not None and (acc["_last"] is None or last > acc["_last"]):
            acc["_last"] = last

    for rec in records:
        if rec["active"]:
            for skill_id in rec["skill_ids"]:
                touch(acc_for(skill_id), rec)
                active_with_skills.add(rec["conversation_id"])
        for call in rec["tool_calls"]:
            for skill_id in call["skill_ids"]:
                acc = acc_for(skill_id)
                acc["load_count"] += 1
                touch(acc, rec)

    rows = []
    for skill_id, acc in per_skill.items():
        rows.append({
            **_skill_identity(skill_id, labels),
            "conversation_count": len(acc["_conversations"]),
            "routine_conversation_count": len(acc["_routine_conversations"]),
            "user_count": len(acc["_users"]),
            "load_count": acc["load_count"],
            "last_used_at": _iso(acc["_last"]),
        })
    rows.sort(key=lambda r: (
        -r["conversation_count"], -r["load_count"], r["name"].lower(), r["skill_id"],
    ))
    return {
        "skills": rows,
        "conversation_count": len(active_with_skills),
        "scanned_conversations": len(records),
    }
