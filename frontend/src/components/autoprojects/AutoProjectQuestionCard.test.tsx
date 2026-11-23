// A run's question on the AutoProject page: answering by a suggested
// option, the answered and cancelled states, a 409 question_resolved
// (answered elsewhere or cancelled by a stop) and other failures. Typed
// answers go through the page's composer (AutoProjectMainColumn tests).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ApiClientError } from '../../api/request';
import { AutoProjectQuestionCard } from './AutoProjectQuestionCard';
import { makeQuestion } from './autoprojectTestFixtures';

const mocks = vi.hoisted(() => ({ answerAutoProjectQuestion: vi.fn() }));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  answerAutoProjectQuestion: mocks.answerAutoProjectQuestion,
}));

const WITH_OPTIONS = makeQuestion({ options: ['Q3', 'Q4'] });

function resolvedError(detail: Record<string, unknown>) {
  return new ApiClientError('Question already resolved', 409, 'question_resolved', undefined, {
    detail: { error: 'question_resolved', message: 'Question already resolved', ...detail },
  });
}

beforeEach(() => {
  mocks.answerAutoProjectQuestion.mockReset();
});

afterEach(cleanup);

describe('AutoProjectQuestionCard', () => {
  it('answers with a suggested option and shows the answer before the page refetches', async () => {
    mocks.answerAutoProjectQuestion.mockResolvedValue(
      makeQuestion({ status: 'answered', answer: 'Q3', answered_at: '2026-11-10T11:00:00' }),
    );
    const onAnswered = vi.fn();
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={WITH_OPTIONS} onAnswered={onAnswered} />);
    expect(screen.getByText('Or type your answer in the box above.')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Q3' }));
    expect(mocks.answerAutoProjectQuestion).toHaveBeenCalledWith('ap1', 'h1', 'Q3');
    await screen.findByText('Answered:');
    expect(screen.getByText('Q3')).toBeTruthy();
    expect(screen.queryByRole('group', { name: 'Suggested answers' })).toBeNull();
    expect(onAnswered).toHaveBeenCalledTimes(1);
    // The clicked option unmounted: the card keeps focus. It takes it in an
    // effect after the answer renders (outside act()), so wait for it.
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('region', { name: 'Question' })),
    );
  });

  it('locks every option while one is being sent', async () => {
    let resolve: (value: unknown) => void = () => {};
    mocks.answerAutoProjectQuestion.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={WITH_OPTIONS} />);
    fireEvent.click(screen.getByRole('button', { name: 'Q4' }));
    const buttons = screen.getAllByRole('button') as HTMLButtonElement[];
    expect(buttons.every((b) => b.disabled)).toBe(true);
    expect(screen.getByRole('button', { name: 'Q4' }).getAttribute('aria-busy')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Q3' }));
    expect(mocks.answerAutoProjectQuestion).toHaveBeenCalledTimes(1);
    await act(async () => resolve(makeQuestion({ status: 'answered', answer: 'Q4' })));
  });

  it('points at the composer for a question without options', () => {
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={makeQuestion()} />);
    expect(screen.getByText('Type your answer in the box above.')).toBeTruthy();
    expect(screen.queryByRole('group', { name: 'Suggested answers' })).toBeNull();
  });

  it.each([
    ['no AutoProject to answer through', { autoprojectId: null }],
    ['the composer sending an answer', { disabled: true }],
  ])('disables the options with %s', (_name, props) => {
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={WITH_OPTIONS} {...props} />);
    expect((screen.getByRole('button', { name: 'Q3' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows an answered question with its answer and no options', () => {
    render(
      <AutoProjectQuestionCard
        autoprojectId="ap1"
        question={makeQuestion({ options: ['Q3'], status: 'answered', answer: 'Neither' })}
      />,
    );
    expect(screen.getByText('Neither')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Q3' })).toBeNull();
  });

  it('shows a cancelled question as stopped before it was answered', () => {
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={makeQuestion({ status: 'cancelled' })} />);
    expect(screen.getByText('Cancelled: the update was stopped before you answered.')).toBeTruthy();
  });

  it.each([
    ['answered elsewhere', { status: 'answered', answer: 'Q4' }, 'Q4'],
    ['cancelled by a stop', { status: 'cancelled' }, 'Cancelled: the update was stopped before you answered.'],
  ])('takes a 409 question_resolved (%s) as the outcome', async (_name, detail, text) => {
    mocks.answerAutoProjectQuestion.mockRejectedValue(resolvedError(detail));
    const onAnswered = vi.fn();
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={WITH_OPTIONS} onAnswered={onAnswered} />);
    fireEvent.click(screen.getByRole('button', { name: 'Q3' }));
    await screen.findByText(text);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(onAnswered).toHaveBeenCalledTimes(1);
  });

  it('shows any other failure inline and lets the user try again', async () => {
    mocks.answerAutoProjectQuestion.mockRejectedValueOnce(
      new ApiClientError('The answer is too long.', 400, 'invalid_answer'),
    );
    const onAnswered = vi.fn();
    render(<AutoProjectQuestionCard autoprojectId="ap1" question={WITH_OPTIONS} onAnswered={onAnswered} />);
    fireEvent.click(screen.getByRole('button', { name: 'Q3' }));
    expect((await screen.findByRole('alert')).textContent).toBe('The answer is too long.');
    expect(onAnswered).not.toHaveBeenCalled();

    mocks.answerAutoProjectQuestion.mockResolvedValueOnce(makeQuestion({ status: 'answered', answer: 'Q3' }));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Q3' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Q3' }));
    await screen.findByText('Answered:');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('lets the refetched question win over the local outcome', async () => {
    mocks.answerAutoProjectQuestion.mockResolvedValue(makeQuestion({ status: 'answered', answer: 'Q3' }));
    const { rerender } = render(<AutoProjectQuestionCard autoprojectId="ap1" question={WITH_OPTIONS} />);
    fireEvent.click(screen.getByRole('button', { name: 'Q3' }));
    await screen.findByText('Answered:');

    rerender(
      <AutoProjectQuestionCard
        autoprojectId="ap1"
        question={makeQuestion({ options: ['Q3', 'Q4'], status: 'answered', answer: 'Q3 (from the server)' })}
      />,
    );
    expect(screen.getByText('Q3 (from the server)')).toBeTruthy();
  });
});
