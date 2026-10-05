"""Local tool handler functions for the LLM integration.

One module per tool domain; ``tool_dispatch.py`` imports the handlers by
name from this package. Callers outside the package (authed_get, Gmail
drafts, plugin file-download tools) import the shared workspace helpers
from here too, so every handler and helper is re-exported below.

Tests that patch a helper must target the module that *uses* it (e.g.
``chat.gemini_api.tool_handlers.workspace._get_workspace_dir``), not this
package -- the re-export is a separate binding.
"""

from chat.gemini_api.tool_handlers._common import (
    _get_workspace_dir,
    _publish_file_list_changed,
    _parse_content_disposition_filename,
    _sanitize_workspace_filename,
)
from chat.gemini_api.tool_handlers.misc import (
    _handle_get_current_time,
    _handle_set_conversation_name,
)
from chat.gemini_api.tool_handlers.workspace import (
    _handle_list_workspace_files,
    _handle_get_workspace_file,
    _handle_load_gmail_attachment,
    _handle_write_workspace_file,
    _handle_edit_workspace_file,
)
from chat.gemini_api.tool_handlers.drive import (
    GOOGLE_DOC_EXPORT_FORMATS,
    _resolve_doc_export_format,
    _handle_download_drive_file,
    _handle_google_export_doc,
)
from chat.gemini_api.tool_handlers.gmail_labels import (
    _handle_archive_gmail_message,
    _handle_list_gmail_quest_labels,
    _handle_modify_gmail_labels,
)
from chat.gemini_api.tool_handlers.gmail_simple import (
    _run_gmail_simple_endpoint,
    _handle_get_gmail_messages,
    _handle_list_gmail_labels,
    _handle_get_gmail_message_urls,
    _handle_create_gmail_draft,
    _handle_send_gmail_to_self,
)
from chat.gemini_api.tool_handlers.memory import (
    _handle_memory_search,
    _handle_memory_list,
)
from chat.gemini_api.tool_handlers.skills import (
    _handle_list_skills,
    _handle_search_skills,
    _handle_load_skills,
    _handle_list_my_skills,
    _handle_get_skill,
)
from chat.gemini_api.tool_handlers.routines import _handle_list_routines
from chat.gemini_api.tool_handlers.sandbox import (
    _build_script_podman_cmd,
    _handle_run_script,
    _handle_run_python,
)
from chat.gemini_api.tool_handlers.project_db import (
    _execute_project_db_query,
    _handle_project_db_query,
)
from chat.gemini_api.tool_handlers.response_blobs import _handle_get_response_content
from chat.gemini_api.tool_handlers.docs import (
    _handle_list_docs,
    _handle_search_docs,
    _handle_read_doc,
    _handle_create_doc,
    _handle_edit_doc,
    _handle_append_to_doc,
    _handle_add_doc_image,
)

__all__ = [
    "_get_workspace_dir",
    "_publish_file_list_changed",
    "_parse_content_disposition_filename",
    "_sanitize_workspace_filename",
    "_handle_get_current_time",
    "_handle_set_conversation_name",
    "_handle_list_workspace_files",
    "_handle_get_workspace_file",
    "_handle_load_gmail_attachment",
    "_handle_write_workspace_file",
    "_handle_edit_workspace_file",
    "GOOGLE_DOC_EXPORT_FORMATS",
    "_resolve_doc_export_format",
    "_handle_download_drive_file",
    "_handle_google_export_doc",
    "_handle_archive_gmail_message",
    "_handle_list_gmail_quest_labels",
    "_handle_modify_gmail_labels",
    "_run_gmail_simple_endpoint",
    "_handle_get_gmail_messages",
    "_handle_list_gmail_labels",
    "_handle_get_gmail_message_urls",
    "_handle_create_gmail_draft",
    "_handle_send_gmail_to_self",
    "_handle_memory_search",
    "_handle_memory_list",
    "_handle_list_skills",
    "_handle_search_skills",
    "_handle_load_skills",
    "_handle_list_my_skills",
    "_handle_get_skill",
    "_handle_list_routines",
    "_build_script_podman_cmd",
    "_handle_run_script",
    "_handle_run_python",
    "_execute_project_db_query",
    "_handle_project_db_query",
    "_handle_get_response_content",
    "_handle_list_docs",
    "_handle_search_docs",
    "_handle_read_doc",
    "_handle_create_doc",
    "_handle_edit_doc",
    "_handle_append_to_doc",
    "_handle_add_doc_image",
]
