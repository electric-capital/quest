"""Upstream access for the vulnerability data plugin (plugin module).

Three free, public, unauthenticated sources, each read over HTTPS with no
credentials:

- **OSV.dev** (``api.osv.dev``): the open-source vulnerability database
  that merges GitHub Security Advisories, PyPA, RustSec, the Go
  vulnerability database, distro trackers and more under one schema.
  ``POST /v1/querybatch`` answers which advisories affect a package version
  (ids only); ``GET /v1/vulns/{id}`` returns one full record.
- **CISA KEV**: the Known Exploited Vulnerabilities catalog, one JSON feed
  (~2 MB, updated a few times a week) of CVEs exploited in the wild. It has
  no query API, so it is fetched whole and cached in-process for
  ``KEV_TTL_SECONDS``.
- **FIRST EPSS** (``api.first.org/data/v1/epss``): the daily probability
  (and percentile) that a CVE sees exploitation activity in the next 30
  days.

Every helper takes the caller's ``httpx.AsyncClient`` (``make_client()``;
tests pass one over an ``httpx.MockTransport``) and raises
:class:`VulnsError` on transport failures and non-2xx answers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

OSV_API = "https://api.osv.dev"
KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)
EPSS_API = "https://api.first.org/data/v1/epss"

USER_AGENT = "Quest/1.0"
_HTTP_TIMEOUT = 30.0

# OSV accepts up to 1000 queries per querybatch call.
OSV_BATCH_MAX = 1000
# Concurrent GET /v1/vulns/{id} fetches per tool call.
OSV_FETCH_CONCURRENCY = 8
# Follow-up querybatch rounds for results that carry a next_page_token
# (OSV pages at 1000 ids per query; only distro kernels get that far).
OSV_MAX_PAGE_ROUNDS = 3
# Digest cache entries (one per OSV record id + modified time).
OSV_CACHE_SIZE = 4000
# CVE ids per EPSS request (the API's default page size).
EPSS_CHUNK = 100
KEV_TTL_SECONDS = 6 * 3600

_SOURCE_NAMES = {"osv": "OSV.dev", "kev": "The CISA KEV feed", "epss": "The FIRST EPSS API"}
_BATCH_INDEX_RE = re.compile(r"error in query at index (\d+)")


class VulnsError(RuntimeError):
    """An upstream request failed (transport error, non-2xx, bad payload).

    ``source`` is ``"osv"``, ``"kev"`` or ``"epss"``.
    """

    def __init__(self, message: str, *, source: str,
                 code: str = "vulns_upstream_error",
                 status_code: int | None = None):
        super().__init__(message)
        self.source = source
        self.code = code
        self.status_code = status_code


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """One client per tool call; ``transport`` is the test seam."""
    kwargs: dict[str, Any] = {
        "timeout": _HTTP_TIMEOUT,
        "headers": {"User-Agent": USER_AGENT, "Accept": "application/json"},
        "follow_redirects": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def _error_text(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    text = (resp.text or "").strip()
    return text[:300] if text else f"HTTP {resp.status_code}"


async def _request_json(client: httpx.AsyncClient, method: str, url: str, *,
                        source: str, params: dict | None = None,
                        json_body: Any = None, not_found_ok: bool = False) -> Any:
    """One request; the JSON body, or None for an allowed 404."""
    name = _SOURCE_NAMES[source]
    try:
        resp = await client.request(method, url, params=params, json=json_body)
    except httpx.TimeoutException as exc:
        raise VulnsError(f"{name} timed out: {exc}", source=source,
                         code="vulns_timeout") from exc
    except httpx.HTTPError as exc:
        raise VulnsError(f"{name} could not be reached: {exc}", source=source,
                         code="vulns_unreachable") from exc
    if resp.status_code == 404 and not_found_ok:
        return None
    if resp.status_code == 429:
        raise VulnsError(f"{name} rate limit reached; try again shortly.",
                         source=source, code="vulns_rate_limited", status_code=429)
    if resp.status_code >= 400:
        raise VulnsError(f"{name} returned HTTP {resp.status_code}: {_error_text(resp)}",
                         source=source, status_code=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise VulnsError(f"{name} returned a non-JSON response.", source=source) from exc


# ---------------------------------------------------------------------------
# OSV
# ---------------------------------------------------------------------------

async def osv_query_batch(client: httpx.AsyncClient, queries: list[dict], *,
                          max_ids_per_query: int) -> list[dict]:
    """Run OSV queries; per query ``{"vulns": [{id, modified}], "more": bool}``
    or ``{"error": message}``.

    OSV rejects a whole batch when one query is invalid (an unknown
    ecosystem, a malformed purl) and names its index, so that query is
    marked failed and the rest re-sent. A result with a ``next_page_token``
    is followed up for ``OSV_MAX_PAGE_ROUNDS`` rounds while the query has
    fewer than ``max_ids_per_query`` ids; ``more`` says ids were left
    unread.
    """
    results: list[dict] = [{"vulns": [], "more": False} for _ in queries]
    pending = list(range(len(queries)))  # indexes still to (re)send
    page_tokens: dict[int, str] = {}
    rounds = 0
    while pending:
        for start in range(0, len(pending), OSV_BATCH_MAX):
            chunk = pending[start:start + OSV_BATCH_MAX]
            while chunk:
                body = {"queries": [
                    {**queries[i], **({"page_token": page_tokens[i]} if i in page_tokens else {})}
                    for i in chunk
                ]}
                try:
                    payload = await _request_json(
                        client, "POST", f"{OSV_API}/v1/querybatch",
                        source="osv", json_body=body,
                    )
                except VulnsError as exc:
                    match = _BATCH_INDEX_RE.search(str(exc))
                    if exc.status_code != 400 or not match or int(match.group(1)) >= len(chunk):
                        raise
                    bad = chunk[int(match.group(1))]
                    results[bad] = {"error": _query_error_text(str(exc))}
                    page_tokens.pop(bad, None)
                    chunk = [i for i in chunk if i != bad]
                    continue
                rows = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(rows, list) or len(rows) != len(chunk):
                    raise VulnsError("OSV.dev returned an unexpected querybatch shape.",
                                     source="osv")
                for i, row in zip(chunk, rows):
                    vulns = row.get("vulns") if isinstance(row, dict) else None
                    for vuln in vulns or []:
                        if isinstance(vuln, dict) and isinstance(vuln.get("id"), str):
                            results[i]["vulns"].append(
                                {"id": vuln["id"], "modified": vuln.get("modified")}
                            )
                    token = row.get("next_page_token") if isinstance(row, dict) else None
                    if token:
                        page_tokens[i] = token
                    else:
                        page_tokens.pop(i, None)
                break
        rounds += 1
        pending = []
        for i in list(page_tokens):
            if rounds < OSV_MAX_PAGE_ROUNDS and len(results[i]["vulns"]) < max_ids_per_query:
                pending.append(i)
            else:
                results[i]["more"] = True
                page_tokens.pop(i)
    return results


def _query_error_text(message: str) -> str:
    """The useful tail of OSV's per-query error ("... desc = invalid ecosystem")."""
    _, _, tail = message.rpartition("desc = ")
    return (tail or message).strip()


async def osv_get_vuln(client: httpx.AsyncClient, vuln_id: str) -> dict | None:
    """One full OSV record, or None when OSV has no such id."""
    payload = await _request_json(
        client, "GET", f"{OSV_API}/v1/vulns/{quote(vuln_id, safe=':')}",
        source="osv", not_found_ok=True,
    )
    if payload is not None and not isinstance(payload, dict):
        raise VulnsError("OSV.dev returned an unexpected record shape.", source="osv")
    return payload


class DigestCache:
    """An LRU of compact OSV record digests keyed by ``(id, modified)``.

    A changed record has a new ``modified`` time in the querybatch answer,
    so a stale digest is never served for it.
    """

    def __init__(self, size: int):
        self._size = size
        self._items: OrderedDict[tuple[str, str | None], dict] = OrderedDict()

    def get(self, key: tuple[str, str | None]) -> dict | None:
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, key: tuple[str, str | None], value: dict) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


DIGEST_CACHE = DigestCache(OSV_CACHE_SIZE)


async def osv_get_digests(client: httpx.AsyncClient, refs: list[dict], digest) -> dict[str, dict]:
    """Digests for ``refs`` (``{id, modified}`` from querybatch) by id.

    ``digest`` turns a full record into its compact form (see
    plugins/vulns/osv.py). Cached digests are reused; the rest are fetched
    ``OSV_FETCH_CONCURRENCY`` at a time. An id OSV no longer serves is left
    out.
    """
    out: dict[str, dict] = {}
    missing: list[dict] = []
    for ref in refs:
        cached = DIGEST_CACHE.get((ref["id"], ref.get("modified")))
        if cached is not None:
            out[ref["id"]] = cached
        else:
            missing.append(ref)
    semaphore = asyncio.Semaphore(OSV_FETCH_CONCURRENCY)

    async def fetch(ref: dict) -> None:
        async with semaphore:
            record = await osv_get_vuln(client, ref["id"])
        if record is None:
            return
        item = digest(record)
        DIGEST_CACHE.put((ref["id"], ref.get("modified")), item)
        out[ref["id"]] = item

    await asyncio.gather(*(fetch(ref) for ref in missing))
    return out


# ---------------------------------------------------------------------------
# CISA KEV
# ---------------------------------------------------------------------------

class KevCache:
    """The parsed KEV catalog, refreshed when older than ``KEV_TTL_SECONDS``."""

    def __init__(self):
        self.catalog: dict | None = None
        self.fetched_at: float = 0.0

    def clear(self) -> None:
        self.catalog = None
        self.fetched_at = 0.0


KEV_CACHE = KevCache()


def _parse_kev(payload: Any) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("vulnerabilities"), list):
        raise VulnsError("The CISA KEV feed has an unexpected shape.", source="kev")
    entries = [v for v in payload["vulnerabilities"]
               if isinstance(v, dict) and isinstance(v.get("cveID"), str)]
    return {
        "catalog_version": payload.get("catalogVersion"),
        "date_released": payload.get("dateReleased"),
        "entries": entries,
        "by_cve": {v["cveID"].upper(): v for v in entries},
    }


async def kev_catalog(client: httpx.AsyncClient) -> dict:
    """The KEV catalog (cached). ``stale`` is set when a refresh failed and
    the previous copy is served instead."""
    now = time.monotonic()
    cached = KEV_CACHE.catalog
    if cached is not None and now - KEV_CACHE.fetched_at < KEV_TTL_SECONDS:
        return cached
    try:
        payload = await _request_json(client, "GET", KEV_FEED_URL, source="kev")
        catalog = _parse_kev(payload)
    except VulnsError as exc:
        if cached is None:
            raise
        logger.warning("KEV refresh failed, serving the cached catalog: %s", exc)
        return {**cached, "stale": True}
    KEV_CACHE.catalog = catalog
    KEV_CACHE.fetched_at = now
    return catalog


# ---------------------------------------------------------------------------
# FIRST EPSS
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def epss_scores(client: httpx.AsyncClient, cves: list[str], *,
                      date: str | None = None, history: bool = False) -> dict[str, dict]:
    """EPSS rows by CVE id: ``{epss, percentile, date}`` (+ ``history``, a
    newest-first list of the same, when asked). CVEs EPSS does not score
    are absent."""
    out: dict[str, dict] = {}
    unique = list(dict.fromkeys(c.upper() for c in cves))
    for start in range(0, len(unique), EPSS_CHUNK):
        chunk = unique[start:start + EPSS_CHUNK]
        params: dict[str, Any] = {"cve": ",".join(chunk), "limit": EPSS_CHUNK}
        if date:
            params["date"] = date
        if history:
            params["scope"] = "time-series"
        payload = await _request_json(client, "GET", EPSS_API, source="epss", params=params)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise VulnsError("The FIRST EPSS API returned an unexpected shape.", source="epss")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("cve"), str):
                continue
            item = {
                "epss": _to_float(row.get("epss")),
                "percentile": _to_float(row.get("percentile")),
                "date": row.get("date"),
            }
            if history and isinstance(row.get("time-series"), list):
                item["history"] = [
                    {"date": point.get("date"), "epss": _to_float(point.get("epss")),
                     "percentile": _to_float(point.get("percentile"))}
                    for point in row["time-series"] if isinstance(point, dict)
                ]
            out[row["cve"].upper()] = item
    return out
