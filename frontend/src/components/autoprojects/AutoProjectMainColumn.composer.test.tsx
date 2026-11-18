// The AutoProject page's composer as AutoProjectMainColumn drives it: the
// start placeholder with the app name, "Working..." with Stop on the run's
// conversation, "Answering the question" (the oldest one) with "Show
// question" and a typed answer, the archived notice with Unarchive, the
// "Delete update" confirm, and the updates' sort order remembered per
// browser. The shared Composer is stubbed (AutoProjectComposer.test.tsx
// covers it); the run activity hook and the header are stubbed too.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ApiClientError } from '../../api/request';
import type { AutoProject } from '../../api/types';
import type { AutoProjectRunsView } from '../../hooks/useAutoProjectRuns';
import type { AutoProjectComposerProps } from './AutoProjectComposer';
import { AutoProjectMainColumn } from './AutoProjectMainColumn';
import { makeAutoProject, makeQuestion, makeRun, makeRunsView } from './autoprojectTestFixtures';

const ORDER_KEY = 'quest_autoproject_updates_order';

const mocks = vi.hoisted(() => ({
  answerAutoProjectQuestion: vi.fn(),
  deleteAutoProjectRun: vi.fn(),
  unarchiveAutoProject: vi.fn(),
  updateAutoProject: vi.fn(),
  startAutoProjectRunFromComposer: vi.fn(),
  stopAutoProjectRun: vi.fn(),
  composerProps: null as AutoProjectComposerProps | null,
}));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  answerAutoProjectQuestion: mocks.answerAutoProjectQuestion,
  deleteAutoProjectRun: mocks.deleteAutoProjectRun,
  unarchiveAutoProject: mocks.unarchiveAutoProject,
  updateAutoProject: mocks.updateAutoProject,
}));

vi.mock('../../services/autoprojectRuns', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/autoprojectRuns')>()),
  stopAutoProjectRun: mocks.stopAutoProjectRun,
}));

vi.mock('./autoprojectComposerSend', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./autoprojectComposerSend')>()),
  startAutoProjectRunFromComposer: mocks.startAutoProjectRunFromComposer,
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: { onGlobalEvent: () => () => {} },
}));

vi.mock('../../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({ appName: 'Atlas' }),
}));

vi.mock('../../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({ setQueuedSkillsForConversation: vi.fn() }),
}));

vi.mock('../../hooks/useAutoProjectRunActivity', () => ({
  useAutoProjectRunActivity: () => ({ label: 'Reading files' }),
}));

vi.mock('../../hooks/useAutoProjectUnread', () => ({
  useAutoProjectUnread: () => false,
}));

vi.mock('./AutoProjectHeader', () => ({
  AutoProjectHeader: () => null,
}));

// The composer stub: its lock state, its placeholder, Stop when offered,
// and a Send that submits whatever is in its input.
vi.mock('./AutoProjectComposer', () => ({
  AutoProjectComposer: (props: AutoProjectComposerProps) => {
    mocks.composerProps = props;
    return (
      <form
        aria-label="Composer"
        onSubmit={(e) => {
          e.preventDefault();
          const text = (e.currentTarget.elements.namedItem('prompt') as HTMLInputElement).value;
          props.onSubmit({ text, model: null, thinkingEffort: null, skillIds: [], files: [], imageFiles: [] });
        }}
      >
        <input name="prompt" aria-label="Prompt" placeholder={props.placeholder} disabled={props.busy || props.disabled} />
        {props.onStop ? (
          <button type="button" onClick={props.onStop}>Stop</button>
        ) : (
          <button type="submit">Send</button>
        )}
      </form>
    );
  },
}));

function renderColumn(autoproject: AutoProject, runs: AutoProjectRunsView) {
  const onAutoProjectDetail = vi.fn();
  const utils = render(
    <AutoProjectMainColumn
      autoprojectId={autoproject.id}
      autoproject={autoproject}
      runs={runs}
      onAutoProjectDetail={onAutoProjectDetail}
      lead={null}
      openRunConversationId={null}
      openRunSeq={null}
      onViewRun={vi.fn()}
      onOpenFile={vi.fn()}
      onNewestVisibleChange={vi.fn()}
    />,
  );
  return { ...utils, onAutoProjectDetail };
}

function prompt(): HTMLInputElement {
  return screen.getByLabelText('Prompt') as HTMLInputElement;
}

function send(text: string) {
  fireEvent.change(prompt(), { target: { value: text } });
  fireEvent.submit(screen.getByRole('form', { name: 'Composer' }));
}

const AP = makeAutoProject({ run_count: 2 });

beforeEach(() => {
  for (const fn of [
    mocks.answerAutoProjectQuestion,
    mocks.deleteAutoProjectRun,
    mocks.unarchiveAutoProject,
    mocks.updateAutoProject,
    mocks.startAutoProjectRunFromComposer,
    mocks.stopAutoProjectRun,
  ]) fn.mockReset();
  mocks.composerProps = null;
  localStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
  if (typeof globalThis.CSS === 'undefined' || typeof globalThis.CSS.escape !== 'function') {
    vi.stubGlobal('CSS', { escape: (s: string) => s });
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('AutoProject page composer', () => {
  it('starts the next update with the app name in the placeholder', async () => {
    let finish: (value: unknown) => void = () => {};
    mocks.startAutoProjectRunFromComposer.mockReturnValue(new Promise((r) => { finish = r; }));
    const runs = makeRunsView([makeRun(1)]);
    renderColumn(AP, runs);
    expect(prompt().placeholder).toBe('What should Atlas do next?');
    expect(prompt().disabled).toBe(false);

    send('Draft the summary');
    expect(mocks.startAutoProjectRunFromComposer).toHaveBeenCalledWith(
      'ap1',
      expect.objectContaining({ text: 'Draft the summary' }),
      expect.anything(),
    );
    // The optimistic entry shows at once and the composer locks without Stop.
    const pending = screen.getByRole('article', { name: 'New update' });
    expect(within(pending).getByText('Draft the summary')).toBeTruthy();
    expect(prompt().disabled).toBe(true);
    expect(prompt().placeholder).toBe('Starting...');
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();

    finish({ conversationId: 'conv-2' });
    await waitFor(() => expect(runs.refresh).toHaveBeenCalled());
  });

  it('asks for a prompt instead of starting an empty update', () => {
    renderColumn(AP, makeRunsView([]));
    send('');
    expect(mocks.startAutoProjectRunFromComposer).not.toHaveBeenCalled();
    expect(screen.getByText('Write a prompt first.')).toBeTruthy();
  });

  it('shows "Working..." with Stop on the running update\'s conversation', () => {
    mocks.stopAutoProjectRun.mockReturnValue(true);
    const running = makeRun(2, { status: 'running', result: null, finished_at: null });
    renderColumn({ ...AP, active_run: { id: 'run-2', seq: 2, status: 'running' } }, makeRunsView([makeRun(1), running]));
    expect(prompt().placeholder).toBe('Working...');
    expect(prompt().disabled).toBe(true);

    const composer = screen.getByRole('form', { name: 'Composer' });
    fireEvent.click(within(composer).getByRole('button', { name: 'Stop' }));
    expect(mocks.stopAutoProjectRun).toHaveBeenCalledWith('conv-2');
    expect(screen.getByRole('button', { name: 'Stopping...' })).toBeTruthy();
  });

  it('locks with Stop while an update waits on something other than a question', () => {
    // The approval / connect wording is autoprojectComposerState's (its own tests).
    const waiting = makeRun(2, { status: 'waiting', result: null });
    renderColumn({ ...AP, active_run: { id: 'run-2', seq: 2, status: 'waiting' } }, makeRunsView([waiting]));
    expect(prompt().placeholder).toBe('Waiting for you');
    expect(prompt().disabled).toBe(true);
    expect(within(screen.getByRole('form', { name: 'Composer' })).getByRole('button', { name: 'Stop' })).toBeTruthy();
    expect(screen.queryByText(/Answering/)).toBeNull();
  });

  it('locks without Stop while the active update is not loaded yet', () => {
    renderColumn({ ...AP, active_run: { id: 'run-9', seq: 9, status: 'running' } }, makeRunsView([makeRun(1)]));
    expect(prompt().placeholder).toBe('Working...');
    expect(prompt().disabled).toBe(true);
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('answers the pending question from the composer and shows its card on request', async () => {
    mocks.answerAutoProjectQuestion.mockResolvedValue(makeQuestion({ status: 'answered', answer: 'Q3' }));
    const waiting = makeRun(2, {
      status: 'waiting',
      result: null,
      questions: [makeQuestion({ handle_id: 'h7', question: 'Which quarter?' })],
    });
    const runs = makeRunsView([makeRun(1), waiting]);
    renderColumn({ ...AP, active_run: { id: 'run-2', seq: 2, status: 'waiting' } }, runs);

    const label = document.querySelector('.autoproject-composer-label')!;
    expect(within(label as HTMLElement).getByText('Answering the question')).toBeTruthy();
    expect(within(label as HTMLElement).getByText('Which quarter?')).toBeTruthy();
    expect(prompt().placeholder).toBe('Type your answer');
    expect(prompt().disabled).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Show question' }));
    const card = screen.getByRole('region', { name: 'Question' });
    expect(card.scrollIntoView).toHaveBeenCalled();
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.contexts[0]).toBe(card);

    send('Q3, the one that just closed');
    expect(mocks.answerAutoProjectQuestion).toHaveBeenCalledWith('ap1', 'h7', 'Q3, the one that just closed');
    expect(mocks.startAutoProjectRunFromComposer).not.toHaveBeenCalled();
    await waitFor(() => expect(runs.refresh).toHaveBeenCalled());
  });

  it('answers the oldest of several pending questions', () => {
    mocks.answerAutoProjectQuestion.mockResolvedValue(makeQuestion({ status: 'answered' }));
    const waiting = makeRun(2, {
      status: 'waiting',
      result: null,
      questions: [
        makeQuestion({ handle_id: 'h1', question: 'First?' }),
        makeQuestion({ handle_id: 'h2', question: 'Second?' }),
      ],
    });
    renderColumn({ ...AP, active_run: { id: 'run-2', seq: 2, status: 'waiting' } }, makeRunsView([waiting]));
    expect(screen.getByText('Answering the oldest question')).toBeTruthy();
    send('Yes');
    expect(mocks.answerAutoProjectQuestion).toHaveBeenCalledWith('ap1', 'h1', 'Yes');
  });

  it('reports an answer that came too late', async () => {
    mocks.answerAutoProjectQuestion.mockRejectedValue(
      new ApiClientError('resolved', 409, 'question_resolved', undefined, { detail: { status: 'cancelled' } }),
    );
    const waiting = makeRun(2, { status: 'waiting', result: null, questions: [makeQuestion()] });
    renderColumn({ ...AP, active_run: { id: 'run-2', seq: 2, status: 'waiting' } }, makeRunsView([waiting]));
    send('Q3');
    expect(await screen.findByText('This question was already answered or cancelled.')).toBeTruthy();
  });

  it('replaces the composer with Unarchive on an archived AutoProject', async () => {
    const unarchived = { ...AP, archived: false };
    mocks.unarchiveAutoProject.mockResolvedValue(unarchived);
    const { onAutoProjectDetail } = renderColumn({ ...AP, archived: true }, makeRunsView([makeRun(1)]));
    expect(screen.getByText('This AutoProject is archived.')).toBeTruthy();
    expect(screen.queryByRole('form', { name: 'Composer' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Unarchive' }));
    expect(mocks.unarchiveAutoProject).toHaveBeenCalledWith('ap1');
    await waitFor(() => expect(onAutoProjectDetail).toHaveBeenCalledWith(unarchived));
  });
});

describe('AutoProject page updates', () => {
  it('deletes an update after the confirm', async () => {
    mocks.deleteAutoProjectRun.mockResolvedValue({ success: true });
    const runs = makeRunsView([makeRun(1), makeRun(2)]);
    renderColumn(AP, runs);

    fireEvent.click(screen.getByRole('button', { name: 'Update 1 options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete update' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete update 1?' });
    expect(mocks.deleteAutoProjectRun).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete update' }));
    expect(mocks.deleteAutoProjectRun).toHaveBeenCalledWith('ap1', 'run-1');
    await waitFor(() => expect(runs.removeRun).toHaveBeenCalledWith('run-1'));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('keeps the confirm open with the reason when the update is still active', async () => {
    mocks.deleteAutoProjectRun.mockRejectedValue(new ApiClientError('active', 409, 'autoproject_run_active'));
    const runs = makeRunsView([makeRun(1)]);
    renderColumn(AP, runs);
    fireEvent.click(screen.getByRole('button', { name: 'Update 1 options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete update' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete update 1?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete update' }));
    expect((await within(dialog).findByRole('alert')).textContent).toBe(
      'This update is still in progress. Stop it before deleting it.',
    );
    expect(runs.removeRun).not.toHaveBeenCalled();
  });

  it('cancels the delete without calling the server', () => {
    renderColumn(AP, makeRunsView([makeRun(1)]));
    fireEvent.click(screen.getByRole('button', { name: 'Update 1 options' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete update' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(mocks.deleteAutoProjectRun).not.toHaveBeenCalled();
  });

  it('remembers the sort order in this browser for every AutoProject', () => {
    const runs = makeRunsView([makeRun(1), makeRun(2)]);
    const labels = () => screen.getAllByRole('article').map((a) => a.getAttribute('aria-label'));
    const { unmount } = renderColumn(AP, runs);
    expect(labels()).toEqual(['Update 2', 'Update 1']);

    fireEvent.click(screen.getByRole('button', { name: 'Oldest first' }));
    expect(labels()).toEqual(['Update 1', 'Update 2']);
    expect(localStorage.getItem(ORDER_KEY)).toBe('oldest');
    unmount();

    renderColumn(makeAutoProject({ id: 'ap2' }), makeRunsView([makeRun(1), makeRun(2)]));
    expect(labels()).toEqual(['Update 1', 'Update 2']);
  });

  it('falls back to recent first when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    renderColumn(AP, makeRunsView([makeRun(1), makeRun(2)]));
    const labels = () => screen.getAllByRole('article').map((a) => a.getAttribute('aria-label'));
    expect(labels()).toEqual(['Update 2', 'Update 1']);
    fireEvent.click(screen.getByRole('button', { name: 'Oldest first' }));
    expect(labels()).toEqual(['Update 1', 'Update 2']);
  });
});
