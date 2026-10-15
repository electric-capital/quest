"""URL normalisation for ``authed_get`` / ``authed_post`` (``_parse_request_url``).

Every authed request is parsed once with ``httpx.URL``; the registry lookup
and allow-list match read the path exactly as it will be sent, the client is
handed that same ``httpx.URL``, and a path with dot segments, empty segments,
backslashes or control characters -- literal or percent-encoded -- is refused
before any registry lookup, credential load or network I/O.

The per-entry suites iterate the live ``_SERVICE_REGISTRY`` with every plugin
the server would load registered into it, so a service or allow-list pattern
added later is covered without touching this file.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

try:  # Python 3.11+
    import re._constants as _sre
    import re._parser as _sre_parse
except ImportError:  # pragma: no cover - older interpreters
    import sre_constants as _sre  # type: ignore[no-redef]
    import sre_parse as _sre_parse  # type: ignore[no-redef]

from chat.gemini_api import authed_get
from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _make_authed_request,
    _parse_request_url,
    _path_problem,
    handle_authed_get,
    handle_authed_post,
)
from config import plugins as plugins_mod


def _run(coro):
    return asyncio.run(coro)


_USER = {"id": 1, "email": "user@example.com"}

DOT = "a '.' or '..' segment"
EMPTY = "an empty segment"
BACKSLASH = "a backslash"
CONTROL = "a control character"


# ---------------------------------------------------------------------------
# A matching example path for any allow-list regex
# ---------------------------------------------------------------------------

_REPEATS = {
    getattr(_sre, name)
    for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT")
    if hasattr(_sre, name)
}
_IGNORED = {_sre.AT, _sre.ASSERT, _sre.ASSERT_NOT}


def _char_in(items) -> str:
    """A character inside the parsed character class *items*."""
    negate = bool(items) and items[0][0] == _sre.NEGATE
    body = items[1:] if negate else items

    def hit(ch: str) -> bool:
        code = ord(ch)
        for op, av in body:
            if op == _sre.LITERAL and av == code:
                return True
            if op == _sre.RANGE and av[0] <= code <= av[1]:
                return True
            if op == _sre.CATEGORY:
                if av == _sre.CATEGORY_DIGIT and ch.isdigit():
                    return True
                if av == _sre.CATEGORY_WORD and (ch.isalnum() or ch == "_"):
                    return True
        return False

    for ch in "x1aQ-_~":
        if hit(ch) != negate:
            return ch
    raise ValueError(f"no candidate character for class {items!r}")


def _generate(items) -> str:
    out: list[str] = []
    for op, av in items:
        if op == _sre.LITERAL:
            out.append(chr(av))
        elif op == _sre.NOT_LITERAL:
            out.append("x" if av != ord("x") else "y")
        elif op == _sre.ANY:
            out.append("x")
        elif op == _sre.IN:
            out.append(_char_in(av))
        elif op in _REPEATS:
            low, _high, sub = av
            out.append(_generate(sub) * low)
        elif op == _sre.SUBPATTERN:
            out.append(_generate(av[-1]))
        elif op == _sre.BRANCH:
            out.append(_generate(av[1][0]))
        elif hasattr(_sre, "ATOMIC_GROUP") and op == _sre.ATOMIC_GROUP:
            out.append(_generate(av))
        elif op in _IGNORED:
            continue
        else:
            raise ValueError(f"unsupported regex construct {op!r}")
    return "".join(out)


def _example_path(pattern: str | None) -> str:
    """The shortest path the allow-list *pattern* accepts (``None``: any path)."""
    if pattern is None:
        return "/v1/x"
    return _generate(_sre_parse.parse(pattern))


# ---------------------------------------------------------------------------
# The cases: every allow-list entry of every registered service
# ---------------------------------------------------------------------------

# Every plugin the server would load (in-tree plugins/ plus QUEST_PLUGIN_PATH
# roots). Their service entries are registered into the live registry by the
# module fixture below; core entries are already there.
_PLUGINS = plugins_mod.discover_plugins()
_PLUGIN_ENTRIES = {
    service["key"]: service["entry"]
    for plugin in _PLUGINS
    for service in plugin.services
}


def _all_entries() -> dict[str, dict]:
    entries = {k: v for k, v in _SERVICE_REGISTRY.items() if k not in _PLUGIN_ENTRIES}
    entries.update(_PLUGIN_ENTRIES)
    return entries


def _cases() -> list:
    cases = []
    for key, entry in _all_entries().items():
        get_patterns = entry.get("allowed_endpoints")
        if get_patterns is None:
            # No GET allow-list: any path on the host is allowed.
            cases.append(pytest.param(key, "GET", None, id=f"{key}|GET|any"))
        else:
            for index, _ in enumerate(get_patterns):
                cases.append(pytest.param(key, "GET", index, id=f"{key}|GET|{index}"))
        for index, _ in enumerate(entry.get("allowed_post_endpoints") or ()):
            cases.append(pytest.param(key, "POST", index, id=f"{key}|POST|{index}"))
    return cases


_CASES = _cases()


@pytest.fixture(scope="module")
def all_services():
    """Register every discoverable plugin's surfaces into the live registries."""
    registered = []
    for plugin in _PLUGINS:
        if plugin.id in plugins_mod._REGISTRATIONS:
            continue
        plugins_mod.register_plugin(plugin)
        registered.append(plugin)
    yield _SERVICE_REGISTRY
    for plugin in reversed(registered):
        plugins_mod.unregister_plugin(plugin)


def _live_pattern(key: str, method: str, index: int | None) -> str | None:
    entry = _SERVICE_REGISTRY[key]
    if index is None:
        assert "allowed_endpoints" not in entry
        return None
    source = "allowed_post_endpoints" if method == "POST" else "allowed_endpoints"
    return entry[source][index]


def _url(key: str, path: str) -> str:
    return f"https://{key.split('/', 1)[0]}{path}"


class _CapturingClient:
    """Stands in for ``httpx.AsyncClient``; records what would be sent."""

    calls: list[tuple[str, object]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        self.calls.append(("GET", url))
        return httpx.Response(200, json={"ok": True})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url))
        return httpx.Response(200, json={"ok": True})


@pytest.fixture()
def wire(monkeypatch):
    """Capture outgoing requests and credential loads; no network, no secrets."""
    _CapturingClient.calls = []
    loader = AsyncMock(return_value="test-credential")
    monkeypatch.setattr(authed_get.httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setattr(authed_get, "_call_loader", loader)
    return _CapturingClient.calls, loader


def _stub_injector(monkeypatch, key: str) -> None:
    def inject(_credentials, headers):
        headers["Authorization"] = "Bearer test"

    monkeypatch.setitem(_SERVICE_REGISTRY[key], "inject_auth", inject)


# ---------------------------------------------------------------------------
# Per allow-list entry
# ---------------------------------------------------------------------------

# Suffixes appended to (or spliced into) a path the entry allows. ``{p}`` is
# the allowed example path, ``{head}`` / ``{tail}`` its parent and last
# segment.
_REFUSED_VARIANTS = [
    ("{p}/../../../../x", DOT),
    ("{p}/..", DOT),
    ("{p}/./x", DOT),
    ("{p}/.", DOT),
    ("{head}/x/../{tail}", DOT),
    ("{p}/%2e%2e/%2e%2e/x", DOT),
    ("{p}/%2E%2E/x", DOT),
    ("{p}/.%2e/x", DOT),
    ("{p}/%2e./x", DOT),
    ("{p}/%2E/x", DOT),
    ("{p}/..%2fx", DOT),
    ("{p}/..%2Fx", DOT),
    ("{p}%2f..%2f..%2fx", DOT),
    ("{p}/%252e%252e/x", DOT),
    ("{p}//x", EMPTY),
    ("{p}/%2f/x", EMPTY),
    ("{p}\\..\\x", BACKSLASH),
    ("{p}/%5c..%5cx", BACKSLASH),
    ("{p}/%5C..%5Cx", BACKSLASH),
    ("{p}/x%00", CONTROL),
]


def test_cases_cover_every_registered_allow_list(all_services):
    """Nothing in the live registry escapes the parametrised cases."""
    covered: dict[str, set] = {}
    for case in _CASES:
        key, method, index = case.values
        covered.setdefault(key, set()).add((method, index))
    assert set(covered) == set(all_services)
    for key, entry in all_services.items():
        expected = {("GET", i) for i, _ in enumerate(entry.get("allowed_endpoints") or ())}
        if "allowed_endpoints" not in entry:
            expected = {("GET", None)}
        expected |= {("POST", i) for i, _ in enumerate(entry.get("allowed_post_endpoints") or ())}
        assert covered[key] == expected, key


@pytest.mark.parametrize("key,method,index", _CASES)
def test_allowed_paths_still_pass(all_services, wire, monkeypatch, key, method, index):
    """The entry's own path is sent unchanged, as the matched httpx.URL."""
    calls, loader = wire
    _stub_injector(monkeypatch, key)
    example = _example_path(_live_pattern(key, method, index))
    assert _path_problem(example) is None, example

    for suffix in ("", "?q=a/../b", "#x/../y"):
        calls.clear()
        result = _run(_make_authed_request(
            _url(key, example + suffix), user=_USER, method=method,
            json_body={} if method == "POST" else None,
        ))
        assert json.loads(result) == {"ok": True}, (example + suffix, result)
        assert len(calls) == 1
        sent_method, sent_url = calls[0]
        assert sent_method == method
        assert isinstance(sent_url, httpx.URL)
        assert sent_url.raw_path.decode().partition("?")[0] == example
        assert sent_url.host == key.split("/", 1)[0]
    assert loader.await_count == 3


@pytest.mark.parametrize("key,method,index", _CASES)
def test_dot_segments_rejected(all_services, wire, monkeypatch, key, method, index):
    """Dot / empty segments and backslashes under an allowed path are refused
    with a clear error, before credential loading or any request."""
    calls, loader = wire
    _stub_injector(monkeypatch, key)
    example = _example_path(_live_pattern(key, method, index))
    head, _, tail = example.rpartition("/")

    for template, problem in _REFUSED_VARIANTS:
        path = template.format(p=example, head=head, tail=tail)
        result = json.loads(_run(_make_authed_request(
            _url(key, path), user=_USER, method=method,
            json_body={} if method == "POST" else None,
        )))
        assert isinstance(result.get("error"), str), (path, result)
        assert problem in result["error"], (path, result)
    assert calls == []
    loader.assert_not_awaited()


# ---------------------------------------------------------------------------
# The path checker itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "",
    "/",
    "/a/b",
    "/a/b/",
    "/a/.hidden",
    "/a/v1.2",
    "/a/b.json",
    "/a/...",
    "/a/%2e%2ex",
    "/a/x%2e%2e",
    "/a/%20",
    "/a/caf%C3%A9",
    "/v4/spreadsheets/x/values/'My%20Sheet'!A1:B2",
    "/repos/o/r/contents/dir%2Ffile.txt",
])
def test_clean_paths_have_no_problem(path):
    assert _path_problem(path) is None


@pytest.mark.parametrize("path,problem", [
    ("/a/..", DOT),
    ("/a/.", DOT),
    ("/a/../b", DOT),
    ("/./a", DOT),
    ("/a/%2e%2e/b", DOT),
    ("/a/%2E%2e/b", DOT),
    ("/a/%2e/b", DOT),
    ("/a/..%2fb", DOT),
    ("/a/..%2Fb", DOT),
    ("/a%2f..%2fb", DOT),
    ("/a/%252e%252e/b", DOT),
    ("/a/%25252e%25252e/b", DOT),
    ("//a", EMPTY),
    ("/a//b", EMPTY),
    ("/a/%2f/b", EMPTY),
    ("/a%2F%2Fb", EMPTY),
    ("/a\\b", BACKSLASH),
    ("/a/%5cb", BACKSLASH),
    ("/a/%5Cb", BACKSLASH),
    ("/a/%00", CONTROL),
    ("/a/%0d%0aX", CONTROL),
    ("/a/%7f", CONTROL),
])
def test_refused_paths_name_the_problem(path, problem):
    assert problem in (_path_problem(path) or ""), path


def test_over_encoded_path_is_refused():
    # Six layers of percent-encoding around "A": more than the checker
    # decodes, so it cannot prove the fully decoded form is clean.
    assert _path_problem("/a/%" + "25" * 6 + "41") == "nested percent-encoding"
    # Four layers decode to a clean path within the limit.
    assert _path_problem("/a/%" + "25" * 3 + "41") is None


# ---------------------------------------------------------------------------
# Host, scheme, port, userinfo: checked on the same parsed URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://api.github.com@other.example/user",
    "https://api.github.com:@other.example/user",
    "https://api.github.com:443@other.example/user",
    "https://api.github.com%2F@other.example/user",
    "https://sheets.googleapis.com@other.example/v4/spreadsheets/x",
    "https://other.example\\@api.github.com/user",
    "https://other.example%5C@api.github.com/user",
    "https://user:pass@api.github.com/user",
    "https://api.github.com.other.example/user",
    "https://api.github.com:8443/user",
    "https://api.github.com#@other.example/user",
    "https://" + "".join(map(chr, (0xFF41, 0xFF50, 0xFF49))) + ".github.com/user",  # fullwidth "api"
    "https://api.github.com" + chr(0x3002) + "other.example/user",  # ideographic full stop
    "http://api.github.com/user",
    "https:api.github.com/user",
    "https:///api.github.com/user",
    "//api.github.com/user",
])
def test_nonstandard_authority_is_rejected(all_services, wire, url):
    calls, loader = wire
    result = json.loads(_run(_make_authed_request(url, user=_USER)))
    assert "error" in result, url
    assert calls == []
    loader.assert_not_awaited()


def test_userinfo_is_refused_even_for_a_registered_host(all_services, wire):
    calls, _ = wire
    result = json.loads(_run(_make_authed_request(
        "https://other.example%5C@api.github.com/user", user=_USER,
    )))
    assert "user@ part" in result["error"]
    assert calls == []


def test_explicit_default_port_is_accepted(all_services, wire, monkeypatch):
    calls, _ = wire
    _stub_injector(monkeypatch, "api.github.com")
    result = _run(_make_authed_request("https://api.github.com:443/user", user=_USER))
    assert json.loads(result) == {"ok": True}
    assert str(calls[0][1]) == "https://api.github.com/user"


def test_uppercase_scheme_and_host_are_normalised(all_services, wire, monkeypatch):
    calls, _ = wire
    _stub_injector(monkeypatch, "api.github.com")
    result = _run(_make_authed_request("HTTPS://API.GITHUB.COM/user", user=_USER))
    assert json.loads(result) == {"ok": True}
    assert calls[0][1].host == "api.github.com"


@pytest.mark.parametrize("url", [None, 42, ["https://api.github.com/user"]])
def test_non_string_url_is_invalid(wire, url):
    calls, _ = wire
    result = json.loads(_run(_make_authed_request(url, user=_USER)))
    assert result["error"].startswith("Invalid URL")
    assert calls == []


# ---------------------------------------------------------------------------
# Matched path == sent path
# ---------------------------------------------------------------------------

def test_path_parameters_are_part_of_the_matched_path(wire):
    """';' stays in the path that is matched, as it does on the wire."""
    calls, loader = wire
    result = json.loads(_run(_make_authed_request(
        "https://docs.googleapis.com/v1/documents/abc;/extra", user=_USER,
    )))
    assert "Endpoint path '/v1/documents/abc;/extra' is not allowed" in result["error"]
    assert calls == []
    loader.assert_not_awaited()


def test_query_and_fragment_are_not_matched(wire, monkeypatch):
    calls, _ = wire
    _stub_injector(monkeypatch, "www.googleapis.com/drive/v3")
    result = _run(_make_authed_request(
        "https://www.googleapis.com/drive/v3/files?q='a/../b' in parents#/../about",
        user=_USER,
    ))
    assert json.loads(result) == {"ok": True}
    sent = calls[0][1]
    assert sent.raw_path.decode().partition("?")[0] == "/drive/v3/files"
    assert sent.fragment == "/../about"  # never sent by the client


def test_parse_returns_the_url_that_is_sent():
    target, error = _parse_request_url(
        "https://sheets.googleapis.com/v4/spreadsheets/x/values/'My Sheet'!A1?majorDimension=ROWS",
    )
    assert error is None
    assert target.host == "sheets.googleapis.com"
    assert target.path == "/v4/spreadsheets/x/values/'My%20Sheet'!A1"
    assert target.query == "majorDimension=ROWS"
    assert target.url.raw_path.decode() == f"{target.path}?{target.query}"


# ---------------------------------------------------------------------------
# Tool handlers and the sandbox proxy routes share the check
# ---------------------------------------------------------------------------

_DOT_URL = "https://sheets.googleapis.com/v4/spreadsheets/x/values/../../../x"
_POST_DOT_URL = "https://logging.googleapis.com/v2/projects/p/../../entries:list"


def test_tool_handlers_reject_dot_segments(wire):
    calls, loader = wire
    get_result = json.loads(_run(handle_authed_get(_DOT_URL, user=_USER)))
    to_file = json.loads(_run(handle_authed_get(
        _DOT_URL, user=_USER, conversation_id="c1", output_file="out.json",
    )))
    post_result = json.loads(_run(handle_authed_post(_POST_DOT_URL, body={}, user=_USER)))
    for result in (get_result, to_file, post_result):
        assert DOT in result["error"], result
    assert calls == []
    loader.assert_not_awaited()


def test_export_steering_reads_the_normalised_path(wire):
    """The files.export steering sees the same path that would be matched."""
    calls, _ = wire
    result = json.loads(_run(handle_authed_get(
        "https://www.googleapis.com/drive/v3/files/abc/export?mimeType=application/pdf",
        user=_USER,
    )))
    assert "google_export_doc" in result["error"]
    assert calls == []


@pytest.mark.parametrize("route,body", [
    ("/api/authed-get", {"url": _DOT_URL}),
    ("/api/authed-get", {"url": _DOT_URL + "?alt=media"}),
    ("/api/authed-get", {"url": "https://sheets.googleapis.com/v4/spreadsheets/x/values/%2e%2e/%2e%2e/x"}),
    ("/api/authed-post", {"url": _POST_DOT_URL, "body": {}}),
])
def test_sandbox_proxy_routes_reject_dot_segments(monkeypatch, route, body):
    """The sandbox port's authed routes go through the same normalisation."""
    import db.user_store as user_store
    from chat.sandbox_api import create_sandbox_app
    from chat.sandbox_tokens import sandbox_token_lease

    user = {"id": 31, "email": "user@example.com", "settings": {}}

    async def _get_user_by_id(uid):
        return dict(user) if uid == user["id"] else None

    monkeypatch.setattr(user_store, "get_user_by_id", _get_user_by_id)
    # Rejection must happen before credentials are even loaded; returning
    # None also guarantees no upstream request if it did not.
    loader = AsyncMock(return_value=None)
    monkeypatch.setattr(authed_get, "_call_loader", loader)

    app = create_sandbox_app()

    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://sandbox") as client:
            with sandbox_token_lease(user["id"], ttl_seconds=60) as token:
                return await client.post(
                    route, json=body, headers={"Authorization": f"Bearer {token}"},
                )

    response = _run(_go())
    assert response.status_code == 200, response.text
    assert DOT in response.json()["error"]
    loader.assert_not_awaited()
