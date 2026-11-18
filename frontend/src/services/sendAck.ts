/**
 * Pieces shared by every `send_message` the client sends on the persistent
 * socket (WebSocketManager and useConversation for a chat, services/
 * autoprojectRuns.ts for an AutoProject run).
 */

/**
 * How long to wait for the server's ``send_message_accepted`` receipt
 * before declaring a send unconfirmed. The server appends the user row
 * synchronously on receipt, so on a healthy path the receipt arrives well
 * within a second or two; a longer silence means the frame most likely
 * never left the device (half-open mobile socket, radio off).
 */
export const SEND_ACK_TIMEOUT_MS = 15_000;

/**
 * Random id for an outbound send. ``crypto.randomUUID`` is only defined in
 * secure contexts, and local instances are served over plain http on a raw
 * IP, so fall back to a time + random string there.
 */
export function newClientSendId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
