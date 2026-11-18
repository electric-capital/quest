// The AutoProject page's read rule: the AutoProject is reported as seen (and the
// unread store then acknowledges it, useMarkAutoProjectViewed) only once its
// newest update has been on screen or the detail panel shows the newest run
// (once the panel resolved it), never just because the page opened at the
// brief; on a phone the full-screen panel covers the column, so a visible
// newest update behind it does not count. An AutoProject without updates
// counts as seen. Everything around the rule is stubbed: the main column
// reports visibility through its own buttons here.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { AutoProject, AutoProjectRun } from '../../api/types';
import { AutoProjectPage } from './AutoProjectPage';

const mocks = vi.hoisted(() => ({
  isMobile: false,
  runs: [] as AutoProjectRun[],
  seen: [] as boolean[],
  resolveSeq: null as number | null,
}));

vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => mocks.isMobile }));
vi.mock('../../hooks/useAutoProject', () => ({
  useAutoProject: () => ({
    autoproject: { id: 'ap1', title: 'Market study', archived: false } as AutoProject,
    loading: false,
    error: null,
    notFound: false,
    refresh: vi.fn(),
    changeSeq: 0,
    applyBrief: vi.fn(),
    applyDetail: vi.fn(),
  }),
}));
vi.mock('../../hooks/useAutoProjectRuns', () => ({
  useAutoProjectRuns: () => ({
    runs: mocks.runs, loading: false, error: null, notFound: false, hasOlder: false, loadingOlder: false,
    loadOlder: vi.fn(), refresh: vi.fn(), removeRun: vi.fn(),
  }),
}));
vi.mock('../../hooks/useAutoProjectUnread', () => ({
  useMarkAutoProjectViewed: (_id: string, seen: boolean) => {
    mocks.seen.push(seen);
  },
}));
vi.mock('../../hooks/useWorkspaceCopy', () => ({
  useWorkspaceCopy: () => ({
    notices: { conversation: null, autoproject: null },
    dismissNotice: vi.fn(),
    overwritePrompt: null,
    overwriteBusy: false,
    overwriteError: null,
    confirmOverwrite: vi.fn(),
    cancelOverwrite: vi.fn(),
  }),
  overwritePromptText: () => null,
}));
vi.mock('../../hooks/useWorkspaceDownload', () => ({ useWorkspaceDownload: () => vi.fn() }));
vi.mock('../FileBrowser', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../FileBrowser')>()),
  FileBrowser: () => null,
}));
vi.mock('./AutoProjectBrief', () => ({ AutoProjectBrief: () => null }));
vi.mock('./AutoProjectMainColumn', () => ({
  AutoProjectMainColumn: ({ onNewestVisibleChange }: { onNewestVisibleChange: (v: boolean) => void }) => (
    <div>
      <button type="button" onClick={() => onNewestVisibleChange(true)}>newest on screen</button>
      <button type="button" onClick={() => onNewestVisibleChange(false)}>newest off screen</button>
    </div>
  ),
}));
vi.mock('./AutoProjectRunPanel', async () => {
  const { useEffect } = await import('react');
  return {
    AutoProjectRunPanel: ({ conversationId, onResolved }: {
      conversationId: string;
      onResolved: (c: string, seq: number | null) => void;
    }) => {
      useEffect(() => {
        onResolved(conversationId, mocks.resolveSeq);
      }, [conversationId, onResolved]);
      return <div data-testid="run-panel">{conversationId}</div>;
    },
  };
});

function run(seq: number): AutoProjectRun {
  return { id: `r${seq}`, seq, conversation_id: `c${seq}` } as AutoProjectRun;
}

function renderPage(runConversationId: string | null = null) {
  return render(
    <MemoryRouter initialEntries={['/autoprojects/ap1']}>
      <AutoProjectPage autoprojectId="ap1" runConversationId={runConversationId} />
    </MemoryRouter>,
  );
}

const lastSeen = () => mocks.seen[mocks.seen.length - 1];

beforeEach(() => {
  mocks.isMobile = false;
  mocks.runs = [run(1), run(2), run(3)];
  mocks.seen = [];
  mocks.resolveSeq = null;
});

afterEach(cleanup);

describe('AutoProjectPage read rule', () => {
  it('does not count opening the page as seen, only the newest update on screen', () => {
    renderPage();
    expect(mocks.seen.every((s) => s === false)).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'newest on screen' }));
    expect(lastSeen()).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'newest off screen' }));
    expect(lastSeen()).toBe(false);
  });

  it('counts the newest run open in the panel as seen once it resolved', () => {
    mocks.resolveSeq = 3;
    renderPage('c3');
    expect(screen.getByTestId('run-panel')).toBeTruthy();
    expect(lastSeen()).toBe(true);
  });

  it('does not count an older run in the panel, nor a run the panel did not resolve', () => {
    mocks.resolveSeq = 2;
    renderPage('c2');
    expect(mocks.seen.every((s) => s === false)).toBe(true);
    cleanup();

    mocks.seen = [];
    mocks.resolveSeq = null;
    renderPage('c3');
    expect(mocks.seen.every((s) => s === false)).toBe(true);
  });

  it('on a phone, a newest update behind the full-screen panel does not count', () => {
    mocks.isMobile = true;
    mocks.resolveSeq = 1;
    renderPage('c1');
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'newest on screen' }));
    });
    expect(mocks.seen.every((s) => s === false)).toBe(true);
  });

  it('counts an AutoProject without updates as seen', () => {
    mocks.runs = [];
    renderPage();
    expect(lastSeen()).toBe(true);
  });
});
