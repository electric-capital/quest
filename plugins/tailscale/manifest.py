"""The Tailscale plugin manifest.

Read-only access to a tailnet's configuration for Tailscale administrators:
devices (incl. routes and posture attributes), the policy file (ACL /
grants), DNS, users, auth keys and OAuth clients (metadata only), tailnet
settings, contacts, webhooks, device-posture integrations, Tailscale
Services, and network flow logs.

The plugin id is ``tailscale``: the ``connected_services`` key, the
``system:tailscale`` skill gate, and the per-user ``api_key`` connection
(the generic ``POST /auth/service-key/tailscale`` routes).

Shape: the **services-only** shape of the Google Workspace Admin plugin
(one GET-only ``authed_get`` service entry plus a skill; no tools, no action
requests) on the **api_key-kind** connection of the Iru plugin, with no
admin card at all -- ``api.tailscale.com`` is a fixed public host and every
credential is the user's own, so there is nothing to configure server-side
(``plugin_server_available()`` treats a schema-less plugin as always
available).

Read-only is enforced by the allow-list: GET paths only and no
``allowed_post_endpoints``, so ``authed_post`` rejects every POST to the
host. A user who connects a read-scoped OAuth client (see upstream.py)
additionally holds a token that cannot write at all.
"""

from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import HelpLink, QuestPlugin, UserConnectionSpec

from plugins.tailscale.upstream import (
    API_HOST,
    MISSING_CREDENTIALS_ERROR,
    inject_tailscale_bearer_auth,
    load_tailscale_credentials,
    tailscale_connected,
    validate_credential,
)

_PLUGIN_DIR = Path(__file__).parent


def _tailscale_skill_content(_base_url: str, _api_key: str) -> str:
    """The system:tailscale skill body, from the instructions.md data file."""
    return (_PLUGIN_DIR / "instructions.md").read_text()


_V2 = r"^/api/v2"
# One path segment: a tailnet name (``-``, ``example.com``,
# ``alice@example.com``, ``alice.github``), a device / key / user / webhook
# id, or a service name (``svc:web`` -- Tailscale ids carry no ``:``-encoded
# custom verbs, so the colon is fine). Dot segments, literal or
# percent-encoded, are refused so a path cannot climb out of its resource.
_SEG = r"(?!(?:\.|%2[eE]){1,2}(?:/|$))[^/]+"
_TAILNET = _V2 + rf"/tailnet/{_SEG}"

# Deliberately NOT listed although Tailscale serves them as GETs:
# - user invites (``/tailnet/{t}/user-invites``, ``/user-invites/{id}``) and
#   device invites (``/device/{id}/device-invites``, ``/device-invites/{id}``):
#   the rows carry the ``inviteUrl`` that accepts the invite -- a live
#   credential that would land in transcripts.
# - log streaming configuration (``/tailnet/{t}/logging/{type}/stream`` and
#   ``.../stream/status``): the destination record can include the
#   streaming destination's credentials.
# - ``/tailnet/{t}/acl/validate`` and ``/acl/preview`` are POSTs (no POST
#   allow-list), as is ``/oauth/token`` (used only by the credential
#   loader, never reachable through authed_get).
_TAILSCALE_SERVICE = {
    "key": API_HOST,
    "entry": {
        "name": "Tailscale",
        "load_credentials": load_tailscale_credentials,
        "inject_auth": inject_tailscale_bearer_auth,
        "requires_user": True,
        "missing_credentials_error": MISSING_CREDENTIALS_ERROR,
        "default_headers": {
            "User-Agent": "Quest/1.0",
        },
        "allowed_endpoints": [
            # -- devices
            _TAILNET + r"/devices$",                        # list devices (fields=all|default)
            _V2 + rf"/device/{_SEG}$",                      # get a device (nodeId or legacy id)
            _V2 + rf"/device/{_SEG}/routes$",               # advertised + enabled subnet routes
            _V2 + rf"/device/{_SEG}/attributes$",           # posture attributes
            # -- policy file
            _TAILNET + r"/acl$",                            # the policy file (JSON, or HuJSON via Accept)
            # -- DNS
            _TAILNET + r"/dns/nameservers$",                # global nameservers
            _TAILNET + r"/dns/preferences$",                # MagicDNS on/off
            _TAILNET + r"/dns/searchpaths$",                # search domains
            _TAILNET + r"/dns/split-dns$",                  # split DNS (domain -> nameservers)
            _TAILNET + r"/dns/configuration$",              # whole DNS configuration (alpha)
            # -- keys (metadata only: the key value is returned at creation only)
            _TAILNET + r"/keys$",                           # list auth keys / OAuth clients (all=true)
            _TAILNET + rf"/keys/{_SEG}$",                   # get one key
            # -- users
            _TAILNET + r"/users$",                          # list users (type=, role=)
            _V2 + rf"/users/{_SEG}$",                       # get a user
            # -- tailnet
            _TAILNET + r"/settings$",                       # tailnet settings
            _TAILNET + r"/contacts$",                       # account / support / security contacts
            _V2 + rf"/organizations/{_SEG}/tailnets$",      # tailnets of the organization (``-`` = own)
            # -- webhooks (the secret is returned at creation / rotation only)
            _TAILNET + r"/webhooks$",                       # list webhooks
            _V2 + rf"/webhooks/{_SEG}$",                    # get a webhook
            # -- device posture integrations (client ids only, no secrets)
            _TAILNET + r"/posture/integrations$",           # list integrations
            _V2 + rf"/posture/integrations/{_SEG}$",        # get an integration
            # -- Tailscale Services (virtual IP services)
            _TAILNET + r"/vip-services$",                   # list services
            _TAILNET + rf"/vip-services/{_SEG}$",           # get a service (svc:name)
            # -- logs
            _TAILNET + r"/logging/network$",                # network flow logs (start=, end= required)
        ],
    },
}


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id="tailscale",
        label="Tailscale",
        user_connection=UserConnectionSpec(
            kind="api_key",
            connected=tailscale_connected,
            validate_key=validate_credential,
            key_placeholder=(
                "Paste an API access token (tskey-api-...) or OAuth client "
                "secret (tskey-client-...)"
            ),
            key_help=(
                "Create the credential in the Tailscale admin console (Owner, "
                "Admin, IT admin or Network admin role). An OAuth client with "
                "read-only scopes is recommended: it never expires and cannot "
                "change anything. An API access token also works but carries "
                "your full rights and expires within 90 days."
            ),
            key_help_links=(
                HelpLink(
                    "Generate an OAuth client",
                    "https://login.tailscale.com/admin/settings/oauth",
                ),
                HelpLink(
                    "Generate an API access token",
                    "https://login.tailscale.com/admin/settings/keys",
                ),
                HelpLink(
                    "OAuth clients and scopes (docs)",
                    "https://tailscale.com/kb/1215/oauth-clients",
                ),
                HelpLink(
                    "API access tokens (docs)",
                    "https://tailscale.com/kb/1101/api",
                ),
            ),
        ),
        services=(_TAILSCALE_SERVICE,),
        system_skills=(
            SystemSkill(
                id="system:tailscale",
                name="Tailscale",
                description=(
                    "Read-only tailnet admin: devices, routes, ACL policy, "
                    "DNS, users, keys, settings, flow logs via authed_get."
                ),
                when_to_load=(
                    "Load when the user asks about their Tailscale network "
                    "(tailnet): devices and their status, subnet routes or "
                    "exit nodes, the ACL / grants policy file, MagicDNS and "
                    "split DNS, tailnet users and roles, auth keys or OAuth "
                    "clients, tailnet settings, webhooks, or network flow logs."
                ),
                requires="tailscale",
                content_builder=_tailscale_skill_content,
            ),
        ),
    )
