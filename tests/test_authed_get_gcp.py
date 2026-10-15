"""Tests for the Google Cloud Platform (GCP) entries in the ``authed_get`` /
``authed_post`` registry.

The six GCP hosts (Resource Manager, Compute Engine, Kubernetes Engine,
Cloud Logging, Cloud Billing, Billing Budgets) are per-user OAuth Google
services modelled like Slides/Docs:
they reuse the shared ``_load_google_services_credentials`` loader and
``_inject_google_bearer_auth`` injector, set ``requires_user`` and
``retry_on_401`` to ``True``, and are keyed by plain hostnames (path gating by
the regex allow-lists).

Three hosts additionally expose a SEPARATE POST allow-list
(``allowed_post_endpoints``) for the read-shaped POST verbs Google only
offers as POST: ``organizations:search`` and project ``:getIamPolicy``
(Resource Manager), ``entries:list`` (Cloud Logging) and billing-account
``:getIamPolicy`` (Cloud Billing). These are reachable only via ``authed_post``.

Covers:
* All six GCP entries present as plain-hostname keys, correct names, per-user
  OAuth model, shared Google loader/injector.
* GET allow-lists match the intended read paths and reject write verbs
  (``:`` -suffixed custom methods), list-vs-id mismatches, and unrelated paths.
* The POST allow-lists match exactly ``organizations:search`` / ``entries:list``
  and reject write verbs (``entries:write``) -- and the GET and POST allow-lists
  are STRICTLY independent (a GET cannot reach a POST verb and vice versa).
* A mocked POST request injects ``Authorization: Bearer ...`` and forwards the
  JSON body via exactly one ``client.post`` call.
* A POST to a host with no POST allow-list (e.g. Compute) is rejected without an
  HTTP call.

All HTTP calls are mocked -- no real network traffic.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.gemini_api import authed_get
from chat.gemini_api.authed_get import (
    _SERVICE_REGISTRY,
    _find_service,
    _inject_google_bearer_auth,
    _load_google_services_credentials,
    _make_authed_request,
)


_RM_KEY = "cloudresourcemanager.googleapis.com"
_COMPUTE_KEY = "compute.googleapis.com"
_GKE_KEY = "container.googleapis.com"
_LOGGING_KEY = "logging.googleapis.com"
_BILLING_KEY = "cloudbilling.googleapis.com"
_BUDGETS_KEY = "billingbudgets.googleapis.com"

_ALL_GCP_KEYS = [_RM_KEY, _COMPUTE_KEY, _GKE_KEY, _LOGGING_KEY, _BILLING_KEY, _BUDGETS_KEY]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _matches(compiled, path: str) -> bool:
    return any(pat.match(path) for pat in compiled)


def _mock_httpx_client(captured_get: list, captured_post: list,
                       status_code: int = 200, body: dict | None = None):
    """Patch ``httpx.AsyncClient`` capturing both get() and post() calls.

    ``client.get(url, headers=...)`` -> appends ``(url, headers)`` to
    ``captured_get``; ``client.post(url, headers=..., json=...)`` -> appends
    ``(url, headers, json)`` to ``captured_post``.
    """
    if body is None:
        body = {"ok": True}
    response_text = json.dumps(body)

    response_mock = MagicMock()
    response_mock.status_code = status_code
    response_mock.text = response_text
    response_mock.json = MagicMock(return_value=body)

    async def fake_get(url, headers=None):
        captured_get.append((str(url), dict(headers or {})))
        return response_mock

    async def fake_post(url, headers=None, json=None):
        captured_post.append((str(url), dict(headers or {}), json))
        return response_mock

    client_mock = MagicMock()
    client_mock.get = AsyncMock(side_effect=fake_get)
    client_mock.post = AsyncMock(side_effect=fake_post)

    async_ctx = MagicMock()
    async_ctx.__aenter__ = AsyncMock(return_value=client_mock)
    async_ctx.__aexit__ = AsyncMock(return_value=None)

    return patch(
        "chat.gemini_api.authed_get.httpx.AsyncClient",
        return_value=async_ctx,
    )


def _mock_credentials_loader(token: str | None = "ya29.test-token"):
    creds = None
    if token is not None:
        creds = MagicMock()
        creds.token = token
    return patch.object(
        authed_get,
        "get_valid_service_credentials",
        new=AsyncMock(return_value=creds),
    )


# ---------------------------------------------------------------------------
# Tests: registry shape
# ---------------------------------------------------------------------------

class TestGcpRegistryEntries:
    def test_all_entries_present_as_plain_hostname_keys(self):
        for key in _ALL_GCP_KEYS:
            assert key in _SERVICE_REGISTRY, f"missing GCP host: {key}"
            entry = _SERVICE_REGISTRY[key]
            assert "path_prefix" not in entry

    def test_entries_are_per_user_oauth(self):
        for key in _ALL_GCP_KEYS:
            entry = _SERVICE_REGISTRY[key]
            assert entry["requires_user"] is True
            assert entry["retry_on_401"] is True

    def test_entries_reuse_shared_google_helpers(self):
        for key in _ALL_GCP_KEYS:
            entry = _SERVICE_REGISTRY[key]
            assert entry["load_credentials"] is _load_google_services_credentials
            assert entry["inject_auth"] is _inject_google_bearer_auth

    def test_only_rm_logging_and_billing_have_post_allow_lists(self):
        assert "_allowed_post_endpoints" in _SERVICE_REGISTRY[_RM_KEY]
        assert "_allowed_post_endpoints" in _SERVICE_REGISTRY[_LOGGING_KEY]
        assert "_allowed_post_endpoints" in _SERVICE_REGISTRY[_BILLING_KEY]
        assert "_allowed_post_endpoints" not in _SERVICE_REGISTRY[_COMPUTE_KEY]
        assert "_allowed_post_endpoints" not in _SERVICE_REGISTRY[_GKE_KEY]
        assert "_allowed_post_endpoints" not in _SERVICE_REGISTRY[_BUDGETS_KEY]


# ---------------------------------------------------------------------------
# Tests: GET allow-lists
# ---------------------------------------------------------------------------

class TestGcpGetAllowLists:
    def test_resource_manager_allowed(self):
        compiled = _SERVICE_REGISTRY[_RM_KEY]["_allowed_endpoints"]
        for path in [
            "/v1/organizations/123456",
            "/v1/projects",
            "/v1/projects/my-project-123",
            "/v3/projects/my-project-123",
            "/v3/folders/987654",
        ]:
            assert _matches(compiled, path), f"expected allowed: {path}"

    def test_resource_manager_denied(self):
        compiled = _SERVICE_REGISTRY[_RM_KEY]["_allowed_endpoints"]
        for path in [
            "/v1/organizations:search",          # POST verb -- not on GET list
            "/v1/projects/my-project:delete",    # write verb
            "/v1/organizations",                 # list orgs is POST-only
            "/v1/projects/my-project/extra",     # extra segment
        ]:
            assert not _matches(compiled, path), f"should be denied: {path}"

    def test_compute_allowed(self):
        compiled = _SERVICE_REGISTRY[_COMPUTE_KEY]["_allowed_endpoints"]
        for path in [
            "/compute/v1/projects/p/zones/us-central1-a/instances",
            "/compute/v1/projects/p/zones/us-central1-a/instances/vm1",
            "/compute/v1/projects/p/zones/us-central1-a/instances/vm1/getIamPolicy",
            "/compute/v1/projects/p/aggregated/instances",
            "/compute/v1/projects/p/zones/us-central1-a/disks",
            "/compute/v1/projects/p/zones/us-central1-a/disks/disk1",
            "/compute/v1/projects/p/aggregated/disks",
            "/compute/v1/projects/p/zones",
            "/compute/v1/projects/p/regions",
            "/compute/v1/projects/p/zones/us-central1-a",
            "/compute/v1/projects/p/regions/us-central1",
        ]:
            assert _matches(compiled, path), f"expected allowed: {path}"

    def test_compute_denied_write_verbs(self):
        compiled = _SERVICE_REGISTRY[_COMPUTE_KEY]["_allowed_endpoints"]
        for path in [
            "/compute/v1/projects/p/zones/z/instances/vm1:stop",
            "/compute/v1/projects/p/zones/z/instances/vm1:start",
            "/compute/v1/projects/p/zones/z/instances/vm1:setMachineType",
            # setIamPolicy is a POST on its own segment; even as a GET path it
            # must not match, and getIamPolicy must not widen to sub-resources.
            "/compute/v1/projects/p/zones/z/instances/vm1/setIamPolicy",
            "/compute/v1/projects/p/zones/z/instances/vm1/getIamPolicy/extra",
            # Disk write/mutate verbs (':'-suffixed custom methods) must NOT match.
            "/compute/v1/projects/p/zones/z/disks/disk1:createSnapshot",
            "/compute/v1/projects/p/zones/z/disks/disk1:resize",
            "/compute/v1/projects/p/zones/z/disks/disk1:setLabels",
        ]:
            assert not _matches(compiled, path), f"should be denied: {path}"

    def test_compute_disks_denied_extra_segments(self):
        """Disk read patterns must not widen to deeper/unrelated sub-resources."""
        compiled = _SERVICE_REGISTRY[_COMPUTE_KEY]["_allowed_endpoints"]
        for path in [
            "/compute/v1/projects/p/zones/z/disks/disk1/extra",   # extra segment
            "/compute/v1/projects/p/aggregated/disks/disk1",      # aggregated is list-only
            "/compute/v1/projects/p/regions/r/disks",             # region disks not allow-listed
        ]:
            assert not _matches(compiled, path), f"should be denied: {path}"

    def test_gke_allowed(self):
        compiled = _SERVICE_REGISTRY[_GKE_KEY]["_allowed_endpoints"]
        for path in [
            "/v1/projects/p/locations/-/clusters",
            "/v1/projects/p/locations/us-central1/clusters/c1",
            "/v1/projects/p/locations/us-central1/clusters/c1/nodePools",
            "/v1/projects/p/locations/us-central1/clusters/c1/nodePools/np1",
        ]:
            assert _matches(compiled, path), f"expected allowed: {path}"

    def test_gke_denied_write_verbs(self):
        compiled = _SERVICE_REGISTRY[_GKE_KEY]["_allowed_endpoints"]
        for path in [
            "/v1/projects/p/locations/us-central1/clusters/c1:setMasterAuth",
            "/v1/projects/p/locations/us-central1/clusters/c1:delete",
        ]:
            assert not _matches(compiled, path), f"should be denied: {path}"

    def test_logging_get_metadata_only(self):
        compiled = _SERVICE_REGISTRY[_LOGGING_KEY]["_allowed_endpoints"]
        for path in [
            "/v2/projects/p/logs",
            "/v2/projects/p/sinks",
            "/v2/projects/p/sinks/s1",
            "/v2/projects/p/metrics",
            "/v2/projects/p/metrics/m1",
        ]:
            assert _matches(compiled, path), f"expected allowed: {path}"
        # The GET list must NOT reach entries:list (that is POST-only).
        assert not _matches(compiled, "/v2/entries:list")
        assert not _matches(compiled, "/v2/entries:write")

    def test_billing_allowed(self):
        compiled = _SERVICE_REGISTRY[_BILLING_KEY]["_allowed_endpoints"]
        for path in [
            "/v1/billingAccounts",
            "/v1/billingAccounts/012345-6789AB-CDEF01",
            "/v1/billingAccounts/012345-6789AB-CDEF01/projects",
            "/v1/billingAccounts/012345-6789AB-CDEF01/subAccounts",
            "/v1/organizations/123456/billingAccounts",
            "/v1/projects/my-project-123/billingInfo",
            "/v1/services",
            "/v1/services/6F81-5844-456A/skus",
            "/v1beta/skus/0001-0002-0003/price",
            "/v1beta/billingAccounts/012345-6789AB-CDEF01/services",
            "/v1beta/billingAccounts/012345-6789AB-CDEF01/skus",
            "/v1beta/billingAccounts/012345-6789AB-CDEF01/skus/0001-0002-0003/price",
        ]:
            assert _matches(compiled, path), f"expected allowed: {path}"

    def test_billing_denied_write_verbs_and_extra_segments(self):
        compiled = _SERVICE_REGISTRY[_BILLING_KEY]["_allowed_endpoints"]
        for path in [
            # ':'-verbs (create/update/move/IAM) must never match a GET pattern.
            "/v1/billingAccounts:create",
            "/v1/billingAccounts/012345-6789AB-CDEF01:patch",
            "/v1/billingAccounts/012345-6789AB-CDEF01:move",
            "/v1/billingAccounts/012345-6789AB-CDEF01:getIamPolicy",   # POST-only
            "/v1/billingAccounts/012345-6789AB-CDEF01:setIamPolicy",
            "/v1/billingAccounts/012345-6789AB-CDEF01:testIamPermissions",
            "/v1/projects/my-project-123/billingInfo:update",
            "/v1beta/billingAccounts/012345-6789AB-CDEF01:estimateCostScenario",
            # Deeper / unrelated sub-resources must not widen the patterns.
            "/v1/billingAccounts/012345-6789AB-CDEF01/projects/p",
            "/v1/services/6F81-5844-456A",
            "/v1/services/6F81-5844-456A/skus/0001",
            "/v1beta/skus/0001-0002-0003/prices",
            "/v1/projects/my-project-123",
        ]:
            assert not _matches(compiled, path), f"should be denied: {path}"

    def test_budgets_allowed_and_denied(self):
        compiled = _SERVICE_REGISTRY[_BUDGETS_KEY]["_allowed_endpoints"]
        for path in [
            "/v1/billingAccounts/012345-6789AB-CDEF01/budgets",
            "/v1/billingAccounts/012345-6789AB-CDEF01/budgets/b1",
        ]:
            assert _matches(compiled, path), f"expected allowed: {path}"
        for path in [
            "/v1/billingAccounts/012345-6789AB-CDEF01/budgets/b1:patch",
            "/v1/billingAccounts/012345-6789AB-CDEF01/budgets/b1/extra",
            "/v1/billingAccounts/012345-6789AB-CDEF01",
            "/v1beta1/billingAccounts/012345-6789AB-CDEF01/budgets",
        ]:
            assert not _matches(compiled, path), f"should be denied: {path}"


# ---------------------------------------------------------------------------
# Tests: POST allow-lists and verb independence
# ---------------------------------------------------------------------------

class TestGcpPostAllowLists:
    def test_resource_manager_post_reads_only(self):
        compiled = _SERVICE_REGISTRY[_RM_KEY]["_allowed_post_endpoints"]
        for path in [
            "/v1/organizations:search",
            "/v1/projects/my-project-123:getIamPolicy",
            "/v3/projects/my-project-123:getIamPolicy",
        ]:
            assert _matches(compiled, path), f"expected allowed (POST): {path}"
        # No other POST verb is reachable.
        for path in [
            "/v1/projects",
            "/v1/projects/p:delete",
            "/v3/organizations:search",              # only v1 form is allow-listed
            "/v1/projects/p:setIamPolicy",           # the mutating IAM verb
            "/v3/projects/p:setIamPolicy",
            "/v1/projects/p:testIamPermissions",     # not allow-listed
            "/v1/organizations/123:getIamPolicy",    # org-level IAM not allow-listed
            "/v3/folders/123:getIamPolicy",          # folder-level IAM not allow-listed
        ]:
            assert not _matches(compiled, path), f"should be denied (POST): {path}"

    def test_logging_post_entries_list_only(self):
        compiled = _SERVICE_REGISTRY[_LOGGING_KEY]["_allowed_post_endpoints"]
        assert _matches(compiled, "/v2/entries:list")
        # Write verb must NOT be reachable.
        assert not _matches(compiled, "/v2/entries:write")
        assert not _matches(compiled, "/v2/projects/p/logs")

    def test_billing_post_get_iam_policy_only(self):
        compiled = _SERVICE_REGISTRY[_BILLING_KEY]["_allowed_post_endpoints"]
        assert _matches(compiled, "/v1/billingAccounts/012345-6789AB-CDEF01:getIamPolicy")
        for path in [
            "/v1/billingAccounts/012345-6789AB-CDEF01:setIamPolicy",
            "/v1/billingAccounts/012345-6789AB-CDEF01:testIamPermissions",
            "/v1/billingAccounts:create",
            "/v1/billingAccounts/012345-6789AB-CDEF01:move",
            "/v1beta/billingAccounts/012345-6789AB-CDEF01:estimateCostScenario",
            "/v1/billingAccounts",
        ]:
            assert not _matches(compiled, path), f"should be denied (POST): {path}"

    def test_get_and_post_allow_lists_are_independent(self):
        """A GET path cannot appear on a POST list and vice versa."""
        rm = _SERVICE_REGISTRY[_RM_KEY]
        logging_ = _SERVICE_REGISTRY[_LOGGING_KEY]
        billing = _SERVICE_REGISTRY[_BILLING_KEY]
        # POST verbs must not match the GET lists.
        assert not _matches(rm["_allowed_endpoints"], "/v1/organizations:search")
        assert not _matches(rm["_allowed_endpoints"], "/v1/projects/p:getIamPolicy")
        assert not _matches(rm["_allowed_endpoints"], "/v3/projects/p:getIamPolicy")
        assert not _matches(logging_["_allowed_endpoints"], "/v2/entries:list")
        assert not _matches(billing["_allowed_endpoints"], "/v1/billingAccounts/a:getIamPolicy")
        # GET metadata paths must not match the POST lists.
        assert not _matches(rm["_allowed_post_endpoints"], "/v1/projects")
        assert not _matches(logging_["_allowed_post_endpoints"], "/v2/projects/p/logs")
        assert not _matches(billing["_allowed_post_endpoints"], "/v1/billingAccounts")


# ---------------------------------------------------------------------------
# Tests: _make_authed_request behavior for GCP
# ---------------------------------------------------------------------------

class TestMakeAuthedRequestGcp:
    def test_get_request_injects_bearer_auth(self):
        get_calls: list = []
        post_calls: list = []
        body = {"projects": [{"projectId": "p1"}]}
        with _mock_credentials_loader("ya29.live"), \
                _mock_httpx_client(get_calls, post_calls, body=body):
            result = _run(_make_authed_request(
                "https://cloudresourcemanager.googleapis.com/v1/projects",
                user={"email": "user@example.com"},
            ))
        assert json.loads(result) == body
        assert len(get_calls) == 1
        assert post_calls == []
        _, headers = get_calls[0]
        assert headers.get("Authorization") == "Bearer ya29.live"

    def test_post_entries_list_forwards_body(self):
        get_calls: list = []
        post_calls: list = []
        req_body = {"resourceNames": ["projects/p"], "filter": "severity>=ERROR"}
        resp_body = {"entries": []}
        with _mock_credentials_loader("ya29.logtoken"), \
                _mock_httpx_client(get_calls, post_calls, body=resp_body):
            result = _run(_make_authed_request(
                "https://logging.googleapis.com/v2/entries:list",
                user={"email": "user@example.com"},
                method="POST",
                json_body=req_body,
            ))
        assert json.loads(result) == resp_body
        assert get_calls == []
        assert len(post_calls) == 1
        url, headers, sent_json = post_calls[0]
        assert url.endswith("/v2/entries:list")
        assert headers.get("Authorization") == "Bearer ya29.logtoken"
        assert sent_json == req_body

    def test_post_org_search_optional_empty_body(self):
        get_calls: list = []
        post_calls: list = []
        with _mock_credentials_loader("ya29.org"), \
                _mock_httpx_client(get_calls, post_calls, body={"organizations": []}):
            result = _run(_make_authed_request(
                "https://cloudresourcemanager.googleapis.com/v1/organizations:search",
                user={"email": "user@example.com"},
                method="POST",
                json_body={},
            ))
        assert json.loads(result) == {"organizations": []}
        assert len(post_calls) == 1

    def test_post_to_disallowed_verb_rejected_without_http_call(self):
        """entries:write is not in the POST allow-list -> rejected, no call."""
        get_calls: list = []
        post_calls: list = []
        with _mock_credentials_loader(), \
                _mock_httpx_client(get_calls, post_calls):
            result = _run(_make_authed_request(
                "https://logging.googleapis.com/v2/entries:write",
                user={"email": "user@example.com"},
                method="POST",
                json_body={"entries": []},
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert get_calls == [] and post_calls == []

    def test_post_billing_get_iam_policy_forwards_body(self):
        get_calls: list = []
        post_calls: list = []
        resp_body = {"bindings": [{"role": "roles/billing.admin", "members": ["user:a@example.com"]}]}
        with _mock_credentials_loader("ya29.billing"), \
                _mock_httpx_client(get_calls, post_calls, body=resp_body):
            result = _run(_make_authed_request(
                "https://cloudbilling.googleapis.com/v1/billingAccounts/012345-6789AB-CDEF01:getIamPolicy",
                user={"email": "user@example.com"},
                method="POST",
                json_body={},
            ))
        assert json.loads(result) == resp_body
        assert get_calls == []
        assert len(post_calls) == 1
        url, headers, sent_json = post_calls[0]
        assert url.endswith("/v1/billingAccounts/012345-6789AB-CDEF01:getIamPolicy")
        assert headers.get("Authorization") == "Bearer ya29.billing"
        assert sent_json == {}

    def test_post_billing_set_iam_policy_rejected_without_http_call(self):
        get_calls: list = []
        post_calls: list = []
        with _mock_credentials_loader(), \
                _mock_httpx_client(get_calls, post_calls):
            result = _run(_make_authed_request(
                "https://cloudbilling.googleapis.com/v1/billingAccounts/012345-6789AB-CDEF01:setIamPolicy",
                user={"email": "user@example.com"},
                method="POST",
                json_body={"policy": {}},
            ))
        assert "error" in json.loads(result)
        assert get_calls == [] and post_calls == []

    def test_post_to_host_without_post_list_rejected(self):
        """Compute has no POST allow-list -> all POSTs rejected, no call."""
        get_calls: list = []
        post_calls: list = []
        with _mock_credentials_loader(), \
                _mock_httpx_client(get_calls, post_calls):
            result = _run(_make_authed_request(
                "https://compute.googleapis.com/compute/v1/projects/p/zones",
                user={"email": "user@example.com"},
                method="POST",
                json_body={},
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert "POST" in parsed["error"]
        assert get_calls == [] and post_calls == []

    def test_get_cannot_reach_post_verb(self):
        """A GET to entries:list is rejected (it is POST-only), no HTTP call."""
        get_calls: list = []
        post_calls: list = []
        with _mock_credentials_loader(), \
                _mock_httpx_client(get_calls, post_calls):
            result = _run(_make_authed_request(
                "https://logging.googleapis.com/v2/entries:list",
                user={"email": "user@example.com"},
            ))
        parsed = json.loads(result)
        assert "error" in parsed
        assert get_calls == [] and post_calls == []


# ---------------------------------------------------------------------------
# Tests: _find_service resolution
# ---------------------------------------------------------------------------

class TestGcpServiceLookup:
    def test_hosts_resolve(self):
        assert _find_service(_RM_KEY, "/v1/projects")["name"] == "Google Cloud Resource Manager"
        assert _find_service(_COMPUTE_KEY, "/compute/v1/projects/p/zones")["name"] == "Google Compute Engine"
        assert _find_service(_GKE_KEY, "/v1/projects/p/locations/-/clusters")["name"] == "Google Kubernetes Engine"
        assert _find_service(_LOGGING_KEY, "/v2/entries:list")["name"] == "Google Cloud Logging"
        assert _find_service(_BILLING_KEY, "/v1/billingAccounts")["name"] == "Google Cloud Billing"
        assert _find_service(_BUDGETS_KEY, "/v1/billingAccounts/a/budgets")["name"] == "Google Cloud Billing Budgets"
