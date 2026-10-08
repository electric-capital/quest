"""Podman sandbox handlers: run_script and run_python plus the shared argv
builder.
"""

import asyncio
import json
import logging
import os
import shlex

from chat.gemini_api.constants import (
    get_script_runner_image,
    get_public_script_runner_image,
    SCRIPT_RUNNER_TIMEOUT,
    SCRIPT_RUNNER_MAX_TIMEOUT,
    SCRIPT_RUNNER_MAX_OUTPUT,
    get_sandbox_port,
)
from chat.gemini_api.sandbox_runtime import get_sandbox_runtime
from chat.gemini_api.sandbox_seccomp import get_sandbox_seccomp_profile_path
from chat.sandbox_tokens import TOKEN_GRACE_SECONDS, sandbox_token_lease
from chat.gemini_api.tool_handlers._common import (
    conversation_workspace_dir,
    _publish_file_list_changed,
)

logger = logging.getLogger(__name__)


def _build_script_podman_cmd(
    workspace_dir,
    exec_cmd: list[str],
    sandbox_token: str,
    public: bool = False,
    interactive: bool = False,
) -> list[str]:
    """Build the ``podman run`` argv for a sandboxed script execution.

    Two profiles share the resource limits, workspace mount, the
    root-entrypoint privilege-drop choreography (--user=0:0 +
    NET_ADMIN/SETPCAP so the entrypoint can install iptables rules, then
    setpriv down to QUEST_RUN_UID/GID), and the no-symlink seccomp profile
    (the workspace mount is host-backed, so symlink creation is denied at
    the syscall level -- see chat/gemini_api/sandbox_seccomp.py; a profile
    generation failure fails the run rather than launching unconfined), but
    differ in image and network posture:

    - **Restricted (default):** ``quest-script-runner`` image. slirp4netns
      with ``outbound_addr=127.0.0.1`` kills all external egress and DNS
      (``--dns=none`` also keeps the host's resolv.conf out of the
      container); ``allow_host_loopback=true`` exposes the host at 10.0.2.2 so the
      entrypoint's socat bridge can forward localhost:<port> to the quest
      sandbox tool API, authenticated via the injected QUEST_API_KEY -- a
      per-run ephemeral token from chat/sandbox_tokens.py, never the
      user's long-lived ``users.api_key``.
    - **Public:** ``quest-script-runner-public`` image (separate Dockerfile
      and entrypoint). slirp4netns WITHOUT ``outbound_addr`` restores
      internet + DNS (pinned to the slirp resolver at 10.0.2.3 with no
      search domains, so the host's resolv.conf never leaks in);
      ``allow_host_loopback=false`` keeps the host
      unreachable. No QUEST_API_KEY / QUEST_PORT are injected -- the
      container has no way to authenticate to any internal API even if a
      network path existed. The public entrypoint installs iptables
      REJECTs for RFC1918 + link-local (LAN hosts, cloud metadata service)
      before dropping privileges.

    Both profiles run with IPv6 disabled (``enable_ipv6=false`` on the slirp
    network plus ``--sysctl net.ipv6.conf.all.disable_ipv6=1``): every
    confinement above is IPv4-only, and slirp's default IPv6 stack would
    otherwise bypass it.

    Args:
        workspace_dir: Host path mounted read-write at /workspace.
        exec_cmd: Command argv to run inside the container.
        sandbox_token: Ephemeral sandbox token minted for this run (see
            chat/sandbox_tokens.py). Ignored in the public profile.
        public: Select the public (internet-enabled) profile.
        interactive: Add ``-i`` (stdin piping, used by run_python).

    Returns:
        Full podman argv list.
    """
    host_uid = os.getuid()
    host_gid = os.getgid()

    cmd = ["podman"]
    # OCI runtime: crun when available (much cheaper container create and
    # teardown than runc), see chat/gemini_api/sandbox_runtime.py.
    runtime = get_sandbox_runtime()
    if runtime:
        cmd += ["--runtime", runtime]
    cmd += ["run", "--rm"]
    if interactive:
        cmd.append("-i")

    # DNS: by default podman derives the container's /etc/resolv.conf from
    # the host's, which leaks internal topology (VPC-internal search domains,
    # tailnet suffixes, internal nameserver IPs) into the sandbox. Override
    # it in both profiles:
    # - Public: resolve ONLY via the slirp4netns builtin resolver (10.0.2.3,
    #   which forwards to the host without exposing its config) and clear
    #   the host's search-domain list ("--dns-search=." means "no search
    #   domains").
    # - Restricted: DNS is dead anyway (outbound_addr kills egress), so
    #   don't write a resolv.conf at all ("--dns=none" leaves the image's
    #   own, i.e. none). localhost for the socat bridge comes from
    #   /etc/hosts, not DNS.
    #
    # IPv6 is switched off in BOTH profiles, twice over. slirp4netns enables
    # IPv6 by default and gives the container a ULA address with a default
    # route; ``outbound_addr`` and the entrypoints' iptables rules are
    # IPv4-only, so over IPv6 the host's loopback (fd00::2) and, on a host
    # with IPv6 routing, the internet were reachable unfiltered -- enough
    # to exfiltrate QUEST_API_KEY from the restricted profile. Neither
    # profile needs IPv6 (the socat bridge and slirp DNS are IPv4), so
    # ``enable_ipv6=false`` stops slirp from serving it and the sysctl
    # removes the stack from the container's network namespace entirely
    # (no addresses, AF_INET6 connects fail with EADDRNOTAVAIL). The
    # entrypoints additionally install a fail-closed ip6tables REJECT.
    if public:
        network = "slirp4netns:allow_host_loopback=false,enable_ipv6=false"
        dns_flags = ["--dns=10.0.2.3", "--dns-search=."]
    else:
        network = (
            "slirp4netns:allow_host_loopback=true,outbound_addr=127.0.0.1"
            ",enable_ipv6=false"
        )
        dns_flags = ["--dns=none"]

    cmd += [
        f"--network={network}",
        "--sysctl=net.ipv6.conf.all.disable_ipv6=1",
        *dns_flags,
        "--userns=keep-id",
        "--user=0:0",
        "--cap-add=NET_ADMIN",
        "--cap-add=SETPCAP",
        f"--security-opt=seccomp={get_sandbox_seccomp_profile_path()}",
        "-v", f"{workspace_dir}:/workspace:Z",
        "-w", "/workspace",
        "--memory=512m",
        "--cpus=1",
        "-e", "HOME=/tmp",
    ]

    if not public:
        cmd += [
            "-e", f"QUEST_API_KEY={sandbox_token}",
            # The sandbox tool API port, NOT the main server port: the
            # entrypoint's socat forwarder and iptables rules key on
            # QUEST_PORT, so this confines the container to the
            # script-facing endpoint surface (chat/sandbox_api.py).
            "-e", f"QUEST_PORT={get_sandbox_port()}",
        ]

    cmd += [
        "-e", f"QUEST_RUN_UID={host_uid}",
        "-e", f"QUEST_RUN_GID={host_gid}",
        get_public_script_runner_image() if public else get_script_runner_image(),
        *exec_cmd,
    ]
    return cmd


def _script_runner_image_error(public: bool) -> str:
    """Build the image-not-found error JSON for the active profile."""
    image = get_public_script_runner_image() if public else get_script_runner_image()
    dockerfile = (
        "Dockerfile.script-runner-public" if public else "Dockerfile.script-runner"
    )
    return json.dumps({
        "error": (
            f"Script runner image '{image}' not found. "
            f"Build it with: podman build -t {image} -f {dockerfile} ."
        )
    })


async def _handle_run_script(
    user_id: int,
    conversation_id: str,
    path: str,
    args: str = "",
    timeout: int | None = None,
    project_id: str | None = None,
    public: bool = False,
    block_mutating_tools: bool = False,
) -> str:
    """Run a script from the workspace inside an ephemeral Podman container.

    The default (restricted) profile uses the quest-script-runner image
    (Python 3.12), mounts the workspace read-write, and uses slirp4netns
    networking which blocks all external network access. The container's
    entrypoint runs a socat forwarder so scripts can reach the sandbox tool
    API at localhost (port from QUEST_PORT). A per-run ephemeral sandbox
    token (chat/sandbox_tokens.py) is injected as the QUEST_API_KEY
    environment variable for authentication; it is minted right before the
    container starts, expires with the container's timeout, and is revoked
    the moment the container exits. The public profile (``public=True``,
    public-project conversations) inverts this: internet egress with no
    proxy bridge and no token -- see _build_script_podman_cmd.

    Uses --userns=keep-id to map the host user's UID/GID into the container,
    ensuring files written by the script are owned by the correct user.

    Args:
        user_id: User's integer ID.
        conversation_id: Conversation UUID.
        path: Relative path to the script within the workspace.
        args: Optional command-line arguments to pass to the script.
        timeout: Execution timeout in seconds (default 60, max 150).
        project_id: Optional project UUID for project-aware workspace resolution.
        public: Run in the public-project sandbox profile (internet-enabled,
            no proxy bridge, no API key; see _build_script_podman_cmd).

    Returns:
        JSON string with path, exit_code, stdout, stderr, timed_out, and truncated.
    """
    try:
        # Resolve workspace directory
        workspace_dir = await conversation_workspace_dir(conversation_id)

        # Validate the path (same pattern as _handle_get_workspace_file)
        clean_path = path.lstrip("/").lstrip("\\")
        if ".." in clean_path:
            return json.dumps({"error": "Invalid path: path traversal not allowed"})

        file_path = (workspace_dir / clean_path).resolve()

        # Ensure resolved path is within workspace
        try:
            file_path.relative_to(workspace_dir.resolve())
        except ValueError:
            return json.dumps({"error": "Invalid path: outside workspace directory"})

        if not file_path.exists():
            return json.dumps({"error": f"File not found: {path}"})

        if not file_path.is_file():
            return json.dumps({"error": f"Not a file: {path}"})

        # Determine execution command based on file extension
        suffix = file_path.suffix.lower()
        if suffix == ".py":
            exec_cmd = ["python3", f"/workspace/{clean_path}"]
        elif suffix in (".sh", ".bash"):
            exec_cmd = ["bash", f"/workspace/{clean_path}"]
        else:
            exec_cmd = [f"/workspace/{clean_path}"]

        # Append script arguments if provided
        if args:
            exec_cmd.extend(shlex.split(args))

        # Clamp timeout
        clamped_timeout = timeout if timeout is not None else SCRIPT_RUNNER_TIMEOUT
        clamped_timeout = max(1, min(clamped_timeout, SCRIPT_RUNNER_MAX_TIMEOUT))

        # Execute the container under an ephemeral sandbox token: minted
        # here, valid for the clamped timeout (+ grace), revoked as soon as
        # the container exits -- see chat/sandbox_tokens.py. The public
        # profile never injects it (see _build_script_podman_cmd) but the
        # lease is harmless there and keeps the two paths identical.
        timed_out = False
        with sandbox_token_lease(
            user_id,
            ttl_seconds=clamped_timeout + TOKEN_GRACE_SECONDS,
            conversation_id=conversation_id,
            block_mutating_tools=block_mutating_tools,
        ) as sandbox_token:
            # Build the Podman command (see _build_script_podman_cmd for
            # the full networking / privilege-drop rationale of both
            # profiles).
            podman_cmd = _build_script_podman_cmd(
                workspace_dir, exec_cmd, sandbox_token, public=public,
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *podman_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=clamped_timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                timed_out = True
                stdout_bytes = b""
                stderr_bytes = b""
            except FileNotFoundError:
                return json.dumps({
                    "error": "Podman not found. The 'podman' command is not available on this system."
                })

        # Process output
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        truncated = False
        if len(stdout_str) > SCRIPT_RUNNER_MAX_OUTPUT:
            stdout_str = stdout_str[:SCRIPT_RUNNER_MAX_OUTPUT] + "\n... [output truncated at 256KB]"
            truncated = True
        if len(stderr_str) > SCRIPT_RUNNER_MAX_OUTPUT:
            stderr_str = stderr_str[:SCRIPT_RUNNER_MAX_OUTPUT] + "\n... [output truncated at 256KB]"
            truncated = True

        exit_code = process.returncode

        # Check if the image was not found (exit code 125 with specific error)
        if exit_code == 125 and "image not known" in stderr_str.lower():
            return _script_runner_image_error(public)

        # The container could have written zero or many files via the
        # ``:Z`` workspace mount; we cannot cheaply diff so emit
        # unconditionally and let the FE silent-fetch be a no-op when nothing
        # actually changed.
        _publish_file_list_changed(user_id, "conversation", conversation_id, project_id)

        return json.dumps({
            "path": clean_path,
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "timed_out": timed_out,
            "truncated": truncated,
        })

    except Exception as e:
        logger.exception(
            "[run_script] Failed to execute script (user_id=%s, conversation=%s, path=%s)",
            user_id, conversation_id, path,
        )
        return json.dumps({"error": f"Script execution failed: {e}"})


async def _handle_run_python(
    user_id: int,
    conversation_id: str,
    script: str,
    args: str = "",
    timeout: int | None = None,
    project_id: str | None = None,
    public: bool = False,
    block_mutating_tools: bool = False,
) -> str:
    """Run inline Python code inside an ephemeral Podman container.

    Similar to _handle_run_script() but the script content is piped via stdin
    instead of being read from a workspace file. No file is created in the
    workspace. The container setup (image, networking, mounts, security) is
    identical to _handle_run_script().

    Args:
        user_id: User's integer ID.
        conversation_id: Conversation UUID.
        script: Python script content to execute.
        args: Optional command-line arguments (accessible via sys.argv).
        timeout: Execution timeout in seconds (default 60, max 150).
        project_id: Optional project UUID for project-aware workspace resolution.
        public: Run in the public-project sandbox profile (internet-enabled,
            no proxy bridge, no API key; see _build_script_podman_cmd).

    Returns:
        JSON string with exit_code, stdout, stderr, timed_out, and truncated.
    """
    try:
        # Resolve workspace directory (still needed for the volume mount)
        workspace_dir = await conversation_workspace_dir(conversation_id)

        # Clamp timeout
        clamped_timeout = timeout if timeout is not None else SCRIPT_RUNNER_TIMEOUT
        clamped_timeout = max(1, min(clamped_timeout, SCRIPT_RUNNER_MAX_TIMEOUT))

        # Build the execution command: python3 -u - [args...]
        # -u unbuffers stdout/stderr, - reads script from stdin
        exec_cmd = ["python3", "-u", "-"]
        if args:
            exec_cmd.extend(shlex.split(args))

        # Execute the container with stdin piping, under the same
        # per-run sandbox token lease as _handle_run_script.
        timed_out = False
        with sandbox_token_lease(
            user_id,
            ttl_seconds=clamped_timeout + TOKEN_GRACE_SECONDS,
            conversation_id=conversation_id,
            block_mutating_tools=block_mutating_tools,
        ) as sandbox_token:
            # Build the Podman command (identical flags to
            # _handle_run_script, plus -i for stdin piping)
            podman_cmd = _build_script_podman_cmd(
                workspace_dir, exec_cmd, sandbox_token, public=public,
                interactive=True,
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *podman_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(input=script.encode("utf-8")),
                    timeout=clamped_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                timed_out = True
                stdout_bytes = b""
                stderr_bytes = b""
            except FileNotFoundError:
                return json.dumps({
                    "error": "Podman not found. The 'podman' command is not available on this system."
                })

        # Process output
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        truncated = False
        if len(stdout_str) > SCRIPT_RUNNER_MAX_OUTPUT:
            stdout_str = stdout_str[:SCRIPT_RUNNER_MAX_OUTPUT] + "\n... [output truncated at 256KB]"
            truncated = True
        if len(stderr_str) > SCRIPT_RUNNER_MAX_OUTPUT:
            stderr_str = stderr_str[:SCRIPT_RUNNER_MAX_OUTPUT] + "\n... [output truncated at 256KB]"
            truncated = True

        exit_code = process.returncode

        # Check if the image was not found (exit code 125 with specific error)
        if exit_code == 125 and "image not known" in stderr_str.lower():
            return _script_runner_image_error(public)

        # See _handle_run_script: the inline script may have written workspace
        # files via the :Z mount, so emit unconditionally after the container
        # exits.
        _publish_file_list_changed(user_id, "conversation", conversation_id, project_id)

        return json.dumps({
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
            "timed_out": timed_out,
            "truncated": truncated,
        })

    except Exception as e:
        logger.exception(
            "[run_python] Failed to execute script (user_id=%s, conversation=%s)",
            user_id, conversation_id,
        )
        return json.dumps({"error": f"Script execution failed: {e}"})

