"""Fixtures for the Tailscale plugin's test suite (see tests/plugin_support.py)."""

from pathlib import Path

from tests.plugin_support import plugin_fixture

tailscale_plugin = plugin_fixture(Path(__file__).resolve().parent.parent)
