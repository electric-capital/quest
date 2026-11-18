// The AutoProject composer around the shared Composer: the host's
// placeholder, Send handing the host a submission (never sending on the
// draft key), the two lock states (busy: no Stop; disabled: Stop), the
// draft text kept per draft key, and the model seed.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { AppModelInfo } from '../../api/types';
import { setModelCatalog } from '../../constants/models';
import { ConversationModelsProvider, useConversationModels } from '../../contexts/ConversationModelsContext';
import { setAutoProjectDraft } from '../../store/autoprojectDrafts';
import { AutoProjectComposer, type AutoProjectComposerProps } from './AutoProjectComposer';

const MODEL = 'claude-sonnet-5';

const mocks = vi.hoisted(() => ({
  available: ['claude-sonnet-5', 'claude-opus-4-8'] as string[],
  auth: { sessionSnapshot: { default_model: null }, isAdmin: false, enabledFeatures: [] as string[] },
}));

vi.mock('../../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({
    appName: 'Atlas',
    availableModelIds: mocks.available,
    getAvailableModelIds: () => mocks.available,
    whenConfigLoaded: Promise.resolve(),
    maxUploadBytes: 1024,
    refreshAppConfig: vi.fn(),
  }),
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => mocks.auth,
}));

vi.mock('../../utils/auth', () => ({
  checkSession: async () => ({ default_model: null }),
}));

vi.mock('../../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({
    getQueuedSkillsForConversation: () => [],
    setQueuedSkillsForConversation: () => {},
    getLoadedSkillsForConversation: () => [],
    setLoadedSkillsForConversation: () => {},
    markSkillsAsLoaded: () => {},
  }),
}));

vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false }));

function model(id: string, slot: number): AppModelInfo {
  return {
    id,
    display_name: id,
    provider: 'anthropic',
    provider_label: 'Anthropic',
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

function DraftModel({ draftKey }: { draftKey: string }) {
  const { getModelForConversation } = useConversationModels();
  return <span data-testid="draft-model">{getModelForConversation(draftKey)}</span>;
}

function renderComposer(props: Partial<AutoProjectComposerProps> = {}) {
  const onSubmit = vi.fn();
  const all = { draftKey: '__autoproject__:ap1', onSubmit, placeholder: 'What should Atlas do next?', ...props };
  const ui = (p: typeof all) => (
    <ConversationModelsProvider>
      <AutoProjectComposer {...p} />
      <DraftModel draftKey={p.draftKey} />
    </ConversationModelsProvider>
  );
  const utils = render(ui(all));
  return { ...utils, onSubmit, rerenderWith: (next: Partial<AutoProjectComposerProps>) => utils.rerender(ui({ ...all, ...next })) };
}

function input(): HTMLTextAreaElement {
  return screen.getByRole('textbox') as HTMLTextAreaElement;
}

beforeEach(() => {
  setModelCatalog([model('claude-opus-4-8', 1), model(MODEL, 2)]);
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  // The kept drafts live in memory for the page's life.
  setAutoProjectDraft('__autoproject__:ap1', '');
  setAutoProjectDraft('__autoproject__:ap2', '');
});

describe('AutoProjectComposer', () => {
  it('shows the host placeholder and hands Send to the host as a submission', async () => {
    const { onSubmit } = renderComposer({ initialModel: MODEL });
    expect(input().placeholder).toBe('What should Atlas do next?');
    await waitFor(() => expect(screen.getByTestId('draft-model').textContent).toBe(MODEL));

    fireEvent.change(input(), { target: { value: '  Draft the summary  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSubmit).toHaveBeenCalledWith({
      text: 'Draft the summary',
      model: MODEL,
      thinkingEffort: null,
      skillIds: [],
      files: [],
      imageFiles: [],
    });
    expect(input().value).toBe('');
  });

  it('locks the input with Stop while a run works', () => {
    const onStop = vi.fn();
    renderComposer({ disabled: true, onStop, placeholder: 'Working...' });
    expect(input().disabled).toBe(true);
    expect(input().placeholder).toBe('Working...');
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('locks the input without Stop while a submit is in flight', () => {
    renderComposer({ busy: true, onStop: vi.fn(), placeholder: 'Starting...' });
    expect(input().disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('keeps the unsent text per draft key across remounts', () => {
    const { unmount } = renderComposer();
    fireEvent.change(input(), { target: { value: 'Half-written prompt' } });
    unmount();

    const { rerenderWith } = renderComposer();
    expect(input().value).toBe('Half-written prompt');

    rerenderWith({ draftKey: '__autoproject__:ap2' });
    expect(input().value).toBe('');
    rerenderWith({ draftKey: '__autoproject__:ap1' });
    expect(input().value).toBe('Half-written prompt');
  });

  it('seeds the draft model from the AutoProject and not again on a later prop change', async () => {
    const { rerenderWith } = renderComposer({ initialModel: 'claude-opus-4-8' });
    await waitFor(() => expect(screen.getByTestId('draft-model').textContent).toBe('claude-opus-4-8'));
    rerenderWith({ initialModel: MODEL });
    expect(screen.getByTestId('draft-model').textContent).toBe('claude-opus-4-8');
  });
});
