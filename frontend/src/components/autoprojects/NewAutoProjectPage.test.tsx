// The New AutoProject page: its caption, the composer placeholder naming the
// app, and the first Send creating the AutoProject (POST /autoprojects with
// the prompt), then the run conversation (POST /autoprojects/{id}/runs), then
// the `send_message` on the socket, before it navigates to the new page. A
// refused create shows inline and starts nothing. The composer is stubbed:
// AutoProjectComposer has its own tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { ApiClientError } from '../../api/request';
import type { AutoProjectComposerSubmission } from './autoprojectComposerSend';
import { NewAutoProjectPage } from './NewAutoProjectPage';

type Handler = (event: Record<string, unknown>) => void;

const mocks = vi.hoisted(() => ({
  log: [] as string[],
  createAutoProject: vi.fn(),
  createAutoProjectRunConversation: vi.fn(),
  sent: [] as Record<string, unknown>[],
  conversationHandlers: new Map<string, Set<(event: Record<string, unknown>) => void>>(),
  setQueuedSkills: vi.fn(),
}));

vi.mock('../../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/autoprojectsApi')>()),
  createAutoProject: mocks.createAutoProject,
  createAutoProjectRunConversation: mocks.createAutoProjectRunConversation,
}));

vi.mock('../../services/PersistentWebSocket', () => ({
  persistentWebSocket: {
    connected: true,
    send: (payload: Record<string, unknown>) => {
      mocks.log.push(`ws:${String(payload.op)}`);
      mocks.sent.push(payload);
      return true;
    },
    onConversationEvent: (conversationId: string, handler: Handler) => {
      const set = mocks.conversationHandlers.get(conversationId) ?? new Set<Handler>();
      set.add(handler);
      mocks.conversationHandlers.set(conversationId, set);
      return () => set.delete(handler);
    },
  },
}));

vi.mock('../../contexts/AppConfigContext', () => ({
  useAppConfig: () => ({ appName: 'Acme' }),
}));

vi.mock('../../contexts/ConversationSkillsContext', () => ({
  useConversationSkills: () => ({ setQueuedSkillsForConversation: mocks.setQueuedSkills }),
}));

vi.mock('./AutoProjectComposer', async () => {
  const { useState } = await import('react');
  return {
    AutoProjectComposer: ({ onSubmit, placeholder, busy }: {
      onSubmit: (s: AutoProjectComposerSubmission) => void;
      placeholder?: string;
      busy?: boolean;
    }) => {
      const [text, setText] = useState('');
      return (
        <div>
          <textarea placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)} />
          <button
            type="button"
            disabled={busy}
            onClick={() => onSubmit({
              text: text.trim(), model: null, thinkingEffort: null, skillIds: ['s1'], files: [], imageFiles: [],
            })}
          >
            Send
          </button>
        </div>
      );
    },
  };
});

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/autoprojects/new']}>
      <NewAutoProjectPage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

function typeAndSend(text: string) {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
}

beforeEach(() => {
  mocks.log = [];
  mocks.sent = [];
  mocks.conversationHandlers.clear();
  mocks.createAutoProject.mockReset().mockImplementation(async () => {
    mocks.log.push('POST /autoprojects');
    return { id: 'ap1', title: 'Plan the offsite' };
  });
  mocks.createAutoProjectRunConversation.mockReset().mockImplementation(async (id: string) => {
    mocks.log.push(`POST /autoprojects/${id}/runs`);
    return { conversation_id: 'run-c1' };
  });
  mocks.setQueuedSkills.mockReset();
});

afterEach(cleanup);

describe('NewAutoProjectPage', () => {
  it('explains AutoProjects and asks for the goal by the app name', () => {
    renderPage();
    expect(screen.getByRole('heading', { name: 'New AutoProject' })).toBeTruthy();
    expect(screen.getByText(
      'AutoProjects are a new way to work with an agent across many iterations on very large pieces '
      + 'of work. Notes and files about the AutoProject are accumulated automatically.',
    )).toBeTruthy();
    expect(screen.getByRole('textbox').getAttribute('placeholder')).toBe(
      'Describe the goal of the AutoProject and what Acme should do first',
    );
  });

  it('creates the AutoProject, then its run, then sends the prompt, then opens the page', async () => {
    renderPage();
    typeAndSend('  Plan the offsite in March  ');

    await waitFor(() => expect(mocks.sent).toHaveLength(1));
    expect(mocks.log).toEqual(['POST /autoprojects', 'POST /autoprojects/ap1/runs', 'ws:send_message']);
    expect(mocks.createAutoProject).toHaveBeenCalledWith({ prompt: 'Plan the offsite in March' });
    expect(mocks.sent[0]).toMatchObject({
      op: 'send_message',
      conversation_id: 'run-c1',
      message: 'Plan the offsite in March',
      skill_ids: ['s1'],
    });
    // Still on the New page until the server accepts the message.
    expect(screen.getByTestId('location').textContent).toBe('/autoprojects/new');

    act(() => {
      for (const handler of mocks.conversationHandlers.get('run-c1') ?? []) {
        handler({
          type: 'send_message_accepted',
          conversation_id: 'run-c1',
          client_send_id: mocks.sent[0].client_send_id,
          seq: 1,
        });
      }
    });
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/autoprojects/ap1'));
  });

  it('shows a refused create inline, puts the skills back and starts no run', async () => {
    mocks.createAutoProject.mockReset().mockRejectedValue(
      new ApiClientError('You have reached the limit of 200 active AutoProjects.', 409, 'autoprojects_cap_reached'),
    );
    renderPage();
    typeAndSend('Another one');

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('You have reached the limit of 200 active AutoProjects.');
    expect(mocks.createAutoProjectRunConversation).not.toHaveBeenCalled();
    expect(mocks.sent).toEqual([]);
    expect(mocks.setQueuedSkills).toHaveBeenCalledWith(expect.any(String), ['s1']);
    expect(screen.getByTestId('location').textContent).toBe('/autoprojects/new');
  });

  it('asks for a prompt when Send carries none', () => {
    renderPage();
    typeAndSend('   ');
    expect(screen.getByRole('alert').textContent).toContain('Write a prompt to start the AutoProject.');
    expect(mocks.createAutoProject).not.toHaveBeenCalled();
  });
});
