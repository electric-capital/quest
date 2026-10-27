# Vulnerability Data (OSV, KEV, EPSS)

## Overview

Read-only access to three free, public vulnerability sources, packaged as the in-tree `plugins/vulns` plugin (plugin id `vulns`; see [Plugin Architecture](../../../docs/architecture/plugins.md)):

- **OSV.dev** (`api.osv.dev`): which published advisories affect a package version and which versions fix them, merged from GitHub Security Advisories, PyPA, RustSec, the Go vulnerability database, distro trackers and other databases under one schema.
- **CISA KEV**: the Known Exploited Vulnerabilities catalog, the CVEs CISA has confirmed are exploited in the wild, published as one JSON feed.
- **FIRST EPSS** (`api.first.org/data/v1/epss`): a daily probability that a CVE sees exploitation activity in the next 30 days, with its percentile.

None of them takes credentials, so the plugin is the **credential-free** shape that SEC EDGAR introduced (no admin card, no Data Connections row, an ungated `system:vulns` skill, see [SEC EDGAR API](../../sec_edgar/docs/sec-edgar-api.md#connected-services-gate)), but its surface is four `tools` rather than `authed_get` entries.

## Key Files

| File | Description |
|------|-------------|
| `plugins/vulns/manifest.py` | The manifest: the ungated `system:vulns` skill, the four tools and the script-bridge allowlist |
| `plugins/vulns/upstream.py` | The httpx client (`make_client`, the test seam), `VulnsError`, the OSV querybatch / record fetchers with the record digest cache, the cached KEV catalog, and the EPSS fetcher |
| `plugins/vulns/osv.py` | OSV record helpers: ecosystem name canonicalization, building a query from a package entry, matching a record's `affected` entries to the queried package, the compact record digest, severity and the CVSS v3 base-score calculator, and grouping one issue's copies across databases |
| `plugins/vulns/tools.py` | The tool handlers and specs (`VULNS_TOOLS`, `SCRIPT_TOOL_ALLOWLIST`) |
| `plugins/vulns/instructions.md` | The `system:vulns` skill body (LLM-facing): which tool for which question, reading the results, the lockfile pattern, prioritization guidance |
| `plugins/vulns/tests/` | Plugin test suite; `fake_upstream.py` is an `httpx.MockTransport` serving trimmed real OSV records, a KEV feed and EPSS answers |

## Tools

Every tool answers a JSON object. Failures are `{"error", "source", "message", "status_code"?}` with codes `vulns_upstream_error` (non-2xx), `vulns_unreachable`, `vulns_timeout`, `vulns_rate_limited`, `vulns_not_found` and `invalid_arguments`; `source` names the upstream (`osv`, `kev` or `epss`). The parameters are in the specs in `plugins/vulns/tools.py`.

- `vulns_check_packages(packages, max_vulns_per_package?, enrich?)`: one `POST /v1/querybatch` for all packages (each `{ecosystem, name, version}` or `{purl}`), then `GET /v1/vulns/{id}` for every advisory id listed, then KEV and EPSS for the CVEs among the ids and aliases. Returns only the vulnerable packages in full, each with one row per issue, worst first (KEV-listed, then severity, CVSS score, EPSS), and lists clean packages and per-package errors separately.
- `vulns_get_vulnerability(id, epss_history?)`: `GET /v1/vulns/{id}` trimmed for reading (details, affected packages, references, related ids each capped), plus the KEV entry and EPSS score of each CVE among the id and aliases. A CVE that OSV does not have still answers with KEV and EPSS.
- `vulns_search_kev(cve_ids?, query?, vendor?, product?, cwe?, added_since?, added_before?, ransomware_only?, limit?, include_epss?)`: filters the cached catalog, newest additions first, with EPSS scores for the returned rows.
- `vulns_get_epss(cve_ids, date?, history?)`: EPSS scores, today's or for a past date, or the 30-day time series.

All four are in the script bridge (`SCRIPT_TOOL_ALLOWLIST`), so a `run_python` script can parse a lockfile and check every pinned package through `POST /api/tool-call` without the full result passing through the conversation.

### Package checks

OSV keeps one record per database, so a package query returns the same issue several times (`GHSA-...`, `PYSEC-...`, `GO-...`, the CVE), each listing the others as aliases. `group_advisories` merges records that share an id or alias, transitively, and the row takes the GitHub advisory's id when there is one (it carries a reviewed severity and summary), else the CVE. Withdrawn records are dropped.

A row's `fixed_in` collects the `fixed` range events of the record's `affected` entries for the queried package only (`package_matcher`: PEP 503 names for PyPI, the entry `purl` for purl queries), so it lists the first fixed version on each release line the advisory covers. Severity is the advisory's own rating (GHSA's `database_specific.severity` or a distro's urgency), else derived from the CVSS v3 score computed from the vector; v2 and v4 vectors are passed through unscored.

### Ecosystems and invalid queries

OSV ecosystem names are case-sensitive, and one invalid query fails a whole querybatch call with HTTP 400 naming its index. `canonical_ecosystem` maps common spellings (`pip`, `golang`, `cargo`, a lowercase distro prefix) to OSV's names and passes anything else through. `osv_query_batch` answers a 400 that names an index by marking that query failed and re-sending the rest, so one bad entry is reported in `errors` and the others still get checked.

## Caching and Limits

- **KEV catalog:** no query API exists, so the feed is downloaded whole and parsed into a by-CVE map, cached in-process for `KEV_TTL_SECONDS`. When a refresh fails the previous copy is served and the answer says it is stale; with no cached copy the call fails.
- **OSV records:** the tools work on a compact digest of each record (a full record can be tens of kilobytes), kept in an in-process LRU keyed by id and `modified` time, so an updated record is fetched again. Up to `OSV_FETCH_CONCURRENCY` record fetches run at once.
- **Read caps:** a distro kernel package lists thousands of advisories across several querybatch pages. A package reads at most `DETAIL_IDS_PER_PACKAGE` records and a call at most `DETAIL_IDS_PER_CALL`; past that the package's result says how many ids were left unread. `max_vulns_per_package` caps the rows returned, not the rows read.
- **Enrichment is best effort:** a KEV or EPSS failure during a package check or a vulnerability lookup is reported in `enrichment_errors` and the OSV part still answers.
- **Not in public projects:** plugin tools are blocked there like every other plugin surface.

Only package names and versions (to OSV) and CVE ids (to EPSS) leave the server; the KEV feed is a plain download.

## Design Decisions

**Why tools instead of `authed_get` entries?** OSV's package query is a POST, its records run to tens of kilobytes, and KEV is a ~2 MB file with no query API. Through `authed_get` every one of those would hit the 3 KB inline size gate and need `output_file` plus a script to read. The tools query, merge and trim server-side and return a compact answer the model can use directly.

**Why one plugin for three sources?** A package check is only actionable with all three joined: OSV says what is affected and what fixes it, KEV and EPSS say what to fix first. Joining them in the tool saves the model three round trips per check and keeps the CVE-alias bookkeeping out of the prompt.

**Why compute CVSS scores?** OSV carries CVSS vectors, not scores, and the v3 base-score formula is short and fixed by the specification. A score reads better than a vector for both the model and the user. CVSS v4 scoring needs the specification's lookup tables, so v4 vectors are left unscored.

**Why merge advisories per issue?** Without merging, a single Jinja issue appears as a GHSA row and a PYSEC row, and the model either double-counts or has to do the alias bookkeeping itself.
