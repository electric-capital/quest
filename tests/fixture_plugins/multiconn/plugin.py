"""Test-only multi-connection plugin (issue #84 phase 1).

NOT discovered in production (it lives under tests/, outside every plugin
root). Test suites register it with ``tests.plugin_support.plugin_fixture``
(see ``multiconn_plugin`` in tests/conftest.py). It is the smallest plugin
that opts into multi-account connections: a ``form``-kind
``UserConnectionSpec`` with ``multi_connection=True``, a select field fed by
``field_options``, a stub ``verify`` and one tool that declares the
``connection`` argument and resolves it through
``config.user_connections.resolve_connection``.

Stub verify behaviour (deterministic, no network):
- password ``"wrong"`` -> ``VerifyResult(error="Login failed: bad password")``
- password ``"slow"`` -> sleeps longer than any test timeout (tests patch the
  route's verify timeout down to exercise the timeout path)
- otherwise -> account = the username lower-cased when it contains ``@``,
  else ``<username>@<host>``; ``extras`` records the host.

It also contributes an authed_get service entry on ``api.multiconn.test``
(``accepts_connection=True``; the loader resolves the ``connection``
argument and returns the row's account as the bearer credential) and a
``system:multiconn`` skill whose content builder takes the ``connections``
keyword (it lists the account addresses it receives).
"""

import asyncio
import json

from chat.system_skills.catalog import SystemSkill
from config.plugin_types import (
    CredentialField,
    PluginTool,
    QuestPlugin,
    UserConnectionSpec,
    VerifyResult,
)
from config.user_connections import (
    ConnectionNotFound,
    resolve_connection,
)

PLUGIN_ID = "multiconn"
HOSTS = ["mail.example.com", "mail.example.org"]


def _field_options() -> dict[str, list[str]]:
    return {"host": list(HOSTS)}


async def _verify(values: dict) -> VerifyResult:
    password = values.get("password") or ""
    if password == "wrong":
        return VerifyResult(error="Login failed: bad password")
    if password == "slow":
        await asyncio.sleep(3600)
    username = (values.get("username") or "").strip()
    account = username.lower() if "@" in username else f"{username}@{values.get('host')}".lower()
    return VerifyResult(account=account, extras={"verified_host": values.get("host")})


def _connected(row: dict) -> bool:
    blob = row.get("oauth_blob") or {}
    return bool(blob.get("values"))


def _row_warning(row: dict) -> str | None:
    host = ((row.get("oauth_blob") or {}).get("values") or {}).get("host")
    if host and host not in HOSTS:
        return "Server no longer allowed"
    return None


async def _handle_whoami(ctx, args: dict) -> str:
    try:
        row = resolve_connection(ctx.user, PLUGIN_ID, args.get("connection"))
    except ConnectionNotFound as exc:
        return json.dumps({
            "error": "connection_not_found",
            "service": PLUGIN_ID,
            "requested": exc.requested,
            "available": exc.available,
        })
    if row is None:
        return json.dumps({"error": "not_connected", "service": PLUGIN_ID})
    return json.dumps({"account": row.get("account")})


_WHOAMI_TOOL = PluginTool(
    spec={
        "name": "multiconn_whoami",
        "description": "Return the account of the connection a call resolves to (test fixture).",
        "parameters": {
            "type": "object",
            "properties": {
                "connection": {
                    "type": "string",
                    "description": "Account address of the connection to use; omit for the default.",
                },
                "intent_message": {
                    "type": "string",
                    "description": "A brief, user-friendly summary of your intent (max 50 characters).",
                },
            },
            "required": [],
        },
    },
    handler=_handle_whoami,
    requires_service=PLUGIN_ID,
)


SERVICE_HOST = "api.multiconn.test"


async def _load_service_credentials(user: dict, connection: str | None = None):
    """authed_get loader: the resolved row's account is the "token".

    ConnectionNotFound propagates; authed_get turns it into the structured
    ``connection_not_found`` error.
    """
    row = resolve_connection(user, PLUGIN_ID, connection)
    if row is None:
        return None
    return row.get("account")


def _inject_service_auth(token, headers: dict[str, str]) -> None:
    headers["Authorization"] = f"Bearer {token}"


_SERVICE = {
    "key": SERVICE_HOST,
    "entry": {
        "name": "Multi Connection Fixture API",
        "load_credentials": _load_service_credentials,
        "inject_auth": _inject_service_auth,
        "requires_user": True,
        "accepts_connection": True,
        "allowed_endpoints": [r"^/v1/[a-z]+$"],
        "missing_credentials_error": {
            "error": "multiconn_not_connected",
            "message": "Connect Multi Connection Fixture in Settings.",
        },
    },
}


def _skill_content(base_url: str, api_key: str, connections=None) -> str:
    accounts = ", ".join(c["label"] for c in (connections or [])) or "none"
    return f"## Multi Connection Fixture\n\nYour connections: {accounts}\n"


_SKILL = SystemSkill(
    id="system:multiconn",
    name="Multi Connection Fixture",
    description="Test fixture skill for multi-connection services.",
    when_to_load="Never (test fixture).",
    content_builder=_skill_content,
    requires=PLUGIN_ID,
)


def get_plugin() -> QuestPlugin:
    return QuestPlugin(
        id=PLUGIN_ID,
        label="Multi Connection Fixture",
        user_connection=UserConnectionSpec(
            kind="form",
            connected=_connected,
            multi_connection=True,
            connection_limit=3,
            fields=(
                CredentialField(key="host", label="Server", type="select", required=True),
                CredentialField(key="port", label="Port", type="text", placeholder="993"),
                CredentialField(key="username", label="Username", type="text", required=True),
                CredentialField(key="password", label="Password", type="secret", required=True),
            ),
            field_options=_field_options,
            verify=_verify,
            row_warning=_row_warning,
        ),
        tools=(_WHOAMI_TOOL,),
        services=(_SERVICE,),
        system_skills=(_SKILL,),
    )
