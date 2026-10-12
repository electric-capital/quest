"""The IMAP mailbox plugin manifest.

The first ``form``-kind, multi-connection plugin (issue #84 section 10):
a user adds up to ten IMAP mailboxes, each a connection row whose
encrypted ``oauth_blob["values"]`` holds host / port / security /
username / password and whose account is the mailbox address
(``account_for``). The ``verify`` hook logs in live before a row is
saved.

The admin card (``imap.json``) holds only the ``allowed_hosts``
allow-list: the connector row is ``available: false`` until it lists at
least one valid server, the host select's choices come from it
(``field_options``), and every connect re-checks it (plugins/imap/
upstream.py). Transport: stdlib ``imaplib`` over verified TLS only.
"""

from config.plugin_types import CredentialField, QuestPlugin, UserConnectionSpec

from plugins.imap.settings import (
    SECURITY_CHOICES,
    SERVICE_ID,
    imap_connected,
    imap_field_options,
    imap_is_configured,
    imap_row_warning,
    validate_imap_admin,
)
from plugins.imap.upstream import verify_imap_connection

CONNECTION_LIMIT = 10


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id=SERVICE_ID,
        label="IMAP mailbox",
        credential_schema=(
            CredentialField(
                key="allowed_hosts",
                label="Allowed IMAP servers",
                type="textarea",
                required=True,
                placeholder="imap.example.com\nmail.example.org:993",
            ),
        ),
        is_configured=imap_is_configured,
        credential_validate=validate_imap_admin,
        user_connection=UserConnectionSpec(
            kind="form",
            multi_connection=True,
            connection_limit=CONNECTION_LIMIT,
            fields=(
                CredentialField(key="host", label="IMAP server", type="select", required=True),
                # Blank = the security default: 993 for ssl, 143 for starttls.
                CredentialField(key="port", label="Port", type="text", placeholder="993"),
                CredentialField(
                    key="security", label="Security", type="select", required=True,
                    options=SECURITY_CHOICES,
                ),
                CredentialField(key="username", label="Username", type="text", required=True),
                CredentialField(key="password", label="Password", type="secret", required=True),
            ),
            field_options=imap_field_options,
            verify=verify_imap_connection,
            connected=imap_connected,
            row_warning=imap_row_warning,
        ),
    )
