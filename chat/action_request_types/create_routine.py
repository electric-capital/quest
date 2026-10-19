"""CreateRoutineHandler -- approve-to-create action request for project routines.

Creates a new routine in the current conversation's project: ``name`` +
``prompt`` (both required) plus an optional ``model`` override (when omitted,
the pre-card check injects a default -- Sonnet 5, then Gemini 3.7 Flash, by
availability -- and the approval card labels it "(default)"), an optional
``schedule`` spec (the same full-replacement shape ``edit_routine`` takes),
and optional ``skill_ids`` to auto-load. Strictly project-scoped -- rejected
same-turn outside project conversations, in public projects (whose
routines are managed from the UI only -- the routine request types are not
public-safe), and on a per-project name collision by
``routine_precard_check`` in
:mod:`chat.action_request_types.routine_precard`, with the project/name
guards re-run at execute time.

There is deliberately no ``guide_id`` (guides are deprecated) and no
agent-side routine delete. Validation shared with ``edit_routine`` lives in
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
        "name",
        "prompt",
        "model",
        "schedule",
        "skill_ids",
    }
)


class CreateRoutineHandler(ActionRequestHandler):
    """Create a new project routine after user approval."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CREATE_ROUTINE

    @property
    def display_name(self) -> str:
        return "Create Routine"

    @property
    def approve_label(self) -> str:
        return "Create"

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        name = params.get("name")
        if name is None:
            raise ValueError("Missing required parameter: name")
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

        prompt = params.get("prompt")
        if prompt is None:
            raise ValueError("Missing required parameter: prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Routine prompt cannot be empty.")
        if len(prompt.encode("utf-8")) > MAX_ROUTINE_PROMPT_SIZE:
            raise ValueError(
                f"Routine prompt exceeds maximum size of "
                f"{MAX_ROUTINE_PROMPT_SIZE} bytes."
            )

        out: dict = {"name": name, "prompt": prompt}

        if "model" in params and params["model"] is not None:
            out["model"] = validate_model_value(params["model"])

        if "schedule" in params and params["schedule"] is not None:
            out["schedule"] = validate_schedule_spec(params["schedule"])

        if "skill_ids" in params and params["skill_ids"] is not None:
            out["skill_ids"] = validate_skill_id_list(params["skill_ids"], "skill_ids")

        return out

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        fields: list[dict] = [
            {"key": "Name", "value": str(params.get("name", ""))},
            {"key": "Prompt", "value": str(params.get("prompt", ""))},
        ]
        if "model" in params:
            model_value = str(params["model"])
            # model_defaulted is server-injected by the pre-card check when
            # the proposal omitted a model.
            if params.get("model_defaulted"):
                model_value += " (default)"
            fields.append({"key": "Model", "value": model_value})
        if "schedule" in params:
            fields.append(
                {"key": "Schedule", "value": describe_schedule(params["schedule"])}
            )
        if params.get("skill_ids"):
            # skill_names is server-injected by the pre-card check.
            skill_names = params.get("skill_names") or {}
            fields.append({
                "key": "Auto-load skills",
                "value": ", ".join(
                    skill_names.get(s, s) for s in params["skill_ids"]
                ),
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
        from db import project_store, routine_store, skill_store

        if not project_id:
            raise RuntimeError(
                "create_routine is only available in project conversations."
            )
        project = await project_store.get_project(user["id"], project_id)
        if not project:
            raise RuntimeError("Project not found.")
        if project.get("public"):
            raise RuntimeError(
                "Routines in public projects are managed from the UI."
            )

        # Re-verify skill access up front (the pre-card check is only the
        # same-turn fast path) so a failure can't leave a half-configured
        # routine behind.
        for skill_id in params.get("skill_ids", []):
            if not await skill_store.user_can_access_skill(user["id"], skill_id):
                raise RuntimeError(f"Skill not found or not accessible: {skill_id}")

        try:
            routine = await routine_store.create_routine(
                user_id=user["id"],
                project_id=project_id,
                name=params["name"],
                prompt=params["prompt"],
                model=params.get("model"),
            )
        except ValueError as e:
            raise RuntimeError(str(e))
        except Exception as e:
            if "unique" in str(e).lower():
                raise RuntimeError(
                    f"A routine named '{params['name']}' already exists in "
                    "this project."
                )
            raise
        routine_id = routine["id"]

        if "schedule" in params:
            await apply_schedule_spec(user["id"], routine_id, params["schedule"])

        for skill_id in params.get("skill_ids", []):
            await skill_store.set_routine_skill_autoload(routine_id, skill_id, True)

        return {"success": True, "routine_id": routine_id}
