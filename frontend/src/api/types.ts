export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  last_message_at: string;
  project_id?: string;
  routine_id?: string | null;
  model?: string | null;
  /** Thinking level picked for ``model`` (one of the model's
   *  thinking_efforts); null/absent = the model's default. */
  thinking_effort?: string | null;
  archived?: boolean;
  custom_name?: string | null;
  origin?: 'web' | 'slack' | 'user_subagent' | 'inference_api';
  slack_channel_id?: string;
  slack_thread_ts?: string;
  slack_team_id?: string;
}

/** A composer attachment ref returned by the upload-composer-attachments
 *  endpoint. Carried on ``WebSocketSendMessage.attachments`` and persisted
 *  on the user-message row so the transcript can render thumbnails. */
export interface ComposerAttachmentRef {
  attachment_id: string;
  filename: string;
  /** Path relative to the workspace root, e.g. ``pasted/<uuid>.png``. */
  workspace_path: string;
  mime_type: 'image/png' | 'image/jpeg';
  size_bytes: number;
}

export interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  type?: 'text';
  /** Server-assigned per-conversation message sequence. Absent on optimistic
   *  client-only entries; populated once the row reaches the client via
   *  fetchConversationTail / catchup. */
  seq?: number;
  /** Client-only flag: the bubble was inserted by ``sendMessage`` /
   *  ``addUserMessage`` for zero-latency UX before the server persisted the
   *  row. Cleared by ``reconcileOptimisticUserMessage`` when the
   *  ``message_appended`` round-trip lands. */
  optimistic?: boolean;
  /** Client-only id stamped on optimistic user bubbles sent via the
   *  persistent WS. Echoed back by the server's ``send_message_accepted``
   *  receipt so the sending tab can match the ack to this bubble; also the
   *  key for the failed-send Retry / Discard actions. */
  client_send_id?: string;
  /** Client-only flag: the send never reached the server (socket closed at
   *  send time, or no ``send_message_accepted`` receipt within the ack
   *  window). The bubble renders in a failed state with Retry / Discard.
   *  Cleared when a late receipt lands or the row is reconciled. */
  send_failed?: 'not_connected' | 'unconfirmed';
  /** Client-only flag: the assistant bubble was finalized in-place from the
   *  streaming buffer at ``message_appended`` time, ahead of the tail-fetch
   *  for the canonical row. Replaced by ``insertMessagesBySeq`` once the
   *  canonical body arrives. */
  synthetic?: boolean;
  /** Composer-pasted image refs attached to this user message. Persisted on
   *  the message row by the backend so a reload re-renders the thumbnails. */
  attachments?: ComposerAttachmentRef[];
}

export interface ToolUseMessage {
  type: 'tool_use';
  role: 'assistant';
  tool_name: string;
  tool_input: Record<string, unknown>;
  tool_id: string;
  intent_message?: string;
  timestamp: string;
}

export interface ToolResultMessage {
  type: 'tool_result';
  role: 'assistant';
  tool_id: string;
  tool_output: string;
  timestamp: string;
  /** Persisted sub-agent tool call history (display-only). Present on
   *  tool_result messages for agent_task / agent_task_parallel tools. */
  sub_agent_tool_calls?: PersistedSubAgentEvent[];
}

/** A single persisted sub-agent event (tool_use, tool_result, or finished)
 *  as stored in chat_history.json alongside the parent tool_result message.
 *  `sub_agent_finished` is emitted once per sub-agent when it returns and
 *  carries status='success' or status='error'. */
export interface PersistedSubAgentEvent {
  type: 'sub_agent_tool_use' | 'sub_agent_tool_result' | 'sub_agent_finished';
  parent_tool_id: string;
  agent_name: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_id?: string;
  intent_message?: string;
  tool_output?: string;
  status?: 'success' | 'error';
  error?: string;
  /** Nested (2nd-level) sub-agent surfacing. Set on the `agent_task_nested`
   *  tool_use / tool_result NODE event (a 1st-level agent spawning a
   *  grandchild); identifies the nested-agent node. */
  nested_agent_id?: string;
  nested_agent_name?: string;
  nested_agent_model?: string;
  nested_agent_status?: 'success' | 'error';
  /** Set on a 2nd-level (grandchild) agent's own tool_use / tool_result /
   *  finished events. Equals the parent node's `nested_agent_id` so the FE
   *  nests these one indent level under that node. */
  nested_parent_id?: string;
}

export interface UsageStats {
  input_tokens: number;
  output_tokens: number;
  cached_tokens?: number;
  duration_ms: number;
  tool_calls: number;
  // Unified new input tokens metric
  new_input_tokens?: number;
  provider?: string;
  model?: string;
  cache_creation_tokens?: number;
  cache_read_tokens?: number;
  // Breakdown by call type
  top_level_input_tokens?: number;
  top_level_output_tokens?: number;
  top_level_cached_tokens?: number;
  top_level_new_input_tokens?: number;
  top_level_cache_creation_tokens?: number;
  top_level_cache_read_tokens?: number;
  sub_agent_input_tokens?: number;
  sub_agent_output_tokens?: number;
  sub_agent_cached_tokens?: number;
  sub_agent_new_input_tokens?: number;
  sub_agent_cache_creation_tokens?: number;
  sub_agent_cache_read_tokens?: number;
  sub_agent_call_count?: number;
  // Context window usage for the indicator
  context_tokens?: number;
  max_context_tokens?: number;
}

export interface StatsMessage {
  type: 'stats';
  stats: UsageStats;
  timestamp: string;
}

export interface ErrorMessage {
  type: 'error';
  error: string;
  stacktrace?: string;
  timestamp: string;
}

export interface InterruptedMessage {
  type: 'interrupted';
  timestamp: string;
}

/** Marker appended to the transcript when the requested model's safety
 *  classifiers declined the request mid-turn and the SDK's refusal-fallback
 *  middleware continued the response on a fallback model (see
 *  refusal_fallback_models in chat/llm/config.py). */
export interface ModelFallbackMessage {
  type: 'model_fallback';
  role?: string;
  from_model?: string;
  to_model?: string;
  from_display?: string;
  to_display?: string;
  category?: string | null;
  timestamp?: string;
  seq?: number;
}

/** Notice appended to a routine conversation when a routine run ended
 *  without calling the `routine_completed` tool and the server sent one
 *  follow-up turn asking the model to verify its work and finish (see
 *  chat/routine_runs.py). `content` is the exact text the model received. */
export interface RoutineNudgeMessage {
  type: 'routine_nudge';
  role?: string;
  content?: string;
  attempt?: number;
  timestamp?: string;
  seq?: number;
}

/** Marker appended to the transcript when the conversation's model-facing
 *  history was compacted (see chat/compaction.py). The summary is exactly
 *  the text the model sees in place of the compacted messages. */
export interface CompactionMessage {
  type: 'compaction';
  role?: string;
  summary?: string;
  messages_summarized?: number;
  messages_kept?: number;
  tokens_before_estimate?: number;
  tokens_after_estimate?: number;
  archive_file?: string;
  timestamp?: string;
  seq?: number;
}

/** Result of POST /conversations/{id}/compact. */
export interface CompactResult {
  messages_summarized: number;
  messages_kept: number;
  tokens_before_estimate: number;
  tokens_after_estimate: number;
  archive_file: string;
  summary: string;
}

export type WaitHandleStatus =
  | 'pending'
  | 'accepted'
  | 'rejected'
  | 'timed_out'
  | 'cancelled'
  // Action-request Stop: terminal for the UI (composer unlocks), but the
  // model's tool_use is closed only by the user's next message.
  | 'stopped';

export interface WaitHandle {
  id: string;
  user_id: number;
  conversation_id: string;
  kind: string;
  tool_id: string;
  status: WaitHandleStatus;
  payload: Record<string, unknown>;
  response: Record<string, unknown> | null;
  correlation_kind: string | null;
  correlation_id: string | null;
  expires_at: string | null;
  created_at: string | null;
  resolved_at: string | null;
}

/** Narrow projection of a pending ``tool_wait_handles`` row, surfaced by
 *  ``GET /conversations/:id`` so the FE can lock the composer on load
 *  even when the agent is suspended on a wait. */
export interface PendingWaitInfo {
  id: string;
  kind: string;
  correlation_id: string | null;
  created_at: string | null;
}

// Action request types

// Structured grid payload behind an edit_google_spreadsheet preview field
// (type === 'spreadsheet_diff'). Coordinates are 1-based spreadsheet
// row/column indexes; `current` covers the rendered window (replaced
// range plus up to 2 context rows/cols on each side, captured
// server-side at proposal time), while `new`/`changed` are sized to the
// replaced range only.
export interface SpreadsheetDiffGrid {
  start_row: number;
  start_col: number;
  target_start_row: number;
  target_start_col: number;
  target_end_row: number;
  target_end_col: number;
  current: string[][];
  new: string[][];
  changed: boolean[][];
}

// One line of an edit_skill content diff (type === 'skill_content_diff').
// Line numbers are 1-based; context lines carry both sides, del/add lines
// only the side they exist on.
export interface SkillContentDiffLine {
  type: 'context' | 'del' | 'add';
  old_line: number | null;
  new_line: number | null;
  text: string;
}

// Structured line-diff payload behind an edit_skill content-edit preview
// field, computed server-side at proposal time. `lines` covers the WHOLE
// old/new skill body so the card can render both the collapsed
// changed-hunks snippet and the expanded full-content view.
//
// write_doc cards send a BOUNDED window instead (changed hunks plus 3
// context lines): they set `total_old_lines` / `total_new_lines` so the
// card can show the elided stretches, and `truncated` when the window was
// cut at its line cap (the +added / -removed counts stay exact).
export interface SkillContentDiff {
  added: number;
  removed: number;
  lines: SkillContentDiffLine[];
  truncated?: boolean;
  total_old_lines?: number;
  total_new_lines?: number;
}

/** The image a write_doc `add_image` operation embeds (preview field type
 *  'doc_image'). `workspace_path` is relative to the proposing
 *  conversation's workspace; `markdown` is the line appended to the doc. */
export interface DocImagePreview {
  workspace_path: string;
  asset_name: string;
  markdown: string;
  size_bytes: number | null;
}

/** One workspace file listed on a subagent_return approval card. `path` is
 *  relative to the SAME conversation's workspace, so the card can preview /
 *  download it with the conversation's own file APIs. */
export interface SubagentReturnFileEntry {
  path: string;
  name: string;
  size_bytes: number;
}

export interface PreviewField {
  key: string;
  value: string;
  // Discriminator for structured (non key/value) preview fields:
  // 'spreadsheet_diff', 'skill_content_diff', 'subagent_return_files' and
  // 'doc_image' exist today; unknown types fall back to the plain value
  // string.
  type?: string;
  grid?: SpreadsheetDiffGrid;
  diff?: SkillContentDiff;
  files?: SubagentReturnFileEntry[];
  image?: DocImagePreview;
  // Quest Tables cards: 'table_columns' (create_table), 'table_schema_diff'
  // (change_table_schema), 'table_rows_diff' (write_table_rows).
  columns?: TableSchemaDiffColumn[];
  schema_diff?: TableSchemaDiff;
  rows_diff?: TableRowsDiff;
}

export interface ActionRequestMessage {
  type: 'action_request';
  request_id: number;
  request_type: string;
  params: Record<string, unknown>;
  reasoning: string;
  status: 'open' | 'denied' | 'executed' | 'stopped';
  display_name: string;
  preview_fields?: PreviewField[];
  approve_label?: string;
  // Server-derived collapsed-card fields: the executed status label
  // (e.g. 'Created') and a short params summary. Absent on old messages;
  // the mount-time refetch fills them in from the live handler.
  resolved_label?: string;
  summary_snippet?: string;
  wait_handle_id?: string;
  timestamp: string;
  // Populated on disk after resolution (see ChatStorage.update_action_request_message).
  result?: Record<string, unknown> | null;
  feedback?: string | null;
}

/** Card outcome: `connected` once the server verified the credential,
 *  `declined` on Not now, `cancelled` when the conversation was stopped
 *  (or the wait timed out) with the card still open. */
export type ConnectRequestStatus = 'open' | 'connected' | 'declined' | 'cancelled';

/** Inline "Connect <service>" card opened by the model's
 *  request_service_connection tool (chat/connect_requests.py); the agent is
 *  suspended on the `wait_handle_id` row while `status` is `open`. */
export interface ConnectRequestMessage {
  type: 'connect_request';
  wait_handle_id: string;
  status: ConnectRequestStatus;
  service: string;
  label: string;
  description?: string;
  kind: 'oauth' | 'api_key' | 'form';
  /** The OAuth popup URL (oauth kind only); other kinds use the Settings add flow. */
  connect_url?: string;
  /** Connected but the stored grant is missing scopes: the card re-authorizes. */
  needs_reauth?: boolean;
  /** The model's one-line reason, shown verbatim. */
  reason: string;
  timestamp: string;
}

export interface ActionRequest {
  id: number;
  user_id: number;
  conversation_id: string;
  request_type: string;
  params: Record<string, unknown>;
  reasoning: string;
  status: 'open' | 'denied' | 'executed' | 'stopped';
  result: Record<string, unknown> | null;
  created_at: string;
  resolved_at: string | null;
  preview_fields?: PreviewField[];
  display_name?: string;
  approve_label?: string;
  resolved_label?: string;
  summary_snippet?: string;
}

export interface ActionRequestsListResponse {
  action_requests: ActionRequest[];
}

export interface EnrichedActionRequest extends ActionRequest {
  routine_name?: string | null;
  project_name?: string | null;
  project_id?: string | null;
}

export interface EnrichedActionRequestsListResponse {
  action_requests: EnrichedActionRequest[];
}

export interface ActionRequestCountResponse {
  count: number;
}

export interface ActionRequestCountsResponse {
  counts: { open: number; executed: number; denied: number; stopped: number; all: number };
}

// Sub-agent tool call types (display-only, persisted as metadata on parent tool_result)
export interface SubAgentToolUseMessage {
  type: 'sub_agent_tool_use';
  parent_tool_id: string;
  agent_name: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  tool_id: string;
  intent_message?: string;
  /** Present on the `agent_task_nested` NODE event: marks this tool_use as a
   *  nested (2nd-level) agent node rather than a leaf tool call. */
  nested_agent_id?: string;
  nested_agent_name?: string;
  nested_agent_model?: string;
  /** Present on a 2nd-level agent's own tool calls; equals the parent node's
   *  `nested_agent_id`. */
  nested_parent_id?: string;
}

export interface SubAgentToolResultMessage {
  type: 'sub_agent_tool_result';
  parent_tool_id: string;
  agent_name: string;
  tool_id: string;
  tool_output: string;
  /** Present on the `agent_task_nested` NODE result event. */
  nested_agent_id?: string;
  nested_agent_status?: 'success' | 'error';
  /** Present on a 2nd-level agent's own tool result. */
  nested_parent_id?: string;
}

/** Per-sub-agent finished state keyed by agent name within a parent tool. */
export interface SubAgentFinishedInfo {
  status: 'success' | 'error';
  error?: string;
}

export interface SubAgentToolCallInfo {
  agentName: string;
  toolUse: SubAgentToolUseMessage;
  toolResult?: SubAgentToolResultMessage;
}

// Union type for all message content types
export type MessageContent = Message | ToolUseMessage | ToolResultMessage | StatsMessage
  | ErrorMessage | InterruptedMessage | ActionRequestMessage | ConnectRequestMessage
  | CompactionMessage | ModelFallbackMessage | RoutineNudgeMessage;

/** Server verdict that resuming this conversation is expensive (long-idle,
 *  long-context, costly model). Mirrors the dict built by
 *  chat/expensive_resume.py check_expensive_resume. */
export interface ExpensiveResumeInfo {
  model: string;
  /** Latest top-level call's input-side token count (approximates what the
   *  next turn will re-read at uncached rates). */
  context_tokens: number;
  idle_seconds: number;
  /** Rough list-price USD cost of re-processing the full context on the
   *  next message (null for models without a pricing entry). */
  estimated_resume_cost_usd?: number | null;
  /** Rough one-time USD cost of the "Compact this chat" summarization call
   *  (null when the history is unavailable or the model is unpriced). */
  estimated_compaction_cost_usd?: number | null;
  /** Thresholds of the matched rule, for messaging. */
  min_context_tokens: number;
  min_idle_seconds: number;
}

/** Metadata about a cross-user subagent run, returned on
 *  GET /conversations/{id} for origin === 'user_subagent' conversations.
 *  The owner (target user) watches the agent run headlessly; the caller
 *  identity feeds the read-only composer notice. */
export interface SubagentRunInfo {
  status: string;
  caller_email: string;
  caller_name: string;
  created_at?: string | null;
}

export interface ConversationDetail {
  id: string;
  user_id: number;
  created_at: string;
  messages: Message[];
  model?: string | null;
  /** Thinking level picked for ``model`` (null/absent = its default). */
  thinking_effort?: string | null;
  routine_id?: string | null;
  project_id?: string | null;
  origin?: 'web' | 'slack' | 'user_subagent' | 'inference_api';
  slack_channel_id?: string;
  slack_thread_ts?: string;
  slack_team_id?: string;
  /** Present for origin === 'user_subagent' conversations: status + caller
   *  identity of the cross-user subagent run driving this conversation. */
  subagent_run?: SubagentRunInfo;
  /** High-water seq of the conversation's chat_history.json (Phase 2). */
  last_message_seq?: number;
  /** Persisted per-conversation flags (opt-in behaviors set at the start).
   *  Drives the read-only "N flags enabled" composer label after first send. */
  flags?: string[];
  /** User-set display name (null = auto title). Drives the chat header
   *  title unit and its Rename action. */
  custom_name?: string | null;
  /** Resolved sidebar title: custom_name, else the cached first-message
   *  slice, else "New Chat". */
  title?: string;
  archived?: boolean;
  /** Pending wait-handle rows for this conversation. Non-empty when the
   *  agent is suspended on an action request or slack_reply. */
  pending_wait_handles?: PendingWaitInfo[];
  /** Non-null when resuming this conversation is expensive; the FE blocks
   *  the composer with a warning card until acknowledged. */
  expensive_resume?: ExpensiveResumeInfo | null;
  /** Count of model-proposed memories awaiting review in this conversation
   *  (the composer bubble); kept fresh by ``memory_suggestions_changed``. */
  pending_memory_suggestions?: number;
  /** The same for ``suggest_project_memory`` proposals (the separate
   *  project-memory bubble); kept fresh by
   *  ``project_memory_suggestions_changed``. */
  pending_project_memory_suggestions?: number;
}

export interface CreateConversationResponse {
  id: string;
  created_at: string;
  /** Always 0 for a freshly-created conversation; included so the FE can
   *  prime its persistent-WS subscribe with a usable last_seq. */
  last_message_seq?: number;
  /** Always "web" for the FE-driven create endpoints. */
  origin?: 'web' | 'slack' | 'user_subagent' | 'inference_api';
  /** Set on the project-conversation create endpoint; null otherwise. */
  project_id?: string | null;
  /** Always null for a fresh conversation -- model is locked at first message. */
  model?: string | null;
}

export interface StreamEvent {
  // Type can be various values from the backend
  type?: 'text' | 'tool_use' | 'tool_result' | 'message' | 'init' | 'result'
       | 'stats' | 'action_request' | 'connect_request'
       | 'sub_agent_tool_use' | 'sub_agent_tool_result' | 'sub_agent_finished'
       | 'conversation_updated';
  content?: string;
  // Stats fields (for type: 'stats')
  stats?: UsageStats;
  text?: string;
  error?: string;
  stacktrace?: string;
  // For message type
  role?: 'user' | 'assistant';
  delta?: boolean;
  // Tool use fields
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  parameters?: Record<string, unknown>;  // Backend sends 'parameters' not 'tool_input'
  tool_id?: string;
  // Tool result fields
  tool_output?: string;
  output?: string;  // Backend sometimes sends 'output' not 'tool_output'
  status?: string;
  // Intent message for tool calls
  intent_message?: string;
  // Confirm action fields
  action_type?: string;
  payload?: Record<string, unknown>;
  // Action request fields
  request_id?: number;
  request_type?: string;
  params?: Record<string, unknown>;
  reasoning?: string;
  display_name?: string;
  preview_fields?: PreviewField[];
  approve_label?: string;
  resolved_label?: string;
  summary_snippet?: string;
  wait_handle_id?: string;
  // Sub-agent tool call fields
  parent_tool_id?: string;
  agent_name?: string;
  // Conversation updated fields
  conversation_id?: string;
  custom_name?: string;
}

export interface ListConversationsResponse {
  conversations: Conversation[];
  /** True when a paged request has older rows beyond this page. */
  has_more?: boolean;
  /** Opaque keyset cursor for the next page; null on the final page. */
  next_cursor?: string | null;
}

export interface ApiError {
  error: string;
  message: string;
  stacktrace?: string;
}

export interface WebSocketSendMessage {
  message: string;
  timezone?: string;
  model?: string;
  /** Thinking level picked for ``model`` (one of its thinking_efforts);
   *  omitted = the model's default. */
  thinking_effort?: string;
  guide_id?: string;
  skill_ids?: string[];
  /** Per-conversation flags selected via the composer Flags popover.
   *  Honored on the first message only (out-of-band of the %%flags line). */
  flags?: string[];
  attachments?: ComposerAttachmentRef[];
  /** Workspace-relative names of generic files attached to THIS message
   *  (uploaded out-of-band to /files/upload before the send). Surfaced in the
   *  triggering turn's <message_metadata> block; bytes do not ride the frame. */
  attached_filenames?: string[];
}

/** Response shape for ``POST /conversations/:id/composer-attachments``. */
export interface ComposerAttachmentUploadResponse {
  attachments: ComposerAttachmentRef[];
  errors: { filename: string; error: string; message: string }[];
}

// File browser types
export interface FileEntry {
  name: string;
  type: 'file' | 'folder';
  size: number | null;
  lastModified: string;
}

export interface ListFilesResponse {
  currentPath: string;
  files: FileEntry[];
  canGoUp: boolean;
}

export interface FileContentResponse {
  name: string;
  path: string;
  content: string;
  size: number;
}

export interface UploadedFile {
  name: string;
  size: number;
  path: string;
}

export interface UploadError {
  filename: string;
  error: string;
  message: string;
}

export interface UploadResponse {
  uploadedFiles: UploadedFile[];
  errors: UploadError[];
}

export interface FileInfoResponse {
  name: string;
  type: 'file' | 'folder';
  fileCount: number;
}

export interface DeleteFileResponse {
  name: string;
  type: 'file' | 'folder';
  deletedCount: number;
}

export interface CreateFolderResponse {
  name: string;
  path: string;
}

/** Response of POST /conversations/:id/files/copy-to-project|copy-from-project. */
export interface CopyEntryResponse {
  type: 'file' | 'folder';
  /** Destination path, root-relative (`/<dest>`). */
  path: string;
  files_copied: number;
  skipped: number;
  /**
   * True when a requested move removed the source. False for a plain copy
   * and for a move whose copy succeeded but whose source removal failed.
   */
  moved: boolean;
}

// Search types
export interface SearchResult {
  conversation_id: string;
  conversation_title: string;
  project_id: string | null;
  message_index: number;
  message_role: string;
  snippet: string;
  timestamp: string;
  archived?: boolean;
  /** Set when a routine created the conversation (only present with include_routines). */
  routine_id?: string | null;
}

export interface SearchResponse {
  results: SearchResult[];
  total_matches: number;
  query: string;
}

export interface UserSettings {
  custom_system_prompt?: string;
  slack_default_model?: string | null;
  default_model?: string | null;
  // Last-used composer model inside public projects (public visibility has
  // its own admin allow-list, so it is tracked apart from default_model).
  public_default_model?: string | null;
  // The thinking level each of the two was last sent with (null = the
  // model's default); written alongside the model on a chat's first send.
  default_thinking_effort?: string | null;
  public_default_thinking_effort?: string | null;
  // Quest-manageable Gmail label names; each maps to a "[Quest]/<name>"
  // label in Gmail (see Settings > Gmail).
  gmail_labels?: string[] | null;
  // Settings > Appearance colour scheme: "light" | "dark" | "auto"; the
  // server stores auto as null.
  theme?: string | null;
  // Settings > Appearance colour theme ("prototype" | "electric-blue" | "alloy" | "recall"); the
  // server stores the default (prototype) as null.
  color_theme?: string | null;
  // Settings > Slack: bot DM reminders about unanswered (open) action
  // requests. Absent/null means enabled; false is the stored off state.
  slack_pending_notifications_enabled?: boolean | null;
  // Reminder cadence in minutes (5-1440); null means the server default (60).
  slack_pending_notification_interval_minutes?: number | null;
}

export interface SettingsResponse {
  settings: UserSettings;
}

// Inference API key types (Settings > Inference API)
export interface InferenceApiKey {
  id: string;
  user_id: number;
  name: string;
  // Last characters of the token, for display ("...abcd"). The full
  // token is never returned after creation.
  token_hint: string;
  created_at: string;
  last_used_at: string | null;
}

export interface InferenceApiKeysListResponse {
  keys: InferenceApiKey[];
}

// Create response: the only place the raw token ever appears.
export interface CreatedInferenceApiKey extends InferenceApiKey {
  token: string;
}

// One Data Connections row, rendered generically by DataConnectionsSection.
export interface ConnectorRow {
  service: string;
  label: string;
  description?: string;
  // 'form': a multi-connection plugin whose connections are added by
  // POSTing the `form_fields` values to `connections_url`.
  kind: 'oauth' | 'api_key' | 'form';
  // Single-connection rows: the credential is present. Multi-connection
  // rows: at least one connection is connected.
  connected: boolean;
  needs_reauth?: boolean;
  // Server-side availability (e.g. a plugin service the admin has not configured).
  // Absent means available; false hides the row in Settings.
  available?: boolean;
  // oauth rows: popup URL for Connect/Reconnect.
  connect_url?: string;
  // api_key rows: key entry POSTs {[key_field]: key} to key_url; the
  // Disconnect button POSTs to disconnect_url.
  key_url?: string;
  key_field?: string;
  key_placeholder?: string;
  key_hint?: string;
  disconnect_url?: string;
  // api_key rows, optional: guidance shown above the key form in the
  // add-connection key-entry step -- a short text plus links to the
  // upstream pages where the credential is created.
  key_help?: string;
  key_help_links?: ConnectorHelpLink[];
  // Multi-connection rows (a user may hold several connections, one per
  // account): the per-connection list (default first), the cap on how many
  // a user may add, and the base URL of the generic connection routes
  // (POST to add, PUT/DELETE `<connections_url>/<id>`; see
  // createConnection / updateConnection / deleteConnection in client.ts).
  multi_connection?: boolean;
  connection_limit?: number;
  connections?: ConnectionRow[];
  connections_url?: string;
  // form-kind rows: the fields of the add / re-enter form (no values).
  form_fields?: ConnectionFormField[];
}

// One connection of a multi-connection service (never carries credentials).
export interface ConnectionRow {
  id: number;
  // The account the connection belongs to (normalized, e.g. an email
  // address); '' for a legacy row created before accounts existed.
  account: string;
  description: string | null;
  is_default: boolean;
  needs_reauth: boolean;
  // Plugin-supplied per-connection warning text, null when none.
  warning: string | null;
  created_at: string | null;
}

// POST <connections_url>: `values` for form kind, `api_key` for api_key kind.
export interface CreateConnectionBody {
  values?: SchemaFormValues;
  api_key?: string;
  description?: string | null;
}

// PUT <connections_url>/<id>: every key optional; `values` (form kind) or
// `api_key` (api_key kind) re-enters the credential and must resolve to the
// connection's own account. An empty secret value keeps the stored one.
// `is_default` accepts only true.
export interface UpdateConnectionBody {
  description?: string | null;
  is_default?: true;
  values?: SchemaFormValues;
  api_key?: string;
}

export interface ConnectionResponse {
  connection: ConnectionRow;
}

export interface ConnectorHelpLink {
  label: string;
  url: string;
}

export interface ConnectorsResponse {
  connectors: ConnectorRow[];
}

// Twilio plugin: pre-written SMS messages (Settings > SMS Messages), served
// by the plugin's own /auth/twilio/templates routes.
export interface SmsTemplate {
  name: string;
  body: string;
}

export interface SmsTemplatesResponse {
  templates: SmsTemplate[];
  // Admin trusted-channel switch: true -> Quest may also text free-form messages.
  trusted_channel: boolean;
  // The user's verified number, or null while not connected.
  phone_number: string | null;
  max_templates: number;
}

export interface LogoutResponse {
  success: boolean;
}

// Memory types
export interface Memory {
  id: string;
  user_id: number;
  content: string;
  created_at: string;
  updated_at: string | null;
  archived: boolean;
}

/** A memory the model proposed via ``suggest_memory``, reviewed from the
 *  composer's suggestions panel (chat/memory_suggestions.py). */
export interface MemorySuggestion {
  id: string;
  user_id: number;
  conversation_id: string;
  content: string;
  /** The model's one-line justification, or null. */
  reasoning: string | null;
  /** Existing memory archived when this suggestion is accepted, or null. */
  replaces_memory_id: string | null;
  /** Current text of that memory (list endpoint only), for the panel's diff. */
  replaces_content?: string | null;
  status: 'pending' | 'accepted' | 'dismissed';
  memory_id: string | null;
  created_at: string | null;
  resolved_at: string | null;
}

export interface MemorySuggestionsListResponse {
  suggestions: MemorySuggestion[];
  pending_count: number;
}

/** A row of the inbox's Memories tab (chat/memory_inbox.py): a personal or
 *  project suggestion plus where it came from. */
export interface MemoryInboxSuggestion extends MemorySuggestion {
  kind: 'user' | 'project';
  /** The project suggestion's project, or the conversation's project. */
  project_id: string | null;
  project_name: string | null;
  /** The conversation's sidebar title; null when it has none yet or is gone. */
  conversation_title: string | null;
}

export interface MemoryInboxCountsResponse {
  user: number;
  project: number;
  /** Pending suggestions of both kinds across all conversations. */
  pending_count: number;
}

export interface MemoryInboxPendingResponse {
  suggestions: MemoryInboxSuggestion[];
  pending_count: number;
}

export interface MemoryInboxResolvedResponse {
  suggestions: MemoryInboxSuggestion[];
  /** Cursor of the next page; null at the end. */
  next_cursor: string | null;
}

export interface AcceptMemorySuggestionResponse {
  suggestion: MemorySuggestion;
  memory: Memory;
}

export interface MemoriesListResponse {
  memories: Memory[];
}

/** A memory attached to a project (chat/project_memories.py): same shape
 *  as a user ``Memory`` but scoped to ``project_id``. */
export interface ProjectMemory {
  id: string;
  project_id: string;
  content: string;
  created_at: string;
  updated_at: string | null;
  archived: boolean;
}

export interface ProjectMemoriesListResponse {
  memories: ProjectMemory[];
}

/** A project memory the model proposed via ``suggest_project_memory``;
 *  same fields as ``MemorySuggestion`` plus the project it belongs to. */
export interface ProjectMemorySuggestion extends MemorySuggestion {
  project_id: string;
}

export interface ProjectMemorySuggestionsListResponse {
  suggestions: ProjectMemorySuggestion[];
  pending_count: number;
}

export interface AcceptProjectMemorySuggestionResponse {
  suggestion: ProjectMemorySuggestion;
  memory: ProjectMemory;
}

// Guide types
export interface Guide {
  id: string;
  user_id: number;
  name: string;
  content: string;
  is_default: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface GuidesListResponse {
  guides: Guide[];
}

// Skill types
export interface Skill {
  id: string;
  creator_id: number | null;
  creator_name: string | null;
  creator_email: string | null;
  name: string;
  description: string;
  content: string;
  visibility: 'private' | 'shared' | 'public' | 'project';
  created_at: string;
  updated_at: string | null;
}

export interface SkillsListResponse {
  skills: Skill[];
}

export interface SkillShare {
  user_id: number;
  email: string;
  name: string;
  created_at: string;
}

export interface SkillSharesResponse {
  shares: SkillShare[];
}

// User search types
export interface UserSearchResult {
  id: number;
  email: string;
  name: string;
}

export interface UserSearchResponse {
  users: UserSearchResult[];
}

// Project types
export interface Project {
  id: string;
  user_id: number;
  name: string;
  guide: string;
  // Public mode: internet-enabled sandbox, no internal data access.
  // Set at creation time only; immutable afterwards.
  public: boolean;
  // Soft-hide flag (twin of Conversation.archived): hidden from the default
  // list, scheduled routines paused, everything kept.
  archived: boolean;
  created_at: string;
  updated_at: string | null;
  conversation_count: number;
}

export interface ProjectsListResponse {
  projects: Project[];
}

/**
 * One public project whose Quest Docs a private project's conversations
 * may read (Project Settings > Project Access). A row of
 * GET/PUT /projects/{id}/doc-sources.
 */
export interface ProjectDocSource {
  id: string;
  name: string;
  public: boolean;
  archived: boolean;
}

export interface ProjectDocSourcesResponse {
  sources: ProjectDocSource[];
}

// Routine types
export interface Routine {
  id: string;
  project_id: string;
  user_id: number;
  name: string;
  prompt: string;
  guide_id: string | null;
  model: string | null;
  created_at: string;
  updated_at: string | null;
  schedule?: RoutineScheduleSummary | null;
}

export interface RoutinesListResponse {
  routines: Routine[];
}

// Quest Docs types (/app/api/docs; see docs/api/quest-docs-api.md)

/** A user doc is always private; a project doc mirrors its project's `public` flag. */
export type DocMode = 'private' | 'public';

/** `project` when the doc belongs to a project (`project_id` set), else `user`. */
export type DocScope = 'user' | 'project';

/** What the UI may offer for this doc, computed server-side per viewer. */
export interface DocAccess {
  can_rename: boolean;
  // Always false in v1: no doc's mode can be switched.
  can_switch_mode: boolean;
  can_delete: boolean;
  // The UI write verdict: 'free' for the owner or a write share.
  write: 'free' | 'approval' | 'denied';
  // Edit the body, upload images, restore a revision (owner or write share).
  can_edit: boolean;
  // Manage the share roster (owner only).
  can_share: boolean;
  // Delete images from assets/ (owner only).
  can_delete_assets: boolean;
  // Flip the doc's `require_approval` switch (owner only).
  can_require_approval: boolean;
}

/**
 * Safe identity fields of another Quest user (share rosters, attribution).
 * `name` / `email` are null when the account is gone.
 */
export interface DocUserRef {
  id: number;
  name: string | null;
  email: string | null;
}

export type DocSharePermission = 'read' | 'write';

/** One share-roster entry; `user_id` / `user` null means everyone. Owner only. */
export interface DocShare {
  id: number;
  user_id: number | null;
  permission: DocSharePermission;
  created_at: string;
  user: DocUserRef | null;
}

/** Doc row as returned by the list / create / rename endpoints. */
export interface Doc {
  id: string;
  owner_id: number;
  project_id: string | null;
  title: string;
  description: string;
  mode: DocMode;
  content_size: number;
  asset_count: number;
  // 'ui', 'conversation:<id>', 'action_request:<id>', or null (always null
  // for non-owners).
  last_write_source: string | null;
  // The owner's "Require approval for agent writes" switch: every change a
  // conversation proposes goes through a write_doc approval card, even on
  // the owner's own unshared doc. Human edits in the UI are unaffected.
  require_approval: boolean;
  // Naive-UTC ISO strings (no 'Z'; parse with parseUTCTimestamp).
  // `updated_at` doubles as the optimistic-concurrency token.
  created_at: string;
  updated_at: string;
  scope: DocScope;
  // The doc has at least one share (always true for a recipient).
  shared: boolean;
  access: DocAccess;
  // Present for the owner only.
  shares?: DocShare[];
  // True when the viewer is not the owner (the doc reached them by a share).
  shared_with_me: boolean;
  // The viewer's effective share; null for the owner.
  permission: DocSharePermission | null;
  // The owner, for share recipients; null for the owner.
  owner: DocUserRef | null;
  // Owner only: the person behind a `ui:<user_id>` last_write_source, or
  // behind an `action_request:<id>` one when the approved card was a share
  // recipient's (name / email null when that user was deleted).
  last_write_user: DocUserRef | null;
}

/** GET /docs/{id}: the row plus the whole markdown body. */
/**
 * The conversation behind `last_write_source` (a `conversation:<id>` writer,
 * or the conversation that proposed the owner's own approved card), for the
 * viewer footer and History.
 */
export interface DocWriteConversation {
  id: string;
  title: string;
  project_id: string | null;
}

/**
 * One embedded image in the doc's `assets/` directory, as listed by
 * GET /docs/{id} (regular files with a known image extension only).
 */
export interface DocAsset {
  name: string;
  size: number;
  mime: string;
}

export interface DocDetail extends Doc {
  content: string;
  /**
   * Resolved by GET /docs/{id} when `last_write_source` is a conversation
   * the owner still has, or an approved card the owner proposed from one;
   * null otherwise (deleted, a share recipient's card, not a conversation,
   * or blanked for non-owners together with `last_write_source`).
   */
  last_write_conversation: DocWriteConversation | null;
  /** The doc's images, sorted by name (served by GET /docs/{id}/assets/{name}). */
  assets: DocAsset[];
}

export interface ListDocsResponse {
  docs: Doc[];
  has_more: boolean;
  next_cursor: string | null;
}

/** POST /docs/{id}/shares: exactly one of `user_email` / `everyone: true`. */
export interface ShareDocRequest {
  user_email?: string;
  everyone?: boolean;
  permission: DocSharePermission;
}

/** PUT /docs/{id}/content (both fields required). */
export interface UpdateDocContentRequest {
  content: string;
  expected_updated_at: string;
}

/**
 * The row plus the body as stored, returned by a content save or a restore.
 * `changed` is false when the body was identical (nothing written, the
 * token unchanged).
 */
export interface DocContentResponse extends Doc {
  content: string;
  changed: boolean;
}

/**
 * POST /docs/{id}/assets (201). An editor holding `previous_updated_at` as
 * its token adopts `updated_at` (its own upload bumped the row).
 */
export interface DocAssetUploadResponse {
  asset: DocAsset;
  markdown: string;
  asset_count: number;
  updated_at: string;
  previous_updated_at: string;
}

/** DELETE /docs/{id}/assets/{name}. */
export interface DocAssetDeleteResponse {
  deleted: boolean;
  asset_count: number;
  updated_at: string;
  previous_updated_at: string;
}

export type DocVersionSourceKind = 'conversation' | 'action_request' | 'ui' | 'unknown';

/**
 * One version of a doc's body in History: the current body (`id` null) or a
 * revision snapshot (`id` = "<YYYYMMDDTHHMMSSZ>-<n>"). `written_at` is when
 * that body was written, `replaced_at` when a later write replaced it
 * (ISO with 'Z'). `source_label` is ready to show ("you", a person's name, a
 * conversation title, ...); `source` and `conversation` are owner-only.
 */
export interface DocVersion {
  id: string | null;
  written_at: string;
  replaced_at: string | null;
  size: number;
  source: string | null;
  source_kind: DocVersionSourceKind;
  source_label: string;
  conversation: DocWriteConversation | null;
}

/** GET /docs/{id}/revisions (revisions newest first). */
export interface DocRevisionsResponse {
  current: DocVersion;
  revisions: DocVersion[];
}

/** GET /docs/{id}/revisions/{rev_id}; `diff` (revision -> current) with ?diff=current. */
export interface DocRevisionDetail extends DocVersion {
  content: string;
  diff?: SkillContentDiff;
}

export interface RestoreDocRevisionRequest {
  expected_updated_at: string;
}

export interface CopyDocRevisionRequest {
  title?: string;
}

export interface CreateDocRequest {
  title: string;
  description?: string;
  // No `mode`: a user doc is always private and a project doc takes its
  // project's mode, so the UI never sends one.
  project_id?: string | null;
}

export interface UpdateDocRequest {
  title?: string;
  description?: string;
  // The owner's require-approval switch (see Doc.require_approval).
  require_approval?: boolean;
  // Optimistic-concurrency token (the row's updated_at as loaded). A
  // mismatch rejects with a flat 409 stale_update carrying `current`.
  expected_updated_at?: string;
}

// Quest Tables types (/app/api/tables; see docs/api/tables-api.md). Field
// names match chat/tables/routes.py (`_ui_row`, `_ui_detail`) and
// chat/tables/history_routes.py exactly.

/** A user table is always private; a project table mirrors its project's `public` flag. */
export type TableMode = 'private' | 'public';

/** `project` when the table belongs to a project (`project_id` set), else `user`. */
export type TableScope = 'user' | 'project';

export type TableColumnType = 'text' | 'number' | 'url' | 'boolean' | 'enum' | 'date';

/**
 * One stored column (spec 4.3). `key` (`c_` + 8 hex) is server-assigned and
 * stable across renames: the UI addresses cells by it.
 */
export interface TableColumn {
  key: string;
  name: string;
  type: TableColumnType;
  required: boolean;
  unique: boolean;
  // A logical cell value (multi-enum: a list of options), or null.
  default: TableCellValue;
  // Enum columns only, else null.
  options: string[] | null;
  multiple: boolean;
  // Number columns only (0..10), else null.
  precision: number | null;
  description: string;
}

/**
 * A column spec for POST /tables and the `add_column` operation: no `key`
 * (the server refuses one); every field but name and type optional.
 */
export interface TableColumnSpec {
  name: string;
  type: TableColumnType;
  required?: boolean;
  unique?: boolean;
  default?: TableCellValue;
  options?: string[] | null;
  multiple?: boolean;
  precision?: number | null;
  description?: string;
}

/**
 * A cell in logical form: text / url / number / date as strings (numbers
 * are canonical decimal strings, e.g. "1.1"; dates are "YYYY-MM-DD"),
 * booleans as true/false, a multi-enum as a list of options, null for no
 * value.
 */
export type TableCellValue = string | boolean | string[] | null;

/** The raw UI verdicts of chat/tables/access.py. */
export type TableRowsVerdict = 'free' | 'approval' | 'denied';
export type TableSchemaVerdict = 'approval' | 'denied';

/** What the UI may offer for this table, computed server-side per viewer. */
export interface TableAccess {
  can_read: boolean;
  rows: TableRowsVerdict;
  schema: TableSchemaVerdict;
  // rows === 'free': the owner or a write share.
  can_edit: boolean;
  can_restore: boolean;
  // Owner only.
  can_rename: boolean;
  can_delete: boolean;
  can_share: boolean;
  can_edit_schema: boolean;
  can_require_approval: boolean;
  // = can_edit: a SQLite download with the change log.
  can_download_history: boolean;
  deny_reason: string | null;
  schema_deny_reason: string | null;
}

export type TableSharePermission = 'read' | 'write';

/** Safe identity fields of another Quest user; name / email null once deleted. */
export interface TableUserRef {
  id: number;
  name: string | null;
  email: string | null;
}

/** One share-roster entry; `user_id` / `user` null means everyone. Owner only. */
export interface TableShare {
  id: number;
  user_id: number | null;
  permission: TableSharePermission;
  created_at: string;
  user: TableUserRef | null;
}

/** Table row as returned by GET /tables and the share routes (`_ui_row`). */
export interface TableListRow {
  id: string;
  owner_id: number;
  project_id: string | null;
  title: string;
  description: string;
  mode: TableMode;
  schema_version: number;
  row_count: number;
  // 'ui:<user_id>', 'conversation:<id>', 'action_request:<id>',
  // 'revert:<change_id>', or null (always null for non-owners).
  last_write_source: string | null;
  // The owner's "Require approval for agent writes" switch.
  require_approval: boolean;
  // Naive-UTC ISO strings (no 'Z'; parse with parseUTCTimestamp).
  // `updated_at` doubles as the metadata-edit concurrency token.
  created_at: string;
  updated_at: string;
  scope: TableScope;
  column_count: number;
  // The table has at least one share.
  shared: boolean;
  // True when the viewer is not the owner.
  shared_with_me: boolean;
  // The viewer's effective share; null for the owner.
  permission: TableSharePermission | null;
  // The owner, for share recipients; null for the owner.
  owner: TableUserRef | null;
  // Owner only (null for everyone else).
  shares: TableShare[] | null;
  // Owner only: the user behind a `ui:<id>` source, or the proposer of an
  // `action_request:<id>` one when that is not the owner.
  last_write_user: TableUserRef | null;
  access: TableAccess;
}

/** The conversation named by `last_write_source`, for the viewer footer. */
export interface TableWriteConversation {
  id: string;
  title: string;
  project_id: string | null;
}

/**
 * GET / POST / PUT one table (`_ui_detail`): the row plus the full schema.
 * `last_write_conversation` is only on GET /tables/{id}.
 */
export interface TableDetail extends TableListRow {
  schema: TableColumn[];
  last_write_conversation?: TableWriteConversation | null;
}

/** PUT /tables/{id}/schema: the detail plus the new change's id. */
export interface TableSchemaChangeResponse extends TableDetail {
  change_id: number;
}

export interface ListTablesResponse {
  tables: TableListRow[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface CreateTableRequest {
  title: string;
  description?: string;
  // No `mode`: a user table is private, a project table takes its project's.
  project_id?: string | null;
  columns: TableColumnSpec[];
}

export interface UpdateTableRequest {
  title?: string;
  description?: string;
  require_approval?: boolean;
  // The row's updated_at as loaded; a mismatch rejects with the flat 409
  // stale_update carrying `current` (a TableListRow).
  expected_updated_at?: string;
}

/**
 * One schema operation (spec 4.4, chat/tables/schema_ops.py `_OP_PARAMS`).
 * Columns are addressed by NAME, as for the change_table_schema card.
 */
export type TableSchemaOperation =
  | { op: 'add_column'; column: TableColumnSpec; after?: string | null }
  | { op: 'rename_column'; name: string; new_name: string }
  | { op: 'drop_column'; name: string }
  | { op: 'reorder_columns'; names: string[] }
  | { op: 'set_description'; name: string; description: string }
  | { op: 'set_required'; name: string; required: boolean }
  | { op: 'set_unique'; name: string; unique: boolean }
  | { op: 'set_default'; name: string; default: TableCellValue }
  | { op: 'set_precision'; name: string; precision: number | null }
  | { op: 'add_enum_option'; name: string; option: string; after?: string | null }
  | { op: 'rename_enum_option'; name: string; option: string; new_option: string }
  | { op: 'remove_enum_option'; name: string; option: string }
  | { op: 'set_enum_multiple'; name: string; multiple: boolean }
  | { op: 'rename_table'; title: string };

export interface UpdateTableSchemaRequest {
  operations: TableSchemaOperation[];
  expected_schema_version: number;
}

/** Filter operators of the query DSL (spec 6.1, chat/tables/filters.py). */
export type FilterOp =
  | 'eq'
  | 'ne'
  | 'in'
  | 'is_null'
  | 'not_null'
  | 'contains'
  | 'starts_with'
  | 'ends_with'
  | 'lt'
  | 'lte'
  | 'gt'
  | 'gte'
  | 'between'
  | 'contains_any'
  | 'contains_all'
  | 'is_empty';

/** A condition on one column, addressed by NAME. `value` per op (spec 6.1). */
export interface FilterLeaf {
  column: string;
  op: FilterOp;
  value?: unknown;
}

/** The filter DSL: max depth 3, max 20 leaves (validated server-side). */
export type FilterNode =
  | { and: FilterNode[] }
  | { or: FilterNode[] }
  | { not: FilterNode }
  | FilterLeaf;

/** One sort key (max 3), by column NAME; nulls sort last. */
export interface SortKey {
  column: string;
  direction: SortDirection;
}

/**
 * A grid row: cells keyed by column KEY, plus the row id and timestamp.
 * Read a cell as `row[column.key] as TableCellValue`.
 */
export interface TableRow {
  // The row's integer id (never reused, survives restores).
  _id: number;
  _updated_at: string;
  [columnKey: string]: TableCellValue | number;
}

/** GET /tables/{id}/rows. */
export interface TableRowsPage {
  rows: TableRow[];
  // Filtered count, ignoring paging.
  total: number;
  limit: number;
  offset: number;
  // More rows match past this page (offset + rows.length < total).
  truncated: boolean;
  // Lets the grid tell its loaded schema went stale.
  schema_version: number;
}

export type TableChangeKind = 'rows' | 'schema' | 'restore';

/** One change-log entry (GET /tables/{id}/changes; history_routes.py Entry). */
export interface TableChange {
  id: number;
  created_at: string;
  // Raw write source, owner only (null otherwise).
  source: string | null;
  // Phrased for the viewer ("you", a name, a conversation title,
  // "Request #12", "Restored to #4", ...).
  source_label: string;
  // The conversation behind the source when the viewer owns it.
  conversation: TableWriteConversation | null;
  actor: { user_id: number; name: string } | null;
  kind: TableChangeKind;
  op: string;
  summary: string;
  row_count: number | null;
  // False once a restore rewound past this entry.
  in_effect: boolean | null;
  // A restore entry's target change id, else null.
  restore_to: number | null;
  schema_before: number;
  schema_after: number;
  size_bytes: number;
}

export interface ListTableChangesResponse {
  changes: TableChange[];
  has_more: boolean;
  next_before_id: number | null;
}

/** A column label in a change detail; `type` null for a key no longer known. */
export interface TableChangeColumn {
  key: string;
  name: string;
  type: TableColumnType | null;
  multiple: boolean;
  options: string[] | null;
  precision: number | null;
  // The key is in the table's current schema.
  current: boolean;
}

/**
 * One entry of a schema diff (chat/tables/schema_ops.py `schema_diff`). The
 * history detail sends it with column keys; the change_table_schema card
 * sends it key-less with the rewrite counts (`cells_rewritten`,
 * `options_renamed`). The column dicts are key-less on the card too.
 */
export interface TableSchemaDiffEntry {
  key?: string;
  status: 'added' | 'dropped' | 'renamed' | 'changed' | 'unchanged';
  moved: boolean;
  before: TableSchemaDiffColumn | null;
  after: TableSchemaDiffColumn | null;
  changes: Record<string, { before: unknown; after: unknown }>;
  options_added: string[];
  options_removed: string[];
  // Card only: distinct cells of this column an option rename rewrites.
  cells_rewritten?: number;
  // Card only: this column's rename_enum_option rewrites, in order.
  options_renamed?: { option: string; new_option: string }[];
}

/** A column dict inside a schema diff: a TableColumn, `key` absent on cards. */
export type TableSchemaDiffColumn = Omit<TableColumn, 'key'> & { key?: string };

export interface TableSchemaDiff {
  columns: TableSchemaDiffEntry[];
  order_changed: boolean;
  before_order: string[];
  after_order: string[];
  changed: boolean;
  // Card only: the sum of the entries' cells_rewritten.
  cells_rewritten_total?: number;
}

/**
 * A before/after row grid: the write_table_rows card's `rows_diff` (values
 * keyed by column NAME, `columns` the names in schema order), and the shape
 * the History detail maps a rows change onto, so both render through
 * TableRowsDiffPreview.
 */
export interface TableRowsDiff {
  columns: string[];
  rows: TableRowsDiffRow[];
  // The full count of affected rows (rows may be cut short).
  total: number;
  truncated: boolean;
}

export interface TableRowsDiffRow {
  // Null for a row an insert would add.
  _id: number | null;
  status: 'added' | 'changed' | 'deleted';
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  changed_columns: string[];
}

/** A row image in a change detail: cells keyed by column KEY. */
export interface TableChangeRowImage {
  _id: number;
  _created_at?: string;
  _updated_at?: string;
  [columnKey: string]: unknown;
}

/**
 * GET /tables/{id}/changes/{change_id}: the entry plus its images. A rows
 * change has row-image lists in `before` / `after`; a schema change has
 * `{columns, title, dropped_values}` / `{op?, columns, title, operations,
 * backfill}` objects; a restore `{}` / `{restore_to}`.
 */
export interface TableChangeDetail extends TableChange {
  columns: TableChangeColumn[];
  before: TableChangeRowImage[] | Record<string, unknown>;
  after: TableChangeRowImage[] | Record<string, unknown>;
  truncated: { before: boolean; after: boolean };
  before_total: number | null;
  after_total: number | null;
  schema_diff: TableSchemaDiff | null;
  operations: Record<string, unknown>[] | null;
  row_ids: number[];
  row_ids_total: number;
}

/** POST /tables/{id}/restore. `change` null if edit access was lost meanwhile. */
export interface RestoreTableResponse {
  change: TableChange | null;
  table: TableDetail;
  schema_version: number;
  row_count: number;
  restored_to: number;
}

/** POST /tables/{id}/shares: exactly one of `user_email` / `everyone: true`. */
export interface ShareTableRequest {
  user_email?: string;
  everyone?: boolean;
  permission: TableSharePermission;
}

// Project table types
export interface ProjectTable {
  name: string;
}

export interface ProjectTablesResponse {
  tables: ProjectTable[];
}

export type SortDirection = 'asc' | 'desc';

export interface TableDataResponse {
  table_name: string;
  columns: string[];
  rows: unknown[][];
  total_rows: number;
  limit: number;
  offset: number;
  sort_by: string | null;
  sort_dir: SortDirection | null;
}

// Version types (GET /app/api/version). `git_hash` drives the redeploy
// poll; the rest comes from the nearest `v<semver>` release tag and feeds
// Settings > About. All null/0 on an untagged checkout.
export interface VersionResponse {
  git_hash: string | null;
  // "1.4.0" on a release, "1.4.0+3.g1a2b3c4" past one, null when untagged.
  version: string | null;
  tag: string | null;
  // ISO-8601 date-time of the release tag.
  released: string | null;
  commits_since_tag: number;
}

// Admin system monitor types.
// Per-model usage is a discriminated union on `provider`: each entry carries
// that provider's summed NATIVE token fields in `metrics` (no normalized
// in/out/cached buckets) plus `estimated_cost_usd`, the USD cost of those
// calls, and `cost_source` saying where that figure came from:
// 'reported' when every call was priced from the amount the provider
// itself reported (OpenRouter rows carry `usage.cost`), 'estimated' when
// every call is a list-price estimate computed per-call-tier server-side
// (db/llm_pricing.py), 'mixed' when some of each. Both are null when the
// model has neither reported amounts nor a pricing entry.
export type AdminCostSource = 'reported' | 'estimated' | 'mixed';

// --- Routine Settings > Costs (GET /projects/{id}/routines/{id}/costs) ---
// One run = one conversation the routine created; a run's cost is that
// conversation's whole recorded usage, and every figure buckets runs by
// their START time (not per call), so the window totals, run counts and
// the recent-runs table always agree. Costs follow the null-on-unpriced
// convention: null when a run in the bucket used a model without pricing.

export interface RoutineCostBucket {
  run_count: number;
  call_count: number;
  total_tokens: number;
  cost_usd: number | null;
  cost_source: AdminCostSource | null;
}

export interface RoutineCostWindow {
  days: number;
  // Runs started in [now - days, now).
  current: RoutineCostBucket;
  // Runs started in [now - 2*days, now - days): the period-over-period base.
  previous: RoutineCostBucket;
}

export interface RoutineCostRun {
  conversation_id: string;
  title: string;
  started_at: string;
  // Every model that made a call during the run, heaviest first; falls
  // back to the conversation's model when the run recorded no calls.
  models: string[];
  call_count: number;
  total_tokens: number;
  // 0 (not null) for a run that recorded no calls.
  cost_usd: number | null;
  cost_source: AdminCostSource | null;
}

export interface RoutineCostReport {
  routine_id: string;
  routine_created_at: string | null;
  generated_at: string;
  // In display order: 7 days, then 28 days.
  windows: RoutineCostWindow[];
  // Every surviving run since the routine was created.
  lifetime: RoutineCostBucket;
  // Newest first, at most 10.
  recent_runs: RoutineCostRun[];
}

export interface AdminGeminiModelUsage {
  model: string;
  provider: 'gemini';
  call_count: number;
  // prompt + candidates + thoughts + tool_use_prompt (coarse magnitude).
  total_tokens: number;
  estimated_cost_usd: number | null;
  cost_source: AdminCostSource | null;
  metrics: {
    prompt_token_count: number; // includes cached_content_token_count
    cached_content_token_count: number;
    candidates_token_count: number; // excludes thoughts_token_count
    thoughts_token_count: number;
    tool_use_prompt_token_count: number;
  };
}

export interface AdminAnthropicModelUsage {
  model: string;
  provider: 'anthropic';
  call_count: number;
  // input + output + cache_read + cache_creation (coarse magnitude).
  total_tokens: number;
  estimated_cost_usd: number | null;
  cost_source: AdminCostSource | null;
  metrics: {
    input_tokens: number; // uncached input only
    output_tokens: number;
    cache_read_input_tokens: number;
    cache_creation_input_tokens: number;
    cache_creation_5m_input_tokens: number;
    cache_creation_1h_input_tokens: number;
  };
}

export interface AdminOpenRouterModelUsage {
  model: string;
  provider: 'openrouter';
  call_count: number;
  // prompt + completion (coarse magnitude; prompt includes cached,
  // completion includes reasoning).
  total_tokens: number;
  estimated_cost_usd: number | null;
  cost_source: AdminCostSource | null;
  metrics: {
    prompt_tokens: number; // includes cached_prompt_tokens
    cached_prompt_tokens: number;
    completion_tokens: number; // includes reasoning_tokens
    reasoning_tokens: number;
  };
}

export type AdminConversationModelUsage =
  | AdminGeminiModelUsage
  | AdminAnthropicModelUsage
  | AdminOpenRouterModelUsage;

// Coarse cross-provider conversation total (native buckets don't sum
// meaningfully across providers, but an all-in magnitude does).
// `estimated_cost_usd` is null when any model in the conversation lacks a
// pricing entry (a partial sum would read as the full conversation cost).
// `cost_source` follows the per-model convention across all models (null
// while nothing priced has been folded in, so also on zero-activity rows).
export interface AdminConversationUsageTotal {
  call_count: number;
  total_tokens: number;
  estimated_cost_usd: number | null;
  cost_source: AdminCostSource | null;
}

// One conversation row in the System Reports tables. Served by both the
// latest-active and most-expensive endpoints; on the latter, a conversation
// deleted since its calls were made keeps its spend but loses its metadata
// (placeholder title, null last_message_at, owner resolved from call rows).
export interface AdminActiveConversation {
  id: string;
  title: string;
  user_id: number | null;
  user_email: string;
  user_name: string;
  project_id: string | null;
  // Set when the conversation was created by a routine (scheduled or
  // one-click run); the dashboard uses it to badge routine rows.
  routine_id: string | null;
  last_message_at: string | null;
  origin: string | null;
  last_model: string | null;
  // Per-model provider-native token usage aggregated from the raw
  // llm_calls_gemini / llm_calls_anthropic tables (one entry per model used
  // in the conversation, incl. sub-agent models), plus a coarse total.
  usage_by_model: AdminConversationModelUsage[];
  usage_total: AdminConversationUsageTotal;
  // Input-side token count of the most recent top-level call (either
  // provider) -- what the next turn would re-read; null when no calls
  // are recorded.
  latest_context_tokens: number | null;
  // Distinct UTC days with at least one user message, over the whole
  // conversation lifetime.
  active_days: number;
}

export interface LatestActiveConversationsResponse {
  conversations: AdminActiveConversation[];
}

// Ranked most-expensive-first; same row shape as the latest-active list.
export interface MostExpensiveConversationsResponse {
  conversations: AdminActiveConversation[];
}

// One row in the System Reports "Users" section: per-user activity/cost
// aggregates over the selected date range. Costs follow the usual partial-sum
// convention (null when any model in that split lacks a pricing entry);
// token aggregates reuse the per-conversation model-usage shapes so the
// breakdown cell renders identically.
// One routine's share of a user's in-range routine spend (Users report).
export interface AdminUserRoutineCost {
  routine_id: string;
  // "(deleted routine)" with null project fields only if the routine
  // vanished mid-request (routine deletes null the conversations'
  // routine_id, so this is a race, not a steady state).
  routine_name: string;
  project_id: string | null;
  // Routine names are unique per project only; the project name
  // disambiguates same-named routines across projects.
  project_name: string | null;
  // Distinct routine-created conversations with at least one call in range.
  conversation_count: number;
  // null when any model used under this routine lacks a pricing entry.
  cost_usd: number | null;
  cost_source: AdminCostSource | null;
}

export interface AdminUserReportRow {
  user_id: number;
  // Empty email + "(unknown user)" name when call rows reference a user id
  // that no longer exists in the users table.
  user_email: string;
  user_name: string;
  // Distinct UTC days with at least one user message across the user's
  // non-routine conversations, clipped to the range.
  active_days: number;
  // Distinct conversations with at least one recorded call in the range.
  conversation_count: number; // non-routine only
  routine_conversation_count: number;
  cost_excluding_routines_usd: number | null;
  cost_excluding_routines_source: AdminCostSource | null;
  cost_routines_usd: number | null;
  cost_routines_source: AdminCostSource | null;
  // The routine split itemized per routine, most expensive first (ranked
  // by the priceable portion, so an unpriced routine still sorts by what
  // CAN be priced). Empty when the user ran no routines in the range.
  routine_costs: AdminUserRoutineCost[];
  // Aggregated across ALL of the user's conversations (routines included).
  usage_by_model: AdminConversationModelUsage[];
  usage_total: AdminConversationUsageTotal;
}

// Sorted by known in-range cost (descending); every user keeps a row even
// with zero activity in the range.
export interface AdminUserReportResponse {
  users: AdminUserReportRow[];
}

// One of a model's most expensive users in the range (Models report).
export interface AdminModelTopUser {
  user_id: number;
  // Empty email + "(unknown user)" name when call rows reference a user id
  // that no longer exists in the users table.
  user_email: string;
  user_name: string;
  // Calls this user made to the model in the range.
  call_count: number;
  // null when the model has no pricing entry and no reported amounts.
  cost_usd: number | null;
  cost_source: AdminCostSource | null;
}

// One row of the admin Models report (System Reports > Models): a model id
// with at least one recorded call in the range, aggregated across every
// user, conversation and call type.
export interface AdminModelReportRow {
  model: string;
  provider: 'gemini' | 'anthropic' | 'openrouter';
  // Distinct users (per the call rows) and conversations with at least one
  // call to this model in the range.
  user_count: number;
  conversation_count: number;
  // Share of the model's in-range spend accrued in routine-created
  // conversations (surviving rows' routine_id) and by sub-agent calls
  // (the call rows' call_type). Each null when an unpriced call landed in
  // it; 0 with a null source when the model saw no such calls at all.
  cost_routines_usd: number | null;
  cost_routines_source: AdminCostSource | null;
  cost_subagents_usd: number | null;
  cost_subagents_source: AdminCostSource | null;
  // Up to ten users ranked by their spend on this model (priceable
  // portion first, then call count).
  top_users: AdminModelTopUser[];
  // Exactly one entry -- this model -- in the conversation-row shapes so
  // the shared token cell renders it; usage_total mirrors that entry.
  usage_by_model: AdminConversationModelUsage[];
  usage_total: AdminConversationUsageTotal;
}

// Sorted by known in-range cost (descending; ties: total tokens, model id).
export interface AdminModelReportResponse {
  models: AdminModelReportRow[];
}

// --- Total Usage (GET /admin/system-monitor/usage-report) ---
// Instance-wide aggregates for one period. Every figure keys on the UTC
// calendar day of each recorded LLM call; see chat/usage_report.py for
// the metric definitions.
export interface AdminUsageBucket {
  // Inclusive UTC dates the bucket covers.
  start: string;
  end: string;
  // Distinct conversations with at least one call in the period (routine
  // runs included; routine_conversations is that subset).
  conversations: number;
  routine_conversations: number;
  // Distinct users with a call in a NON-routine conversation.
  active_users: number;
  // Accounts created in the period.
  new_users: number;
  call_count: number;
  total_tokens: number;
  // null when any model in the period is unpriced (partial-sum rule).
  cost_usd: number | null;
  cost_source: AdminCostSource | null;
  // The priceable portion, never null -- what the charts plot.
  known_cost_usd: number;
}

export interface AdminUsageWindow {
  // 1 = today (vs yesterday), else "last N days" incl. today.
  days: number;
  current: AdminUsageBucket;
  // The N days right before `current`.
  previous: AdminUsageBucket;
}

export type AdminUsageGranularity = 'daily' | 'weekly' | 'monthly';

export interface AdminUsageReport {
  generated_at: string;
  // The UTC date the windows and series end on.
  today: string;
  windows: AdminUsageWindow[];
  // Every recorded call (start = the earliest call or signup day).
  lifetime: AdminUsageBucket;
  // Gap-free, oldest first, each ending with the in-progress period that
  // contains `today`: 90 days / 52 Monday-start weeks / 24 calendar months.
  series: Record<AdminUsageGranularity, AdminUsageBucket[]>;
}

// One row of the admin Guides report (System Reports > Guides). A deprecation
// tracker: every user guide (guides table, incl. empty auto-created default
// rows) plus every project with non-empty project instructions
// (projects.guide). Content is never shipped, only its length.
export interface AdminGuideReportRow {
  kind: 'user' | 'project';
  // Guide id for kind=user, project id for kind=project.
  id: string;
  // Guide name (user) or project name (project).
  name: string;
  user_id: number;
  user_email: string;
  user_name: string;
  // kind=user only (null for project rows).
  is_default: boolean | null;
  // kind=project only (null for user rows).
  project_id: string | null;
  public: boolean | null;
  content_length: number;
  // Routines still referencing the guide as an override; kind=user only.
  routine_count: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AdminGuidesReportResponse {
  guides: AdminGuideReportRow[];
}

// Admin System Reports "Docs" / "Tables" sections
// (GET /admin/system-monitor/docs-report and /tables-report): one row per
// user who owns at least one doc / table, lifetime ownership figures next
// to the items created / updated in the selected range. No titles or
// bodies are ever shipped; see chat/feature_usage_report.py.
interface AdminOwnedItemsReportRowBase {
  user_id: number;
  user_email: string;
  user_name: string;
  // Items whose latest body write came from a model (conversation or
  // approved action request) rather than a person in the UI.
  model_written_count: number;
  // Items created, resp. last updated, inside the selected range.
  created_in_range: number;
  updated_in_range: number;
  // Newest updated_at across the user's items (ISO, naive UTC).
  last_updated_at: string | null;
}

export interface AdminDocsReportRow extends AdminOwnedItemsReportRowBase {
  doc_count: number;
  user_doc_count: number;
  project_doc_count: number;
  public_doc_count: number;
  // Docs with at least one share grant (a user or "everyone").
  shared_doc_count: number;
  // Sum of the cached doc.md sizes, and of the embedded image counts.
  content_bytes: number;
  asset_count: number;
}

export interface AdminDocsReportResponse {
  users: AdminDocsReportRow[];
  // Roster size, for the "N of M users" summary.
  total_users: number;
}

export interface AdminTablesReportRow extends AdminOwnedItemsReportRowBase {
  table_count: number;
  user_table_count: number;
  project_table_count: number;
  public_table_count: number;
  shared_table_count: number;
  // Sum of the cached row counts, and of the schema lengths.
  row_total: number;
  column_total: number;
}

export interface AdminTablesReportResponse {
  users: AdminTablesReportRow[];
  total_users: number;
}

// Admin System Reports "Memories" section
// (GET /admin/system-monitor/memories-report): one row per user with any
// personal memory, suggestion, project memory (in a project they own) or
// project suggestion (raised in their conversation). The *_in_range
// suggestion figures are suggestions created in the range, split by
// their current status.
export interface AdminMemoriesReportRow {
  user_id: number;
  user_email: string;
  user_name: string;
  // Active (not archived) personal memories, archived ones, and the
  // characters stored in the active ones.
  memory_count: number;
  archived_count: number;
  content_chars: number;
  created_in_range: number;
  suggested_in_range: number;
  accepted_in_range: number;
  dismissed_in_range: number;
  pending_in_range: number;
  // Project memories across the projects the user owns.
  project_memory_count: number;
  project_archived_count: number;
  // Owned projects holding at least one project memory.
  project_count: number;
  project_created_in_range: number;
  project_suggested_in_range: number;
  project_accepted_in_range: number;
  project_dismissed_in_range: number;
  project_pending_in_range: number;
  // Newest memory or suggestion timestamp of any kind (ISO, naive UTC).
  last_activity_at: string | null;
}

export interface AdminMemoriesReportResponse {
  users: AdminMemoriesReportRow[];
  total_users: number;
}

// System Reports "Skills" section: one row per skill whose body was put in
// front of the model in the selected range (skill_loads ledger).
export type AdminSkillReportKind =
  | 'system'
  | 'private'
  | 'shared'
  | 'public'
  | 'project'
  | 'deleted';

export interface AdminSkillsReportRow {
  skill_id: string;
  name: string;
  kind: AdminSkillReportKind;
  // Creator identity for DB skills; null for system and deleted skills.
  owner_email: string | null;
  owner_name: string | null;
  project_id: string | null;
  // Distinct conversations with a load in range, and the routine-created share.
  conversation_count: number;
  routine_conversation_count: number;
  // Distinct users those loads belong to.
  user_count: number;
  // Loads in range (one per skill per load event), split by source family.
  load_count: number;
  autoload_count: number;
  composer_count: number;
  model_load_count: number;
  // Rows imported from pre-ledger skill_reads.json sidecars (source unknown).
  backfill_count: number;
  // Newest load in range (ISO, naive UTC).
  last_used_at: string | null;
}

export interface AdminSkillsReportBackfill {
  completed_at: string;
  row_count: number;
}

export interface AdminSkillsReportResponse {
  skills: AdminSkillsReportRow[];
  // Distinct conversations with any skill load in range.
  conversation_count: number;
  load_count: number;
  // Status of the skill_reads.json import; null until it has run.
  backfill: AdminSkillsReportBackfill | null;
}

// System Reports "Tool Calls" section: one row per effective tool called in
// the selected range (tool_calls ledger; a call through the tool_call
// meta-tool counts under the tool it routed to).
export interface AdminToolCallsReportRow {
  tool: string;
  // Calls in range, and the splits in the sub-line.
  call_count: number;
  // Issued through the tool_call meta-tool rather than by the tool's own name.
  dynamic_count: number;
  // Calls made in routine-created conversations.
  routine_call_count: number;
  sub_agent_call_count: number;
  // Calls bridged from sandbox scripts (POST /api/tool-call).
  script_call_count: number;
  // Rows imported from pre-ledger chat_history.json transcripts.
  backfill_count: number;
  // Distinct conversations with a call in range, and distinct users.
  conversation_count: number;
  user_count: number;
  // Newest call in range (ISO, naive UTC).
  last_called_at: string | null;
}

export interface AdminToolCallsReportResponse {
  tools: AdminToolCallsReportRow[];
  call_count: number;
  // Distinct conversations with any tool call in range.
  conversation_count: number;
  // Status of the transcript import; null until it has run.
  backfill: AdminSkillsReportBackfill | null;
}

// Admin feature gates (server-global on/off switches for optional features;
// every feature is off by default)
export interface FeatureGate {
  feature: string;
  label: string;
  description: string;
  enabled: boolean;
  // Emails allowed to use the feature while enabled; null = all users.
  // Only meaningful when supports_user_access is true.
  allowed_users: string[] | null;
  // Whether this gate can be narrowed to specific users (allowed_users).
  supports_user_access: boolean;
  // False when the server lacks something the feature needs (e.g. voice
  // input without a Gemini Vertex model); the gate cannot be turned on
  // until it is fixed and unavailable_reason says what is missing.
  available: boolean;
  unavailable_reason: string | null;
}

// POST /transcribe response (composer voice input).
export interface TranscribeResponse {
  text: string;
  model: string;
}

// PUT /admin/feature-gates/{feature} body. Omit allowed_users to keep the
// stored list; pass null to open the gate to all users.
export interface FeatureGateUpdate {
  enabled: boolean;
  allowed_users?: string[] | null;
}

export interface FeatureGatesListResponse {
  features: FeatureGate[];
}

// GET/PUT /admin/limits: admin-configurable server limits with their
// default and allowed range (config/limits.py).
export interface AdminLimits {
  // Per-file workspace upload cap in MB.
  max_upload_mb: number;
  max_upload_mb_default: number;
  max_upload_mb_min: number;
  max_upload_mb_max: number;
}

// Admin service credentials (server-level upstream API credentials).
// Fully schema-driven: the backend describes each service's form as a list
// of CredentialFieldSchema rows and the frontend renders one generic card
// per service -- core services and plugins alike.
export interface ServiceCredentialSummary {
  service: string;
  label: string;
  configured: boolean;
  // "store" = per-service file in the data directory, "legacy" = still read
  // from a legacy credentials file, null = unconfigured
  source: 'store' | 'legacy' | null;
}

// Field types of a schema-rendered form (components/settings/SchemaForm).
// `list` (admin cards only) holds zero or more entries, each a sub-form of
// `item_fields`, edited one entry at a time with add / edit / remove.
export type SchemaFieldType = 'text' | 'secret' | 'bool' | 'textarea' | 'select' | 'list';

// One field of a schema-rendered form: the common shape of the admin
// credential schema (CredentialFieldSchema) and the per-user connection
// form (ConnectionFormField), both views of the backend's CredentialField.
export interface SchemaFormField {
  key: string;
  label: string;
  type: SchemaFieldType;
  placeholder: string;
  // Optional plain-text guidance shown under the label (what the input
  // should look like).
  help?: string;
  required: boolean;
  // Key of a sibling bool field; when set, the field is required iff that
  // toggle is on (takes precedence over `required`).
  required_if?: string | null;
  // Key of a sibling bool field that must be on for the field to render.
  visible_if?: string | null;
  // select fields: the allowed choices.
  options?: string[];
  // Initial value of a text / textarea / select field when nothing is
  // stored (a fresh form, or a new entry of a list field).
  default?: string;
  // list fields: the entry sub-form (text / textarea / bool / select only),
  // the singular noun for one entry ("server" -> "Add server") and the
  // entry cap (0 or absent = unlimited).
  item_fields?: SchemaFormField[];
  item_label?: string;
  max_items?: number;
}

// One entry of a list field: its item fields' values.
export type SchemaFormItem = Record<string, string | boolean>;

// A form value: strings for text-like fields, booleans for bool fields, an
// entry array for list fields.
export type SchemaFormValue = string | boolean | SchemaFormItem[];

// Form values keyed by field key.
export type SchemaFormValues = Record<string, SchemaFormValue>;

export interface CredentialFieldSchema extends SchemaFormField {
  required_if: string | null;
  visible_if: string | null;
}

// A per-user form-kind connection field (GET /connectors `form_fields`):
// select choices are resolved server-side at request time.
export interface ConnectionFormField extends SchemaFormField {
  required_if: string | null;
  visible_if: string | null;
  options: string[];
}

// Masked form values keyed by field key: text/textarea fields round-trip
// verbatim, bool fields are booleans, secret fields appear only as
// `<key>_set` booleans (the server never returns secrets), list fields are
// arrays of entry objects in the same masked form.
export type ServiceCredentialForm = Record<string, string | boolean | SchemaFormItem[]>;

export interface ServiceCredentialDetail extends ServiceCredentialSummary {
  fields: CredentialFieldSchema[];
  credentials: ServiceCredentialForm;
}

export interface ServiceCredentialsListResponse {
  services: ServiceCredentialDetail[];
}

// Generic PUT body: flat {field key: value}. An empty secret value keeps
// the currently stored one; a list field is sent as its full entry array.
export type ServiceCredentialUpdate = Record<string, string | boolean | SchemaFormItem[]>;

// Admin inference providers (LLM backend credentials/config)

// Where the Google credentials for Vertex were detected from
export interface VertexCredentialsInfo {
  // "env" = GOOGLE_APPLICATION_CREDENTIALS, "gcloud_adc" = gcloud
  // Application Default Credentials file, null = nothing detected
  source: 'env' | 'gcloud_adc' | null;
  key_path: string | null;
  service_account_email: string | null;
  project_id: string | null;
  problem: string | null;
}

// One Vertex config section (Claude on Vertex / Gemini on Vertex)
export interface VertexSectionInfo {
  configured: boolean;
  vertex_project_id: string;
  vertex_region: string;
  // Which layer the project id came from; "anthropic_fallback" means the
  // Gemini section inherited the Anthropic project id
  project_source: 'env' | 'server_config' | 'anthropic_fallback' | null;
}

// Latest stored health verdict for one model (startup sweep or admin recheck)
export interface InferenceModelStatus {
  ok: boolean;
  error: string | null;
  checked_at: string;
}

// One model row on a provider card
export interface InferenceModelInfo {
  // Stored (qualified) id: bare for Vertex, "<instance>:<wire_id>" for
  // instance-served models
  id: string;
  // The model string sent in API calls -- the primary label in the UI
  wire_id: string;
  display_name: string;
  // Vertex models are split into independently-configured families;
  // null for instance-served models
  family: 'anthropic' | 'gemini_vertex' | null;
  // Admin toggle: disabled models are hidden from the picker and never
  // health-checked
  enabled: boolean;
  // Context window (tokens) the app assumes for the model; for Ollama
  // models this is also the num_ctx requested per call
  max_input_tokens: number;
  // Provider-announced shutdown date ("YYYY-MM-DD", Vertex models only);
  // the server stops listing the model on that date
  discontinued_on: string | null;
  // NEAR AI models only: where the model runs per NEAR AI's catalog -- in
  // NEAR's own hardware-attested TEE fleet or relayed to a third party.
  // Informational badge, enforced nowhere. null elsewhere.
  tee: TeeLabel | null;
  // null when the model has never been health-checked
  status: InferenceModelStatus | null;
}

export type TeeLabel = 'enclave' | 'proxy';

// Result of a live per-model health recheck (also the newly stored status)
export interface InferenceModelTestResult extends InferenceModelStatus {
  model: string;
}

export interface VertexProviderStatus {
  provider: 'vertex';
  label: string;
  kind: 'detected';
  configured: boolean;
  detail: {
    credentials: VertexCredentialsInfo;
    anthropic: VertexSectionInfo;
    gemini_vertex: VertexSectionInfo;
    configured: boolean;
  };
  models: InferenceModelInfo[];
}

// One admin-configured provider instance (an OpenRouter, Fireworks AI or
// NEAR AI / TrustedRouter configuration, or a self-hosted inference server)
export interface InferenceInstanceStatus {
  id: string;
  kind: string;
  kind_label: string;
  label: string;
  // Usable at all: a stored key (OpenRouter, Fireworks, NEAR AI, TrustedRouter) or a
  // server URL (self-hosted)
  configured: boolean;
  // "store" = key file in the data directory, null = no key yet
  source: 'store' | null;
  credentials: { api_key_set: boolean };
  hint: string;
  // false for self-hosted servers, whose key is optional
  key_required: boolean;
  // Self-hosted endpoints only; null on fixed-upstream kinds
  base_url: string | null;
  api_type: string | null;
  models: InferenceModelInfo[];
}

// One API type a self-hosted instance can speak
export interface InferenceApiType {
  id: string;
  label: string;
  description: string;
}

// Where a kind's "Add model" typeahead looks: the shared OpenRouter
// catalog, the instance's own key-fetched vendor catalog (Anthropic,
// OpenAI, Gemini, Fireworks), NEAR AI's or TrustedRouter's public model
// list, or the self-hosted server's live model list
export type InferenceCatalogSource =
  | 'openrouter' | 'fireworks' | 'nearai' | 'trustedrouter'
  | 'anthropic' | 'openai' | 'gemini' | 'server';

// Catalogs read with the instance's own key (the typeahead waits for a
// saved key)
export const KEY_FETCHED_CATALOGS: ReadonlySet<InferenceCatalogSource> = new Set([
  'fireworks', 'anthropic', 'openai', 'gemini',
]);

export interface InferenceInstanceKind {
  kind: string;
  label: string;
  // Carries its own base URL + API type (self-hosted server)
  endpoint: boolean;
  catalog: InferenceCatalogSource;
  // API-key input placeholder ("sk-ant-...")
  hint: string;
  // false for self-hosted servers, whose key is optional
  key_required: boolean;
}

// POST /admin/inference-providers/quick-setup -- the composer's first-run
// model setup card: connect a provider from one credential
export interface InferenceQuickSetupRequest {
  kind: string;
  api_key?: string;
  // Self-hosted kind only
  base_url?: string;
  api_type?: string;
}

export interface InferenceQuickSetupResult {
  instance: InferenceInstanceStatus;
  // Qualified ids of the models added (empty when the key was rejected or
  // the kind has nothing to suggest)
  added: string[];
  // Live health verdict of the first suggested model; null when nothing
  // was suggested
  check: InferenceModelTestResult | null;
  // The catalog fetch error, if any (a rejected key shows up here too)
  catalog_error: string | null;
}

export interface InferenceProvidersListResponse {
  vertex: VertexProviderStatus;
  instances: InferenceInstanceStatus[];
  // Instance kinds an admin can add
  kinds: InferenceInstanceKind[];
  api_types: InferenceApiType[];
}

export interface VertexModelsUpdate {
  // Full replacement of the disabled set (Vertex model ids)
  disabled_models: string[];
}

export interface InferenceInstanceCreate {
  kind: string;
  label?: string;
}

export interface InferenceInstanceUpdate {
  label?: string;
  // Empty string keeps the currently stored key
  api_key?: string;
  // Self-hosted endpoints only
  base_url?: string;
  api_type?: string;
  // Full replacement of the model list (wire ids), in display order; the
  // optional name / context_length override the stored snapshot
  models?: { id: string; enabled: boolean; name?: string; context_length?: number }[];
}

// One catalog entry (typeahead candidate) -- the OpenRouter row shape,
// shared by the Fireworks and NEAR AI catalogs and self-hosted discovery
export interface OpenRouterCatalogModel {
  id: string;
  name: string;
  context_length: number | null;
  max_completion_tokens: number | null;
  pricing: { prompt: number; completion: number; cache_read?: number } | null;
  // Self-hosted discovery / Fireworks / NEAR AI only: a short summary
  // (family, size, quantization / "vision · reasoning") and capabilities
  // (null = the catalog publishes none, so nothing is claimed)
  detail?: string;
  capabilities?: string[] | null;
  // NEAR AI only: enclave / proxy (see InferenceModelInfo.tee)
  tee?: TeeLabel;
}

// An instance's own catalog: the models a self-hosted server reports right
// now (not cached), the Fireworks serverless catalog or NEAR AI's public
// model list (both cached)
export interface InstanceCatalogResponse {
  models: OpenRouterCatalogModel[];
  error: string | null;
}

export interface OpenRouterCatalogResponse {
  models: OpenRouterCatalogModel[];
  fetched_at: number | null;
  stale: boolean;
  error: string | null;
}

// One known model as reported by GET /app/api/config `models` (every model
// incl. deprecated / admin-disabled ones, for labelling old conversations)
export interface AppModelInfo {
  id: string;
  display_name: string;
  provider: string;
  provider_label: string;
  max_input_tokens: number;
  deprecated: boolean;
  // Thinking levels the model menu offers as a sublevel under the model
  // (empty = none; "none" = thinking off), and the level the model runs at
  // when the conversation picks none (null = provider default, unnamed).
  thinking_efforts: string[];
  default_thinking_effort: string | null;
  // Admin Model Selection settings (config/model_selection.py): the
  // composer menu's top-level slot for private and for public-project
  // conversations (1..max, null = "All models" only), the free-text label
  // shown for a slotted model, the thinking level a slotted pick is fixed
  // to (null = the model's default), and whether the model may be used in
  // private / public-project conversations.
  slot: number | null;
  public_slot: number | null;
  descriptor: string;
  slot_effort: string | null;
  allow_private: boolean;
  allow_public: boolean;
}

// One row of the admin Settings > Model Selection table
export interface ModelSelectionRow {
  id: string;
  wire_id: string;
  display_name: string;
  provider_label: string;
  instance_id: string | null;
  // Offerable right now: credentials configured and no failing health verdict
  available: boolean;
  unavailable_reason: 'not_configured' | 'failing' | null;
  // Levels the slot-effort dropdown offers (empty = no thinking sublevel)
  // and the one the model runs at by default.
  thinking_efforts: string[];
  default_thinking_effort: string | null;
  slot: number | null;
  public_slot: number | null;
  descriptor: string;
  slot_effort: string | null;
  allow_private: boolean;
  allow_public: boolean;
}

export interface ModelSelectionListResponse {
  max_slots: number;
  max_descriptor_length: number;
  models: ModelSelectionRow[];
}

// Full replacement: unlisted models are reset to unset
export interface ModelSelectionUpdate {
  models: {
    id: string;
    slot: number | null;
    public_slot: number | null;
    descriptor: string;
    slot_effort: string | null;
    allow_private: boolean;
    allow_public: boolean;
  }[];
}

// Schedule types
export type RoutineScheduleType = 'daily' | 'weekly' | 'hourly' | 'every_n_minutes';

// One row of the schedule run ledger (GET .../schedule `recent_runs`)
export interface RoutineScheduleRun {
  id: string;
  schedule_id: string;
  occurrence_at: string;
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'missed';
  attempt: number;
  conversation_id: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RoutineSchedule {
  id: string;
  routine_id: string;
  user_id: number;
  schedule_type: RoutineScheduleType;
  daily_time_utc: string | null;
  daily_time_local: string | null;
  timezone: string | null;
  // Weekly only: weekday numbers, 0=Monday .. 6=Sunday
  weekly_days: number[] | null;
  hourly_minute: number | null;
  interval_minutes: number | null;
  is_enabled: boolean;
  next_due_at: string | null;
  last_run_started_at: string | null;
  last_run_completed_at: string | null;
  is_running: boolean;
  last_conversation_id: string | null;
  created_at: string;
  updated_at: string | null;
  // Present on GET .../schedule only
  recent_runs?: RoutineScheduleRun[];
}

// Summary included in routine list responses (subset of full RoutineSchedule)
export interface RoutineScheduleSummary {
  id: string;
  schedule_type: RoutineScheduleType;
  daily_time_local: string | null;
  timezone: string | null;
  weekly_days: number[] | null;
  next_due_at: string | null;
  hourly_minute: number | null;
  interval_minutes: number | null;
  is_enabled: boolean;
  is_running: boolean;
  last_run_completed_at: string | null;
}

// Settings > Sign-in (admin): GET /admin/sign-in
export interface AdminSignInStatus {
  login_method: 'google' | 'password';
  google_oauth_configured: boolean;
  smtp_configured: boolean;
}

// POST /admin/sign-in/password-links
export interface PasswordLinkResult {
  email: string;
  url: string;
  account_exists: boolean;
  emailed: boolean;
  email_error: string | null;
  added_to_allowed_emails: boolean;
}

// POST /auth/password/link-info
export interface PasswordLinkInfo {
  email: string;
  purpose: 'invite' | 'reset';
  account_exists: boolean;
  name: string;
}
