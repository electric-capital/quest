/**
 * API client for chat application
 *
 * Every function here is one call to the shared JSON wrapper in request.ts
 * (session-cookie authed, ApiClientError on non-2xx, parsed JSON body back).
 */

import { endpoints, API_BASE_URL } from './config';
import { apiGet, apiPost, apiPut, apiDelete, handleErrorResponse } from './request';
import type {
  ListConversationsResponse,
  CreateConversationResponse,
  CompactResult,
  Conversation,
  ConversationDetail,
  SettingsResponse,
  UserSettings,
  ConnectorsResponse,
  SmsTemplate,
  SmsTemplatesResponse,
  LogoutResponse,
  Memory,
  MemoriesListResponse,
  Guide,
  GuidesListResponse,
  Skill,
  SkillsListResponse,
  SkillSharesResponse,
  UserSearchResult,
  UserSearchResponse,
  Project,
  ProjectDocSourcesResponse,
  ProjectsListResponse,
  Routine,
  RoutineCostReport,
  RoutinesListResponse,
  RoutineSchedule,
  ActionRequest,
  EnrichedActionRequestsListResponse,
  ActionRequestCountsResponse,
  SearchResponse,
  VersionResponse,
  LatestActiveConversationsResponse,
  MostExpensiveConversationsResponse,
  AdminUserReportResponse,
  AdminModelReportResponse,
  AdminGuidesReportResponse,
  AdminUsageReport,
  AdminSignInStatus,
  FeatureGate,
  FeatureGatesListResponse,
  FeatureGateUpdate,
  TranscribeResponse,
  PasswordLinkInfo,
  PasswordLinkResult,
  ServiceCredentialsListResponse,
  ServiceCredentialDetail,
  ServiceCredentialUpdate,
  InferenceProvidersListResponse,
  InferenceInstanceCreate,
  InferenceInstanceStatus,
  InferenceInstanceUpdate,
  InstanceCatalogResponse,
  OpenRouterCatalogResponse,
  VertexModelsUpdate,
  VertexProviderStatus,
  InferenceModelTestResult,
  ModelSelectionListResponse,
  ModelSelectionUpdate,
  InferenceApiKeysListResponse,
  CreatedInferenceApiKey,
} from './types';

export { ApiClientError } from './request';

interface FetchConversationsOptions {
  includeArchived?: boolean;
  /** Drop project-linked conversations server-side (top-level sidebar). */
  excludeProjects?: boolean;
  /** Include origin="slack" conversations (default true server-side). */
  includeSlack?: boolean;
  /** Include origin="inference_api" conversations (default true server-side). */
  includeInference?: boolean;
  /** Page size; omitting it returns the full (unpaged) list. */
  limit?: number;
  /** Opaque next_cursor from a previous page response. */
  cursor?: string;
}

/**
 * Fetch conversations for the authenticated user. With no options this
 * returns the full list; the sidebar passes filters + limit for a paged
 * load (response then carries has_more/next_cursor).
 */
export function fetchConversations(
  options: FetchConversationsOptions = {}
): Promise<ListConversationsResponse> {
  return apiGet(endpoints.conversations(), {
    query: {
      include_archived: options.includeArchived || undefined,
      exclude_projects: options.excludeProjects || undefined,
      include_slack: options.includeSlack === false ? false : undefined,
      include_inference: options.includeInference === false ? false : undefined,
      limit: options.limit,
      cursor: options.cursor || undefined,
    },
  });
}

/**
 * Create a new conversation
 */
export function createConversation(): Promise<CreateConversationResponse> {
  return apiPost(endpoints.conversations());
}

/**
 * Create a new conversation seeded with a copy of another conversation's workspace files
 */
export function duplicateConversationWorkspace(
  conversationId: string
): Promise<CreateConversationResponse> {
  return apiPost(endpoints.duplicateWorkspace(conversationId));
}

/**
 * Compact a conversation's model-facing history: the server summarizes the
 * older messages with a one-off LLM call and keeps recent messages verbatim.
 */
export function compactConversation(conversationId: string): Promise<CompactResult> {
  return apiPost(endpoints.compactConversation(conversationId));
}

/**
 * Fetch a specific conversation by ID
 */
export function fetchConversation(id: string): Promise<ConversationDetail> {
  return apiGet(endpoints.conversation(id));
}

/**
 * Fetch only the messages newer than ``afterSeq`` for a conversation.
 *
 * Used by the persistent-WS client after it receives a ``message_appended``
 * event, to incrementally append the new tail without re-downloading the
 * entire conversation history.
 */
export function fetchConversationTail(
  id: string,
  afterSeq: number,
): Promise<{ messages: Array<Record<string, unknown>>; last_message_seq: number }> {
  return apiGet(`${endpoints.conversation(id)}/tail`, { query: { after_seq: afterSeq } });
}

/**
 * Fetch current user's settings
 */
export function fetchSettings(): Promise<SettingsResponse> {
  return apiGet(endpoints.settings());
}

/**
 * Update current user's settings
 */
export function updateSettings(settings: Partial<UserSettings>): Promise<SettingsResponse> {
  return apiPut(endpoints.settings(), { body: settings });
}

const SMS_TEMPLATES_URL = '/auth/twilio/templates';

/**
 * Twilio plugin: the user's pre-written SMS messages + trusted-channel state.
 * Plugin-owned routes (session-cookie authed), not part of /app/api.
 */
export function fetchSmsTemplates(): Promise<SmsTemplatesResponse> {
  return apiGet(SMS_TEMPLATES_URL);
}

export function updateSmsTemplates(templates: SmsTemplate[]): Promise<SmsTemplatesResponse> {
  return apiPut(SMS_TEMPLATES_URL, { body: { templates } });
}

/**
 * Fetch connector status for all OAuth services
 */
export function fetchConnectors(): Promise<ConnectorsResponse> {
  return apiGet(endpoints.connectors());
}

/**
 * Logout (clear session cookie, preserve data)
 */
export function logout(): Promise<LogoutResponse> {
  return apiPost(`${API_BASE_URL}/logout`);
}

/**
 * Logout and disconnect all OAuth services
 */
export function logoutAndDisconnect(): Promise<LogoutResponse> {
  return apiPost(`${API_BASE_URL}/logout-and-disconnect`);
}

/**
 * Delete account and all associated data
 */
export function deleteAccount(): Promise<LogoutResponse> {
  return apiPost(`${API_BASE_URL}/delete-account`);
}

// Memory API functions

export function fetchMemories(includeArchived: boolean = false): Promise<MemoriesListResponse> {
  return apiGet(endpoints.memories(), {
    query: { include_archived: includeArchived || undefined },
  });
}

export function createMemory(content: string): Promise<Memory> {
  return apiPost(endpoints.memories(), { body: { content } });
}

export function updateMemory(memoryId: string, content: string): Promise<Memory> {
  return apiPut(endpoints.memory(memoryId), { body: { content } });
}

export function archiveMemory(memoryId: string): Promise<Memory> {
  return apiPut(`${endpoints.memory(memoryId)}/archive`);
}

export function unarchiveMemory(memoryId: string): Promise<Memory> {
  return apiPut(`${endpoints.memory(memoryId)}/unarchive`);
}

export function deleteMemory(memoryId: string): Promise<{ success: boolean }> {
  return apiDelete(endpoints.memory(memoryId));
}

// Inference API key functions (Settings > Inference API)

export function fetchInferenceApiKeys(): Promise<InferenceApiKeysListResponse> {
  return apiGet(endpoints.inferenceApiKeys());
}

export function createInferenceApiKey(name: string): Promise<CreatedInferenceApiKey> {
  return apiPost(endpoints.inferenceApiKeys(), { body: { name } });
}

export function deleteInferenceApiKey(keyId: string): Promise<{ success: boolean }> {
  return apiDelete(endpoints.inferenceApiKey(keyId));
}

// Guide API functions

export function fetchGuides(): Promise<GuidesListResponse> {
  return apiGet(endpoints.guides());
}

export function convertGuideToSkill(
  guideId: string,
): Promise<{ skill: Skill; autoload_enabled: boolean }> {
  return apiPost(`${endpoints.guide(guideId)}/convert-to-skill`);
}

export function updateGuide(
  guideId: string,
  updates: { name?: string; content?: string },
): Promise<Guide> {
  return apiPut(endpoints.guide(guideId), { body: updates });
}

export function deleteGuide(guideId: string): Promise<{ success: boolean }> {
  return apiDelete(endpoints.guide(guideId));
}

// Skill API functions

export function fetchSkills(owned?: boolean, visibility?: string): Promise<SkillsListResponse> {
  return apiGet(endpoints.skills(), {
    query: { owned: owned || undefined, visibility: visibility || undefined },
  });
}

export function createSkill(data: {
  name: string;
  description?: string;
  content: string;
  visibility?: string;
}): Promise<Skill> {
  return apiPost(endpoints.skills(), { body: data });
}

export function updateSkill(
  skillId: string,
  updates: { name?: string; description?: string; content?: string; visibility?: string },
): Promise<Skill> {
  return apiPut(endpoints.skill(skillId), { body: updates });
}

export function deleteSkill(skillId: string): Promise<{ success: boolean }> {
  return apiDelete(endpoints.skill(skillId));
}

export function fetchAutoloadedSkillIds(): Promise<{ skill_ids: string[] }> {
  return apiGet(endpoints.skillsAutoloaded());
}

export function setSkillAutoload(skillId: string, enabled: boolean): Promise<{ success: boolean }> {
  return apiPut(endpoints.skillAutoload(skillId), { body: { enabled } });
}

export function fetchSharedWithMeSkills(): Promise<SkillsListResponse> {
  return apiGet(endpoints.skillsSharedWithMe());
}

export function fetchSkillShares(skillId: string): Promise<SkillSharesResponse> {
  return apiGet(endpoints.skillShares(skillId));
}

export function addSkillShares(skillId: string, emails: string[]): Promise<SkillSharesResponse> {
  return apiPost(endpoints.skillShares(skillId), { body: { emails } });
}

export function removeSkillShare(skillId: string, userId: number): Promise<{ success: boolean }> {
  return apiDelete(endpoints.skillShare(skillId, userId));
}

// Project skill API functions

export function fetchProjectSkills(projectId: string): Promise<SkillsListResponse> {
  return apiGet(endpoints.projectSkills(projectId));
}

export function createProjectSkill(
  projectId: string,
  data: { name: string; description?: string; content: string },
): Promise<Skill> {
  return apiPost(endpoints.projectSkills(projectId), { body: data });
}

export function updateProjectSkill(
  projectId: string,
  skillId: string,
  updates: { name?: string; description?: string; content?: string },
): Promise<Skill> {
  return apiPut(endpoints.projectSkill(projectId, skillId), { body: updates });
}

export function deleteProjectSkill(
  projectId: string,
  skillId: string,
): Promise<{ success: boolean }> {
  return apiDelete(endpoints.projectSkill(projectId, skillId));
}

export function fetchProjectAutoloadedSkillIds(
  projectId: string,
): Promise<{ skill_ids: string[] }> {
  return apiGet(endpoints.projectSkillsAutoloaded(projectId));
}

export function setProjectSkillAutoload(
  projectId: string,
  skillId: string,
  enabled: boolean,
): Promise<{ success: boolean }> {
  return apiPut(endpoints.projectSkillAutoload(projectId, skillId), { body: { enabled } });
}

export function fetchRoutineAutoloadedSkillIds(
  projectId: string,
  routineId: string,
): Promise<{ skill_ids: string[] }> {
  return apiGet(endpoints.routineSkillsAutoloaded(projectId, routineId));
}

export function setRoutineSkillAutoload(
  projectId: string,
  routineId: string,
  skillId: string,
  enabled: boolean,
): Promise<{ success: boolean }> {
  return apiPut(endpoints.routineSkillAutoload(projectId, routineId, skillId), {
    body: { enabled },
  });
}

export function fetchConversationLoadedSkills(
  conversationId: string,
): Promise<{ skill_ids: string[] }> {
  return apiGet(endpoints.conversationLoadedSkills(conversationId));
}

export function fetchSystemPrompt(
  conversationId: string,
): Promise<{ system_prompt: string | null }> {
  return apiGet(endpoints.conversationSystemPrompt(conversationId));
}

export function searchUsers(query: string): Promise<UserSearchResponse> {
  return apiGet(endpoints.userSearch(query));
}

/**
 * Save an api_key-kind connector's key. The row's key_url/key_field come
 * from GET /connectors, so new (e.g. plugin-provided) connectors need no
 * dedicated client function.
 */
export async function saveConnectorKey(
  keyUrl: string,
  keyField: string,
  key: string,
): Promise<void> {
  await apiPost(keyUrl, { body: { [keyField]: key } });
}

/**
 * Disconnect a connector via the row's disconnect_url from GET /connectors.
 */
export async function disconnectConnector(disconnectUrl: string): Promise<void> {
  await apiPost(disconnectUrl);
}

// Project API functions

/**
 * Fetch the user's projects. Archived projects are left out unless
 * `includeArchived` is set (the sidebar fetches everything and filters
 * client-side so a drilled-into archived project still resolves).
 */
export function fetchProjects(
  options: { includeArchived?: boolean } = {},
): Promise<ProjectsListResponse> {
  return apiGet(endpoints.projects(), {
    query: { include_archived: options.includeArchived || undefined },
  });
}

export function createProject(name: string, isPublic: boolean = false): Promise<Project> {
  return apiPost(endpoints.projects(), { body: { name, public: isPublic } });
}

/**
 * Create a new project from an existing standalone conversation. The
 * conversation's workspace files move into the project workspace and the
 * conversation becomes the project's first conversation.
 */
export function createProjectFromConversation(
  conversationId: string,
  name: string,
): Promise<Project> {
  return apiPost(endpoints.projectFromConversation(), {
    body: { name, conversation_id: conversationId },
  });
}

export function fetchProject(projectId: string): Promise<Project> {
  return apiGet(endpoints.project(projectId));
}

export function updateProject(
  projectId: string,
  updates: { name?: string; guide?: string },
): Promise<Project> {
  return apiPut(endpoints.project(projectId), { body: updates });
}

export function deleteProject(projectId: string): Promise<{ success: boolean }> {
  return apiDelete(endpoints.project(projectId));
}

/**
 * Archive a project: hidden from the default sidebar list, its scheduled
 * routines paused, nothing deleted.
 */
export function archiveProject(projectId: string): Promise<Project> {
  return apiPut(`${endpoints.project(projectId)}/archive`);
}

/**
 * Restore an archived project.
 */
export function unarchiveProject(projectId: string): Promise<Project> {
  return apiPut(`${endpoints.project(projectId)}/unarchive`);
}

/**
 * The public projects whose Quest Docs this (private) project's
 * conversations may read (Project Settings > Docs Access).
 */
export function fetchProjectDocSources(projectId: string): Promise<ProjectDocSourcesResponse> {
  return apiGet(`${endpoints.project(projectId)}/doc-sources`);
}

/**
 * Replace the project's doc sources with the given public project ids
 * (full replacement; an empty list removes every source).
 */
export function updateProjectDocSources(
  projectId: string,
  sourceProjectIds: string[],
): Promise<ProjectDocSourcesResponse> {
  return apiPut(`${endpoints.project(projectId)}/doc-sources`, {
    body: { source_project_ids: sourceProjectIds },
  });
}

export function fetchProjectConversations(
  projectId: string,
  includeArchived: boolean = false,
): Promise<ListConversationsResponse> {
  return apiGet(endpoints.projectConversations(projectId), {
    query: { include_archived: includeArchived || undefined },
  });
}

export function createProjectConversation(
  projectId: string,
  routineId?: string,
): Promise<CreateConversationResponse> {
  return apiPost(endpoints.projectConversations(projectId), {
    body: routineId ? { routine_id: routineId } : undefined,
  });
}

/**
 * Archive a conversation (soft-delete from sidebar)
 */
export function archiveConversation(conversationId: string): Promise<Conversation> {
  return apiPut(`${endpoints.conversation(conversationId)}/archive`);
}

/**
 * Unarchive a conversation (restore to sidebar)
 */
export function unarchiveConversation(conversationId: string): Promise<Conversation> {
  return apiPut(`${endpoints.conversation(conversationId)}/unarchive`);
}

/**
 * Rename a conversation (set or clear a custom display name)
 */
export function renameConversation(
  conversationId: string,
  customName: string | null,
): Promise<Conversation> {
  return apiPut(`${endpoints.conversation(conversationId)}/rename`, {
    body: { custom_name: customName },
  });
}

// Routine API functions

export function fetchProjectRoutines(projectId: string): Promise<RoutinesListResponse> {
  return apiGet(endpoints.projectRoutines(projectId));
}

export function fetchRoutine(projectId: string, routineId: string): Promise<Routine> {
  return apiGet(endpoints.projectRoutine(projectId, routineId));
}

/**
 * Inference cost report for one routine (Routine Settings > Costs): rolling
 * 7/28-day totals with the preceding period, lifetime total, recent runs.
 */
export function fetchRoutineCosts(
  projectId: string,
  routineId: string,
): Promise<RoutineCostReport> {
  return apiGet(endpoints.routineCosts(projectId, routineId));
}

export function createRoutine(
  projectId: string,
  data: { name: string; prompt: string; guide_id?: string | null; model?: string | null },
): Promise<Routine> {
  return apiPost(endpoints.projectRoutines(projectId), { body: data });
}

export function updateRoutine(
  projectId: string,
  routineId: string,
  updates: {
    name?: string;
    prompt?: string;
    guide_id?: string | null;
    clear_guide?: boolean;
    model?: string | null;
    clear_model?: boolean;
    // Optimistic-concurrency token: the routine's updated_at ISO string at
    // the time the caller loaded the row. If it no longer matches on the
    // server, the call rejects with a 409 stale_update ApiClientError.
    expected_updated_at?: string | null;
  },
): Promise<Routine> {
  return apiPut(endpoints.projectRoutine(projectId, routineId), { body: updates });
}

export function deleteRoutine(
  projectId: string,
  routineId: string,
): Promise<{ success: boolean }> {
  return apiDelete(endpoints.projectRoutine(projectId, routineId));
}

// Schedule API functions

export function fetchRoutineSchedule(
  projectId: string,
  routineId: string,
): Promise<RoutineSchedule | null> {
  // 404 means the routine has no schedule yet, not an error.
  return apiGet(endpoints.routineSchedule(projectId, routineId), { nullOn: [404] });
}

export function createRoutineSchedule(
  projectId: string,
  routineId: string,
  data: {
    schedule_type: string;
    daily_time_local?: string;
    timezone?: string;
    weekly_days?: number[];
    hourly_minute?: number;
    interval_minutes?: number;
  },
): Promise<RoutineSchedule> {
  return apiPost(endpoints.routineSchedule(projectId, routineId), { body: data });
}

export function updateRoutineSchedule(
  projectId: string,
  routineId: string,
  updates: {
    schedule_type?: string;
    daily_time_local?: string;
    timezone?: string;
    weekly_days?: number[];
    hourly_minute?: number;
    interval_minutes?: number;
    is_enabled?: boolean;
    // Optimistic-concurrency token: the schedule's updated_at ISO string at
    // the time the caller loaded the row. If it no longer matches on the
    // server, the call rejects with a 409 stale_update ApiClientError.
    expected_updated_at?: string | null;
  },
): Promise<RoutineSchedule> {
  return apiPut(endpoints.routineSchedule(projectId, routineId), { body: updates });
}

// Action Request API functions

export function resolveActionRequest(
  requestId: number,
  action: 'execute' | 'deny' | 'stop',
  feedback?: string,
): Promise<ActionRequest> {
  // Strip whitespace and only send `feedback` when there's actual text
  // and the action is "deny" (Revise) -- the backend ignores feedback on
  // execute and stop, but keeping the wire shape clean avoids future
  // ambiguity.
  const trimmed = feedback?.trim();
  const body: { action: string; feedback?: string } = { action };
  if (action === 'deny' && trimmed) {
    body.feedback = trimmed;
  }
  return apiPost(endpoints.actionRequestResolve(requestId), { body });
}

export function fetchActionRequestsEnriched(
  status?: 'open' | 'executed' | 'denied' | 'stopped',
): Promise<EnrichedActionRequestsListResponse> {
  return apiGet(endpoints.actionRequests(), {
    query: { include_context: true, status },
  });
}

export function fetchActionRequestCounts(): Promise<ActionRequestCountsResponse> {
  return apiGet(endpoints.actionRequestsCounts());
}

// Admin API functions

export function triggerAdminShutdown(): Promise<{ status: string; message: string }> {
  return apiPost(endpoints.adminShutdown());
}

export function fetchAdminUsers(
  // The impersonation picker excludes the requesting admin (default); the
  // feature-gate access picker includes them so admins can grant themselves.
  includeSelf = false,
): Promise<{ users: UserSearchResult[] }> {
  return apiGet(endpoints.adminUsers(includeSelf));
}

export function impersonateUser(userId: number): Promise<{ success: boolean }> {
  return apiPost(endpoints.adminImpersonate(), { body: { user_id: userId } });
}

export function stopImpersonation(): Promise<{ success: boolean }> {
  return apiPost(endpoints.adminStopImpersonation());
}

export function fetchLatestActiveConversations(
  limit?: number,
  includeRoutines?: boolean,
): Promise<LatestActiveConversationsResponse> {
  return apiGet(endpoints.adminLatestActiveConversations(), {
    query: {
      limit,
      include_routines: includeRoutines === false ? false : undefined,
    },
  });
}

export function fetchMostExpensiveConversations(
  start?: string,
  end?: string,
  limit?: number,
): Promise<MostExpensiveConversationsResponse> {
  return apiGet(endpoints.adminMostExpensiveConversations(), {
    query: { start: start || undefined, end: end || undefined, limit },
  });
}

export function fetchAdminUserReport(
  start?: string,
  end?: string,
): Promise<AdminUserReportResponse> {
  return apiGet(endpoints.adminUserReport(), {
    query: { start: start || undefined, end: end || undefined },
  });
}

export function fetchAdminModelReport(
  start?: string,
  end?: string,
): Promise<AdminModelReportResponse> {
  return apiGet(endpoints.adminModelReport(), {
    query: { start: start || undefined, end: end || undefined },
  });
}

export function fetchAdminGuidesReport(): Promise<AdminGuidesReportResponse> {
  return apiGet(endpoints.adminGuidesReport());
}

export function fetchAdminUsageReport(): Promise<AdminUsageReport> {
  return apiGet(endpoints.adminUsageReport());
}

export function fetchAdminSignIn(): Promise<AdminSignInStatus> {
  return apiGet(endpoints.adminSignIn());
}

export function switchToGoogleSignIn(): Promise<{ login_method: string }> {
  return apiPut(endpoints.adminLoginMethod(), { body: { login_method: 'google' } });
}

export function createPasswordLink(email: string, sendEmail: boolean): Promise<PasswordLinkResult> {
  return apiPost(endpoints.adminPasswordLinks(), { body: { email, send_email: sendEmail } });
}

// Email/password sign-in. The unauthenticated calls (sign-in screen,
// set-password page) go through the same helpers: they just carry no
// session cookie yet, and the server sets one on success.
export function passwordLogin(email: string, password: string): Promise<{ success: boolean }> {
  return apiPost(endpoints.passwordLogin(), { body: { email, password } });
}

export function requestPasswordLink(email: string): Promise<{ success: boolean; message: string }> {
  return apiPost(endpoints.passwordRequestLink(), { body: { email } });
}

export function fetchPasswordLinkInfo(token: string): Promise<PasswordLinkInfo> {
  return apiPost(endpoints.passwordLinkInfo(), { body: { token } });
}

export function setPasswordWithLink(token: string, password: string, name: string): Promise<{ success: boolean }> {
  return apiPost(endpoints.passwordSet(), { body: { token, password, name } });
}

export function changePassword(currentPassword: string, newPassword: string): Promise<{ success: boolean }> {
  return apiPost(endpoints.passwordChange(), {
    body: { current_password: currentPassword, new_password: newPassword },
  });
}

export function fetchFeatureGates(): Promise<FeatureGatesListResponse> {
  return apiGet(endpoints.adminFeatureGates());
}

export function updateFeatureGate(
  feature: string,
  // allowed_users omitted = keep the stored list; null = all users.
  update: FeatureGateUpdate,
): Promise<FeatureGate> {
  return apiPut(endpoints.adminFeatureGate(feature), { body: update });
}

/**
 * Composer voice input: upload one recorded clip (multipart ``audio``) and
 * get its transcript back. The clip is transcribed server-side on the
 * deployment's own Vertex project and never stored. Error codes:
 * voice_input_disabled (403), unsupported_audio / audio_too_large /
 * empty_audio (400), transcription_unavailable (503), transcription_failed
 * (502).
 */
export async function transcribeAudio(clip: Blob, filename: string): Promise<TranscribeResponse> {
  const formData = new FormData();
  formData.append('audio', clip, filename);
  const response = await fetch(endpoints.transcribe(), {
    method: 'POST',
    body: formData,
    credentials: 'include',
  });
  if (!response.ok) {
    await handleErrorResponse(response);
  }
  return (await response.json()) as TranscribeResponse;
}

export function fetchServiceCredentials(): Promise<ServiceCredentialsListResponse> {
  return apiGet(endpoints.adminServiceCredentials());
}

/**
 * Generic schema-validated save for any credential service (core or
 * plugin). The body is a flat {field key: value} object; empty secret
 * values keep the stored ones.
 */
export function updateServiceCredentials(
  service: string,
  update: ServiceCredentialUpdate,
): Promise<ServiceCredentialDetail> {
  return apiPut(endpoints.adminServiceCredential(service), { body: update });
}

export function fetchInferenceProviders(): Promise<InferenceProvidersListResponse> {
  return apiGet(endpoints.adminInferenceProviders());
}

export function testInferenceModel(model: string): Promise<InferenceModelTestResult> {
  return apiPost(endpoints.adminInferenceModelTest(), { body: { model } });
}

export function updateVertexModels(update: VertexModelsUpdate): Promise<VertexProviderStatus> {
  return apiPut(endpoints.adminInferenceVertex(), { body: update });
}

export function createInferenceInstance(
  body: InferenceInstanceCreate,
): Promise<InferenceInstanceStatus> {
  return apiPost(endpoints.adminInferenceInstances(), { body });
}

export function updateInferenceInstance(
  instanceId: string,
  update: InferenceInstanceUpdate,
): Promise<InferenceInstanceStatus> {
  return apiPut(endpoints.adminInferenceInstance(instanceId), { body: update });
}

export function deleteInferenceInstance(instanceId: string): Promise<{ success: boolean }> {
  return apiDelete(endpoints.adminInferenceInstance(instanceId));
}

export function fetchModelSelection(): Promise<ModelSelectionListResponse> {
  return apiGet(endpoints.adminModelSelection());
}

export function updateModelSelection(
  update: ModelSelectionUpdate,
): Promise<ModelSelectionListResponse> {
  return apiPut(endpoints.adminModelSelection(), { body: update });
}

export function searchOpenRouterCatalog(
  q: string,
  options: { limit?: number; refresh?: boolean } = {},
): Promise<OpenRouterCatalogResponse> {
  return apiGet(endpoints.adminOpenRouterCatalog(), {
    query: { q, limit: options.limit, refresh: options.refresh || undefined },
  });
}

/**
 * An instance's own model catalog: what a self-hosted server reports right
 * now (live, uncached) or the Fireworks serverless catalog fetched with the
 * instance's key (cached; `refresh` re-fetches).
 */
export function searchInstanceCatalog(
  instanceId: string,
  q: string,
  options: { limit?: number; refresh?: boolean } = {},
): Promise<InstanceCatalogResponse> {
  return apiGet(endpoints.adminInferenceInstanceCatalog(instanceId), {
    query: { q, limit: options.limit, refresh: options.refresh || undefined },
  });
}

// Search API function

export function searchConversations(query: string): Promise<SearchResponse> {
  return apiGet(endpoints.search(), { query: { q: query } });
}

/**
 * Fetch server version (unauthenticated)
 */
export function fetchVersion(): Promise<VersionResponse> {
  return apiGet(endpoints.version());
}
