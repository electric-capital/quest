"""HTTP routes for a Quest Doc's notes (spec 00011 section 7, phase A6).

Skeleton: phase A6's builder fills in the five notes routes.
"""

from fastapi import APIRouter

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)
