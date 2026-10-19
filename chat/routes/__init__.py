"""Chat routes package.

Defines the shared ``router`` and imports submodules that register their
endpoints on it.  The key contract is that ``from chat.routes import router``
continues to work unchanged (backward compatible with quest.py).
"""

from fastapi import APIRouter

# Create the shared router -- submodules import this to register endpoints.
router = APIRouter(
    prefix="/app/api",
    tags=["chat"]
)

# Import submodules so their @router decorators execute and register routes.
# These must come AFTER the router definition to avoid circular imports.
from chat.routes import conversations  # noqa: E402, F401
from chat.routes import user  # noqa: E402, F401
from chat.routes import admin  # noqa: E402, F401
from chat.routes import inference_api_keys  # noqa: E402, F401
from chat.routes import transcribe  # noqa: E402, F401
from chat.routes import transcribe_live  # noqa: E402, F401
