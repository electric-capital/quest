/**
 * Model selection state: the per-user default conversation model (one per
 * visibility), the per-conversation model overrides, and the per-conversation
 * provider locks.
 *
 * Hydrates the defaults from the GET /me session snapshot (AuthContext) and
 * validates them against the credentialed-model list (AppConfigContext).
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { DEPRECATED_MODEL_MAP, DEFAULT_MODEL_ID, isModelSelectableFor, resolveFallbackModel } from '../constants/models';
import type { ModelVisibility } from '../constants/models';
import { HOME_DRAFT_KEY } from '../constants/drafts';
import { checkSession } from '../utils/auth';
import { useAppConfig } from './AppConfigContext';
import { useAuth, type SessionUser } from './AuthContext';

/**
 * Resolve a chain of (possibly null / stale / unknown) server-stored model
 * ids to a usable model id for a conversation visibility: the first
 * candidate that -- after the deprecated-id remap -- is in the catalog, not
 * deprecated, allowed by the admin for that visibility (Settings > Model
 * Selection) and credentialed (available_models from GET /app/api/config;
 * null while unknown) wins; otherwise the best offerable model for that
 * visibility (see resolveFallbackModel: Opus 4.8 when allowed + credentialed,
 * else the admin's first top-level pick, else the first offerable model in
 * catalog order). A stored pick that is deprecated (still runnable, but
 * hidden from the picker) also falls through, so new conversations never
 * start on a deprecated model. Single place the FE applies the fallback (the
 * backend /me returns the raw stored values).
 */
export function resolveDefaultModel(
  candidates: (string | null | undefined)[],
  availableIds: string[] | null,
  visibility: ModelVisibility,
): string {
  for (const raw of candidates) {
    if (!raw) continue;
    const remapped = DEPRECATED_MODEL_MAP[raw] || raw;
    if (isModelSelectableFor(remapped, visibility, availableIds)) return remapped;
  }
  return resolveFallbackModel(availableIds, visibility);
}

/**
 * The stored "last-used" candidates for a visibility, from a GET /me payload.
 * Public composers try the public pick first and then the private one, so a
 * user's first public project starts on their usual model when the admin
 * allows it there; private composers only ever use the private pick.
 */
export function defaultModelCandidates(
  userInfo: { default_model?: string | null; public_default_model?: string | null } | null,
  visibility: ModelVisibility,
): (string | null | undefined)[] {
  if (!userInfo) return [];
  return visibility === 'public'
    ? [userInfo.public_default_model, userInfo.default_model]
    : [userInfo.default_model];
}

function resolveDefaultsFromSession(
  userInfo: SessionUser | null,
  availableIds: string[] | null,
): Record<ModelVisibility, string> {
  return {
    private: resolveDefaultModel(defaultModelCandidates(userInfo, 'private'), availableIds, 'private'),
    public: resolveDefaultModel(defaultModelCandidates(userInfo, 'public'), availableIds, 'public'),
  };
}

export interface ConversationModelsContextValue {
  // Per-user "last-used" default conversation model, one per conversation
  // visibility: `private` (users.settings.default_model -- everything outside
  // a public project) and `public` (users.settings.public_default_model --
  // public-project conversations, whose admin allow-list differs). Sourced
  // ONLY from a fresh GET /me fetch (no localStorage); each falls back to the
  // best model offerable for its visibility (resolveFallbackModel). Re-read
  // on every new-composer mount AND every private/public context switch via
  // refreshDefaultModel(); written server-side only on the first send of a
  // new chat via persistDefaultModel(). `defaultModel` is the private one.
  defaultModel: string;
  defaultModels: Record<ModelVisibility, string>;
  setDefaultModel: (model: string, visibility?: ModelVisibility) => void;
  // Re-fetch the per-user defaults from the server (GET /me) and re-apply the
  // one for the given visibility (private when omitted). Called on every
  // fresh new-chat composer mount and whenever a composer switches between
  // the private and public contexts, for cross-tab correctness (no WS push).
  // Does not touch localStorage. Resolves to the freshly-resolved model id
  // (visibility-aware fallback when unset/disallowed/uncredentialed).
  refreshDefaultModel: (visibility?: ModelVisibility) => Promise<string>;
  // Best-effort PUT /settings { default_model } (private) or
  // { public_default_model } (public) persisting the user-level last-used
  // pick for that visibility. The ONLY server write of either; called solely
  // from the first-send paths.
  persistDefaultModel: (model: string, visibility?: ModelVisibility) => void;
  getModelForConversation: (conversationId: string) => string;
  setModelForConversation: (conversationId: string, model: string) => void;
  hydrateModelForConversation: (conversationId: string, model: string) => void;
  // Draft-safe model setter for the root home composer: updates only the
  // in-memory per-conversation model map (and the given visibility's
  // in-memory default for display, private when omitted). Does NOT PATCH the
  // server and does NOT persist the user default (the draft "conversation"
  // doesn't exist yet; the default is persisted on first send).
  setDraftModelForConversation: (conversationId: string, model: string, visibility?: ModelVisibility) => void;
  // Provider locking state (lock model provider after first message)
  getLockedProvider: (conversationId: string) => string | null;
  lockConversationProvider: (conversationId: string, provider: string) => void;
  isProviderLocked: (conversationId: string) => boolean;
}

const ConversationModelsContext = createContext<ConversationModelsContextValue | null>(null);

/** Read the persisted provider-lock map, migrating/cleaning legacy entries. */
function readLockedProviders(): Record<string, string> {
  // One-time migration from the pre-rename (praixy) key so existing
  // conversations keep their provider lock.
  const legacy = localStorage.getItem('praixy_locked_providers');
  if (legacy !== null && localStorage.getItem('quest_locked_providers') === null) {
    localStorage.setItem('quest_locked_providers', legacy);
  }
  if (legacy !== null) {
    localStorage.removeItem('praixy_locked_providers');
  }
  const stored = localStorage.getItem('quest_locked_providers');
  const parsed: Record<string, string> = stored ? JSON.parse(stored) : {};
  // Older builds' home composer wrote locks for the in-memory home-draft key
  // on send, permanently poisoning this persisted map (nothing ever removes
  // an entry) and filtering the home model dropdown to a single provider.
  // Strip any legacy HOME_DRAFT_KEY entry on init; the localStorage blob
  // itself is left stale on purpose -- the next lock write re-serializes the
  // cleaned in-memory map.
  delete parsed[HOME_DRAFT_KEY];
  return parsed;
}

export function ConversationModelsProvider({ children }: { children: React.ReactNode }) {
  const { sessionSnapshot } = useAuth();
  const { getAvailableModelIds, whenConfigLoaded } = useAppConfig();

  // Per-user default conversation model per visibility. No localStorage: the
  // authoritative values come from a fresh GET /me fetch (hydration + every
  // new-composer mount / context switch). Start both at the Opus-4.8
  // fallback as a pre-fetch placeholder.
  const [defaultModels, setDefaultModels] = useState<Record<ModelVisibility, string>>({
    private: DEFAULT_MODEL_ID,
    public: DEFAULT_MODEL_ID,
  });
  // Hydrate the per-visibility defaults from the session snapshot (remap +
  // validate against the admin allow-list for the visibility and the
  // credentialed-model list + best-offerable fallback; same resolution as
  // refreshDefaultModel()). Done as a render-time state adjustment rather
  // than an effect so the FIRST authenticated render already sees the
  // hydrated defaults -- the session check only flips isAuthenticated after
  // the config fetch settled, so the credentialed list is in place here.
  const [hydratedFrom, setHydratedFrom] = useState<SessionUser | null>(null);
  if (sessionSnapshot && sessionSnapshot !== hydratedFrom) {
    setHydratedFrom(sessionSnapshot);
    setDefaultModels(resolveDefaultsFromSession(sessionSnapshot, getAvailableModelIds()));
  }
  const defaultModel = defaultModels.private;

  const setDefaultModelState = useCallback((model: string, visibility: ModelVisibility = 'private') => {
    setDefaultModels((prev) => (prev[visibility] === model ? prev : { ...prev, [visibility]: model }));
  }, []);

  const [conversationModels, setConversationModels] = useState<Record<string, string>>({});
  const [lockedProviders, setLockedProviders] = useState<Record<string, string>>(readLockedProviders);

  const setDefaultModel = useCallback((model: string, visibility: ModelVisibility = 'private') => {
    // In-memory only: reflects a picker change in the current composer's
    // display. Does NOT write the server default or localStorage -- the
    // user-level default is persisted only on first-send (persistDefaultModel).
    setDefaultModelState(model, visibility);
  }, [setDefaultModelState]);

  // Re-fetch the per-user defaults from the server (GET /me) and re-apply the
  // one for the requested visibility. Invoked on every fresh new-chat composer
  // mount and on every private<->public context switch so the composer
  // reflects the latest server value across tabs without any cross-tab push.
  const refreshDefaultModel = useCallback(async (visibility: ModelVisibility = 'private'): Promise<string> => {
    const userInfo = await checkSession();
    // Make sure the mount-time config fetch has settled so resolution sees the
    // credentialed-model list (no-op after the first composer mount).
    await whenConfigLoaded;
    const resolved = resolveDefaultModel(
      defaultModelCandidates(userInfo, visibility),
      getAvailableModelIds(),
      visibility,
    );
    setDefaultModelState(resolved, visibility);
    return resolved;
  }, [setDefaultModelState, whenConfigLoaded, getAvailableModelIds]);

  // Persist the user-level last-used pick for a visibility server-side
  // (best-effort, fire-and-forget). The ONLY write of either key; called
  // exclusively from the first-send paths. Does not touch localStorage.
  const persistDefaultModel = useCallback((model: string, visibility: ModelVisibility = 'private') => {
    const key = visibility === 'public' ? 'public_default_model' : 'default_model';
    fetch('/app/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: model }),
    }).catch(() => {});
  }, []);

  const getModelForConversation = useCallback((conversationId: string) => {
    const raw = conversationModels[conversationId] || defaultModel;
    return DEPRECATED_MODEL_MAP[raw] || raw;
  }, [conversationModels, defaultModel]);

  const setModelForConversation = useCallback((conversationId: string, model: string) => {
    // Update the in-memory per-conversation model. This is the per-conversation
    // override (persisted on the conversation row below); it does NOT mutate the
    // user-level default model (that is written only on a new chat's first send).
    setConversationModels((prev) => ({ ...prev, [conversationId]: model }));
    // Persist the per-conversation override to server (best-effort, fire-and-forget)
    fetch(`/app/api/conversations/${conversationId}/model`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    }).catch(() => {});
  }, []);

  const hydrateModelForConversation = useCallback((conversationId: string, model: string) => {
    // Set the in-memory per-conversation model from the server response.
    // Does NOT update defaultModel or fire a PATCH back to the server.
    setConversationModels((prev) => ({ ...prev, [conversationId]: model }));
  }, []);

  const setDraftModelForConversation = useCallback((
    conversationId: string,
    model: string,
    visibility: ModelVisibility = 'private',
  ) => {
    // Home-composer model setter: update the in-memory per-conversation map (and
    // the visibility's default for display). Do NOT PATCH the server -- the
    // draft-keyed "conversation" does not exist yet. Do NOT persist the
    // user-level default here either: selecting a model without sending must
    // not write the default. The default is persisted on first send
    // (persistDefaultModel, in ChatPanel's first-send paths). No localStorage.
    setConversationModels((prev) => ({ ...prev, [conversationId]: model }));
    setDefaultModelState(model, visibility);
  }, [setDefaultModelState]);

  const getLockedProvider = useCallback((conversationId: string): string | null => {
    return lockedProviders[conversationId] || null;
  }, [lockedProviders]);

  const lockConversationProvider = useCallback((conversationId: string, provider: string) => {
    setLockedProviders((prev) => {
      const updated = { ...prev, [conversationId]: provider };
      localStorage.setItem('quest_locked_providers', JSON.stringify(updated));
      return updated;
    });
  }, []);

  const isProviderLocked = useCallback((conversationId: string): boolean => {
    return lockedProviders[conversationId] != null;
  }, [lockedProviders]);

  const value = useMemo<ConversationModelsContextValue>(() => ({
    defaultModel,
    defaultModels,
    setDefaultModel,
    refreshDefaultModel,
    persistDefaultModel,
    getModelForConversation,
    setModelForConversation,
    hydrateModelForConversation,
    setDraftModelForConversation,
    getLockedProvider,
    lockConversationProvider,
    isProviderLocked,
  }), [
    defaultModel,
    defaultModels,
    setDefaultModel,
    refreshDefaultModel,
    persistDefaultModel,
    getModelForConversation,
    setModelForConversation,
    hydrateModelForConversation,
    setDraftModelForConversation,
    getLockedProvider,
    lockConversationProvider,
    isProviderLocked,
  ]);

  return <ConversationModelsContext.Provider value={value}>{children}</ConversationModelsContext.Provider>;
}

export function useConversationModels(): ConversationModelsContextValue {
  const context = useContext(ConversationModelsContext);
  if (!context) {
    throw new Error('useConversationModels must be used within a ConversationModelsProvider');
  }
  return context;
}
