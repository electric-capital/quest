"""Handler for subagent_return action requests (cross-user subagent returns).

Created ONLY by the ``return_to_caller`` dispatch arm inside a
``origin="user_subagent"`` conversation -- the generic
``create_action_request`` path rejects this type. The card is shown to the
TARGET user (the account the subagent ran in) so they confirm the gathered
information may be shared back to the calling user, with an explicit list
of the workspace files that will be copied and a per-file preview in the
UI.

Resolution semantics (see chat/action_request_routes.py):

* Approve  -- execute() copies the listed files into the caller
  conversation's workspace under ``.subagent_responses/`` and resolves the
  caller-side ``user_subagent`` wait handle with the response text + the
  copied filenames.
* Revise   -- deny + feedback; the subagent loop resumes with the feedback
  and may propose another return call.
* Deny     -- (no feedback) ends the run immediately; the caller is told
  the target user denied the return call.
"""

import asyncio
import logging
import shutil
from pathlib import Path

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from db.models import ActionRequestType, ToolWaitHandleStatus

logger = logging.getLogger(__name__)

_MAX_RESPONSE_LEN = 50_000
_MAX_FILES = 10
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB per file

# Destination subdirectory in the CALLER's workspace. Dot-prefixed so the
# file browser hides it by default, mirroring the authed_get ``.responses``
# convention.
RETURN_FILES_DIR = ".subagent_responses"

# The model supplies only these; caller_name / caller_email / file_entries
# / run_id are injected by prepare_return_params in the dispatch arm.
_ALLOWED_PARAMS = frozenset({"response", "files"})


async def prepare_return_params(
    validated_params: dict, run: dict, subagent_conversation_id: str,
) -> dict:
    """Verify the listed files against the subagent workspace and enrich.

    Called by the ``return_to_caller`` dispatch arm after
    ``validate_params``, in the same pre-card position as the skill
    pre-card check: a ValueError here reaches the model as ``Invalid
    parameters`` with no card. Injects ``file_entries`` (name/path/size
    for the approval card's file list + preview buttons), the caller
    identity for display, and the ``run_id`` linkage.
    """
    from chat.file_storage import validate_path
    from chat.storage import ChatStorage
    from db.user_store import get_user_by_id

    files_root = ChatStorage.get_conversation_workspace_root(
        subagent_conversation_id,
    )

    file_entries: list[dict] = []
    for rel_path in validated_params.get("files") or []:
        ok, resolved = validate_path(files_root, rel_path)
        if not ok or resolved is None:
            raise ValueError(f"Invalid file path: {rel_path!r}")
        if not resolved.is_file():
            raise ValueError(
                f"File not found in the workspace: {rel_path!r}. Only "
                "existing workspace files can be returned."
            )
        size = resolved.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ValueError(
                f"File too large to return: {rel_path!r} "
                f"({size} bytes; max {_MAX_FILE_BYTES})"
            )
        file_entries.append({
            "path": str(resolved.relative_to(files_root)),
            "name": resolved.name,
            "size_bytes": size,
        })

    caller = await get_user_by_id(run["caller_user_id"])
    validated_params["file_entries"] = file_entries
    validated_params["caller_email"] = (caller or {}).get("email", "")
    validated_params["caller_name"] = (caller or {}).get("name", "")
    validated_params["run_id"] = run["id"]
    return validated_params


class SubagentReturnHandler(ActionRequestHandler):
    """Return a cross-user subagent's response to the calling user."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.SUBAGENT_RETURN

    @property
    def display_name(self) -> str:
        return "Return Subagent Response"

    @property
    def approve_label(self) -> str:
        return "Approve & Return"

    @property
    def resolved_label(self) -> str:
        return "Returned"

    def summary_snippet(self, params: dict) -> str:
        files = params.get("files")
        caller = str(params.get("caller_email") or "")
        if isinstance(files, list) and files:
            suffix = f" to {caller}" if caller else ""
            return f"Returned {len(files)} file(s){suffix}"
        return str(params.get("response") or "")

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(
            self.type_name.value, params, _ALLOWED_PARAMS,
        )

        response = params.get("response")
        if not isinstance(response, str) or not response.strip():
            raise ValueError("response is required and must be a non-empty string")
        response = response.strip()
        if len(response) > _MAX_RESPONSE_LEN:
            raise ValueError(
                f"response is too long ({len(response)} chars; "
                f"max {_MAX_RESPONSE_LEN})"
            )

        raw_files = params.get("files", [])
        if raw_files is None:
            raw_files = []
        if not isinstance(raw_files, list):
            raise ValueError(
                "files must be a list of workspace-relative path strings"
            )
        files: list[str] = []
        for entry in raw_files:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("files entries must be non-empty strings")
            entry = entry.strip()
            if entry not in files:
                files.append(entry)
        if len(files) > _MAX_FILES:
            raise ValueError(f"files accepts at most {_MAX_FILES} entries")

        return {"response": response, "files": files}

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        caller_name = params.get("caller_name") or ""
        caller_email = params.get("caller_email") or ""
        caller_display = (
            f"{caller_name} ({caller_email})" if caller_name else caller_email
        )
        fields: list[dict] = []
        if caller_display:
            fields.append({"key": "Returning To", "value": caller_display})
        fields.append({"key": "Response", "value": params.get("response", "")})

        file_entries = params.get("file_entries") or []
        if file_entries:
            fields.append({
                "key": "Files",
                "value": (
                    f"{len(file_entries)} file(s) will be copied into the "
                    "calling conversation's workspace"
                ),
                "type": "subagent_return_files",
                "files": file_entries,
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
        """Copy the approved files to the caller and wake their conversation."""
        from chat import user_subagent
        from chat.file_storage import validate_path
        from chat.realtime import bus, events as realtime_events
        from chat.storage import ChatStorage
        from db import tool_wait_handle_store
        from db.models import UserSubagentRunStatus
        from db.user_subagent_run_store import (
            TERMINAL_STATUSES,
            get_run_by_subagent_conversation,
            update_run_status,
        )

        if not conversation_id:
            raise ValueError("subagent_return requires a conversation context")

        run = await get_run_by_subagent_conversation(conversation_id)
        if run is None:
            raise ValueError(
                "No subagent run is linked to this conversation."
            )
        if run["status"] in TERMINAL_STATUSES:
            raise ValueError(
                f"This subagent run has already ended (status: "
                f"{run['status']})."
            )

        # The caller may have stopped waiting (handle timed out or was
        # cancelled). Surface that BEFORE copying anything into their
        # workspace.
        handle = await tool_wait_handle_store.get_handle(run["wait_handle_id"])
        if handle is None or handle.get("status") != ToolWaitHandleStatus.PENDING:
            raise ValueError(
                "The calling conversation is no longer waiting for this "
                "subagent (its wait handle is "
                f"{(handle or {}).get('status', 'missing')}); nothing was "
                "returned. Deny this request to close it."
            )

        # Re-verify the files against the live workspace (TOCTOU close --
        # the card may have sat open while the workspace changed).
        src_workspace = ChatStorage.get_conversation_workspace_root(conversation_id)
        sources: list[Path] = []
        for rel_path in params.get("files") or []:
            ok, resolved = validate_path(src_workspace, rel_path)
            if not ok or resolved is None or not resolved.is_file():
                raise ValueError(
                    f"File no longer exists in the workspace: {rel_path!r}. "
                    "Deny with feedback so the subagent can fix the file "
                    "list."
                )
            if resolved.stat().st_size > _MAX_FILE_BYTES:
                raise ValueError(f"File too large to return: {rel_path!r}")
            sources.append(resolved)

        # Copy into the caller's CONVERSATION workspace under
        # .subagent_responses/ (never the caller's project workspace, even
        # for a project conversation), never clobbering existing files
        # (suffix -2, -3, ... on collision).
        caller_root = ChatStorage.get_conversation_workspace_root(
            run["caller_conversation_id"],
        )
        dest_dir = caller_root / RETURN_FILES_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied_rel_paths: list[str] = []
        for src in sources:
            dest = dest_dir / src.name
            counter = 2
            while dest.exists():
                dest = dest_dir / f"{src.stem}-{counter}{src.suffix}"
                counter += 1
            await asyncio.to_thread(shutil.copy2, src, dest)
            copied_rel_paths.append(f"{RETURN_FILES_DIR}/{dest.name}")

        updated_run = await update_run_status(
            run["id"], UserSubagentRunStatus.RETURNED,
        )
        if updated_run is not None and updated_run["status"] != UserSubagentRunStatus.RETURNED:
            # A racing finalizer (hard deny / failure) won; roll nothing
            # back but do not deliver either.
            raise ValueError(
                f"This subagent run has already ended (status: "
                f"{updated_run['status']})."
            )

        await user_subagent.resolve_caller_handle(
            run,
            ToolWaitHandleStatus.ACCEPTED,
            {
                "status": "returned",
                "response": params.get("response", ""),
                "files": copied_rel_paths,
                "from_user": user.get("email", ""),
            },
        )

        # Refresh the caller's file browser if they have the conversation
        # open.
        if copied_rel_paths:
            try:
                bus.publish_to_user(
                    run["caller_user_id"],
                    realtime_events.make_file_list_changed(
                        run["caller_conversation_id"], None, "conversation",
                    ),
                )
            except Exception:
                logger.debug(
                    "[subagent_return] publish file_list_changed failed",
                    exc_info=True,
                )

        logger.info(
            "[subagent_return] Run %s returned to caller conversation %s "
            "(%d file(s))",
            run["id"], run["caller_conversation_id"], len(copied_rel_paths),
        )

        return {
            "success": True,
            "delivered": True,
            "files_returned": copied_rel_paths,
            "note": (
                "Response delivered to the calling user. This subagent "
                "run is complete -- do not take any further actions."
            ),
        }
