"""Base class for action request type handlers."""

from abc import ABC, abstractmethod

from db.models import ActionRequestType


class ActionRequestHandler(ABC):
    """Base class for action request type handlers."""

    @property
    @abstractmethod
    def type_name(self) -> "ActionRequestType | str":
        """The request type string for this handler.

        Core handlers return a db.models.ActionRequestType member; plugin
        handlers return a plain ``<plugin id>_``-prefixed string. Both are
        strings (ActionRequestType is a StrEnum) and the registry keys by
        the string value.
        """
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for UI display (e.g., 'Send Slack DM')."""
        ...

    @property
    def preview_fields(self) -> list[str]:
        """Parameter keys to highlight in the inline preview UI."""
        return []

    @property
    def approve_label(self) -> str:
        """Label for the approve/execute button (e.g., 'Send' or 'Approve')."""
        return "Approve"

    @property
    def resolved_label(self) -> str:
        """Status label for the collapsed card after execution.

        Shown as "<resolved_label>: <display_name> -- <summary_snippet>"
        once the request is approved and executed (denied cards always say
        "Denied", stopped cards "Stopped"). Override per handler, e.g. 'Created' / 'Saved' /
        'Uploaded'.
        """
        return "Sent"

    def summary_snippet(self, params: dict) -> str:
        """One-line params summary for the collapsed (resolved) card.

        Returns an empty string when the type name alone is summary enough.
        Callers truncate; keep it short and never raise on odd params (old
        chat history may carry legacy shapes).
        """
        return ""

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        """Render a structured preview as a list of {key, value} dicts.

        Subclasses should override this to provide type-specific previews.
        The default implementation uses preview_fields to extract values.

        Args:
            params: Validated parameter dict for the action request.
            user: Authenticated user dict, passed through for handlers that
                  need to resolve external data (e.g., upstream entity names).
        """
        fields = []
        for key in self.preview_fields:
            value = params.get(key)
            if value is not None:
                fields.append({"key": key, "value": str(value)})
        return fields

    @abstractmethod
    def validate_params(self, params: dict) -> dict:
        """Validate and normalize parameters.

        Args:
            params: Raw parameter dict from the LLM.

        Returns:
            Normalized parameter dict.

        Raises:
            ValueError: If parameters are invalid.
        """
        ...

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Optional second-stage validation against an upstream service.

        Runs after ``validate_params`` succeeds and BEFORE any
        ``action_requests`` row, wait handle, or event is written.
        Handlers whose upstream offers a dry-run endpoint override this
        to POST the same body that
        ``execute`` would send so FK / enum / cross-field rejections
        become same-turn ``Invalid parameters`` envelopes instead of
        post-Approve failures.

        Default no-op returns ``params`` unchanged. Override to raise
        ``ValueError`` on a rejection; raising any other exception is a
        bug and will leak as ``Invalid parameters`` to the model.
        """
        return params

    async def enrich_params_for_preview(self, params: dict, user: dict) -> None:
        """Optional server-side preview enrichment, mutating ``params``.

        Runs after validation succeeds, right before the approval card is
        written, so handlers can resolve human-readable names (channel
        names, entity names, ...) into server-injected params fields that
        ``render_preview`` displays. Must never raise on resolution
        failure -- the card simply renders without the enrichment.

        Default no-op. Core types still enriched by the legacy ladder in
        chat/gemini_api/turn_tools.py get their arm there; new handlers
        (and plugins) override this instead.
        """
        return None

    def selectable_items(self, params: dict) -> int:
        """How many independently skippable items this request has.

        0 (the default) means the request is all-or-nothing. A handler
        whose card lists separate items the user can untick (write_doc's
        ``multi_edit``: one per edit) returns their count; the resolve
        route then accepts ``skipped_items`` on Approve, checks the
        positions against this count (at least one item must stay), and
        passes them to :meth:`execute` as ``skipped_items`` -- a list of
        ``{"index": <0-based position>, "reason": <str>}`` the handler
        leaves out of the write and reports back in its result.
        """
        return 0

    @abstractmethod
    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Execute the action.

        A handler with :meth:`selectable_items` also takes the keyword
        ``skipped_items`` (only passed when the user unticked something).

        Args:
            params: Validated parameter dict.
            user: Authenticated user dict.
            conversation_id: Optional conversation UUID. Most handlers
                ignore this; the IO create/edit handlers use it to
                resolve the workspace directory for file uploads.
            project_id: Optional project UUID of the conversation's
                project. Most handlers ignore this, but the skill
                handlers (create_skill / edit_skill) gate project-skill
                writes on it and fail when it is omitted -- callers
                must resolve it from the conversation row, not rely on
                handlers deriving it from ``conversation_id``.

        Returns:
            Result dict with execution details.

        Raises:
            Exception: If execution fails.
        """
        ...
