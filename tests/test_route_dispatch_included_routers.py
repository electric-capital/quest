"""resolve_route must see routes that reach the app through include_router.

Since FastAPI 0.137, app.routes holds one node per included router instead
of the router's path operations, so a plain loop over app.routes misses
every /app/api/* and plugin route. The dispatcher walks the flat table from
iter_app_routes; these tests pin that for nested routers with prefixes.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

# quest.py loads chat.gemini_api before chat.route_dispatch; importing the
# dispatcher first trips a circular import, so match that order here.
import chat.gemini_api  # noqa: F401
from chat.route_dispatch import resolve_route
from chat.route_table import iter_app_routes


@pytest.fixture
def app() -> FastAPI:
    inner = APIRouter(prefix="/inner")

    @inner.get("/items/{item_id}")
    async def get_item(item_id: int):  # pragma: no cover - never called
        return item_id

    @inner.post("/items")
    async def create_item():  # pragma: no cover - never called
        return {}

    outer = APIRouter(prefix="/api")
    outer.include_router(inner)

    app = FastAPI()
    app.include_router(outer)

    @app.get("/api/{range:path}")
    async def catch_all(range: str):  # pragma: no cover - never called
        return range

    return app


def test_iter_app_routes_flattens_nested_includes_in_order(app):
    paths = [r.path for r in iter_app_routes(app) if isinstance(r.original_route, APIRoute)]
    assert paths == ["/api/inner/items/{item_id}", "/api/inner/items", "/api/{range:path}"]
    get_item = next(r for r in iter_app_routes(app) if r.path == "/api/inner/items/{item_id}")
    assert get_item.methods == {"GET"}
    assert get_item.endpoint.__name__ == "get_item"
    assert hasattr(get_item, "dependant")
    assert isinstance(get_item.original_route, APIRoute)


def test_resolve_route_reaches_included_router_routes(app):
    endpoint, params = resolve_route(app, "GET", "/api/inner/items/7")
    assert endpoint.__name__ == "get_item"
    assert params == {"item_id": "7"}

    endpoint, params = resolve_route(app, "POST", "/api/inner/items")
    assert endpoint.__name__ == "create_item"
    assert params == {}


def test_resolve_route_honours_method_and_falls_through(app):
    # DELETE is not offered by the included router; the catch-all is GET-only
    # too, so nothing matches.
    with pytest.raises(ValueError, match="No route matched DELETE"):
        resolve_route(app, "DELETE", "/api/inner/items")
    # The catch-all still serves what the included router does not.
    endpoint, params = resolve_route(app, "GET", "/api/other/thing")
    assert endpoint.__name__ == "catch_all"
    assert params == {"range": "other/thing"}
