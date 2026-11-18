// Starting and stopping an AutoProject run: POST the run's conversation, then
// one send_message on it, settled by the server's direct answer (accepted,
// rejected, error) or the receipt timeout. Every failure is an
// AutoProjectRunStartError whose reason maps to one sentence for the UI.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/request';
import {
  AutoProjectRunStartError,
  autoprojectRunStartErrorMessage,
  startAutoProjectRun,
  stopAutoProjectRun,
} from './autoprojectRuns';
import { SEND_ACK_TIMEOUT_MS } from './sendAck';

type Handler = (event: Record<string, unknown>) => void;

const mocks = vi.hoisted(() => ({
  connected: true,
  sendResult: true,
  sent: [] as Record<string, unknown>[],
  handlers: new Map<string, Set<Handler>>(),
  createAutoProjectRunConversation: vi.fn(),
}));

vi.mock('./PersistentWebSocket', () => ({
  persistentWebSocket: {
    get connected() {
      return mocks.connected;
    },
    send: (payload: Record<string, unknown>) => {
      if (mocks.sendResult) mocks.sent.push(payload);
      return mocks.sendResult;
    },
    onConversationEvent: (id: string, handler: Handler) => {
      const set = mocks.handlers.get(id) ?? new Set<Handler>();
      set.add(handler);
      mocks.handlers.set(id, set);
      return () => set.delete(handler);
    },
  },
}));

vi.mock('../api/autoprojectsApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/autoprojectsApi')>()),
  createAutoProjectRunConversation: mocks.createAutoProjectRunConversation,
}));

function emit(conversationId: string, event: Record<string, unknown>) {
  for (const handler of [...(mocks.handlers.get(conversationId) ?? [])]) handler(event);
}

function listeners(conversationId: string): number {
  return mocks.handlers.get(conversationId)?.size ?? 0;
}

/** Let the POST and beforeSend settle so the send has gone out. */
async function flush() {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
}

async function rejection(promise: Promise<unknown>): Promise<AutoProjectRunStartError> {
  try {
    await promise;
  } catch (err) {
    expect(err).toBeInstanceOf(AutoProjectRunStartError);
    return err as AutoProjectRunStartError;
  }
  throw new Error('expected a rejection');
}

beforeEach(() => {
  mocks.connected = true;
  mocks.sendResult = true;
  mocks.sent.length = 0;
  mocks.handlers.clear();
  mocks.createAutoProjectRunConversation.mockReset().mockResolvedValue({ conversation_id: 'conv-1' });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('startAutoProjectRun', () => {
  it('creates the conversation, sends the prompt once and resolves on its receipt', async () => {
    const pending = startAutoProjectRun('ap1', {
      message: 'Draft the plan',
      model: 'm1',
      thinkingEffort: 'high',
      skillIds: ['s1'],
    });
    await flush();
    expect(mocks.createAutoProjectRunConversation).toHaveBeenCalledWith('ap1');
    expect(mocks.sent).toHaveLength(1);
    const [payload] = mocks.sent;
    expect(payload).toMatchObject({
      op: 'send_message',
      conversation_id: 'conv-1',
      message: 'Draft the plan',
      model: 'm1',
      thinking_effort: 'high',
      skill_ids: ['s1'],
    });
    expect(typeof payload.client_send_id).toBe('string');

    // A receipt for another send is not ours.
    emit('conv-1', { type: 'send_message_accepted', client_send_id: 'someone-else', seq: 9 });
    emit('conv-1', { type: 'send_message_accepted', client_send_id: payload.client_send_id, seq: 2 });
    await expect(pending).resolves.toEqual({ conversationId: 'conv-1', seq: 2 });
    expect(listeners('conv-1')).toBe(0);
  });

  it('omits the optional fields it was not given, and the thinking level without a model', async () => {
    const pending = startAutoProjectRun('ap1', { message: 'Go', thinkingEffort: 'high', skillIds: [] });
    await flush();
    const [payload] = mocks.sent;
    expect(payload).not.toHaveProperty('model');
    expect(payload).not.toHaveProperty('thinking_effort');
    expect(payload).not.toHaveProperty('skill_ids');
    expect(payload).not.toHaveProperty('attachments');
    expect(payload).not.toHaveProperty('attached_filenames');
    emit('conv-1', { type: 'send_message_accepted', client_send_id: payload.client_send_id, seq: 1 });
    await pending;
  });

  it('uploads in beforeSend on the new conversation and sends what it returns', async () => {
    const beforeSend = vi.fn(async () => ({
      attachedFilenames: ['a.csv'],
      attachments: [{ id: 'img1' } as never],
    }));
    const pending = startAutoProjectRun('ap1', { message: 'Look', beforeSend });
    await flush();
    expect(beforeSend).toHaveBeenCalledWith('conv-1');
    expect(mocks.sent[0]).toMatchObject({ attached_filenames: ['a.csv'], attachments: [{ id: 'img1' }] });
    emit('conv-1', { type: 'send_message_accepted', client_send_id: mocks.sent[0].client_send_id, seq: 1 });
    await pending;
  });

  it.each([
    ['an empty prompt', { message: '   ' }, true, 'invalid_prompt'],
    ['a closed socket', { message: 'Go' }, false, 'not_connected'],
  ])('refuses %s before creating a conversation', async (_name, opts, connected, reason) => {
    mocks.connected = connected;
    const err = await rejection(startAutoProjectRun('ap1', opts));
    expect(err.reason).toBe(reason);
    expect(err.conversationId).toBeNull();
    expect(mocks.createAutoProjectRunConversation).not.toHaveBeenCalled();
    expect(mocks.sent).toEqual([]);
  });

  it('carries the server code and active run of a refused POST', async () => {
    mocks.createAutoProjectRunConversation.mockRejectedValue(
      new ApiClientError('busy', 409, 'autoproject_run_active', undefined, {
        detail: { error: 'autoproject_run_active', active_run: { id: 'r3', seq: 3, status: 'running' } },
      }),
    );
    const err = await rejection(startAutoProjectRun('ap1', { message: 'Go' }));
    expect(err.reason).toBe('autoproject_run_active');
    expect(err.activeRun).toEqual({ id: 'r3', seq: 3, status: 'running' });
    expect(autoprojectRunStartErrorMessage(err)).toBe('Another update of this AutoProject is still in progress.');
    expect(mocks.sent).toEqual([]);
  });

  it('a failing beforeSend aborts the start with the conversation marked spent', async () => {
    const err = await rejection(startAutoProjectRun('ap1', {
      message: 'Go',
      beforeSend: async () => {
        throw new Error('Pasted images could not be uploaded: too big');
      },
    }));
    expect(err).toMatchObject({ reason: 'start_failed', conversationId: 'conv-1' });
    expect(autoprojectRunStartErrorMessage(err)).toBe('Pasted images could not be uploaded: too big');
    expect(mocks.sent).toEqual([]);
  });

  it('rejects with the reason, message and active run of send_message_rejected', async () => {
    const pending = startAutoProjectRun('ap1', { message: 'Go' });
    await flush();
    emit('conv-1', {
      type: 'send_message_rejected',
      reason: 'autoproject_run_active',
      active_run: { id: 'r2', seq: 2, status: 'waiting' },
    });
    const err = await rejection(pending);
    expect(err).toMatchObject({
      reason: 'autoproject_run_active',
      conversationId: 'conv-1',
      activeRun: { id: 'r2', seq: 2, status: 'waiting' },
      message: 'Another update of this AutoProject is still in progress.',
    });
    expect(listeners('conv-1')).toBe(0);
  });

  it('rejects on an error envelope for the conversation', async () => {
    const pending = startAutoProjectRun('ap1', { message: 'Go' });
    await flush();
    emit('conv-1', { type: 'error', message: 'Prompt too long' });
    const err = await rejection(pending);
    expect(err).toMatchObject({ reason: 'start_failed', message: 'Prompt too long' });
    expect(autoprojectRunStartErrorMessage(err)).toBe('Prompt too long');
  });

  it('is not_connected when the send cannot be written', async () => {
    mocks.sendResult = false;
    const err = await rejection(startAutoProjectRun('ap1', { message: 'Go' }));
    expect(err).toMatchObject({ reason: 'not_connected', conversationId: 'conv-1' });
    expect(listeners('conv-1')).toBe(0);
  });

  it('is unconfirmed when no receipt arrives in time, and ignores a late one', async () => {
    vi.useFakeTimers();
    const pending = startAutoProjectRun('ap1', { message: 'Go' });
    const settled = rejection(pending);
    await vi.advanceTimersByTimeAsync(0);
    expect(mocks.sent).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(SEND_ACK_TIMEOUT_MS - 1);
    expect(listeners('conv-1')).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    const err = await settled;
    expect(err.reason).toBe('unconfirmed');
    expect(autoprojectRunStartErrorMessage(err)).toBe(
      'The server did not confirm the message. Check the AutoProject before sending again.',
    );
    expect(listeners('conv-1')).toBe(0);
  });
});

describe('autoprojectRunStartErrorMessage', () => {
  it.each([
    ['autoprojects_disabled', 'AutoProjects are not enabled for your account.'],
    ['autoproject_archived', 'This AutoProject is archived. Unarchive it to send a prompt.'],
    ['autoproject_run_single_message', 'This update already has its prompt. Send the next prompt from the AutoProject.'],
    ['autoproject_run_start_failed', 'The prompt could not be sent. Try again.'],
  ])('%s', (reason, sentence) => {
    expect(autoprojectRunStartErrorMessage(new AutoProjectRunStartError(reason, 'raw'))).toBe(sentence);
  });

  it('falls back to the error message for an unknown reason', () => {
    expect(autoprojectRunStartErrorMessage(new AutoProjectRunStartError('weird', 'Server said no'))).toBe('Server said no');
  });
});

describe('stopAutoProjectRun', () => {
  it('sends the stop op on the run conversation and reports a closed socket', () => {
    expect(stopAutoProjectRun('conv-9')).toBe(true);
    expect(mocks.sent).toEqual([{ op: 'stop', conversation_id: 'conv-9' }]);
    mocks.sendResult = false;
    expect(stopAutoProjectRun('conv-9')).toBe(false);
  });
});
