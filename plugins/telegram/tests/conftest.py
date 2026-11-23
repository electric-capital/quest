"""Fixtures for the Telegram plugin's test suite (see tests/plugin_support.py)."""

from pathlib import Path

from tests.plugin_support import plugin_fixture
from tests.route_table_support import quest_route_table  # noqa: F401  (fixture, re-exported)

telegram_plugin = plugin_fixture(Path(__file__).resolve().parent.parent)
