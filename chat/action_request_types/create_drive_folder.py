"""CreateDriveFolderHandler -- approve-to-create action request for Drive
folders.

The model issues a
``create_action_request(request_type="create_drive_folder",
params={"name": "...", ...}, reasoning="...")`` call which appends an
inline approval card to the chat. On Approve, the handler creates the
folder in the user's Google Drive via the ``files.create`` metadata
endpoint (shared drives supported). On Deny/Revise nothing is created.
"""

from __future__ import annotations

import logging

from chat.action_request_types import _drive
from chat.action_request_types._drive import (
    DRIVE_FILE_SCOPE as _DRIVE_FILE_SCOPE,
    _drive_reauth_message,
    _resolve_folder_name,
)
from chat.action_request_types._google import require_google_scope
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._param_validation import reject_unknown_params
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

# `parent_folder_name` is server-injected after validation by the dispatch
# arm (conversation.py); it is not model-supplied and is therefore not in
# the allow-list (parallels `folder_name` for upload_to_drive).
_ALLOWED_PARAMS = frozenset({"name", "parent_folder_id"})


class CreateDriveFolderHandler(ActionRequestHandler):
    """Create a Google Drive folder after explicit user approval.

    Params:
        name (str): Name of the folder to create. Required, non-empty.
        parent_folder_id (str, optional): Parent Drive folder id (shared
            drives supported). Defaults to the Drive root ("My Drive").
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.CREATE_DRIVE_FOLDER

    @property
    def display_name(self) -> str:
        return "Create Drive Folder"

    @property
    def approve_label(self) -> str:
        return "Create"

    @property
    def resolved_label(self) -> str:
        return "Created"

    def summary_snippet(self, params: dict) -> str:
        return str(params.get("name") or "")

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        name = params.get("name", "")

        parent_id = params.get("parent_folder_id")
        if parent_id:
            parent_value = params.get("parent_folder_name")
            if not parent_value and user:
                parent_value = await _resolve_folder_name(str(parent_id), user)
            if not parent_value:
                parent_value = str(parent_id)
        else:
            parent_value = "My Drive (root)"

        return [
            {"key": "Folder", "value": str(name)},
            {"key": "Parent", "value": str(parent_value)},
        ]

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        name = params.get("name")
        if name is None or not isinstance(name, str):
            raise ValueError("Missing required parameter: name")
        # Drive folder names are metadata, not filesystem paths --
        # trim + non-empty only (no filename sanitization).
        name = name.strip()
        if not name:
            raise ValueError("name must be a non-empty string")

        validated: dict = {"name": name}

        parent_folder_id = params.get("parent_folder_id")
        if parent_folder_id is not None:
            if not isinstance(parent_folder_id, str):
                raise ValueError("parent_folder_id must be a string if provided")
            parent_folder_id = parent_folder_id.strip()
            if not parent_folder_id:
                raise ValueError(
                    "parent_folder_id must be a non-empty string if provided"
                )
            validated["parent_folder_id"] = parent_folder_id

        return validated

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        require_google_scope(user, _DRIVE_FILE_SCOPE, _drive_reauth_message())

        payload = await _drive.create_folder(
            user,
            params["name"],
            params.get("parent_folder_id"),
        )

        folder_id = payload.get("id", "")
        return {
            "success": True,
            "folder_id": folder_id,
            "name": payload.get("name", params["name"]),
            "url": f"https://drive.google.com/drive/folders/{folder_id}",
        }
