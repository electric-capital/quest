"""CreateSkillHandler -- approve-to-create action request for DB-backed skills.

The model issues a
``create_action_request(request_type="create_skill", params={...}, ...)``
call which appends an inline approval card to the chat. On approve the skill
is persisted via ``db.skill_store.create_skill``. Skills are an internal Quest
store (like memory), so this handler -- like ``create_memory`` -- has NO
``validate_against_upstream`` and no external API key.

Two targets are supported (D2):
- ``target="user"`` (default): a user-level skill owned by the caller, with a
  visibility in ``{private, shared, public}`` and an optional ``share_emails``
  roster (only when ``visibility == "shared"``).
- ``target="project"``: a project-scoped skill, created only when the
  conversation runs inside a project the caller owns. Project skills have a
  fixed ``visibility="project"`` and no sharing roster, so ``visibility`` /
  ``share_emails`` are rejected.
"""

import logging

from db.models import ActionRequestType
from db.skill_store import (
    MAX_SKILL_NAME_LENGTH,
    MAX_SKILL_DESCRIPTION_LENGTH,
    MAX_SKILL_CONTENT_SIZE,
)
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._param_validation import reject_unknown_params

logger = logging.getLogger(__name__)


def _mark_created_skill_read(
    conversation_id: str | None, user_id: int, skill_id: str,
    project_id: str | None = None,
) -> None:
    """Best-effort: the model authored the new skill's full content, so it
    counts as read for the edit_skill content-edit gate (parallel to
    write_workspace_file licensing edit_workspace_file), and as a
    ``create_skill`` load in the skill_loads ledger."""
    if not conversation_id:
        return
    try:
        from chat.storage import ChatStorage
        ChatStorage.add_skill_read_ids(conversation_id, [skill_id])
    except Exception:
        logger.debug(
            "[create_skill] failed to record skill read "
            "(conversation_id=%s, skill_id=%s)",
            conversation_id, skill_id, exc_info=True,
        )
    from analytics.ledger import record_skill_load
    record_skill_load(
        conversation_id=conversation_id, user_id=user_id, skill_ids=[skill_id],
        source="create_skill", project_id=project_id,
    )


_ALLOWED_PARAMS = frozenset(
    {"target", "name", "description", "content", "visibility", "share_emails"}
)

_VALID_TARGETS = frozenset({"user", "project"})
# User-level skills only; "project" is forced for project skills and "system"
# is reserved for hardcoded system skills, so neither is selectable here.
_USER_VISIBILITIES = frozenset({"private", "shared", "public"})


class CreateSkillHandler(ActionRequestHandler):
    """Create a new DB-backed skill after explicit user approval.

    Params:
        target (str): "user" (default) or "project".
        name (str): Skill name. Non-empty after stripping; max 100 chars.
        content (str): Full skill instructions. Non-empty; max 64 KB.
        description (str): Optional short description; max 500 chars.
        visibility (str): User target only. One of {private, shared, public};
            defaults to "private". Rejected for project target.
        share_emails (list[str]): User target only, and only when
            visibility == "shared". Emails to share the new skill with;
            unknown / self emails are silently skipped at execute time.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CREATE_SKILL

    @property
    def display_name(self) -> str:
        return "Create Skill"

    @property
    def approve_label(self) -> str:
        return "Create"

    @property
    def resolved_label(self) -> str:
        return "Created"

    def summary_snippet(self, params: dict) -> str:
        prefix = "Project: " if params.get("target") == "project" else ""
        return f"{prefix}{params.get('name') or ''}"

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        # target
        target = params.get("target", "user")
        if not isinstance(target, str) or target not in _VALID_TARGETS:
            raise ValueError(
                f"Invalid target '{target}'. Must be one of: "
                f"{sorted(_VALID_TARGETS)}."
            )

        # name (required)
        name = params.get("name")
        if name is None:
            raise ValueError("Missing required parameter: name")
        if not isinstance(name, str):
            name = str(name)
        name = name.strip()
        if not name:
            raise ValueError("Skill name cannot be empty.")
        if len(name) > MAX_SKILL_NAME_LENGTH:
            raise ValueError(
                f"Skill name exceeds maximum length of {MAX_SKILL_NAME_LENGTH} characters."
            )

        # content (required)
        content = params.get("content")
        if content is None:
            raise ValueError("Missing required parameter: content")
        if not isinstance(content, str):
            content = str(content)
        if not content.strip():
            raise ValueError("Skill content cannot be empty.")
        if len(content.encode("utf-8")) > MAX_SKILL_CONTENT_SIZE:
            raise ValueError(
                f"Skill content exceeds maximum size of {MAX_SKILL_CONTENT_SIZE} bytes."
            )

        out: dict = {"target": target, "name": name, "content": content}

        # description (optional)
        if "description" in params and params["description"] is not None:
            description = params["description"]
            if not isinstance(description, str):
                description = str(description)
            if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
                raise ValueError(
                    f"Skill description exceeds maximum length of "
                    f"{MAX_SKILL_DESCRIPTION_LENGTH} characters."
                )
            out["description"] = description

        if target == "project":
            # Project skills have a fixed visibility="project" and no roster.
            if "visibility" in params:
                raise ValueError(
                    "visibility is not allowed for project skills (project "
                    "skills are always project-visible)."
                )
            if "share_emails" in params:
                raise ValueError(
                    "share_emails is not allowed for project skills (project "
                    "skills have no sharing roster)."
                )
            return out

        # --- user target ---
        visibility = params.get("visibility", "private")
        if not isinstance(visibility, str) or visibility not in _USER_VISIBILITIES:
            raise ValueError(
                f"Invalid visibility '{visibility}'. Must be one of: "
                f"{sorted(_USER_VISIBILITIES)}."
            )
        out["visibility"] = visibility

        if "share_emails" in params:
            share_emails = params["share_emails"]
            if visibility != "shared":
                raise ValueError(
                    "share_emails is only valid when visibility is 'shared'."
                )
            if not isinstance(share_emails, list):
                raise ValueError("share_emails must be a list of email strings.")
            normalized: list[str] = []
            for email in share_emails:
                if not isinstance(email, str) or not email.strip():
                    raise ValueError(
                        "share_emails must be a list of non-empty email strings."
                    )
                normalized.append(email.strip())
            out["share_emails"] = normalized

        return out

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        target = params.get("target", "user")
        fields: list[dict] = [
            {
                "key": "Target",
                "value": "Project skill" if target == "project" else "User skill",
            },
            {"key": "Name", "value": str(params.get("name", ""))},
        ]
        if target != "project":
            fields.append(
                {"key": "Visibility", "value": str(params.get("visibility", "private"))}
            )
        description = params.get("description")
        if description:
            fields.append({"key": "Description", "value": str(description)})
        share_emails = params.get("share_emails")
        if share_emails:
            fields.append({"key": "Shared with", "value": ", ".join(share_emails)})
        # Full content (previews are not truncated).
        content = params.get("content")
        if content:
            fields.append({"key": "Content", "value": str(content)})
        return fields

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        from db import skill_store, project_store, user_store

        target = params.get("target", "user")
        name = params["name"]

        if target == "project":
            if not project_id:
                raise RuntimeError(
                    "This conversation is not in a project, so a project "
                    "skill cannot be created here."
                )
            project = await project_store.get_project(user["id"], project_id)
            if project is None:
                raise RuntimeError("This conversation is not in a project you own.")
            if project.get("public"):
                raise RuntimeError("Public projects cannot have project skills.")

            # TOCTOU re-check: the store raises ValueError on a per-project
            # name collision; surface it as a clean RuntimeError so the request
            # stays open and the model sees a readable error.
            try:
                skill = await skill_store.create_skill(
                    creator_id=user["id"],
                    name=name,
                    description=params.get("description", ""),
                    content=params["content"],
                    project_id=project_id,
                )
            except ValueError as e:
                raise RuntimeError(str(e))

            _mark_created_skill_read(conversation_id, user["id"], skill["id"], project_id)
            return {
                "success": True,
                "skill_id": skill["id"],
                "target": "project",
                "shared_with": [],
            }

        # --- user target ---
        # TOCTOU re-check: per-creator name collision.
        owned = await skill_store.list_accessible_skills(user["id"], owned_only=True)
        if any(s["name"] == name for s in owned):
            raise RuntimeError(
                f"A skill named '{name}' already exists for you."
            )

        visibility = params.get("visibility", "private")
        skill = await skill_store.create_skill(
            creator_id=user["id"],
            name=name,
            description=params.get("description", ""),
            content=params["content"],
            visibility=visibility,
        )

        shared_with: list[str] = []
        if visibility == "shared" and params.get("share_emails"):
            resolved_ids: list[int] = []
            for email in params["share_emails"]:
                target_user = await user_store.get_user_by_email(email.strip())
                if target_user and target_user["id"] != user["id"]:
                    resolved_ids.append(target_user["id"])
                    shared_with.append(email.strip())
            if resolved_ids:
                await skill_store.add_skill_shares(skill["id"], resolved_ids)

        _mark_created_skill_read(conversation_id, user["id"], skill["id"])
        return {
            "success": True,
            "skill_id": skill["id"],
            "target": "user",
            "shared_with": shared_with,
        }
