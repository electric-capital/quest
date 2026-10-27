"""The vulnerability data plugin manifest.

Plugin id ``vulns``: the ``system:vulns`` skill and the four ``vulns_*``
tools over three free, public, unauthenticated vulnerability sources --
OSV.dev (which advisories affect a package version), CISA's Known
Exploited Vulnerabilities catalog (which CVEs are exploited in the wild)
and FIRST EPSS (how likely a CVE is to be exploited in the next 30 days).

Shape notes:

1. **Credential-free.** Like the SEC EDGAR plugin there is nothing to
   configure or connect: no ``credential_schema`` (no admin card), no
   ``user_connection`` (no Data Connections row), an ungated skill
   (``requires=None``) and tools without ``requires_service``. GET /me's
   ``has_any_service_connected`` skips the always-true ``vulns`` key
   (``config.plugins.connectionless_plugin_ids``).
2. **Tools, not ``authed_get`` entries.** OSV records run to tens of
   kilobytes, its package query is a POST, and the KEV catalog is a single
   ~2 MB file with no query API -- none of which fits the 3 KB inline
   ``authed_get`` gate. The tools query, merge and trim server-side and
   return compact JSON; every tool is in the script bridge so a sandbox
   script can check a whole lockfile.
3. **One plugin, three sources.** A package check is only actionable with
   all three joined: OSV says what is affected and what fixes it, KEV and
   EPSS say which of those to fix first.
"""

from pathlib import Path

from chat.system_skills import SystemSkill
from config.plugin_types import QuestPlugin

from plugins.vulns.tools import SCRIPT_TOOL_ALLOWLIST, VULNS_TOOLS

_PLUGIN_DIR = Path(__file__).parent


def _vulns_skill_content(_base_url: str, _api_key: str) -> str:
    """The system:vulns skill body, from the instructions.md data file."""
    return (_PLUGIN_DIR / "instructions.md").read_text()


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id="vulns",
        label="Vulnerability Data (OSV, KEV, EPSS)",
        system_skills=(
            SystemSkill(
                id="system:vulns",
                name="Vulnerability data",
                description=(
                    "Check package versions for known vulnerabilities (OSV.dev), "
                    "CISA KEV exploited CVEs, EPSS scores."
                ),
                when_to_load=(
                    "Load when the user asks whether a dependency, package version or "
                    "lockfile has known vulnerabilities, about a specific CVE / GHSA "
                    "advisory, whether a CVE is exploited in the wild (KEV), or how "
                    "to prioritize vulnerabilities (EPSS)."
                ),
                # Ungated: the sources need no connection, so there is no
                # connected_services key to gate on.
                requires=None,
                content_builder=_vulns_skill_content,
            ),
        ),
        tools=VULNS_TOOLS,
        script_tool_allowlist=SCRIPT_TOOL_ALLOWLIST,
    )
