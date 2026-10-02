"""Dataclasses describing a Quest plugin manifest.

A plugin is a directory under ``plugins/`` whose ``plugin.py`` exports a
``get_plugin() -> QuestPlugin`` function. The loader in ``config/plugins.py``
discovers these at startup, validates them, and fans the manifest out into
the existing core registries (system skills, action-request handlers,
tool_call tools, authed_get services, script allowlist).

Plugins deliberately CANNOT mount ``/api/*`` HTTP routes: any mounted
``/api/*`` route would be reachable by the LLM via ``curl_proxy_get`` with
no service gating, no size gate, and no allow-listing. Bespoke upstream
shapes (GraphQL proxies, non-HTTP protocols, response shaping) belong in
``tools`` handlers -- arbitrary Python behind the gated tool registry --
with sandbox-script access opted in via ``script_tool_allowlist``. The one
routing exception is the oauth-kind user connection's ``oauth_router``:
browser-facing, session-cookie-authed routes confined to the plugin's
``/auth/<id>`` namespace (enforced at load time), matching the core OAuth
connector flows.

This module is deliberately stdlib-only so manifests can be imported and
validated without FastAPI (or any other app dependency) on the path. Fields
that hold framework objects (``oauth_router``) are typed ``Any``.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional


@dataclass(frozen=True)
class CredentialField:
    """One admin-configurable server credential field, rendered generically.

    ``secret`` fields are masked on GET (only ``<key>_set: bool`` is
    returned) and an empty PUT value means "keep the stored secret".
    ``required`` marks a field that must always be non-empty on save;
    ``required_if`` names a sibling ``bool`` field key and, when set, takes
    precedence (required iff that toggle is on). ``visible_if`` names a
    sibling ``bool`` field key that must be on for the field to render;
    ``None`` means always visible.
    """

    key: str
    label: str
    type: Literal["text", "secret", "bool", "textarea"]
    placeholder: str = ""
    required: bool = False
    required_if: Optional[str] = None
    visible_if: Optional[str] = None


@dataclass(frozen=True)
class HelpLink:
    """One link shown in a connector's key-entry step (api_key kind).

    Points the user at the upstream page where the credential is created,
    e.g. ``HelpLink("Generate an API token", "https://.../settings/keys")``.
    Must be an ``https://`` URL; enforced at load.
    """

    label: str
    url: str


@dataclass(frozen=True)
class UserConnectionSpec:
    """Per-user connection surface for a plugin (Data Connections row).

    ``connected`` receives the user's stored credential row (a dict) and
    returns whether the user counts as connected. The ``api_key`` kind gets
    generic key-entry routes (the key lands in the row's ``secret``); the
    ``oauth`` kind supplies an ``oauth_router`` whose routes all live under
    ``/auth/<plugin id>`` (underscores in the id written as hyphens, e.g.
    ``/auth/google-admin``; enforced at load), mounted by quest.py after
    plugin load -- the router's callback stores token JSON in the row's
    ``oauth_blob`` via db/user_service_credential_store.upsert_credential.
    Both kinds get a generically rendered /connectors row (oauth rows use
    the ``/auth/<id>?popup=1`` connect-URL convention and the shared popup
    postMessage helpers in auth/popup_helpers.py).
    """

    kind: Literal["api_key", "oauth"]
    connected: Callable[[dict], bool]
    # api_key kind:
    validate_key: Optional[Callable[[str], Optional[str]]] = None  # error msg or None
    key_hint: bool = True  # show last-4 in /connectors
    # Optional key-entry placeholder for the Data Connections row (e.g.
    # "Paste API key (64-char hex)"); None -> "Paste <label> API key".
    key_placeholder: Optional[str] = None
    # Optional guidance rendered above the key form in the Data Connections
    # key-entry step: one or two plain-text sentences (which credential to
    # create, which role can create it) plus links to the upstream pages
    # where it is minted. Both land on the /connectors row as ``key_help``
    # and ``key_help_links``; None / () render nothing.
    key_help: Optional[str] = None
    key_help_links: tuple[HelpLink, ...] = ()
    # oauth kind:
    oauth_router: Any = None  # fastapi.APIRouter; every route under /auth/<id>
    scopes: tuple[str, ...] = ()  # OAuth scopes the flow requests (informational)
    # Optional: whether the stored row's grant is stale (e.g. granted
    # scopes no longer cover the plugin's needs). True -> the Data
    # Connections row shows the "Update Available" re-authorize badge.
    # Called only when a stored row exists; exceptions read as False.
    needs_reauth: Optional[Callable[[dict], bool]] = None


@dataclass(frozen=True)
class PluginTool:
    """A dynamic (tool_call-routed) tool contributed by a plugin.

    ``spec`` is a provider-agnostic ToolSpec dict (see chat/llm/base.py):
    ``{"name": ..., "description": ..., "parameters": {...}}``. The name must
    be prefixed with ``<plugin id>_``. ``handler`` is an async callable
    ``(ctx, args) -> str | (str, extra_parts)`` where ``ctx`` is the
    dispatch ToolContext (chat/gemini_api/tool_dispatch.py) and ``args`` is
    the inner arguments dict with ``intent_message`` already stripped.

    ``requires_service`` optionally names a ``connected_services`` key that
    must be truthy for the tool to be enumerated in system prompts. The tool
    stays callable either way (parity with core tools); the gate only trims
    the advertisement. Defaults to ``None`` (always advertised).

    ``mutating`` marks a tool that changes state outside the conversation's
    workspace without an approval card (a self-DM / self-SMS send, a mail
    draft, an archive). Such tools are refused in one-shot inference API
    runs, which must not change anything (the loader stamps
    ``spec["mutating"]`` so ``chat.llm.tool_schemas.mutating_tool_call_tools()``
    sees it). Reads and workspace-only writes leave it ``False``.
    """

    spec: dict
    handler: Callable
    requires_service: Optional[str] = None
    mutating: bool = False


@dataclass(frozen=True)
class QuestPlugin:
    """A self-contained upstream-API integration, discovered at startup.

    Only ``id`` and ``label`` are required; every extension surface defaults
    to empty. Plugin-contributed action-request type names and tool names
    must be prefixed ``<id>_`` and system skill ids ``system:<id>``; the
    loader enforces this at load time.

    Public-project conversations always block plugin tools and services --
    there is deliberately no field to opt out of that.
    """

    id: str  # e.g. "acme_tracker" -- the service key everywhere
    label: str  # e.g. "Acme Tracker"

    # -- server-level (admin) configuration -> generic Settings card
    credential_schema: tuple[CredentialField, ...] = ()
    is_configured: Optional[Callable[[dict], bool]] = None
    # Optional post-resolution normalization/validation of the admin form
    # values (the ServiceSpec ``validate`` hook): ``(flat values) -> flat
    # values``, raising ValueError to reject the save.
    credential_validate: Optional[Callable[[dict], dict]] = None

    # -- per-user connection -> generic Data Connections row
    user_connection: Optional[UserConnectionSpec] = None

    # -- upstream access
    # authed_get service-registry entries: {"key": "<hostname[/prefix]>",
    # "entry": {...}} pairs matching _SERVICE_REGISTRY's value shape.
    # Anything a pass-through proxy can't express (GraphQL proxies,
    # runtime-configured hosts, non-HTTP protocols) goes in ``tools``.
    services: tuple[dict, ...] = ()

    # -- agent surface
    system_skills: tuple = ()  # chat.system_skills.SystemSkill instances
    action_request_handlers: tuple = ()  # ActionRequestHandler instances
    tools: tuple[PluginTool, ...] = ()

    # -- policy (safe defaults, explicit opt-in)
    # Names from ``tools`` additionally invocable by sandbox scripts via
    # POST /api/tool-call. Must be a subset of this plugin's tool names.
    script_tool_allowlist: frozenset = field(default_factory=frozenset)

    # Action-request type names exempt from the ``<id>_`` prefix rule.
    # Exists ONLY for core integrations migrated into plugins whose type
    # names predate the packaging and are persisted in action_requests
    # rows (e.g. the Twitter/X plugin's ``send_twitter_dm``).
    # Every name listed must match a declared handler's type_name; the
    # duplicate-registration checks still apply. New plugins must use
    # prefixed names -- unprefixed names risk colliding with future core
    # types.
    unprefixed_action_types: frozenset = field(default_factory=frozenset)

    # Tool names exempt from the ``<id>_`` prefix rule. The same
    # core-migration escape hatch as ``unprefixed_action_types``, for tool
    # names that predate the packaging and are baked into persisted
    # transcripts, sandbox scripts calling POST /api/tool-call, and skill
    # prose (e.g. the Slack plugin's ``list_slack_teams`` /
    # ``send_slack_dm_to_self``). Every name listed must match a declared
    # tool; the duplicate-registration checks still apply. New plugins
    # must use prefixed names.
    unprefixed_tools: frozenset = field(default_factory=frozenset)

    # -- startup hook
    # Optional zero-arg callable run once by load_plugins() after the
    # plugin is registered, for one-time startup work the plugin owns
    # (e.g. migrating a legacy credential-store location into the
    # plugin's store file). It runs only on server startup -- test
    # fixtures that register a manifest directly do not invoke it -- and
    # an exception is logged without unloading the plugin.
    post_load: Optional[Callable[[], None]] = None

    # -- shutdown hook
    # Optional zero-arg callable run once by shutdown_plugins() from the
    # app lifespan's shutdown phase, for plugin-owned teardown (e.g.
    # closing long-lived upstream client connections). May be a plain
    # function or a coroutine function (the returned awaitable is
    # awaited). Hooks run in reverse load order; an exception is logged
    # and the remaining hooks still run. Test fixtures that register a
    # manifest directly do not invoke it.
    on_shutdown: Optional[Callable[[], Any]] = None
