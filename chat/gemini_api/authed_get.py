"""Handler for authed_get: authenticated GET requests to external APIs.

Makes real HTTP requests to upstream external APIs (not localhost proxy
endpoints) with automatic credential injection.  The handler inspects the
target URL, matches it against a registry of known services, loads the
appropriate API key / token, and injects it into the outgoing request.

Service registry design:
    Each entry maps a hostname (optionally scoped by path prefix) to a
    service descriptor containing a human-readable name, a credential
    loader function, and an auth injection function.  Services that
    require per-user OAuth credentials set ``requires_user: True`` and
    provide an async credential loader that accepts the user dict.
    Adding a new service requires only a new entry in _SERVICE_REGISTRY.
"""

import hashlib
import inspect
import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from auth.config import load_coingecko_api_key
from auth.google_credentials import get_valid_service_credentials
from auth.session import get_current_user_cookie_or_apikey

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------


def _inject_coingecko_auth(api_key: str, headers: dict[str, str]) -> None:
    """Inject CoinGecko Pro API key as a request header."""
    headers["x-cg-pro-api-key"] = api_key


def _inject_google_bearer_auth(credentials, headers: dict[str, str]) -> None:
    """Inject Google OAuth Bearer token into request headers."""
    headers["Authorization"] = f"Bearer {credentials.token}"


def _inject_airtable_bearer_auth(token: str, headers: dict[str, str]) -> None:
    """Inject Airtable PAT as a Bearer token into request headers."""
    headers["Authorization"] = f"Bearer {token}"


def _inject_ramp_bearer_auth(token: str, headers: dict[str, str]) -> None:
    """Inject Ramp OAuth access token as a Bearer token into request headers."""
    headers["Authorization"] = f"Bearer {token}"


def _inject_no_auth(_credentials, _headers: dict[str, str]) -> None:
    """No-op auth injector for unauthenticated services (e.g. Federal Register).

    ``inject_auth`` is always invoked by ``_make_authed_request`` even when the
    loaded credential is ``None``; a no-auth service supplies this injector so
    that call is harmless. Both arguments are accepted and ignored.
    """
    return None


async def _load_google_services_credentials(user: dict):
    """Load Google Services OAuth credentials for the given user.

    Returns a google.oauth2.credentials.Credentials object, or None if
    the user has not connected Google Services.  The same credentials
    work for all Google APIs (Calendar, Drive, etc.).
    """
    return await get_valid_service_credentials(user)


async def _load_airtable_credentials(user: dict):
    """Load Airtable Personal Access Token from the user dict.

    Returns the PAT string, or None if the user has not configured
    an Airtable token in Settings > Data Connections.
    """
    return user.get("airtable_token")


async def _load_ramp_credentials(user: dict):
    """Load a valid Ramp OAuth access token for the given user.

    Delegates to ``api.ramp.get_ramp_token``, which proactively refreshes the
    token when it is expired or near expiry (Ramp access tokens expire and
    refresh tokens rotate on every use). Returns None when Ramp is not
    connected or the refresh fails, so the registry entry's
    ``missing_credentials_error`` surfaces an actionable reconnect message
    instead of a raw exception.
    """
    from fastapi import HTTPException

    from api.ramp import get_ramp_token

    try:
        return await get_ramp_token(user)
    except HTTPException:
        return None


def _load_federal_register_credentials() -> None:
    """Load credentials for the Federal Register API: there are none.

    The Federal Register API is a free, public, unauthenticated US government
    API. Modelled as a no-auth service: a sync, no-arg loader returning ``None``
    (matching ``load_coingecko_api_key``'s call shape). ``requires_user`` is NOT
    set on the registry entry, so ``_make_authed_request`` invokes this with no
    args and a ``None`` return flows past the missing-credentials guard.
    """
    return None


def _load_sec_edgar_credentials() -> None:
    """Load credentials for the SEC EDGAR data API: there are none.

    The SEC EDGAR data API (host ``data.sec.gov``) is a free, public,
    unauthenticated US government API. Modelled as a no-auth service: a sync,
    no-arg loader returning ``None`` (matching ``load_coingecko_api_key``'s call
    shape). ``requires_user`` is NOT set on the registry entry, so
    ``_make_authed_request`` invokes this with no args and a ``None`` return
    flows past the missing-credentials guard. SEC's fair-access policy requires
    a descriptive ``User-Agent`` on every request; that is supplied via the
    entry's ``default_headers``, not here.
    """
    return None


# Ramp developer-API resources reachable read-only via authed_get. Each name
# expands to a regex allowing that resource root and any sub-path beneath it
# (list + detail + nested reads, UUID id segments). Only GETs consult
# allowed_endpoints and the Ramp entry defines no allowed_post_endpoints, so
# this surface is read-only by construction. "cards" is NOT in this list --
# its physical/virtual reads are allow-listed explicitly below so the
# card-vault endpoints (/cards/vault/..., full card numbers behind the
# cards:read_vault scope) and the sibling /vault/... root stay unreachable.
_RAMP_READ_RESOURCES = (
    "accounting",
    "audit-logs",
    "bank-accounts",
    "banking",
    "bills",
    "business",
    "cashbacks",
    "custom-records",
    "departments",
    "entities",
    "funds",
    "item-receipts",
    "limits",
    "locations",
    "memos",
    "merchants",
    "purchase-orders",
    "receipts",
    "reimbursements",
    "repayments",
    "spend-programs",
    "statements",
    "transactions",
    "transfers",
    "trips",
    "unified-requests",
    "users",
    "vendors",
)


# Each entry: hostname -> {name, load_credentials, inject_auth, ...}
#
# Services scoped to a path prefix include a ``path_prefix`` field so
# that the registry lookup can distinguish between different APIs on the
# same hostname (e.g. www.googleapis.com hosts Calendar, Drive, etc.).
_SERVICE_REGISTRY: dict[str, dict[str, Any]] = {
    "pro-api.coingecko.com": {
        "name": "CoinGecko Pro",
        "load_credentials": load_coingecko_api_key,
        "inject_auth": _inject_coingecko_auth,
    },
    "www.googleapis.com/calendar/v3": {
        "name": "Google Calendar",
        "path_prefix": "/calendar/v3",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/calendar/v3/users/me/calendarList$",           # list calendars
            r"^/calendar/v3/users/me/calendarList/[^/]+$",     # get a specific calendar
            r"^/calendar/v3/calendars/[^/]+/events$",          # list events
            r"^/calendar/v3/calendars/[^/]+/events/[^/]+$",    # get a specific event
        ],
    },
    "www.googleapis.com/drive/v3": {
        "name": "Google Drive",
        "path_prefix": "/drive/v3",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/drive/v3/files$",          # list files
            r"^/drive/v3/files/[^/]+$",    # get a specific file's metadata (or alt=media download)
            # export a Google Workspace file (google_export_doc / _sheet / _slides tools)
            r"^/drive/v3/files/[^/]+/export$",
            r"^/drive/v3/files/[^/]+/permissions$",         # who has access
            r"^/drive/v3/files/[^/]+/permissions/[^/]+$",   # one permission
            r"^/drive/v3/files/[^/]+/revisions$",           # version history
            r"^/drive/v3/files/[^/]+/revisions/[^/]+$",     # one revision's metadata
            r"^/drive/v3/files/[^/]+/comments$",            # comment threads
            r"^/drive/v3/files/[^/]+/comments/[^/]+$",      # one comment
            r"^/drive/v3/files/[^/]+/comments/[^/]+/replies$",        # replies on a comment
            r"^/drive/v3/files/[^/]+/comments/[^/]+/replies/[^/]+$",  # one reply
            r"^/drive/v3/changes$",        # what changed since a page token
            r"^/drive/v3/changes/startPageToken$",  # current change cursor
            r"^/drive/v3/about$",          # storage quota + connected account
            r"^/drive/v3/drives$",         # list shared drives
            r"^/drive/v3/drives/[^/]+$",   # one shared drive
        ],
    },
    "docs.googleapis.com": {
        "name": "Google Docs",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/v1/documents/[^/]+$",    # get a document by ID
        ],
    },
    "sheets.googleapis.com": {
        "name": "Google Sheets",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/v4/spreadsheets/[^/]+$",                  # get spreadsheet metadata
            r"^/v4/spreadsheets/[^/]+/values/.+$",         # get values from a range (range can contain special chars)
            r"^/v4/spreadsheets/[^/]+/values:batchGet$",   # batch get values
        ],
    },
    "slides.googleapis.com": {
        "name": "Google Slides",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            # Exclude ':' in the id segments so write verbs like
            # ``/v1/presentations/{id}:batchUpdate`` are NOT matched (read-only).
            r"^/v1/presentations/[^/:]+$",                 # get a presentation by ID
            r"^/v1/presentations/[^/:]+/pages/[^/:]+$",    # get one page (slide) by object ID
        ],
    },
    "gmail.googleapis.com": {
        "name": "Gmail",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/gmail/v1/users/me/messages$",                          # list/search messages
            r"^/gmail/v1/users/me/messages/[^/]+$",                    # get a specific message
            r"^/gmail/v1/users/me/messages/[^/]+/attachments/[^/]+$",  # get a specific attachment
            r"^/gmail/v1/users/me/threads$",                           # list/search threads
            r"^/gmail/v1/users/me/threads/[^/]+$",                     # get a specific thread
            r"^/gmail/v1/users/me/labels$",                            # list all labels
            r"^/gmail/v1/users/me/labels/[^/]+$",                      # get a specific label
        ],
    },
    "tasks.googleapis.com": {
        "name": "Google Tasks",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/tasks/v1/users/@me/lists$",            # list all task lists
            r"^/tasks/v1/users/@me/lists/[^/]+$",      # get a specific task list
            r"^/tasks/v1/lists/[^/]+/tasks$",          # list tasks in a task list
            r"^/tasks/v1/lists/[^/]+/tasks/[^/]+$",    # get a specific task
        ],
    },
    # --- Google Cloud Platform (GCP) -- read-only via per-user Google OAuth ---
    # All four GCP hosts share the Google Services loader/injector (same Bearer
    # token, granted the full cloud-platform scope -- Compute/GKE reject the
    # read-only variant; read-only is enforced HERE at the app layer by these
    # allow-lists, not by the OAuth scope). GET allow-lists use
    # ``[^/:]+`` for id segments (NOT ``[^/]+``) to exclude the ':' character,
    # which is how GCP encodes custom/write methods (e.g. ``instances/{id}:stop``,
    # ``clusters/{id}:setMasterAuth``), keeping the GET path read-only. A few
    # read-shaped POST verbs (``organizations:search``, ``entries:list``, and
    # the project ``:getIamPolicy`` pair) are exposed via the separate
    # ``allowed_post_endpoints`` dimension, reachable only through the
    # authed_post tool -- never through authed_get.
    "cloudresourcemanager.googleapis.com": {
        "name": "Google Cloud Resource Manager",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/v1/organizations/[^/:]+$",   # get one organization by id (GET)
            r"^/v1/projects$",               # list projects (supports filter=)
            r"^/v1/projects/[^/:]+$",        # get one project by id (v1)
            r"^/v3/projects/[^/:]+$",        # get one project by id (v3 resource shape)
            r"^/v3/folders/[^/:]+$",         # get one folder (context)
        ],
        "allowed_post_endpoints": [
            # Search/list organizations (POST; optional {query} JSON body). The
            # canonical way to discover org ids. Reachable only via authed_post.
            r"^/v1/organizations:search$",
            # Project-level IAM policy read (POST; body carries only
            # GetPolicyOptions -- the verb RETURNS the policy and cannot carry
            # one, so it cannot mutate; the mutating verb is the separate
            # ``:setIamPolicy`` literal, which these anchors never match).
            r"^/v1/projects/[^/:]+:getIamPolicy$",
            r"^/v3/projects/[^/:]+:getIamPolicy$",
        ],
    },
    "compute.googleapis.com": {
        "name": "Google Compute Engine",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/compute/v1/projects/[^/:]+/zones/[^/:]+/instances$",          # list instances in a zone
            r"^/compute/v1/projects/[^/:]+/zones/[^/:]+/instances/[^/:]+$",   # get one instance
            # Instance-level IAM policy read. Compute encodes getIamPolicy as a
            # plain GET path segment (no ':'); the mutating setIamPolicy is a
            # POST on a different segment and stays unreachable.
            r"^/compute/v1/projects/[^/:]+/zones/[^/:]+/instances/[^/:]+/getIamPolicy$",
            r"^/compute/v1/projects/[^/:]+/aggregated/instances$",            # list instances across all zones
            r"^/compute/v1/projects/[^/:]+/zones/[^/:]+/disks$",              # list disks in a zone
            r"^/compute/v1/projects/[^/:]+/zones/[^/:]+/disks/[^/:]+$",       # get one disk
            r"^/compute/v1/projects/[^/:]+/aggregated/disks$",                # list disks across all zones (find orphaned disks)
            r"^/compute/v1/projects/[^/:]+/zones$",                           # list zones (context)
            r"^/compute/v1/projects/[^/:]+/regions$",                         # list regions (context)
            r"^/compute/v1/projects/[^/:]+/zones/[^/:]+$",                    # get a zone (context)
            r"^/compute/v1/projects/[^/:]+/regions/[^/:]+$",                  # get a region (context)
        ],
    },
    "container.googleapis.com": {
        "name": "Google Kubernetes Engine",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            r"^/v1/projects/[^/:]+/locations/[^/:]+/clusters$",                          # list clusters (loc may be '-' for all)
            r"^/v1/projects/[^/:]+/locations/[^/:]+/clusters/[^/:]+$",                   # get one cluster
            r"^/v1/projects/[^/:]+/locations/[^/:]+/clusters/[^/:]+/nodePools$",         # list node pools
            r"^/v1/projects/[^/:]+/locations/[^/:]+/clusters/[^/:]+/nodePools/[^/:]+$",  # get one node pool
        ],
    },
    "logging.googleapis.com": {
        "name": "Google Cloud Logging",
        "load_credentials": _load_google_services_credentials,
        "inject_auth": _inject_google_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "allowed_endpoints": [
            # Metadata reads only (none return log lines); ':'-excluded so the
            # GET path can never reach entries:list.
            r"^/v2/projects/[^/:]+/logs$",            # list log names
            r"^/v2/projects/[^/:]+/sinks$",           # list sinks
            r"^/v2/projects/[^/:]+/sinks/[^/:]+$",    # get one sink
            r"^/v2/projects/[^/:]+/metrics$",         # list log-based metrics
            r"^/v2/projects/[^/:]+/metrics/[^/:]+$",  # get one log-based metric
        ],
        "allowed_post_endpoints": [
            # Read log entries (POST; required JSON body with resourceNames +
            # filter + orderBy + pageSize). The ONLY POST verb allow-listed for
            # this host -- entries:write and other write verbs are unreachable.
            r"^/v2/entries:list$",
        ],
    },
    "api.airtable.com": {
        "name": "Airtable",
        "load_credentials": _load_airtable_credentials,
        "inject_auth": _inject_airtable_bearer_auth,
        "requires_user": True,
        "missing_credentials_error": {
            "error": "airtable_token_required",
            "message": "Airtable Personal Access Token not configured. Please add your token in Settings > Data Connections.",
        },
        "allowed_endpoints": [
            r"^/v0/meta/bases$",                     # list bases
            r"^/v0/meta/bases/[^/]+/tables$",        # get base schema
            r"^/v0/[^/]+/[^/]+$",                    # list records from a table
            r"^/v0/[^/]+/[^/]+/[^/]+$",              # get a single record
            r"^/v0/[^/]+/[^/]+/[^/]+/comments$",     # list comments on a record
        ],
    },
    # The GitHub entry ("api.github.com") is plugin-registered
    # (plugins/github) via register_service().
    # Federal Register API: free, public, UNAUTHENTICATED US government API.
    # Path-scoped to /api/v1 so authed_get cannot proxy arbitrary
    # www.federalregister.gov (human website) URLs. No-auth service: the loader
    # returns None and the injector is a no-op (requires_user left unset so the
    # None credential is not treated as an error).
    "www.federalregister.gov/api/v1": {
        "name": "Federal Register",
        "path_prefix": "/api/v1",
        "load_credentials": _load_federal_register_credentials,
        "inject_auth": _inject_no_auth,
        # The API host serves bot user agents fine, but a UA is polite and
        # de-risks any future bot filtering (the human site blocks some bots).
        "default_headers": {
            "User-Agent": "Quest/1.0",
        },
        "allowed_endpoints": [
            r"^/api/v1/documents(\.json|\.csv)?$",                       # documents search
            r"^/api/v1/documents/facets/[^/]+$",                         # document facets
            r"^/api/v1/documents/[^/]+(\.json|\.csv)?$",                 # single or comma-joined multi document
            r"^/api/v1/public-inspection-documents(\.json)?$",           # public-inspection search
            r"^/api/v1/public-inspection-documents/current(\.json)?$",   # current public-inspection docs
            r"^/api/v1/public-inspection-documents/[^/]+(\.json)?$",      # single (or multi) public-inspection doc
            r"^/api/v1/agencies(\.json)?$",                              # agencies list
            r"^/api/v1/agencies/[^/]+$",                                 # single agency (slug or numeric id)
            r"^/api/v1/suggested_searches$",                            # suggested searches list
            r"^/api/v1/suggested_searches/[^/]+$",                      # single suggested search
        ],
    },
    # SEC EDGAR data API: free, public, UNAUTHENTICATED US government API on the
    # dedicated data host data.sec.gov. No-auth service: the loader returns None
    # and the injector is a no-op (requires_user left unset so the None
    # credential is not treated as an error). A plain hostname key (no
    # path_prefix) is used because data.sec.gov serves multiple distinct path
    # roots (/submissions/, /api/xbrl/...); path gating is done entirely by the
    # allowed_endpoints regexes. The ticker->CIK file company_tickers.json lives
    # on www.sec.gov, which is intentionally NOT registered.
    "data.sec.gov": {
        "name": "SEC EDGAR",
        "load_credentials": _load_sec_edgar_credentials,
        "inject_auth": _inject_no_auth,
        # SEC's fair-access policy returns HTTP 403 to requests lacking a
        # descriptive User-Agent (format: name + contact). This is injected as a
        # default header (below caller headers in precedence), mirroring GitHub
        # and Federal Register. Includes a contact token per SEC policy.
        "default_headers": {
            "User-Agent": "Quest/1.0 (admin@quest.example)",
        },
        "allowed_endpoints": [
            # CIK is 10-digit zero-padded (e.g. CIK0000320193).
            r"^/submissions/CIK\d{10}\.json$",                                  # primary entity submission/filing history
            r"^/submissions/CIK\d{10}-submissions-\d+\.json$",                  # older-filings spillover for large filers
            r"^/api/xbrl/companyconcept/CIK\d{10}/[^/]+/[^/]+\.json$",          # one concept's time series for one entity
            r"^/api/xbrl/companyfacts/CIK\d{10}\.json$",                        # all XBRL facts for one entity (large)
            r"^/api/xbrl/frames/[^/]+/[^/]+/[^/]+/CY\d{4}(Q[1-4]I?)?\.json$",   # one concept+unit across entities for a period
        ],
    },
    # SEC EDGAR ticker->CIK map files on the www.sec.gov host. The submissions
    # and XBRL calls above require a 10-digit CIK, but the only public ticker->CIK
    # mapping lives in two static JSON files on www.sec.gov. This entry narrowly
    # allow-lists EXACTLY those two files so the agent can resolve a ticker to a
    # CIK itself. Same no-auth modeling as data.sec.gov: None loader, no-op
    # injector, requires_user unset, and the SEC User-Agent default header. The
    # rest of www.sec.gov (the human site, /cgi-bin/browse-edgar, other /files/)
    # remains NOT proxied -- the allowed_endpoints regexes match only these two.
    "www.sec.gov": {
        "name": "SEC EDGAR (files)",
        "load_credentials": _load_sec_edgar_credentials,
        "inject_auth": _inject_no_auth,
        # Same SEC fair-access User-Agent requirement as data.sec.gov.
        "default_headers": {
            "User-Agent": "Quest/1.0 (admin@quest.example)",
        },
        "allowed_endpoints": [
            r"^/files/company_tickers\.json$",           # ticker -> CIK + company name
            r"^/files/company_tickers_exchange\.json$",  # same, with exchange info
        ],
    },
    # Ramp developer API: per-user OAuth Bearer token. Ramp access tokens
    # expire and refresh tokens rotate, so the loader proactively refreshes
    # (api.ramp.get_ramp_token) and retry_on_401 is set as a backstop.
    "api.ramp.com": {
        "name": "Ramp",
        "load_credentials": _load_ramp_credentials,
        "inject_auth": _inject_ramp_bearer_auth,
        "requires_user": True,
        "retry_on_401": True,
        "missing_credentials_error": {
            "error": "ramp_oauth_required",
            "message": (
                "Ramp not connected (or the connection expired). "
                "Please connect Ramp in Settings > Data Connections."
            ),
        },
        "allowed_endpoints": [
            # One pattern per read resource: the resource root plus any
            # sub-path of URL-safe segments (see _RAMP_READ_RESOURCES).
            *(
                rf"^/developer/v1/{resource}(/[A-Za-z0-9._~%\-]+)*$"
                for resource in _RAMP_READ_RESOURCES
            ),
            # Cards: physical/virtual reads only. The /cards/vault/... and
            # /vault/... card-number endpoints are deliberately absent.
            r"^/developer/v1/cards/physical$",           # list physical cards
            r"^/developer/v1/cards/physical/[^/]+$",     # get a physical card
            r"^/developer/v1/cards/virtual$",            # list virtual cards
            r"^/developer/v1/cards/virtual/[^/]+$",      # get a virtual card
        ],
    },
}


# ---------------------------------------------------------------------------
# Service registration (regex precompile at registration time)
# ---------------------------------------------------------------------------

def _compile_service_patterns(entry: dict[str, Any]) -> None:
    """Precompile an entry's allow-list regexes in place.

    POST verbs live in a SEPARATE allow-list dimension so a GET request can
    never reach a POST-only path and vice-versa (see authed_post design).
    """
    if "allowed_endpoints" in entry:
        entry["_allowed_endpoints"] = [
            re.compile(pattern) for pattern in entry["allowed_endpoints"]
        ]
    if "allowed_post_endpoints" in entry:
        entry["_allowed_post_endpoints"] = [
            re.compile(pattern) for pattern in entry["allowed_post_endpoints"]
        ]


def register_service(key: str, entry: dict[str, Any]) -> None:
    """Register an upstream service into the authed_get/authed_post registry.

    ``key`` is a hostname or ``hostname/path-prefix`` (in which case the
    entry must carry a matching ``path_prefix``). Used by the plugin loader;
    core services stay in the module-level literal above. Regexes are
    compiled here, at registration time.
    """
    if key in _SERVICE_REGISTRY:
        raise ValueError(f"authed_get service already registered: {key!r}")
    if not entry.get("name"):
        raise ValueError(f"authed_get service {key!r} entry must have a 'name'")
    if not callable(entry.get("load_credentials")) or not callable(entry.get("inject_auth")):
        raise ValueError(
            f"authed_get service {key!r} entry must have callable "
            "'load_credentials' and 'inject_auth'"
        )
    if "/" in key and not entry.get("path_prefix"):
        raise ValueError(
            f"authed_get service key {key!r} has a path component but the "
            "entry declares no 'path_prefix'"
        )
    _compile_service_patterns(entry)
    _SERVICE_REGISTRY[key] = entry


# Compile core entries' allow-list regexes at module load time.
for _service in _SERVICE_REGISTRY.values():
    _compile_service_patterns(_service)


# ---------------------------------------------------------------------------
# Registry lookup
# ---------------------------------------------------------------------------

def _find_service(hostname: str, path: str) -> dict[str, Any] | None:
    """Find a matching service in the registry by hostname and path.

    Checks path-prefix-scoped entries first (e.g.
    ``www.googleapis.com/calendar/v3``), then falls back to plain
    hostname entries.
    """
    # Try hostname + path prefix keys first (more specific match). The key's
    # host component must EQUAL the request hostname: a prefix comparison
    # would let a proper string prefix of a registered host (e.g.
    # ``www.googleapis.co``, or a bare ``www``) select a credentialed
    # service and receive its injected bearer token.
    for key, service in _SERVICE_REGISTRY.items():
        prefix = service.get("path_prefix")
        if not prefix:
            continue
        key_host = key.split("/", 1)[0]
        if key_host == hostname and path.startswith(prefix):
            return service

    # Fall back to plain hostname match
    return _SERVICE_REGISTRY.get(hostname)


# ---------------------------------------------------------------------------
# Caller header allow-list
# ---------------------------------------------------------------------------

# The ONLY request headers a caller (the model via the authed_get/authed_post
# tools, or sandbox code via the /api/authed-get|post proxy) may attach to an
# upstream request. Every other header is rejected before credential loading
# and network I/O.
#
# Caller headers ride on requests that carry the user's upstream credential
# (e.g. the broad cloud-platform Google OAuth bearer), so an untrusted header
# is an untrusted instruction to the provider under the user's identity.
# Providers interpret many headers as operation metadata or method controls:
# ``X-HTTP-Method-Override`` turns a nominally read-only verb into a write,
# ``X-Goog-User-Project`` re-bills quota to another project, and
# ``X-Goog-Request-Reason`` is persisted verbatim into the target project's
# Cloud Audit Logs (an exfiltration channel to an attacker-owned project). The
# path allow-lists cannot see any of that, so the header dimension gets its own
# allow-list: plain content negotiation only, lowercase for the
# case-insensitive comparison. Legitimate use today is the GitHub media-type
# ``Accept`` override (e.g. ``application/vnd.github.raw+json``).
_ALLOWED_CALLER_HEADERS: frozenset[str] = frozenset({
    "accept",
    "accept-language",
})


def _reject_disallowed_caller_headers(headers: dict[str, str] | None) -> str | None:
    """Return a JSON error string when *headers* contains a non-allow-listed name.

    Header names are compared case-insensitively against
    ``_ALLOWED_CALLER_HEADERS``. Returns ``None`` when every header is allowed
    (or there are none). Rejects rather than silently strips so the model gets
    an actionable error instead of a request that quietly ignored its input.
    """
    if not headers:
        return None
    disallowed = sorted(
        name for name in headers if str(name).lower() not in _ALLOWED_CALLER_HEADERS
    )
    if not disallowed:
        return None
    allowed = ", ".join(sorted(_ALLOWED_CALLER_HEADERS))
    return json.dumps({
        "error": (
            f"Header(s) not allowed: {', '.join(disallowed)}. "
            f"Only these caller-supplied headers are accepted: {allowed}. "
            "Authentication headers are injected automatically; do not "
            "include them."
        )
    })


# ---------------------------------------------------------------------------
# Internal request helper
# ---------------------------------------------------------------------------

async def _make_authed_request(
    url: str,
    headers: dict[str, str] | None = None,
    user: dict | None = None,
    *,
    raw_response: bool = False,
    method: str = "GET",
    json_body: dict | None = None,
) -> str | httpx.Response:
    """Core authenticated request logic shared by the tool handlers and HTTP endpoints.

    Handles both GET (``authed_get``) and the narrow set of read-shaped POST
    verbs (``authed_post``). *method* selects the verb; *json_body* is the JSON
    request body forwarded on POST requests (ignored on GET). Everything else --
    HTTPS validation, registry lookup, allow-list gating, credential loading +
    401 refresh, header precedence, error pass-through -- is identical across
    verbs.

    Endpoint allow-listing is verb-scoped and the two dimensions are STRICTLY
    independent: GET requests are validated against ``_allowed_endpoints`` and
    POST requests against ``_allowed_post_endpoints``. A GET can never reach a
    POST-only path and a POST can never reach a GET-only path. A host with no
    POST allow-list rejects all POSTs.

    When *raw_response* is ``False`` (the default), returns a JSON string
    (either the response body on success, or a JSON error object on failure).

    When *raw_response* is ``True``, returns the ``httpx.Response`` object
    directly on success so callers can access ``.content`` for binary data.
    On failure, still returns a JSON error string.

    Request header precedence (lowest priority first, highest last):
        1. Service ``default_headers`` (e.g. GitHub's ``User-Agent``).
        2. Caller-supplied ``headers`` argument -- can override service defaults,
           but ONLY names in ``_ALLOWED_CALLER_HEADERS`` (content negotiation)
           are accepted; anything else is rejected before any I/O.
        3. ``inject_auth`` result -- always wins so auth headers cannot be clobbered.
    """
    method = method.upper()
    # --- URL validation ---------------------------------------------------
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return json.dumps({"error": f"Invalid URL: {url}"})

    if parsed.scheme != "https":
        return json.dumps({
            "error": (
                "Only HTTPS URLs are supported for security. "
                f"Got scheme: '{parsed.scheme}'."
            )
        })

    hostname = parsed.hostname
    if not hostname:
        return json.dumps({"error": f"Could not extract hostname from URL: {url}"})

    service = _find_service(hostname, parsed.path)
    if service is None:
        supported = [
            f"  - {info['name']}: https://{host}/..."
            for host, info in _SERVICE_REGISTRY.items()
        ]
        return json.dumps({
            "error": (
                f"Unknown service host: '{hostname}'. "
                "Supported services:\n" + "\n".join(supported)
            )
        })

    # --- Caller header validation ------------------------------------------
    # Runs before credential loading and network I/O: a disallowed header
    # never reaches a provider under the user's credential (see
    # _ALLOWED_CALLER_HEADERS).
    header_error = _reject_disallowed_caller_headers(headers)
    if header_error is not None:
        return header_error

    # --- Endpoint path validation -----------------------------------------
    # Verb-scoped: GET validates against _allowed_endpoints, POST against the
    # independent _allowed_post_endpoints. A host exposing no allow-list for the
    # requested verb rejects the request (POST hosts without an
    # allowed_post_endpoints list reject all POSTs).
    if method == "POST":
        compiled_patterns = service.get("_allowed_post_endpoints")
        allowed_source_key = "allowed_post_endpoints"
    else:
        compiled_patterns = service.get("_allowed_endpoints")
        allowed_source_key = "allowed_endpoints"

    if method == "POST" and compiled_patterns is None:
        # No POST allow-list for this host -> all POSTs rejected.
        return json.dumps({
            "error": (
                f"POST requests are not allowed for {service['name']}. "
                "This service exposes no POST endpoints."
            )
        })

    if compiled_patterns is not None:
        if not any(pat.match(parsed.path) for pat in compiled_patterns):
            allowed_list = "\n".join(
                f"  - {p}" for p in service.get(allowed_source_key, [])
            )
            return json.dumps({
                "error": (
                    f"Endpoint path '{parsed.path}' is not allowed for "
                    f"{service['name']} ({method}). Allowed endpoint patterns:\n"
                    + allowed_list
                )
            })

    # --- Credential loading -----------------------------------------------
    requires_user = service.get("requires_user", False)
    if requires_user and user is None:
        return json.dumps({
            "error": (
                f"{service['name']} requires user authentication but no user "
                "context is available."
            )
        })

    try:
        loader = service["load_credentials"]
        if requires_user:
            credentials = await loader(user)
        elif inspect.iscoroutinefunction(loader):
            credentials = await loader()
        else:
            credentials = loader()
    except Exception:
        return json.dumps({
            "error": (
                f"Failed to load credentials for {service['name']}. "
                "Ensure credentials are configured correctly."
            )
        })

    if credentials is None and requires_user:
        error_info = service.get("missing_credentials_error", {
            "error": "google_services_auth_required",
            "message": (
                "Google services authorization required. "
                "Please visit Settings > Data Connections and connect Google Services."
            ),
        })
        return json.dumps({"error": error_info})

    # --- Auth injection ---------------------------------------------------
    # Build request headers with the documented precedence:
    #   service default_headers  <  caller headers  <  inject_auth
    service_default_headers = service.get("default_headers") or {}
    request_headers: dict[str, str] = dict(service_default_headers)
    if headers:
        request_headers.update(headers)
    service["inject_auth"](credentials, request_headers)

    # --- HTTP request -----------------------------------------------------
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "POST":
                response = await client.post(url, headers=request_headers, json=json_body)
            else:
                response = await client.get(url, headers=request_headers)

            # --- 401 retry for OAuth services -----------------------------
            if response.status_code == 401 and service.get("retry_on_401") and requires_user:
                logger.info(
                    "[authed_get] Got 401 from %s, refreshing credentials for %s",
                    service["name"],
                    user.get("email", "unknown"),
                )
                credentials = await service["load_credentials"](user)
                if credentials is None:
                    error_info = service.get("missing_credentials_error", {
                        "error": "google_services_auth_required",
                        "message": (
                            "Google services authorization required. "
                            "Please visit Settings > Data Connections and connect Google Services."
                        ),
                    })
                    return json.dumps({"error": error_info})
                # Re-inject auth with refreshed credentials, preserving
                # the same header precedence as the initial request.
                request_headers = dict(service_default_headers)
                if headers:
                    request_headers.update(headers)
                service["inject_auth"](credentials, request_headers)
                if method == "POST":
                    response = await client.post(url, headers=request_headers, json=json_body)
                else:
                    response = await client.get(url, headers=request_headers)

    except httpx.TimeoutException:
        return json.dumps({
            "error": f"Request to {service['name']} timed out after 30 seconds."
        })
    except Exception as exc:
        return json.dumps({
            "error": f"HTTP request to {service['name']} failed: {exc}"
        })

    # --- Response handling ------------------------------------------------
    if response.status_code >= 400:
        # Return the upstream error so the LLM can interpret it
        try:
            body = response.json()
        except Exception:
            body = response.text
        return json.dumps({
            "error": {
                "status_code": response.status_code,
                "service": service["name"],
                "response": body,
            }
        })

    if raw_response:
        return response

    # Return successful response body as-is (it's already JSON text for
    # most APIs).  We use response.text rather than re-serialising via
    # json.dumps to preserve the original formatting.
    return response.text


# ---------------------------------------------------------------------------
# Handler (tool-call path -- always returns a JSON string)
# ---------------------------------------------------------------------------


def _content_type_to_extension(content_type: str | None) -> str:
    """Pick a file extension based on the upstream Content-Type header.

    Used only when ``output_file`` resolves to a directory and we need to
    synthesize a filename. Conservative mapping: anything we do not
    recognise falls back to ``.bin`` so the receipt's ``filename`` is at
    least unambiguously "binary" rather than misleadingly named.
    """
    if not content_type:
        return ".bin"
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct == "application/json" or ct.endswith("+json"):
        return ".json"
    if ct == "text/html":
        return ".html"
    if ct == "text/csv":
        return ".csv"
    if ct == "application/xml" or ct == "text/xml" or ct.endswith("+xml"):
        return ".xml"
    if ct.startswith("text/"):
        return ".txt"
    return ".bin"


async def _handle_authed_get_to_file(
    *,
    url: str,
    headers: dict[str, str] | None,
    user: dict | None,
    conversation_id: str | None,
    project_id: str | None,
    output_file: str,
    method: str = "GET",
    json_body: dict | None = None,
) -> str:
    """Run the authed request and stream the body into the workspace.

    See ``handle_authed_get`` docstring for the contract. This branch is
    independent from the inline / blob-store paths and never touches the
    response size gate. Shared by ``authed_get`` (GET) and ``authed_post``
    (POST) via *method* / *json_body*.

    Note on size cap: matches ``download_drive_file`` /
    ``github_get_job_log`` -- no extra cap beyond
    the upstream-imposed limits and the in-process httpx 30s timeout.
    The user opted in by passing ``output_file``.
    """
    from chat.gemini_api.tool_handlers import (
        conversation_workspace_dir,
        _publish_file_list_changed,
        _sanitize_workspace_filename,
    )

    if conversation_id is None:
        return json.dumps({
            "error": (
                "Conversation context is not available; cannot write output_file."
            ),
        })

    # Pre-validate the path before issuing the HTTP request so a clearly
    # malformed output_file (absolute / traversal) does not waste an
    # upstream round-trip.
    candidate = Path(output_file)
    if candidate.is_absolute():
        return json.dumps({
            "error": (
                "Invalid output_file: absolute paths are not allowed. "
                "Provide a workspace-relative path."
            ),
        })
    if any(part == ".." for part in candidate.parts):
        return json.dumps({
            "error": (
                "Invalid output_file: parent-directory traversal ('..') "
                "is not allowed."
            ),
        })

    response = await _make_authed_request(
        url, headers=headers, user=user, raw_response=True,
        method=method, json_body=json_body,
    )
    # On any failure (validation, credential, upstream HTTP 4xx/5xx,
    # transport error) _make_authed_request returns a JSON error string.
    # We pass it through unchanged and skip the file write.
    if isinstance(response, str):
        return response

    # response is an httpx.Response with status < 400 here.
    content: bytes = response.content
    content_type = response.headers.get("content-type", "application/octet-stream")
    status_code = response.status_code

    try:
        workspace_dir = await conversation_workspace_dir(conversation_id)
    except Exception as exc:
        return json.dumps({
            "error": f"Failed to resolve workspace directory: {exc}",
        })

    workspace_root = workspace_dir.resolve()

    # Re-root the (validated) candidate under a hidden ".responses/"
    # subdirectory of the workspace. Machine-written API bodies are noise
    # the end user rarely wants to see, so dropping them in a dot-prefixed
    # dir keeps the file browser clean (the FE hides dotfiles by default,
    # with a toggle to reveal them). The absolute / ".." guards above ran
    # on the raw candidate before re-rooting, and the post-resolve
    # relative_to(workspace_root) check below remains the containment
    # backstop.
    responses_root = workspace_root / ".responses"

    # Directory-vs-file resolution. Mirrors the github plugin's
    # github_get_job_log handler:
    # trailing slash or an existing directory in the workspace means
    # "drop a synthesized filename inside this directory". Otherwise the
    # candidate is treated as the full file path. The existing-directory
    # probe runs under .responses so an already-created .responses/<dir>/
    # is detected.
    treat_as_dir = output_file.endswith("/") or output_file.endswith(os.sep)
    if not treat_as_dir:
        probe = (responses_root / candidate)
        if probe.exists() and probe.is_dir():
            treat_as_dir = True

    if treat_as_dir:
        ext = _content_type_to_extension(content_type)
        digest = hashlib.sha256(content).hexdigest()[:16]
        synth = _sanitize_workspace_filename(f"authed-get-{digest}{ext}")
        rel_path = Path(".responses") / candidate / synth
    else:
        rel_path = Path(".responses") / candidate

    file_path = (workspace_root / rel_path).resolve()
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        return json.dumps({
            "error": (
                "Invalid output_file: resolved destination is outside the "
                "conversation workspace."
            ),
        })

    # Never clobber a previously saved response. Collisions error out so the
    # model picks a fresh name rather than silently overwriting an earlier
    # body it (or another turn) may still need.
    if file_path.exists():
        rel_existing = file_path.relative_to(workspace_root).as_posix()
        return json.dumps({
            "error": (
                f"A file already exists at '{rel_existing}'. Refusing to "
                f"overwrite it; choose a different output_file name."
            ),
        })

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    except Exception as exc:
        return json.dumps({
            "error": f"Failed to write output_file: {exc}",
        })

    if user is not None:
        _publish_file_list_changed(
            user["id"], "conversation", conversation_id, project_id,
        )

    rel_written = file_path.relative_to(workspace_root).as_posix()
    bytes_written = len(content)

    return json.dumps({
        "status": "success",
        "path": rel_written,
        "filename": file_path.name,
        "bytes_written": bytes_written,
        "content_type": content_type,
        "status_code": status_code,
        "message": (
            f"Response saved to '{rel_written}' ({bytes_written} bytes) "
            f"under the '.responses/' workspace directory. Use "
            f"get_workspace_file or run_python/run_script with this exact "
            f"path to process it."
        ),
    })


# Drive ``files.export`` path (GET-allow-listed for the google_export_doc /
# google_export_sheet / google_export_slides tools).
_DRIVE_EXPORT_PATH_RE = re.compile(r"^/drive/v3/files/[^/]+/export$")


async def handle_authed_get(
    url: str,
    headers: dict[str, str] | None = None,
    user: dict | None = None,
    force_large_response: bool = False,
    conversation_id: str | None = None,
    project_id: str | None = None,
    output_file: str | None = None,
) -> str:
    """Make an authenticated GET request to a supported external API.

    Args:
        url: Full upstream API URL (must be HTTPS and match a known service).
        headers: Optional additional HTTP headers from the caller. Only
            content-negotiation headers (``_ALLOWED_CALLER_HEADERS``) are
            accepted; anything else is rejected. Auth headers are injected
            automatically and must NOT be included.
        user: Authenticated user dict.  Required for services that use
            per-user OAuth credentials (e.g. Google Calendar).
        force_large_response: If True, large responses are saved to a file
            for chunked reading instead of being rejected.  Ignored when
            ``output_file`` is set (output_file wins).
        conversation_id: Conversation UUID for file storage location.
        project_id: Project UUID (kept for API consistency).
        output_file: Optional workspace-relative path. When set, the
            response body is written under the hidden ``.responses/``
            subdirectory of this conversation's workspace (the path
            is re-rooted there) and a small JSON receipt is returned instead
            of the body. A collision errors out rather than overwriting an
            existing file. The size gate and ``force_large_response`` blob
            mechanism are bypassed; ``alt=media`` is also unlocked because
            the bytes go straight to disk.

    Returns:
        JSON string of the response body on success, or a JSON error
        string on failure.
    """
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query)

    # When output_file is set, the body never enters LLM context, so the
    # alt=media block does not apply -- this is the deliberate "near-superset
    # of download_drive_file" path. When output_file is absent, the original
    # block stays in place to steer the model toward download_drive_file.
    if output_file is None and query_params.get("alt") == ["media"]:
        return json.dumps({
            "error": (
                "alt=media is not supported via the authed_get tool call because "
                "binary file content cannot be used directly by the assistant. "
                "Use the download_drive_file tool instead: "
                'tool_call(tool_name="download_drive_file", arguments={"file_id": "..."}). '
                "It downloads the file to the workspace, then use get_workspace_file to read it."
            )
        })

    # Same reasoning for Drive ``files.export``: the body is a converted
    # document (PDF/DOCX/XLSX/PPTX/...), not JSON. The google_export_doc /
    # google_export_sheet / google_export_slides tools write it to the
    # workspace; the allow-list entry exists so those tools (and the
    # output_file path) can reach the endpoint through _make_authed_request.
    if output_file is None and _DRIVE_EXPORT_PATH_RE.match(parsed.path):
        return json.dumps({
            "error": (
                "Drive files.export is not supported via the authed_get tool call "
                "because the exported document bytes cannot be used directly by the "
                "assistant. Use the matching export tool instead: "
                'tool_call(tool_name="google_export_doc", arguments={"document_id": "...", "format": "pdf"}) '
                "for Google Docs, google_export_sheet (spreadsheet_id) for Google Sheets, "
                "or google_export_slides (presentation_id) for Google Slides. "
                "They write the export to the workspace, then use get_workspace_file to read it."
            )
        })

    if output_file is not None:
        return await _handle_authed_get_to_file(
            url=url,
            headers=headers,
            user=user,
            conversation_id=conversation_id,
            project_id=project_id,
            output_file=output_file,
        )

    result = await _make_authed_request(url, headers=headers, user=user, raw_response=False)
    # _make_authed_request with raw_response=False always returns a string
    result = result  # type: ignore[assignment]

    return _apply_size_gate(
        result,
        force_large_response=force_large_response,
        conversation_id=conversation_id,
    )


def _apply_size_gate(
    result: str,
    *,
    force_large_response: bool,
    conversation_id: str | None,
) -> str:
    """Apply the inline response size gate shared by authed_get / authed_post.

    Error responses pass through regardless of size. Responses within the
    limit are returned inline. Over-limit responses are either rejected (with
    a narrow-the-request hint) or, when ``force_large_response`` is set, saved
    to a blob file for chunked reading via ``get_response_content``.
    """
    from chat.gemini_api.constants import (
        AUTHED_GET_SIZE_LIMIT,
        _RESPONSE_BLOB_DIR,
    )
    from chat.storage import ChatStorage

    # --- Size gate --------------------------------------------------------
    # Error responses should always pass through regardless of size.
    try:
        parsed_result = json.loads(result)
        if isinstance(parsed_result, dict) and "error" in parsed_result:
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    response_size = len(result.encode("utf-8"))

    if response_size <= AUTHED_GET_SIZE_LIMIT:
        return result

    if not force_large_response:
        return json.dumps({
            "error": "response_too_large",
            "response_size_bytes": response_size,
            "size_limit_bytes": AUTHED_GET_SIZE_LIMIT,
            "message": (
                "The API response is too large to return directly. "
                "Either (a) make a more targeted request (e.g., use the 'fields' parameter, "
                "add filters, reduce 'maxResults'/'pageSize'), or "
                "(b) retry with force_large_response=true if you truly need the full response."
            ),
        })

    # force_large_response=True: save to file for chunked reading
    if conversation_id is None:
        return json.dumps({
            "error": "Conversation context is not available for file storage. "
                     "Cannot save large response without a conversation_id."
        })

    response_hash = hashlib.sha256(result.encode("utf-8")).hexdigest()[:16]
    response_dir = ChatStorage._get_conversation_dir(conversation_id) / _RESPONSE_BLOB_DIR
    response_dir.mkdir(parents=True, exist_ok=True)
    blob_path = response_dir / f"response-{response_hash}.blob"
    blob_path.write_text(result, encoding="utf-8")

    return json.dumps({
        "stored": True,
        "hash": response_hash,
        "size_bytes": response_size,
        "message": (
            f"Response saved to file. Use get_response_content(hash=\"{response_hash}\", "
            f"offset=0, length=5120) to read the content in chunks."
        ),
    })


# ---------------------------------------------------------------------------
# authed_post handler (POST-capable sibling of authed_get)
# ---------------------------------------------------------------------------

async def handle_authed_post(
    url: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    user: dict | None = None,
    force_large_response: bool = False,
    conversation_id: str | None = None,
    project_id: str | None = None,
    output_file: str | None = None,
) -> str:
    """Make an authenticated POST request to a narrow set of read-shaped endpoints.

    This is the POST-capable sibling of ``handle_authed_get``. It is NOT a
    general POST proxy: the target host must be registered in
    ``_SERVICE_REGISTRY`` AND the path must match that host's
    ``allowed_post_endpoints`` allow-list, which today contains exactly the two
    read-shaped GCP verbs ``organizations:search`` and ``entries:list``. Write
    verbs are absent from every POST allow-list and are therefore unreachable.

    Args:
        url: Full upstream API URL (must be HTTPS and match a known service +
            POST allow-list entry).
        body: JSON request body forwarded to the upstream endpoint.
        headers: Optional additional HTTP headers. Only content-negotiation
            headers (``_ALLOWED_CALLER_HEADERS``) are accepted; anything else
            is rejected. Auth headers are injected automatically and must NOT
            be included.
        user: Authenticated user dict (required -- POST endpoints are gated on
            per-user OAuth credentials).
        force_large_response: If True, large responses are saved to a file for
            chunked reading instead of being rejected. Ignored when
            ``output_file`` is set.
        conversation_id: Conversation UUID for file storage location.
        project_id: Project UUID (kept for API consistency).
        output_file: Optional workspace-relative path. When set, the response
            body is written under the hidden ``.responses/`` subdirectory and a
            small JSON receipt is returned instead of the body (same contract as
            ``handle_authed_get``).

    Returns:
        JSON string of the response body on success, or a JSON error string on
        failure.
    """
    if output_file is not None:
        return await _handle_authed_get_to_file(
            url=url,
            headers=headers,
            user=user,
            conversation_id=conversation_id,
            project_id=project_id,
            output_file=output_file,
            method="POST",
            json_body=body,
        )

    result = await _make_authed_request(
        url, headers=headers, user=user, raw_response=False,
        method="POST", json_body=body,
    )
    result = result  # type: ignore[assignment]

    return _apply_size_gate(
        result,
        force_large_response=force_large_response,
        conversation_id=conversation_id,
    )


# ---------------------------------------------------------------------------
# Proxy endpoint
# ---------------------------------------------------------------------------

class AuthedGetRequest(BaseModel):
    """Request body for the POST /api/authed-get proxy endpoint."""
    url: str
    headers: Optional[dict[str, str]] = None


class AuthedPostRequest(BaseModel):
    """Request body for the POST /api/authed-post proxy endpoint."""
    url: str
    body: Optional[dict] = None
    headers: Optional[dict[str, str]] = None


async def authed_get_endpoint(
    body: AuthedGetRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey),
) -> Response:
    """Proxy endpoint exposing authed_get for sandbox code.

    Accepts the same arguments as the authed_get tool call (minus
    intent_message) and delegates to handle_authed_get().

    For JSON responses (the common case), the result is parsed from
    JSON string back to a dict/list so FastAPI returns properly
    formatted JSON.

    For binary responses (e.g. Google Drive ``alt=media`` downloads),
    the raw bytes are returned with the upstream Content-Type header
    so that callers receive the file content intact.
    """
    # Check if this is a binary download request (e.g. Drive alt=media).
    # If so, we need the raw response bytes, not text.
    parsed_url = urllib.parse.urlparse(body.url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    is_binary = query_params.get("alt") == ["media"]

    if is_binary:
        result = await _make_authed_request(
            body.url, headers=body.headers, user=user, raw_response=True,
        )
        if isinstance(result, str):
            # Error path -- result is a JSON error string
            try:
                parsed = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                parsed = {"error": result}
            return JSONResponse(content=parsed)
        # Success path -- result is an httpx.Response with binary content
        content_type = result.headers.get(
            "content-type", "application/octet-stream",
        )
        return Response(
            content=result.content,
            status_code=200,
            media_type=content_type,
        )

    # Standard JSON path -- delegate to handle_authed_get
    result_text = await handle_authed_get(body.url, headers=body.headers, user=user)
    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        parsed = {"raw": result_text}
    return JSONResponse(content=parsed)


async def authed_post_endpoint(
    req: AuthedPostRequest,
    user: dict = Depends(get_current_user_cookie_or_apikey),
) -> Response:
    """Proxy endpoint exposing authed_post for sandbox code.

    Accepts the same arguments as the authed_post tool call (minus
    intent_message) and delegates to handle_authed_post(). Like the tool, it
    can only reach the narrow set of read-shaped POST verbs allow-listed per
    host (no arbitrary-host or write-verb POSTs).
    """
    result_text = await handle_authed_post(
        req.url, body=req.body, headers=req.headers, user=user,
    )
    try:
        parsed = json.loads(result_text)
    except (json.JSONDecodeError, TypeError):
        parsed = {"raw": result_text}
    return JSONResponse(content=parsed)
