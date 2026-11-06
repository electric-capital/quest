"""Project-routine inspection handler (list_routines).
"""

import json


async def _handle_list_routines(
    user_id: int,
    project_id: str | None = None,
) -> str:
    """List the current project's routines with schedules and auto-loaded skills.

    Read-only inspection tool backing ``list_routines``. Routines are
    project-scoped, so a conversation outside a project gets a structured
    error. Prompts are returned in full (they are capped at 16 KB) so the
    model can propose ``edit_routine`` changes without a second read.

    Args:
        user_id: User's integer ID.
        project_id: Conversation's project ID, or None.

    Returns:
        JSON string ``{"routine_count": N, "routines": [...]}``.
    """
    if not project_id:
        return json.dumps({
            "error": (
                "list_routines is only available in project conversations "
                "(routines belong to projects)."
            ),
        })
    try:
        from db.routine_store import list_routines_with_schedules
        from db.skill_store import list_routine_autoloaded_skill_ids, get_skill

        routines = await list_routines_with_schedules(project_id)

        payload: list[dict] = []
        for r in routines:
            skills: list[dict] = []
            for skill_id in await list_routine_autoloaded_skill_ids(r["id"]):
                skill = await get_skill(skill_id)
                skills.append({
                    "id": skill_id,
                    "name": skill["name"] if skill else None,
                })
            payload.append({
                "id": r["id"],
                "name": r["name"],
                "prompt": r["prompt"],
                "model": r["model"],
                "schedule": r["schedule"],
                "autoloaded_skills": skills,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })

        return json.dumps({
            "routine_count": len(payload),
            "routines": payload,
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to list routines: {e}"})

