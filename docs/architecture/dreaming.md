# Dreaming

## Overview

Dreaming is the offline review of each user's saved memories. A *dream* reads one user's active personal memories, asks the admin-chosen model for hygiene proposals -- merge near-duplicates, archive stale or contradicted facts, make relative dates absolute, shorten over-long entries -- and registers each proposal as a memory suggestion in the user's inbox ([Memory Suggestions -- The Inbox's Memories Tab](memory-suggestions.md#the-inboxs-memories-tab)). Nothing changes until the user accepts a suggestion, so a dream can only ever be wrong in the inbox, never in the memory store. It is the first, deliberately narrow step of offline memory consolidation: the input is the memory store itself, never conversation transcripts.

A background daemon dreams for every admitted user whose store has moved enough since their last dream; an admin can also dream for themselves at once from Settings > Dreaming. Every dream is a `dream_runs` ledger row with the model, the counts, the token usage and the list-price cost of its one model call.

## Key Files

| File | Role |
|------|------|
| `chat/dreaming.py` | The daemon (`dreaming_loop`), the pacing rules (`skip_reason`), the model choice (`resolve_dream_model`, `dreamable_models`), the prompts, the answer parsing and validation, and `run_dream` |
| `config/dreaming.py` | The admin settings in `data/dreaming.json` (`model`, `thinking_effort`; stdlib only) |
| `config/feature_gates.py` | `FEATURE_DREAMING` and `dreaming_enabled_for()` |
| `db/models.py` | `DreamRun` / `DreamRunStatus`; the `source`, `dream_run_id` and nullable `conversation_id` / `content` of `MemorySuggestion` |
| `db/dream_run_store.py` | The ledger: create / finish / latest per user / recent, and the new-memories count the pacing uses |
| `db/memory_suggestion_store.py` | Archive-only rows and rows without a conversation (see [Memory Suggestions](memory-suggestions.md)) |
| `alembic/versions/b4e8d2c7a1f3_add_dreaming.py` | Creates `dream_runs`; rebuilds `memory_suggestions` with the new shape |
| `chat/routes/dreaming.py` | `GET` / `PUT /app/api/admin/dreaming`, `POST /app/api/admin/dreaming/run` |
| `quest.py` | Starts and cancels the daemon in the lifespan |
| `frontend/src/components/settings/DreamingSection.tsx` | Settings > Dreaming (admin): model and thinking level, run-now, recent runs |
| `frontend/src/components/MemoryInbox.tsx`, `MemorySuggestionRow.tsx` | The "From a memory review" source line and the archive-only row |
| `tests/test_dreaming.py`, `tests/test_dreaming_migration.py` | The daemon, the stores, the routes; the migration end to end |

## The Daemon

`dreaming_loop()` is an asyncio task started from the FastAPI lifespan like the Slack notifier. Every `POLL_INTERVAL_SECONDS` (15 minutes) it returns at once while the `dreaming` gate is closed; otherwise it walks every user, skips those the gate does not admit (`dreaming_enabled_for`, plus `check_user_allowed`), and dreams for each one `skip_reason(user_id, scheduled=True)` lets through. Dreams run one at a time under a process-wide lock, so a sweep never competes with daytime users for provider quota, and `_dreaming_users` keeps a manual run from doubling a scheduled one.

## Pacing

`skip_reason()` returns why a user should not dream now, or None:

- `too_few_memories` -- fewer than `MIN_MEMORIES_TO_DREAM` (2) active memories: nothing to compare.
- `running` -- a dream is in flight for the user, or their latest run row is still `running` and younger than `STALE_RUNNING_HOURS` (1); an older `running` row belongs to a dream the process lost and no longer blocks.
- `pending_review` -- the user still has dream suggestions pending. Piling more onto an unreviewed batch helps nobody, and reviewing them changes the store the next dream should read.
- Scheduled dreams only: `too_soon` while the latest run (of any outcome, so a failing dream is not retried every poll) started less than `MIN_HOURS_BETWEEN_DREAMS` (24) ago, and `too_few_new_memories` until `MIN_NEW_MEMORIES` (3) memories were created since that run -- `MIN_MEMORIES_FIRST_DREAM` (5) for a user who never dreamed, so a first dream waits for a store worth reviewing.

A manual run (Settings > Dreaming) skips the two scheduled-only rules but not the others, and not the gate.

## Model Choice

A dream runs with no user present and costs the deployment, so the model is the admin's decision, not the user's composer pick. `resolve_dream_model(settings)` returns `(model, thinking_effort, automatic)`:

- Candidates are `dreamable_models()`: `get_available_models()` (configured, not failing a health check) filtered by `is_model_allowed(model, public=False)`. A dream reads private memories, so a model only allowed in public conversations never sees them -- the same fence Model Selection draws for conversations.
- The admin's pinned `model` wins while it is a candidate. Without a pin, or when the pin is unavailable (logged), the choice is automatic: the first of `MODEL_PREFERENCE` (Claude Opus 5.5, then Sonnet 5.5, then the previous generation) that is a candidate, else the first candidate. A pinned model that is down therefore falls back rather than skipping the night.
- `thinking_effort` is kept only when the chosen model lists it (`resolve_thinking_effort`); otherwise the model runs at its default level.
- With no candidate at all `DreamingUnavailable` is raised; the run row records it as a failure and Settings > Dreaming shows the reason.

The task is small and judgement-heavy (a few thousand input tokens, a short list out), which is why the default preference is the most capable model rather than the cheapest: a wrong merge costs the user's attention in the inbox, and the per-dream cost is cents either way.

## The Dream

`run_dream(user, trigger)` never raises; it returns the finished run row:

1. Opens a `running` ledger row (`trigger` is `scheduled` or `manual`).
2. Reads the user's active memories, newest first (`list_memories`).
3. Resolves the model, builds the prompts (`build_dream_prompts`) and makes one call on an ephemeral session with no tools and prompt-cache writes disabled (the prompt is never reused). The system prompt states the job (hygiene, not invention), the two proposal kinds and the rules; the user prompt carries today's date, every memory as `<memory id created>` and whether the store exceeds the full-text tier of `chat/memory_prompt.py`'s personal budget (older memories then reach conversations only as index lines), in which case consolidation is worth proposing.
4. Parses the answer (`parse_proposals`: a JSON object, tolerating a code fence or prose around it) and validates it (`validate_proposals`): each proposal names an active memory at most once; a `replace` carries non-empty text within `MAX_MEMORY_SIZE` that differs from the current text; at most `MAX_PROPOSALS_PER_DREAM` (10) survive. Dropped proposals are logged, never surfaced.
5. Registers each survivor with `create_suggestion(..., conversation_id=None, source="dream", dream_run_id=...)`: a `replace` becomes a suggestion with `content` and `replaces_memory_id`, an `archive` an archive-only suggestion (no `content`). A merge of two memories is therefore one replace carrying the merged text plus one archive, reviewed as two rows. One `memory_suggestions_changed` event (`conversation_id` None) follows the batch.
6. Closes the row `completed` with the counts, the provider-reported token usage and its list-price estimate (`db/llm_pricing.estimate_cost_usd`), or `failed` with the error -- a provider error, an unparseable answer, no model. A dream cancelled by shutdown is marked failed on the way out.

Dreams are not recorded in the `llm_calls_*` tables: those rows require a conversation, and a dream has none. The run row is the cost record; the admin section sums nothing yet and shows the per-run figure.

## Settings and Gate

`FEATURE_DREAMING` is an off-by-default gate with per-user access ([Feature Gates](feature-gates.md)). Closed for a user = the daemon skips them and `POST /admin/dreaming/run` answers 403 `feature_disabled`. The admin routes stay usable while the gate is closed so the model can be chosen first.

`GET /app/api/admin/dreaming` returns the settings, what the next dream would run on (`resolved`, or `unavailable_reason`), the gate state (`enabled`, `enabled_for_me`), every enabled non-deprecated model with `dreamable` and its `unavailable_reason` (`not_allowed_private`, `not_configured`, `failing`), and the `recent_runs` with each user's email. `PUT` replaces the settings: 400 `unknown_model` for an id the registry cannot resolve or a deprecated one, 400 `invalid_params` for a model not allowed privately or a thinking level the pinned model does not list; an unavailable-but-allowed pin is accepted and falls back at run time. `POST .../run` dreams for the requesting admin now and returns `{"status": "skipped", "reason"}` or `{"status": "completed" | "failed", "run"}`.

The Settings > Dreaming section renders those three cards: the model and thinking pickers (automatic shows the model it currently resolves to), a "Review my memories now" button, and the recent-runs table.

## Design Decisions

- **Suggestions, never writes.** The literature on agent "dreaming" converges on copy-on-write with a review step; Quest's suggestion inbox already is that step, so a dream writes nothing a user has not accepted and the review UI, the inbox badge and the Slack reminders carry over unchanged.
- **The store, not the transcripts.** Reading the memory list keeps a dream to a few thousand tokens, keeps it free of anything the user did not already choose to save, and helps the users who need it most (a store past the prompt's full-text tier shows its older memories only as index lines). Replaying transcripts for new facts is a later step with its own cost and consent questions.
- **Two row shapes, no new table.** A dream's proposals reuse `memory_suggestions` with a `source`; only archive-only rows are new. One inbox lists everything, and accept / dismiss are the same routes.
- **Admin-chosen model with a fallback chain.** Nobody is in the loop when a dream runs and the deployment pays, so the pick is the admin's; it is a preference with a fallback so a pinned model being down never skips the night.
- **Pacing by the store, not the clock alone.** A dream only runs when memories were added since the last one and the last batch was reviewed, so a quiet user costs nothing and a busy inbox is not buried.
- **One at a time.** Serialising dreams keeps a sweep from contending with live conversations for the shared provider quota; a few hundred users still finish within the night.
