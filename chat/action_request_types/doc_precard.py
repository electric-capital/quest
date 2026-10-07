"""Pre-card check + preview enrichment for the ``write_doc`` action request.

Runs in the ``create_action_request`` dispatch arm (chat/gemini_api/
turn_tools.py, after ``validate_params``, before the request row is
written), like ``skill_precard_check`` / ``routine_precard_check``. It
delegates every decision to ``chat.docs.service.preview_write_operation``
(the access rule, edit's read-before-edit, the live-body dry run, image and
size caps) and only interprets the verdict:

- ``free`` -> rejected: the doc is writable directly, so the model should
  call the write tool instead of opening a card.
- ``denied`` / hidden / missing / unread / stale ``old_string`` / gate
  closed -> the service's own message, verbatim (a hidden doc keeps the
  byte-equal ``doc_not_found_message`` text).
- ``approval`` -> accepted; the server-only preview keys are injected into
  ``validated_params`` (never accepted from the model, see
  ``write_doc.SERVER_INJECTED_KEYS``): ``current_title``, ``doc_mode``,
  ``doc_scope``, ``share_summary``, ``content_diff`` (the edit_skill line
  diff of the live body vs the result; for ``add_image`` only when the body
  changes) and, for ``add_image``, ``image_preview``.

Rejections raise ``ValueError`` so the dispatch arm reuses its ``Invalid
parameters`` early return -- same turn, no card. ``WriteDocHandler.execute``
re-runs the whole check at approve time (TOCTOU close).
"""

import asyncio

from chat.action_request_types._skill_content_edit import build_bounded_content_diff
from chat.action_request_types.write_doc import (
    OPERATION_TOOLS,
    operation_params,
    project_is_public,
)
from chat.docs import service as doc_service


def share_summary(shares: list) -> str:
    """``"shared with everyone"`` / ``"shared with N user(s)"`` / both.

    Counts only; never names a recipient. Empty for an unshared doc.
    """
    everyone = any(share.get("user_id") is None for share in shares)
    users = sum(1 for share in shares if share.get("user_id") is not None)
    user_text = f"{users} user" if users == 1 else f"{users} users"
    if everyone and users:
        return f"shared with everyone and {user_text}"
    if everyone:
        return "shared with everyone"
    if users:
        return f"shared with {user_text}"
    return ""


async def doc_precard_check(
    validated_params: dict,
    user: dict,
    project_id: str | None,
    conversation_id: str | None,
) -> None:
    """Verify a ``write_doc`` proposal and enrich ``validated_params``.

    Raises:
        ValueError: the doc is writable directly (``free``), or the service
            refused (hidden/missing doc, denied write, unread doc for edit,
            failed match, caps, gate closed) -- surfaced as ``Invalid
            parameters`` by the dispatch arm.
    """
    operation = validated_params["operation"]
    doc_id = validated_params["doc_id"]
    try:
        is_public = await project_is_public(user, project_id)
    except RuntimeError as e:
        # Missing / foreign project row: refuse same-turn, not run-fatal.
        raise ValueError(str(e)) from None
    caller = doc_service.Caller(
        user=user,
        conversation_id=conversation_id,
        project_id=project_id,
        is_public=is_public,
        run_kind="top_level",
    )
    try:
        preview = await doc_service.preview_write_operation(
            caller, doc_id, operation, operation_params(validated_params),
        )
    except doc_service.DocError as e:
        # Includes DocDisabled; the text is the tool's own error.
        raise ValueError(str(e)) from None

    access = preview["access"]
    if access.write == "free":
        raise ValueError(
            "This doc is writable directly; call the "
            f"{OPERATION_TOOLS[operation]} tool instead of proposing an "
            "action request."
        )
    if access.write != "approval":
        # preview_write_operation already raises for denied verdicts; keep a
        # fail-closed guard in case that ever changes.
        raise ValueError(access.deny_reason or "This doc cannot be written here.")

    doc = preview["doc"]
    validated_params["current_title"] = doc["title"]
    validated_params["doc_mode"] = doc["mode"]
    validated_params["doc_scope"] = "project" if doc.get("project_id") else "user"
    validated_params["share_summary"] = share_summary(doc.get("shares") or [])
    validated_params["require_approval"] = bool(doc.get("require_approval", False))

    current_body = preview["current_body"]
    new_body = preview["new_body"]
    if operation != "add_image" or new_body != current_body:
        # Bounded (prefix/suffix-trimmed, line-capped) and off the event
        # loop: a doc body is up to 1 MB, and the full-body SequenceMatcher
        # diff edit_skill uses is quadratic on it.
        validated_params["content_diff"] = await asyncio.to_thread(
            build_bounded_content_diff, current_body, new_body,
        )
    if operation == "add_image":
        validated_params["image_preview"] = {
            "workspace_path": validated_params["workspace_path"],
            "asset_name": preview["asset_name_preview"],
            "markdown": preview["markdown"],
            "size_bytes": preview["image_bytes_size"],
            # Pins the file the card showed: execute refuses a swapped file.
            "sha256": preview["image_sha256"],
        }
