# Admin System Reports

Admin-only operator dashboard at `/admin/system-reports` (titled "System Reports"; the pre-rename `/admin/system-monitor` URL redirects, and the API prefix keeps the historical `system-monitor` name). A left-hand nav switches between report sections; ships with "Latest Conversations", "Total Usage", "Top Conversations", "Users", "Models", "Docs", "Tables", "Memories", "Skills", "Tool Calls", and "Guides".

## Overview

System Reports gives admins a global view of activity and spend across all users. Unlike [Admin Impersonation](admin-impersonation.md), nothing here switches identity -- it is read-only observation. The page is a thin frame: a header, a left-hand section nav (`SECTIONS` in `AdminSystemReportsPage.tsx` -- adding a report is one entry plus its component), and the selected section. Each section owns its own data fetching, error state, and styling. Section choice is local component state, not a URL segment.

Admin gating is enforced on the backend via the inline `is_admin(user["email"])` check used by every other endpoint in `chat/routes/admin.py` (returns 403 for non-admins, including admins who are currently impersonating another user). The frontend menu-item hide and page-level guard are UX niceties, not security boundaries.

## Key Files

**Backend:**
- `chat/routes/admin.py` -- `admin_latest_active_conversations`, `admin_usage_report`, `admin_most_expensive_conversations`, `admin_user_report`, `admin_model_report`, `admin_docs_report`, `admin_tables_report`, `admin_memories_report`, `admin_skills_report`, `admin_tool_calls_report`, and `admin_guides_report` handlers (admin check, limit clamping, date-range parsing via the shared `_resolve_range_window`, title resolution) sharing the `_admin_conversation_view` row assembler
- `chat/usage_report.py` -- `build_usage_report()`, the pure fold of per-day usage buckets into the Total Usage response (rolling windows with their previous periods, lifetime total, daily / weekly / monthly series; metric definitions in the module docstring)
- `chat/feature_usage_report.py` -- `build_docs_report()`, `build_tables_report()` and `build_memories_report()`, the pure per-user folds behind the Docs, Tables and Memories sections (row shapes and the range semantics in the module docstring)
- `db/conversation_store.py` -- `list_latest_active_conversations()` (joins `Conversation` to `User`, orders by `last_message_at DESC`) and the batch companion `get_conversations_with_users()`, sharing `_admin_conversation_row`; plus `list_conversation_activity_rows()`, the narrow all-conversation owner/routine/activity projection behind the user and Total Usage reports
- `db/llm_call_store.py` -- `_collect_usage_buckets()` grouped `GROUP BY (conversation_id, model, long_context_flag)` aggregation over the per-provider `llm_calls_gemini` / `llm_calls_anthropic` raw tables (one query per table, optional conversation-id and `created_at`-window filters, optional `by_call_type` split adding the rows' `call_type` to the key, optional `by_day` split adding the UTC calendar day), behind `get_usage_by_model_for_conversations()` (batch), `get_most_expensive_conversations()` (date-range top-N ranking), `get_usage_by_user()` (date-range per-user fold with the routine cost split), `get_usage_by_model()` (date-range per-model fold with audience counts, routine + sub-agent cost shares and top users), and `get_daily_usage_buckets()` (the per-day (conversation, model) buckets behind Total Usage); plus `get_latest_context_tokens_for_conversations()` (batched latest top-level call context size)
- `db/user_store.py` -- `list_user_signup_dates()` (user id -> `created_at`, the new-user counts of Total Usage)
- `db/llm_pricing.py` -- per-model USD list-price tables and `estimate_cost_usd()` (cache read/write rates incl. the Anthropic 5m/1h TTL split; long-context tiers: Gemini Pro >200K, Claude Haiku 5.5 >100K via a per-entry `long_context_threshold`; date-ranged rate sets, so a call is priced at the rate in force on its `created_at` -- the convention for recording a price change is in the module docstring), consulted only for calls without a provider-reported amount
- `db/doc_store.py` / `db/table_store.py` -- `list_all_doc_rows()` / `list_all_table_rows()`, the narrow all-rows metadata projections (owner, project, mode, cached sizes, last write source, share count, timestamps; never a title or body) behind the Docs and Tables sections
- `db/memory_store.py`, `db/memory_suggestion_store.py`, `db/project_memory_store.py`, `db/project_memory_suggestion_store.py` -- `list_all_memory_rows()`, `list_all_suggestion_rows()` (both stores) and `list_all_project_memory_rows()` (joined to the project owner), the text-free projections behind the Memories section
- `analytics/reports.py` -- `query_skill_usage()` / `query_tool_calls()` (the grouped `skill_loads` / `tool_calls` queries, run on an [Analytics Store](analytics.md) cursor in a worker thread) and the pure `build_skills_report()` / `build_tool_calls_report()` folds behind the Skills and Tool Calls sections; `db/skill_store.py` -- `list_skill_labels()`, the any-owner name / kind / creator lookup for the skill ids a report names (never the content)
- `db/guide_store.py` / `db/project_store.py` -- `list_all_guides()` (every user guide joined to its owner plus a per-guide routine reference count) and `list_all_project_guides()` (projects with non-empty `projects.guide` instructions) behind the guides report
- `chat/storage.py` -- `ChatStorage.count_user_message_active_days()` (distinct UTC days with user messages, from chat_history.json) and the range-clipped day-set variant `user_message_active_days()`

**Frontend:**
- `frontend/src/pages/AdminSystemReportsPage.tsx` / `.css` -- Shell page (admin guard, header, left nav, section container, `AdminOpsMenu` overlay)
- `frontend/src/components/LatestActiveConversationsTable.tsx` / `.css` -- "Latest Conversations" polling section
- `frontend/src/components/TotalUsageReport.tsx` / `.css` -- "Total Usage" section (period-comparison matrix + trend charts, fetch-on-demand), with `UsageBarChart.tsx` (the dependency-free SVG bar chart) and the pure helpers in `frontend/src/utils/usageReport.ts` (period labels, period-over-period deltas, axis math)
- `frontend/src/components/TopConversationsTable.tsx` / `.css` -- "Top Conversations" section (date-range selector + ranked table, fetch-on-demand)
- `frontend/src/components/UsersReportTable.tsx` / `.css` -- "Users" section (date-range selector + per-user activity/cost table, fetch-on-demand)
- `frontend/src/components/ModelsReportTable.tsx` / `.css` -- "Models" section (date-range selector + per-model cost/audience table, fetch-on-demand)
- `frontend/src/components/FeatureUsageReport.tsx` / `.css` -- the shared frame (header with summary, date range and refresh; loading / empty / error states), the `useRangedAdminReport` fetch hook and the cell helpers of the three feature-usage sections
- `frontend/src/components/DocsReportTable.tsx`, `TablesReportTable.tsx`, `MemoriesReportTable.tsx` -- the "Docs", "Tables" and "Memories" sections (per-user tables on the shared frame)
- `frontend/src/components/SkillsReportTable.tsx`, `ToolCallsReportTable.tsx` -- the "Skills" and "Tool Calls" sections (per-skill and per-tool tables on the shared frame)
- `frontend/src/components/GuidesReportTable.tsx` / `.css` -- "Guides" section (deprecation tracker: every user guide and project guide with owner, fetch-on-demand, client-side "Hide empty" toggle)
- `frontend/src/components/ReportDateRange.tsx` / `.css` -- shared date-range picker (presets + custom start/end, `resolveRange` date math) used by Top Conversations, Users and Models
- `frontend/src/components/ConversationUsageCell.tsx` / `.css` -- shared per-model token-usage breakdown cell (native-field row format + exact-numbers tooltip), used by both tables
- `frontend/src/components/AdminOpsMenu.tsx` -- "System Reports" menu item (admin-only, hidden during impersonation; sibling to "Impersonate user" and the shutdown button)
- `frontend/src/App.tsx` -- `AdminSystemReportsRoute` at `/admin/system-reports`, plus a legacy `/admin/system-monitor` redirect
- `quest.py` -- `serve_spa_admin` SPA catch-all for `/admin/{rest:path}` so direct browser loads serve `index.html` instead of 404 (safe because all admin API routes live under `/app/api/admin/...`; see [Frontend -- Backend SPA Support](frontend.md#backend-spa-support))
- `frontend/src/api/client.ts` -- `fetchLatestActiveConversations()`, `fetchAdminUsageReport()`, `fetchMostExpensiveConversations()`, `fetchAdminUserReport()`, `fetchAdminModelReport()`, `fetchAdminDocsReport()`, `fetchAdminTablesReport()`, `fetchAdminMemoriesReport()`, `fetchAdminSkillsReport()`, `fetchAdminToolCallsReport()`, `fetchAdminGuidesReport()`
- `frontend/src/api/config.ts` -- `adminLatestActiveConversations`, `adminUsageReport`, `adminMostExpensiveConversations`, `adminUserReport`, `adminModelReport`, `adminDocsReport`, `adminTablesReport`, `adminMemoriesReport`, `adminSkillsReport`, `adminToolCallsReport`, `adminGuidesReport` URL builders
- `frontend/src/api/types.ts` -- `AdminActiveConversation`, `AdminConversationModelUsage`, `AdminConversationUsageTotal`, `AdminUsageReport` / `AdminUsageWindow` / `AdminUsageBucket`, `AdminUserReportRow`, `AdminModelReportRow`, `AdminDocsReportRow`, `AdminTablesReportRow`, `AdminMemoriesReportRow`, `AdminSkillsReportRow`, `AdminToolCallsReportRow`, `AdminGuideReportRow`, response types
- `frontend/src/utils/formatters.ts` -- `formatRelativeTimestamp` shared by the tables' last-active columns, `formatCompactNumber` ("12.3K" / "4M") for the Total Usage cells and chart axes, `formatBytes` for the Docs section's size column

See [Admin System Reports API](../api/admin-system-monitor-api.md) for the endpoint contracts.

## Shared Row Columns

The two conversation-level sections render the same conversation row shape (assembled by `_admin_conversation_view` in `chat/routes/admin.py`):

- **Token usage** -- per-model provider-native breakdown plus a bold coarse total ("N tokens · M calls · ~$X"); see below.
- **Context** -- `latest_context_tokens`: the input-side token count of the conversation's most recent TOP_LEVEL call in either provider table (Gemini: `prompt_token_count`; Anthropic: input + cache_read + cache_creation) -- what the next turn would re-read. Fetched batched by `get_latest_context_tokens_for_conversations()` (one grouped `MAX(id)` subquery per provider table; the later call wins when a conversation switched providers). Same formula as the single-conversation `get_latest_context_tokens()` used by the expensive-resume warning.
- **Active days** -- `active_days`: distinct UTC days with at least one user message over the whole conversation lifetime (not clipped to any query window), computed by `ChatStorage.count_user_message_active_days()` from message timestamps in chat_history.json (one file read per displayed row; fine at ≤30 rows).

**Per-model token usage and cost.** Token consumption is broken down per model used by the conversation (including sub-agent models) in the shared `ConversationUsageCell` component. The route handlers attach `usage_by_model` (provider-discriminated per-model line items with native-field `metrics`, `estimated_cost_usd` and `cost_source`, heaviest-first) and `usage_total` (a coarse `{call_count, total_tokens, estimated_cost_usd, cost_source}` magnitude).

Costs come from two sources, and every figure says which (`cost_source`): OpenRouter calls carry the USD amount OpenRouter reported charging for the request (`llm_calls_openrouter.cost`, plus `upstream_inference_cost` on bring-your-own-key rows, captured because the provider opts in with `usage: {"include": true}`), which is used as-is -- so OpenRouter's time-of-day pricing, BYOK fees and catalog drift since the model's pricing snapshot are all reflected; every other call (Vertex-served Gemini and Claude report token counts only, Fireworks / NEAR AI / self-hosted instances report no per-request cost, and OpenRouter rows recorded before capture existed have NULL cost) is a list-price estimate computed server-side from `db/llm_pricing.py`. The grouped query groups on a per-call "has reported cost" flag next to the long-context flag (context > 200K tokens, so tier-priced models like Gemini Pro's >200K rates are costed against each call's own context size) and a pricing-period column (the latest rate change at or before the call's `created_at`, from `pricing_period_starts()` -- dated table entries plus retired instance pricing snapshots -- so a price change never reprices earlier calls); reported buckets sum the reported amounts, estimated buckets are priced at their period's rates, and the buckets merge back into one displayed entry per model whose `cost_source` is `"reported"`, `"estimated"` or `"mixed"`. A model with neither reported amounts nor a pricing entry shows no per-model dollar figure and nulls out the conversation total (a partial sum would read as the full cost).

The cell renders each line item in a provider-specific format (`formatModelUsageRow`): Gemini rows as `prompt P (cached C) · out O (+T thoughts)`, Anthropic rows as `in I · cache-r R · cache-w W · out O`, each suffixed with `· $X` when priced (green `tokens-cost` span; `<$0.01` below a cent; the `~` prefix marks a figure that includes an estimate -- `formatCost` in `ConversationUsageCell.tsx` drops it only for `"reported"`); the row tooltip (`formatModelUsageTooltip`) shows the full native field names and values plus the call count and a 4-decimal cost line naming its source (`describeCostSource`), including the Anthropic 5m/1h cache-write TTL split (tooltip-only). See [Database -- llm_call_store](database.md#data-access-layer) for the query.

**Title resolution.** The route handlers call `ChatStorage._resolve_list_title` with the row dict, so titles use the same precedence (`custom_name`, then `auto_title`, then a generated default) as the sidebar.

## Latest Conversations Section

Renders the global top-N most-recently-active conversations across all users. Each row shows the resolved title, owner (display name + email), the shared columns above, the conversation's last-used model, and a relative-time last-active label.

**Routine badge and filter.** Conversations created by a routine (`Conversation.routine_id` set -- covers both scheduled runs and one-click manual runs; the two are not distinguishable at the conversation level) get a ⏰ prefix on the title cell. An "Include Routines" checkbox in the section header (on by default) controls whether routine conversations appear: turning it off re-fetches with `include_routines=false` so the server filters them out in SQL and the list stays full-length, while a client-side filter pass on the rows already in hand makes the toggle take effect instantly before the re-fetch lands.

**Activity definition.** "Active" is keyed off `Conversation.last_message_at`, the canonical most-recent-activity column already maintained by the conversation store. The store layer bumps this column on user input, model output, and tool-call completion -- exactly the signals an operator dashboard cares about. The query orders by this column descending; see `list_latest_active_conversations()` in `db/conversation_store.py`.

**Polling cadence.** The section polls every 60 seconds with three guards (see `LatestActiveConversationsTable.tsx`):
- Visibility pause: interval-driven polls skip the request when `document.hidden` is true; a `visibilitychange` listener triggers an immediate fetch when the tab becomes visible again.
- In-flight dedupe: a ref guards against overlapping requests if the network is slow (applies to both interval and manual fetches).
- Cancellation: unmount cancels pending state updates.

**Freshness indicator and manual refresh.** The header shows a live seconds-granularity elapsed label ("Updated 5s ago" / "2m ago" / "1h ago") driven by a 1s ticker (`formatElapsed`, distinct from the minute-granularity `formatRelativeTimestamp` used for the last-active column). The anchor is set only on a *successful* fetch, so during failed polls the label keeps growing while the error strip shows what went wrong. A manual refresh button (lucide-react `RefreshCw`) re-fetches immediately and resets the timer; it bypasses the hidden-tab guard (the click implies a visible tab) and is disabled with a spinning icon while a fetch is in flight.

A future iteration may replace polling with an admin-scoped per-user global on the [persistent WebSocket](realtime.md); for v1 polling is fine because the section is admin-only and the row count is small.

## Total Usage Section

The instance-wide overview: how much Quest is being used, by how many people, at what cost, and whether that is going up or down. One parameter-less fetch of `GET /admin/system-monitor/usage-report` (on mount and via the refresh button; no date picker -- the periods are fixed) renders two things.

**Comparison matrix.** One row per rolling window -- Today, Last 7 / 30 / 90 days, Last 12 months (each with its UTC date range under the label), plus an "All time" reference row -- and one column per metric: **Conversations** (distinct conversations with at least one model call in the period, routine runs included; the routine-run count rides in a detail line), **Active users** (distinct users with a call in a *non-routine* conversation), **New users** (accounts created in the period), **Calls**, **Tokens** (compact "1.07M" magnitudes, exact figure in the tooltip) and **Cost**. Every window cell carries the change against the preceding period of the same length ("▲ +331 (+1034%)", "▼ -$0.13 (-11%)", "no change", "+4 (from 0)" when the previous period was empty; the tooltip names the previous period's dates and figure). Deltas are coloured by whether the direction is welcome -- growth in activity is green and decline amber, while for cost it is the other way round (`good` on each `METRIC_COLUMNS` entry in `TotalUsageReport.tsx`). The lifetime row has no previous period, so no deltas.

Costs follow the dashboard-wide provenance rules (`~` marks a figure that includes list-price estimates, dropped only for fully provider-reported spend) with one deliberate departure from the null-on-unpriced convention: a window containing calls on a model without a pricing entry shows its priceable portion as a lower bound (`≥ ~$6.03`, toned down, tooltip explains) instead of `n/a`, because a single unpriced model would otherwise blank the lifetime figure for good; the delta of such a cell stays `n/a`.

**Trend charts.** Four bar charts -- Conversations, Active users, Cost, Tokens -- over one of three gap-free series picked by a granularity dropdown: daily for the last 90 days, weekly (Monday-start ISO weeks) for the last 52 weeks, or monthly for the last 24 months, each ending with the in-progress period that contains today. `UsageBarChart` is a dependency-free SVG: one bar per period on a "nice" axis ceiling (even ceilings for whole-number metrics so the mid gridline is an integer -- `niceCeil` / `niceCeilEven` in `utils/usageReport.ts`), sparse x-axis ticks, and a hover readout in the chart header (the hovered period and its figure, defaulting to the latest period) rather than a positioned tooltip; each bar also carries a native `<title>`. The in-progress last bar is drawn lighter and labelled "in progress"; on the Cost chart, a period that also contains unpriced calls plots its priceable portion as a hatched bar labelled "excludes unpriced calls" (the series points carry both the null-on-unpriced `cost_usd` and the never-null `known_cost_usd`).

Distinct counts are computed per period from per-day (conversation, model) buckets (`get_daily_usage_buckets()` folded by `build_usage_report()` in `chat/usage_report.py`), so a week's conversations or users are not the sum of its days. See [Admin System Reports API](../api/admin-system-monitor-api.md) for the exact response contract.

## Top Conversations Section

Renders the top-30 most-expensive conversations for a selected date range, ranked most-expensive-first with a rank column and a prominent green "Est. cost" column. It adds a **Compactions** column: how often the history was compacted (`compaction_count()` in `chat/compaction.py`; 0 once deleted).

**Date range selector.** The shared `ReportDateRange` component (also used by the Users and Models sections): a preset dropdown (Today, Last 7 / 30 / 90 days, Last 12 months, All time -- see `RANGE_PRESETS` in `ReportDateRange.tsx`) plus a "Custom range" option that swaps in two date inputs (either side may be left empty for an open-ended bound). Presets resolve to UTC dates ("Last N days" includes today, so the window starts N-1 days back; "Today" is the current UTC date alone) because the `llm_calls_*` rows are stamped in UTC; the backend interprets the inclusive `start`/`end` dates as UTC midnights.

**Ranking.** Server-side in `get_most_expensive_conversations()`: the same tier-bucketed grouped queries as the batch aggregation, filtered to the `created_at` window instead of a conversation-id list, ranked by each conversation's *known* cost (the sum of per-model figures that could be priced -- reported or estimated) so a conversation mixing priced and unpriced models still ranks by what can be priced -- while the displayed total keeps the null-when-partial convention (the FE shows "n/a" with a tooltip pointing at the per-model tooltips).

Ties break on total tokens. Only calls inside the window count toward the ranking and the displayed sums; the Context, Active days and Compactions columns are whole-conversation values.

**Deleted conversations.** The `llm_calls_*` tables are append-only and outlive conversation deletion, so a deleted conversation can still rank. It renders with a "(deleted conversation)" title, no last-active timestamp, and its owner resolved from the call rows' `user_id` (one `users` lookup per distinct missing owner).

**Fetching.** No polling -- this is an on-demand report. The section fetches on mount, on every range change, and via a manual refresh button; a monotonic fetch counter discards stale responses so a slow wide-range query can never overwrite a newer selection.

## Users Section

Renders one row per user for the selected date range (same `ReportDateRange` picker and fetch-on-demand pattern as Top Conversations, incl. the stale-response guard), sorted by known in-range cost. Columns per row (`AdminUserReportRow`):

- **Active days** -- distinct UTC days with at least one user message across the user's *non-routine* conversations, clipped to the range: the server unions per-conversation day sets (`ChatStorage.user_message_active_days()`) so one day spent in several chats counts once. Unlike the conversation tables' lifetime `active_days`, this one IS range-clipped.
- **Convos** -- distinct non-routine conversations with at least one recorded call in the range (the routine count rides in the cell tooltip).
- **Cost / Routine cost** -- the in-range spend split on the conversation rows' `routine_id`; each split independently follows the null-when-any-model-unpriced convention, so an unpriced model inside a routine never hides organic spend (and vice versa), and carries its own `cost_source` (`cost_excluding_routines_source` / `cost_routines_source`) so the cell drops the `~` only when the whole split is provider-reported. A zero split renders as an em dash, an unpriced split as "n/a" with a tooltip.
- **By routine** -- the routine split itemized per routine so the most expensive routines stand out: one row per routine with at least one call in the range (`routine_costs`), showing the routine name, its project name (routine names are only unique per project) and the routine's cost with its own `cost_source`, ranked by the priceable portion (an unpriced routine shows "n/a" but still sorts by what CAN be priced); the row tooltip carries the run count and the 4-decimal figure.

  The `RoutineCostsCell` collapses past the top five rows behind a "+N more" toggle (`ROUTINE_ROWS_COLLAPSED` in `UsersReportTable.tsx`). Names come from one batched `get_routine_labels()` lookup in `db/routine_store.py`; a routine deleted mid-request shows as "(deleted routine)" (routine deletes null the conversations' `routine_id`, so steadily-deleted routines simply fold into the non-routine split).
- **Token usage** -- the shared `ConversationUsageCell`, fed per-user aggregates: one merged entry per model across ALL the user's conversations in range, routines included (`get_usage_by_user()` in `db/llm_call_store.py` folds the same tier-bucketed (conversation, model) buckets by the call rows' `user_id`, so the tier pricing stays exact).

The roster comes from `list_all_users()` -- zero-activity users keep their row so the report doubles as a "who is not using Quest" view. Calls whose conversation row was deleted count as non-routine (routine provenance dies with the row) and contribute no active days (the chat history file is gone); calls whose *user* was deleted keep their spend on a placeholder "(unknown user)" row. See [Admin System Reports API](../api/admin-system-monitor-api.md) for the exact row contract.

## Models Section

Renders one row per model id with at least one recorded call in the selected date range (same `ReportDateRange` picker and fetch-on-demand pattern as Top Conversations and Users, incl. the stale-response guard), sorted by known in-range cost, so an admin can see which models carry the spend and who drives it. Columns per row (`AdminModelReportRow`):

- **Model** -- the catalog display name (`getModelDisplayName()`, falling back to the raw id) over the raw model id; the row tooltip adds the provider.
- **Cost** -- the model's total in-range spend across every user, conversation and call type (`usage_total.estimated_cost_usd`); `null`/"n/a" only when the model has no pricing entry and reported no amounts, with the usual `cost_source` provenance so the `~` drops only for fully provider-reported figures.
- **Users** -- distinct users (per the call rows' `user_id`, so deleted users and deleted conversations still count) with at least one call to the model in range; the tooltip adds the distinct conversation count.
- **Top users** -- the model's ten most expensive users (`top_users`), ranked by the priceable portion of their spend on this model (ties: call count), each showing name (or email) and cost with its own `cost_source`; the tooltip carries email, call count and the 4-decimal figure. `TopUsersCell` collapses past the top five behind a "+N more" toggle (`TOP_USER_ROWS_COLLAPSED` in `ModelsReportTable.tsx`). Identity comes from one `list_all_users()` roster lookup; a deleted user shows as "(unknown user)".
- **Routine cost** -- the share of the model's spend accrued in routine-created conversations, split on the surviving conversation rows' `routine_id` exactly like the Users report (calls of deleted conversations count as non-routine). Zero renders as an em dash.
- **Subagent cost** -- the share accrued by sub-agent calls, from the call rows' own `call_type` (`ApiCallType.SUB_AGENT`, i.e. `agent_task` / nested sub-agents), attributed to the model the sub-agent actually ran on: an Opus sub-agent spawned from a Gemini conversation lands on the Opus row. Cross-user subagent conversations (`origin="user_subagent"`) are ordinary top-level calls here. The two shares are independent (a sub-agent inside a routine run counts in both), so they need not sum to the total.
- **Token usage** -- the shared `ConversationUsageCell` fed a single-entry `usage_by_model` list (this model, merged across conversations and call types) so the provider-native token fields and exact-numbers tooltip render like everywhere else.

Aggregation is `get_usage_by_model()` in `db/llm_call_store.py`: `_collect_usage_buckets(by_call_type=True)` widens the tier-bucketed grouped query with the rows' `call_type`, and the fold merges the (conversation, model, call_type) buckets per model while tracking distinct users/conversations, the two cost shares and per-user spend for the top-N list. See [Admin System Reports API](../api/admin-system-monitor-api.md) for the exact row contract.

## Docs, Tables and Memories Sections

Three feature-adoption reports answering "who uses this, and how much": one row per user who has anything of the feature, for a selected date range (the same `ReportDateRange` picker and fetch-on-demand pattern as Users, incl. the stale-response guard). The three sections share one frame, fetch hook and cell helpers (`FeatureUsageReport.tsx`), so they read as one report in three shapes. Each table mixes **lifetime** columns (what the user owns now) with **range-clipped** columns (green: what happened inside the selected range), and the header summary gives the instance totals and the number of users with rows out of the whole roster (`total_users`). Only metadata rows are read: no doc body or title, table row, schema or memory text ever reaches the dashboard. Row contracts are in [Admin System Reports API](../api/admin-system-monitor-api.md); the folds are the pure builders in `chat/feature_usage_report.py`, run in a worker thread like the other report handlers.

**Docs** (`AdminDocsReportRow`): **Docs** owned now with the user / project split in the sub-line, **Public** (docs of public projects), **Shared** (docs with at least one share grant, the "everyone" row included), **By model** (docs whose latest body write came from a model: `last_write_source` of the `conversation:` or `action_request:` forms, as opposed to `ui`), **Size** (sum of the cached `content_size`), **Images** (sum of `asset_count`), then the range columns **Created** and **Updated** and the lifetime **Last activity** (newest `updated_at`). Sorted by docs updated in range, then docs owned, then email.

**Tables** (`AdminTablesReportRow`): the same layout with **Rows** (sum of the cached `row_count`) and **Columns** (sum of the schema lengths) in place of size and images; a History restore (`revert:` source) counts as a human write. No `table.db` is opened.

**Memories** (`AdminMemoriesReportRow`): **Memories** (active personal memories, archived ones in the sub-line), **Chars** (characters stored in the active ones), **Created** in range, the **Suggestions** funnel (suggestions the model raised in the user's conversations in range, split by their *current* status into accepted / dismissed / pending, with the acceptance rate in the tooltip), **Project memories** (active memories across the projects the user owns, with the number of those projects and the archived and created-in-range counts in the sub-line), the **Project suggestions** funnel (raised in the user's conversations, so attributed to the suggester, not the project owner) and **Last activity** (newest memory or suggestion timestamp of any kind). Sorted by in-range activity (memories created plus suggestions raised, personal and project), then active memories, then email.

**"Updated in range" counts items, not writes.** `updated_at` is one timestamp per doc or table, bumped by any change (body, rename, share), so a doc written ten times in the range counts once, and a doc renamed in the range counts as updated. True write counts would need the per-doc revision snapshots (pruned by age and count) or every table's change log; see [Design Decisions](#design-decisions).

## Skills Section

"Which skills are actually used, by whom, through which path", answered from the `skill_loads` ledger of the [Analytics Store](analytics.md#skill_loads) rather than by scanning transcripts: one row per skill with at least one load in the selected range, on the shared feature-usage frame. The ledger query (`query_skill_usage()`) groups per skill, conversation and user; the fold (`build_skills_report()`, both in `analytics/reports.py`) joins what `quest.db` knows -- the skill's name, kind and creator from `list_skill_labels()` (any owner; an id with no row and no system-catalog entry is reported as **deleted**, keeping its history) and which conversations a routine created -- and sorts by conversations, then loads, then name.

Columns (`AdminSkillsReportRow`): **Skill** (kind badge, name, creator), **Conversations** (distinct conversations with a load in range), **Routine convos** (of those, routine-created: by the `conversations` row, or by the `routine_id` the ledger row carried, so a deleted routine conversation keeps its attribution), **Users** (distinct), **Loads** (every load event in range, split in the sub-line into auto-loads, composer loads, model calls and rows the `skill_reads.json` backfill imported) and **Last used**. Loads are events, not conversations: an auto-loaded skill counts once per turn, so the load figure is a weight of how often the body was in context, while the conversation figure is the adoption count. The header summary carries the backfill status; until the import has run, history before the ledger existed is simply absent.

## Tool Calls Section

"Which tools does the model call, how often, by whom", answered from the `tool_calls` ledger of the [Analytics Store](analytics.md#tool_calls): one row per effective tool called in the selected range (a `tool_call` meta-tool call counts under the tool it routed to), on the shared frame. `query_tool_calls()` groups per tool, conversation and user; `build_tool_calls_report()` attributes routine calls like the Skills fold and sorts by calls, then conversations, then name.

Columns (`AdminToolCallsReportRow`): **Tool** (name and share of all calls), **Calls** (every call in range, whoever issued it; the sub-line splits out sub-agent, sandbox-script, `tool_call`-routed and backfill-imported calls), **Routine calls**, **Conversations** and **Users** (distinct; a script call without a launching conversation counts for its user only), **Last call**. Arguments and outputs are never recorded. The header summary carries the backfill status.

## Guides Section

A deprecation tracker for the retired [guides](guides.md) feature: guide creation is already disabled, and this section lists who still has guides so the remaining users can be contacted before the feature is removed entirely. It renders one row per guide in the system, sorted by owner email (fetch-on-demand: on mount and via the refresh button; no date range -- guides are a current-state inventory, not an activity stream). Two kinds of rows share the table (`AdminGuideReportRow`, `kind` discriminated):

- **User guide** -- a `guides` table row (yellow badge), including the empty auto-created default guides (tagged "default"), so an admin can tell "has a default row" from "wrote instructions". The **Routines** column counts routines still referencing the guide as an override -- the only remaining code path that applies a guide to new conversations -- so a non-zero count marks a guide that is still live, not just stored.
- **Project** -- a project with non-empty Project Instructions (the `projects.guide` text field, blue badge, "public" tag on public projects). Projects with empty instructions are omitted server-side. These are NOT deprecated (Project Instructions outlived the guides feature), but the operator asked to see every guide-shaped prompt in one inventory.

Other columns: **Chars** is the content length (an em dash for empty content -- the text itself is never shipped to the report), **Updated** the row's `updated_at` falling back to `created_at`. The header carries a live count summary (user / project rows shown, distinct owners) and a client-side **Hide empty** checkbox that drops zero-length guides so the real stragglers stand out; the summary reports how many rows the toggle hid.

## Last-Model Caveat

`last_model` is sourced from `Conversation.model`, which is set on first turn and overwritten only by the explicit `PATCH /conversations/{id}` model change. Per-message model is not persisted anywhere. In practice this equals the last used model unless a user just switched models without sending again -- in that case `last_model` reflects the *next* model they intend to use, not the model that produced the most recent output. Documented at the `last_model` field in `db/conversation_store.py:list_latest_active_conversations()`.

## Design Decisions

**Why a shell page with a section nav?**
The page is explicitly designed to grow. Each section owns its own data fetching so new reports can be added by appending to `SECTIONS` in `AdminSystemReportsPage.tsx` without touching unrelated code.

**Why does the API prefix still say `system-monitor`?**
The UI was renamed to "System Reports"; the REST prefix (`/app/api/admin/system-monitor/...`) kept its original name to avoid churning working endpoints. Only the browser-facing route moved (`/admin/system-reports`, with a redirect from the old path).

**Why poll the latest-conversations endpoint instead of pushing via the persistent WebSocket?**
The realtime infrastructure is per-user-scoped; broadcasting global activity to admins would require a new fan-out channel. For an internal dashboard with a small admin audience, 60s polling plus an on-demand manual refresh is cheap and avoids new protocol surface.

**Why clamp `limit` server-side?**
Both handlers cap `limit` at 100 so a crafted query string cannot fan out into a giant scan. The FE only ever requests the defaults (20 latest / 30 most expensive).

**Why do the Skills and Tool Calls sections read ledgers instead of the transcripts?**
A transcript scan (what PR #170 built) re-reads every `chat_history.json` overlapping the range on each refresh, cannot see auto-loads at all (they leave no message), misses sandbox-script calls (never persisted) and gets slower with every conversation. A ledger row is written once at the moment of the event, knows its source, and aggregates in one grouped query -- see [Analytics Store](analytics.md) for why the ledgers are not in `quest.db`.

**Why rank by known cost instead of skipping unpriced conversations?**
Hiding spend because one model lacks a pricing entry would make the report silently lossy; ranking by the priceable portion keeps every conversation visible while the null total flags the estimate as incomplete.

**Why prefer the provider-reported amount over the pricing table?**
OpenRouter's catalog prices drift after the model's pricing snapshot is taken, several models carry weekday peak-hour multipliers the snapshot does not model, and cache-hit discounts on cheap models (DeepSeek's ~3% cache-read rate) make estimates sensitive to fields the estimate can only approximate. The amount OpenRouter charged is the ground truth, so a call that has it is never re-estimated; the `cost_source` flag keeps the two kinds of figure distinguishable instead of silently mixing them. Vertex-served Gemini and Claude return token counts only (billing lives in Cloud Billing), so those stay estimates.

**Why does the user report scan chat_history.json files for active days?**
User-message days are not derivable from the `llm_calls_*` tables (a call's `created_at` is a model-turn signal, not a user-message signal, and resumed turns blur it further). The report is fetch-on-demand and admin-only, and the scan prunes by each conversation row's `created_at`/`last_message_at` bounds before touching a file, so narrow ranges read few files.

**Why do the report handlers build their rows in `asyncio.to_thread`?**
Every chat_history.json read (active days on all three conversation/user reports, the legacy title fallback in `_resolve_list_title`) is a synchronous parse of a file that can run to many megabytes for a long tool-call conversation, and the user report reads one per non-routine conversation in range. Done on the event loop, that would freeze the persistent WebSocket for the duration: no heartbeats, no streaming events for every live run on the instance, and a stall past the socket's 60s deadlines closes the connection and drops the run's transient lifecycle envelopes (see [Realtime -- Run-State Reconcile on Subscribe](realtime.md#run-state-reconcile-on-subscribe)). The row-building loops therefore run in a worker thread; the async DB lookups they depend on are resolved on the loop first.

**Why do Total Usage's active users exclude routine runs?**
A scheduled routine running overnight on someone's behalf produces call rows under their user id, but it is not that person using Quest. Counting it would make the active-user figure read as engagement it is not; the routine runs still count as conversations (with their own sub-count) so the activity is not hidden, just attributed honestly. Deleted conversations lose their routine link, so their calls count as organic -- the same convention as the Users report.

**Why does Total Usage show a cost lower bound instead of `n/a`?**
The other sections null out a figure as soon as one model in it is unpriced, which is right for a per-conversation or per-user figure the admin can drill into. The Total Usage matrix aggregates everything, so one unpriced model used once would blank the lifetime cost permanently and every window it falls in. Showing the priceable portion marked `≥` keeps the information while still flagging it as incomplete; the chart does the same with hatched bars.

**Why hand-rolled SVG charts instead of a chart library?**
Four bar charts with gridlines, ticks and a hover readout are ~150 lines of SVG; a charting dependency would add hundreds of kilobytes to the bundle for an admin-only page, and the frontend already prefers dependency-free utilities (the CSV parser, the markdown renderers) where the need is this bounded.

**Why do the Docs / Tables / Memories reports read only the metadata rows?**
A per-user adoption view needs counts, sizes and timestamps, all of which the `docs` / `data_tables` / `memories` rows (and the suggestion rows) already carry. Counting actual writes would mean listing every doc's `revisions/` directory (snapshots are pruned past `DOC_REVISION_RETENTION_DAYS` and `DOC_REVISION_MAX_COUNT`, so old activity is gone anyway) or opening every table's `table.db` for its change log, one file per item on an admin request; the one-timestamp-per-item "updated in range" figure is cheap, honest about what it is, and enough to see who is active. For the same reason as the guides report, nothing a user wrote -- doc bodies, titles, table rows, memory text -- leaves the store.

**Why does the Memories report attribute project memories to the project owner but project suggestions to the suggester?**
A project memory belongs to the project, and the project to its owner, so "project memories" on a row are the memories of the projects that user owns. A suggestion is raised in one person's conversation (the `user_id` on the row) and is that person's to accept or dismiss, so the funnel measures how that person responds to the model's suggestions, whichever project they were working in.

**Why does the guides report ship content length instead of the guide text?**
The report exists to find owners, not to read prompts: an admin needs "who still has non-empty guides, and are any still wired to routines" to reach out before deprecation. Shipping every user's system-prompt text to a dashboard would be a needless disclosure for that purpose, and the length alone separates the empty auto-created default rows from real ones.

**Why hide the menu item while impersonating?**
Mirrors the existing impersonation UI affordance: while pretending to be another user, admin-only entry points are hidden to avoid identity-confusion during debugging sessions. Backend admin checks still 403 even if the link is hit directly during impersonation.
