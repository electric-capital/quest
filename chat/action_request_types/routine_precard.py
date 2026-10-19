"""Pre-card checks + preview enrichment for routine create/edit action requests.

Runs in the ``create_action_request`` dispatch arm (after
``validate_against_upstream``, before the request row is written), parallel
to ``skill_precard_check``: rejections raise ``ValueError`` so they surface
as same-turn ``Invalid parameters`` with no card, letting the model
self-correct. It performs:

- **Project-scoping pre-check:** both types require a project conversation;
  ``create_routine`` additionally verifies the project exists for the user
  and is not public (routines there are managed from the UI only), while
  ``edit_routine`` verifies the routine exists and belongs to the
  conversation's project.
- **Name-collision pre-check:** per-project unique routine names, for
  ``create_routine`` always and for ``edit_routine`` only when ``name`` is
  changing.
- **Skill-access pre-check:** every id in ``skill_ids`` (create) /
  ``add_skill_ids`` (edit) must pass ``user_can_access_skill``.
- **Model defaulting (create only):** a ``create_routine`` proposal without
  an explicit ``model`` gets one injected server-side via
  ``pick_default_routine_model`` (Sonnet 5, then Gemini 3.7 Flash, first
  available; unset when neither is), with ``model_defaulted`` set so the
  approval card labels the row as a default rather than an agent choice.
- **Preview enrichment:** injects a ``skill_names`` id->name map for the
  card's skill rows, and -- for ``edit_routine`` -- ``current_routine_name``
  for the card header, the routine row's ``updated_at`` as
  ``expected_updated_at`` (the execute-time optimistic-concurrency token),
  and ``content_diff`` (the ``build_content_diff`` line diff of the current
  vs proposed prompt) when the edit changes the prompt, rendered by the
  same ``skill_content_diff`` preview field the skill edits use.

These are an agent-experience optimization; the execute-time guards in the
handlers remain the authoritative enforcement (TOCTOU close).
"""

from chat.action_request_types._routine_validation import (
    pick_default_routine_model,
)
from chat.action_request_types._skill_content_edit import build_content_diff
from db import project_store, routine_store, skill_store


async def routine_precard_check(
    req_type: str,
    params: dict,
    user: dict,
    project_id: str | None,
) -> None:
    """Run pre-card checks, mutating ``params`` for enrichment.

    Raises:
        ValueError: On a missing/public project, a missing or
            wrong-project routine, a name collision, or an inaccessible
            skill -- surfaced as ``Invalid parameters`` by the dispatch arm.
    """
    if not project_id:
        raise ValueError(
            f"{req_type} is only available in project conversations "
            "(routines belong to projects)."
        )

    if req_type == "create_routine":
        await _create_precard(params, user, project_id)
    elif req_type == "edit_routine":
        await _edit_precard(params, user, project_id)

    await _check_and_name_skills(params, user)


async def _create_precard(params: dict, user: dict, project_id: str) -> None:
    project = await project_store.get_project(user["id"], project_id)
    if not project:
        raise ValueError("Project not found.")
    if project.get("public"):
        raise ValueError("Routines in public projects are managed from the UI.")

    existing = await routine_store.list_routines(project_id)
    if any(r["name"] == params["name"] for r in existing):
        raise ValueError(
            f"A routine named '{params['name']}' already exists in this "
            "project."
        )

    # Model defaulting: a proposal without an explicit model gets one picked
    # server-side (Sonnet 5, then Gemini 3.7 Flash, by availability) so the
    # approval card can show exactly what the routine will run on.
    # ``model_defaulted`` marks the card row as a default rather than an
    # agent choice. When neither preferred model is available the param
    # stays unset and the routine keeps the run-time default behavior.
    if "model" not in params:
        default_model = pick_default_routine_model()
        if default_model:
            params["model"] = default_model
            params["model_defaulted"] = True


async def _edit_precard(params: dict, user: dict, project_id: str) -> None:
    routine = await routine_store.get_routine(user["id"], params["routine_id"])
    if not routine or routine["project_id"] != project_id:
        raise ValueError(
            "Routine not found in this project. Use list_routines to see "
            "this project's routines and their ids."
        )

    # Name collision pre-check (per-project unique index).
    new_name = params.get("name")
    if new_name and new_name != routine["name"]:
        existing = await routine_store.list_routines(project_id)
        if any(r["name"] == new_name and r["id"] != routine["id"] for r in existing):
            raise ValueError(
                f"A routine named '{new_name}' already exists in this project."
            )

    # Prompt diff for the approval card (full-replacement edits still get a
    # reviewable line diff, reusing the skill content-diff renderer).
    if "prompt" in params and params["prompt"] != routine["prompt"]:
        params["content_diff"] = build_content_diff(
            routine["prompt"], params["prompt"]
        )

    params["current_routine_name"] = routine["name"]
    # Captured now, checked at Approve time: an edit that lands while the
    # card sits open fails cleanly instead of being silently clobbered.
    params["expected_updated_at"] = routine["updated_at"]


async def _check_and_name_skills(params: dict, user: dict) -> None:
    """Verify access to added skills and inject the id->name preview map."""
    add_ids = params.get("skill_ids", []) + params.get("add_skill_ids", [])
    for skill_id in add_ids:
        if not await skill_store.user_can_access_skill(user["id"], skill_id):
            raise ValueError(f"Skill not found or not accessible: {skill_id}")

    skill_names: dict[str, str] = {}
    for skill_id in add_ids + params.get("remove_skill_ids", []):
        skill = await skill_store.get_skill(skill_id)
        if skill:
            skill_names[skill_id] = skill["name"]
    if skill_names:
        params["skill_names"] = skill_names
