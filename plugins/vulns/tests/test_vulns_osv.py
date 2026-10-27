"""Tests for the OSV record helpers (plugins/vulns/osv.py): ecosystem names,
package queries and matching, severity and CVSS v3 scoring, and merging one
issue's copies across databases."""

import pytest

from plugins.vulns import osv
from plugins.vulns.tests.fake_upstream import (
    JINJA_SANDBOX,
    JINJA_XMLATTR,
    JINJA_XMLATTR_PYSEC,
    LOG4SHELL_GHSA,
)


class TestCvss3:
    @pytest.mark.parametrize("vector, score", [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),  # Log4Shell
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:L/I:L/A:N", 5.4),
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8),
        ("CVSS:3.0/AV:N/AC:H/PR:L/UI:N/S:U/C:H/I:N/A:N", 5.3),
        ("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:C/C:L/I:N/A:N", 2.3),
        ("CVSS:3.1/AV:N/AC:L/PR:H/UI:R/S:C/C:L/I:L/A:N", 4.8),
        ("CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:C/C:L/I:L/A:N", 5.5),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
    ])
    def test_base_scores_match_the_reference_calculator(self, vector, score):
        assert osv.cvss3_base_score(vector) == score

    @pytest.mark.parametrize("vector", [
        "AV:N/AC:L/Au:N/C:P/I:P/A:P",                                  # v2
        "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H",                    # A missing
        "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "",
    ])
    def test_non_v3_or_incomplete_vectors_have_no_score(self, vector):
        assert osv.cvss3_base_score(vector) is None

    @pytest.mark.parametrize("score, label", [
        (0.0, "none"), (0.1, "low"), (3.9, "low"), (4.0, "medium"),
        (6.9, "medium"), (7.0, "high"), (8.9, "high"), (9.0, "critical"),
    ])
    def test_score_labels(self, score, label):
        assert osv.score_label(score) == label


class TestEcosystems:
    @pytest.mark.parametrize("raw, canonical", [
        ("pypi", "PyPI"), ("pip", "PyPI"), ("PyPI", "PyPI"), ("golang", "Go"),
        ("cargo", "crates.io"), ("gem", "RubyGems"), ("composer", "Packagist"),
        ("nuget", "NuGet"), ("maven", "Maven"), ("github actions", "GitHub Actions"),
        ("debian:12", "Debian:12"), ("ubuntu:22.04:LTS", "Ubuntu:22.04:LTS"),
        ("alpine:v3.20", "Alpine:v3.20"), ("rocky linux:9", "Rocky Linux:9"),
        ("SomethingNew", "SomethingNew"),
    ])
    def test_canonical_names(self, raw, canonical):
        assert osv.canonical_ecosystem(raw) == canonical


class TestBuildQuery:
    def test_object_with_version(self):
        query, echo, error = osv.build_query({"ecosystem": "pypi", "name": " Jinja2 ", "version": "2.11.2"})
        assert error is None
        assert query == {"package": {"ecosystem": "PyPI", "name": "Jinja2"}, "version": "2.11.2"}
        assert echo == {"ecosystem": "PyPI", "name": "Jinja2", "version": "2.11.2"}

    def test_object_without_version_queries_the_whole_package(self):
        query, echo, _ = osv.build_query({"ecosystem": "npm", "name": "lodash"})
        assert query == {"package": {"ecosystem": "npm", "name": "lodash"}}
        assert "version" not in echo

    def test_purl_string_and_object(self):
        purl = "pkg:pypi/jinja2@2.11.2"
        assert osv.build_query(purl) == ({"package": {"purl": purl}}, {"purl": purl}, None)
        assert osv.build_query({"purl": purl})[0] == {"package": {"purl": purl}}

    @pytest.mark.parametrize("item", [
        {"name": "jinja2"}, {"ecosystem": "PyPI"}, {"ecosystem": "", "name": "x"},
        {"purl": "jinja2@1.0"}, 42, None,
    ])
    def test_malformed_entries(self, item):
        query, _, error = osv.build_query(item)
        assert query is None and error


class TestPackageMatcher:
    def test_pypi_names_are_pep503_normalized(self):
        matches = osv.package_matcher({"ecosystem": "PyPI", "name": "Zope_Interface"})
        assert matches({"ecosystem": "PyPI", "name": "zope.interface"})
        assert not matches({"ecosystem": "npm", "name": "zope.interface"})
        assert not matches({"ecosystem": "PyPI", "name": "zope"})

    def test_distro_release_suffix_is_ignored(self):
        matches = osv.package_matcher({"ecosystem": "Debian:12", "name": "linux"})
        assert matches({"ecosystem": "Debian:11", "name": "linux"})

    def test_purl_matches_the_entry_purl_without_version(self):
        matches = osv.package_matcher({"purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"})
        assert matches({"ecosystem": "Maven", "name": "org.apache.logging.log4j:log4j-core",
                        "purl": "pkg:maven/org.apache.logging.log4j/log4j-core"})
        assert not matches({"ecosystem": "Maven", "name": "org.apache.logging.log4j:log4j-api",
                            "purl": "pkg:maven/org.apache.logging.log4j/log4j-api"})
        # No purl on the entry: fall back to the last name segment.
        assert matches({"ecosystem": "Maven", "name": "org.apache.logging.log4j:log4j-core"})


class TestDigest:
    def test_ghsa_record(self):
        item = osv.digest(JINJA_XMLATTR)
        assert item["id"] == "GHSA-h5c8-rqwp-cp95"
        assert item["severity"] == "medium"  # GHSA "MODERATE"
        assert item["cvss_score"] == 5.4
        assert item["cwes"] == ["CWE-79"]
        assert item["published"] == "2024-01-11"
        assert item["affected"][0]["fixed"] == ["3.1.3"]
        assert item["withdrawn"] is None

    def test_summary_falls_back_to_the_first_details_paragraph(self):
        item = osv.digest(JINJA_XMLATTR_PYSEC)
        assert item["summary"].startswith("Jinja is an extensible templating engine.")
        assert item["severity"] is None and item["cvss_score"] is None

    def test_distro_urgency_is_the_label(self):
        record = {"id": "DEBIAN-CVE-2020-1", "affected": [
            {"package": {"name": "linux", "ecosystem": "Debian:12"},
             "ecosystem_specific": {"urgency": "unimportant"}}]}
        assert osv.digest(record)["severity"] == "unimportant"

    def test_label_derived_from_the_v3_score_when_unrated(self):
        record = {"id": "X-1", "severity": [
            {"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"},
            {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}
        item = osv.digest(record)
        assert item["cvss_vector"].startswith("CVSS:3.1/")
        assert (item["cvss_score"], item["severity"]) == (9.8, "critical")

    def test_v4_only_keeps_the_vector_without_a_score(self):
        vector = "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
        item = osv.digest({"id": "X-2", "severity": [{"type": "CVSS_V4", "score": vector}]})
        assert (item["cvss_vector"], item["cvss_score"], item["severity"]) == (vector, None, None)

    def test_multiple_fixed_release_lines(self):
        fixed = [f for entry in osv.digest(LOG4SHELL_GHSA)["affected"] for f in entry["fixed"]]
        assert fixed == ["2.15.0", "2.12.2", "2.3.1"]


class TestGroupAdvisories:
    def test_copies_of_one_issue_merge_and_the_ghsa_names_the_group(self):
        groups = osv.group_advisories([
            osv.digest(JINJA_XMLATTR_PYSEC), osv.digest(JINJA_SANDBOX), osv.digest(JINJA_XMLATTR),
        ])
        ids = sorted([d["id"] for d in g] for g in groups)
        assert ids == [["GHSA-h5c8-rqwp-cp95", "PYSEC-2026-1473"], ["GHSA-q2x7-8rv6-6q7h"]]

    def test_transitive_aliases_merge(self):
        a = {"id": "A-1", "aliases": ["CVE-2024-1"]}
        b = {"id": "B-1", "aliases": ["C-1"]}
        c = {"id": "C-1", "aliases": ["CVE-2024-1"]}
        groups = osv.group_advisories([a, b, c])
        assert len(groups) == 1
        assert [d["id"] for d in groups[0]] == ["A-1", "B-1", "C-1"]

    def test_cve_names_the_group_without_a_ghsa(self):
        groups = osv.group_advisories([
            {"id": "PYSEC-2021-1", "aliases": ["CVE-2021-1"]},
            {"id": "CVE-2021-1", "aliases": ["PYSEC-2021-1"]},
        ])
        assert groups[0][0]["id"] == "CVE-2021-1"
