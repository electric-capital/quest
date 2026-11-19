"""RenameAutoProjectHandler -- approve-to-rename action request for an AutoProject.

A run names its AutoProject once without asking: ``complete_autoproject_run``'s
``autoproject_title`` applies while the title is still the default one cut
from the first prompt. Every later change of the title by the model goes
through this card: ``create_action_request(request_type="rename_autoproject",
params={"title": "..."}, ...)``, issued by a run for its own AutoProject.
The AutoProject is never a parameter; it is the proposing run's.

``rename_autoproject_precard_check`` runs in the ``create_action_request``
dispatch arm before any card: it refuses the request outside a run, while
the title is still the default (no approval is needed then) and when the
title would not change, and snapshots the current title into the
server-only ``current_title`` param the card previews.
"""

import logging

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from db.models import ActionRequestType

logger = logging.getLogger(__name__)


_ALLOWED_PARAMS = frozenset({"title"})

_NOT_A_RUN_ERROR = (
    "rename_autoproject is only available in an AutoProject run: it renames "
    "the run's own AutoProject."
)


def _normalize_title(raw) -> str:
    """The title as the store will save it. Raises ValueError (worded for
    the model) for a missing, empty or over-long one."""
    from db.autoproject_store import AutoProjectValidationError, normalize_title

    if raw is None:
        raise ValueError("Missing required parameter: title (the new AutoProject title)")
    try:
        return normalize_title(raw)
    except AutoProjectValidationError as exc:
        raise ValueError(str(exc)) from None


async def _load_autoproject(user: dict, autoproject_id: str | None) -> dict:
    """The proposing run's AutoProject, which must be the user's and reachable
    through the gate. Raises ValueError otherwise."""
    if not autoproject_id:
        raise ValueError(_NOT_A_RUN_ERROR)
    from chat.autoprojects.service import require_owned_autoproject
    from db.autoproject_store import AutoProjectStoreError

    try:
        return await require_owned_autoproject(user, autoproject_id)
    except AutoProjectStoreError as exc:
        raise ValueError(str(exc)) from None


async def rename_autoproject_precard_check(
    validated_params: dict, user: dict, autoproject_id: str | None,
) -> None:
    """Check the rename before the card is written.

    Injects the server-only ``current_title`` so the card shows what the
    title would change from. Raises ValueError -- surfaced as ``Invalid
    parameters`` by the dispatch arm -- outside a run, for an AutoProject
    that is not the user's, while the title is still the default one, and
    when the new title equals the current one.

    Args:
        validated_params: The handler's validated params (mutated).
        user: The acting user.
        autoproject_id: The proposing run's AutoProject
            (``ToolContext.autoproject_id``), None outside a run.
    """
    autoproject = await _load_autoproject(user, autoproject_id)
    if autoproject["title_is_default"]:
        raise ValueError(
            "This AutoProject still has its default title, so naming it "
            "needs no approval: pass the title as autoproject_title to "
            "complete_autoproject_run when you finish the run."
        )
    if autoproject["title"] == validated_params["title"]:
        raise ValueError(
            f"This AutoProject is already titled {autoproject['title']!r}."
        )
    validated_params["current_title"] = autoproject["title"]


class RenameAutoProjectHandler(ActionRequestHandler):
    """Rename the proposing run's AutoProject after explicit user approval.

    Params:
        title (str): The new title (whitespace collapsed, at most
            ``AUTOPROJECT_TITLE_MAX_CHARS`` characters).

    ``current_title`` is server-injected by the pre-card check for the
    preview and never accepted from the model.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.RENAME_AUTOPROJECT

    @property
    def display_name(self) -> str:
        return "Rename AutoProject"

    @property
    def approve_label(self) -> str:
        return "Rename"

    @property
    def resolved_label(self) -> str:
        return "Renamed"

    @property
    def preview_fields(self) -> list[str]:
        return ["current_title", "title"]

    def summary_snippet(self, params: dict) -> str:
        title = params.get("title")
        return title if isinstance(title, str) else ""

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        fields: list[dict] = []
        current = params.get("current_title")
        if current:
            fields.append({"key": "Current title", "value": str(current)})
        title = params.get("title")
        if title:
            fields.append({"key": "New title", "value": str(title)})
        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)
        return {"title": _normalize_title(params.get("title"))}

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        from chat.autoprojects import service
        from db import conversation_store

        # The AutoProject is the proposing run's, read from the conversation
        # row; the gate and ownership are re-checked at approve time.
        autoproject_id = await conversation_store.get_conversation_autoproject_id(
            conversation_id,
        )
        before = await _load_autoproject(user, autoproject_id)
        title = _normalize_title(params.get("title"))
        # The user-side rename: it also clears title_is_default and
        # publishes the AutoProject events.
        autoproject = await service.update_autoproject(user, autoproject_id, title=title)

        logger.info(
            "[autoprojects] Renamed AutoProject %s for user_id %s via action request",
            autoproject_id, user["id"],
        )
        return {
            "success": True,
            "title": autoproject["title"],
            "previous_title": before["title"],
        }
