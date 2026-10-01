/**
 * Session and user identity.
 *
 * Owns the mount-time session check (GET /app/api/me), the signed-in user's
 * identity and account flags (admin, enabled feature gates, impersonation,
 * connected services, sign-in password), and the persistent WebSocket's
 * lifecycle (open while a session is live). Per-feature state that is merely
 * *hydrated* from the same GET /me payload (appearance, default models) lives
 * in its own context and reads `sessionSnapshot` from here.
 */

import React, { createContext, useContext, useEffect, useMemo, useState, useCallback } from 'react';
import { checkSession } from '../utils/auth';
import { persistentWebSocket } from '../services/PersistentWebSocket';
import { useAppConfig } from './AppConfigContext';

/** The GET /app/api/me payload (see utils/auth.ts). */
export type SessionUser = NonNullable<Awaited<ReturnType<typeof checkSession>>>;

export interface AuthContextValue {
  isAuthenticated: boolean;
  isCheckingAuth: boolean;
  // The GET /me payload from the mount-time session check; null until it
  // resolves or when signed out. Other contexts hydrate their slice from it
  // exactly once (appearance, default models). Later re-reads of /me by the
  // refresh* functions below update only their own fields, never this.
  sessionSnapshot: SessionUser | null;
  // User info
  userEmail: string | null;
  userName: string | null;
  // Admin state
  isAdmin: boolean;
  // Server-global admin feature gates that are currently on (enabled_features
  // from GET /app/api/me). Feature-gated composer flags are hidden when their
  // feature is absent from this list.
  enabledFeatures: string[];
  // Re-fetch enabled_features from GET /me (called after an admin flips a
  // gate in Settings > Features so their own composer reflects it without a
  // reload; other sessions pick it up on their next load).
  refreshEnabledFeatures: () => Promise<void>;
  // Whether the signed-in account has a sign-in password (GET /me); the
  // Settings > Password section asks for the current one when it does.
  hasPassword: boolean;
  setHasPassword: (value: boolean) => void;
  // Google Services connection status (for new user flow)
  googleServicesConnected: boolean;
  // Whether user has any backend service connected (for auto-open settings decision)
  hasAnyServiceConnected: boolean;
  // Refresh connection status after OAuth popup completes
  refreshConnectionStatus: () => void;
  // Impersonation state
  isImpersonating: boolean;
  impersonatorEmail: string | null;
  impersonatorName: string | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const { whenConfigLoaded } = useAppConfig();

  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isCheckingAuth, setIsCheckingAuth] = useState(true);
  const [sessionSnapshot, setSessionSnapshot] = useState<SessionUser | null>(null);
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [userName, setUserName] = useState<string | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [enabledFeatures, setEnabledFeatures] = useState<string[]>([]);
  const [hasPassword, setHasPassword] = useState(false);
  const [googleServicesConnected, setGoogleServicesConnected] = useState(true); // Default true to avoid flash
  const [hasAnyServiceConnected, setHasAnyServiceConnected] = useState(true); // Default true to avoid flash
  const [isImpersonating, setIsImpersonating] = useState(false);
  const [impersonatorEmail, setImpersonatorEmail] = useState<string | null>(null);
  const [impersonatorName, setImpersonatorName] = useState<string | null>(null);

  // Check the session on mount.
  useEffect(() => {
    const doCheckAuth = async () => {
      const userInfo = await checkSession();
      // Wait for the concurrent config fetch so everything hydrated from this
      // session (default models in particular) sees the credentialed-model
      // list, and so the sign-in screen never renders before `loginMethod`
      // is known. Never rejects; on fetch failure the list stays null.
      await whenConfigLoaded;
      if (userInfo) {
        setUserEmail(userInfo.email);
        setUserName(userInfo.name || '');
        setGoogleServicesConnected(userInfo.google_services_connected);
        setHasAnyServiceConnected(userInfo.has_any_service_connected);
        setIsAdmin(userInfo.is_admin);
        setEnabledFeatures(userInfo.enabled_features ?? []);
        setHasPassword(userInfo.has_password === true);
        setIsImpersonating(userInfo.is_impersonating);
        setImpersonatorEmail(userInfo.impersonator_email);
        setImpersonatorName(userInfo.impersonator_name);
        setSessionSnapshot(userInfo);
        setIsAuthenticated(true);
      } else {
        setIsAuthenticated(false);
      }
      setIsCheckingAuth(false);
    };
    void doCheckAuth();
  }, [whenConfigLoaded]);

  // Open the persistent multiplexed WebSocket once authenticated; close it
  // on logout / account-delete. The connection is a single per-tab socket
  // that carries per-user globals (request count, conversation list,
  // wait-handle resolutions) and per-conversation events.
  useEffect(() => {
    if (!isAuthenticated) return;
    persistentWebSocket.connect();
    return () => {
      persistentWebSocket.disconnect();
    };
  }, [isAuthenticated]);

  const refreshConnectionStatus = useCallback(async () => {
    const userInfo = await checkSession();
    if (userInfo) {
      setGoogleServicesConnected(userInfo.google_services_connected);
      setHasAnyServiceConnected(userInfo.has_any_service_connected);
    }
  }, []);

  const refreshEnabledFeatures = useCallback(async () => {
    const userInfo = await checkSession();
    if (userInfo) {
      setEnabledFeatures(userInfo.enabled_features ?? []);
    }
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    isAuthenticated,
    isCheckingAuth,
    sessionSnapshot,
    userEmail,
    userName,
    isAdmin,
    enabledFeatures,
    refreshEnabledFeatures,
    hasPassword,
    setHasPassword,
    googleServicesConnected,
    hasAnyServiceConnected,
    refreshConnectionStatus,
    isImpersonating,
    impersonatorEmail,
    impersonatorName,
  }), [
    isAuthenticated,
    isCheckingAuth,
    sessionSnapshot,
    userEmail,
    userName,
    isAdmin,
    enabledFeatures,
    refreshEnabledFeatures,
    hasPassword,
    googleServicesConnected,
    hasAnyServiceConnected,
    refreshConnectionStatus,
    isImpersonating,
    impersonatorEmail,
    impersonatorName,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
