# Documentation Style Guide

This guide defines how documentation should be written in the `./docs` folder. These docs are written **for AI agents, not humans**. An agent can read any source file directly, so documentation should point to code rather than explain or reproduce it.

## Purpose

Documentation in this project serves as a **navigation index for agents**. Agents read code; docs tell them *which* files to read and *why* the system is designed a certain way. The goal is:

1. **Locate source files** - Know which files to read for a given feature
2. **Understand data flow** - Trace requests through frontend → backend → external services
3. **Learn constraints** - Environment requirements, auth flows, deployment specifics
4. **Understand design decisions** - Why things are built a certain way

## Core Principles

### 1. Reference Files, Don't Duplicate Code

**Do this:**
```
The WebSocket streaming logic is in `frontend/src/services/PersistentWebSocket.ts`.
Message types are defined in `frontend/src/api/types.ts` (see the `Message` interface).
```

**Don't do this:**
```typescript
// Don't paste the actual code
export interface Message {
  role: 'user' | 'assistant';
  content?: string;
  // ...
}
```

### 1b. Don't Replicate Lists or Plain Facts That Live in the Code

The same rule applies to *data*, not just code. If a fact can be read straight out of a source file with no interpretation -- the members of a list, set, enum or registry, a default value, a date, a price, a limit, a mapping -- the doc names the symbol and file and stops there. It never copies the values. A copied list is stale the moment the code changes, nobody remembers to update it, and an agent that trusts the doc over the code then acts on wrong data.

The canonical case is the model catalog: docs point to `MODEL_REGISTRY` in `chat/llm/config.py` instead of spelling out "the available models are A, B, C, D". The same goes for allow-lists such as `TEMPLATE_BATCH_ALLOWED_MODELS`, enums such as `ModelId` in `db/models.py`, and defaults such as `DEFAULT_ROUTINE_MODELS`.

**Do this:**
```
Sub-agent batches may only run on the models in `TEMPLATE_BATCH_ALLOWED_MODELS`
(`chat/gemini_api/constants.py`); the set is the cheaper Flash- and Haiku-class
models because batch work is high-volume.
```

**Don't do this:**
```
Sub-agent batches may only run on `claude-haiku-4.5`, `claude-haiku-5-5`,
`claude-sonnet-4-6`, `gemini-3.5-flash-lite`, `gemini-3.6-flash`, ...
```

What *does* belong in the doc is what the code cannot say on its own: why the list exists, what rule decides membership, which code paths branch on it, and the consequence of changing it. Describe the shape ("the newer Claude models", "every entry flagged `deprecated`", "models whose `max_input_tokens` is 200K or less") rather than the current members. A single illustrative example id is fine; an enumeration is not. The same goes for `docs/index.md` summaries.

Per-model or per-entry rationale belongs as a comment next to the entry itself (the registry entries already carry theirs), so the doc can say "the reasons are on the entries" instead of restating them.

### 2. Document Flows with File References

Show request/data flows as sequences of file references, one step per file boundary. Don't narrate the control flow *inside* one file (see [rule 8](#8-dont-restate-code-comments)).

```
## Message Send Flow

1. User submits message → `frontend/src/components/Composer.tsx`
2. WebSocket frame sent → `frontend/src/services/PersistentWebSocket.ts` (send)
3. Backend receives → `chat/realtime/socket.py` (_handle_send_message)
4. Model invoked → `chat/gemini_api/conversation.py` (run_conversation_turn)
5. Response streamed back through same path
```

### 3. State Where Things Are Configured

Instead of showing config examples, point to the source:

```
## Configuration

- Server settings schema: `server_config.example.json`
- API endpoints: `frontend/src/api/config.ts`
- Login domain restriction: `auth/config.py` (`allowed_login_domain()`)
```

### 4. Document Environment Constraints

Be explicit about runtime requirements:

```
## Environment Requirements

- Podman must be available (sandbox scripts run in ephemeral containers)
- Ports: one main port serves both the API and the built frontend
- Storage: SQLite database for user data, JSON files for chat history
```

### 5. Explain Design Decisions Briefly

Capture the "why" to prevent agents from re-architecting. A design decision names **what was chosen, the alternative that was rejected, and why**, in one to three sentences. An entry that only restates what the code does is not a design decision; delete it. A decision lives in the doc of the feature it concerns.

```
## Design Decisions

**Why SQLite for user data instead of PostgreSQL?**
No separate server required, single-file deployment, Alembic handles migrations.

**Why a persistent WebSocket instead of polling?**
Responses stream token by token and several tabs need the same events; polling
would add latency and duplicate every fetch per tab.
```

### 6. Write the End State, Never the Change

A doc describes the system as it is, as if it had always worked that way. It never compares the code with an earlier version of itself. Words that do that are a red flag: "now", "no longer", "previously", "formerly", "used to", "replaced", "new", "since PR #N", "after devplan N". The change belongs in the commit message and the PR body.

A doc update written from a diff naturally narrates the diff ("the sidebar now shows X instead of Y"). Rewrite it as a description of the result ("the sidebar shows X").

The one exception is a **migration note a reader still has to act on** (stored data in an old shape, a setting users must change). It goes under a `Migration Notes` heading and says what to do, not what happened.

### 7. One Owning Doc per Topic

Every topic is described in exactly one doc. Every other doc that touches it gets at most one sentence and a link to the owner. Before writing about a topic, grep `docs/` for it: if another doc already covers it, edit that doc instead of adding a second description. Copies drift apart, and an agent cannot tell which copy is current.

`docs/architecture/overview.md` is the map: a code map and the cross-cutting decisions. It holds no feature detail.

### 8. Don't Restate Code Comments

If a module docstring, a class or function docstring, or a component's header comment already explains something, point at it and add only what spans files: which other modules depend on it, the invariant it protects, why it exists. A doc paragraph that paraphrases a docstring doubles the text and is the first thing to go stale.

### 9. Describe Behaviour, Not the UI Surface

Don't describe what the UI looks like: button order, icons, labels, keyboard hints, CSS class names, colours, spacing. These change constantly and the component shows them exactly. Describe the behaviour and constraints the component doesn't make obvious (what a button may not do, why a state is kept, which event refreshes what).

### 10. Keep Paragraphs Short

No paragraph runs past about 150 words. A paragraph that walks through every branch, error code or route is an inventory written as prose; cut it to the invariant plus a pointer to the code. A paragraph that carries several separate points becomes several paragraphs or a list.

## Document Structure

### Required Sections

Each doc should have:

1. **Overview** - One paragraph describing what this doc covers
2. **Key Files** - List of relevant source files with brief descriptions (a role per file, not a function list)
3. **Flows** (if applicable) - Step-by-step with file references
4. **Constraints** (if applicable) - Environment/runtime requirements
5. **Design Decisions** (if applicable) - Brief rationale for major choices

### File Naming

- Use kebab-case: `api-client-usage.md`, `chat-api.md`
- Group by category in folders: `architecture/`, `setup/`, `api/`

### Cross-References

Link to other docs and source files:

```
See [Architecture Overview](architecture/overview.md) for the code map.
Authentication is handled in `auth/session.py` (see the `get_current_user` dependency).
```

A link to a heading must match that heading's anchor; `tests/test_docs.py` checks it.

## Size Budgets

A doc has a word budget (`DEFAULT_WORD_BUDGET` in `tests/test_docs.py`). Docs that were already over it carry their own ceiling in `OVER_BUDGET_CEILINGS` in the same file, and a ceiling only ever goes down. A doc at its budget is trimmed or split **before** anything is added to it; raising a ceiling to make room is not an option. When an edit shrinks an over-budget doc, lower its ceiling in the same change.

## What NOT to Include

- **Inline code blocks** - Reference the file instead
- **Full API response examples** - Just describe the shape and reference types
- **"Workflow Recommendations" or tutorial sections** — Don't write numbered-step instructions showing how to call an API or perform a task. Agents read code and the system skills directly. Sections titled "Workflow Recommendations" are banned
- **Generated content** - No auto-generated API docs
- **Changelogs** - Use git history (see [rule 6](#6-write-the-end-state-never-the-change))
- **Phase labels or chronological organization** - Organize by topic; use git history for timeline
- **Plans and status** — no "Not Yet Implemented", "Future Enhancements", "Modified Files" or "Testing Considerations" sections. Plans live in `devplans/`, status in git and the issue tracker
- **Lists of test files or test cases** — say where the tests live in one sentence; an agent can glob them
- **Module dependency lists** - Import dependencies are apparent from the code itself
- **ASCII trees of component or file layout** - the directory listing and the JSX show the hierarchy exactly
- **Function/endpoint inventories or parameter listings** — Don't list out functions, endpoints, query parameters, request body fields, or response fields. These are in the code (Pydantic models, FastAPI signatures, function docstrings). Just reference the file and function name: "See `chat/routes/conversations.py` (`list_conversations`)"
- **Copies of lists, enums, registries, defaults, or any other fact readable verbatim from the code** — model ids, allow-lists, enum members, default values, dates, prices, limits. Name the symbol and file (see [1b](#1b-dont-replicate-lists-or-plain-facts-that-live-in-the-code)); describe the rule that decides membership, never the current members. "Currently X, Y and Z" phrasing is a red flag
- **Human-oriented instructional content** — No curl examples, no "To create X, do Y" instructions, no step-by-step procedures aimed at a human reader. The audience is an agent that reads and executes code directly
- **"Related Documentation" sections** - Don't add sections dedicated solely to linking other docs. The top-level `docs/index.md` (auto-included in `CLAUDE.md`) already serves as a cross-reference index

## Organization Principles

### Organize by Topic, Not by Timeline

Documentation must be organized by **topic** (what a feature is, how it works) rather than **chronological development order** (when it was built).

**Don't do this** (phase/timeline organization):
```
## Phase 1: Backend Foundation
- Chat message storage
- User data stored in SQLite

## Phase 2: API Endpoints
- REST endpoints for conversation management
- WebSocket endpoint for streaming

## Phase 3: Frontend
- Vite + React + TypeScript configuration
```

**Do this** (topic-based organization):
```
## Chat Storage
- Chat message storage with JSON-based persistence
- User data stored in SQLite via SQLAlchemy ORM

## API Layer
- REST endpoints for conversation management
- WebSocket endpoint for real-time chat streaming

## Frontend
- Vite + React + TypeScript configuration
- Build pipeline for static asset generation
```

Specific rules:
- **Never use "Phase N.N" labels** as section headers or organizational markers
- **Never organize content chronologically** by development order -- group by what the feature *is*
- **Remove parenthetical phase references** like "(Phase 4.1)" from file/component descriptions. These are development history, not documentation
- **Remove phase labels from link descriptions** -- e.g., write "Route Dispatch Architecture" not "Route Dispatch Architecture (Phase 1)"
- **Use git history for changelogs** -- development chronology belongs in version control, not in reference docs

### Keep One Table of Contents

`docs/index.md` is the only table of contents. `docs/README.md` is a short entry point that points at it, not a second index, and `overview.md` is a code map rather than a list of docs.

## Example: Good vs Bad

### Bad (too much code)

```markdown
## Creating a Conversation

To create a conversation, call the API:

\`\`\`bash
curl -X POST -H "Authorization: Bearer KEY" http://localhost:8000/app/api/conversations
\`\`\`

Response:
\`\`\`json
{
  "id": "uuid-here",
  "created_at": "2026-01-30T12:00:00"
}
\`\`\`
```

### Good (reference-based)

```markdown
## Creating a Conversation

**Endpoint:** POST `/app/api/conversations` (see `chat/routes/conversations.py`, `create_conversation`)

**Auth:** session cookie or API key (the `auth/session.py` dependencies)

**Response type:** `Conversation` in `frontend/src/api/types.ts`

**Side effects:**
- Creates a row in the `conversations` table (see `db/conversation_store.py`)
- The conversation's directory is created lazily on first access
```

### Bad (parameter listing + workflow steps)

```markdown
### Event List Parameters

- `maxResults` - Maximum number of events per page (default: 250)
- `timeMin` - Lower bound for event start time (RFC3339)
- `timeMax` - Upper bound for event end time (RFC3339)
- `q` - Free text search terms

## Workflow Recommendations

**Searching for events:**
1. Call `authed_get` with `timeMin` and `timeMax` parameters
2. If `nextPageToken` in response, add `pageToken` to next request
```

### Good (pointer to code)

```markdown
## Calendar Read Access

Calendar reads use `authed_get` with the Google Calendar API v3. Allowed paths and parameters are defined in the `_SERVICE_REGISTRY` entry in `chat/gemini_api/authed_get.py`. The usage instructions the LLM sees are the `system:calendar` skill in `chat/system_skills/catalog.py`.
```

### Bad (narrates the change)

```markdown
The sidebar no longer has a Requests section; it was replaced by an icon in the
new brand bar (previously the badge polled every 30 seconds, now it listens to
the WebSocket).
```

### Good (end state)

```markdown
The requests inbox is an icon button in the sidebar brand bar
(`frontend/src/components/sidebar/RequestsBadge.tsx`); its count refreshes on
the `request_count_changed` global from the persistent WebSocket.
```

## Docs Index (`docs/index.md`)

The file `docs/index.md` is a quick-reference index with one table row for every doc file in `docs/` **and every plugin doc under `plugins/<dir>/docs/`** (listed in the index's "Plugins" section). It is auto-included into `CLAUDE.md` via `@docs/index.md`, so every agent session pays for every character in it.

- Each row's summary is **under 150 characters** (`INDEX_SUMMARY_MAX_CHARS` in `tests/test_docs.py`).
- A summary says **what the doc covers**, not what the code does: name the topic and its main areas, and leave limits, defaults, symbols and behaviours to the doc. Naming the tool or mechanism that is the doc's subject (`authed_get`, `project_db_query`) is fine.
- A row changes only when its doc is added, removed, renamed, or its **scope** changes. A feature that adds detail inside an existing doc does not touch the row.

## Plugin Docs Live in the Plugin Directory

Documentation specific to a single plugin (its API reference, upstream app setup guide, etc.) lives inside that plugin's directory under `plugins/<dir>/docs/`, not under `docs/` -- the plugin directory is self-contained (code, `instructions.md`, and docs move together). Cross-cutting docs stay in `docs/` (e.g. `docs/architecture/plugins.md` for the plugin loader itself, and setup guides for non-plugin core services like Slack). Plugin docs follow all the same style rules and are indexed in `docs/index.md`'s "Plugins" section.

## Updating Documentation

Update docs when a change moves a file path, changes a flow, adds or removes a constraint (env var, dependency, port), or changes a design decision. Don't update docs for implementation details that don't affect those. Adding, retiring or re-pricing a model, or changing any other list member or default, needs no doc edit when the doc already points at the symbol; if you find yourself updating a copied list, replace it with the pointer instead.

When you do update docs, work in this order:

1. **Find the owning doc** for each behaviour the change touches ([rule 7](#7-one-owning-doc-per-topic)). Edit that doc; other docs get at most a link.
2. **Grep `docs/` and `plugins/*/docs/` for every symbol, file and behaviour the change renamed or removed**, and fix or delete each hit. Most stale doc text comes from changes that added a description in one place and left the old one standing elsewhere.
3. **Rewrite the affected paragraphs in end-state voice** ([rule 6](#6-write-the-end-state-never-the-change)) so each one is correct as a whole. Delete what the change made untrue. A doc update that only adds lines is suspect.
4. **Leave the `docs/index.md` row alone** unless a doc was added, removed, renamed or changed scope.
5. **Run the docs checks:** `uv run pytest tests/test_docs.py`.

## Automated Checks

`tests/test_docs.py` runs in the normal pytest suite and enforces the mechanical parts of this guide:

- `docs/index.md` lists every doc exactly once, one row each, every summary under the length limit
- every backticked repo file path in a doc names a file that exists
- every relative link resolves, including its `#anchor`
- no history or status markers (devplan and phase references, "Not Yet Implemented" and similar sections)
- every doc stays within its word budget, and over-budget ceilings stay tight

The rest of this guide (end-state voice, one owning doc, no restated comments) is on the writer.
