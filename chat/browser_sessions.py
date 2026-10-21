"""Browser sessions for public-project conversations.

A public conversation that uses the ``browser_*`` tools gets one long-lived
podman container (image ``quest-browser-sidecar-<mode>``, see
``Dockerfile.browser-sidecar``) holding a headless Chromium driven by the
agent script inside it (``browser-sidecar-agent.py``). The container is
the session: open tabs, cookies and logins live there, every browser tool
call of that conversation talks to the same container, and it is reaped
after :data:`BROWSER_IDLE_SECONDS` without a call, when the conversation
is archived, or at server shutdown. Cookies and web storage are saved by
the agent to ``<conversation dir>/browser/storage_state.json`` and restored
by the next container, so logins outlive the reap; nothing else of the
browser profile persists.

Transport: the agent listens on a Unix socket in the conversation's
``browser/`` directory, bind-mounted into the container as
``/browser-state``. No TCP port is published, so the host reaches the
agent and the container has no path to the host. The network posture is
the public script-runner's (``script-runner-entry-public.sh``: open
internet, every private / link-local destination rejected, host loopback
unmapped, no credential injected), and the container mounts neither
workspace: screenshots come back over the socket as bytes and the tool
handler writes them into the conversation workspace itself.

See docs/architecture/public-browser.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Seconds without a browser tool call after which a session's container
# is stopped. Logins survive (storage state); open tabs do not.
BROWSER_IDLE_SECONDS = int(os.environ.get("QUEST_BROWSER_IDLE_SECONDS", "600"))
# Concurrent browser containers per user and per server: a Chromium
# container holds a few hundred MB for as long as it lives.
BROWSER_MAX_PER_USER = int(os.environ.get("QUEST_BROWSER_MAX_PER_USER", "2"))
BROWSER_MAX_TOTAL = int(os.environ.get("QUEST_BROWSER_MAX_TOTAL", "8"))
# How long to wait for a fresh container's agent to answer /health.
BROWSER_START_TIMEOUT = 45.0
# Per-call timeout on the agent socket. The agent's own navigation timeout
# is 30 s, so a hung page surfaces as its error well before this.
BROWSER_CALL_TIMEOUT = 90.0
# Container resource limits (Chromium needs far more than the 512 MB of
# the script sandbox).
BROWSER_CONTAINER_MEMORY = "2g"
BROWSER_CONTAINER_CPUS = "2"
BROWSER_CONTAINER_SHM = "256m"
REAPER_INTERVAL_SECONDS = 30

# The connected-services pseudo-key (api/instructions.py PSEUDO_SERVICE_KEYS)
# the browser tool specs are gated on (``requires_service``): set from the
# ``browser`` feature gate, never a connector.
BROWSER_SERVICE_KEY = "browser"


def browser_disabled_message() -> str:
    """The one gate-closed text, used by the tool handlers and the system
    skill loader."""
    return (
        "The browser is not enabled for you on this server (Settings > "
        "Features > Browser in public projects)."
    )

STATE_SUBDIR = "browser"
SOCKET_NAME = "agent.sock"
# Podman label marking this server instance's sidecars, so the boot-time
# sweep never touches another checkout's containers on the same host.
INSTANCE_LABEL = "quest.browser.instance"


class BrowserError(Exception):
    """A browser session problem worth relaying to the model verbatim."""


def instance_key() -> str:
    """Per-server-instance key stamped on sidecar containers (the main
    server port is unique per instance on one host)."""
    from chat.gemini_api.constants import get_proxy_port
    return get_proxy_port()


def container_name(conversation_id: str) -> str:
    return f"quest-browser-{instance_key()}-{conversation_id}"


def browser_state_dir(conversation_id: str) -> Path:
    """``<conversation dir>/browser``: the agent socket and storage state.

    Outside the conversation workspace on purpose: the saved cookies are
    credentials for whatever the browser logged into, and the file tools
    and file browser must not list them.
    """
    from chat.storage import ChatStorage
    return ChatStorage.get_conversation_dir(conversation_id) / STATE_SUBDIR


def build_browser_podman_cmd(
    name: str, state_dir: Path, image: str, instance: str,
) -> list[str]:
    """The ``podman run`` argv for a browser sidecar.

    Shares the public script-runner profile (slirp4netns with host loopback
    unmapped and IPv6 off, DNS pinned to the slirp resolver, keep-id user
    namespace, root entrypoint with NET_ADMIN/SETPCAP for the iptables
    rules then setpriv down to the host uid, the no-symlink seccomp
    profile, no credential env vars) and differs in what it mounts (only
    the conversation's ``browser/`` state directory), its resource limits,
    and in running detached under a stable name and instance label.
    """
    from chat.gemini_api.sandbox_runtime import get_sandbox_runtime
    from chat.gemini_api.sandbox_seccomp import get_sandbox_seccomp_profile_path

    cmd = ["podman"]
    runtime = get_sandbox_runtime()
    if runtime:
        cmd += ["--runtime", runtime]
    cmd += [
        "run", "-d", "--rm",
        "--name", name,
        "--label", f"{INSTANCE_LABEL}={instance}",
        "--network=slirp4netns:allow_host_loopback=false,enable_ipv6=false",
        "--sysctl=net.ipv6.conf.all.disable_ipv6=1",
        "--dns=10.0.2.3", "--dns-search=.",
        "--userns=keep-id",
        "--user=0:0",
        "--cap-add=NET_ADMIN",
        "--cap-add=SETPCAP",
        f"--security-opt=seccomp={get_sandbox_seccomp_profile_path()}",
        "-v", f"{state_dir}:/browser-state:Z",
        f"--memory={BROWSER_CONTAINER_MEMORY}",
        f"--cpus={BROWSER_CONTAINER_CPUS}",
        f"--shm-size={BROWSER_CONTAINER_SHM}",
        "-e", "HOME=/tmp",
        "-e", f"QUEST_RUN_UID={os.getuid()}",
        "-e", f"QUEST_RUN_GID={os.getgid()}",
        image,
    ]
    return cmd


@dataclass
class BrowserSession:
    conversation_id: str
    user_id: int
    name: str
    state_dir: Path
    last_used: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    calls: int = 0
    # O_PATH descriptor of state_dir, see connect_path.
    dir_fd: int | None = None

    @property
    def socket_path(self) -> Path:
        return self.state_dir / SOCKET_NAME

    @property
    def connect_path(self) -> str:
        """The path the host connects to. A Unix socket path is limited
        to 108 bytes and ``<data dir>/chats/<uuid>/browser/agent.sock``
        can exceed that (a deep data directory is enough), so the socket
        is reached relative to an open descriptor of its directory:
        ``/proc/self/fd/<n>/agent.sock`` is short whatever the data
        directory is."""
        if self.dir_fd is None:
            self.dir_fd = os.open(self.state_dir, os.O_PATH | os.O_DIRECTORY)
        return f"/proc/self/fd/{self.dir_fd}/{SOCKET_NAME}"

    def close_fd(self) -> None:
        if self.dir_fd is not None:
            try:
                os.close(self.dir_fd)
            except OSError:
                pass
            self.dir_fd = None

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used


async def _run_podman(*args: str, timeout: float = 60.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "podman", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


class BrowserSessionManager:
    """The per-process table of live browser sessions, keyed by
    conversation id, and the podman calls that start and stop them."""

    def __init__(self) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._start_lock = asyncio.Lock()

    # -- queries ----------------------------------------------------------

    def get(self, conversation_id: str) -> BrowserSession | None:
        return self._sessions.get(conversation_id)

    def sessions(self) -> list[BrowserSession]:
        return list(self._sessions.values())

    def _user_session_count(self, user_id: int) -> int:
        return sum(1 for s in self._sessions.values() if s.user_id == user_id)

    # -- lifecycle --------------------------------------------------------

    async def acquire(self, conversation_id: str, user_id: int) -> BrowserSession:
        """The conversation's session, started if it has none.

        Raises :class:`BrowserError` when the per-user or server-wide cap
        is reached or the container fails to come up.
        """
        session = self._sessions.get(conversation_id)
        if session is not None:
            return session
        async with self._start_lock:
            session = self._sessions.get(conversation_id)
            if session is not None:
                return session
            if self._user_session_count(user_id) >= BROWSER_MAX_PER_USER:
                raise BrowserError(
                    f"You already have {BROWSER_MAX_PER_USER} browser sessions "
                    "open in other conversations. Wait for one to go idle "
                    f"({BROWSER_IDLE_SECONDS // 60} minutes without browser "
                    "calls) or close it there with browser_close."
                )
            if len(self._sessions) >= BROWSER_MAX_TOTAL:
                raise BrowserError(
                    "The server's browser capacity is in use right now. Try "
                    "again in a few minutes."
                )
            session = await self._start(conversation_id, user_id)
            self._sessions[conversation_id] = session
            return session

    async def _start(self, conversation_id: str, user_id: int) -> BrowserSession:
        from chat.gemini_api.constants import get_browser_sidecar_image

        image = get_browser_sidecar_image()
        state_dir = browser_state_dir(conversation_id)
        state_dir.mkdir(parents=True, exist_ok=True)
        name = container_name(conversation_id)
        socket_path = state_dir / SOCKET_NAME
        # A stale socket from a container that died uncleanly would make
        # the readiness poll connect to nothing; the agent unlinks it too.
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        # Likewise a container of that name left over from a crashed
        # server process (the boot sweep covers the normal case).
        await _run_podman("rm", "-f", name)

        cmd = build_browser_podman_cmd(name, state_dir, image, instance_key())
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            message = err.decode(errors="replace").strip()
            logger.error(
                "Browser sidecar failed to start (conversation=%s): %s",
                conversation_id, message,
            )
            if "no such image" in message.lower() or "image not known" in message.lower():
                raise BrowserError(
                    "The browser is not available on this server: its "
                    f"container image '{image}' is not built."
                )
            raise BrowserError(f"The browser could not be started: {message[:300]}")

        session = BrowserSession(
            conversation_id=conversation_id, user_id=user_id,
            name=name, state_dir=state_dir,
        )
        try:
            await self._wait_ready(session)
        except BrowserError:
            await self._stop_container(session)
            raise
        logger.info(
            "Browser sidecar started (conversation=%s, user_id=%s, container=%s)",
            conversation_id, user_id, name,
        )
        return session

    async def _wait_ready(self, session: BrowserSession) -> None:
        deadline = time.monotonic() + BROWSER_START_TIMEOUT
        last_error = ""
        while time.monotonic() < deadline:
            if session.socket_path.exists():
                try:
                    result = await self._request(session, "GET", "health", None, timeout=5.0)
                    if result.get("ok"):
                        return
                except BrowserError as exc:
                    last_error = str(exc)
            # A container that exited early (image error, Chromium crash)
            # never writes the socket; stop waiting as soon as it is gone.
            code, out, _ = await _run_podman(
                "ps", "-q", "--filter", f"name=^{session.name}$", timeout=15.0,
            )
            if code == 0 and not out.strip():
                _, logs, logs_err = await _run_podman("logs", "--tail", "20", session.name, timeout=15.0)
                raise BrowserError(
                    "The browser container exited before it was ready: "
                    f"{(logs or logs_err).strip()[-500:] or 'no output'}"
                )
            await asyncio.sleep(0.25)
        raise BrowserError(
            f"The browser did not become ready within {int(BROWSER_START_TIMEOUT)} "
            f"seconds{(': ' + last_error) if last_error else ''}."
        )

    async def _stop_container(self, session: BrowserSession) -> None:
        try:
            code, _, err = await _run_podman("stop", "-t", "5", session.name, timeout=30.0)
            if code != 0 and "no such container" not in err.lower():
                logger.warning(
                    "podman stop %s failed: %s", session.name, err.strip()[:300],
                )
        except Exception:
            logger.warning("podman stop %s failed", session.name, exc_info=True)
        try:
            session.socket_path.unlink()
        except OSError:
            pass
        session.close_fd()

    async def stop(self, conversation_id: str, reason: str = "") -> bool:
        """Stop the conversation's session, if any. Returns whether one ran."""
        session = self._sessions.pop(conversation_id, None)
        if session is None:
            return False
        async with session.lock:
            await self._stop_container(session)
        logger.info(
            "Browser sidecar stopped (conversation=%s, container=%s, calls=%d%s)",
            conversation_id, session.name, session.calls,
            f", reason={reason}" if reason else "",
        )
        return True

    async def stop_all(self, reason: str = "shutdown") -> None:
        for conversation_id in list(self._sessions):
            try:
                await self.stop(conversation_id, reason=reason)
            except Exception:
                logger.warning(
                    "Stopping browser session %s failed", conversation_id, exc_info=True,
                )

    async def reap_idle(self) -> int:
        """Stop every session idle for longer than BROWSER_IDLE_SECONDS."""
        reaped = 0
        for session in list(self._sessions.values()):
            if session.idle_seconds >= BROWSER_IDLE_SECONDS and not session.lock.locked():
                if await self.stop(session.conversation_id, reason="idle"):
                    reaped += 1
        return reaped

    async def reaper_loop(self) -> None:
        """Background task: reap idle sessions every REAPER_INTERVAL_SECONDS."""
        while True:
            try:
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Browser session reaper failed; continuing")

    # -- calls ------------------------------------------------------------

    async def call(
        self, conversation_id: str, user_id: int, action: str, params: dict | None = None,
    ) -> dict:
        """Run one agent action for the conversation, starting its
        session when needed. Calls of one session run one at a time.

        Raises :class:`BrowserError` when the session cannot be started or
        its agent cannot be reached (the session is dropped then, so the
        next call starts a fresh container). An action the agent ran but
        that failed comes back as its ``{"error": ...}`` result."""
        session = await self.acquire(conversation_id, user_id)
        async with session.lock:
            try:
                result = await self._request(session, "POST", action, params or {})
            except BrowserError:
                # The agent is gone (container OOM-killed, crashed, reaped
                # by someone else): drop the session so the next call
                # starts over instead of failing forever.
                self._sessions.pop(conversation_id, None)
                await self._stop_container(session)
                raise
            session.last_used = time.monotonic()
            session.calls += 1
            return result

    async def _request(
        self, session: BrowserSession, method: str, action: str,
        params: dict | None, timeout: float = BROWSER_CALL_TIMEOUT,
    ) -> dict:
        import httpx

        transport = httpx.AsyncHTTPTransport(uds=session.connect_path)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
                if method == "GET":
                    response = await client.get(f"http://browser/{action}")
                else:
                    response = await client.post(
                        f"http://browser/{action}", content=json.dumps(params or {}),
                        headers={"Content-Type": "application/json"},
                    )
        except httpx.TimeoutException:
            raise BrowserError(
                f"The browser did not answer within {int(timeout)} seconds."
            )
        except (httpx.TransportError, OSError) as exc:
            raise BrowserError(f"The browser session is not reachable: {exc}")
        try:
            body = response.json()
        except ValueError:
            raise BrowserError("The browser agent returned a malformed response.")
        if not isinstance(body, dict):
            raise BrowserError("The browser agent returned a malformed response.")
        return body


browser_sessions = BrowserSessionManager()


# ---------------------------------------------------------------------------
# Host-level availability and boot-time cleanup
# ---------------------------------------------------------------------------


_availability_cache: tuple[float, tuple[bool, str | None]] | None = None
_AVAILABILITY_TTL = 30.0


def browser_availability(use_cache: bool = True) -> tuple[bool, str | None]:
    """Whether the browser feature can run here: podman on PATH and the
    sidecar image built. ``(True, None)`` or ``(False, reason)``. Cached
    briefly: the admin Features panel asks on every listing."""
    global _availability_cache
    now = time.monotonic()
    if use_cache and _availability_cache and now - _availability_cache[0] < _AVAILABILITY_TTL:
        return _availability_cache[1]
    from chat.gemini_api.constants import get_browser_sidecar_image

    image = get_browser_sidecar_image()
    if shutil.which("podman") is None:
        result = (False, "podman is not installed on this server.")
    else:
        try:
            proc = subprocess.run(
                ["podman", "images", "-q", image],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                result = (False, f"podman is not working: {proc.stderr.strip()[:200]}")
            elif not proc.stdout.strip():
                result = (
                    False,
                    f"The browser container image '{image}' is not built; "
                    "run.py builds it at startup from Dockerfile.browser-sidecar.",
                )
            else:
                result = (True, None)
        except (OSError, subprocess.SubprocessError) as exc:
            result = (False, f"podman is not working: {exc}")
    _availability_cache = (now, result)
    return result


def remove_stale_containers() -> int:
    """Remove every sidecar container of this server instance (by label)
    left behind by a previous process. Synchronous; run at boot before
    any session can start. Returns the number removed."""
    if shutil.which("podman") is None:
        return 0
    try:
        proc = subprocess.run(
            [
                "podman", "ps", "-a", "-q",
                "--filter", f"label={INSTANCE_LABEL}={instance_key()}",
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not list stale browser containers", exc_info=True)
        return 0
    ids = proc.stdout.split()
    if not ids:
        return 0
    try:
        subprocess.run(
            ["podman", "rm", "-f", *ids], capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not remove stale browser containers", exc_info=True)
        return 0
    return len(ids)
