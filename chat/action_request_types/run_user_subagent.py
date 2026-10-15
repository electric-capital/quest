"""Handler for run_user_subagent action requests (cross-user subagents).

The caller proposes running a subagent inside ANOTHER user's account. The
card exists so the caller verifies the exact prompt being shared -- the
prompt text crosses a user boundary the moment the run launches, so it is
shown in full on the approval card. Validation fails immediately (no card)
when the target user does not exist or cannot see any of the requested
autoload skills.

On approve, execute() creates the subagent conversation
(``origin="user_subagent"``) in the target's account, a caller-side
``user_subagent`` wait handle, and the ``user_subagent_runs`` row, then
launches the headless run (chat/user_subagent.py). The result tells the
model to block on ``wait_for_handles`` until the target user approves or
denies the subagent's return call.
"""

import logging
from datetime import datetime, timedelta, timezone

from chat.action_request_types._param_validation import reject_unknown_params
from chat.action_request_types.base import ActionRequestHandler
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

_MAX_PROMPT_LEN = 20_000
_MAX_SKILLS = 10

# Caller-side wait-handle backstop: if the target user never resolves the
# return card (or the server restarts mid-run), the expires_at sweep in
# chat/wait_handles/wait_timer.py times the handle out and wakes the caller
# conversation instead of leaving it locked forever. A wait_for_handles
# call on the handle replaces this deadline with its own timeout.
_CALLER_HANDLE_TTL = timedelta(days=14)

# request_type is server-populated context, never model-supplied. The
# target_user_name / skill_names keys are injected by
# validate_against_upstream for preview display and are likewise not
# accepted from the model.
_ALLOWED_PARAMS = frozenset({
    "target_user_email",
    "prompt",
    "skill_ids",
    "model",
})


def _known_models() -> list[str]:
    from chat.llm.config import list_model_specs

    return [spec.id for spec in list_model_specs()]


class RunUserSubagentHandler(ActionRequestHandler):
    """Run a read-only subagent in another user's account."""

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.RUN_USER_SUBAGENT

    @property
    def display_name(self) -> str:
        return "Run Subagent as Another User"

    @property
    def approve_label(self) -> str:
        return "Send"

    @property
    def resolved_label(self) -> str:
        return "Launched"

    def summary_snippet(self, params: dict) -> str:
        email = str(params.get("target_user_email") or "")
        prompt = str(params.get("prompt") or "")
        suffix = f": {prompt}" if prompt else ""
        return f"Subagent for {email}{suffix}"

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(
            self.type_name.value, params, _ALLOWED_PARAMS,
        )

        email = params.get("target_user_email")
        if not isinstance(email, str) or not email.strip():
            raise ValueError("target_user_email is required")
        email = email.strip().lower()
        if "@" not in email:
            raise ValueError(
                f"target_user_email does not look like an email: {email!r}"
            )

        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required and must be a non-empty string")
        prompt = prompt.strip()
        if len(prompt) > _MAX_PROMPT_LEN:
            raise ValueError(
                f"prompt is too long ({len(prompt)} chars; max {_MAX_PROMPT_LEN})"
            )

        raw_skill_ids = params.get("skill_ids", [])
        if raw_skill_ids is None:
            raw_skill_ids = []
        if not isinstance(raw_skill_ids, list):
            raise ValueError("skill_ids must be a list of skill id strings")
        skill_ids: list[str] = []
        for sid in raw_skill_ids:
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError("skill_ids entries must be non-empty strings")
            sid = sid.strip()
            if sid.startswith("system:"):
                raise ValueError(
                    "system:* skills cannot be autoloaded into a user "
                    "subagent; the subagent can load them itself via "
                    "load_skills"
                )
            if sid not in skill_ids:
                skill_ids.append(sid)
        if len(skill_ids) > _MAX_SKILLS:
            raise ValueError(
                f"skill_ids accepts at most {_MAX_SKILLS} skills"
            )

        model = params.get("model")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be a non-empty string when provided")
            model = model.strip()
            from chat.llm.config import resolve_model

            if resolve_model(model) is None:
                raise ValueError(
                    f"Unknown model: {model!r}. Valid models: "
                    f"{sorted(_known_models())}"
                )

        validated: dict = {
            "target_user_email": email,
            "prompt": prompt,
            "skill_ids": skill_ids,
        }
        if model:
            validated["model"] = model
        return validated

    @staticmethod
    def _require_feature_enabled() -> None:
        """Raise unless the server-global cross-user subagent gate is open.

        The conversation loop's dispatch arm already blocks proposals when
        the gate is closed; this re-check covers cards approved after an
        admin turns the feature off (and belt-and-braces the proposal path).
        """
        from config.feature_gates import FEATURE_USER_SUBAGENTS, is_feature_enabled

        if not is_feature_enabled(FEATURE_USER_SUBAGENTS):
            raise ValueError(
                "Cross-user subagents are disabled server-wide. An admin "
                "can enable the feature in Settings > Features."
            )

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Resolve the target user and verify skill visibility.

        Failing either check raises ValueError so the model gets an
        immediate ``Invalid parameters`` result and no card is shown --
        the caller never sees a card for a run that cannot launch.
        """
        self._require_feature_enabled()
        target, skills = await self._resolve_target_and_skills(params)
        params["target_user_name"] = target.get("name") or ""
        params["skill_names"] = [s["name"] for s in skills]
        return params

    @staticmethod
    async def _resolve_target_and_skills(params: dict) -> tuple[dict, list[dict]]:
        from db.skill_store import (
            get_accessible_skills_by_ids,
            user_can_access_skill,
        )
        from db.user_store import get_user_by_email

        email = params["target_user_email"]
        target = await get_user_by_email(email)
        if target is None:
            raise ValueError(f"No Quest user exists with email {email!r}")

        skill_ids = params.get("skill_ids") or []
        invisible = []
        for sid in skill_ids:
            if not await user_can_access_skill(target["id"], sid):
                invisible.append(sid)
        if invisible:
            raise ValueError(
                f"The target user ({email}) cannot access these skills: "
                f"{invisible}. Share the skills with them first, or drop "
                "them from skill_ids."
            )
        skills = await get_accessible_skills_by_ids(target["id"], skill_ids)
        if len(skills) != len(skill_ids):
            found = {s["id"] for s in skills}
            missing = [sid for sid in skill_ids if sid not in found]
            raise ValueError(
                f"These skills do not exist or are not accessible to the "
                f"target user ({email}): {missing}"
            )
        return target, skills

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        target_name = params.get("target_user_name") or ""
        target_email = params.get("target_user_email", "")
        target_display = (
            f"{target_name} ({target_email})" if target_name else target_email
        )
        skill_names = params.get("skill_names") or []
        fields = [
            {"key": "Target User", "value": target_display},
            {"key": "Prompt", "value": params.get("prompt", "")},
            {
                "key": "Autoload Skills",
                "value": ", ".join(skill_names) if skill_names else "None",
            },
        ]
        if params.get("model"):
            fields.append({"key": "Model", "value": params["model"]})
        return fields

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Create the subagent conversation + run and launch it.

        Re-runs the target/skill checks first (the card may have sat open
        while shares changed -- TOCTOU close, mirroring the skill and
        spreadsheet handlers).
        """
        from chat import user_subagent
        from chat.realtime import bus, events as realtime_events
        from chat.storage import ChatStorage
        from db import tool_wait_handle_store
        from db.user_subagent_run_store import create_run

        if not conversation_id:
            raise ValueError(
                "run_user_subagent requires a conversation context"
            )

        self._require_feature_enabled()
        target, _skills = await self._resolve_target_and_skills(params)

        caller_name = user.get("name") or user.get("email", "")
        subagent_conversation_id = await ChatStorage.create_user_subagent_conversation(
            user_id=target["id"],
            model=params.get("model"),
            custom_name=f"Subagent for {caller_name}",
        )

        wait_handle = await tool_wait_handle_store.create_handle(
            user_id=user["id"],
            conversation_id=conversation_id,
            kind="user_subagent",
            tool_id="",
            payload={
                "subagent_conversation_id": subagent_conversation_id,
                "target_user_email": params["target_user_email"],
            },
            expires_at=datetime.now(timezone.utc) + _CALLER_HANDLE_TTL,
        )

        run = await create_run(
            caller_user_id=user["id"],
            target_user_id=target["id"],
            caller_conversation_id=conversation_id,
            subagent_conversation_id=subagent_conversation_id,
            wait_handle_id=wait_handle["id"],
            prompt=params["prompt"],
            skill_ids=params.get("skill_ids") or [],
        )

        # Seed the approved prompt as the subagent conversation's first
        # user message so the target user sees exactly what was shared
        # even if the run dies before its first flush.
        await ChatStorage.append_message(
            subagent_conversation_id, "user", params["prompt"],
        )

        # Surface the new conversation in the target user's sidebar.
        try:
            bus.publish_to_user(
                target["id"],
                realtime_events.make_conversation_list_changed(
                    subagent_conversation_id, "created",
                ),
            )
        except Exception:
            logger.debug(
                "[run_user_subagent] publish conversation_list_changed "
                "failed", exc_info=True,
            )

        user_subagent.start_run(run)

        logger.info(
            "[run_user_subagent] Launched run %s (caller=%s, target=%s, "
            "subagent_conversation=%s)",
            run["id"], user.get("email"), params["target_user_email"],
            subagent_conversation_id,
        )

        return {
            "success": True,
            "status": "launched",
            "run_id": run["id"],
            "target_user_email": params["target_user_email"],
            "subagent_conversation_id": subagent_conversation_id,
            "wait_handle_id": wait_handle["id"],
            "next_step": (
                "The subagent is now running in the target user's account. "
                f"Call wait_for_handles(handle_ids=[\"{wait_handle['id']}\"], "
                "reason=\"Waiting for the subagent response\") to block "
                "until the target user approves or denies the subagent's "
                "return call. The resolved handle's response carries "
                "status \"returned\" (with the response text and any "
                "returned file paths under .subagent_responses/), "
                "\"denied\", or \"failed\"."
            ),
        }
