"""Schema-driven service credential specs (core services + plugins).

Every server-level credential service -- core (google_oauth, ramp, coingecko)
and plugin-contributed alike -- is described by a :class:`ServiceSpec`:
a label, a tuple of :class:`~config.plugin_types.CredentialField` rows,
and an ``is_configured`` predicate. The admin Settings UI renders one
generic card per spec and the single generic PUT endpoint in
``chat/routes/admin.py`` validates saves against the schema, so adding a
service (or dropping a plugin into ``plugins/``) requires no per-service
endpoint or frontend change.

Specs may carry three optional hooks for shapes the flat schema cannot
express directly:

- ``flatten(stored) -> flat``: project the stored config into flat form
  values keyed by field key (secrets in cleartext -- only used server-side
  for empty-means-keep resolution and ``<key>_set`` masking). Default:
  the stored dict itself.
- ``unflatten(flat, existing) -> stored``: map resolved flat values back
  to the stored shape. Default: merge over ``existing`` (unknown stored
  keys survive a save; empty optional secrets stay absent).
- ``validate(flat) -> flat``: post-resolution normalization/validation of
  the flat values; raises ``ValueError`` to reject the save.

Like the rest of ``config/``, this module is stdlib-only.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from config.plugin_types import CredentialField, QuestPlugin


@dataclass(frozen=True)
class ServiceSpec:
    """One credential service as rendered/validated by the generic surfaces."""

    service: str
    label: str
    fields: tuple[CredentialField, ...]
    # Whether a non-None stored config counts as configured (badge + any
    # availability gating built on it). Called with the effective config dict.
    is_configured: Callable[[dict], bool] = lambda config: True
    validate: Optional[Callable[[dict], dict]] = None
    flatten: Optional[Callable[[dict], dict]] = None
    unflatten: Optional[Callable[[dict, dict], dict]] = None


FIELD_TYPES = ("text", "secret", "bool", "textarea", "select")


def _validate_fields(
    service: str,
    fields: tuple[CredentialField, ...],
    *,
    allow_select: bool = False,
    runtime_option_keys: frozenset = frozenset(),
) -> None:
    """Reject schemas the generic form machinery cannot render safely.

    ``select`` is accepted only with ``allow_select`` (per-user ``form``
    connection fields; the admin card cannot render it yet). Select fields
    need static ``options`` unless their key is in ``runtime_option_keys``
    (a ``form`` connection whose ``field_options()`` hook supplies the
    choices at request time).
    """
    keys = [f.key for f in fields]
    if len(keys) != len(set(keys)):
        raise ValueError(f"Service {service!r} declares duplicate field keys")
    bool_keys = {f.key for f in fields if f.type == "bool"}
    for f in fields:
        if f.type not in FIELD_TYPES or (f.type == "select" and not allow_select):
            raise ValueError(
                f"Service {service!r} field {f.key!r} has unknown type {f.type!r}"
            )
        if not isinstance(f.options, tuple) or not all(
            isinstance(o, str) and o for o in f.options
        ):
            raise ValueError(
                f"Service {service!r} field {f.key!r} options must be a tuple "
                "of non-empty strings"
            )
        if f.type == "select":
            if not f.options and f.key not in runtime_option_keys:
                raise ValueError(
                    f"Service {service!r} select field {f.key!r} needs options"
                )
        elif f.options:
            raise ValueError(
                f"Service {service!r} field {f.key!r} sets options but is "
                "not a select field"
            )
        for ref_attr in ("required_if", "visible_if"):
            ref = getattr(f, ref_attr)
            if ref is not None and ref not in bool_keys:
                raise ValueError(
                    f"Service {service!r} field {f.key!r} {ref_attr}={ref!r} "
                    "must name a bool field"
                )


# ---------------------------------------------------------------------------
# Core service specs
# ---------------------------------------------------------------------------

# Standard Google OAuth endpoints, filled into saved configs when absent so
# the stored config matches the client-secret JSON that Google Cloud Console
# would have produced.
_GOOGLE_OAUTH_WEB_DEFAULTS = {
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
}


def _google_oauth_web(config: dict) -> dict:
    return config.get("web", config.get("installed", {})) or {}


def _google_oauth_flatten(config: dict) -> dict:
    web = _google_oauth_web(config)
    return {
        "client_id": web.get("client_id", ""),
        "client_secret": web.get("client_secret", ""),
        "project_id": web.get("project_id", ""),
        "redirect_uris": "\n".join(web.get("redirect_uris", []) or []),
    }


def _google_oauth_unflatten(values: dict, existing: dict) -> dict:
    web = dict(_google_oauth_web(existing or {}))
    for key, value in _GOOGLE_OAUTH_WEB_DEFAULTS.items():
        web.setdefault(key, value)
    web["client_id"] = values["client_id"]
    web["client_secret"] = values["client_secret"]
    web["redirect_uris"] = [
        u.strip() for u in values["redirect_uris"].split("\n") if u.strip()
    ]
    if values["project_id"]:
        web["project_id"] = values["project_id"]
    else:
        web.pop("project_id", None)
    return {"web": web}


_CORE_SPECS: dict[str, ServiceSpec] = {}


def _register_core(spec: ServiceSpec) -> None:
    _validate_fields(spec.service, spec.fields)
    _CORE_SPECS[spec.service] = spec


_register_core(ServiceSpec(
    service="google_oauth",
    label="Google (login + Google services)",
    fields=(
        CredentialField(
            key="client_id", label="Client ID", type="text",
            placeholder="xxxx.apps.googleusercontent.com", required=True,
        ),
        CredentialField(
            key="client_secret", label="Client secret", type="secret",
            placeholder="Client secret from Google Cloud Console", required=True,
        ),
        CredentialField(
            key="project_id", label="GCP project ID", type="text",
            placeholder="my-gcp-project",
        ),
        CredentialField(
            key="redirect_uris", label="Redirect URIs", type="textarea",
            placeholder="One per line, e.g. https://example.com/auth/callback",
        ),
    ),
    flatten=_google_oauth_flatten,
    unflatten=_google_oauth_unflatten,
))

def _flat_oauth_client_fields(id_placeholder: str, secret_placeholder: str):
    return (
        CredentialField(
            key="client_id", label="Client ID", type="text",
            placeholder=id_placeholder, required=True,
        ),
        CredentialField(
            key="client_secret", label="Client secret", type="secret",
            placeholder=secret_placeholder, required=True,
        ),
    )


# The Slack, GitHub, and Twitter/X specs are plugin-registered
# (plugins/slack, plugins/github, plugins/twitter) via
# register_plugin_credentials(); the store keys stay "slack", "github",
# and "twitter".

_register_core(ServiceSpec(
    service="ramp",
    label="Ramp",
    fields=_flat_oauth_client_fields(
        "Ramp OAuth client ID", "Ramp OAuth client secret"),
))

# CoinGecko Pro: a single server-level API key injected by the authed_get
# service registry (chat/gemini_api/authed_get.py). Legacy location is the
# ``coingecko`` section of server_credentials.json, migrated like the
# other core sections.
_register_core(ServiceSpec(
    service="coingecko",
    label="CoinGecko Pro",
    fields=(
        CredentialField(
            key="api_key", label="API key", type="secret",
            placeholder="CoinGecko Pro API key", required=True,
        ),
    ),
    is_configured=lambda config: bool(config.get("api_key")),
))



def _validate_smtp(values: dict) -> dict:
    port = str(values.get("port") or "").strip()
    if port and (not port.isdigit() or not 0 < int(port) < 65536):
        raise ValueError("Port must be a number between 1 and 65535.")
    from_address = str(values.get("from_address") or "").strip()
    if "@" not in from_address:
        raise ValueError("From address must be an email address.")
    return values


# Outgoing email (SMTP) for the email/password sign-in flow: with it,
# users can reset their own password and allowed-domain users can create
# their own account from the sign-in screen (the emailed link proves they
# own the address). Without it, only admin-issued set-password links work.
# See auth/mailer.py.
_register_core(ServiceSpec(
    service="smtp",
    label="Outgoing email (SMTP)",
    fields=(
        CredentialField(
            key="host", label="SMTP host", type="text",
            placeholder="smtp.example.com", required=True,
        ),
        CredentialField(
            key="port", label="Port", type="text",
            placeholder="587 (STARTTLS) or 465 (TLS)",
        ),
        CredentialField(
            key="implicit_tls", label="Use implicit TLS (port 465)", type="bool",
        ),
        CredentialField(
            key="username", label="Username", type="text",
            placeholder="Leave empty for unauthenticated relays",
        ),
        CredentialField(
            key="password", label="Password", type="secret",
            placeholder="SMTP password or app password",
        ),
        CredentialField(
            key="from_address", label="From address", type="text",
            placeholder="Quest <quest@example.com>", required=True,
        ),
    ),
    is_configured=lambda config: bool(config.get("host") and config.get("from_address")),
    validate=_validate_smtp,
))


# (The Telegram spec is plugin-registered from plugins/telegram.)

# ---------------------------------------------------------------------------
# Plugin specs
# ---------------------------------------------------------------------------

_PLUGIN_SPECS: dict[str, ServiceSpec] = {}


def register_plugin_credentials(plugin: QuestPlugin) -> None:
    """Register a plugin's admin credential surface (card + store roster).

    Called by the plugin loader for plugins that declare a
    ``credential_schema``. Adds the plugin id to the credential store's
    known services so ``data/service_credentials/<id>.json`` becomes a
    valid store file. Raises ValueError on schema problems or collisions
    (the loader logs and skips the plugin).
    """
    spec = ServiceSpec(
        service=plugin.id,
        label=plugin.label,
        fields=tuple(plugin.credential_schema),
        is_configured=plugin.is_configured or (lambda config: True),
        validate=plugin.credential_validate,
    )
    _validate_fields(spec.service, spec.fields)
    if spec.service in _CORE_SPECS or spec.service in _PLUGIN_SPECS:
        raise ValueError(f"Credential service already registered: {spec.service!r}")
    from config.service_credentials import register_plugin_service
    register_plugin_service(spec.service)
    _PLUGIN_SPECS[spec.service] = spec


def get_service_spec(service: str) -> ServiceSpec | None:
    return _CORE_SPECS.get(service) or _PLUGIN_SPECS.get(service)


def all_service_specs() -> tuple[ServiceSpec, ...]:
    """Core specs in roster order, then plugin specs in load order."""
    return tuple(_CORE_SPECS.values()) + tuple(_PLUGIN_SPECS.values())


# ---------------------------------------------------------------------------
# Generic form projection and update resolution
# ---------------------------------------------------------------------------

def fields_view(spec: ServiceSpec) -> list[dict]:
    """The field schema as sent to the frontend for generic rendering."""
    return [
        {
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "placeholder": f.placeholder,
            "help": f.help,
            "required": f.required,
            "required_if": f.required_if,
            "visible_if": f.visible_if,
            **({"options": list(f.options)} if f.type == "select" else {}),
        }
        for f in spec.fields
    ]


def _flat(spec: ServiceSpec, config: dict) -> dict:
    return spec.flatten(config) if spec.flatten else dict(config)


def form_view(spec: ServiceSpec, config: dict | None) -> dict:
    """Project a stored config into the masked form values.

    Secrets are never returned; the form only learns ``<key>_set``.
    """
    flat = _flat(spec, config or {})
    view: dict = {}
    for f in spec.fields:
        value = flat.get(f.key)
        if f.type == "secret":
            view[f.key + "_set"] = bool(value)
        elif f.type == "bool":
            view[f.key] = bool(value)
        else:
            view[f.key] = value if isinstance(value, str) else ""
    return view


def resolve_update(spec: ServiceSpec, values: dict, existing: dict | None) -> dict:
    """Validate a generic PUT body and produce the config dict to store.

    Applies the schema rules: unknown keys rejected, strings stripped,
    empty secret = keep the stored one, ``required``/``required_if``
    enforcement, then the spec's ``validate`` hook and stored-shape
    mapping. Raises ValueError with a user-facing message on rejection.
    """
    if not isinstance(values, dict):
        raise ValueError("Expected a JSON object of credential fields.")
    field_map = {f.key: f for f in spec.fields}
    unknown = sorted(set(values) - set(field_map))
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(unknown)}")

    existing = existing or {}
    flat_existing = _flat(spec, existing)

    resolved: dict = {}
    for f in spec.fields:
        raw = values.get(f.key)
        if f.type == "bool":
            resolved[f.key] = bool(raw)
            continue
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise ValueError(f"{f.label} must be a string.")
        value = raw.strip()
        if f.type == "secret" and not value:
            # Empty means "keep the stored secret" (round-trips the masked
            # read view without ever sending the secret back out).
            stored = flat_existing.get(f.key)
            value = stored if isinstance(stored, str) else ""
        if f.type == "select" and value and value not in f.options:
            raise ValueError(f"{f.label} must be one of: {', '.join(f.options)}.")
        resolved[f.key] = value

    for f in spec.fields:
        if f.type == "bool":
            continue
        required = bool(resolved.get(f.required_if)) if f.required_if else f.required
        if required and not resolved.get(f.key):
            if f.type == "secret":
                raise ValueError(
                    f"{f.label} is required (no stored secret to keep).")
            raise ValueError(f"{f.label} is required.")

    if spec.validate:
        resolved = spec.validate(resolved)

    if spec.unflatten:
        return spec.unflatten(resolved, existing)

    # Default stored shape: merge over the existing config so unrecognized
    # keys survive a save; optional secrets with no value stay absent.
    stored = dict(existing)
    for f in spec.fields:
        value = resolved.get(f.key)
        if f.type == "secret" and not value:
            continue
        stored[f.key] = value
    return stored
