// AutoProjectMainColumn, the run side of the AutoProject page:
//
// - Deep link to a run (C5): while the run's update is not loaded, older
//   pages load only once the detail panel resolved the run (its seq), and
//   only while the oldest loaded update is newer than it. A run the panel
//   never resolves (another AutoProject's, deleted, a typo) pages nothing.
// - The newest update's visibility, reported to the page for its read rule
//   (`onNewestVisibleChange`), from the scroller's and the updates' boxes
//   (stubbed getBoundingClientRect: jsdom has no layout), and the header's
//   "Latest" jump while it is out of view.
// - The composer's routing: a prompt starts a run, an answer goes to the
//   pending question, Stop goes to the working run's conversation.
//
// The Updates card, header and composer are stubbed (own tests); the scroll
// positions themselves are not asserted (jsdom does not scroll).
import { forwardRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { AutoProject, AutoProjectQuestion, AutoProjectRun } from '../../api/types';
import type { AutoProjectRunsView } from '../../hooks/useAutoProjectRuns';
import type { AutoProjectHeaderJump } from './AutoProjectHeader';
import type { AutoProjectComposerSubmission } from './autoprojectComposerSend';
import { AutoProjectMainColumn } from './AutoProjectMainColumn';

const mocks = vi.hoisted(() => ({
  jump: undefined as AutoProjectHeaderJump | undefined,
  composerProps: null as null | {
    onSubmit: (s: AutoProjectComposerSubmission) => void;
    onStop?: () => void;
    placeholder?: string;
    disabled?: boolean;
    busy?: boolean;
  },
  startAutoProjectRunFromComposer: vi.fn(),
  answerAutoProjectQuestion: vi.fn(),
  stopAutoProjectRun: vi.fn(),
}));

vi.mock('./AutoProjectHeader', () => ({
  AutoProjectHeader: ({ jump }: { jump?: AutoProjectHeaderJump }) => {
    mocks.jump = jump;
    return <div data-testid="header">{jump ? `jump:${jump.target}:${jump.mark ?? '-'}` : 'no-jump'}</div>;
  },
}));
vi.mock('./AutoProjectComposer', () => ({
  AutoProjectComposer: (props: NonNullable<typeof mocks.composerProps>) => {
    mocks.composerProps = props;
    return <div data-testid="composer">{props.placeholder}</div>;
  },
}));
vi.mock('./AutoProjectUpdates', () => ({
  AutoProjectUpdates: forwardRef<HTMLElement, { runs: AutoProjectRunsView }>(function Updates({ runs }, ref) {
    // Recent first (the default order): newest at the top.
    const shown = [...runs.runs].reverse();
    return (
      <section ref={ref}>
        <div className="autoproject-updates-list">
          {shown.map((run) => (
            <article
              key={run.id}
              className="autoproject-run"
              data-run-id={run.id}
              data-conversation-id={run.conversation_id}
            >
              {run.prompt}
              {run.questions.some((q) => q.status === 'pending') && <section aria-label="Question">q</section>}
            </article>
          ))}
        </div>
      </section>
    );
  }),
}));
vi.mock('./autoprojectComposerSend', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./autoprojectComposerSend')>()),
  startAutoProjectRunFromComposer: mocks.startAutoProjectRunFromComposer,
}));
vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  answerAutoProjectQuestion: mocks.answerAutoProjectQuestion,
}));
vi.mock('../../services/autoprojectRuns', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/autoprojectRuns')>()),
  stopAutoProjectRun: mocks.stopAutoProjectRun,
}));
vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: { onGlobalEvent: () => () => {} },
}));
vi.mock('../../hooks/useAutoProjectRunActivity', () => ({
  useAutoProjectRunActivity: () => ({ label: 'Thinking' }),
}));
vi.mock('../../hooks/useAutoProjectUnread', () => ({
  useAutoProjectUnread: () => false,
}));
vi.mock('../../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({ appName: 'Acme' }),
}));
vi.mock('../../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({ setQueuedSkillsForConversation: () => {} }),
}));

const AUTOPROJECT = {
  id: 'ap1',
  title: 'Market study',
  archived: false,
  model: null,
  thinking_effort: null,
  run_count: 0,
  active_run: null,
  unread_seq: 0,
  read_seq: 0,
} as unknown as AutoProject;

function run(seq: number, overrides: Partial<AutoProjectRun> = {}): AutoProjectRun {
  return {
    id: `r${seq}`,
    seq,
    conversation_id: `c${seq}`,
    prompt: `Prompt ${seq}`,
    status: 'completed',
    summary: null,
    result: `Result ${seq}`,
    notes: null,
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

function seqs(from: number, to: number): AutoProjectRun[] {
  const out: AutoProjectRun[] = [];
  for (let s = from; s <= to; s += 1) out.push(run(s));
  return out;
}

function runsView(runs: AutoProjectRun[], overrides: Partial<AutoProjectRunsView> = {}): AutoProjectRunsView {
  return {
    runs,
    loading: false,
    error: null,
    notFound: false,
    hasOlder: true,
    loadingOlder: false,
    loadOlder: vi.fn(),
    refresh: vi.fn().mockResolvedValue(undefined),
    removeRun: vi.fn(),
    ...overrides,
  };
}

interface Props {
  runs: AutoProjectRunsView;
  openRunConversationId?: string | null;
  openRunSeq?: number | null;
  autoproject?: AutoProject;
  onNewestVisibleChange?: (visible: boolean) => void;
}

function Column({ runs, openRunConversationId = null, openRunSeq = null, autoproject = AUTOPROJECT, onNewestVisibleChange = () => {} }: Props) {
  return (
    <AutoProjectMainColumn
      autoprojectId="ap1"
      autoproject={autoproject}
      runs={runs}
      onAutoProjectDetail={() => {}}
      lead={<div>brief</div>}
      openRunConversationId={openRunConversationId}
      openRunSeq={openRunSeq}
      onViewRun={() => {}}
      onOpenFile={() => {}}
      onNewestVisibleChange={onNewestVisibleChange}
    />
  );
}

const submission = (text: string): AutoProjectComposerSubmission => ({
  text, model: null, thinkingEffort: null, skillIds: [], files: [], imageFiles: [],
});

beforeEach(() => {
  localStorage.clear();
  mocks.jump = undefined;
  mocks.composerProps = null;
  mocks.startAutoProjectRunFromComposer.mockReset();
  mocks.answerAutoProjectQuestion.mockReset();
  mocks.stopAutoProjectRun.mockReset().mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('AutoProjectMainColumn deep link paging (C5)', () => {
  it('pages nothing for a run the panel has not resolved, even with older pages left', () => {
    const view = runsView(seqs(31, 40));
    const { rerender } = render(<Column runs={view} openRunConversationId="c-unknown" openRunSeq={null} />);
    expect(view.loadOlder).not.toHaveBeenCalled();

    // Later refreshes of the list change nothing while the run is unresolved.
    const next = runsView(seqs(31, 41), { loadOlder: view.loadOlder });
    rerender(<Column runs={next} openRunConversationId="c-unknown" openRunSeq={null} />);
    expect(view.loadOlder).not.toHaveBeenCalled();
  });

  it('once resolved, pages until the oldest loaded update reaches the run, one page at a time', () => {
    const loadOlder = vi.fn();
    const props = { openRunConversationId: 'c-deleted-sibling', openRunSeq: 25 };
    const { rerender } = render(<Column runs={runsView(seqs(31, 40), { loadOlder })} {...props} openRunSeq={null} />);
    expect(loadOlder).not.toHaveBeenCalled();

    rerender(<Column runs={runsView(seqs(31, 40), { loadOlder })} {...props} />);
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // A page is loading: no second request.
    rerender(<Column runs={runsView(seqs(31, 40), { loadOlder, loadingOlder: true })} {...props} />);
    expect(loadOlder).toHaveBeenCalledTimes(1);

    // Still newer than the run: one more page.
    rerender(<Column runs={runsView(seqs(28, 40), { loadOlder })} {...props} />);
    expect(loadOlder).toHaveBeenCalledTimes(2);

    // The oldest loaded update is at or below the run's seq: stop, although
    // the run's own update is not among them and older pages remain.
    const reached = [...seqs(20, 24), ...seqs(26, 40)];
    rerender(<Column runs={runsView(reached, { loadOlder })} {...props} />);
    rerender(<Column runs={runsView([...reached], { loadOlder })} {...props} />);
    expect(loadOlder).toHaveBeenCalledTimes(2);
  });

  it('does not page when the run is already loaded, nor at the bottom of the list', () => {
    const loadOlder = vi.fn();
    render(<Column runs={runsView(seqs(31, 40), { loadOlder })} openRunConversationId="c33" openRunSeq={33} />);
    expect(loadOlder).not.toHaveBeenCalled();
    cleanup();

    render(
      <Column runs={runsView(seqs(31, 40), { loadOlder, hasOlder: false })} openRunConversationId="c5" openRunSeq={5} />,
    );
    expect(loadOlder).not.toHaveBeenCalled();
  });

  it('does not page while the first page is loading', () => {
    const loadOlder = vi.fn();
    render(
      <Column runs={runsView([], { loadOlder, loading: true })} openRunConversationId="c5" openRunSeq={5} />,
    );
    expect(loadOlder).not.toHaveBeenCalled();
  });
});

describe('AutoProjectMainColumn newest update visibility', () => {
  /** Boxes by element: the scroller is 0..600; each update's box from `updates`. */
  function stubLayout(updates: Record<string, { top: number; bottom: number }>) {
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const box = (top: number, bottom: number) =>
        ({ top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
      if (this.classList.contains('autoproject-page-scroller')) return box(0, 600);
      const id = (this as HTMLElement).dataset?.runId;
      const b = id ? updates[id] : undefined;
      return b ? box(b.top, b.bottom) : box(0, 0);
    });
  }

  it('reports the newest update off screen, offers Latest, then reports it on screen after a scroll', async () => {
    const layout: Record<string, { top: number; bottom: number }> = { r3: { top: 900, bottom: 1100 } };
    stubLayout(layout);
    const onNewestVisibleChange = vi.fn();
    const { container } = render(
      <Column
        runs={runsView(seqs(1, 3), { hasOlder: false })}
        autoproject={{ ...AUTOPROJECT, active_run: { id: 'r9', seq: 9, status: 'running' } } as AutoProject}
        onNewestVisibleChange={onNewestVisibleChange}
      />,
    );
    expect(onNewestVisibleChange.mock.calls).toEqual([[false]]);
    expect(screen.getByTestId('header').textContent).toBe('jump:latest:working');

    layout.r3 = { top: 200, bottom: 400 };
    const scroller = container.querySelector('.autoproject-page-scroller') as HTMLElement;
    fireEvent.scroll(scroller);
    await waitFor(() => expect(onNewestVisibleChange.mock.calls).toEqual([[false], [true]]));
    expect(screen.getByTestId('header').textContent).toBe('no-jump');
  });

  it('counts an update that only peeks in at the bottom edge as off screen', () => {
    stubLayout({ r2: { top: 590, bottom: 800 } });
    const onNewestVisibleChange = vi.fn();
    render(<Column runs={runsView(seqs(1, 2), { hasOlder: false })} onNewestVisibleChange={onNewestVisibleChange} />);
    expect(onNewestVisibleChange.mock.calls).toEqual([[false]]);
  });

  it('follows the stored order: oldest first, the newest update is the last one', () => {
    localStorage.setItem('quest_autoproject_updates_order', 'oldest');
    // The stub renders recent first, so the LAST element in the DOM is r1;
    // oldest first the column reads the last element as the newest.
    stubLayout({ r3: { top: 100, bottom: 300 }, r1: { top: 900, bottom: 1000 } });
    const onNewestVisibleChange = vi.fn();
    render(<Column runs={runsView(seqs(1, 3), { hasOlder: false })} onNewestVisibleChange={onNewestVisibleChange} />);
    expect(onNewestVisibleChange.mock.calls).toEqual([[false]]);
  });
});

describe('AutoProjectMainColumn composer routing', () => {
  const question = (handle: string, status: AutoProjectQuestion['status'] = 'pending'): AutoProjectQuestion => ({
    handle_id: handle, question: `Which ${handle}?`, options: [], answer: null, status, asked_at: null, answered_at: null,
  });

  it('starts a run with a prompt while nothing is active', async () => {
    mocks.startAutoProjectRunFromComposer.mockResolvedValue({ conversationId: 'c4', seq: 1 });
    const view = runsView(seqs(1, 3), { hasOlder: false });
    render(<Column runs={view} />);
    expect(screen.getByTestId('composer').textContent).toBe('What should Acme do next?');

    act(() => mocks.composerProps!.onSubmit(submission('Next step')));
    await waitFor(() => expect(mocks.startAutoProjectRunFromComposer).toHaveBeenCalledWith(
      'ap1', submission('Next step'), expect.any(Object),
    ));
    await waitFor(() => expect(view.refresh).toHaveBeenCalled());
  });

  it('sends the answer to the oldest pending question and labels the composer', async () => {
    mocks.answerAutoProjectQuestion.mockResolvedValue({});
    const waiting = run(4, {
      status: 'waiting', result: null, questions: [question('h0', 'answered'), question('h1'), question('h2')],
    });
    render(<Column runs={runsView([...seqs(1, 3), waiting], { hasOlder: false })} />);
    expect(screen.getByText('Answering the oldest question')).toBeTruthy();
    expect(screen.getByText('Which h1?')).toBeTruthy();
    // jsdom has neither CSS.escape nor scrollIntoView.
    vi.stubGlobal('CSS', { escape: (value: string) => value });
    const scrolled: Element[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this); };
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Show question' }));
    } finally {
      Element.prototype.scrollIntoView = original;
      vi.unstubAllGlobals();
    }
    expect(scrolled).toHaveLength(1);
    expect(scrolled[0].getAttribute('aria-label')).toBe('Question');
    expect(scrolled[0].closest('[data-run-id]')?.getAttribute('data-run-id')).toBe('r4');

    act(() => mocks.composerProps!.onSubmit(submission('Option B')));
    await waitFor(() => expect(mocks.answerAutoProjectQuestion).toHaveBeenCalledWith('ap1', 'h1', 'Option B'));
    expect(mocks.startAutoProjectRunFromComposer).not.toHaveBeenCalled();
  });

  it('locks the composer while a run works and stops that run', () => {
    const working = run(4, { status: 'running', result: null });
    render(<Column runs={runsView([...seqs(1, 3), working], { hasOlder: false })} />);
    expect(screen.getByTestId('composer').textContent).toBe('Working...');
    expect(mocks.composerProps!.disabled).toBe(true);

    act(() => mocks.composerProps!.onSubmit(submission('ignored')));
    expect(mocks.startAutoProjectRunFromComposer).not.toHaveBeenCalled();
    act(() => mocks.composerProps!.onStop!());
    expect(mocks.stopAutoProjectRun).toHaveBeenCalledWith('c4');
  });

  it('replaces the composer with Unarchive on an archived AutoProject', () => {
    render(<Column runs={runsView(seqs(1, 2), { hasOlder: false })} autoproject={{ ...AUTOPROJECT, archived: true }} />);
    expect(screen.getByText('This AutoProject is archived.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Unarchive' })).toBeTruthy();
    expect(screen.queryByTestId('composer')).toBeNull();
  });
});
