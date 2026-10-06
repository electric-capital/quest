"""WriteDocHandler -- approve-to-write action request for Quest Docs.

The approval form of the three doc write tools. A write to a shared private
doc (``resolve_doc_access(...).write == "approval"``) makes ``edit_doc`` /
``append_to_doc`` / ``add_doc_image`` refuse with ``approval_required`` and a
``suggested_request`` the model forwards unchanged as
``create_action_request(request_type="write_doc", params=...)``. One type
covers the three operations:

- ``edit``: ``{operation, doc_id, old_string, new_string, replace_all?}``
- ``append``: ``{operation, doc_id, content, ensure_blank_line?}``
- ``add_image``: ``{operation, doc_id, workspace_path, alt?, placement?}``

``validate_params`` accepts exactly those key sets (the ``suggested_request``
round-trips; a test pins it) and fills the optional keys' defaults. The
pre-card check (:mod:`chat.action_request_types.doc_precard`) resolves
access through the service (must be ``approval``), runs the operation
against the live body and injects the server-only preview keys
(:data:`SERVER_INJECTED_KEYS`). ``execute`` calls
``chat.docs.service.apply_write_operation(..., bypass_approval=True)``, which
re-resolves access (a doc hidden meanwhile -- the share revoked, the doc
deleted -- refuses; a doc whose shares were all removed is simply written),
re-checks the read sidecar for ``edit`` and re-applies against the live
body (TOCTOU: a stale ``old_string`` fails the approve), then snapshots,
writes, bumps ``updated_at`` and records
``last_write_source = "action_request:<id>"``.
The service publishes ``doc_changed`` / ``doc_list_changed`` itself.

Like the skill handlers there is no ``validate_against_upstream`` -- docs
are an internal Quest store.
"""

import logging

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from chat.docs import constants as doc_constants
from chat.docs import service as doc_service
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

WRITE_OPERATIONS = doc_service.WRITE_OPERATIONS
IMAGE_PLACEMENTS = ("append", "none")

# Per-operation keys besides ``operation`` / ``doc_id`` (the service's
# ``apply_write_operation`` params), split into required and optional.
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "edit": ("old_string", "new_string"),
    "append": ("content",),
    "add_image": ("workspace_path",),
}
_OPTIONAL_KEYS: dict[str, tuple[str, ...]] = {
    "edit": ("replace_all",),
    "append": ("ensure_blank_line",),
    "add_image": ("alt", "placement"),
}
OPERATION_PARAM_KEYS: dict[str, tuple[str, ...]] = {
    op: _REQUIRED_KEYS[op] + _OPTIONAL_KEYS[op] for op in WRITE_OPERATIONS
}
_ALL_PARAMS = frozenset(
    {"operation", "doc_id"}
    | {key for keys in OPERATION_PARAM_KEYS.values() for key in keys}
)

# Server-only preview keys written by the pre-card check. A model-supplied
# copy is dropped by validate_params so only the pre-card's values reach the
# card.
SERVER_INJECTED_KEYS = frozenset(
    {
        "content_diff",
        "current_title",
        "doc_mode",
        "doc_scope",
        "share_summary",
        "image_preview",
    }
)

# The approval-free tool for each operation (named by the pre-card when the
# doc turns out to be writable directly).
OPERATION_TOOLS = {
    "edit": "edit_doc",
    "append": "append_to_doc",
    "add_image": "add_doc_image",
}

# Collapsed-card snippet verbs. Neutral (not past tense): the snippet is
# also shown on denied / stopped cards ("Denied: #12 Write Doc -- Edit
# 'Notes'"), and resolved_label carries the outcome ("Applied: ...").
_SNIPPET_VERBS = {
    "edit": "Edit",
    "append": "Append to",
    "add_image": "Add image to",
}

_OPERATION_LABELS = {
    "edit": "Edit",
    "append": "Append",
    "add_image": "Add image",
}


def operation_params(params: dict) -> dict:
    """The service params of one validated request (no ``operation`` /
    ``doc_id``, no server-injected preview keys)."""
    operation = params.get("operation")
    keys = OPERATION_PARAM_KEYS.get(operation, ()) if isinstance(operation, str) else ()
    return {key: params[key] for key in keys if key in params}


async def project_is_public(user: dict, project_id: str | None) -> bool:
    """Whether the conversation's project is public (False without one).

    Same derivation as ``run_conversation_turn``: the project row's
    immutable ``public`` flag, looked up scoped to the user.
    """
    if not project_id:
        return False
    from db import project_store

    project = await project_store.get_project(user["id"], project_id)
    if project is None:
        # Fail closed: a conversation whose project row is gone (or not the
        # user's) must not be treated as a private conversation.
        raise RuntimeError("The conversation's project was not found.")
    return bool(project.get("public"))


def _require_str(params: dict, key: str) -> str:
    value = params.get(key)
    if value is None:
        raise ValueError(f"Missing required parameter: {key}")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    return value


def _optional_bool(params: dict, key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean.")
    return value


def _optional_str(params: dict, key: str, default: str) -> str:
    value = params.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string.")
    return value


def _check_text_size(value: str, key: str) -> None:
    if len(value.encode("utf-8")) > doc_constants.DOC_MAX_CONTENT_SIZE:
        raise ValueError(
            f"{key} exceeds the maximum doc size of "
            f"{doc_constants.DOC_MAX_CONTENT_SIZE} bytes."
        )


class WriteDocHandler(ActionRequestHandler):
    """Edit / append to / add an image to a shared private doc after approval."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.WRITE_DOC

    @property
    def display_name(self) -> str:
        return "Write Doc"

    @property
    def approve_label(self) -> str:
        return "Apply"

    @property
    def resolved_label(self) -> str:
        # The base property takes no params, so the operation rides on the
        # summary snippet instead: "Applied: #12 Write Doc -- Edit 'Notes'".
        return "Applied"

    def summary_snippet(self, params: dict) -> str:
        operation = params.get("operation")
        verb = _SNIPPET_VERBS.get(operation) if isinstance(operation, str) else None
        title = str(params.get("current_title") or params.get("doc_id") or "")
        if verb is None:
            return f"'{title}'" if title else ""
        return f"{verb} '{title}'" if title else verb

    # ------------------------------------------------------------------
    # Validation (shape only; access + live-body checks are the pre-card's)
    # ------------------------------------------------------------------

    def validate_params(self, params: dict) -> dict:
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        # Drop server-only preview keys a model may echo back (e.g. copied
        # from an earlier card); the pre-card check re-injects them.
        params = {k: v for k, v in params.items() if k not in SERVER_INJECTED_KEYS}
        type_name = self.type_name.value
        reject_unknown_params(type_name, params, _ALL_PARAMS)

        operation = params.get("operation")
        if operation is None:
            raise ValueError("Missing required parameter: operation")
        if not isinstance(operation, str) or operation not in WRITE_OPERATIONS:
            raise ValueError(
                f"Invalid operation {operation!r}. Must be one of: "
                f"{', '.join(WRITE_OPERATIONS)}."
            )
        # Second pass: keys that belong to a different operation.
        reject_unknown_params(
            f"{type_name} operation '{operation}'",
            params,
            {"operation", "doc_id", *OPERATION_PARAM_KEYS[operation]},
        )

        doc_id = _require_str(params, "doc_id").strip()
        if not doc_id:
            raise ValueError("doc_id must be a non-empty string.")
        out: dict = {"operation": operation, "doc_id": doc_id}

        if operation == "edit":
            old_string = _require_str(params, "old_string")
            if not old_string:
                raise ValueError("old_string must be a non-empty string.")
            new_string = params.get("new_string")
            if new_string is None:
                raise ValueError("Missing required parameter: new_string")
            if not isinstance(new_string, str):
                raise ValueError(
                    "new_string must be a string (it may be empty to delete "
                    "the matched text)."
                )
            if old_string == new_string:
                raise ValueError(
                    "old_string and new_string are identical -- nothing to change."
                )
            _check_text_size(new_string, "new_string")
            out["old_string"] = old_string
            out["new_string"] = new_string
            out["replace_all"] = _optional_bool(params, "replace_all", False)
        elif operation == "append":
            content = _require_str(params, "content")
            if not content.strip():
                raise ValueError("content must be a non-empty string.")
            _check_text_size(content, "content")
            out["content"] = content
            out["ensure_blank_line"] = _optional_bool(params, "ensure_blank_line", True)
        else:
            workspace_path = _require_str(params, "workspace_path")
            if not workspace_path.strip():
                raise ValueError("workspace_path must be a non-empty string.")
            placement = _optional_str(params, "placement", "append")
            if placement not in IMAGE_PLACEMENTS:
                raise ValueError(
                    f"Invalid placement {placement!r}. Must be one of: "
                    f"{', '.join(IMAGE_PLACEMENTS)}."
                )
            out["workspace_path"] = workspace_path
            out["alt"] = _optional_str(params, "alt", "")
            out["placement"] = placement
        return out

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        operation = params.get("operation")
        title = params.get("current_title") or params.get("doc_id") or ""
        fields: list[dict] = [{"key": "Doc", "value": str(title)}]
        # doc_mode / doc_scope / share_summary / content_diff / image_preview
        # are server-injected by the pre-card check (doc_precard.py).
        if params.get("doc_mode"):
            fields.append({"key": "Mode", "value": str(params["doc_mode"])})
        if params.get("doc_scope"):
            fields.append({"key": "Scope", "value": str(params["doc_scope"])})
        if params.get("share_summary"):
            fields.append({"key": "Shares", "value": str(params["share_summary"])})

        label = _OPERATION_LABELS.get(operation) if isinstance(operation, str) else None
        if label is None:
            # Unknown / legacy shape: never raise, show what we have.
            if operation is not None:
                fields.append({"key": "Operation", "value": str(operation)})
            return fields
        if operation == "edit" and params.get("replace_all"):
            label = "Edit (replace all)"
        fields.append({"key": "Operation", "value": label})

        if operation == "add_image":
            fields.extend(self._image_fields(params))

        diff_field = _content_diff_field(params.get("content_diff"))
        if diff_field is not None:
            fields.append(diff_field)
        elif operation == "edit":
            # Fallback (no injected diff): the raw replacement.
            fields.append({"key": "Replace", "value": str(params.get("old_string", ""))})
            fields.append({"key": "With", "value": str(params.get("new_string", ""))})
        elif operation == "append":
            fields.append({"key": "Append", "value": str(params.get("content", ""))})
        return fields

    @staticmethod
    def _image_fields(params: dict) -> list[dict]:
        fields: list[dict] = []
        preview = params.get("image_preview")
        if isinstance(preview, dict) and preview.get("asset_name"):
            size = preview.get("size_bytes")
            size_text = f" ({_format_size(size)})" if isinstance(size, int) else ""
            fields.append({
                "key": "Image",
                "value": f"{preview['asset_name']}{size_text}",
                # Structured payload for the Phase 2 card (thumbnail from the
                # conversation workspace). Unknown field types fall back to
                # the plain value string in ActionRequestPreviewFields.
                "type": "doc_image",
                "image": {
                    "workspace_path": str(preview.get("workspace_path", "")),
                    "asset_name": str(preview.get("asset_name", "")),
                    "markdown": str(preview.get("markdown", "")),
                    "size_bytes": size if isinstance(size, int) else None,
                },
            })
        else:
            fields.append({"key": "Image", "value": str(params.get("workspace_path", ""))})
        placement = params.get("placement") or "append"
        fields.append({
            "key": "Placement",
            "value": (
                "Append to the end of the doc"
                if placement == "append"
                else "Store only (not placed in the body)"
            ),
        })
        return fields

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
        request_id: int | None = None,
    ) -> dict:
        operation = params.get("operation")
        doc_id = params.get("doc_id")
        if operation not in WRITE_OPERATIONS or not isinstance(doc_id, str) or not doc_id:
            raise RuntimeError("Malformed write_doc request (operation / doc_id).")

        caller = doc_service.Caller(
            user=user,
            conversation_id=conversation_id,
            project_id=project_id,
            is_public=await project_is_public(user, project_id),
            run_kind="top_level",
        )
        write_source = (
            f"action_request:{request_id}" if request_id is not None else "action_request"
        )
        try:
            # bypass_approval: the user just approved. Hidden / denied
            # verdicts (e.g. the share was revoked meanwhile), a closed gate,
            # an unread doc and a stale old_string still refuse.
            op_params = operation_params(params)
            if operation == "add_image":
                # Server-injected at pre-card time (model copies are
                # stripped by validate_params): the stored asset must be
                # the file the card showed.
                preview = params.get("image_preview")
                if isinstance(preview, dict) and preview.get("sha256"):
                    op_params["expected_sha256"] = preview["sha256"]
            result = await doc_service.apply_write_operation(
                caller,
                doc_id,
                operation,
                op_params,
                write_source=write_source,
                bypass_approval=True,
            )
        except doc_service.DocError as e:
            raise RuntimeError(str(e)) from None

        title = params.get("current_title") or doc_id
        try:
            from db import doc_store

            fresh = await doc_store.get_doc(doc_id, with_shares=False)
            if fresh:
                title = fresh["title"]
        except Exception:
            logger.warning("[write_doc] title lookup failed for %s", doc_id, exc_info=True)
        return {**result, "doc_id": doc_id, "operation": operation, "title": title}


def _content_diff_field(diff) -> dict | None:
    """The edit_skill-style structured diff field, or None without a diff."""
    if not isinstance(diff, dict) or not isinstance(diff.get("lines"), list):
        return None
    return {
        "key": "Content",
        "value": f"+{diff.get('added', 0)} / -{diff.get('removed', 0)} line(s)",
        "type": "skill_content_diff",
        "diff": diff,
    }


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
