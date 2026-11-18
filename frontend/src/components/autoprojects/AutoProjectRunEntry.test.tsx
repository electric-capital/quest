// One update on the AutoProject page: the prompt clamp and its toggle, the
// outcome line per finished status, "View details" / "Close details", the
// row menu's "Delete update" (finished runs only) and the active run's
// activity line with Stop.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { AutoProjectRun } from '../../api/types';
import { AutoProjectRunEntry, type AutoProjectRunActivityView } from './AutoProjectRunEntry';
import { makeQuestion, makeRun } from './autoprojectTestFixtures';

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  answerAutoProjectQuestion: vi.fn(),
}));

function renderEntry(
  run: AutoProjectRun,
  { activity = null, isOpen = false }: { activity?: AutoProjectRunActivityView | null; isOpen?: boolean } = {},
) {
  const handlers = { onStop: vi.fn(), onDelete: vi.fn(), onView: vi.fn() };
  const utils = render(
    <AutoProjectRunEntry
      autoprojectId="ap1"
      run={run}
      cards={[]}
      activity={activity}
      answeringHandleId={null}
      onQuestionAnswered={vi.fn()}
      onOpenFile={vi.fn()}
      isOpen={isOpen}
      claimArrival={() => false}
      {...handlers}
    />,
  );
  return { ...utils, ...handlers };
}

/** jsdom lays nothing out: make every element report `scroll` px of content in a `client` px box. */
function fakeLayout(scroll: number, client: number) {
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(scroll);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(client);
}

const LONG_PROMPT = 'word '.repeat(200);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('AutoProjectRunEntry prompt', () => {
  it('clamps a long prompt that overflows and toggles it open and closed', () => {
    fakeLayout(800, 200);
    const { container } = renderEntry(makeRun(1, { prompt: LONG_PROMPT }));
    const request = container.querySelector('.autoproject-run-request')!;
    const toggle = screen.getByRole('button', { name: 'Show full prompt' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(request.classList.contains('clamped')).toBe(true);

    fireEvent.click(toggle);
    expect(screen.getByRole('button', { name: 'Show less' }).getAttribute('aria-expanded')).toBe('true');
    expect(request.classList.contains('clamped')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Show less' }));
    expect(screen.getByRole('button', { name: 'Show full prompt' })).toBeTruthy();
  });

  it('offers no toggle for a short prompt or a long one that fits', () => {
    fakeLayout(200, 200);
    renderEntry(makeRun(1, { prompt: LONG_PROMPT }));
    expect(screen.queryByRole('button', { name: 'Show full prompt' })).toBeNull();
    cleanup();

    fakeLayout(800, 200);
    renderEntry(makeRun(2, { prompt: 'Short' }));
    expect(screen.getByText('Short')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Show full prompt' })).toBeNull();
  });

  it('treats a prompt of many short lines as long', () => {
    fakeLayout(800, 200);
    renderEntry(makeRun(1, { prompt: Array.from({ length: 13 }, (_, i) => `line ${i}`).join('\n') }));
    expect(screen.getByRole('button', { name: 'Show full prompt' })).toBeTruthy();
  });
});

describe('AutoProjectRunEntry outcome', () => {
  it('shows the result of a completed update', () => {
    renderEntry(makeRun(1, { result: 'The **report** is done.' }));
    expect(screen.getByText('report').tagName).toBe('STRONG');
  });

  it.each([
    ['stopped', { status: 'stopped', result: null }, 'Stopped.'],
    ['completed without a result', { result: null }, 'This update finished without a result.'],
    ['failed without an error text', { status: 'failed', result: null, error: null }, 'Failed: unknown error'],
    ['failed', { status: 'failed', result: null, error: 'Provider timed out' }, 'Failed: Provider timed out'],
  ] as const)('shows the state line of a %s update', (_name, overrides, text) => {
    renderEntry(makeRun(1, overrides as Partial<AutoProjectRun>));
    expect(screen.getByText(text)).toBeTruthy();
  });

  it('keeps a long failure to one line with the full text behind "Show details"', () => {
    const error = 'Upstream error 529: overloaded\nTraceback line 1\nTraceback line 2';
    renderEntry(makeRun(1, { status: 'failed', result: null, error }));
    expect(screen.getByText('Failed: Upstream error 529: overloaded')).toBeTruthy();
    expect(screen.queryByText(/Traceback line 2/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show details' }));
    expect(screen.getByText(/Traceback line 2/)).toBeTruthy();
  });

  it('says a run that ended without a handoff shows its final text as the result', () => {
    renderEntry(makeRun(1, { handoff_missing: true, result: 'Final text', notes: null }));
    expect(screen.getByText(/ended without a handoff; its final text is shown as the result/)).toBeTruthy();
    expect(screen.getByText('Final text')).toBeTruthy();
  });

  it('lists the questions the update asked, answered ones included', () => {
    renderEntry(makeRun(1, {
      questions: [makeQuestion({ status: 'answered', answer: 'Q3', question: 'Which quarter?' })],
    }));
    const card = screen.getByRole('region', { name: 'Question' });
    expect(within(card).getByText('Which quarter?')).toBeTruthy();
    expect(within(card).getByText('Q3')).toBeTruthy();
  });
});

describe('AutoProjectRunEntry details and menu', () => {
  it('toggles the details panel with "View details" / "Close details"', () => {
    const run = makeRun(2);
    const { onView, rerender } = renderEntry(run);
    const view = screen.getByRole('button', { name: 'View details' });
    expect(view.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(view);
    expect(onView).toHaveBeenCalledWith('conv-2');

    rerender(
      <AutoProjectRunEntry
        autoprojectId="ap1"
        run={run}
        cards={[]}
        activity={null}
        answeringHandleId={null}
        onStop={vi.fn()}
        onQuestionAnswered={vi.fn()}
        onOpenFile={vi.fn()}
        onDelete={vi.fn()}
        isOpen
        onView={onView}
        claimArrival={() => false}
      />,
    );
    expect(screen.getByRole('button', { name: 'Close details' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('article', { name: 'Update 2' }).getAttribute('aria-current')).toBe('true');
  });

  it('offers "Delete update" in the menu of a finished update', () => {
    const run = makeRun(2);
    const { onDelete } = renderEntry(run);
    const toggle = screen.getByRole('button', { name: 'Update 2 options' });
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete update' }));
    expect(onDelete).toHaveBeenCalledWith(run);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('closes the menu on Escape and returns focus to its button', () => {
    renderEntry(makeRun(2));
    const toggle = screen.getByRole('button', { name: 'Update 2 options' });
    fireEvent.click(toggle);
    expect(screen.getByRole('menu')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
    expect(document.activeElement).toBe(toggle);
  });

  it('shows the activity line with Stop, and no menu, while the update works', () => {
    const run = makeRun(3, { status: 'running', result: null, finished_at: null });
    const { onStop } = renderEntry(run, {
      activity: { label: 'Reading files', waiting: false, stopping: false, stopError: null },
    });
    expect(screen.queryByRole('button', { name: 'Update 3 options' })).toBeNull();
    const line = screen.getByTestId('autoproject-activity-line');
    expect(within(line).getByText('Reading files')).toBeTruthy();
    fireEvent.click(within(line).getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalledWith('conv-3');
  });

  it('shows "Stopping..." and a stop error on the activity line', () => {
    renderEntry(makeRun(3, { status: 'running', result: null }), {
      activity: { label: 'Thinking', waiting: false, stopping: true, stopError: 'Not connected' },
    });
    expect((screen.getByRole('button', { name: 'Stopping...' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('alert').textContent).toBe('Not connected');
  });
});
