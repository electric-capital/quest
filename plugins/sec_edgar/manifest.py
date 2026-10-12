"""The SEC EDGAR plugin manifest.

Read-only access to the US Securities and Exchange Commission's EDGAR data:
company submission / filing history and XBRL financial facts on
``data.sec.gov``, plus the two static ticker->CIK map files on
``www.sec.gov`` that let the agent resolve a stock ticker to the 10-digit
CIK every ``data.sec.gov`` path requires.

The plugin id is ``sec_edgar``: the ``system:sec_edgar`` skill and the
``plugins/sec_edgar`` directory. There is NO connection of any kind -- the
API is a free, public, unauthenticated US government service -- so the
plugin declares neither a ``credential_schema`` (no admin card) nor a
``user_connection`` (no Data Connections row), and its skill is ungated
(``requires=None``): the **credential-free** plugin shape, everything the
services-only Tailscale plugin has minus the connection. Such a plugin
counts as nothing "connected" for GET /me's ``has_any_service_connected``
(see ``config.plugins.connectionless_plugin_ids``).

Two ``authed_get`` service entries carry the whole surface:

- ``data.sec.gov`` -- a plain hostname key (no ``path_prefix``, because the
  host serves several distinct path roots: ``/submissions/`` and
  ``/api/xbrl/...``), path-gated entirely by its ``allowed_endpoints``
  regexes, which also pin the CIK to exactly 10 zero-padded digits.
- ``www.sec.gov`` -- EXACTLY the two ticker->CIK map files. The rest of the
  host (the human site, ``/cgi-bin/browse-edgar``, other ``/files/``) stays
  unreachable, as does the ``efts.sec.gov`` full-text search host.

Both are the no-auth modeling: a sync no-arg loader returning ``None``, a
no-op injector, ``requires_user`` left unset so the ``None`` credential
flows past the missing-credentials guard, no ``retry_on_401``. The one
SEC-specific detail is the fair-access policy's descriptive ``User-Agent``
(name + contact), without which SEC answers HTTP 403; it rides on each
entry's ``default_headers`` (below caller headers in precedence), never
set by the model. Read-only by construction: GET paths only and no
``allowed_post_endpoints``.
"""

from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import QuestPlugin

_PLUGIN_DIR = Path(__file__).parent

DATA_HOST = "data.sec.gov"
FILES_HOST = "www.sec.gov"

# SEC's fair-access policy requires a descriptive User-Agent with a contact
# token on every request (HTTP 403 otherwise).
SEC_USER_AGENT = "Quest/1.0 (admin@quest.example)"


def load_sec_edgar_credentials() -> None:
    """Load credentials for the SEC EDGAR data API: there are none.

    A sync, no-arg loader returning ``None`` (the CoinGecko key loader's
    call shape). ``requires_user`` is NOT set on either registry entry, so
    ``_make_authed_request`` invokes this with no args and the ``None``
    return flows past the missing-credentials guard.
    """
    return None


def inject_no_auth(_credentials, _headers: dict[str, str]) -> None:
    """No-op auth injector: ``inject_auth`` is always invoked, so a no-auth
    service supplies this to make that call harmless."""
    return None


def _sec_edgar_skill_content(_base_url: str, _api_key: str) -> str:
    """The system:sec_edgar skill body, from the instructions.md data file."""
    return (_PLUGIN_DIR / "instructions.md").read_text()


_DATA_SERVICE = {
    "key": DATA_HOST,
    "entry": {
        "name": "SEC EDGAR",
        "load_credentials": load_sec_edgar_credentials,
        "inject_auth": inject_no_auth,
        "default_headers": {
            "User-Agent": SEC_USER_AGENT,
        },
        "allowed_endpoints": [
            # CIK is 10-digit zero-padded (e.g. CIK0000320193).
            r"^/submissions/CIK\d{10}\.json$",                                  # primary entity submission/filing history
            r"^/submissions/CIK\d{10}-submissions-\d+\.json$",                  # older-filings spillover for large filers
            r"^/api/xbrl/companyconcept/CIK\d{10}/[^/]+/[^/]+\.json$",          # one concept's time series for one entity
            r"^/api/xbrl/companyfacts/CIK\d{10}\.json$",                        # all XBRL facts for one entity (large)
            r"^/api/xbrl/frames/[^/]+/[^/]+/[^/]+/CY\d{4}(Q[1-4]I?)?\.json$",   # one concept+unit across entities for a period
        ],
    },
}

_FILES_SERVICE = {
    "key": FILES_HOST,
    "entry": {
        "name": "SEC EDGAR (files)",
        "load_credentials": load_sec_edgar_credentials,
        "inject_auth": inject_no_auth,
        "default_headers": {
            "User-Agent": SEC_USER_AGENT,
        },
        "allowed_endpoints": [
            r"^/files/company_tickers\.json$",           # ticker -> CIK + company name
            r"^/files/company_tickers_exchange\.json$",  # same, with exchange info
        ],
    },
}


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id="sec_edgar",
        label="SEC EDGAR",
        services=(_DATA_SERVICE, _FILES_SERVICE),
        system_skills=(
            SystemSkill(
                id="system:sec_edgar",
                name="SEC EDGAR",
                description=(
                    "Read SEC EDGAR company submissions and XBRL financial "
                    "facts via authed_get (no auth)."
                ),
                when_to_load=(
                    "Load when the user asks about SEC filings, 10-K/10-Q, "
                    "company financials, or EDGAR data."
                ),
                # Ungated: the API needs no connection, so there is no
                # connected_services key to gate on.
                requires=None,
                content_builder=_sec_edgar_skill_content,
            ),
        ),
    )
