"""ArchiveMemoryHandler -- approve-to-archive action request for memories.

The model issues ``create_action_request(request_type="archive_memory",
params={"memory_id": "...", "reason": "..."}, ...)`` to retire one saved
memory during an interactive cleanup. ``scope`` picks the store: the user's
personal memories (the default) or the memories of the conversation's
project. On approve the memory is archived -- out of the prompt, search and
list, still restorable from Settings -- and ``reason`` is stored on the row
as its ``archive_reason``. Nothing is deleted.

``archive_memory_precard_check`` runs in the ``create_action_request``
dispatch arm before any card: it resolves the memory, refuses unknown and
already-archived ones same-turn, and snapshots the text into the
server-only ``memory_content`` param the card previews.
"""

import logging

from db.models import ActionRequestType
from db.memory_store import MAX_ARCHIVE_REASON_LEN
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._param_validation import reject_unknown_params

logger = logging.getLogger(__name__)


SCOPE_PERSONAL = "personal"
SCOPE_PROJECT = "project"
_SCOPES = (SCOPE_PERSONAL, SCOPE_PROJECT)

_ALLOWED_PARAMS = frozenset({"memory_id", "reason", "scope"})

_NO_PROJECT_ERROR = (
    "scope \"project\" is only available in a project conversation; this "
    "conversation has no project."
)


async def _load_memory(
    scope: str, memory_id: str, user: dict, project_id: str | None,
) -> dict:
    """The memory ``memory_id`` names in ``scope``, which must be active.

    Raises ValueError (worded for the model) when the scope has no project,
    the id matches nothing in that store, or the memory is already archived.
    """
    if scope == SCOPE_PROJECT:
        if not project_id:
            raise ValueError(_NO_PROJECT_ERROR)
        from db.project_memory_store import get_project_memory
        memory = await get_project_memory(project_id, memory_id)
        if memory is None:
            raise ValueError(
                f"No memory {memory_id!r} in this project's memories. Take "
                "the id from project_memory_list / project_memory_search; "
                "for one of the user's personal memories use scope "
                "\"personal\"."
            )
    else:
        from db.memory_store import get_memory
        memory = await get_memory(user["id"], memory_id)
        if memory is None:
            raise ValueError(
                f"No memory {memory_id!r} among the user's personal "
                "memories. Take the id from memory_list / memory_search; "
                "for a project memory pass scope \"project\"."
            )
    if memory.get("archived"):
        raise ValueError(f"Memory {memory_id!r} is already archived.")
    return memory


async def archive_memory_precard_check(
    validated_params: dict, user: dict, project_id: str | None,
) -> None:
    """Resolve the memory before the card is written.

    Injects the server-only ``memory_content`` (the text as it stands now)
    so the card shows what would be archived. Raises ValueError -- surfaced
    as ``Invalid parameters`` by the dispatch arm -- per ``_load_memory``.
    The execute-time re-check in the handler stays authoritative.
    """
    memory = await _load_memory(
        validated_params["scope"], validated_params["memory_id"],
        user, project_id,
    )
    validated_params["memory_content"] = memory["content"]


class ArchiveMemoryHandler(ActionRequestHandler):
    """Archive one personal or project memory after explicit user approval.

    Params:
        memory_id (str): Id of the memory to archive.
        reason (str): Why it is being archived; one short line, stored as
            the memory's ``archive_reason`` (max MAX_ARCHIVE_REASON_LEN
            characters).
        scope (str, optional): ``"personal"`` (default) or ``"project"``
            (a memory of the conversation's project).

    ``memory_content`` is server-injected by the pre-card check for the
    preview and never accepted from the model.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.ARCHIVE_MEMORY

    @property
    def display_name(self) -> str:
        return "Archive Memory"

    @property
    def approve_label(self) -> str:
        return "Archive"

    @property
    def resolved_label(self) -> str:
        return "Archived"

    @property
    def preview_fields(self) -> list[str]:
        return ["memory_content", "reason"]

    def summary_snippet(self, params: dict) -> str:
        content = params.get("memory_content")
        return content if isinstance(content, str) else ""

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        fields: list[dict] = []
        is_project = params.get("scope") == SCOPE_PROJECT
        content = params.get("memory_content")
        if content:
            fields.append({
                "key": "Project memory" if is_project else "Memory",
                "value": str(content),
            })
        reason = params.get("reason")
        if reason:
            fields.append({"key": "Reason", "value": str(reason)})
        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        memory_id = params.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("Missing required parameter: memory_id (a memory id string)")

        reason = params.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                "Missing required parameter: reason (one short line saying "
                "why this memory is being archived)"
            )
        reason = reason.strip()
        if len(reason) > MAX_ARCHIVE_REASON_LEN:
            raise ValueError(
                f"reason exceeds {MAX_ARCHIVE_REASON_LEN} characters; keep "
                "it to one short line."
            )

        scope = params.get("scope", SCOPE_PERSONAL)
        if scope not in _SCOPES:
            raise ValueError(
                f"scope must be one of {list(_SCOPES)!r} (default \"personal\")."
            )

        return {"memory_id": memory_id.strip(), "reason": reason, "scope": scope}

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        scope = params.get("scope", SCOPE_PERSONAL)
        memory_id = params["memory_id"]
        reason = params["reason"]
        # Re-check at approve time: the memory may have been archived or
        # deleted from Settings while the card was open.
        await _load_memory(scope, memory_id, user, project_id)

        if scope == SCOPE_PROJECT:
            from db.project_memory_store import set_project_memory_archived
            archived = await set_project_memory_archived(
                project_id, memory_id, True, reason=reason,
            )
        else:
            from db.memory_store import archive_memory
            archived = await archive_memory(user["id"], memory_id, reason=reason)
        if archived is None:
            raise ValueError(f"Memory {memory_id!r} no longer exists.")

        logger.info(
            "[memory] Archived %s memory %s for user_id %s via action request",
            scope, memory_id, user["id"],
        )
        return {
            "success": True,
            "memory_id": memory_id,
            "scope": scope,
            "archived": True,
        }
