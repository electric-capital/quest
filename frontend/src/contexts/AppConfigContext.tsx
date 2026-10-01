/**
 * Unauthenticated server configuration.
 *
 * Owns what the app knows before -- and independently of -- a signed-in
 * session: GET /app/api/config (app name, run mode, sign-in method, model
 * catalog + credentialed-model list) and the GET /app/api/version redeploy
 * poll. Nothing in here changes on user actions except an admin re-fetching
 * the model catalog, so consumers of this context practically never
 * re-render after boot.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { fetchVersion } from '../api/client';
import { setModelCatalog } from '../constants/models';

/** How often to poll for server version changes (ms). */
const VERSION_POLL_INTERVAL_MS = 60_000;

export type LoginMethod = 'google' | 'password';

export interface AppConfigContextValue {
  // App name: "DevQuest" in local mode, "Quest" in staging/production
  appName: string;
  // Whether running in local mode (QUEST_ENV=local; legacy alias "dev").
  // Enables the canned-account sign-in picker.
  isDevMode: boolean;
  // Human-readable sign-in restriction (from GET /app/api/config), e.g.
  // "@example.com accounts" or "approved accounts" on deployments with an
  // email whitelist. Defaults to "approved accounts" until the config fetch
  // resolves; shown on the sign-in screen.
  loginRestriction: string;
  // Active sign-in method (GET /app/api/config): "google" (Google OAuth) or
  // "password" (email + password accounts). Exactly one is active. null
  // until the config fetch resolves.
  loginMethod: LoginMethod | null;
  // Password sign-in only: whether the sign-in screen can offer self-service
  // sign-up / forgot-password (the server has outgoing email configured).
  passwordSelfService: boolean;
  // Model IDs whose backend credentials are configured server-side (from
  // GET /app/api/config). null until the config fetch resolves; treat null
  // as "all models" so the picker doesn't flicker empty on load.
  availableModelIds: string[] | null;
  // Same list, read synchronously from the provider's ref. For async code
  // that has just awaited `whenConfigLoaded`: the state above may not have
  // re-rendered yet at that point, the ref already holds the fetched list.
  getAvailableModelIds: () => string[] | null;
  // Settles once the mount-time config fetch has finished (success or
  // failure; never rejects). Session hydration and default-model resolution
  // await it so they see the credentialed-model list.
  whenConfigLoaded: Promise<void>;
  // Re-fetch GET /app/api/config and replace the model catalog +
  // available_models (used after an admin saves Settings > Model Selection
  // so the composer menu in this tab reflects the change without a reload).
  refreshModelCatalog: () => Promise<void>;
  // Version update detection: the server has been redeployed since this
  // page loaded (Sidebar shows a reload banner).
  updateAvailable: boolean;
}

const AppConfigContext = createContext<AppConfigContextValue | null>(null);

export function AppConfigProvider({ children }: { children: React.ReactNode }) {
  // App name: "DevQuest" in local mode, "Quest" in staging/production.
  // Defaults to "Quest" until the backend config is fetched.
  const [appName, setAppName] = useState('Quest');
  const [isDevMode, setIsDevMode] = useState(false);
  const [loginRestriction, setLoginRestriction] = useState('approved accounts');
  // null until the config fetch resolves, so the sign-in screen does not
  // flash the wrong form.
  const [loginMethod, setLoginMethod] = useState<LoginMethod | null>(null);
  const [passwordSelfService, setPasswordSelfService] = useState(false);
  const [availableModelIds, setAvailableModelIds] = useState<string[] | null>(null);
  // Ref mirror of availableModelIds so async default-model resolution can
  // read the credentialed-model list right after awaiting the config fetch,
  // without racing the state update.
  const availableModelIdsRef = useRef<string[] | null>(null);

  // Version update detection
  const [updateAvailable, setUpdateAvailable] = useState(false);
  const initialVersionHash = useRef<string | null>(null);

  // A promise that settles when the mount-time config fetch finishes. Created
  // eagerly so it exists from the first render (children may capture it in
  // their own mount effects, which run before this provider's).
  const [configLoaded] = useState(() => {
    let resolve!: () => void;
    const promise = new Promise<void>((r) => {
      resolve = r;
    });
    return { promise, resolve };
  });

  const applyConfig = useCallback((data: Record<string, unknown>) => {
    // The model catalog must land before available_models: consumers
    // re-render on the availableModelIds state change and read the
    // catalog (module state) during that render.
    setModelCatalog(data.models as Parameters<typeof setModelCatalog>[0]);
    if (Array.isArray(data.available_models)) {
      availableModelIdsRef.current = data.available_models as string[];
      setAvailableModelIds(data.available_models as string[]);
    }
  }, []);

  // Fetch app config and the initial server version hash on mount (both
  // unauthenticated).
  useEffect(() => {
    const doFetchConfig = async () => {
      try {
        const response = await fetch('/app/api/config');
        if (response.ok) {
          const data = await response.json();
          // 'local' is the canonical mode name; 'dev' is the legacy alias
          // (still returned by older backends).
          if (data.quest_env === 'local' || data.quest_env === 'dev') {
            setAppName('DevQuest');
            setIsDevMode(true);
          }
          applyConfig(data);
          if (typeof data.login_restriction === 'string' && data.login_restriction) {
            setLoginRestriction(data.login_restriction);
          } else if (typeof data.allowed_login_domain === 'string' && data.allowed_login_domain) {
            // Older backends only send the domain.
            setLoginRestriction(`@${data.allowed_login_domain} accounts`);
          }
          // Older backends have no login_method: Google sign-in.
          setLoginMethod(data.login_method === 'password' ? 'password' : 'google');
          setPasswordSelfService(data.password_self_service === true);
        } else {
          setLoginMethod('google');
        }
      } catch {
        // Config fetch failed; keep default "Quest"
        setLoginMethod('google');
      } finally {
        configLoaded.resolve();
      }
    };
    void doFetchConfig();

    const doFetchVersion = async () => {
      try {
        const data = await fetchVersion();
        initialVersionHash.current = data.git_hash;
      } catch {
        // Version fetch failed; leave hash null (polling will be a no-op)
      }
    };
    void doFetchVersion();
  }, [applyConfig, configLoaded]);

  // Poll for server version changes
  useEffect(() => {
    const checkVersion = async () => {
      if (document.hidden) return;
      if (initialVersionHash.current === null) return;
      try {
        const data = await fetchVersion();
        if (data.git_hash !== null && data.git_hash !== initialVersionHash.current) {
          setUpdateAvailable(true);
        }
      } catch {
        // Silently ignore network errors
      }
    };

    const intervalId = setInterval(() => {
      if (updateAvailable) return; // Stop polling once detected
      checkVersion();
    }, VERSION_POLL_INTERVAL_MS);

    const handleVisibilityChange = () => {
      if (!document.hidden && !updateAvailable) {
        checkVersion();
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      clearInterval(intervalId);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [updateAvailable]);

  const refreshModelCatalog = useCallback(async () => {
    try {
      const response = await fetch('/app/api/config');
      if (!response.ok) return;
      applyConfig(await response.json());
    } catch {
      // Best-effort: the next page load picks the change up anyway.
    }
  }, [applyConfig]);

  const getAvailableModelIds = useCallback(() => availableModelIdsRef.current, []);

  const value = useMemo<AppConfigContextValue>(() => ({
    appName,
    isDevMode,
    loginRestriction,
    loginMethod,
    passwordSelfService,
    availableModelIds,
    getAvailableModelIds,
    whenConfigLoaded: configLoaded.promise,
    refreshModelCatalog,
    updateAvailable,
  }), [
    appName,
    isDevMode,
    loginRestriction,
    loginMethod,
    passwordSelfService,
    availableModelIds,
    getAvailableModelIds,
    configLoaded.promise,
    refreshModelCatalog,
    updateAvailable,
  ]);

  return <AppConfigContext.Provider value={value}>{children}</AppConfigContext.Provider>;
}

export function useAppConfig(): AppConfigContextValue {
  const context = useContext(AppConfigContext);
  if (!context) {
    throw new Error('useAppConfig must be used within an AppConfigProvider');
  }
  return context;
}
