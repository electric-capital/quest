"""Tests for the GitHub action-request handlers (plugins/github/handlers.py).

Drives the same server-side sequence as a real action request -- validate,
verify against GitHub, render the card, execute -- with only the GitHub
HTTP boundary (``github_request``) faked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugins.github import handlers as handlers_mod
from plugins.github import upstream as upstream_mod
from plugins.github.handlers import (
    GitHubCommentOnIssueHandler,
    GitHubSetIssueStateHandler,
    GitHubTriggerWorkflowHandler,
)
from plugins.github.upstream import GitHubAuthError, github_error_message


def _run(coro):
    return asyncio.run(coro)


_USER = {
    "id": 7,
    "email": "u@example.com",
    "service_credentials": {
        "github": {"oauth_blob": {"access_token": "gho_testtoken"}},
    },
}

_REPO = {"owner": "acme", "repo": "site"}

_WORKFLOW = {
    "id": 161335,
    "name": "Deploy to production",
    "path": ".github/workflows/deploy.yml",
    "state": "active",
}

_WORKFLOW_FILE = """\
name: Deploy to production
on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      environment:
        description: Where to deploy
        type: choice
        options: [staging, production]
        default: staging
      dry_run:
        type: boolean
        default: false
      replicas:
        type: number
      note:
jobs: {}
"""

_FILE_ROUTE = ("GET", "/repos/acme/site/contents/.github/workflows/deploy.yml")


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ("" if payload is None else str(payload))
        self.content = b"" if (payload is None and not text) else b"x"

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _FakeGitHub:
    """Routes (method, path) to canned responses and records every call."""

    def __init__(self, monkeypatch, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []
        monkeypatch.setattr(handlers_mod, "github_request", self)

    async def __call__(self, user, method, path, *, params=None, json_body=None, headers=None):
        self.calls.append({
            "method": method, "path": path, "params": params,
            "json": json_body, "headers": headers,
        })
        route = self.routes.get((method, path))
        if route is None:
            raise AssertionError(f"unexpected GitHub call: {method} {path}")
        if isinstance(route, Exception):
            raise route
        return route

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] != "GET"]


def _workflow_routes(overrides: dict | None = None) -> dict:
    routes = {
        ("GET", "/repos/acme/site"): _Response(payload={"default_branch": "main"}),
        ("GET", "/repos/acme/site/actions/workflows/deploy.yml"): _Response(payload=_WORKFLOW),
        ("GET", "/repos/acme/site/contents/.github/workflows/deploy.yml"): _Response(text=_WORKFLOW_FILE),
    }
    routes.update(overrides or {})
    return routes


def _preview(handler, params) -> dict:
    return {f["key"]: f["value"] for f in _run(handler.render_preview(params, _USER))}


# ---------------------------------------------------------------------------
# github_trigger_workflow
# ---------------------------------------------------------------------------


class TestTriggerWorkflowValidateParams:
    def test_normalizes(self):
        out = GitHubTriggerWorkflowHandler().validate_params({
            "owner": " acme ", "repo": "site",
            "workflow": ".github/workflows/deploy.yml", "ref": " main ",
            "inputs": {"environment": "staging", "dry_run": True, "retries": 3},
        })
        assert out == {
            "owner": "acme", "repo": "site", "workflow": "deploy.yml",
            "ref": "main",
            "inputs": {"environment": "staging", "dry_run": "true", "retries": "3"},
        }

    def test_numeric_workflow_id_and_optional_fields(self):
        out = GitHubTriggerWorkflowHandler().validate_params({**_REPO, "workflow": 161335})
        assert out == {**_REPO, "workflow": "161335"}

    @pytest.mark.parametrize("params,match", [
        ({"repo": "site", "workflow": "deploy.yml"}, "owner"),
        ({"owner": "acme", "repo": "acme/site", "workflow": "deploy.yml"}, "repo must be"),
        ({**_REPO}, "workflow"),
        ({**_REPO, "workflow": "Deploy to production"}, "file name"),
        ({**_REPO, "workflow": "../../issues/1"}, "file name"),
        ({**_REPO, "workflow": "deploy.yml", "ref": "a" * 40}, "commit SHA"),
        ({**_REPO, "workflow": "deploy.yml", "ref": "my branch"}, "branch or tag"),
        ({**_REPO, "workflow": "deploy.yml", "inputs": ["a"]}, "inputs must be an object"),
        ({**_REPO, "workflow": "deploy.yml", "inputs": {"a": {"b": 1}}}, "flat values"),
        ({**_REPO, "workflow": "deploy.yml", "inputs": {str(i): "v" for i in range(26)}}, "at most 25"),
        # Server-injected card fields are not model-suppliable.
        ({**_REPO, "workflow": "deploy.yml", "workflow_name": "Harmless lint"}, "Unknown parameter"),
    ])
    def test_rejections(self, params, match):
        with pytest.raises(ValueError, match=match):
            GitHubTriggerWorkflowHandler().validate_params(params)


class TestTriggerWorkflowProposal:
    def _propose(self, params):
        handler = GitHubTriggerWorkflowHandler()
        return _run(handler.validate_against_upstream(handler.validate_params(params), _USER))

    def test_card_names_the_workflow(self, monkeypatch):
        """The card shows the workflow's GitHub name, not the raw file/id."""
        github = _FakeGitHub(monkeypatch, _workflow_routes())
        params = self._propose({
            **_REPO, "workflow": "deploy.yml", "ref": "release",
            "inputs": {"environment": "staging", "dry_run": False},
        })
        handler = GitHubTriggerWorkflowHandler()
        assert _preview(handler, params) == {
            "Workflow": "Deploy to production (deploy.yml)",
            "Repository": "acme/site",
            "Ref": "release",
            "Inputs": "environment = staging\ndry_run = false",
        }
        assert handler.summary_snippet(params) == (
            "Deploy to production (deploy.yml) on acme/site@release"
        )
        assert handler.display_name == "Run GitHub Workflow"
        # The dispatchability check read the workflow file on the chosen ref.
        file_call = github.calls[-1]
        assert file_call["params"] == {"ref": "release"}
        assert not github.writes()

    def test_numeric_id_resolves_to_name(self, monkeypatch):
        _FakeGitHub(monkeypatch, _workflow_routes({
            ("GET", "/repos/acme/site/actions/workflows/161335"): _Response(payload=_WORKFLOW),
        }))
        params = self._propose({**_REPO, "workflow": "161335", "ref": "main"})
        assert _preview(GitHubTriggerWorkflowHandler(), params)["Workflow"] == (
            "Deploy to production (deploy.yml)"
        )

    def test_unnamed_workflow_shows_file_name(self, monkeypatch):
        # A workflow file without `name:` reports its path as the name.
        unnamed = {**_WORKFLOW, "name": ".github/workflows/deploy.yml"}
        _FakeGitHub(monkeypatch, _workflow_routes({
            ("GET", "/repos/acme/site/actions/workflows/deploy.yml"): _Response(payload=unnamed),
        }))
        params = self._propose({**_REPO, "workflow": "deploy.yml", "ref": "main"})
        assert _preview(GitHubTriggerWorkflowHandler(), params)["Workflow"] == "deploy.yml"

    def test_default_branch_filled_in(self, monkeypatch):
        _FakeGitHub(monkeypatch, _workflow_routes())
        params = self._propose({**_REPO, "workflow": "deploy.yml"})
        assert params["ref"] == "main"
        preview = _preview(GitHubTriggerWorkflowHandler(), params)
        assert preview["Ref"] == "main (default branch)"
        assert preview["Inputs"] == "none (the workflow's defaults apply)"

    def test_unresolvable_default_branch_rejected(self, monkeypatch):
        _FakeGitHub(monkeypatch, _workflow_routes({
            ("GET", "/repos/acme/site"): _Response(status_code=500, payload={"message": "boom"}),
        }))
        with pytest.raises(ValueError, match="pass ref"):
            self._propose({**_REPO, "workflow": "deploy.yml"})

    @pytest.mark.parametrize("override,match", [
        ({("GET", "/repos/acme/site/actions/workflows/deploy.yml"):
            _Response(status_code=404, payload={"message": "Not Found"})}, "not found"),
        ({("GET", "/repos/acme/site/actions/workflows/deploy.yml"):
            _Response(payload={**_WORKFLOW, "state": "disabled_manually"})}, "not active"),
        ({("GET", "/repos/acme/site/contents/.github/workflows/deploy.yml"):
            _Response(status_code=404, payload={"message": "Not Found"})}, "does not exist on ref"),
        ({("GET", "/repos/acme/site/contents/.github/workflows/deploy.yml"):
            _Response(text="on:\n  push:\n")}, "workflow_dispatch"),
    ])
    def test_same_turn_rejections(self, monkeypatch, override, match):
        _FakeGitHub(monkeypatch, _workflow_routes(override))
        with pytest.raises(ValueError, match=match):
            self._propose({**_REPO, "workflow": "deploy.yml", "ref": "main"})

    @pytest.mark.parametrize("failure", [
        _Response(status_code=503, payload={"message": "unavailable"}),
        _Response(status_code=403, payload={"message": "rate limited"}),
        GitHubAuthError("GitHub not connected"),
        RuntimeError("network down"),
    ])
    def test_transient_failures_defer_to_execute(self, monkeypatch, failure):
        _FakeGitHub(monkeypatch, _workflow_routes({
            ("GET", "/repos/acme/site/actions/workflows/deploy.yml"): failure,
        }))
        params = self._propose({**_REPO, "workflow": "deploy.yml", "ref": "main"})
        assert "workflow_name" not in params
        # The card falls back to the identifier the model supplied.
        assert _preview(GitHubTriggerWorkflowHandler(), params)["Workflow"] == "deploy.yml"


class TestTriggerWorkflowInputValidation:
    """Inputs are checked against the workflow file's declared inputs
    (GitHub has no dry-run endpoint for a dispatch)."""

    def _propose(self, monkeypatch, inputs=None, *, file_text=_WORKFLOW_FILE):
        _FakeGitHub(monkeypatch, _workflow_routes({_FILE_ROUTE: _Response(text=file_text)}))
        handler = GitHubTriggerWorkflowHandler()
        raw = {**_REPO, "workflow": "deploy.yml", "ref": "main"}
        if inputs is not None:
            raw["inputs"] = inputs
        return _run(handler.validate_against_upstream(handler.validate_params(raw), _USER))

    def test_declared_inputs_accepted(self, monkeypatch):
        params = self._propose(monkeypatch, {
            "environment": "production", "dry_run": True, "replicas": 3, "note": "hi",
        })
        assert params["inputs"] == {
            "environment": "production", "dry_run": "true", "replicas": "3", "note": "hi",
        }
        # Nothing is required here, so no inputs at all is fine too.
        assert "inputs" not in self._propose(monkeypatch)

    def test_unknown_input_lists_the_declared_ones(self, monkeypatch):
        with pytest.raises(ValueError) as exc:
            self._propose(monkeypatch, {"env": "production"})
        message = str(exc.value)
        assert "Unknown workflow input(s) ['env']" in message
        assert ".github/workflows/deploy.yml on ref 'main'" in message
        assert "environment (choice: staging | production, default staging)" in message
        assert "dry_run (boolean, default false)" in message
        assert "replicas (number)" in message
        assert "note (string)" in message

    @pytest.mark.parametrize("inputs,match", [
        ({"environment": "prod"}, r"must be one of \['staging', 'production'\]"),
        ({"dry_run": "yes"}, "boolean input"),
        ({"replicas": "three"}, "number input"),
        # Input names are matched exactly.
        ({"Environment": "staging"}, "Unknown workflow input"),
    ])
    def test_value_type_rejections(self, monkeypatch, inputs, match):
        with pytest.raises(ValueError, match=match):
            self._propose(monkeypatch, inputs)

    def test_missing_required_input(self, monkeypatch):
        text = (
            "on:\n  workflow_dispatch:\n    inputs:\n"
            "      version:\n        required: true\n"
            "      channel:\n        required: true\n        default: stable\n"
        )
        with pytest.raises(ValueError) as exc:
            self._propose(monkeypatch, {"channel": "beta"}, file_text=text)
        assert "Missing required workflow input(s) ['version']" in str(exc.value)
        assert "version (string, required)" in str(exc.value)
        # A required input with a default may be left out.
        self._propose(monkeypatch, {"version": "1.2.3"}, file_text=text)

    @pytest.mark.parametrize("file_text", [
        "on: workflow_dispatch\n",
        "on: [push, workflow_dispatch]\n",
        "on:\n  workflow_dispatch:\n",
        "on:\n  workflow_dispatch: {}\n",
        '"on":\n  workflow_dispatch:\n    inputs: {}\n',
    ])
    def test_trigger_without_inputs(self, monkeypatch, file_text):
        self._propose(monkeypatch, file_text=file_text)
        with pytest.raises(ValueError, match="declares no inputs"):
            self._propose(monkeypatch, {"environment": "staging"}, file_text=file_text)

    @pytest.mark.parametrize("file_text", [
        "on: push\n",
        "on: [push, pull_request]\n",
        # The trigger name only appears in a comment / a step.
        "# workflow_dispatch was removed\non:\n  push:\njobs: {}\n",
    ])
    def test_no_dispatch_trigger(self, monkeypatch, file_text):
        with pytest.raises(ValueError, match="no 'workflow_dispatch' trigger"):
            self._propose(monkeypatch, file_text=file_text)

    def test_unparseable_file_leaves_inputs_to_github(self, monkeypatch):
        broken = "on:\n  workflow_dispatch:\n\tinputs: [unclosed\n"
        params = self._propose(monkeypatch, {"anything": "goes"}, file_text=broken)
        assert params["inputs"] == {"anything": "goes"}
        # ...but a file that never mentions the trigger is still rejected.
        with pytest.raises(ValueError, match="no 'workflow_dispatch' trigger"):
            self._propose(monkeypatch, file_text="on:\n\tpush: [unclosed\n")


class TestTriggerWorkflowExecute:
    _PARAMS = {
        **_REPO, "workflow": "deploy.yml", "ref": "main",
        "inputs": {"environment": "staging"},
        "workflow_name": "Deploy to production",
        "workflow_path": ".github/workflows/deploy.yml",
    }
    _DISPATCH = ("POST", "/repos/acme/site/actions/workflows/deploy.yml/dispatches")

    def test_dispatches_ref_and_inputs(self, monkeypatch):
        github = _FakeGitHub(monkeypatch, {self._DISPATCH: _Response(payload={
            "workflow_run_id": 991,
            "run_url": "https://api.github.com/repos/acme/site/actions/runs/991",
            "html_url": "https://github.com/acme/site/actions/runs/991",
        })})
        out = _run(GitHubTriggerWorkflowHandler().execute(dict(self._PARAMS), _USER))
        assert github.writes()[0]["json"] == {
            "ref": "main", "inputs": {"environment": "staging"},
        }
        assert out == {
            "success": True,
            "repository": "acme/site",
            "workflow": "Deploy to production (deploy.yml)",
            "ref": "main",
            "run_id": 991,
            "url": "https://github.com/acme/site/actions/runs/991",
            "message": "Workflow run started.",
        }

    def test_bodyless_204_points_at_the_runs_list(self, monkeypatch):
        github = _FakeGitHub(monkeypatch, {self._DISPATCH: _Response(status_code=204)})
        params = {**_REPO, "workflow": "deploy.yml", "ref": "main"}
        out = _run(GitHubTriggerWorkflowHandler().execute(params, _USER))
        assert github.writes()[0]["json"] == {"ref": "main"}
        assert out["success"] is True and "run_id" not in out
        assert "actions/workflows/deploy.yml/runs" in out["message"]

    def test_github_rejection_keeps_request_open(self, monkeypatch):
        _FakeGitHub(monkeypatch, {self._DISPATCH: _Response(status_code=422, payload={
            "message": "Unexpected inputs provided: [\"environment\"]",
        })})
        with pytest.raises(RuntimeError, match="Unexpected inputs provided"):
            _run(GitHubTriggerWorkflowHandler().execute(dict(self._PARAMS), _USER))

    def test_permission_failure_carries_hint(self, monkeypatch):
        _FakeGitHub(monkeypatch, {self._DISPATCH: _Response(status_code=403, payload={
            "message": "Resource not accessible by integration",
        })})
        with pytest.raises(RuntimeError, match="write access"):
            _run(GitHubTriggerWorkflowHandler().execute(dict(self._PARAMS), _USER))

    def test_not_connected(self, monkeypatch):
        _FakeGitHub(monkeypatch, {self._DISPATCH: GitHubAuthError("GitHub not connected")})
        with pytest.raises(RuntimeError, match="GitHub not connected"):
            _run(GitHubTriggerWorkflowHandler().execute(dict(self._PARAMS), _USER))


# ---------------------------------------------------------------------------
# github_comment_on_issue
# ---------------------------------------------------------------------------

_ISSUE_PATH = "/repos/acme/site/issues/42"
_ISSUE = {"number": 42, "title": "Login page 500s", "state": "open"}
_PULL = {**_ISSUE, "title": "Fix login", "pull_request": {"url": "..."}}


class TestCommentOnIssue:
    def test_validate_params(self):
        handler = GitHubCommentOnIssueHandler()
        assert handler.validate_params({**_REPO, "issue_number": "#42", "body": " hi "}) == {
            **_REPO, "issue_number": 42, "body": "hi",
        }

    @pytest.mark.parametrize("params,match", [
        ({**_REPO, "body": "hi"}, "issue_number"),
        ({**_REPO, "issue_number": 0, "body": "hi"}, "positive"),
        ({**_REPO, "issue_number": True, "body": "hi"}, "issue_number"),
        ({**_REPO, "issue_number": "forty-two", "body": "hi"}, "positive"),
        ({**_REPO, "issue_number": 42}, "body"),
        ({**_REPO, "issue_number": 42, "body": "  "}, "body"),
        ({**_REPO, "issue_number": 42, "body": "x" * 65537}, "65536"),
        ({**_REPO, "issue_number": 42, "body": "hi", "issue_title": "x"}, "Unknown parameter"),
    ])
    def test_rejections(self, params, match):
        with pytest.raises(ValueError, match=match):
            GitHubCommentOnIssueHandler().validate_params(params)

    def test_card_shows_issue_title(self, monkeypatch):
        _FakeGitHub(monkeypatch, {("GET", _ISSUE_PATH): _Response(payload=_ISSUE)})
        handler = GitHubCommentOnIssueHandler()
        params = handler.validate_params({**_REPO, "issue_number": 42, "body": "On it."})
        params = _run(handler.validate_against_upstream(params, _USER))
        assert _preview(handler, params) == {
            "Repository": "acme/site",
            "Issue": "#42 Login page 500s (open)",
            "Comment": "On it.",
        }
        assert handler.summary_snippet(params) == "acme/site#42: On it."
        # Multi-line markdown collapses to one line on the collapsed card.
        assert handler.summary_snippet({**params, "body": "Fixed.\n\nPlease retest."}) == (
            "acme/site#42: Fixed. Please retest."
        )

    def test_pull_request_is_labelled(self, monkeypatch):
        _FakeGitHub(monkeypatch, {("GET", _ISSUE_PATH): _Response(payload=_PULL)})
        handler = GitHubCommentOnIssueHandler()
        params = handler.validate_params({**_REPO, "issue_number": 42, "body": "LGTM"})
        params = _run(handler.validate_against_upstream(params, _USER))
        assert _preview(handler, params)["Pull request"] == "#42 Fix login (open)"

    def test_missing_issue_rejected_same_turn(self, monkeypatch):
        _FakeGitHub(monkeypatch, {
            ("GET", _ISSUE_PATH): _Response(status_code=404, payload={"message": "Not Found"}),
        })
        handler = GitHubCommentOnIssueHandler()
        params = handler.validate_params({**_REPO, "issue_number": 42, "body": "hi"})
        with pytest.raises(ValueError, match="#42 not found in acme/site"):
            _run(handler.validate_against_upstream(params, _USER))

    def test_execute_posts_the_body(self, monkeypatch):
        github = _FakeGitHub(monkeypatch, {
            ("POST", f"{_ISSUE_PATH}/comments"): _Response(status_code=201, payload={
                "id": 5, "html_url": "https://github.com/acme/site/issues/42#issuecomment-5",
            }),
        })
        out = _run(GitHubCommentOnIssueHandler().execute(
            {**_REPO, "issue_number": 42, "body": "On it."}, _USER,
        ))
        assert github.writes()[0]["json"] == {"body": "On it."}
        assert out == {
            "success": True, "repository": "acme/site", "issue_number": 42,
            "comment_id": 5,
            "url": "https://github.com/acme/site/issues/42#issuecomment-5",
        }

    def test_execute_failure(self, monkeypatch):
        _FakeGitHub(monkeypatch, {
            ("POST", f"{_ISSUE_PATH}/comments"): _Response(status_code=410, payload={
                "message": "Issues are disabled for this repo",
            }),
        })
        with pytest.raises(RuntimeError, match="Failed to comment on acme/site#42.*disabled"):
            _run(GitHubCommentOnIssueHandler().execute(
                {**_REPO, "issue_number": 42, "body": "hi"}, _USER,
            ))


# ---------------------------------------------------------------------------
# github_set_issue_state
# ---------------------------------------------------------------------------


class TestSetIssueState:
    def test_validate_params(self):
        handler = GitHubSetIssueStateHandler()
        assert handler.validate_params({**_REPO, "issue_number": 42, "state": "Closed"}) == {
            **_REPO, "issue_number": 42, "state": "closed", "state_reason": "completed",
        }
        assert handler.validate_params({
            **_REPO, "issue_number": 42, "state": "closed", "state_reason": "not_planned",
        })["state_reason"] == "not_planned"
        assert handler.validate_params({**_REPO, "issue_number": 42, "state": "open"}) == {
            **_REPO, "issue_number": 42, "state": "open",
        }

    @pytest.mark.parametrize("params,match", [
        ({**_REPO, "issue_number": 42}, "state must be"),
        ({**_REPO, "issue_number": 42, "state": "resolved"}, "state must be"),
        ({**_REPO, "issue_number": 42, "state": "open", "state_reason": "completed"}, "only applies"),
        ({**_REPO, "issue_number": 42, "state": "closed", "state_reason": "wontfix"}, "state_reason must be"),
        ({**_REPO, "issue_number": 42, "state": "closed", "comment": "bye"}, "Unknown parameter"),
    ])
    def test_rejections(self, params, match):
        with pytest.raises(ValueError, match=match):
            GitHubSetIssueStateHandler().validate_params(params)

    def _propose(self, params):
        handler = GitHubSetIssueStateHandler()
        return _run(handler.validate_against_upstream(handler.validate_params(params), _USER))

    def test_card_shows_transition(self, monkeypatch):
        _FakeGitHub(monkeypatch, {("GET", _ISSUE_PATH): _Response(payload=_ISSUE)})
        params = self._propose({
            **_REPO, "issue_number": 42, "state": "closed", "state_reason": "not_planned",
        })
        handler = GitHubSetIssueStateHandler()
        assert _preview(handler, params) == {
            "Repository": "acme/site",
            "Issue": "#42 Login page 500s",
            "Current state": "open",
            "New state": "closed (not planned)",
        }
        assert handler.summary_snippet(params) == "Close acme/site#42 Login page 500s"

    def test_reopen_labels(self, monkeypatch):
        _FakeGitHub(monkeypatch, {
            ("GET", _ISSUE_PATH): _Response(payload={**_ISSUE, "state": "closed"}),
        })
        params = self._propose({**_REPO, "issue_number": 42, "state": "open"})
        handler = GitHubSetIssueStateHandler()
        assert _preview(handler, params)["New state"] == "open (reopened)"
        assert handler.summary_snippet(params).startswith("Reopen acme/site#42")

    def test_pull_request_and_noop_rejected_same_turn(self, monkeypatch):
        _FakeGitHub(monkeypatch, {("GET", _ISSUE_PATH): _Response(payload=_PULL)})
        with pytest.raises(ValueError, match="pull request"):
            self._propose({**_REPO, "issue_number": 42, "state": "closed"})

        _FakeGitHub(monkeypatch, {("GET", _ISSUE_PATH): _Response(payload=_ISSUE)})
        with pytest.raises(ValueError, match="already open"):
            self._propose({**_REPO, "issue_number": 42, "state": "open"})

    def test_execute_patches_state(self, monkeypatch):
        github = _FakeGitHub(monkeypatch, {
            ("GET", _ISSUE_PATH): _Response(payload=_ISSUE),
            ("PATCH", _ISSUE_PATH): _Response(payload={
                "state": "closed", "state_reason": "not_planned",
                "html_url": "https://github.com/acme/site/issues/42",
            }),
        })
        out = _run(GitHubSetIssueStateHandler().execute({
            **_REPO, "issue_number": 42, "state": "closed", "state_reason": "not_planned",
        }, _USER))
        assert github.writes()[0]["json"] == {"state": "closed", "state_reason": "not_planned"}
        assert out == {
            "success": True, "repository": "acme/site", "issue_number": 42,
            "state": "closed", "state_reason": "not_planned",
            "url": "https://github.com/acme/site/issues/42",
        }

    def test_execute_reopen_sends_no_reason(self, monkeypatch):
        github = _FakeGitHub(monkeypatch, {
            ("GET", _ISSUE_PATH): _Response(payload={**_ISSUE, "state": "closed"}),
            ("PATCH", _ISSUE_PATH): _Response(payload={"state": "open", "state_reason": "reopened"}),
        })
        _run(GitHubSetIssueStateHandler().execute(
            {**_REPO, "issue_number": 42, "state": "open"}, _USER,
        ))
        assert github.writes()[0]["json"] == {"state": "open"}

    def test_execute_refuses_pull_request(self, monkeypatch):
        """Approve-time guard for a proposal whose GitHub read was deferred."""
        github = _FakeGitHub(monkeypatch, {("GET", _ISSUE_PATH): _Response(payload=_PULL)})
        with pytest.raises(RuntimeError, match="pull request"):
            _run(GitHubSetIssueStateHandler().execute(
                {**_REPO, "issue_number": 42, "state": "closed"}, _USER,
            ))
        assert not github.writes()


# ---------------------------------------------------------------------------
# Registration + skill content + the shared request helper
# ---------------------------------------------------------------------------

_TYPE_NAMES = (
    "github_trigger_workflow",
    "github_comment_on_issue",
    "github_set_issue_state",
)


def test_types_registered_by_the_plugin(github_plugin):
    from chat.action_request_types import get_handler
    from chat.llm.tool_schemas import ACTION_REQUEST_TYPE_ENUM

    for name in _TYPE_NAMES:
        assert get_handler(name) is not None
        assert name in ACTION_REQUEST_TYPE_ENUM


def test_skill_documents_the_write_types():
    content = (Path(handlers_mod.__file__).parent / "instructions.md").read_text()
    for name in _TYPE_NAMES:
        assert f"`{name}`" in content
    # The sub-agent escape-hatch reminder every write-capable skill carries.
    assert "agent_task_response" in content


def test_github_request_requires_a_connection():
    with pytest.raises(GitHubAuthError):
        _run(upstream_mod.github_request({"id": 1}, "GET", "/user"))


def test_github_request_sends_bearer_and_default_headers(monkeypatch):
    seen = {}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, *, params=None, json=None, headers=None):
            seen.update(method=method, url=url, json=json, headers=dict(headers))
            return _Response(payload={})

    monkeypatch.setattr(upstream_mod.httpx, "AsyncClient", _Client)
    _run(upstream_mod.github_request(
        _USER, "POST", "/repos/acme/site/issues/42/comments", json_body={"body": "hi"},
    ))
    assert seen["url"] == "https://api.github.com/repos/acme/site/issues/42/comments"
    assert seen["json"] == {"body": "hi"}
    assert seen["headers"]["Authorization"] == "Bearer gho_testtoken"
    assert seen["headers"]["User-Agent"] == "Quest/1.0"


def test_github_error_message_flattens_errors():
    resp = _Response(status_code=422, payload={
        "message": "Validation Failed",
        "errors": [{"resource": "Issue", "field": "state", "code": "invalid"}, "plain"],
    })
    assert github_error_message(resp) == (
        "HTTP 422: Validation Failed; Issue, state, invalid; plain"
    )
    assert github_error_message(_Response(status_code=502, text="Bad Gateway")) == (
        "HTTP 502: Bad Gateway"
    )
