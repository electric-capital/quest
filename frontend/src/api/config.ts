/**
 * API configuration for chat application
 */

export const API_BASE_URL = '/app/api';

/**
 * Get the WebSocket URL for the current location
 * Handles ws:// vs wss:// based on protocol
 */
export function getWebSocketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  return `${protocol}//${host}`;
}

/**
 * API endpoints
 */
export const endpoints = {
  /**
   * List all conversations
   */
  conversations: () => `${API_BASE_URL}/conversations`,

  /**
   * Get or create a specific conversation
   */
  conversation: (id: string) => `${API_BASE_URL}/conversations/${id}`,

  /**
   * Create a new conversation with a copy of this conversation's workspace files
   */
  duplicateWorkspace: (id: string) => `${API_BASE_URL}/conversations/${id}/duplicate-workspace`,

  /**
   * Compact this conversation's model-facing history (summary + recent messages)
   */
  compactConversation: (id: string) => `${API_BASE_URL}/conversations/${id}/compact`,

  /**
   * Persistent multiplexed WebSocket endpoint (one per browser session)
   */
  persistentStream: () => {
    const wsBase = getWebSocketUrl();
    return `${wsBase}${API_BASE_URL}/stream`;
  },

  /**
   * User settings
   */
  settings: () => `${API_BASE_URL}/settings`,

  /**
   * Current user info (via API key auth)
   */
  me: () => `${API_BASE_URL}/me`,

  /**
   * Connector status for all OAuth services
   */
  connectors: () => `${API_BASE_URL}/connectors`,

  /**
   * Composer voice input: multipart audio clip -> transcript
   */
  transcribe: () => `${API_BASE_URL}/transcribe`,

  /**
   * Inference API keys (Settings > Inference API)
   */
  inferenceApiKeys: () => `${API_BASE_URL}/inference-api-keys`,

  inferenceApiKey: (id: string) => `${API_BASE_URL}/inference-api-keys/${id}`,

  memories: () => `${API_BASE_URL}/memories`,

  memory: (id: string) => `${API_BASE_URL}/memories/${id}`,

  guides: () => `${API_BASE_URL}/guides`,

  guide: (id: string) => `${API_BASE_URL}/guides/${id}`,

  skills: () => `${API_BASE_URL}/skills`,

  skill: (id: string) => `${API_BASE_URL}/skills/${id}`,

  skillAutoload: (skillId: string) => `${API_BASE_URL}/skills/${skillId}/autoload`,

  skillsAutoloaded: () => `${API_BASE_URL}/skills/autoloaded`,

  skillsSearch: (query: string) => `${API_BASE_URL}/skills/search?q=${encodeURIComponent(query)}`,

  skillsSharedWithMe: () => `${API_BASE_URL}/skills/shared-with-me`,

  skillShares: (skillId: string) => `${API_BASE_URL}/skills/${skillId}/shares`,

  skillShare: (skillId: string, userId: number) => `${API_BASE_URL}/skills/${skillId}/shares/${userId}`,

  userSearch: (query: string) => `${API_BASE_URL}/users/search?q=${encodeURIComponent(query)}`,

  projects: () => `${API_BASE_URL}/projects`,

  projectFromConversation: () => `${API_BASE_URL}/projects/from-conversation`,

  project: (id: string) => `${API_BASE_URL}/projects/${id}`,

  projectConversations: (projectId: string) => `${API_BASE_URL}/projects/${projectId}/conversations`,

  projectConversation: (projectId: string, conversationId: string) =>
    `${API_BASE_URL}/projects/${projectId}/conversations/${conversationId}`,

  projectRoutines: (projectId: string) => `${API_BASE_URL}/projects/${projectId}/routines`,

  projectRoutine: (projectId: string, routineId: string) =>
    `${API_BASE_URL}/projects/${projectId}/routines/${routineId}`,

  routineCosts: (projectId: string, routineId: string) =>
    `${API_BASE_URL}/projects/${projectId}/routines/${routineId}/costs`,

  routineSkillsAutoloaded: (projectId: string, routineId: string) =>
    `${API_BASE_URL}/projects/${projectId}/routines/${routineId}/skills/autoloaded`,

  routineSkillAutoload: (projectId: string, routineId: string, skillId: string) =>
    `${API_BASE_URL}/projects/${projectId}/routines/${routineId}/skills/${skillId}/autoload`,

  projectSkills: (projectId: string) => `${API_BASE_URL}/projects/${projectId}/skills`,

  projectSkill: (projectId: string, skillId: string) =>
    `${API_BASE_URL}/projects/${projectId}/skills/${skillId}`,

  projectTables: (projectId: string) => `${API_BASE_URL}/projects/${projectId}/tables`,

  projectTableData: (projectId: string, tableName: string) =>
    `${API_BASE_URL}/projects/${projectId}/tables/${encodeURIComponent(tableName)}`,

  projectSkillsAutoloaded: (projectId: string) =>
    `${API_BASE_URL}/projects/${projectId}/skills/autoloaded`,

  projectSkillAutoload: (projectId: string, skillId: string) =>
    `${API_BASE_URL}/projects/${projectId}/skills/${skillId}/autoload`,

  routineSchedule: (projectId: string, routineId: string) =>
    `${API_BASE_URL}/projects/${projectId}/routines/${routineId}/schedule`,

  actionRequests: () => `${API_BASE_URL}/action-requests`,
  actionRequestsCount: () => `${API_BASE_URL}/action-requests/count`,
  actionRequestsCounts: () => `${API_BASE_URL}/action-requests/counts`,
  actionRequest: (id: number) => `${API_BASE_URL}/action-requests/${id}`,
  actionRequestResolve: (id: number) => `${API_BASE_URL}/action-requests/${id}/resolve`,

  waitHandle: (id: string) => `${API_BASE_URL}/wait-handles/${id}`,
  waitHandleResolve: (id: string) => `${API_BASE_URL}/wait-handles/${id}/resolve`,

  conversationLoadedSkills: (conversationId: string) => `${API_BASE_URL}/conversations/${conversationId}/loaded-skills`,
  conversationSystemPrompt: (conversationId: string) => `${API_BASE_URL}/conversations/${conversationId}/system-prompt`,

  search: () => `${API_BASE_URL}/search`,

  adminShutdown: () => `${API_BASE_URL}/admin/shutdown`,

  adminUsers: (includeSelf = false) =>
    `${API_BASE_URL}/admin/users${includeSelf ? '?include_self=true' : ''}`,

  adminImpersonate: () => `${API_BASE_URL}/admin/impersonate`,

  adminStopImpersonation: () => `${API_BASE_URL}/admin/stop-impersonation`,

  adminLatestActiveConversations: () => `${API_BASE_URL}/admin/system-monitor/latest-active-conversations`,
  adminMostExpensiveConversations: () => `${API_BASE_URL}/admin/system-monitor/most-expensive-conversations`,
  adminUserReport: () => `${API_BASE_URL}/admin/system-monitor/user-report`,
  adminModelReport: () => `${API_BASE_URL}/admin/system-monitor/model-report`,
  adminGuidesReport: () => `${API_BASE_URL}/admin/system-monitor/guides-report`,

  adminSignIn: () => `${API_BASE_URL}/admin/sign-in`,
  adminLoginMethod: () => `${API_BASE_URL}/admin/sign-in/login-method`,
  adminPasswordLinks: () => `${API_BASE_URL}/admin/sign-in/password-links`,

  // Email/password sign-in (auth/password_login.py; outside /app/api).
  passwordLogin: () => '/auth/password/login',
  passwordRequestLink: () => '/auth/password/request-link',
  passwordLinkInfo: () => '/auth/password/link-info',
  passwordSet: () => '/auth/password/set',
  passwordChange: () => '/auth/password/change',

  adminFeatureGates: () => `${API_BASE_URL}/admin/feature-gates`,

  adminFeatureGate: (feature: string) => `${API_BASE_URL}/admin/feature-gates/${feature}`,

  adminServiceCredentials: () => `${API_BASE_URL}/admin/service-credentials`,

  adminServiceCredential: (service: string) => `${API_BASE_URL}/admin/service-credentials/${service}`,

  adminInferenceProviders: () => `${API_BASE_URL}/admin/inference-providers`,

  adminInferenceVertex: () => `${API_BASE_URL}/admin/inference-providers/vertex`,

  adminInferenceInstances: () => `${API_BASE_URL}/admin/inference-providers/instances`,

  adminInferenceInstance: (instanceId: string) =>
    `${API_BASE_URL}/admin/inference-providers/instances/${encodeURIComponent(instanceId)}`,

  adminInferenceInstanceCatalog: (instanceId: string) =>
    `${API_BASE_URL}/admin/inference-providers/instances/${encodeURIComponent(instanceId)}/catalog`,

  adminOpenRouterCatalog: () => `${API_BASE_URL}/admin/inference-providers/openrouter/catalog`,

  adminInferenceModelTest: () => `${API_BASE_URL}/admin/inference-providers/test-model`,

  adminModelSelection: () => `${API_BASE_URL}/admin/model-selection`,

  version: () => `${API_BASE_URL}/version`,
};
