"""The real app's route table, for tests that pin what ``quest.app`` mounts.

Importing ``quest`` loads every plugin into the process-global registries,
which would pollute other tests, so the table is read in a subprocess and
handed back as plain data. The app is booted once per test process however
many tests and suites ask (the result is kept at module level, so the copies
of the fixture that different conftests register share it).
``tests/conftest.py`` exposes it to the core suite; a plugin suite imports
it into its own conftest, as with ``tests.db_support``.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = (
    "import json, quest\n"
    "from chat.route_table import iter_app_routes\n"
    "app = quest.app\n"
    "routes = sorted({(m, r.path) for r in iter_app_routes(app)"
    " if r.methods for m in r.methods})\n"
    "paths = [r.path for r in iter_app_routes(app)]\n"
    "print(json.dumps({\n"
    "    'routes': routes,\n"
    "    'paths': paths,\n"
    "    'docs_url': app.docs_url,\n"
    "    'redoc_url': app.redoc_url,\n"
    "    'openapi_url': app.openapi_url,\n"
    "    'oauth2_redirect_url': app.swagger_ui_oauth2_redirect_url,\n"
    "}))\n"
)


_cached: dict | None = None


@pytest.fixture(scope="session")
def quest_route_table() -> dict:
    """``quest.app``'s routes as data.

    ``routes`` is the sorted list of ``[method, path]`` pairs, ``paths``
    every route path in registration order (duplicates and method-less
    routes included), plus the app's ``docs_url``, ``redoc_url``,
    ``openapi_url`` and ``oauth2_redirect_url``.
    """
    global _cached
    if _cached is not None:
        return _cached
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    # The JSON document is the last stdout line (imports may log above it).
    _cached = json.loads(proc.stdout.strip().splitlines()[-1])
    return _cached
