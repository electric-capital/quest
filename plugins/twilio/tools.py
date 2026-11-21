"""Twilio dynamic tools (plugin module).

- ``twilio_list_sms_templates`` -- the user's pre-written messages plus
  whether free-form texts to themselves are allowed right now.
- ``twilio_send_self_sms`` -- text the user's own VERIFIED number with no
  approval card. What it may send is governed by the admin's
  trusted-channel switch (plugins/twilio/upstream.py
  ``is_trusted_channel``): trusted -> free-form ``body`` or a template;
  untrusted -> a ``template`` name only, sent verbatim.

Texts to anyone else always go through the approval-gated
``twilio_send_sms`` action request (plugins/twilio/handlers.py).
"""

from __future__ import annotations

import json
import logging

from config.plugin_tool_helpers import arg_str as _arg_str
from config.plugin_types import PluginTool

from plugins.twilio.templates import find_template, get_configured_templates
from plugins.twilio.upstream import (
    MAX_SMS_BODY_LENGTH,
    MISSING_CREDENTIALS_ERROR,
    TwilioError,
    get_user_phone_number,
    is_trusted_channel,
    load_twilio_config,
    mask_phone_number,
    send_sms,
)

logger = logging.getLogger(__name__)


def _load_config_or_error() -> tuple[dict | None, str | None]:
    """The admin config, or a JSON error string when unconfigured."""
    from fastapi import HTTPException

    try:
        return load_twilio_config(), None
    except HTTPException as exc:
        return None, json.dumps({"error": "twilio_not_configured", "message": str(exc.detail)})


def _template_names(templates: list[dict]) -> list[str]:
    return [t["name"] for t in templates]


# ---------------------------------------------------------------------------
# Handlers ((ctx, args) shape -- see config/plugin_types.py PluginTool)
# ---------------------------------------------------------------------------

async def _tool_list_sms_templates(ctx, args: dict) -> str:
    config, error = _load_config_or_error()
    if error:
        return error
    templates = get_configured_templates(ctx.user.get("settings"))
    trusted = is_trusted_channel(config)
    phone = get_user_phone_number(ctx.user)
    return json.dumps({
        "connected": phone is not None,
        "phone_number_masked": mask_phone_number(phone),
        "trusted_channel": trusted,
        "free_form_allowed": trusted,
        "templates": templates,
        "note": (
            "Free-form text is allowed: pass `body` (or a `template` name) to "
            "twilio_send_self_sms."
            if trusted else
            "SMS is not a trusted channel: twilio_send_self_sms only accepts a "
            "`template` name from this list, sent verbatim. The user adds "
            "messages in Settings > SMS Messages."
        ),
    })


async def _tool_send_self_sms(ctx, args: dict) -> str:
    phone = get_user_phone_number(ctx.user)
    if not phone:
        return json.dumps({"error": "twilio_not_connected", "message": MISSING_CREDENTIALS_ERROR})

    config, error = _load_config_or_error()
    if error:
        return error

    body = _arg_str(args.get("body"))
    template_name = _arg_str(args.get("template"))
    if body and template_name:
        return json.dumps({
            "error": "invalid_arguments",
            "message": "Pass either `body` or `template`, not both.",
        })
    if not body and not template_name:
        return json.dumps({
            "error": "invalid_arguments",
            "message": "Pass `body` (free-form text) or `template` (a pre-written message name).",
        })

    templates = get_configured_templates(ctx.user.get("settings"))
    trusted = is_trusted_channel(config)

    if template_name:
        template = find_template(templates, template_name)
        if template is None:
            return json.dumps({
                "error": "unknown_template",
                "message": f"No pre-written message named {template_name!r}.",
                "available_templates": _template_names(templates),
            })
        text = template["body"]
        sent_as = {"template": template["name"]}
    else:
        if not trusted:
            return json.dumps({
                "error": "untrusted_channel",
                "message": (
                    "SMS is not configured as a trusted channel, so free-form "
                    "text cannot be sent to the user's phone. Choose one of the "
                    "user's pre-written messages by name via `template` "
                    "(twilio_list_sms_templates lists them)."
                    + ("" if templates else " The user has not written any yet "
                       "(Settings > SMS Messages).")
                ),
                "available_templates": _template_names(templates),
            })
        assert body is not None
        if len(body) > MAX_SMS_BODY_LENGTH:
            return json.dumps({
                "error": "body_too_long",
                "message": f"SMS body must be at most {MAX_SMS_BODY_LENGTH} characters.",
            })
        text = body
        sent_as = {"free_form": True}

    try:
        result = await send_sms(phone, text, config)
    except TwilioError as exc:
        return json.dumps({"error": "sms_send_failed", "message": str(exc)})

    logger.info(
        "[twilio_send_self_sms] sent (user=%s, %s)",
        ctx.user.get("email"), "template" if template_name else "free-form",
    )
    return json.dumps({
        "success": True,
        "to": mask_phone_number(phone),
        "message_sid": result.get("sid"),
        "status": result.get("status"),
        "body": text,
        **sent_as,
    })


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------

LIST_SMS_TEMPLATES_TOOL = PluginTool(
    spec={
        "name": "twilio_list_sms_templates",
        "description": (
            "List the user's pre-written SMS messages (name + body) and "
            "whether free-form texts to their own phone are currently "
            "allowed (the admin's trusted-channel switch). Call this before "
            "twilio_send_self_sms when SMS is not a trusted channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Check SMS templates'."
                    ),
                },
            },
            "required": [],
        },
    },
    handler=_tool_list_sms_templates,
    requires_service="twilio",
)

SEND_SELF_SMS_TOOL = PluginTool(
    spec={
        "name": "twilio_send_self_sms",
        "description": (
            "Text the user's own verified mobile number -- no approval "
            "needed. The right choice for 'text me when...' reminders, "
            "alerts, and scheduled routines. Pass EITHER `body` (free-form "
            "text; only allowed when the admin marked SMS as a trusted "
            "channel) OR `template` (the name of one of the user's "
            "pre-written messages, always allowed, sent verbatim). When SMS "
            "is not a trusted channel a `body` is refused -- use "
            "twilio_list_sms_templates to pick a template. Texts to anyone "
            "else must use the twilio_send_sms action request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "body": {
                    "type": "string",
                    "description": (
                        f"Free-form message text (max {MAX_SMS_BODY_LENGTH} "
                        "characters, plain text). Trusted channel only."
                    ),
                },
                "template": {
                    "type": "string",
                    "description": (
                        "Name of one of the user's pre-written messages "
                        "(see twilio_list_sms_templates). Sent verbatim."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Text reminder to user'."
                    ),
                },
            },
            "required": [],
        },
    },
    handler=_tool_send_self_sms,
    requires_service="twilio",
    mutating=True,
)

TWILIO_TOOLS: tuple[PluginTool, ...] = (LIST_SMS_TEMPLATES_TOOL, SEND_SELF_SMS_TOOL)
