"""OSV record helpers: ecosystems, package queries, digests, CVSS scores.

A full OSV record can run to tens of kilobytes (CVE-2021-44228 is ~60 KB:
every distro's affected ranges and hundreds of references), so the tools
work on a compact *digest* per record (:func:`digest`), cached by id and
modified time in plugins/vulns/upstream.py.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import unquote

# OSV ecosystem names are case-sensitive ("pypi" is rejected), so common
# spellings and package-manager names map to the canonical name. Anything
# else is sent as given, which keeps newer ecosystems usable.
_ECOSYSTEM_ALIASES = {
    "pypi": "PyPI", "pip": "PyPI", "python": "PyPI",
    "npm": "npm", "node": "npm", "nodejs": "npm", "javascript": "npm",
    "yarn": "npm", "pnpm": "npm",
    "go": "Go", "golang": "Go",
    "crates.io": "crates.io", "crates": "crates.io", "cargo": "crates.io",
    "rust": "crates.io",
    "maven": "Maven", "java": "Maven", "gradle": "Maven",
    "nuget": "NuGet", "dotnet": "NuGet", ".net": "NuGet",
    "rubygems": "RubyGems", "gem": "RubyGems", "gems": "RubyGems",
    "ruby": "RubyGems", "bundler": "RubyGems",
    "packagist": "Packagist", "composer": "Packagist", "php": "Packagist",
    "pub": "Pub", "dart": "Pub", "flutter": "Pub",
    "hex": "Hex", "elixir": "Hex", "erlang": "Hex",
    "hackage": "Hackage", "haskell": "Hackage",
    "cran": "CRAN",
    "swifturl": "SwiftURL", "swift": "SwiftURL",
    "github actions": "GitHub Actions", "actions": "GitHub Actions",
    "bioconductor": "Bioconductor",
    "conancenter": "ConanCenter", "conan": "ConanCenter",
}
# Distro / platform ecosystems take an optional ":<release>" suffix
# ("Debian:12", "Alpine:v3.20", "Ubuntu:22.04:LTS"); only the prefix is
# canonicalized.
_DISTRO_ECOSYSTEMS = {
    name.lower(): name for name in (
        "Debian", "Ubuntu", "Alpine", "Rocky Linux", "AlmaLinux", "Red Hat",
        "SUSE", "openSUSE", "Wolfi", "Chainguard", "Mageia", "Photon OS",
        "openEuler", "Azure Linux", "Echo", "MinimOS", "Bitnami", "Linux",
        "Android", "OSS-Fuzz", "GIT",
    )
}


def canonical_ecosystem(raw: str) -> str:
    text = raw.strip()
    alias = _ECOSYSTEM_ALIASES.get(text.lower())
    if alias:
        return alias
    head, sep, tail = text.partition(":")
    distro = _DISTRO_ECOSYSTEMS.get(head.strip().lower())
    if distro:
        return distro + sep + tail
    return text


def build_query(item: Any) -> tuple[dict | None, dict | None, str | None]:
    """One ``packages`` entry -> ``(osv query, echo of the package, error)``.

    Accepts a purl string (``pkg:pypi/jinja2@2.11.2``) or an object with
    ``ecosystem`` + ``name`` and an optional ``version`` (or ``purl``). A
    query without a version asks for every advisory ever filed against the
    package.
    """
    if isinstance(item, str):
        item = {"purl": item}
    if not isinstance(item, dict):
        return None, None, "each package must be an object or a purl string"
    purl = item.get("purl")
    if isinstance(purl, str) and purl.strip():
        purl = purl.strip()
        if not purl.startswith("pkg:") or "/" not in purl:
            return None, {"purl": purl}, "purl must look like pkg:<type>/<name>@<version>"
        return {"package": {"purl": purl}}, {"purl": purl}, None
    ecosystem = item.get("ecosystem")
    name = item.get("name")
    if not isinstance(ecosystem, str) or not ecosystem.strip() or \
            not isinstance(name, str) or not name.strip():
        return None, {k: item.get(k) for k in ("ecosystem", "name", "version") if item.get(k)}, \
            "each package needs ecosystem and name (or a purl)"
    echo = {"ecosystem": canonical_ecosystem(ecosystem), "name": name.strip()}
    query: dict = {"package": dict(echo)}
    version = item.get("version")
    if version is not None and str(version).strip():
        echo["version"] = query["version"] = str(version).strip()
    return query, echo, None


# ---------------------------------------------------------------------------
# Package matching (which affected[] entries are "this" package)
# ---------------------------------------------------------------------------

def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _name_key(ecosystem: str, name: str) -> str:
    return _pep503(name) if ecosystem == "PyPI" else name.lower()


def _purl_base(purl: str) -> str:
    """A purl without version / qualifiers / subpath, lowercased."""
    base = purl.split("#", 1)[0].split("?", 1)[0]
    if "@" in base.rsplit("/", 1)[-1]:
        base = base.rsplit("@", 1)[0]
    return unquote(base).lower()


def _tail(name: str) -> str:
    return re.split(r"[/:]", name.rstrip("/"))[-1].lower()


def package_matcher(echo: dict):
    """A predicate over a digest's ``affected`` entries for the queried package."""
    if "purl" in echo:
        base = _purl_base(echo["purl"])
        tail = _tail(base)

        def match(entry: dict) -> bool:
            if entry.get("purl"):
                return _purl_base(entry["purl"]) == base
            return _tail(entry.get("name") or "") == tail
        return match
    ecosystem = echo["ecosystem"]
    key = _name_key(ecosystem.split(":", 1)[0], echo["name"])

    def match(entry: dict) -> bool:
        entry_eco = entry.get("ecosystem") or ""
        return (entry_eco.split(":", 1)[0] == ecosystem.split(":", 1)[0]
                and _name_key(entry_eco.split(":", 1)[0], entry.get("name") or "") == key)
    return match


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

_CVSS3_WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "C": {"H": 0.56, "L": 0.22, "N": 0.0},
    "I": {"H": 0.56, "L": 0.22, "N": 0.0},
    "A": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_CVSS3_PR = {"U": {"N": 0.85, "L": 0.62, "H": 0.27},
             "C": {"N": 0.85, "L": 0.68, "H": 0.5}}


def _roundup(value: float) -> float:
    """CVSS v3.1 Roundup: the smallest one-decimal number >= value."""
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def cvss3_base_score(vector: str) -> float | None:
    """The CVSS v3.0 / v3.1 base score of a vector string, or None when the
    vector is not a complete v3 base vector."""
    parts = vector.strip().split("/")
    if not parts or not parts[0].startswith("CVSS:3"):
        return None
    metrics = dict(p.split(":", 1) for p in parts[1:] if ":" in p)
    try:
        scope = metrics["S"]
        av = _CVSS3_WEIGHTS["AV"][metrics["AV"]]
        ac = _CVSS3_WEIGHTS["AC"][metrics["AC"]]
        pr = _CVSS3_PR[scope][metrics["PR"]]
        ui = _CVSS3_WEIGHTS["UI"][metrics["UI"]]
        c = _CVSS3_WEIGHTS["C"][metrics["C"]]
        i = _CVSS3_WEIGHTS["I"][metrics["I"]]
        a = _CVSS3_WEIGHTS["A"][metrics["A"]]
    except KeyError:
        return None
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    if scope == "U":
        impact = 6.42 * iss
    else:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    exploitability = 8.22 * av * ac * pr * ui
    if impact <= 0:
        return 0.0
    if scope == "U":
        return _roundup(min(impact + exploitability, 10))
    return _roundup(min(1.08 * (impact + exploitability), 10))


def score_label(score: float) -> str:
    if score == 0:
        return "none"
    if score < 4.0:
        return "low"
    if score < 7.0:
        return "medium"
    if score < 9.0:
        return "high"
    return "critical"


_LABEL_RANK = {"none": 0, "unimportant": 0, "negligible": 0, "low": 1,
               "medium": 2, "moderate": 2, "high": 3, "important": 3,
               "critical": 4}


def label_rank(label: str | None) -> int:
    return _LABEL_RANK.get((label or "").lower(), -1)


def _severity(record: dict) -> tuple[str | None, float | None, str | None]:
    """``(cvss vector, v3 base score, label)`` for a record.

    The vector prefers CVSS v3 (scored here), then v4, then v2 (vector
    only). The label is the advisory's own rating (GHSA
    ``database_specific.severity``, a distro's ``ecosystem_specific``
    urgency), else derived from the v3 score; GHSA's "moderate" reads as
    "medium".
    """
    vectors: dict[str, str] = {}
    entries = list(record.get("severity") or [])
    for affected in record.get("affected") or []:
        if isinstance(affected, dict):
            entries.extend(affected.get("severity") or [])
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("score"), str):
            vectors.setdefault(str(entry.get("type")), entry["score"])
    vector = vectors.get("CVSS_V3") or vectors.get("CVSS_V4") or vectors.get("CVSS_V2")
    score = cvss3_base_score(vectors["CVSS_V3"]) if "CVSS_V3" in vectors else None

    label = None
    db_specific = record.get("database_specific")
    if isinstance(db_specific, dict) and isinstance(db_specific.get("severity"), str):
        label = db_specific["severity"]
    if label is None:
        for affected in record.get("affected") or []:
            eco = affected.get("ecosystem_specific") if isinstance(affected, dict) else None
            if isinstance(eco, dict):
                for key in ("severity", "urgency"):
                    if isinstance(eco.get(key), str):
                        label = eco[key]
                        break
            if label:
                break
    if label is None and score is not None:
        label = score_label(score)
    if label:
        label = label.strip().lower()
        if label == "moderate":
            label = "medium"
    return vector, score, label or None


def _summary_line(record: dict, limit: int) -> str | None:
    text = record.get("summary")
    if not isinstance(text, str) or not text.strip():
        details = record.get("details")
        if not isinstance(details, str) or not details.strip():
            return None
        text = details.strip().split("\n\n", 1)[0]
    return truncate(" ".join(text.split()), limit)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _cwes(record: dict) -> list[str]:
    db_specific = record.get("database_specific")
    cwes = db_specific.get("cwe_ids") if isinstance(db_specific, dict) else None
    return [c for c in cwes if isinstance(c, str)] if isinstance(cwes, list) else []


def range_events(affected: dict) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {"introduced": [], "fixed": [], "last_affected": []}
    for rng in affected.get("ranges") or []:
        if not isinstance(rng, dict):
            continue
        for event in rng.get("events") or []:
            if not isinstance(event, dict):
                continue
            for key in events:
                value = event.get(key)
                if isinstance(value, str) and value not in events[key]:
                    events[key].append(value)
    return events


def digest(record: dict) -> dict:
    """The compact form of an OSV record that the package checks work on."""
    vector, score, label = _severity(record)
    affected = []
    for entry in record.get("affected") or []:
        if not isinstance(entry, dict):
            continue
        package = entry.get("package") if isinstance(entry.get("package"), dict) else {}
        events = range_events(entry)
        affected.append({
            "ecosystem": package.get("ecosystem"),
            "name": package.get("name"),
            "purl": package.get("purl"),
            "fixed": events["fixed"],
            "last_affected": events["last_affected"],
        })
    return {
        "id": record.get("id"),
        "aliases": [a for a in record.get("aliases") or [] if isinstance(a, str)],
        "summary": _summary_line(record, 160),
        "severity": label,
        "cvss_score": score,
        "cvss_vector": vector,
        "cwes": _cwes(record),
        "published": (record.get("published") or "")[:10] or None,
        "withdrawn": (record.get("withdrawn") or "")[:10] or None,
        "affected": affected,
    }


def cve_ids(ids: list[str]) -> list[str]:
    return [i.upper() for i in ids if i.upper().startswith("CVE-")]


# ---------------------------------------------------------------------------
# Grouping one package's advisories
# ---------------------------------------------------------------------------

def _primary_rank(vuln_id: str) -> int:
    """Which id names a merged group: the GitHub advisory (reviewed severity
    and summary), then a CVE, then any other database's id."""
    if vuln_id.startswith("GHSA-"):
        return 0
    if vuln_id.startswith("CVE-"):
        return 1
    return 2


def group_advisories(digests: list[dict]) -> list[list[dict]]:
    """Group digests that describe the same issue (sharing an id or alias).

    OSV answers a package query with every database's copy of an issue --
    GHSA-..., PYSEC-..., the CVE -- each listing the others as aliases.
    """
    groups: list[tuple[set[str], list[dict]]] = []
    for item in digests:
        keys = {item["id"], *item["aliases"]}
        merged_keys, merged_items = set(keys), [item]
        rest = []
        for group_keys, group_items in groups:
            if group_keys & merged_keys:
                merged_keys |= group_keys
                merged_items = group_items + merged_items
            else:
                rest.append((group_keys, group_items))
        groups = rest + [(merged_keys, merged_items)]
    return [sorted(items, key=lambda d: (_primary_rank(d["id"]), d["id"]))
            for _, items in groups]
