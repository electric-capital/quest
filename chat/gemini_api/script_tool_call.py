"""HTTP bridge exposing a subset of the dynamic tools to sandbox scripts.

Code running in the ``run_script`` / ``run_python`` sandbox cannot use the
``tool_call`` meta tool -- it talks to Quest over the local API proxy with
``QUEST_API_KEY``. The ``POST /api/tool-call`` endpoint defined here lets
that code invoke an allow-listed subset of ``TOOL_CALL_REGISTRY`` tools with
the same ``{tool_name, arguments}`` shape the LLM uses, generalizing the
pattern set by ``POST /api/authed-get`` / ``POST /api/authed-post``.

Only tools that make sense outside the agent loop are exposed:

* No suspend/blocking tools (``wait_for_handles`` raises loop sentinels).
* No conversation/project-scoped tools (``set_conversation_name``,
  ``project_db_query``, ``get_response_content``, and the workspace-download
  tools need dispatch-context ids the endpoint does not have; scripts already
  have the conversation workspace mounted at ``/workspace``, so the workspace
  file tools are pointless here too). The project file and copy tools
  (``list_project_files``, ``get_project_file``, ``write_project_file``,
  ``edit_project_file``, ``copy_file_to_project``, ``copy_project_file``) are
  left out for the same reason: a project conversation's scripts have the
  project workspace mounted at ``/project`` and copy with ``shutil``.
* No provider-part tools (``get_workspace_file`` returns provider-specific
  attachment parts that have no HTTP representation).
* ``authed_get`` / ``authed_post`` keep their dedicated routes
  (``/api/authed-get`` handles binary downloads, which this bridge does not).

Tools run with ``conversation_id=None`` -- the sandbox API key is
user-scoped, so conversation-dependent conveniences (e.g. Gmail URL-identifier
caching) gracefully degrade exactly as they do on the direct HTTP routes.
"""

import json
import logging

from fastapi import Depends
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from auth.session import get_current_user_cookie_or_apikey
from chat.sandbox_tokens import SandboxLease, get_sandbox_lease

logger = logging.getLogger(__name__)

# Tools invocable from sandbox scripts via POST /api/tool-call. An explicit
# allow-list (not a deny-list) so newly registered tools stay script-invisible
# until deliberately added. The workspace and project file / copy tools are
# deliberately absent: scripts read and write /workspace (and /project in a
# project conversation) directly.
SCRIPT_TOOL_CALL_ALLOWLIST: frozenset[str] = frozenset({
    # The Slack tools (reads + send_slack_dm_to_self -- the bridge is the
    # script path to them; the old /api/slack-simple/dm-self HTTP route is
    # gone) are plugin-declared: the in-tree Slack plugin opts them in via
    # its script_tool_allowlist, unioned here by extend_script_allowlist().
    # Gmail Simple + Quest-managed labels (parity with the /api/gmail-simple/*
    # HTTP routes that already serve scripts)
    "get_gmail_messages",
    "list_gmail_labels",
    "get_gmail_message_urls",
    "create_gmail_draft",
    "send_gmail_to_self",
    "archive_gmail_message",
    "list_gmail_quest_labels",
    "modify_gmail_labels",
    # (The Telegram reads -- telegram_get_me, telegram_list_dialogs, ... --
    # are unioned in by the Telegram plugin's script_tool_allowlist.)
    # Memory reads (user-scoped)
    "memory_search",
    "memory_list",
    # Quest Docs reads. Scripts run with run_kind="script" (is_script below):
    # no conversation context, so only user docs are visible (project docs
    # are reachable only from their project's conversations), and every doc
    # write is refused by the access rule -- the write tools are not listed.
    "list_docs",
    "search_docs",
    "read_doc",
})


def extend_script_allowlist(names: "frozenset[str] | set[str]") -> None:
    """Union plugin-declared tool names into the script allowlist.

    Called by the plugin loader for manifests that opt specific plugin
    tools into sandbox-script invocation. Rebinds the module global; the
    endpoint below reads it by name so the extension takes effect
    immediately.
    """
    global SCRIPT_TOOL_CALL_ALLOWLIST
    SCRIPT_TOOL_CALL_ALLOWLIST = SCRIPT_TOOL_CALL_ALLOWLIST | frozenset(names)


class ScriptToolCallRequest(BaseModel):
    """Request body for POST /api/tool-call."""

    tool_name: str
    arguments: dict = Field(default_factory=dict)


async def script_tool_call_endpoint(
    body: ScriptToolCallRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey),
    lease: SandboxLease | None = Depends(get_sandbox_lease),
) -> Response:
    """Invoke an allow-listed dynamic tool on behalf of sandbox code.

    Accepts the same ``{tool_name, arguments}`` shape as the LLM's
    ``tool_call`` meta tool and delegates to the shared dispatch chain in
    ``chat/gemini_api/tool_dispatch.py``, so argument coercion, error
    conversion, and notices behave identically to agent-initiated calls.

    JSON tool results are returned as JSON; non-JSON results (e.g. the
    ``get_gmail_messages`` markdown document) are returned as text/plain.

    A container launched from an inference API run holds a lease with
    ``block_mutating_tools`` set: the mutating dynamic tools are refused
    here exactly as the run's own tool dispatch refuses them.
    """
    from chat.gemini_api.tool_dispatch import _dispatch_tool_call

    if body.tool_name not in SCRIPT_TOOL_CALL_ALLOWLIST:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"Tool '{body.tool_name}' is not available from scripts. "
                    "Available tools: "
                    + ", ".join(sorted(SCRIPT_TOOL_CALL_ALLOWLIST))
                    + "."
                ),
            },
        )

    # isinstance: direct (non-FastAPI) callers in tests leave the Depends
    # marker in place; only a real lease can carry the restriction.
    if isinstance(lease, SandboxLease) and lease.block_mutating_tools:
        from chat.llm.tool_schemas import mutating_tool_call_tools
        if body.tool_name in mutating_tool_call_tools():
            return JSONResponse(
                status_code=403,
                content={
                    "error": (
                        f"Tool '{body.tool_name}' is not available from this "
                        "container: it changes external state and the "
                        "launching inference API run is read-only."
                    ),
                },
            )

    # dict(...) copy: the dispatch chain pops intent_message from the inner
    # arguments in place.
    result_text, extra_parts = await _dispatch_tool_call(
        app=None,
        provider=None,
        user=user,
        conversation_id=None,
        timezone="UTC",
        tool_name="tool_call",
        args={"tool_name": body.tool_name, "arguments": dict(body.arguments)},
        is_script=True,
    )
    if extra_parts:
        # Allow-listed tools never produce provider parts; log if one appears
        # so a future allow-list addition that does is caught early.
        logger.warning(
            "[script_tool_call] Tool '%s' returned %d provider parts, dropped",
            body.tool_name, len(extra_parts),
        )

    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        return PlainTextResponse(result_text)
    return JSONResponse(content=parsed)
