"""Names, removal and sweeps for run_script / run_python sandbox containers.

Sandbox containers run without ``--rm``: with it, ``podman run`` only exits
after tearing the container down, which put that teardown on every call's
critical path (timings: docs/architecture/script-runner.md, Startup
Cost). Instead each run gets a
unique name and this server instance's label; the handler returns as soon
as the container exits and removes it in the background
(``schedule_removal``). A timed-out or cancelled run is force-removed, which
also stops a script that is still running. Leftovers are cleared by
``remove_stale_sandbox_containers`` at boot and at shutdown (a previous
process crashed between exit and removal, or a run cancelled by the
shutdown never got its removal) and by ``prune_loop`` (a background removal
that failed).

The label value is the main server port, unique per instance on one host,
so neither sweep touches another checkout's containers (the browser
sidecars in chat/browser_sessions.py use the same scheme).
"""

import asyncio
import logging
import shutil
import subprocess
import uuid

from chat.gemini_api.constants import get_proxy_port

logger = logging.getLogger(__name__)

SANDBOX_INSTANCE_LABEL = "quest.sandbox.instance"

# The periodic prune removes only stopped containers created longer ago than
# this. It is well above SCRIPT_RUNNER_MAX_TIMEOUT (150 s), so the prune
# never races a ``podman run`` client still collecting its container's exit
# code.
PRUNE_MIN_AGE = "10m"
PRUNE_INTERVAL_SECONDS = 600

# Background removals in flight; held so the tasks are not garbage
# collected before they finish.
_pending_removals: set[asyncio.Task] = set()


def sandbox_instance_key() -> str:
    """Per-server-instance value of SANDBOX_INSTANCE_LABEL."""
    return get_proxy_port()


def sandbox_instance_label() -> str:
    """The ``key=value`` label stamped on this instance's containers."""
    return f"{SANDBOX_INSTANCE_LABEL}={sandbox_instance_key()}"


def sandbox_container_name() -> str:
    """A fresh, unique container name for one sandbox run."""
    return f"quest-sandbox-{sandbox_instance_key()}-{uuid.uuid4().hex[:12]}"


async def _podman(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Run ``podman <args>`` and return (exit code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "podman", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def remove_container(name: str) -> None:
    """Force-remove a sandbox container, stopping it first if it still
    runs (no grace period). Missing containers are fine. Never raises."""
    try:
        code, _, err = await _podman("rm", "-f", "-t", "0", "--ignore", name)
        if code != 0:
            logger.warning("podman rm %s failed: %s", name, err.strip()[:300])
    except Exception:
        logger.warning("podman rm %s failed", name, exc_info=True)


def schedule_removal(name: str, client: asyncio.subprocess.Process | None = None) -> None:
    """Remove a sandbox container in the background, after reaping its
    (already killed) ``podman run`` client when one is given."""

    async def _remove() -> None:
        if client is not None:
            await client.wait()
        await remove_container(name)

    task = asyncio.get_running_loop().create_task(_remove())
    _pending_removals.add(task)
    task.add_done_callback(_pending_removals.discard)


async def prune_stopped_containers() -> None:
    """Remove this instance's stopped sandbox containers older than
    PRUNE_MIN_AGE. Never raises."""
    try:
        code, _, err = await _podman(
            "container", "prune", "-f",
            "--filter", f"label={sandbox_instance_label()}",
            "--filter", f"until={PRUNE_MIN_AGE}",
        )
        if code != 0:
            logger.warning("Sandbox container prune failed: %s", err.strip()[:300])
    except Exception:
        logger.warning("Sandbox container prune failed", exc_info=True)


async def prune_loop() -> None:
    """Prune leftover sandbox containers every PRUNE_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
        if shutil.which("podman") is not None:
            await prune_stopped_containers()


def remove_stale_sandbox_containers() -> int:
    """Remove every sandbox container of this server instance left behind
    by a previous process, running or not. Synchronous; run at boot before
    any sandbox call can start. Returns the number removed."""
    if shutil.which("podman") is None:
        return 0
    try:
        proc = subprocess.run(
            ["podman", "ps", "-a", "-q", "--filter", f"label={sandbox_instance_label()}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not list stale sandbox containers", exc_info=True)
        return 0
    ids = proc.stdout.split()
    if not ids:
        return 0
    try:
        subprocess.run(
            ["podman", "rm", "-f", "-t", "0", *ids],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not remove stale sandbox containers", exc_info=True)
        return 0
    return len(ids)
