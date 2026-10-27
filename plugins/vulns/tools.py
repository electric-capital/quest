"""Vulnerability data tools (plugin module).

Four read-only tools over OSV.dev, the CISA KEV catalog and FIRST EPSS
(clients and caches in plugins/vulns/upstream.py, OSV record helpers in
plugins/vulns/osv.py):

- ``vulns_check_packages``: which advisories affect these package versions,
  merged across databases, with fixed versions, KEV status and EPSS.
- ``vulns_get_vulnerability``: one advisory or CVE in detail.
- ``vulns_search_kev``: look up or filter the KEV catalog.
- ``vulns_get_epss``: EPSS scores (optionally a past date or 30-day history).

Every handler answers a JSON object and converts :class:`VulnsError` into
``{"error": <code>, "source", "message"}`` instead of raising. KEV / EPSS
failures during a package check are reported in ``enrichment_errors`` and
do not fail the check.
"""

from __future__ import annotations

import json
import re

from config.plugin_types import PluginTool

from plugins.vulns import osv
from plugins.vulns.upstream import (
    VulnsError,
    epss_scores,
    kev_catalog,
    make_client,
    osv_get_digests,
    osv_get_vuln,
    osv_query_batch,
)

MAX_PACKAGES = 500
DEFAULT_PER_PACKAGE = 25
MAX_PER_PACKAGE = 100
# Advisory records fetched per package and per call. OSV lists a distro
# kernel with thousands of advisories; past these caps the result says how
# many were left unread instead of fetching them all.
DETAIL_IDS_PER_PACKAGE = 150
DETAIL_IDS_PER_CALL = 1500

KEV_DEFAULT_LIMIT = 25
KEV_MAX_LIMIT = 200
EPSS_MAX_CVES = 500
EPSS_HISTORY_MAX_CVES = 20

DETAILS_LIMIT = 2000
MAX_AFFECTED = 30
MAX_REFERENCES = 15
MAX_RELATED = 20

_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")
_VULN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,99}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REFERENCE_ORDER = {"ADVISORY": 0, "FIX": 1, "REPORT": 2, "WEB": 3, "ARTICLE": 4,
                    "EVIDENCE": 5, "PACKAGE": 6, "INTRODUCED": 7, "DETECTION": 8,
                    "DISCUSSION": 9, "GIT": 10}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _error(code: str, message: str, **extra) -> str:
    return json.dumps({"error": code, "message": message, **extra})


def _upstream_error(exc: VulnsError) -> str:
    extra: dict = {"source": exc.source}
    if exc.status_code is not None:
        extra["status_code"] = exc.status_code
    return _error(exc.code, str(exc), **extra)


def _arg_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _arg_bool(value) -> bool | None:
    """Tri-state boolean argument: None when absent / unparseable."""
    if value is True or value is False:
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    return None


def _arg_int(value, default: int, *, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _arg_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = _arg_str(value)
    return [p.strip() for p in re.split(r"[,\s]+", text) if p.strip()] if text else []


def _arg_date(args: dict, key: str) -> tuple[str | None, str | None]:
    value = _arg_str(args.get(key))
    if value is None:
        return None, None
    if not _DATE_RE.match(value):
        return None, f"{key} must be a date in YYYY-MM-DD form."
    return value, None


def _cve_list(args: dict, key: str) -> tuple[list[str], str | None]:
    cves = list(dict.fromkeys(c.upper() for c in _arg_list(args.get(key))))
    bad = [c for c in cves if not _CVE_RE.match(c)]
    if bad:
        return [], f"Not CVE ids: {', '.join(bad[:10])} (expected CVE-YYYY-NNNN)."
    return cves, None


def _kev_brief(entry: dict) -> dict:
    return {
        "cve": entry.get("cveID"),
        "name": entry.get("vulnerabilityName"),
        "date_added": entry.get("dateAdded"),
        "due_date": entry.get("dueDate"),
        "ransomware": entry.get("knownRansomwareCampaignUse"),
    }


async def _enrich(client, cves: list[str]) -> tuple[dict | None, dict | None, list[dict]]:
    """``(KEV catalog, EPSS by CVE, errors)``; a failed source is None."""
    errors: list[dict] = []
    kev = epss = None
    if not cves:
        return None, {}, errors
    try:
        kev = await kev_catalog(client)
        if kev.get("stale"):
            errors.append({"source": "kev", "message": (
                "The KEV feed could not be refreshed; this used the copy "
                "fetched earlier.")})
    except VulnsError as exc:
        errors.append({"source": exc.source, "message": str(exc)})
    try:
        epss = await epss_scores(client, cves)
    except VulnsError as exc:
        errors.append({"source": exc.source, "message": str(exc)})
    return kev, epss, errors


# ---------------------------------------------------------------------------
# vulns_check_packages
# ---------------------------------------------------------------------------

def _advisory_row(group: list[dict], matches) -> dict:
    """One row for a group of digests describing the same issue."""
    primary = group[0]
    every_id = {primary["id"]}
    for item in group:
        every_id.add(item["id"])
        every_id.update(item["aliases"])
    scored = [d for d in group if d["cvss_score"] is not None]
    best = max(scored, key=lambda d: d["cvss_score"]) if scored else None
    vector = (best or next((d for d in group if d["cvss_vector"]), {})).get("cvss_vector")
    labels = [d["severity"] for d in group if d["severity"]]
    fixed: list[str] = []
    last_affected: list[str] = []
    for item in group:
        entries = [e for e in item["affected"] if matches(e)]
        if not entries and len(item["affected"]) == 1:
            entries = item["affected"]
        for entry in entries:
            fixed.extend(v for v in entry["fixed"] if v not in fixed)
            last_affected.extend(v for v in entry["last_affected"] if v not in last_affected)
    published = sorted(d["published"] for d in group if d["published"])
    row = {
        "id": primary["id"],
        "aliases": sorted(every_id - {primary["id"]}),
        "summary": next((d["summary"] for d in group if d["summary"]), None),
        "severity": max(labels, key=osv.label_rank) if labels else None,
        "cvss_score": best["cvss_score"] if best else None,
        "cvss_vector": vector,
        "fixed_in": fixed,
        "published": published[0] if published else None,
    }
    if not fixed and last_affected:
        row["last_affected"] = last_affected
    row["_cves"] = osv.cve_ids(sorted(every_id))
    return row


def _row_sort_key(row: dict):
    return (
        0 if row.get("kev") else 1,
        -osv.label_rank(row.get("severity")),
        -(row.get("cvss_score") or 0),
        -(row.get("epss") or 0),
        row["id"],
    )


def _attach_enrichment(row: dict, kev: dict | None, epss: dict | None) -> None:
    cves = row.pop("_cves")
    if kev is not None:
        listed = next((kev["by_cve"][c] for c in cves if c in kev["by_cve"]), None)
        if listed is not None:
            row["kev"] = _kev_brief(listed)
    if epss:
        scored = [(c, epss[c]) for c in cves if c in epss and epss[c]["epss"] is not None]
        if scored:
            cve, best = max(scored, key=lambda pair: pair[1]["epss"])
            row["epss"] = best["epss"]
            row["epss_percentile"] = best["percentile"]
            if len(cves) > 1:
                row["epss_cve"] = cve


async def _tool_check_packages(ctx, args: dict) -> str:
    raw = args.get("packages")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return _error("invalid_arguments", "packages must be a non-empty list.")
    if len(raw) > MAX_PACKAGES:
        return _error("invalid_arguments",
                      f"At most {MAX_PACKAGES} packages per call; split the list.")
    per_package = _arg_int(args.get("max_vulns_per_package"), DEFAULT_PER_PACKAGE,
                           lo=1, hi=MAX_PER_PACKAGE)
    enrich = _arg_bool(args.get("enrich")) is not False

    entries: list[dict] = []
    errors: list[dict] = []
    for item in raw:
        query, echo, problem = osv.build_query(item)
        if problem:
            errors.append({"package": echo if echo is not None else item, "error": problem})
        else:
            entries.append({"query": query, "package": echo})

    out: dict = {}
    try:
        async with make_client() as client:
            batch = await osv_query_batch(
                client, [e["query"] for e in entries],
                max_ids_per_query=DETAIL_IDS_PER_PACKAGE,
            )
            refs: dict[str, dict] = {}
            for entry, result in zip(entries, batch):
                entry["result"] = result
                if "error" in result:
                    continue
                listed = result["vulns"]
                take = []
                for ref in listed[:DETAIL_IDS_PER_PACKAGE]:
                    if ref["id"] in refs or len(refs) < DETAIL_IDS_PER_CALL:
                        refs.setdefault(ref["id"], ref)
                        take.append(ref)
                entry["ids"] = [r["id"] for r in take]
                entry["unread"] = len(listed) - len(take)
                entry["more"] = result["more"]
            digests = await osv_get_digests(client, list(refs.values()), osv.digest)

            all_cves: set[str] = set()
            for entry in entries:
                if "ids" not in entry:
                    continue
                items = [digests[i] for i in entry["ids"]
                         if i in digests and not digests[i]["withdrawn"]]
                matches = osv.package_matcher(entry["package"])
                entry["rows"] = [_advisory_row(g, matches) for g in osv.group_advisories(items)]
                for row in entry["rows"]:
                    all_cves.update(row["_cves"])
            kev = epss = None
            if enrich:
                kev, epss, enrichment_errors = await _enrich(client, sorted(all_cves))
                if enrichment_errors:
                    out["enrichment_errors"] = enrichment_errors
    except VulnsError as exc:
        return _upstream_error(exc)

    vulnerable: list[dict] = []
    clean: list[dict] = []
    advisory_total = kev_total = 0
    for entry in entries:
        result = entry["result"]
        if "error" in result:
            errors.append({"package": entry["package"], "error": result["error"]})
            continue
        rows = entry["rows"]
        for row in rows:
            _attach_enrichment(row, kev, epss)
        if not rows and not entry["unread"] and not entry["more"]:
            clean.append(entry["package"])
            continue
        rows.sort(key=_row_sort_key)
        advisory_total += len(rows)
        kev_total += sum(1 for r in rows if r.get("kev"))
        item = {"package": entry["package"], "advisory_count": len(rows),
                "vulnerabilities": rows[:per_package]}
        if len(rows) > per_package:
            item["omitted"] = len(rows) - per_package
        if entry["unread"] or entry["more"]:
            item["unread_advisories"] = (
                f"{entry['unread']}{'+' if entry['more'] else ''} more advisory ids "
                "were not read (per-call cap); results for this package are partial."
            )
        if "version" not in entry["package"] and "purl" not in entry["package"]:
            item["note"] = "No version given: every advisory ever filed for this package."
        vulnerable.append(item)

    out.update({
        "packages_checked": len(raw),
        "packages_vulnerable": len(vulnerable),
        "advisories": advisory_total,
        "in_kev": kev_total if kev is not None else None,
        "vulnerable": vulnerable,
        "not_vulnerable": clean,
    })
    if errors:
        out["errors"] = errors
    return json.dumps(out)


# ---------------------------------------------------------------------------
# vulns_get_vulnerability
# ---------------------------------------------------------------------------

def _normalize_vuln_id(raw: str) -> str:
    if raw.upper().startswith("CVE-"):
        return raw.upper()
    if raw.upper().startswith("GHSA-"):
        return "GHSA-" + raw[5:].lower()
    return raw


def _affected_view(record: dict) -> tuple[list[dict], int]:
    rows = []
    for entry in record.get("affected") or []:
        if not isinstance(entry, dict):
            continue
        package = entry.get("package") if isinstance(entry.get("package"), dict) else {}
        events = osv.range_events(entry)
        row = {
            "ecosystem": package.get("ecosystem"),
            "name": package.get("name"),
            "introduced": events["introduced"][:10],
            "fixed": events["fixed"][:10],
        }
        if events["last_affected"]:
            row["last_affected"] = events["last_affected"][:10]
        if not package and entry.get("ranges"):
            repos = [r.get("repo") for r in entry["ranges"]
                     if isinstance(r, dict) and r.get("repo")]
            if repos:
                row["repo"] = repos[0]
        rows.append(row)
    return rows[:MAX_AFFECTED], len(rows)


def _references_view(record: dict) -> tuple[list[dict], int]:
    refs = [r for r in record.get("references") or []
            if isinstance(r, dict) and isinstance(r.get("url"), str)]
    ordered = sorted(enumerate(refs), key=lambda pair: (
        _REFERENCE_ORDER.get(str(pair[1].get("type")), 99), pair[0]))
    return [{"type": r.get("type"), "url": r["url"]}
            for _, r in ordered[:MAX_REFERENCES]], len(refs)


async def _tool_get_vulnerability(ctx, args: dict) -> str:
    raw = _arg_str(args.get("id"))
    if not raw or not _VULN_ID_RE.match(raw):
        return _error("invalid_arguments",
                      "id is required: an OSV, GHSA, CVE, PYSEC, GO, RUSTSEC, ... id.")
    vuln_id = _normalize_vuln_id(raw)
    history = _arg_bool(args.get("epss_history")) is True

    out: dict = {"id": vuln_id}
    try:
        async with make_client() as client:
            record = await osv_get_vuln(client, vuln_id)
            if record is None and not vuln_id.startswith("CVE-"):
                return _error("vulns_not_found", f"OSV.dev has no record {vuln_id}.",
                              source="osv")
            if record is not None:
                digest = osv.digest(record)
                details = record.get("details")
                affected, affected_total = _affected_view(record)
                references, references_total = _references_view(record)
                related = [r for r in record.get("related") or [] if isinstance(r, str)]
                out.update({
                    "id": record.get("id") or vuln_id,
                    "aliases": digest["aliases"],
                    "summary": record.get("summary"),
                    "details": osv.truncate(details.strip(), DETAILS_LIMIT)
                    if isinstance(details, str) and details.strip() else None,
                    "severity": digest["severity"],
                    "cvss_score": digest["cvss_score"],
                    "cvss_vector": digest["cvss_vector"],
                    "cwes": digest["cwes"],
                    "published": digest["published"],
                    "modified": (record.get("modified") or "")[:10] or None,
                    "affected": affected,
                    "references": references,
                })
                if digest["withdrawn"]:
                    out["withdrawn"] = digest["withdrawn"]
                if affected_total > len(affected):
                    out["affected_total"] = affected_total
                if references_total > len(references):
                    out["references_total"] = references_total
                if related:
                    out["related"] = related[:MAX_RELATED]
                    if len(related) > MAX_RELATED:
                        out["related_total"] = len(related)
                cves = osv.cve_ids([out["id"], *digest["aliases"]])
            else:
                out["osv"] = "OSV.dev has no record for this CVE; KEV and EPSS below."
                cves = [vuln_id]

            if cves:
                errors: list[dict] = []
                try:
                    kev = await kev_catalog(client)
                    listed = [kev["by_cve"][c] for c in cves if c in kev["by_cve"]]
                    out["kev"] = [{
                        **_kev_brief(e),
                        "vendor": e.get("vendorProject"),
                        "product": e.get("product"),
                        "required_action": osv.truncate(e.get("requiredAction") or "", 400),
                        "notes": osv.truncate(e.get("notes") or "", 600),
                    } for e in listed] or None
                except VulnsError as exc:
                    errors.append({"source": exc.source, "message": str(exc)})
                try:
                    out["epss"] = await epss_scores(client, cves, history=history) or None
                except VulnsError as exc:
                    errors.append({"source": exc.source, "message": str(exc)})
                if errors:
                    out["enrichment_errors"] = errors
    except VulnsError as exc:
        return _upstream_error(exc)
    return json.dumps(out)


# ---------------------------------------------------------------------------
# vulns_search_kev
# ---------------------------------------------------------------------------

async def _tool_search_kev(ctx, args: dict) -> str:
    cves, problem = _cve_list(args, "cve_ids")
    if problem:
        return _error("invalid_arguments", problem)
    added_since, problem = _arg_date(args, "added_since")
    if problem:
        return _error("invalid_arguments", problem)
    added_before, problem = _arg_date(args, "added_before")
    if problem:
        return _error("invalid_arguments", problem)
    terms = [t.lower() for t in (_arg_str(args.get("query")) or "").split()]
    vendor = (_arg_str(args.get("vendor")) or "").lower()
    product = (_arg_str(args.get("product")) or "").lower()
    cwe = (_arg_str(args.get("cwe")) or "").upper()
    if cwe and not cwe.startswith("CWE-"):
        cwe = f"CWE-{cwe}"
    ransomware_only = _arg_bool(args.get("ransomware_only")) is True
    limit = _arg_int(args.get("limit"), KEV_DEFAULT_LIMIT, lo=1, hi=KEV_MAX_LIMIT)
    include_epss = _arg_bool(args.get("include_epss")) is not False

    try:
        async with make_client() as client:
            catalog = await kev_catalog(client)
            wanted = set(cves)
            matches = []
            for entry in catalog["entries"]:
                if wanted and entry["cveID"].upper() not in wanted:
                    continue
                date_added = entry.get("dateAdded") or ""
                if added_since and date_added < added_since:
                    continue
                if added_before and date_added >= added_before:
                    continue
                if vendor and vendor not in (entry.get("vendorProject") or "").lower():
                    continue
                if product and product not in (entry.get("product") or "").lower():
                    continue
                if ransomware_only and entry.get("knownRansomwareCampaignUse") != "Known":
                    continue
                if cwe and cwe not in (entry.get("cwes") or []):
                    continue
                if terms:
                    haystack = " ".join(str(entry.get(k) or "") for k in (
                        "cveID", "vendorProject", "product", "vulnerabilityName",
                        "shortDescription")).lower()
                    if not all(t in haystack for t in terms):
                        continue
                matches.append(entry)
            matches.sort(key=lambda e: (e.get("dateAdded") or "", e["cveID"]), reverse=True)
            shown = matches[:limit]
            epss: dict = {}
            epss_error = None
            if include_epss and shown:
                try:
                    epss = await epss_scores(client, [e["cveID"] for e in shown])
                except VulnsError as exc:
                    epss_error = str(exc)
    except VulnsError as exc:
        return _upstream_error(exc)

    rows = []
    for entry in shown:
        row = {
            **_kev_brief(entry),
            "vendor": entry.get("vendorProject"),
            "product": entry.get("product"),
            "description": osv.truncate(entry.get("shortDescription") or "", 240),
            "cwes": entry.get("cwes") or [],
        }
        if wanted:
            row["required_action"] = osv.truncate(entry.get("requiredAction") or "", 400)
            row["notes"] = osv.truncate(entry.get("notes") or "", 600)
        score = epss.get(entry["cveID"].upper())
        if score:
            row["epss"] = score["epss"]
            row["epss_percentile"] = score["percentile"]
        rows.append(row)

    out: dict = {
        "catalog_version": catalog.get("catalog_version"),
        "catalog_released": catalog.get("date_released"),
        "catalog_count": len(catalog["entries"]),
        "match_count": len(matches),
        "returned": len(rows),
        "rows": rows,
    }
    if wanted:
        found = {e["cveID"].upper() for e in matches}
        out["not_in_kev"] = [c for c in cves if c not in found]
    if catalog.get("stale"):
        out["stale"] = "The KEV feed could not be refreshed; this is the copy fetched earlier."
    if epss_error:
        out["enrichment_errors"] = [{"source": "epss", "message": epss_error}]
    return json.dumps(out)


# ---------------------------------------------------------------------------
# vulns_get_epss
# ---------------------------------------------------------------------------

async def _tool_get_epss(ctx, args: dict) -> str:
    cves, problem = _cve_list(args, "cve_ids")
    if problem:
        return _error("invalid_arguments", problem)
    if not cves:
        return _error("invalid_arguments", "cve_ids is required (one or more CVE ids).")
    if len(cves) > EPSS_MAX_CVES:
        return _error("invalid_arguments", f"At most {EPSS_MAX_CVES} CVE ids per call.")
    date, problem = _arg_date(args, "date")
    if problem:
        return _error("invalid_arguments", problem)
    history = _arg_bool(args.get("history")) is True
    if history and len(cves) > EPSS_HISTORY_MAX_CVES:
        return _error("invalid_arguments",
                      f"history is limited to {EPSS_HISTORY_MAX_CVES} CVE ids per call.")
    if history and date:
        return _error("invalid_arguments", "Pass either date or history, not both.")
    try:
        async with make_client() as client:
            scores = await epss_scores(client, cves, date=date, history=history)
    except VulnsError as exc:
        return _upstream_error(exc)
    rows = sorted(({"cve": cve, **scores[cve]} for cve in cves if cve in scores),
                  key=lambda r: -(r["epss"] or 0))
    out: dict = {"scores": rows}
    missing = [c for c in cves if c not in scores]
    if missing:
        out["not_scored"] = missing
    return json.dumps(out)


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

def _intent(example: str) -> dict:
    return {
        "type": "string",
        "description": (
            "A brief, user-friendly summary of your intent (max 50 characters). "
            f"Example: '{example}'."
        ),
    }


def _tool(name: str, description: str, properties: dict, required: list[str],
          handler) -> PluginTool:
    return PluginTool(
        spec={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        handler=handler,
    )


_CVE_IDS_PARAM = {
    "type": "array",
    "items": {"type": "string"},
    "description": "CVE ids, e.g. [\"CVE-2021-44228\"].",
}

CHECK_PACKAGES_TOOL = _tool(
    "vulns_check_packages",
    "Check package versions against OSV.dev (GitHub advisories, PyPA, RustSec, Go, "
    "npm, Maven, distro trackers, ...). Per vulnerable package: advisories merged "
    "across databases (id, aliases incl. CVE, summary, severity, CVSS, fixed_in "
    "versions), each flagged if it is in CISA KEV and scored with EPSS; worst first. "
    f"Up to {MAX_PACKAGES} packages per call.",
    {
        "packages": {
            "type": "array",
            "description": (
                "Packages to check: {ecosystem, name, version} (ecosystem e.g. PyPI, npm, "
                "Go, crates.io, Maven, NuGet, RubyGems, Packagist, Debian:12) or {purl} "
                "(e.g. pkg:pypi/jinja2@2.11.2). Omitting version lists every advisory "
                "ever filed for the package."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "ecosystem": {"type": "string"},
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "purl": {"type": "string"},
                },
            },
        },
        "max_vulns_per_package": {
            "type": "integer",
            "description": f"Advisories listed per package (default {DEFAULT_PER_PACKAGE}, "
                           f"max {MAX_PER_PACKAGE}); the rest are counted in omitted.",
        },
        "enrich": {
            "type": "boolean",
            "description": "Add KEV and EPSS to each advisory (default true).",
        },
        "intent_message": _intent("Check dependencies for vulnerabilities"),
    },
    ["packages"],
    _tool_check_packages,
)

GET_VULNERABILITY_TOOL = _tool(
    "vulns_get_vulnerability",
    "One vulnerability by id (CVE, GHSA, PYSEC, GO, RUSTSEC, OSV, distro ids, ...): "
    "OSV record (summary, details, severity / CVSS, CWEs, affected packages with "
    "introduced / fixed versions, references) plus its CISA KEV entry and EPSS score. "
    "A CVE that OSV lacks still gets KEV and EPSS.",
    {
        "id": {"type": "string", "description": "e.g. CVE-2024-3094, GHSA-h5c8-rqwp-cp95."},
        "epss_history": {
            "type": "boolean",
            "description": "Include the last 30 days of EPSS scores (default false).",
        },
        "intent_message": _intent("Look up CVE-2024-3094"),
    },
    ["id"],
    _tool_get_vulnerability,
)

SEARCH_KEV_TOOL = _tool(
    "vulns_search_kev",
    "Search CISA's Known Exploited Vulnerabilities catalog (CVEs exploited in the "
    "wild): by CVE ids, free text, vendor, product, CWE, date added, or known "
    "ransomware use. Newest additions first, each with its EPSS score. With no "
    "filters, lists the latest additions.",
    {
        "cve_ids": {**_CVE_IDS_PARAM,
                    "description": "Exact CVE ids to look up (not_in_kev lists the misses)."},
        "query": {"type": "string",
                  "description": "Words that must all appear in the CVE id, vendor, product, "
                                 "name or description."},
        "vendor": {"type": "string", "description": "Vendor contains, e.g. Microsoft."},
        "product": {"type": "string", "description": "Product contains, e.g. Exchange."},
        "cwe": {"type": "string", "description": "CWE id, e.g. CWE-787."},
        "added_since": {"type": "string", "description": "Added on or after (YYYY-MM-DD)."},
        "added_before": {"type": "string", "description": "Added before (YYYY-MM-DD)."},
        "ransomware_only": {"type": "boolean",
                            "description": "Only entries with known ransomware campaign use."},
        "limit": {"type": "integer",
                  "description": f"Rows returned (default {KEV_DEFAULT_LIMIT}, max {KEV_MAX_LIMIT}); "
                                 "match_count is the full count."},
        "include_epss": {"type": "boolean", "description": "Add EPSS scores (default true)."},
        "intent_message": _intent("Check recent KEV additions"),
    },
    [],
    _tool_search_kev,
)

GET_EPSS_TOOL = _tool(
    "vulns_get_epss",
    "FIRST EPSS scores for CVEs: the probability (0-1) of exploitation activity in "
    "the next 30 days, and its percentile among all scored CVEs. Today's scores by "
    "default, or a past date, or the last 30 days.",
    {
        "cve_ids": {**_CVE_IDS_PARAM,
                    "description": f"CVE ids (max {EPSS_MAX_CVES}; max "
                                   f"{EPSS_HISTORY_MAX_CVES} with history)."},
        "date": {"type": "string", "description": "Scores as of this date (YYYY-MM-DD)."},
        "history": {"type": "boolean", "description": "Daily scores for the last 30 days."},
        "intent_message": _intent("Get EPSS scores"),
    },
    ["cve_ids"],
    _tool_get_epss,
)

VULNS_TOOLS: tuple[PluginTool, ...] = (
    CHECK_PACKAGES_TOOL,
    GET_VULNERABILITY_TOOL,
    SEARCH_KEV_TOOL,
    GET_EPSS_TOOL,
)

# Every tool is a read-only lookup with no workspace dependency, so sandbox
# scripts may call them via POST /api/tool-call (e.g. to check a whole
# lockfile parsed in run_python).
SCRIPT_TOOL_ALLOWLIST = frozenset(t.spec["name"] for t in VULNS_TOOLS)
