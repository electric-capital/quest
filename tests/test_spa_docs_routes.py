"""The /docs SPA routes and the moved Swagger UI / ReDoc URLs (quest.py).

/docs and /docs/<id> are the frontend's Quest Docs routes, so FastAPI's
interactive API docs moved from its defaults (/docs, /redoc) to /api-docs and
/api-redoc. These tests pin both halves of that swap on the real route table.

The route table comes from the shared ``quest_route_table`` fixture
(tests/route_table_support.py).
"""

from __future__ import annotations


def test_spa_docs_routes_exist(quest_route_table):
    routes = {tuple(r) for r in quest_route_table["routes"]}
    assert ("GET", "/docs") in routes
    assert ("GET", "/docs/{rest:path}") in routes


def test_spa_docs_routes_precede_static_fallback(quest_route_table):
    """/docs must be registered before the /{filename} static fallback,
    which would otherwise 404 it (no dist file named "docs")."""
    paths = quest_route_table["paths"]
    assert paths.index("/docs") < paths.index("/{filename}")
    assert paths.index("/docs/{rest:path}") < paths.index("/{filename}")


def test_swagger_and_redoc_moved(quest_route_table):
    assert quest_route_table["docs_url"] == "/api-docs"
    assert quest_route_table["redoc_url"] == "/api-redoc"
    assert quest_route_table["openapi_url"] == "/openapi.json"


def test_default_redoc_path_gone(quest_route_table):
    assert "/redoc" not in quest_route_table["paths"]


def test_docs_path_is_only_the_spa_route(quest_route_table):
    """Exactly one route owns /docs: the SPA one, not Swagger UI."""
    assert quest_route_table["paths"].count("/docs") == 1
    # Swagger's OAuth2 redirect page follows docs_url too, so no non-SPA
    # route is left anywhere under /docs/.
    assert quest_route_table["oauth2_redirect_url"] == "/api-docs/oauth2-redirect"
    assert not [p for p in quest_route_table["paths"] if p and p.startswith("/docs/") and p != "/docs/{rest:path}"]
    assert "/api-docs" in quest_route_table["paths"]
    assert "/api-redoc" in quest_route_table["paths"]
