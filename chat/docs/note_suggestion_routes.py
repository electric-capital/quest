"""HTTP routes for reviewing doc note suggestions (spec 00011 section 7,
phase A6).

Skeleton: phase A6's builder fills in the four suggestion routes.
"""

from fastapi import APIRouter

router = APIRouter(
    prefix="/app/api",
    tags=["docs"],
)
