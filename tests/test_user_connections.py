"""Tests for multi-connection plumbing in config/.

Covers the connection resolver (config/user_connections.py), the
``form`` / ``multi_connection`` manifest validation in config/plugins.py,
and ``select`` fields on the schema-driven admin credential card
(config/service_specs.py).
"""

import subprocess
import sys

import pytest

from config import plugins as plugins_mod
from config.plugin_types import (
    CredentialField,
    HelpLink,
    PluginTool,
    QuestPlugin,
    UserConnectionSpec,
    VerifyResult,
)
from config.user_connections import (
    ConnectionNotFound,
    ConnectionNotSupported,
    connection_label,
    connection_summary,
    is_multi_connection,
    resolve_connection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _verify(values):
    return VerifyResult(account=values.get("username"))


async def _tool_handler(ctx, args):
    return "{}"


_FORM_FIELDS = (
    CredentialField(key="host", label="Server", type="select", required=True),
    CredentialField(key="username", label="Username", type="text", required=True),
    CredentialField(key="password", label="Password", type="secret", required=True),
)


def _form_spec(**overrides):
    kwargs = dict(
        kind="form",
        connected=lambda row: bool(row.get("oauth_blob")),
        multi_connection=True,
        fields=_FORM_FIELDS,
        field_options=lambda: {"host": ["imap.example.com"]},
        verify=_verify,
    )
    kwargs.update(overrides)
    return UserConnectionSpec(**kwargs)


def _tool(name, *, with_connection=True):
    props = {"folder": {"type": "string"}}
    if with_connection:
        props["connection"] = {"type": "string"}
    return PluginTool(
        spec={
            "name": name, "description": "d",
            "parameters": {"type": "object", "properties": props},
        },
        handler=_tool_handler,
    )


def _plugin(spec=None, tools=(), pid="tmc"):
    from chat.system_skills import SystemSkill
    # A plugin with tools needs a system skill to document them
    # (PluginTool.skill defaults to the plugin's single skill).
    skills = (SystemSkill(
        id=f"system:{pid}", name="X", description="d",
        when_to_load="w", content_builder=lambda b, k: "",
    ),) if tools else ()
    return QuestPlugin(
        id=pid, label="Test Multi", user_connection=spec, tools=tuple(tools),
        system_skills=skills,
    )


def _row(account, *, is_default=False, description=None, row_id=1):
    return {
        "id": row_id, "service": "tmc", "account": account,
        "description": description, "is_default": is_default,
        "secret": None, "oauth_blob": {"values": {}},
    }


def _user(rows, service="tmc"):
    default = next((r for r in rows if r.get("is_default")), None)
    return {
        "id": 1,
        "service_credentials": {service: default} if default else {},
        "service_connections": {service: list(rows)} if rows else {},
    }


@pytest.fixture()
def multi_loaded(monkeypatch):
    """A multi-connection plugin visible to the resolver as loaded."""
    plugin = _plugin(_form_spec(), tools=(_tool("tmc_read"),))
    monkeypatch.setattr(plugins_mod, "_LOADED", [plugin])
    return plugin


@pytest.fixture()
def single_loaded(monkeypatch):
    """A single-connection api_key plugin visible to the resolver."""
    plugin = _plugin(UserConnectionSpec(
        kind="api_key", connected=lambda row: bool(row.get("secret")),
    ))
    monkeypatch.setattr(plugins_mod, "_LOADED", [plugin])
    return plugin


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestResolveConnection:
    def test_default_when_connection_omitted(self, multi_loaded):
        work = _row("work@example.com", is_default=True, row_id=1)
        home = _row("me@home.example", row_id=2)
        user = _user([work, home])
        assert resolve_connection(user, "tmc") is work
        assert resolve_connection(user, "tmc", None) is work
        assert resolve_connection(user, "tmc", "   ") is work

    def test_case_insensitive_match(self, multi_loaded):
        work = _row("work@example.com", is_default=True, row_id=1)
        home = _row("me@home.example", row_id=2)
        user = _user([work, home])
        assert resolve_connection(user, "tmc", "  ME@Home.Example ") is home
        assert resolve_connection(user, "tmc", "WORK@example.com") is work

    def test_not_found_lists_available_default_first(self, multi_loaded):
        # The list is stored out of order; the default still comes first.
        home = _row("me@home.example", row_id=2)
        work = _row("work@example.com", is_default=True, row_id=1)
        user = _user([home, work])
        with pytest.raises(ConnectionNotFound) as excinfo:
            resolve_connection(user, "tmc", "other@example.com")
        err = excinfo.value
        assert isinstance(err, LookupError)
        assert err.service == "tmc"
        assert err.requested == "other@example.com"
        assert err.available == ["work@example.com", "me@home.example"]

    def test_none_when_no_rows(self, multi_loaded):
        assert resolve_connection({"id": 1}, "tmc") is None
        assert resolve_connection(_user([]), "tmc") is None
        assert resolve_connection(_user([]), "tmc", "x@example.com") is None

    def test_single_connection_rejects_other_name(self, single_loaded):
        row = _row("", is_default=True)
        user = _user([row])
        assert resolve_connection(user, "tmc") is row
        with pytest.raises(ConnectionNotSupported) as excinfo:
            resolve_connection(user, "tmc", "someone@example.com")
        assert isinstance(excinfo.value, ValueError)
        assert excinfo.value.service == "tmc"
        assert excinfo.value.requested == "someone@example.com"

    def test_single_connection_accepts_sole_rows_account(self, single_loaded):
        row = _row("acct@example.com", is_default=True)
        user = _user([row])
        assert resolve_connection(user, "tmc", "Acct@Example.com") is row

    def test_unloaded_service_is_single_connection(self, monkeypatch):
        monkeypatch.setattr(plugins_mod, "_LOADED", [])
        assert not is_multi_connection("tmc")
        user = _user([_row("a@example.com", is_default=True)])
        with pytest.raises(ConnectionNotSupported):
            resolve_connection(user, "tmc", "b@example.com")

    def test_falls_back_to_service_credentials(self, multi_loaded):
        # A user dict without service_connections (built before the list
        # existed): the default row is the only row.
        row = _row("work@example.com", is_default=True)
        user = {"id": 1, "service_credentials": {"tmc": row}}
        assert resolve_connection(user, "tmc") is row
        assert resolve_connection(user, "tmc", "WORK@example.com") is row
        with pytest.raises(ConnectionNotFound) as excinfo:
            resolve_connection(user, "tmc", "x@example.com")
        assert excinfo.value.available == ["work@example.com"]

    def test_is_multi_connection(self, multi_loaded):
        assert is_multi_connection("tmc")
        assert not is_multi_connection("nope")


class TestConnectionSummary:
    def test_label(self):
        assert connection_label(_row("a@example.com", is_default=True)) \
            == "a@example.com (default)"
        assert connection_label(_row("a@example.com")) == "a@example.com"
        # Legacy rows carry no account address.
        assert connection_label(_row("", is_default=True)) == "default"
        assert connection_label({"secret": "s", "is_default": True}) == "default"

    def test_summary_default_first_with_fields(self, multi_loaded):
        home = _row("me@home.example", description="personal", row_id=2)
        work = _row("work@example.com", is_default=True,
                    description="main work mailbox", row_id=1)
        summary = connection_summary(_user([home, work]), "tmc")
        assert summary == [
            {"account": "work@example.com", "description": "main work mailbox",
             "is_default": True, "label": "work@example.com (default)"},
            {"account": "me@home.example", "description": "personal",
             "is_default": False, "label": "me@home.example"},
        ]
        # Never leaks ids or credentials.
        assert all(set(s) == {"account", "description", "is_default", "label"}
                   for s in summary)

    def test_summary_empty_and_legacy_shape(self, multi_loaded):
        assert connection_summary({"id": 1}, "tmc") == []
        legacy = {"id": 1, "service_credentials": {"tmc": {"secret": "s"}}}
        assert connection_summary(legacy, "tmc") == [
            {"account": "", "description": None, "is_default": True,
             "label": "default"},
        ]


def test_module_is_stdlib_only_at_import():
    code = (
        "import sys; import config.user_connections; "
        "bad = [m for m in ('config.plugins', 'sqlalchemy', 'fastapi') "
        "if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        cwd=str(plugins_mod.PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Loader validation
# ---------------------------------------------------------------------------


def _validate(plugin):
    plugins_mod.validate_plugin(plugin, set())


class TestFormKindValidation:
    def test_valid_form_spec(self):
        _validate(_plugin(_form_spec(), tools=(_tool("tmc_read"),)))
        # Static select options instead of field_options().
        _validate(_plugin(_form_spec(
            field_options=None,
            fields=(
                CredentialField(key="host", label="H", type="select",
                                options=("a.example", "b.example")),
                CredentialField(key="password", label="P", type="secret"),
            ),
        )))

    def test_form_requires_multi_connection(self):
        with pytest.raises(ValueError, match="requires multi_connection=True"):
            _validate(_plugin(_form_spec(multi_connection=False)))

    def test_form_requires_fields(self):
        with pytest.raises(ValueError, match="needs fields"):
            _validate(_plugin(_form_spec(fields=())))

    def test_form_requires_verify(self):
        with pytest.raises(ValueError, match="verify hook"):
            _validate(_plugin(_form_spec(verify=None)))

    @pytest.mark.parametrize("extra, match", [
        ({"oauth_router": object()}, "oauth_router"),
        ({"scopes": ("x",)}, "scopes"),
        ({"validate_key": lambda k: None}, "validate_key"),
        ({"key_placeholder": "Paste"}, "key_"),
        ({"key_help": "Help"}, "key_"),
        ({"key_help_links": (HelpLink("K", "https://x.example"),)}, "key_"),
    ])
    def test_form_rejects_other_kind_fields(self, extra, match):
        with pytest.raises(ValueError, match=match):
            _validate(_plugin(_form_spec(**extra)))

    def test_non_form_rejects_form_fields(self):
        with pytest.raises(ValueError, match="form-only"):
            _validate(_plugin(UserConnectionSpec(
                kind="api_key", connected=lambda row: True,
                fields=_FORM_FIELDS,
            )))
        with pytest.raises(ValueError, match="form-only"):
            _validate(_plugin(UserConnectionSpec(
                kind="api_key", connected=lambda row: True, verify=_verify,
            )))

    @pytest.mark.parametrize("key", ["Host", "1host", "host-name", "", "h" * 33])
    def test_bad_field_key(self, key):
        fields = (CredentialField(key=key, label="X", type="text"),)
        with pytest.raises(ValueError, match="invalid"):
            _validate(_plugin(_form_spec(fields=fields)))

    def test_max_length_field_key_ok(self):
        fields = (CredentialField(key="h" * 32, label="X", type="text"),)
        _validate(_plugin(_form_spec(fields=fields), tools=()))

    @pytest.mark.parametrize("key", ["description", "account", "connection"])
    def test_reserved_field_key(self, key):
        fields = (CredentialField(key=key, label="X", type="text"),)
        with pytest.raises(ValueError, match="reserved"):
            _validate(_plugin(_form_spec(fields=fields)))

    def test_select_needs_options_or_field_options(self):
        fields = (CredentialField(key="host", label="H", type="select"),)
        with pytest.raises(ValueError, match="needs options"):
            _validate(_plugin(_form_spec(fields=fields, field_options=None)))
        # Runtime options cover it.
        _validate(_plugin(_form_spec(fields=fields)))

    def test_options_must_be_tuple_of_nonempty_strings(self):
        for options in (["a"], ("a", ""), ("a", 3)):
            fields = (CredentialField(key="host", label="H", type="select",
                                      options=options),)
            with pytest.raises(ValueError, match="tuple of non-empty strings"):
                _validate(_plugin(_form_spec(fields=fields)))

    def test_options_on_non_select_rejected(self):
        fields = (CredentialField(key="port", label="P", type="text",
                                  options=("993",)),)
        with pytest.raises(ValueError, match="not a select"):
            _validate(_plugin(_form_spec(fields=fields)))

    def test_unknown_field_type_rejected(self):
        fields = (CredentialField(key="port", label="P", type="number"),)
        with pytest.raises(ValueError, match="unknown type"):
            _validate(_plugin(_form_spec(fields=fields)))

    def test_duplicate_field_keys_rejected(self):
        fields = (
            CredentialField(key="port", label="P", type="text"),
            CredentialField(key="port", label="P2", type="text"),
        )
        with pytest.raises(ValueError, match="duplicate"):
            _validate(_plugin(_form_spec(fields=fields)))

    def test_hooks_must_be_callable(self):
        for hook in ("verify", "field_options", "row_warning", "account_from"):
            with pytest.raises(ValueError, match=f"{hook} is not callable"):
                _validate(_plugin(_form_spec(**{hook: "nope"})))


class TestMultiConnectionValidation:
    def test_oauth_and_api_key_need_account_from(self):
        with pytest.raises(ValueError, match="account_from"):
            _validate(_plugin(UserConnectionSpec(
                kind="api_key", connected=lambda row: True,
                multi_connection=True,
            )))
        _validate(_plugin(UserConnectionSpec(
            kind="api_key", connected=lambda row: True,
            multi_connection=True, account_from=lambda row: "a",
        )))

        from types import SimpleNamespace
        router = SimpleNamespace(routes=[SimpleNamespace(path="/auth/tmc")])
        with pytest.raises(ValueError, match="account_from"):
            _validate(_plugin(UserConnectionSpec(
                kind="oauth", connected=lambda row: True,
                oauth_router=router, multi_connection=True,
            )))

    def test_form_multi_connection_needs_no_account_from(self):
        _validate(_plugin(_form_spec(account_from=None)))

    @pytest.mark.parametrize("limit", [0, 51, -1, True, 2.5, "10"])
    def test_connection_limit_bounds(self, limit):
        with pytest.raises(ValueError, match="connection_limit"):
            _validate(_plugin(_form_spec(connection_limit=limit)))

    @pytest.mark.parametrize("limit", [1, 10, 50])
    def test_connection_limit_ok(self, limit):
        _validate(_plugin(_form_spec(connection_limit=limit)))

    def test_tool_without_connection_param_rejected(self):
        with pytest.raises(ValueError, match="'connection' parameter"):
            _validate(_plugin(_form_spec(), tools=(
                _tool("tmc_read"),
                _tool("tmc_list", with_connection=False),
            )))

    def test_connection_param_must_be_string(self):
        tool = PluginTool(
            spec={
                "name": "tmc_read", "description": "d",
                "parameters": {"type": "object", "properties": {
                    "connection": {"type": "integer"},
                }},
            },
            handler=_tool_handler,
        )
        with pytest.raises(ValueError, match="type string"):
            _validate(_plugin(_form_spec(), tools=(tool,)))

    def test_connection_param_must_be_optional(self):
        tool = PluginTool(
            spec={
                "name": "tmc_read", "description": "d",
                "parameters": {
                    "type": "object",
                    "properties": {"connection": {"type": "string"}},
                    "required": ["connection"],
                },
            },
            handler=_tool_handler,
        )
        with pytest.raises(ValueError, match="must be optional"):
            _validate(_plugin(_form_spec(), tools=(tool,)))

    def test_single_connection_tools_need_no_connection_param(self):
        _validate(_plugin(
            UserConnectionSpec(kind="api_key", connected=lambda row: True),
            tools=(_tool("tmc_list", with_connection=False),),
        ))

    def test_valid_plugin_registers_and_unregisters(self):
        from chat.gemini_api.tool_dispatch import TOOL_CALL_HANDLERS
        from chat.llm.tool_schemas import TOOL_CALL_REGISTRY

        plugin = _plugin(_form_spec(), tools=(_tool("tmc_read"),))
        _validate(plugin)
        plugins_mod.register_plugin(plugin)
        plugins_mod._LOADED.append(plugin)
        try:
            assert "tmc_read" in TOOL_CALL_REGISTRY
            assert "tmc_read" in TOOL_CALL_HANDLERS
            assert plugins_mod.get_user_connection_spec("tmc") is plugin.user_connection
            assert is_multi_connection("tmc")
        finally:
            plugins_mod.unregister_plugin(plugin)
        assert "tmc_read" not in TOOL_CALL_REGISTRY
        assert "tmc_read" not in TOOL_CALL_HANDLERS
        assert plugins_mod.get_user_connection_spec("tmc") is None
        assert not is_multi_connection("tmc")


# ---------------------------------------------------------------------------
# Admin card select fields
# ---------------------------------------------------------------------------


class TestAdminSelectFields:
    def _spec(self, **field_kwargs):
        from config.service_specs import ServiceSpec
        return ServiceSpec(
            service="tsel", label="T",
            fields=(CredentialField(key="region", label="Region", type="select",
                                    **field_kwargs),),
        )

    def test_admin_card_rejects_select(self):
        # The admin CredentialForm cannot render select yet: only form
        # connections (allow_select) accept it.
        from config.service_specs import _validate_fields
        with pytest.raises(ValueError, match="unknown type"):
            _validate_fields("tsel", self._spec(options=("us", "eu")).fields)

    def test_plugin_credential_schema_select_rejected(self):
        from config.service_specs import register_plugin_credentials
        plugin = QuestPlugin(
            id="tsel", label="T",
            credential_schema=(CredentialField(
                key="region", label="Region", type="select", options=("us",),
            ),),
        )
        with pytest.raises(ValueError, match="unknown type"):
            register_plugin_credentials(plugin)

    def test_select_with_allow_select_requires_options(self):
        from config.service_specs import _validate_fields
        with pytest.raises(ValueError, match="needs options"):
            _validate_fields("tsel", self._spec().fields, allow_select=True)
        _validate_fields(
            "tsel", self._spec(options=("us", "eu")).fields, allow_select=True,
        )

    def test_fields_view_carries_options(self):
        from config.service_specs import fields_view
        view = fields_view(self._spec(options=("us", "eu")))
        assert view[0]["options"] == ["us", "eu"]

    def test_resolve_update_checks_membership(self):
        from config.service_specs import resolve_update
        spec = self._spec(options=("us", "eu"), required=True)
        assert resolve_update(spec, {"region": "eu"}, None) == {"region": "eu"}
        with pytest.raises(ValueError, match="must be one of"):
            resolve_update(spec, {"region": "mars"}, None)
        with pytest.raises(ValueError, match="required"):
            resolve_update(spec, {"region": ""}, None)
