"""EditRoutineHandler -- approve-to-edit action request for project routines.

Partial-edit an existing routine identified by ``routine_id`` (the UUID the
model gets from the ``list_routines`` read tool). Routines are strictly
project-scoped, so the request is only valid in a project conversation and
only for a routine belonging to that project -- enforced same-turn by
``routine_precard_check`` in :mod:`chat.action_request_types.routine_precard`
(which also injects preview enrichment -- incl. the ``content_diff`` line
diff for prompt changes -- and the optimistic-concurrency token) and again
at execute time.

Editable pieces:

- ``name`` / ``prompt`` / ``model`` (full-value replacement; ``clear_model``
  resets the routine to the default model).
- ``schedule`` -- a full replacement spec for the routine's (single)
  schedule, created if none exists; ``clear_schedule`` deletes it.
- ``add_skill_ids`` / ``remove_skill_ids`` -- toggle the routine's
  auto-loaded skills (the ``routine_skill_autoloads`` tier).

Validation shared with ``create_routine`` (schedule spec, model registry
check, skill-id lists) lives in
:mod:`chat.action_request_types._routine_validation`. Like the skill
handlers there is no ``validate_against_upstream`` -- routines are an
internal Quest store.
"""

import logging

from db.models import ActionRequestType
from db.routine_store import (
    MAX_ROUTINE_NAME_LENGTH,
    MAX_ROUTINE_PROMPT_SIZE,
)
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types._routine_validation import (
    apply_schedule_spec,
    describe_schedule,
    validate_model_value,
    validate_schedule_spec,
    validate_skill_id_list,
)

logger = logging.getLogger(__name__)


_ALLOWED_PARAMS = frozenset(
    {
        "routine_id",
        "name",
        "prompt",
        "model",
        "clear_model",
        "schedule",
        "clear_schedule",
        "add_skill_ids",
        "remove_skill_ids",
    }
)

# Fields that count as a meaningful edit (routine_id alone is rejected).
_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "prompt",
        "model",
        "clear_model",
        "schedule",
        "clear_schedule",
        "add_skill_ids",
        "remove_skill_ids",
    }
)


class EditRoutineHandler(ActionRequestHandler):
    """Edit an existing project routine (partial update) after user approval."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.EDIT_ROUTINE

    @property
    def display_name(self) -> str:
        return "Edit Routine"

    @property
    def approve_label(self) -> str:
        return "Save"

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        routine_id = params.get("routine_id")
        if routine_id is None:
            raise ValueError("Missing required parameter: routine_id")
        if not isinstance(routine_id, str) or not routine_id.strip():
            raise ValueError("routine_id must be a non-empty string.")

        out: dict = {"routine_id": routine_id.strip()}

        if "name" in params and params["name"] is not None:
            name = params["name"]
            if not isinstance(name, str):
                name = str(name)
            name = name.strip()
            if not name:
                raise ValueError("Routine name cannot be empty.")
            if len(name) > MAX_ROUTINE_NAME_LENGTH:
                raise ValueError(
                    f"Routine name exceeds maximum length of "
                    f"{MAX_ROUTINE_NAME_LENGTH} characters."
                )
            out["name"] = name

        if "prompt" in params and params["prompt"] is not None:
            prompt = params["prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("Routine prompt cannot be empty.")
            if len(prompt.encode("utf-8")) > MAX_ROUTINE_PROMPT_SIZE:
                raise ValueError(
                    f"Routine prompt exceeds maximum size of "
                    f"{MAX_ROUTINE_PROMPT_SIZE} bytes."
                )
            out["prompt"] = prompt

        if "clear_model" in params and params["clear_model"] is not None:
            if not isinstance(params["clear_model"], bool):
                raise ValueError("clear_model must be a boolean.")
            if params["clear_model"]:
                out["clear_model"] = True

        if "model" in params and params["model"] is not None:
            if out.get("clear_model"):
                raise ValueError("model and clear_model are mutually exclusive.")
            out["model"] = validate_model_value(params["model"])

        if "clear_schedule" in params and params["clear_schedule"] is not None:
            if not isinstance(params["clear_schedule"], bool):
                raise ValueError("clear_schedule must be a boolean.")
            if params["clear_schedule"]:
                out["clear_schedule"] = True

        if "schedule" in params and params["schedule"] is not None:
            if out.get("clear_schedule"):
                raise ValueError("schedule and clear_schedule are mutually exclusive.")
            out["schedule"] = validate_schedule_spec(params["schedule"])

        if "add_skill_ids" in params and params["add_skill_ids"] is not None:
            out["add_skill_ids"] = validate_skill_id_list(
                params["add_skill_ids"], "add_skill_ids"
            )
        if "remove_skill_ids" in params and params["remove_skill_ids"] is not None:
            out["remove_skill_ids"] = validate_skill_id_list(
                params["remove_skill_ids"], "remove_skill_ids"
            )
        overlap = set(out.get("add_skill_ids", [])) & set(out.get("remove_skill_ids", []))
        if overlap:
            raise ValueError(
                f"Skill id(s) in both add_skill_ids and remove_skill_ids: "
                f"{sorted(overlap)}"
            )

        if not (set(out) & _EDITABLE_FIELDS):
            raise ValueError(
                "No editable fields provided. Include at least one of: name, "
                "prompt, model, clear_model, schedule, clear_schedule, "
                "add_skill_ids, remove_skill_ids."
            )

        return out

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        routine_id = params.get("routine_id", "")
        # current_routine_name / skill_names / content_diff are
        # server-injected by the pre-card check (not in _ALLOWED_PARAMS),
        # parallel to current_skill_name on edit_skill.
        current_name = params.get("current_routine_name")
        header = current_name or f"Routine #{str(routine_id)[:8]}"
        fields: list[dict] = [{"key": "Routine", "value": str(header)}]

        if "name" in params:
            fields.append({"key": "Name", "value": str(params["name"])})
        if "prompt" in params:
            diff = params.get("content_diff")
            if isinstance(diff, dict) and isinstance(diff.get("lines"), list):
                fields.append({
                    "key": "Prompt",
                    "value": (
                        f"+{diff.get('added', 0)} / -{diff.get('removed', 0)} "
                        "line(s)"
                    ),
                    "type": "skill_content_diff",
                    "diff": diff,
                })
            else:
                # Fallback (no injected diff): show the raw new prompt.
                fields.append({"key": "Prompt", "value": str(params["prompt"])})
        if "model" in params:
            fields.append({"key": "Model", "value": str(params["model"])})
        if params.get("clear_model"):
            fields.append({"key": "Model", "value": "Reset to default"})
        if "schedule" in params:
            fields.append(
                {"key": "Schedule", "value": describe_schedule(params["schedule"])}
            )
        if params.get("clear_schedule"):
            fields.append({"key": "Schedule", "value": "Remove schedule"})

        skill_names = params.get("skill_names") or {}

        def _label(skill_id: str) -> str:
            return skill_names.get(skill_id, skill_id)

        if params.get("add_skill_ids"):
            fields.append({
                "key": "Auto-load skills",
                "value": ", ".join(_label(s) for s in params["add_skill_ids"]),
            })
        if params.get("remove_skill_ids"):
            fields.append({
                "key": "Remove auto-load skills",
                "value": ", ".join(_label(s) for s in params["remove_skill_ids"]),
            })
        return fields

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        from db import routine_store, schedule_store, skill_store

        routine_id = params["routine_id"]
        routine = await routine_store.get_routine(user["id"], routine_id)
        if not routine:
            raise RuntimeError("Routine not found.")
        if not project_id or routine["project_id"] != project_id:
            raise RuntimeError("Routine does not belong to this conversation's project.")

        # Re-verify skill access up front (the pre-card check is only the
        # same-turn fast path) so a failure can't leave the routine-row and
        # schedule edits half-applied.
        for skill_id in params.get("add_skill_ids", []):
            if not await skill_store.user_can_access_skill(user["id"], skill_id):
                raise RuntimeError(f"Skill not found or not accessible: {skill_id}")

        # Routine-row fields (name / prompt / model) go through
        # update_routine with the proposal-time optimistic-concurrency
        # token, so a routine edited while the card sat open fails cleanly.
        model_value = ...
        if params.get("clear_model"):
            model_value = None
        elif "model" in params:
            model_value = params["model"]
        if "name" in params or "prompt" in params or model_value is not ...:
            try:
                updated = await routine_store.update_routine(
                    user_id=user["id"],
                    routine_id=routine_id,
                    name=params.get("name"),
                    prompt=params.get("prompt"),
                    model=model_value,
                    expected_updated_at=params.get("expected_updated_at"),
                )
            except routine_store.StaleRoutineError:
                raise RuntimeError(
                    "This routine was modified after the request was "
                    "proposed. Re-read it with list_routines and propose "
                    "the edit again."
                )
            except ValueError as e:
                raise RuntimeError(str(e))
            except Exception as e:
                if "unique" in str(e).lower():
                    raise RuntimeError(
                        "A routine with that name already exists in this project."
                    )
                raise
            if updated is None:
                raise RuntimeError("Routine not found.")

        if params.get("clear_schedule"):
            await schedule_store.delete_schedule_for_routine(routine_id)

        if "schedule" in params:
            await apply_schedule_spec(user["id"], routine_id, params["schedule"])

        for skill_id in params.get("add_skill_ids", []):
            await skill_store.set_routine_skill_autoload(routine_id, skill_id, True)
        for skill_id in params.get("remove_skill_ids", []):
            await skill_store.set_routine_skill_autoload(routine_id, skill_id, False)

        return {"success": True, "routine_id": routine_id}
