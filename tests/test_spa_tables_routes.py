"""The /tables SPA routes (quest.py) and the tables API router mount.

/tables, /tables?project=<id> and /tables/<id> are the frontend's Quest
Tables routes; they must be registered before the /{filename} static
fallback. The route table comes from the shared ``quest_route_table``
fixture (tests/route_table_support.py).
"""

from __future__ import annotations


def test_spa_tables_routes_exist(quest_route_table):
    routes = {tuple(r) for r in quest_route_table["routes"]}
    assert ("GET", "/tables") in routes
    assert ("GET", "/tables/{rest:path}") in routes


def test_spa_tables_routes_precede_static_fallback(quest_route_table):
    paths = quest_route_table["paths"]
    assert paths.index("/tables") < paths.index("/{filename}")
    assert paths.index("/tables/{rest:path}") < paths.index("/{filename}")


def test_tables_api_mounted(quest_route_table):
    routes = {tuple(r) for r in quest_route_table["routes"]}
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
    paths = quest_route_table["paths"]
    assert paths.index("/app/api/tables/{table_id}") < paths.index("/tables/{rest:path}")
