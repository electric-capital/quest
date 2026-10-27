"""Tests for the vulnerability data tools (plugins/vulns/tools.py) against the
in-memory OSV / KEV / EPSS in fake_upstream.py."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import plugins.vulns.tools as tools_mod
from plugins.vulns import upstream
from plugins.vulns.tests.fake_upstream import BIG_PAGE_SIZE, FakeUpstream


def _run(coro):
    return asyncio.run(coro)


_CTX = SimpleNamespace(user={"id": 7, "email": "u@example.com"}, conversation_id="conv-1",
                       project_id=None, provider=None, model="claude-opus-4-8")


@pytest.fixture()
def fake():
    fake = FakeUpstream()
    with patch.object(tools_mod, "make_client", fake.make_client):
        yield fake


def _call(handler, args=None):
    return json.loads(_run(handler(_CTX, args or {})))


def _check(packages, **args):
    return _call(tools_mod._tool_check_packages, {"packages": packages, **args})


JINJA = {"ecosystem": "PyPI", "name": "jinja2", "version": "2.11.2"}
LOG4J_PURL = {"purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"}
XNET = {"ecosystem": "Go", "name": "golang.org/x/net", "version": "v0.10.0"}


# ---------------------------------------------------------------------------
# vulns_check_packages
# ---------------------------------------------------------------------------

class TestCheckPackages:
    def test_merges_copies_and_orders_worst_first(self, fake):
        out = _check([JINJA])
        assert (out["packages_checked"], out["packages_vulnerable"]) == (1, 1)
        pkg = out["vulnerable"][0]
        assert pkg["package"] == JINJA
        # GHSA + PYSEC copies of CVE-2024-22195 merged; the withdrawn one dropped.
        assert pkg["advisory_count"] == 2 == out["advisories"]
        high, medium = pkg["vulnerabilities"]
        assert high["id"] == "GHSA-q2x7-8rv6-6q7h"
        assert (high["severity"], high["cvss_score"], high["fixed_in"]) == ("high", 7.8, ["3.1.5"])
        assert medium == {
            "id": "GHSA-h5c8-rqwp-cp95",
            "aliases": ["CVE-2024-22195", "PYSEC-2026-1473"],
            "summary": "Jinja vulnerable to HTML attribute injection when passing user input as keys to xmlattr filter",
            "severity": "medium",
            "cvss_score": 5.4,
            "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N",
            "fixed_in": ["3.1.3"],
            "published": "2024-01-11",
            "epss": 0.00892,
            "epss_percentile": 0.59161,
        }
        assert out["in_kev"] == 0
        assert out["not_vulnerable"] == []

    def test_kev_listed_issue_sorts_above_a_higher_severity_one(self, fake):
        pkg = _check([XNET])["vulnerable"][0]
        first, second = pkg["vulnerabilities"]
        assert first["id"] == "GHSA-qppj-fm5r-hxr3"
        assert first["severity"] == "medium"
        assert first["kev"] == {
            "cve": "CVE-2023-44487", "name": "HTTP/2 Rapid Reset Attack Vulnerability",
            "date_added": "2023-10-10", "due_date": "2023-10-31", "ransomware": "Unknown",
        }
        assert set(first["aliases"]) == {"CVE-2023-44487", "GO-2023-2102"}
        assert (second["id"], second["severity"]) == ("GHSA-4374-p667-p6c8", "high")
        assert "kev" not in second

    def test_purl_query_and_every_release_line_fix(self, fake):
        out = _check([LOG4J_PURL])
        row = out["vulnerable"][0]["vulnerabilities"][0]
        assert out["vulnerable"][0]["package"] == LOG4J_PURL
        assert row["id"] == "GHSA-jfh8-c2jp-5v3q"
        assert row["fixed_in"] == ["2.15.0", "2.12.2", "2.3.1"]
        assert (row["severity"], row["cvss_score"]) == ("critical", 10.0)
        assert row["kev"]["ransomware"] == "Known"
        assert row["epss"] == 0.99999
        assert out["in_kev"] == 1
        assert "note" not in out["vulnerable"][0]

    def test_ecosystem_spellings_are_canonicalized(self, fake):
        out = _check([{"ecosystem": "pip", "name": "Jinja2", "version": "2.11.2"}])
        assert out["vulnerable"][0]["package"]["ecosystem"] == "PyPI"
        assert not out.get("errors")

    def test_clean_versions_are_listed_separately(self, fake):
        clean = {"ecosystem": "PyPI", "name": "jinja2", "version": "3.1.6"}
        out = _check([clean, JINJA])
        assert out["not_vulnerable"] == [clean]
        assert out["packages_vulnerable"] == 1

    def test_one_invalid_ecosystem_does_not_sink_the_batch(self, fake):
        bad = {"ecosystem": "Nope", "name": "x", "version": "1"}
        out = _check([bad, JINJA, LOG4J_PURL])
        assert out["errors"] == [{"package": bad, "error": "invalid ecosystem"}]
        assert out["packages_vulnerable"] == 2
        batches = fake.calls_to("/v1/querybatch")
        assert len(batches) == 2
        assert len(batches[1][4]["queries"]) == 2

    def test_malformed_entries_are_reported(self, fake):
        out = _check([{"name": "x"}, 42, JINJA])
        assert [e["error"] for e in out["errors"]] == [
            "each package needs ecosystem and name (or a purl)",
            "each package must be an object or a purl string",
        ]
        assert out["packages_checked"] == 3

    def test_argument_validation(self, fake):
        assert _check([])["error"] == "invalid_arguments"
        assert _call(tools_mod._tool_check_packages, {})["error"] == "invalid_arguments"
        too_many = [JINJA] * (tools_mod.MAX_PACKAGES + 1)
        assert "At most" in _check(too_many)["message"]
        assert fake.calls == []

    def test_a_json_string_list_is_accepted(self, fake):
        out = _call(tools_mod._tool_check_packages, {"packages": json.dumps([JINJA])})
        assert out["packages_vulnerable"] == 1

    def test_rows_per_package_are_capped(self, fake):
        pkg = _check([JINJA], max_vulns_per_package=1)["vulnerable"][0]
        assert len(pkg["vulnerabilities"]) == 1
        assert pkg["omitted"] == 1
        assert pkg["advisory_count"] == 2

    def test_versionless_query_is_flagged(self, fake):
        pkg = _check([{"ecosystem": "PyPI", "name": "jinja2"}])["vulnerable"][0]
        assert "every advisory ever filed" in pkg["note"]

    def test_kev_and_epss_failures_are_not_fatal(self, fake):
        fake.fail = {"kev", "epss"}
        out = _check([LOG4J_PURL])
        row = out["vulnerable"][0]["vulnerabilities"][0]
        assert "kev" not in row and "epss" not in row
        assert out["in_kev"] is None
        assert {e["source"] for e in out["enrichment_errors"]} == {"kev", "epss"}

    def test_enrich_false_skips_kev_and_epss(self, fake):
        out = _check([LOG4J_PURL], enrich=False)
        assert out["in_kev"] is None
        assert {c[1] for c in fake.calls} == {"api.osv.dev"}

    def test_osv_failure_is_an_error(self, fake):
        fake.fail = {"osv"}
        out = _check([JINJA])
        assert out["error"] == "vulns_upstream_error"
        assert (out["source"], out["status_code"]) == ("osv", 503)

    def test_a_huge_package_is_read_partially(self, fake):
        out = _check([{"ecosystem": "Debian:12", "name": "linux", "version": "6.1.0"}],
                     max_vulns_per_package=5)
        pkg = out["vulnerable"][0]
        assert pkg["advisory_count"] == tools_mod.DETAIL_IDS_PER_PACKAGE
        assert pkg["unread_advisories"].startswith(
            f"{BIG_PAGE_SIZE - tools_mod.DETAIL_IDS_PER_PACKAGE}+ ")
        # low before unimportant
        assert [r["severity"] for r in pkg["vulnerabilities"]] == ["low"] * 5
        # The first page already exceeds the read cap: no follow-up page.
        assert len(fake.calls_to("/v1/querybatch")) == 1
        assert len(fake.calls_to("/v1/vulns/")) == tools_mod.DETAIL_IDS_PER_PACKAGE

    def test_records_are_cached_between_calls(self, fake):
        _check([JINJA])
        fetched = len(fake.calls_to("/v1/vulns/"))
        assert fetched == 4
        _check([JINJA])
        assert len(fake.calls_to("/v1/vulns/")) == fetched
        assert len(fake.calls_to("/v1/querybatch")) == 2


class TestOsvQueryBatchPaging:
    def test_follows_next_page_tokens_up_to_the_cap(self, fake):
        async def run():
            async with fake.make_client() as client:
                return await upstream.osv_query_batch(
                    client, [{"package": {"ecosystem": "Debian:12", "name": "linux"}, "version": "1"}],
                    max_ids_per_query=5000,
                )
        (result,) = _run(run())
        assert len(result["vulns"]) == 1400
        assert result["more"] is False
        assert [c[4]["queries"][0].get("page_token") for c in fake.calls] == [None, "1000"]


# ---------------------------------------------------------------------------
# vulns_get_vulnerability
# ---------------------------------------------------------------------------

class TestGetVulnerability:
    def test_ghsa_record_with_enrichment(self, fake):
        out = _call(tools_mod._tool_get_vulnerability, {"id": "ghsa-H5C8-RQWP-CP95"})
        assert fake.calls_to("/v1/vulns/")[0][2] == "/v1/vulns/GHSA-h5c8-rqwp-cp95"
        assert out["id"] == "GHSA-h5c8-rqwp-cp95"
        assert out["aliases"] == ["CVE-2024-22195", "PYSEC-2026-1473"]
        assert out["details"].startswith("Details of GHSA-h5c8-rqwp-cp95.")
        assert (out["severity"], out["cvss_score"], out["cwes"]) == ("medium", 5.4, ["CWE-79"])
        assert out["affected"] == [{"ecosystem": "PyPI", "name": "jinja2",
                                    "introduced": ["0"], "fixed": ["3.1.3"]}]
        assert out["references"][0]["type"] == "ADVISORY"
        assert out["kev"] is None
        assert out["epss"] == {"CVE-2024-22195": {"epss": 0.00892, "percentile": 0.59161,
                                                  "date": "2026-10-26"}}

    def test_cve_record_with_kev_entry_and_history(self, fake):
        out = _call(tools_mod._tool_get_vulnerability,
                    {"id": "cve-2021-44228", "epss_history": True})
        assert out["id"] == "CVE-2021-44228"
        assert out["affected"][0]["repo"] == "https://github.com/apache/logging-log4j2"
        assert len(out["related"]) == tools_mod.MAX_RELATED and out["related_total"] == 25
        (kev,) = out["kev"]
        assert kev["cve"] == "CVE-2021-44228"
        assert kev["required_action"].startswith("For all affected software assets")
        assert kev["notes"] == "https://nvd.nist.gov/vuln/detail/CVE-2021-44228"
        assert len(out["epss"]["CVE-2021-44228"]["history"]) == 3
        assert fake.calls_to("/data/v1/epss")[0][3]["scope"] == "time-series"

    def test_references_are_capped_advisory_and_fix_first(self, fake):
        out = _call(tools_mod._tool_get_vulnerability, {"id": "GHSA-jfh8-c2jp-5v3q"})
        assert len(out["references"]) == tools_mod.MAX_REFERENCES
        assert out["references_total"] == 20
        assert [r["type"] for r in out["references"][:2]] == ["ADVISORY", "FIX"]

    def test_cve_unknown_to_osv_still_gets_kev_and_epss(self, fake):
        out = _call(tools_mod._tool_get_vulnerability, {"id": "CVE-2026-1000"})
        assert "no record" in out["osv"]
        assert out["kev"][0]["product"] == "PHP"
        assert out["epss"]["CVE-2026-1000"]["epss"] == 0.99981

    def test_unknown_non_cve_id(self, fake):
        out = _call(tools_mod._tool_get_vulnerability, {"id": "GHSA-0000-0000-0000"})
        assert (out["error"], out["source"]) == ("vulns_not_found", "osv")

    @pytest.mark.parametrize("bad", [None, "", "../etc/passwd", "a b", "x" * 200])
    def test_invalid_ids_are_rejected_before_any_request(self, fake, bad):
        out = _call(tools_mod._tool_get_vulnerability, {"id": bad})
        assert out["error"] == "invalid_arguments"
        assert fake.calls == []

    def test_enrichment_failure_is_reported(self, fake):
        fake.fail = {"kev"}
        out = _call(tools_mod._tool_get_vulnerability, {"id": "CVE-2021-44228"})
        assert out["enrichment_errors"][0]["source"] == "kev"
        assert out["epss"]["CVE-2021-44228"]["epss"] == 0.99999


# ---------------------------------------------------------------------------
# vulns_search_kev
# ---------------------------------------------------------------------------

def _kev(**args):
    return _call(tools_mod._tool_search_kev, args)


class TestSearchKev:
    def test_no_filters_lists_the_latest_additions(self, fake):
        out = _kev(limit=2)
        assert (out["catalog_version"], out["catalog_count"]) == ("2026.10.26", 4)
        assert (out["match_count"], out["returned"]) == (4, 2)
        assert [r["cve"] for r in out["rows"]] == ["CVE-2026-1000", "CVE-2023-44487"]
        assert out["rows"][0]["epss"] == 0.99981
        assert "required_action" not in out["rows"][0]

    def test_vendor_product_and_text_filters(self, fake):
        assert [r["cve"] for r in _kev(vendor="apache", product="log4j")["rows"]] == [
            "CVE-2021-45046", "CVE-2021-44228"]
        assert [r["cve"] for r in _kev(query="log4j remote code")["rows"]] == ["CVE-2021-44228"]
        assert _kev(query="nothing-like-this")["rows"] == []

    def test_cwe_ransomware_and_date_filters(self, fake):
        assert [r["cve"] for r in _kev(cwe="917")["rows"]] == ["CVE-2021-45046"]
        assert [r["cve"] for r in _kev(ransomware_only=True)["rows"]] == [
            "CVE-2021-45046", "CVE-2021-44228"]
        assert [r["cve"] for r in _kev(added_since="2023-05-01", added_before="2026-01-01")["rows"]] == [
            "CVE-2023-44487", "CVE-2021-45046"]

    def test_exact_lookup_reports_misses_and_remediation(self, fake):
        out = _kev(cve_ids=["cve-2021-44228", "CVE-2024-22195"])
        assert [r["cve"] for r in out["rows"]] == ["CVE-2021-44228"]
        assert out["not_in_kev"] == ["CVE-2024-22195"]
        assert out["rows"][0]["required_action"].startswith("For all affected")

    def test_include_epss_false(self, fake):
        out = _kev(include_epss=False)
        assert "epss" not in out["rows"][0]
        assert fake.calls_to("/data/v1/epss") == []

    @pytest.mark.parametrize("args", [
        {"added_since": "last week"}, {"added_before": "2026/01/01"}, {"cve_ids": ["log4shell"]},
    ])
    def test_argument_validation(self, fake, args):
        assert _kev(**args)["error"] == "invalid_arguments"

    def test_catalog_is_cached(self, fake):
        _kev()
        _kev(vendor="apache")
        assert len(fake.calls_to("/sites/default/files/feeds/")) == 1

    def test_stale_catalog_is_served_when_a_refresh_fails(self, fake):
        _kev()
        upstream.KEV_CACHE.fetched_at -= upstream.KEV_TTL_SECONDS + 1
        fake.fail = {"kev"}
        out = _kev(limit=1)
        assert out["returned"] == 1
        assert "could not be refreshed" in out["stale"]

    def test_unreachable_feed_without_a_cached_copy(self, fake):
        fake.fail = {"kev"}
        out = _kev()
        assert (out["error"], out["source"]) == ("vulns_upstream_error", "kev")


# ---------------------------------------------------------------------------
# vulns_get_epss
# ---------------------------------------------------------------------------

def _epss(**args):
    return _call(tools_mod._tool_get_epss, args)


class TestGetEpss:
    def test_scores_sorted_with_misses(self, fake):
        out = _epss(cve_ids=["CVE-2024-22195", "cve-2021-44228", "CVE-2099-0001"])
        assert [r["cve"] for r in out["scores"]] == ["CVE-2021-44228", "CVE-2024-22195"]
        assert out["scores"][0] == {"cve": "CVE-2021-44228", "epss": 0.99999,
                                    "percentile": 1.0, "date": "2026-10-26"}
        assert out["not_scored"] == ["CVE-2099-0001"]
        assert fake.calls_to("/data/v1/epss")[0][3]["cve"] == (
            "CVE-2024-22195,CVE-2021-44228,CVE-2099-0001")

    def test_date_and_history(self, fake):
        out = _epss(cve_ids=["CVE-2021-44228"], date="2026-01-15")
        assert out["scores"][0]["date"] == "2026-01-15"
        assert fake.calls_to("/data/v1/epss")[0][3]["date"] == "2026-01-15"
        out = _epss(cve_ids="CVE-2021-44228", history=True)
        assert [p["date"] for p in out["scores"][0]["history"]] == [
            "2026-10-25", "2026-10-24", "2026-10-23"]

    def test_large_lists_are_chunked(self, fake):
        cves = [f"CVE-2020-{n:05d}" for n in range(250)]
        out = _epss(cve_ids=cves)
        assert len(out["not_scored"]) == 250
        assert len(fake.calls_to("/data/v1/epss")) == 3

    @pytest.mark.parametrize("args", [
        {},
        {"cve_ids": ["GHSA-h5c8-rqwp-cp95"]},
        {"cve_ids": [f"CVE-2020-{n:05d}" for n in range(tools_mod.EPSS_MAX_CVES + 1)]},
        {"cve_ids": [f"CVE-2020-{n:05d}" for n in range(21)], "history": True},
        {"cve_ids": ["CVE-2021-44228"], "history": True, "date": "2026-01-01"},
        {"cve_ids": ["CVE-2021-44228"], "date": "yesterday"},
    ])
    def test_argument_validation(self, fake, args):
        assert _epss(**args)["error"] == "invalid_arguments"
        assert fake.calls == []

    def test_upstream_failure(self, fake):
        fake.fail = {"epss"}
        out = _epss(cve_ids=["CVE-2021-44228"])
        assert (out["error"], out["source"]) == ("vulns_upstream_error", "epss")
