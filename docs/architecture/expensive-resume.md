# Expensive-Resume Warning

## Overview

Resuming a long-idle, long-context conversation on a costly model re-reads the entire history at uncached input rates (the provider prompt cache expires after a short inactivity window), so a single resumed message can cost dollars.

When a **standalone** (non-project) conversation matches a configured rule (model family + minimum context tokens + minimum idle time), the web UI blocks the composer behind a warning card that offers cheaper alternatives (Duplicate Workspace, Create Project from Chat) and only unlocks after an explicit "Continue anyway" acknowledgement.

The server independently rejects unacknowledged sends, so the block cannot be bypassed by a stale tab. Project conversations never warn -- `check_expensive_resume` returns `None` when `meta.project_id` is set.

## Key Files

- `chat/expensive_resume.py` -- rule registry (`EXPENSIVE_RESUME_RULES`, a tuple of `ExpensiveResumeRule` dataclasses: exact model ids + min context tokens + min idle seconds; add entries to cover more models/thresholds), the DB-free pre-check (`candidate_rules`), the resume-cost estimate (`_estimate_resume_cost_usd`, reusing `db/llm_pricing.py` -- Anthropic priced as a full cache re-write at the 5m TTL rate, Gemini as plain prompt tokens), and the full verdict (`check_expensive_resume`, best-effort: returns `None` on any internal failure)
- `db/llm_call_store.py` -- `get_latest_context_tokens()`: the latest top-level call's input-side token count from the provider's raw `llm_calls_*` table (sub-agent rows excluded), using the same per-call context formula the pricing tiers key on
- `chat/routes/conversations.py` -- `get_conversation()` surfaces the verdict as the `expensive_resume` response field (`null` == no warning)
- `chat/realtime/socket.py` -- `_handle_send_message` rejects a matching non-first-message send with `{type: "send_message_rejected", reason: "expensive_resume_unacknowledged"}` unless the envelope carries `expensive_resume_acknowledged: true`
- `frontend/src/components/ExpensiveResumeWarning.tsx` -- the blocking card above the composer (three actions + "Learn more"); rendered by `ChatPanel.tsx`, which owns the action handlers (duplicate-workspace navigation, `ConvertToProjectModal` mount)
- `frontend/src/components/TokenCostModal.tsx` -- the "Learn more" modal explaining prompt-cache expiry and the projects-plus-short-conversations pattern
- `frontend/src/store/conversationStore.ts` -- per-conversation `expensiveResume` / `expensiveResumeAcknowledged` state; `WebSocketManager.sendMessage` reads the acknowledged flag into the WS envelope
- `frontend/src/hooks/useConversation.ts` -- seeds the store from the GET payload and exposes `expensiveResumeBlocked` / `acknowledgeExpensiveResume` to `ChatPanel` / `Composer`

## Flow

1. `GET /conversations/{id}` -> `check_expensive_resume(conversation_id, meta)` -> `expensive_resume` field on the response
2. FE seeds `conversationStore.setExpensiveResume` -> `ChatPanel` renders `ExpensiveResumeWarning`, `Composer` disables input (`expensiveResumeBlocked` prop)
3. User picks: Duplicate Workspace (`POST /conversations/{id}/duplicate-workspace`, navigate to the fresh chat), Create Project from Chat (`ConvertToProjectModal` -> `POST /projects/from-conversation`), or Continue anyway (`conversationStore.acknowledgeExpensiveResume`)
4. Acknowledged sends carry `expensive_resume_acknowledged: true` on the `send_message` envelope; the server gate in `_handle_send_message` re-checks and lets the turn through

## Design Decisions

**Why check both on load and on send?**
The GET-time verdict drives the UI; the send-time gate is the enforcement point (a tab left open from before the idle window would otherwise send without ever seeing the warning). The acknowledgement is in-memory only -- once the user continues, `last_message_at` resets and the rule stops matching, so nothing needs persisting.

**Why the latest top-level call for context size?**
The context the next turn re-reads is approximately what the previous top-level turn sent (Gemini `prompt_token_count`; Anthropic `input + cache_read + cache_creation`). Summing usage across calls would conflate cumulative spend with resume cost, and sub-agent rows would understate it.

**Why exact model ids instead of prefixes?**
Vertex model naming schemes change over time; a prefix could silently start (or stop) matching new ids. Each covered model is listed explicitly, kept in sync with `MODEL_REGISTRY` in `chat/llm/config.py`.

**Why a DB-free pre-check?**
`candidate_rules` decides from `meta` alone (model prefix, idle time, first-message state) whether the `llm_calls_*` query is worth running, keeping the common send path free of any analytics-table read.

**Why don't project conversations warn?**
Both alternatives on the card are wrong there: Duplicate Workspace copies only the conversation's own workspace into a new standalone conversation, outside the project (no project instructions, skills or Project Files), and `POST /projects/from-conversation` rejects conversations already in a project. The cheap in-project alternative -- a new conversation in the same project, which reaches the shared project workspace through `proj://` -- is already the product's recommended pattern; a project-aware variant of the warning could add that option later.
