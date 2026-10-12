"""Fixtures for the IMAP plugin's test suite (see tests/plugin_support.py).

``imap_plugin`` registers the plugin; ``fake_imap`` (from fake_imaplib.py)
routes the transport to an in-memory IMAP server and points the admin
allow-list at it. See the fake_imaplib module docstring.
"""

from pathlib import Path

from tests.plugin_support import plugin_fixture

from plugins.imap.tests.fake_imaplib import fake_imap, imap_admin_hosts  # noqa: F401

imap_plugin = plugin_fixture(Path(__file__).resolve().parent.parent)
