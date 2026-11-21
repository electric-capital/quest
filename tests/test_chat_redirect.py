"""The legacy /chat* redirect (quest.py) only ever targets this origin.

``/chat//host`` used to answer ``Location: //host``, which a browser treats
as a protocol-relative URL to another site. Runs in a subprocess because
importing quest loads the plugins into the process-global registries (same
pattern as tests/test_spa_docs_routes.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_PATHS = [
    "/chat",
    "/chat/",
    "/chat/chats/abc",
    "/chat//evil.example.com",
    "/chat///evil.example.com/x",
    "/chat/%2Fevil.example.com",
    "/chat/%2F%2Fevil.example.com",
    "/chat/%5Cevil.example.com",
    "/chat/%2F%5Cevil.example.com",
]

_SCRIPT = (
    "import json, sys, quest\n"
    "from starlette.testclient import TestClient\n"
    "client = TestClient(quest.app, follow_redirects=False)\n"
    "out = {}\n"
    "for path in json.loads(sys.argv[1]):\n"
    "    r = client.get(path)\n"
    "    out[path] = [r.status_code, r.headers.get('location')]\n"
    "print(json.dumps(out))\n"
)


@pytest.fixture(scope="module")
def redirects() -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, json.dumps(_PATHS)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    # The JSON document is the last stdout line (imports may log above it).
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_legacy_paths_still_redirect(redirects):
    assert redirects["/chat"] == [301, "/"]
    assert redirects["/chat/"] == [301, "/"]
    assert redirects["/chat/chats/abc"] == [301, "/chats/abc"]


@pytest.mark.parametrize("path", [p for p in _PATHS if "evil" in p])
def test_redirect_never_leaves_the_origin(redirects, path):
    status, location = redirects[path]
    assert status == 301
    # A single leading slash followed by a non-slash, non-backslash
    # character is a same-origin path; "//host" and "/\\host" are not.
    assert location.startswith("/")
    assert not location.startswith(("//", "/\\", "/%5C", "/%5c", "/%2F", "/%2f"))
