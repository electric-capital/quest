// Pieces shared by every send_message: the receipt window and the client
// send id, including its fallback outside a secure context (local instances
// on plain http have no crypto.randomUUID).
import { afterEach, describe, expect, it, vi } from 'vitest';
import { newClientSendId, SEND_ACK_TIMEOUT_MS } from './sendAck';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('sendAck', () => {
  it('waits 15 s for the send_message_accepted receipt', () => {
    expect(SEND_ACK_TIMEOUT_MS).toBe(15_000);
  });

  it('uses crypto.randomUUID when available', () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'uuid-1' });
    expect(newClientSendId()).toBe('uuid-1');
  });

  it('falls back to a unique time + random id without randomUUID', () => {
    vi.stubGlobal('crypto', {});
    const ids = new Set(Array.from({ length: 50 }, () => newClientSendId()));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(/^[0-9a-z]+-[0-9a-z]+$/);
  });

  it('falls back without any crypto global', () => {
    vi.stubGlobal('crypto', undefined);
    expect(newClientSendId()).toMatch(/^[0-9a-z]+-[0-9a-z]+$/);
  });
});
