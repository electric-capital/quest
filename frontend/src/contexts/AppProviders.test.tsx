/**
 * Boundary checks for the split contexts: a consumer of one context must not
 * re-render when unrelated state changes in another, and the hydration order
 * guaranteed by the old monolithic provider still holds (the first
 * authenticated render already sees the user's default model).
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_MODEL_ID, getKnownModels } from '../constants/models';
import { AppProviders } from './AppProviders';
import { useAppConfig } from './AppConfigContext';
import { useAuth } from './AuthContext';
import { useConversationModels } from './ConversationModelsContext';
import { useFileBrowserState } from './FileBrowserStateContext';
import { useNavigationState } from './NavigationContext';

const mocks = vi.hoisted(() => ({
  checkSession: vi.fn(),
  fetchVersion: vi.fn(() => Promise.resolve({ git_hash: 'abc' })),
  fetchProjects: vi.fn(() => Promise.resolve({ projects: [] })),
  fetchGuides: vi.fn(() => Promise.resolve({ guides: [] })),
  wsConnect: vi.fn(),
  wsDisconnect: vi.fn(),
}));

vi.mock('../utils/auth', () => ({ checkSession: mocks.checkSession }));
vi.mock('../api/client', () => ({
  fetchVersion: mocks.fetchVersion,
  fetchProjects: mocks.fetchProjects,
  fetchGuides: mocks.fetchGuides,
}));
vi.mock('../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    connect: mocks.wsConnect,
    disconnect: mocks.wsDisconnect,
    onGlobalEvent: () => () => {},
  },
}));

// A non-default, non-deprecated, privately allowed model from the built-in
// catalog, so the hydration test can tell the user's pick from the placeholder.
const USER_MODEL = getKnownModels().find((m) => m.id !== DEFAULT_MODEL_ID && !m.deprecated && m.allowPrivate)!.id;

const SESSION_USER = {
  email: 'user@example.com',
  name: 'Test User',
  google_services_connected: true,
  has_any_service_connected: true,
  is_admin: false,
  is_impersonating: false,
  impersonator_email: null,
  impersonator_name: null,
  default_model: USER_MODEL,
  enabled_features: [],
  theme: null,
  color_theme: null,
};

function okJson(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

/** Counts its own renders and shows the auth email. */
function AuthProbe({ renders }: { renders: { count: number } }) {
  const { userEmail } = useAuth();
  renders.count += 1;
  return <div data-testid="auth-email">{userEmail ?? 'anonymous'}</div>;
}

function AppNameProbe({ renders }: { renders: { count: number } }) {
  const { appName } = useAppConfig();
  renders.count += 1;
  return <div data-testid="app-name">{appName}</div>;
}

function NavigationProbe({ renders }: { renders: { count: number } }) {
  const { activeConversationId } = useNavigationState();
  renders.count += 1;
  return <div data-testid="active">{activeConversationId ?? 'none'}</div>;
}

/** Exposes the file-browser setter and shows the current path. */
function FileBrowserProbe({
  renders,
  handle,
}: {
  renders: { count: number };
  handle: { set?: (path: string) => void };
}) {
  const { getFileBrowserState, setFileBrowserState } = useFileBrowserState();
  renders.count += 1;
  handle.set = (path) => setFileBrowserState('c1', { path, history: ['/', path], historyIndex: 1 });
  return <div data-testid="path">{getFileBrowserState('c1').path}</div>;
}

/** Records (isAuthenticated, defaultModel) for every render. */
function HydrationProbe({ log }: { log: { isAuthenticated: boolean; defaultModel: string }[] }) {
  const { isAuthenticated } = useAuth();
  const { defaultModel } = useConversationModels();
  const seen = useRef(log);
  seen.current.push({ isAuthenticated, defaultModel });
  return <div data-testid="default-model">{isAuthenticated ? defaultModel : 'signed-out'}</div>;
}

describe('AppProviders', () => {
  beforeEach(() => {
    mocks.checkSession.mockResolvedValue(SESSION_USER);
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/app/api/config') {
        return okJson({ quest_env: 'local', models: [], login_method: 'password' });
      }
      return okJson({});
    }));
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    mocks.checkSession.mockReset();
    mocks.wsConnect.mockClear();
    mocks.wsDisconnect.mockClear();
    localStorage.clear();
  });

  it('re-renders only the consumers of the context that changed', async () => {
    const authRenders = { count: 0 };
    const appNameRenders = { count: 0 };
    const navRenders = { count: 0 };
    const fileRenders = { count: 0 };
    const fileHandle: { set?: (path: string) => void } = {};

    render(
      <AppProviders>
        <AuthProbe renders={authRenders} />
        <AppNameProbe renders={appNameRenders} />
        <NavigationProbe renders={navRenders} />
        <FileBrowserProbe renders={fileRenders} handle={fileHandle} />
      </AppProviders>,
    );

    await waitFor(() => expect(screen.getByTestId('auth-email').textContent).toBe('user@example.com'));
    await waitFor(() => expect(screen.getByTestId('app-name').textContent).toBe('DevQuest'));
    // Let the post-auth loads (projects, guides gate) settle.
    await waitFor(() => expect(mocks.fetchProjects).toHaveBeenCalled());

    const before = {
      auth: authRenders.count,
      appName: appNameRenders.count,
      nav: navRenders.count,
      file: fileRenders.count,
    };

    act(() => fileHandle.set!('/reports'));

    expect(screen.getByTestId('path').textContent).toBe('/reports');
    expect(fileRenders.count).toBe(before.file + 1);
    expect(authRenders.count).toBe(before.auth);
    expect(appNameRenders.count).toBe(before.appName);
    expect(navRenders.count).toBe(before.nav);
  });

  it('hydrates the default model before the first authenticated render', async () => {
    const log: { isAuthenticated: boolean; defaultModel: string }[] = [];
    render(
      <AppProviders>
        <HydrationProbe log={log} />
      </AppProviders>,
    );

    await waitFor(() => expect(screen.getByTestId('default-model').textContent).toBe(USER_MODEL));

    const authenticatedRenders = log.filter((entry) => entry.isAuthenticated);
    expect(authenticatedRenders.length).toBeGreaterThan(0);
    // Never a signed-in frame still showing the pre-fetch placeholder.
    expect(authenticatedRenders.every((entry) => entry.defaultModel === USER_MODEL)).toBe(true);
  });

  it('opens the persistent socket once the session is live', async () => {
    render(
      <AppProviders>
        <AuthProbe renders={{ count: 0 }} />
      </AppProviders>,
    );
    await waitFor(() => expect(mocks.wsConnect).toHaveBeenCalledTimes(1));
  });

  it('waits for the server config before reporting a finished session check', async () => {
    // Hold the config response so the session check cannot settle first.
    let releaseConfig!: () => void;
    const configGate = new Promise<void>((resolve) => {
      releaseConfig = resolve;
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/app/api/config') {
        await configGate;
        return okJson({ quest_env: 'local', models: [], login_method: 'password' });
      }
      return okJson({});
    }));

    function SessionProbe() {
      const { isCheckingAuth } = useAuth();
      const { loginMethod } = useAppConfig();
      return <div data-testid="session">{isCheckingAuth ? 'checking' : `done:${loginMethod}`}</div>;
    }

    render(
      <AppProviders>
        <SessionProbe />
      </AppProviders>,
    );

    await waitFor(() => expect(mocks.checkSession).toHaveBeenCalled());
    // The session response is in, but the config is not: still checking.
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId('session').textContent).toBe('checking');

    await act(async () => {
      releaseConfig();
    });
    await waitFor(() => expect(screen.getByTestId('session').textContent).toBe('done:password'));
  });
});
