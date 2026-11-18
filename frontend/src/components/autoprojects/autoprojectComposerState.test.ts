// The AutoProject page's composer state machine (spec 8.2 item 4): start,
// answering the (oldest) pending question, locked while a run works or waits
// on cards, the detail's active_run as a fallback, and the archived state.
import { describe, expect, it } from 'vitest';
import type { AutoProjectQuestion, AutoProjectRun } from '../../api/types';
import {
  ANSWER_PLACEHOLDER,
  deriveAutoProjectComposerState,
  findActiveRun,
  oldestPendingQuestion,
  waitingOnPhrase,
  type AutoProjectComposerStateInput,
} from './autoprojectComposerState';

function question(handle_id: string, status: AutoProjectQuestion['status'] = 'pending'): AutoProjectQuestion {
  return {
    handle_id,
    question: `Question ${handle_id}?`,
    options: [],
    answer: status === 'answered' ? 'yes' : null,
    status,
    asked_at: '2026-11-10T10:00:00Z',
    answered_at: null,
  };
}

function run(seq: number, status: AutoProjectRun['status'], extra: Partial<AutoProjectRun> = {}): AutoProjectRun {
  return {
    id: `r${seq}`,
    seq,
    conversation_id: `c${seq}`,
    prompt: `Prompt ${seq}`,
    status,
    summary: null,
    result: null,
    notes: null,
    handoff_missing: false,
    error: null,
    model: null,
    started_at: '2026-11-10T10:00:00Z',
    finished_at: null,
    questions: [],
    open_requests: [],
    ...extra,
  };
}

const actionCard = { type: 'action_request' } as AutoProjectRun['open_requests'][number];
const connectCard = { type: 'connect_request' } as AutoProjectRun['open_requests'][number];

function derive(overrides: Partial<AutoProjectComposerStateInput>) {
  return deriveAutoProjectComposerState({
    archived: false,
    runs: [],
    autoprojectActiveRun: null,
    starting: false,
    answering: false,
    appName: 'Quest',
    ...overrides,
  });
}

describe('deriveAutoProjectComposerState', () => {
  it('starts the next update with no active run, naming the app in the placeholder', () => {
    const state = derive({ runs: [run(1, 'completed')], appName: 'Atlas' });
    expect(state).toMatchObject({
      mode: 'start',
      busy: false,
      disabled: false,
      stopConversationId: null,
      placeholder: 'What should Atlas do next?',
      label: null,
      question: null,
      activeRun: null,
    });
  });

  it('locks without Stop while a start is in flight', () => {
    expect(derive({ starting: true })).toMatchObject({
      mode: 'start', busy: true, disabled: false, stopConversationId: null, placeholder: 'Starting...',
    });
  });

  it('answers the only pending question with "Answering the question"', () => {
    const q = question('h1');
    const active = run(2, 'waiting', { questions: [q] });
    const state = derive({ runs: [run(1, 'completed'), active] });
    expect(state).toMatchObject({
      mode: 'answer',
      busy: false,
      disabled: false,
      label: 'Answering the question',
      placeholder: ANSWER_PLACEHOLDER,
      question: q,
      activeRun: active,
    });
  });

  it('answers the oldest of several pending questions, skipping answered ones', () => {
    const active = run(1, 'waiting', {
      questions: [question('h0', 'answered'), question('h1'), question('h2')],
    });
    const state = derive({ runs: [active] });
    expect(state.label).toBe('Answering the oldest question');
    expect(state.question?.handle_id).toBe('h1');
  });

  it('is busy while an answer is in flight', () => {
    const state = derive({ runs: [run(1, 'waiting', { questions: [question('h1')] })], answering: true });
    expect(state).toMatchObject({ mode: 'answer', busy: true });
  });

  it.each([
    ['running', [], 'Working...'],
    ['waiting', [actionCard, connectCard], 'Waiting for your approval'],
    ['waiting', [connectCard], 'Waiting for you to connect a service'],
    ['waiting', [], 'Waiting for you'],
  ] as const)('locks with Stop on a %s run (%#)', (status, cards, placeholder) => {
    const active = run(3, status, { open_requests: [...cards] });
    const state = derive({ runs: [run(1, 'completed'), active] });
    expect(state).toMatchObject({
      mode: 'locked',
      busy: false,
      disabled: true,
      stopConversationId: 'c3',
      placeholder,
      label: null,
      activeRun: active,
    });
  });

  it('a running run never offers an answer even with a pending question listed', () => {
    const state = derive({ runs: [run(1, 'running', { questions: [question('h1')] })] });
    expect(state.mode).toBe('locked');
    expect(state.question).toBeNull();
  });

  it.each([
    ['running', 'Working...'],
    ['waiting', 'Waiting for you'],
  ] as const)('falls back to the detail active_run (%s) while that run is not loaded, without Stop', (status, placeholder) => {
    const state = derive({
      runs: [run(1, 'completed')],
      autoprojectActiveRun: { id: 'r2', seq: 2, status },
    });
    expect(state).toMatchObject({
      mode: 'locked', busy: true, disabled: false, stopConversationId: null, placeholder,
    });
  });

  it('ignores a stale detail active_run once the loaded runs show it finished', () => {
    const state = derive({
      runs: [run(1, 'completed')],
      autoprojectActiveRun: { id: 'r1', seq: 1, status: 'running' },
    });
    expect(state.mode).toBe('start');
  });

  it('shows no composer for an archived AutoProject, whatever the runs', () => {
    const state = derive({
      archived: true,
      runs: [run(1, 'waiting', { questions: [question('h1')] })],
      starting: true,
    });
    expect(state).toMatchObject({
      mode: 'archived', busy: true, placeholder: 'This AutoProject is archived.', question: null,
    });
  });
});

describe('helpers', () => {
  it('findActiveRun returns the newest running or waiting run', () => {
    expect(findActiveRun([])).toBeNull();
    expect(findActiveRun([run(1, 'completed'), run(2, 'failed'), run(3, 'stopped')])).toBeNull();
    expect(findActiveRun([run(1, 'running'), run(2, 'completed'), run(3, 'waiting')])?.seq).toBe(3);
  });

  it('oldestPendingQuestion skips answered and cancelled questions', () => {
    expect(oldestPendingQuestion(run(1, 'waiting'))).toBeNull();
    const r = run(1, 'waiting', { questions: [question('a', 'cancelled'), question('b', 'answered'), question('c')] });
    expect(oldestPendingQuestion(r)?.handle_id).toBe('c');
  });

  it('waitingOnPhrase prefers an approval card over a connect card', () => {
    expect(waitingOnPhrase(run(1, 'waiting', { open_requests: [connectCard, actionCard] }))).toBe('your approval');
  });
});
