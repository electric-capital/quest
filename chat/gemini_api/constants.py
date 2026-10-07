"""Constants and configuration values for the Gemini API integration."""

import os

# Deprecated model IDs mapped to their replacements.
_DEPRECATED_MODELS: dict[str, str] = {
    "gemini-3-pro-preview": "gemini-3.1-pro-preview",
}

# ---------------------------------------------------------------------------
# Workspace file helpers
# ---------------------------------------------------------------------------

# Thresholds for inline vs upload
_TEXT_INLINE_LIMIT = 100 * 1024  # 100 KB - text files under this are returned inline
_TEXT_EXTENSIONS = {
    '.txt', '.md', '.csv', '.json', '.xml', '.yaml', '.yml', '.toml',
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.scss',
    '.sh', '.bash', '.zsh', '.fish',
    '.sql', '.graphql', '.gql',
    '.rs', '.go', '.java', '.kt', '.swift', '.c', '.cpp', '.h', '.hpp',
    '.rb', '.php', '.pl', '.r', '.lua', '.ex', '.exs',
    '.env', '.gitignore', '.dockerignore', '.editorconfig',
    '.ini', '.cfg', '.conf', '.properties',
    '.log', '.diff', '.patch',
}
_TEXT_MIME_PREFIXES = ('text/',)

# MIME types known to be rejected by the Gemini content generation API when
# included via Part.from_uri.  The File Upload API accepts these files, but
# send_message_stream returns 400 INVALID_ARGUMENT.  We check this list
# *before* uploading to avoid a wasted upload + a conversation-killing error.
_UNSUPPORTED_GEMINI_MIME_TYPES = {
    # Microsoft Office / OpenXML
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',    # .docx
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',          # .xlsx
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',  # .pptx
    'application/msword',                                                          # .doc
    'application/vnd.ms-excel',                                                    # .xls
    'application/vnd.ms-powerpoint',                                               # .ppt
    # OpenDocument
    'application/vnd.oasis.opendocument.text',                                     # .odt
    'application/vnd.oasis.opendocument.spreadsheet',                              # .ods
    'application/vnd.oasis.opendocument.presentation',                             # .odp
    # Archives / compressed
    'application/zip',
    'application/x-zip-compressed',
    'application/x-rar-compressed',
    'application/x-7z-compressed',
    'application/gzip',
    'application/x-tar',
    # Executables / binaries
    'application/x-executable',
    'application/x-mach-binary',
    'application/x-dosexec',
    'application/vnd.microsoft.portable-executable',
}

# Maximum file size for LLM-written files (1MB)
_WRITE_FILE_MAX_SIZE = 1 * 1024 * 1024  # 1 MB

# ---------------------------------------------------------------------------
# Large response protection (authed_get size gate)
# ---------------------------------------------------------------------------

# Default size gate threshold: responses larger than this are rejected unless
# the caller explicitly passes force_large_response=True.
AUTHED_GET_SIZE_LIMIT = 3 * 1024  # 3 KB

# Maximum chunk size for the get_response_content tool.
GET_RESPONSE_CONTENT_MAX_CHUNK = 5 * 1024  # 5 KB (characters)

# Subdirectory name within the conversation directory for response blob files.
_RESPONSE_BLOB_DIR = "responses"

# SDK history persistence
_SDK_HISTORY_FILENAME = "sdk_history.json"

# ---------------------------------------------------------------------------
# Sub-agent limits
# ---------------------------------------------------------------------------

MAX_PARALLEL_TASKS = 10  # Maximum number of parallel sub-agents
MAX_PARALLEL_TEMPLATE_TASKS = 20  # Maximum number of template-based parallel sub-agents
MAX_AGENT_TASKS_PER_TURN = 10  # Maximum number of agent_task calls in a single model response
MAX_SUB_AGENT_TURNS = 60  # Maximum number of model turns for a sub-agent
SUB_AGENT_TURN_WARNING_THRESHOLD = 50  # Warn model when this turn is reached
SUB_AGENT_CONTEXT_WARNING_THRESHOLD = 0.75  # Fraction of max context at which to warn (75%)

TEMPLATE_BATCH_ALLOWED_MODELS = {
    "claude-haiku-4.5",
    "claude-haiku-5-5",
    "claude-sonnet-4-6",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
}

# Models that may NOT be used by spawned sub-agents (agent_task /
# agent_task_parallel), whether chosen explicitly via the optional `model`
# parameter or inherited from the parent agent's model. Gemini 3.1 Pro is
# unreliable, expensive, and slow, so it is reserved for top-level
# (user-driven) conversations only. The check is applied after the
# _DEPRECATED_MODELS remap so the deprecated alias is also blocked.
# (agent_task_parallel_template already excludes it via
# TEMPLATE_BATCH_ALLOWED_MODELS, so no separate subtraction is needed there.)
SUB_AGENT_DISALLOWED_MODELS = {
    "gemini-3.1-pro-preview",
}

# Models a 2nd-level (nested) sub-agent may use, when the per-conversation
# ``nested_subagents`` flag is enabled. 2nd-level sub-agents are restricted to
# the cheapest/fastest models -- Claude Haiku and Gemini Flash Lite -- for
# cheap leaf work (counting, retrieval, simple distillation), and they cannot
# spawn any further sub-agents. Enforced both in the nested-spawn dispatch
# branch and as a defense-in-depth guard at the top of _run_sub_agent(level=2).
# (gemini-3.1-pro-preview stays excluded here, consistent with
# SUB_AGENT_DISALLOWED_MODELS.)
NESTED_SUB_AGENT_ALLOWED_MODELS = {
    "claude-haiku-4.5",
    "claude-haiku-5-5",
    "gemini-3.5-flash-lite",
}


def get_sub_agent_turn_limits(model: str) -> tuple[int, int]:
    """Return (max_turns, warning_threshold) based on the model's token capacity.

    Models with <= 200K token context windows get a lower turn limit (20 turns,
    warning at 15) because they fill their context much faster than 1M token
    models. Unknown models fall back to the large-model defaults (60/50).
    """
    from chat.llm.config import get_max_input_tokens

    max_input = get_max_input_tokens(model)
    if 0 < max_input <= 200_000:
        return (20, 15)
    return (MAX_SUB_AGENT_TURNS, SUB_AGENT_TURN_WARNING_THRESHOLD)

# ---------------------------------------------------------------------------
# Script executor (run_script tool)
# ---------------------------------------------------------------------------

_SCRIPT_RUNNER_IMAGE_BASE = "quest-script-runner"  # Podman image base name

# Public-project sandbox image: a SEPARATE image (own Dockerfile + own
# entrypoint) rather than a mode switch on the restricted image, so the
# tooling inside each can diverge independently. The public entrypoint
# opens internet egress but blocks RFC1918 / link-local destinations and
# never runs the loopback proxy bridge.
_PUBLIC_SCRIPT_RUNNER_IMAGE_BASE = "quest-script-runner-public"


def get_script_runner_image() -> str:
    """Return the Podman script-runner image name with environment suffix.

    Returns ``quest-script-runner-<mode>`` where mode is the canonical run
    mode (``local``/``staging``/``prod``) from config.environment.
    """
    from config import environment
    return f"{_SCRIPT_RUNNER_IMAGE_BASE}-{environment.image_suffix()}"


def get_public_script_runner_image() -> str:
    """Return the internet-enabled public-project sandbox image name.

    Returns ``quest-script-runner-public-<mode>``; built from
    ``Dockerfile.script-runner-public``.
    """
    from config import environment
    return f"{_PUBLIC_SCRIPT_RUNNER_IMAGE_BASE}-{environment.image_suffix()}"

SCRIPT_RUNNER_TIMEOUT = 60  # Default execution timeout in seconds
SCRIPT_RUNNER_MAX_TIMEOUT = 150  # Maximum allowed timeout (2.5 minutes)
SCRIPT_RUNNER_MAX_OUTPUT = 256 * 1024  # Maximum stdout/stderr capture size (256KB)


def get_proxy_port() -> str:
    """Return the quest API proxy port from the QUEST_PORT env var.

    Falls back to deriving the port from the run mode (9000 for local,
    8000 for staging/prod), and defaults to "8000" if neither variable
    is set.
    """
    port = os.environ.get("QUEST_PORT")
    if port:
        return port
    from config import environment
    return "9000" if environment.is_local() else "8000"


def get_proxy_base_url() -> str:
    """Return the quest API proxy base URL (e.g. http://localhost:8000)."""
    return f"http://localhost:{get_proxy_port()}"


def get_sandbox_port() -> str:
    """Return the sandbox tool API port from the QUEST_SANDBOX_PORT env var.

    This is the loopback-only port served by chat/sandbox_api.py that
    exposes just the script-facing endpoints; script containers get it
    injected as their QUEST_PORT. Defaults to the main server port + 1.
    """
    port = os.environ.get("QUEST_SANDBOX_PORT")
    if port:
        return port
    return str(int(get_proxy_port()) + 1)
