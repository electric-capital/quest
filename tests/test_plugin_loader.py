"""Tests for the filesystem plugin loader (config/plugins.py).

Covers discovery (valid / broken / hidden directories), manifest
validation, the registry fan-out of the in-repo ``plugins/_example``
smoke plugin, skip-and-log resilience, and the string-keyed
action-request-type round-trip through the DB.
"""

import asyncio
import json
import os
import textwrap
from unittest.mock import patch

import pytest

from config import plugins as plugins_mod
from config.plugin_types import CredentialField, HelpLink, PluginTool, QuestPlugin


def _run(coro):
    return asyncio.run(coro)


def _load_example_plugin() -> QuestPlugin:
    module = plugins_mod._import_plugin_module(
        plugins_mod.PLUGINS_DIR / "_example" / "plugin.py"
    )
    return module.get_plugin()


# Teardown goes through the loader's own inverse so the fan-out targets
# can never drift from register_plugin (config/plugins.py).
_unregister = plugins_mod.unregister_plugin


@pytest.fixture()
def example_plugin():
    """The _example smoke plugin, registered into the live registries."""
    plugin = _load_example_plugin()
    plugins_mod.register_plugin(plugin)
    yield plugin
    _unregister(plugin)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_VALID_PLUGIN = textwrap.dedent("""
    from config.plugin_types import QuestPlugin

    def get_plugin():
        return QuestPlugin(id={plugin_id!r}, label="Test Plugin")
""")


def _write_plugin(root, dirname, source):
    d = root / dirname
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(source)


class TestDiscovery:
    def test_discovers_valid_plugin(self, tmp_path):
        _write_plugin(tmp_path, "tplug", _VALID_PLUGIN.format(plugin_id="tplug"))
        found = plugins_mod.discover_plugins(plugins_dir=tmp_path)
        assert [p.id for p in found] == ["tplug"]

    def test_missing_dir_returns_empty(self, tmp_path):
        assert plugins_mod.discover_plugins(plugins_dir=tmp_path / "nope") == []

    def test_broken_plugin_is_skipped(self, tmp_path, caplog):
        _write_plugin(tmp_path, "broken", "raise RuntimeError('boom')\n")
        _write_plugin(tmp_path, "tplug", _VALID_PLUGIN.format(plugin_id="tplug"))
        found = plugins_mod.discover_plugins(plugins_dir=tmp_path)
        assert [p.id for p in found] == ["tplug"]
        assert "Skipping broken plugin" in caplog.text

    def test_missing_get_plugin_is_skipped(self, tmp_path):
        _write_plugin(tmp_path, "noexport", "x = 1\n")
        assert plugins_mod.discover_plugins(plugins_dir=tmp_path) == []

    def test_hidden_directories_are_skipped(self, tmp_path):
        _write_plugin(tmp_path, "_hidden", _VALID_PLUGIN.format(plugin_id="hidden"))
        assert plugins_mod.discover_plugins(plugins_dir=tmp_path) == []
        found = plugins_mod.discover_plugins(
            plugins_dir=tmp_path, include_hidden=True,
        )
        assert [p.id for p in found] == ["hidden"]

    def test_dir_without_plugin_py_is_ignored(self, tmp_path):
        (tmp_path / "notaplugin").mkdir()
        assert plugins_mod.discover_plugins(plugins_dir=tmp_path) == []

    def test_duplicate_id_second_is_skipped(self, tmp_path):
        _write_plugin(tmp_path, "aaa", _VALID_PLUGIN.format(plugin_id="dup"))
        _write_plugin(tmp_path, "bbb", _VALID_PLUGIN.format(plugin_id="dup"))
        found = plugins_mod.discover_plugins(plugins_dir=tmp_path)
        assert [p.id for p in found] == ["dup"]


# ---------------------------------------------------------------------------
# QUEST_PLUGIN_PATH extra search roots
# ---------------------------------------------------------------------------


class TestPluginPathEnv:
    def _builtin(self, tmp_path, monkeypatch):
        builtin = tmp_path / "builtin"
        builtin.mkdir()
        monkeypatch.setattr(plugins_mod, "PLUGINS_DIR", builtin)
        return builtin

    def test_env_roots_scanned_after_builtin_in_listed_order(
        self, tmp_path, monkeypatch,
    ):
        builtin = self._builtin(tmp_path, monkeypatch)
        extra1 = tmp_path / "extra1"
        extra2 = tmp_path / "extra2"
        _write_plugin(builtin, "aplug", _VALID_PLUGIN.format(plugin_id="aplug"))
        _write_plugin(extra1, "bplug", _VALID_PLUGIN.format(plugin_id="bplug"))
        _write_plugin(extra2, "cplug", _VALID_PLUGIN.format(plugin_id="cplug"))
        monkeypatch.setenv(
            plugins_mod.PLUGIN_PATH_ENV, f"{extra2}{os.pathsep}{extra1}",
        )
        found = plugins_mod.discover_plugins()
        assert [p.id for p in found] == ["aplug", "cplug", "bplug"]

    def test_duplicate_id_across_roots_builtin_wins(
        self, tmp_path, monkeypatch, caplog,
    ):
        builtin = self._builtin(tmp_path, monkeypatch)
        extra = tmp_path / "extra"
        _write_plugin(builtin, "aaa", _VALID_PLUGIN.format(plugin_id="dup"))
        _write_plugin(extra, "bbb", _VALID_PLUGIN.format(plugin_id="dup"))
        monkeypatch.setenv(plugins_mod.PLUGIN_PATH_ENV, str(extra))
        found = plugins_mod.discover_plugins()
        assert [p.id for p in found] == ["dup"]
        assert "Skipping broken plugin" in caplog.text

    def test_same_dirname_in_two_roots_both_load(self, tmp_path, monkeypatch):
        builtin = self._builtin(tmp_path, monkeypatch)
        extra = tmp_path / "extra"
        _write_plugin(builtin, "shared", _VALID_PLUGIN.format(plugin_id="one"))
        _write_plugin(extra, "shared", _VALID_PLUGIN.format(plugin_id="two"))
        monkeypatch.setenv(plugins_mod.PLUGIN_PATH_ENV, str(extra))
        found = plugins_mod.discover_plugins()
        assert [p.id for p in found] == ["one", "two"]

    def test_missing_entry_logged_and_skipped(
        self, tmp_path, monkeypatch, caplog,
    ):
        builtin = self._builtin(tmp_path, monkeypatch)
        extra = tmp_path / "extra"
        _write_plugin(builtin, "aplug", _VALID_PLUGIN.format(plugin_id="aplug"))
        _write_plugin(extra, "bplug", _VALID_PLUGIN.format(plugin_id="bplug"))
        monkeypatch.setenv(
            plugins_mod.PLUGIN_PATH_ENV,
            f"{tmp_path / 'nope'}{os.pathsep}{extra}",
        )
        found = plugins_mod.discover_plugins()
        assert [p.id for p in found] == ["aplug", "bplug"]
        assert "is not a directory" in caplog.text

    def test_blank_and_repeated_entries_dropped(self, tmp_path, monkeypatch):
        builtin = self._builtin(tmp_path, monkeypatch)
        extra = tmp_path / "extra"
        extra.mkdir()
        monkeypatch.setenv(
            plugins_mod.PLUGIN_PATH_ENV,
            os.pathsep.join(["", str(extra), str(builtin), f" {extra} "]),
        )
        assert plugins_mod._extra_plugin_roots() == [extra.resolve()]

    def test_explicit_plugins_dir_ignores_env(self, tmp_path, monkeypatch):
        extra = tmp_path / "extra"
        only = tmp_path / "only"
        _write_plugin(extra, "eplug", _VALID_PLUGIN.format(plugin_id="eplug"))
        _write_plugin(only, "oplug", _VALID_PLUGIN.format(plugin_id="oplug"))
        monkeypatch.setenv(plugins_mod.PLUGIN_PATH_ENV, str(extra))
        found = plugins_mod.discover_plugins(plugins_dir=only)
        assert [p.id for p in found] == ["oplug"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def _dummy_tool_handler(ctx, args):
    return "{}"


def _tool(name):
    return PluginTool(
        spec={"name": name, "description": "d", "parameters": {"type": "object", "properties": {}}},
        handler=_dummy_tool_handler,
    )


class _StrTypeHandler:
    """Minimal duck-typed action-request handler with a string type_name."""

    def __init__(self, type_name):
        self._type_name = type_name

    @property
    def type_name(self):
        return self._type_name


class TestValidation:
    def test_bad_id_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="9bad", label="X"), set(),
            )

    def test_core_service_name_collision_rejected(self):
        with pytest.raises(ValueError, match="core service"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="ramp", label="X"), set(),
            )

    def test_duplicate_id_rejected(self):
        with pytest.raises(ValueError, match="Duplicate"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X"), {"tp"},
            )

    def test_unprefixed_tool_rejected(self):
        with pytest.raises(ValueError, match="must be prefixed 'tp_'"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X", tools=(_tool("ping"),)), set(),
            )

    def test_public_allowlist_tool_collision_rejected(self):
        # "project_db_query" IS "project_"-prefixed, so only the explicit
        # public-allowlist guard catches it.
        with pytest.raises(ValueError, match="public-project"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="project", label="X", tools=(_tool("project_db_query"),),
                ),
                set(),
            )

    def test_unprefixed_action_type_rejected(self):
        with pytest.raises(ValueError, match="must be prefixed 'tp_'"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="tp", label="X",
                    action_request_handlers=(_StrTypeHandler("echo"),),
                ),
                set(),
            )

    def test_bad_skill_id_rejected(self):
        from chat.system_skills import SystemSkill
        skill = SystemSkill(
            id="system:other", name="X", description="d",
            when_to_load="w", content_builder=lambda b, k: "",
        )
        with pytest.raises(ValueError, match="system skill id"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X", system_skills=(skill,)), set(),
            )

    def test_script_allowlist_must_be_subset_of_tools(self):
        with pytest.raises(ValueError, match="does not declare"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="tp", label="X", tools=(_tool("tp_ping"),),
                    script_tool_allowlist=frozenset({"tp_other"}),
                ),
                set(),
            )

    def test_malformed_service_entry_rejected(self):
        with pytest.raises(ValueError, match="services entries"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X", services=({"key": "h"},)), set(),
            )

    def test_bad_user_connection_rejected(self):
        from config.plugin_types import UserConnectionSpec
        with pytest.raises(ValueError, match="kind"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="tp", label="X",
                    user_connection=UserConnectionSpec(
                        kind="carrier_pigeon", connected=lambda row: True,
                    ),
                ),
                set(),
            )
        with pytest.raises(ValueError, match="connected is not callable"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="tp", label="X",
                    user_connection=UserConnectionSpec(
                        kind="api_key", connected=True,
                    ),
                ),
                set(),
            )
        with pytest.raises(ValueError, match="validate_key is not callable"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="tp", label="X",
                    user_connection=UserConnectionSpec(
                        kind="api_key", connected=lambda row: True,
                        validate_key="not callable",
                    ),
                ),
                set(),
            )

    def test_oauth_user_connection_validation(self):
        from types import SimpleNamespace
        from config.plugin_types import UserConnectionSpec

        def _router(*paths):
            return SimpleNamespace(
                routes=[SimpleNamespace(path=p) for p in paths],
            )

        def _plugin(**uc_kwargs):
            return QuestPlugin(
                id="tp", label="X",
                user_connection=UserConnectionSpec(
                    kind="oauth", connected=lambda row: True, **uc_kwargs,
                ),
            )

        # No router at all -> rejected.
        with pytest.raises(ValueError, match="must supply an oauth_router"):
            plugins_mod.validate_plugin(_plugin(), set())

        # Routes outside the plugin's /auth/<id> namespace -> rejected.
        with pytest.raises(ValueError, match="outside the plugin's"):
            plugins_mod.validate_plugin(
                _plugin(oauth_router=_router("/auth/tp", "/auth/other/x")),
                set(),
            )
        with pytest.raises(ValueError, match="outside the plugin's"):
            plugins_mod.validate_plugin(
                # Prefix-string trickery: /auth/tpx is not /auth/tp/...
                _plugin(oauth_router=_router("/auth/tpx")), set(),
            )

        # An id's underscores are written as hyphens in the URL namespace
        # (/auth/two-words, like the core /auth/google-services); the
        # underscore spelling is outside it.
        def _underscore_plugin(*paths):
            return QuestPlugin(
                id="two_words", label="X",
                user_connection=UserConnectionSpec(
                    kind="oauth", connected=lambda row: True,
                    oauth_router=_router(*paths),
                ),
            )

        assert plugins_mod.plugin_auth_prefix("two_words") == "/auth/two-words"
        assert plugins_mod.plugin_auth_prefix("tp") == "/auth/tp"
        plugins_mod.validate_plugin(
            _underscore_plugin("/auth/two-words", "/auth/two-words/callback"),
            set(),
        )
        with pytest.raises(ValueError, match="outside the plugin's"):
            plugins_mod.validate_plugin(
                _underscore_plugin("/auth/two_words"), set(),
            )

        # api_key-only fields rejected on an oauth spec.
        with pytest.raises(ValueError, match="validate_key"):
            plugins_mod.validate_plugin(
                _plugin(
                    oauth_router=_router("/auth/tp"),
                    validate_key=lambda key: None,
                ),
                set(),
            )
        with pytest.raises(ValueError, match="key_placeholder"):
            plugins_mod.validate_plugin(
                _plugin(
                    oauth_router=_router("/auth/tp"),
                    key_placeholder="Paste key",
                ),
                set(),
            )

        with pytest.raises(ValueError, match="key_help"):
            plugins_mod.validate_plugin(
                _plugin(
                    oauth_router=_router("/auth/tp"),
                    key_help="Where to get the key",
                ),
                set(),
            )
        with pytest.raises(ValueError, match="key_help_links"):
            plugins_mod.validate_plugin(
                _plugin(
                    oauth_router=_router("/auth/tp"),
                    key_help_links=(HelpLink("Keys", "https://x.example/keys"),),
                ),
                set(),
            )

        # api_key help links must be https URLs with labels.
        def _api_key_plugin(**uc_kwargs):
            return QuestPlugin(
                id="tp", label="X",
                user_connection=UserConnectionSpec(
                    kind="api_key", connected=lambda row: True, **uc_kwargs,
                ),
            )
        plugins_mod.validate_plugin(
            _api_key_plugin(key_help_links=(HelpLink("Keys", "https://x.example/keys"),)),
            set(),
        )
        with pytest.raises(ValueError, match="https://"):
            plugins_mod.validate_plugin(
                _api_key_plugin(key_help_links=(HelpLink("Keys", "http://x.example/keys"),)),
                set(),
            )
        with pytest.raises(ValueError, match="empty label"):
            plugins_mod.validate_plugin(
                _api_key_plugin(key_help_links=(HelpLink("  ", "https://x.example/keys"),)),
                set(),
            )
        with pytest.raises(ValueError, match="HelpLink instances"):
            plugins_mod.validate_plugin(
                _api_key_plugin(key_help_links=(("Keys", "https://x.example/keys"),)),
                set(),
            )

        # oauth_router rejected on an api_key spec.
        with pytest.raises(ValueError, match="must not set.*oauth_router"):
            plugins_mod.validate_plugin(
                QuestPlugin(
                    id="tp", label="X",
                    user_connection=UserConnectionSpec(
                        kind="api_key", connected=lambda row: True,
                        oauth_router=_router("/auth/tp"),
                    ),
                ),
                set(),
            )

        # Bad scopes / needs_reauth rejected.
        with pytest.raises(ValueError, match="scopes"):
            plugins_mod.validate_plugin(
                _plugin(oauth_router=_router("/auth/tp"), scopes=("ok", "")),
                set(),
            )
        with pytest.raises(ValueError, match="needs_reauth is not callable"):
            plugins_mod.validate_plugin(
                _plugin(oauth_router=_router("/auth/tp"), needs_reauth=True),
                set(),
            )

        # A well-formed oauth spec validates.
        plugins_mod.validate_plugin(
            _plugin(
                oauth_router=_router("/auth/tp", "/auth/tp/callback"),
                scopes=("repo",),
                needs_reauth=lambda row: False,
            ),
            set(),
        )

    def test_overlong_skill_description_rejected(self):
        # The catalog's static skill checks (description <= 120 chars) run
        # at validation time, not just registration -- otherwise a bad
        # description only surfaces as a skipped plugin at server startup.
        from chat.system_skills import SystemSkill
        skill = SystemSkill(
            id="system:tp", name="X", description="x" * 121,
            when_to_load="w", content_builder=lambda b, k: "",
        )
        with pytest.raises(ValueError, match="exceeds 120 chars"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X", system_skills=(skill,)), set(),
            )

    def test_non_callable_on_shutdown_rejected(self):
        with pytest.raises(ValueError, match="on_shutdown is not callable"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X", on_shutdown="not-a-callable"),
                set(),
            )

    def test_non_callable_post_load_rejected(self):
        with pytest.raises(ValueError, match="post_load is not callable"):
            plugins_mod.validate_plugin(
                QuestPlugin(id="tp", label="X", post_load="not-a-callable"),
                set(),
            )

    def test_example_plugin_validates(self):
        plugins_mod.validate_plugin(_load_example_plugin(), set())

    def test_router_field_removed_from_manifest(self):
        # Design decision: plugins cannot mount HTTP routes (a mounted
        # /api/* route is LLM-reachable via curl_proxy_get with no
        # gating). A manifest passing router= fails construction and is
        # treated as a broken plugin by discovery.
        with pytest.raises(TypeError):
            QuestPlugin(id="tp", label="X", router=object())


# ---------------------------------------------------------------------------
# In-tree plugins
# ---------------------------------------------------------------------------


def _in_tree_plugin_dirs() -> list[str]:
    """Directory names of the real (non-hidden) in-repo plugins."""
    return sorted(
        child.name
        for child in plugins_mod.PLUGINS_DIR.iterdir()
        if child.is_dir()
        and not child.name.startswith(("_", "."))
        and (child / "plugin.py").is_file()
    )


class TestInTreePlugins:
    """Every shipped plugin must import and validate cleanly.

    discover_plugins()/load_plugins() skip-and-log broken plugins so the
    server survives them, which also means startup won't fail CI when an
    in-tree plugin regresses (e.g. a system-skill description over the
    catalog's 120-char limit). This sweep imports each plugin the way
    startup does and runs the loader validation strictly, and picks up
    future plugin directories automatically.
    """

    @pytest.mark.parametrize("plugin_dir", _in_tree_plugin_dirs())
    def test_in_tree_plugin_imports_and_validates(self, plugin_dir):
        module = plugins_mod._import_plugin_module(
            plugins_mod.PLUGINS_DIR / plugin_dir / "plugin.py"
        )
        plugin = module.get_plugin()
        plugins_mod.validate_plugin(plugin, set())

    def test_sweep_covers_the_expected_roster(self):
        # Guards the sweep itself: if the directory scan silently broke
        # (e.g. plugins moved), parametrization would collect nothing and
        # every in-tree plugin would go untested.
        assert set(_in_tree_plugin_dirs()) >= {
            "github", "m365", "twitter", "telegram",
        }


# ---------------------------------------------------------------------------
# Registry fan-out (the _example smoke plugin exercises every field)
# ---------------------------------------------------------------------------


class TestFanOut:
    def test_tool_registered_in_registry_and_dispatch_table(self, example_plugin):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        assert "example_ping" in TOOL_CALL_REGISTRY
        assert TOOL_CALL_REGISTRY["example_ping"]["requires_service"] == "example"
        assert "example_ping" in TOOL_CALL_HANDLERS

    def test_action_request_handler_and_enum(self, example_plugin):
        from chat.action_request_types import get_handler
        from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM, _CREATE_ACTION_REQUEST
        assert get_handler("example_echo") is not None
        assert "example_echo" in ACTION_REQUEST_TYPE_ENUM
        # The live create_action_request schema shares the enum list.
        schema_enum = _CREATE_ACTION_REQUEST["parameters"]["properties"]["request_type"]["enum"]
        assert "example_echo" in schema_enum

    def test_system_skill_in_catalog(self, example_plugin):
        from chat.system_skills import CATALOG
        assert "system:example" in CATALOG

    def test_service_registered_with_compiled_patterns(self, example_plugin):
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        entry = _SERVICE_REGISTRY["api.example.com"]
        assert entry["name"] == "Example API"
        assert all(hasattr(p, "match") for p in entry["_allowed_endpoints"])
        assert all(hasattr(p, "match") for p in entry["_allowed_post_endpoints"])

    def test_script_allowlist_extended(self, example_plugin):
        from chat.gemini_api import script_tool_call
        assert "example_ping" in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST

    def test_loaded_plugins_accessor(self):
        plugin = _load_example_plugin()
        plugins_mod.register_plugin(plugin)
        plugins_mod._LOADED.append(plugin)
        try:
            assert any(p.id == "example" for p in plugins_mod.get_loaded_plugins())
        finally:
            _unregister(plugin)

    def test_plugin_tool_dispatches_via_tool_call(self, example_plugin):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        result, extra = _run(_dispatch_tool_call(
            app=None, provider=None,
            user={"id": 1, "email": "t@example.com"},
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "example_ping", "arguments": {"value": "hi"}},
        ))
        assert json.loads(result) == {"pong": "hi"}
        assert extra == []

    def test_plugin_tool_blocked_in_public_projects(self, example_plugin):
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call
        result, _ = _run(_dispatch_tool_call(
            app=None, provider=None,
            user={"id": 1, "email": "t@example.com"},
            conversation_id="c1", timezone="UTC",
            tool_name="tool_call",
            args={"tool_name": "example_ping", "arguments": {}},
            is_public=True,
        ))
        assert "not available in public-project" in result

    def test_mutating_flag_stamped_and_enforced_for_inference_runs(self):
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY, mutating_tool_call_tools
        from chat.gemini_api.tool_dispatch import _dispatch_tool_call

        plugin = QuestPlugin(
            id="tp", label="X",
            tools=(
                _tool("tp_read"),
                PluginTool(
                    spec={"name": "tp_write", "description": "d",
                          "parameters": {"type": "object", "properties": {}}},
                    handler=_dummy_tool_handler, mutating=True,
                ),
            ),
        )
        plugins_mod.validate_plugin(plugin, set())
        plugins_mod.register_plugin(plugin)
        try:
            assert TOOL_CALL_REGISTRY["tp_write"]["mutating"] is True
            assert "mutating" not in TOOL_CALL_REGISTRY["tp_read"]
            assert "tp_write" in mutating_tool_call_tools()
            assert "tp_read" not in mutating_tool_call_tools()

            def _go(name):
                result, _ = _run(_dispatch_tool_call(
                    app=None, provider=None,
                    user={"id": 1, "email": "t@example.com"},
                    conversation_id="c1", timezone="UTC",
                    tool_name="tool_call",
                    args={"tool_name": name, "arguments": {}},
                    is_inference_api=True,
                ))
                return result

            assert "not available in inference API runs" in _go("tp_write")
            assert "not available in inference API runs" not in _go("tp_read")
        finally:
            _unregister(plugin)

    def test_plugin_tool_gated_out_of_prompt_when_service_missing(self, example_plugin):
        from chat.gemini_api.system_prompt import _build_dynamic_tools_section
        assert "example_ping" not in _build_dynamic_tools_section(
            connected_services={"example": False},
        )
        assert "example_ping" in _build_dynamic_tools_section(
            connected_services={"example": True},
        )
        # Unknown caller context (None) keeps the show-everything behavior.
        assert "example_ping" in _build_dynamic_tools_section()

    def test_action_request_preview_for_plugin_type(self, example_plugin):
        from chat.action_request_types import get_preview_for_request
        preview = _run(get_preview_for_request(
            "example_echo", {"message": "hello"},
        ))
        assert preview["display_name"] == "Example Echo"
        assert preview["preview_fields"] == [{"key": "message", "value": "hello"}]


# ---------------------------------------------------------------------------
# Skip-and-log resilience
# ---------------------------------------------------------------------------

_REG_CONFLICT_PLUGIN = textwrap.dedent("""
    from config.plugin_types import QuestPlugin

    def _load():
        return None

    def _inject(headers, _creds):
        return headers

    def get_plugin():
        # Collides with the core Airtable authed_get entry -> registration fails.
        return QuestPlugin(
            id="treg", label="T",
            services=({"key": "api.airtable.com", "entry": {
                "name": "X", "load_credentials": _load, "inject_auth": _inject,
            }},),
        )
""")


class TestLoadPlugins:
    def test_registration_failure_is_skipped_not_raised(self, tmp_path, caplog):
        _write_plugin(tmp_path, "treg", _REG_CONFLICT_PLUGIN)
        loaded_before = plugins_mod.get_loaded_plugins()
        result = plugins_mod.load_plugins(plugins_dir=tmp_path)
        assert result == loaded_before  # nothing new registered
        assert "registration failed" in caplog.text
        # The core entry was not clobbered.
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        assert _SERVICE_REGISTRY["api.airtable.com"]["name"] == "Airtable"

    def test_load_plugins_registers_valid_plugin(self, tmp_path):
        _write_plugin(tmp_path, "tplug", _VALID_PLUGIN.format(plugin_id="tplug"))
        result = plugins_mod.load_plugins(plugins_dir=tmp_path)
        try:
            assert any(p.id == "tplug" for p in result)
        finally:
            plugins_mod._LOADED[:] = [
                p for p in plugins_mod._LOADED if p.id != "tplug"
            ]

    def test_post_load_hook_runs_after_registration(self, tmp_path):
        _write_plugin(tmp_path, "thook", textwrap.dedent("""
            from config.plugin_types import QuestPlugin

            CALLS = []

            def _hook():
                # Registration already happened when the hook runs.
                from config.plugins import get_loaded_plugins
                CALLS.append([p.id for p in get_loaded_plugins()])

            def get_plugin():
                return QuestPlugin(id="thook", label="Hooked", post_load=_hook)
        """))
        plugins_mod.load_plugins(plugins_dir=tmp_path)
        try:
            import sys
            module = sys.modules["quest_plugin_thook"]
            assert len(module.CALLS) == 1
            assert "thook" in module.CALLS[0]
        finally:
            plugins_mod._LOADED[:] = [
                p for p in plugins_mod._LOADED if p.id != "thook"
            ]

    def test_failing_post_load_hook_keeps_plugin_loaded(self, tmp_path, caplog):
        _write_plugin(tmp_path, "tboom", textwrap.dedent("""
            from config.plugin_types import QuestPlugin

            def _hook():
                raise RuntimeError("hook boom")

            def get_plugin():
                return QuestPlugin(id="tboom", label="Boom", post_load=_hook)
        """))
        result = plugins_mod.load_plugins(plugins_dir=tmp_path)
        try:
            assert any(p.id == "tboom" for p in result)
            assert "post_load hook failed" in caplog.text
        finally:
            plugins_mod._LOADED[:] = [
                p for p in plugins_mod._LOADED if p.id != "tboom"
            ]


# ---------------------------------------------------------------------------
# Atomic registration (preflight + rollback)
# ---------------------------------------------------------------------------


def _example_surface_absent() -> None:
    """Assert no trace of the _example plugin in any registry."""
    from chat.system_skills import CATALOG
    from chat.action_request_types import registry as ar_registry
    from chat.llm import tool_schemas
    from chat.gemini_api import script_tool_call, tool_dispatch
    from chat.gemini_api.authed_get import _SERVICE_REGISTRY
    import config.service_credentials as service_credentials
    import config.service_specs as service_specs

    assert "example" not in service_credentials.KNOWN_SERVICES
    assert "example" not in service_specs._PLUGIN_SPECS
    assert "api.example.com" not in _SERVICE_REGISTRY
    assert "system:example" not in CATALOG
    assert "example_echo" not in ar_registry._REGISTRY
    assert "example_echo" not in tool_schemas.ACTION_REQUEST_TYPE_ENUM
    assert "example_ping" not in tool_schemas.TOOL_CALL_REGISTRY
    assert "example_ping" not in tool_schemas.PLUGIN_TOOL_NAMES
    assert "example_ping" not in tool_dispatch.TOOL_CALL_HANDLERS
    assert "example_ping" not in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST
    assert "example" not in plugins_mod._REGISTRATIONS


class TestAtomicRegistration:
    def test_preflight_reports_every_collision_without_mutating(self):
        # Collides with a core tool AND a core authed_get service; the
        # manifest is otherwise valid (unprefixed_tools exempts the name).
        from chat.system_skills import CATALOG, SystemSkill
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        import config.service_credentials as service_credentials

        plugin = QuestPlugin(
            id="tpre", label="X",
            credential_schema=(
                CredentialField(key="token", label="Token", type="secret"),
            ),
            services=({"key": "api.airtable.com", "entry": {
                "name": "X", "load_credentials": lambda: None,
                "inject_auth": lambda h, c: h,
            }},),
            system_skills=(SystemSkill(
                id="system:tpre", name="X", description="d",
                when_to_load="w", content_builder=lambda b, k: "",
            ),),
            tools=(_tool("memory_search"),),
            unprefixed_tools=frozenset({"memory_search"}),
        )
        plugins_mod.validate_plugin(plugin, set())
        with pytest.raises(ValueError) as excinfo:
            plugins_mod.register_plugin(plugin)
        message = str(excinfo.value)
        assert "collides with already-registered entries" in message
        assert "tool 'memory_search'" in message
        assert "authed_get service 'api.airtable.com'" in message
        # Nothing was registered -- not even the stages before the clash.
        assert "tpre" not in service_credentials.KNOWN_SERVICES
        assert "system:tpre" not in CATALOG
        assert "tpre" not in plugins_mod._REGISTRATIONS
        # The core entries the manifest named are untouched, and
        # unregistering the never-registered manifest can't remove them.
        plugins_mod.unregister_plugin(plugin)
        assert _SERVICE_REGISTRY["api.airtable.com"]["name"] == "Airtable"
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        assert "memory_search" in TOOL_CALL_HANDLERS

    @pytest.mark.parametrize("failing_step", [
        "register_system_skill",
        "register_action_request_type",
        "register_dynamic_tool",
        "extend_script_allowlist",
    ])
    def test_failure_at_any_stage_rolls_back_earlier_stages(self, failing_step):
        # Fault injection: the _example plugin exercises every manifest
        # field, so breaking one fan-out target at a time proves each
        # earlier stage is undone (credentials, services, skills, action
        # types, tools, allowlist) and no plugin-owned entry survives.
        targets = {
            "register_system_skill": "chat.system_skills.register_system_skill",
            "register_action_request_type":
                "chat.llm.tool_schemas.register_action_request_type",
            "register_dynamic_tool":
                "chat.gemini_api.tool_dispatch.register_dynamic_tool",
            "extend_script_allowlist":
                "chat.gemini_api.script_tool_call.extend_script_allowlist",
        }
        plugin = _load_example_plugin()
        with patch(targets[failing_step], side_effect=RuntimeError("injected")):
            with pytest.raises(RuntimeError, match="injected"):
                plugins_mod.register_plugin(plugin)
        _example_surface_absent()

    def test_rolled_back_plugin_registers_cleanly_afterwards(self):
        # The "stale collision" failure mode: a plugin whose first attempt
        # died mid-registration must not block its own corrected retry.
        plugin = _load_example_plugin()
        with patch(
            "chat.gemini_api.script_tool_call.extend_script_allowlist",
            side_effect=RuntimeError("injected"),
        ):
            with pytest.raises(RuntimeError):
                plugins_mod.register_plugin(plugin)
        plugins_mod.register_plugin(plugin)
        try:
            from chat.system_skills import CATALOG
            from chat.gemini_api import script_tool_call
            assert "system:example" in CATALOG
            assert "example_ping" in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST
        finally:
            _unregister(plugin)
        _example_surface_absent()

    def test_corrected_manifest_registers_after_preflight_rejection(self):
        broken = QuestPlugin(
            id="tfix", label="X",
            tools=(_tool("memory_search"),),
            unprefixed_tools=frozenset({"memory_search"}),
        )
        with pytest.raises(ValueError, match="collides"):
            plugins_mod.register_plugin(broken)
        fixed = QuestPlugin(id="tfix", label="X", tools=(_tool("tfix_search"),))
        plugins_mod.register_plugin(fixed)
        try:
            from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
            assert "tfix_search" in TOOL_CALL_HANDLERS
        finally:
            _unregister(fixed)
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY
        assert "tfix_search" not in TOOL_CALL_REGISTRY

    def test_rollback_preserves_other_plugins_entries(self, example_plugin):
        # A second plugin failing late must leave the already-loaded
        # example plugin's surface (and the core tables) exactly as they
        # were; the journal only ever holds the failing plugin's own adds.
        from chat.system_skills import CATALOG, SystemSkill
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS

        other = QuestPlugin(
            id="tother", label="X",
            services=({"key": "api.other.example", "entry": {
                "name": "Other", "load_credentials": lambda: None,
                "inject_auth": lambda h, c: h,
            }},),
            system_skills=(SystemSkill(
                id="system:tother", name="X", description="d",
                when_to_load="w", content_builder=lambda b, k: "",
            ),),
            tools=(_tool("tother_ping"),),
        )
        with patch(
            "chat.gemini_api.tool_dispatch.register_dynamic_tool",
            side_effect=RuntimeError("injected"),
        ):
            with pytest.raises(RuntimeError):
                plugins_mod.register_plugin(other)
        assert "api.other.example" not in _SERVICE_REGISTRY
        assert "system:tother" not in CATALOG
        assert "system:example" in CATALOG
        assert _SERVICE_REGISTRY["api.example.com"]["name"] == "Example API"
        assert "example_ping" in TOOL_CALL_HANDLERS
        assert "memory_search" in TOOL_CALL_HANDLERS

    def test_double_registration_rejected(self, example_plugin):
        with pytest.raises(ValueError, match="already registered"):
            plugins_mod.register_plugin(example_plugin)

    def test_shared_allowlist_names_survive_another_plugins_unregister(self):
        # Ownership: the allowlist undo subtracts only the names the plugin
        # added. A name already allow-listed (here via a prior plugin) is
        # not removed when a later plugin naming it is unregistered.
        from chat.gemini_api import script_tool_call
        first = QuestPlugin(
            id="tfirst", label="X", tools=(_tool("tfirst_ping"),),
            script_tool_allowlist=frozenset({"tfirst_ping"}),
        )
        plugins_mod.register_plugin(first)
        try:
            script_tool_call.extend_script_allowlist({"tfirst_ping"})  # no-op union
            second = QuestPlugin(id="tsecond", label="X")
            plugins_mod.register_plugin(second)
            plugins_mod.unregister_plugin(second)
            assert "tfirst_ping" in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST
        finally:
            _unregister(first)
        assert "tfirst_ping" not in script_tool_call.SCRIPT_TOOL_CALL_ALLOWLIST

    def test_load_plugins_leaves_no_partial_surface(self, tmp_path, caplog):
        # End to end through the loader: a plugin whose credential card and
        # service register fine but whose tool clashes with a core tool is
        # skipped with NO leftover card, store roster entry, or service.
        _write_plugin(tmp_path, "tpart", textwrap.dedent("""
            from config.plugin_types import CredentialField, PluginTool, QuestPlugin

            async def _h(ctx, args):
                return "{}"

            def get_plugin():
                return QuestPlugin(
                    id="tpart", label="Partial",
                    credential_schema=(
                        CredentialField(key="token", label="Token", type="secret"),
                    ),
                    services=({"key": "api.partial.example", "entry": {
                        "name": "Partial", "load_credentials": lambda: None,
                        "inject_auth": lambda h, c: h,
                    }},),
                    tools=(PluginTool(
                        spec={"name": "memory_search", "description": "d",
                              "parameters": {"type": "object", "properties": {}}},
                        handler=_h,
                    ),),
                    unprefixed_tools=frozenset({"memory_search"}),
                )
        """))
        loaded_before = plugins_mod.get_loaded_plugins()
        assert plugins_mod.load_plugins(plugins_dir=tmp_path) == loaded_before
        assert "registration failed" in caplog.text
        from chat.gemini_api.authed_get import _SERVICE_REGISTRY
        import config.service_credentials as service_credentials
        import config.service_specs as service_specs
        assert "tpart" not in service_credentials.KNOWN_SERVICES
        assert service_specs.get_service_spec("tpart") is None
        assert "api.partial.example" not in _SERVICE_REGISTRY
        assert "tpart" not in plugins_mod._REGISTRATIONS


# ---------------------------------------------------------------------------
# on_shutdown hooks (shutdown_plugins)
# ---------------------------------------------------------------------------


class TestShutdownHooks:
    def test_hooks_run_in_reverse_load_order_sync_and_async(self):
        calls: list[str] = []

        def _sync_hook():
            calls.append("first")

        async def _async_hook():
            calls.append("second")

        loaded = [
            QuestPlugin(id="tfirst", label="First", on_shutdown=_sync_hook),
            QuestPlugin(id="tnone", label="None"),
            QuestPlugin(id="tsecond", label="Second", on_shutdown=_async_hook),
        ]
        with patch.object(plugins_mod, "_LOADED", loaded):
            asyncio.run(plugins_mod.shutdown_plugins())
        assert calls == ["second", "first"]

    def test_failing_hook_is_logged_and_others_still_run(self, caplog):
        calls: list[str] = []

        async def _boom():
            raise RuntimeError("shutdown boom")

        def _ok():
            calls.append("ok")

        loaded = [
            QuestPlugin(id="tok", label="Ok", on_shutdown=_ok),
            QuestPlugin(id="tboom", label="Boom", on_shutdown=_boom),
        ]
        with patch.object(plugins_mod, "_LOADED", loaded):
            asyncio.run(plugins_mod.shutdown_plugins())
        assert calls == ["ok"]
        assert "Plugin 'tboom' on_shutdown hook failed" in caplog.text

    def test_example_plugin_declares_the_hook(self):
        module = plugins_mod._import_plugin_module(
            plugins_mod.PLUGINS_DIR / "_example" / "plugin.py"
        )
        plugin = module.get_plugin()
        assert plugin.on_shutdown is not None
        with patch.object(plugins_mod, "_LOADED", [plugin]):
            asyncio.run(plugins_mod.shutdown_plugins())  # no-op, must not raise


# ---------------------------------------------------------------------------
# String-keyed action-request types
# ---------------------------------------------------------------------------


class TestStringKeyedActionTypes:
    def test_registry_round_trip_with_plain_string_type(self):
        from chat.action_request_types.registry import (
            _REGISTRY, get_all_type_names, get_handler, register_handler,
        )
        from chat.action_request_types.base import ActionRequestHandler

        class _PlugHandler(ActionRequestHandler):
            @property
            def type_name(self):
                return "tp_widget_create"

            @property
            def display_name(self):
                return "Create Widget"

            def validate_params(self, params):
                return params

            async def execute(self, params, user, *, conversation_id=None, project_id=None):
                return {"ok": True}

        handler = _PlugHandler()
        register_handler(handler)
        try:
            assert get_handler("tp_widget_create") is handler
            assert "tp_widget_create" in get_all_type_names()
            with pytest.raises(ValueError, match="already registered"):
                register_handler(handler)
        finally:
            _REGISTRY.pop("tp_widget_create", None)

    def test_core_handlers_still_keyed_by_enum_value(self):
        from chat.action_request_types import get_handler
        assert get_handler("create_memory") is not None
        assert get_handler("create_calendar_invite") is not None

    def test_plugin_type_string_round_trips_through_db(self):
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from db.models import ActionRequest, Base, User

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            user = User(email="t@example.com", api_key="k1", name="T")
            session.add(user)
            session.commit()
            session.add(ActionRequest(
                user_id=user.id,
                conversation_id="c1",
                request_type="example_echo",  # NOT an ActionRequestType member
                params={"message": "hi"},
                reasoning="test",
            ))
            session.commit()
            row = session.execute(
                select(ActionRequest).where(
                    ActionRequest.request_type == "example_echo"
                )
            ).scalar_one()
            assert row.params == {"message": "hi"}
            assert row.status == "open"
