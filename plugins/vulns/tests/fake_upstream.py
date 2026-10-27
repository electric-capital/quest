"""In-memory OSV.dev, CISA KEV and FIRST EPSS for the vulns plugin tests.

``FakeUpstream.make_client`` stands in for ``plugins.vulns.upstream.make_client``:
a real client over an ``httpx.MockTransport`` answering with the upstream
response shapes (trimmed real records), so the tools' parsing, merging and
error paths run unchanged. ``calls`` records ``(method, host, path, params,
body)``; ``fail`` names sources (``"osv"``, ``"kev"``, ``"epss"``) that
answer HTTP 503.
"""

from __future__ import annotations

import json

import httpx

from plugins.vulns import upstream


def _ghsa(ghsa_id, *, aliases, summary, severity, vector, ecosystem, name, fixed,
          published="2024-01-11T15:20:48Z", purl=None, cwes=(), withdrawn=None,
          references=None, extra_fixed=()):
    record = {
        "id": ghsa_id,
        "summary": summary,
        "details": f"Details of {ghsa_id}.\n\nSecond paragraph.",
        "aliases": list(aliases),
        "modified": "2026-09-10T03:50:00Z",
        "published": published,
        "database_specific": {"cwe_ids": list(cwes), "severity": severity,
                              "github_reviewed": True},
        "references": references if references is not None else [
            {"type": "WEB", "url": f"https://example.org/{ghsa_id}"},
            {"type": "ADVISORY", "url": f"https://nvd.nist.gov/vuln/detail/{aliases[0]}"},
        ],
        "affected": [{
            "package": {"name": name, "ecosystem": ecosystem,
                        **({"purl": purl} if purl else {})},
            "ranges": [{"type": "ECOSYSTEM",
                        "events": [{"introduced": "0"}, {"fixed": fixed}]}],
        }] + [{
            "package": {"name": name, "ecosystem": ecosystem,
                        **({"purl": purl} if purl else {})},
            "ranges": [{"type": "ECOSYSTEM",
                        "events": [{"introduced": intro}, {"fixed": fix}]}],
        } for intro, fix in extra_fixed],
        "schema_version": "1.7.3",
        "severity": [{"type": "CVSS_V3", "score": vector}] if vector else [],
    }
    if withdrawn:
        record["withdrawn"] = withdrawn
    return record


JINJA_XMLATTR = _ghsa(
    "GHSA-h5c8-rqwp-cp95", aliases=["CVE-2024-22195", "PYSEC-2026-1473"],
    summary="Jinja vulnerable to HTML attribute injection when passing user input as keys to xmlattr filter",
    severity="MODERATE", vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N",
    ecosystem="PyPI", name="jinja2", purl="pkg:pypi/jinja2", fixed="3.1.3", cwes=["CWE-79"],
)
# PyPA's copy of the same issue: no summary or severity of its own.
JINJA_XMLATTR_PYSEC = {
    "id": "PYSEC-2026-1473",
    "details": "Jinja is an extensible templating engine. The xmlattr filter accepts keys containing spaces.",
    "aliases": ["CVE-2024-22195", "GHSA-h5c8-rqwp-cp95"],
    "modified": "2026-07-07T17:46:13Z",
    "published": "2024-01-11T03:15:11Z",
    "affected": [{"package": {"name": "jinja2", "ecosystem": "PyPI", "purl": "pkg:pypi/jinja2"},
                  "ranges": [{"type": "ECOSYSTEM",
                              "events": [{"introduced": "0"}, {"fixed": "3.1.3"}]}]}],
}
JINJA_SANDBOX = _ghsa(
    "GHSA-q2x7-8rv6-6q7h", aliases=["CVE-2024-56326", "PYSEC-2026-1475"],
    summary="Jinja has a sandbox breakout through indirect reference to format method",
    severity="HIGH", vector="CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H",
    ecosystem="PyPI", name="jinja2", purl="pkg:pypi/jinja2", fixed="3.1.5",
    published="2024-12-23T00:00:00Z",
)
JINJA_WITHDRAWN = _ghsa(
    "GHSA-wwww-wwww-wwww", aliases=["CVE-2020-99999"], summary="Withdrawn duplicate",
    severity="CRITICAL", vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    ecosystem="PyPI", name="jinja2", fixed="9.0.0", withdrawn="2025-01-01T00:00:00Z",
)
LOG4SHELL_GHSA = _ghsa(
    "GHSA-jfh8-c2jp-5v3q", aliases=["CVE-2021-44228"],
    summary="Remote code injection in Log4j", severity="CRITICAL",
    vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H",
    ecosystem="Maven", name="org.apache.logging.log4j:log4j-core",
    purl="pkg:maven/org.apache.logging.log4j/log4j-core", fixed="2.15.0",
    extra_fixed=[("2.13.0", "2.12.2"), ("2.4", "2.3.1")], published="2021-12-10T00:00:40Z",
    cwes=["CWE-502"],
    references=[{"type": "WEB", "url": f"https://example.org/web/{i}"} for i in range(17)]
    + [{"type": "FIX", "url": "https://github.com/apache/logging-log4j2/pull/608"},
       {"type": "ADVISORY", "url": "https://logging.apache.org/log4j/2.x/security.html"},
       {"type": "PACKAGE", "url": "https://github.com/apache/logging-log4j2"}],
)
# NVD-derived CVE record: git ranges only, the package ranges live in the GHSA.
LOG4SHELL_CVE = {
    "id": "CVE-2021-44228",
    "details": "Apache Log4j2 2.0-beta9 through 2.15.0 JNDI features ... do not protect against attacker controlled LDAP endpoints.",
    "aliases": ["GHSA-jfh8-c2jp-5v3q"],
    "related": [f"SUSE-SU-2021:{4000 + i}-1" for i in range(25)],
    "modified": "2026-10-24T00:00:00Z",
    "published": "2021-12-10T10:15:09Z",
    "references": [{"type": "ADVISORY", "url": "https://logging.apache.org/log4j/2.x/security.html"}],
    "affected": [{"ranges": [{"type": "GIT", "repo": "https://github.com/apache/logging-log4j2",
                              "events": [{"introduced": "0"}, {"fixed": "c30a1398a6697fb832c650870c44284d0052103e"}]}]}],
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"}],
}
XNET_RAPID_RESET = _ghsa(
    "GHSA-qppj-fm5r-hxr3", aliases=["CVE-2023-44487", "GO-2023-2102"],
    summary="HTTP/2 Stream Cancellation Attack", severity="MODERATE",
    vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L",
    ecosystem="Go", name="golang.org/x/net", fixed="0.17.0",
)
XNET_HIGH = _ghsa(
    "GHSA-4374-p667-p6c8", aliases=["CVE-2023-39325", "GO-2023-2101"],
    summary="HTTP/2 rapid reset can cause excessive work in net/http", severity="HIGH",
    vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H",
    ecosystem="Go", name="golang.org/x/net", fixed="0.17.0",
)
# The Go database's own copy of the rapid-reset entry, aliasing the GHSA.
XNET_GO = {
    "id": "GO-2023-2102",
    "summary": "HTTP/2 rapid reset can cause excessive work in net/http",
    "aliases": ["CVE-2023-44487", "GHSA-qppj-fm5r-hxr3"],
    "modified": "2024-05-20T00:00:00Z",
    "published": "2023-10-11T00:00:00Z",
    "affected": [{"package": {"name": "golang.org/x/net", "ecosystem": "Go"},
                  "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "0.17.0"}]}]}],
}

RECORDS = {r["id"]: r for r in (
    JINJA_XMLATTR, JINJA_XMLATTR_PYSEC, JINJA_SANDBOX, JINJA_WITHDRAWN,
    LOG4SHELL_GHSA, LOG4SHELL_CVE, XNET_RAPID_RESET, XNET_HIGH, XNET_GO,
)}

# (ecosystem, lowercased name) -> advisory ids OSV lists for an affected version.
PACKAGES = {
    ("PyPI", "jinja2"): ["GHSA-h5c8-rqwp-cp95", "PYSEC-2026-1473",
                         "GHSA-q2x7-8rv6-6q7h", "GHSA-wwww-wwww-wwww"],
    ("Maven", "org.apache.logging.log4j:log4j-core"): ["GHSA-jfh8-c2jp-5v3q"],
    ("Go", "golang.org/x/net"): ["GHSA-4374-p667-p6c8", "GO-2023-2102",
                                "GHSA-qppj-fm5r-hxr3"],
}
PURLS = {
    "pkg:pypi/jinja2": ("PyPI", "jinja2"),
    "pkg:maven/org.apache.logging.log4j/log4j-core": ("Maven", "org.apache.logging.log4j:log4j-core"),
}
# Versions with no advisories.
CLEAN = {("PyPI", "jinja2", "3.1.6"), ("PyPI", "requests", "2.32.4")}
# A distro kernel: more advisories than the per-package read cap, paged.
BIG_PACKAGE = ("Debian:12", "linux")
BIG_PAGE_SIZE = 1000
BIG_TOTAL = 1400
ECOSYSTEMS = {"PyPI", "npm", "Go", "Maven", "Debian:12"}


def _big_record(vuln_id: str) -> dict:
    number = int(vuln_id.rsplit("-", 1)[1])
    return {
        "id": vuln_id,
        "details": f"Kernel issue {number}.",
        "aliases": [],
        "upstream": [f"CVE-2020-{number}"],
        "modified": "2026-01-01T00:00:00Z",
        "published": "2020-01-01T00:00:00Z",
        "affected": [{"package": {"name": "linux", "ecosystem": "Debian:12"},
                      "ecosystem_specific": {"urgency": "low" if number % 2 else "unimportant"},
                      "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}]}],
    }


KEV_FEED = {
    "title": "CISA Catalog of Known Exploited Vulnerabilities",
    "catalogVersion": "2026.10.26",
    "dateReleased": "2026-10-26T22:24:24.6101Z",
    "count": 4,
    "vulnerabilities": [
        {"cveID": "CVE-2026-1000", "vendorProject": "PHP Group", "product": "PHP",
         "vulnerabilityName": "PHP Memory Corruption Vulnerability", "dateAdded": "2026-10-26",
         "shortDescription": "PHP contains an out-of-memory corruption vulnerability that allows for unauthenticated remote code execution.",
         "requiredAction": "Apply mitigations per vendor instructions.", "dueDate": "2026-10-29",
         "knownRansomwareCampaignUse": "Unknown", "notes": "https://www.php.net/", "cwes": ["CWE-416"]},
        {"cveID": "CVE-2023-44487", "vendorProject": "IETF", "product": "HTTP/2",
         "vulnerabilityName": "HTTP/2 Rapid Reset Attack Vulnerability", "dateAdded": "2023-10-10",
         "shortDescription": "HTTP/2 contains a rapid reset vulnerability that allows for a distributed denial-of-service attack (DDoS).",
         "requiredAction": "Apply mitigations per vendor instructions.", "dueDate": "2023-10-31",
         "knownRansomwareCampaignUse": "Unknown", "notes": "", "cwes": ["CWE-400"]},
        {"cveID": "CVE-2021-45046", "vendorProject": "Apache", "product": "Log4j2",
         "vulnerabilityName": "Apache Log4j2 Deserialization of Untrusted Data Vulnerability",
         "dateAdded": "2023-05-01",
         "shortDescription": "Apache Log4j2 contains a deserialization of untrusted data vulnerability due to the incomplete fix of CVE-2021-44228.",
         "requiredAction": "Apply updates per vendor instructions.", "dueDate": "2023-05-22",
         "knownRansomwareCampaignUse": "Known", "notes": "", "cwes": ["CWE-917"]},
        {"cveID": "CVE-2021-44228", "vendorProject": "Apache", "product": "Log4j2",
         "vulnerabilityName": "Apache Log4j2 Remote Code Execution Vulnerability",
         "dateAdded": "2021-12-10",
         "shortDescription": "Apache Log4j2 contains a vulnerability where JNDI features do not protect against attacker-controlled JNDI-related endpoints, allowing for remote code execution.",
         "requiredAction": "For all affected software assets for which updates exist, the only acceptable remediation actions are: 1) Apply updates; OR 2) remove affected assets from agency networks.",
         "dueDate": "2021-12-24", "knownRansomwareCampaignUse": "Known",
         "notes": "https://nvd.nist.gov/vuln/detail/CVE-2021-44228",
         "cwes": ["CWE-20", "CWE-400", "CWE-502"]},
    ],
}

EPSS = {
    "CVE-2021-44228": ("0.999990000", "1.000000000"),
    "CVE-2021-45046": ("0.999770000", "0.999800000"),
    "CVE-2023-44487": ("0.944000000", "0.998900000"),
    "CVE-2023-39325": ("0.003100000", "0.540000000"),
    "CVE-2024-22195": ("0.008920000", "0.591610000"),
    "CVE-2024-56326": ("0.005220000", "0.440080000"),
    "CVE-2026-1000": ("0.999810000", "0.999810000"),
}
EPSS_DATE = "2026-10-26"


class FakeUpstream:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail: set[str] = set()

    # -- transport -------------------------------------------------------

    def make_client(self) -> httpx.AsyncClient:
        return upstream.make_client(transport=httpx.MockTransport(self._handle))

    def calls_to(self, path_prefix: str) -> list[tuple]:
        return [c for c in self.calls if c[2].startswith(path_prefix)]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, host, path, params, body))
        source = {"api.osv.dev": "osv", "www.cisa.gov": "kev",
                  "api.first.org": "epss"}.get(host)
        if source is None:
            return httpx.Response(404, json={"message": "unknown host"})
        if source in self.fail:
            return httpx.Response(503, text="Service Unavailable")
        if source == "kev":
            return httpx.Response(200, json=KEV_FEED)
        if source == "epss":
            return self._epss(params)
        if request.method == "POST" and path == "/v1/querybatch":
            return self._querybatch(body)
        if request.method == "GET" and path.startswith("/v1/vulns/"):
            vuln_id = path[len("/v1/vulns/"):]
            if vuln_id.startswith("DEBIAN-CVE-"):
                return httpx.Response(200, json=_big_record(vuln_id))
            if vuln_id in RECORDS:
                return httpx.Response(200, json=RECORDS[vuln_id])
            return httpx.Response(404, json={"code": 5, "message": "Vulnerability not found"})
        return httpx.Response(404, json={"code": 5, "message": "Not found"})

    def _querybatch(self, body: dict) -> httpx.Response:
        results = []
        for index, query in enumerate(body["queries"]):
            package = query.get("package") or {}
            version = query.get("version")
            if "purl" in package:
                purl = package["purl"]
                base, _, version = purl.partition("@")
                key = PURLS.get(base)
                if key is None:
                    results.append({})
                    continue
            else:
                if package.get("ecosystem") not in ECOSYSTEMS:
                    return httpx.Response(400, json={
                        "code": 3,
                        "message": (f"error in query at index {index}: rpc error: "
                                    "code = InvalidArgument desc = invalid ecosystem"),
                    })
                key = (package["ecosystem"], package["name"].lower())
            if (*key, version) in CLEAN:
                results.append({})
                continue
            if key == BIG_PACKAGE:
                start = int(query.get("page_token") or 0)
                stop = min(start + BIG_PAGE_SIZE, BIG_TOTAL)
                row = {"vulns": [{"id": f"DEBIAN-CVE-2020-{n}", "modified": "2026-01-01T00:00:00Z"}
                                 for n in range(start, stop)]}
                if stop < BIG_TOTAL:
                    row["next_page_token"] = str(stop)
                results.append(row)
                continue
            ids = PACKAGES.get(key, [])
            results.append({"vulns": [{"id": i, "modified": RECORDS[i]["modified"]} for i in ids]}
                           if ids else {})
        return httpx.Response(200, json={"results": results})

    def _epss(self, params: dict) -> httpx.Response:
        cves = [c for c in params.get("cve", "").split(",") if c]
        data = []
        for cve in cves:
            if cve not in EPSS:
                continue
            epss, percentile = EPSS[cve]
            row = {"cve": cve, "epss": epss, "percentile": percentile,
                   "date": params.get("date", EPSS_DATE)}
            if params.get("scope") == "time-series":
                row["time-series"] = [
                    {"epss": epss, "percentile": percentile, "date": f"2026-10-{day:02d}"}
                    for day in range(25, 22, -1)
                ]
            data.append(row)
        return httpx.Response(200, json={
            "status": "OK", "status-code": 200, "version": "1.0", "access": "public",
            "total": len(data), "offset": 0, "limit": 100, "data": data,
        })
