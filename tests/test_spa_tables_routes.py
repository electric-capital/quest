"""The /tables SPA routes (quest.py) and the tables API router mount.

/tables, /tables?project=<id> and /tables/<id> are the frontend's Quest
Tables routes; they must be registered before the /{filename} static
fallback. Runs in a subprocess because importing quest loads the plugins
into the process-global registries (same pattern as
tests/test_spa_docs_routes.py).
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
    "print(json.dumps({'routes': routes, 'paths': paths}))\n"
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


def test_spa_tables_routes_exist(route_table):
    routes = {tuple(r) for r in route_table["routes"]}
    assert ("GET", "/tables") in routes
    assert ("GET", "/tables/{rest:path}") in routes


def test_spa_tables_routes_precede_static_fallback(route_table):
    paths = route_table["paths"]
    assert paths.index("/tables") < paths.index("/{filename}")
    assert paths.index("/tables/{rest:path}") < paths.index("/{filename}")


def test_tables_api_mounted(route_table):
    routes = {tuple(r) for r in route_table["routes"]}
    for method, path in [
        ("GET", "/app/api/tables"),
        ("POST", "/app/api/tables"),
        ("GET", "/app/api/tables/{table_id}"),
        ("PUT", "/app/api/tables/{table_id}"),
        ("DELETE", "/app/api/tables/{table_id}"),
        ("PUT", "/app/api/tables/{table_id}/schema"),
        ("GET", "/app/api/tables/{table_id}/rows"),
    ]:
        assert (method, path) in routes, (method, path)
    # The API routes come before the SPA catch-all.
    paths = route_table["paths"]
    assert paths.index("/app/api/tables/{table_id}") < paths.index("/tables/{rest:path}")
