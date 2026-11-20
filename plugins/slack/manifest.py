"""The Slack integration plugin manifest.

An oauth-kind plugin (the GitHub shape: non-expiring user tokens) that
packages the Slack *integration surface*: the admin credential card, the
per-user OAuth connection, the nine dynamic tools (reads +
``send_slack_dm_to_self``), the two send action requests, the
``system:slack`` skill, and the sandbox-script bridge entries.

The plugin id is ``slack``: the ``connected_services`` key, the
``system:slack`` skill gate, the admin credential store file
(``slack.json``: OAuth app client id/secret plus the shared bot token
and Socket Mode app token, with the legacy server_credentials.json
"slack" section fallback preserved in config/service_credentials.py),
and the OAuth namespace (``/auth/slack`` -- the same URLs as
pre-plugin).

Because Slack predates the plugin packaging, it leans on both
grandfather lists: the ``send_slack_message`` / ``send_slack_dm``
action-request types are persisted in old ``action_requests`` rows
(``unprefixed_action_types``), and the infix-named tools
(``list_slack_teams``, ``send_slack_dm_to_self``, ...) are baked into
transcripts, sandbox scripts, and skill prose (``unprefixed_tools``).
``send_slack_dm_to_self`` additionally stays in the core public-project
allowlist via the ``_PUBLIC_ALLOWLIST_MIGRATED_TOOLS`` exemption in
config/plugins.py.

NOT part of the plugin: the Slack-*driven* conversation machinery -- the
Socket Mode worker (chat/slack_socket_mode.py), the
``slack_conversations`` table, the ``slack_reply`` wait-handle kind,
``SLACK_TOP_LEVEL_TOOLS`` / ``send_slack_reply_and_get_response``, and
the Slack Reply Mode prompt. Those stay core (the plugin contract has no
lifespan/suspend/tool-tier extension points); they read the same admin
credential store entry and resolve users via the same
``user_service_credentials`` rows this plugin writes.
"""

from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import CredentialField, QuestPlugin, UserConnectionSpec

from plugins.slack.handlers import SendSlackDmHandler, SendSlackMessageHandler
from plugins.slack.oauth import router as slack_oauth_router
from plugins.slack.tools import ALL_TOOLS, ALL_TOOL_NAMES, SCRIPT_TOOL_NAMES
from plugins.slack.upstream import (
    SLACK_USER_SCOPES,
    slack_connected,
    slack_is_configured,
)

_PLUGIN_DIR = Path(__file__).parent


def _slack_skill_content(_base_url: str, _api_key: str) -> str:
    """The system:slack skill body, from the instructions.md data file."""
    return (_PLUGIN_DIR / "instructions.md").read_text()


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id="slack",
        label="Slack",
        credential_schema=(
            CredentialField(
                key="client_id", label="Client ID", type="text",
                placeholder="Slack app client ID", required=True,
            ),
            CredentialField(
                key="client_secret", label="Client secret", type="secret",
                placeholder="Slack app client secret", required=True,
            ),
            CredentialField(
                key="bot_token", label="Bot token", type="secret",
                placeholder="xoxb-...",
            ),
            CredentialField(
                key="socket_mode_token", label="Socket Mode app token", type="secret",
                placeholder="xapp-...",
                help="Read at startup: changing or removing it takes effect after a server restart.",
            ),
        ),
        is_configured=slack_is_configured,
        user_connection=UserConnectionSpec(
            kind="oauth",
            connected=slack_connected,
            oauth_router=slack_oauth_router,
            scopes=SLACK_USER_SCOPES,
        ),
        system_skills=(
            SystemSkill(
                id="system:slack",
                name="Slack",
                description=(
                    "Slack read tools (search/history/replies, "
                    "channels/users/teams), self-DMs with file "
                    "attachments, send actions."
                ),
                when_to_load=(
                    "Load when the user asks about Slack messages, "
                    "channels, or wants to send a Slack message."
                ),
                requires="slack",
                content_builder=_slack_skill_content,
            ),
        ),
        action_request_handlers=(
            SendSlackMessageHandler(),
            SendSlackDmHandler(),
        ),
        tools=ALL_TOOLS,
        script_tool_allowlist=SCRIPT_TOOL_NAMES,
        unprefixed_action_types=frozenset({
            "send_slack_message",
            "send_slack_dm",
        }),
        unprefixed_tools=ALL_TOOL_NAMES,
    )
