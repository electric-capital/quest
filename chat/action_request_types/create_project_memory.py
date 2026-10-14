"""CreateProjectMemoryHandler -- approve-to-save action request for project
memories (chat/project_memories.py).

The project twin of ``create_memory``: the model issues
``create_action_request(request_type="create_project_memory",
params={"content": "..."})`` when the user explicitly asks it to record
something for the project; the approval card names the project, and
approving saves a ``project_memories`` row of the conversation's project.
Only private project conversations can propose it: the pre-card check in
chat/gemini_api/turn_tools.py rejects it elsewhere same-turn
(``project_memory_precard_check`` below) and ``execute`` re-checks.
"""

import logging

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from db.memory_store import MAX_MEMORY_SIZE
from db.models import ActionRequestType

logger = logging.getLogger(__name__)


_ALLOWED_PARAMS = frozenset({"content"})


async def _require_private_project(user: dict, project_id: str | None) -> dict:
    """The user's own private project, else ValueError."""
    if not project_id:
        raise ValueError(
            "create_project_memory is only available in project conversations "
            "(project memories belong to a project). Use create_memory for a "
            "memory about the user."
        )
    from db import project_store
    project = await project_store.get_project(user["id"], project_id)
    if not project:
        raise ValueError("Project not found.")
    if project.get("public"):
        raise ValueError("Public projects cannot have project memories.")
    return project


async def project_memory_precard_check(
    params: dict, user: dict, project_id: str | None,
) -> None:
    """Same-turn validation + preview enrichment (the project's name)."""
    project = await _require_private_project(user, project_id)
    params["project_name"] = project.get("name") or ""


class CreateProjectMemoryHandler(ActionRequestHandler):
    """Save a new project memory after explicit user approval.

    Params:
        content (str): Memory text. Non-empty after stripping; max 4 KB.
        project_name (str): Server-injected by the pre-card check for the
            card; never accepted from the model.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CREATE_PROJECT_MEMORY

    @property
    def display_name(self) -> str:
        return "Save Project Memory"

    @property
    def approve_label(self) -> str:
        return "Save"

    @property
    def preview_fields(self) -> list[str]:
        return ["content"]

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        content = params.get("content", "")
        if not content:
            return []
        rows = []
        if params.get("project_name"):
            rows.append({"key": "Project", "value": str(params["project_name"])})
        rows.append({"key": "Project memory", "value": str(content)})
        return rows

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        content = params.get("content")
        if content is None:
            raise ValueError("Missing required parameter: content")
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            raise ValueError("Memory content cannot be empty.")
        if len(content.encode("utf-8")) > MAX_MEMORY_SIZE:
            raise ValueError(
                f"Memory content exceeds maximum size of {MAX_MEMORY_SIZE} bytes."
            )
        return {"content": content}

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        await _require_private_project(user, project_id)
        from db.project_memory_store import create_project_memory
        memory = await create_project_memory(project_id, params["content"])
        logger.info(
            "[project-memories] Saved project memory %s (project=%s) via action request",
            memory["id"], project_id,
        )
        return {"success": True, "project_memory_id": memory["id"]}
