"""GitHub dynamic tools: github_get_job_log.

Per-job GitHub Actions logs are served as a 302 redirect to a short-lived
signed URL on a third-party host, which the generic ``authed_get`` proxy
deliberately refuses to follow -- so the two-hop download lives in a
dedicated gated plugin tool (previously the core ``get_github_job_log``
tool; renamed for the ``<plugin id>_`` prefix rule -- tool names have no
persistence, so the rename is free).
"""

import json
import os
from pathlib import Path

import httpx

from config.plugin_types import PluginTool

# Preview size returned inline with the job-log tool result. Kept well under
# the large-tool-result threshold (2 KB) so the tool response itself never
# trips _log_large_tool_result.
_JOB_LOG_PREVIEW_BYTES = 500


async def _handle_github_get_job_log(ctx, args: dict) -> str:
    """Download a GitHub Actions job log to the conversation workspace.

    The GitHub REST API returns job logs via the
    ``/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` endpoint, which
    responds with a 302 redirect to a short-lived signed URL (typically on
    an Azure Blob Storage or GitHub Actions results host) that carries the
    actual plain-text log payload. The dedicated tool handles this flow:

    1. Call ``_make_authed_request`` with ``raw_response=True`` so we see
       the 302 directly (the allow-list already covers the ``/logs`` path).
    2. If the response is a 302, issue a second GET against the
       ``Location`` URL using a fresh ``httpx.AsyncClient`` with
       ``follow_redirects=False`` and **no** ``Authorization`` header. The
       signed query params carry their own auth and leaking the user's
       GitHub bearer token to a third-party host would be a security bug.
    3. Decode the body as UTF-8 (with ``errors="replace"``) and apply the
       same size gate as ``handle_authed_get``.
    4. On success, write the log into the conversation workspace. By
       default the file lands in ``github-job-logs/github-job-{job_id}.log``
       so that many downloads do not clutter the workspace root; callers
       may override the destination via ``path``. The response JSON
       carries a short preview (first ~500 bytes) so the model can react
       without a follow-up ``get_workspace_file`` call.
    """
    from chat.gemini_api.authed_get import _make_authed_request
    from chat.gemini_api.constants import AUTHED_GET_SIZE_LIMIT
    from chat.gemini_api.tool_handlers import (
        conversation_workspace_dir,
        _publish_file_list_changed,
    )

    owner = args.get("owner")
    repo = args.get("repo")
    job_id = args.get("job_id")
    path = args.get("path")
    force_large_response = bool(args.get("force_large_response"))

    # --- Parameter validation --------------------------------------------
    if not owner or not repo or job_id in (None, ""):
        return json.dumps({
            "error": "owner, repo, and job_id are required.",
        })

    if not ctx.conversation_id:
        return json.dumps({
            "error": (
                "github_get_job_log requires a conversation workspace, "
                "which this call has no access to."
            ),
        })

    # Normalise job_id to a string and verify it is numeric so we fail fast
    # rather than relying on the allow-list regex for what is effectively a
    # caller error.
    job_id_str = str(job_id).strip()
    if not job_id_str.isdigit():
        return json.dumps({
            "error": f"Invalid job_id '{job_id_str}': must be a numeric GitHub job ID.",
        })

    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/jobs/{job_id_str}/logs"
    )

    # --- Step 1: initial request (may return 302, 200, or JSON error) ---
    initial = await _make_authed_request(url, user=ctx.user, raw_response=True)

    # A string return value indicates an error from _make_authed_request
    # (disallowed path, missing credentials, upstream 4xx/5xx, etc.).
    if isinstance(initial, str):
        return initial

    log_text: str | None = None

    if initial.status_code == 200:
        # Some small jobs return logs inline without the redirect dance.
        try:
            log_text = initial.content.decode("utf-8", errors="replace")
        except Exception as exc:
            return json.dumps({
                "error": f"Failed to decode job log body: {exc}",
            })

    elif initial.status_code == 302:
        location = initial.headers.get("Location") or initial.headers.get("location")
        if not location:
            return json.dumps({
                "error": "GitHub returned a 302 with no Location header for the job log.",
            })

        # Fetch the signed URL with a fresh client and no auth headers.
        # Signed URLs reject Authorization: Bearer, and more importantly we
        # must never leak the user's GitHub token to a third-party host.
        try:
            async with httpx.AsyncClient(
                timeout=60.0, follow_redirects=False,
            ) as signed_client:
                signed_resp = await signed_client.get(location)
        except httpx.TimeoutException:
            return json.dumps({
                "error": "Timed out fetching GitHub job log from signed URL.",
            })
        except Exception as exc:
            return json.dumps({
                "error": f"Failed to fetch GitHub job log from signed URL: {exc}",
            })

        if signed_resp.status_code >= 400:
            return json.dumps({
                "error": {
                    "status_code": signed_resp.status_code,
                    "service": "GitHub Actions job log (signed URL)",
                    "response": signed_resp.text[:500],
                },
            })

        if signed_resp.status_code in (301, 302, 303, 307, 308):
            return json.dumps({
                "error": (
                    "GitHub Actions job log signed URL returned a second "
                    "redirect; refusing to follow multiple hops."
                ),
            })

        try:
            log_text = signed_resp.content.decode("utf-8", errors="replace")
        except Exception as exc:
            return json.dumps({
                "error": f"Failed to decode job log body from signed URL: {exc}",
            })

    else:
        # Unexpected status (1xx, 3xx other than 302, etc.). 4xx/5xx are
        # already mapped to JSON error strings by _make_authed_request.
        return json.dumps({
            "error": {
                "status_code": initial.status_code,
                "service": "GitHub Actions job log",
                "response": getattr(initial, "text", "")[:500],
            },
        })

    if log_text is None:
        return json.dumps({
            "error": "Unable to retrieve GitHub Actions job log (no content).",
        })

    # --- Step 2: size gate ------------------------------------------------
    size_bytes = len(log_text.encode("utf-8"))

    default_filename = f"github-job-{job_id_str}.log"

    if size_bytes > AUTHED_GET_SIZE_LIMIT and not force_large_response:
        return json.dumps({
            "error": "response_too_large",
            "response_size_bytes": size_bytes,
            "size_limit_bytes": AUTHED_GET_SIZE_LIMIT,
            "message": (
                "The GitHub Actions job log is larger than the response size "
                "limit. Retry with force_large_response=true to write the "
                "full log to the conversation workspace as "
                f"github-job-logs/{default_filename}."
            ),
        })

    # --- Step 3: persist to workspace ------------------------------------
    try:
        workspace_dir = await conversation_workspace_dir(ctx.conversation_id)
    except Exception as exc:
        return json.dumps({
            "error": f"Failed to resolve workspace directory: {exc}",
        })

    workspace_root = workspace_dir.resolve()

    # --- Resolve destination path ----------------------------------------
    #
    # Default: github-job-logs/github-job-{job_id}.log
    # Caller override via ``path``:
    #   * ends with '/' or names an existing directory -> dir + default name
    #   * otherwise treated as the full file path (caller picks filename)
    #
    # Traversal outside the workspace and absolute paths are rejected.
    if path is None or path == "":
        rel_path = Path("github-job-logs") / default_filename
    else:
        # Reject absolute paths up-front so we never silently rebase them
        # against the workspace root. ``Path.is_absolute`` catches both
        # POSIX ``/foo`` and Windows-style roots.
        candidate = Path(path)
        if candidate.is_absolute():
            return json.dumps({
                "error": (
                    "Invalid path: absolute paths are not allowed. "
                    "Provide a workspace-relative path."
                ),
            })

        # Reject any explicit '..' segment before resolving, so we surface
        # a clear error even if the resolved path happens to stay inside
        # the workspace by accident.
        if any(part == ".." for part in candidate.parts):
            return json.dumps({
                "error": (
                    "Invalid path: parent-directory traversal ('..') "
                    "is not allowed."
                ),
            })

        # Directory vs file resolution. A trailing slash in the raw string
        # or an already-existing directory in the workspace both mean
        # "drop the default filename inside this directory".
        treat_as_dir = path.endswith("/") or path.endswith(os.sep)
        if not treat_as_dir:
            probe = (workspace_root / candidate)
            if probe.exists() and probe.is_dir():
                treat_as_dir = True

        if treat_as_dir:
            rel_path = candidate / default_filename
        else:
            rel_path = candidate

    file_path = (workspace_root / rel_path).resolve()

    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        return json.dumps({
            "error": (
                "Invalid path: resolved destination is outside the "
                "conversation workspace."
            ),
        })

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(log_text, encoding="utf-8")
    except Exception as exc:
        return json.dumps({
            "error": f"Failed to save job log to workspace: {exc}",
        })

    _publish_file_list_changed(
        ctx.user["id"], "conversation", ctx.conversation_id, ctx.project_id,
    )

    # Workspace-relative path as a forward-slash string for the model.
    rel_written = file_path.relative_to(workspace_root).as_posix()
    filename = file_path.name

    # Short inline preview so the LLM can diagnose without a follow-up read.
    preview = log_text[:_JOB_LOG_PREVIEW_BYTES]

    return json.dumps({
        "status": "success",
        "filename": filename,
        "path": rel_written,
        "size_bytes": size_bytes,
        "preview": preview,
        "message": (
            f"GitHub Actions job log for job {job_id_str} saved to the "
            f"workspace as '{rel_written}' ({size_bytes} bytes). Use "
            f'tool_call(tool_name="get_workspace_file", arguments={{"path": "{rel_written}"}}) '
            "to read the full content, or run_python / run_script to grep it."
        ),
    })


GITHUB_GET_JOB_LOG_TOOL = PluginTool(
    spec={
        "name": "github_get_job_log",
        "description": (
            "Download a GitHub Actions job log to the conversation workspace. "
            "Use this to inspect the full stdout/stderr of a failed (or "
            "passing) CI job after finding the job_id via authed_get on "
            "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs. The GitHub "
            "API serves logs as plain text via a 302 redirect to a signed "
            "URL, so this endpoint is NOT reachable via authed_get -- use "
            "this tool instead. "
            "By default the log is saved under "
            "'github-job-logs/github-job-{job_id}.log' in the workspace so "
            "many downloads do not clutter the workspace root. Pass the "
            "optional 'path' arg to override the destination. "
            "A short preview (first ~500 bytes) is returned inline. "
            "Read the full log afterwards with get_workspace_file, or use "
            "run_python / run_script to grep it. "
            "Requires GitHub to be connected in Settings > Data Connections. "
            "Logs larger than the ~3KB size limit are rejected unless "
            "force_large_response=true is passed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "The GitHub repository owner (user or organization).",
                },
                "repo": {
                    "type": "string",
                    "description": "The GitHub repository name.",
                },
                "job_id": {
                    "type": "string",
                    "description": (
                        "The numeric GitHub Actions job ID (passed as a string to "
                        "match how other GitHub IDs are handled). Get this from "
                        "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional workspace-relative destination path. If omitted, "
                        "the log is written to 'github-job-logs/github-job-{job_id}.log' "
                        "in the conversation workspace (the 'github-job-logs' folder is "
                        "created automatically). If provided and it ends with '/' or "
                        "names an existing directory, the log is placed inside that "
                        "directory as 'github-job-{job_id}.log'. Otherwise the value "
                        "is treated as the full file path (the caller picks the "
                        "filename). Absolute paths and '..' traversal outside the "
                        "workspace are rejected."
                    ),
                },
                "force_large_response": {
                    "type": "boolean",
                    "description": (
                        "Set to true to allow logs larger than the default size "
                        "limit to be written to the workspace. Only use this "
                        "after an initial call returns response_too_large."
                    ),
                },
                "intent_message": {
                    "type": "string",
                    "description": (
                        "A brief, user-friendly summary of your intent "
                        "(max 50 characters). Example: 'Read failed CI log'."
                    ),
                },
            },
            "required": ["owner", "repo", "job_id"],
        },
    },
    handler=_handle_github_get_job_log,
    requires_service="github",
)

ALL_TOOLS = (GITHUB_GET_JOB_LOG_TOOL,)
