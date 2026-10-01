"""GitHub action-request handlers: the plugin's approval-gated writes.

Three request types, each executed only after the user approves the card:

* ``github_trigger_workflow`` -- dispatch a ``workflow_dispatch`` run of a
  GitHub Actions workflow, with optional inputs.
* ``github_comment_on_issue`` -- post a comment on an issue or a pull
  request's conversation.
* ``github_set_issue_state`` -- close or reopen an issue.

The writes use :func:`plugins.github.upstream.github_request` directly;
the plugin's authed_get service entry stays GET-only, so nothing here is
reachable without an approval card.

Every handler reads its target at proposal time
(``validate_against_upstream``): a typo'd repo / workflow / issue number
rejects same-turn with no card, and the target's human-readable identity
(the workflow's name, the issue's title and state) is injected into the
params for the card. Those injected keys are never model-suppliable --
they are absent from the allow-lists -- so a card can only ever name the
target ``execute()`` writes to. Transient read failures (network, 401,
403, 5xx) log a WARNING and defer to ``execute()``, where the
authoritative error surfaces at Approve time.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from chat.action_request_types.base import ActionRequestHandler
from chat.action_request_types._param_validation import reject_unknown_params

from plugins.github.upstream import (
    GitHubAuthError,
    github_error_message,
    github_request,
)

logger = logging.getLogger(__name__)


# Owner logins and repo names are alphanumerics plus ``-`` / ``_`` / ``.``.
# The real gate is GitHub's 404; this only has to keep the value
# URL-path-safe and catch pasted URLs / "owner/repo" in the wrong field.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# A workflow is addressed by its numeric id or its file name.
_WORKFLOW_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,250}\.ya?ml$")
_WORKFLOWS_DIR = ".github/workflows/"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_REF_LENGTH = 255

# GitHub's documented cap on workflow_dispatch input properties.
_MAX_WORKFLOW_INPUTS = 25
_MAX_INPUT_VALUE_LENGTH = 4000

# GitHub's issue-comment body limit.
_MAX_COMMENT_LENGTH = 65536

_CLOSE_REASONS = ("completed", "not_planned")

_PERMISSION_HINT = (
    " The GitHub connection may not have write access here: with a GitHub "
    "App the admin must grant the Actions / Issues / Pull requests "
    "read-and-write permissions and the app must be installed on the "
    "repository; with an OAuth App the organization may need to approve it."
)


def _truncate_for_error(value, limit: int = 100) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _validate_repo(params: dict) -> dict:
    """Validate ``owner`` / ``repo`` and return them as the start of the
    validated dict."""
    validated: dict = {}
    for key, hint in (
        ("owner", "the repository owner's login (user or organization)"),
        ("repo", "the repository name without the owner"),
    ):
        value = params.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing required parameter: {key}")
        value = value.strip()
        if not _NAME_RE.match(value) or value in (".", ".."):
            raise ValueError(
                f"{key} must be {hint}; got {_truncate_for_error(value)!r}"
            )
        validated[key] = value
    return validated


def _validate_issue_number(params: dict) -> int:
    value = params.get("issue_number")
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing required parameter: issue_number")
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        value = int(value.strip().lstrip("#"))
    if not isinstance(value, int) or value <= 0:
        raise ValueError(
            "issue_number must be the positive issue / pull request number "
            f"(e.g. 42); got {_truncate_for_error(value)!r}"
        )
    return value


def _repo_label(params: dict) -> str:
    return f"{params.get('owner', '')}/{params.get('repo', '')}"


def _repo_path(params: dict) -> str:
    return (
        f"/repos/{quote(params['owner'], safe='')}"
        f"/{quote(params['repo'], safe='')}"
    )


def _issue_path(params: dict) -> str:
    return f"{_repo_path(params)}/issues/{int(params['issue_number'])}"


async def _proposal_read(
    type_name: str,
    user: dict,
    path: str,
    *,
    not_found: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> httpx.Response | None:
    """Proposal-time GET of the write's target.

    Returns the response on success, raises ``ValueError(not_found)`` on a
    404, and returns ``None`` for everything that must not be reported to
    the model as bad parameters (no GitHub connection, network errors,
    401/403, 5xx) -- ``execute()`` owns those errors.
    """
    try:
        response = await github_request(
            user, "GET", path, params=params, headers=headers,
        )
    except GitHubAuthError:
        return None
    except Exception:
        logger.warning(
            "[%s] GitHub read failed during proposal validation; deferring "
            "verification to execute()", type_name, exc_info=True,
        )
        return None

    if response.status_code == 404:
        raise ValueError(not_found)
    if response.status_code >= 400:
        logger.warning(
            "[%s] GitHub read returned %s during proposal validation; "
            "deferring verification to execute()",
            type_name, response.status_code,
        )
        return None
    return response


async def _write(user: dict, method: str, path: str, *, json_body: dict, failure: str) -> httpx.Response:
    """Execute-time write; converts every failure into ``RuntimeError`` so
    the request stays open with a readable message."""
    try:
        response = await github_request(user, method, path, json_body=json_body)
    except GitHubAuthError as exc:
        raise RuntimeError(str(exc)) from exc
    if response.status_code >= 400:
        hint = _PERMISSION_HINT if response.status_code in (403, 404) else ""
        raise RuntimeError(f"{failure}: {github_error_message(response)}.{hint}")
    return response


def _inject_issue(params: dict, issue: dict) -> None:
    """Copy the live issue's identity into the params for the card."""
    params["issue_title"] = str(issue.get("title") or "")
    params["issue_state"] = str(issue.get("state") or "")
    params["is_pull_request"] = bool(issue.get("pull_request"))


def _issue_kind(params: dict) -> str:
    return "Pull request" if params.get("is_pull_request") else "Issue"


def _issue_label(params: dict) -> str:
    label = f"#{params.get('issue_number', '')}"
    title = params.get("issue_title")
    return f"{label} {title}" if title else label


def _issue_not_found(params: dict) -> str:
    return (
        f"Issue #{params['issue_number']} not found in {_repo_label(params)} "
        "(or the GitHub connection has no access to that repository). "
        "Check the owner, repo and number with authed_get first."
    )


# ---------------------------------------------------------------------------
# github_trigger_workflow
# ---------------------------------------------------------------------------

# `workflow_name`, `workflow_path` and `ref_is_default` are injected by
# validate_against_upstream() from the live workflow / repository read.
_TRIGGER_ALLOWED_PARAMS = frozenset({"owner", "repo", "workflow", "ref", "inputs"})


def _validate_workflow_inputs(inputs) -> dict:
    """Normalize ``inputs`` to the string map GitHub expects."""
    if inputs is None:
        return {}
    if not isinstance(inputs, dict):
        raise ValueError(
            "inputs must be an object mapping workflow input names to values"
        )
    if len(inputs) > _MAX_WORKFLOW_INPUTS:
        raise ValueError(
            f"inputs has {len(inputs)} entries; GitHub accepts at most "
            f"{_MAX_WORKFLOW_INPUTS} workflow inputs"
        )
    normalized: dict = {}
    for name, value in inputs.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("inputs keys must be non-empty input names")
        if isinstance(value, bool):
            # workflow_dispatch boolean inputs are the strings true/false.
            value = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value = str(value)
        elif not isinstance(value, str):
            raise ValueError(
                f"inputs[{name!r}] must be a string, number or boolean "
                "(workflow inputs are flat values)"
            )
        if len(value) > _MAX_INPUT_VALUE_LENGTH:
            raise ValueError(
                f"inputs[{name!r}] exceeds {_MAX_INPUT_VALUE_LENGTH} characters"
            )
        normalized[name.strip()] = value
    return normalized


def _workflow_label(params: dict) -> str:
    """The workflow as the card names it: its GitHub name plus file.

    Falls back to the raw identifier when the proposal-time read could
    not resolve the workflow.
    """
    workflow = str(params.get("workflow") or "")
    path = str(params.get("workflow_path") or "")
    file_name = path.rsplit("/", 1)[-1] if path else ""
    name = str(params.get("workflow_name") or "")
    # A workflow file without a `name:` key reports its path as the name.
    if name and name != path:
        return f"{name} ({file_name})" if file_name else name
    if file_name:
        return file_name
    return f"workflow #{workflow}" if workflow.isdigit() else workflow


class GitHubTriggerWorkflowHandler(ActionRequestHandler):
    """Run a GitHub Actions workflow via a ``workflow_dispatch`` event.

    Params:
        owner, repo (str): The repository.
        workflow (str): Workflow file name (``deploy.yml``) or numeric id.
        ref (str, optional): Branch or tag to run on; defaults to the
            repository's default branch (resolved at proposal time).
        inputs (dict, optional): Workflow input values.
    """

    @property
    def type_name(self) -> str:
        return "github_trigger_workflow"

    @property
    def display_name(self) -> str:
        return "Run GitHub Workflow"

    @property
    def approve_label(self) -> str:
        return "Run"

    @property
    def resolved_label(self) -> str:
        return "Triggered"

    def summary_snippet(self, params: dict) -> str:
        snippet = f"{_workflow_label(params)} on {_repo_label(params)}"
        ref = params.get("ref")
        return f"{snippet}@{ref}" if ref else snippet

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        ref = str(params.get("ref") or "")
        if ref and params.get("ref_is_default"):
            ref = f"{ref} (default branch)"
        inputs = params.get("inputs") or {}
        if inputs:
            inputs_value = "\n".join(f"{name} = {value}" for name, value in inputs.items())
        else:
            inputs_value = "none (the workflow's defaults apply)"
        return [
            {"key": "Workflow", "value": _workflow_label(params)},
            {"key": "Repository", "value": _repo_label(params)},
            {"key": "Ref", "value": ref},
            {"key": "Inputs", "value": inputs_value},
        ]

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name, params, _TRIGGER_ALLOWED_PARAMS)
        validated = _validate_repo(params)

        workflow = params.get("workflow")
        if isinstance(workflow, bool) or workflow is None or not str(workflow).strip():
            raise ValueError("Missing required parameter: workflow")
        workflow = str(workflow).strip()
        if workflow.startswith(_WORKFLOWS_DIR):
            workflow = workflow[len(_WORKFLOWS_DIR):]
        if not (workflow.isdigit() or _WORKFLOW_FILE_RE.match(workflow)):
            raise ValueError(
                "workflow must be the workflow's file name (e.g. 'deploy.yml') "
                "or its numeric id -- not its display name; list them with "
                "authed_get on /repos/{owner}/{repo}/actions/workflows. Got "
                f"{_truncate_for_error(workflow)!r}"
            )
        validated["workflow"] = workflow

        ref = params.get("ref")
        if ref is not None and str(ref).strip():
            if not isinstance(ref, str):
                raise ValueError("ref must be a branch or tag name")
            ref = ref.strip()
            if len(ref) > _MAX_REF_LENGTH or any(c.isspace() for c in ref):
                raise ValueError(
                    f"ref must be a branch or tag name; got {_truncate_for_error(ref)!r}"
                )
            if _SHA_RE.match(ref):
                raise ValueError(
                    "ref must be a branch or tag name -- GitHub does not "
                    "accept a commit SHA for workflow_dispatch"
                )
            validated["ref"] = ref

        inputs = _validate_workflow_inputs(params.get("inputs"))
        if inputs:
            validated["inputs"] = inputs
        return validated

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        """Resolve the workflow (and the default branch) at proposal time.

        Rejects same-turn when the workflow does not exist, is disabled,
        its file is missing on the chosen ref, or the file has no
        ``workflow_dispatch`` trigger; injects the workflow's GitHub name
        and path so the card names the workflow rather than echoing the
        raw file name / id. A ``ref`` left out by the model is filled in
        with the repository's default branch -- or the proposal is
        rejected when that cannot be determined, so a card never goes out
        without the ref it will run on.
        """
        repo_path = _repo_path(params)
        repo_label = _repo_label(params)

        if "ref" not in params:
            repo_resp = await _proposal_read(
                self.type_name, user, repo_path,
                not_found=(
                    f"Repository {repo_label} not found (or the GitHub "
                    "connection has no access to it)."
                ),
            )
            default_branch = (
                str(repo_resp.json().get("default_branch") or "")
                if repo_resp is not None else ""
            )
            if not default_branch:
                raise ValueError(
                    f"Could not determine the default branch of {repo_label}; "
                    "pass ref (a branch or tag name) explicitly."
                )
            params["ref"] = default_branch
            params["ref_is_default"] = True

        workflow_resp = await _proposal_read(
            self.type_name, user,
            f"{repo_path}/actions/workflows/{quote(params['workflow'], safe='')}",
            not_found=(
                f"Workflow {params['workflow']!r} not found in {repo_label} "
                "(or the GitHub connection has no access to that repository). "
                "List the repo's workflows with authed_get on "
                f"/repos/{repo_label}/actions/workflows and use a workflow's "
                "file name or id."
            ),
        )
        if workflow_resp is None:
            return params

        workflow = workflow_resp.json()
        state = str(workflow.get("state") or "")
        if state and state != "active":
            raise ValueError(
                f"Workflow {params['workflow']!r} is not active (state: "
                f"{state}), so it cannot be run until it is re-enabled on GitHub."
            )
        path = str(workflow.get("path") or "")
        if workflow.get("name"):
            params["workflow_name"] = str(workflow["name"])
        if path:
            params["workflow_path"] = path

        if path.startswith(_WORKFLOWS_DIR):
            # A dispatch runs the workflow file as it exists on the ref, so
            # read that copy: missing file or no workflow_dispatch trigger
            # both mean GitHub would refuse the run.
            file_resp = await _proposal_read(
                self.type_name, user,
                f"{repo_path}/contents/{quote(path, safe='/')}",
                params={"ref": params["ref"]},
                headers={"Accept": "application/vnd.github.raw+json"},
                not_found=(
                    f"Workflow file {path} does not exist on ref "
                    f"{params['ref']!r} of {repo_label} (or that ref does "
                    "not exist). Pick a branch or tag that contains it."
                ),
            )
            if file_resp is not None and "workflow_dispatch" not in file_resp.text:
                raise ValueError(
                    f"Workflow {path} on ref {params['ref']!r} has no "
                    "'workflow_dispatch' trigger, so it cannot be run "
                    "manually."
                )
        return params

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        ref = params.get("ref")
        if not ref:
            raise RuntimeError("No ref recorded for this workflow run request.")

        body: dict = {"ref": ref}
        if params.get("inputs"):
            body["inputs"] = params["inputs"]
        response = await _write(
            user, "POST",
            f"{_repo_path(params)}/actions/workflows/"
            f"{quote(params['workflow'], safe='')}/dispatches",
            json_body=body,
            failure=f"Failed to run workflow {_workflow_label(params)}",
        )

        result = {
            "success": True,
            "repository": _repo_label(params),
            "workflow": _workflow_label(params),
            "ref": ref,
        }
        # GitHub answers 200 with the new run's id and URLs; older API
        # behavior was a bare 204.
        try:
            run = response.json() if response.content else {}
        except ValueError:
            run = {}
        if isinstance(run, dict) and run.get("workflow_run_id"):
            result["run_id"] = run["workflow_run_id"]
            result["url"] = run.get("html_url", "")
            result["message"] = "Workflow run started."
        else:
            result["message"] = (
                "Workflow run requested. GitHub did not return the run id; "
                "find the run with authed_get on "
                f"/repos/{_repo_label(params)}/actions/workflows/"
                f"{params['workflow']}/runs?event=workflow_dispatch&per_page=1."
            )
        logger.info(
            "[github_trigger_workflow] dispatched %s on %s@%s (user=%s)",
            params["workflow"], _repo_label(params), ref, user.get("email"),
        )
        return result


# ---------------------------------------------------------------------------
# github_comment_on_issue
# ---------------------------------------------------------------------------

# `issue_title`, `issue_state` and `is_pull_request` are injected by
# validate_against_upstream() from the live issue read.
_COMMENT_ALLOWED_PARAMS = frozenset({"owner", "repo", "issue_number", "body"})


class GitHubCommentOnIssueHandler(ActionRequestHandler):
    """Post a comment on an issue or on a pull request's conversation.

    Params:
        owner, repo (str): The repository.
        issue_number (int): Issue or pull request number.
        body (str): The comment, GitHub-flavored markdown.
    """

    @property
    def type_name(self) -> str:
        return "github_comment_on_issue"

    @property
    def display_name(self) -> str:
        return "Post GitHub Comment"

    @property
    def approve_label(self) -> str:
        return "Comment"

    @property
    def resolved_label(self) -> str:
        return "Commented"

    def summary_snippet(self, params: dict) -> str:
        # One line: comment bodies are multi-line markdown.
        body = " ".join(str(params.get("body") or "").split())
        return f"{_repo_label(params)}#{params.get('issue_number', '')}: {body}"

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        target = _issue_label(params)
        if params.get("issue_state"):
            target = f"{target} ({params['issue_state']})"
        return [
            {"key": "Repository", "value": _repo_label(params)},
            {"key": _issue_kind(params), "value": target},
            {"key": "Comment", "value": str(params.get("body") or "")},
        ]

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name, params, _COMMENT_ALLOWED_PARAMS)
        validated = _validate_repo(params)
        validated["issue_number"] = _validate_issue_number(params)

        body = params.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("Missing required parameter: body")
        body = body.strip()
        if len(body) > _MAX_COMMENT_LENGTH:
            raise ValueError(
                f"body exceeds GitHub's {_MAX_COMMENT_LENGTH}-character comment limit"
            )
        validated["body"] = body
        return validated

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        response = await _proposal_read(
            self.type_name, user, _issue_path(params),
            not_found=_issue_not_found(params),
        )
        if response is not None:
            _inject_issue(params, response.json())
        return params

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        response = await _write(
            user, "POST", f"{_issue_path(params)}/comments",
            json_body={"body": params["body"]},
            failure=(
                f"Failed to comment on {_repo_label(params)}"
                f"#{params['issue_number']}"
            ),
        )
        comment = response.json()
        logger.info(
            "[github_comment_on_issue] commented on %s#%s (user=%s)",
            _repo_label(params), params["issue_number"], user.get("email"),
        )
        return {
            "success": True,
            "repository": _repo_label(params),
            "issue_number": params["issue_number"],
            "comment_id": comment.get("id"),
            "url": comment.get("html_url", ""),
        }


# ---------------------------------------------------------------------------
# github_set_issue_state
# ---------------------------------------------------------------------------

_STATE_ALLOWED_PARAMS = frozenset({
    "owner", "repo", "issue_number", "state", "state_reason",
})

_PULL_REQUEST_REJECTION = (
    "#{number} in {repo} is a pull request; github_set_issue_state only "
    "closes or reopens issues."
)


class GitHubSetIssueStateHandler(ActionRequestHandler):
    """Close or reopen an issue.

    Params:
        owner, repo (str): The repository.
        issue_number (int): The issue number (pull requests are refused).
        state (str): ``closed`` or ``open``.
        state_reason (str, optional): Why it is closed -- ``completed``
            (default) or ``not_planned``. Only with ``state: closed``.
    """

    @property
    def type_name(self) -> str:
        return "github_set_issue_state"

    @property
    def display_name(self) -> str:
        return "Change GitHub Issue State"

    @property
    def approve_label(self) -> str:
        return "Apply"

    @property
    def resolved_label(self) -> str:
        return "Updated"

    @staticmethod
    def _verb(params: dict) -> str:
        return "Reopen" if params.get("state") == "open" else "Close"

    @staticmethod
    def _new_state_label(params: dict) -> str:
        if params.get("state") == "open":
            return "open (reopened)"
        reason = str(params.get("state_reason") or "completed")
        return f"closed ({reason.replace('_', ' ')})"

    def summary_snippet(self, params: dict) -> str:
        snippet = (
            f"{self._verb(params)} {_repo_label(params)}"
            f"#{params.get('issue_number', '')}"
        )
        title = params.get("issue_title")
        return f"{snippet} {title}" if title else snippet

    async def render_preview(self, params: dict, user: dict | None = None) -> list[dict]:
        fields = [
            {"key": "Repository", "value": _repo_label(params)},
            {"key": "Issue", "value": _issue_label(params)},
        ]
        if params.get("issue_state"):
            fields.append({"key": "Current state", "value": str(params["issue_state"])})
        fields.append({"key": "New state", "value": self._new_state_label(params)})
        return fields

    def validate_params(self, params: dict) -> dict:
        reject_unknown_params(self.type_name, params, _STATE_ALLOWED_PARAMS)
        validated = _validate_repo(params)
        validated["issue_number"] = _validate_issue_number(params)

        state = params.get("state")
        if not isinstance(state, str) or state.strip().lower() not in ("open", "closed"):
            raise ValueError(
                "state must be 'closed' (close the issue) or 'open' (reopen "
                f"it); got {_truncate_for_error(state)!r}"
            )
        state = state.strip().lower()
        validated["state"] = state

        reason = params.get("state_reason")
        if reason is not None and str(reason).strip():
            if state != "closed":
                raise ValueError("state_reason only applies when state is 'closed'")
            reason = str(reason).strip().lower()
            if reason not in _CLOSE_REASONS:
                raise ValueError(
                    f"state_reason must be one of {list(_CLOSE_REASONS)}; got "
                    f"{_truncate_for_error(reason)!r}"
                )
            validated["state_reason"] = reason
        elif state == "closed":
            # GitHub's own default, pinned so the card and the write agree.
            validated["state_reason"] = "completed"
        return validated

    async def validate_against_upstream(self, params: dict, user: dict) -> dict:
        response = await _proposal_read(
            self.type_name, user, _issue_path(params),
            not_found=_issue_not_found(params),
        )
        if response is None:
            return params
        issue = response.json()
        if issue.get("pull_request"):
            raise ValueError(_PULL_REQUEST_REJECTION.format(
                number=params["issue_number"], repo=_repo_label(params),
            ))
        if issue.get("state") == params["state"]:
            raise ValueError(
                f"Issue #{params['issue_number']} in {_repo_label(params)} is "
                f"already {params['state']}."
            )
        _inject_issue(params, issue)
        return params

    async def execute(
        self,
        params: dict,
        user: dict,
        *,
        conversation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        label = f"{_repo_label(params)}#{params['issue_number']}"
        failure = f"Failed to {self._verb(params).lower()} {label}"

        # Re-read at Approve time: the proposal-time read may have been
        # deferred, and the issues endpoint would happily close a pull
        # request addressed by its number.
        try:
            current = await github_request(user, "GET", _issue_path(params))
        except GitHubAuthError as exc:
            raise RuntimeError(str(exc)) from exc
        if current.status_code >= 400:
            raise RuntimeError(f"{failure}: {github_error_message(current)}")
        if current.json().get("pull_request"):
            raise RuntimeError(_PULL_REQUEST_REJECTION.format(
                number=params["issue_number"], repo=_repo_label(params),
            ))

        body = {"state": params["state"]}
        if params["state"] == "closed":
            body["state_reason"] = params.get("state_reason") or "completed"
        response = await _write(
            user, "PATCH", _issue_path(params), json_body=body, failure=failure,
        )
        issue = response.json()
        logger.info(
            "[github_set_issue_state] set %s to %s (user=%s)",
            label, params["state"], user.get("email"),
        )
        return {
            "success": True,
            "repository": _repo_label(params),
            "issue_number": params["issue_number"],
            "state": issue.get("state", params["state"]),
            "state_reason": issue.get("state_reason"),
            "url": issue.get("html_url", ""),
        }


ALL_HANDLERS = (
    GitHubTriggerWorkflowHandler(),
    GitHubCommentOnIssueHandler(),
    GitHubSetIssueStateHandler(),
)
