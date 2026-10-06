"""Canonical tool definitions in JSON Schema format.

Defines all tools available to the LLM in a provider-agnostic format.
Each provider converts these to its own native format using the converter
functions at the bottom of this file.

Tool tiers:
- BASE_TOOLS: Available to both top-level agents and sub-agents
- TOP_LEVEL_TOOLS: BASE_TOOLS + agent spawning + create_action_request
- SUB_AGENT_TOOLS: BASE_TOOLS + agent_task_response
"""

import copy

from chat.docs.constants import DOCS_SERVICE_KEY
from chat.llm.base import ToolSpec
from db.models import ActionRequestType

# Maximum number of parallel sub-agent tasks.
# Duplicated from chat.gemini_api.constants to avoid circular imports
# (tool_schemas -> constants -> gemini_api.__init__ -> conversation -> session -> tool_schemas).
MAX_PARALLEL_TASKS = 10
MAX_PARALLEL_TEMPLATE_TASKS = 20

TEMPLATE_BATCH_ALLOWED_MODELS = {
    "claude-haiku-4.5",
    "claude-sonnet-4-6",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
}

# Models a 2nd-level (nested) sub-agent may use. Duplicated from
# chat.gemini_api.constants.NESTED_SUB_AGENT_ALLOWED_MODELS to avoid the same
# circular import as the constants above; keep the two in sync.
NESTED_SUB_AGENT_ALLOWED_MODELS = {
    "claude-haiku-4.5",
    "gemini-3.5-flash-lite",
}


# ---------------------------------------------------------------------------
# Dynamic tool dispatch via tool_call
# ---------------------------------------------------------------------------

TOOL_CALL_SPEC: ToolSpec = {
    "name": "tool_call",
    "description": (
        "Execute a tool by name. Consult the 'Dynamic Tools' section in your "
        "system instructions for available tool names, their parameters, and "
        "usage guidance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": (
                    "The name of the tool to execute. See the 'Dynamic Tools' "
                    "section in system instructions for the list of available tools."
                ),
            },
            "arguments": {
                "type": "object",
                "description": (
                    "A JSON object of arguments for the tool. Keys and values "
                    "depend on the tool being called. See system instructions "
                    "for each tool's parameters."
                ),
            },
            "intent_message": {
                "type": "string",
                "description": (
                    "A brief, user-friendly summary of your intent "
                    "(max 50 characters)."
                ),
            },
        },
        "required": ["tool_name", "intent_message"],
    },
}

# Attachment list schema shared by create_gmail_draft and send_gmail_to_self.
_GMAIL_ATTACHMENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["drive", "workspace", "gmail"],
                "description": (
                    "'drive' for Google Drive files, "
                    "'workspace' for conversation workspace "
                    "files, 'gmail' for attachments from an "
                    "existing Gmail message."
                ),
            },
            "drive_file_id": {
                "type": "string",
                "description": "Google Drive file ID (required when type='drive').",
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Workspace-relative path (required when "
                    "type='workspace'); same paths as "
                    "list_workspace_files."
                ),
            },
            "message_id": {
                "type": "string",
                "description": "Gmail message ID containing the attachment (required when type='gmail').",
            },
            "attachment_id": {
                "type": "string",
                "description": "Gmail attachmentId from the message's attachments list (required when type='gmail').",
            },
            "filename": {
                "type": "string",
                "description": "Optional override for the attachment filename.",
            },
        },
        "required": ["type"],
    },
    "description": (
        "Files to attach to the email. Each attachment gets "
        "a Content-ID equal to its filename (non "
        "[A-Za-z0-9._-] runs become '_'); embed an attached "
        "image in body_md with ![caption](cid:<filename>)."
    ),
}

# Registry of tools routed through tool_call. Maps tool name to its full
# ToolSpec.  Used by the system prompt builder (to generate the "Dynamic
# Tools" documentation section), by the dispatch layer (to validate that a
# requested tool_name is valid), and as the single source of truth for which
# tools are dispatched via tool_call.
#
# A spec may carry ``"mutating": True``: the tool changes state outside the
# conversation's own workspace WITHOUT an approval card (Gmail drafts/sends/
# label changes; plugin self-DM / self-SMS / mailbox tools; the Quest Docs
# writes create_doc / edit_doc / append_to_doc / add_doc_image, whose
# approval-gated cases refuse with approval_required instead of opening a
# card). Everything else is a read or a workspace-local write. The
# classification feeds mutating_tool_call_tools(), which one-shot inference
# API runs (origin="inference_api") are refused at dispatch time -- see
# INFERENCE_API_TOOLS below. Every new tool must be classified: the roster
# is pinned by tests/test_inference_api.py.
#
# A spec may also carry ``"requires_service": "<key>"``: the tool is left
# out of the prompt's Dynamic Tools section unless that connected-services
# key is truthy (plugin services, and the "docs" pseudo-key that mirrors
# the per-user Quest Docs feature gate).
TOOL_CALL_REGISTRY: dict[str, ToolSpec] = {
    "get_current_time": {
        "name": "get_current_time",
        "description": (
            "Get the current date and time. Returns both UTC/Unix time and "
            "the time in the user's local timezone. Use this when the user "
            "asks about the current time, date, or when you need to reason "
            "about time-sensitive information (e.g., 'emails from today', "
            "'schedule for tomorrow')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent for checking the time (max 50 characters). Example: 'Check current time'.",
                },
            },
            "required": [],
        },
    },
    # The nine Slack tools (find_slack_channel, list_slack_teams, ...,
    # send_slack_dm_to_self) are registered by the in-tree Slack plugin
    # (plugins/slack) via register_tool_call_tool().
    # The four Telegram read tools (telegram_get_me, telegram_list_dialogs,
    # telegram_get_messages, telegram_list_contacts) are registered by the
    # in-tree Telegram plugin (plugins/telegram) via register_tool_call_tool().
    "list_workspace_files": {
        "name": "list_workspace_files",
        "description": (
            "List all files in the conversation workspace directory. "
            "Returns a JSON array of relative file paths with sizes. "
            "The workspace contains files uploaded by the user for this conversation. "
            "Use this to discover what files are available before reading them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'List uploaded files'.",
                },
            },
            "required": [],
        },
    },
    "get_workspace_file": {
        "name": "get_workspace_file",
        "description": (
            "Retrieve a file from the conversation workspace. "
            "For small text files (under 100KB), returns the file contents directly. "
            "For large or binary files, makes the file available for you to analyze "
            "directly in this response. "
            "Very large files (over the per-model attachment limit) cannot be "
            "attached and return an error suggesting how to split or reduce them "
            "with run_python first. "
            "Use list_workspace_files first to see available files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path within the workspace (e.g. 'report.pdf', 'data/input.csv'). Use paths from list_workspace_files.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Read the CSV file'.",
                },
            },
            "required": ["path"],
        },
    },
    "write_workspace_file": {
        "name": "write_workspace_file",
        "description": (
            "Write or create a file in the conversation workspace. "
            "If the file already exists, it will be overwritten. "
            "Parent directories are created automatically. "
            "Use this to save code, text, reports, data files, or any other "
            "text-based output that the user might want to download or reference later. "
            "Maximum file size is 1MB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path within the workspace (e.g. 'output.txt', 'src/main.py', 'data/results.csv'). Parent directories are created automatically.",
                },
                "content": {
                    "type": "string",
                    "description": "The full file content to write.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Write Python script', 'Save analysis results'.",
                },
            },
            "required": ["path", "content"],
        },
    },
    "edit_workspace_file": {
        "name": "edit_workspace_file",
        "description": (
            "Perform an exact string replacement in a text file in the "
            "conversation workspace. old_string must match the file contents "
            "exactly (including whitespace and indentation) and must appear "
            "exactly once in the file unless replace_all is true. "
            "You must have read the file with get_workspace_file (or written "
            "it with write_workspace_file) earlier in this conversation before "
            "editing it. Prefer this over rewriting the whole file with "
            "write_workspace_file when making small changes. Text files only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path within the workspace (e.g. 'report.md', 'src/main.py'). The file must already exist.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace. Must match the file contents exactly, including whitespace and indentation.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text. Must differ from old_string. May be empty to delete the matched text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence of old_string instead of requiring a unique match. Defaults to false.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Fix typo in report'.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    "memory_search": {
        "name": "memory_search",
        "description": (
            "Search the user's saved memories using full-text search. Memories are "
            "personal notes and facts the user has asked you to remember, or that "
            "have been saved from previous conversations. Use this to recall information "
            "about the user's preferences, past requests, or any facts they wanted "
            "remembered. The search supports keywords, phrases (\"exact phrase\"), "
            "prefix matching (meet*), and boolean operators (AND, OR, NOT). "
            "Always search memories when the user refers to something you should "
            "already know, or at the start of complex tasks to check for relevant context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "FTS5 search query. Use keywords, phrases, prefix matching, or boolean operators. Example: 'meeting preferences', '\"project alpha\"', 'coffee OR tea'.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Search memories for preferences'.",
                },
            },
            "required": ["query"],
        },
    },
    "memory_list": {
        "name": "memory_list",
        "description": (
            "List all of the user's saved memories. Returns all active (non-archived) "
            "memories ordered by creation time (newest first). Use this when you need "
            "a complete picture of what you know about the user, or when the user asks "
            "something like 'what do you remember about me?' or 'show me my memories'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'List all saved memories'.",
                },
            },
            "required": [],
        },
    },
    "wait_for_handles": {
        "name": "wait_for_handles",
        "description": (
            "Block until at least one of the given wait handles is resolved "
            "(accepted, rejected, cancelled, or timed out). Wait handles are "
            "returned by tools that ask the user for input. In current usage, "
            "`create_action_request` already blocks until resolved and "
            "returns the verdict directly, so this tool is rarely needed -- "
            "keep it for future opt-in waits. Pass the handle ids you want "
            "to wait on. The tool returns when ANY one of them resolves; "
            "still-pending ids are reported under `still_pending` so you can "
            "call `wait_for_handles` again on the rest. By default the call "
            "blocks for up to ~2 weeks (until the user gets to it); pass "
            "`timeout_seconds` only if you want a deliberately shorter cap. "
            "If you do not need to block on the user's decision, simply end "
            "your turn instead -- the resolution will appear in chat history "
            "before your next turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "handle_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Wait handle ids returned by earlier tool calls "
                        "(maximum 8 per wait)."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short human-readable description of WHAT YOU ARE "
                        "WAITING FOR, written for a non-technical end user. "
                        "Sentence case, no trailing period, ideally under "
                        "80 characters; values longer than 200 characters "
                        "are truncated. The chat UI shows this verbatim as "
                        "'Waiting for: <reason>' so it must read naturally "
                        "to the user. Examples: 'Waiting for you to review "
                        "the proposed memory', 'Waiting for you to approve "
                        "the calendar invite'."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Optional maximum number of seconds to wait. OMIT "
                        "this parameter for the normal case -- the call "
                        "will then block until resolved or up to ~2 weeks "
                        "(1,209,600 seconds), which is appropriate for "
                        "waiting on a human reply that may take hours or "
                        "days. Only set this when you deliberately want a "
                        "shorter cap (e.g., 'give the user 5 minutes to "
                        "confirm, otherwise move on'); do not set a small "
                        "value out of habit. Bounds when set: minimum 1 "
                        "second, maximum 1,209,600 seconds (~14 days); "
                        "values outside the range are silently clamped. "
                        "On timeout, still-pending handles are returned "
                        "with `status: \"timed_out\"`."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Wait for memory "
                        "decision'."
                    ),
                },
            },
            "required": ["handle_ids", "reason"],
        },
    },
    "download_drive_file": {
        "name": "download_drive_file",
        "description": (
            "Download a Google Drive file's binary content to the conversation workspace. "
            "Use this to download PDFs, images, spreadsheets, and other files from Google Drive. "
            "After downloading, use get_workspace_file to read or analyze the file. "
            "Requires Google Services to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": (
                        "The Google Drive file ID. Found in Drive URLs or from the "
                        "Drive API files list endpoint."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional filename to save the file as in the workspace. "
                        "If not provided, the original filename from Drive metadata is used."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Download report PDF'."
                    ),
                },
            },
            "required": ["file_id"],
        },
    },
    "google_export_doc": {
        "name": "google_export_doc",
        "description": (
            "Export a native Google Doc to the conversation workspace in any format "
            "Google Docs supports: pdf, docx, odt, rtf, txt, md (Markdown), html, "
            "epub, or zip (zipped HTML with images). Google Docs have no raw bytes, "
            "so download_drive_file cannot fetch them -- use this tool instead. "
            "After exporting, use get_workspace_file to read or analyze the file "
            "(md or txt are the cheapest formats to read back). Only Google Docs are "
            "supported; regular Drive files go through download_drive_file. "
            "Requires Google Services to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": (
                        "The Google Doc id -- the segment after /document/d/ in a Docs "
                        "URL, or the file id from a Drive files list."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["pdf", "docx", "odt", "rtf", "txt", "md", "html", "epub", "zip"],
                    "description": (
                        "Export format. One of: pdf, docx (Word), odt (OpenDocument), "
                        "rtf, txt (plain text), md (Markdown), html, epub, zip (zipped HTML)."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional filename to save the export as in the workspace. "
                        "If not provided, the document title plus the format's "
                        "extension is used (e.g. 'Q3 Report.pdf')."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Export doc as PDF'."
                    ),
                },
            },
            "required": ["document_id", "format"],
        },
    },
    "archive_gmail_message": {
        "name": "archive_gmail_message",
        "mutating": True,
        "description": (
            "Archive a Gmail message. This removes the message from the inbox by "
            "removing the INBOX label and applies a '[Quest]/archived' label so the "
            "user can find archived messages in Gmail under the [Quest] label group. "
            "The message is NOT deleted -- it remains accessible in Gmail under "
            "All Mail and the [Quest]/archived label. "
            "Optionally applies labels from the user's configured list (see "
            "list_gmail_quest_labels) in the same call via add_labels, so "
            "archive-and-label doesn't need a separate modify_gmail_labels call. "
            "Requires Google Services to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": (
                        "The Gmail message ID to archive. Found in message list "
                        "results or from the Gmail Simple/Raw API."
                    ),
                },
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional label names to apply alongside archiving, using "
                        "the short configured names from the user's list (e.g. "
                        "'receipts', not '[Quest]/receipts'). Unknown names "
                        "reject the call without archiving."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Archive newsletter email'."
                    ),
                },
            },
            "required": ["message_id"],
        },
    },
    "list_gmail_quest_labels": {
        "name": "list_gmail_quest_labels",
        "description": (
            "List the Gmail labels Quest is allowed to add to or remove from "
            "the user's emails. The user configures this list under Settings > "
            "Gmail; each configured name maps to a nested Gmail label "
            "'[Quest]/<name>'. Call this before modify_gmail_labels to see "
            "which label names are available. Archiving via "
            "archive_gmail_message is always available and is not part of "
            "this list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List usable Gmail labels'."
                    ),
                },
            },
            "required": [],
        },
    },
    "modify_gmail_labels": {
        "name": "modify_gmail_labels",
        "mutating": True,
        "description": (
            "Add and/or remove Quest-managed labels on one or more Gmail "
            "messages in a single call. Only label names from the user's "
            "configured list (see list_gmail_quest_labels) can be used; each "
            "is applied in Gmail as a nested '[Quest]/<name>' label. Labels "
            "being added are created in Gmail automatically if missing. "
            "Messages are NOT archived or deleted by this tool -- use "
            "archive_gmail_message to archive. Requires Google Services to "
            "be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Gmail message IDs to modify (1-100 per call). All "
                        "listed messages receive the same label changes."
                    ),
                },
                "add_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Label names to add, using the short configured names "
                        "(e.g. 'receipts', not '[Quest]/receipts'). May be "
                        "empty when only removing."
                    ),
                },
                "remove_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Label names to remove, using the short configured "
                        "names. May be empty when only adding."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Label 12 receipts emails'."
                    ),
                },
            },
            "required": ["message_ids"],
        },
    },
    "get_gmail_messages": {
        "name": "get_gmail_messages",
        "description": (
            "Fetch one or more Gmail messages (by Gmail message ID) rendered "
            "as a markdown document with decoded body, key headers (From/To/"
            "Cc/Date/Message-ID/Thread ID/Labels), and an attachments list. "
            "This is the recommended way to read Gmail messages. Long URLs in "
            "the body are replaced with short numeric identifiers like (#1#) "
            "to save tokens (resolve them with get_gmail_message_urls). Does "
            "NOT support search queries -- search for message IDs first via "
            "authed_get on the Gmail Raw API "
            "(https://gmail.googleapis.com/gmail/v1/users/me/messages?q=...). "
            "Requires Google Services to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Gmail message IDs to fetch (1-50 per call). More "
                        "than 50 must be split into multiple calls."
                    ),
                },
                "include_html": {
                    "type": "boolean",
                    "description": (
                        "If true, return the raw HTML body in a fenced html "
                        "block instead of the markdown conversion "
                        "(default: false)."
                    ),
                },
                "include_urls": {
                    "type": "boolean",
                    "description": (
                        "If true, keep original full URLs inline instead of "
                        "replacing them with short numeric identifiers "
                        "(default: false)."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read 3 unread emails'."
                    ),
                },
            },
            "required": ["message_ids"],
        },
    },
    "list_gmail_labels": {
        "name": "list_gmail_labels",
        "description": (
            "List the user's Gmail labels (id, name, type, message/thread "
            "counts) in a simplified format. Pass label_id to fetch a single "
            "label instead. Not to be confused with list_gmail_quest_labels, "
            "which lists only the Quest-managed label names Quest may "
            "add/remove. Requires Google Services to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label_id": {
                    "type": "string",
                    "description": (
                        "Optional Gmail label ID (e.g. 'INBOX' or a user "
                        "label id). When set, returns just that label."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List Gmail labels'."
                    ),
                },
            },
            "required": [],
        },
    },
    "get_gmail_message_urls": {
        "name": "get_gmail_message_urls",
        "description": (
            "Resolve the short numeric URL identifiers like (#1#) that "
            "get_gmail_messages substitutes into message bodies back to the "
            "original full URLs. Pass the identifiers to resolve, or omit "
            "them to get all URL mappings for the message. Only works for "
            "messages already fetched in this conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Gmail message ID the identifiers came from.",
                },
                "identifiers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Numeric identifiers to resolve (e.g. [1, 2, 3]). "
                        "Omit to return all URL mappings for the message."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Resolve email links'."
                    ),
                },
            },
            "required": ["message_id"],
        },
    },
    "create_gmail_draft": {
        "name": "create_gmail_draft",
        "mutating": True,
        "description": (
            "Create a draft email in the user's Gmail account. The draft is "
            "NOT sent -- the user reviews and sends it from Gmail. Write the "
            "body as markdown in body_md (rendered to HTML) -- use the "
            "plain-text body parameter only when the user explicitly asks "
            "for a plain-text email. Supports reply/forward threading "
            "(in_reply_to_message_id + thread_id from a fetched message), and "
            "attachments from Google Drive, the conversation workspace, or an "
            "existing Gmail message (total attachment size limit 25MB; native "
            "Google file types like Docs/Sheets/Slides cannot be attached -- "
            "link to them in the body instead). Load system:gmail for the "
            "reply/forward workflows. Requires Google Services to be "
            "connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address(es), comma-separated.",
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line.",
                },
                "body_md": {
                    "type": "string",
                    "description": (
                        "Markdown-formatted email body, rendered to HTML. "
                        "Use this by default, even for short or simple "
                        "emails. Write each paragraph as one unwrapped line "
                        "and separate paragraphs with a blank line -- do not "
                        "hard-wrap text at a fixed column, since every "
                        "newline becomes a line break in the email."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Plain text email body. Only use INSTEAD of body_md "
                        "when the user explicitly asks for a plain-text "
                        "email. Required if body_md is not provided; when "
                        "both are provided, body is used as the plain-text "
                        "part."
                    ),
                },
                "cc": {
                    "type": "string",
                    "description": "CC recipients, comma-separated.",
                },
                "bcc": {
                    "type": "string",
                    "description": "BCC recipients, comma-separated.",
                },
                "in_reply_to_message_id": {
                    "type": "string",
                    "description": (
                        "Message-ID header of the message being replied to "
                        "(the 'Message-ID:' line from get_gmail_messages, "
                        "not the Gmail message id)."
                    ),
                },
                "references": {
                    "type": "string",
                    "description": "References header value for threading.",
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "Gmail thread ID to attach the draft to an existing "
                        "thread (the 'Thread ID:' line from "
                        "get_gmail_messages)."
                    ),
                },
                "attachments": _GMAIL_ATTACHMENTS_SCHEMA,
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Draft reply to Alice'."
                    ),
                },
            },
            "required": ["to", "subject"],
        },
    },
    "send_gmail_to_self": {
        "name": "send_gmail_to_self",
        "mutating": True,
        "description": (
            "Send an email from the user to themselves, immediately (not a "
            "draft). Useful for delivering reports, summaries, or reminders "
            "to the user's inbox. The subject is automatically prefixed with "
            "'[Quest]' and body_md is rendered to HTML. Supports attachments "
            "from the conversation workspace, Google Drive, or an existing "
            "Gmail message (25MB total; native Google Docs/Sheets/Slides "
            "cannot be attached -- link to them instead), and an attached "
            "image can be shown inline with ![caption](cid:<filename>). "
            "Requires Google Services to be connected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Email subject (auto-prefixed with [Quest]).",
                },
                "body_md": {
                    "type": "string",
                    "description": (
                        "Markdown-formatted email body, rendered to HTML for "
                        "a nicely formatted email. Remote images "
                        "(![](https://...)) and raw HTML are stripped from "
                        "the rendered email -- attach the image and embed "
                        "it with ![caption](cid:<filename>) instead."
                    ),
                },
                "attachments": _GMAIL_ATTACHMENTS_SCHEMA,
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Email daily summary'."
                    ),
                },
            },
            "required": ["subject", "body_md"],
        },
    },
    "set_conversation_name": {
        "name": "set_conversation_name",
        "description": (
            "Set the conversation's display name shown in the sidebar navigation. "
            "This sets a custom name for the current conversation. The name will "
            "only be set if no custom name has been set yet (either by the user or "
            "by a previous call). Call this after the user's first message to "
            "summarize their request concisely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "A short summary of the conversation topic (max 100 characters). "
                        "This will be displayed as the conversation title in the sidebar. "
                        "Keep it concise and descriptive. Examples: 'Bitcoin price analysis', "
                        "'Email draft for Alice', 'Project planning for Q2'."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Name this conversation'."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    "project_db_query": {
        "name": "project_db_query",
        "description": (
            "Execute a SQL query against the project's dedicated SQLite database. "
            "This database is private to the project and persists across all conversations "
            "in the project. Use it to store structured data, create tables, insert/update/"
            "delete rows, and run SELECT queries. The database is initially empty -- create "
            "tables as needed. Only available in project conversations. "
            "Only one SQL statement per call is supported; make multiple calls for multiple "
            "statements."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The SQL query to execute. Supports any valid SQLite SQL "
                        "(CREATE TABLE, INSERT, UPDATE, DELETE, SELECT, etc.). "
                        "One statement per call."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Create tasks table'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    "authed_get": {
        "name": "authed_get",
        "description": (
            "Make an authenticated GET request to a supported external API. "
            "The URL must point to a known external service -- authentication "
            "credentials are injected automatically. Do NOT include API keys in "
            "the URL or headers.\n"
            "\n"
            "Supported upstream hosts include Gmail Raw, Google Calendar, "
            "Drive, Docs, Sheets, Slides, Tasks, Google Cloud (Resource "
            "Manager, Compute, GKE), Airtable, Ramp, Federal "
            "Register, SEC EDGAR, and CoinGecko Pro. "
            "Exact URL shapes, query parameters, and example calls for each "
            "backend are documented in the corresponding system skill "
            "(`system:gmail`, `system:calendar`, `system:drive`, `system:docs`, "
            "`system:sheets`, `system:slides`, `system:tasks`, `system:gcp`, "
            "`system:airtable`, "
            "`system:ramp`, `system:federal_register`, "
            "`system:sec_edgar`). "
            "Load the backend's skill via `load_skills` before constructing "
            "requests for that service. CoinGecko does not have a dedicated "
            "skill -- see the CoinGecko notes below.\n"
            "\n"
            "The two GCP reads that Google exposes only as POST "
            "(`organizations:search` and `entries:list`) use the sibling "
            "`authed_post` tool, NOT authed_get.\n"
            "\n"
            "CoinGecko notes (no dedicated system skill):\n"
            "- Base URL: https://pro-api.coingecko.com/api/v3/\n"
            "- Common endpoints: /search?query=, /simple/price?ids=&vs_currencies=, "
            "/coins/{id}, /coins/{id}/market_chart?vs_currency=&days=, "
            "/coins/{id}/ohlc?vs_currency=&days=, "
            "/coins/{platform}/contract/{contract_address}\n"
            "- 'ids' use CoinGecko coin IDs (e.g. 'bitcoin'), not tickers -- search first if unsure.\n"
            "- 'vs_currencies' supports 'usd', 'eur', 'btc', etc.\n"
            "- 'days' can be 1, 7, 14, 30, 90, 180, 365, or 'max'.\n"
            "\n"
            "Some endpoints (e.g. GitHub Actions per-job logs) answer with a "
            "302 redirect to a signed URL and are NOT reachable via "
            "authed_get -- the backend's system skill documents the "
            "dedicated tool to use instead.\n"
            "\n"
            "Size limit: Responses larger than ~3KB will be rejected with a "
            "suggestion to narrow the request. If the full response is truly "
            "needed, pass force_large_response=true and the content will be "
            "saved to a file for chunked reading via get_response_content.\n"
            "\n"
            "Set output_file to a workspace-relative path to save the response "
            "body directly to a file under the hidden '.responses/' workspace "
            "directory instead of returning it inline. This bypasses the response "
            "size gate and is the preferred option when you know up-front you want "
            "to process the body with run_python / run_script / get_workspace_file "
            "rather than read it inline. The receipt's 'path' is the full "
            "'.responses/...' path -- read the file back with that exact path. "
            "When output_file is set, force_large_response is ignored."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "The full upstream API URL including query parameters. "
                        "Must be HTTPS and match a supported service. "
                        "Example: https://pro-api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
                    ),
                },
                "headers": {
                    "type": "object",
                    "description": (
                        "Optional additional HTTP headers as key-value pairs. Only "
                        "content-negotiation headers (Accept, Accept-Language) are "
                        "accepted; any other header is rejected. "
                        "Do NOT include authentication headers -- they are injected automatically."
                    ),
                },
                "force_large_response": {
                    "type": "boolean",
                    "description": (
                        "Set to true to allow large responses. When a response exceeds the size "
                        "limit, it will be saved to a file for chunked reading via "
                        "get_response_content. Only use this after trying to reduce the response "
                        "size with more targeted query parameters (fields, filters, maxResults, etc.). "
                        "Ignored when output_file is set."
                    ),
                },
                "output_file": {
                    "type": "string",
                    "description": (
                        "Optional workspace-relative path. When set, the response body is "
                        "written under the hidden '.responses/' directory of the "
                        "conversation/project workspace (so the path you pass is re-rooted to "
                        "'.responses/<output_file>') and the tool returns a small JSON receipt "
                        "(path, bytes_written, content_type, status_code) instead of the body "
                        "itself. The returned 'path' is the full '.responses/...' path -- pass it "
                        "verbatim to get_workspace_file / run_python / run_script to read it back. "
                        "The '.responses/' directory is hidden by default in the file browser "
                        "(the user can reveal it with a toggle). Use forward slashes. Absolute "
                        "paths and any '..' segments are rejected. If the path ends with '/' or "
                        "names an existing directory, an auto-generated filename is appended "
                        "inside that directory. If a file already exists at the resolved "
                        "path, the call errors instead of overwriting it -- choose a "
                        "different output_file name. When set, the "
                        "response size gate and force_large_response are bypassed (output_file "
                        "wins). Setting output_file also unlocks alt=media for binary downloads, "
                        "since the bytes go straight to disk."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Fetch Bitcoin price'."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    "authed_post": {
        "name": "authed_post",
        "description": (
            "Make an authenticated POST request to the NARROW set of read-shaped "
            "POST endpoints that Google Cloud only exposes as POST. This is NOT a "
            "general POST proxy -- it can reach ONLY two endpoints today, both "
            "read-only:\n"
            "- `POST https://cloudresourcemanager.googleapis.com/v1/organizations:search` "
            "-- search/list GCP organizations (optional `{\"query\": \"...\"}` body; "
            "empty body lists all visible orgs).\n"
            "- `POST https://logging.googleapis.com/v2/entries:list` -- read Cloud "
            "Logging log entries (body with `resourceNames`, `filter`, `orderBy`, "
            "`pageSize`).\n"
            "\n"
            "Write verbs (e.g. `entries:write`) are rejected by a per-host POST "
            "allow-list, and POSTs to any other host or path are rejected. "
            "Authentication credentials are injected automatically -- do NOT "
            "include API keys in the URL, headers, or body. Exact body shapes and "
            "example calls are documented in the `system:gcp` skill; load it via "
            "`load_skills` first.\n"
            "\n"
            "Size limit, force_large_response, and output_file behave exactly as "
            "for authed_get: responses larger than ~3KB are rejected unless "
            "force_large_response=true (saved for chunked reading) or output_file "
            "is set (body written under the hidden '.responses/' workspace "
            "directory; recommended for large log pulls)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "The full upstream API URL. Must be HTTPS and match one of "
                        "the two allow-listed read-shaped POST endpoints. "
                        "Example: https://logging.googleapis.com/v2/entries:list"
                    ),
                },
                "body": {
                    "type": "object",
                    "description": (
                        "The JSON request body forwarded to the upstream endpoint "
                        "(e.g. {\"resourceNames\": [\"projects/my-project\"], "
                        "\"filter\": \"severity>=ERROR\", \"orderBy\": \"timestamp desc\", "
                        "\"pageSize\": 50}). Optional for organizations:search."
                    ),
                },
                "headers": {
                    "type": "object",
                    "description": (
                        "Optional additional HTTP headers as key-value pairs. Only "
                        "content-negotiation headers (Accept, Accept-Language) are "
                        "accepted; any other header is rejected. "
                        "Do NOT include authentication headers -- they are injected automatically."
                    ),
                },
                "force_large_response": {
                    "type": "boolean",
                    "description": (
                        "Set to true to allow large responses. When a response exceeds the size "
                        "limit, it will be saved to a file for chunked reading via "
                        "get_response_content. Only use this after trying to reduce the response "
                        "size (e.g. a smaller pageSize or a tighter filter). "
                        "Ignored when output_file is set."
                    ),
                },
                "output_file": {
                    "type": "string",
                    "description": (
                        "Optional workspace-relative path. When set, the response body is "
                        "written under the hidden '.responses/' directory of the "
                        "conversation/project workspace and the tool returns a small JSON "
                        "receipt (path, bytes_written, content_type, status_code) instead of "
                        "the body itself. The returned 'path' is the full '.responses/...' "
                        "path -- pass it verbatim to get_workspace_file / run_python / "
                        "run_script to read it back. Absolute paths and '..' segments are "
                        "rejected. When set, the size gate and force_large_response are "
                        "bypassed. Recommended for large log-entry pulls."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read error logs'."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    "get_response_content": {
        "name": "get_response_content",
        "description": (
            "Read a chunk of a previously-saved large API response file. Use this after "
            "authed_get returns a stored response with a hash. Pass the hash, a character "
            "offset (0-indexed), and a length to read. Returns the content chunk along with "
            "total file size and whether more content remains."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hash": {
                    "type": "string",
                    "description": "The hash identifier from the authed_get stored response.",
                },
                "offset": {
                    "type": "integer",
                    "description": "0-indexed character offset to start reading from.",
                },
                "length": {
                    "type": "integer",
                    "description": "Number of characters to read (maximum 5120).",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read response chunk'."
                    ),
                },
            },
            "required": ["hash", "offset", "length"],
        },
    },
    # -- Quest Docs (chat/docs/service.py). All seven carry
    # requires_service=DOCS_SERVICE_KEY: the "docs" connected-services
    # pseudo-key mirrors the per-user docs feature gate, so the tools are
    # left out of prompts while the gate is closed (the handlers also
    # refuse with docs_disabled). The four writes are "mutating": the
    # access rule already denies them in read-only runs, and the flag
    # keeps inference API runs and their sandbox leases refusing them at
    # dispatch like every other approval-free write.
    "list_docs": {
        "name": "list_docs",
        "requires_service": DOCS_SERVICE_KEY,
        "description": (
            "List the Quest Docs this conversation can see -- markdown "
            "documents kept inside Quest, owned by the user (scope 'user') "
            "or by this conversation's project (scope 'project') -- newest "
            "updated first. Each row: id, title, description, mode "
            "('private' or 'public'), scope, project_id, content_size "
            "(bytes), asset_count (embedded images), updated_at, writable "
            "and write_note. writable is 'free' (the write tools work "
            "directly), 'approval' (a shared private doc: changes go "
            "through a write_doc action request) or 'denied' (read-only "
            "here; write_note says why). Find docs by content with "
            "search_docs; read one with read_doc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["user", "project", "all"],
                    "default": "all",
                    "description": (
                        "'user' = the user's own docs, 'project' = this "
                        "project's docs (empty outside a project), 'all' "
                        "(default) = both."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                    "description": "Maximum number of docs to return (1-200, default 50).",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List my docs'."
                    ),
                },
            },
            "required": [],
        },
    },
    "search_docs": {
        "name": "search_docs",
        "requires_service": DOCS_SERVICE_KEY,
        "description": (
            "Search the Quest Docs this conversation can see: a "
            "case-insensitive substring match over title, description and "
            "body. Returns {results: [{id, title, mode, scope, matches: "
            "[{line, snippet}]}], truncated}, newest-updated docs first, "
            "with up to 3 snippets of about 200 characters per doc and their "
            "1-based line numbers (page to them with read_doc start_line / "
            "end_line). A doc that matched only on its title or description "
            "has matches: []. truncated: true means the bounded body scan "
            "stopped early -- narrow the scope or the query. Search results "
            "do not count as reading a doc for edit_doc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to find (case-insensitive substring, not a pattern).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["user", "project", "all"],
                    "default": "all",
                    "description": (
                        "'user', 'project' (this project's docs) or 'all' "
                        "(default)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                    "description": "Maximum number of matching docs (1-50, default 20).",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Search docs for Q3'."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    "read_doc": {
        "name": "read_doc",
        "requires_service": DOCS_SERVICE_KEY,
        "description": (
            "Read a Quest Doc's markdown body. Without a range it returns up "
            "to 200,000 characters (cut at a line boundary; truncated: true "
            "and a note when the doc is longer -- page with start_line / "
            "end_line); with start_line / end_line it returns that 1-based, "
            "inclusive line range. Returns {id, title, mode, scope, "
            "total_lines, content, writable, write_note, start_line, "
            "end_line, truncated}. Reading a doc here is required before "
            "edit_doc can change it in this conversation. Images in the "
            "body are referenced relatively as ![alt](assets/<name>)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "The doc id from list_docs, search_docs or create_doc.",
                },
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "First line to return (1-based, inclusive). Defaults to 1.",
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Last line to return (1-based, inclusive). Defaults to the last line.",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read meeting notes'."
                    ),
                },
            },
            "required": ["doc_id"],
        },
    },
    "create_doc": {
        "name": "create_doc",
        "requires_service": DOCS_SERVICE_KEY,
        "mutating": True,
        "description": (
            "Create a new Quest Doc: a markdown document kept inside Quest "
            "that later conversations can find with list_docs / search_docs "
            "and read with read_doc. Use it for content meant to outlive "
            "this conversation (notes, reports, running logs); keep scratch "
            "files in the workspace. target 'user' (default) creates one of "
            "the user's own docs; user docs are always private, so in a "
            "public project use target=\"project\" (target 'user' is "
            "refused there). target 'project' creates a doc of this "
            "conversation's project, visible only from that project's "
            "conversations; it takes the project's mode (public in a "
            "public project, private otherwise). No doc's mode can be "
            "changed later. Titles are unique per user (per project for "
            "project docs), case-insensitively. Content is "
            "markdown, max 1 MB; reference images with add_doc_image rather "
            "than external URLs. The new doc counts as read, so edit_doc "
            "works on it right away. Returns {id, title, mode, scope, "
            "project_id, content_size, updated_at}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Doc title, 1-200 characters, unique in its scope.",
                },
                "content": {
                    "type": "string",
                    "description": "The initial markdown body (may be empty; max 1 MB).",
                },
                "description": {
                    "type": "string",
                    "description": "Optional one-line summary shown in doc lists (max 500 characters).",
                },
                "target": {
                    "type": "string",
                    "enum": ["user", "project"],
                    "default": "user",
                    "description": (
                        "'user' (default) for one of the user's docs "
                        "(always private; refused in a public project), "
                        "'project' for a doc of this conversation's project "
                        "(project conversations only; the only target in a "
                        "public project)."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Create research doc'."
                    ),
                },
            },
            "required": ["title", "content"],
        },
    },
    "edit_doc": {
        "name": "edit_doc",
        "requires_service": DOCS_SERVICE_KEY,
        "mutating": True,
        "description": (
            "Change a Quest Doc with an exact string replacement: old_string "
            "must match the current body exactly (whitespace included) and "
            "appear exactly once unless replace_all is true. The doc must "
            "have been read with read_doc (or created) earlier in this "
            "conversation. There is no whole-body replace: make targeted "
            "edits, or add content with append_to_doc. Returns {replaced, "
            "total_lines, updated_at}. If the result is "
            "{\"error\": \"approval_required\", ...} the doc is shared: "
            "propose the same change as a write_doc action request by "
            "forwarding the returned suggested_request (its request_type "
            "and params) unchanged through create_action_request. Any other "
            "error explains why the doc cannot be changed here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "The doc id.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace, as it appears in the doc.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text. Must differ from old_string. May be empty to delete the match.",
                },
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace every occurrence instead of requiring a unique match. Defaults to false.",
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Fix doc typo'."
                    ),
                },
            },
            "required": ["doc_id", "old_string", "new_string"],
        },
    },
    "append_to_doc": {
        "name": "append_to_doc",
        "requires_service": DOCS_SERVICE_KEY,
        "mutating": True,
        "description": (
            "Append markdown to the end of a Quest Doc. Needs no prior read "
            "(nothing existing is overwritten), so it is the way to grow "
            "running logs and recurring reports -- e.g. a dated "
            "'## YYYY-MM-DD' heading followed by the new entry. "
            "ensure_blank_line (default true) separates the new block from "
            "the existing text with a blank line. The whole body stays "
            "capped at 1 MB. Returns {appended_lines, total_lines, "
            "updated_at}. An approval_required result is handled exactly "
            "as for edit_doc: forward suggested_request unchanged as a "
            "write_doc action request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "The doc id.",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown to append (non-empty).",
                },
                "ensure_blank_line": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Separate the appended block from the existing text "
                        "with a blank line (default true); false only starts "
                        "it on a new line."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Log daily summary'."
                    ),
                },
            },
            "required": ["doc_id", "content"],
        },
    },
    "add_doc_image": {
        "name": "add_doc_image",
        "requires_service": DOCS_SERVICE_KEY,
        "mutating": True,
        "description": (
            "Embed an image from this conversation's workspace in a Quest "
            "Doc: the file (PNG, JPEG, GIF or WebP, max 5 MB, checked by its "
            "content -- SVG is refused) is copied into the doc's assets "
            "under a sanitized name (-2, -3 ... on collision; max 200 images "
            "/ 100 MB per doc). placement 'append' (default) also appends "
            "![alt](assets/<name>) to the end of the doc; 'none' only stores "
            "the image and returns the markdown snippet for you to place "
            "with edit_doc. Returns {asset, markdown, appended, asset_count, "
            "updated_at, total_lines (when appended)}. An approval_required "
            "result is handled exactly as for edit_doc: forward "
            "suggested_request unchanged as a write_doc action request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "The doc id.",
                },
                "workspace_path": {
                    "type": "string",
                    "description": "Workspace-relative path of the image (same paths as list_workspace_files).",
                },
                "alt": {
                    "type": "string",
                    "description": "Alt text for the image; defaults to the stored file name.",
                },
                "placement": {
                    "type": "string",
                    "enum": ["append", "none"],
                    "default": "append",
                    "description": (
                        "'append' (default) appends the image markdown to the "
                        "doc; 'none' only stores the image and returns the "
                        "snippet."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Add chart to doc'."
                    ),
                },
            },
            "required": ["doc_id", "workspace_path"],
        },
    },
}


# ---------------------------------------------------------------------------
# Base tools (available to both parent and sub-agents)
# ---------------------------------------------------------------------------

BASE_TOOLS: list[ToolSpec] = [
    {
        "name": "curl_proxy_get",
        "description": (
            "Make a GET request to a Quest API endpoint. "
            "Authentication is automatic — do NOT set an Authorization header. "
            "The URL must be http://localhost:8000/api/* (e.g. "
            "http://localhost:8000/api/gmail-simple/labels). Most services "
            "have dedicated tool_call tools instead -- prefer those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to the Quest API endpoint, e.g. http://localhost:8000/api/gmail-simple/labels",
                },
                "headers": {
                    "type": "object",
                    "description": "Optional HTTP headers as key-value pairs. Do NOT include Authorization — it is injected automatically.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent for making this call (max 50 characters). Example: 'List Gmail labels', 'Update task status'.",
                },
            },
            "required": ["url", "intent_message"],
        },
    },
    {
        "name": "curl_proxy_post",
        "description": (
            "Make a POST request to a Quest API endpoint. "
            "Authentication is automatic — do NOT set an Authorization header. "
            "The URL must be http://localhost:8000/api/* (e.g. "
            "http://localhost:8000/api/gmail-simple/drafts)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to the Quest API endpoint, e.g. http://localhost:8000/api/gmail-simple/drafts",
                },
                "headers": {
                    "type": "object",
                    "description": "Optional HTTP headers as key-value pairs. Do NOT include Authorization — it is injected automatically.",
                },
                "body": {
                    "type": "string",
                    "description": "JSON request body as a string.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent for making this call (max 50 characters). Example: 'List Telegram chats', 'Update task status'.",
                },
            },
            "required": ["url", "intent_message"],
        },
    },
    TOOL_CALL_SPEC,
    {
        "name": "load_gmail_attachment",
        "description": (
            "Fetch a Gmail attachment and make it available for analysis. "
            "Use this to read PDF, image, or other file attachments from Gmail messages. "
            "Get the message_id and attachment_id from the '## Attachments' section in a "
            "get_gmail_messages response (the 'attachmentId' value). "
            "The file is included in this response for you to analyze directly. "
            "Note: the 'account' parameter is accepted for forward-compatibility but is currently "
            "ignored -- the authenticated user's Google Services account is always used."
        ),
        "provider_descriptions": {
            "gemini": (
                "Fetch a Gmail attachment and upload it to the Gemini File API for analysis. "
                "Use this to read PDF, image, or other file attachments from Gmail messages. "
                "Get the message_id and attachment_id from the '## Attachments' section in a "
                "get_gmail_messages response (the 'attachmentId' value). "
                "The file is uploaded to Gemini and included in this response for you to analyze directly. "
                "Note: the 'account' parameter is accepted for forward-compatibility but is currently "
                "ignored -- the authenticated user's Google Services account is always used."
            ),
            "anthropic": (
                "Fetch a Gmail attachment and make it available for analysis. "
                "Use this to read PDF, image, or other file attachments from Gmail messages. "
                "Get the message_id and attachment_id from the '## Attachments' section in a "
                "get_gmail_messages response (the 'attachmentId' value). "
                "The file content is encoded inline in this response for you to analyze directly. "
                "Note: the 'account' parameter is accepted for forward-compatibility but is currently "
                "ignored -- the authenticated user's Google Services account is always used."
            ),
        },
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Gmail message ID containing the attachment.",
                },
                "attachment_id": {
                    "type": "string",
                    "description": "The attachment ID from the message's '## Attachments' section (the 'attachmentId' value in get_gmail_messages responses).",
                },
                "filename": {
                    "type": "string",
                    "description": "The filename of the attachment (from the 'filename' field in the attachments array). Used as the display name when uploading. Defaults to 'attachment' if not provided.",
                },
                "mime_type": {
                    "type": "string",
                    "description": "The MIME type of the attachment (from the 'mimeType' field in the attachments array). Defaults to 'application/octet-stream' if not provided.",
                },
                "account": {
                    "type": "string",
                    "description": "The Gmail account identifier. Currently only the authenticated user's account is supported. This parameter is accepted but not used for account selection.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Load PDF attachment'.",
                },
            },
            "required": ["message_id", "attachment_id"],
        },
    },
    {
        "name": "run_script",
        "description": (
            "Run a script file from the workspace inside an ephemeral Podman container "
            "with Python 3.12 pre-installed. The workspace is mounted read-write so the "
            "script can read input files and produce output files. The Quest API proxy "
            "is available at localhost inside the container (same port as your tool calls). "
            "A short-lived sandbox API token (valid only while the container runs) "
            "is in the QUEST_API_KEY environment variable. "
            "Pre-installed Python libraries: requests, openpyxl, python-docx, matplotlib, seaborn, "
            "pypdf (PDF merge/split/extract), PyPDFForm (fill PDF forms). "
            "Pre-installed CLI tools: curl, jq, bash, zip, unzip (the zip and unzip commands "
            "support creating and extracting optionally password-protected zip archives via "
            "`zip -P <password> archive.zip files...` and `unzip -P <password> archive.zip`). "
            "Returns stdout, stderr, and exit code. The script must already exist in "
            "the workspace (use write_workspace_file first to create it)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the script file within the workspace (e.g., 'analyze.py', 'scripts/process.sh').",
                },
                "args": {
                    "type": "string",
                    "description": "Command-line arguments to pass to the script.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default 60, max 150).",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Run analysis script'.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run inline Python code inside the same sandboxed Podman container as run_script. "
            "The script content is passed via stdin -- no file is created in the workspace. "
            "The workspace is mounted read-write so the script can read input files and create "
            "output files. Pre-installed Python libraries: requests, openpyxl, python-docx, "
            "matplotlib, seaborn, pypdf (PDF merge/split/extract), PyPDFForm (fill PDF forms). "
            "Pre-installed CLI tools: curl, jq, bash, zip, unzip (shell "
            "out via subprocess to use `zip -P <password> archive.zip files...` for "
            "password-protected archives, or `unzip -P <password> archive.zip` to extract them). "
            "The Quest API proxy is accessible inside the container. "
            "Returns stdout, stderr, and exit code. "
            "Use this for one-off tasks (data analysis, file conversion, quick computations). "
            "For reusable scripts that should persist in the workspace, use "
            "write_workspace_file + run_script instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "The Python script content to execute.",
                },
                "args": {
                    "type": "string",
                    "description": "Command-line arguments accessible via sys.argv inside the script.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default 60, max 150).",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Analyze Excel data'.",
                },
            },
            "required": ["script"],
        },
    },
    {
        "name": "list_skills",
        "description": (
            "List all skills accessible to the current user, including their own skills, "
            "shared skills, public skills, AND built-in 'system' skills (ids prefixed "
            "with `system:`, e.g. `system:gmail`). System skills carry per-backend API "
            "documentation and appear first in the result with visibility=='system'. "
            "Returns skill IDs, names, descriptions, and visibility levels -- but NOT "
            "the full skill content. Use load_skills to fetch the full content of "
            "specific skills (system or DB) after identifying interesting ones from this list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Browse available skills'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_skills",
        "description": (
            "Search skills by keyword, matching against skill names and descriptions. "
            "Includes both DB-backed user/shared/public skills AND built-in 'system' "
            "skills (ids prefixed with `system:`, visibility=='system'). Returns "
            "matching skill IDs, names, descriptions, and visibility levels -- but "
            "NOT the full skill content. Use load_skills to fetch the full content "
            "of specific skills after finding relevant ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "The search term to match against skill names and descriptions.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Search for email skills'.",
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "load_skills",
        "description": (
            "Fetch the full content of one or more skills by their IDs. Accepts "
            "both DB skill UUIDs and built-in 'system' skill ids (prefixed "
            "`system:`, e.g. `system:gmail`). System skill ids are listed in the "
            "System Skills enumeration block of the system prompt and in the "
            "results of list_skills / search_skills. You can mix system and DB "
            "ids in a single call. Each skill can be up to 64KB, so loading many "
            "skills at once may produce a large result. "
            "Returns a markdown document (not JSON): a top-level `# Loaded "
            "skills` heading with a counts line, an optional `## Load errors` "
            "section first (when any requested system skill's gate failed -- "
            "e.g. the backend is disconnected), then one `===`-separated block "
            "per successfully loaded skill. Each block has a `# <skill name>` "
            "heading, a metadata header (`**ID:**`, `**Visibility:**`, "
            "`**Description:**`), and a `## Skill Content` section holding the "
            "raw skill content verbatim. Unknown system ids and inaccessible DB "
            "UUIDs are silently skipped (not counted)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The skill IDs to load. Mix of `system:*` ids and DB UUIDs (from list_skills / search_skills, or the System Skills enumeration in the system prompt).",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Load data analysis skill'.",
                },
            },
            "required": ["skill_ids"],
        },
    },
    {
        "name": "list_my_skills",
        "description": (
            "List the skills visible to you. By default returns all of them: "
            "skills you created, skills others shared directly with you, "
            "public skills, and -- when this conversation is in a project -- "
            "skills scoped to that project. Each entry shows its visibility, "
            "its category (own / shared / public / project), and which tier(s) "
            "it auto-loads at (user / project / routine). Use the exclude_* "
            "flags to narrow the categories. Read-only; does NOT include "
            "built-in system skills (use list_skills for those) or full skill "
            "bodies (use get_skill for one skill's full content and settings)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "exclude_own": {
                    "type": "boolean",
                    "description": "When true, omit skills you authored. Default false.",
                },
                "exclude_shared": {
                    "type": "boolean",
                    "description": "When true, omit skills shared directly with you (visibility 'shared', not authored by you). Default false.",
                },
                "exclude_public": {
                    "type": "boolean",
                    "description": "When true, omit public skills you did not author (visibility 'public', not authored by you). Default false.",
                },
                "exclude_project": {
                    "type": "boolean",
                    "description": "When true, omit skills scoped to the current project (visibility 'project'). Default false.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'List my skills'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_skill",
        "description": (
            "Get the full contents and settings of one skill by id: its body, "
            "visibility, creator, autoload status, and -- if you own it -- who "
            "it is shared with. Read-only. The share roster is returned only "
            "when you are the skill's creator (otherwise shares is null)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "The id (UUID) of the skill to fetch.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters). Example: 'Inspect skill detail'.",
                },
            },
            "required": ["skill_id"],
        },
    },
    {
        "name": "list_routines",
        "description": (
            "List this project's routines (canned prompts that run in one "
            "click or on a schedule). Each entry includes the routine's id, "
            "name, full prompt, model override, schedule (or null), and "
            "auto-loaded skills. Read-only; only available in project "
            "conversations. To propose a new routine or changes to an "
            "existing one (name, prompt, model, schedule, auto-loaded "
            "skills), use `create_action_request` with "
            "`request_type=\"create_routine\"` / `\"edit_routine\"` -- "
            "load `system:routines` first for the exact params."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'List project routines'."
                    ),
                },
            },
            "required": [],
        },
    },
]

# ---------------------------------------------------------------------------
# Agent-specific tools
# ---------------------------------------------------------------------------

_AGENT_TASK_RESPONSE: ToolSpec = {
    "name": "agent_task_response",
    "description": (
        "Return the result of your task to the parent agent. You MUST call this tool "
        "when you have completed your assigned task. Pass your complete findings, "
        "analysis, or results as the response parameter. After calling this tool, "
        "your task is finished. If your task required a write that needs user "
        "approval (Slack/Telegram/Twitter sends, calendar invites, "
        "memory saves, etc.), do NOT try to call `create_action_request` -- it is "
        "top-level only and unavailable here. Instead, include the proposed action "
        "(the `request_type`, full `params`, and a recommended `reasoning`) in your "
        "response so the parent can issue `create_action_request(...)` on your behalf."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "response": {
                "type": "string",
                "description": "Your complete response/findings to return to the parent agent.",
            },
        },
        "required": ["response"],
    },
}

_AGENT_TASK: ToolSpec = {
    "name": "agent_task",
    "description": (
        "Spawn a sub-agent to work on a specific task. The sub-agent has access to "
        "the same read APIs and workspace files as you, but it does NOT have "
        "`create_action_request`, `wait_for_handles`, `set_conversation_name`, or "
        "any of the `agent_task*` spawning tools -- those are top-level only. Do "
        "not delegate writes that need user approval (Slack/Telegram/Twitter sends, "
        "calendar invites, memory saves) to a sub-agent; instead have "
        "the sub-agent gather data / draft the proposal / verify parameters and "
        "return that to you, and you (the parent) issue the `create_action_request`. "
        "Use this to delegate work that can be done independently, such as "
        "researching a topic, analyzing a file, or gathering information from "
        "multiple API calls. The sub-agent will execute the task and return its "
        "findings as text. If you need to run "
        f"multiple independent sub-agent tasks, prefer agent_task_parallel (max {MAX_PARALLEL_TASKS} "
        "tasks) instead of calling agent_task multiple times."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A short display name for this sub-agent (shown to the user). Example: 'Email Researcher', 'Data Analyst'.",
            },
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the sub-agent. Be specific about what you want it to accomplish and what information to include in its response.",
            },
            "description": {
                "type": "string",
                "description": "A brief description of what this sub-agent does (max 80 characters, shown in the UI). Example: 'Searching for unread emails from Alice'.",
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional: the model for this sub-agent to use. "
                    "If omitted, the sub-agent uses the same model as you. "
                    "Valid values: 'gemini-3.5-flash-lite', 'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash', 'claude-haiku-4.5', 'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-opus-4-7', 'claude-opus-4-8', 'claude-sonnet-5', 'claude-sonnet-5-5', 'claude-opus-5', 'claude-opus-5-5'. "
                    "Gemini 3.1 Pro ('gemini-3.1-pro-preview') is NOT available to sub-agents. "
                    "Use a faster/cheaper model for simple tasks like data retrieval, "
                    "and a more capable model for complex analysis or reasoning."
                ),
            },
        },
        "required": ["name", "prompt", "description"],
    },
}

_AGENT_TASK_PARALLEL: ToolSpec = {
    "name": "agent_task_parallel",
    "description": (
        "Spawn multiple sub-agents to work on tasks in parallel. All sub-agents "
        "run concurrently and the tool returns when all have completed. Each task "
        "must have a unique 'id' so you can identify which result came from which "
        "task. Use this when you have multiple independent tasks that can run "
        "simultaneously, such as researching different topics, checking multiple "
        "data sources, or analyzing several files at once. Each sub-agent has "
        "access to the same read APIs and workspace files as you, but NOT to "
        "`create_action_request`, `wait_for_handles`, `set_conversation_name`, or "
        "the `agent_task*` spawners (top-level only). Do not delegate writes that "
        "need user approval (Slack/Telegram/Twitter sends, calendar invites, "
        "memory saves) to a sub-agent -- have the sub-agent draft the "
        "proposal and return it, then issue `create_action_request` yourself. "
        f"Maximum {MAX_PARALLEL_TASKS} tasks per call. If you need more, split them across "
        "sequential agent_task_parallel calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": f"Array of sub-agent task specifications to run in parallel (maximum {MAX_PARALLEL_TASKS}).",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "A unique identifier for this task in the batch. Used to tag the result so you can tell which response came from which task. Example: 'email-search', 'calendar-check'.",
                        },
                        "name": {
                            "type": "string",
                            "description": "A short display name for this sub-agent (shown to the user). Example: 'Email Researcher', 'Data Analyst'.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Detailed instructions for the sub-agent. Be specific about what you want it to accomplish and what information to include in its response.",
                        },
                        "description": {
                            "type": "string",
                            "description": "A brief description of what this sub-agent does (max 80 characters, shown in the UI). Example: 'Searching for unread emails from Alice'.",
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Optional: the model for this sub-agent to use. "
                                "If omitted, the sub-agent uses the same model as you. "
                                "Valid values: 'gemini-3.5-flash-lite', 'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash', 'claude-haiku-4.5', 'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-opus-4-7', 'claude-opus-4-8', 'claude-sonnet-5', 'claude-sonnet-5-5', 'claude-opus-5', 'claude-opus-5-5'. "
                                "Gemini 3.1 Pro ('gemini-3.1-pro-preview') is NOT available to sub-agents. "
                                "Use a faster/cheaper model for simple tasks like data retrieval."
                            ),
                        },
                    },
                    "required": ["id", "name", "prompt", "description"],
                },
            },
            "intent_message": {
                "type": "string",
                "description": "A brief, user-friendly summary of your intent for running these parallel tasks (max 50 characters). Example: 'Research emails and calendar'.",
            },
        },
        "required": ["tasks"],
    },
}

_AGENT_TASK_PARALLEL_TEMPLATE: ToolSpec = {
    "name": "agent_task_parallel_template",
    "description": (
        "Batch-spawn sub-agents from a single prompt template. The prompt_template "
        "uses {var}-style placeholders that are filled from each agent's variable "
        "dict. The model is set once for the entire batch and must be a cheaper model "
        "(claude-haiku-4.5, claude-sonnet-4-6, gemini-3.5-flash-lite, gemini-3.6-flash, gemini-3.7-flash, or gemini-3.8-flash). "
        "Each agent dict must include 'name' plus any template variables. "
        f"Maximum {MAX_PARALLEL_TEMPLATE_TASKS} agents per call. Use this instead of "
        "agent_task_parallel when all sub-agents share the same prompt structure but "
        "differ only in specific parameters -- it saves output tokens by avoiding "
        "prompt repetition. As with `agent_task` / `agent_task_parallel`, the spawned "
        "sub-agents do NOT have `create_action_request`, `wait_for_handles`, "
        "`set_conversation_name`, or the `agent_task*` spawners -- do not template "
        "writes that need user approval; have the sub-agents return drafts and you "
        "issue the action requests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt_template": {
                "type": "string",
                "description": (
                    "A prompt template with {var}-style placeholders that will be "
                    "formatted using Python's str.format() with each variable dict. "
                    "Example: \"Look up the email from {sender} about {topic} and "
                    "summarize the key points.\""
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "The model for ALL sub-agents in this batch. Required. Must be "
                    "one of: 'claude-haiku-4.5', 'claude-sonnet-4-6', 'gemini-3.5-flash-lite', "
                    "'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash'. Template batching is designed "
                    "for high-volume tasks using cheaper models."
                ),
                "enum": sorted(TEMPLATE_BATCH_ALLOWED_MODELS),
            },
            "agents": {
                "type": "array",
                "description": (
                    f"Array of variable dictionaries (maximum {MAX_PARALLEL_TEMPLATE_TASKS}). "
                    "Each dict must contain a 'name' key (short display name for the "
                    "sub-agent) and may contain any other keys matching the "
                    "placeholders in prompt_template."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "A short display name for this sub-agent (shown to the "
                                "user). Example: 'Email Lookup - Alice'."
                            ),
                        },
                    },
                    "required": ["name"],
                },
            },
            "intent_message": {
                "type": "string",
                "description": (
                    "A brief, user-friendly summary of your intent for running "
                    "these parallel tasks (max 50 characters)."
                ),
            },
        },
        "required": ["prompt_template", "model", "agents"],
    },
}

_AGENT_TASK_NESTED: ToolSpec = {
    "name": "agent_task_nested",
    "description": (
        "Spawn a single 2nd-level (nested) sub-agent to handle a small, cheap "
        "leaf task on your behalf. This tool is ONLY available to 1st-level "
        "sub-agents when the conversation has the `nested_subagents` flag "
        "enabled. The nested sub-agent has the same read APIs and workspace "
        "access as you, but it CANNOT spawn any further sub-agents and CANNOT "
        "issue action requests -- it is a leaf agent. Use it sparingly for "
        "cheap, well-scoped work (counting, retrieval, simple distillation) "
        "that you want to fan out without filling your own context window. "
        "The nested sub-agent returns its findings as text. The `model` "
        "parameter is REQUIRED and must be one of "
        "'claude-haiku-4.5' or 'gemini-3.5-flash-lite' (the only "
        "models permitted for 2nd-level sub-agents)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A short display name for this nested sub-agent. Example: 'Page Counter', 'Row Distiller'.",
            },
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the nested sub-agent. Be specific about the task and what to include in its response.",
            },
            "description": {
                "type": "string",
                "description": "A brief description of what this nested sub-agent does (max 80 characters, shown in the UI).",
            },
            "model": {
                "type": "string",
                "description": (
                    "REQUIRED: the model for the nested (2nd-level) sub-agent. "
                    "Must be one of 'claude-haiku-4.5' (Claude Haiku) or "
                    "'gemini-3.5-flash-lite' (Gemini Flash-Lite). No "
                    "other model is permitted for 2nd-level sub-agents."
                ),
                "enum": sorted(NESTED_SUB_AGENT_ALLOWED_MODELS),
            },
        },
        "required": ["name", "prompt", "description", "model"],
    },
}

# The request_type enum for create_action_request. A plain mutable list so
# plugin-registered action-request types (registered at startup, before any
# conversation runs) extend the schema in place -- _CREATE_ACTION_REQUEST
# holds a reference to this exact list, and the provider converters
# deep-copy per call, so mutations here are picked up everywhere.
ACTION_REQUEST_TYPE_ENUM: list[str] = [
    e.value for e in ActionRequestType
    if e is not ActionRequestType.SUBAGENT_RETURN
]


def register_action_request_type(type_name: str) -> None:
    """Admit a plugin-registered action-request type string into the
    create_action_request schema enum.

    Core types come from the db.models.ActionRequestType StrEnum; plugin
    types are plain ``<plugin id>_``-prefixed strings. Duplicate
    registration raises so a plugin can never shadow a core type.
    """
    if type_name in ACTION_REQUEST_TYPE_ENUM:
        raise ValueError(f"Action request type already registered: {type_name!r}")
    if type_name == ActionRequestType.SUBAGENT_RETURN.value:
        raise ValueError("subagent_return is reserved")
    ACTION_REQUEST_TYPE_ENUM.append(type_name)


# Names contributed through register_tool_call_tool() (plugins). Lets the
# core-tool classification test tell core registry entries from plugin ones.
PLUGIN_TOOL_NAMES: set[str] = set()


def register_tool_call_tool(spec: ToolSpec) -> None:
    """Register a plugin-contributed dynamic tool into TOOL_CALL_REGISTRY.

    The spec's name becomes callable via ``tool_call`` (dispatch needs a
    matching handler -- see
    chat.gemini_api.tool_dispatch.register_tool_call_handler) and is
    enumerated in the system prompt's Dynamic Tools section, subject to the
    optional ``requires_service`` gate on the spec.
    """
    name = spec.get("name", "")
    if not name:
        raise ValueError("Tool spec must have a non-empty 'name'")
    if name in TOOL_CALL_REGISTRY:
        raise ValueError(f"Tool already registered: {name!r}")
    if not isinstance(spec.get("parameters"), dict):
        raise ValueError(f"Tool {name!r} spec must have a 'parameters' object")
    TOOL_CALL_REGISTRY[name] = spec
    PLUGIN_TOOL_NAMES.add(name)


_CREATE_ACTION_REQUEST: ToolSpec = {
    "name": "create_action_request",
    "description": (
        "Propose a write operation against an external service (send a "
        "message, create/edit a record, schedule an event, etc.). The "
        "request is shown inline in the conversation with Approve / "
        "Revise / Stop buttons so the user can confirm before it is "
        "executed -- nothing happens externally until the user approves. "
        "\n\n"
        "This call BLOCKS until the user resolves the request. The "
        "return value carries the verdict directly so you can keep "
        "going on the same turn (no need to call `wait_for_handles` "
        "afterwards). The shape is "
        "`{verdict: \"executed\"|\"denied\"|\"stopped\", request_id: N, "
        "feedback?: \"<user text>\", result: {...}}`: on `executed`, "
        "`result` carries the backend's return value (e.g. a Slack `ts` "
        "or a calendar event id); on `denied`, "
        "`result` is `{\"denied\": true}` plus an optional `feedback` "
        "field if the user clicked Revise and supplied a reason; on "
        "`stopped`, the user pressed Stop, which halted the conversation "
        "-- the call only returns once they send a new message, which "
        "arrives in the same turn as the current instruction (a `note` "
        "field says so). "
        "\n\n"
        "On a deny with feedback, read `feedback` directly off this "
        "tool's return value (the same string is also mirrored inside "
        "`result.feedback`) and adapt your next move -- apologise, "
        "propose an alternative, or fix the parameters and re-issue. "
        "On `stopped`, do not re-issue the request unless the new "
        "message asks for it. "
        "See `system:action_requests` for the full response shape. "
        "\n\n"
        "Use this for any write that touches a connected service (Slack, "
        "Telegram, Twitter/X, Google Calendar, ...). Do NOT call write "
        "endpoints directly with `curl_proxy_post`. "
        "\n\n"
        "This tool is top-level only and is not available inside sub-agents "
        "(`agent_task` / `agent_task_parallel` / "
        "`agent_task_parallel_template`). Sub-agents that need a write "
        "should return the proposed `request_type` and `params` to the "
        "parent via `agent_task_response` so the parent can issue the "
        "action request. "
        "\n\n"
        "The full list of supported `request_type` values and their exact "
        "`params` shapes lives in each backend's system skill -- e.g. Slack "
        "sends in `system:slack`, calendar "
        "invites in `system:calendar`, Telegram in `system:telegram`, "
        "Twitter DMs in `system:twitter`, memory writes "
        "(`request_type=\"create_memory\"`, `params={\"content\": \"...\"}`) "
        "in `system:memory`, skill create/edit in `system:skill_management`, "
        "routine create/edit (`create_routine` / `edit_routine`) in "
        "`system:routines`, "
        "cross-user subagent runs (`request_type=\"run_user_subagent\"`) in "
        "`system:user_subagents`, shared Quest Doc changes (`write_doc`) in "
        "`system:quest_docs`. "
        "Before calling this tool for a given backend, "
        "load that backend's `system:<name>` skill so you have the correct "
        "request_type and parameter names. "
        "`system:action_requests` covers the generic approval / cancel / "
        "retry mechanics that apply to all types."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request_type": {
                "type": "string",
                "description": (
                    "The type of action request. The enum lists every "
                    "supported value, but the expected `params` for each "
                    "type are documented in the corresponding backend "
                    "system skill (load `system:<backend>` first)."
                ),
                # subagent_return is excluded: it is created only by the
                # return_to_caller dispatch arm inside cross-user subagent
                # conversations, never via this generic tool.
                # ACTION_REQUEST_TYPE_ENUM is shared by reference so types
                # registered by plugins at startup appear here too.
                "enum": ACTION_REQUEST_TYPE_ENUM,
            },
            "params": {
                "type": "object",
                "description": (
                    "Type-specific parameters as a JSON object. The exact "
                    "required / optional keys depend on `request_type` and "
                    "are documented in the backend's `system:<name>` skill "
                    "(e.g. `system:slack` for Slack sends, `system:calendar` "
                    "for calendar invites). Load that skill before calling this "
                    "tool if you haven't already."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "Explain why you are making this request. This reasoning is shown "
                    "to the user alongside the request to help them decide whether to approve it."
                ),
            },
            "intent_message": {
                "type": "string",
                "description": (
                    "A brief, user-friendly summary of your intent "
                    "(max 50 characters). Example: 'Send DM to Alice'."
                ),
            },
        },
        "required": ["request_type", "params", "reasoning"],
    },
}

# ---------------------------------------------------------------------------
# Cross-user subagent conversations
# ---------------------------------------------------------------------------

_RETURN_TO_CALLER: ToolSpec = {
    "name": "return_to_caller",
    "description": (
        "Propose returning your findings to the calling user. You are a "
        "subagent running inside this account's owner (the target user) on "
        "behalf of another user; NOTHING you gather leaves this account "
        "until the target user approves this return call. The tool shows "
        "the target user an approval card with your full response text and "
        "the exact list of workspace files you want to send back, then "
        "BLOCKS until they resolve it. On approve, the files are copied "
        "into the calling conversation's workspace and your response is "
        "delivered -- your task is then complete. On revise, the return "
        "value carries the target user's feedback (`{verdict: \"denied\", "
        "feedback: \"...\"}`) -- address it and call return_to_caller "
        "again. On deny (no feedback), the run ends immediately. Call this "
        "EXACTLY ONCE when your task is done; put everything the caller "
        "needs in `response`, and list any supporting workspace files "
        "(reports, extracts, data files you wrote) in `files`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "response": {
                "type": "string",
                "description": (
                    "Your complete findings to deliver to the calling "
                    "user. This text is shown to the target user for "
                    "approval first."
                ),
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of workspace-relative file paths (max "
                    "10, 50 MB each) to copy into the calling "
                    "conversation's workspace on approval. Only files "
                    "that exist in this conversation's workspace are "
                    "accepted."
                ),
            },
            "intent_message": {
                "type": "string",
                "description": (
                    "A brief, user-friendly summary of your intent "
                    "(max 50 characters). Example: 'Return findings'."
                ),
            },
        },
        "required": ["response"],
    },
}

# ---------------------------------------------------------------------------
# Inference API conversations
# ---------------------------------------------------------------------------

_RETURN_FINAL_RESPONSE: ToolSpec = {
    "name": "return_final_response",
    "description": (
        "Deliver your complete final answer to the calling application and "
        "end this run. You are running headlessly behind a one-shot API "
        "call: nothing you write as assistant text reaches the caller -- "
        "the ONLY output channel is this tool's `response` parameter. Call "
        "it EXACTLY ONCE, when your task is fully complete, with the entire "
        "final answer formatted as GitHub-flavored markdown. The run ends "
        "immediately after this call; there are no follow-up turns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "response": {
                "type": "string",
                "description": (
                    "The complete final answer in GitHub-flavored "
                    "markdown. This is delivered verbatim to the calling "
                    "application; include everything the caller needs."
                ),
            },
        },
        "required": ["response"],
    },
}

# ---------------------------------------------------------------------------
# Slack-driven conversations
# ---------------------------------------------------------------------------

_SEND_SLACK_REPLY: ToolSpec = {
    "name": "send_slack_reply_and_get_response",
    "description": (
        "Post YOUR FINAL REPLY to the Slack thread you are speaking in and wait for "
        "the user's next reply. Call this EXACTLY ONCE per turn -- after you have "
        "completed all the tool calls and thinking you need for the current user "
        "message. The tool posts the given text as a threaded reply in the Slack DM, "
        "then blocks until the user sends their next reply in the same thread. The "
        "returned string is the user's next message (multiple rapid messages are "
        "collated). Format the text using Slack's mrkdwn dialect: *bold*, _italic_, "
        "`inline code`, ```multi-line code blocks```, and <https://url|label> for "
        "links. Do NOT use GitHub-flavored markdown (**bold**, [text](url)) -- it "
        "will not render correctly in Slack."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "The full reply text to post into the Slack thread. Use Slack "
                    "mrkdwn. Must be under 3000 characters."
                ),
            },
        },
        "required": ["text"],
    },
}


# ---------------------------------------------------------------------------
# Tool tiers
# ---------------------------------------------------------------------------

SUB_AGENT_TOOLS: list[ToolSpec] = BASE_TOOLS + [_AGENT_TASK_RESPONSE]

# Tier for 1st-level sub-agents when the ``nested_subagents`` flag is on: the
# normal sub-agent tools plus a single nested-spawn tool (agent_task_nested).
# 2nd-level sub-agents always use SUB_AGENT_TOOLS (no spawner) -- there is no
# 3rd level.
SUB_AGENT_TOOLS_NESTED: list[ToolSpec] = SUB_AGENT_TOOLS + [_AGENT_TASK_NESTED]

TOP_LEVEL_TOOLS: list[ToolSpec] = BASE_TOOLS + [
    _AGENT_TASK,
    _AGENT_TASK_PARALLEL,
    _AGENT_TASK_PARALLEL_TEMPLATE,
    _CREATE_ACTION_REQUEST,
]

SLACK_TOP_LEVEL_TOOLS: list[ToolSpec] = TOP_LEVEL_TOOLS + [_SEND_SLACK_REPLY]

# Cross-user subagent conversations (origin="user_subagent"): the base
# read/workspace/skill tools plus return_to_caller -- deliberately NO
# create_action_request (the target user cannot approve writes proposed by
# someone else's agent) and NO agent_task* spawners.
USER_SUBAGENT_TOOLS: list[ToolSpec] = BASE_TOOLS + [_RETURN_TO_CALLER]

# One-shot inference API conversations (origin="inference_api"): the base
# read/workspace/skill tools plus return_final_response -- NO
# create_action_request (there is no user watching to approve a card; the
# run is a single blocking HTTP request) and NO agent_task* spawners.
# Inference runs must not change anything outside their own workspace:
# the approval-free mutating dynamic tools (mutating_tool_call_tools())
# and the mutating internal proxy paths (MUTATING_PROXY_PATHS) are
# additionally hard-rejected at dispatch time (is_inference_api in
# chat/gemini_api/tool_dispatch.py + chat/route_dispatch.py), in the
# sandbox-script bridge (restricted sandbox leases,
# chat/sandbox_tokens.py), and hidden from the inference system prompt.
INFERENCE_API_TOOLS: list[ToolSpec] = BASE_TOOLS + [_RETURN_FINAL_RESPONSE]


def mutating_tool_call_tools() -> frozenset[str]:
    """Names of the tool_call-routed tools that change external state.

    Read from the live registry (plugins register at startup), so the
    result reflects every loaded plugin's ``PluginTool.mutating`` flag as
    well as the core specs' ``"mutating": True`` key.
    """
    return frozenset(
        name for name, spec in TOOL_CALL_REGISTRY.items() if spec.get("mutating")
    )


# Internal /api/* routes reachable through curl_proxy_post (main app) or
# from a sandbox container (sandbox tool API) that change state. Blocked
# for inference API runs alongside the mutating dynamic tools; the raw
# Gmail batch endpoint is GET-only inside and stays reachable.
MUTATING_PROXY_PATHS: frozenset[str] = frozenset({
    "/api/gmail-simple/drafts",
    "/api/gmail-simple/send-self",
    "/api/reset-api-key",
})

# Public-project conversations: internet-enabled sandbox, cut off from every
# internal resource. Only tool_call (restricted to
# PUBLIC_TOOL_CALL_ALLOWLIST), run_script, and run_python -- NO curl_proxy_*
# (internal proxy), NO skill tools, NO load_gmail_attachment, NO agent_task*
# spawners, NO create_action_request. The schema-level trim is convenience;
# the enforced boundary is the allowlist check in
# chat/gemini_api/tool_dispatch.py plus the loop-arm rejects in
# chat/gemini_api/conversation.py.
_PUBLIC_BASE_TOOL_NAMES = {"tool_call", "run_script", "run_python"}
PUBLIC_TOOLS: list[ToolSpec] = [
    t for t in BASE_TOOLS if t["name"] in _PUBLIC_BASE_TOOL_NAMES
]

# Dynamic (tool_call-routed) tools available in public-project
# conversations: time, workspace files, conversation naming, large-response
# paging, the project-local database, and send_slack_dm_to_self -- the one
# connector write allowed here because it is outbound-only to the user
# themselves (fixed recipient, no message history or internal reads; it
# only sends text the model composed plus workspace files, which are
# already public-project accessible). send_slack_dm_to_self is served by
# the in-tree Slack plugin; its presence here rides on the core-owned
# _PUBLIC_ALLOWLIST_MIGRATED_TOOLS exemption in config/plugins.py (the
# allowlist itself stays core-only -- plugins cannot extend it).
# The seven Quest Docs tools are allowed too: docs are mode-partitioned,
# and the access rule (chat/docs/access.py, resolve_doc_access with
# is_public=True) hides every private doc from a public conversation --
# it behaves exactly like a nonexistent id -- and lets it create and write
# only public docs, which hold sandbox-originated content by construction.
# Everything else in TOOL_CALL_REGISTRY reads internal data (connectors,
# memories, authed APIs) and is hard-rejected at dispatch time.
PUBLIC_TOOL_CALL_ALLOWLIST: frozenset[str] = frozenset({
    "get_current_time",
    "list_workspace_files",
    "get_workspace_file",
    "write_workspace_file",
    "edit_workspace_file",
    "set_conversation_name",
    "get_response_content",
    "project_db_query",
    "send_slack_dm_to_self",
    "list_docs",
    "search_docs",
    "read_doc",
    "create_doc",
    "edit_doc",
    "append_to_doc",
    "add_doc_image",
})


# ---------------------------------------------------------------------------
# Provider-specific description resolution
# ---------------------------------------------------------------------------


def _apply_provider_descriptions(tools: list[ToolSpec], provider: str) -> list[ToolSpec]:
    """Return a copy of *tools* with provider-specific descriptions applied.

    For each tool that has a ``provider_descriptions`` dict, the matching
    provider entry replaces the generic ``description``.  The
    ``provider_descriptions`` key is then removed so downstream converters
    only see a flat ``description``.

    Deep-copies the list so the canonical BASE_TOOLS / TOP_LEVEL_TOOLS /
    SUB_AGENT_TOOLS are never mutated.

    Args:
        tools: List of ToolSpec dicts.
        provider: Provider key -- ``"gemini"`` or ``"anthropic"``.

    Returns:
        New list of ToolSpec dicts with resolved descriptions.
    """
    tools = copy.deepcopy(tools)
    for tool in tools:
        overrides = tool.pop("provider_descriptions", None)
        if overrides and provider in overrides:
            tool["description"] = overrides[provider]
    return tools


# ---------------------------------------------------------------------------
# Converter functions
# ---------------------------------------------------------------------------

def _json_schema_type_to_gemini(json_type: str) -> str:
    """Map JSON Schema type strings to Gemini types.Schema type strings."""
    mapping = {
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
        "object": "OBJECT",
        "array": "ARRAY",
    }
    return mapping.get(json_type, "STRING")


def _json_schema_to_gemini_schema(schema: dict) -> "types.Schema":
    """Convert a JSON Schema dict to a Gemini types.Schema object."""
    from google.genai import types

    kwargs: dict = {"type": _json_schema_type_to_gemini(schema.get("type", "string"))}

    if "description" in schema:
        kwargs["description"] = schema["description"]

    if "enum" in schema:
        kwargs["enum"] = schema["enum"]

    if "properties" in schema:
        kwargs["properties"] = {
            name: _json_schema_to_gemini_schema(prop_schema)
            for name, prop_schema in schema["properties"].items()
        }

    if "required" in schema:
        kwargs["required"] = schema["required"]

    if "items" in schema:
        kwargs["items"] = _json_schema_to_gemini_schema(schema["items"])

    return types.Schema(**kwargs)


def to_gemini_declarations(tools: list[ToolSpec]) -> list:
    """Convert canonical tool specs to Gemini FunctionDeclaration objects.

    Args:
        tools: List of ToolSpec dicts in JSON Schema format.

    Returns:
        List of types.FunctionDeclaration objects for the Gemini SDK.
    """
    from google.genai import types

    tools = _apply_provider_descriptions(tools, "gemini")
    declarations = []
    for tool in tools:
        params = tool.get("parameters", {})
        declarations.append(
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=_json_schema_to_gemini_schema(params),
            )
        )
    return declarations


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict]:
    """Convert canonical tool specs to Anthropic tool definition dicts.

    Args:
        tools: List of ToolSpec dicts in JSON Schema format.

    Returns:
        List of Anthropic tool definition dicts with name, description,
        and input_schema keys.
    """
    tools = _apply_provider_descriptions(tools, "anthropic")
    anthropic_tools = []
    for tool in tools:
        anthropic_tools.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
        })
    return anthropic_tools


def to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    """Convert canonical tool specs to OpenAI function-tool definition dicts.

    Used by the OpenRouter provider (OpenAI-compatible chat completions).
    Provider-specific description overrides use the ``"openai"`` key of
    ``provider_descriptions``.

    Args:
        tools: List of ToolSpec dicts in JSON Schema format.

    Returns:
        List of ``{"type": "function", "function": {...}}`` dicts.
    """
    tools = _apply_provider_descriptions(tools, "openai")
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            },
        })
    return openai_tools
