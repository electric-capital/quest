"""The GitHub plugin manifest.

An in-tree reference plugin, and the first with an **oauth-kind** user
connection: the GitHub integration
packaged as a :class:`~config.plugin_types.QuestPlugin`. The plugin id is
``github`` -- the key the integration has always used for
``connected_services``, the ``system:github`` skill gate, and the admin
credential store file (``github.json``; the legacy server_credentials.json
``github`` section still migrates at startup because
``migrate_legacy_credentials()`` runs after plugin load). The OAuth URLs
(``/auth/github``, ``/auth/github/callback``) are unchanged, so existing
GitHub OAuth app registrations keep working; per-user tokens move from the
old ``users.github_oauth`` column into ``user_service_credentials``
``oauth_blob`` rows (Alembic data migration).

The core ``get_github_job_log`` tool is renamed ``github_get_job_log`` to
satisfy the ``<id>_`` prefix rule (tool names have no persistence).
"""

from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import CredentialField, QuestPlugin, UserConnectionSpec

from plugins.github.oauth import router as github_oauth_router
from plugins.github.tools import ALL_TOOLS
from plugins.github.upstream import (
    github_connected,
    github_is_configured,
    github_needs_reauth,
    GITHUB_SCOPES,
    inject_github_bearer_auth,
    load_github_credentials,
    MISSING_CREDENTIALS_ERROR,
)

_PLUGIN_DIR = Path(__file__).parent


def _github_skill_content(_base_url: str, _api_key: str) -> str:
    """The system:github skill body, from the instructions.md data file."""
    return (_PLUGIN_DIR / "instructions.md").read_text()


# authed_get service entry for the GitHub REST API. Read-only by
# construction: only these GET paths are reachable, and the entry defines
# no allowed_post_endpoints. GitHub App user tokens expire after 8 hours,
# so the loader proactively refreshes them (plugins/github/upstream.py)
# and retry_on_401 re-runs it -- re-reading the stored row -- as a
# backstop; classic OAuth App tokens never expire and pass straight through.
_GITHUB_SERVICE = {
    "key": "api.github.com",
    "entry": {
        "name": "GitHub",
        "load_credentials": load_github_credentials,
        "inject_auth": inject_github_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        # GitHub REST API requires a User-Agent header on every request
        # and recommends the versioned Accept header.  These are merged
        # into every outgoing request by _make_authed_request() before
        # caller-supplied headers and auth injection.
        "default_headers": {
            "User-Agent": "Quest/1.0",
            "Accept": "application/vnd.github+json",
        },
        "missing_credentials_error": MISSING_CREDENTIALS_ERROR,
        "allowed_endpoints": [
            r"^/user$",                                              # authenticated user profile
            r"^/user/repos$",                                        # list user repos
            r"^/user/orgs$",                                         # list user orgs
            r"^/repos/[^/]+/[^/]+$",                                 # get a repo
            r"^/repos/[^/]+/[^/]+/branches$",                        # list branches
            r"^/repos/[^/]+/[^/]+/stargazers$",                      # list users who starred a repo
            r"^/repos/[^/]+/[^/]+/issues$",                          # list issues
            r"^/repos/[^/]+/[^/]+/issues/\d+$",                      # get a single issue
            r"^/repos/[^/]+/[^/]+/issues/\d+/comments$",             # list issue comments
            r"^/repos/[^/]+/[^/]+/pulls$",                           # list pull requests
            r"^/repos/[^/]+/[^/]+/pulls/\d+$",                       # get a single PR
            r"^/repos/[^/]+/[^/]+/pulls/\d+/files$",                 # list PR changed files
            r"^/repos/[^/]+/[^/]+/pulls/\d+/reviews$",               # list PR reviews
            r"^/repos/[^/]+/[^/]+/commits$",                         # list commits
            r"^/repos/[^/]+/[^/]+/commits/[^/]+$",                   # get a single commit
            r"^/repos/[^/]+/[^/]+/contents(/.*)?$",                  # file/directory contents (any sub-path or root)
            # --- GitHub Actions (read-only) ---
            r"^/repos/[^/]+/[^/]+/actions/runs$",                                 # list workflow runs
            r"^/repos/[^/]+/[^/]+/actions/runs/\d+$",                             # get a single workflow run
            r"^/repos/[^/]+/[^/]+/actions/runs/\d+/jobs$",                        # list jobs for a run
            r"^/repos/[^/]+/[^/]+/actions/runs/\d+/attempts/\d+/jobs$",           # list jobs for a specific re-run attempt
            r"^/repos/[^/]+/[^/]+/actions/jobs/\d+$",                             # get a single job (includes steps)
            r"^/repos/[^/]+/[^/]+/actions/jobs/\d+/logs$",                        # job logs (302 to signed URL; see github_get_job_log)
            r"^/repos/[^/]+/[^/]+/actions/runs/\d+/logs$",                        # run logs zip (defensive allow; no dedicated tool yet)
            r"^/repos/[^/]+/[^/]+/actions/workflows$",                            # list workflows defined in the repo
            r"^/repos/[^/]+/[^/]+/actions/workflows/[^/]+$",                      # get a single workflow (ID or filename)
            r"^/repos/[^/]+/[^/]+/actions/workflows/[^/]+/runs$",                 # list runs for a specific workflow
            r"^/orgs/[^/]+/repos$",                                  # list org repos
            r"^/search/repositories$",                               # search repositories
            r"^/search/issues$",                                     # search issues/PRs
            r"^/search/code$",                                       # search code
        ],
    },
}


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id="github",
        label="GitHub",
        credential_schema=(
            CredentialField(
                key="client_id", label="Client ID", type="text",
                placeholder="GitHub OAuth app client ID", required=True,
            ),
            CredentialField(
                key="client_secret", label="Client secret", type="secret",
                placeholder="GitHub OAuth app client secret", required=True,
            ),
        ),
        is_configured=github_is_configured,
        user_connection=UserConnectionSpec(
            kind="oauth",
            connected=github_connected,
            oauth_router=github_oauth_router,
            scopes=GITHUB_SCOPES,
            needs_reauth=github_needs_reauth,
        ),
        services=(_GITHUB_SERVICE,),
        system_skills=(
            SystemSkill(
                id="system:github",
                name="GitHub",
                description=(
                    "Read repos/issues/PRs/commits/Actions via authed_get; "
                    "github_get_job_log tool."
                ),
                when_to_load=(
                    "Load when the user asks about GitHub repos, issues, "
                    "PRs, commits, or CI runs."
                ),
                requires="github",
                content_builder=_github_skill_content,
            ),
        ),
        tools=ALL_TOOLS,
    )
