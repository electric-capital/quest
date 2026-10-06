export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  last_message_at: string;
  project_id?: string;
  routine_id?: string | null;
  model?: string | null;
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
  | ErrorMessage | InterruptedMessage | ActionRequestMessage | CompactionMessage
  | ModelFallbackMessage;

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
       | 'stats' | 'action_request'
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
  kind: 'oauth' | 'api_key';
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

export interface MemoriesListResponse {
  memories: Memory[];
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

/** A user doc picks its mode; a project doc mirrors its project's `public` flag. */
export type DocMode = 'private' | 'public';

/** `project` when the doc belongs to a project (`project_id` set), else `user`. */
export type DocScope = 'user' | 'project';

/** What the UI may offer for this doc, computed server-side per viewer. */
export interface DocAccess {
  can_rename: boolean;
  can_switch_mode: boolean;
  can_delete: boolean;
  // The UI write verdict: 'free' for the owner or a write share.
  write: 'free' | 'approval' | 'denied';
}

/** One share-roster entry; `user_id` null means everyone. Owner only. */
export interface DocShare {
  id: number;
  user_id: number | null;
  permission: 'read' | 'write';
  created_at: string;
}

/** Doc row as returned by the list / create / rename / mode endpoints. */
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
  // Naive-UTC ISO strings (no 'Z'; parse with parseUTCTimestamp).
  // `updated_at` doubles as the optimistic-concurrency token.
  created_at: string;
  updated_at: string;
  scope: DocScope;
  shared: boolean;
  access: DocAccess;
  // Present for the owner only.
  shares?: DocShare[];
}

/** GET /docs/{id}: the row plus the whole markdown body. */
/** The conversation named by `last_write_source`, for the viewer footer. */
export interface DocWriteConversation {
  id: string;
  title: string;
  project_id: string | null;
}

export interface DocDetail extends Doc {
  content: string;
  /**
   * Resolved by GET /docs/{id} when `last_write_source` is a conversation
   * the owner still has; null otherwise (deleted, not a conversation, or
   * blanked for non-owners together with `last_write_source`).
   */
  last_write_conversation: DocWriteConversation | null;
}

export interface ListDocsResponse {
  docs: Doc[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface CreateDocRequest {
  title: string;
  description?: string;
  // User docs only; a project doc always takes its project's mode.
  mode?: DocMode;
  project_id?: string | null;
}

export interface UpdateDocRequest {
  title?: string;
  description?: string;
  // Optimistic-concurrency token (the row's updated_at as loaded). A
  // mismatch rejects with a flat 409 stale_update carrying `current`.
  expected_updated_at?: string;
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

export interface CredentialFieldSchema {
  key: string;
  label: string;
  type: 'text' | 'secret' | 'bool' | 'textarea';
  placeholder: string;
  required: boolean;
  // Key of a sibling bool field; when set, the field is required iff that
  // toggle is on (takes precedence over `required`).
  required_if: string | null;
  // Key of a sibling bool field that must be on for the field to render.
  visible_if: string | null;
}

// Masked form values keyed by field key: text/textarea fields round-trip
// verbatim, bool fields are booleans, secret fields appear only as
// `<key>_set` booleans (the server never returns secrets).
export type ServiceCredentialForm = Record<string, string | boolean>;

export interface ServiceCredentialDetail extends ServiceCredentialSummary {
  fields: CredentialFieldSchema[];
  credentials: ServiceCredentialForm;
}

export interface ServiceCredentialsListResponse {
  services: ServiceCredentialDetail[];
}

// Generic PUT body: flat {field key: value}. An empty secret value keeps
// the currently stored one.
export type ServiceCredentialUpdate = Record<string, string | boolean>;

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
  // null when the model has never been health-checked
  status: InferenceModelStatus | null;
}

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

// One admin-configured provider instance (an OpenRouter configuration or a
// self-hosted inference server)
export interface InferenceInstanceStatus {
  id: string;
  kind: string;
  kind_label: string;
  label: string;
  // Usable at all: a stored key (OpenRouter) or a server URL (self-hosted)
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

export interface InferenceProvidersListResponse {
  vertex: VertexProviderStatus;
  instances: InferenceInstanceStatus[];
  // Instance kinds an admin can add (`endpoint` = carries its own base URL)
  kinds: { kind: string; label: string; endpoint: boolean }[];
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

// One OpenRouter catalog entry (typeahead candidate)
export interface OpenRouterCatalogModel {
  id: string;
  name: string;
  context_length: number | null;
  max_completion_tokens: number | null;
  pricing: { prompt: number; completion: number; cache_read?: number } | null;
  // Self-hosted discovery only: server-reported summary (family, size,
  // quantization) and capabilities (e.g. "tools")
  detail?: string;
  capabilities?: string[] | null;
}

// Models a self-hosted instance's server reports right now (not cached)
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
  // Admin Model Selection settings (config/model_selection.py): the
  // composer menu's top-level slot for private and for public-project
  // conversations (1..max, null = "All models" only), the free-text label
  // shown for a slotted model, and whether the model may be used in
  // private / public-project conversations. While public mode (the
  // public_projects gate) is off the server reports public_slot as null
  // and both flags as true.
  slot: number | null;
  public_slot: number | null;
  descriptor: string;
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
  // Stored values, unmasked even while public mode is off
  slot: number | null;
  public_slot: number | null;
  descriptor: string;
  allow_private: boolean;
  allow_public: boolean;
}

export interface ModelSelectionListResponse {
  max_slots: number;
  max_descriptor_length: number;
  // The public_projects gate is on for anyone: the table shows the public
  // menu slot, the Private/Public columns and the public menu preview
  public_mode_enabled: boolean;
  models: ModelSelectionRow[];
}

// Full replacement: unlisted models are reset to unset
export interface ModelSelectionUpdate {
  models: {
    id: string;
    slot: number | null;
    public_slot: number | null;
    descriptor: string;
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
