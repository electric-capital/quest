"""Shared model-facing wording for the two file spaces of a project conversation.

Dependency-free on purpose: imported by the tool schemas
(chat/llm/tool_schemas.py), the space-path parser
(chat/gemini_api/tool_handlers/file_paths.py, which re-exports both
constants) and the handlers' not-found errors, without an import cycle.
"""

# Schema / skill sentence for tools that take conversation-workspace paths
# only (attachments, run_script, add_doc_image, return_to_caller, ...).
PROJECT_COPY_FIRST_SENTENCE = (
    "Project files must be copied into this conversation's workspace first "
    "(in a project conversation: `copy_file` from `proj://` to `chat://`)."
)

# Suffix of such a tool's not-found error in a project conversation.
PROJECT_COPY_FIRST_SUFFIX = (
    " (paths are in this conversation's workspace; copy project files in "
    "first with copy_file from proj:// to chat://)"
)
