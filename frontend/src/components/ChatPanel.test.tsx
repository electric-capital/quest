// ChatPanel for an AutoProject run's conversation (origin "autoproject"):
// read-only, with the notice line in place of the composer (no disabled
// input box), linking to the AutoProject page only while the AutoProjects
// gate is open; the host's `header` replaces the conversation title unit.
// Other read-only origins keep the disabled composer. useConversation and
// the composer are stubbed: they have their own tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ChatPanel } from './ChatPanel';

const mocks = vi.hoisted(() => ({
  conversation: {} as Record<string, unknown>,
  enabledFeatures: [] as string[],
  onAutoProjectIdLoaded: vi.fn(),
  // Stable identities: effects depend on them.
  models: {
    getModelForConversation: () => 'm1',
    getThinkingEffortForConversation: () => null,
    hydrateModelForConversation: () => {},
    lockConversationProvider: () => {},
    isProviderLocked: () => false,
    refreshDefaultModel: async () => ({ model: 'm1', thinkingEffort: null }),
    persistDefaultModel: () => {},
  },
}));

vi.mock('../hooks/useConversation', () => ({
  useConversation: () => mocks.conversation,
}));
vi.mock('../hooks/useConversationUnread', () => ({ useMarkConversationViewed: () => {} }));
vi.mock('../hooks/useWorkspaceDownload', () => ({
  useWorkspaceDownload: () => ({ downloadWorkspaceFile: vi.fn() }),
}));
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ enabledFeatures: mocks.enabledFeatures }),
}));
vi.mock('../contexts/ConversationModelsContext', () => ({
  useConversationModels: () => mocks.models,
}));
vi.mock('../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({
    setQueuedSkillsForConversation: vi.fn(),
    setLoadedSkillsForConversation: vi.fn(),
  }),
}));
vi.mock('../contexts/NavigationContext', () => ({
  useNavigationState: () => ({
    pendingRoutineMessage: null,
    setPendingRoutineMessage: vi.fn(),
    pendingFirstMessage: null,
    setPendingFirstMessage: vi.fn(),
    scrollToMessageIndex: null,
    setScrollToMessageIndex: vi.fn(),
  }),
}));
vi.mock('./Composer', () => ({
  Composer: ({ isReadOnly }: { isReadOnly?: boolean }) => (
    <div data-testid="composer">{isReadOnly ? 'read-only' : 'editable'}</div>
  ),
}));
vi.mock('./ConversationHeader', () => ({
  ConversationHeader: ({ title }: { title: string }) => <div data-testid="conversation-header">{title}</div>,
}));
vi.mock('./MemorySuggestions', () => ({ MemorySuggestionsBar: () => null }));

function conversation(overrides: Record<string, unknown> = {}) {
  return {
    messages: [],
    isStreaming: false,
    partialResponse: '',
    streamingMessages: [],
    error: null,
    isLoaded: true,
    isLoading: false,
    sendMessage: vi.fn(),
    subAgentToolCalls: {},
    subAgentReturned: {},
    contextTokens: 0,
    maxContextTokens: 0,
    origin: null,
    subagentRun: null,
    slackChannelId: null,
    slackThreadTs: null,
    slackTeamId: null,
    autoprojectId: null,
    conversationModel: null,
    conversationFlags: [],
    hasPendingWait: false,
    pendingWaitKind: null,
    pendingWaitCount: 0,
    expensiveResume: null,
    expensiveResumeBlocked: false,
    acknowledgeExpensiveResume: vi.fn(),
    clearExpensiveResume: vi.fn(),
    pendingMemorySuggestions: [],
    pendingProjectMemorySuggestions: [],
    pendingDocNoteSuggestions: [],
    customName: null,
    setCustomName: vi.fn(),
    archived: false,
    setArchived: vi.fn(),
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderPanel(header?: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={['/autoprojects/ap1/runs/c1']}>
      <ChatPanel
        conversationId="c1"
        onAutoProjectIdLoaded={mocks.onAutoProjectIdLoaded}
        {...(header !== undefined ? { header } : {})}
      />
      <LocationProbe />
    </MemoryRouter>,
  );
}

const NOTICE = 'This is the work behind one update of an AutoProject.';

beforeEach(() => {
  mocks.enabledFeatures = [];
  mocks.onAutoProjectIdLoaded.mockReset();
  Element.prototype.scrollTo = vi.fn() as unknown as typeof Element.prototype.scrollTo;
});

afterEach(cleanup);

describe('ChatPanel for an AutoProject run', () => {
  it('shows the notice with a link to the AutoProject instead of the composer while the gate is open', () => {
    mocks.enabledFeatures = ['autoprojects'];
    mocks.conversation = conversation({ origin: 'autoproject', autoprojectId: 'ap1' });
    renderPanel();

    expect(screen.queryByTestId('composer')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText(/This is the work behind one update of an AutoProject\./)).toBeTruthy();
    const link = screen.getByRole('link', { name: 'Continue in the AutoProject.' });
    fireEvent.click(link);
    expect(screen.getByTestId('location').textContent).toBe('/autoprojects/ap1');
    expect(mocks.onAutoProjectIdLoaded).toHaveBeenCalledWith('c1', 'ap1');
  });

  it('keeps the notice without a link while the gate is closed', () => {
    mocks.conversation = conversation({ origin: 'autoproject', autoprojectId: 'ap1' });
    renderPanel();
    expect(screen.queryByTestId('composer')).toBeNull();
    expect(screen.getByText(NOTICE)).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it("shows the host's header in place of the conversation title unit", () => {
    mocks.conversation = conversation({ origin: 'autoproject', autoprojectId: 'ap1' });
    renderPanel(<p>Failed: provider error</p>);
    expect(screen.getByText('Failed: provider error')).toBeTruthy();
    expect(screen.queryByTestId('conversation-header')).toBeNull();
  });

  it('keeps the read-only composer for other read-only origins, and the title unit without a header', () => {
    mocks.conversation = conversation({ origin: 'inference_api' });
    renderPanel();
    expect(screen.getByTestId('composer').textContent).toBe('read-only');
    expect(screen.getByTestId('conversation-header')).toBeTruthy();
    expect(screen.queryByText(NOTICE)).toBeNull();
    expect(mocks.onAutoProjectIdLoaded).not.toHaveBeenCalled();
  });

  it('keeps an ordinary conversation editable', () => {
    mocks.conversation = conversation({ origin: 'web' });
    renderPanel();
    expect(screen.getByTestId('composer').textContent).toBe('editable');
  });
});
