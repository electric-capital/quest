"""Hardcoded catalog of system skills.

Each entry binds a stable ``system:<name>`` id to a callable that produces
the full skill content on demand. Most entries delegate to existing
``api/*/get_instructions`` functions so the source of truth for backend
documentation stays where it already lives. A few cross-cutting skills
(memory, workspace, action_requests, project_db) carry inlined Markdown
that used to live directly in the system prompt.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from api import (
    airtable as airtable_api,
    calendar as calendar_api,
    docs as docs_api,
    drive as drive_api,
    federal_register as federal_register_api,
    gcp as gcp_api,
    ramp as ramp_api,
    sec_edgar as sec_edgar_api,
    sheets as sheets_api,
    slides as slides_api,
    tasks as tasks_api,
)
from api.gmail import get_instructions as gmail_get_instructions
from chat.docs import access as doc_access
from chat.docs.constants import (
    DOC_MAX_ASSETS,
    DOC_MAX_ASSETS_TOTAL_BYTES,
    DOC_MAX_CONTENT_SIZE,
    DOC_MAX_IMAGE_SIZE,
    DOC_READ_MAX_CHARS,
    DOCS_SERVICE_KEY,
)


SYSTEM_SKILL_PREFIX = "system:"


@dataclass(frozen=True)
class SystemSkill:
    """A single hardcoded system skill.

    Attributes:
        id: Stable identifier, must start with ``system:``.
        name: Short human label shown to the agent.
        description: One-line description (<=120 chars), shown both in the
            system-prompt enumeration and in list_skills/search_skills output.
        when_to_load: Brief trigger hint shown alongside the description.
        requires: Optional ``connected_services`` key that must be true for
            this skill to be visible/loadable. ``None`` means always available.
        content_builder: ``(base_url, api_key) -> str`` returning the full
            skill content when the skill is loaded.
        requires_project: When True, skill is only surfaced when the
            current conversation belongs to a project.
    """

    id: str
    name: str
    description: str
    when_to_load: str
    content_builder: Callable[[str, str], str]
    requires: Optional[str] = None
    requires_project: bool = False


# ---------------------------------------------------------------------------
# Cross-cutting skill content builders
# ---------------------------------------------------------------------------

def _memory_content(_base_url: str, _api_key: str) -> str:
    return """## Memory

Quest maintains a per-user "memory" store of small factual notes about the
user (preferences, ongoing context, things they've asked you to remember).

### Reads (call directly via `tool_call`)

- **memory_search(query)** — semantic substring search over saved memories.
  Use this when the user mentions a preference, a past interaction, or
  something they've asked you to remember.
- **memory_list()** — return every active memory. Use this when the user
  asks "what do you know about me", or when you want a complete view of
  saved context before answering a personal question.

### Writes (always go through an action request)

To save a new memory, call:

```
create_action_request(
    request_type="create_memory",
    params={"content": "<concise factual note>"},
    reasoning="<why this is worth saving, in plain English>",
)
```

The user is shown an inline approval card with Approve / Revise / Stop
buttons. The call BLOCKS until they resolve it; the verdict comes back
on the return value (you do NOT need a separate `wait_for_handles`
step):

- `verdict: "executed"` — the memory was saved. `result.memory_id`
  carries the new id.
- `verdict: "denied"` with `feedback` — the user clicked Revise and
  supplied a reason (e.g. "rephrase: I prefer 'pour-over' not 'drip
  coffee'"). Read `feedback` directly off the return value, adapt the
  content, and issue a fresh `create_action_request` with the corrected
  text. (A `denied` verdict without `feedback` is a plain decline: save
  nothing.)
- `verdict: "stopped"` — the user pressed Stop, which halted the
  conversation; the call returns only once they send a new message,
  which arrives in the same turn. Save nothing and act on the new
  message.

Write concise, factual notes (e.g. "Prefers window seats on flights",
not "User said they like window seats during our chat on Monday").
Each note is capped at 4KB.

`create_action_request(request_type="create_memory", ...)` is top-level
only -- sub-agents cannot save memories (the create_action_request tool
itself is restricted to the top-level agent).

In Slack-driven conversations `create_action_request` is disabled (the
approval card lives in the Quest web app), so memory writes are
unavailable there. If the user asks you to remember something in Slack,
acknowledge it in your reply and mention that persisting memories
requires the web UI.

### When to load this skill

- Before doing any memory-related operation the user asks for explicitly
  ("remember that I…", "what do you know about me", "save a note").
- Before answering questions where the user implies you should know
  something about them (preferences, recurring contacts, ongoing goals).

### Operational notes

- Memories are short text strings, not structured records.
- Searches are best-effort substring + lexical matches; if the user uses
  paraphrased wording, try a couple of keyword variants.
"""


def _skill_management_content(_base_url: str, _api_key: str) -> str:
    return """## Skill Management

Quest stores reusable **skills** (named instruction documents) in a database.
This skill documents how to **inspect** your skills (read tools) and how to
**create / edit** them (write action requests).

Skills come in two flavors:

- **User skills** -- owned by you, with a `visibility` of `private`, `shared`,
  or `public`, and (for `shared`) an explicit roster of users they are shared
  with.
- **Project skills** -- belong to the project the current conversation is in.
  They have a fixed visibility (`project`), no sharing roster, and a per-project
  auto-load toggle. You can only touch them when the conversation runs inside a
  project you own.

`system:*` skills (like this one) are hardcoded, not DB rows, and are NOT
editable.

### Reads (call directly via `tool_call`)

- **list_my_skills()** -- categorized listing of skills you can see (your own,
  shared-with-you, public, and the current project's), with per-skill auto-load
  tiers. No bodies, no `system:*` skills.
- **get_skill(skill_id)** -- full detail for a single skill: `content`,
  `visibility`, `is_owner`, the share roster (`shares`, owner-only), and
  `autoload`. Use this to read a skill's current state before proposing an
  edit.

Always read with `get_skill` before proposing an `edit_skill` so you know the
current name / visibility / target. For CONTENT edits this is mandatory:
`edit_skill` rejects `old_string` / `new_string` edits unless this
conversation has already loaded or read the skill (via `get_skill`,
`load_skills`, an auto-load, or having created it here).

### Writes (always go through an action request)

Like every action request, skill writes render an inline Approve / Revise /
Deny card, BLOCK until the user resolves it, and return the verdict directly
(no separate `wait_for_handles` step). The standard verdict shape and Revise
feedback handling are documented in `system:action_requests`. In Slack-driven
conversations `create_action_request` is disabled, so skill writes are
unavailable there.

#### create_skill

```
create_action_request(
    request_type="create_skill",
    params={
        "target": "user",          # "user" (default) or "project"
        "name": "<skill name>",    # required, <= 100 chars, unique
        "content": "<full body>",  # required, <= 64 KB
        "description": "<short>",  # optional, <= 500 chars
        "visibility": "private",   # user target only: private|shared|public
        "share_emails": ["a@b.co"] # user target only, ONLY when shared
    },
    reasoning="<why>",
)
```

- **target="user"** (default): creates a skill you own. `visibility` defaults
  to `private`; `share_emails` is accepted only when `visibility == "shared"`
  (unknown / your-own emails are silently skipped).
- **target="project"**: creates a skill in the current project. Requires the
  conversation to be in a project you own. `visibility` and `share_emails` are
  rejected (project skills are always `project`-visible with no roster).
- Name must be unique (per-creator for user skills, per-project for project
  skills); a collision is rejected before the card so you can rename.
- Returns `result.skill_id` (and `result.target`, `result.shared_with`).

#### edit_skill

```
create_action_request(
    request_type="edit_skill",
    params={
        "skill_id": "<uuid>",            # required; NOT a system:* id
        "name": "<new name>",            # optional
        "description": "<new>",          # optional
        "old_string": "<exact text>",    # optional pair: content search...
        "new_string": "<replacement>",   # ...and replace ("" deletes)
        "replace_all": false,            # optional, with old_string only
        "visibility": "shared",          # user skills only
        "add_share_emails": ["a@b.co"],  # user skills only, when shared
        "remove_share_emails": ["c@d.co"],
        "project_autoload": true         # project skills only
    },
    reasoning="<why>",
)
```

- The target (user vs project skill) is inferred from the skill row's
  `project_id` -- there is no `target` param. Edit the right skill by passing
  the `skill_id` you got from `list_my_skills` / `get_skill`.
- Partial edit: send only the fields you want to change. At least one editable
  field is required (`skill_id` alone is rejected). `project_autoload` counts
  as an editable field, so `skill_id` + `project_autoload` is a valid edit.
- **Content edits are an exact search-and-replace** (like
  `edit_workspace_file`), NOT a full-body overwrite -- there is no `content`
  param. `old_string` must match the current content exactly (whitespace
  included) and be unique unless `replace_all: true`; `new_string` replaces
  it (empty string deletes the match). Requirements, checked BEFORE the card
  so you can self-correct:
  - This conversation must have loaded or read the skill (`get_skill`,
    `load_skills`, an auto-load, or you created it here). If not, read it
    first.
  - A not-found or ambiguous `old_string` is rejected -- re-read the skill
    with `get_skill` and retry with the exact current text.
  - The match is verified again at Approve time against the then-current
    content, so a skill that changed while the card sat open fails cleanly
    instead of being clobbered.
  The card shows the user a line diff of the change.
- **User skills**: editable fields are `name` / `description` / content
  (`old_string`/`new_string`) / `visibility` plus `add_share_emails` /
  `remove_share_emails`. Share changes are valid only while the skill is (or
  becomes) `shared`. Moving visibility AWAY from `shared` (to `private` or
  `public`) **wipes the entire share roster**. Only the creator can edit.
  `project_autoload` is rejected.
- **Project skills**: editable fields are `name` / `description` / content
  (`old_string`/`new_string`) plus `project_autoload` (a bool toggling
  whether the skill auto-loads into the project's conversations).
  `visibility` and the share lists are rejected. You must own the project the
  skill belongs to.
- Name changes must stay unique (per-creator / per-project); a collision is
  rejected before the card.
- Returns `result.skill_id` and `result.target` (`"user"` or `"project"`).

### When to load this skill

- Before listing, reading, creating, or editing a skill (the user asks to
  "save this as a skill", "update my X skill", "share this skill with ...",
  "make this auto-load for the project").

### Restrictions

- `create_action_request` is top-level only -- sub-agents cannot issue skill
  writes (they should return the proposed `request_type` + `params` via
  `agent_task_response`).
- `system:*` skills are not editable.
- Project skill operations require the conversation to be in a project you own.
"""


def _routines_content(_base_url: str, _api_key: str) -> str:
    return """## Routines

Routines are canned prompts attached to the current project. Each routine
combines a name, a prompt, an optional model override, an optional schedule
(daily / weekly / hourly / every-N-minutes automatic runs), and a set of auto-loaded
skills merged into every conversation the routine creates. Running a routine
starts a new conversation in the project with the prompt as the first
message.

Everything here is project-scoped: these tools only work in a conversation
that belongs to a project, and only see that project's routines.

### Reads (call directly)

- **list_routines()** -- list this project's routines. Each entry carries
  `id`, `name`, the full `prompt`, `model` (null = the default model),
  `schedule` (null when unscheduled; otherwise `schedule_type`,
  `daily_time_local` + `timezone` (daily and weekly) / `weekly_days` /
  `hourly_minute` / `interval_minutes`, `is_enabled`, `is_running`,
  `next_due_at`, `last_run_completed_at`), and
  `autoloaded_skills` (`[{id, name}]`). Always list first so you have the
  current `id`s and values before proposing an edit.

### Writes (always go through an action request)

Routine writes render an inline Approve / Revise / Deny card, BLOCK until
the user resolves it, and return the verdict directly (see
`system:action_requests` for the shared mechanics). In Slack-driven
conversations `create_action_request` is disabled, so routine writes are
unavailable there.

#### create_routine

```
create_action_request(
    request_type="create_routine",
    params={
        "name": "<name>",              # required, <= 100 chars, unique per project
        "prompt": "<prompt>",          # required, <= 16 KB
        "model": "<model id>",         # optional; a valid non-deprecated model id
        "schedule": { ... },           # optional; same spec shape as edit_routine
        "skill_ids": ["<uuid>"]        # optional; skills to auto-load
    },
    reasoning="<why>",
)
```

- Creates the routine in the current project. Name collisions, inaccessible
  skills, and public projects are rejected same-turn before any card.
- Omitting `model` picks a default server-side (Claude Sonnet 5, falling
  back to Gemini 3.7 Flash when Sonnet is unavailable); the approval card
  shows the chosen model marked "(default)". Omitting `schedule` creates a
  manual-run-only routine.
- Returns `result.routine_id`.

#### edit_routine

```
create_action_request(
    request_type="edit_routine",
    params={
        "routine_id": "<uuid>",        # required, from list_routines
        "name": "<new name>",          # optional, <= 100 chars, unique per project
        "prompt": "<new prompt>",      # optional FULL replacement, <= 16 KB
        "model": "<model id>",         # optional; a valid non-deprecated model id
        "clear_model": true,           # optional; reset to the default model
        "schedule": {                  # optional FULL replacement of the schedule
            "schedule_type": "daily",  # daily | weekly | hourly | every_n_minutes
            "daily_time_local": "09:00",          # daily + weekly, "HH:MM"
            "timezone": "America/New_York",       # daily + weekly, IANA name
            "weekly_days": [0, 2, 4],             # weekly only, 0=Mon .. 6=Sun
            "hourly_minute": 15,                  # hourly only, 0-59
            "interval_minutes": 30,               # every_n_minutes only, 1-1440
            "is_enabled": true                    # optional, default true
        },
        "clear_schedule": true,        # optional; delete the schedule
        "add_skill_ids": ["<uuid>"],   # optional; enable skill auto-loads
        "remove_skill_ids": ["<uuid>"] # optional; disable skill auto-loads
    },
    reasoning="<why>",
)
```

- Partial edit: send only the fields you want to change; at least one is
  required (`routine_id` alone is rejected).
- `prompt` is a full replacement (routine prompts are small), NOT a
  search-and-replace. Read the current prompt from `list_routines` and send
  the complete new text.
- `model` / `clear_model` and `schedule` / `clear_schedule` are mutually
  exclusive pairs. Each routine has at most one schedule; `schedule` creates
  it when missing and fully replaces it otherwise. For a daily or weekly
  schedule, pass the user's timezone (check `get_current_time` if unsure).
- A run due while the server was down still starts when it comes back, if
  it is not too late (hourly 45 min, daily 6 h, weekly 24 h); otherwise it
  is recorded as missed. A run cut off by a restart is retried once.
- `add_skill_ids` / `remove_skill_ids` take skill ids from `list_my_skills`;
  you can only add skills you can access (own / shared-with-you / public).
- The routine must belong to the current project; bad ids, name collisions,
  and inaccessible skills are rejected same-turn before any card.
- If the routine changes while the approval card is open, the approve fails
  cleanly -- re-run `list_routines` and propose the edit again.
- There is no agent-side delete: users delete routines in the project UI.
  The deprecated per-routine guide override is not settable or editable
  here either.

### When to load this skill

- When the user asks to inspect, create, or change a project's routines:
  set up a new canned prompt, rename one, tweak its prompt or model, put it
  on a schedule (or pause/remove one), or change which skills it auto-loads.
"""


def _workspace_content(_base_url: str, _api_key: str) -> str:
    return f"""## Workspace files, run_python, and run_script

Each conversation has a per-conversation workspace directory mounted at
`/workspace` inside the sandboxed script container. Several tools operate on
it.

### Workspace file tools (via `tool_call`)

- **list_workspace_files()** — list every file in the workspace. Use this
  before trying to read a file whose exact path you don't already know.
- **get_workspace_file(path)** — read a workspace file. Supports text files,
  images (returned as inline image parts), and PDFs (returned as inline
  PDF parts). Do NOT use this for Office documents (`.docx`, `.xlsx`,
  `.pptx`); instead extract their contents inside `run_python` /
  `run_script` using `python-docx` or `openpyxl`. Very large files (over
  the per-model attachment limit, roughly 7-16 MB depending on the model)
  cannot be attached — split or reduce them first with `run_python` (e.g.
  `pypdf` page chunks for PDFs) and read the smaller pieces.
- **write_workspace_file(path, content)** — create or overwrite a workspace
  file. Use this for generated reports, intermediate data, scripts the user
  may want to keep, or any text artifact the user should be able to
  download.

### Hidden directories for non-user-facing files

Workspace entries whose name starts with a dot (`.`) are **hidden by default
in the file browser** (the user can reveal them with a show-hidden toggle).
Use this to keep scratch out of the user's view:

- Put temporary / intermediate files that the **user should not see** into a
  hidden subdirectory, e.g. `.temp/` — `write_workspace_file(".temp/raw.json", ...)`
  or a script that writes to `/workspace/.temp/...`. Throwaway scratch goes
  in `.temp/`; user-facing deliverables (reports, generated files the user
  should download) go in the workspace root or normal directories.
- `run_python` writes nothing to the workspace, so it is already the right
  choice for pure throwaway computation. Only when a script *must* persist
  intermediate files should you write them — and then prefer a hidden dir.
- For consistency, `authed_get(output_file=...)` already saves API response
  bodies under the hidden `.responses/` directory for the same reason. Read
  those back via the `.responses/...` path the receipt returns.

### `run_python` vs `run_script`

Both execute Python 3.12 inside the same sandboxed container, with the
workspace mounted read-write and `requests`, `openpyxl`, `python-docx`,
`matplotlib`, `seaborn`, `pypdf`, and `PyPDFForm` pre-installed. The `zip`
and `unzip` CLI tools are also available via `subprocess`.

For PDF work inside the sandbox, use `pypdf` (`from pypdf import PdfReader`)
for programmatic manipulation — merging, splitting, rotating pages,
extracting text or metadata — and `PyPDFForm`
(`from PyPDFForm import PdfWrapper`) for inspecting and filling PDF form
fields. To *read* a PDF's content for analysis, prefer `get_workspace_file`
(PDFs are returned as inline parts); the libraries are for
manipulation/generation. If a PDF is over the per-model attachment limit,
split it into page chunks with `pypdf` first and read the chunks.

**Document conversion (`.docx` / `.doc` / `.odt` / `.rtf` / `.html` -> PDF,
and between those formats):** headless LibreOffice Writer, Calc and Impress
are installed in the sandbox, together with the Microsoft core fonts (Arial,
Times New Roman, Courier New, Verdana, Georgia, Trebuchet MS, ...) and
metric-compatible substitutes for Calibri / Cambria (Carlito / Caladea).
**Use it for every document conversion** unless the user asks for a
different route; never stitch a PDF together from `python-docx` output --
that loses layout, fonts, images, headers/footers and tables. From
`run_python` / `run_script`:
`subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", "/workspace", "/workspace/report.docx"], check=True)`
-- about a second per document; the result lands in `--outdir` as
`report.pdf`. Any LibreOffice format works as source (`.docx`, `.doc`,
`.odt`, `.rtf`, `.html`, `.txt`, `.xlsx`, `.pptx`, ...) or as target
(`--convert-to docx`, `odt`, `xlsx`, `pptx`, `png` for a first-page
preview, ...). For a document stored in Google Drive, `download_drive_file`
it first, then convert. Native Google Docs / Sheets / Slides have no bytes
to convert -- export Docs with `google_export_doc` instead. Do not pass
`-env:UserInstallation` yourself; the `soffice` wrapper already provides a
per-run profile.

- **run_python(script, args?, timeout?)** — pass inline Python source as a
  string. Nothing is written to the workspace. Use this for one-off,
  throwaway tasks: analysing an uploaded file, quick data processing,
  one-time computations.
- **run_script(path, args?, timeout?)** — execute a script that already
  lives in the workspace. Use this for scripts the user will want to keep,
  re-run, or modify. The standard pattern is:
  1. `tool_call(tool_name="write_workspace_file", arguments={{"path": "analyze.py", "content": "..."}})`
  2. `run_script(path="analyze.py")`
  3. `tool_call(tool_name="get_workspace_file", arguments={{"path": "results.json"}})`

### Talking to Quest from inside the sandbox

- Quest's sandbox tool API is the ONLY host endpoint reachable from inside
  the container. Build its base URL from the injected `QUEST_PORT`
  environment variable (the port differs from the main Quest server):
  `base_url = f"http://localhost:{{os.environ['QUEST_PORT']}}"`.
- Authenticate with the `QUEST_API_KEY` environment variable — for example:
  `auth_headers = {{"Authorization": f"Bearer {{os.environ['QUEST_API_KEY']}}"}}`.
- That port serves ONLY the script-facing endpoints below (`/api/tool-call`,
  `/api/authed-get`, `/api/authed-post`, `/api/gmail-simple/*`); the rest of
  the Quest API is not reachable from the sandbox.
- For external (authenticated) APIs available via `authed_get`, POST to
  `{{base_url}}/api/authed-get` with a JSON body of `{{"url": "https://..."}}`
  (and optional `"headers": {{...}}`). Credentials are injected
  automatically. Example:
  `requests.post(f"{{base_url}}/api/authed-get", headers=auth_headers, json={{"url": "https://pro-api.coingecko.com/api/v3/..."}})`
- For the narrow set of read-shaped POST endpoints available via `authed_post`
  (today only GCP `organizations:search` and Cloud Logging `entries:list`), POST
  to `{{base_url}}/api/authed-post` with a JSON body of `{{"url": "https://...", "body": {{...}}}}`
  (and optional `"headers": {{...}}`). Credentials are injected automatically.
  Example:
  `requests.post(f"{{base_url}}/api/authed-post", headers=auth_headers, json={{"url": "https://logging.googleapis.com/v2/entries:list", "body": {{"resourceNames": ["projects/p"], "filter": "severity>=ERROR"}}}})`
- An allow-listed subset of the dynamic tools is invocable from scripts by
  POSTing to `{{base_url}}/api/tool-call` with a JSON body of
  `{{"tool_name": "...", "arguments": {{...}}}}` — the same shape as the
  `tool_call` meta tool. Available: the Slack read tools (`list_slack_teams`,
  `list_slack_conversations`, `get_slack_conversation_history`,
  `get_slack_conversation_replies`, `search_slack_messages`,
  `get_slack_user_info`, `list_slack_users`, `find_slack_channel`), the Gmail
  Simple tools (`get_gmail_messages`, `list_gmail_labels`,
  `get_gmail_message_urls`, `create_gmail_draft`, `send_gmail_to_self`,
  `archive_gmail_message`, `list_gmail_quest_labels`, `modify_gmail_labels`),
  the Telegram reads (`telegram_get_me`, `telegram_list_dialogs`,
  `telegram_get_messages`, `telegram_list_contacts`), the memory reads
  (`memory_search`, `memory_list`), and -- when Quest Docs is enabled -- the
  doc reads (`list_docs`, `search_docs`, `read_doc`). JSON results come
  back as JSON; markdown results (e.g. `get_gmail_messages`) come back as
  plain text. A non-allow-listed tool_name returns a 400 listing what is
  available. Example:
  `requests.post(f"{{base_url}}/api/tool-call", headers=auth_headers, json={{"tool_name": "search_slack_messages", "arguments": {{"query": "deploy failed"}}}})`

### Charts and large output

- For charts and visualisations, use `matplotlib` or `seaborn` inside
  `run_python` / `run_script`. Save to the workspace
  (`plt.savefig('/workspace/chart.png')`). Don't try to print or encode
  image bytes to stdout.
- **To show a chart (or any workspace image) to the user, embed it in your
  reply** as a markdown image whose target is the workspace-relative path:
  `![Revenue by quarter](chart.png)`. The web chat resolves the path
  against this conversation's workspace and renders the image inline
  (PNG/JPEG/GIF/WebP only). Always use a path relative to the workspace
  root, even for files in subdirectories (`![](reports/fig1.png)`), and
  avoid spaces in filenames you plan to embed. You do NOT need to read the
  image back with `get_workspace_file` just to show it — only read it back
  when you yourself need to inspect the pixels (e.g. to check a render).
  Slack replies cannot render workspace images.
- For large textual output, write to a workspace file and read it back with
  `get_workspace_file` instead of returning everything via stdout.
"""


def _action_requests_content(_base_url: str, _api_key: str) -> str:
    return """## Action Requests (generic mechanics)

`create_action_request(request_type, params, reasoning)` is how you
propose any write operation that touches an external service — sending a
message, scheduling a calendar event, editing a spreadsheet, sending a
DM, etc. The request is appended inline to the conversation
with Approve / Deny buttons; nothing happens externally until the user
approves.

This skill covers the **generic** approval / cancel / retry / routing
mechanics that apply to every action request type. The **per-type
catalog** — exact `request_type` names and their required / optional
`params` — lives in each backend's `system:<name>` skill:

- Slack sends → `system:slack`
- Telegram sends → `system:telegram`
- Twitter/X DMs → `system:twitter`
- Google Calendar invites → `system:calendar`
- Google Drive uploads and folder creation (`upload_to_drive`, `create_drive_folder`) → `system:drive`
- Google Sheets cell edits (`edit_google_spreadsheet`) → `system:sheets`
- GCP VM hard resets (`reset_gcp_instance`) → `system:gcp`
- User memory writes (`create_memory`) → `system:memory`
- Cross-user subagent runs (`run_user_subagent`) → `system:user_subagents`
- Quest Doc changes to shared private docs (`write_doc`) → `system:quest_docs`

Before calling `create_action_request` for a given backend, load that
backend's system skill (if you haven't already) so you know the correct
`request_type` and parameter names.

### The golden rule
Never call an external-service write endpoint directly with
`curl_proxy_post`. Always go through `create_action_request` so the user
stays in control.

### Minimize approval round trips
Every request is a card the user must click through, so batch related
writes into as few requests as the type allows. Several request types
take list-shaped params for exactly this reason -- e.g. `upload_to_drive`
takes a `files` list (up to 10 files per request). Prefer one batched request
over a sequence of single-item requests, and prefer params that fold a
prerequisite into the same request (e.g. `upload_to_drive`'s
`new_folder_name` creates the destination folder in the same approval
instead of a separate `create_drive_folder` request). Only split when
the params force it (different destinations/targets, over the per-request
limit) or when the user should approve items separately.

### The `reasoning` field
Always include a clear, human-readable `reasoning` string. It is shown to
the user directly beside the Approve / Deny buttons and helps them decide
whether to approve. "The user asked me to send this reminder" is fine;
"send message" is not.

### Approval lifecycle

1. Your call to `create_action_request(...)` BLOCKS until the user
   clicks Approve / Revise / Stop on the inline card. The call returns
   the verdict directly — you do not need a separate
   `wait_for_handles` step to learn the outcome.
2. A card renders inline in the conversation. The action is **pending**
   for as long as the user takes to decide.
3. The user clicks Approve, Revise, or Stop.
   - Approve → the backend executes the action, the card updates to
     show success or error, and the call returns with `verdict:
     "executed"` plus the backend's result.
   - Revise → a deny that carries a free-text **feedback** message
     (e.g. "wrong channel, send to #general", "this is a Cherry not a
     Mango"). The call returns right away with `verdict: "denied"` and
     the feedback so you can adapt your next move (apologise, propose
     an alternative, fix the parameters and re-issue).
   - Stop → the action is discarded AND the conversation halts. The
     call does not return until the user sends a new message; when it
     does, it returns `verdict: "stopped"` and the new message arrives
     in the same turn as the current instruction.
4. A denied, stopped, or failed action is not retried automatically. If
   the user wants a retry, you'll see that in their next message — you
   can issue a fresh `create_action_request` with fixed parameters.
5. **Several independent actions in one turn.** You may emit multiple
   `create_action_request` calls in the same response (parallel tool
   calls). Each one gets its own card; the user can approve or revise
   them individually, and your turn resumes only after EVERY card is
   resolved, with each call returning its own verdict. Pressing Stop on
   any card stops every still-open card of the batch. Use this when
   the actions do not depend on each other (e.g. a Slack message and a
   calendar invite). When a later action needs an earlier one's result
   (e.g. `upload_to_drive` into a folder created by
   `create_drive_folder`), issue them sequentially instead.

### Reading the result

`create_action_request` returns one of these shapes directly:

```
{"verdict": "executed", "request_id": N, "result": {...handler return...}}
{"verdict": "denied",   "request_id": N,
                        "feedback": "<user text>",
                        "result": {"denied": true, "feedback": "<user text>"}}
{"verdict": "denied",   "request_id": N, "result": {"denied": true}}
{"verdict": "stopped",  "request_id": N, "result": {"stopped": true},
                        "note": "<why the call returned now>"}
```

On `verdict: "executed"`, `result` carries the backend's execute return
value (e.g. a Slack `ts`, a calendar event id, a Drive folder id) — read
the field your next tool call needs (e.g. `result.folder_id`).

On `verdict: "denied"`, `result` is `{"denied": true}`. When the user
clicked **Revise** the reason appears at `feedback` (top-level, next to
`verdict`) and inside `result.feedback`. Read it and adapt your next
move; when `feedback` is absent the user declined without comment, so
treat it as a plain deny.

On `verdict: "stopped"`, the user pressed **Stop**: nothing was
executed, the conversation was halted, and the call is returning now
only because the user sent a new message (it follows in this turn; the
`note` field says so). Treat that message as the current instruction
and do not re-issue the stopped request unless it asks for it.

### Multi-step accept-then-create example

To create a Drive folder *and* upload files into it:

1. `create_action_request(request_type="create_drive_folder",
   params={...}, reasoning="Creating a reports folder because ...")`
   blocks until the user clicks Approve / Revise / Deny.
2. The call returns `{"verdict": "executed", "request_id": 17,
   "result": {"folder_id": "abc", ...}}`. Read `result.folder_id`
   directly off the return value.
3. `create_action_request(request_type="upload_to_drive",
   params={"folder_id": "abc", ...}, reasoning="...")` — again
   blocks until the user approves.

If the user denied step 1 (with or without `feedback`), do NOT issue
step 2; instead apologise / clarify / re-issue step 1 with corrected
params based on the feedback. If they stopped step 1, do NOT issue
step 2 either -- act on the new message that arrived with the verdict.

### Re-trying after a failure
If an approved action fails at the backend (e.g. Slack rejected the
channel id, the upstream API rejected a field), the card will show the
error. When
the user asks you to retry, issue a **new** `create_action_request` with
corrected `params`. Do not resubmit the same parameters and expect a
different result.

### Exception: messaging the user themselves

To send a message to the user themselves (e.g. "send me a reminder", "DM
me", "message me on Slack"), use the `send_slack_dm_to_self` dynamic
tool (via `tool_call`) directly. This sends a Quest-bot DM, does NOT
require approval, works in automated / scheduled routines where there is
no human to approve action requests, and optionally attaches workspace
files (`files`: workspace-relative paths). Note: self-DMs appear from
the Quest bot, not the user. Full usage lives in `system:slack`.

### In scheduled / automated routines
Routines run unattended — there is no human to approve action requests
in real time. Because `create_action_request` now BLOCKS the call until
the user approves, a routine that issues one will park inside the call
for up to ~14 days waiting for the user to open the web UI and resolve
the card. Prefer `send_slack_dm_to_self`-style paths (see `system:slack`) for routine
writes; only call `create_action_request` from a routine when that
park-until-approved behaviour is acceptable.

### In Slack-driven conversations
`create_action_request` is **disabled** in Slack-driven conversations.
The approval UI is the Quest web app card, and a Slack-only user has
no way to see or resolve it; with the call now blocking, suspending on
a card the user cannot see would deadlock the conversation. Calls
short-circuit with a structured error. Deliver proposals as part of
the Slack reply text instead, and let the user open the Quest web app
if they want to perform a write that requires approval.

### Inside a sub-agent
`create_action_request` is exclusively a top-level agent tool -- it is
not available inside `agent_task` / `agent_task_parallel` /
`agent_task_parallel_template` runs. This applies to every
`request_type`, including `create_memory`. A sub-agent that needs a
write should return the proposed action (the `request_type`, full
`params`, and `reasoning` it would have used) to the parent via
`agent_task_response`, and let the parent issue
`create_action_request` after reviewing.
"""


def _project_db_content(_base_url: str, _api_key: str) -> str:
    return """## Project Database (project_db_query)

When a conversation belongs to a project, the project has a dedicated
SQLite database that persists across every conversation in that project.
Use `tool_call(tool_name="project_db_query", arguments={"query": "..."})`
to talk to it.

### When to use it

- The user asks you to track or store structured data ("track the
  candidates we're interviewing", "save these expenses", "log every
  newsletter signup").
- The user asks for a summary that requires aggregating or filtering
  prior records.
- A previous conversation in the same project created tables that this
  conversation needs to read or update.

### Schema

Create tables yourself when needed. Choose schemas that match the user's
data, not the user's exact wording. Prefer:

- `id INTEGER PRIMARY KEY AUTOINCREMENT` for synthetic ids.
- `created_at TEXT DEFAULT CURRENT_TIMESTAMP` and `updated_at TEXT` for
  audit columns.
- `TEXT` for free-form strings, `INTEGER` for counts/ids,
  `REAL` for monetary values (or `TEXT` if exact decimal precision
  matters).

### Limits and constraints

- A query may return up to a few thousand rows; very large result sets
  are truncated. Page with `LIMIT` / `OFFSET` if needed.
- Each query has a result-size cap; if you blow it, narrow the
  projection or paginate.
- `ATTACH DATABASE` is blocked for safety. You can only operate on the
  per-project database.
- Multiple statements per query are allowed (e.g. `CREATE TABLE; INSERT
  INTO ...;`).
- The Project DB tables are visible in the right-side panel's table
  browser, so the user can also inspect what you've stored.

### Patterns

- When the user describes new data to track, design the schema first
  (one or two `CREATE TABLE` statements), then insert.
- When updating existing data, do a `SELECT` first to confirm what's
  there before issuing an `UPDATE`.
- When asked for "everything we have on X", read all relevant tables
  via `SELECT` and present the data — don't paraphrase the schema.
"""


def _user_subagents_content(_base_url: str, _api_key: str) -> str:
    return """## Cross-User Subagents (`run_user_subagent`)

Run a read-only subagent inside ANOTHER Quest user's account -- with their
data access -- and get an approved response back. Both sides stay in
control: the current user approves the exact prompt before anything is
shared with the target account, and the target user approves the exact
response (and files) before anything comes back.

**Feature flag:** proposing a run requires the `user_subagents`
conversation flag on THIS conversation. Flags are set only on a
conversation's first message (composer Flags popover, or a leading
`%%flags[user_subagents]` line); if the request is rejected for a
missing flag, tell the user to start a new conversation with the flag
enabled -- it cannot be turned on mid-conversation.

### Proposing a run

```
create_action_request(
  request_type="run_user_subagent",
  params={
    "target_user_email": "colleague@example.com",   # required
    "prompt": "Summarize your Q3 pipeline notes ...",  # required, shown in full on the approval card
    "skill_ids": ["<skill-uuid>", ...],   # optional, max 10 -- autoloaded into the subagent
    "model": "claude-sonnet-5"            # optional, defaults to this conversation's model
  },
  reasoning="..."
)
```

Validation fails immediately (no card) when the target user does not
exist or cannot access any listed skill -- share the skill with them
first. `system:*` skill ids are rejected; the subagent can load those
itself. Write the `prompt` knowing BOTH users will read it: it is the
approval card's centerpiece for the current user and the first message of
the subagent conversation in the target user's account.

### After approval

The approve verdict's `result` includes a `wait_handle_id` and a
`subagent_conversation_id`. You MUST then call
`wait_for_handles(handle_ids=["<wait_handle_id>"], reason="Waiting for
the subagent response")` -- the subagent runs in the target user's
account (read tools + workspace only, no writes) and its response comes
back only after the target user approves its return call.

The resolved handle's `response` is one of:

- `{"status": "returned", "response": "...", "files":
  [".subagent_responses/report.md", ...], "from_user": "..."}` --
  success. Returned files were copied into THIS conversation's workspace
  under `.subagent_responses/` (read them with `get_workspace_file`).
- `{"status": "denied", "note": "..."}` -- the target user denied the
  return call. Do not retry without checking with the user.
- `{"status": "failed", "error": "..."}` -- the run failed.

### Notes

- Usage is billed to the target user's account; be considerate with
  prompt scope and model choice.
- One run per request; propose separate requests for separate targets.
"""


def _quest_docs_content(_base_url: str, _api_key: str) -> str:
    mb = 1024 * 1024
    body_cap = f"{DOC_MAX_CONTENT_SIZE // mb} MB"
    image_cap = f"{DOC_MAX_IMAGE_SIZE // mb} MB"
    assets_cap = f"{DOC_MAX_ASSETS} images / {DOC_MAX_ASSETS_TOTAL_BYTES // mb} MB"
    read_cap = f"{DOC_READ_MAX_CHARS:,}"
    return f"""## Quest Docs

A **Quest Doc** is a markdown document kept inside Quest. Unlike a
workspace file it is not tied to this conversation: it is owned by the user
(scope `user`) or by a project (scope `project`), survives after the
conversation ends, is rendered in the Quest UI (and downloadable as `.md` or
`.zip` with its images), and any later conversation can find it again with
`list_docs` / `search_docs`.

Put content in a doc when it should outlive this conversation and be found
again: notes, reference pages, reports, meeting summaries, running logs a
routine keeps appending to. Keep scratch work, intermediate data, scripts and
one-off downloads in the workspace. Structured rows belong in the project
database, short facts about the user in memories.

### Tools (call via `tool_call`)

Reads:

- **list_docs(scope?, limit?)** -- the docs this conversation can see,
  newest updated first. `scope`: `user` | `project` | `all` (default);
  `limit` 1-200 (default 50). Each row: `id`, `title`, `description`,
  `mode`, `scope`, `project_id`, `content_size`, `asset_count`,
  `updated_at`, `shared` (true when the doc has share recipients),
  `writable`, `write_note`.
- **search_docs(query, scope?, limit?)** -- case-insensitive substring match
  over title, description and body; `limit` 1-50 (default 20). Returns
  `{{results: [{{id, title, mode, scope, matches: [{{line, snippet}}]}}],
  truncated}}`. Search hits do NOT count as reading the doc.
- **read_doc(doc_id, start_line?, end_line?)** -- the markdown body plus
  `total_lines`, `writable`, `write_note`. Marks the doc as read in this
  conversation.

Writes:

- **create_doc(title, content, description?, target?)** -- a new doc.
  `target`: `user` (default; always a private doc) or `project` (project
  conversations only; the doc takes the project's mode). Titles are 1-200
  characters and unique per owner (per project for project docs),
  case-insensitively. The new doc counts as read.
- **edit_doc(doc_id, old_string, new_string, replace_all?)** -- exact string
  replacement (whitespace included); `old_string` must be unique unless
  `replace_all` is true; an empty `new_string` deletes the match.
- **append_to_doc(doc_id, content, ensure_blank_line?)** -- appends to the
  end; `ensure_blank_line` (default true) separates the new block with a
  blank line.
- **add_doc_image(doc_id, workspace_path, alt?, placement?)** -- embeds a
  workspace image (see Images below).

There is no whole-body replace, and no tool renames or deletes a doc: the
user does that in the UI. The body is capped at {body_cap}.

### Modes and who may write

Every doc is `private` or `public`. **User docs are always private.**
Public docs live only in public projects: a project doc takes its
project's mode for good. A doc's mode never changes -- there is no mode
switch, in the tools or in the UI. Private conversations (standalone chats,
private projects and their routines, Slack) create private docs, user or
project. Public-project conversations (and their routines) create only
public docs of their own project, with `create_doc(target="project")`;
`target="user"` is refused there.

`list_docs` and `read_doc` report the verdict for each doc in `writable`:
`free` (the write tools work directly), `approval` (a shared private doc:
changes go through a `write_doc` action request) or `denied` (read-only
here; `write_note` says why). The rules behind it:

- **The user's own private doc that is not shared with anyone**: written
  freely.
- **A private doc shared with anyone** (any share, read or write): every
  change needs the user's approval through a `write_doc` action request --
  for the owner and for people with write access alike. With a read-only
  share: "{doc_access.DENY_READ_ONLY_SHARE}"
- **Public docs** are written freely only from public-project
  conversations (by the owner and people with write access). Everywhere
  else they are read-only: "{doc_access.DENY_PUBLIC_DOC_FROM_PRIVATE}"
- **Private docs (every user doc included) are invisible to public-project
  conversations** -- there they behave exactly like a nonexistent id.
- **Project docs** are visible only from conversations of that project;
  standalone chats and other projects never see them.
- **Read-only runs** never write: sub-agents ("{doc_access.DENY_SUB_AGENT}"),
  inference API runs, cross-user subagent runs and sandbox scripts. A
  sub-agent returns the content to the top-level agent, which writes it.

### Read before edit

`edit_doc` works only on a doc this conversation has read with `read_doc`
(or created with `create_doc`); otherwise it tells you to read it first. A
`search_docs` snippet is not a read. If `old_string` is not found or is
ambiguous, re-read the doc and retry with the exact current text (or a
longer, unique excerpt). `append_to_doc` and `add_doc_image` need no read:
they overwrite nothing.

### Shared private docs: the approval handoff

When a write tool returns

```
{{"error": "approval_required", "message": "...",
 "suggested_request": {{"request_type": "write_doc", "params": {{...}}}}}}
```

forward the suggested request unchanged:

```
create_action_request(
    request_type="write_doc",
    params=<suggested_request.params>,
    reasoning="<why this change, in plain English>",
)
```

The three `params` shapes, for reference (always forward what the tool
returned rather than rebuilding it):

```
{{"operation": "edit", "doc_id": "<id>", "old_string": "...", "new_string": "...", "replace_all": false}}
{{"operation": "append", "doc_id": "<id>", "content": "...", "ensure_blank_line": true}}
{{"operation": "add_image", "doc_id": "<id>", "workspace_path": "chart.png", "alt": "Q3 revenue", "placement": "append"}}
```

The card shows the user the doc, how many people it is shared with, and a
line diff (or the image). Like every action request the call blocks until
the user resolves it and returns the verdict (shapes in
`system:action_requests`). At Approve the change is re-applied to the doc
as it is then: if the text (or the image file) changed meanwhile the
approve fails with the reason and the card stays open for the user to
retry or stop -- when you get the verdict back, re-read and propose again.
Do not propose `write_doc` for a doc whose `writable` is `free` (it is
rejected: call the tool directly) or `denied`. In `list_docs` /
`read_doc` output the `write_note` for these docs is
"{doc_access.APPROVAL_WRITE_NOTE}".

**Slack-driven conversations cannot open approval cards**, so a write to a
shared private doc fails there ("{doc_access.DENY_SLACK_NEEDS_APPROVAL}").
Tell the user; free writes still work in Slack.

### Images

Docs embed raster images (PNG, JPEG, GIF, WebP; max {image_cap} each,
{assets_cap} per doc; checked by content, SVG refused). Put the image in
the workspace first (e.g. a matplotlib chart saved to
`/workspace/chart.png`, or a downloaded file), then call
`add_doc_image(doc_id, workspace_path="chart.png", alt="...")`. The image is
copied into the doc's `assets/` (renamed `-2`, `-3`, ... on collision) and
with the default `placement: "append"` the line `![alt](assets/<name>)` is
appended in the same call -- one approval for a shared doc, not two. To put
the image elsewhere, pass `placement: "none"`: the result's `markdown` field
is the snippet, which you then insert with `edit_doc`. Image references in a
doc are always relative `assets/<name>` paths; external image URLs are not
rendered.

### Large docs

`read_doc` without a range returns up to {read_cap} characters (cut at a
line boundary, with `truncated: true` and a note when the doc is longer).
Page through the rest with `start_line` / `end_line` (1-based, inclusive)
using `total_lines`, or jump to the line numbers `search_docs` reports.

### Routine pattern: one doc, appended each run

A routine that collects something on every run (daily digest, weekly
metrics, a running log) keeps ONE doc and appends to it:

1. Find the doc by its title with `search_docs(query="<doc title>")` (or
   `list_docs`).
2. Only if it does not exist yet, create it once:
   `create_doc(title="<doc title>", content="# <doc title>\\n")` (add
   `target="project"` for a project-level log).
3. `append_to_doc(doc_id, content="## YYYY-MM-DD\\n\\n<this run's entry>")`
   -- one dated `##` heading per run.

Never rewrite the whole doc, and never create a new doc per run. Appending
needs no read, so each run stays cheap as the doc grows. If the doc is a
shared private doc, every append becomes an approval card that waits for
the user, which defeats an unattended routine.

### Sandbox scripts

`run_script` / `run_python` code can call `list_docs`, `search_docs` and
`read_doc` through the sandbox tool API (`/api/tool-call`, see
`system:workspace`). Scripts are read-only and have no conversation context,
so they never see project docs; the agent writes docs with the doc tools.
"""


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

CATALOG: dict[str, SystemSkill] = {}


def validate_system_skill_definition(skill: SystemSkill) -> None:
    """Static checks on a single skill definition. Raises ValueError.

    Shared by catalog registration and the plugin loader's manifest
    validation (config/plugins.py), so a plugin skill that would fail to
    register is rejected at validation time -- where tests exercise it --
    instead of only during the startup registration fan-out.
    """
    if not skill.id.startswith(SYSTEM_SKILL_PREFIX):
        raise ValueError(f"System skill id must start with '{SYSTEM_SKILL_PREFIX}': {skill.id!r}")
    if len(skill.description) > 120:
        raise ValueError(
            f"System skill {skill.id!r} description exceeds 120 chars "
            f"({len(skill.description)} chars): {skill.description!r}"
        )


def _register(skill: SystemSkill) -> None:
    validate_system_skill_definition(skill)
    if skill.id in CATALOG:
        raise ValueError(f"Duplicate system skill id: {skill.id!r}")
    CATALOG[skill.id] = skill


def register_system_skill(skill: SystemSkill) -> None:
    """Public registration API for system skills contributed outside this
    catalog (the plugin loader). Same validation as the core entries below;
    plugin skill ids are additionally required (by the loader) to be
    ``system:<plugin id>``-prefixed.
    """
    _register(skill)


# Per-backend skills (delegate to existing api/*/get_instructions)
_register(SystemSkill(
    id="system:gmail",
    name="Gmail",
    description="Read, draft, send-self, archive email via Gmail Simple tools and the Raw API.",
    when_to_load="Load when the user asks about email, inbox, drafts, or attachments.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: gmail_get_instructions(base_url),
))

_register(SystemSkill(
    id="system:calendar",
    name="Google Calendar",
    description="List calendars, events, free/busy via authed_get; writes via create_calendar_invite.",
    when_to_load="Load when the user asks about their schedule, meetings, or calendar events.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: calendar_api.get_instructions(base_url),
))

_register(SystemSkill(
    id="system:drive",
    name="Google Drive",
    description="Drive list/search/download and Save-to-Drive; multi-file upload via upload_to_drive; folders via create_drive_folder.",
    when_to_load="Load when the user asks about Drive files.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: drive_api.get_instructions(base_url),
))

_register(SystemSkill(
    id="system:docs",
    name="Google Docs",
    description="Read Docs via authed_get; list via Drive mimeType filter; export to workspace (pdf/docx/md/...) via google_export_doc.",
    when_to_load="Load when the user asks to read, list, or export/download Google Docs.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: docs_api.get_instructions(base_url),
))

_register(SystemSkill(
    id="system:sheets",
    name="Google Sheets",
    description="Read spreadsheet values via authed_get; list via Drive with mimeType filter; edit cells via edit_google_spreadsheet.",
    when_to_load="Load when the user asks about spreadsheet values, to list spreadsheets, or to edit spreadsheet cells.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: sheets_api.get_instructions(base_url),
))

_register(SystemSkill(
    id="system:slides",
    name="Google Slides",
    description="Read Slides presentations/pages via authed_get; list via Drive with mimeType filter.",
    when_to_load="Load when the user asks to read or list Google Slides presentations.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: slides_api.get_instructions(base_url),
))

_register(SystemSkill(
    id="system:tasks",
    name="Google Tasks",
    description="Read Google Tasks task lists and tasks via authed_get.",
    when_to_load="Load when the user asks about their task lists or tasks.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: tasks_api.get_instructions(base_url),
))

_register(SystemSkill(
    id="system:gcp",
    name="Google Cloud",
    description="Read GCP projects, Compute VMs, GKE clusters, orgs, and logs; hard-reset a stuck VM via reset_gcp_instance.",
    when_to_load="Load when the user asks about GCP projects, VMs/instances (incl. resetting a stuck VM), GKE/Kubernetes clusters, organizations, or logs.",
    requires="google_services",
    content_builder=lambda base_url, _api_key: gcp_api.get_instructions(base_url),
))

# system:slack is registered by the in-tree Slack plugin (plugins/slack)
# via register_system_skill(), and system:telegram by the in-tree Telegram
# plugin (plugins/telegram).

# system:twitter is registered by the in-tree Twitter/X plugin
# (plugins/twitter) via register_system_skill().

_register(SystemSkill(
    id="system:airtable",
    name="Airtable",
    description="Read bases/tables/records via authed_get.",
    when_to_load="Load when the user asks about Airtable bases, tables, or records.",
    requires="airtable",
    content_builder=lambda base_url, _api_key: airtable_api.get_instructions(base_url),
))

# system:github is registered by the in-tree GitHub plugin
# (plugins/github) via register_system_skill().

_register(SystemSkill(
    id="system:ramp",
    name="Ramp",
    description="Read Ramp spend data (transactions, cards, bills, reimbursements, vendors) via authed_get.",
    when_to_load="Load when the user asks about Ramp, corporate card spend, expenses, bills, or reimbursements.",
    requires="ramp",
    content_builder=lambda base_url, _api_key: ramp_api.get_instructions(base_url),
))

# Ungated: the Federal Register API is free/public and needs no connection, so
# requires=None makes this skill always visible/loadable.
_register(SystemSkill(
    id="system:federal_register",
    name="Federal Register",
    description="Search/read US Federal Register documents, agencies, public-inspection docs via authed_get (no auth).",
    when_to_load="Load when the user asks about federal rules, regulations, notices, executive orders, or agency filings.",
    requires=None,
    content_builder=lambda base_url, _api_key: federal_register_api.get_instructions(base_url),
))

# Ungated: the SEC EDGAR data API is free/public and needs no connection, so
# requires=None makes this skill always visible/loadable.
_register(SystemSkill(
    id="system:sec_edgar",
    name="SEC EDGAR",
    description="Read SEC EDGAR company submissions and XBRL financial facts via authed_get (no auth).",
    when_to_load="Load when the user asks about SEC filings, 10-K/10-Q, company financials, or EDGAR data.",
    requires=None,
    content_builder=lambda base_url, _api_key: sec_edgar_api.get_instructions(base_url),
))

# Cross-cutting skills (always available -- gates apply only to backend skills)
_register(SystemSkill(
    id="system:memory",
    name="Memory",
    description="Reads via memory_search / memory_list; writes via create_action_request(create_memory).",
    when_to_load="Load when the user refers to saved context, preferences, or asks you to remember.",
    requires=None,
    content_builder=_memory_content,
))

_register(SystemSkill(
    id="system:workspace",
    name="Workspace files",
    description="list/get/write workspace files plus run_python / run_script patterns.",
    when_to_load="Load for file-heavy tasks, generated artifacts, charts, or sandbox script work.",
    requires=None,
    content_builder=_workspace_content,
))

_register(SystemSkill(
    id="system:action_requests",
    name="Action Requests",
    description="Full reference for create_action_request types and their param shapes.",
    when_to_load="Load before sending messages, scheduling events, or other approval-gated writes.",
    requires=None,
    content_builder=_action_requests_content,
))

_register(SystemSkill(
    id="system:skill_management",
    name="Skill Management",
    description="Inspect skills (list_my_skills/get_skill) and create/edit them via create_action_request(create_skill/edit_skill).",
    when_to_load="Load when the user asks to list, read, create, edit, share, or auto-load a skill.",
    requires=None,
    content_builder=_skill_management_content,
))

_register(SystemSkill(
    id="system:user_subagents",
    name="Cross-User Subagents",
    description="Run an approved read-only subagent in another user's account via run_user_subagent.",
    when_to_load="Load when the user wants information gathered from another Quest user's account/data.",
    requires=None,
    content_builder=_user_subagents_content,
))

# Gated on the "docs" connected-services pseudo-key (the per-user Quest Docs
# feature gate, see api/instructions.py PSEUDO_SERVICE_KEYS) like the seven
# doc tools. Not "system:docs": that id is the Google Docs skill.
_register(SystemSkill(
    id="system:quest_docs",
    name="Quest Docs",
    description="Persistent Quest Docs: list/search/read/create/edit/append docs, embed images; shared-doc changes via write_doc.",
    when_to_load="Load when the user wants something kept as a doc, asks about their docs, or a routine maintains a running log.",
    requires=DOCS_SERVICE_KEY,
    content_builder=_quest_docs_content,
))

_register(SystemSkill(
    id="system:routines",
    name="Routines",
    description="Inspect project routines (list_routines) and create/edit them via create_action_request(create_routine/edit_routine).",
    when_to_load="Load when the user asks to list, create, or change a project routine (name, prompt, model, schedule, skills).",
    requires=None,
    requires_project=True,
    content_builder=_routines_content,
))

_register(SystemSkill(
    id="system:project_db",
    name="Project DB",
    description="project_db_query usage, schema/limits, when to create tables.",
    when_to_load="Load when the user wants to track or query structured project-scoped data.",
    requires=None,
    requires_project=True,
    content_builder=_project_db_content,
))


