"""Fixtures for the vulnerability data plugin's test suite (see tests/plugin_support.py)."""

from pathlib import Path

import pytest

from plugins.vulns import upstream
from tests.plugin_support import plugin_fixture

vulns_plugin = plugin_fixture(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def _fresh_caches():
    """The OSV digest and KEV caches are process-wide; start each test empty."""
    upstream.DIGEST_CACHE.clear()
    upstream.KEV_CACHE.clear()
    yield
    upstream.DIGEST_CACHE.clear()
    upstream.KEV_CACHE.clear()
