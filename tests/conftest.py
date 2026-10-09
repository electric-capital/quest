"""Shared fixtures: registering the in-tree reference plugins.

Plugin-specific test suites live in each plugin's own ``tests/``
directory (``plugins/<name>/tests``) with their own conftest. The
fixtures here serve the cross-cutting core tests that exercise
plugin-registered surfaces (connector rows, dispatch tables, the
action-request schema, system-skill enumeration, ...) with a real
plugin registered. All of them are built by the shared factory in
tests/plugin_support.py.
"""

from config.plugins import PLUGINS_DIR

from tests.plugin_support import plugin_fixture

# The _example smoke plugin (never discovered in production -- the loader
# skips _-prefixed directories, but plugin_fixture imports it directly) is
# the stand-in whenever a cross-cutting test just needs SOME registered
# plugin; plugin-specific behavior is pinned in each plugin's own tests/
# suite.
example_plugin = plugin_fixture(PLUGINS_DIR / "_example")
github_plugin = plugin_fixture(PLUGINS_DIR / "github")
slack_plugin = plugin_fixture(PLUGINS_DIR / "slack")
twitter_plugin = plugin_fixture(PLUGINS_DIR / "twitter")
telegram_plugin = plugin_fixture(PLUGINS_DIR / "telegram")


import pytest


@pytest.fixture(autouse=True)
def _isolated_inference_provider_files(tmp_path, monkeypatch):
    """Keep every test away from the real data-dir provider files.

    ``chat.llm.config.list_model_specs()`` (reached by anything that
    resolves a model id or lists available models) reads the provider
    configuration store, and that store synthesizes -- and persists -- an
    instance entry for any credential file it finds. Pointing the config
    file, the credential store and the OpenRouter catalog cache at
    tmp_path makes model resolution deterministic (Vertex registry only,
    nothing disabled) and prevents tests from writing into data/.
    """
    import config.inference_providers as ip
    import chat.llm.fireworks_catalog as fireworks_catalog
    import chat.llm.nearai_catalog as nearai_catalog
    import chat.llm.openrouter_catalog as catalog

    monkeypatch.setattr(ip, "INFERENCE_PROVIDERS_FILE", tmp_path / "inference_providers.json")
    monkeypatch.setattr(ip, "INFERENCE_CREDENTIALS_DIR", tmp_path / "inference_credentials")
    monkeypatch.setattr(catalog, "OPENROUTER_CATALOG_FILE", tmp_path / "openrouter_catalog.json")
    monkeypatch.setattr(fireworks_catalog, "FIREWORKS_CATALOG_FILE", tmp_path / "fireworks_catalog.json")
    monkeypatch.setattr(nearai_catalog, "NEARAI_CATALOG_FILE", tmp_path / "nearai_catalog.json")
    # Same for the admin Model Selection store (read by public_model_catalog
    # and the turn-level usage check): tmp_path means "file absent", i.e.
    # the historical default top-level picks.
    import config.model_selection as model_selection

    monkeypatch.setattr(model_selection, "MODEL_SELECTION_FILE", tmp_path / "model_selection.json")
