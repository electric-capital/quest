"""Internal route dispatch for tool calls.

Phase 1 of Gemini API migration: resolves tool call URLs against the FastAPI
app's registered routes and invokes the corresponding handler directly,
bypassing HTTP entirely.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import typing
import urllib.parse
from typing import Any, Union, get_args, get_origin

from fastapi.params import Depends as DependsClass
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from chat.gemini_api.constants import get_proxy_port, get_sandbox_port
from chat.route_table import iter_app_routes
from chat.tool_notices import ToolResultWithNotices

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. validate_url
# ---------------------------------------------------------------------------

def validate_url(url: str) -> tuple[str, dict[str, list[str]]]:
    """Parse and validate a tool-call URL.

    Ensures the URL points at localhost:8000/api/* and returns the path
    together with parsed query-string parameters.

    Args:
        url: Full URL string (e.g. ``http://localhost:8000/api/gmail-raw/...``).

    Returns:
        Tuple of ``(path, query_params)`` where *query_params* is a dict
        mapping parameter names to lists of string values.

    Raises:
        ValueError: If the URL does not satisfy the constraints.
    """
    parsed = urllib.parse.urlparse(url)

    # Validate host
    hostname = parsed.hostname or ""
    if hostname not in ("localhost", "127.0.0.1"):
        raise ValueError(
            f"URL host must be localhost or 127.0.0.1, got '{hostname}'"
        )

    # Validate port (allow omitted port, 8000, the configured proxy port,
    # or the sandbox tool API port -- the model sees the latter in the
    # script-sandbox docs and may echo it in curl_proxy URLs)
    allowed_ports = {8000, int(get_proxy_port()), int(get_sandbox_port())}
    if parsed.port is not None and parsed.port not in allowed_ports:
        raise ValueError(
            f"URL port must be one of {allowed_ports} (or omitted), got {parsed.port}"
        )

    # Validate path
    path = parsed.path
    if not path.startswith("/api/"):
        raise ValueError(
            f"URL path must start with /api/, got '{path}'"
        )

    query_params = urllib.parse.parse_qs(parsed.query)
    return path, query_params


# ---------------------------------------------------------------------------
# 2. resolve_route
# ---------------------------------------------------------------------------

def resolve_route(
    app: Any,
    method: str,
    path: str,
) -> tuple[Any, dict[str, Any]]:
    """Resolve a method + path to a route endpoint and path parameters.

    Iterates the flat route table from ``chat.route_table.iter_app_routes``
    and uses Starlette's ``route.matches()`` mechanism, which correctly
    handles path converters such as ``{range:path}``.

    Args:
        app: The FastAPI application instance.
        method: HTTP method (``GET``, ``POST``, etc.).
        path: URL path (e.g. ``/api/sheets-raw/v4/spreadsheets/abc/values/Sheet1!A:Z``).

    Returns:
        Tuple of ``(endpoint_callable, path_params_dict)``.

    Raises:
        ValueError: If no matching route is found.
    """
    method_upper = method.upper()
    scope: dict[str, Any] = {
        "type": "http",
        "method": method_upper,
        "path": path,
    }

    for route in iter_app_routes(app):
        match, child_scope = route.matches(scope)
        if match == Match.FULL:
            # Verify the route supports the requested method
            route_methods = getattr(route, "methods", None)
            if route_methods and method_upper not in route_methods:
                continue
            path_params = child_scope.get("path_params", {})
            return route.endpoint, path_params

    raise ValueError(f"No route matched {method_upper} {path}")


# ---------------------------------------------------------------------------
# 3. build_handler_kwargs  (async: handler bodies are read with await)
# ---------------------------------------------------------------------------

def _is_optional(annotation: Any) -> bool:
    """Return True if *annotation* is ``Optional[X]`` (i.e. ``Union[X, None]``)."""
    origin = get_origin(annotation)
    if origin is Union:
        args = get_args(annotation)
        return type(None) in args
    return False


def _unwrap_optional(annotation: Any) -> Any:
    """Unwrap Optional[X] to X."""
    if _is_optional(annotation):
        args = get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        return non_none[0] if len(non_none) == 1 else annotation
    return annotation


def _is_list_type(annotation: Any) -> bool:
    """Return True if *annotation* is ``list`` or ``List[...]``."""
    if annotation is list:
        return True
    origin = get_origin(annotation)
    return origin is list


def _is_pydantic_model(annotation: Any) -> bool:
    """Return True if *annotation* is a Pydantic BaseModel subclass."""
    try:
        return isinstance(annotation, type) and issubclass(annotation, BaseModel)
    except TypeError:
        return False


def _coerce(value: Any, annotation: Any) -> Any:
    """Best-effort coercion of *value* to *annotation* type."""
    if annotation is inspect.Parameter.empty or annotation is Any:
        return value
    try:
        if annotation is bool:
            # Special-case bool to handle string "true"/"false"
            if isinstance(value, str):
                return value.lower() in ("true", "1", "yes")
            return bool(value)
        if annotation in (int, float, str):
            return annotation(value)
        return value
    except (TypeError, ValueError):
        return value


async def build_handler_kwargs(
    endpoint: Any,
    path_params: dict[str, Any],
    query_params: dict[str, list[str]],
    user: dict[str, Any],
    body_str: str | None,
    *,
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Build the keyword arguments needed to call *endpoint* directly.

    Introspects the handler's signature and resolves every parameter from
    path params, query params, the ``user`` dict, a Pydantic body model,
    a fake ``Request``, or FastAPI ``Depends`` declarations.

    Args:
        endpoint: The route handler callable.
        path_params: Path parameters extracted by :func:`resolve_route`.
        query_params: Parsed query-string parameters (lists of strings).
        user: The authenticated user dict.
        body_str: Raw request body string (for POST requests), or ``None``.
        method: HTTP method for building a fake ``Request``.
        path: URL path for building a fake ``Request``.
        headers: Optional HTTP headers for building a fake ``Request``.

    Returns:
        Dict of keyword arguments ready to be unpacked into the endpoint.

    Raises:
        ValueError: If a required parameter cannot be resolved.
    """
    sig = inspect.signature(endpoint)
    # Use get_type_hints for resolved annotations (handles string annotations)
    try:
        type_hints = typing.get_type_hints(endpoint)
    except Exception:
        type_hints = {}

    kwargs: dict[str, Any] = {}

    for param_name, param in sig.parameters.items():
        annotation = type_hints.get(param_name, param.annotation)
        default = param.default

        # Unwrap FastAPI FieldInfo defaults (Query, Path, Header, etc.)
        # inspect.signature() returns the Query()/Path()/etc. object as the
        # default, but the actual default lives in FieldInfo.default.
        if isinstance(default, FieldInfo):
            from pydantic_core import PydanticUndefined
            actual_default = default.default
            if actual_default is PydanticUndefined:
                # Query(...) means required -- treat as no default
                default = inspect.Parameter.empty
            else:
                default = actual_default

        # --- path params ---
        if param_name in path_params:
            value = path_params[param_name]
            if annotation is not inspect.Parameter.empty:
                value = _coerce(value, _unwrap_optional(annotation))
            kwargs[param_name] = value
            continue

        # --- Depends injection ---
        if isinstance(default, DependsClass):
            dep_func = default.dependency
            dep_name = getattr(dep_func, "__name__", "")

            if dep_name == "get_current_user":
                kwargs[param_name] = user
                continue

            # Unknown dependency – skip (let handler fail naturally if needed)
            logger.warning(
                "Unhandled Depends dependency %r for param %r",
                dep_name,
                param_name,
            )
            continue

        # --- Request object ---
        if annotation is Request or (
            isinstance(annotation, type) and issubclass(annotation, Request)
        ):
            kwargs[param_name] = _build_fake_request(
                method=method,
                path=path,
                headers=headers,
                body_bytes=body_str.encode("utf-8") if body_str else b"",
            )
            continue

        # --- Pydantic body model ---
        if _is_pydantic_model(annotation):
            if body_str:
                model_instance = annotation.model_validate_json(body_str)
                # Inject conversation_id if the model has the field and it's not set
                if (
                    conversation_id is not None
                    and hasattr(model_instance, "conversation_id")
                    and getattr(model_instance, "conversation_id") is None
                ):
                    model_instance.conversation_id = conversation_id
                kwargs[param_name] = model_instance
            else:
                raise ValueError(
                    f"Parameter '{param_name}' expects a Pydantic model "
                    f"({annotation.__name__}) but no request body was provided"
                )
            continue

        # --- conversation_id auto-injection (scalar params) ---
        # For any handler parameter named "conversation_id", inject the
        # value from the dispatch context and SKIP query param lookup.
        # This is dispatch-only: the LLM cannot override it via the URL
        # (prevents hallucinated/incorrect conversation IDs and cross-
        # conversation data access).  When called outside dispatch
        # (direct HTTP), conversation_id is None and the handler should
        # gracefully degrade (e.g., skip URL replacement).
        if param_name == "conversation_id" and not _is_pydantic_model(annotation):
            kwargs[param_name] = conversation_id  # may be None
            continue

        # --- query params / scalar defaults ---
        unwrapped = _unwrap_optional(annotation) if annotation is not inspect.Parameter.empty else annotation
        is_list = _is_list_type(unwrapped) if unwrapped is not inspect.Parameter.empty else False

        if param_name in query_params:
            raw_values = query_params[param_name]
            if is_list:
                kwargs[param_name] = raw_values
            else:
                value = raw_values[0]
                if annotation is not inspect.Parameter.empty:
                    value = _coerce(value, unwrapped)
                kwargs[param_name] = value
            continue

        # Not in query params – fall back to defaults / Optional
        if _is_optional(annotation):
            if default is not inspect.Parameter.empty:
                kwargs[param_name] = default
            else:
                kwargs[param_name] = None
            continue

        if default is not inspect.Parameter.empty:
            kwargs[param_name] = default
            continue

        # Required parameter missing
        raise ValueError(
            f"Required parameter '{param_name}' not found in path params "
            f"or query params"
        )

    return kwargs


def _build_fake_request(
    method: str,
    path: str,
    headers: dict[str, str] | None,
    body_bytes: bytes,
) -> Request:
    """Construct a minimal Starlette ``Request`` for handler injection.

    The resulting object supports ``.body()``, ``.headers``, and
    ``.base_url`` — the three attributes used by existing handlers
    (``batch_request`` and ``get_instructions``).
    """
    encoded_headers: list[tuple[bytes, bytes]] = []
    if headers:
        encoded_headers = [
            (k.lower().encode(), v.encode()) for k, v in headers.items()
        ]

    scope: dict[str, Any] = {
        "type": "http",
        "method": method.upper(),
        "path": path,
        "query_string": b"",
        "headers": encoded_headers,
        "root_path": "",
        "scheme": "http",
        "server": ("localhost", 8000),
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body_bytes}

    return Request(scope, receive=_receive)


# ---------------------------------------------------------------------------
# 4. execute_tool_call
# ---------------------------------------------------------------------------

_TOOL_METHOD_MAP: dict[str, str] = {
    "curl_proxy_get": "GET",
    "curl_proxy_post": "POST",
}

# Internal API paths that must NOT be reachable through curl_proxy_get/post.
# These endpoints are only callable as direct tool invocations (e.g. via
# tool_call dispatch) and should never be proxied through the generic HTTP
# tools.  Paths are matched as prefixes.
_BLOCKED_PROXY_PATHS: list[str] = [
    "/api/authed-get",
    "/api/authed-post",
    # The script tool-call bridge is for sandbox code only; the LLM invokes
    # dynamic tools natively via the tool_call meta tool.
    "/api/tool-call",
    # The one-shot inference API is for external applications (dedicated
    # bearer tokens); letting a run call it through curl_proxy_* would
    # allow unbounded recursion.
    "/api/inference",
    # Slack is dedicated dynamic tools only (list_slack_conversations,
    # send_slack_dm_to_self, etc.); the HTTP routes are gone, but keep the
    # prefixes here so cached sessions that still emit curl_proxy_* get a
    # "use the dedicated tool" pointer instead of a bare no-route error.
    # /api/slack-simple needs its own entry: prefix matching is
    # segment-bounded, so "/api/slack" does not cover it.
    "/api/slack",
    "/api/slack-simple",
    # Twitter/X reads moved to the twitter plugin's api.twitter.com
    # authed_get service entry; the /api/twitter/* HTTP routes are gone,
    # but keep the prefix here so cached sessions that still emit
    # curl_proxy_get get a "use the dedicated tool" pointer instead of a
    # bare no-route error.
    "/api/twitter",
    # Telegram reads are the dedicated telegram_* dynamic tools
    # (telegram_list_dialogs, telegram_get_messages, ... -- registered by
    # the in-tree Telegram plugin, plugins/telegram); the
    # /api/telegram/* HTTP routes are gone, but keep the prefix here so
    # cached sessions that still emit curl_proxy_get get a "use the
    # dedicated tool" pointer instead of a bare no-route error.
    "/api/telegram",
    # IMAP mailboxes are the dedicated imap_* dynamic tools (plugins/imap)
    # and never had HTTP routes; the prefix is reserved here so a model
    # guessing at /api/imap/* gets a "use the dedicated tool" pointer, and
    # so no future route under it is reachable through curl_proxy_*.
    "/api/imap",
]


async def execute_tool_call(
    app: Any,
    user: dict[str, Any],
    tool_name: str,
    tool_args: dict[str, Any],
    conversation_id: str | None = None,
    block_mutating: bool = False,
) -> tuple[str, list[str]]:
    """Execute a tool call by dispatching to the matching app route.

    This is the main entry point used by the Gemini chat loop.  It maps
    tool names (``curl_proxy_get``, ``curl_proxy_post``) to HTTP methods,
    validates the target URL, resolves the route, builds handler kwargs,
    and returns the result as a string.

    Any exception is caught and returned as an error string so that the
    calling code never crashes.

    Args:
        app: The FastAPI application instance.
        user: The authenticated user dict.
        tool_name: Name of the tool (e.g. ``curl_proxy_get``).
        tool_args: Dict of arguments supplied by the model.
        conversation_id: Optional conversation ID for context injection.
        block_mutating: Refuse the state-changing internal routes
            (``chat.llm.tool_schemas.MUTATING_PROXY_PATHS``). Set for
            inference API and cross-user subagent runs.

    Returns:
        Tuple of (result_string, notices) where notices is a list of
        advisory strings for the LLM (empty for most calls).
    """
    try:
        # Map tool name → HTTP method
        http_method = _TOOL_METHOD_MAP.get(tool_name)
        if http_method is None:
            return json.dumps({
                "error": f"Unknown tool name: {tool_name}. "
                         f"Supported tools: {', '.join(_TOOL_METHOD_MAP)}"
            }), []

        # Extract URL
        url = tool_args.get("url")
        if not url:
            return json.dumps({"error": "Missing required 'url' argument"}), []

        # Validate URL
        path, query_params = validate_url(url)

        # Block internal-only endpoints from being reached via proxy tools
        for blocked in _BLOCKED_PROXY_PATHS:
            if path == blocked or path.startswith(blocked + "/"):
                return json.dumps({
                    "error": f"The path '{path}' is not accessible through "
                             f"{tool_name}. Use the dedicated tool instead."
                }), []

        if block_mutating:
            from chat.llm.tool_schemas import MUTATING_PROXY_PATHS
            if path.rstrip("/") in MUTATING_PROXY_PATHS:
                return json.dumps({
                    "error": f"The path '{path}' is not available in "
                             "inference API runs or cross-user subagent runs: "
                             "it changes external state and these runs are read-only."
                }), []

        # Resolve route
        endpoint, path_params = resolve_route(app, http_method, path)

        # Extract optional body and headers
        body_str = tool_args.get("body")
        req_headers = tool_args.get("headers", {})

        # Build kwargs
        kwargs = await build_handler_kwargs(
            endpoint,
            path_params,
            query_params,
            user,
            body_str,
            method=http_method,
            path=path,
            headers=req_headers,
            conversation_id=conversation_id,
        )

        # Call the handler
        call_start = time.time()
        result = await endpoint(**kwargs)
        call_duration_ms = int((time.time() - call_start) * 1000)

        # Extract notices if the handler returned a ToolResultWithNotices wrapper
        notices: list[str] = []
        if isinstance(result, ToolResultWithNotices):
            notices = result.notices
            result = result.result

        serialized = _serialize_result(result)

        endpoint_name = getattr(endpoint, "__name__", str(endpoint))
        logger.info(
            "[route_dispatch] %s %s -> %s completed in %dms (user=%s, result_length=%d)",
            http_method, path, endpoint_name, call_duration_ms,
            user.get("email", "unknown"), len(serialized),
        )

        # Process the result
        return serialized, notices

    except Exception as exc:
        logger.exception(
            "[route_dispatch] Error executing %s %s (user=%s)",
            tool_name, tool_args.get("url", "?"), user.get("email", "unknown"),
        )
        return json.dumps({"error": str(exc)}), []


def _serialize_result(result: Any) -> str:
    """Convert a handler return value to a string."""
    if result is None:
        return ""

    # Starlette Response (including JSONResponse, HTMLResponse, etc.)
    if isinstance(result, Response):
        # Response has a .body attribute with raw bytes
        try:
            body = result.body
            if isinstance(body, bytes):
                return body.decode("utf-8", errors="replace")
            return str(body)
        except Exception:
            return str(result)

    # dict / list → JSON
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)

    # str → as-is
    if isinstance(result, str):
        return result

    # Fallback
    return str(result)
