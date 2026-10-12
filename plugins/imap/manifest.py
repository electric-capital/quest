"""The IMAP mailbox plugin manifest.

The first ``form``-kind, multi-connection plugin (issue #84 section 10):
a user adds up to ten IMAP mailboxes, each a connection row whose
encrypted ``oauth_blob["values"]`` holds server / username / password
and whose account is the mailbox address (``account_for``). The
``verify`` hook logs in live before a row is saved.

The admin card (``imap.json``) holds only the ``allowed_hosts`` server
list (one ``host[:port][ ssl|starttls]`` configuration per line, saved
in the canonical ``host:port mode`` form; see plugins/imap/settings.py):
the connector row is ``available: false`` until it lists at least one
valid server, the server select's choices are its canonical lines
(``field_options``), and every connect resolves the row's ``server``
against it again (plugins/imap/upstream.py). Transport: stdlib
``imaplib`` over verified TLS only.
"""

import json
from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import CredentialField, QuestPlugin, UserConnectionSpec

from plugins.imap.settings import (
    SERVICE_ID,
    imap_connected,
    imap_field_options,
    imap_is_configured,
    imap_row_warning,
    validate_imap_admin,
)
from plugins.imap.tools import ALL_TOOLS, SCRIPT_TOOL_NAMES
from plugins.imap.upstream import verify_imap_connection

CONNECTION_LIMIT = 10

_PLUGIN_DIR = Path(__file__).parent
_EXAMPLE_ACCOUNT = "work@example.com"
_NO_CONNECTIONS = (
    "_No IMAP mailbox is connected yet. The user adds one in Settings > Data "
    "Connections; until then the imap_* tools cannot reach any mailbox._"
)


def _clean_account(account) -> str:
    """An account for display: one token, no markdown/quote breakers (the
    store already refuses whitespace, quotes and backticks)."""
    text = "".join(str(account or "").split())
    return text.replace("`", "").replace('"', "")


def imap_skill_content(_base_url: str, _api_key: str, connections=None) -> str:
    """The system:imap skill body: instructions.md with the user's real
    connection accounts substituted into the connection list and example.

    ``connections`` are ``config.user_connections.connection_summary``
    dicts (default first); rows without an account are skipped.
    """
    accounts = [
        (_clean_account(c.get("account")), bool(c.get("is_default")))
        for c in (connections or [])
        if isinstance(c, dict) and _clean_account(c.get("account"))
    ]
    if accounts:
        listed = "\n".join(
            f"- `{account}`" + (" (default)" if is_default else "")
            for account, is_default in accounts
        )
        # Prefer a non-default account so the example shows naming one.
        example = next((a for a, d in accounts if not d), accounts[0][0])
    else:
        listed = _NO_CONNECTIONS
        example = _EXAMPLE_ACCOUNT
    text = (_PLUGIN_DIR / "instructions.md").read_text()
    return (
        text.replace("{{CONNECTIONS_LIST}}", listed)
        .replace("{{CONNECTION_EXAMPLE}}", json.dumps(example))
    )


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id=SERVICE_ID,
        label="IMAP mailbox",
        credential_schema=(
            CredentialField(
                key="allowed_hosts",
                label="IMAP servers",
                type="textarea",
                required=True,
                placeholder="imap.example.com\nmail.example.org:143 starttls",
                help=(
                    "One server per line: host[:port] [ssl|starttls]. A bare host "
                    "means port 993 over TLS; write host:143 starttls for STARTTLS. "
                    "Users pick their mailbox's server from this list; removing a "
                    "line disables the connections that use it."
                ),
            ),
        ),
        is_configured=imap_is_configured,
        credential_validate=validate_imap_admin,
        user_connection=UserConnectionSpec(
            kind="form",
            multi_connection=True,
            connection_limit=CONNECTION_LIMIT,
            fields=(
                # Options: the admin card's canonical lines (field_options).
                CredentialField(key="server", label="IMAP server", type="select", required=True),
                CredentialField(key="username", label="Username", type="text", required=True),
                CredentialField(key="password", label="Password", type="secret", required=True),
            ),
            field_options=imap_field_options,
            verify=verify_imap_connection,
            connected=imap_connected,
            row_warning=imap_row_warning,
        ),
        system_skills=(
            SystemSkill(
                id="system:imap",
                name="IMAP Mailboxes",
                description=(
                    "IMAP email (one or more mailboxes): imap_* tools to list "
                    "folders, search, read messages, save attachments."
                ),
                when_to_load=(
                    "Load when the user asks about email in a connected IMAP "
                    "mailbox (folders, messages, searches, attachments)."
                ),
                requires=SERVICE_ID,
                content_builder=imap_skill_content,
            ),
        ),
        tools=ALL_TOOLS,
        script_tool_allowlist=SCRIPT_TOOL_NAMES,
    )
