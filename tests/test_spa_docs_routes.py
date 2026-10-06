"""The /docs SPA routes and the moved Swagger UI / ReDoc URLs (quest.py).

/docs and /docs/<id> are the frontend's Quest Docs routes, so FastAPI's
interactive API docs moved from its defaults (/docs, /redoc) to /api-docs and
/api-redoc. These tests pin both halves of that swap on the real route table.

Runs in a subprocess because importing quest loads the plugins into the
process-global registries, which would pollute other tests (same pattern as
tests/test_sandbox_api.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = (
    "import json, quest\n"
    "app = quest.app\n"
    "routes = sorted({(m, r.path) for r in app.routes"
    " if hasattr(r, 'methods') for m in r.methods})\n"
    "paths = [getattr(r, 'path', None) for r in app.routes]\n"
    "print(json.dumps({\n"
    "    'routes': routes,\n"
    "    'paths': paths,\n"
    "    'docs_url': app.docs_url,\n"
    "    'redoc_url': app.redoc_url,\n"
    "    'openapi_url': app.openapi_url,\n"
    "}))\n"
)


@pytest.fixture(scope="module")
def route_table() -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    # The JSON document is the last stdout line (imports may log above it).
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_spa_docs_routes_exist(route_table):
    routes = {tuple(r) for r in route_table["routes"]}
    assert ("GET", "/docs") in routes
    assert ("GET", "/docs/{rest:path}") in routes


def test_spa_docs_routes_precede_static_fallback(route_table):
    """/docs must be registered before the /{filename} static fallback,
    which would otherwise 404 it (no dist file named "docs")."""
    paths = route_table["paths"]
    assert paths.index("/docs") < paths.index("/{filename}")
    assert paths.index("/docs/{rest:path}") < paths.index("/{filename}")


def test_swagger_and_redoc_moved(route_table):
    assert route_table["docs_url"] == "/api-docs"
    assert route_table["redoc_url"] == "/api-redoc"
    assert route_table["openapi_url"] == "/openapi.json"


def test_default_redoc_path_gone(route_table):
    assert "/redoc" not in route_table["paths"]


def test_docs_path_is_only_the_spa_route(route_table):
    """Exactly one route owns /docs: the SPA one, not Swagger UI."""
    assert route_table["paths"].count("/docs") == 1
    assert "/api-docs" in route_table["paths"]
    assert "/api-redoc" in route_table["paths"]
