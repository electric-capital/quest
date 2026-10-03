"""The Google Workspace Admin plugin manifest.

Read-only access to a Google Workspace account's directory (users,
groups, org units, domains, rooms), device inventory (ChromeOS, mobile),
and Google Meet activity (calls, call quality, Meet hardware, usage) for
Workspace administrators.

The plugin id is ``google_admin``: the ``connected_services`` key, the
``system:google_admin`` skill gate, the admin credential store file
(``google_admin.json``: just the ``enabled`` switch). The OAuth namespace
writes the underscore as a hyphen: ``/auth/google-admin``.

Shape: an oauth-kind user connection (the Microsoft 365 expiring-token
pattern) that borrows the core Google OAuth client instead of carrying
its own, two GET-only ``authed_get`` service entries on
``admin.googleapis.com`` (the Directory API, and the Reports API scoped by
path prefix), and four read-only Meet tools (plugins/google_admin/tools.py)
that page, flatten and aggregate Reports API records too large for the
``authed_get`` size gate. No action requests.

Read-only is enforced twice: the OAuth grant holds only ``*.readonly``
scopes (see plugins/google_admin/upstream.py), and the service entries
allow-list GET paths only (no ``allowed_post_endpoints``, so authed_post
rejects every POST to the host).
"""

from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import CredentialField, QuestPlugin, UserConnectionSpec

from plugins.google_admin.oauth import router as google_admin_oauth_router
from plugins.google_admin.tools import GOOGLE_ADMIN_TOOL_NAMES, GOOGLE_ADMIN_TOOLS
from plugins.google_admin.upstream import (
    GOOGLE_ADMIN_SCOPES,
    MISSING_CREDENTIALS_ERROR,
    google_admin_connected,
    google_admin_is_configured,
    google_admin_needs_reauth,
    inject_google_admin_bearer_auth,
    load_google_admin_credentials,
    validate_google_admin_credentials,
)

_PLUGIN_DIR = Path(__file__).parent


def _google_admin_skill_content(_base_url: str, _api_key: str) -> str:
    """The system:google_admin skill body, from the instructions.md data file."""
    return (_PLUGIN_DIR / "instructions.md").read_text()


_DIRECTORY = r"^/admin/directory/v1"
# ``{customerId}`` path segment: ``my_customer`` or a customer id.
_CUSTOMER = _DIRECTORY + r"/customer/[^/:]+"
# One segment of an org unit path. Org unit paths are multi-segment
# (``corp/sales/emea``) and the id form contains a colon (``id:03ph8a2z``),
# so the ``[^/:]+`` convention used everywhere else does not fit; dot
# segments (literal or percent-encoded) are refused instead so the path
# cannot climb out of ``orgunits/``.
_OU_SEGMENT = r"(?!(?:\.|%2[eE]){1,2}(?:/|$))[^/]+"

# Admin SDK Directory API. Id segments use ``[^/:]+`` (not ``[^/]+``) to
# exclude ':', which is how Google encodes custom methods; the one custom
# method allowed (the read-only ChromeOS device count) is spelled out.
#
# Deliberately NOT listed, although they are GETs: ``users/{key}/tokens``,
# ``/asps`` and ``/verificationCodes`` (third-party grants, app passwords,
# backup codes -- the latter are live credentials), user photos, admin
# roles, and printers.
_DIRECTORY_SERVICE = {
    "key": "admin.googleapis.com",
    "entry": {
        "name": "Google Workspace Admin (Directory)",
        "load_credentials": load_google_admin_credentials,
        "inject_auth": inject_google_admin_bearer_auth,
        "requires_user": True,
        "missing_credentials_error": MISSING_CREDENTIALS_ERROR,
        "allowed_endpoints": [
            # -- users
            _DIRECTORY + r"/users$",                                  # list/search users
            _DIRECTORY + r"/users/[^/:]+$",                           # get a user (email, alias or id)
            _DIRECTORY + r"/users/[^/:]+/aliases$",                   # a user's email aliases
            # -- groups
            _DIRECTORY + r"/groups$",                                 # list/search groups (userKey= for one user's groups)
            _DIRECTORY + r"/groups/[^/:]+$",                          # get a group
            _DIRECTORY + r"/groups/[^/:]+/aliases$",                  # a group's aliases
            _DIRECTORY + r"/groups/[^/:]+/members$",                  # list members
            _DIRECTORY + r"/groups/[^/:]+/members/[^/:]+$",           # get one membership (role, type)
            _DIRECTORY + r"/groups/[^/:]+/hasMember/[^/:]+$",         # membership check incl. nested groups
            # -- org structure
            _CUSTOMER + r"/orgunits$",                                # list org units
            _CUSTOMER + rf"/orgunits/{_OU_SEGMENT}(?:/{_OU_SEGMENT})*$",  # get an org unit (path or id:...)
            _CUSTOMER + r"/domains$",                                 # list domains
            _CUSTOMER + r"/domains/[^/:]+$",                          # get a domain
            _CUSTOMER + r"/domainaliases$",                           # list domain aliases
            _CUSTOMER + r"/domainaliases/[^/:]+$",                    # get a domain alias
            _CUSTOMER + r"/schemas$",                                 # list custom user schemas
            _CUSTOMER + r"/schemas/[^/:]+$",                          # get a custom user schema
            _DIRECTORY + r"/customers/[^/:]+$",                       # the Workspace account (note: customerS)
            # -- devices
            _CUSTOMER + r"/devices/chromeos$",                        # list/search ChromeOS devices
            _CUSTOMER + r"/devices/chromeos:countChromeOsDevices$",   # count ChromeOS devices (read-only custom method)
            _CUSTOMER + r"/devices/chromeos/[^/:]+$",                 # get a ChromeOS device
            _CUSTOMER + r"/devices/mobile$",                          # list/search mobile devices
            _CUSTOMER + r"/devices/mobile/[^/:]+$",                   # get a mobile device
            # -- rooms (calendar resources: where Meet hardware lives)
            _CUSTOMER + r"/resources/calendars$",                     # list rooms / resources
            _CUSTOMER + r"/resources/calendars/[^/:]+$",              # get one
            _CUSTOMER + r"/resources/buildings$",                     # list buildings
            _CUSTOMER + r"/resources/buildings/[^/:]+$",              # get one
            _CUSTOMER + r"/resources/features$",                      # list room features
            _CUSTOMER + r"/resources/features/[^/:]+$",               # get one
        ],
    },
}

_REPORTS = r"^/admin/reports/v1"

# Admin SDK Reports API, a separate entry on the same host selected by
# path prefix. The token's Reports scopes cover every application's audit
# log; the allow-list confines reads to the Meet and Meet hardware logs
# (``users/all`` or one user) and the daily customer usage report.
# Deliberately NOT listed: the other applications' audit logs (login,
# admin, token, drive, ...), user / entity usage reports, and the
# ``.../watch`` push-channel endpoint (a POST anyway).
_REPORTS_SERVICE = {
    "key": "admin.googleapis.com/admin/reports/v1",
    "entry": {
        "name": "Google Workspace Admin (Reports)",
        "path_prefix": "/admin/reports/v1",
        "load_credentials": load_google_admin_credentials,
        "inject_auth": inject_google_admin_bearer_auth,
        "requires_user": True,
        "missing_credentials_error": MISSING_CREDENTIALS_ERROR,
        "allowed_endpoints": [
            _REPORTS + r"/activity/users/[^/:]+/applications/(?:meet|meet_hardware)$",  # audit records
            _REPORTS + r"/usage/dates/\d{4}-\d{2}-\d{2}$",  # customer usage (parameters=meet:...)
        ],
    },
}


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id="google_admin",
        label="Google Workspace Admin",
        credential_schema=(
            # No credentials of its own: the flow reuses the core Google
            # OAuth client. The switch is the admin's confirmation that the
            # client has the /auth/google-admin/callback redirect URI and
            # that the Admin SDK API is enabled (see
            # docs/google-admin-setup.md).
            CredentialField(key="enabled", label="Enabled", type="bool"),
        ),
        is_configured=google_admin_is_configured,
        credential_validate=validate_google_admin_credentials,
        user_connection=UserConnectionSpec(
            kind="oauth",
            connected=google_admin_connected,
            oauth_router=google_admin_oauth_router,
            scopes=GOOGLE_ADMIN_SCOPES,
            needs_reauth=google_admin_needs_reauth,
        ),
        services=(_DIRECTORY_SERVICE, _REPORTS_SERVICE),
        tools=GOOGLE_ADMIN_TOOLS,
        # Read-only: sandbox scripts may aggregate across many calls.
        script_tool_allowlist=frozenset(GOOGLE_ADMIN_TOOL_NAMES),
        system_skills=(
            SystemSkill(
                id="system:google_admin",
                name="Google Workspace Admin",
                description=(
                    "Read-only Workspace directory, rooms, ChromeOS/mobile "
                    "devices, and Google Meet calls, call quality, hardware."
                ),
                when_to_load=(
                    "Load when the user asks about their organization's "
                    "Google Workspace users, groups, org units, rooms, "
                    "managed devices, or Google Meet calls, call quality, "
                    "Meet hardware (room devices) or Meet usage."
                ),
                requires="google_admin",
                content_builder=_google_admin_skill_content,
            ),
        ),
    )
