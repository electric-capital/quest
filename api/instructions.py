"""Aggregation module for API instruction text.

Assembles the full instruction document from per-submodule instruction
functions and the shared preamble.  This module also provides
``get_user_connected_services`` which determines which services a user
has connected.
"""

import logging

from api.gmail import get_instructions as gmail_instructions
from api.calendar import get_instructions as calendar_instructions
from api.drive import get_instructions as drive_instructions
from api.docs import get_instructions as docs_instructions
from api.sheets import get_instructions as sheets_instructions
from api.slides import get_instructions as slides_instructions
from api.tasks import get_instructions as tasks_instructions
from api.airtable import get_instructions as airtable_instructions
from api.ramp import get_instructions as ramp_instructions
from chat.docs.constants import DOCS_SERVICE_KEY
from config.feature_gates import docs_enabled_for

logger = logging.getLogger(__name__)

# Keys of get_user_connected_services() that are capabilities, not
# connectors: they gate tools (``requires_service``) and system skills
# (``SystemSkill.requires``) like a connected service does, but nothing is
# "connected" -- so aggregates such as GET /me's has_any_service_connected
# must skip them.
PSEUDO_SERVICE_KEYS = frozenset({DOCS_SERVICE_KEY})


def get_user_connected_services(user: dict) -> dict[str, bool]:
    """Determine which services a user has connected.

    Core services map to dedicated user-dict fields. Loaded plugins each
    contribute a ``<plugin id>`` key whose value combines both notions of
    "configured": the server side must be configured
    (:func:`config.plugins.plugin_server_available`) AND, when the plugin
    declares a per-user connection, the user's stored credential row (from
    ``user["service_credentials"]``, attached by db/user_store.py) must
    satisfy the plugin's ``connected`` predicate. Both must hold, so a
    user-side credential can't keep a plugin's ``system:<id>`` skill
    visible after an admin disables the plugin's service server-side.

    The map also carries capability pseudo-keys (:data:`PSEUDO_SERVICE_KEYS`,
    today only ``"docs"`` for the Quest Docs feature gate) that gate tools
    and skills the same way but are not connections.

    Args:
        user: User dict from the database (via db/user_store.py).

    Returns:
        Dict mapping service group names to connection status booleans.
    """
    # "slack" and "telegram" are plugin-contributed (plugins/slack,
    # plugins/telegram) via the loop below.
    services = {
        "google_services": user.get("google_services_oauth") is not None,
        "airtable": user.get("airtable_token") is not None,
        "ramp": user.get("ramp_oauth") is not None,
    }

    from config.plugins import get_loaded_plugins, plugin_server_available

    stored_rows = user.get("service_credentials") or {}
    for plugin in get_loaded_plugins():
        connected = True
        spec = plugin.user_connection
        if spec is not None:
            row = stored_rows.get(plugin.id)
            try:
                connected = bool(row) and bool(spec.connected(row))
            except Exception:
                logger.exception(
                    "Plugin %r connected predicate raised; treating as "
                    "not connected", plugin.id,
                )
                connected = False
        services[plugin.id] = connected and plugin_server_available(plugin)

    # Capability pseudo-key (PSEUDO_SERVICE_KEYS), not a connector: mirrors
    # the per-user Quest Docs feature gate so the doc tools (registry specs
    # with ``requires_service: "docs"``) and the ``system:quest_docs``
    # skill are hidden from prompts while the gate is closed for this user.
    # Set after the plugin loop so the gate always decides this key.
    services[DOCS_SERVICE_KEY] = docs_enabled_for(user.get("email") or "")
    return services


def _preamble_instructions(base_url: str, api_key: str, connected_services: dict[str, bool] | None = None) -> str:
    """Preamble section: proxy intro, auth, and API version list."""
    include_all = connected_services is None
    gs = include_all or connected_services.get("google_services", False)
    sl = include_all or connected_services.get("slack", False)
    tg = include_all or connected_services.get("telegram", False)

    # Build the list of available services for the intro line
    service_names = []
    if gs:
        service_names.extend(["Gmail", "Calendar", "Drive", "Docs", "Sheets", "Tasks"])
    if sl:
        service_names.append("Slack")
    if tg:
        service_names.append("Telegram")
    at = include_all or connected_services.get("airtable", False)
    if at:
        service_names.append("Airtable")
    rp = include_all or connected_services.get("ramp", False)
    if rp:
        service_names.append("Ramp")

    if service_names:
        services_text = ", ".join(service_names)
        intro_line = f"There's a local proxy running at `{base_url}` that provides access to {services_text} APIs."
    else:
        intro_line = (
            f"There's a local proxy running at `{base_url}`. "
            "No API services are currently connected. Connect services in Settings > Data Connections."
        )

    # Build the numbered API list dynamically
    api_items = []
    counter = 1
    if gs:
        api_items.append(f"{counter}. **Gmail Simple Tools** (`get_gmail_messages`, `create_gmail_draft`, ... via `tool_call`; `/api/gmail-simple/*` over HTTP for scripts) - Recommended for Gmail. Returns simplified, decoded output that's easy to work with.")
        counter += 1
        api_items.append(f"{counter}. **Gmail Raw API** (via `authed_get`) - Access raw Gmail API responses using the authed_get tool with `https://gmail.googleapis.com/gmail/v1/...` URLs.")
        counter += 1
        api_items.append(f"{counter}. **Google Calendar API** (via `authed_get`) - Access Google Calendar data using the authed_get tool with `https://www.googleapis.com/calendar/v3/...` URLs.")
        counter += 1
        api_items.append(f"{counter}. **Google Drive API** (via `authed_get`) - Access Google Drive data using the authed_get tool with `https://www.googleapis.com/drive/v3/...` URLs.")
        counter += 1
        api_items.append(f"{counter}. **Google Docs API** (via `authed_get`) - Access Google Docs content using the authed_get tool with `https://docs.googleapis.com/v1/...` URLs. List Docs via the Drive API with a mimeType filter.")
        counter += 1
        api_items.append(f"{counter}. **Google Sheets API** (via `authed_get`) - Access Google Sheets content using the authed_get tool with `https://sheets.googleapis.com/v4/...` URLs. List spreadsheets via the Drive API with a mimeType filter.")
        counter += 1
        api_items.append(f"{counter}. **Google Slides API** (via `authed_get`) - Access Google Slides content using the authed_get tool with `https://slides.googleapis.com/v1/...` URLs. List presentations via the Drive API with a mimeType filter.")
        counter += 1
        api_items.append(f"{counter}. **Google Tasks API** (via `authed_get`) - Access Google Tasks data using the authed_get tool with `https://tasks.googleapis.com/tasks/v1/...` URLs.")
        counter += 1
    if sl:
        api_items.append(f"{counter}. **Slack Read Tools** (`search_slack_messages`, `get_slack_conversation_history`, ... via `tool_call` only; no HTTP endpoints) - Search messages and read conversations. Returns Slack API responses.")
        counter += 1
    if tg:
        api_items.append(f"{counter}. **Telegram Read Tools** (`telegram_list_dialogs`, `telegram_get_messages`, `telegram_list_contacts`, `telegram_get_me` via `tool_call` only; no HTTP endpoints) - Read Telegram dialogs, messages, and contacts.")
        counter += 1
    if at:
        api_items.append(f"{counter}. **Airtable API** (via `authed_get`) - Access Airtable data using the authed_get tool with `https://api.airtable.com/v0/...` URLs.")
        counter += 1
    if rp:
        api_items.append(f"{counter}. **Ramp API** (via `authed_get`) - Access Ramp spend data (transactions, cards, bills, reimbursements) using the authed_get tool with `https://api.ramp.com/developer/v1/...` URLs.")
        counter += 1

    api_list = "\n".join(api_items)

    api_versions_section = ""
    if api_items:
        api_versions_section = f"""
---

**API Versions:**

This proxy provides multiple APIs:
{api_list}"""

    return f"""**Quest API Proxy**

{intro_line}

**Authentication:**

All API requests require your API key in the Authorization header:
```
Authorization: Bearer {api_key}
```

All curl examples below omit the auth header for brevity. Add `-H "Authorization: Bearer {api_key}"` to each request.
{api_versions_section}"""


def get_instructions_content(base_url: str, api_key: str, connected_services: dict[str, bool] | None = None) -> str:
    """Generate API instructions content, optionally filtered by connected services.

    This function is shared between the /api/instructions endpoint, the
    system prompt generation for Gemini API mode, and the GEMINI.md
    generation for Docker containers.

    Args:
        base_url: The base URL of the proxy (e.g., "http://localhost:8000")
        api_key: The user's API key for authentication
        connected_services: Dict mapping service group names to booleans.
            When None (default), all services are included (backward-compatible).
            Example: {"google_services": True, "slack": False, "telegram": True}

    Returns:
        Formatted markdown instructions string
    """
    # Start with preamble (always included)
    sections = [_preamble_instructions(base_url, api_key, connected_services)]

    # Determine which services to include. The full Slack and Telegram
    # sections are not appended here anymore -- they live in the plugins'
    # system:slack / system:telegram skills (plugins/<id>/instructions.md);
    # the preamble's API list still mentions their tools when the plugin
    # reports connected.
    include_all = connected_services is None
    gs = include_all or connected_services.get("google_services", False)
    at = include_all or connected_services.get("airtable", False)

    if gs:
        sections.append(gmail_instructions(base_url))
        sections.append(calendar_instructions(base_url))
        sections.append(drive_instructions(base_url))
        sections.append(docs_instructions(base_url))
        sections.append(sheets_instructions(base_url))
        sections.append(slides_instructions(base_url))
        sections.append(tasks_instructions(base_url))
    if at:
        sections.append(airtable_instructions(base_url))
    rp = include_all or connected_services.get("ramp", False)
    if rp:
        sections.append(ramp_instructions(base_url))

    return "\n\n---\n\n".join(sections)
