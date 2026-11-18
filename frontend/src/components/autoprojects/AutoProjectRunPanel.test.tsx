// A run in the AutoProject page's detail panel: "Update N" with its status,
// the Transcript / Handoff notes / Chat Files tabs (Handoff notes only for a
// finished run that left notes), the failed / stopped line above the
// transcript, the resolved seq reported to the page, and the not-found note
// for a conversation that is not a run of this AutoProject (no transcript
// mounted). useAutoProjectRun, ChatPanel, FileBrowser and the markdown are
// stubbed: each has its own tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { AutoProject, AutoProjectRun } from '../../api/types';
import type { AutoProjectRunLookup } from '../../hooks/useAutoProjectRun';
import type { WorkspaceCopy } from '../../hooks/useWorkspaceCopy';
import { AutoProjectRunPanel } from './AutoProjectRunPanel';

const mocks = vi.hoisted(() => ({
  lookup: null as AutoProjectRunLookup | null,
  useAutoProjectRun: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock('../../hooks/useAutoProjectRun', () => ({
  useAutoProjectRun: (...args: unknown[]) => {
    mocks.useAutoProjectRun(...args);
    return mocks.lookup;
  },
}));
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('../ChatPanel', () => ({
  ChatPanel: ({ conversationId, header }: { conversationId: string; header?: React.ReactNode }) => (
    <div data-testid="chat-panel">
      {header}
      transcript:{conversationId}
    </div>
  ),
}));
vi.mock('../FileBrowser', () => ({
  FileBrowser: ({ source, compact }: { source: { kind: string; id: string }; compact?: boolean }) => (
    <div data-testid="file-browser">{source.kind}:{source.id}:{compact ? 'compact' : 'full'}</div>
  ),
}));
vi.mock('./AutoProjectBriefMarkdown', () => ({
  AutoProjectBriefMarkdown: ({ content }: { content: string }) => <div data-testid="notes-markdown">{content}</div>,
}));

const AUTOPROJECT = { id: 'ap1', title: 'Market study' } as AutoProject;

const COPY: WorkspaceCopy = {
  chatRowActions: () => [],
  notices: { conversation: null },
  dismissNotice: vi.fn(),
} as unknown as WorkspaceCopy;

function run(overrides: Partial<AutoProjectRun> = {}): AutoProjectRun {
  return {
    id: 'r3',
    seq: 3,
    conversation_id: 'c3',
    prompt: 'Find the top competitors',
    status: 'completed',
    summary: 'Listed five competitors',
    result: 'Five competitors.',
    notes: 'Pricing pages are in autoproject://pricing/.',
    handoff_missing: false,
    error: null,
    model: null,
    started_at: '2026-11-10T10:00:00',
    finished_at: '2026-11-10T10:05:00',
    questions: [],
    open_requests: [],
    ...overrides,
  };
}

function serve(partial: Partial<AutoProjectRunLookup>) {
  mocks.lookup = { run: null, loading: false, notFound: false, error: null, refresh: mocks.refresh, ...partial };
}

function renderPanel(conversationId = 'c3') {
  const onResolved = vi.fn();
  const onClose = vi.fn();
  render(
    <AutoProjectRunPanel
      autoproject={AUTOPROJECT}
      changeSeq={4}
      conversationId={conversationId}
      copy={COPY}
      onResolved={onResolved}
      onClose={onClose}
      onOpenFile={vi.fn()}
    />,
  );
  return { onResolved, onClose };
}

const tabs = () => screen.getAllByRole('tab').map((t) => t.textContent);

beforeEach(() => {
  mocks.useAutoProjectRun.mockReset();
  mocks.refresh.mockReset().mockResolvedValue(undefined);
});

afterEach(cleanup);

describe('AutoProjectRunPanel', () => {
  it('shows a finished run with notes: Transcript first, then Handoff notes and Chat Files', () => {
    serve({ run: run() });
    const { onResolved } = renderPanel();

    expect(mocks.useAutoProjectRun).toHaveBeenCalledWith('ap1', 'c3', 4);
    expect(onResolved).toHaveBeenCalledWith('c3', 3);
    expect(screen.getByRole('heading', { name: 'Update 3' })).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('Completed');
    expect(tabs()).toEqual(['Transcript', 'Handoff notes', 'Chat Files']);
    expect(screen.getByRole('tab', { name: 'Transcript' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('chat-panel').textContent).toContain('transcript:c3');

    fireEvent.click(screen.getByRole('tab', { name: 'Handoff notes' }));
    expect(screen.getByRole('tabpanel', { name: 'Handoff notes' })).toBeTruthy();
    expect(screen.getByTestId('notes-markdown').textContent).toBe('Pricing pages are in autoproject://pricing/.');
    expect(screen.queryByTestId('chat-panel')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: 'Chat Files' }));
    expect(screen.getByTestId('file-browser').textContent).toBe('conversation:c3:compact');
  });

  it.each([
    ['a working run', run({ status: 'running', notes: null, result: null }), 'Working'],
    ['a run that waits on the user', run({ status: 'waiting', notes: null, result: null }), 'Needs you'],
    ['a finished run with empty notes', run({ notes: '   ' }), 'Completed'],
    ['a run that ended without a handoff', run({ notes: null, handoff_missing: true }), 'Completed without handoff'],
  ])('has no Handoff notes tab for %s', (_name, r, status) => {
    serve({ run: r });
    renderPanel();
    expect(tabs()).toEqual(['Transcript', 'Chat Files']);
    expect(screen.getByRole('status').textContent).toBe(status);
  });

  it('shows a failed run\'s error and a stopped run\'s line above the transcript', () => {
    serve({ run: run({ status: 'failed', notes: null, error: 'Provider error: overloaded' }) });
    renderPanel();
    expect(screen.getByRole('status').textContent).toBe('Failed');
    expect(screen.getByTestId('chat-panel').textContent).toContain('Provider error: overloaded');
    cleanup();

    serve({ run: run({ status: 'stopped', notes: null }) });
    renderPanel();
    expect(screen.getByRole('status').textContent).toBe('Stopped');
    expect(screen.getByTestId('chat-panel').textContent).toContain('Stopped by you.');
  });

  it('shows the not-found note, no tabs and no transcript for a run outside this AutoProject', () => {
    serve({ notFound: true });
    const { onResolved } = renderPanel('other-conv');
    expect(screen.getByText('This update does not exist, was deleted, or belongs to another AutoProject.')).toBeTruthy();
    expect(screen.queryByRole('tab')).toBeNull();
    expect(screen.queryByTestId('chat-panel')).toBeNull();
    expect(onResolved).toHaveBeenCalledWith('other-conv', null);
  });

  it('says it is loading, and offers Retry on a failed lookup', () => {
    serve({ loading: true });
    renderPanel();
    expect(screen.getByRole('status').textContent).toBe('Loading update...');
    cleanup();

    serve({ error: { message: 'Failed to load the update.' } as AutoProjectRunLookup['error'] });
    renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mocks.refresh).toHaveBeenCalledTimes(1);
  });

  it('closes on Escape and on the close button', () => {
    serve({ run: run() });
    const { onClose } = renderPanel();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Close update 3' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
