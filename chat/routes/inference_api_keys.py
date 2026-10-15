"""Settings > Inference API key management endpoints.

CRUD for the named bearer tokens that authenticate the one-shot
``POST /api/inference`` endpoint (see chat/inference_api.py). The raw
token is returned exactly once, in the create response; afterwards only
the name and a short hint are visible.

Every route here is refused (403) to an admin impersonation session: a key
minted under impersonation would outlive the impersonation window.
"""

import logging

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from auth.session import forbid_impersonation
from chat.auth import get_current_user_cookie_or_apikey_checked
from chat.routes import router
from db import inference_api_key_store

logger = logging.getLogger(__name__)

MAX_KEY_NAME_LENGTH = 100


class InferenceApiKeyCreate(BaseModel):
    name: str


@router.get("/inference-api-keys", dependencies=[Depends(forbid_impersonation)])
async def list_inference_api_keys(
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """List the caller's inference API keys (names + hints, never tokens)."""
    keys = await inference_api_key_store.list_keys(user["id"])
    return {"keys": keys}


@router.post("/inference-api-keys", dependencies=[Depends(forbid_impersonation)])
async def create_inference_api_key(
    body: InferenceApiKeyCreate,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Mint a new inference API key. The raw token appears ONLY here."""
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_name",
                "message": "Key name is required.",
            },
        )
    if len(name) > MAX_KEY_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_name",
                "message": (
                    f"Key names are limited to {MAX_KEY_NAME_LENGTH} "
                    "characters."
                ),
            },
        )

    if (
        await inference_api_key_store.count_keys(user["id"])
        >= inference_api_key_store.MAX_KEYS_PER_USER
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "too_many_keys",
                "message": (
                    "At most "
                    f"{inference_api_key_store.MAX_KEYS_PER_USER} inference "
                    "API keys are allowed per user. Delete an unused key "
                    "first."
                ),
            },
        )

    token = inference_api_key_store.generate_token()
    key = await inference_api_key_store.create_key(user["id"], name, token)
    logger.info(
        "[inference-api] Key created (user=%s, key_id=%s, name=%s)",
        user["email"], key["id"], name,
    )
    return {**key, "token": token}


@router.delete(
    "/inference-api-keys/{key_id}", dependencies=[Depends(forbid_impersonation)],
)
async def delete_inference_api_key(
    key_id: str,
    user: dict = Depends(get_current_user_cookie_or_apikey_checked),
):
    """Delete (revoke) one of the caller's inference API keys."""
    deleted = await inference_api_key_store.delete_key(user["id"], key_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "key_not_found",
                "message": "No such inference API key.",
            },
        )
    logger.info(
        "[inference-api] Key deleted (user=%s, key_id=%s)",
        user["email"], key_id,
    )
    return {"success": True}
