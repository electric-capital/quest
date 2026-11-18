/**
 * Starting and stopping an AutoProject run.
 *
 * A run starts in two steps (see chat/autoprojects/routes.py and
 * `_handle_send_message` in chat/realtime/socket.py):
 *
 * 1. POST /autoprojects/{id}/runs creates the run's conversation.
 * 2. The first `send_message` on that conversation starts the run: the AutoProject
 *    service checks the gate, the archive flag, the one-active-run rule and
 *    the model BEFORE the user row is appended, so a refused send leaves
 *    nothing behind but the empty conversation.
 *
 * `startAutoProjectRun` does both and settles on the server's direct answer to the
 * sending socket: `send_message_accepted` (matched by `client_send_id`; its
 * `seq` is the user row's message seq) resolves, `send_message_rejected` or
 * an `error` envelope for the conversation rejects. It listens on the
 * conversation channel only for that answer and does NOT subscribe to the
 * conversation or touch webSocketManager / conversationStore: how the
 * page follows the run (autoproject_changed, or a subscription for live
 * progress) is the caller's business.
 *
 * A conversation whose send failed is spent: the server takes exactly one
 * message per run conversation, and an unconfirmed send may still have
 * landed. Every call therefore creates a fresh conversation; never retry a
 * send on the conversation of a failed start (on `unconfirmed`, check the
 * AutoProject's updates before starting again).
 *
 * `startAutoProjectRun` rejects with an AutoProjectRunStartError only; its `reason` is the
 * server's code where there is one, and `autoprojectRunStartErrorMessage` turns it
 * into one sentence for the UI.
 */

import { createAutoProjectRunConversation, parseAutoProjectActiveRun, autoprojectActiveRunFromError } from '../api/autoprojectsApi';
import { ApiClientError } from '../api/request';
import type { ComposerAttachmentRef, AutoProjectActiveRun } from '../api/types';
import { persistentWebSocket, type PersistentEvent } from './PersistentWebSocket';

/**
 * How long to wait for the `send_message_accepted` receipt before the
 * start counts as unconfirmed; the same deadline as WebSocketManager's
 * SEND_ACK_TIMEOUT_MS (the server appends the user row and answers well
 * within a second on a healthy path).
 */
export const AUTOPROJECT_RUN_SEND_ACK_TIMEOUT_MS = 15_000;

/**
 * Why a start failed: a server refusal code (REST or `send_message_rejected`,
 * e.g. `autoproject_run_start_failed`), or one of the client-side reasons
 * `not_connected` (the socket is closed; nothing was sent), `unconfirmed`
 * (sent, but no receipt in time: it may or may not have landed) and
 * `start_failed` (anything else).
 */
export type AutoProjectRunStartReason =
  | 'autoprojects_disabled'
  | 'autoproject_not_found'
  | 'autoproject_archived'
  | 'autoproject_run_active'
  | 'autoproject_run_single_message'
  | 'model_not_allowed'
  | 'invalid_prompt'
  | 'not_connected'
  | 'unconfirmed'
  | 'start_failed'
  | (string & {});

export class AutoProjectRunStartError extends Error {
  reason: AutoProjectRunStartReason;
  // The AutoProject's active run, for `autoproject_run_active` when the server named it.
  activeRun: AutoProjectActiveRun | null;
  // The run conversation, once it was created (spent: never send on it again).
  conversationId: string | null;

  constructor(
    reason: AutoProjectRunStartReason,
    message: string,
    extras: { activeRun?: AutoProjectActiveRun | null; conversationId?: string | null } = {},
  ) {
    super(message);
    this.name = 'AutoProjectRunStartError';
    this.reason = reason;
    this.activeRun = extras.activeRun ?? null;
    this.conversationId = extras.conversationId ?? null;
  }
}

export interface StartAutoProjectRunOptions {
  message: string;
  // Model and thinking level for this run; omitted / null = the AutoProject's default.
  // The thinking level is sent only together with a model, as for chats.
  model?: string | null;
  thinkingEffort?: string | null;
  // Skills to load into the run (the composer's queued skills); sent as
  // skill_ids when non-empty.
  skillIds?: string[];
  // Runs after the run conversation exists and before the send (upload
  // attachments to the conversation workspace here); may return the names
  // to send as attached_filenames and the pasted-image refs to send as
  // attachments (from uploadComposerAttachments).
  beforeSend?: (conversationId: string) => Promise<AutoProjectRunBeforeSendResult | void>;
}

export interface AutoProjectRunBeforeSendResult {
  attachedFilenames?: string[];
  attachments?: ComposerAttachmentRef[];
}

export interface StartedAutoProjectRun {
  conversationId: string;
  // The user row's message seq in the run conversation.
  seq: number;
}

/** Fallback text of an AutoProjectRunStartError that has no better message. */
const START_FAILED_MESSAGE = 'The prompt could not be sent. Try again.';

/** The one sentence the UI shows for each known reason. */
const REASON_MESSAGES: Record<string, string> = {
  autoprojects_disabled: 'AutoProjects are not enabled for your account.',
  autoproject_not_found: 'This AutoProject no longer exists.',
  autoproject_archived: 'This AutoProject is archived. Unarchive it to send a prompt.',
  autoproject_run_active: 'Another update of this AutoProject is still in progress.',
  autoproject_run_single_message: 'This update already has its prompt. Send the next prompt from the AutoProject.',
  model_not_allowed: 'The selected model is not available here. Pick another model.',
  invalid_prompt: 'Write a prompt first.',
  not_connected: 'Not connected to the server. Your message was not sent.',
  unconfirmed: 'The server did not confirm the message. Check the AutoProject before sending again.',
  autoproject_run_start_failed: START_FAILED_MESSAGE,
};

/** Random id for the send; crypto.randomUUID only exists in secure contexts. */
function newClientSendId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function errorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : START_FAILED_MESSAGE;
}

/**
 * Start a run of the AutoProject with `opts.message` as its prompt. Resolves once
 * the server has stored the message (the run is live); rejects with a
 * AutoProjectRunStartError only.
 */
export async function startAutoProjectRun(
  autoprojectId: string,
  opts: StartAutoProjectRunOptions,
): Promise<StartedAutoProjectRun> {
  // Refused up front, before a conversation is spent: the server answers an
  // empty message with an `error` envelope that carries no conversation_id,
  // which the conversation listener below would never see.
  if (!opts.message.trim()) {
    throw new AutoProjectRunStartError('invalid_prompt', REASON_MESSAGES.invalid_prompt);
  }
  if (!persistentWebSocket.connected) {
    throw new AutoProjectRunStartError('not_connected', REASON_MESSAGES.not_connected);
  }

  let conversationId: string;
  try {
    conversationId = (await createAutoProjectRunConversation(autoprojectId)).conversation_id;
  } catch (err) {
    const reason = err instanceof ApiClientError && err.errorCode ? err.errorCode : 'start_failed';
    throw new AutoProjectRunStartError(reason, errorMessage(err), {
      activeRun: autoprojectActiveRunFromError(err),
    });
  }

  let attachedFilenames: string[] | undefined;
  let attachments: ComposerAttachmentRef[] | undefined;
  if (opts.beforeSend) {
    try {
      const result = await opts.beforeSend(conversationId);
      attachedFilenames = result?.attachedFilenames;
      attachments = result?.attachments;
    } catch (err) {
      throw new AutoProjectRunStartError('start_failed', errorMessage(err), { conversationId });
    }
  }

  const clientSendId = newClientSendId();
  const payload: Record<string, unknown> = {
    op: 'send_message',
    conversation_id: conversationId,
    message: opts.message,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    ...(opts.model && { model: opts.model }),
    ...(opts.model && opts.thinkingEffort && { thinking_effort: opts.thinkingEffort }),
    ...(opts.skillIds && opts.skillIds.length > 0 && { skill_ids: opts.skillIds }),
    ...(attachments && attachments.length > 0 && { attachments }),
    ...(attachedFilenames && attachedFilenames.length > 0 && {
      attached_filenames: attachedFilenames,
    }),
    client_send_id: clientSendId,
  };

  return new Promise<StartedAutoProjectRun>((resolve, reject) => {
    let settled = false;
    let timer: number | null = null;
    const fail = (reason: AutoProjectRunStartReason, message: string, activeRun?: AutoProjectActiveRun | null) =>
      new AutoProjectRunStartError(reason, message, { activeRun, conversationId });

    // Registered before the send: the receipt can never beat the listener.
    const unsubscribe = persistentWebSocket.onConversationEvent(
      conversationId,
      (event: PersistentEvent) => {
        if (event.type === 'send_message_accepted') {
          if (event.client_send_id !== clientSendId) return;
          const seq = typeof event.seq === 'number' ? event.seq : 0;
          settle(() => resolve({ conversationId, seq }));
          return;
        }
        if (event.type === 'send_message_rejected') {
          // The conversation is fresh, so a rejection on it is ours.
          const reason = typeof event.reason === 'string' && event.reason
            ? event.reason
            : 'start_failed';
          const message = typeof event.message === 'string' && event.message
            ? event.message
            : REASON_MESSAGES[reason] ?? START_FAILED_MESSAGE;
          settle(() => reject(fail(reason, message, parseAutoProjectActiveRun(event.active_run))));
          return;
        }
        if (event.type === 'error') {
          const message = typeof event.message === 'string' && event.message
            ? event.message
            : START_FAILED_MESSAGE;
          settle(() => reject(fail('start_failed', message)));
        }
      },
    );

    function settle(finish: () => void): void {
      if (settled) return;
      settled = true;
      unsubscribe();
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
      finish();
    }

    if (!persistentWebSocket.send(payload)) {
      settle(() => reject(fail('not_connected', REASON_MESSAGES.not_connected)));
      return;
    }
    // The frame is in the browser's buffer, which is not proof it reached
    // the server (a half-open socket accepts writes): wait for the receipt.
    timer = window.setTimeout(() => {
      settle(() => reject(fail('unconfirmed', REASON_MESSAGES.unconfirmed)));
    }, AUTOPROJECT_RUN_SEND_ACK_TIMEOUT_MS);
  });
}

/**
 * One user-facing sentence for a failed start: the fixed sentence of a
 * known reason, else the error's own message (a `start_failed` carries the
 * server's or the upload's message).
 */
export function autoprojectRunStartErrorMessage(err: unknown): string {
  if (err instanceof AutoProjectRunStartError) {
    const known = REASON_MESSAGES[err.reason];
    if (known) return known;
  }
  return errorMessage(err);
}

/**
 * Stop the run on its conversation (the existing `stop` op; works while the
 * run is `waiting` too). False when the socket is closed and nothing was sent.
 */
export function stopAutoProjectRun(conversationId: string): boolean {
  return persistentWebSocket.send({ op: 'stop', conversation_id: conversationId });
}
