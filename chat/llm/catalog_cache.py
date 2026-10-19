"""Shared plumbing for the key-fetched vendor model catalogs.

The Anthropic, OpenAI and Gemini catalog modules (``anthropic_catalog.py``,
``openai_catalog.py``, ``gemini_catalog.py``) all do the same thing with a
different upstream: GET the vendor's model list with the instance's own
API key, normalize it to the OpenRouter row shape the admin typeahead and
:func:`openrouter_catalog.catalog_snapshot` consume, and cache the result
in a JSON file under the data directory for :data:`CATALOG_TTL_SECONDS`,
serving the stale cache -- or an empty list -- with the error alongside
when a fetch fails. This module holds that shared part; each catalog module
keeps only its URL, headers, entry normalization and metadata enrichment.
(The older Fireworks module predates it and carries its own copy.)
"""

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 15


class CatalogError(RuntimeError):
    """The catalog could not be fetched (network, auth, unexpected shape)."""


def get_json(url: str, headers: dict[str, str], *, label: str):
    """GET ``url`` as JSON; raises :class:`CatalogError` carrying the
    vendor's ``error.message`` (or the HTTP status) on failure."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - best-effort error text
            pass
        detail = ""
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or "")
                elif isinstance(error, str):
                    detail = error
                if not detail:
                    detail = str(parsed.get("message") or "")
            detail = detail or body[:200]
        raise CatalogError(f"HTTP {exc.code}" + (f": {detail}" if detail else "")) from exc
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise CatalogError(str(exc) or exc.__class__.__name__) from exc


def validate_cached_entry(raw) -> dict | None:
    """Validate one ALREADY-normalized cache entry (the cache stores the
    output of a module's normalizer, so it is not normalized again)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        return None
    pricing = raw.get("pricing")
    if not (isinstance(pricing, dict) and "prompt" in pricing and "completion" in pricing):
        pricing = None
    name = raw.get("name")
    context_length = raw.get("context_length")
    max_completion = raw.get("max_completion_tokens")
    capabilities = raw.get("capabilities")
    entry = {
        "id": raw["id"],
        "name": name if isinstance(name, str) and name else raw["id"],
        "context_length": int(context_length) if isinstance(context_length, (int, float)) else None,
        "max_completion_tokens": (
            int(max_completion) if isinstance(max_completion, (int, float)) else None
        ),
        "pricing": pricing,
        "reasoning": raw.get("reasoning") if isinstance(raw.get("reasoning"), dict) else None,
        "detail": raw.get("detail") if isinstance(raw.get("detail"), str) else "",
        "capabilities": (
            [c for c in capabilities if isinstance(c, str)] if isinstance(capabilities, list) else []
        ),
    }
    if raw.get("registry_id") and isinstance(raw["registry_id"], str):
        entry["registry_id"] = raw["registry_id"]
    return entry


def read_cache(path: Path, *, label: str) -> dict | None:
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Unreadable %s catalog cache: %s", label, path)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    models = [m for m in (validate_cached_entry(x) for x in data["models"]) if m]
    return {"fetched_at": float(fetched_at), "models": models}


def write_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def serve_catalog(
    path: Path,
    fetch: Callable[[], list[dict]],
    *,
    refresh: bool,
    label: str,
) -> dict:
    """Return ``{"models", "fetched_at", "stale", "error"}`` (blocking).

    Serves the cache at ``path`` while it is younger than the TTL,
    otherwise (or on ``refresh``) calls ``fetch`` -- which returns the
    normalized model list or raises :class:`CatalogError`. A failed fetch
    returns whatever cache exists (``stale: True``) with the error text;
    with no cache at all ``models`` is empty. Call via ``asyncio.to_thread``
    from request handlers.
    """
    cache = read_cache(path, label=label)
    fresh = cache is not None and (time.time() - cache["fetched_at"]) < CATALOG_TTL_SECONDS
    if fresh and not refresh:
        return {**cache, "stale": False, "error": None}
    try:
        models = fetch()
    except CatalogError as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("%s catalog fetch failed: %s", label, error)
        if cache is None:
            return {"fetched_at": None, "models": [], "stale": True, "error": error}
        return {**cache, "stale": True, "error": error}
    cache = {"fetched_at": time.time(), "models": models}
    try:
        write_cache(path, cache)
    except OSError:
        logger.warning("Failed to write %s catalog cache", label, exc_info=True)
    return {**cache, "stale": False, "error": None}
