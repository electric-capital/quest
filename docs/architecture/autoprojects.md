# AutoProjects Architecture

## Overview

An **AutoProject** is a long-lived unit of work that one user keeps prompting into. To the user it is one thread; underneath, every prompt is answered by a **run** that starts with a fresh model context. What carries over between runs lives in the AutoProject, not in a transcript:

- the **brief**: the current state of the work (facts, decisions, open questions, next steps), rewritten by runs, editable by the user and versioned;
- the **work log**: one entry per run with the prompt, a one-line `summary`, the `result` written for the user and the `notes` (the handoff) written for the next run;
- the **AutoProject workspace**: files every run reads and writes.

A run is an ordinary `conversations` row carrying `autoproject_id` (origin `"autoproject"`), the way a routine run carries `routine_id` ([Routines](routines.md)), so stop, approval cards, wait-handle resume and per-call cost records work through the existing machinery. The UI calls a run an "update"; code, routes and model-facing text say "run".

This doc covers the invariants, data model, run lifecycle, starting context, questions, the model-facing surface, files, the docs and tables an AutoProject owns, events, unread state, the `autoprojects` [feature gate](feature-gates.md) and the security posture. The HTTP routes and the WebSocket start sequence are in the [AutoProjects API](../api/autoprojects-api.md), the UI in [AutoProjects Frontend](autoprojects-frontend.md).

## Key Files

| File | Description |
|------|-------------|
| `chat/autoprojects/__init__.py` | Package map: which module owns what |
| `chat/autoprojects/constants.py` | Every cap, the run statuses, the run conversation's origin, the pseudo-key, the one not-found text and the gate-closed text |
| `chat/autoprojects/service.py` | The rules behind every route and the send path: AutoProject lifecycle, `require_owned_autoproject()`, `create_run_conversation()` / `start_run()`, `answer_question()`, the deletes and the startup sweeps (module docstring) |
| `chat/autoprojects/runs.py` | The `complete_autoproject_run` body, the run driver `drive_autoproject_run()`, `settle_finished_run()`, `stop_autoproject_run()`; the module docstring holds the run-status table |
| `chat/autoprojects/questions.py` | `ask_autoproject_question`: validation, the wait handle, the transcript message and the page's question view |
| `chat/autoprojects/context.py` | The `## AutoProject` prompt block, its sub-agent variant and the per-run freeze; the module docstring holds the size budget and the escaping rule |
| `chat/autoprojects/artifacts.py` | The delete-time capture and sweep of the Quest Docs and Quest Tables an AutoProject owns ([Docs and Tables](#docs-and-tables)) |
| `chat/artifact_access.py` | The AutoProject rule of the shared doc and table access procedure |
| `chat/autoprojects/events.py` / `chat/realtime/events.py` | The two realtime globals: audience and publishers (module docstring); the `make_autoproject_*` envelopes |
| `chat/autoprojects/routes.py`, `chat/autoprojects/http.py`, `chat/autoprojects/views.py` | The HTTP routes; error-code-to-status mapping and the route guard; the response shapes ([AutoProjects API](../api/autoprojects-api.md)) |
| `db/autoproject_store.py` | Every write to the three tables, the refusal classes and their codes; the module docstring holds the concurrency rule |
| `db/models.py` | `AutoProject`, `AutoProjectRun`, `AutoProjectBriefVersion`, `Conversation.autoproject_id`, `ToolWaitHandleKind.AUTOPROJECT_QUESTION` |
| `alembic/versions/4b57cca1158c_add_autoprojects_tables_and_conversations_autoproject_id.py` | Creates the three tables and `conversations.autoproject_id` (its docstring explains the raw `ADD COLUMN`) |
| `chat/gemini_api/turn_tools.py` | The two run loop arms, `SuspendForAutoProjectQuestion`, `FinishAutoProjectRun`, and the sub-agent context load in the spawn arms |
| `chat/gemini_api/tool_handlers/autoproject_runs.py` | The `list_autoproject_runs` / `read_autoproject_run` handlers |
| `chat/llm/tool_schemas.py` | `AUTOPROJECT_RUN_TOOLS`, `AUTOPROJECT_ONLY_TOOL_CALL_TOOLS`, the tool specs and `AUTOPROJECT_RUN_FILE_TOOL_SPECS` |
| `chat/gemini_api/system_prompt.py` | The run variant of the top-level and sub-agent prompts (`AUTOPROJECT_RUN_EXCLUDED_TOOLS`, the file-spaces and naming paragraphs) |
| `chat/system_skills/catalog.py` / `chat/system_skills/loader.py` | `system:autoprojects`, the `system:workspace` run note, the `requires_autoproject_run` scoping |
| `chat/storage.py` / `config/paths.py` | `AUTOPROJECTS_DIR`, the two resolvers, `create_autoproject_run_conversation()` |
| `chat/file_routes.py` | The AutoProject workspace routes and the copy routes to and from a run |
| `chat/realtime/socket.py` / `chat/wait_handles/resume.py` | The send-path start, the stop op, and the resume turn through the driver |
| `config/feature_gates.py` / `api/instructions.py` | `FEATURE_AUTOPROJECTS`, `autoprojects_enabled_for()`; the `autoprojects` pseudo-key |
| `quest.py` | Router mount, `run_startup_sweeps()` in the lifespan, the `serve_spa_autoprojects` catch-all |

The backend tests are the `test_autoproject_*` files in `tests/`; the frontend tests sit beside their modules.

## Invariants

1. **Fresh context.** A run conversation takes exactly one user message (`start_run()` refuses a second with `autoproject_run_single_message`), so a run's model history never holds another run's messages. Everything a run knows of earlier runs comes from its starting context and the workspace.
2. **Bounded context.** The `## AutoProject` block is at most `AUTOPROJECT_CONTEXT_MAX_CHARS` however many runs exist. Every cap that feeds it is enforced at write time, and the builder cuts each field again at render time ([Starting Context](#starting-context)).
3. **The AutoProject is the record.** A later run inherits only from `autoprojects`, `autoproject_runs` and the AutoProject workspace; deleting a run's transcript changes nothing it sees.
4. **User text is the user's.** The model writes only the brief, its own run's log entry, files, and docs and tables under their own access rule. It may set the title only while `title_is_default` is true (the title is still the one cut from the first prompt).
5. **A run writes only its own entry.** The run tools take no AutoProject or run id; both come from `ToolContext.autoproject_id` / `autoproject_run_id`, resolved from the conversation row by `run_conversation_turn()`.
6. **One active run per AutoProject** (`running` or `waiting`). `allocate_run()` in the store checks it inside the inserting transaction, and `start_run()` is its one send-path caller, so lifting the rule is a change in those two places.
7. **Owner only, one 404.** `require_owned_autoproject()` (`chat/autoprojects/service.py`) checks the gate, then ownership; a foreign AutoProject raises exactly what a missing one does, with `autoproject_not_found_message()` as the only text. Questions, runs and the read tools follow the same rule ([AutoProjects API -- Conventions](../api/autoprojects-api.md#conventions)).
8. **Private.** A run is never public and never in a project: `create_autoproject_run_conversation()` sets no `project_id`, `run_conversation_turn()` refuses a run that has one, and `get_public_connected_services()` pins the pseudo-key to false.
9. **Off means unchanged.** With the gate closed for the user nothing of the feature appears in a prompt, tool list, route or sidebar, and nothing is deleted ([Feature Gate](#feature-gate)).

## Data Model

### Tables

`autoprojects`, `autoproject_runs` and `autoproject_brief_versions`, plus `conversations.autoproject_id`, are modeled in `db/models.py`; the columns, indexes and cascades are on the classes. What the columns do not say:

- `brief_version` counts brief writes and is the brief's concurrency token. Every write also stores the text as an `autoproject_brief_versions` row whose `source` names the writer (`ui`, `run:<run_id>`, `restore:<version>`), and prunes the oldest rows past `AUTOPROJECT_BRIEF_MAX_VERSIONS` in the same transaction.
- `next_run_seq` only grows, so a run number is never reused after a run delete and the log index may show gaps.
- `event_seq` moves on every change the AutoProject page shows; `autoproject_changed` carries it. `unread_seq` / `read_seq` follow the conversation scheme ([Unread](#unread)). `last_activity_at` orders the sidebar.
- On a run, `context` and `context_brief_version` freeze its starting block on the first turn; `brief_conflict_version` records the brief version a `brief_conflict` result handed it; `handoff_missing` marks a run that finished without `complete_autoproject_run`.
- The run link sits on both sides: `autoproject_runs.conversation_id` (unique) is what the page reads, `conversations.autoproject_id` what the conversation side reads (list filters, the send path, `ToolContext`). Both cascade, so deleting a run's conversation deletes its log entry.
- Questions have no table: a question is a `tool_wait_handles` row ([Questions](#questions)).

### Store

`db/autoproject_store.py` returns plain dicts. Functions taking `user_id` are owner-scoped; the others take ids and serve callers that already checked access (the route guard, `ToolContext`, the driver). Every check-then-write (the per-user cap, one active run, the brief token) issues its write first and checks inside the same transaction, because SQLite takes its write lock at the first write. Every refusal is an `AutoProjectStoreError` subclass with a stable `code` and `details`; the routes map it through `chat/autoprojects/http.py` and the send path forwards it as the rejection reason. The store never touches the filesystem: its deletes return the ids the caller sweeps.

### Filesystem

The AutoProject workspace is `AUTOPROJECTS_DIR/<id>/workspace`, reached only through `ChatStorage.get_autoproject_dir()` / `get_autoproject_workspace_root()` ([Data Paths](data-paths.md#conversation-project-doc-and-table-path-resolution)). The AutoProject has no transcript or sidecar of its own; each run's conversation directory under `CHATS_DIR` has the usual layout, so per-conversation sidecars such as `workspace_reads.json` start empty in every run: a fresh context has read nothing.

## Run Lifecycle

### Start

A run starts in two steps ([AutoProjects API -- Starting a Run](../api/autoprojects-api.md#starting-a-run)). `POST /autoprojects/{id}/runs` calls `create_run_conversation()`, an early refusal for the UI that reserves nothing; it first deletes the AutoProject's unsent run conversations older than `AUTOPROJECT_UNSENT_RUN_CONVERSATION_GRACE`.

The first `send_message` on that conversation reaches `_handle_send_message()` (`chat/realtime/socket.py`), which calls `start_run()` before the user row is appended, so a refused send leaves nothing in the transcript. `start_run()` re-checks the gate, ownership, the archive flag, the one-message and one-active-run rules, the prompt cap (`AUTOPROJECT_PROMPT_MAX_CHARS`) and the model (`is_model_allowed(model, public=False)`), then allocates the `running` run. The model is the composer's pick, else `autoprojects.model`, else the server default, with the deprecated-model remap applied so the row records the model that runs.

A run's send skips the `%%flags` parsing, the attachments-during-resume check and the [expensive-resume gate](expensive-resume.md). Any early return between the allocation and the run task fails the run (`fail_unstarted_run()`).

### Drive

`drive_autoproject_run()` (`chat/autoprojects/runs.py`) wraps the first turn (`_run_send_message`) and every resume turn (`_run_resume`) and sets the run's status from the run row after each turn, never from the transcript. Its module docstring has the full table. In short: an accepted `complete_autoproject_run` completes the run and the loop ends with no further model call (`FinishAutoProjectRun`); a suspend (a question, an approval or a connect card) makes it `waiting`; a turn that ended with neither gets up to `MAX_AUTOPROJECT_COMPLETION_NUDGES` follow-up turns, each announced by a durable `autoproject_nudge` row (in `FLUSH_EVENT_TYPES`), after which the run completes with `handoff_missing` (the final text as the result, its first line as the summary, no notes). A run-fatal error fails it; a cancellation stops it.

A resume turn re-checks first: a run that finished meanwhile (a card approved after a Stop) runs no turn, a run whose owner's gate has closed is stopped instead of resumed, and a `waiting` run goes back to `running`.

### Complete

`complete_autoproject_run(summary, result, notes, brief?, expected_brief_version?, autoproject_title?)` validates every argument before writing anything; a refusal (`summary_too_long`, `brief_too_long`, ... with `cap` and `actual`) is a tool error and the run stays open for a retry. Then it writes the brief, the entry and the status, applies `autoproject_title` while the title is still the default, and settles the run.

The brief write is checked against `brief_write_base_version()`: the version the run's prompt showed (`context_brief_version`), or the `brief_conflict_version` a conflict handed the run. Any other `expected_brief_version` is ignored, so a model cannot overwrite a user edit it never saw. When the user changed the brief during the run, the entry is saved as pending (the page hides it while the run is active), the run stays open, and the tool returns `brief_conflict` with the current brief for the model to merge and call again. A run that ends without retrying keeps the pending entry and the user's brief wins.

### Stop, Failure and Settling

The WS `stop` op cancels the live turn or resume and the pending wait handles, then calls `stop_autoproject_run()`, which also covers a `waiting` run with nothing to cancel. Stop on a run's approval card ends the run too (`chat/action_request_routes.py`), since a run never gets the next message a Stop waits for in a chat ([Action Requests -- Stop](action-requests.md#stop)).

Every finish (completed, failed, stopped) goes through `settle_finished_run()`: it cancels the run conversation's pending wait handles (an open question reads as cancelled), closes its open action requests as `stopped` with reason `autoproject_run_finished` (`stop_open_action_requests_for_conversations()` in `db/action_request_store.py`), publishes `request_count_changed`, marks the AutoProject unread unless the run was stopped, and publishes the two AutoProject events.

### Startup Sweeps

`run_startup_sweeps()` runs in the `quest.py` lifespan before the scheduler. A `running` run lost its in-memory turn, so it fails ("interrupted by a restart") and is settled. A `waiting` run with a pending handle survives, because the handle does. A `waiting` run without one was answered just before the restart: its resume is kicked again, or it fails when there is nothing to resume. Run conversations created but never sent are deleted once older than `AUTOPROJECT_ABANDONED_RUN_CONVERSATION_MAX_AGE`.

## Starting Context

`build_autoproject_context()` (`chat/autoprojects/context.py`) renders what a run inherits: what an AutoProject run is and how to finish and ask, the title and run number, the brief (or a "No brief yet" line), the **log window** (full entries newest first: prompt excerpt, summary, notes, an outcome line for a run without a handoff) and the **log index** (one line per older run, then a count pointing at `list_autoproject_runs`). Results are never in it; `read_autoproject_run` returns them. The module docstring has the order, the window and index rules and the worst-case size budget.

`get_system_prompt()` places the block as the `## AutoProject` section after the stable sections and before the proxy preamble; the run's stable paragraphs (file spaces, naming note) stay in the cacheable part, so each run writes the prompt cache once.

The block is built once per run: `load_run_autoproject_context()` freezes it with its brief version on the run row on the first turn and returns the frozen pair on every later one. A run resumed after a question, a rebuilt session or a restart therefore sees the context it started with, and its brief write is checked against the version that context showed.

## Questions

`ask_autoproject_question(question, options?)` is a loop arm (`chat/gemini_api/turn_tools.py`) over `chat/autoprojects/questions.py`. It validates the question and options, creates a pending wait handle of kind `autoproject_question` (payload `{question, options}`, correlated to the run from creation, no expiry), records a durable `autoproject_question` transcript row and raises `SuspendForAutoProjectQuestion`; the driver marks the run `waiting`. An unanswered question keeps the run waiting until the user answers or stops it.

The answer comes only through `POST /autoprojects/{id}/questions/{handle_id}/answer` (`answer_question()`): it validates the answer, resolves the handle with `{answer}`, rewrites the transcript row in place, publishes `wait_handle_resolved` and `autoproject_changed`, and kicks the resume, whose bucket closes the tool_use with `{"answer": ...}` (any other terminal status reads `{"status": "cancelled"}`). The generic wait-handle resolve route refuses this kind with 409 `use_autoproject_answer_route`, since it would skip the validation, the gate and the rewrite. The kind is one of the multi-card gate's one-row-per-tool_use kinds, so a question and an approval card from one batch resume together ([Wait Handles -- Suspend / Resume](wait-handles.md#suspend--resume)).

## Model-Facing Surface

A run is a private standalone top-level conversation in everything else: `run_kind` stays `top_level`, and it gets the user's enabled skills, saved memories and connected services, no project section and no conversation flags. What differs:

- **Tier.** `run_conversation_turn()` gives a run `AUTOPROJECT_RUN_TOOLS`: the top-level tier plus `complete_autoproject_run` and `ask_autoproject_question`. Sub-agents keep their own tier, and shared dispatch refuses the two loop tools to sub-agents and scripts.
- **Read tools.** `list_autoproject_runs` (pages the whole log) and `read_autoproject_run` (one run's prompt, result, notes and questions) are core-tier `tool_call` tools in `AUTOPROJECT_ONLY_TOOL_CALL_TOOLS`, gated on the pseudo-key and excluded from every prompt but a run's and its sub-agents'. They are in `SCRIPT_TOOL_CALL_ALLOWLIST` (the sandbox lease carries the run's ids) and never public. Outside a run they answer `not_autoproject_run`; an unknown or deleted run number answers `run_not_found`.
- **Prompt.** The `## AutoProject` section stands alone, so a run needs no skill load to finish correctly. `set_conversation_name` is hidden and refused: the title comes from `autoproject_title`.
- **Skill.** `system:autoprojects` holds the longer guidance (what belongs in the brief, the notes and the result, when to ask, durable files in `autoproject://`). It requires the pseudo-key and `requires_autoproject_run`, so it is listed and loads only in a run. Loaded in a run, `system:workspace` gets a note on the run's two spaces prepended, and `system:quest_docs` and `system:tables` a note on the `autoproject` target and scope ([Docs and Tables](#docs-and-tables)).
- **Sub-agents.** The spawn arms load `load_autoproject_subagent_context()` (title and brief, read-only) into the sub-agent prompt and pass the run's ids to every sub-agent tool call, nested ones included, so `autoproject://`, the `/autoproject` mount and the read tools reach the parent run's AutoProject.

## Files

A run has two file spaces, like a project conversation with the AutoProject workspace in place of the project one:

- **Tools.** `chat://` is the run's own conversation workspace (scratch) and `autoproject://` the shared one. `parse_space_path()` (`chat/gemini_api/tool_handlers/file_paths.py`) refuses `autoproject://` outside a run (`no_autoproject`) and `proj://` inside one (`no_project`). A run's prompt offers the five scheme-qualified file tools in their AutoProject wording and hides the four `*_workspace_file` tools ([Conversation Loop -- Scheme-Qualified File Tools](gemini-api.md#scheme-qualified-file-tools)).
- **Sandbox.** The AutoProject workspace is mounted read-write at `/autoproject` with the shared `:z` label ([Script Runner -- Mounts](script-runner.md#mounts)).
- **HTTP.** The `/autoprojects/{id}/files...` routes mirror the project file routes, and two copy routes move entries between a run's workspace and its AutoProject's ([File Browser API](../api/file-browser-api.md#autoproject-workspace-routes)).
- **Events.** Writes to the AutoProject workspace publish `file_list_changed` with `scope: "autoproject"` and `autoproject_id`.

## Docs and Tables

A [Quest Doc](quest-docs.md) or [Quest Table](quest-tables.md) can belong to an AutoProject: `docs.autoproject_id` / `data_tables.autoproject_id` (migration `c5e8a1d3f7b2`, inline cascading keys like `conversations.autoproject_id`) name the owner, never together with a `project_id` (the stores refuse both). Its derived scope is `autoproject` (`doc_scope()` / `table_scope()` in the two stores), it is always private, and its title is unique per AutoProject. A user doc or table is one with neither id, so every "user" listing and title check filters on both.

- **Access.** `resolve_core()` (`chat/artifact_access.py`) takes the caller's `autoproject_id`, which `ToolContext` carries for a run and its sub-agents and scripts and which the two service `Caller`s pass on. From a run of the owning AutoProject and from the UI the artifact follows the ordinary matrix ([Quest Docs -- Access Rule](quest-docs.md#access-rule)): the run writes a doc and a table's rows freely, a shared one or one with the owner's require-approval switch goes through its card, and a table's schema always does. From the owner's other private standalone conversations, another AutoProject's runs included, it is read-only (cause `outside_autoproject`, `DENY_OUTSIDE_AUTOPROJECT` in both resolvers). Project conversations, public conversations and every conversation of a share recipient see nothing; a recipient reaches a shared one in the UI only, as for project docs.
- **Creation.** A run creates a doc with `create_doc(target="autoproject")` and proposes a table with a `create_table` card whose `target` is `"autoproject"`; both refuse the target outside a run and check that the AutoProject is the caller's. The person creates either from the AutoProject page (`POST /docs` and `POST /tables` with `autoproject_id`). An approval card proposed by a run rebuilds the run's caller from the conversation row (`get_conversation_autoproject_id()` in `db/conversation_store.py`), so its pre-card and execute steps see what the run saw.
- **Listing.** The list and search scope `autoproject` is the run's own AutoProject in a run and all of the user's AutoProjects from any other private standalone conversation; `all` includes both. The UI lists one AutoProject's docs and tables with `GET /docs?autoproject_id=` and `GET /tables?autoproject_id=` ([Quest Docs API](../api/quest-docs-api.md#docs), [Quest Tables API](../api/tables-api.md#endpoints)), which the [page's card](autoprojects-frontend.md#autoproject-page) reads. They are in neither "Your docs" nor All Docs and All Tables.
- **Prompting.** The `autoproject` values are in the tool registry's enums but in no prompt outside a run: the two skills' `autoproject_run_note` and `system:autoprojects` name them, so a conversation that is not a run reads the same text as before.
- **Delete.** `delete_autoproject()` captures the AutoProject's docs and tables with their shares (`artifacts.capture()`), lets the cascade remove the rows, then sweeps their directories and publishes the doc and table events to their audiences (`artifacts.sweep()`), the way a project delete does. An account delete sweeps them with the user's other docs and tables.
- **Gate.** The `autoprojects` gate is the only switch, and it is checked per viewer inside the access rule: `resolve_core()` (`chat/artifact_access.py`) answers Hidden for an AutoProject doc or table unless the caller passes `autoprojects_enabled`, which every caller takes from `autoprojects_open()` for the acting user. A user outside the gate therefore sees none of them: not in a list, not by URL, not from a chat, a script or a card, and not through a share (a recipient needs the gate too). The refusal is the ordinary `doc_not_found` / `table_not_found`. Creating one is refused with `autoproject_not_found`, the same body as for a missing or foreign AutoProject, and the routes that take `autoproject_id` answer 404 with it. Nothing is deleted, and reopening the gate restores everything.

## Realtime Events

Two per-user globals, published only through `chat/autoprojects/events.py`, best-effort and to the owner alone: `autoproject_list_changed` (no payload; list views re-fetch) and `autoproject_changed {autoproject_id, event_seq, brief_version, active_run}` (an open page re-fetches its newest runs, and its brief only when `brief_version` moved). They carry no AutoProject text and no `conversation_id`, so they reach the frontend's global handlers without an allow-list entry. The module docstring lists who publishes what.

Live progress is not an AutoProject event: the page subscribes to the active run's conversation and reads its streaming events ([AutoProjects Frontend](autoprojects-frontend.md#activity-and-subscriptions)).

## Unread

Unread state lives on the AutoProject, with the two monotonic seqs of [Conversation Unread State](conversation-unread.md). Run conversations are outside `UNREAD_ORIGINS`, so they never carry a dot of their own. A run that completes, fails or starts waiting raises `unread_seq` to `event_seq` (`mark_autoproject_unread()`); a stopped run does not, because the user was looking at it. A client acknowledges with `POST /autoprojects/{id}/read {seq}` once the newest update has been on screen in a focused window ([AutoProjects Frontend](autoprojects-frontend.md#read-state)); `mark_autoproject_read()` clamps the seq to `event_seq` and never lowers `read_seq`. Archiving marks the AutoProject read, and an admin impersonating the owner acknowledges nothing. There is no unread event: list rows and the detail carry the seqs, and `autoproject_list_changed` makes the lists re-fetch.

## Lifecycle

- **Archive** is refused while a run is active. It hides the AutoProject from the sidebar and refuses new runs; the page and files stay readable. Unarchiving counts against `MAX_ACTIVE_AUTOPROJECTS_PER_USER`.
- **AutoProject delete** (`delete_autoproject()`, steps in its docstring) cancels live turns, stops the active run, cancels the run conversations' handles and closes their open cards (reason `autoproject_run_deleted`), deletes the rows (runs, versions, run conversations and the AutoProject's [docs and tables](#docs-and-tables) cascade), sweeps the docs' and tables' directories, the `CHATS_DIR` directories and `AUTOPROJECTS_DIR/<id>`, then publishes.
- **Run delete** (`delete_run()`) is for finished runs only. It removes the entry and its conversation; docs, tables and AutoProject files the run created stay (they belong to the user or the AutoProject, not the run).
- **Account delete** (`delete_account()` in `chat/routes/user.py`) collects the AutoProject ids and the run conversations (`include_autoproject_runs=True`) before the cascade and sweeps their directories.
- **Conversation routes.** Run conversations are left out of the conversation list and search, cannot be compacted (409 `autoproject_run_conversation`) or converted into a project (400 `autoproject_run_conversation`), and open read-only by their own URL.
- **Inbox.** Enriched action-request rows of a run carry `autoproject_id` and `autoproject_title`, so the inbox links to the AutoProject ([Action Requests -- Requests Single-Pane View](action-requests.md#requests-single-pane-view)).
- **Cost.** LLM calls are recorded against the run conversation like any other, so the admin reports count runs as conversations.

## Feature Gate

The off-by-default, per-user `autoprojects` gate (`FEATURE_AUTOPROJECTS`, `autoprojects_enabled_for()`) is mirrored into the connected-services map as the pseudo-key `autoprojects` (`AUTOPROJECTS_SERVICE_KEY`). While it is closed for the user, every route answers 403 `autoprojects_disabled` first, a run cannot start, the read tools and the skill leave the prompt (a `load_skills` of the skill returns `autoprojects_disabled_message()`), the frontend hides the section and routes, and a resume arriving for a run stops it. A waiting run can still be stopped. Nothing is deleted, and reopening restores everything. See [Feature Gates -- Enforcement Layers (AutoProjects)](feature-gates.md#enforcement-layers-autoprojects).

## Security

- **Model-written text reaches later prompts.** The brief, summaries and notes are written by runs and injected into every later run without review. They are private to the owner and written only by the owner's runs, visible, editable and versioned on the page, and labelled in the block as notes on the state of the work, not instructions. Stored text sits inside the block's own tags, and `_neutralize()` escapes any of those tag names inside it, so stored text can neither close its element nor forge another. This exposure is why AutoProjects are private only.
- **Approval gates are unchanged.** A run is a top-level conversation, so every external write is still an action request the user approves. Closing a finished run's open cards keeps an approval from executing with no run behind it.
- **Existence never leaks.** Missing and foreign AutoProjects, runs and questions get identical bodies on every route and tool. Route integers are capped (`_MAX_INT` in `chat/autoprojects/routes.py`), so an oversized value is a 422, not an overflow deeper down.
- **At rest.** AutoProject text is plaintext in `quest.db`, like conversation metadata ([Encryption at Rest](encryption-at-rest.md#constraints)).

## Constraints

- Every cap lives in `chat/autoprojects/constants.py` and is enforced where the text is written: the AutoProject text and per-user count in the store, the log entry in `complete_autoproject_run`, questions and answers in `chat/autoprojects/questions.py`, the context window and index in the builder. `frontend/src/api/autoprojectsApi.ts` hand-mirrors the title and brief caps and the runs page sizes for the UI counters and paging.
- `AUTOPROJECT_CONTEXT_MAX_CHARS` is asserted by construction; raising a field cap shrinks the index budget before it can push the block past the bound.
- The run tools and the read tools need the run's conversation row: a run has no meaning outside its conversation.

## Design Decisions

**Why one conversation per run instead of one long conversation with compaction?**
A fresh context per prompt keeps the cost of prompt N independent of N and never exhausts the context window, and a run as a plain conversation reuses stop, approvals, resume and cost records. Compaction would still grow and would summarize without the model choosing what matters.

**Why a mandatory completion tool instead of a server-side summarizer?**
The run that did the work knows what the next one needs. One nudge plus the `handoff_missing` fallback keeps a run that ignores the tool from losing its output, at no extra model call.

**Why questions as wait handles instead of a table?**
Suspend, resume, restart survival, cancellation on Stop and the multi-card gate already exist for wait handles; a question is one more kind with its own answer route.

**Why freeze the starting context on the run row?**
The in-memory session dies with a restart or a model switch. Rebuilding the block then would change the prompt mid-run and move the brief version the run's write is checked against.

**Why ignore an `expected_brief_version` the run was not handed?**
Passing the current version would let a model overwrite a user edit it never read. Only the version of its starting context or of a `brief_conflict` result counts.

**Why one active run per AutoProject?**
Two runs finishing together would both rewrite the brief. The version token already gives the later one a merge, so lifting the rule is one change in `allocate_run()` and `start_run()`, but the composer and thread are built for one.

**Why private and owner-only?**
Model-written brief and notes flow into later prompts unreviewed; within one user's private work that is the user's own agent talking to itself. Shared or public AutoProjects would need a taint rule for that text.

## Scope

AutoProjects are top-level, owned by one user and private: they do not live in projects, are never public or shared, and a run takes exactly one prompt (no reply inside a run, no continuing an old one). Runs start only from the AutoProject page, not from Slack, routines, schedules or the inference API. There is no per-AutoProject cost display, no conversion of a chat into an AutoProject, no search over the log, and log entries can be deleted but not edited.
