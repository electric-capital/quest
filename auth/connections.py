"""Generic per-connection routes for multi-connection plugin services.

Plugins whose ``UserConnectionSpec`` sets ``multi_connection=True`` let a
user hold several connections (``user_service_credentials`` rows, one per
account) to the same service. These routes manage the individual rows for
the Settings UI (issue #84, spec section 8.2):

- ``POST /auth/connections/{service}``: add a connection. ``form`` kind
  takes ``{"values": {...}, "description"?}``, validates the values against
  the spec's ``fields`` and runs the plugin's ``verify`` hook, which yields
  the account; ``api_key`` kind takes ``{"api_key", "description"?}`` and
  derives the account with ``account_from``. ``oauth`` connections are
  created by the plugin's own callback (400 ``use_connect_url``).
- ``PUT /auth/connections/{service}/{id}``: change the description, make
  the row the default, or (form kind) re-enter values, which re-runs
  ``verify`` and must yield the same account.
- ``DELETE /auth/connections/{service}/{id}``: delete one row; the store
  promotes a new default when the default was deleted.

Row ids are an HTTP-API detail for the Settings UI; the model only ever
sees account addresses. No response ever carries a secret or a form value:
:func:`connection_view` is the only row serialisation, shared with
``GET /connectors``. Every successful write invalidates the user's cached
chat sessions so the next message rebuilds the system prompt.

Verify attempts are rate-limited per user (:data:`VERIFY_RATE_LIMIT`
attempts per rolling :data:`VERIFY_RATE_WINDOW_SECONDS`, in memory) so the
form cannot be used to brute-force an upstream login through Quest.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from auth.session import get_current_user_cookie_or_apikey

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/connections")

# Seconds a plugin's verify hook may take before the attempt fails.
VERIFY_TIMEOUT_SECONDS = 20

# Per-user verify rate limit: at most this many attempts per rolling window.
VERIFY_RATE_LIMIT = 10
VERIFY_RATE_WINDOW_SECONDS = 60.0

_verify_attempts: dict[int, deque] = {}

_TEXT_TYPES = ("text", "secret", "select", "textarea")


def reset_rate_limits() -> None:
    """Forget every recorded verify attempt (tests)."""
    _verify_attempts.clear()


def _error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"error": code, "message": message, **extra},
    )


def _check_rate_limit(user_id: int) -> None:
    """Record one verify attempt for ``user_id``; 429 beyond the limit."""
    now = time.monotonic()
    attempts = _verify_attempts.setdefault(user_id, deque())
    while attempts and now - attempts[0] >= VERIFY_RATE_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= VERIFY_RATE_LIMIT:
        retry_after = max(1, int(VERIFY_RATE_WINDOW_SECONDS - (now - attempts[0])) + 1)
        raise _error(
            429, "rate_limited",
            "Too many connection attempts. Wait a minute and try again.",
            retry_after=retry_after,
        )
    attempts.append(now)


# ---------------------------------------------------------------------------
# Views shared with GET /connectors
# ---------------------------------------------------------------------------

def resolve_field_options(spec) -> dict[str, list[str]]:
    """The select options per field key: runtime ``field_options()`` when
    the spec has the hook (a raising hook yields no options and is logged),
    else each select field's static ``options``."""
    runtime: Optional[dict] = None
    if spec.field_options is not None:
        try:
            runtime = spec.field_options() or {}
        except Exception:
            logger.exception("field_options hook raised; no select options")
            runtime = {}
    options: dict[str, list[str]] = {}
    for field in spec.fields:
        if field.type != "select":
            continue
        if runtime is not None and field.key in runtime:
            options[field.key] = [str(o) for o in (runtime.get(field.key) or [])]
        else:
            options[field.key] = list(field.options)
    return options


def form_fields_view(spec) -> list[dict]:
    """The form schema the Settings UI renders (no values)."""
    options = resolve_field_options(spec)
    return [
        {
            "key": field.key,
            "label": field.label,
            "type": field.type,
            "required": bool(field.required),
            "placeholder": field.placeholder,
            "options": options.get(field.key, []),
        }
        for field in spec.fields
    ]


def connection_view(spec, row: dict) -> dict:
    """The per-connection object for the UI. Never includes credentials."""
    needs_reauth = False
    if spec.needs_reauth is not None:
        try:
            needs_reauth = bool(spec.needs_reauth(row))
        except Exception:
            logger.exception("needs_reauth hook raised; treating as False")
    warning = None
    if spec.row_warning is not None:
        try:
            result = spec.row_warning(row)
            warning = str(result) if result else None
        except Exception:
            logger.exception("row_warning hook raised; no warning")
    return {
        "id": row.get("id"),
        "account": row.get("account") or "",
        "description": row.get("description"),
        "is_default": bool(row.get("is_default")),
        "needs_reauth": needs_reauth,
        "warning": warning,
        "created_at": row.get("created_at"),
    }


def _row_connected(spec, row: dict) -> bool:
    try:
        return bool(row) and bool(spec.connected(row))
    except Exception:
        logger.exception("connected predicate raised; treating as not connected")
        return False


def user_connection_rows(user: dict, service: str) -> list[dict]:
    """The user's rows for ``service`` from the user dict, default first."""
    from config.user_connections import _service_rows
    return _service_rows(user, service)


def multi_connection_fields(plugin, user: dict) -> dict:
    """The multi-connection additions to a plugin's ``/connectors`` row."""
    spec = plugin.user_connection
    rows = user_connection_rows(user, plugin.id)
    fields = {
        "connected": any(_row_connected(spec, row) for row in rows),
        "multi_connection": True,
        "connection_limit": spec.connection_limit,
        "connections": [connection_view(spec, row) for row in rows],
        "connections_url": f"/auth/connections/{plugin.id}",
    }
    if spec.kind == "form":
        fields["form_fields"] = form_fields_view(spec)
    return fields


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _resolve_multi_plugin(service: str):
    """The loaded multi-connection plugin backing ``service``, or 404."""
    from config.plugins import get_loaded_plugin

    plugin = get_loaded_plugin(service)
    spec = plugin.user_connection if plugin else None
    # form kind is always multi-connection (the loader requires it).
    if spec is None or not (spec.multi_connection or spec.kind == "form"):
        raise _error(
            404, "unknown_service",
            f"No multi-connection service named {service!r}.",
        )
    return plugin


async def _read_body(request: Request, allowed: set[str]) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise _error(400, "invalid_request", "Request body must be a JSON object.")
    if not isinstance(body, dict):
        raise _error(400, "invalid_request", "Request body must be a JSON object.")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise _error(
            400, "invalid_request", f"Unknown field(s): {', '.join(unknown)}.",
        )
    return body


def _normalize_description(raw: Any) -> Optional[str]:
    from db.user_service_credential_store import normalize_description
    try:
        return normalize_description(raw)
    except ValueError as exc:
        raise _error(400, "invalid_description", f"Invalid description: {exc}.")


def validate_form_values(spec, values: Any) -> dict:
    """Validate form values against ``spec.fields``; 400 ``invalid_values``
    with a per-field ``fields`` map on any problem. Returns the values."""
    if not isinstance(values, dict):
        raise _error(400, "invalid_values", "values must be an object.", fields={})
    by_key = {field.key: field for field in spec.fields}
    errors: dict[str, str] = {}
    for key in values:
        if key not in by_key:
            errors[str(key)] = "Unknown field."
    options = resolve_field_options(spec)
    for key, field in by_key.items():
        value = values.get(key)
        required = bool(field.required)
        if field.required_if:
            required = values.get(field.required_if) is True
        if field.type == "bool":
            if value is None:
                if required:
                    errors[key] = "This field is required."
            elif not isinstance(value, bool):
                errors[key] = "Must be true or false."
            continue
        if value is None:
            if required:
                errors[key] = "This field is required."
            continue
        if not isinstance(value, str):
            errors[key] = "Must be a string."
            continue
        if not value.strip():
            if required:
                errors[key] = "This field is required."
            continue
        if field.type == "select" and value not in options.get(key, []):
            errors[key] = "Not one of the allowed choices."
    if errors:
        raise _error(
            400, "invalid_values", "Some fields are invalid.", fields=errors,
        )
    return values


async def _run_verify(plugin, user: dict, values: dict):
    """Rate-limited, time-limited ``verify(values)``; returns
    ``(normalized_account, extras)`` or raises 400 ``verify_failed``."""
    from db.user_service_credential_store import normalize_account

    _check_rate_limit(user["id"])
    spec = plugin.user_connection
    try:
        result = await asyncio.wait_for(
            spec.verify(dict(values)), timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.info("[%s] verify timed out (user=%s)", plugin.id, user.get("email"))
        raise _error(
            400, "verify_failed",
            f"The server did not answer within {VERIFY_TIMEOUT_SECONDS} seconds.",
        )
    except Exception:
        logger.exception("[%s] verify hook raised", plugin.id)
        raise _error(400, "verify_failed", "Could not verify the connection.")
    error = getattr(result, "error", None)
    if error:
        raise _error(400, "verify_failed", str(error))
    try:
        account = normalize_account(getattr(result, "account", None))
    except ValueError:
        logger.warning("[%s] verify returned no usable account", plugin.id)
        raise _error(
            400, "verify_failed", "The connection was verified but no account was returned.",
        )
    extras = getattr(result, "extras", None)
    return account, (dict(extras) if isinstance(extras, dict) else {})


def _invalidate(user: dict) -> None:
    from chat.gemini_api import invalidate_user_sessions
    invalidate_user_sessions(user["id"])


async def _connection_exists_error(user: dict, service: str, account: str):
    from db.user_service_credential_store import get_connection
    existing = await get_connection(user["id"], service, account=account)
    return _error(
        409, "connection_exists",
        f"{account} is already connected.",
        account=account,
        connection_id=existing["id"] if existing else None,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/{service}")
async def create_connection_route(
    service: str,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey),
):
    """Add one connection for the current user (201 ``{connection}``)."""
    from db.user_service_credential_store import (
        ConnectionExists,
        ConnectionLimit,
        create_connection,
        normalize_account,
    )

    plugin = _resolve_multi_plugin(service)
    spec = plugin.user_connection

    if spec.kind == "oauth":
        raise _error(
            400, "use_connect_url",
            "OAuth connections are added through the service's connect URL.",
        )

    if spec.kind == "form":
        body = await _read_body(request, {"values", "description"})
        values = validate_form_values(spec, body.get("values"))
        description = _normalize_description(body.get("description"))
        account, extras = await _run_verify(plugin, user, values)
        secret = None
        oauth_blob = {**extras, "values": dict(values)}
    else:  # api_key
        body = await _read_body(request, {"api_key", "description"})
        api_key = body.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise _error(400, "invalid_request", "api_key is required.")
        api_key = api_key.strip()
        description = _normalize_description(body.get("description"))
        if spec.validate_key is not None:
            try:
                key_error = spec.validate_key(api_key)
            except Exception:
                logger.exception("[%s] validate_key raised; rejecting key", plugin.id)
                key_error = "Key validation failed."
            if key_error:
                raise _error(400, "invalid_key", str(key_error))
        raw_account = None
        if spec.account_from is not None:
            try:
                raw_account = spec.account_from({"service": plugin.id, "secret": api_key})
            except Exception:
                logger.exception("[%s] account_from raised", plugin.id)
        try:
            account = normalize_account(raw_account)
        except ValueError:
            raise _error(
                400, "account_unresolved",
                "Could not determine which account this key belongs to.",
            )
        secret = api_key
        oauth_blob = None

    try:
        row = await create_connection(
            user["id"], plugin.id,
            account=account,
            description=description,
            secret=secret,
            oauth_blob=oauth_blob,
            limit=spec.connection_limit,
        )
    except ConnectionExists:
        raise await _connection_exists_error(user, plugin.id, account)
    except ConnectionLimit as exc:
        raise _error(
            400, "connection_limit",
            f"At most {exc.limit} {plugin.label} connections are allowed.",
            limit=exc.limit,
        )

    logger.info(
        "[%s] connection added (user=%s, id=%s)", plugin.id, user.get("email"), row["id"],
    )
    _invalidate(user)
    return JSONResponse(status_code=201, content={"connection": connection_view(spec, row)})


@router.put("/{service}/{connection_id}")
async def update_connection_route(
    service: str,
    connection_id: int,
    request: Request,
    user: dict = Depends(get_current_user_cookie_or_apikey),
):
    """Change a connection's description / default flag / form values."""
    from db.user_service_credential_store import (
        get_connection,
        set_default_connection,
        update_connection,
    )

    plugin = _resolve_multi_plugin(service)
    spec = plugin.user_connection
    body = await _read_body(request, {"description", "is_default", "values"})

    row = await get_connection(user["id"], plugin.id, row_id=connection_id)
    if row is None:
        raise _error(404, "connection_not_found", "No such connection.")

    # Validate everything before any write, so a failing request changes nothing.
    set_description = "description" in body
    description = _normalize_description(body.get("description")) if set_description else None

    is_default = body.get("is_default")
    if "is_default" in body and not isinstance(is_default, bool):
        raise _error(400, "invalid_request", "is_default must be true or false.")
    if is_default is False:
        raise _error(
            400, "cannot_unset_default",
            "Make another connection the default instead.",
        )

    new_blob = None
    if "values" in body:
        if spec.kind != "form":
            raise _error(400, "invalid_request", "values apply to form connections only.")
        incoming = body.get("values")
        if not isinstance(incoming, dict):
            raise _error(400, "invalid_values", "values must be an object.", fields={})
        field_keys = {field.key for field in spec.fields}
        secret_keys = {field.key for field in spec.fields if field.type == "secret"}
        stored_values = ((row.get("oauth_blob") or {}).get("values") or {})
        merged = {k: v for k, v in stored_values.items() if k in field_keys}
        for key, value in incoming.items():
            if key in secret_keys and (value is None or (isinstance(value, str) and not value.strip())):
                continue  # empty secret = keep the stored one
            merged[key] = value
        values = validate_form_values(spec, merged)
        account, extras = await _run_verify(plugin, user, values)
        if account != (row.get("account") or "").strip().lower():
            raise _error(
                400, "account_mismatch",
                f"These settings belong to {account}, not {row.get('account')}. "
                "Add it as a new connection instead.",
            )
        new_blob = {**extras, "values": dict(values)}

    kwargs = {}
    if set_description:
        kwargs["description"] = description
    if new_blob is not None:
        kwargs["oauth_blob"] = new_blob
    if kwargs:
        row = await update_connection(user["id"], plugin.id, connection_id, **kwargs)
        if row is None:
            raise _error(404, "connection_not_found", "No such connection.")
    if is_default is True:
        row = await set_default_connection(user["id"], plugin.id, connection_id)
        if row is None:
            raise _error(404, "connection_not_found", "No such connection.")

    if kwargs or is_default is True:
        logger.info(
            "[%s] connection updated (user=%s, id=%s)",
            plugin.id, user.get("email"), connection_id,
        )
        _invalidate(user)
    return {"connection": connection_view(spec, row)}


@router.delete("/{service}/{connection_id}")
async def delete_connection_route(
    service: str,
    connection_id: int,
    user: dict = Depends(get_current_user_cookie_or_apikey),
):
    """Delete one connection (204); a new default is promoted when needed."""
    from db.user_service_credential_store import delete_connection

    plugin = _resolve_multi_plugin(service)
    removed = await delete_connection(user["id"], plugin.id, connection_id)
    if not removed:
        raise _error(404, "connection_not_found", "No such connection.")
    logger.info(
        "[%s] connection deleted (user=%s, id=%s)",
        plugin.id, user.get("email"), connection_id,
    )
    _invalidate(user)
    return Response(status_code=204)
