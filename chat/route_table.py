"""The flat view of a FastAPI app's route table.

Kept free of chat-package imports so tests and subprocess scripts can import
it on its own (``chat.route_dispatch`` pulls in the conversation loop).
"""

from __future__ import annotations

from typing import Any, Iterator

from fastapi.routing import iter_route_contexts


def iter_app_routes(app: Any) -> Iterator[Any]:
    """Yield every route of *app* as a flat sequence, in registration order.

    Since FastAPI 0.137 ``include_router`` no longer copies the included
    router's path operations into ``app.routes``: it appends one node that
    holds the router, so ``app.routes`` is a tree and a plain loop over it
    sees the node, not the routes inside. FastAPI's ``iter_route_contexts``
    walks that tree and yields a ``RouteContext`` per leaf, exposing the
    route's ``path``, ``methods``, ``endpoint``, ``matches()`` and
    ``dependant`` as they take effect, with include-time prefixes and
    dependencies merged in; ``original_route`` is the ``APIRoute`` as
    declared. Every place that introspects the app's route table goes
    through here, tests included.
    """
    return iter_route_contexts(app.routes)
