"""Action request type registry and handlers.

This package provides the handler registry and all built-in action request
type handlers. All symbols that were previously importable from the monolithic
``chat.action_request_types`` module are re-exported here for backward
compatibility.
"""

# Base class
from chat.action_request_types.base import ActionRequestHandler

# Registry functions
from chat.action_request_types.registry import (
    register_handler,
    get_handler,
    get_all_type_names,
    get_preview_for_request,
    get_summary_snippet,
)

# Resolver functions used by external code (chat/gemini_api/turn_tools.py)
from chat.action_request_types.create_calendar_invite import _resolve_calendar_name

# Handler classes (imported to make them available and for registration below).
# Plugin-provided handlers (e.g. the Slack plugin's send_slack_message /
# send_slack_dm in plugins/slack/handlers.py, the Twitter/X plugin's
# send_twitter_dm in plugins/twitter/handlers.py, the Telegram plugin's
# send_telegram_message in plugins/telegram/handlers.py, or handlers from
# external plugin roots on QUEST_PLUGIN_PATH) are registered by the plugin
# loader instead.
from chat.action_request_types.create_calendar_invite import CreateCalendarInviteHandler
from chat.action_request_types.edit_calendar_event import EditCalendarEventHandler
from chat.action_request_types.create_memory import CreateMemoryHandler
from chat.action_request_types.upload_to_drive import UploadToDriveHandler
from chat.action_request_types.create_drive_folder import CreateDriveFolderHandler
from chat.action_request_types.create_skill import CreateSkillHandler
from chat.action_request_types.edit_skill import EditSkillHandler
from chat.action_request_types.create_routine import CreateRoutineHandler
from chat.action_request_types.edit_routine import EditRoutineHandler
from chat.action_request_types.edit_google_spreadsheet import EditGoogleSpreadsheetHandler
from chat.action_request_types.reset_gcp_instance import ResetGcpInstanceHandler
from chat.action_request_types.run_user_subagent import RunUserSubagentHandler
from chat.action_request_types.subagent_return import SubagentReturnHandler
from chat.action_request_types.write_doc import WriteDocHandler

# Register built-in handlers
register_handler(CreateCalendarInviteHandler())
register_handler(EditCalendarEventHandler())
register_handler(CreateMemoryHandler())
register_handler(UploadToDriveHandler())
register_handler(CreateDriveFolderHandler())
register_handler(CreateSkillHandler())
register_handler(EditSkillHandler())
register_handler(CreateRoutineHandler())
register_handler(EditRoutineHandler())
register_handler(EditGoogleSpreadsheetHandler())
register_handler(ResetGcpInstanceHandler())
register_handler(RunUserSubagentHandler())
register_handler(SubagentReturnHandler())
register_handler(WriteDocHandler())
