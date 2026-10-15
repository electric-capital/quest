/**
 * Model selection state: the per-user default conversation model (one per
 * visibility), the per-conversation model overrides, and the per-conversation
 * provider locks. A pick is a model plus the thinking level chosen for it
 * (ModelPick; null level = the model's default), tracked side by side.
 *
 * Hydrates the defaults from the GET /me session snapshot (AuthContext) and
 * validates them against the credentialed-model list (AppConfigContext).
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import {
  DEPRECATED_MODEL_MAP, DEFAULT_MODEL_ID, isModelSelectableFor, resolveFallbackModel, resolveThinkingEffortFor,
} from '../constants/models';
import type { ModelPick, ModelVisibility } from '../constants/models';
import { HOME_DRAFT_KEY } from '../constants/drafts';
import { checkSession } from '../utils/auth';
import { useAppConfig } from './AppConfigContext';
import { useAuth, type SessionUser } from './AuthContext';

/** A stored "last-used" candidate: the model with the level it was sent at. */
export interface StoredPick {
  model: string | null | undefined;
  thinkingEffort?: string | null;
}

/**
 * Resolve a chain of (possibly null / stale / unknown) server-stored model
 * picks to a usable pick for a conversation visibility: the first candidate
 * whose model -- after the deprecated-id remap -- is in the catalog, not
 * deprecated, allowed by the admin for that visibility (Settings > Model
 * Selection) and credentialed (available_models from GET /app/api/config;
 * null while unknown) wins, keeping its thinking level when the model still
 * lists it; otherwise the best offerable model for that visibility at its
 * default level (see resolveFallbackModel: Opus 4.8 when allowed +
 * credentialed, else the admin's first top-level pick, else the first
 * offerable model in catalog order). A stored pick that is deprecated (still
 * runnable, but hidden from the picker) also falls through, so new
 * conversations never start on a deprecated model. Single place the FE
 * applies the fallback (the backend /me returns the raw stored values).
 */
export function resolveDefaultPick(
  candidates: StoredPick[],
  availableIds: string[] | null,
  visibility: ModelVisibility,
): ModelPick {
  for (const { model: raw, thinkingEffort } of candidates) {
    if (!raw) continue;
    const remapped = DEPRECATED_MODEL_MAP[raw] || raw;
    if (isModelSelectableFor(remapped, visibility, availableIds)) {
      return { model: remapped, thinkingEffort: resolveThinkingEffortFor(remapped, thinkingEffort) };
    }
  }
  return { model: resolveFallbackModel(availableIds, visibility), thinkingEffort: null };
}

/** Model-only form of resolveDefaultPick, for callers without a level. */
export function resolveDefaultModel(
  candidates: (string | null | undefined)[],
  availableIds: string[] | null,
  visibility: ModelVisibility,
): string {
  return resolveDefaultPick(candidates.map((model) => ({ model })), availableIds, visibility).model;
}

/**
 * The stored "last-used" candidates for a visibility, from a GET /me payload.
 * Public composers try the public pick first and then the private one, so a
 * user's first public project starts on their usual model when the admin
 * allows it there; private composers only ever use the private pick.
 */
export function defaultModelCandidates(
  userInfo: {
    default_model?: string | null;
    public_default_model?: string | null;
    default_thinking_effort?: string | null;
    public_default_thinking_effort?: string | null;
  } | null,
  visibility: ModelVisibility,
): StoredPick[] {
  if (!userInfo) return [];
  const privatePick = { model: userInfo.default_model, thinkingEffort: userInfo.default_thinking_effort };
  return visibility === 'public'
    ? [{ model: userInfo.public_default_model, thinkingEffort: userInfo.public_default_thinking_effort }, privatePick]
    : [privatePick];
}

function resolveDefaultsFromSession(
  userInfo: SessionUser | null,
  availableIds: string[] | null,
): Record<ModelVisibility, ModelPick> {
  return {
    private: resolveDefaultPick(defaultModelCandidates(userInfo, 'private'), availableIds, 'private'),
    public: resolveDefaultPick(defaultModelCandidates(userInfo, 'public'), availableIds, 'public'),
  };
}

export interface ConversationModelsContextValue {
  // Per-user "last-used" default conversation pick, one per conversation
  // visibility: `private` (users.settings.default_model -- everything outside
  // a public project) and `public` (users.settings.public_default_model --
  // public-project conversations, whose admin allow-list differs), each with
  // the thinking level it was last sent at. Sourced ONLY from a fresh GET /me
  // fetch (no localStorage); each falls back to the best model offerable for
  // its visibility (resolveFallbackModel). Re-read on every new-composer
  // mount AND every private/public context switch via refreshDefaultModel();
  // written server-side only on the first send of a new chat via
  // persistDefaultModel(). `defaultModel` is the private one's model.
  defaultModel: string;
  defaultPicks: Record<ModelVisibility, ModelPick>;
  setDefaultModel: (model: string, visibility?: ModelVisibility, thinkingEffort?: string | null) => void;
  // Re-fetch the per-user defaults from the server (GET /me) and re-apply the
  // one for the given visibility (private when omitted). Called on every
  // fresh new-chat composer mount and whenever a composer switches between
  // the private and public contexts, for cross-tab correctness (no WS push).
  // Does not touch localStorage. Resolves to the freshly-resolved pick
  // (visibility-aware fallback when unset/disallowed/uncredentialed).
  refreshDefaultModel: (visibility?: ModelVisibility) => Promise<ModelPick>;
  // Best-effort PUT /settings { default_model, default_thinking_effort }
  // (private) or the public_ twins (public) persisting the user-level
  // last-used pick for that visibility. The ONLY server write of either;
  // called solely from the first-send paths.
  persistDefaultModel: (model: string, visibility?: ModelVisibility, thinkingEffort?: string | null) => void;
  getModelForConversation: (conversationId: string) => string;
  // The thinking level picked for the conversation's model (null = default).
  getThinkingEffortForConversation: (conversationId: string) => string | null;
  setModelForConversation: (conversationId: string, model: string, thinkingEffort?: string | null) => void;
  hydrateModelForConversation: (conversationId: string, model: string, thinkingEffort?: string | null) => void;
  // Draft-safe model setter for the root home composer: updates only the
  // in-memory per-conversation model map (and the given visibility's
  // in-memory default for display, private when omitted). Does NOT PATCH the
  // server and does NOT persist the user default (the draft "conversation"
  // doesn't exist yet; the default is persisted on first send).
  setDraftModelForConversation: (
    conversationId: string, model: string, visibility?: ModelVisibility, thinkingEffort?: string | null,
  ) => void;
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

function samePick(a: ModelPick, b: ModelPick): boolean {
  return a.model === b.model && a.thinkingEffort === b.thinkingEffort;
}

export function ConversationModelsProvider({ children }: { children: React.ReactNode }) {
  const { sessionSnapshot } = useAuth();
  const { getAvailableModelIds, whenConfigLoaded } = useAppConfig();

  // Per-user default conversation pick per visibility. No localStorage: the
  // authoritative values come from a fresh GET /me fetch (hydration + every
  // new-composer mount / context switch). Start both at the Opus-4.8
  // fallback as a pre-fetch placeholder.
  const [defaultPicks, setDefaultPicks] = useState<Record<ModelVisibility, ModelPick>>({
    private: { model: DEFAULT_MODEL_ID, thinkingEffort: null },
    public: { model: DEFAULT_MODEL_ID, thinkingEffort: null },
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
    setDefaultPicks(resolveDefaultsFromSession(sessionSnapshot, getAvailableModelIds()));
  }
  const defaultModel = defaultPicks.private.model;

  const setDefaultPickState = useCallback((pick: ModelPick, visibility: ModelVisibility = 'private') => {
    setDefaultPicks((prev) => (samePick(prev[visibility], pick) ? prev : { ...prev, [visibility]: pick }));
  }, []);

  // Per-conversation picks: the model map and the level map are keyed alike
  // (a conversation with a model but no level entry runs at the default).
  const [conversationModels, setConversationModels] = useState<Record<string, string>>({});
  const [conversationEfforts, setConversationEfforts] = useState<Record<string, string | null>>({});
  const [lockedProviders, setLockedProviders] = useState<Record<string, string>>(readLockedProviders);

  const setDefaultModel = useCallback((
    model: string, visibility: ModelVisibility = 'private', thinkingEffort: string | null = null,
  ) => {
    // In-memory only: reflects a picker change in the current composer's
    // display. Does NOT write the server default or localStorage -- the
    // user-level default is persisted only on first-send (persistDefaultModel).
    setDefaultPickState({ model, thinkingEffort }, visibility);
  }, [setDefaultPickState]);

  // Re-fetch the per-user defaults from the server (GET /me) and re-apply the
  // one for the requested visibility. Invoked on every fresh new-chat composer
  // mount and on every private<->public context switch so the composer
  // reflects the latest server value across tabs without any cross-tab push.
  const refreshDefaultModel = useCallback(async (visibility: ModelVisibility = 'private'): Promise<ModelPick> => {
    const userInfo = await checkSession();
    // Make sure the mount-time config fetch has settled so resolution sees the
    // credentialed-model list (no-op after the first composer mount).
    await whenConfigLoaded;
    const resolved = resolveDefaultPick(
      defaultModelCandidates(userInfo, visibility),
      getAvailableModelIds(),
      visibility,
    );
    setDefaultPickState(resolved, visibility);
    return resolved;
  }, [setDefaultPickState, whenConfigLoaded, getAvailableModelIds]);

  // Persist the user-level last-used pick for a visibility server-side
  // (best-effort, fire-and-forget). The ONLY write of either key pair; called
  // exclusively from the first-send paths. Does not touch localStorage. An
  // empty string clears the stored level (= the model's default).
  const persistDefaultModel = useCallback((
    model: string, visibility: ModelVisibility = 'private', thinkingEffort: string | null = null,
  ) => {
    const body = visibility === 'public'
      ? { public_default_model: model, public_default_thinking_effort: thinkingEffort ?? '' }
      : { default_model: model, default_thinking_effort: thinkingEffort ?? '' };
    fetch('/app/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => {});
  }, []);

  const getModelForConversation = useCallback((conversationId: string) => {
    const raw = conversationModels[conversationId] || defaultModel;
    return DEPRECATED_MODEL_MAP[raw] || raw;
  }, [conversationModels, defaultModel]);

  const getThinkingEffortForConversation = useCallback((conversationId: string) => {
    // A conversation without its own model entry shows the private default
    // pick, so its level comes along; the level map alone is never consulted
    // for a model the default supplies.
    if (!conversationModels[conversationId]) return defaultPicks.private.thinkingEffort;
    return conversationEfforts[conversationId] ?? null;
  }, [conversationModels, conversationEfforts, defaultPicks]);

  const setModelForConversation = useCallback((
    conversationId: string, model: string, thinkingEffort: string | null = null,
  ) => {
    // Update the in-memory per-conversation pick. This is the per-conversation
    // override (persisted on the conversation row below); it does NOT mutate the
    // user-level default model (that is written only on a new chat's first send).
    setConversationModels((prev) => ({ ...prev, [conversationId]: model }));
    setConversationEfforts((prev) => ({ ...prev, [conversationId]: thinkingEffort }));
    // Persist the per-conversation override to server (best-effort, fire-and-forget)
    fetch(`/app/api/conversations/${conversationId}/model`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, thinking_effort: thinkingEffort }),
    }).catch(() => {});
  }, []);

  const hydrateModelForConversation = useCallback((
    conversationId: string, model: string, thinkingEffort: string | null = null,
  ) => {
    // Set the in-memory per-conversation pick from the server response.
    // Does NOT update the default or fire a PATCH back to the server.
    setConversationModels((prev) => ({ ...prev, [conversationId]: model }));
    setConversationEfforts((prev) => ({ ...prev, [conversationId]: thinkingEffort }));
  }, []);

  const setDraftModelForConversation = useCallback((
    conversationId: string,
    model: string,
    visibility: ModelVisibility = 'private',
    thinkingEffort: string | null = null,
  ) => {
    // Home-composer model setter: update the in-memory per-conversation map (and
    // the visibility's default for display). Do NOT PATCH the server -- the
    // draft-keyed "conversation" does not exist yet. Do NOT persist the
    // user-level default here either: selecting a model without sending must
    // not write the default. The default is persisted on first send
    // (persistDefaultModel, in ChatPanel's first-send paths). No localStorage.
    setConversationModels((prev) => ({ ...prev, [conversationId]: model }));
    setConversationEfforts((prev) => ({ ...prev, [conversationId]: thinkingEffort }));
    setDefaultPickState({ model, thinkingEffort }, visibility);
  }, [setDefaultPickState]);

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
    defaultPicks,
    setDefaultModel,
    refreshDefaultModel,
    persistDefaultModel,
    getModelForConversation,
    getThinkingEffortForConversation,
    setModelForConversation,
    hydrateModelForConversation,
    setDraftModelForConversation,
    getLockedProvider,
    lockConversationProvider,
    isProviderLocked,
  }), [
    defaultModel,
    defaultPicks,
    setDefaultModel,
    refreshDefaultModel,
    persistDefaultModel,
    getModelForConversation,
    getThinkingEffortForConversation,
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
