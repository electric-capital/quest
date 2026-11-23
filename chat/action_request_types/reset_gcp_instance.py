"""ResetGcpInstanceHandler -- approve-to-reset action request for stuck GCP VMs.

The model issues a
``create_action_request(request_type="reset_gcp_instance",
params={"project": "...", "zone": "...", "instance": "..."},
reasoning="...")`` call which appends an inline approval card to the
chat. On Approve, the handler hard-resets the Compute Engine instance
via the ``instances.reset`` POST verb -- the equivalent of pulling the
power plug and plugging it back in (unsaved in-memory state is lost),
which is the recovery path for a wedged/unresponsive VM that no longer
answers SSH or ACPI shutdown. On Deny/Revise nothing happens.

This is deliberately the ONE Compute Engine write in the product, and it
does NOT ride on the authed_get/authed_post allow-lists (those stay
strictly read-only -- ``:``-suffixed verbs like ``:reset`` are
unreachable there by the ``[^/:]+`` id-segment convention). The write
happens only here, behind the user's explicit Approve click, using the
same per-user Google Services OAuth token (``cloud-platform`` scope).

Proposal-time validation (``validate_against_upstream``) fetches the
instance so a typo'd project/zone/instance rejects same-turn, injects
the live ``status`` / machine type into the preview card, and refuses to
even show a card for a TERMINATED/SUSPENDED instance (reset only
applies to a machine that is powered on).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from auth.google_credentials import make_authenticated_request
from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._google import (
    _extract_google_api_error,
    require_google_scope,
)
from chat.action_request_types._param_validation import reject_unknown_params
from db.models import ActionRequestType

logger = logging.getLogger(__name__)

COMPUTE_API_BASE = "https://compute.googleapis.com/compute/v1"

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# `instance_status` and `machine_type` are injected by
# validate_against_upstream() from the live instance read; they are not
# model-suppliable and therefore not in the allow-list (parallels
# `spreadsheet_title` / `folder_name`).
_ALLOWED_PARAMS = frozenset({"project", "zone", "instance"})

# Project ids are 6-30 chars of lowercase letters/digits/hyphens, but
# legacy domain-scoped ids look like `example.com:my-project`, so admit
# dots and a single colon too. The real gate is Google's 404; this
# pattern only has to keep the value URL-path-safe.
_PROJECT_RE = re.compile(r"^[a-z][a-z0-9.:-]{4,61}[a-z0-9]$")
# Zones and instance names follow RFC1035 (lowercase letter first,
# lowercase letters/digits/hyphens, max 63 chars).
_RFC1035_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")

# Statuses in which instances.reset is meaningless: the machine is off
# (or suspended), so there is nothing to power-cycle and Google rejects
# the call. Refusing at proposal time keeps the model from burning an
# approval round trip on a doomed card.
_UNRESETTABLE_STATUSES = frozenset({"TERMINATED", "SUSPENDED"})


def _gcp_reauth_message() -> str:
    return (
        "Google Cloud access requires reconnecting Google Services via "
        "Settings > Data Connections (the cloud-platform scope has not "
        "been granted)."
    )


def _instance_url(params: dict) -> str:
    return (
        f"{COMPUTE_API_BASE}/projects/{quote(params['project'], safe='')}"
        f"/zones/{quote(params['zone'], safe='')}"
        f"/instances/{quote(params['instance'], safe='')}"
    )


class ResetGcpInstanceHandler(ActionRequestHandler):
    """Hard-reset a Compute Engine VM after explicit user approval.

    Params:
        project (str): GCP project id (not the numeric project number).
        zone (str): Zone the instance lives in (e.g. ``us-central1-a``).
        instance (str): Instance name.
    """

    @property
    def type_name(self) -> ActionRequestType:
        return ActionRequestType.RESET_GCP_INSTANCE

    @property
    def display_name(self) -> str:
        return "Reset GCP VM"

    @property
    def approve_label(self) -> str:
        return "Reset"

    @property
    def resolved_label(self) -> str:
        return "Reset"

    def summary_snippet(self, params: dict) -> str:
        instance = str(params.get("instance") or "")
        project = str(params.get("project") or "")
        zone = str(params.get("zone") or "")
        if project and zone:
            return f"{instance} ({project}/{zone})"
        return instance

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        fields = [
            {"key": "Instance", "value": str(params.get("instance", ""))},
            {"key": "Zone", "value": str(params.get("zone", ""))},
            {"key": "Project", "value": str(params.get("project", ""))},
        ]
        status = params.get("instance_status")
        if status:
            fields.append({"key": "Current status", "value": str(status)})
        machine_type = params.get("machine_type")
        if machine_type:
            fields.append({"key": "Machine type", "value": str(machine_type)})
        fields.append({
            "key": "Effect",
            "value": (
                "Hard power-cycle (like pulling the plug) -- unsaved "
                "in-memory data is lost; disks are untouched"
            ),
        })
        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name.value, params, _ALLOWED_PARAMS)

        validated: dict = {}
        for key, pattern, hint in (
            ("project", _PROJECT_RE, "a GCP project id like 'my-project-123'"),
            ("zone", _RFC1035_RE, "a zone like 'us-central1-a'"),
            ("instance", _RFC1035_RE, "an instance name like 'web-server-1'"),
        ):
            value = params.get(key)
            if value is None or not isinstance(value, str):
                raise ValueError(f"Missing required parameter: {key}")
            value = value.strip()
            if not pattern.match(value):
                raise ValueError(
                    f"{key} must be {hint} (got '{value}'). Use "
                    "authed_get on the Compute Engine API to look up the "
                    "exact ids first."
                )
            validated[key] = value

        return validated

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Fetch the live instance at proposal time.

        A 404 (bad project/zone/instance) raises ValueError so the model
        gets a same-turn rejection instead of the user approving a card
        that can only fail. A powered-off instance (TERMINATED/SUSPENDED)
        is rejected the same way -- reset only applies to a machine that
        is on. On success the live status and machine type are injected
        into the params for the preview card. Transient failures
        (network, 401/403, 5xx) log a WARNING and fall through --
        execute() surfaces the authoritative error at Approve time.
        """
        if not (user.get("google_services_oauth") or {}).get("access_token"):
            # No Google connection: the authoritative "connect Google
            # Services" error belongs to execute(); don't mask it as
            # Invalid parameters.
            return params

        url = f"{_instance_url(params)}?fields=name,status,machineType"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await make_authenticated_request(
                    client, user, "GET", url
                )
        except Exception:
            logger.warning(
                "[reset_gcp_instance] instance fetch failed during proposal "
                "validation; deferring verification to execute()",
                exc_info=True,
            )
            return params

        if response.status_code == 404:
            raise ValueError(
                f"Instance not found: '{params['instance']}' in zone "
                f"'{params['zone']}' of project '{params['project']}'. "
                "Check the ids -- list VMs with authed_get on "
                f"{COMPUTE_API_BASE}/projects/{{project}}/aggregated/instances."
            )
        if response.status_code >= 400:
            logger.warning(
                "[reset_gcp_instance] instance fetch returned %s during "
                "proposal validation; deferring verification to execute()",
                response.status_code,
            )
            return params

        payload = response.json()
        status = str(payload.get("status") or "")
        if status in _UNRESETTABLE_STATUSES:
            raise ValueError(
                f"Instance '{params['instance']}' is {status}, not running "
                "-- reset power-cycles a machine that is on, so it cannot "
                "help here. A stopped instance must be started by the user "
                "from the GCP console."
            )
        if status:
            params["instance_status"] = status
        machine_type = str(payload.get("machineType") or "")
        if machine_type:
            # Full self-link -> short name (".../machineTypes/e2-medium").
            params["machine_type"] = machine_type.rsplit("/", 1)[-1]
        return params

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        require_google_scope(user, CLOUD_PLATFORM_SCOPE, _gcp_reauth_message())

        url = f"{_instance_url(params)}/reset"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await make_authenticated_request(
                client, user, "POST", url
            )

        if response.status_code >= 400:
            raise RuntimeError(
                "Failed to reset instance "
                f"'{params['instance']}': {_extract_google_api_error(response)}"
            )

        operation = response.json()
        return {
            "success": True,
            "project": params["project"],
            "zone": params["zone"],
            "instance": params["instance"],
            "operation": operation.get("name", ""),
            "operation_status": operation.get("status", ""),
            "message": (
                "Reset issued. The VM power-cycles and boots from its "
                "disk; it may take a minute or two to come back up."
            ),
        }
