// Composer on a fresh install: the admin connects a provider from the setup
// card while the new chat's model was resolved before any model existed. The
// chat must move to a model the new provider serves, or its first message
// fails on a Vertex model this install cannot reach.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { AppModelInfo } from '../api/types';
import { setModelCatalog } from '../constants/models';
import { ConversationModelsProvider, useConversationModels } from '../contexts/ConversationModelsContext';
import { Composer } from './Composer';

const INSTANCE_MODEL = 'anthropic:claude-sonnet-5';

const mocks = vi.hoisted(() => ({
  available: [] as string[],
  // Stable identities: a fresh snapshot per render would re-hydrate forever.
  auth: { sessionSnapshot: { default_model: null }, isAdmin: true, enabledFeatures: [] as string[] },
  refreshAppConfig: vi.fn(),
}));

vi.mock('../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({
    availableModelIds: mocks.available,
    getAvailableModelIds: () => mocks.available,
    whenConfigLoaded: Promise.resolve(),
    maxUploadBytes: 1024,
    refreshAppConfig: mocks.refreshAppConfig,
  }),
}));

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => mocks.auth,
}));

vi.mock('../utils/auth', () => ({
  checkSession: async () => ({ default_model: null }),
}));

vi.mock('../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({
    getQueuedSkillsForConversation: () => [],
    setQueuedSkillsForConversation: () => {},
    getLoadedSkillsForConversation: () => [],
    markSkillsAsLoaded: () => {},
  }),
}));

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }));

// The card itself is covered by ModelSetupCard.test.tsx; here it only reports
// that a provider was connected.
vi.mock('./ModelSetupCard', () => ({
  ModelSetupCard: ({ onConnected }: { onConnected: () => void }) => (
    <button onClick={() => void onConnected()}>Connect</button>
  ),
}));

function model(id: string, provider: string, slot: number | null = null): AppModelInfo {
  return {
    id,
    display_name: id,
    provider,
    provider_label: provider,
    max_input_tokens: 200000,
    deprecated: false,
    thinking_efforts: [],
    default_thinking_effort: null,
    slot,
    public_slot: slot,
    descriptor: '',
    slot_effort: null,
    allow_private: true,
    allow_public: true,
  };
}

// The conversation's current model, read from the same context the composer uses.
function Harness({ isFirstMessage }: { isFirstMessage: boolean }) {
  const { getModelForConversation } = useConversationModels();
  return (
    <>
      <span data-testid="model">{getModelForConversation('c1')}</span>
      <Composer conversationId="c1" onSend={() => {}} isFirstMessage={isFirstMessage} />
    </>
  );
}

function renderComposer(isFirstMessage = true) {
  return render(
    <ConversationModelsProvider>
      <Harness isFirstMessage={isFirstMessage} />
    </ConversationModelsProvider>,
  );
}

describe('Composer provider setup', () => {
  beforeEach(() => {
    // A fresh install: the Vertex registry models carry the default slots,
    // and the only model that will work is the one the quick set-up adds.
    setModelCatalog([
      model('claude-opus-4-8', 'anthropic', 1),
      model('claude-sonnet-5', 'anthropic', 2),
      model(INSTANCE_MODEL, 'anthropic'),
    ]);
    mocks.available = [];
    mocks.refreshAppConfig.mockImplementation(async () => {
      mocks.available = [INSTANCE_MODEL];
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('moves a new chat to the connected provider\'s model', async () => {
    renderComposer();
    // Resolved while nothing was available: the Vertex default.
    expect(screen.getByTestId('model').textContent).toBe('claude-opus-4-8');
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(screen.getByTestId('model').textContent).toBe(INSTANCE_MODEL));
  });

  it('leaves a conversation that already has messages on its own model', async () => {
    renderComposer(false);
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(mocks.refreshAppConfig).toHaveBeenCalled());
    expect(screen.getByTestId('model').textContent).toBe('claude-opus-4-8');
  });
});
