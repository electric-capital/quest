# 00009 - Separate conversation and project workspaces

Status: spec, interviewed 2026-10-07. Not started.

## 1. Summary

Today every conversation inside a project shares one directory, the project workspace. Everything
a conversation produces lands there: `authed_get` response bodies under `.responses/`, pasted
composer images under `pasted/`, sub-agent returns, plugin downloads (`github-job-logs/`,
`unifi-snapshots/`, `outlook-attachments/`), throwaway scripts, half-finished drafts. Over a
project's life the shared space fills with scratch that no later conversation wants, and the
deliverables get lost in it.

This change gives every project conversation its own **conversation workspace**, exactly like a
standalone conversation has today, and keeps the **project workspace** as a second, deliberately
used space:

- The conversation workspace is the default for everything. Every existing file tool, every
  attachment-taking tool, `run_script`, inline chat images and the per-conversation hidden
  directories keep working unchanged against it. Standalone conversations do not change at all.
- The project workspace is reached through a **separate set of project file tools**
  (`list_project_files`, `get_project_file`, `write_project_file`, `edit_project_file`) plus two
  **copy tools** that move files between the spaces, and is mounted at `/project` in the sandbox
  next to `/workspace`. The system prompt tells the model the project space is for durable,
  shared deliverables only, and nothing there needs approval.
- The right panel shows two file cards for a project conversation, **Chat Files** and
  **Project Files**, each with its own upload target, plus Copy/Move actions between them.

Decisions from the interview are marked **[decided]**; defaults picked without asking are marked
**[default]** and collected again in section 13.

## 2. Goals and non-goals

Goals

- A project conversation's scratch never lands in the project workspace unless the model or the
  user deliberately puts it there **[decided]**.
- Separate project file tools rather than a path prefix or a `space` argument on the existing
  tools **[decided]**.
- Strict addressing: a path means exactly one place; reads never fall back from one space to the
  other **[decided]**.
- Routines follow the same rule as every chat: conversation space by default, project space when
  the routine prompt says so **[decided]**.
- Writes into the project space are approval-free and prompt-guided, not action requests
  **[decided]**.
- Files move between the spaces three ways: a model copy tool in both directions, plain
  `shutil` between the two writable sandbox mounts, and Copy/Move actions in the file browser
  **[decided]**.
- Two file cards in the right panel, Chat Files and Project Files **[decided]**.
- Create Project from Chat moves nothing: files stay in the conversation space and the model is
  told, from the next turn on, that project tools are now available **[decided]**.
- Pre-existing project conversations get a system-prompt note that their earlier files live in
  the project space; no read fallback, no image fallback **[decided]**.

Non-goals for v1

- Referencing a project file directly from attachment-taking tools (Slack self-DM `files`, Drive
  upload, Gmail/M365 attachments, `add_doc_image`), from `run_script`'s `path` or from an inline
  `![](...)` chat image. The model copies the file into the conversation space first
  **[decided]**; a `project://` reference scheme is a later phase (section 12).
- Moving a conversation into an existing project, or between projects (no route exists today).
- Retention or quota for conversation workspaces (section 12 notes the opening this creates).
- Renaming the on-disk doubled `projects/{pid}/workspace/workspace/` layout (section 4.1).
- A per-routine default space setting (rejected in the interview; prompt guidance only).

## 3. Concepts and invariants

| Term | Meaning |
|------|---------|
| Conversation workspace ("chat files" in the UI) | `data/chats/{cid}/workspace/`. Exists for every conversation, standalone or in a project. Mounted at `/workspace` in the sandbox. The default target of every file-producing tool. |
| Project workspace ("project files" in the UI) | `data/projects/{pid}/workspace/workspace/` (unchanged on disk). Shared by all conversations of the project. Mounted at `/project` in the sandbox of project conversations. Reached only through the project file tools, the copy tools, scripts, and the Project Files card. |
| Space | One of the two above. Every path argument belongs to exactly one space, fixed by the tool or route it is passed to. |
| Legacy project conversation | A project conversation that produced output before this change shipped. Its earlier files are in the project space. |
| Converted conversation | A standalone conversation turned into a project's first conversation by Create Project from Chat after this change. Its files stay in the conversation space. |

Invariants the implementation must keep:

1. **Strict addressing.** A bare path passed to a conversation-space tool or route resolves only
   inside the conversation workspace, and a path passed to a project tool or project route
   resolves only inside the project workspace. No tool, route or renderer searches the other
   space on a miss **[decided]**.
2. **Standalone conversations are untouched.** Their tools, routes, prompt text and layout stay
   byte-for-byte compatible. Project tools exist only in project conversations.
3. **One resolver per space.** `ChatStorage` grows two root resolvers (section 4.2); no production
   code joins a root with `"workspace"` itself any more. The containment and canonical-id checks
   from data-paths.md apply to both.
4. **Every path crossing the boundary is validated on both ends.** Copy tools and copy routes run
   `validate_path` against the source space and the destination space independently, refuse
   symlinks and non-regular files, and never follow a symlink while walking a directory.
5. **Hidden-by-convention directories stay per conversation.** `.responses/`,
   `.subagent_responses/`, `.temp/`, `pasted/`, `github-job-logs/`, `unifi-snapshots/`,
   `outlook-attachments/` are created in the conversation workspace only.

## 4. Data model

### 4.1 Filesystem

```
data/chats/{cid}/
    chat_history.json, sdk_history.json, system_prompt.txt, loaded_skills.json,
    skill_reads.json, workspace_reads.json, doc_reads.json, responses/ (authed_get blobs) ...
    workspace/                 <- conversation workspace. NEW for project conversations;
                                  unchanged for standalone ones
data/projects/{pid}/
    project.db
    workspace/
        workspace/             <- project workspace (unchanged on disk)
```

`create_project_conversation()` creates `chats/{cid}/workspace/` the same way `create_conversation()`
does for standalone conversations, so the marker "conversation workspace dir exists" is reliable
for every conversation created after the cutover (used by section 10.1).

The doubled `workspace/workspace` for projects is kept as-is **[default]**: a rename would need a
data migration touching every install for no user-visible gain, and after this change only the
resolver knows about it.

### 4.2 Resolvers (`chat/storage.py`)

- `ChatStorage.get_conversation_workspace_root(conversation_id) -> Path`: `chats/{cid}/workspace`.
- `ChatStorage.get_project_workspace_root(project_id) -> Path`: `projects/{pid}/workspace/workspace`.
- `get_workspace_path(conversation_id, project_id=None)` is **removed**, together with
  `_get_workspace_dir()` in `tool_handlers/_common.py` and the `/ "workspace"` joins in
  `chat/file_storage.py`, `subagent_return.py`, `conversation.py` (composer attachments) and the
  seed script. `file_storage.validate_path(root, rel)` takes the browsable root directly.
- `conversation_access.resolve_owned_workspace(user_id, conversation_id)` now returns the
  conversation workspace root for any conversation. New
  `resolve_owned_project_workspace(user_id, project_id)` wraps `require_owned_project` and
  returns the project root (replaces the unused `resolve_owned_project_dir`).

### 4.3 Read-before-edit sidecar

`workspace_reads.json` stays per conversation and keeps bare keys for conversation-space files.
Project-space reads are recorded in the same file under a `project:` prefix
(`"project:reports/q3.md"`) **[default]**, so a `notes.md` in both spaces never collides.
`edit_project_file` checks the prefixed key; `edit_workspace_file` the bare key. Reading a project
file in conversation A still does not license an edit in sibling conversation B (unchanged rule).
Sub-agents share the parent's sidecar (unchanged).

### 4.4 Per-conversation notices (`chat_history.json` top level)

Two optional boolean fields, persisted once and read by the system prompt builder:

- `legacy_shared_workspace: true`: set the first time a turn runs in a project conversation that
  has no `workspace/` dir yet and already has at least one assistant message (section 10.1).
- `converted_from_standalone: true`: set by `POST /projects/from-conversation` (section 10.2).

No DB migration is needed.

## 5. Sandbox

`_build_script_podman_cmd` in `tool_handlers/sandbox.py` mounts:

```
-v {conversation_root}:/workspace:Z   -w /workspace          (every conversation)
-v {project_root}:/project:z                                 (project conversations only)
```

- `/project` is mounted with the shared `:z` relabel rather than `:Z` **[default]**: sibling
  conversations of one project can run containers concurrently against the same host directory.
  (Today's code already mounts the shared project dir `:Z`, so this is a fix, not a regression.)
- Both mounts are writable; `shutil.copy('/workspace/out.pdf', '/project/out.pdf')` is the
  scripted promotion route and the prompt documents it **[decided]**.
- `run_script` takes conversation-space paths only **[decided]**. To run a script kept in the
  project space the model copies it over first (copy tool) or runs it from `run_python`
  (`subprocess.run(["python3", "/project/etl.py"])`); the prompt says so.
- The public sandbox image gets the same two mounts for public-project conversations; its network
  profile, missing `QUEST_API_KEY`/`QUEST_PORT` and everything else stay as they are.
- `run_script` / `run_python` publish `file_list_changed` for **both** scopes after a clean
  container exit (section 8), since the `:Z`/`:z` mounts still make a cheap diff unavailable.
- The script tool-call bridge (`/api/tool-call`) excludes the project tools like it excludes the
  workspace tools: scripts have `/project` mounted.

## 6. Model-facing tools

### 6.1 Conversation-space tools (unchanged)

`list_workspace_files`, `get_workspace_file`, `write_workspace_file`, `edit_workspace_file`,
`run_script`, `run_python`, `download_drive_file`, `google_export_doc/sheet/slides`,
`authed_get/authed_post(output_file=...)`, `send_slack_dm_to_self(files=...)`,
`upload_to_drive(files=...)`, Gmail draft / self-send attachments, M365 attachments and
`m365_save_mail_attachment`, `github_get_job_log`, `unifi_get_camera_snapshot`,
`add_doc_image(path=...)`, `return_to_caller(files=...)` and the `.subagent_responses/` landing
dir all keep their schemas and resolve bare paths against the conversation workspace. For a
project conversation that is now a different directory than before; for a standalone one nothing
changes.

Schema wording that says "conversation/project workspace" is reworded to "this conversation's
workspace"; the attachment-taking tools gain one sentence: "Project files must be copied into the
conversation workspace first (`copy_project_file`)."

### 6.2 Project tools (project conversations only) **[decided]**

Added to the tool list exactly where `project_db_query` is added today (every tier, top-level and
sub-agent, nested sub-agent, routine, public), and never present outside a project:

| Tool | Arguments | Behaviour |
|------|-----------|-----------|
| `list_project_files` | none | Same output shape as `list_workspace_files`, over the project root. |
| `get_project_file` | `path` | Same as `get_workspace_file` (text inline up to the limit, images/PDFs as parts, per-model attach cap pre-flight, Office files refused with the `run_python` hint). Records `project:<path>` in the read sidecar. |
| `write_project_file` | `path`, `content` | Same as `write_workspace_file` (1 MB cap, creates parents, records the read). |
| `edit_project_file` | `path`, `old_string`, `new_string` | Same as `edit_workspace_file`, gated on the `project:<path>` sidecar key. |

The handlers are thin wrappers: the four workspace handlers are refactored to take a root
(`_list_files(root)`, `_read_file(root, path, sidecar_prefix)`, ...) and both tool families call
them. `ctx.project_id` is already on the dispatch context.

### 6.3 Copy tools (project conversations only) **[decided]**

| Tool | Arguments | Behaviour |
|------|-----------|-----------|
| `copy_file_to_project` | `path`, `dest?` (default: same relative path), `overwrite?` (default false) | Copies a file or directory from the conversation workspace into the project workspace. |
| `copy_project_file` | `path`, `dest?`, `overwrite?` | The reverse: project workspace into the conversation workspace. |

Rules for both:

- `path` must exist in the source space; `dest` is validated in the destination space; parents
  are created.
- Existing destination without `overwrite: true` -> structured error `destination_exists`, no
  partial write. With `overwrite` a file replaces a file; a directory merges entry by entry (same
  semantics as `copy_workspace_files` today).
- Symlinks are skipped, special files refused (`not_a_regular_file`), hidden entries copied only
  when named explicitly (copying a directory skips dot-entries unless `include_hidden: true`
  **[default]**). The `.responses/`, `.subagent_responses/`, `pasted/` roots are refused as a
  source for `copy_file_to_project` outright: they are scratch by definition.
- No size cap beyond the existing 200 MB per-file upload limit **[default]**; the copy runs in
  `asyncio.to_thread`.
- Both publish `file_list_changed` for the destination scope.
- Two tools rather than one with a `direction` field **[default]**: smaller models get the
  direction wrong less often when it is in the name.

### 6.4 Prompting

System prompt (`system_prompt.py`, all three variants: standard, public-project, sub-agent) gains
a short "Two file spaces" paragraph for project conversations only:

> This conversation has its own workspace (`/workspace` in scripts, the `*_workspace_file` tools)
> and the project has a shared workspace (`/project` in scripts, the `*_project_file` tools).
> Everything you produce goes to the conversation workspace by default: drafts, downloads,
> intermediate data, scripts, API responses. Put a file in the project workspace only when it is
> a finished deliverable that later conversations in this project should find, or when the user
> asks. Read project files with `list_project_files` / `get_project_file`; move finished work
> with `copy_file_to_project`. Attachments, inline images and `run_script` take conversation
> paths only: `copy_project_file` first.

`system:workspace` (`chat/system_skills/catalog.py`) gets a matching section, drops the
inaccurate "per-conversation workspace mounted at /workspace" opener in favour of the two-space
description, and keeps the `.temp/` guidance for scratch the user should not see at all.

`system:routines` and the routine-prompt UI help text add one line: a routine whose runs build on
each other must read from and write to the project space explicitly (section 10.4).

The `attached_filenames` message-metadata sentence changes to "in this chat's workspace".

Two conditional prompt notes (section 10):

- legacy: "This chat started before conversation workspaces existed: files it created earlier are
  in the project workspace. Use `list_project_files` / `get_project_file` to find them."
- converted: "This chat was just turned into a project. Its existing files are in the chat
  workspace; the project workspace is empty. Use `copy_file_to_project` for files that should
  become shared project files, or do so when the user asks."

## 7. HTTP API

### 7.1 Conversation routes (unchanged URLs, new meaning for project conversations)

`/conversations/{cid}/files`, `/files/upload`, `/files/content`, `/files/download`,
`/files/download-folder`, `/files/info`, `DELETE /files`, `/files/create-folder`,
`/files/save-to-drive`, `/composer-attachments` all operate on the conversation workspace via
`resolve_owned_workspace`. The `file_list_changed` they publish is `scope: "conversation"` always
(today a project conversation publishes `scope: "project"`).

### 7.2 Project routes (new)

A mirror set under `/projects/{pid}/files...` with the same handlers parameterised by root, via
`resolve_owned_project_workspace`: list, upload, content, download, download-folder, info,
delete, create-folder, save-to-drive. Hidden public projects 404 through `_get_visible_project()`
like every other project route. They publish `scope: "project"`.

### 7.3 Copy / move routes (new)

```
POST /conversations/{cid}/files/copy-to-project   {path, dest?, overwrite?, move?}
POST /conversations/{cid}/files/copy-from-project {path, dest?, overwrite?, move?}
```

Same rules as the copy tools (section 6.3); `move: true` deletes the source after a successful
copy. `409 destination_exists` without `overwrite`. Both publish `file_list_changed` for both
scopes.

### 7.4 Changed routes

- `POST /projects/from-conversation`: no longer moves files. Creates the project and its workspace
  dir, flips `project_id`, sets `converted_from_standalone` (section 10.2).
  `move_conversation_workspace_to_project` is deleted.
- `POST /conversations/{cid}/duplicate-workspace`: copies the **conversation** workspace only
  (today a project source copies the whole project workspace) into a new standalone conversation,
  as now **[default]**.
- `GET /conversations/{cid}`: unchanged shape; no new field is needed because project membership
  already comes back as `project_id`.

## 8. Realtime

`file_list_changed` keeps its `{scope, conversation_id, project_id}` shape. The FileBrowser
filter already distinguishes `scope === 'project' && project_id === projectId` from
`scope === 'conversation' && conversation_id === conversationId`; the Chat Files card subscribes
to the former, the Project Files card to the latter. Sandbox runs and copy routes publish one
event per scope.

## 9. Frontend **[decided]**

### 9.1 Right panel

`RightPanel.tsx` renders, for a project conversation, three `.right-panel-card` units: **Chat
Files** (FileBrowser over `/conversations/{cid}/files...`), **Project Files** (FileBrowser over
`/projects/{pid}/files...`), **Tables**. For a standalone conversation: one card, retitled from
"Workspace Files" to "Chat Files". For the home composer drilled into a project (URL `/`, no
conversation yet): Project Files + Tables only, served by the project routes, which **removes the
`useProjectWorkspaceProxy` borrowed-conversation trick** entirely.

The FileBrowser component takes a `source` prop (`{kind: 'conversation', id} | {kind: 'project',
id}`) that selects the API base and the `file_list_changed` filter; `useFileBrowser` and
`FileBrowserStateContext` key navigation state by `${kind}:${id}` so the two cards keep separate
paths and dotfile toggles. Upload, create-folder, delete, zip, Save to Drive and FileViewerModal
all go through the card's own source. The row-resize split handle today separates two cards; with
three cards it becomes two handles with the same persisted-height mechanism **[default]**.

### 9.2 Copy / Move actions

Each file and folder row's existing actions menu gains, in a project conversation, "Copy to
project" / "Move to project" (Chat Files card) and "Copy to chat" / "Move to chat" (Project Files
card), calling the routes in 7.3. On `409 destination_exists` the UI asks to overwrite. The rows
refresh from the two `file_list_changed` events.

### 9.3 Inline images and previews

`MarkdownImage`, composer attachment thumbnails, `DocImagePreview`, `SubagentReturnFilesPreview`
and `FileViewerModal` opened from chat keep resolving bare paths through
`/conversations/{cid}/files/download`, i.e. the conversation workspace. Project-space images are
not renderable inline in v1 **[decided]**; the prompt tells the model to copy first. Legacy
transcripts whose `![](chart.png)` pointed at a now-project-space file show the existing
broken-reference chip **[decided]**.

### 9.4 Create Project from Chat modal

The copy "the workspace files move into the project" changes to: "Your files stay with this chat;
the project starts with an empty Project Files space. Ask the chat, or use Move to project, to
share files with later chats."

## 10. Lifecycle and edge cases

### 10.1 Legacy project conversations **[decided: prompt note, no fallback]**

Detection at turn start (`run_conversation_turn`): the conversation has a `project_id`, no
`chats/{cid}/workspace/` directory, and at least one assistant message in its history. The turn
then creates the directory, sets `legacy_shared_workspace: true` in `chat_history.json`, and from
then on the legacy prompt note (6.4) is part of every turn's system prompt. Nothing moves. A
project conversation with no assistant message yet simply gets its directory and no note.

Consequences, accepted: old `get_workspace_file("report.md")` calls replayed from the transcript
context will miss until the model follows the note; old inline images in those transcripts show
the broken chip; the Chat Files card of a legacy conversation starts empty while Project Files
shows everything.

### 10.2 Create Project from Chat **[decided: move nothing, tell the model]**

Files stay in `chats/{cid}/workspace/`. The route sets `converted_from_standalone: true`; the
next turn's system prompt carries the converted note (6.4) and the project tools appear because
`project_id` is now set. The user can move files via the Chat Files card or ask the model.
The cached SDK session is still invalidated (system prompt changed), as today.

### 10.3 Sub-agents and user subagents

- `agent_task` sub-agents (and nested ones) run with the parent's `conversation_id` +
  `project_id`, so they see both spaces and get the project tools and copy tools whenever the
  parent does; their sidecar is the parent's.
- A user-subagent run is a standalone conversation in the target's account: conversation space
  only, no project tools, unchanged. Its returned files land in the **caller's conversation
  workspace** under `.subagent_responses/`, never in the caller's project space.
- Inference-API runs and Slack-driven conversations are standalone: unchanged.

### 10.4 Routines **[decided: conversation default, prompt guidance]**

Every routine run is a fresh conversation, so by default its output is scratch that disappears
from the project's view. A routine whose runs accumulate (weekly report appended, state file read
back next run) must say so in its prompt ("read `state.json` from the project files; write the
report to the project files"). Migration note for the release: owners of existing routines that
rely on the shared workspace must update their prompts; the release notes and the Routine
Settings prompt field help text call this out. The routine completion tool and scheduler are
untouched.

### 10.5 Public projects

Same two spaces, same tools (project tools and copy tools are added to `PUBLIC_TOOLS` /
`PUBLIC_ROUTINE_TOOLS` the way `project_db_query` is), same public image with two mounts. The
public prompt's "the project workspace is mounted read-write at /workspace" sentence is replaced
by the two-space paragraph. The "files the user uploads to this project are fair game" rule now
covers both spaces.

### 10.6 Quest Docs

`add_doc_image(path)` reads the conversation workspace only. The `expected_sha256` TOCTOU guard
on the `write_doc` card stays (a sibling conversation can no longer swap the file, but the user
can through the Chat Files card).

### 10.7 Deletion, archive, account delete

- Project delete already rmtrees every conversation dir and the project dir: nothing to add.
- Archiving a conversation or a project leaves both spaces on disk, as today.
- Account delete already covers every conversation and project dir.
- There is still no single-conversation delete; when one is added it must rmtree
  `chats/{cid}/` (which now holds real user files for project conversations too).

### 10.8 Names, collisions, odd inputs

- Copy tools and routes validate both ends with `validate_path`; `..`, absolute paths, symlink
  leaves and special files are refused exactly as uploads refuse them today.
- A copy of a directory onto itself or into its own subtree is refused (`invalid_destination`).
- Hidden entries (dot-prefixed) are skipped when copying a directory unless named explicitly or
  `include_hidden` is set; `.responses/`, `.subagent_responses/`, `pasted/` are never promoted.
- The 200 MB per-file limit applies to uploads only; copies between spaces have no cap.
- Concurrent sandbox runs of sibling conversations against `/project` are the same race as
  today's shared workspace; the `:z` label makes it work under SELinux enforcing.
- `list_project_files` in a project with thousands of files has the same unbounded `rglob`
  cost as `list_workspace_files`; both already run in a thread.

### 10.9 Seed data, tests, docs

- `scripts/seed_local.py` seeds project conversations with a conversation workspace and the
  project workspace separately.
- Tests: resolver containment for both roots; the four project tools; copy tools (both
  directions, overwrite, directory merge, hidden skipping, refusal list, symlink/special-file
  refusal); project routes incl. hidden-public-project 404; copy routes incl. 409 + move;
  legacy detection + note; converted note; sandbox argv builder with two mounts (`:z` on
  `/project`); `file_list_changed` scopes; from-conversation no longer moving; duplicate-workspace
  copying only the conversation space; frontend FileBrowser `source` prop + two-card RightPanel
  + Copy/Move menu.
- Docs: projects.md (workspace resolution, layout, from-conversation), data-paths.md (new
  resolvers), script-runner.md (two mounts), gemini-api.md (project + copy tools), file-browser-api.md
  + a new projects file-routes section in projects-api.md, frontend.md (right panel), routines.md
  (prompt guidance), public-projects.md, quest-docs.md (`add_doc_image` source), docs/index.md.

## 11. Security notes

- Both roots keep the canonical-id + containment checks from data-paths.md; the project root is
  only ever reached through `require_owned_project` (HTTP) or `ctx.project_id` (dispatch), never
  from a model-supplied id.
- The sandbox gains a second host-backed mount, so the no-symlink seccomp profile matters for
  `/project` exactly as for `/workspace`; the boot-time symlink scrub already covers
  `PROJECTS_DIR`.
- Copy tools are approval-free writes inside the user's own data, same trust level as
  `write_workspace_file`; they never read outside the two roots.
- No new credential, scope or network surface.

## 12. Later phases (designed for, not built)

- **`project://` references** in attachment-taking tools, `run_script` and inline chat images,
  so a project deliverable can be sent or shown without a copy. The strict one-space-per-argument
  rule makes this additive: a prefixed path is unambiguous.
- **Retention**: conversation workspaces of archived or old conversations are now cleanly
  prunable without touching project deliverables.
- **Project-level "promote" UI** beyond the row menu: multi-select move, or a "Promote" button on
  the Chat Files card.
- **Conversation move between projects**: now possible because conversation files travel with
  the conversation dir.

## 13. Defaults I chose; say the word to change any

| # | Default | Alternative |
|---|---------|-------------|
| 1 | Keep the doubled `projects/{pid}/workspace/workspace/` on disk | Flatten with a data migration |
| 2 | Sandbox mounts `/workspace` (`:Z`) + `/project` (`:z`) | `/workspace/project` nested mount |
| 3 | Two copy tools, `copy_file_to_project` / `copy_project_file` | One tool with a `direction` field |
| 4 | Directory copies skip dot-entries unless `include_hidden`; `.responses/`, `.subagent_responses/`, `pasted/` never promotable | Copy everything |
| 5 | No size cap on copies | Reuse the 200 MB upload cap |
| 6 | Project reads stored in `workspace_reads.json` under a `project:` prefix | Separate `project_reads.json` |
| 7 | Legacy / converted notices persisted as booleans in `chat_history.json` | New DB columns |
| 8 | Duplicate Workspace on a project conversation copies the conversation space into a new standalone conversation | New conversation in the same project |
| 9 | Standalone card retitled "Chat Files" | Keep "Workspace Files" |
| 10 | Three right-panel cards with two resize handles | Collapsible Project Files inside one card |
| 11 | Routine Settings prompt help text mentions the project-space rule | Release notes only |

## 14. Implementation phases

1. **Resolvers and routes**: two root resolvers, `validate_path(root, rel)`, conversation
   workspace dir for project conversations, project file routes, copy routes, `file_list_changed`
   scopes, from-conversation + duplicate-workspace changes. Tests.
2. **Tools and sandbox**: refactor workspace handlers by root, add project tools and copy tools to
   every tier, two mounts in `_build_script_podman_cmd`, bridge exclusion, prompt + skill text,
   legacy/converted notices. Tests.
3. **Frontend**: FileBrowser `source` prop, two cards + Tables, drilled-home Project Files via
   project routes (delete the proxy hook), Copy/Move row actions, Create Project modal copy,
   "Chat Files" rename. Vitest coverage for the card wiring and menu.
4. **Docs and seed**: every doc listed in 10.9, docs/index.md, seed script, release note on
   routines.
