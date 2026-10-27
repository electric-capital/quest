## Vulnerability data -- OSV.dev, CISA KEV, FIRST EPSS

Three free, public vulnerability sources, read through four `vulns_*` tools (called via `tool_call`). Nothing to connect, nothing is changed anywhere.

- **OSV.dev** -- which published advisories affect a package version, and which versions fix them. It merges GitHub Security Advisories (GHSA), PyPA (PYSEC), RustSec, the Go vulnerability database, npm, Maven, NuGet, RubyGems, Packagist, distro trackers (Debian, Ubuntu, Alpine, ...) and more.
- **CISA KEV** (Known Exploited Vulnerabilities) -- CVEs with confirmed exploitation in the wild. A KEV listing is the strongest "fix this first" signal there is.
- **FIRST EPSS** -- a daily estimate (0-1) that a CVE sees exploitation activity in the next 30 days, plus its percentile among all scored CVEs. Most CVEs score well under 0.01; anything above ~0.1 (roughly the top few percent) is unusually likely to be exploited.

### Which tool

| Question | Tool |
|----------|------|
| Is package X version Y vulnerable? Check these dependencies / this lockfile. | `vulns_check_packages` |
| Tell me about CVE-... / GHSA-... (what, which versions, fixes, exploited?) | `vulns_get_vulnerability` |
| Is CVE-... exploited in the wild? What did CISA add to KEV this week? KEV entries for vendor X? | `vulns_search_kev` |
| How likely is CVE-... to be exploited? Rank these CVEs. How has its score moved? | `vulns_get_epss` |

```
tool_call(tool_name="vulns_check_packages", arguments={"packages": [{"ecosystem": "PyPI", "name": "jinja2", "version": "2.11.2"}, {"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}, {"purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"}]})
tool_call(tool_name="vulns_get_vulnerability", arguments={"id": "CVE-2024-3094"})
tool_call(tool_name="vulns_search_kev", arguments={"added_since": "2026-10-01"})
tool_call(tool_name="vulns_search_kev", arguments={"vendor": "Microsoft", "product": "Exchange", "ransomware_only": true})
tool_call(tool_name="vulns_get_epss", arguments={"cve_ids": ["CVE-2023-44487", "CVE-2024-22195"]})
```

### Checking packages (`vulns_check_packages`)

- Each package is `{ecosystem, name, version}` or `{purl}`. Ecosystem names follow OSV: `PyPI`, `npm`, `Go`, `crates.io`, `Maven` (name `group:artifact`), `NuGet`, `RubyGems`, `Packagist` (`vendor/package`), `Pub`, `Hex`, `Hackage`, `CRAN`, `SwiftURL`, `GitHub Actions`, and distro releases such as `Debian:12`, `Ubuntu:22.04:LTS`, `Alpine:v3.20`. Common spellings (`pypi`, `pip`, `golang`, `cargo`, `gem`, `composer`) are accepted. Go versions take the `v` prefix (`v0.10.0`).
- Always pass the exact installed version. Without a version the answer is every advisory ever filed against the package, which says nothing about the user's copy.
- One call takes up to 500 packages; checking them in one call is much faster than one call per package.
- The answer lists only the vulnerable packages (`vulnerable`), plus `not_vulnerable` (no known advisories) and `errors` (an unknown ecosystem, a malformed entry). Each vulnerable package lists its advisories worst first: KEV-listed, then by severity, CVSS score and EPSS.
- One row per issue. OSV holds the same issue under several ids (GHSA-..., PYSEC-..., GO-..., the CVE); they are merged, the row's `id` is the GitHub advisory when there is one, and the others are in `aliases`.
- `fixed_in` lists the first fixed version on **each** release line the advisory covers (e.g. `["1.11.28", "2.2.10", "3.0.3"]`): the fix for the user is the one on their line, or a newer line. An empty `fixed_in` means no fixed version is recorded for this package (say so; `last_affected`, when present, is the newest affected version).
- `severity` is the advisory's own rating (`low` / `medium` / `high` / `critical`, or a distro's urgency such as `unimportant`), `cvss_score` the CVSS v3 base score computed from `cvss_vector` (null when only a v2 / v4 vector exists).
- `kev` (present only when listed) has the KEV `date_added`, `due_date` and `ransomware` ("Known" / "Unknown"). `epss` / `epss_percentile` are the highest score among the row's CVEs (`epss_cve` names it when there are several).
- `max_vulns_per_package` (default 25) caps the rows per package; `omitted` counts the rest. `unread_advisories` appears when a package has so many advisories (a distro kernel has thousands) that only the first 150 were read: say the result is partial.
- `enrichment_errors` means KEV or EPSS could not be reached; the OSV part is still complete.

#### A lockfile or requirements file

Parse the file with `run_python` and pass the pinned versions; for many packages, call the tool from the script and summarize there, so the full result never has to pass through the conversation:

```python
import json, os, urllib.request

def tool(name, arguments):
    req = urllib.request.Request(
        f"http://localhost:{os.environ['QUEST_PORT']}/api/tool-call",
        data=json.dumps({"tool_name": name, "arguments": arguments}).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['QUEST_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    return json.loads(urllib.request.urlopen(req).read())

packages = [{"ecosystem": "PyPI", "name": n, "version": v} for n, v in pins]  # from the parsed file
result = tool("vulns_check_packages", {"packages": packages, "max_vulns_per_package": 100})
```

All four tools are callable this way. Only pinned (resolved) versions give a real answer: for a version range (`>=2.0`) ask for the lockfile or the installed version instead of guessing.

### One vulnerability (`vulns_get_vulnerability`)

Takes any OSV id: `CVE-...`, `GHSA-...`, `PYSEC-...`, `GO-...`, `RUSTSEC-...`, `DEBIAN-CVE-...`, `USN-...`, ... It returns the OSV record trimmed for reading (details up to 2,000 characters, at most 30 affected packages with `introduced` / `fixed` versions, 15 references, ADVISORY and FIX first) plus `kev` (the KEV entry with CISA's `required_action` and `notes`, or null) and `epss` keyed by CVE (`epss_history: true` adds the last 30 days).

A `CVE-...` record often comes from NVD and lists only git commit ranges (`repo`); the package-level ranges live in its GHSA alias, so look that up when the user asks which package versions are affected. A CVE that OSV does not have still answers with KEV and EPSS.

### The KEV catalog (`vulns_search_kev`)

Filters combine (all must match): `cve_ids` (exact; misses listed in `not_in_kev`), `query` (every word must appear in the CVE id, vendor, product, name or description), `vendor` / `product` (contains), `cwe`, `added_since` / `added_before` (YYYY-MM-DD), `ransomware_only`. Rows are newest first with EPSS scores; `match_count` is the full count, `limit` (default 25, max 200) caps the rows. No filters lists the latest additions. `due_date` is the remediation deadline CISA sets for US federal agencies, a useful urgency signal for everyone else.

Not being in KEV does not mean "not exploited", only that CISA has not confirmed exploitation.

### EPSS (`vulns_get_epss`)

Up to 500 CVE ids per call, highest score first; `not_scored` lists CVEs EPSS has no score for (too new, rejected, or not a real id). `date` gives the scores as of a past day; `history: true` (at most 20 CVEs) gives the last 30 days, newest first.

### Prioritizing

When asked what to fix first: KEV-listed issues first, then high EPSS, then CVSS severity. CVSS measures how bad exploitation would be, not how likely it is; a critical-CVSS issue with an EPSS of 0.001 is usually less urgent than a medium one in KEV. Mention when a fix needs a major-version upgrade.

### Limits

- Read-only. What leaves the server is the package names and versions being checked (to OSV.dev) and CVE ids (to EPSS); the KEV catalog is downloaded whole.
- OSV only knows published advisories for open-source packages; it does not scan code, and an unlisted package is "no known advisories", not "secure".
- The KEV catalog is cached by the server for up to 6 hours; EPSS updates once a day.
