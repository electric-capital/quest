# Script Runner Architecture

This document describes the `run_script` and `run_python` tools, which allow the LLM to execute scripts inside ephemeral Podman containers with sandboxed networking and file access. `run_script` runs scripts from workspace files; `run_python` runs inline Python code piped via stdin, avoiding throwaway `.py` files for one-off tasks.

## Overview

The script runner provides a secure execution environment for scripts. Both tools run inside an ephemeral Podman container based on Python 3.12-slim, with the conversation workspace mounted at `/workspace` and, in project conversations, the project workspace at `/project` (see [Mounts](#mounts)). There are **two sandbox profiles backed by two separate images** (separate Dockerfiles + entrypoints so their tooling can diverge independently):

- **Restricted (default)**: no external internet access -- scripts reach the **sandbox tool API** via `localhost:<port>` using slirp4netns networking with socat forwarding, authenticated by the injected `QUEST_API_KEY` -- an **ephemeral per-run sandbox token** (see [Sandbox tokens](#sandbox-tokens)), never the user's long-lived `users.api_key`. Image `quest-script-runner-<mode>` from `Dockerfile.script-runner` + `script-runner-entry.sh`.
- **Public** (conversations in public projects, see [Public Projects Architecture](public-projects.md)): open internet egress and DNS, **no** proxy bridge and **no** `QUEST_API_KEY`/`QUEST_PORT` env vars; the entrypoint iptables-REJECTs all private/link-local destinations (LAN, cloud metadata service) before dropping privileges. Image `quest-script-runner-public-<mode>` from `Dockerfile.script-runner-public` + `script-runner-entry-public.sh`.

The profile is selected by the `public` kwarg on `_handle_run_script`/`_handle_run_python`, chosen from the conversation's project row and threaded through `_dispatch_tool_call()`. The shared argv builder `_build_script_podman_cmd()` in `chat/gemini_api/tool_handlers/sandbox.py` encodes both profiles and is unit-tested in `tests/test_public_projects.py`. Image names come from `get_script_runner_image()` / `get_public_script_runner_image()` in `chat/gemini_api/constants.py` (environment suffix from `QUEST_ENV`). Both images are auto-built at startup by `run.py` using `--network=host`.

The two tools share the same container, networking, mounts, and security model. The difference is how the script is provided: `run_script` reads from a workspace file, while `run_python` receives inline code via the `script` parameter and pipes it to the Python interpreter via stdin (`python3 -u -`). The rest of this document describes the restricted profile unless noted.

## Tool Declarations

Both tools are defined in `chat/llm/tool_schemas.py` as part of `BASE_TOOLS`. `run_script` requires a workspace `path`; `run_python` requires a `script` string containing inline Python code. Both accept optional `args`, `timeout` (default 60s, max 150s), and `intent_message`. Both return a JSON object with `stdout`, `stderr`, and `return_code`.

## Execution Flow

### `run_script`

1. LLM calls `run_script(path="scripts/analyze.py", args="--verbose", intent_message="Analyze data")` during the conversation
2. `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` routes the call to `_handle_run_script()` in `chat/gemini_api/tool_handlers/sandbox.py`
3. Handler launches an ephemeral Podman container with the conversation workspace mounted at `/workspace` (plus `/project` in project conversations, see [Mounts](#mounts)). `path` resolves only inside the conversation workspace; in a project conversation a miss returns a not-found error that points the model at `copy_file` (from `proj://` to `chat://`) or `run_python` for a script kept in the project workspace
4. The handler mints an ephemeral sandbox token for the run (`sandbox_token_lease()` in `chat/sandbox_tokens.py`) and the script executes inside the container with it injected as `QUEST_API_KEY`, plus `QUEST_PORT` (the **sandbox tool API** port, not the main server port -- see [Networking](#networking)). The token is revoked as soon as the container exits
5. On completion (or timeout), stdout, stderr, and return_code are captured and returned to the LLM. After every container run -- including a non-zero exit or a timeout; skipped only when podman or the runner image is missing or the handler raises -- the handler also publishes a per-user `file_list_changed` global on the realtime bus so any open file browsers silent-refresh -- for the `conversation` scope always, and additionally the `project` scope in project conversations (the `/project` mount was writable) -- emitted unconditionally because the `:Z`/`:z` mounts make a cheap diff unavailable; the FE-side fetch is a no-op when nothing actually changed. See [Realtime Architecture](realtime.md#backend-publish-sites-per-user-globals).

### `run_python`

1. LLM calls `run_python(script="import json; print(json.dumps({'status': 'ok'}))")` during the conversation
2. `_dispatch_tool_call()` in `chat/gemini_api/tool_dispatch.py` routes the call to `_handle_run_python()` in `chat/gemini_api/tool_handlers/sandbox.py`
3. Handler launches an ephemeral Podman container (same image and configuration as `run_script`) with the same mounts
4. The script content is piped to `python3 -u -` via stdin, with any `args` appended to the command line
5. On completion (or timeout), stdout, stderr, and return_code are captured and returned to the LLM. Same `file_list_changed` emission as `run_script` (after every container run, timeouts and non-zero exits included).

## Mounts

`_build_script_podman_cmd(..., project_dir=None)` in `chat/gemini_api/tool_handlers/sandbox.py` mounts, in both profiles:

| Mount | When | Relabel |
|-------|------|---------|
| conversation workspace -> `/workspace` (`-w /workspace`) | every conversation | `:Z` (private: only this conversation's containers use it) |
| project workspace -> `/project` | project conversations only (`project_dir` from `project_workspace_dir()`) | `:z` (shared: sibling conversations of one project can run containers against it concurrently) |

Both are writable, so `shutil.copy('/workspace/out.pdf', '/project/out.pdf')` promotes a file into the project workspace from a script. The relabels differ because SELinux `:Z` gives the mount a private per-container label, which on a directory shared by concurrently running sibling conversations would relabel it out from under the other containers; the conversation workspace belongs to one conversation, so it keeps the stricter private label. `run_script`'s `path` still resolves only inside the conversation workspace (one path grammar for every non-file-tool path), so a project script is copied over first or started from `run_python` (`subprocess.run(["python3", "/project/etl.py"])`). Standalone conversations pass `project_dir=None` and get exactly the single-mount argv. The images and entrypoints need nothing path-specific for `/project`: `WORKDIR /workspace` stays, the bind mount creates `/project`, and the entrypoints only install network rules and drop privileges (no chown or relabel of either path).

## Container Environment

- **Base image**: Python 3.12-slim
- **Pre-installed tools**: curl, jq, bash, zip, unzip, socat, iptables, and headless LibreOffice (see below). Pre-installed Python libraries: requests, openpyxl, python-docx, matplotlib, seaborn, pypdf (PDF merge/split/rotate/extract), PyPDFForm (inspect and fill PDF form fields). The Info-ZIP `zip`/`unzip` CLIs enable creating password-protected archives via `zip -P` / `zip -e`.
- **LibreOffice + fonts** (both images): `libreoffice-writer-nogui`, `-calc-nogui` and `-impress-nogui` (Debian's headless builds without the GUI toolkit) for document conversion inside the sandbox -- `soffice --headless --convert-to pdf --outdir /workspace <file>` handles `.docx`/`.doc`/`.odt`/`.rtf`/`.html`/`.xlsx`/`.pptx` sources and any LibreOffice target format. It is the sandbox's document converter: the system prompts, the `system:workspace` skill and the Docs / Drive skills tell the model to use it for every document conversion unless the user asks for a different route, and never to build a PDF from `python-docx` output (Drive-stored documents are fetched with `download_drive_file` first; native Google Docs are exported with `google_export_doc` instead, see [Docs API](../api/docs-api.md)). Fonts: the metric-compatible clones LibreOffice substitutes automatically (Carlito for Calibri, Caladea for Cambria, Liberation for Arial/Times New Roman/Courier New), DejaVu and Noto Core for coverage, and Microsoft's EULA-redistributable "Core fonts for the Web" (Arial, Times New Roman, Courier New, Verdana, Georgia, Trebuchet MS, Comic Sans MS, Impact, Andale Mono, Webdings) via the `contrib` package `ttf-mscorefonts-installer`, which downloads them at build time -- that install is wrapped so a build host without egress to the download mirror still produces an image with the clones only. The Office-era fonts (Calibri, Cambria, Segoe UI, Aptos, ...) are not redistributable and are never installed. `fc-cache -f` runs at build time so no container rebuilds the fontconfig cache. The `soffice` / `libreoffice` commands resolve to the wrapper `script-runner-soffice.sh` (COPY'd to `/usr/local/bin/soffice`, listed in run.py's rebuild-trigger sources): LibreOffice builds a user profile on first start (~0.5 s here, more on slow disks) and every container has a fresh `HOME=/tmp`, so the wrapper seeds a per-run copy of a profile pre-built at image build time under `/opt/libreoffice-profile` (a warm-up txt->pdf conversion creates it) and passes `-env:UserInstallation` for it; `LIBREOFFICE_PROFILE_DIR` overrides the location. Measured on the dev host: image 447 MB -> 954 MB, container startup unchanged (~0.3 s either way -- overlay layers are mounted, not read), a two-page docx -> PDF conversion ~0.75 s inside the container under the 512 MB / 1 CPU limits (~1.4 s without the pre-built profile), with the real Times New Roman / Verdana and the Carlito / Caladea substitutes embedded. Note podman's one-time cost after every image rebuild: the first `--userns=keep-id` run creates an id-mapped copy of the image's layers, which scales with image size (~20 s of kernel time for the bigger image on the dev host, once; more on slow disks). run.py pays that cost at startup (`warm_podman_image()`: a no-op `--entrypoint /bin/true --network=none` keep-id run of each image right after the build check, ~0.5 s once warm) so it never lands inside a script's own timeout -- a `run_python` killed mid-copy discards the partial copy, so with short script timeouts every run used to die at `exit_code -9` until someone ran a long one. A rebuild also removes the build it supersedes (`remove_superseded_podman_image()`: the previous image ID, only once the name no longer resolves to it and no other tag does either -- a local checkout's `-local`/`-prod` images built from identical sources share one ID), since every rebuild otherwise leaves ~1 GB of untagged image plus its ~1 GB id-mapped copy behind per image; deliberately not a blanket `podman image prune`, which would also drop the build cache and force the full LibreOffice install on the next rebuild.

  The library list is enumerated for the model in the `run_script`/`run_python` tool descriptions (`chat/llm/tool_schemas.py`) and the `system:workspace` skill (`chat/system_skills/catalog.py`), which also notes that *reading* a PDF's content for analysis should go through `get_workspace_file` (PDFs are returned as inline parts) -- the libraries are for manipulation/generation inside the sandbox
- **Environment variables**: `MPLBACKEND=Agg` (headless matplotlib backend for chart generation without a display server)
- **File access**: Conversation workspace mounted at `/workspace` (the working directory), plus the project workspace at `/project` in project conversations; both writable via `--userns=keep-id` (see [Mounts](#mounts))
- **API access**: Scripts access the sandbox tool API at `localhost:<port>` using the injected `QUEST_API_KEY` (per-run sandbox token) and `QUEST_PORT` environment variables. For authenticated external API requests (e.g., CoinGecko Pro), scripts use `POST /api/authed-get` on the same port -- see [Gemini API Integration - Proxy Endpoint](gemini-api.md#proxy-endpoint-post-apiauthed-get)
- **Image definition**: `Dockerfile.script-runner` with entrypoint `script-runner-entry.sh`
- **Podman flags**: `--userns=keep-id`, `--user=0:0`, `--cap-add=NET_ADMIN`, `--cap-add=SETPCAP`, `--security-opt=seccomp=<data_dir>/sandbox-seccomp.json` (the no-symlink profile, see [Security](#security)), `--memory=512m`
- **Environment variables injected**: `QUEST_API_KEY` (ephemeral sandbox token), `QUEST_PORT`, `QUEST_RUN_UID`, `QUEST_RUN_GID`

## Sandbox tokens

`QUEST_API_KEY` inside a container is **not** the user's `users.api_key`. Every `run_script` / `run_python` invocation mints its own random `qsb_`-prefixed token (`chat/sandbox_tokens.py`) and the sandbox tool API server accepts nothing else:

- **Minting**: `_handle_run_script` / `_handle_run_python` wrap the `podman run` in `sandbox_token_lease(user_id, ttl_seconds=<clamped timeout> + TOKEN_GRACE_SECONDS, conversation_id=...)`. The handlers take no `api_key` argument anymore; the token is derived from the launching user's id, so a run driven with a blank `users.api_key` (inference-API runs, see [Inference API](../api/inference-api.md)) still gets a working bridge.
- **Lifetime**: the token expires with the container's clamped timeout (max `SCRIPT_RUNNER_MAX_TIMEOUT` = 150s, plus a 15s grace) and is revoked on every exit path of the lease (normal exit, timeout kill, exception). A container that outlives its `podman run` client after the timeout SIGKILL is left holding a dead credential.
- **Storage**: a process-local dict (`_leases`, guarded by a lock, expired entries purged lazily). Nothing is persisted: containers die with the server process, so a restart invalidates every outstanding token by construction.
- **Sandbox-side auth**: `get_current_sandbox_user()` resolves the bearer through the token store and re-reads the user from the DB by id on every call. `create_sandbox_app()` installs it via FastAPI `dependency_overrides` in place of every cookie/`users.api_key` dependency the shared route functions declare (`auth.session.get_current_user`, both `get_current_user_cookie_or_apikey` variants, the `_checked` variant -- enumerated by `_db_bearer_auth_dependencies()` in `chat/sandbox_api.py`). Net effect: `users.api_key` and session cookies are 401 on the sandbox port, and a sandbox token is 401 on the main port because the main app never consults the token store.
- **Scope**: a sandbox token authenticates exactly the script-facing roster (`/api/tool-call`, `/api/authed-get|post`, `/api/gmail-simple/*`) on the loopback-only sandbox port. Leaking it (echoed output, prompt injection, provider-side logs) yields at most a few minutes of that surface, not an account bearer.
- **Restricted leases**: a container launched from an inference API run (`ToolContext.is_inference_api`) gets a lease with `block_mutating_tools=True`. `get_current_sandbox_user()` stores the lease on `request.state` and 403s the mutating HTTP routes (`MUTATING_PROXY_PATHS` in `chat/llm/tool_schemas.py`: the Gmail draft and send-to-self routes); the `POST /api/tool-call` bridge reads it through the `get_sandbox_lease` dependency and 403s the mutating dynamic tools (`mutating_tool_call_tools()`). The container can therefore do no more than the run's own tool dispatch allows -- see [Inference API](../api/inference-api.md).
- **Tests**: `tests/test_sandbox_tokens.py` (store semantics, dependency behaviour, the override roster walked from each route's dependency tree, ASGI-level 401/pass-through, and the handlers minting/revoking around a stubbed podman).

Public-profile containers get no `QUEST_API_KEY` at all (the lease is still taken but the builder withholds it).

## Networking

The container uses slirp4netns networking with socat forwarding and iptables-based host access restriction, and the port it is confined to is the **sandbox tool API** -- a second, loopback-only uvicorn server started in-process by quest.py's lifespan (`chat/sandbox_api.py`) that serves ONLY the script-facing endpoints: `POST /api/tool-call`, `POST /api/authed-get` / `/api/authed-post`, and the Gmail Simple routes (`/api/gmail-simple/*`), plus a `/health` check. The rest of the Quest HTTP surface (auth routes, chat API, admin endpoints, the inference API, the frontend) lives only on the main server port, which the container cannot reach. This means:

- Scripts can reach the sandbox tool API via `localhost:<port>` (the port is injected as `QUEST_PORT`; it resolves via `get_sandbox_port()` in `chat/gemini_api/constants.py` -- `QUEST_SANDBOX_PORT` env var, defaulting to the main server port + 1, e.g. 9501 for a checkout on 9500)
- No external internet access is available -- all outbound traffic is blocked
- The socat forwarding bridges the container's localhost to the host's sandbox tool API port (10.0.2.2 in slirp4netns; the sandbox server binds `127.0.0.1` so it is never exposed on the LAN)
- iptables rules restrict host access to only the sandbox tool API port on the slirp gateway (`10.0.2.2:<port>`) and REJECT every private + link-local range (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `100.64/10`); this is deliberately broad because the host is also reachable at its *real* LAN IP (surfaced as `host.containers.internal` in the container's `/etc/hosts`), which `outbound_addr=127.0.0.1` does NOT block -- so without the range REJECTs the container could reach the main Quest server, SSHd, and the cloud metadata endpoint on that address. The IPv4 rules are loaded by one atomic `iptables-restore` call (one process start instead of one per rule; all-or-nothing), and the entrypoint fails closed (refuses to start) if `iptables-restore` or `ip6tables` is unavailable or either policy fails to install
- IPv6 is disabled outright in both profiles:
  - `enable_ipv6=false` on the slirp4netns network option (slirp enables it by default, handing the container a ULA address, a default route, and the host's loopback at `fd00::2`),
  - `--sysctl net.ipv6.conf.all.disable_ipv6=1` (no IPv6 stack in the container's network namespace, so `AF_INET6` connects fail with `EADDRNOTAVAIL`), and
  - a fail-closed `ip6tables -A OUTPUT -j REJECT` in both entrypoints.

  `outbound_addr` and every iptables rule above are IPv4-only, so before this the restricted container could reach any host service listening on `::1`/`::` and, on a host with IPv6 routing, the internet -- carrying `QUEST_API_KEY` (then the user's long-lived key) with it
- DNS: the container never sees the host's `/etc/resolv.conf` (podman would otherwise copy its nameservers and search domains -- internal VPC/tailnet topology -- into the container). The restricted profile passes `--dns=none` (no resolv.conf written; DNS is dead here anyway), the public profile passes `--dns=10.0.2.3 --dns-search=.` (slirp4netns builtin resolver only, no search domains)
- The route roster is registered by `register_sandbox_api_routes()` in `chat/sandbox_api.py` on **both** the main app (the LLM's in-process `curl_proxy_*` dispatch resolves against the main app's route table) and the sandbox app, so the two surfaces cannot drift; the exact roster is pinned by `tests/test_sandbox_api.py`

## Security

- **Network isolation**: slirp4netns with iptables rules restricts outbound traffic to only the sandbox tool API port on the host, which itself serves only the script-facing endpoints. All other host ports (including the main Quest server) and external internet are blocked, preventing data exfiltration and cross-service access; IPv6 is disabled entirely (slirp option + sysctl + ip6tables) since the confinement is IPv4-only
- **Privilege drop**: The entrypoint runs as root inside the container (`--user=0:0`) to set up iptables rules, then drops to the unprivileged target user via `setpriv` with all capabilities cleared (`--inh-caps=-all`, `--bounding-set=-all`) and `--no-new-privs` set. Root inside a rootless Podman container maps to a sub-UID on the host (not actual host root) via `--userns=keep-id`
- **Defense-in-depth**: After iptables rules are configured, execute permissions are removed from iptables binaries so the user's script cannot modify the rules even if capabilities were somehow retained
- **Memory/CPU limits**: Container resource limits prevent runaway scripts
- **Ephemeral containers**: Each script execution creates and destroys a fresh container
- **Timeout enforcement**: Default 60s, maximum 150s, prevents indefinite execution
- **Workspace isolation**: Only the conversation workspace (`data/chats/{id}/workspace/`) and, for project conversations, the project workspace (`data/projects/{id}/workspace/workspace/`) are mounted; no access to host filesystem beyond `/workspace` and `/project`
- **No symlink creation (seccomp)**: Both profiles run under a custom seccomp profile that denies the `symlink`/`symlinkat` syscalls with `EPERM` (covering `ln -s`, `os.symlink`, and archive extractors like `unzip` writing symlink entries). The workspace mount is host-backed, and host-side consumers (folder zip downloads, workspace duplication/moves, uploads) operate on workspace entries with the server's privileges -- a symlink pointing outside the workspace would redirect their reads/writes to arbitrary host paths.

  `chat/gemini_api/sandbox_seccomp.py` generates `<data_dir>/sandbox-seccomp.json` on first sandbox use by patching the host's default containers profile (`/etc/containers/seccomp.json`, then `/usr/share/containers/seccomp.json`, falling back to the vendored repo-root snapshot `script-runner-seccomp-fallback.json`), preserving the rest of the default confinement -- notably the `io_uring_*` denial, without which `IORING_OP_SYMLINKAT` could bypass the filter. A profile generation failure fails the sandbox run (fail closed) instead of launching unconfined.

  The quest.py lifespan additionally sweeps `data/chats/` + `data/projects/` at boot and deletes any symlink that predates this fix (`chat/workspace_symlinks.py`), and the host-side consumers keep their own symlink guards as defense in depth (see [File Browser API](../api/file-browser-api.md#endpoints)).

  Known cost: anything inside the sandbox that needs to *create* a symlink fails -- e.g. `python -m venv` (even with `--copies`, which still symlinks `lib64`) and git checkouts containing symlink entries in the public profile; plain `pip install --target` still works

## Startup Cost

Every `run_script`/`run_python` call pays a full container create + teardown, so per-call overhead is kept low in three places:

- **OCI runtime**: `chat/gemini_api/sandbox_runtime.py` picks the runtime once per process and `_build_script_podman_cmd()` passes it as podman's global `--runtime` flag. crun (much cheaper create/delete than runc: ~320 ms vs ~570 ms per restricted-profile run on a 4-vCPU Debian host) is used when found on `PATH` and `crun --version` succeeds; otherwise the flag is omitted and podman's default runtime applies. `QUEST_SANDBOX_RUNTIME` overrides detection: a runtime name or binary path is passed through verbatim, `default` omits the flag. The choice is logged at startup (`Sandbox OCI runtime: ...`). The confinement (seccomp profile, network, entrypoint iptables, setpriv) is applied by podman and the entrypoint, not the runtime, so both runtimes enforce the same profile. On Debian: `apt install crun`.
- **Entrypoint**: IPv4 rules go in via a single `iptables-restore` (see [Networking](#networking)).
- **matplotlib font cache**: both images set `MPLCONFIGDIR=/opt/matplotlib` and pre-build the font cache at image build time (world-writable, since matplotlib falls back to a temp dir -- and rebuilds -- when the directory is not writable by the run user). Without it the fresh `HOME=/tmp` made every matplotlib import rebuild `fontlist.json` (~1 s).

## Auto-Build

Both Podman images (restricted + public) are automatically built (or rebuilt) at startup in `run.py` using `--network=host`. Image names include an environment suffix (e.g., `quest-script-runner-dev`, `quest-script-runner-public-prod`). A rebuild triggers when the image's Dockerfile **or** its entrypoint script is newer than the built image (`should_rebuild_podman_image()` with `extra_sources`), so no manual build steps are needed.

## Design Decisions

**Why two tools (`run_script` vs `run_python`)?**
`run_python` avoids creating throwaway `.py` files in the workspace for one-off tasks (quick calculations, data transformations, format conversions). `run_script` via `write_workspace_file` (`write_file` on a `chat://` path in a project conversation) is better for reusable scripts that the user may want to keep, inspect, or re-run.

**Why Podman instead of Docker?**
Podman runs rootless containers with `--userns=keep-id`, which maps the container user to the host user for correct file ownership on the mounted workspace. This avoids the root-owned file permission issues common with Docker volume mounts.

**Why no external internet?**
Preventing external network access eliminates data exfiltration risk. Scripts that need to call external APIs can do so through the API proxy (which enforces authentication and access control).

**Why auto-build at startup?**
Auto-building removes the manual image build step from the developer workflow. The images are Python 3.12-slim plus a handful of packages and headless LibreOffice with fonts (~950 MB each); the build takes a few minutes (LibreOffice + font downloads) but container startup cost is unaffected by image size.

**Why block symlink creation with seccomp instead of checking for symlinks host-side?**
The sandbox is the only place a workspace symlink can be born (uploads write raw bytes; no host-side archive extraction exists), so one syscall denial at the container boundary kills the whole attack class at its source. Host-side check-then-use guards alone would be insufficient: containers run concurrently with HTTP requests, so a script could swap a regular file for a symlink between an `is_symlink()` check and the dereference in a zip/copy operation. Blocking creation is race-free, and the host-side guards then only serve as defense in depth.

A seccomp filter is process-wide rather than path-scoped, so in-container symlink use (e.g. `venv` creation) breaks too -- an accepted cost for a data-processing sandbox, and the failure is an explicit "Operation not permitted".

**Why run as root inside the container then drop privileges?**
Iptables rules require `NET_ADMIN` capability, which is only available to root. Running the entrypoint as root (`--user=0:0`) allows setting up iptables rules to restrict host access, then `setpriv` drops to the unprivileged target user with all capabilities cleared before executing the user's script. This is safe because root inside a rootless Podman container (via `--userns=keep-id`) maps to a sub-UID on the host, not actual host root.
